#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用真机抓到的真实输出格式，验证 read_netdetail 的解析逻辑。

为什么需要：真机部署要 4 分钟，改一次解析逻辑就重部署一次太慢。
这里把真机 `ip -j` 的真实 JSON 喂给本地抽出来的函数，
验「能不能正确解析出界面上要显示的字段」。

真实样本来源（192.168.7.3 / 2026-10-04）：
  ip -4 -j neigh show
  ip -6 -j route show
  ip -j -6 rule show
"""
import ast
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), 'backend', 'drouter-helper.py')

fails = []
n = 0


def ck(desc, cond, extra=''):
    global n
    n += 1
    print(('ok    ' if cond else 'FAIL  ') + desc + (' ' + str(extra) if extra else ''))
    if not cond:
        fails.append(desc)


# ---- 真机原始输出（原样粘贴）----
REAL_NEIGH4 = json.loads('''[
 {"dst":"192.168.7.17","dev":"ens18","lladdr":"94:d3:31:49:f3:8e","state":["STALE"]},
 {"dst":"192.168.7.10","dev":"ens18","lladdr":"bc:24:11:b1:9d:93","state":["DELAY"]},
 {"dst":"192.168.7.99","dev":"ens18","state":["FAILED"]}
]''')
REAL_NEIGH4B = json.loads('''[
 {"dst":"192.168.7.3","dev":"lo","lladdr":"00:00:00:00:00:00","state":["PERMANENT"]}
]''')
REAL_ROUTES6 = json.loads('''[
 {"dst":"2408:8240:5416:ad61::/64","dev":"ens18","protocol":"ra","metric":100,"flags":[],"pref":"medium"},
 {"dst":"default","gateway":"fe80::be24:11ff:fef2:6262","dev":"ens18","protocol":"ra","metric":100,"flags":[],"pref":"high"}
]''')
REAL_ROUTES6_ALL = json.loads('''[
 {"dst":"2408:8240:5416:ad61::/64","dev":"ens18","protocol":"ra","metric":100},
 {"dst":"fd7a:115c:a1e0::53","dev":"tailscale0","protocol":"kernel","metric":1024,"table":"52"},
 {"dst":"::/0","gateway":"fe80::1","dev":"lan6","protocol":"static","metric":512,"prefsrc":"240e::3c4"},
 {"dst":"fe80::1","dev":"lo","protocol":"kernel","metric":256}
]''')
REAL_RULES6 = json.loads('''[
 {"priority":0,"src":"all","table":"local"},
 {"priority":5210,"src":"all","table":"all"},
 {"priority":32766,"src":"all","table":"main"}
]''')

# ---- 抽出 read_netdetail 及其依赖 ----
tree = ast.parse(open(SRC, encoding='utf-8').read())


def _ok(data, msg_cn='ok', code=''):
    """drouter-helper 的统一响应包装（同名函数，抽出来时要一起带上）。"""
    return {'ok': True, 'code': code, 'msg_cn': msg_cn, 'data': data}


ns = {'json': json, 'sh': None, 'ok': _ok}
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in (
            'read_netdetail',):
        exec(compile(ast.Module(body=[node], type_ignores=[]), SRC, 'exec'), ns)
ck('read_netdetail 抽出来了', 'read_netdetail' in ns)
if 'read_netdetail' not in ns:
    sys.exit(1)

# ---- 用假 sh() 喂真机数据 ----
CALLS = []


def make_sh(table):
    """table: {判别函数名: 返回数据}。

    ⚠️ 这里用**完整命令元组**做键，不用前缀 ——
    因为 `ip -6 -j route show` 和 `ip -6 -j rule show` 前 4 段完全一样
    （ip, -6, -j, 后接 route / rule 是第 4 段… 实际第 3 段是 -j，
    第 4 段才是 route/rule），前缀匹配会歧义。
    最初按前 3 段匹配，结果 neigh 和 route 抢同一个键。
    """
    def _sh(cmd, timeout=15, **kw):
        CALLS.append(tuple(cmd))
        full = tuple(cmd)
        for k, v in table.items():
            if full == k:
                return 0, v, ''
        # 退化：只按 (ip, family, 模式) 匹配，模式是 cmd 中第一个非选项词
        mode = next((c for c in cmd[1:] if not c.startswith('-')), '')
        fam = next((c for c in cmd[1:] if c in ('-4', '-6', '-j', '-j6')), '')
        key2 = ('ip', fam, mode)
        for k, v in table.items():
            if len(k) == 3 and key2 == k:
                return 0, v, ''
        return 1, '', 'no data'
    return _sh


def run(tbl):
    ns['sh'] = make_sh(tbl)
    return ns['read_netdetail']({})['data']


print()
print('=== 1. 邻居表：区分 STALE / FAILED / PERMANENT ===')
d = run({('ip','-4','-j','neigh','show'): json.dumps(REAL_NEIGH4 + REAL_NEIGH4B)})
ck('解析出 4 条', len(d['neigh4']) == 4, len(d['neigh4']))
a = {x['dst']: x for x in d['neigh4']}
ck('STALE 被标记（曾可达非故障）', a['192.168.7.17']['stale'] is True)
ck('DELAY 不算 stale', a['192.168.7.10']['stale'] is False)
ck('DELAY 状态名保留', a['192.168.7.10']['states'] == 'DELAY',
   a['192.168.7.10']['states'])
ck('FAILED 被标记（真的故障）', a['192.168.7.99']['failed'] is True)
ck('FAILED 无 MAC 时不编造', a['192.168.7.99']['mac'] == '')
ck('PERMANENT 被标记', a['192.168.7.3']['permanent'] is True)
ck('MAC 原样保留（大小写不变）',
   a['192.168.7.17']['mac'] == '94:d3:31:49:f3:8e')

print()
print('=== 2. IPv6 路由：只取 main 表（排除 loopback 噪音） ===')
d = run({('ip','-6','-j','route','show','table','main'): json.dumps(REAL_ROUTES6)})
_n = len(d['routes6'])
ck('取到 2 条', _n == 2, _n)
ck('default 路由被识别', any(x['dst'] == 'default' for x in d['routes6']))
g = [x for x in d['routes6'] if x['dst'] == 'default'][0]
ck('网关正确', g['gw'] == 'fe80::be24:11ff:fef2:6262', g['gw'])
ck('metric 是数字不是字符串', g['metric'] == 100, repr(g['metric']))
ck('协议名保留', g['proto'] == 'ra', g['proto'])

# 命令行必须带 table main —— 否则 lo 的 local 表条目会灌进来
# ⚠️ 元组是**精确相等**比较，原来的写法只比了前 5 段，永远不匹配。
ck('命令带 table main 过滤（排除 local 表噪音）',
   ('ip', '-6', '-j', 'route', 'show', 'table', 'main') in CALLS,
   [c for c in CALLS if len(c) > 3 and c[3] == 'route'])

print()
print('=== 3. 用户贴的那种多表路由（含 tailscale/lan6）===')
d = run({('ip','-6','-j','route','show','table','main'): json.dumps(REAL_ROUTES6_ALL)})
r = d['routes6']
ck('四条都解析', len(r) == 4, len(r))
ck('tailscale 路由保留', any('tailscale0' in x['dev'] for x in r))
ck('prefsrc 映射到 src',
   [x for x in r if x['dst'] == '::/0'][0]['src'] == '240e::3c4')
ck('表号 52 保留',
   [x for x in r if 'tailscale' in x['dev']][0]['metric'] == 1024)

print()
print('=== 4. IPv6 规则：区分内核内置与用户配置 ===')
d = run({('ip','-j','-6','rule','show'): json.dumps(REAL_RULES6)})
ck('3 条规则', len(d['rules6']) == 3, len(d['rules6']))
pri = {x['prio']: x for x in d['rules6']}
ck('优先级 0 标为内置（用户没配）', pri[0]['builtin'] is True)
ck('优先级 32766 标为内置', pri[32766]['builtin'] is True)
ck('优先级 5210 不是内置', pri[5210]['builtin'] is False)
ck('表名保留', pri[0]['table'] == 'local')

print()
print('=== 5. 空数据不崩 ===')
d = run({})
ck('邻居空列表', d['neigh4'] == [] and d['neigh6'] == [])
ck('路由空列表', d['routes4'] == [] and d['routes6'] == [])
ck('规则空列表', d['rules6'] == [])

print()
print('=== 6. 非法 JSON 不崩（sh 返回垃圾时） ===')
ns['sh'] = lambda cmd, timeout=15, **kw: (0, 'not json at all', '')
try:
    d = ns['read_netdetail']({})['data']
    ok = all(d[k] == [] for k in
             ('neigh4', 'neigh6', 'routes4', 'routes6', 'rules6'))
    ck('垃圾输出被当成空表', ok, d)
except Exception as e:
    ck('垃圾输出被当成空表', False, repr(e))

print()
print('=' * 56)
print('解析验证 %d 条，失败 %d 条' % (n, len(fails)))
for f in fails:
    print('  FAIL: %s' % f)
sys.exit(1 if fails else 0)

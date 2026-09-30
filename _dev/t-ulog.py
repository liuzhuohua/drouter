#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#11 连接跟踪与流量日志（统一日志系统）—— 纯逻辑测试。

从 drouter-helper.py 抽取 #11 相关片段，在 NS 命名空间内隔离执行。
覆盖：
  A. ULog 统一记录结构
  B. conntrack -E 事件行解析
  C. conntrack -L 连接表行解析（flow）
  D. 统一过滤（级别/源/动作/协议/地址/端口/关键字）
  E. 聚合统计
  F. 保留策略 _ulog_prune
  G. act_ulog 参数与导出格式
"""
import os
import re
import sys
import json
import time
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, 'backend', 'drouter-helper.py')
src = open(SRC, encoding='utf-8').read()

PASS, FAIL = [], []


def chk(name, got, want):
    if got == want:
        PASS.append(name)
        return True
    FAIL.append('%s：得到 %r，期望 %r' % (name, got, want))
    return False


def has(name, hay, needle, want=True):
    got = needle in (hay or '')
    if got == want:
        PASS.append(name)
        return True
    FAIL.append('%s：%r in 输出 = %s，期望 %s' % (name, needle, got, want))
    return False


def grab(pattern, name):
    m = re.search(pattern, src, re.S)
    if not m:
        raise SystemExit('抽取失败：%s' % name)
    return m.group(0)


TMP = tempfile.mkdtemp(prefix='ulog-test-')
ARCHIVE = os.path.join(TMP, 'ulog.jsonl')
CONF = os.path.join(TMP, 'log.conf')

NS = {
    're': re, 'os': os, 'json': json, 'sys': sys, 'datetime': __import__('datetime').datetime,
    'ValidateError': type('ValidateError', (Exception,), {
        '__init__': lambda self, msg, field=None: (
            setattr(self, 'msg_cn', msg), setattr(self, 'field', field))[0] or None}),
    'sh': lambda *a, **k: (1, '', 'skip'),
    'ok': lambda d=None, m=None: {'ok': True, 'data': d, 'msg_cn': m},
    'fail': lambda m, c='ERR', d=None: {'ok': False, 'code': c, 'msg_cn': m, 'data': d},
    'LOGDIR': TMP,
    'ULOG_ARCHIVE': ARCHIVE,
    'ULOG_CONF': CONF,
    '_load_setting': lambda k, d=None: d if d is not None else {},
    '_split_journal_line': lambda line: ('', line),
    'translate_fw_log': lambda msg: None,
    'read_wan_log': lambda p: {'ok': True, 'data': {'items': []}},
    '_fw_log_enabled': lambda: True,
    'read_logs': lambda p: {'ok': True, 'data': []},
}

# 抽取整个 #11 区块
seg = grab(r'ULOG_LEVELS = \[.*?(?=\ndef read_logs\(p\):)', '#11 区块')
exec(compile(seg, SRC, 'exec'), NS)

# 需要的辅助函数单独抽取
for fn in ('_split_journal_line', '_human_bytes'):
    pass  # 已由桩替代 / 在区块内

rec = NS['_ulog_rec']
parse_ev = NS['_ulog_parse_conntrack_event']
parse_tbl = NS['_ulog_parse_conntrack_table']
flt = NS['_ulog_filter']
stats = NS['_ulog_stats']
prune = NS['_ulog_prune']
act = NS['act_ulog']

# ---------------- A. 统一记录结构 ----------------
r = rec('fw', 'warn', action='DROP', proto='TCP', saddr='203.0.113.9',
        sport='54321', daddr='192.168.7.3', dport='22', iface='ens19',
        msg_cn='丢弃数据包')
for k in ('ts', 'src', 'level', 'action', 'proto', 'saddr', 'sport',
          'daddr', 'dport', 'iface', 'host', 'port', 'msg_cn', 'raw', 'extra'):
    chk('A 字段存在 %s' % k, k in r, True)
chk('A level_cn', r['level_cn'], '警告')
chk('A action 大写', r['action'], 'DROP')
chk('A proto 大写', r['proto'], 'TCP')
chk('A sport 字符串', r['sport'], '54321')
chk('A 非法 level 归一', rec('fw', 'boom')['level'], 'info')
chk('A extra 默认', r['extra'], {})
chk('A raw 截断', len(rec('fw', raw='x' * 2000)['raw']), 800)

# ---------------- B. conntrack -E 事件 ----------------
ev = ('   [NEW] tcp      6 120 SYN_SENT src=192.168.7.100 dst=1.1.1.1 '
      'sport=51234 dport=443 [UNREPLIED]')
p = parse_ev(ev)
chk('B NEW 解析', bool(p), True)
chk('B src 名', p['src'], 'conntrack')
chk('B action', p['action'], 'NEW')
chk('B proto', p['proto'], 'TCP')
chk('B saddr', p['saddr'], '192.168.7.100')
chk('B daddr', p['daddr'], '1.1.1.1')
chk('B sport', p['sport'], '51234')
chk('B dport', p['dport'], '443')
chk('B level', p['level'], 'info')
has('B 中文消息', p['msg_cn'], '新建连接')
has('B 中文消息含地址', p['msg_cn'], '192.168.7.100')

p2 = parse_ev('[DESTROY] tcp      6 src=10.0.0.5 dst=8.8.8.8 sport=1000 dport=53 [CLOSE]')
chk('B DESTROY', p2['action'], 'DESTROY')
has('B DESTROY 中文', p2['msg_cn'], '连接关闭')
chk('B 状态 extra', p2['extra']['state'], 'CLOSE')

p3 = parse_ev('[UPDATE] udp      17 src=10.0.0.5 dst=224.0.0.251 sport=5353 dport=5353')
chk('B UDP', p3['proto'], 'UDP')
chk('B UDP level', p3['level'], 'debug')

chk('B 非事件行不解析', parse_ev('this is not conntrack'), None)
chk('B 空行不解析', parse_ev(''), None)

# IPv6 事件
p4 = parse_ev('[NEW] tcp      6 src=2408:8207:1234::1 dst=2408:8888::2 sport=40000 dport=80 [SYN_SENT]')
chk('B IPv6 saddr', p4['saddr'], '2408:8207:1234::1')
chk('B IPv6 daddr', p4['daddr'], '2408:8888::2')

# ---------------- C. conntrack -L 连接表 ----------------
tbl = ('tcp      6 431999 ESTABLISHED src=192.168.7.100 dst=1.1.1.1 sport=51234 '
       'dport=443 src=1.1.1.1 dst=192.168.7.100 sport=443 dport=51234 '
       '[ASSURED] mark=0 use=1')
t = parse_tbl(tbl)
chk('C 解析成功', bool(t), True)
chk('C src 名', t['src'], 'flow')
chk('C proto', t['proto'], 'TCP')
chk('C saddr', t['saddr'], '192.168.7.100')
chk('C dport', t['dport'], '443')
chk('C state', t['extra']['state'], 'ESTABLISHED')
has('C 中文状态', t['msg_cn'], '已建立')

# 无 src/dst → None
chk('C 无效行', parse_tbl('garbage'), None)
chk('C 表头行', parse_tbl('conntrack v1.4.6 (conntrack-tools)'), None)

# ---------------- D. 统一过滤 ----------------
def mk(src, level, action, proto, sa, sp, da, dp, msg):
    return rec(src, level, action=action, proto=proto, saddr=sa, sport=sp,
               daddr=da, dport=dp, msg_cn=msg)


DATA = [
    mk('fw', 'warn', 'DROP', 'TCP', '203.0.113.9', '54321', '192.168.7.3', '22', '丢弃 SSH'),
    mk('fw', 'info', 'ACCEPT', 'TCP', '192.168.7.5', '40000', '8.8.8.8', '443', '放行 HTTPS'),
    mk('conntrack', 'info', 'NEW', 'UDP', '192.168.7.9', '5353', '224.0.0.251', '5353', '新建 UDP'),
    mk('system', 'err', '', '', '', '', '', '', '磁盘错误'),
    mk('fw', 'debug', 'ACCEPT', 'ICMP', '10.0.0.1', '', '10.0.0.2', '', 'ICMP 通过'),
]

chk('D 无过滤=全部', len(flt(DATA, {})), 5)
chk('D 源过滤 fw', len(flt(DATA, {'src': 'fw'})), 3)
chk('D 动作过滤 DROP', len(flt(DATA, {'action': 'DROP'})), 1)
chk('D 协议过滤 TCP', len(flt(DATA, {'proto': 'TCP'})), 2)
chk('D 地址过滤', len(flt(DATA, {'addr': '192.168.7'})), 3)
chk('D 端口过滤 443', len(flt(DATA, {'port': '443'})), 1)
chk('D 关键字过滤', len(flt(DATA, {'q': 'ssh'})), 1)
# 级别：warn 及更严重（warn, err）
chk('D 级别 warn 及以上', len(flt(DATA, {'level': 'warn'})), 2)
chk('D 级别 err 以上', len(flt(DATA, {'level': 'err'})), 1)
chk('D 级别 debug 全要', len(flt(DATA, {'level': 'debug'})), 5)
# 组合
chk('D 组合 src+proto', len(flt(DATA, {'src': 'fw', 'proto': 'TCP'})), 2)
chk('D 组合无结果', len(flt(DATA, {'src': 'ddns'})), 0)

# ---------------- E. 聚合统计 ----------------
st = stats(DATA)
chk('E total', st['total'], 5)
chk('E by_src fw', st['by_src'].get('fw'), 3)
chk('E by_level warn', st['by_level'].get('warn'), 1)
chk('E by_action DROP', st['by_action'].get('DROP'), 1)
chk('E by_proto TCP', st['by_proto'].get('TCP'), 2)
chk('E top_src_ip 非空', len(st['top_src_ip']) > 0, True)
chk('E top_dst_port 非空', len(st['top_dst_port']) > 0, True)
# 排序：计数多的在前
chk('E top_src 排序', st['top_src_ip'][0]['n'] >= st['top_src_ip'][-1]['n'], True)

# ---------------- F. 保留策略 ----------------
now = time.time()
old = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(now - 30 * 86400))
fresh = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(now - 3600))
rows = []
for i in range(5):
    rows.append(json.dumps({'ts': old, 'src': 'fw', 'msg_cn': 'old%d' % i}))
for i in range(3):
    rows.append(json.dumps({'ts': fresh, 'src': 'fw', 'msg_cn': 'new%d' % i}))
rows.append('{broken json')
with open(ARCHIVE, 'w', encoding='utf-8') as f:
    f.write('\n'.join(rows) + '\n')

res = prune({'keep_days': 7, 'keep_rows': 200000})
chk('F 删除超期', res['removed'], 5)
chk('F 保留新鲜', res['kept'], 4)   # 3 新鲜 + 1 无法解析（保留）
with open(ARCHIVE, encoding='utf-8') as f:
    kept = f.read().splitlines()
chk('F 文件行数', len(kept), 4)
has('F 保留 new1', '\n'.join(kept), 'new1')
has('F 删除 old1', '\n'.join(kept), 'old1', want=False)
has('F 保留破损行', '\n'.join(kept), '{broken json')

# 按条数裁剪
with open(ARCHIVE, 'w', encoding='utf-8') as f:
    for i in range(10):
        f.write(json.dumps({'ts': fresh, 'src': 'fw', 'msg_cn': 'r%d' % i}) + '\n')
res2 = prune({'keep_days': 30, 'keep_rows': 4})
chk('F 条数裁剪 removed', res2['removed'], 6)
chk('F 条数裁剪 kept', res2['kept'], 4)
with open(ARCHIVE, encoding='utf-8') as f:
    last = f.read().splitlines()
has('F 保留最新 r9', last[-1], 'r9')
has('F 丢弃最旧 r0', '\n'.join(last), '"r0"', want=False)

# 文件不存在
os.remove(ARCHIVE)
chk('F 无文件', prune({'keep_days': 7}), {'removed': 0, 'kept': 0})

# ---------------- G. act_ulog 参数 ----------------
# save_conf 校验
NS['_ulog_load'] = lambda: dict(NS['ULOG_DEFAULT_CONF'],
                                sources=dict(NS['ULOG_DEFAULT_CONF']['sources']))
NS['_ulog_save'] = lambda d: None
r = act({'op': 'save_conf', 'keep_days': 'abc'})
chk('G keep_days 非法', r['ok'], False)
r = act({'op': 'save_conf', 'keep_rows': 0})
chk('G keep_rows 0 被拒', r['ok'], False)
r = act({'op': 'save_conf', 'keep_days': 14, 'keep_rows': 5000,
         'sources': {'fw': True, 'system': False}, 'min_level': 'info'})
chk('G save_conf 成功', r['ok'], True)
chk('G keep_days 生效', r['data']['conf']['keep_days'], 14)
chk('G sources 生效', r['data']['conf']['sources']['system'], False)
r = act({'op': 'save_conf', 'min_level': 'nonsense'})
chk('G min_level 归一 debug', r['data']['conf']['min_level'], 'debug')

# 未知 op
r = act({'op': 'nonsense'})
chk('G 未知 op', r['ok'], False)
has('G 未知 op 中文', r['msg_cn'], '不支持的日志操作')

# export 格式（用桩数据）
NS['_ulog_collect'] = lambda p: (list(DATA), {'sources_used': ['fw']})
r = act({'op': 'export', 'format': 'csv'})
chk('G export csv ok', r['ok'], True)
has('G csv 表头', r['data']['text'], 'ts,src,level,action,proto')
chk('G csv 行数', r['data']['rows'], 5)
r = act({'op': 'export', 'format': 'json'})
chk('G export json ok', r['ok'], True)
chk('G json 行数', len(r['data']['text'].splitlines()), 5)
# JSONL 每行可解析
okj = all(json.loads(x) for x in r['data']['text'].splitlines())
chk('G json 每行合法', okj, True)

# clear_archive
with open(ARCHIVE, 'w') as f:
    f.write('x\n')
r = act({'op': 'clear_archive'})
chk('G clear_archive ok', r['ok'], True)
chk('G 归档已删', os.path.isfile(ARCHIVE), False)

# ---------------- H. 归档与去重（drouter-logd） ----------------
LOGD_SRC = os.path.join(ROOT, 'backend', 'drouter-logd.py')
ldsrc = open(LOGD_SRC, encoding='utf-8').read()

# 抽出去重函数与指纹函数做隔离测试
LNS = {'os': os, 'json': json, 'ARCHIVE': ARCHIVE,
       'datetime': __import__('datetime').datetime}
for fname in ('_fp', 'dedupe_archive'):
    m = re.search(r'\ndef %s\(.*?(?=\ndef |\nif __name__)' % fname, ldsrc, re.S)
    if not m:
        raise SystemExit('抽取失败：%s' % fname)
    exec(compile(m.group(0), LOGD_SRC, 'exec'), LNS)

fp = LNS['_fp']
dedupe = LNS['dedupe_archive']

# 指纹：字段不同 → 指纹不同
a = {'ts': '2026-09-28T10:00:00', 'src': 'fw', 'action': 'DROP',
     'saddr': '1.1.1.1', 'daddr': '2.2.2.2', 'sport': '1', 'dport': '2', 'msg_cn': 'x'}
b = dict(a, dport='3')
chk('H 指纹不同', fp(a) != fp(b), True)
chk('H 指纹稳定', fp(a), fp(dict(a)))

# 去重：3 条重复 + 1 条唯一
rows = [a, a, b, a, {'ts': '2026-09-28T11:00:00', 'src': 'system', 'msg_cn': 'disk'}]
with open(ARCHIVE, 'w', encoding='utf-8') as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + '\n')
removed = dedupe()
chk('H 去重移除数', removed, 2)   # a 出现 3 次 → 去 2 条
with open(ARCHIVE, encoding='utf-8') as f:
    out = f.read().splitlines()
chk('H 去重后行数', len(out), 3)  # a / b / system 各一条
has('H 保留 system', '\n'.join(out), 'disk')

# 空文件 / 不存在
os.remove(ARCHIVE)
chk('H 无文件去重', dedupe(), 0)

# 保留无法解析的行（不误删）
with open(ARCHIVE, 'w', encoding='utf-8') as f:
    f.write('{broken\n' + json.dumps(a) + '\n{broken\n')
chk('H 破损行不去重', dedupe(), 0)
with open(ARCHIVE, encoding='utf-8') as f:
    chk('H 破损行保留', len(f.read().splitlines()), 3)

# archive op（act_ulog）
NS['_ulog_collect'] = lambda p: (list(DATA), {'sources_used': ['fw'], 'errors': {}})
NS['_ulog_archive_write'] = lambda recs, conf=None: len(recs)
NS['_ulog_prune'] = lambda conf: {'removed': 2, 'kept': 10}
r = act({'op': 'archive', 'since': '5 min ago'})
chk('H archive ok', r['ok'], True)
chk('H archive 条数', r['data']['archived'], 5)
chk('H archive prune', r['data']['prune']['removed'], 2)

# 关闭时不归档
NS['_ulog_load'] = lambda: dict(NS['ULOG_DEFAULT_CONF'], enabled=False,
                                sources=dict(NS['ULOG_DEFAULT_CONF']['sources']))
r = act({'op': 'archive'})
chk('H 关闭时跳过', r['data']['archived'], 0)
has('H 关闭提示', r['msg_cn'], '已关闭')

# 守护脚本静态检查
has('H logd 归档路径', ldsrc, "ARCHIVE = os.path.join(LOG_DIR, 'ulog.jsonl')")
has('H logd 配置路径', ldsrc, "CONF = '/etc/drouter/generated/log.conf'")
has('H logd 调用 archive', ldsrc, "'op': 'archive'")
has('H logd 有时间窗', ldsrc, "SINCE = '5 min ago'")
has('H logd 有去重', ldsrc, 'def dedupe_archive()')
# 守护脚本必须只读：不得出现 nft 规则加载、不得启停服务。
# 只看「非注释行」：注释里为了说明背景提到 nft 是正常的，不能算违规。
ldcode = '\n'.join(l for l in ldsrc.split('\n') if not l.lstrip().startswith('#'))
has('H logd 不加载规则', ldcode, 'nft', want=False)
has('H logd 不改网络', ldcode, 'ip link', want=False)

# ---------------- 静态结构检查 ----------------
has('S ACTIONS read:ulog', src, "'read:ulog': read_ulog")
has('S ACTIONS ulog', src, "'ulog': act_ulog")
has('S ACTIONS read:ulog_flow', src, "'read:ulog_flow': read_ulog_flow")
has('S ACTIONS read:ulog_conf', src, "'read:ulog_conf': read_ulog_conf")
has('S 归档函数', src, 'def _ulog_archive_write(')
has('S 采集函数', src, 'def _ulog_collect(')
has('S 配置路径', src, "ULOG_CONF = '/etc/drouter/generated/log.conf'")
chk('S 源数量', len(NS['ULOG_SOURCES']), 7)
chk('S 级别数量', len(NS['ULOG_LEVELS']), 8)

print('=' * 62)
print('#11 统一日志系统 · 单测结果')
print('=' * 62)
print('通过：%d   失败：%d' % (len(PASS), len(FAIL)))
if FAIL:
    print('-' * 62)
    for f in FAIL:
        print('  ✗ ' + f)
    sys.exit(1)
print('全部通过 ✔')

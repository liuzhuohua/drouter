#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""历史累计流量（概览页「累计已下载 / 已上传」）的纯逻辑测试。

从 drouter-helper.py 抽取 _hbytes / _net_totals_* 片段，在隔离命名空间执行，
把 /proc/net/dev 与持久化文件都换成假的，不碰真实系统。
"""
import ast
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'backend', 'drouter-helper.py')
code = open(SRC, encoding='utf-8').read()
tree = ast.parse(code)

WANT = ['_hbytes', '_net_totals_load', '_net_totals_cur',
        '_net_totals_snapshot', '_net_totals_update', 'net_totals']
picked = []
for node in tree.body:
    if isinstance(node, ast.Assign):
        # 故意不抽取 NET_TOTALS_FILE 的赋值：那会把我们注入的临时路径
        # 覆盖回 /var/lib/drouter/...，测试就会读写到真实位置（Git Bash 下
        # 还会被映射到 Git 安装目录），造成跨次运行互相污染。
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in WANT:
                picked.append(node)
    elif isinstance(node, ast.FunctionDef) and node.name in WANT:
        picked.append(node)

got = set()
for n in picked:
    if isinstance(n, ast.Assign):
        got.update(t.id for t in n.targets if isinstance(t, ast.Name))
    else:
        got.add(n.name)
missing = set(WANT) - got
assert not missing, '未能从源码抽取：%s' % ', '.join(sorted(missing))

tmp = tempfile.mkdtemp()
STATE = {'rx': 0, 'tx': 0, 'cur': (0, 0), 'path': os.path.join(tmp, 'net-totals.json')}

ns = {
    'os': os, 'json': json, 'time': __import__('time'),
    'NET_TOTALS_FILE': STATE['path'],
}


def fake_counters():
    rx, tx = STATE['cur']
    return {'ens18': {'rx_bytes': rx, 'tx_bytes': tx, 'rx_pkts': 0, 'tx_pkts': 0}}


ns['_net_counters'] = fake_counters
exec(compile(ast.Module(body=picked, type_ignores=[]), SRC, 'exec'), ns)

snapshot = ns['_net_totals_snapshot']
update = ns['_net_totals_update']
hbytes = ns['_hbytes']

fails = []


def ck(name, cond, extra=''):
    print('  %-44s %s %s' % (name, 'OK  ' if cond else 'FAIL', extra))
    if not cond:
        fails.append(name)


print('字节人性化：')
ck('0 → 0 B', hbytes(0) == '0 B', hbytes(0))
ck('1024 → 1.0 KB', hbytes(1024) == '1.0 KB', hbytes(1024))
ck('1.5 MB', hbytes(1024 ** 2 * 1.5) == '1.5 MB', hbytes(1024 ** 2 * 1.5))
ck('3.0 GB', hbytes(1024 ** 3 * 3) == '3.0 GB', hbytes(1024 ** 3 * 3))
ck('2.0 TB', hbytes(1024 ** 4 * 2) == '2.0 TB', hbytes(1024 ** 4 * 2))

print('\n累计逻辑：')
# 起始：网卡已收 100 / 发 50
STATE['cur'] = (100, 50)
s = snapshot()
ck('无历史文件时从 0 起算', s['rx_bytes'] == 100 and s['tx_bytes'] == 50,
   '%d/%d' % (s['rx_bytes'], s['tx_bytes']))

update()
# 再产生 200 / 80 的增量
STATE['cur'] = (300, 130)
s = snapshot()
ck('增量叠加正确', s['rx_bytes'] == 300 and s['tx_bytes'] == 130,
   '%d/%d' % (s['rx_bytes'], s['tx_bytes']))

update()
ck('落盘后读取稳定', snapshot()['rx_bytes'] == 300)

# 模拟重启：/proc 计数器清零（比上次记录的 last 更小）
STATE['cur'] = (10, 5)
s = snapshot()
ck('计数器回绕不产生负增量', s['rx_bytes'] >= 300 and s['pending_rx'] == 0,
   'rx=%d pending=%d' % (s['rx_bytes'], s['pending_rx']))
ck('回绕后仍给出可读值', s['rx_h'].endswith('B'), s['rx_h'])

update()
# 重启后重新增长
STATE['cur'] = (60, 30)
s = snapshot()
ck('重启后继续累计', s['rx_bytes'] == 300 + 50 and s['tx_bytes'] == 130 + 25,
   '%d/%d' % (s['rx_bytes'], s['tx_bytes']))

ck('since 起始时间已被记录', bool(snapshot().get('since')), snapshot().get('since') or '(空)')

print('\n结果：%d 项，失败 %d 项' % (12, len(fails)))
sys.exit(1 if fails else 0)

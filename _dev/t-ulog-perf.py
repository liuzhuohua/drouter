#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一日志性能契约（#7）—— 防止「每次查询固定白等 2 秒」回归。

踩过的坑：_ulog_from_conntrack 里固定跑
    timeout 2 conntrack -E -o extended
`conntrack -E` 是阻塞的事件流，只能靠 timeout 掐断，于是**每查一次统一日志
都要白等 2 秒**。在这台还没接管转发的机器上连接跟踪表是空的，2 秒换来 0 条
记录 —— 实测 /api/ulog 的 2109ms 里 2019ms 花在这上面。

修法：实时事件订阅改成按需（live=True 才跑），默认查询走快路径。
本测试锁住这个约定。
"""
import ast
import io
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
fails = 0


def chk(label, cond, extra=''):
    global fails
    if not cond:
        fails += 1
    print('[%s] %s %s' % ('OK' if cond else 'FAIL', label, extra))


def read(rel):
    with io.open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


HELPER = read('backend/drouter-helper.py')
APP = read('web/app.js')
TREE = ast.parse(HELPER)

fn = None
for n in TREE.body:
    if isinstance(n, ast.FunctionDef) and n.name == '_ulog_from_conntrack':
        fn = n
chk('存在 _ulog_from_conntrack', fn is not None)
if fn is None:
    sys.exit(1)

src = ast.get_source_segment(HELPER, fn) or ''

# 1) 阻塞订阅必须被 live 开关包住，不能无条件执行
chk('conntrack -E 只在 live 分支里执行',
    "'conntrack', '-E'" in src and 'if live:' in src)
# 无条件调用的特征：函数体里出现不在 if 内的 timeout 调用。
# 简单可靠的判断：'-E' 那行前面必须能找到 'if live:'
lines = src.splitlines()
e_idx = [i for i, l in enumerate(lines) if "'-E'" in l]
live_idx = [i for i, l in enumerate(lines) if 'if live:' in l]
chk('-E 调用位于 if live: 之后',
    bool(e_idx) and bool(live_idx) and min(e_idx) > min(live_idx),
    'if live 行=%s, -E 行=%s' % (live_idx[:1], e_idx[:1]))
# 2) 绝对不能出现写死的 timeout 2
chk('不再有写死的 timeout 2 阻塞订阅',
    not re.search(r"'timeout',\s*'2'", src))
chk('订阅秒数由参数控制且上限 10 秒',
    'live_sec' in src and 'min(int(live_sec), 10)' in src)
# 3) 默认关闭
chk('live 默认 False', 'live=False' in src)
chk('默认仍走内核日志回退（能力不丢）', 'journalctl' in src)

# 4) 采集层把 live 传下去
collect = None
for n in TREE.body:
    if isinstance(n, ast.FunctionDef) and n.name == '_ulog_collect':
        collect = n
csrc = ast.get_source_segment(HELPER, collect) or ''
chk('_ulog_collect 读取 live 参数',
    "p.get('live')" in csrc and 'live=live' in csrc)
chk('_ulog_collect 把 live 状态回给前端', "'live': live" in csrc)

# 5) 前端有开关，且默认关
chk('前端 ulog 状态默认不订阅实时事件', 'live: false' in APP)
chk('前端查询带上 live 参数', 'live: !!u.live' in APP)
chk('前端提供实时开关控件', 'id="ul-live"' in APP)
chk('开关只在「统一日志」视图出现（当前连接视图不查事件流）',
    "${isFlow ? '' : `" in APP and 'ul-live' in APP)

print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % fails))
sys.exit(1 if fails else 0)

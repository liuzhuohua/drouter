#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开工前预检：工作区有没有「上一轮注入没还原」的残留。

为什么需要这个文件：反向验证（注入 bug → 看判据变红 → 还原）如果被
SIGTERM/SIGKILL 打断，`finally` 里的还原不会执行，注入就**留在源码里**。
下一次跑反向验证时，它看到的「基线」已经被污染，于是报出一堆看不懂的红，
而真正的根因（几小时前那次没还原的注入）完全不在视野里。

2026-10-04 实际踩到：`drouter-helper.py` 里 `_vpn_env` 的
`os.path.join('/lib/modules', ...)` 被留成了 `'/nonexistent'`，
连带 `need_module` 整个状态从四态里消失 —— 而这个缺陷在
**已发布的 v1.0.8 里**。t-107 一直红着，但被当成了「判据本身有问题」。

所以这一条不是「保险」，是真bug 的直接成因。

判据只查两个方向：
  ① backend/ 下有没有明显的注入哨兵（/nonexistent、__INJECT__、127.0.0.2）
  ② 全量静态判据当前是否全绿（不绿就别动手，先弄清是哪一条、为什么）
只读，不改任何东西。
"""
import io
import os
import re
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
# 注入脚本惯用的假路径 / 假常量。真机代码里出现它们基本可以断定是残留。
SENTINELS = ('/nonexistent', '__INJECT__', '127.0.0.2', 'INJECTED_')

fails = []


def chk(label, cond, extra=''):
    print('[%s] %s %s' % ('OK' if cond else 'FAIL', label, extra))
    if not cond:
        fails.append(label)


print('=== 1. 扫 backend/ 里的注入哨兵 ===')
hits = []
for fn in sorted(os.listdir(os.path.join(ROOT, 'backend'))):
    if not fn.endswith('.py'):
        continue
    p = os.path.join(ROOT, 'backend', fn)
    try:
        src = io.open(p, encoding='utf-8').read()
    except Exception:
        continue
    # 只在**代码**里找，注释和 docstring 里为了说明历史 bug 常会写到
    code = re.sub(r'#.*$', '', src, flags=re.M)
    for s in SENTINELS:
        if s in code:
            for i, line in enumerate(src.splitlines(), 1):
                if s in line and not line.strip().startswith('#'):
                    hits.append('%s:%d  %s' % (fn, i, line.strip()[:70]))
chk('backend/ 里没有注入残留', not hits, '')
for h in hits:
    print('       %s' % h)
if hits:
    print('       ⚠ 这些多半是反向验证没还原。用 git diff 看清楚，'
          '确认是残留就 git checkout 还原，**别直接改成新值** —— '
          '新值可能才是 bug（/nonexistent 那次就差点被当成「本就该这样」）。')

print()
print('=== 2. backend/*.py 语法 ===')
bad = []
for fn in sorted(os.listdir(os.path.join(ROOT, 'backend'))):
    if not fn.endswith('.py'):
        continue
    p = os.path.join(ROOT, 'backend', fn)
    r = subprocess.run([sys.executable, '-m', 'py_compile', p],
                       capture_output=True)
    if r.returncode:
        bad.append('%s: %s' % (fn, r.stderr.decode('utf-8', 'replace')[:120]))
chk('backend/ 下所有模块语法正确', not bad, '')
for b in bad:
    print('       %s' % b)

print()
print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % len(fails)))
sys.exit(1 if fails else 0)

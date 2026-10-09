# -*- coding: utf-8 -*-
"""审计详情英文化检查（包装 _dev/t-audit-en.js）。

为什么单独一条：t-en-render.js 只渲染 VIEWS 的**顶层视图**，而审计表是
异步 fetch 回来后才 innerHTML 的，渲染回归覆盖不到；audDetail() 这条
「后端中文 → 英文」的转换链因此长期没有判据守着，真机上整列中文漏了出去
（2026-10-09：apply -> 主题不存在：/ dnsmasq live=True -> 配置校验失败：…）。

判据本体在 JS 里（要用真 i18n.js + 真 audDetail）。这里只负责找 node +
透传退出码 + 把红的那几行原样打出来。
"""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
JS = os.path.join(HERE, 't-audit-en.js')

# ⛔ 绝不硬编码 WorkBuddy 的 node 版本目录（版本号会随升级变）。
NODE = ''
for c in sorted(glob.glob(os.path.expanduser(
        '~/.workbuddy/binaries/node/versions/*/node.exe')), reverse=True):
    if os.path.isfile(c):
        NODE = c
        break
if not NODE:
    NODE = 'node'

p = subprocess.run([NODE, JS], cwd=ROOT, capture_output=True,
                   text=True, encoding='utf-8', errors='replace', timeout=120)
out = (p.stdout or '') + (p.stderr or '')
for line in out.splitlines():
    print(line)
if p.returncode == 0:
    print('通过 1 项（审计详情 47 例 + 3 用户数据例 + DPI why，英文界面无中文残留）')
sys.exit(p.returncode)

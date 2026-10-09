# -*- coding: utf-8 -*-
"""英文界面残留中文检查（包装 _dev/t-en-render.js）。

为什么要有这条包装：run-static.py 只自动收 `t-*.py`，
纯 JS 的检查器不包装就**根本进不了全量回归**。t-en-render.js 是
「英文界面无中文残留」这条主线的核心判据（真机 payload + 真 i18n.js
渲染 47 个顶层视图 + 顶栏时钟 + accel 子块 + 4 个独立模块），之前一直只手动跑，
等于回归里缺了最关键的一道闸。

判据本体在 JS 里（要用 Node 真跑 app.js 的渲染，Python 做不到）。
这里只负责找 node + 透传退出码 + 把红的那几行原样打出来。
"""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
JS = os.path.join(HERE, 't-en-render.js')

# ⛔ 绝不硬编码 WorkBuddy 的 node 版本目录（版本号会随升级变）：
#    先按版本倒序 glob，取不到再退回 PATH 里的 node。
NODE = ''
for c in sorted(glob.glob(os.path.expanduser(
        '~/.workbuddy/binaries/node/versions/*/node.exe')), reverse=True):
    if os.path.isfile(c):
        NODE = c
        break
if not NODE:
    NODE = 'node'

p = subprocess.run([NODE, JS], cwd=ROOT, capture_output=True,
                   text=True, encoding='utf-8', errors='replace', timeout=300)
out = (p.stdout or '') + (p.stderr or '')
for line in out.splitlines():
    print(line)
if p.returncode == 0:
    print('通过 1 项（英文界面 47 视图 + clock + accel + 4 个独立模块 + update 失败分支/文案钩子 无中文残留）')
sys.exit(p.returncode)

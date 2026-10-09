# -*- coding: utf-8 -*-
"""跑 _dev/t-i18n-run2.js（Node 端到端 i18n 验证）并转成 run-static 能收的形状。

为什么要壳：run-static.py 只 glob('t-*.py')，而 i18n 的真跑验证必须在
Node 里做（要 eval 真的 i18n.js 并造 DOM 桩）。

⛔ 判据本体在 .js 里 —— 2026-10-05 的教训：
   纯静态判据（正则查 key 在不在字典）显示 100% 覆盖，
   真跑却抓到 24 条「英文界面仍显示中文」（换行形态不一致）。
   **这类问题只有真跑才看得见**，别再退回静态断言。
"""
import io
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
JS = os.path.join(HERE, 't-i18n-run2.js')

if not os.path.exists(JS):
    print('FAIL: 找不到 %s' % JS)
    sys.exit(1)

# 反向验证脚本会改源码，跑之前先确认判据本体是干净的
src = io.open(JS, encoding='utf-8').read()
if 'INJECT' in src:
    print('FAIL: t-i18n-run2.js 含注入残留，先从备份还原')
    sys.exit(1)
if '\r\n' in src:
    print('FAIL: t-i18n-run2.js 含 CRLF')
    sys.exit(1)

r = subprocess.run(['node', JS], cwd=ROOT, capture_output=True, text=True)
out = (r.stdout or '') + (r.stderr or '')
for line in out.splitlines():
    print(line)
sys.exit(r.returncode)

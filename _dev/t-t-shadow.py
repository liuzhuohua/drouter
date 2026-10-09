# -*- coding: utf-8 -*-
"""跑 _dev/scan-t-shadow.js（扫「t 变量遮蔽」）并转成 run-static 能收的形状。

为什么要这条：2026-10-05 补 i18n 时新增了 800+ 个 t() 调用，
把一批原本无害的遮蔽点激活了 —— `function tmLocalIssues(t)` 的参数
`t`（主题对象）与全局 t() 同名，函数体里 t('…') 一调用就
TypeError: t is not a function，theme/design 页整块崩。

check-render 只抓到「跑到的那 1 处」；这条扫全库。
（判据本体在 .js 里，用 Node 的真词法跟踪字符串/模板/注释，
  避免在注释和字符串里误报。）
"""
import io
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
JS = os.path.join(HERE, 'scan-t-shadow.js')
APP = os.path.join(ROOT, 'web', 'app.js')

if not os.path.exists(JS):
    print('FAIL: 找不到 %s' % JS)
    sys.exit(1)

for p in (JS, APP):
    src = io.open(p, encoding='utf-8').read()
    if 'INJECT' in src:
        print('FAIL: %s 含注入残留，先从备份还原' % p)
        sys.exit(1)
    if '\r\n' in src:
        print('FAIL: %s 含 CRLF' % p)
        sys.exit(1)

r = subprocess.run(['node', JS, APP], cwd=ROOT, capture_output=True, text=True)
out = (r.stdout or '') + (r.stderr or '')
for line in out.splitlines():
    print(line)
sys.exit(r.returncode)

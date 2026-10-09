#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查多行字符串拼接错误 —— **委托给 Node 解析器**（权威）。

⚠️ 之前自己用正则扫，试了 3 版全是假红：
  v1 把正则字符类 `/[&<>"']/g` 当引号
  v2 忽略了**跨行的模板字符串**（反引号 852 行开、853 行闭）
  v3 忽略了**注释里的中文引号**（「绝不能静默降级」被当成字符串开合）

自己写 JS 词法分析器是不现实的 —— **直接用 Node 的解析器**，
它对字符串/模板/正则/注释的处理是完全正确的（跟浏览器同一套）。

只做一件事：把 Node 报的「语法错误 + 行号」翻译成人能懂的提示。
Node 的报错信息形如：
    SyntaxError: Unexpected string
        app.js:272
           en: 'first '
           + 'second',
    → 直接告诉用户「第 272 行这里可能是多行字符串缺 + 号」
"""
import io
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NODE = None
for base in (r'C:\Users\lyrz-pve-win10\.workbuddy\binaries\node\versions',
             '/c/Users/lyrz-pve-win10/.workbuddy/binaries/node/versions'):
    if os.path.isdir(base):
        for d in sorted(os.listdir(base), reverse=True):
            p = os.path.join(base, d, 'node.exe' if os.name == 'nt' else 'node')
            if os.path.isfile(p):
                NODE = p
                break
    if NODE:
        break
if not NODE:
    print('找不到 node，跳过')
    sys.exit(0)

TARGETS = ['web/i18n.js', 'web/app.js', 'web/update.js', 'web/upstream.js',
           'web/netdetail.js', 'web/realtime.js']

fails = []
n = 0


def ck(desc, cond, extra=''):
    global n
    n += 1
    print(('ok    ' if cond else 'FAIL  ') + desc + (' ' + str(extra) if extra else ''))
    if not cond:
        fails.append(desc)


# 写长英文句子时最容易漏的提示
HINTS = [
    (re.compile(r'Unexpected string', re.I),
     "可能是多行字符串漏了 + 号：\n"
     "        en: 'first part '\n"
     "            'second part',      ← 这里要写 + 'second part',\n"
     "     英文句子比较长时容易忘，注意每一行末尾都要有 +"),
    (re.compile(r'Invalid or unexpected token', re.I),
     '可能是中文标点（，、）混进了代码，或者引号不成对'),
    (re.compile(r'Unexpected end of input', re.I),
     '可能有未闭合的引号或括号'),
]

print('=== 语法检查（委托 Node 解析器）===')
for rel in TARGETS:
    p = os.path.join(ROOT, rel)
    if not os.path.isfile(p):
        continue
    r = subprocess.run([NODE, '--check', p], capture_output=True)
    if r.returncode == 0:
        ck('%s 语法正确' % rel, True)
        continue
    err = (r.stderr or b'').decode('utf-8', 'replace')
    m = re.search(r':(\d+)\s*$', err, re.M)
    line = m.group(1) if m else '?'
    hint = ''
    for pat, h in HINTS:
        if pat.search(err):
            hint = h
            break
    # 打印出错行上下文
    try:
        lines = io.open(p, encoding='utf-8').read().split('\n')
        n0 = int(line)
        ctx = '\n'.join('        %4d| %s' % (i + 1, lines[i])
                        for i in range(max(0, n0 - 3),
                                       min(len(lines), n0 + 1)))
    except Exception:
        ctx = ''
    ck('%s 语法正确' % rel, False,
       '\n     第 %s 行：%s%s' % (line, err.strip().split('\n')[0], hint))
    if ctx:
        print(ctx)

print()
print('=' * 56)
print('语法检查 %d 条，失败 %d 条' % (n, len(fails)))
for f in fails:
    print('  FAIL: %s' % f)
sys.exit(1 if fails else 0)

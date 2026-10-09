#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""钉住「翻译调用统一用别名 T()」这件事，防止回退成裸 t()。

背景（2026-10-07）：
  web/app.js 里 `t` 在几十处被当作**局部变量**用：
      const t = tmCur()      // 主题
      const t = $('#toast')  // 提示条
      rows.map(t => ...)     // 表格行
      function tmThemeCard(t, on)
      const t = await api(...)
  在这些作用域里写 t('中文') 会直接抛 "t is not a function"，
  页面表现为「切到英文后整个视图白屏 / 只剩半个页面」。

根治办法：app.js 里所有翻译调用改写成别名 T('…')，
并且把仅有的两个局部 T 绑定（tmRender / tmRenderPane）改名成 TH，
让 T 在 app.js 里**只有一个含义**。

本判据就是守住这个不变量 —— 一旦有人新写了 t('…') 或新增了局部 T，
这里立刻变红。
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

fails = []
n = 0


def ck(desc, cond, extra=''):
    global n
    n += 1
    if cond:
        print('ok    %s' % desc)
    else:
        print('FAIL  %s %s' % (desc, extra))
        fails.append(desc)


def read(*parts):
    with io.open(os.path.join(ROOT, *parts), encoding='utf-8') as f:
        return f.read()


def strip_comments(src):
    """把注释换成等长空白 —— 行号不变，注释里的例子不会误判。"""
    out = list(src)
    i, L = 0, len(src)
    st = None  # None / 'line' / 'block' / 'sq' / 'dq' / 'tpl'
    while i < L:
        c = src[i]
        if st is None:
            if c == '/' and i + 1 < L and src[i + 1] == '/':
                st = 'line'
            elif c == '/' and i + 1 < L and src[i + 1] == '*':
                st = 'block'
            elif c in "'\"":
                st = 'sq' if c == "'" else 'dq'
                i += 1
                continue
            elif c == '`':
                st = 'tpl'
                i += 1
                continue
        elif st == 'line':
            if c == '\n':
                st = None
            else:
                out[i] = ' '
        elif st == 'block':
            if c == '*' and i + 1 < L and src[i + 1] == '/':
                out[i] = out[i + 1] = ' '
                i += 2
                st = None
                continue
            if c != '\n':
                out[i] = ' '
        elif st in ('sq', 'dq'):
            q = "'" if st == 'sq' else '"'
            if c == '\\':
                i += 2
                continue
            if c == q:
                st = None
        elif st == 'tpl':
            if c == '\\':
                i += 2
                continue
            if c == '`':
                st = None
        i += 1
    return ''.join(out)


print('=== 一、T 别名本身 ===')
aj = read('web', 'app.js')
code = strip_comments(aj)

ck('app.js 顶部定义了 T 别名',
   re.search(r"const\s+T\s*=\s*window\.i18n\s*\?\s*window\.i18n\.t\s*:\s*window\.t\s*;",
             aj) is not None)

# 裸 t('：排除 window.t( / .t( / 标识符前缀（如 fmt.t(、ajax.t(）
bare = re.findall(r"(?<![A-Za-z0-9_$.])t\s*\(\s*'", code)
ck('app.js 已无裸 t(\' 翻译调用', not bare,
   '残留 %d 处，例：%s' % (len(bare), bare[:5]))

print()
print('=== 二、T 不被局部遮蔽 ===')
# 允许顶部那一处别名定义；其余 const/let/var T = 都是遮蔽
alias_line = re.search(r"^const\s+T\s*=\s*window\.i18n", aj, re.M)
shadow = [m.start() for m in re.finditer(r"\b(?:const|let|var)\s+T\b\s*=", code)]
if alias_line:
    shadow = [p for p in shadow
              if abs(code[:p].count('\n') - code[:alias_line.start()].count('\n')) > 0]
ck('没有第二个 const/let/var T =', not shadow,
   '位置行号 %s' % [code[:p].count('\n') + 1 for p in shadow[:5]])

ck('没有 .map(T => / .forEach(T => 这类箭头遮蔽',
   re.search(r"\.\s*(?:map|forEach|filter|find|some|every)\s*\(\s*T\s*(?:=>|,)",
             code) is None)

ck('没有 function T( 定义', re.search(r"function\s+T\s*\(", code) is None)

print()
print('=== 三、其余 web 文件走 window.t，也不许遮蔽 ===')
ij = read('web', 'i18n.js')
ck('i18n.js 导出 window.t（其它文件靠它拿到 t）',
   re.search(r"window\.t\s*=\s*t\s*;", ij) is not None)

for fn in ('netdetail.js', 'realtime.js', 'update.js', 'upstream.js'):
    p = os.path.join(ROOT, 'web', fn)
    if not os.path.isfile(p):
        continue
    src = read('web', fn)
    c = strip_comments(src)
    bad = (re.findall(r"\b(?:const|let|var)\s+t\b\s*=", c)
           + re.findall(r"\.\s*(?:map|forEach|filter|find|some|every)\s*\(\s*t\s*(?:=>|,)", c))
    ck('%s 不遮蔽 t' % fn, not bad, '例：%s' % bad[:5])
    # 这些文件用 t() 是可以的，但必须没有 T( 混用（混用说明改了一半）
    ck('%s 不混用 T(' % fn,
       re.search(r"(?<![A-Za-z0-9_$.])T\s*\(\s*'", c) is None)

print()
print('=' * 56)
print('T 别名不变量 %d 条，失败 %d 条' % (n, len(fails)))
for f in fails:
    print('  FAIL: %s' % f)
sys.exit(1 if fails else 0)

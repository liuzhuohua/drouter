# -*- coding: utf-8 -*-
"""钉住 index.html 里的 <script> 加载顺序。

⛔ 为什么这条必须存在（2026-10-05 的事故）：
   补 i18n 时，i18n.js 是**后加**的文件，我给它写的注释是
   「必须在 app.js 之前」，但**没动实际顺序** —— 部署后整站登录失效：
   用户输入密码点登录，页面毫无反应。

   根因链条：
     ① app.js **顶层**就有 `t('…')` 调用（左导航 `g: t('概览')`
        之类是模块加载时求值的常量）
     ② i18n.js 排在 app.js **之后** → 加载 app.js 时 `t` 还没定义
     ③ `ReferenceError: t is not defined` → **整个 app.js 执行中断**
     ④ 登录按钮的 onclick 压根没绑 → 「输入密码后网页无反应」

   而 `node --check` / `check-render.js` 全都测不出来：它们把 app.js
   塞进自己造的 vm/Function 里**自带 t 桩**，与真实页面的加载顺序无关。
   **静态检查器有桩 = 永远抓不到加载顺序问题**，只能在这里钉。

判据：
  ① i18n.js 必须在 app.js **之前**
  ② app.js 必须在 update.js / upstream.js / netdetail.js / realtime.js **之前**
     （它们要用 app.js 暴露的 api()/esc()）
  ③ app.js 顶层确实有 t() 调用（证明 ① 是必要的，不是洁癖）
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HTML = os.path.join(ROOT, 'web', 'index.html')
APP = os.path.join(ROOT, 'web', 'app.js')

FAILS = []
N = 0


def chk(name, cond, detail=''):
    global N
    N += 1
    if cond:
        print('  ok    ' + name)
    else:
        FAILS.append('%s  %s' % (name, detail))
        print('  FAIL  ' + name + '  ' + detail)


html = io.open(HTML, encoding='utf-8').read()

# ---- 抽出所有 <script src="…"> 的顺序 ----
order = []
for m in re.finditer(r'<script\s+src="([^"]+)"', html):
    url = m.group(1).split('?')[0].lstrip('/')
    order.append(url)

print('script 加载顺序: ' + ' → '.join(order))
print('')


def pos(name):
    return order.index(name) if name in order else -1


# ---- ① i18n.js 必须在 app.js 之前 ----
pi, pa = pos('i18n.js'), pos('app.js')
chk('i18n.js 与 app.js 都在 index.html 里', pi >= 0 and pa >= 0,
    'i18n.js=%d app.js=%d' % (pi, pa))
if pi >= 0 and pa >= 0:
    chk('⚠️ i18n.js 排在 app.js 之前（顺序写反会导致整站登录失效）',
        pi < pa, '实际 i18n.js 在第 %d 位、app.js 在第 %d 位' % (pi + 1, pa + 1))

# ---- ② 依赖 app.js 的模块必须在它之后 ----
for name in ('update.js', 'upstream.js', 'netdetail.js', 'realtime.js'):
    p = pos(name)
    if p < 0:
        chk('%s 在 index.html 里' % name, False, '没找到')
        continue
    chk('%s 排在 app.js 之后（要用 api()/esc()）' % name, p > pa,
        '实际第 %d 位' % (p + 1))

# ---- ③ app.js 顶层确有 t() 调用（证明 ① 是必要的）----
app = io.open(APP, encoding='utf-8').read()
# 顶层 = 不缩进（行首无空格）且是常量赋值形态，如 `  g: t('概览'),` 属于
# 某个顶层对象的字面量。用「顶层对象的值里出现 t('」来近似：
# ⚠️ app.js 已统一走 L11 的 shadow-safe 别名（大写 T），
#    这里要同时认大小写，否则会因纯命名迁移而假红。
#    判据立意不变：顶层（模块级）求值在 i18n.js 加载前就执行，
#    顺序错时 window.i18n 为 undefined -> 回落到同样 undefined 的 window.t -> 调用即抛。
top_t = re.findall(r'^\s{0,4}\w+:\s*[tT]\(', app, re.M)
chk('app.js 顶层（≤4 空格缩进）就有 t()/T() 调用 —— ① 是必要的',
    len(top_t) > 0, '一处都没找到；若代码结构变了请同步更新本判据')

# ---- ④ 反向说明：检查器自带 t 桩，所以抓不到顺序问题 ----
print('')
print('提示：node --check 与 check-render.js 都给 app.js 准备了 t 桩，')
print('      所以**它们永远抓不到加载顺序问题** —— 这条判据是唯一的防线。')

print('\n==== t-script-order: %d 条判据, %d 失败 ====' % (N, len(FAILS)))
if FAILS:
    for f in FAILS:
        print('  - ' + f)
    sys.exit(1)
print('SCRIPT_ORDER_OK')

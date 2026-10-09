# -*- coding: utf-8 -*-
"""钉住「所有真跑 app.js 的检查器都必须有 t() 桩」。

为什么需要这条（2026-10-05 的实际经过）：
  补完 i18n 后，app.js **顶层**就有 t('…') 调用了 —— 左侧导航的
  `g: t('概览')` 之类是模块加载时求值的常量。于是每个把 app.js 灌进
  vm / Function 求值的检查器都必须提供 t() 桩，否则在**第一行**就
  `ReferenceError: t is not defined`：

    check-render      → theme/design 抛 t is not a function（真机部署被拦）
    t-theme-parity    → TM_VARS 求值 ReferenceError（回归被拦）
    t-webterm         → g: t('概览') ReferenceError（部署被拦）

  三次都是**逐个被拦**，所以反过来加判据：新增检查器时若要跑 app.js，
  忘了桩会立刻红，而不是等部署失败才发现。

判据：
  ① 列出所有**真跑** app.js 的文件（vm.runInContext / new Function(app.js)）
  ② 每个都必须含 t 桩（`t:` / `const t =` / `.t =` 之一）
  ③ 列出全部 app.js 顶层 t() 调用数（应 > 0，作为「桩确实必要」的证据）
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

FAILS = []
N = 0


def chk(name, cond, detail=''):
    global N
    N += 1
    if cond:
        print('  ok    ' + name)
    else:
        FAILS.append('%s  %s' % (name, detail))
        print('  FAIL  %s  %s' % (name, detail))


# ---- ① 找出**真执行** app.js 的文件 ----
# ⚠️ 判据要分清「**执行**」与「**解析**」——
#    2026-10-05 第一版把两者混在一起，误报了两个：
#      check-jsstrip.js  只 new Function(src) 让 V8 **解析**（不执行）
#                          → 语法检查，不需要 t 桩
#      bench-appjs.js     只 readFileSync 测体积 → 完全不执行
#    真执行的标志是 runInContext / vm.runInThisContext，
#    或者 new Function(...) 之后**真的调用**了它。
RUNNERS = []
for d in (HERE, os.path.join(ROOT, 'devtools')):
    if not os.path.isdir(d):
        continue
    for fn in sorted(os.listdir(d)):
        if not fn.endswith('.js'):
            continue
        p = os.path.join(d, fn)
        try:
            s = io.open(p, encoding='utf-8').read()
        except Exception:
            continue
        if 'app.js' not in s:
            continue
        # 真执行：vm 里跑，或 new Function 之后调用了返回值
        vm_run = bool(re.search(r'runInContext|runInNewContext|runInThisContext', s))
        fn_called = bool(re.search(r'=\s*new Function\([^)]*\)\s*;?', s)) and \
            bool(re.search(r'\(\s*\)\s*;|\(\s*\)\s*\n|\(\s*\);', s))
        if vm_run or fn_called:
            RUNNERS.append((p, s))

print('真跑 app.js 的检查器: %d 个' % len(RUNNERS))
if not RUNNERS:
    chk('至少找到一个真跑 app.js 的检查器', False,
        '判据本身可能失效（app.js 路径或执行原语的名字变了）')
else:
    chk('至少找到一个真跑 app.js 的检查器', True)

# ---- ② 每个都要有 t 桩 ----
STUB = re.compile(
    r"""(?:\bt\s*:\s*(?:\(?\s*\w+\s*\)?\s*=>)|"""
    r"""\bconst\s+t\s*=|"""
    r"""\b(?:sandbox|sb|ctxObj|ctx|context)\.t\s*=|"""
    r"""\bt\s*=\s*\(\s*\w+\s*\)\s*=>\s*\w+)""")

for p, s in RUNNERS:
    rel = os.path.relpath(p, ROOT).replace('\\', '/')
    # t-i18n-run2.js 自己就是 i18n 判据，它 eval 的是 i18n.js 不是 app.js
    if rel.endswith('t-i18n-run2.js'):
        print('  skip  %s（i18n 判据本身，eval 的是 i18n.js）' % rel)
        continue
    # t-real-cn2.js 只是把 app.js 当**文本**做正则比对（inTransform() 里
    # indexOf/slice），并不 vm 执行它 —— 不需要 t 桩。判据按「提到 app.js
    # + 用了 runInContext」粗筛，这类纯文本分析脚本会被误判。
    if rel.endswith('t-real-cn2.js'):
        print('  skip  %s（只读 app.js 文本做正则，不执行）' % rel)
        continue
    # t-webterm.js 走 runInContext 但正文提到 app.js —— 它确实要桩
    chk('%s 有 t() 桩' % rel, bool(STUB.search(s)),
        'app.js 顶层有 t() 调用，缺桩会在第一行 ReferenceError')

# ---- ③ app.js 顶层 t() 调用数（桩确实必要的证据）----
app = io.open(os.path.join(ROOT, 'web', 'app.js'), encoding='utf-8').read()
# 粗略计数：t(' 出现总数（顶层 + 函数内都算，作为「规模」证据）
n_all = len(re.findall(r"(?<![A-Za-z0-9_$.])[tT]\('", app))
chk('app.js 里有大量 t() 调用（桩是必要的）', n_all > 100,
    '只有 %d 处' % n_all)
# NAV 那种顶层常量（g: t('概览')）—— 单独证明「加载即求值」
n_top = 0
for m in re.finditer(r"^(\s*)g:\s*[tT]\(", app, re.M):
    n_top += 1
chk('app.js 顶层常量里就有 t()（模块加载即求值）', n_top > 0,
    '没找到 g: t(...) 形态 —— 若代码结构变了，②的桩要求可以放宽')

print('\n==== t-t-stub: %d 条判据, %d 失败 ====' % (N, len(FAILS)))
if FAILS:
    for f in FAILS:
        print('  - ' + f)
    sys.exit(1)
print('T_STUB_OK')

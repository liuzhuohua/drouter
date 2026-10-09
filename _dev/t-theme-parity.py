#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
前后端校验一致性测试（#12 主题之家）

背景：为了「输入即时报错」的体验，前端 app.js 里镜像了一份取值校验与 CSS 校验
（tmCheckValue / tmCheckCss），后端 theme.py 里有权威实现（valid_css_value /
_check_extra_css）。两份实现一旦漂移，就会出现
「前端说没问题 → 点保存后端拒绝」或「前端拦住了正常值」这类体验问题。

本测试用同一份语料分别喂给两侧，逐条比对结论，
把「前端通过但后端拒绝」标为 ✗（用户会被后端打脸），
把「前端拒绝但后端通过」标为 ⚠（用户被误拦，功能不可用）。

运行：python3 router-build/_dev/t-theme-parity.py
"""
import io
import os
import re
import sys
import json
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BACKEND = os.path.join(ROOT, 'backend')
sys.path.insert(0, BACKEND)
import theme  # noqa: E402

NODE = os.environ.get('NODE_BIN') or 'node'

# ------------------------------------------------------------------ 语料
VALUES = [
    # 颜色
    '#fff', '#FFF', '#ffff', '#1f6feb', '#1f6febcc', '#12345678',
    '#zzz', '#12345', '#gg', '#', '##fff',
    'rgb(31,111,235)', 'rgb(0,0,0)', 'rgb(255,255,255)', 'rgb(256,0,0)', 'rgb(300,0,0)',
    'rgba(31,111,235,.35)', 'rgba(0,0,0,0)', 'rgba(0,0,0,1)', 'rgba(1,2,3)',
    'hsl(210,90%,55%)', 'hsl(0,0%,0%)', 'hsl(360,100%,100%)', 'hsl(361,50%,50%)',
    'hsla(210,90%,55%,0.5)', 'hsl(210deg,90%,55%)', 'hsl(210,101%,50%)', 'hsl(-10,50%,50%)',
    # 具名
    'transparent', 'currentColor', 'white', 'steelblue', 'red', 'rebeccapurple', 'borange',
    # 数值
    '12px', '1rem', '2.5em', '50%', '0', '10pt', '3ch', '2ex',
    # var()
    'var(--pri)', 'var(--pri, #fff)', 'var(--bg)', 'var(--unk)', 'var(', 'var()',
    # 渐变 / 阴影
    'linear-gradient(135deg,#2b7bf3 0%,#1f6feb 45%,#1552c0 100%)',
    'radial-gradient(circle,#fff,#000)',
    '0 1px 2px rgba(18,28,55,.05),0 2px 8px rgba(18,28,55,.05)',
    'inset 0 1px 0 #fff',
    # 攻击串
    'red;background:url(x)', '#fff}', '#fff{', 'url(http://evil.com/x.css)',
    'expression(alert(1))', '@import url(x)', '#fff\\', '/* */ #fff', '#fff !important',
    '#fff /* c */', '', '  ', 'none', 'auto', 'inherit', 'initial',
]

CSS_SAMPLES = [
    '.card{border-width:2px}',
    '.stat .val{font-size:22px}',
    '/* 带注释 */ .card{color:red}',
    '.card{color:#fff;background:var(--pri)}',
    'h3{letter-spacing:.5px}',
    '.a{color:red}.b{color:blue}',
    '.card{box-shadow:0 1px 2px rgba(0,0,0,.1)}',
    '.card{content:"x"}',
    '.card{background:url(x.png)}',
    '@import url("http://evil.com")',
    '@media screen{.card{color:red}}',
    '@font-face{src:url(x)}',
    '.card{color:red',
    '.card < > {color:red}',
    '.card{a;b:c}',
    '.card{background:expression(1)}',
    '.card{background:javascript:alert(1)}',
    '',
    '   ',
]

# ------------------------------------------------------------------ 前端求值
app_js = io.open(os.path.join(ROOT, 'web', 'app.js'), encoding='utf-8').read()


def grab(fn_name):
    """从 app.js 抽取某个顶层函数的源码（大括号配平法）"""
    i = app_js.index('function %s(' % fn_name)
    j = app_js.index('{', i)
    depth = 0
    for k in range(j, len(app_js)):
        if app_js[k] == '{':
            depth += 1
        elif app_js[k] == '}':
            depth -= 1
            if depth == 0:
                return app_js[i:k + 1]
    raise RuntimeError('未找到函数结尾：' + fn_name)


def grab_array(name):
    """抽取 `const NAME = [...];` 字面量（方括号配平）。

    ⚠️ 顶层常量改**惰性函数**之后（`const TM_VARS = () => ([...])`），
       只认 `const NAME = [` 会直接抛 ValueError —— 不是代码错了，
       是抽取器没跟上。两种写法都要支持。
    """
    pat = 'const %s = [' % name
    lazy = False
    if pat in app_js:
        i = app_js.index(pat)
    else:
        pat2 = 'const %s = () => ([' % name
        if pat2 in app_js:
            i = app_js.index(pat2)
            lazy = True
        else:
            raise RuntimeError('未找到数组声明：%s（试过 %r 与 %r）' % (name, pat, pat2))
    j = app_js.index('[', i)
    depth = 0
    for k in range(j, len(app_js)):
        if app_js[k] == '[':
            depth += 1
        elif app_js[k] == ']':
            depth -= 1
            if depth == 0:
                # 惰性写法是 `() => ([ ... ])` —— 方括号后面还有个 `)`，
                # 只补 `;` 会拼成 `() => ([...];` → SyntaxError。
                return app_js[i:k + 1] + (');' if lazy else ';')
    raise RuntimeError('未找到数组结尾：' + name)


harness = ("""
'use strict';
// ⚠️ 必须提供 t() 恒等桩 —— 2026-10-05 i18n 补全后，TM_VARS / TM_NAMED
//    里的中文标签被包成 t('…')，而这份 harness 是把 app.js 的字面量
//    原样贴进 Node 求值的，不给桩就是 ReferenceError: t is not defined。
//    用恒等桩（t(k) => k）而不是查真字典：这份判据只验 CSS 校验逻辑，
//    关心的是**变量名**（x[0]），与显示文案无关。
const t = (k) => k;
// ⚠️ T 是同一个翻译函数的**大写别名**（app.js 顶部：t 会被形参遮蔽时用 T）。
//    只桩 t 不桩 T 的话，贴进来的函数里一调 T() 就 ReferenceError
//    （2026-10-08：TM_VARS 改惰性后 tmCheckValue 里的 T 暴露了这个问题）。
const T = t;
const i18n = { getLang: () => 'zh-CN' };
// 直接用 app.js 自己的 TM_VARS / TM_NAMED 字面量（数组-of-数组形态），
// 避免因桩写错形态而误判：TM_VARS 元素的 x[0] 才是变量名。
@@TMVARSLIT@@
@@TMNAMEDLIT@@

@@TMCHECKVALUE@@

@@TMCHECKCSS@@

const out = {
  values: VALUES.map(v => [v, tmCheckValue(v)]),
  css: CSS_SAMPLES.map(c => [c, tmCheckCss(c)]),
  unit: {},
};
for (const vr of ['--r', '--side', '--side-c']) {
  for (const vl of ['12px', '12vw', '12vh', '2rem', '50%']) {
    out.unit[vr + '|' + vl] = tmCheckValue(vl, vr);
  }
}
console.log(JSON.stringify(out));
""".replace('@@TMVARSLIT@@', grab_array('TM_VARS'))
     .replace('@@TMNAMEDLIT@@', grab_array('TM_NAMED'))
     .replace('@@TMCHECKVALUE@@', grab('tmCheckValue'))
     .replace('@@TMCHECKCSS@@', grab('tmCheckCss')))

values_json = json.dumps(VALUES, ensure_ascii=False)
css_json = json.dumps(CSS_SAMPLES, ensure_ascii=False)
tmp = os.path.join(tempfile.gettempdir(), 'tm-parity-%d.js' % os.getpid())
io.open(tmp, 'w', encoding='utf-8').write(
    'const VALUES = %s;\nconst CSS_SAMPLES = %s;\n%s' % (values_json, css_json, harness))

import subprocess  # noqa: E402
p = subprocess.run([NODE, tmp], capture_output=True, text=True, encoding='utf-8')
if p.returncode != 0:
    print('前端求值失败：', p.stderr[:800])
    sys.exit(1)
fe = json.loads(p.stdout)
os.unlink(tmp)

fe_vals = {k: v for k, v in fe['values']}
fe_css = {k: v for k, v in fe['css']}

# ------------------------------------------------------------------ 比对
pass_n = fail_n = warn_n = 0
lines = []

print('== 取值校验一致性（valid_css_value vs tmCheckValue）==')
for v in VALUES:
    be_ok, be_why = theme.valid_css_value(v)
    fe_why = fe_vals.get(v, '<前端未返回>')
    fe_ok = (fe_why == '')
    if be_ok == fe_ok:
        pass_n += 1
        continue
    if not be_ok and fe_ok:
        fail_n += 1
        lines.append('  ✗ 前端放行但后端拒绝：%r\n        后端原因：%s' % (v, be_why))
    else:
        warn_n += 1
        lines.append('  ⚠ 前端误拦但后端允许：%r' % v)

# 单位限制（只对 --r / --side / --side-c）
print('== 单位限制一致性（--r / --side / --side-c）==')
for var in ('--r', '--side', '--side-c'):
    for val in ('12px', '12vw', '12vh', '2rem', '50%'):
        be_ok, _ = theme.valid_css_value(val, var)
        # 前端同参数
        fe_why = fe.get('unit', {}).get(var + '|' + val)
        if fe_why is None:
            continue
        fe_ok = (fe_why == '')
        if be_ok == fe_ok:
            pass_n += 1
        elif not be_ok and fe_ok:
            fail_n += 1
            lines.append('  ✗ %s=%r 前端放行但后端拒绝' % (var, val))
        else:
            warn_n += 1
            lines.append('  ⚠ %s=%r 前端误拦但后端允许' % (var, val))

print('== 自定义 CSS 校验一致性（_check_extra_css vs tmCheckCss）==')
for c in CSS_SAMPLES:
    be_bad, be_why = theme._check_extra_css(c)
    fe_why = fe_css.get(c, '<前端未返回>')
    fe_bad = (fe_why != '')
    be_ok = not be_bad
    fe_ok = not fe_bad
    if be_ok == fe_ok:
        pass_n += 1
        continue
    if not be_ok and fe_ok:
        fail_n += 1
        lines.append('  ✗ CSS 前端放行但后端拒绝：%r\n        后端原因：%s' % (c[:60], be_why))
    else:
        warn_n += 1
        lines.append('  ⚠ CSS 前端误拦但后端允许：%r' % c[:60])

print('== 关键安全串：两侧都必须拒绝 ==')
MUST_REJECT = [
    'red;background:url(x)', '#fff}', '#fff{', 'url(http://evil.com/x.css)',
    'expression(alert(1))', '@import url(x)', '#fff\\', '/* */ #fff', '#fff !important',
    'rgb(300,0,0)', 'hsl(361,50%,50%)', 'var(--unk)',
]
for v in MUST_REJECT:
    be_ok, _ = theme.valid_css_value(v)
    fe_ok = (fe_vals.get(v) == '')
    ok_pair = (not be_ok) and (not fe_ok)
    if ok_pair:
        pass_n += 1
    else:
        fail_n += 1
        lines.append('  ✗ 危险串 %r 未被两侧同时拒绝（后端ok=%s 前端ok=%s）'
                     % (v, be_ok, fe_ok))

MUST_REJECT_CSS = ['@import url("http://evil.com")', '.card{background:url(x.png)}',
                   '.card{background:expression(1)}', '@font-face{src:url(x)}']
for c in MUST_REJECT_CSS:
    be_bad, _ = theme._check_extra_css(c)
    fe_bad = (fe_css.get(c) != '')
    if be_bad and fe_bad:
        pass_n += 1
    else:
        fail_n += 1
        lines.append('  ✗ 危险 CSS %r 未被两侧同时拒绝（后端bad=%s 前端bad=%s）'
                     % (c[:40], be_bad, fe_bad))

print('== 清单一致性：前端副本必须等于后端权威表 ==')
drift = 0
# TM_VARS（app.js）的元素 x[0] 应正好等于 THEME_VARS 的键
fe_vars_names = re.findall(r"\[\s*'(--[a-z0-9-]+)'\s*,", grab_array('TM_VARS'))
be_vars_names = list(theme.THEME_VARS.keys())
only_fe = sorted(set(fe_vars_names) - set(be_vars_names))
only_be = sorted(set(be_vars_names) - set(fe_vars_names))
if only_fe or only_be:
    drift += 1
    lines.append('  ✗ TM_VARS 与 THEME_VARS 不一致：前端多 %s ｜后端多 %s'
                 % (only_fe, only_be))
else:
    pass_n += 1

# TM_NAMED 应正好等于 _NAMED
fe_named = re.findall(r"'([a-z]+)'", grab_array('TM_NAMED'))
only_fe = sorted(set(fe_named) - set(theme._NAMED))
only_be = sorted(set(theme._NAMED) - set(fe_named))
if only_fe or only_be:
    drift += 1
    lines.append('  ✗ TM_NAMED 与 _NAMED 不一致：前端多 %s ｜后端多 %s'
                 % (only_fe, only_be))
else:
    pass_n += 1
fail_n += drift

print()
print('=' * 60)
print('一致 %d 项｜不一致 %d 项（其中「前端误拦」%d 项）' % (pass_n, fail_n + warn_n, warn_n))
if lines:
    print('-' * 60)
    for l in lines[:60]:
        print(l)
print('=' * 60)
sys.exit(1 if fail_n + warn_n else 0)

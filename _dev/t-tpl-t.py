# -*- coding: utf-8 -*-
"""钉住「模板里的 t() 必须写在 ${…} 内部」。

⛔ 事故（2026-10-05，用户截图）：
       `t('可用 ') + ${mem.avail_mb} + ' MB　共 '`
   浏览器把整个模板当**普通字符串**渲染 —— `${…}` 不再是插值，页面上
   原样出现 `t('可用 ') + 3019 + ' MB　共 ' + 3921 + ' MB'`，
   用户反馈「没看懂什么意思」。**node --check 抓不到**（语法完全合法），
   47 视图的渲染回归也抓不到（它只验「抛不抛异常」）。

   正解：t() 写在 ${…} 内部 ——
       `${t('可用 ')}${mem.avail_mb}${t(' MB　共 ')}…`

判据：字面量定位**委托 Node 真词法**（_dev/scan-tokens.js，6860/6860
     已验证），再对每个 tpl token 判断「t( 前面有没有未闭合的 ${」。
     ⛔ **绝不要自己写 JS 词法分析** —— 2026-10-05 试了 5 版
     （3 版正则 + 2 版手写状态机），全部错：
       · 正则版：字面量里含 \\n 转义或 ${ 时切错位，把后续代码卷进模板；
       · 状态机版：漏了「${插值内含字符串}」→ 模板一直吞，597 处假红。
     Node 那套是唯一可信的。
"""
import io
import json
import os
import re
import subprocess
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
        print('  FAIL  ' + name + '  ' + detail)


def in_interp(seg, pos):
    """seg[pos] 处是 `t(`；它是否在 ${…} 内部。"""
    d = 0
    i = pos - 1
    while i >= 0:
        if seg[i] == '{' and i > 0 and seg[i - 1] == '$':
            d += 1
            i -= 2
            continue
        i -= 1
    return d > 0


_FILES = ('app.js', 'realtime.js', 'upstream.js', 'netdetail.js', 'update.js')
# ⭐ 一次 node 调用扫全部（见 t-i18n-cover.py 的说明：逐个 fork 会拖爆全量回归）
# 用**相对路径**（Node 的输出键就是命令行里给的字符串，# 换绝对路径会导致取不到）
_paths = ['web/' + f for f in _FILES]
_all = subprocess.run(
    ['node', os.path.join(HERE, 'scan-tokens.js'), '--multi'] + _paths,
    capture_output=True, check=True)
TOKS = json.loads(_all.stdout.decode('utf-8'))

for js in _FILES:
    path = os.path.join(ROOT, 'web', js)
    raw = open(path, 'rb').read()
    src = raw.decode('utf-8')
    chk('%s 无 CRLF' % js, b'\r\n' not in raw)
    chk('%s 无 U+FFFD' % js, chr(0xFFFD) not in src)

    toks = TOKS['web/' + js]   # Node 的键是命令行里给的路径
    # token 与源码必须逐条一致（偏移错位会让判据完全失真）
    mism = sum(1 for t in toks
               if raw[t['start']:t['end']].decode('utf-8', 'replace') != t['raw'])
    chk('%s 字面量定位与源码一致' % js, mism == 0, '%d 条错位' % mism)

    bad = []
    for t in toks:
        if t['kind'] != 'tpl':
            continue
        r = t['raw']
        for m in re.finditer(r"t\('", r):
            if not in_interp(r, m.start()):
                bad.append('L%d %s' % (t['line'], r[:86].replace('\n', ' ')))
                break
    chk('%s 的 t() 都在 ${…} 内' % js, not bad, '%d 处：%s' % (len(bad), bad[:2]))

# ---- 变量遮蔽：形参里有 t 的函数，函数体内不得用 t( ----
# 🔴 2026-10-07：tmThemeCard(t, on) 的形参 t 遮蔽了全局翻译函数，
#    渲染时抛 `t is not a function`（check-render 抓到的第一个异常）。
#    那 6 个函数里已改用别名 T —— 这里钉住，别改回去。
import re as _re
_app = io.open(os.path.join(ROOT, 'web', 'app.js'), encoding='utf-8').read()
_lines = _app.split('\n')
_fn = _re.compile(r'^(?:async )?function (\w+)\(([^)]*)\)')
_starts = [(i + 1, m.group(1), m.group(2)) for i, l in enumerate(_lines)
           if (m := _fn.match(l))]
_shadow = [(i, n) for i, n, ps in _starts
           if 't' in [x.strip() for x in ps.split(',')]]
chk('app.js 有 T 别名（遮蔽时的安全出口）',
    _re.search(r'^const T = window\.i18n', _app, _re.M) is not None)
chk('T 定义在使用之前（const 有 TDZ）',
    _app.index('const T = window') < _app.index('${T('))
_leak = []
for idx, (start, name, ps) in enumerate(_starts):
    if name not in [n for _i, n in _shadow]:
        continue
    end = _starts[idx + 1][0] - 1 if idx + 1 < len(_starts) else len(_lines)
    seg = '\n'.join(_lines[start - 1:end])
    if _re.search(r"\$\{t\('", seg):
        _leak.append('%s(L%d)' % (name, start))
chk('形参为 t 的函数体内不用 t()（用 T）', not _leak, str(_leak))

print('\n==== t-tpl-t: %d 条判据, %d 失败 ====' % (N, len(FAILS)))
if FAILS:
    for f in FAILS:
        print('  - ' + f)
    sys.exit(1)
print('TPL_T_OK')

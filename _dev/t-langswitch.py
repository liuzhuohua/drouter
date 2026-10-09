#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语言切换开关的接线检查。

为什么要有这个检查器：
  2026-10-07 真机故障 —— 用户点顶栏「中 / EN」毫无反应。
  根因不是 i18n 坏了，而是 `initLangSwitch()` **定义了却从未被调用**：
  它在 app.js 里只有定义那一处，全项目 0 个调用点 → #btn-lang 的 onclick
  永远是 null。静态语法检查、`node --check`、离线渲染回归全都测不出来
  （它们从不点击按钮）。

所以这个检查器只钉三件事：
  ① initLangSwitch 被真正调用（定义次数 < 总出现次数）
  ② #btn-lang 的 onclick 确实被赋值
  ③ i18n 侧被 app.js 用到的 API 都真实存在（少一个 → 点了没反应）
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, 'web', 'app.js')
I18N = os.path.join(ROOT, 'web', 'i18n.js')
HTML = os.path.join(ROOT, 'web', 'index.html')

PASS = FAIL = 0


def chk(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  [OK]   %s' % name)
    else:
        FAIL += 1
        print('  [NG]   %s %s' % (name, extra))


def _strip_comments(src):
    """粗略剔除 JS 行注释与块注释，只为计数用。

    ⛔ 不做这件事就会假绿：注释里提到函数名会撑大出现次数，
    掩盖「只有定义、没有调用」的死代码。
    """
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '/' and i + 1 < n:
            nxt = src[i + 1]
            if nxt == '/':                      # 行注释
                j = src.find('\n', i)
                if j < 0:
                    break
                out.append(' ' * (j - i))
                i = j
                continue
            if nxt == '*':                      # 块注释
                j = src.find('*/', i + 2)
                j = n if j < 0 else j + 2
                out.append(re.sub(r'[^\n]', ' ', src[i:j]))
                i = j
                continue
        out.append(c)
        i += 1
    return ''.join(out)


def main():
    app = io.open(APP, encoding='utf-8').read()
    i18n = io.open(I18N, encoding='utf-8').read()
    html = io.open(HTML, encoding='utf-8').read()

    print('=== 一、开关必须被调用（死代码是本次真机故障的根因）===')
    # ⛔ 必须**剔除注释**再计数：我在这段代码的注释里写了「initLangSwitch」，
    #    连注释一起数的话，删掉真正的调用后 n_all 仍然 > n_def → 判据假绿
    #    （2026-10-07 反向验证实测：删掉调用仍报 OK）。
    app_nc = _strip_comments(app)
    n_all = len(re.findall(r'\binitLangSwitch\b', app_nc))
    n_def = len(re.findall(r'function\s+initLangSwitch\s*\(', app_nc))
    n_call = len(re.findall(r'^\s*initLangSwitch\s*\(\s*\)\s*;', app_nc, re.M))
    chk('initLangSwitch 有且只有一处定义', n_def == 1, '实际 %d 处' % n_def)
    chk('initLangSwitch 至少被调用一次（剔除注释后统计）',
        n_call >= 1,
        '剔除注释后只有 %d 次出现 = 只有定义，没有调用 → onclick 永不绑定' % n_all)
    if n_call:
        for m in re.finditer(r'^[ \t]*initLangSwitch\s*\(\s*\)\s*;?', app_nc, re.M):
            ln = app_nc[:m.start()].count('\n') + 1
            print('         调用点 L%d: %s' % (ln, m.group(0).strip()))

    print('\n=== 二、按钮与标签存在 ===')
    chk('index.html 有 #btn-lang', 'id="btn-lang"' in html)
    chk('index.html 有 #lang-label', 'id="lang-label"' in html)

    print('\n=== 三、onclick 真的被赋值 ===')
    chk('app.js 给 btn.onclick 赋值', re.search(r'btn\.onclick\s*=', app) is not None)

    print('\n=== 四、i18n 侧 API 齐全（少一个就点了没反应）===')
    for api in ('toggle', 'getLang', 'onChange', 'rerenderAll', 'setLang', 'register'):
        chk('i18n 导出 %s' % api,
            re.search(r'\b%s\s*:' % re.escape(api), i18n) is not None)
    chk('i18n 有 window.i18n 暴露', 'window.i18n' in i18n)

    print('\n=== 五、切换后要重绘当前页（否则「点了变一半」）===')
    chk('onChange 回调里重绘当前页',
        re.search(r'onChange\s*\(\s*\(\)\s*=>', app) is not None
        and re.search(r'onChange[\s\S]{0,400}?go\(S\.page\)', app) is not None)

    print('\n========================================')
    print('通过 %d / 失败 %d' % (PASS, FAIL))
    if FAIL:
        sys.exit(1)
    print('LANG_SWITCH_OK')


if __name__ == '__main__':
    main()
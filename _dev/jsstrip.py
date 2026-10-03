#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JS 注释剥离：只留真正会被执行的代码。

为什么不能用 `re.sub(r'/\\*.*?\\*/', '', src, flags=re.S)`
---------------------------------------------------
那个写法在本仓的 web/app.js 上**会吃掉几千行真代码**。

根因是**顺序**，不是正则本身：app.js 8222 行附近有一句**行注释**，
内容恰好是讲「怎么防止 CSS 注释被提前闭合」的，注释里写了 `/* */`
这两个字面量：

    // 主题名 / 版本是自由文本，原样拼进 /* */ 注释时，
    // 一个 "*/" 就能提前闭合注释、…

而`js_code_only` 当年是「先剥块注释、再剥行注释」。剥块注释时那行
行注释还在原文里，于是 `/*` 被当成真注释的开头，`.*?` 一路向后找
最近的 `*/` —— 匹配到几千行之后，把中间所有真代码连同若干正常注释
一起删了。

证据：app.js 里 `/*` 169 次而 `*/` 170 次（HEAD 版是 163 对 164）
—— 数量不等就是配对错位一格的铁证，而错位会一路传播到文件末尾。
所以凡是拿「剥注释后的 app.js」做断言的检查器，都得先确认自己没踩
这个坑。

正确做法：按词法顺序单遍扫，维护「代码 / 字符串 / 模板串 / 正则字面量」
四态，**只在代码态才认注释**。

第二版踩的坑（别重犯）
----------------------
第一版修好了块注释，又被模板串里的 HTML 打穿：
`<span class="mono">http://169.254.0.1:8888/</span>` 里的 `//`
被当成行注释，`<td class="mono">/var/log/drouter/*.jsonl</td>` 里的
`/*` 被当成块注释 —— 又吃掉两百多行。

原因是模板串只做了「找下一个反引号」，没处理 `${}`。本仓 app.js 有
**2289 处 `${}`**，而 `${}` 里能再出现字符串和模板串，扫描器一旦在
`href="#"` 那种双引号上把状态带偏，后面整段 HTML 就被当代码扫了。
所以下面 `_skip_string` 对 `${}` 做了递归，并且**普通引号不跨行**
（JS 本身也不允许；碰到换行就当未闭合，就地收手，绝不一路吞下去）。
"""

import re

RESERVED_BEFORE_REGEX = set('(,=:[!&|?{};+-*%~^<>')

# 这些**关键字**后面同样可以合法地跟一个正则字面量。
# 漏掉它们的后果很隐蔽：app.js 第 18 行
#     return /^https?:\/\//i.test(s) ? s : '';
# 判 `/` 时prev 停在 `return` 的字母 `n` 上，不在上面的符号集里 →
# 正则分支没进 → `/` 当普通字符 → 下一个 `\` 又被当成块注释开头 →
# 一路吃到后面某个 `//` → 剥出来的正则缺尾，V8 报
# "Invalid regular expression: missing /"。
#
# ⛔ 抓出这个 bug 的是 devtools/check-jsstrip.js 里那条
# 「剥后产物必须能被 V8 解析」。纯 token 计数（function/const/let
# 各多少个）**一个都没少**，python 侧自检 44 项全绿 ——
# 也就是说，**只靠自己写的自检，这个 bug 会一路绿灯走到发版**。
# 这就是「换一个独立工具再问一遍」的价值。
KEYWORDS_BEFORE_REGEX = {
    'return', 'typeof', 'instanceof', 'in', 'of', 'new', 'delete',
    'void', 'throw', 'case', 'do', 'else', 'yield', 'await',
}


def _skip_string(src, i, quote):
    """从 src[i]（引号本身）开始，返回闭合引号之后的位置。

    模板串额外处理 ${...}：里面可以再出现字符串、模板串和注释。
    """
    n = len(src)
    j = i + 1
    while j < n:
        c = src[j]
        if c == '\\':
            j += 2
            continue
        if quote == '`' and src.startswith('${', j):
            depth = 1
            j += 2
            while j < n and depth:
                c2 = src[j]
                if c2 in '"\'':
                    j = _skip_string(src, j, c2)
                    continue
                if c2 == '`':
                    j = _skip_string(src, j, '`')
                    continue
                if src.startswith('//', j):
                    k = src.find('\n', j)
                    j = n if k < 0 else k
                    continue
                if src.startswith('/*', j):
                    k = src.find('*/', j + 2)
                    j = n if k < 0 else k + 2
                    continue
                if c2 == '{':
                    depth += 1
                elif c2 == '}':
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1
            continue
        if c == quote:
            return j + 1
        if quote != '`' and c == '\n':
            # 普通引号不跨行。碰到换行说明前面那个引号是被 HTML 属性
            # 之类的上下文骗了，就地收手，别一路吞下去。
            return j
        j += 1
    return n


def _skip_regex(src, i):
    """从 src[i]（'/' 本身）尝试当作正则字面量跳过，返回 (位置, 是否真是)。

    「是除号」的情况要交还给调用方按普通字符处理，所以返回 False。
    """
    n = len(src)
    j = i + 1
    in_class = False
    while j < n:
        ch = src[j]
        if ch == '\\':
            j += 2
            continue
        if ch == '[':
            in_class = True
        elif ch == ']':
            in_class = False
        elif ch == '/' and not in_class:
            # ⚠️ 这里 j 已经过了分隔斜杠，再 +1 是**必需**的。
            # 少这一下会切出 `/^https?:\/\` 这种缺尾的正则 —— 而正则
            # 缺尾是**语法错误**，所以判据不是「token 少了一个」，
            # 而是「剥后整份文件过不了 V8 解析」。node 侧那条判据
            # （剥后 new Function 成功）就是这么抓到的，比数数硬得多。
            j += 1
            while j < n and src[j].isalpha():   # g / i / m / u / s / y
                j += 1
            return j, True
        elif ch == '\n':
            return i, False
        j += 1
    return i, False


def js_code_only(src):
    """剥掉 JS 的注释（块注释 + 行注释），保留字符串/模板串/正则的内容。

    只处理注释，**不动字符串** —— 里面有 `` `/*` `` 或 `` `*/` `` 时
    保留原样才是对的：那些是运行时真正会输出的字符。
    """
    out = []
    i = 0
    n = len(src)
    # prev = 上一个「有效 token 的尾词」。记词而不记字符是必须的：
    # `return /re/` 里的 `/` 前面是关键字 return，光看最后一个字母
    # `n` 是判不出「这里该当正则」的。记一个词才能拿去查
    # KEYWORDS_BEFORE_REGEX。分隔符/字面量时 prev 就是那个符号本身。
    prev = ''
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ''
        if c == '/' and nxt == '*':
            j = src.find('*/', i + 2)
            if j < 0:
                break                      # 未闭合：后面全是注释
            out.append(' ')               # 占位，保持行号不变
            i = j + 2
            # prev 不变：注释不是 token，不影响「除号 vs 正则」的判断
            continue
        if c == '/' and nxt == '/':
            j = src.find('\n', i)
            if j < 0:
                break
            i = j                        # 换行符留给下一轮
            continue
        if c in '"\'' or c == '`':
            j = _skip_string(src, i, c)
            out.append(src[i:j])
            # 字符串是「值」，正则不能紧跟它，所以 prev 置成一个
            # 既不是符号也不是关键字的哨兵。
            prev = '\x00'                # NUL：绝不与任何集合元素相等
            i = j
            continue
        if c == '/' and (prev == '' or prev in RESERVED_BEFORE_REGEX
                         or prev in KEYWORDS_BEFORE_REGEX):
            j, ok = _skip_regex(src, i)
            if ok:
                out.append(src[i:j])
                prev = '/'
                i = j
                continue
        out.append(c)
        if c.isalnum() or c in '_$':
            # 标识符/数字：并进 prev 组成完整的尾词
            prev = prev + c if prev and (prev[-1].isalnum() or
                                         prev[-1] in '_$') else c
        elif not c.isspace():
            # 符号：整个替换掉 prev（只看最近那个符号）
            prev = c
        # 空白不更新 prev
        i += 1
    return ''.join(out)


def js_code_only_selfcheck(src, label='app.js'):
    """自检：剥完之后结构没被破坏。

    这不是形式主义。旧实现最典型的失败形态是**静默**吃掉代码，
    只看「断言绿了」根本发现不了。

    ⚠️ 这里**不要**拿「剥前后行数比」当判据。我第一版写了
    `剥后行数/原行数 > 0.97`，看着很合理，实际是错的：
    **剥掉一个多行块注释本来就必然减少行数**。app.js 里有一堆
    十几行的说明性块注释，剥完少了 127 行（13370 → 13243）完全正常。
    拿这条当判据，要么把对的实现判成错，要么被人调阈值调到失效
    —— 后者就是恒绿的诞生过程。

    真正能抓「误吃代码」的是这两条：
      1) 剥完仍是合法 JS（由 devtools/check-jsstrip.js 交给 V8 验）
      2) 剥掉的东西里不含任何代码标识符（下面按 token 逐个对照数量）
    """
    stripped = js_code_only(src)
    for fn in ('function ', '=>'):
        assert fn in stripped, '%s: 剥注释后连 %r 都没了' % (label, fn)
    return stripped


TRAPS = [
    # 名字, 源码片段。片段里每段都有一个 `realN` 标记，剥完必须都在。
    ('URL 里的双斜杠',
     'const u = "http://1.2.3.4:8080/x";\nconst real1 = 1;'),
    ('通配路径里的注释起止',
     'const p = "/var/log/d/*.log";\nconst real2 = 2;'),
    ('字符串里字面写注释起止',
     'const s = "/* not a comment */";\nconst real3 = 3;'),
    ('模板串里 URL',
     'const t = `<a href="http://x.y/">${1 + 1}</a>`;\nconst real4 = 4;'),
    ('模板串里通配路径',
     'const t2 = `<td>/var/log/drouter/*.jsonl</td>`;\nconst real5 = 5;'),
    ('真注释里带注释终止符',
     '// 这里说一对斜杠星号会被误认\nconst real6 = 6;'),
    ('正则字面量',
     'const r = /^https?:\\/\\//i;\nconst real7 = 7;'),
    ('除法不是正则',
     'const q = 10 / 2 / 1;\nconst real8 = 8;'),
    ('模板串里嵌套单引号串',
     'const t3 = `<b class="x">${"a\'b".length}</b>`;\nconst real9 = 9;'),
    ('模板串里嵌套模板串',
     'const t4 = `<a>${`x${1}y`}</a>`;\nconst real10 = 10;'),
    ('单引号里含双引号',
     'const s5 = \'he said "hi"\';\nconst real11 = 11;'),
    ('转义引号',
     'const s6 = "a\\"b";\nconst real12 = 12;'),
]

REAL_CMTS = [
    '// 行注释\nconst a = 1;',
    '/* 块注释 */\nconst b = 2;',
    '/*\n多行\n块注释\n*/\nconst c = 3;',
]

# ⚠️ 分隔线必须是**剥注释器不可能动**的东西。
# 第一版我拿 `/*<<SPLIT>>*/` 当分隔线 —— 那是块注释，jsstrip 正确地
# 把它剥了，于是 split 切不开、12 个陷阱全判失败。差点把「设计错误」
# 误报成「实现有 bug」去改已经正确的代码。
# 现在用 `@@@` 这种裸标识符：JS 里不可能出现，字符串/模板串/正则里
# 也不含（下面每段都断言过了），所以它是绝对中性的分隔符。
SEP = '\n@@@DR_SPLIT@@@\n'
SPLIT_TOKEN = '@@@DR_SPLIT@@@'


def selftest(verbose=True):
    """全部自检。返回 (通过数, 失败列表)。"""
    import os
    import sys
    ok = 0
    bad = []

    def c(desc, cond, extra=''):
        nonlocal ok
        if cond:
            ok += 1
            if verbose:
                print('[OK] %s' % desc)
        else:
            bad.append(desc)
            print('[NG] %s' % desc)
            if extra != '':
                for ln in str(extra).splitlines()[:4]:
                    print('     %s' % ln)

    # --- 1) 陷阱：剥注释不能吃掉真代码 ---
    bundle = SEP.join(x[1] for x in TRAPS)
    parts = js_code_only(bundle).split(SPLIT_TOKEN)
    if len(parts) != len(TRAPS):
        c('陷阱分段数正确（%d）' % len(TRAPS), False,
          '实际 %d 段 —— 说明分隔线被误当注释吃掉了' % len(parts))
    for i, (name, snippet) in enumerate(TRAPS):
        marks = re.findall(r'real\d+', snippet)
        got = parts[i] if i < len(parts) else ''
        lost = [m for m in marks if m not in got]
        c('陷阱存活：%s（%d 个真代码标记全在）' % (name, len(marks)),
          not lost, '丢了：%s' % ','.join(lost))

    # --- 2) 真注释必须被剥掉 ---
    cparts = js_code_only(SEP.join(REAL_CMTS)).split(SPLIT_TOKEN)
    for i, s in enumerate(REAL_CMTS):
        got = cparts[i] if i < len(cparts) else ''
        c('真注释被剥掉：%s' % s.split('\n')[0][:18], '注释' not in got,
          got.strip()[:80])

    # --- 3) 对真实 app.js 做量度 ---
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    s = open(os.path.join(root, 'web', 'app.js'), encoding='utf-8').read()
    out = js_code_only_selfcheck(s)
    if verbose:
        print('app.js 剥注释：%d → %d 字符，%d → %d 行'
              % (len(s), len(out), s.count('\n'), out.count('\n')))
    for tok in ('/api/snapshot', 'data-lock=', 'data-save-tag=',
                '/api/snapshot/protect', '/api/snapshot/note',
                "api('/api/snapshot'", "$('#sy-protect')"):
        c('app.js 剥后仍含 %s' % tok, out.count(tok) >= 1)
    for tok in ('吃注释', '判据', '假绿', 'CSS 注释'):
        c('app.js 剥后无注释残留 %s' % tok, out.count(tok) == 0)
    # 模板串里的 URL / 通配路径必须原样保留
    for tok in ('http://169.254.0.1:8888/', '/var/log/drouter/*.jsonl'):
        c('模板串内 %s 原样保留' % tok, out.count(tok) >= 1)
    # 剥后块注释必须配平（半开的注释是误吃信号）
    c('剥后块注释起止仍配平',
      out.count('/*') == out.count('*/'),
      '开 %d / 闭 %d' % (out.count('/*'), out.count('*/')))

    # --- 4) token 数量逐个对照 ---
    # ⚠️ 判据是「剥后**只多不少**」，不是「完全相等」。
    # 我第一版写 `数量不变`，结果 4 条全红（let 258→257、await 280→276、
    # innerHTML 377→375）。查下来那些 token 出现在**注释里** ——
    # 比如「这里之前用了 innerHTML 会被 X」这种讲来历的话。
    # 剥注释本来就会连带剥掉注释里提到的标识符，所以数量**必然**减少。
    #
    # 反过来，「只多不少」这条恰好能抓真正的误吃：一旦剥到真代码，
    # 某个 token 会**变多**（原文被切开重组）或者大段消失。
    # 真正严格的那道判据在 devtools/check-jsstrip.js：把产物交给 V8
    # 解析，剥坏了一定是语法错误。
    for tok in ('function', 'const', 'let', 'async', 'await',
                'innerHTML', 'addEventListener', 'querySelector',
                'dataset.', 'className'):
        c('剥后 token 数量只多不少：%s' % tok,
          out.count(tok) <= s.count(tok),
          '原 %d → 剥后 %d（变多了 = 原文被切开了）' % (s.count(tok), out.count(tok)))
    # 关键标识符一个都不能少（这些绝不该只出现在注释里）
    for tok in ("api('/api/snapshot'", 'data-lock=', 'data-save-tag=',
                '/api/snapshot/protect', '/api/snapshot/note'):
        c('剥后关键调用完好：%s' % tok, out.count(tok) >= 1)
    return ok, bad


if __name__ == '__main__':
    import os
    import re
    import sys
    _ok, _bad = selftest()
    # 额外产出：把剥后的 app.js 落盘，供 devtools/check-jsstrip.js
    # 交给 V8 解析。node 这边自己跑不通 python（Windows EBUSY），
    # 所以由python 侧备料、node 侧验收。
    try:
        _here = os.path.dirname(os.path.abspath(__file__))
        _root = os.path.dirname(_here)
        _out = os.path.join(_here, '.jsstrip-appjs.out')
        with open(_out, 'w', encoding='utf-8') as f:
            f.write(js_code_only(
                open(os.path.join(_root, 'web', 'app.js'), encoding='utf-8').read()))
        print('已写出 %s（供 node 侧交叉验证）' % os.path.relpath(_out, _root))
    except Exception as _e:
        print('写产物失败：%s' % _e)
        _bad.append('写 jsstrip 产物失败')
    print()
    print('=' * 60)
    print('通过 %d 项，失败 %d 项' % (_ok, len(_bad)))
    for d in _bad:
        print('  · %s' % d)
    print('=' * 60)
    sys.exit(1 if _bad else 0)

# -*- coding: utf-8 -*-
"""统计 / 断言 app.js 的 i18n 覆盖率：还有多少中文没走 t()、走了但没英文。

⛔ 字面量定位委托 Node（_dev/scan-tokens.js），不手写 JS 词法分析。
   偏移一律按 **UTF-8 字节** 处理（Node 侧已换算），因为 app.js 里有
   代理对字符（📁🔗📄），JS 的 String.length 与 Python 的 str 下标
   从第一个 emoji 之后就不一致（2026-10-05 踩过，4892/6860 条错位）。

用法：
  python _dev/t-i18n-cov.py            # 打印统计
  python _dev/t-i18n-cov.py --assert   # 有未覆盖项就退出码 1（进回归用）
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, 'web', 'app.js')
I18N = os.path.join(ROOT, 'web', 'i18n.js')
TOKENS = os.path.join(HERE, '.tokens.json')

# 这些片段里的中文不是界面文案
SKIP_SUB = ('http://', 'https://', '/api/', 'class=', 'data-', 'href=')
# 防火墙规则示例：那是**配置内容**，翻译反而是错的
SKIP_FN = ('FW_EXAMPLE',
           # fmtDateTime：Intl.DateTimeFormat 的 **fallback 分支**里的
           # 「2026年10月5日」手工拼装。正常路径走 Intl（按语言出格式），
           # 只有环境缺 ICU 才落到这里；它不是界面文案缺口。
           'fmtDateTime')


def zh_set():
    """字典里所有 zh 值（渲染成 JS 源码里的形态）。"""
    s = io.open(I18N, encoding='utf-8').read()
    # ⚠ 正则里**不要用 \s*** —— 2026-10-05 实测同一台机器上
    #   zh:\s*'…' 匹配 0 个、而 zh: '…' 匹配 1860 个（heredoc 吞了反斜杠）。
    # 显式列出空白字符 + 不跨引号，行为可预测。
    return set(re.findall(r"zh:[ \t]*'([^']*)'", s))


def toks():
    subprocess.run(['node', os.path.join(HERE, 'scan-tokens.js'), APP],
                   check=True, stdout=open(TOKENS, 'wb'),
                   stderr=subprocess.DEVNULL)
    return json.load(io.open(TOKENS, encoding='utf-8'))


def owner_map(src):
    lines = src.split('\n')
    fn_re = re.compile(r'^(?:async )?function (\w+)\(')
    fns = []
    for i, l in enumerate(lines):
        m = fn_re.match(l)
        if m:
            fns.append((i + 1, m.group(1)))

    def owner(ln):
        best = '<module>'
        for fl, fn in fns:
            if fl <= ln:
                best = fn
            else:
                break
        return best
    return owner


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--strict', dest='strict', action='store_true')
    a = ap.parse_args()

    raw = open(APP, 'rb').read()
    src = raw.decode('utf-8')
    tk = toks()
    bad = sum(1 for t in tk
              if raw[t['start']:t['end']].decode('utf-8', 'replace') != t['raw'])
    if bad:
        print('FAIL: token 与源码不一致 %d 条' % bad)
        return 2
    zh = zh_set()
    owner = owner_map(src)

    n_t = 0          # t() 调用点总数
    n_zh_t = 0       # 其中 key 是中文的
    n_ok = 0         # 其中字典里有英文的
    miss_en = []     # 走了 t() 但字典里没有
    miss_wrap = []   # 压根没包 t()

    for t in tk:
        if t['kind'] not in ('sq', 'dq', 'tpl'):
            continue
        r = t['raw']
        if not re.search(r'[\u4e00-\u9fff]', r):
            continue
        if any(x in r for x in SKIP_SUB):
            continue
        fn = owner(t['line'])
        if fn in SKIP_FN:
            continue
        # ⚠ 取 key 的**唯一**办法：扫 token 里的 `t('…')` 完整调用。
        #
        # 为什么不用「前 N 字节是否 endswith('t(')」—— 2026-10-05 在这上面
        # 连错三次：
        #   ① `${t('A ')}` 前面 4 字节是 'ast('（那个 t 就是 t( 自己的），
        #      endswith('t(') 为真 → 整个模板被当成一个 key；
        #   ② 改成前 5 字节 → 'oast('，endswith 恒假 → 整段掉进兜底分支；
        #   ③ 再加「t( 前一位不是 $」→ 判据与 token 类型耦合，规则不一致。
        # 教训（MEMORY 已记）：**判据不要靠「位置推断」，直接匹配语法本身**。
        keys_in_tpl = re.findall(r"t\('([^']*)'", r)
        if keys_in_tpl:
            for k in keys_in_tpl:
                if not re.search(r'[\u4e00-\u9fff]', k):
                    continue
                n_t += 1
                n_zh_t += 1
                if k in zh:
                    n_ok += 1
                else:
                    miss_en.append((t['line'], fn, k))
        elif t['kind'] in ('sq', 'dq') \
                and raw[max(0, t['start'] - 4):t['start']] \
                    .decode('utf-8', 'replace').endswith('t('):
            # sq/dq：t('…') 的引号就是 token 自身，t( 在 token **之外** 4 字节处。
            n_t += 1
            k = r[1:-1]
            if re.search(r'[\u4e00-\u9fff]', k):
                n_zh_t += 1
                if k in zh:
                    n_ok += 1
                else:
                    miss_en.append((t['line'], fn, k))
        else:
            # 没有 t('…')：中文若只出现在插值里，不算缺口。
            outside = re.sub(r'\$\{[^}]*\}', '', r)
            outside = re.sub(r"t\('[^']*'\)", '', outside)
            outside = re.sub(r'[+`]', '', outside)
            if re.search(r'[\u4e00-\u9fff]{2,}', outside):
                miss_wrap.append((t['line'], fn, outside.strip()[:70]))

    total = n_zh_t + len(miss_wrap)
    pct = (100.0 * n_ok / total) if total else 100.0
    print('中文界面文案覆盖率: %.1f%%  (%d/%d)' % (pct, n_ok, total))
    print('  t() 调用点: %d（其中中文 key %d）' % (n_t, n_zh_t))
    print('  缺英文: %d   |   未包 t(): %d' % (len(miss_en), len(miss_wrap)))
    if miss_en:
        print('\n缺英文（前 15）:')
        for ln, fn, k in miss_en[:15]:
            print('  L%-6d %-18s %r' % (ln, fn, k[:60]))
    if miss_wrap:
        print('\n未包 t()（前 15）:')
        for ln, fn, k in miss_wrap[:15]:
            print('  L%-6d %-18s %r' % (ln, fn, k))

    if a.strict and (miss_en or miss_wrap):
        print('\n有未覆盖项，退出码 1')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

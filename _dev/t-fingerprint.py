#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证：新增的前端文件都会被自动指纹化（不依赖真机）。

⚠️ 为什么要专门验：1.0.10 新增了 5 个 web/*.js，如果它们没被
   注入 `?v=<mtime>`，就会走 `no-cache` 分支 —— 浏览器每次都重拉，
   倒也不影响正确性；但**如果将来有人给它们加上长缓存**
   （`public, max-age=31536000, immutable`）而 URL 不带指纹，
   用户改了语言/更新了版本后会**永远看不到新内容**。
   所以现在就把「指纹化覆盖到新文件」钉住。

做法：抠出 _ASSET_RE，直接喂真实的 <script> 片段看有没有被替换。
"""
import io
import os
import re
import sys

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = io.open('backend/drouter-web.py', encoding='utf-8').read()

fails = []
n = 0


def ck(desc, cond, extra=''):
    global n
    n += 1
    print(('ok    ' if cond else 'FAIL  ') + desc + (' ' + str(extra) if extra else ''))
    if not cond:
        fails.append(desc)


# ---- 抠出 _ASSET_RE 的正则 ----
m = re.search(r'_ASSET_RE = re\.compile\(\s*\n?(.*?)\)\n', SRC, re.S)
if not m:
    print('✘ 找不到 _ASSET_RE')
    sys.exit(1)
pat_src = m.group(1).strip()
try:
    _r = eval(pat_src, {'__builtins__': {}}, {})
    # 抠出来的可能是裸 r'...'，也可能是 re.compile(r'...')。
    # eval 一层拿到的是**字符串**（re.compile 对象不会被 eval 出），
    # 所以要自己包一次 —— 我第一版忘了包，拿到字符串就用 .sub() 直接崩。
    import re as _re
    ASSET_RE = _r if hasattr(_r, 'sub') else _re.compile(_r)
except Exception as e:
    print('✘ 正则无法编译：%s' % e)
    print('   内容：%s' % pat_src)
    sys.exit(1)

print('正则：%s' % pat_src)
print()

# ---- 真机 index.html 里的实际脚本标签 ----
idx = io.open('web/index.html', encoding='utf-8').read()
scripts = re.findall(r'<script src="([^"]+)"', idx)
ck('index.html 里有脚本', len(scripts) > 0, len(scripts))

cache = {}


def ver(rel):
    if rel not in cache:
        f = os.path.join('web', rel.lstrip('/'))
        try:
            cache[rel] = int(os.path.getmtime(f))
        except OSError:
            cache[rel] = 0
    return cache[rel]


def sub(mm):
    pre, url, post = mm.group(1), mm.group(2), mm.group(3)
    if '?' in url:
        return mm.group(0)
    return '%s%s?v=%d%s' % (pre, url, ver(url), post)


print('--- 逐个验证是否被指纹化 ---')
NEW_FILES = ['/i18n.js', '/update.js', '/upstream.js',
             '/netdetail.js', '/realtime.js', '/app.js']
for s in scripts:
    if not s.endswith('.js'):
        continue
    tag = '<script src="%s"></script>' % s
    out = ASSET_RE.sub(sub, tag)
    fingered = '?v=' in out
    base = s.rsplit('.', 1)[-1]
    label = '新文件 %s' % s if s in NEW_FILES else '原有 %s' % s
    ck('%s 被指纹化' % label, fingered, out)

# ---- 反向：已带 ? 的不重复加 ----
tag = '<script src="/app.js?v=123"></script>'
out = ASSET_RE.sub(sub, tag)
ck('已带 ?v= 的不会被重复注入', out.count('?v=') == 1, out)

# ---- 边界：非 .js/.css 资源不处理 ----
tag = '<link href="/logo.svg">'
out = ASSET_RE.sub(sub, tag)
ck('.svg 不被处理（正则只匹配 js/css）', out == tag, out)

# ---- 关键：新文件必须真的存在于磁盘（否则 ver() 返回 0，注入 ?v=0 仍是长缓存 → 危险）----
print()
print('--- 新文件必须真实存在（否则 ?v=0 配长缓存 = 永不更新）---')
for f in NEW_FILES:
    p = os.path.join('web', f.lstrip('/'))
    ck('%s 存在' % f, os.path.isfile(p))
    mtime = ver(f)
    ck('%s 的 mtime 有效（非 0）' % f, mtime > 0, mtime)

print()
print('=' * 56)
print('指纹化检查 %d 条，失败 %d 条' % (n, len(fails)))
for f in fails:
    print('  FAIL: %s' % f)
sys.exit(1 if fails else 0)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入口页资源版本号注入（破浏览器缓存）的纯逻辑测试。

从 drouter-web.py 抽出 _ASSET_RE / _inject_asset_version 两个片段，
在隔离命名空间里执行，不导入整个后端（那会连数据库、读证书）。
"""
import ast
import os
import re
import sys
import tempfile

SRC = os.path.join(os.path.dirname(__file__), '..', 'backend', 'drouter-web.py')
code = open(SRC, encoding='utf-8').read()
tree = ast.parse(code)

want = {'_ASSET_RE', '_inject_asset_version'}
picked = []
for node in tree.body:
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in want:
                picked.append(node)
                break
    elif isinstance(node, ast.FunctionDef) and node.name in want:
        picked.append(node)

found = set()
for node in picked:
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                found.add(t.id)
    else:
        found.add(node.name)

missing = want - found
assert not missing, '未能从源码抽取：%s' % ', '.join(sorted(missing))

# 建一个假 WEB_DIR，里面放两个已知 mtime 的文件
tmp = tempfile.mkdtemp()
for name in ('app.js', 'app.css'):
    p = os.path.join(tmp, name)
    open(p, 'w', encoding='utf-8').write('/* %s */' % name)
    os.utime(p, (1_700_000_000, 1_700_000_000))

ns = {'os': os, 're': re, 'WEB_DIR': tmp}
# _inject_asset_version 从 1.0.9 起还会替换页脚版本号占位符，
# 所以要提供 app_version()。**漏了会 NameError** —— 而这个判据只
# 抽了单个函数、不是整个模块，模块级定义一个都带不过来。
# 凡是改了被抽函数的签名或新引用的模块级符号，替身必须同步补。
ns['app_version'] = lambda: '9.9.9'
exec(compile(ast.Module(body=picked, type_ignores=[]), SRC, 'exec'), ns)
inject = ns['_inject_asset_version']

HTML = ('<!doctype html>\n'
        '<link rel="icon" href="/logo.svg" type="image/svg+xml">\n'
        '<link rel="stylesheet" href="/app.css">\n'
        '<link rel="stylesheet" href="/theme.css?v=123">\n'
        '<span id="app-ver">v__APP_VERSION__</span>\n'
        '<script src="/app.js"></script>\n')

out = inject(HTML.encode('utf-8')).decode('utf-8')

fails = []


def ck(name, cond, extra=''):
    print('  %-42s %s %s' % (name, 'OK  ' if cond else 'FAIL', extra))
    if not cond:
        fails.append(name)


print('资源版本号注入：')
ck('app.css 追加了 ?v=', '/app.css?v=1700000000' in out)
ck('app.js 追加了 ?v=', '/app.js?v=1700000000' in out)
ck('已带查询串的 theme.css 不被改动', '/theme.css?v=123' in out and '/theme.css?v=123?v=' not in out)
ck('非 js/css 的 logo.svg 不动', '/logo.svg' in out and '/logo.svg?v=' not in out)
ck('不会重复追加（幂等）',
   inject(out.encode('utf-8')).decode('utf-8').count('/app.js?v=') == 1)
ck('输出仍是合法 utf-8 且保留 doctype', out.startswith('<!doctype html>'))
ck('单引号属性也能处理',
   "/app.css?v=" in inject(b"<link href='/app.css'>").decode('utf-8'))
# 1.0.9：页脚版本号占位符也要被替换掉。
# 漏了不会报错，只是页面底下一直显示 v__APP_VERSION__ 字面量。
ck('页脚 __APP_VERSION__ 被替换成真实版本号',
   '__APP_VERSION__' not in out and 'v9.9.9' in out)

print('\n结果：%d 项，失败 %d 项' % (8, len(fails)))
raise SystemExit(1 if fails else 0)

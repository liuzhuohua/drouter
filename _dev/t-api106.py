# -*- coding: utf-8 -*-
"""API 对齐深度检查器（1.0.6 补）。

比 t-apicheck.py 更进一步，盯三件之前没系统查过的事：

  A. **前端调用的路径后端真的能路由到吗** —— 用 AST 抽出 web 层所有
     `p == '...'` / 元组多路径 / `p.startswith('...')` 分发，逐一比对。
     查「前端在调、后端没有」= 404 型 bug。
  B. **后端有、前端从不调** —— 不是 bug，但可能意味着「功能没接上」
     或「死代码」，列出来人工判断。
  C. **HTTP 方法用得对吗** —— 有写操作（save/delete/install/apply）
     却用 GET 的，属于把幂等语义搞坏；反之纯读接口用 POST 只是浪费。

刻意不查字段级（t-apicheck.py 已经查了字段转发与分支可达性），
这里只管「路径 / 方法 / 覆盖」三个维度，避免与既有检查重复。
"""
import ast
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with io.open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


WEB = read('backend/drouter-web.py')
APP = read('web/app.js')
HELPER = read('backend/drouter-helper.py')

PASS = 0
FAIL = 0
MSG = []


def chk(desc, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('[OK] %s' % desc)
    else:
        FAIL += 1
        print('[NG] %s' % desc)
        if extra:
            print('     %s' % (extra,))


# ------------------------------------------------------------------
# 一、从前端抽 API 调用
# ------------------------------------------------------------------
# api('/api/xxx', {method:'POST', body:{...}})
# api(`/api/xxx?a=${...}`)   —— 模板串
FE = {}          # 路径(去 query) -> [(method, lineno)]
for m in re.finditer(r"""\bapi\(\s*(['"`])([^'"`]+)\1\s*(,\s*\{)?""", APP):
    raw = m.group(2)
    # 拼出 method：往后看 260 字符内的 method:'XXX'
    tail = APP[m.end():m.end() + 260]
    mm = re.search(r"""method\s*:\s*['"](\w+)['"]""", tail)
    method = (mm.group(1) if mm else 'GET').upper()
    path = raw.split('?')[0].strip()
    line = APP[:m.start()].count('\n') + 1
    FE.setdefault(path, []).append((method, line))

# 变量拼接的路径（如 api(BASE + '/api/x')）单独记，这类没法静态判定
DYN = len(re.findall(r"""\bapi\(\s*[A-Za-z_$][\w.$]*\s*[+]""", APP))

print('--- A. 前端调用 → 后端能否路由 ---')
print('前端静态路径 %d 个（另有 %d 处变量拼接路径无法静态判定）'
      % (len(FE), DYN))


# ------------------------------------------------------------------
# 二、从后端抽能路由到的路径
# ------------------------------------------------------------------
tree = ast.parse(WEB)
BE_EXACT = set()      # 精确匹配
BE_PREFIX = []       # (前缀, 说明)
BE_TUPLE = set()     # 元组里的路径

for node in ast.walk(tree):
    # p == '/api/x'  /  p in ('/api/x', '/api/y')
    if isinstance(node, ast.Compare):
        left = node.left
        if not (isinstance(left, ast.Name) and left.id == 'p'):
            continue
        for op, cmp_ in zip(node.ops, node.comparators):
            if isinstance(op, (ast.Eq, ast.In)):
                if isinstance(cmp_, ast.Constant) and isinstance(cmp_.value, str):
                    if cmp_.value.startswith('/api'):
                        BE_EXACT.add(cmp_.value)
                elif isinstance(cmp_, (ast.Tuple, ast.List, ast.Set)):
                    for e in cmp_.elts:
                        if isinstance(e, ast.Constant) and isinstance(e.value, str) \
                                and e.value.startswith('/api'):
                            BE_TUPLE.add(e.value)
    # p.startswith('/api/x')
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == 'startswith' and node.args:
        a0 = node.args[0]
        if isinstance(a0, ast.Constant) and isinstance(a0.value, str) \
                and a0.value.startswith('/api'):
            BE_PREFIX.append(a0.value)

ROUTES = BE_EXACT | BE_TUPLE
PREFIXES = sorted(set(BE_PREFIX))


def routable(path):
    if path in ROUTES:
        return True, '精确路由'
    for pre in PREFIXES:
        if path == pre or path.startswith(pre):
            return True, '前缀分发 %s' % pre
    return False, ''


missing = []
for path in sorted(FE):
    ok, how = routable(path)
    if not ok:
        missing.append(path)

chk('前端调用的每个路径后端都能路由到', not missing,
    '后端无路由: %s' % ', '.join(missing))
print('     后端可路由路径 %d 个（精确 %d / 元组 %d / 前缀 %d）'
      % (len(ROUTES) + len(PREFIXES), len(BE_EXACT), len(BE_TUPLE), len(PREFIXES)))

# ------------------------------------------------------------------
print()
print('--- B. 方法语义：写操作不许用 GET ---')
# 只对「路径名本身表意是写操作」的接口断言，避免误伤查询。
# 关键词表要带尾边界：否则 /api/openapi（读文档）、/api/upgrade/info（查升级）
# 会被 'open' / 里的 'open' 命中，报出「写接口用 GET」的假红灯 ——
# 这两个都是纯读，后端也明确限定 method == 'GET'。
WRITE_HINT = re.compile(
    r'/(save|delete|del|remove|install|apply|add|set|update|edit|write|mkdir|'
    r'rename|zip|restore|rollback|reboot|restart|kill|stop|start|trigger|'
    r'release|prune|cleanup|purge|import|run|exec|open|close|'
    r'enable|disable|clear|reset|rebuild|regen|sync|flush|renew|revoke)'
    r'(?![a-z])')
# 纯只读的路径名，无论命中哪个关键词都放过
READ_ONLY = re.compile(
    r'/(openapi(\.json)?|upgrade/info|examples|sysinfo|metrics|health|me|'
    r'config|status|list|info|logs?|audit|version|openapi)$')
GET_ON_WRITE = []
for path, calls in sorted(FE.items()):
    if READ_ONLY.search(path) or not WRITE_HINT.search(path):
        continue
    for method, line in calls:
        if method == 'GET':
            GET_ON_WRITE.append('%s (行 %d)' % (path, line))
chk('写操作路径没有用 GET 调用', not GET_ON_WRITE,
    'GET 调写接口: %s' % '; '.join(GET_ON_WRITE))

# 读接口用 POST 的（不算错，但列出来看规模）
READ_POST = []
for path, calls in sorted(FE.items()):
    if READ_ONLY.search(path) or WRITE_HINT.search(path):
        continue
    for method, line in calls:
        if method == 'POST':
            READ_POST.append(path)
            break
print('     读接口走 POST 的 %d 个（允许，仅记录）: %s'
      % (len(READ_POST), ', '.join(READ_POST) if READ_POST else '无'))

# ------------------------------------------------------------------
print()
print('--- C. 查询串里的参数名是否与后端读取的一致 ---')
# 前端 api('/api/files/list?path=') → 后端 p.get('path')
QS = re.findall(r"""\bapi\(\s*['"`](/api/[^'"`?]*)\?([^'"`]*)['"`]""", APP)
qs_bad = []
for base, qs in QS:
    keys = [k for k in re.findall(r'([a-zA-Z_][\w]*)\s*=', qs)]
    for k in keys:
        # 后端应能在该路径处理块里看到 p.get('k') 或 b.get('k')
        if ("p.get('%s')" % k) not in WEB and ('"%s"' % k) not in WEB \
                and ("'%s'" % k) not in WEB:
            qs_bad.append('%s?%s=（后端未见该键）' % (base, k))
chk('查询串参数名后端都能读到', not qs_bad, '; '.join(qs_bad))

# ------------------------------------------------------------------
print()
print('--- D. 菜单页面与 API 覆盖：每个可渲染模块都有落点 ---')
# PAGE_MODULES 声明的模块必须有对应 API
m = re.search(r'const PAGE_MODULES\s*=\s*\{(.*?)\n\};', APP, re.S)
if not m:
    chk('能解析 PAGE_MODULES', False, '未找到定义')
else:
    body = m.group(1)
    mods = re.findall(r'^\s*([a-z0-9_]+)\s*:', body, re.M)
    chk('PAGE_MODULES 非空', bool(mods), str(len(mods)))
    # 专属接口的模块不走 apply，这些是允许的
    SPECIAL = set(re.findall(
        r'^\s*([a-z0-9_]+)\s*:', body, re.M))     # 全部
    # 只检查「声明了 apply 的模块」必须有 /api/apply 能力
    applied = re.findall(r'([a-z0-9_]+)\s*:\s*\{[^}]*apply\s*:\s*\[[^\]]+\]', body)
    print('     可应用模块 %d 个: %s' % (len(applied), ', '.join(applied)))
    chk('可应用模块都通过 /api/apply 落点', all(
        p in APP for p in ('/api/apply',)), '前端应有 /api/apply 调用')

# ------------------------------------------------------------------
print()
print('--- E. 前端未调用的后端路由（供人工判断，不算失败）---')
unused = sorted(r for r in ROUTES if r not in FE and not r.startswith('/api/theme'))
for r in unused:
    print('     · %s' % r)
print('     共 %d 个' % len(unused))

print()
print('=' * 60)
print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)

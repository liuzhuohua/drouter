#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""前后端 API 契约交叉检查。

要抓的就是 1.0.5 那个 bug 的整类：前端 `api('/api/x', {method:'POST', body:{k:...}})`
发出去的字段，Web 层压根没读（`b.get('只')`），于是被静默丢掉、helper 拿到空值，
而接口还返回 ok —— 用户点了「安装」什么都没发生，却提示「全部依赖已安装完成」。

光靠 code review 抓不到这类：字段名两边都「看起来对」，只是不在同一层。
所以这里机械地把三层对齐：
    前端 body 字段  ×  Web 层实际读取的字段  ×  Web 层转发给 helper 的字段
另外还查：前端调了但后端根本没有的路由（404）、前端 body 用了变量无法静态解析的调用。

用法： python _dev/t-apicheck.py
退出码 0 = 全过。
"""
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, 'web', 'app.js')
WEB = os.path.join(ROOT, 'backend', 'drouter-web.py')
HELPER = os.path.join(ROOT, 'backend', 'drouter-helper.py')

FAIL = []


def t(name, cond, detail=''):
    print('  %s %s%s' % ('[OK]' if cond else '[NG]', name,
                         ('  -> ' + detail) if (detail and not cond) else ''))
    if not cond:
        FAIL.append(name)
    return 0 if cond else 1


# ---------------------------------------------------------------- JS 侧解析
def _match_brace(s, i):
    """s[i] == '{' 时返回配对 '}' 的下标。"""
    assert s[i] == '{'
    depth = 0
    while i < len(s):
        c = s[i]
        if c in '"\'':
            i = _skip_str(s, i)
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _skip_str(s, i):
    q = s[i]
    i += 1
    while i < len(s):
        if s[i] == '\\':
            i += 2
            continue
        if s[i] == q:
            return i + 1
        i += 1
    return i


def _obj_keys(obj):
    """取对象字面量的一层 key（跳过嵌套的 {} [] () 与字符串）。"""
    keys = []
    depth = 0
    i = 0
    cur = ''
    while i < len(obj):
        c = obj[i]
        if c in '"\'':
            j = _skip_str(obj, i)
            cur += obj[i:j]
            i = j
            continue
        if c in '{[(':
            depth += 1
        elif c in '}])':
            depth -= 1
        if c == ':' and depth == 1:
            k = cur.strip()
            m = re.match(r"^['\"]([^'\"]+)['\"]$", k.strip())
            if m:
                keys.append(m.group(1))
            elif re.match(r'^[A-Za-z_$][\w$]*$', k.strip()):
                keys.append(k.strip())      # 变量当 key：记录下来人工看
            cur = ''
            i += 1
            # 跳过值
            while i < len(obj) and obj[i] not in ',}':
                if obj[i] in '"\'':
                    i = _skip_str(obj, i)
                    continue
                if obj[i] in '{[(':
                    i = _match_any(obj, i)
                    continue
                i += 1
            continue
        if c == ',' and depth == 1:
            cur = ''
            i += 1
            continue
        cur += c
        i += 1
    return keys


def _match_any(s, i):
    pairs = {'{': '}', '[': ']', '(': ')'}
    close = pairs[s[i]]
    depth = 0
    while i < len(s):
        c = s[i]
        if c in '"\'':
            i = _skip_str(s, i)
            continue
        if c in pairs:
            depth += 1
        elif c in '}]).':
            if c in ')]}':
                depth -= 1
                if depth == 0 and c == close:
                    return i + 1
        i += 1
    return i


def parse_frontend(js):
    """返回 [(path, method, body_keys_or_None, line)]，只收字面量路径。"""
    out = []
    for m in re.finditer(r"""\bapi\(\s*(['"])(/[^'"]*)\1\s*,""", js):
        path = m.group(2)
        rest = js[m.end():]
        # options 对象
        mo = re.match(r'\s*\{', rest)
        if not mo:
            out.append((path, 'GET', None, js[:m.start()].count('\n') + 1))
            continue
        end = _match_brace(rest, mo.end() - 1)
        opts = rest[mo.end():end] if end > 0 else rest
        line = js[:m.start()].count('\n') + 1
        mm = re.search(r"method\s*:\s*['\"](\w+)['\"]", opts)
        method = (mm.group(1) if mm else 'GET').upper()
        mb = re.search(r'\bbody\s*:', opts)
        keys = None
        if mb:
            tail = opts[mb.end():].lstrip()
            if tail.startswith('{'):
                e2 = _match_brace(tail, 0)
                keys = _obj_keys(tail[1:e2]) if e2 > 0 else []
            else:
                keys = None       # body 是变量，静态解析不了
        out.append((path, method, keys, line))
    return out


# ------------------------------------------------------------- Python 侧解析
def parse_backend(py):
    """把 Web 层每个 `if p == '/api/x' and method == '...'` 块的原文切出来。

    也要认 `p.startswith('/api/files')` 这类前缀分发（文件管理 / Web 终端 /
    主题都走前缀），否则会把正常的子路由全报成「后端没这个接口」。
    """
    lines = py.split('\n')
    routes = {}
    prefixes = {}
    i = 0
    # 路由判断有三种写法：p == '/api/x'、p in ('/api/x', '/api/x/op')、
    # p.startswith('/api/files')。三种都要认，否则正常的子路由
    # 会被误报成「后端没这个接口」。
    start = re.compile(r"^\s*if\s+(?:p\s*==\s*|.*\bin\s*)\(?\s*'(/api/[^']+)'")
    pfx = re.compile(r"^\s*if\s+p\.startswith\(\s*'(/api/[^']+)'")
    while i < len(lines):
        m = start.match(lines[i])
        mp = pfx.match(lines[i])
        if m or mp:
            base = len(lines[i]) - len(lines[i].lstrip())
            j = i
            body = [lines[i]]
            while j + 1 < len(lines):
                nxt = lines[j + 1]
                if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= base:
                    break
                body.append(nxt)
                j += 1
            seg = '\n'.join(body)
            mm = re.search(r"method\s*==\s*'(\w+)'", lines[i])
            method = mm.group(1).upper() if mm else 'ANY'
            if mp and not m:
                prefixes.setdefault(mp.group(1), []).append((method, seg))
            else:
                # 同一行里可能列了多个路径（p in ('/a', '/a/op')），
                # 全部登记到同一个块上。
                paths = re.findall(r"'(/api/[^']+)'", lines[i])
                for pth in paths:
                    routes.setdefault(pth, []).append((method, seg))
            i = j + 1
            continue
        i += 1
    return routes, prefixes


def block_reads(seg):
    """Web 层在这个路由里读了哪些 body 字段。"""
    keys = set()
    for m in re.finditer(r"""b\.get\(\s*['"]([\w.-]+)['"]""", seg):
        keys.add(m.group(1))
    for m in re.finditer(r"""b\.pop\(\s*['"]([\w.-]+)['"]""", seg):
        keys.add(m.group(1))
    for m in re.finditer(r"""b\[['"](\w+)['"]\]""", seg):
        keys.add(m.group(1))
    if re.search(r"""['"]values?['"]\s*:""", seg) and 'json.loads' in seg:
        keys.add('*')          # 整包透传，无法逐字段核对
    # 整个 body 原样转发给 helper：self.helper('x', b) / dict(b, op=op)
    # 这种写法下 b 里的每个字段都到得了 helper，逐字段核对没有意义。
    if re.search(r"""self\.helper\(\s*['"][\w.-]+['"]\s*,\s*b\s*[,)]""", seg):
        keys.add('*')
    if re.search(r"""\bdict\(b\s*,""", seg):
        keys.add('*')
    # 路由直接委托给独立方法（self.migrate_preview() 等），
    # 字段读取在别处，静态看不出读没读 —— 交给该方法自己的测试去覆盖。
    if re.search(r"""\breturn\s+self\.[a-z_]+\(\)""", seg):
        keys.add('*')
    return keys


def block_forwards(seg):
    """Web 层转发给 helper 的 dict 里带了哪些键。"""
    keys = set()
    for m in re.finditer(r"""['"]op['"]\s*:""", seg):
        pass
    for m in re.finditer(r"""self\.helper\(\s*['"][\w.-]+['"]\s*,\s*\{(.*?)\}\s*,""", seg, re.S):
        for k in re.finditer(r"""['"]([\w.-]+)['"]\s*:""", m.group(1)):
            keys.add(k.group(1))
    return keys


def resolve_body_var(js, call_line, var):
    """body 传的是变量时，往上找最近一次 `var = {` 字面量赋值，把字段解析出来。"""
    head = '\n'.join(js.split('\n')[:call_line - 1])
    best = None
    for m in re.finditer(r'\b(?:const|let|var)?\s*%s\s*=\s*\{' % re.escape(var), head):
        best = m
    if not best:
        return None
    tail = head[best.end() - 1:]
    e = _match_brace(tail, 0)
    if e <= 0:
        return None
    return _obj_keys(tail[1:e])


def main():
    js = io.open(APP, encoding='utf-8').read()
    py = io.open(WEB, encoding='utf-8').read()
    calls = parse_frontend(js)
    routes, prefixes = parse_backend(py)

    def segs_of(path, method):
        out = [s for m, s in routes.get(path, []) if m in (method, 'ANY')]
        for pfx, lst in prefixes.items():
            if path.startswith(pfx):
                out += [s for m, s in lst if m in (method, 'ANY')]
        return out

    print('---- 前端调用 → 后端路由 ----')
    n = 0
    unknown = []
    dynamic = []
    multi = {}
    for path, method, keys, line in calls:
        multi.setdefault((path, method), []).append((keys, line))
    for (path, method), lst in sorted(multi.items()):
        segs = segs_of(path, method)
        if not segs:
            unknown.append('%s %s (app.js:%d)' % (method, path, lst[0][1]))
            continue
        n += t('%s %s 有后端路由' % (method, path), True)
    for u in unknown:
        n += t('前端调用的路由存在: %s' % u, False, '后端 drouter-web.py 里找不到这个路径')
    dyn = [c for c in calls if not c[0].startswith('/api/')]
    for d in dyn:
        dynamic.append(d[0])
    n += t('所有 api() 路径都是字面量（便于静态核对）', not dynamic,
           '这些是变量拼接，需人工确认: %s' % ', '.join(sorted(set(dynamic))[:8]))

    print('\n---- POST body 字段是否被 Web 层读到 ----')
    manual = []
    for (path, method), lst in sorted(multi.items()):
        if method != 'POST':
            continue
        segs = segs_of(path, method)
        if not segs:
            continue
        seg = '\n'.join(segs)
        reads = block_reads(seg)
        fwd = block_forwards(seg)
        for keys, line in lst:
            if keys is None:
                mv = re.search(r'\bbody\s*:\s*([A-Za-z_$][\w$]*)\s*[,}]', js[
                    js.find('\n', 0):][0:0] or '')
                manual.append('%s @%d' % (path, line))
                continue
            for k in keys:
                hit = (k in reads) or ('*' in reads) or (k in fwd)
                n += t('%s 传 %s 被读取/转发' % (path, k), hit,
                       'Web 层没读这个字段 —— 会被静默丢弃（keys/only 那类 bug）')
    if manual:
        print('  [--] %d 处 body 用变量，需人工核对: %s'
              % (len(manual), ', '.join(manual)))

    print('\n---- Web 层转发的字段名 helper 认不认 ----')
    hp = io.open(HELPER, encoding='utf-8').read()
    hread = set(re.findall(r"""p\.get\(\s*['"]([\w.-]+)['"]""", hp))
    hread |= set(re.findall(r"""payload\.get\(\s*['"]([\w.-]+)['"]""", hp))
    hread |= set(re.findall(r"""args\.get\(\s*['"]([\w.-]+)['"]""", hp))
    hread |= set(re.findall(r"""cfg\.get\(\s*['"]([\w.-]+)['"]""", hp))
    for path, lst in sorted(routes.items()):
        seg = '\n'.join(s for _, s in lst)
        fwd = block_forwards(seg)
        bad = [k for k in fwd if k not in hread and k != 'op']
        n += t('%s 转发的字段 helper 都认' % path, not bad,
               'helper 里找不到 p.get(%s)' % bad[0] if bad else '')

    print('\n---- 分支可达性：readings 之类的「通用表」不能吃掉写路由 ----')
    # 1.0.5 最大的 P0：dispatch 开头有一张 `readings` 字典同时匹配 GET 和 POST，
    # 而 /api/acl、/api/share、/api/docker、/api/ddns、/api/theme、/api/ulog
    # 六个模块的写分支在它之后 —— POST 永远走不到，返回的却是读结果 + ok:true，
    # 前端照样弹「保存完成」。静态比对路径存在性完全查不出来（路径确实在文件里），
    # 必须检查「谁先 return」。
    rpos = py.find('readings = {')
    guard = re.search(r'if p in readings and[^\n]*', py[rpos:rpos + 4000]) if rpos >= 0 else None
    n += t('存在 readings 通用只读表', bool(guard))
    if guard:
        g = guard.group(0)
        n += t('readings 拦截 POST 时有白名单排除（否则会遮蔽写路由）',
               "'POST'" not in g or '_has_post_branch' in g,
               'readings 仍在拦 POST 且没有排除名单 → 写路由不可达')
    # 每个「有写 op」的路径都必须有一条 method == 'POST' 的分支，
    # 且它自身不能只出现在 readings 表里。
    for pth in ('/api/acl', '/api/share', '/api/docker', '/api/ddns',
                '/api/theme', '/api/ulog'):
        segs2 = segs_of(pth, 'POST')
        has_write = any(re.search(r"""helper\(\s*['"](?:acl|share|docker|ddns|theme|ulog)['"]""", s)
                        and 'op' in s for s in segs2)
        n += t('%s 的 POST 写分支可达' % pth, has_write,
               '没有找到处理 op 的 POST 分支 —— 可能被 readings 遮蔽了')

    print('\n共 %d 项检查，失败 %d 项' % (n + len(FAIL), len(FAIL)))
    if FAIL:
        print('失败项：')
        for f in FAIL:
            print('  - ' + f)
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开工前预检：工作区有没有「上一轮注入没还原」的残留。

为什么需要这个文件：反向验证（注入 bug → 看判据变红 → 还原）如果被
SIGTERM/SIGKILL 打断，`finally` 里的还原不会执行，注入就**留在源码里**。
下一次跑反向验证时，它看到的「基线」已经被污染，于是报出一堆看不懂的红，
而真正的根因（几小时前那次没还原的注入）完全不在视野里。

2026-10-04 实际踩到：`drouter-helper.py` 里 `_vpn_env` 的
`os.path.join('/lib/modules', ...)` 被留成了 `'/nonexistent'`，
连带 `need_module` 整个状态从四态里消失 —— 而这个缺陷在
**已发布的 v1.0.8 里**。t-107 一直红着，但被当成了「判据本身有问题」。

所以这一条不是「保险」，是真bug 的直接成因。

判据只查两个方向：
  ① backend/ 下有没有明显的注入哨兵（/nonexistent、__INJECT__、127.0.0.2）
  ② 全量静态判据当前是否全绿（不绿就别动手，先弄清是哪一条、为什么）
只读，不改任何东西。
"""
import ast
import io
import os
import re
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
# 注入脚本惯用的假路径 / 假常量。真机代码里出现它们基本可以断定是残留。
SENTINELS = ('/nonexistent', '__INJECT__', '127.0.0.2', 'INJECTED_')

fails = []


def chk(label, cond, extra=''):
    print('[%s] %s %s' % ('OK' if cond else 'FAIL', label, extra))
    if not cond:
        fails.append(label)


print('=== 1. 扫 backend/ 里的注入哨兵 ===')
hits = []
for fn in sorted(os.listdir(os.path.join(ROOT, 'backend'))):
    if not fn.endswith('.py'):
        continue
    p = os.path.join(ROOT, 'backend', fn)
    try:
        src = io.open(p, encoding='utf-8').read()
    except Exception:
        continue
    # 只在**代码**里找，注释和 docstring 里为了说明历史 bug 常会写到
    code = re.sub(r'#.*$', '', src, flags=re.M)
    for s in SENTINELS:
        if s in code:
            for i, line in enumerate(src.splitlines(), 1):
                if s in line and not line.strip().startswith('#'):
                    hits.append('%s:%d  %s' % (fn, i, line.strip()[:70]))
chk('backend/ 里没有注入残留', not hits, '')
for h in hits:
    print('       %s' % h)
if hits:
    print('       ⚠ 这些多半是反向验证没还原。用 git diff 看清楚，'
          '确认是残留就 git checkout 还原，**别直接改成新值** —— '
          '新值可能才是 bug（/nonexistent 那次就差点被当成「本就该这样」）。')

print()
print('=== 2. backend/*.py 语法 ===')
bad = []
for fn in sorted(os.listdir(os.path.join(ROOT, 'backend'))):
    if not fn.endswith('.py'):
        continue
    p = os.path.join(ROOT, 'backend', fn)
    r = subprocess.run([sys.executable, '-m', 'py_compile', p],
                       capture_output=True)
    if r.returncode:
        bad.append('%s: %s' % (fn, r.stderr.decode('utf-8', 'replace')[:120]))
chk('backend/ 下所有模块语法正确', not bad, '')
for b in bad:
    print('       %s' % b)

# ---------------------------------------------------------------------------
# 2026-10-04 发 v1.0.9 时又踩了一次同类的坑，但**形态不一样**：
# t-107-inject-wg.py 注入的方式是把一整行真实代码注释掉 ——
#     -        'env': _vpn_env(),
#     +        # 'env': _vpn_env(),
# 这种注入**不含任何哨兵字样**（没有 /nonexistent、没有 __INJECT__），
# 所以上面第 1 项完全抓不到它。而且它语法合法、判据 t-107 会红，
# 很容易被误判成「上一版遗留的老问题」而放过。
#
# 判据：注释掉的代码行里如果**只差一个前导 #**（去掉 # 和空白后是合法 Python），
# 那它极可能是残留，而不是有意的说明 —— 有意的说明不会是一行完整可执行的代码。
# ---------------------------------------------------------------------------
print()
print('=== 3. 扫「被注释掉的整行代码」（无哨兵的注入形态）===')
susp = []
for fn in sorted(os.listdir(os.path.join(ROOT, 'backend'))):
    if not fn.endswith('.py'):
        continue
    p = os.path.join(ROOT, 'backend', fn)
    try:
        src = io.open(p, encoding='utf-8').read()
    except Exception:
        continue
    for i, line in enumerate(src.splitlines(), 1):
        body = line.strip()
        if not body.startswith('#'):
            continue
        inner = body[1:].strip()
        # 先排掉两类一看就不是代码的：
        #   ① 分隔线注释：# ------------ radvd
        #      （`-----` 会被 Python 解析成负号表达式，一不小心就误报）
        #   ② 纯中文/纯符号的说明文字
        if not inner or inner.startswith(('-', '=', '*', '>', '|', '+')):
            continue
        if not re.search(r'[A-Za-z_]', inner):
            continue
        #   ③ 值里带中文的「结构说明」注释：# module: (service, action, 中文名)
        #      长得像 dict 项，但 value 是中文 —— 那是给人看的文档，不是代码。
        if re.search(r'[\u4e00-\u9fff]', inner):
            continue
        cand = None
        # dict 项的键**通常带引号**（'env': / "env":），
        # 正则必须同时认标识符键和引号键 —— 早先只写 ^[A-Za-z_] 认不出
        # 'env': _vpn_env(), 这种最常见的形态，判据就成了摆设。
        if re.match(r"""^(?:[A-Za-z_][\w.]*|['"][^'"]+['"])\s*:\s*.+,$""", inner):
            cand = '{' + inner + '}'          # dict 项 → 补成 dict 字面量
        else:
            cand = inner
        # ⚠️ 不能用 ast.parse(inner) 直接判 —— dict 项 `'env': _vpn_env(),`
        # 单独拿去解析会 SyntaxError（没有大括号），真正的注入反而被放过。
        try:
            tree = ast.parse(cand)
        except SyntaxError:
            continue
        # 光「能解析」不够 —— 注释里写个 `# 123` 或 `# foo()` 也能解析。
        # 要求含**赋值、调用、return 或 dict 项**之一，纯字面量不算。
        if not (re.search(r'[A-Za-z_]\w*\s*\(', inner)            # 调用
                or re.search(r'^\s*(return|import|from|raise)\b', inner)  # 语句
                or re.search(r'^[A-Za-z_][\w.]*\s*=[^=]', inner)       # 赋值
                or re.match(r"""^\s*(?:[A-Za-z_][\w.]*|['"][^'"]+['"])\s*:""",
                             inner)):                              # dict 项
            continue
        if len(tree.body) != 1:
            continue
        susp.append('%s:%d  %s' % (fn, i, body[:72]))
chk('backend/ 里没有「注释掉的整行代码」残留', not susp, '')
for s in susp:
    print('       %s' % s)
if susp:
    print('       ⚠ 这一行去掉 # 就是一句完整可执行的代码 —— '
          '多半是反向验证把代码注释掉之后没还原。')
    print('       ⚠ 用 git diff 确认：是残留就 git checkout 还原，'
          '别直接删注释（删了就把注入固化成真 bug 了）。')

print()
print('=== 4. 换行符必须是 LF ===')
# .gitattributes 声明了 * eol=lf，但 Windows 上的工具（Edit 工具、某些脚本）
# 写文件时会整篇改成 CRLF。sync.sh 的预检会拦部署，但那时才发现太晚了。
crlf = []
for sub in ('backend', 'packaging', 'scripts', 'devtools', 'web'):
    d = os.path.join(ROOT, sub)
    if not os.path.isdir(d):
        continue
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(('.py', '.sh', '.js', '.css', '.html')):
            continue
        p = os.path.join(d, fn)
        try:
            with open(p, 'rb') as f:
                if b'\r\n' in f.read():
                    crlf.append('%s/%s' % (sub, fn))
        except Exception:
            pass
chk('源码里没有 CRLF（Windows 编辑器整篇改行尾的常见后果）', not crlf, '')

print()
print('=== 5. 「注释与代码矛盾」检测（2026-10-10 新增）===')
# 背景：反向验证被 SIGTERM 打断时，注入器可能已经把某处源码**换成了另一种写法**，
# 而原来的解释性**注释还留在原地**。于是形成：
#     # 正确写法：`(x.get('data') or {})`
#     out['holds'] = helper(...).get('data', {}).get('holds', [])   ← 有 bug
# 前 4 项都抓不到这种形态：
#   ① 哨兵扫描 —— 注入文本里没有哨兵字样
#   ② 语法检查 —— 语法完全正确
#   ③ 注释掉的整行 —— 这次是「替换」不是「注释掉」
#   ④ 换行符 —— 无关
# 1.0.10 真的中过：holds 那行被改回 `.get('data', {})`，而我写的
# P1-8 判据因为「and 第二个条件碰巧成立」而掩盖了问题。
# → 通用做法：凡是「注释里出现了正确写法」的，都验证代码里确实是它。
# ⚠️ 查代码时**必须连着上下文**（`res`/`x` 前缀 + 变量名一起），
#    不能只查 `.get('data') or {})` 这个片段 ——
#    文件里 VPN 相关代码有 4 处**合法**的 `(res.get('data') or {})`，
#    只查片段会匹配到它们，于是「holds 处坏了」也照样报 OK。
#    第一版就是这么写的，注入坏了验证两次都没抓到。
CONTRADICTIONS = [
    ('backend/drouter-web.py',
     r"正确写法：\s*`?\(x\.get\('data'\) or \{\}\)`?",
     r"helper\('pkg', \{'op': 'holds'\}\)\.get\('data'\)"
     r"\s*\n?\s*or \{\}\)",
     'holds 读取：注释说用 (x.get("data") or {})，代码必须也是'),
    ('backend/drouter-helper.py',
     r'必须放在\s*`?finally',
     r'finally:[\s\S]{0,200}?os\.remove\(tmp\)',
     '_verify 的临时文件清理必须在 finally'),
]
contra = []
for rel, want_cmt, want_code, desc in CONTRADICTIONS:
    p = os.path.join(ROOT, rel)
    if not os.path.isfile(p):
        continue
    try:
        with open(p, encoding='utf-8') as f:
            src = f.read()
    except Exception:
        continue
    if not re.search(want_cmt, src):
        continue
    # ⚠️ **必须先剥注释再查代码**：注释里那句「正确写法：`(x.get('data') or {})`」
    #    本身就匹配代码正则 —— 不剥的话检查永远认为「代码里有」，
    #    注入坏了也照样报 OK（第一版就栽在这，白验证一轮）。
    code_only = re.sub(r'#.*$', '', src, flags=re.M)
    if not re.search(want_code, code_only):
        contra.append('%s：%s' % (rel, desc))
chk('注释里声明的正确写法确实在代码里', not contra,
    '→ 注释与代码矛盾（很可能是反向验证的残留）：%s' % contra)
for c in crlf:
    print('       %s' % c)
if crlf:
    print('       ⚠ 用二进制读写把 \\r\\n 换回 \\n，别用会重写整个文件的文本编辑器。')

print()
print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % len(fails)))
sys.exit(1 if fails else 0)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**实际执行** drouter-web.py 的压缩协商函数，验证行为而不是文本。

t-static-perf.py 全是源码文本断言，能挡住「代码被改回去」，但挡不住
「代码还在、行为是错的」。这一份把 _accepts / _compress_cached /
_encode_body 抽出来真跑一遍，覆盖这些真实场景：

  - br 可用 + 客户端要 br → br
  - br 可用 + 客户端只要 gzip → gzip（**不能**给 br）
  - **br 模块缺失 + 客户端只要 br → 必须退到 gzip**（真机就是这个情况）
  - 客户端 br;q=0 → 不能给 br
  - 都不支持 → 原文
  - 同一文件先 gzip 后 br，两次结果都要正确（编码进键，不能互相覆盖）
  - 压不动的小响应 → 原文

⚠️ brotli 用**注入的假模块**而不是真装一个：真机上 python3-brotli 是
可选依赖，装了就测不出降级路径了。假模块要能真的编出合法 brotli 流，
否则解压那步会假失败。
"""
import ast
import io
import os
import sys
import types

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
fails = 0


def chk(label, cond, extra=''):
    global fails
    if not cond:
        fails += 1
    print('[%s] %s %s' % ('OK' if cond else 'FAIL', label, extra))


WEB_PATH = os.path.join(ROOT, 'backend/drouter-web.py')
WEB = io.open(WEB_PATH, encoding='utf-8').read()
TREE = ast.parse(WEB)

# ---- 抽函数（ast 精确取 def 节点，不用正则切）----
WANT = ('_accepts', '_want_gzip', '_want_brotli', '_compress_cached',
        '_brotli_cached', '_gzip_cached', '_encode_body')
nodes = []
for n in TREE.body:
    if isinstance(n, ast.FunctionDef) and n.name in WANT:
        nodes.append(n)
got = set(n.name for n in nodes)
chk('六个压缩函数都能定位到', got == set(WANT),
    '缺: %s' % (set(WANT) - got))

# 常量（GZIP_MIN / BROTLI_QUALITY / _COMP_MAX*）
consts = {}
for n in TREE.body:
    if isinstance(n, ast.Assign) and len(n.targets) == 1:
        t = n.targets[0]
        if isinstance(t, ast.Name) and t.id in (
                'GZIP_MIN', 'BROTLI_QUALITY', '_COMP_MAX', '_COMP_MAX_BYTES'):
            consts[t.id] = n.value
    # 缓存表是 AnnAssign/Assign 都可能
    if isinstance(n, ast.Assign) and len(n.targets) == 1:
        t = n.targets[0]
        if isinstance(t, ast.Name) and t.id in ('_comp_cache', '_comp_bytes'):
            consts[t.id] = n.value
chk('压缩相关常量都在模块顶层', len(consts) >= 4, '拿到 %s' % sorted(consts))

mod = ast.Module(body=[], type_ignores=[])
# 压缩链的必需依赖：**必须原样注入，不能用替身**。
# 教训：曾经漏注 _comp_lock，函数里一句 NameError 被 `except Exception`
# 吞成「返回原文」—— 压缩功能「完好」但从不生效，583KB 一直裸传，
# 而只看响应头的判据完全发现不了。
ns = {
    'os': os, 're': __import__('re'), 'gzip': __import__('gzip'),
    'threading': __import__('threading'),
}
for n in TREE.body:
    if isinstance(n, ast.FunctionDef) and n.name in WANT:
        mod.body.append(n)
    elif isinstance(n, ast.Assign) and len(n.targets) == 1 and \
            isinstance(n.targets[0], ast.Name) and n.targets[0].id in consts:
        mod.body.append(n)
# 锁不是白名单里的常量，但它是压缩链的硬依赖
ns['_comp_lock'] = ns['threading'].Lock()

exec(compile(mod, WEB_PATH, 'exec'), ns)

_accepts = ns['_accepts']
_encode_body = ns['_encode_body']
_comp_cache = ns['_comp_cache']


def hdr(v):
    return {'Accept-Encoding': v} if v is not None else {}


# ---- 1. _accepts：q 值语义 ----
for ae, want_gz, want_br, note in (
        ('gzip, deflate, br', True, True, '标准 Chrome 头'),
        ('gzip', True, False, '只认 gzip'),
        ('br', False, True, '只认 br'),
        ('br;q=0', False, False, '明确不要 br'),
        ('br;q=0.0', False, False, 'q=0.0 也不要'),
        ('br;q=0.5', False, True, 'q>0 就要'),
        ('gzip;q=0, br', False, True, '不要 gzip 但要 br'),
        ('', False, False, '空头'),
        ('identity', False, False, '只接受不压缩'),
):
    g = _accepts(hdr(ae), 'gzip')
    b = _accepts(hdr(ae), 'br')
    chk('Accept-Encoding %-20r → gzip=%-5s br=%-5s（%s）'
        % (ae, g, b, note), g == want_gz and b == want_br,
        '期望 gzip=%s br=%s' % (want_gz, want_br))

chk('缺 Accept-Encoding 头时两个都判 False',
    not _accepts(hdr(None), 'gzip') and not _accepts(hdr(None), 'br'))
chk('头是 None 时不抛异常', not _accepts({'Accept-Encoding': None}, 'br'))

# ---- 2. 无 brotli 模块 → 必须退到 gzip（真机就是这样）----
BIG = (b'drouter static asset payload. ' * 200)   # > GZIP_MIN


def run(data, ae, with_brotli):
    """在指定 brotli 可用性下跑一次 _encode_body。"""
    saved = sys.modules.get('brotli')
    if with_brotli:
        m = types.ModuleType('brotli')
        # 用真 brotli 未必装了；假实现只要稳定可逆，就能验证「选了哪条路」
        m.compress = lambda d, quality=None: __import__('gzip').compress(d, 9)
        sys.modules['brotli'] = m
    else:
        sys.modules.pop('brotli', None)
    _comp_cache.clear()
    ns['_comp_bytes'][0] = 0
    try:
        return _encode_body(hdr(ae), None, data)
    finally:
        if saved is None:
            sys.modules.pop('brotli', None)
        else:
            sys.modules['brotli'] = saved


def _sz(x):
    """安全取长度。被测代码返回 None 时**不能**让判据自己先崩 ——
    判据崩溃 = 退出码非 0 但没有 FAIL 行，看起来像「跑了」，
    实际上没人知道它验出了什么。反向验证时这条尤其致命：
    bug 明明抓到了，输出却是一片 OK。
    """
    try:
        return len(x)
    except TypeError:
        return -1


body, enc = run(BIG, 'gzip, deflate, br', False)
chk('**没有 brotli 模块时要退到 gzip，不是吐原文**',
    enc == 'gzip' and _sz(body) < len(BIG),
    'enc=%s %d→%s 字节' % (enc, len(BIG), _sz(body)))

body, enc = run(BIG, 'gzip, deflate, br', True)
chk('有 brotli 且客户端要 br → 用 br', enc == 'br', 'enc=%s' % enc)

body, enc = run(BIG, 'gzip', True)
chk('有 brotli 但客户端只要 gzip → 给 gzip（不能给 br）',
    enc == 'gzip', 'enc=%s' % enc)

body, enc = run(BIG, 'br', False)
chk('要 br 但没模块、且不支持 gzip → 原文（唯一正确选择）',
    enc is None and body == BIG, 'enc=%s' % enc)

body, enc = run(BIG, 'br;q=0, gzip', True)
chk('客户端 br;q=0 → 给 gzip，不给 br', enc == 'gzip', 'enc=%s' % enc)

body, enc = run(BIG, '', True)
chk('不支持任何压缩 → 原文', enc is None and body == BIG, 'enc=%s' % enc)

# ---- 3. 同文件两种编码互不覆盖 ----
_comp_cache.clear()
ns['_comp_bytes'][0] = 0
saved = sys.modules.get('brotli')
m = types.ModuleType('brotli')
m.compress = lambda d, quality=None: b'BROTLI' + __import__('gzip').compress(d, 1)
sys.modules['brotli'] = m
try:
    gz_body, gz_enc = _encode_body(hdr('gzip'), None, BIG)
    br_body, br_enc = _encode_body(hdr('br'), None, BIG)
    gz_again, gz_again_enc = _encode_body(hdr('gzip'), None, BIG)
finally:
    if saved is None:
        sys.modules.pop('brotli', None)
    else:
        sys.modules['brotli'] = saved

chk('同一份数据两种编码都能取到（键里带了编码，没互相覆盖）',
    gz_enc == 'gzip' and br_enc == 'br'
    and _sz(gz_body) > 0 and _sz(br_body) > 0
    and gz_body != br_body and _sz(br_body) >= 6
    and br_body[:6] == b'BROTLI',
    'gz=%s br=%s' % (gz_enc, br_enc))
chk('gzip 结果二次命中缓存（内容与首次一致）',
    gz_again_enc == 'gzip' and _sz(gz_again) > 0 and gz_again == gz_body)
chk('缓存里确实是两个独立条目', len(_comp_cache) == 2,
    '实际 %d' % len(_comp_cache))

# ---- 4. 压不动的小响应 ----
# ⚠️ 用**真正不可压缩**的数据。b'x'*100 这种单字符重复，gzip 能压到 20 字节，
# 反而更小，于是「压不划算」这条判据测的其实是另一件事（我第一次就踩了）。
# 这里用 os.urandom 模拟已加密/已压缩的载荷（图片、woff、zip 都是这德性）。
import random
_comp_cache.clear()
ns['_comp_bytes'][0] = 0
rnd = random.Random(42)
small = bytes(rnd.randrange(256) for _ in range(200))
body, enc = _encode_body(hdr('gzip'), None, small)
chk('压不划算的小响应直出原文', enc is None and body == small,
    'enc=%s %d→%s 字节' % (enc, len(small), _sz(body)))

# ---- 5. 缓存有上限，不会无限涨 ----
chk('缓存有条目数上限', ns['_COMP_MAX'] > 0)
chk('缓存有字节上限（24 条大响应就能吃掉上百 MB）',
    ns['_COMP_MAX_BYTES'] >= 4 * 1024 * 1024,
    '%d 字节' % ns['_COMP_MAX_BYTES'])

# ---- 6. 内部错误不许被兜底吞成「返回原文」 ----
# 这条是本文件最有价值的判据：压缩链里一个 NameError 曾被
# `except Exception` 吞掉，返回原文，压缩功能「完好」但从不生效。
# 表现和优化前一模一样，页面照常能开，只看响应头的判据也发现不了。
# 唯一的信号是「压缩没生效却没有任何异常」。
_comp_cache.clear()
ns['_comp_bytes'][0] = 0
saved_lock = ns['_comp_lock']
ns['_comp_lock'] = None          # 故意制造 AttributeError/TypeError
try:
    body, enc = _encode_body(hdr('gzip'), None, BIG)
    swallowed = (enc is None and body == BIG)
except Exception:
    swallowed = False
finally:
    ns['_comp_lock'] = saved_lock
chk('压缩链内部错误不会被静默吞成「返回原文」',
    not swallowed,
    '内部错误被 except 吞掉 → 压缩静默失效，583KB 裸传却无人察觉'
    if swallowed else '')

print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % fails))
sys.exit(1 if fails else 0)

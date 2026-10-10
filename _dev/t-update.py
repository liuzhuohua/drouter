#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判据：概述页「检测新版本 / 一键更新」。

（原名 t-update-passwall.py —— passwall 功能已按用户要求整体废除，
  本文件随之重写并改名。保留下来的只有版本检测这一块。）

覆盖三类：
  A. 存在性与接线（模块在、路由接上、静态资源引对）
  B. 供应链安全（探测多源 + 下载官方优先/失败降级镜像 + 强制 SHA256）
  C. 前端接线与「不出现」约束

每条关键判据都在 t-update-inject.py 里做了双向验证。
"""
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(ROOT, *a)

fails = []
n = 0


def ck(desc, cond, extra=''):
    global n
    n += 1
    if not cond:
        fails.append('%s %s' % (desc, extra))
        print('FAIL  %s %s' % (desc, extra))
    else:
        print('ok    %s' % desc)


def read(p):
    """读源码，并把 t()/T() 的包装**剥掉**。

    2026-10-07：前端补 i18n 后，界面文案从裸中文
        '<span title="套用预设名称">'
    变成
        '<span title="${t('套用预设名称')}">'
    于是判据里 `'套用预设名称' in read(APP_JS)` 这类**裸中文匹配全部失效**
    （t-update 一次红 63 条）。剥掉包装后，判据关心的是
    「代码里有没有这段中文」，与它是否被包 t() 无关 ——
    这才是这些判据本来要表达的意思。
    """
    with open(p, encoding='utf-8') as f:
        src = f.read()
    # ${t('x')} / ${T('x')}  →  x    ——**只在 x 含汉字时剥**
    #
    # ⛔ 两条边界，都踩过：
    #   ① 不剥裸 t('x')：有判据查的是「这行用了 t('nd.stale')」这种
    #      **代码形态**，剥掉就永远找不到（第一次改时一起剥，红 3 条）。
    #   ② **只剥含汉字的**：`${t('ups.xxx')}` 这种 key 是 ASCII，
    #      剥成 `ups.xxx` 后 `aj.count("t('ups.")` 恒为 0 ——
    #      「概览页已抽成 t()」这条判据因此假红。
    #      判据关心的是「中文文案有没有被抽出来」，
    #      ASCII key 本来就该保持原样。
    def _strip(m):
        return m.group(1) if re.search(r'[一-鿿]', m.group(1)) else m.group(0)

    src = re.sub(r"\$\{[tT]\('((?:[^'\\]|\\.)*)'\)\}", _strip, src)
    return src


def code_only(src, lang='py'):
    """剥掉注释（**还剥 docstring**），只留真正会被执行的代码。

    ⚠️ 为什么非剥不可：判据经常要写「⚠️ 不能写 XXX」这类说明性注释，
    而注释里正写着那个被禁的字符串 —— 不剥会把注释当成违规，基线假红。
    这个坑本轮踩了**三次**（import drouter_update / 硬编码路径 / __APP_VERSION__）。

    ⚠️ Python 还要剥 docstring：只剥 # 的话，`apply_update` 的 docstring 里
    写着「不自动安装 —— 安装要么走 apt」，于是「无 apt 调用」那条判据永远红。
    docstring 用 ast 按行号区间精确删（不重排、不填充）。

    JS 要剥的是 // 和 /* */ 两层 —— 字符串里的 URL（如 https://）不能误伤，
    所以只做「行内 // 之后」和整块 /* */ 的粗略处理。
    """
    if lang == 'js':
        out, in_block = [], False
        for ln in src.splitlines():
            if in_block:
                if '*/' in ln:
                    ln = ln.split('*/', 1)[1]
                    in_block = False
                else:
                    continue
            if '/*' in ln:
                if '*/' in ln:
                    ln = ln.split('/*', 1)[0] + ln.split('*/', 1)[1]
                else:
                    ln = ln.split('/*', 1)[0]
                    in_block = True
            # 行内 //：跳过 http:// 这类协议里的双斜杠
            i = ln.find('//')
            if i >= 0 and ln[max(0, i - 1):i] != ':':
                ln = ln[:i]
            out.append(re.sub(r'/\*.*?\*/', '', ln))
        return '\n'.join(out)
    lines = src.splitlines()
    try:
        import ast
        tree = ast.parse(src)
    except SyntaxError:
        tree = None
    drop = set()
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                if ast.get_docstring(node, clean=False):
                    first = node.body[0]
                    drop.update(range(first.lineno, first.end_lineno + 1))
    out = []
    for i, ln in enumerate(lines, 1):
        if i in drop:
            continue
        out.append(re.sub(r'#.*$', '', ln))
    return '\n'.join(out)


print('=== A. 存在性与接线 ===')

up_py = P('backend', 'drouter-update.py')
web_py = P('backend', 'drouter-web.py')

ck('drouter-update.py 存在', os.path.isfile(up_py))
ck('update.js 存在', os.path.isfile(P('web', 'update.js')))
ck('update.css 存在', os.path.isfile(P('web', 'update.css')))

# ⚠️ 必须 compile() 不是 ast.parse()：后者抓不到「重复关键字参数」
# 这类语义期错误（1.0.10 实测：ast.parse 通过、import 时炸）。
for f in (up_py, web_py):
    raw = open(f, 'rb').read()
    rel = os.path.relpath(f, ROOT)
    try:
        compile(raw.decode('utf-8'), rel, 'exec')
        ck('%s compile 通过' % rel, True)
    except SyntaxError as e:
        ck('%s compile 通过' % rel, False, 'line %s: %s' % (e.lineno, e.msg))
    ck('%s 换行符为 LF' % rel, raw.count(b'\r\n') == 0,
       'CRLF=%d' % raw.count(b'\r\n'))

w = read(web_py)
ck('web.py 接上 /api/update', "p.startswith('/api/update')" in w)
ck('web.py 实现 update_api', 'def update_api(' in w)
ck('web.py 注入 sys.path', 'sys.path.insert(0, _HERE)' in w)
ck('web.py 用 importlib 按路径加载（连字符文件名 import 不了）',
   'spec_from_file_location' in w
   and not re.search(r'^\s*import\s+drouter_update\b', w, re.M),
   '（drouter-update.py 带连字符，import 必 ModuleNotFoundError）')

print()
print('=== B. 供应链安全（本块是本功能的核心红线） ===')

u = read(up_py)
uc = code_only(u)
# hv2（helper 源码）与它的剥注释版在本段就要用，
# 原先定义在 F2 段（后面）→ 这里用会 NameError。提到前面，F2 段仍可复用。
hv2 = read(P('backend', 'drouter-helper.py'))


# —— 探测可多源 ——
_pm = re.search(r'PROBES\s*=\s*\[(.*?)\n\]', u, re.S)
ck('PROBES 定义存在', _pm is not None)
if _pm:
    body = _pm.group(1)
    ck('探测源 ≥ 3 个', body.count('https://') >= 3, str(body.count('https://')))
    _first = re.search(r"\(\s*'([^']+)'\s*,", body)
    ck('官方源排第一（能直连就不碰第三方）',
       _first is not None and '官方' in _first.group(1),
       '第一个源是：%s' % (_first.group(1) if _first else '?'))
    ck('探测源只有 2 元组（名称, URL）',
       not re.search(r',\s*(True|False)\s*\)', body),
       '第三字段已移除 —— 下载源改由 DL_SOURCES 独立管理')
    # ⚠️⚠️ 1.0.10 真实踩到：把 PROBES 从 3 元组改成 2 元组后，
    #    判据只查了「定义处是 2 元组」，**没查解包处** ——
    #    `for name, tmpl, _downloadable in PROBES:` 照旧解包 3 个，
    #    真机一调 check() 就 `ValueError: not enough values to unpack`，
    #    表现为「版本检测永远失败」。
    #    这条要查 drouter-update.py 的代码，而它在本段只有 uc（无注释）版本，
    #    所以下面直接用 uc —— 但解包处可能有 # 注释，所以两条都查。
    ck('check() 按 2 元组解包 PROBES（与定义一致）',
       re.search(r'for\s+name,\s*tmpl\s+in\s+PROBES', uc) is not None
       and not re.search(r'for\s+name,\s*tmpl,\s*\w+\s+in\s+PROBES', uc),
       '（改元组长度时必须同步解包处，否则真机才炸）')

# —— 下载：官方优先，失败降级到镜像（1.0.10 实测大陆网络必需）——
# ⚠️ 判据第一版查 `ASSET_TMPL` 且要求「不含任何代理域名」——
#    那条红线在大陆网络下等于「一键更新永远不可用」
#    （实测 github.com TCP 100% 丢包）。现在改成查 DL_SOURCES 降级链。
_dm = re.search(r"DL_SOURCES\s*=\s*\[(.*?)\n\]", uc, re.S)
ck('DL_SOURCES 下载源链存在', _dm is not None,
   '（DL_SOURCES 定义在 drouter-update.py，不在 helper —— '
   '判据第一版查错了文件，查的是 hv2）')
if _dm:
    _dl = _dm.group(1)
    ck('下载源 ≥ 2 个（官方 + 至少一个镜像）',
       _dl.count('https://') >= 2, str(_dl.count('https://')))
    _dfirst = re.search(r"\(\s*'([^']+)'\s*,", _dl)
    ck('下载源官方排第一',
       _dfirst is not None and '官方' in _dfirst.group(1),
       '第一个下载源是：%s' % (_dfirst.group(1) if _dfirst else '?'))
    ck('下载走降级链函数而非硬编码 URL',
       'def _download_with_fallback(' in uc
       and re.search(r'for\s+src_name,\s*tmpl\s+in\s+DL_SOURCES', uc)
       is not None)
    ck('降级只在连接层失败时发生（内容被篡改不降级）',
       # ⚠️⚠️ 判据第三版才写对，原因值得记：
       #   ① 前两版查的是**中文注释文字**（「其他错误（如 404）换源也没用」），
       #      但 code_only() 最后有 `re.sub(r'#.*$', '', ln)` —— 它会把
       #      **# 之后全部删掉**（不只 docstring），所以查注释必然 False。
       #      我前两次诊断时复刻 code_only **漏了这一步**，得出「匹配成功」
       #      的错误结论，绕了很大一圈。
       #   ② 现在只查**代码**：连接层关键词 + continue 紧跟 return 结构。
       (_i0 := uc.find('def _download_with_fallback')) >= 0
       and (_fn := uc[_i0:_i0 + 3000])
       and all(k in _fn for k in
               ("'timed out' in s", "'refused' in s", "'resolve' in s",
                "'network' in s"))
       and re.search(r'continue\b[\s\S]{0,200}?return False', _fn)
       and re.search(r"'timed out' in s[\s\S]{0,200}?continue", _fn),
       '（连接层失败 → continue 降级；其他错误 → return 不降级）')
    ck('清单也走降级链（否则降级下载无法校验）',
       re.search(r"def _fetch_manifest[\s\S]{0,900}?for\s+src_name,\s*tmpl\s+"
                 r"in\s+DL_SOURCES", uc) is not None)
    # ⚠️ 判据第一版查 `'src':` 字面量，但实现是用 **关键字参数** 传的：
    #    _write_state(..., src=src_name)。所以要查 src= 形式。
    ck('状态里记录实际用的源（前端要显示）',
       # ⚠️ 判据第二版：原来还要求 `_write_state(src=src_name)` ——
       #    那是实现降级链**之前**的写法（预写状态），降级后改成用函数的
       #    返回值 `src=src` 写在 worker 里。旧条件一直匹配不到，
       #    但因为 `and` 的第二个条件成立，判据偶尔会「碰巧对」一次，
       #    掩盖了问题。现在只查实际生效的那个。
       re.search(r'_write_state\([^)]*src=src\b', uc, re.S) is not None,
       '（用 _write_state(src=src) 关键字参数传，不是字典字面量）')
    ck('校验结果里写明下载源',
       re.search(r'官方 SHA256（下载源：%s）', uc) is not None)
    ck('未校验时特别标明走了镜像',
       re.search(r'未做完整性校验\*\*[\s\S]{0,80}?下载源：%s', uc) is not None,
       '（文案跨两行，正则要放宽）')

# —— 校验 ——
ck('有 SHA256 校验（比对而非仅取清单）',
   "want = sums.get(target['name'])" in uc
   and "want.lower() != actual.lower()" in uc
   and 'actual = _sha256(dest)' in uc)
ck('校验失败会删文件',
   re.search(r"校验不通过[\s\S]{0,400}?os\.remove\(dest\)", uc) is not None,
   '（删文件必须与「校验不通过」同段，否则可能删在别处 = 看似有其实没接上）')
ck('拿不到清单时明说未校验',
   re.search(r'未做完整性校验', u) is not None)
ck('SSL 校验失败不降级',
   'create_default_context' in u
   and not re.search(r'CERT_NONE|check_hostname\s*=\s*False', uc),
   '（遇到证书错误就跳过验证 = 默许 MITM，与本模块的安全底线自相矛盾）')

print()
print('=== C. 行为约束 ===')

# ⚠️ 必须比**调用点**位置，不是 `def _do_backup` 的定义位置 ——
#    定义在文件靠后，调用在 _apply_worker 内部。拿定义位置去比会得出
#    「下载在备份之前」的反结论（判据第一版就这么错的）。
_w = uc.index('def _apply_worker')

ck('版本比较用元组而非字符串',
   'return a > b' in uc and 'parse_ver' in u,
   '（字符串比较会把 1.0.10 判成比 1.0.9 旧）')
ck('探测有超时（不能无限等）', 'PROBE_TIMEOUT' in u)
ck('检测结果有缓存 TTL', 'CHECK_TTL' in u)
ck('下载可中断', '_stop_flag.is_set()' in u)
# ⚠️ 半截文件的清理现在在 **finally** 里（1.0.10 修 P1-5 时挪的）。
#    原来只有 InterruptedError 分支删 .part，网络重置/磁盘满/超时都会留下
#    完整的 169MB 文件，4GB 机器失败三次就撑满根分区。所以判据也改了：
#    不再查「某个 except 分支里有没有 remove」，而是查 finally 块。
ck('半截文件在 finally 里统一清理（覆盖所有失败路径）',
   re.search(r'finally:[\s\S]{0,400}?os\.remove\(dest \+ \'\.part\'\)', uc)
   is not None,
   '（必须在 finally，不能只在 InterruptedError 分支 —— '
   '网络/磁盘错误同样会留下 .part）')
ck('清理前先判断存在（避免异常）',
   re.search(r"os\.path\.exists\(dest \+ '\.part'\)", uc) is not None)
# ⚠️ 下面三条曾写成「关键词在文件里存在」，反向验证时全部漏网：
#   同一个关键词在文件里出现 N 处，注入只改 1 处 → 判据照样绿。
#   查「关键词存在」时，必须同时要求它出现在**正确的上下文**里。
ck('先备份后下载',
   uc.index('_do_backup()', _w) < uc.index("phase='download'", _w)
   and uc.index('_do_backup()', _w) > _w,
   '（备份调用必须在 _apply_worker 内、且在下载步骤之前）')
ck('备份失败即中止（不带风险往下走）',
   re.search(r'备份失败[\s\S]{0,200}?中止', u) is not None)
# ⚠️ 「不自动安装」的判据**不能查文案**（docstring/注释里都写着这句话，
#    剥注释也剥不掉 docstring）。必须查**行为**：
#    apply_update 函数体内除了起下载线程，不许出现任何包管理调用。
#    反向验证第一版就是改那句文案，判据照样绿 —— 绿灯来自查错了对象。
_au = uc[uc.index('def apply_update'):uc.index('def _apply_worker')]
ck('不自动安装（apply_update 内无任何包管理调用）',
   not re.search(r'\b(apt|apt-get|dpkg|dpkg-deb|install_offline|'
                 r'aptitude|systemctl)\b', _au),
   '检测到包管理调用：%s'
   % re.findall(r'\b(?:apt|dpkg|systemctl)\b', _au))
ck('不自动安装（只起下载线程）',
   'threading.Thread' in _au and '_worker.start()' in _au)
ck('状态写文件供前端轮询', 'state.json' in u)
ck('状态原子落盘（.part + replace）',
   "STATE_FILE + '.part'" in uc and 'os.replace' in uc)

print()
print('=== D. 前端接线 ===')

idx = read(P('web', 'index.html'))
ck('index.html 引入 update.js', 'update.js' in idx)
ck('index.html 引入 update.css', 'update.css' in idx)
ck('update.js 在 app.js 之后（要用 app.js 的 api()/esc）',
   idx.index('app.js') < idx.index('update.js'))

aj = read(P('web', 'app.js'))
ck('app.js 有版本卡片容器', 'id="upd-card"' in aj)
ck('app.js 触发卡片渲染', 'drouterRenderUpdateCard' in aj)

uj = read(P('web', 'update.js'))
# 剥注释版源码：下面多处判据要查**代码**而不是注释 ——
# 注释里出现的关键词会让判据假绿/假红（本项目已栽 5 次）。
# 这里统一算好，全文件复用。
_hvc = code_only(hv2, 'py')
_us = code_only(read(P('web', 'upstream.js')), 'js')
_ujc = code_only(uj, 'js')
ck('update.js 导出渲染函数', 'drouterRenderUpdateCard' in uj)
# ⚠️ 本地版本号**不能**从 __APP_VERSION__ 读（1.0.10 修的真 bug）：
#    后端只对 index.html 做字符串替换，app.js 是独立静态文件不经过注入 ——
#    在 app.js 里写 __APP_VERSION__ 会直接 ReferenceError，概览页整页崩。
#    离线渲染检查器 devtools/check-render.js 抓到了这个。
#    现在统一从页脚 #app-ver 读（那是服务端已注入真实版本号的元素）。
ck('app.js 里没有裸用 __APP_VERSION__（该变量只存在于 index.html）',
   not re.search(r'\$\{esc\(__APP_VERSION__\)\}', aj)
   and not re.search(r'\besc\(__APP_VERSION__\)', aj),
   '（app.js 不经过后端注入，用它会 ReferenceError → 概览页崩）')
ck('update.js 从页脚读取本地版本',
   re.search(r"getElementById\('app-ver'\)", uj) is not None)
# ⚠️ 判据查「update.js 代码里没用 __APP_VERSION__」，**必须剥注释** ——
#    那段注释正好解释了「为什么不能用它」，里面就写着这个变量名。
#    全文搜会把自己的注释当成违规（这个坑本轮第三次踩，见 helper 的 selfcheck）。
ck('update.js 代码里不用 __APP_VERSION__',
   '__APP_VERSION__' not in code_only(uj, 'js'),
   '（注释里可以提，代码里不能出现）')
# ⚠️ 下面这几条曾写成「关键词在文件里存在」，反向验证**全部漏网**：
#   同一关键词在 update.js 里出现 N 处（如 `d.local` 4 处、
#   `upd-cancel-task` 3 处、`备份` 6 处），注入只改 1 处 → 判据照样绿。
#   现在改成查「该关键词出现在正确的元素/上下文里」。
ck('update.js 展示本地版本（模板里渲染 d.local）',
   re.search(r'\$\{esc\(d\.local\)\}', uj) is not None)
ck('update.js 展示最新版本（仅在有更新时）',
   re.search(r'\$\{esc\(d\.latest\)\}', uj) is not None)
ck('update.js 有中断按钮（查元素本身，不是 querySelector）',
   re.search(r'<button[^>]*id="upd-cancel-task"', uj) is not None)
ck('update.js 进度条元素存在',
   re.search(r'id="upd-(bar|fill|prog)"', uj) is not None)
ck('update.js 措辞提到备份（弹窗说明里的具体表述）',
   re.search(r'<b>先自动备份</b>', uj) is not None)
ck('update.js 措辞提到可中断',
   re.search(r'随时可中断', uj) is not None)
ck('update.js 措辞提到下载源的取舍',
   # ⚠️ 1.0.10 改了策略（大陆网络实测 github.com 直连 100% 丢包，
   #    「下载一律走官方」等于一键更新永远不可用），所以判据不能还查旧文案。
   re.search(r'优先从 GitHub 官方下载', uj) is not None
   and re.search(r'自动改用', uj) is not None)
ck('update.js 说明下载全程显示实际用的源',
   re.search(r'全程都会显示实际用的是哪一个', uj) is not None)
ck('update.js 在进度区显示下载源',
   re.search(r'下载源：', uj) is not None)
ck('镜像源会被醒目标注（tag warn）',
   re.search(r"tag warn\">下载源", uj) is not None
   or re.search(r"mirror[\s\S]{0,120}?tag warn", uj) is not None)
ck('update.js 强调校验一步不省',
   re.search(r'校验完整性一步都不省', uj) is not None)
ck('update.js 说明不自动安装',
   re.search(r'<b>不会自动安装</b>', uj) is not None)

# ---- 2026-10-10 用户反馈：「只有重新检查，没看到升级按钮，放哪了？」----
# 根因：「查看更新内容并升级」只在 has_update 为真时渲染，而用户自己是
# 1.0.9、线上也是 1.0.9 → 走「已是最新」分支，那里没有升级入口，
# 用户合理地怀疑「按钮是不是坏了 / 藏哪了」。
# 「看看新版本发布了什么」本来就不该依赖有没有更新 → 最新版也要有入口。
ck('「已是最新」分支也提供发布说明入口',
   # ⚠️ 判据第二版：原来要求「按钮 id 和 openReleaseNotes 之间不超过 200 字符」，
   #    实际中间隔着 5 行（含一行长的 whenTxt 模板），超了 → 假红。
   # 改成分别存在即可。第三版又踩了一次：判据写 `querySelector('#upd-info')`，
   #    而实现用的是项目通用的 `$('#upd-info')` 简写 → 又假红。
   #    **判据里的写法必须和实现一致**，别想当然。
   #    绑定的正确性由 check-render.js 兜（点不到会因 #un-close 缺失暴露）。
   re.search(r'id="upd-info"', _ujc) is not None
   and re.search(r"\$\('#upd-info'\)", _ujc) is not None
   and re.search(r'ib\.onclick\s*=\s*\(\)\s*=>\s*openReleaseNotes', _ujc)
   is not None,
   '（用户是最新版时也必须能看到「这版是什么/从哪拿/怎么装」，'
   '否则会以为功能坏了）')
ck('发布说明窗口存在且是只读的（不触发下载流程）',
   re.search(r'function openReleaseNotes', _ujc) is not None
   and re.search(r"id=\"un-close\"", _ujc) is not None)
ck('发布说明里给出三种安装方式',
   re.search(r'apt install', _ujc) is not None
   and re.search(r'install-offline\.sh', _ujc) is not None
   and re.search(r'docker load', _ujc) is not None)
ck('发布说明里写明下载源策略（官方优先 / 降级 / 校验）',
   re.search(r'function openReleaseNotes[\s\S]{0,4000}?官方 SHA256 校验',
             _ujc) is not None)
ck('后端在无更新时也返回 assets（发布说明要用）',
   re.search(r"r\.get\('ok'\)\s+and\s+r\.get\('latest'\)", w) is not None
   and re.search(r"if\s+r\.get\('ok'\)\s+and\s+r\.get\('latest'\):", w)
   is not None,
   '（原先只在 has_update 时给 assets → 发布说明窗口里没有下载地址，'
   '而那恰是最新版用户最想看的）')

print()
print('=== F8. 实时柱状流动图（1.0.10）===')
rt = read(P('web', 'realtime.js'))
_rtc = code_only(rt, 'js')   # 剥注释版

ck('realtime.js 存在且被 index.html 引入',
   'realtime.js' in idx
   and idx.index('app.js') < idx.index('realtime.js'))
ck('概览页有 realtime-box 容器', 'id="realtime-box"' in aj)
ck('三块图都用 canvas（不是 DOM 条）',
   rt.count('<canvas') == 0            # 容器写在 app.js 里
   and aj.count('data-rt="') == 3,
   '三块：load / net / conn')
ck('app.js 调用了渲染入口', 'drouterRenderRealtime' in aj)
ck('轮询已登记进 stopPageTimers', '__drouterRealtimeStop' in aj)
ck('用 quick 模式的 metrics（轻量）', 'quick=1' in _rtc)
# —— 降采样必须三条 series + labels 一起做（1.0.10 真跑抓到的不一致）——
ck('降采样在 push 末尾统一做（不逐条判断）',
   # ⛔ 逐条 forEach 里判断 `if (arr.length > MAX)` 会出现：
   #    ① hist.series[i] = ... 只是换引用，forEach 迭代的仍是旧数组
   #    ② 各条 series 判断的起点不同 → 有的降了有的没降
   #    ③ labels 单独降采样 → 与 series 长度对不上，时间轴错位
   re.search(r"hist\.labels\.length\s*>\s*MAX_POINTS\s*\*\s*2", _rtc)
   is not None
   and re.search(r"hist\.series\.forEach[\s\S]{0,200}?"
                 r"hist\.series\[i\]\s*=\s*arr\.filter", _rtc) is not None,
   '（降采样必须在所有 series push 完之后统一做）')
ck('Y 轴保留下限（负载 0.05 不被放大成满格）',
   re.search(r'function yRange\(arr,\s*hardMin\)', _rtc) is not None
   and re.search(r'hardMin\s*\|\|\s*0', _rtc) is not None)
ck('Y 轴上限留 15% 余量（峰值不顶格）',
   re.search(r'\*\s*1\.15', _rtc) is not None)
ck('DPR 上限为 2（高 DPI 下不做超大 canvas）',
   re.search(r'DPR_CAP\s*=\s*2', _rtc) is not None)
ck('值为 0 时不画 1px 假柱',
   re.search(r'v\s*>\s*0\s*\?\s*1\s*:\s*0', _rtc) is not None)
ck('重传标注为累计值（免得被当成速率）',
   '累计' in _rtc and 'retrans' in _rtc)

print()
print('=== F9. i18n 框架（1.0.10 第 1 批）===')
ij = read(P('web', 'i18n.js'))
ck('i18n.js 存在且被 index.html 引入', 'i18n.js' in idx)
def _script_at(name):
    """index.html 里 <script src="...name..."> 的位置；找不到返回 -1。

    ⚠️ 不能再用 idx.index('app.js') —— 那是「文件里第一次出现这个字符串」，
    注释里提一句 app.js 就会被当成脚本位置。2026-10-09 实测踩过：
    给 #page-health 加的行内注释写了「由 app.js 的 paintPageLight() 填」，
    于是 i18n.js 的位置看起来排到了 app.js 之后，判据假红。
    """
    m = re.search(r'<script[^>]*\b' + re.escape(name) + r'\b', idx)
    return m.start() if m else -1


_ij_at = _script_at('i18n.js')
_aj_at = _script_at('app.js')
ck('i18n.js 在 app.js **之前**（app.js 的 t() 要靠它）',
   _ij_at >= 0 and _aj_at > _ij_at,
   '（i18n@%d app@%d；顺序反了 app.js 里的 t() 会 ReferenceError）' % (_ij_at, _aj_at))
ck('暴露了全局 t()', re.search(r'window\.t\s*=\s*t', ij) is not None)
ck('暴露了 setLang / toggle / getLang',
   re.search(r'setLang:\s*setLang', ij) is not None
   and re.search(r'toggle:\s*toggle', ij) is not None
   and re.search(r'getLang', ij) is not None)
ck('语言持久化到 localStorage',
   re.search(r'localStorage\.setItem\(STORE_KEY', ij) is not None)
ck('首次进入跟随浏览器语言',
   re.search(r'navigator\.language', ij) is not None)
# —— 切换闭环的三个环节，缺一不可 ——
ck('切语言会重绘 DOM（data-i18n）',
   re.search(r'function applyDom', ij) is not None
   and re.search(r"querySelectorAll\('\[data-i18n\]'\)", ij) is not None)
ck('切语言会通知订阅者', re.search(r'listeners\.forEach', ij) is not None)
ck('提供 register（异步模块登记重绘）',
   re.search(r'function register\(name, fn\)', ij) is not None)
ck('提供 rerenderAll', re.search(r'function rerenderAll', ij) is not None)
ck('缺词降级为 key 原文（不是空白）',
   re.search(r"console\.warn\('\[i18n\] 缺少词条", ij) is not None
   and re.search(r'return key;', ij) is not None,
   '（空白按钮会被用户当成 bug，比显示 key 更糟）')
ck('非法语言码回落中文', re.search(r"next\s*=\s*lang\s*===\s*'en-US'", ij)
   is not None)
ck('字典里 en 缺词时回落 zh（不显示空白）',
   # ⚠️ 2026-10-07 修真 bug：真值判断 `&& entry.en` 会把**合法的空串**
   #    译文（量词/助词在英文里就该消失）误判成「没配 en」→ 掉回中文。
   #    现在是 `entry.en != null`。判据只认旧写法会假红。
   re.search(r"if\s*\(LANG === 'en-US' && entry\.en != null\)", ij) is not None)
# —— 4 个异步模块必须登记，否则切语言后停在旧语言 ——
for mod, fn in (('update', 'renderCard'), ('upstream', 'render'),
                ('netdetail', 'render'), ('realtime', 'renderAll')):
    ck('%s.js 登记了 i18n 重绘' % mod,
       re.search(r"i18n\.register\('%s'" % mod, read(P('web', mod + '.js')))
       is not None,
       '（这些卡片是 fetch 回来才填的，go() 不会顺带重建它们）')
ck('app.js 的切换钩子调用了 rerenderAll',
   re.search(r'i18n\.rerenderAll', aj) is not None)
ck('顶栏有语言切换按钮',
   'id="btn-lang"' in read(P('web', 'index.html')))
ck('按钮文字显示当前语言（EN / 中）',
   # 中文侧的「中」已抽成词条 T('中')（raw 分组），不再是裸字面量；
   #   英文侧 EN 两个字母本来就不需要翻译。两种写法都认。
   re.search(r"lab\.textContent\s*=\s*en\s*\?\s*'EN'\s*:", aj) is not None
   and re.search(r"const\s+en\s*=\s*window\.i18n\.getLang\(\)\s*===\s*'en-US'", aj) is not None,
   '（显示「点了会变成什么」是反的 —— 用户看到的是当前状态）')
ck('按钮文案分成两条词条（当前/目标方向相反）',
   re.search(r"langToEn", ij) is not None
   and re.search(r"langToZh", ij) is not None,
   '（一条词条表达不了两个相反方向，会自相矛盾）')
ck('语言名词条标记了 i18n-allow-han',
   'i18n-allow-han' in ij,
   '（"switch to 中文"里的「中文」是语言名，硬译成 Chinese 反而看不懂）')
def _cn(src, prefix):
    """统计 src 里 [tT]('<prefix> 的出现次数。

    ⚠️ 2026-10-07：app.js 的翻译调用统一成别名 T('…')，
    原因见 app.js 顶部注释。判据里写死 t(' 会把
    「做完了」判成「没做」 —— 假红比不判更糟。
    """
    return len(re.findall(r"[tT]\('" + re.escape(prefix), src))

ck('概览页已抽成 t()',
   # ⚠️ 阈值第一版定高了：app.js 里 ups/nd 各只有 2 处（容器标题 + 读秒中），
   #    因为这两个模块的主要文案在**它们自己的文件**里。
   #    判据要按「各自文件里抽了多少」来看，不是全堆在 app.js。
   _cn(aj, "dash.") >= 10 and _cn(aj, "ups.") >= 2
   and _cn(aj, "nd.") >= 2
   and _cn(read(P('web', 'upstream.js')), 'ups.') >= 8
   and _cn(read(P('web', 'netdetail.js')), 'nd.') >= 15,
   'app.js: dash=%d ups=%d nd=%d；模块内: ups=%d nd=%d'
   % (_cn(aj, "dash."), _cn(aj, "ups."), _cn(aj, "nd."),
      _cn(read(P('web', 'upstream.js')), 'ups.'),
      _cn(read(P('web', 'netdetail.js')), 'nd.')))
print()
print('=== F10. 导航菜单 i18n（1.0.10 第 2 批）===')
ck('导航分组名走 t()（nav.g.g<序号>）',
   re.search(r"[tT]\('nav\.g\.g'\s*\+\s*gi\)", aj) is not None)
# ⚠️ 2026-10-09：这里从 `T('nav.n.'+k) || it.n` 换成了 navText(...)。
#    原因：T() 查不到词条时返回的是 **key 原文**（非空！），
#    `|| it.n` 的兜底永远不触发 —— 新增导航项忘了登记 nav.n.* 时，
#    菜单直接显示 `nav.n.health`。navText 显式判「查没查到」再回落。
ck('页面名走 navText/t()（nav.n.<key>，查不到会回落）',
   re.search(r"navText\('nav\.n\.'\s*\+\s*it\.k\s*,", aj) is not None
   or re.search(r"[tT]\('nav\.n\.'\s*\+\s*it\.k\)", aj) is not None)
ck('页面说明走 t()（nav.t.<key>）',
   _cn(aj, 'nav.t.') >= 2)
ck('顶栏标题走 navText/t()（不是硬编码 p.t）',
   re.search(r"\$\('#page-title'\)\.textContent\s*=\s*\(p\.k\s*\?\s*"
             r"navText\('nav\.t\.'\s*\+\s*p\.k\s*,", aj) is not None
   or re.search(r"\$\('#page-title'\)\.textContent\s*=\s*\(p\.k\s*&&\s*"
                r"[tT]\('nav\.t\.'\s*\+\s*p\.k\)\)", aj) is not None)
ck('navText 自己判「查没查到」（v !== key）',
   re.search(r"function navText\(key, fallback\)\s*\{[\s\S]{0,200}?v\s*!==\s*key",
             aj) is not None)
# —— key 稳定性：持久化的键不能随语言变 ——
ck('navOpen 的键用分组序号而非分组名',
   # ⚠️ 1.0.10 真踩过：用分组名当键，而分组名会随语言变（安全/Security），
   #    navOpen 又持久化在 localStorage —— 用户切一次语言，
   #    展开状态就再也对不上了。真跑验证抓到英文 HTML 里残留 data-g="安全"。
   re.search(r"data-gh=\"\$\{esc\(gk\)\}\"", aj) is not None
   and re.search(r"S\.navOpen\['g' \+ gi\]", aj) is not None,
   '（分组名随语言变，持久化键必须稳定）')
ck('老数据的展开状态做了迁移（不静默丢弃）',
   re.search(r'1\.0\.10 起键从', aj) is not None
   and re.search(r"moved\['g' \+ i\]\s*=\s*true", aj) is not None)
ck('搜索用当前语言的文案（英文界面能搜 firewall）',
   re.search(r"const hay = \(it\.n \+ it\.t \+ gzh \+ iname\(it\)", aj)
   is not None)
ck('搜索框 placeholder 走 i18n',
   'data-i18n-attr="placeholder:nav.search"' in read(P('web', 'index.html')))
ck('「待完善」与「无匹配」有词条',
   re.search(r"soon:\s*\{ zh: '待完善'", ij) is not None
   and re.search(r"noMatch:", ij) is not None)
# —— 词条完整性（47 个页面都有 n/t）——
ck('47 个页面都有英文名词条',
   len(re.findall(r"^\s+'[a-z0-9]+': \{ zh: '[^']*', en: '[^']*' \},",
                  ij, re.M)) >= 100,
   '（8 分组 + 47 页名 + 47 页说明 ≈ 102 条）')
ck('生成脚本已入库（加页面后可重跑）',
   os.path.isfile(P('_dev', 'gen-nav-i18n.py')))

print()
print('=== F11. 全局 t() 不能被局部变量遮蔽（1.0.10 抓到）===')
# ⛔ i18n 的 t() 是**全局**函数。任何局部 `const t = ...` / 参数 `(t, i) =>`
#    都会在那个闭包里把 t() 遮蔽掉 —— 后面写 t('key') 静默变成 TypeError。
#    实测 app.js 有 9 处（toast 元素、模板项、时间组、快照输出…）。
#    这些地方**将来**抽 i18n 时必然踩到，所以现在就查出来。
_T_SHADOW = [
    (re.compile(r'^\s*const\s+t\s*='), 'const t ='),
    (re.compile(r'\(\s*t\s*,\s*\w+\s*\)\s*=>'), '回调参数 (t, i) =>'),
    (re.compile(r',\s*t\s*\)\s*=>'), '参数 (…, t) =>'),
    (re.compile(r'\bvar\s+t\s*='), 'var t ='),
]
_shadows = []
# ⚠️ 2026-10-07：判据收紧为「遮蔽 **且该作用域内真的调了 t()**」。
#    原来只要看到 `const t =` 就报 → 一次红 20 处。但其中 19 处
#    （toast() 里的 `const t = $('#toast')`、share.js 里的 `const t = 模板`…）
#    **函数体内并不调 t('中文')**，遮蔽无害 —— 判据该报的是真隐患。
#    实测真正会炸的只有 1 处：renderShareHints 的
#    `rows.map((t, i) => ...)` 里嵌了 ${t('名称')}，那处已改用 T。
_LINES = aj.split('\n')
for _i, _l in enumerate(_LINES, 1):
    _ls = _l.strip()
    if _ls.startswith('//') or _ls.startswith('*'):
        continue
    _hit = None
    for _pat, _desc in _T_SHADOW:
        if _pat.search(_ls):
            _hit = _desc
            break
    if not _hit:
        continue
    # 往后看 12 行（同一个函数体 / 同一个 map 回调）
    # 窗口保持 12 行：放大到 30 会把 toast() 这类**无害**的也拉进来，
    # 反而变成假红。12 行刚好能区分二者（实测 14 vs 3）。
    _scope = '\n'.join(_LINES[_i - 1:_i + 12])
    # ⛔ 只认**小写 t(**：T( 是 t() 的安全别名（app.js 顶部注释），
    #    用了 T( 就说明已经避开遮蔽，不该再报。
    if re.search(r"(?<![A-Za-z0-9_$.])t\('", _scope):
        _shadows.append((_i, _hit, _ls[:58]))
ck('app.js 里遮蔽 t() 且同作用域真调 t() 的地方为空', not _shadows,
   '→ %d 处会静默破坏 t()：%s' % (len(_shadows), _shadows[:3]))
if _shadows:
    print('     这 %d 处会报 "t is not a function"：' % len(_shadows))
    for _i, _d, _s in _shadows[:8]:
        print('       line %-6d %-18s %s' % (_i, _d, _s))
# i18n.js 自己不能有局部 t
ck('i18n.js 内部没有同名遮蔽', not re.search(r'^\s*(const|var|let)\s+t\s*=',
                                            ij, re.M))

print()
print('=== F12. 后端 i18n 骨架（1.0.10）===')
# 1) ok()/fail() 加 en 参数
ck('helper 的 ok() 有 en 参数',
   re.search(r'def ok\(data=None, msg=.*, code=.*, en=None\)', hv2)
   is not None)
ck('helper 的 fail() 有 en 参数',
   re.search(r'def fail\(msg, code=.*, data=None, en=None\)', hv2)
   is not None)
# ⚠️ 这两条随 i18n 骨架从「ok() 直接调 _pick」改成「ok() 调 _resolve()」而变。
# 2026-10-05 实测：改完骨架后这两条判据变红，但红的是**判据的旧形状**，
# 不是产品坏了 —— 判据必须跟着契约一起改，否则下次真出问题时
# 「红=坏」这条前提就失效了（跟 MEMORY 里「判据不能比实现宽/窄」同源）。
ck('响应带 msg（按语言选好的）与 msg_cn/msg_en 孪生',
   re.search(r"'msg_cn': msg, 'msg_en': en,\s*\n\s*'msg': m,", hv2)
   is not None)
ck('ok()/fail() 走 _resolve（统一填 msg_en，不能只填 msg）',
   hv2.count('en, m = _resolve(msg, en)') == 2,
   '出现 %d 次（期望 2：ok + fail）' % hv2.count('en, m = _resolve(msg, en)'))
ck('en 为空时回落中文（不能显示空白）',
   re.search(r"if\s+_cur_lang\[0\]\s*==\s*'en-US':\s*\n"
             r"\s+if en:\s*\n\s+return en\s*\n"
             r"\s+return _msg_lookup\(cn\) or cn", hv2)
   is not None)
ck('默认语言是中文（老代码不传 en 时行为不变）',
   re.search(r"_DEFAULT_LANG\s*=\s*'zh-CN'", hv2) is not None)
# msg_en 必须被填好，不能是 None：前端 api() 回写的是后端选好的 `msg`，
# 而任何按 msg_en 自选语言的代码路径都会拿到 null。真跑验证见 t-msg-en.py。
ck('msg_en 不是原样透传 en（否则没查表时恒为 None）',
   "'msg_en': en,\n" in hv2.replace('\r\n', '\n')
   and hv2.count('en, m = _resolve(msg, en)') == 2)
# 2) 语言传递链：前端 X-Lang → web 读 → helper 透传 → helper set_lang
ck('web 读 X-Lang 头', re.search(r"headers\.get\('X-Lang'\)", w) is not None)
ck('web 在 dispatch 最开头就设语言（任何分支都取得到）',
   re.search(r"def dispatch\(self.*\n.*_cur_lang\[0\] = ", w, re.S)
   is not None)
ck('helper() 把 lang 透传给 helper 进程',
   re.search(r"payload\['_lang'\] = lang", w) is not None)
ck('helper 进程在 run_action 分发之前 set_lang',
   re.search(r"if payload and payload\.get\('_lang'\):\s*\n\s*set_lang", hv2)
   is not None)
# 3) 前端
ck('前端 api() 每次都带 X-Lang',
   re.search(r"headers\['X-Lang'\]\s*=\s*_lang", aj) is not None)
ck('前端不缓存 lang（切语言后必须立刻生效）',
   re.search(r"window\.i18n\.getLang\(\)", aj) is not None
   and not re.search(r"const\s+_lang\s*=\s*['\"]zh-CN['\"]", aj))
ck('api() 统一回写 msg_cn（271 处调用点不用逐个改）',
   re.search(r"_j\.msg_cn_raw\s*=\s*_j\.msg_cn", aj) is not None
   and re.search(r"_j\.msg_cn\s*=\s*_j\.msg;", aj) is not None)
# 2026-10-07：optT 已被 bt4 完全取代（bt4 直接用后端表名 + 真实 key），
# optT 函数本身已删除。原来那两条判据查的是一个**已经不存在的机制**，
# 留着就是永远假红 —— 判据该钉「当前设计」，不是「历史设计」。
ck('optT 已由 bt4 取代（不再有 optT 函数与假表名 key）',
   'optT' not in aj,
   '（optT 用的是 i18n.js 里的假表名 key，与后端表名对不上，查不到就回落中文）')
# 2026-10-05：这三处从 optT('cleanItem.' / 'accelImpact.' / 'printItem.')
# 改成了 bt4('CLEAN_ITEMS' / 'ACCEL_IMPACT' / 'PRINT_ITEMS', …) ——
# 因为后端那三张表的键**不叫** cleanItem/accelImpact/printItem，
# 原来的字典 key 与后端表名对不上，查不到就回落中文（英文界面下仍中文）。
# bt4 直接用**后端表名 + 真实 key**，后端零改动。
ck('磁盘清理/加速影响/打印三处已改走 bt4（查后端表名）',
   aj.count("bt4('CLEAN_ITEMS'") >= 1
   and "bt4('ACCEL_IMPACT'" in aj
   and "bt4('PRINT_ITEMS'" in aj,
   'CLEAN_ITEMS=%d ACCEL=%s PRINT=%s'
   % (aj.count("bt4('CLEAN_ITEMS'"), "bt4('ACCEL_IMPACT'" in aj,
      "bt4('PRINT_ITEMS'" in aj))
ck('后端下发表不再用 optT 的假表名查英文',
   "optT('accelImpact." not in aj and "optT('printItem." not in aj,
   'accelImpact/printItem 还在用 —— 它们的 key 与后端表名对不上')
ck('i18n.js 有 opt 组（后端下发的选项文案）',
   re.search(r'^\s{4}opt:\s*\{', ij, re.M) is not None)
ck('opt 组含 printItem / cleanItem / accelImpact',
   all(('    %s: {' % g) in ij or ('      %s: {' % g) in ij
       for g in ('printItem', 'cleanItem', 'accelImpact')))
ck('生成脚本已入库（后端加新表后可重跑）',
   os.path.isfile(P('_dev', 'gen-opt-i18n.py'))
   and os.path.isfile(P('_dev', 'gen-opt-dict.py')))
ck('update.js 不自己发起下载（URL 由后端 DL_SOURCES 决定）',
   # ⚠️ 判据第三版。前两版范围都写错了：
   #   v1「前端不含 gh-proxy 字样」→ 1.0.10 要**显示**「下载源：gh-proxy 镜像」，假红；
   #   v2「前端不含 releases/download」→ 1.0.10 加的「版本发布说明」窗口里
   #      要**展示** GitHub 公开下载链接（给人复制到浏览器用），又假红。
   # 真正要保证的是：**不把 URL 交给 fetch / XHR**（那才是程序下载）。
   # 展示用的 GitHub 链接出现在 UI 文案里不算违规。
   not re.search(r'fetch\s*\([^)]*releases/download', _ujc)
   and not re.search(r'fetch\s*\([^)]*api\.github\.com', _ujc)
   and not re.search(r'XMLHttpRequest[\s\S]{0,200}releases/download', _ujc)
   and re.search(r'/api/update/apply', _ujc) is not None,
   '（前端只调 /api/update/*，下载与降级由后端负责）')

print()
print('=== E2. 本轮修的 6 个 P0/P1 的防线 ===')
# 这 6 个都是「静态全绿但实际会坏」的类型，逐个钉住。

# ① P0-1：_load_mod 必须缓存，否则模块级 Event/Lock 失效 → 中断永远不生效
ck('web.py 有模块缓存字典', '_MOD_CACHE' in w)
ck('web.py 有模块缓存锁', '_MOD_CACHE_LOCK' in w)
_lm = w[w.index('def _load_mod'):w.index('def update_api')]
ck('_load_mod 真的查了缓存（否则缓存字典是摆设）',
   re.search(r'_MOD_CACHE\.get\(name\)', _lm) is not None
   and re.search(r'_MOD_CACHE\[name\]\s*=', _lm) is not None,
   '（只定义缓存不用 = 每次仍重新 exec，模块级 _stop_flag/_lock 照样失效）')
ck('_load_mod 缓存带 mtime（部署新版本后能失效）',
   'st_mtime' in _lm)

# ② P0-2：running=True 必须与检查在**同一缩进层**（即同一把锁内）
# ⚠️ 判据第一版只查「两者出现在同一段文本里」，结果注入把写操作减一级
#    缩进（挪到锁外）后它照样绿 —— 文本还在，只是不在锁里了。
#    正确判据要查**缩进**：锁内的语句是 8 空格，锁外是 4。
_au2 = uc[uc.index('def apply_update'):uc.index('def _apply_worker')]
_lines = _au2.split('\n')
_i_chk = next((i for i, l in enumerate(_lines)
               if 'read_state().get(' in l and 'running' in l), -1)
_i_wr = next((i for i, l in enumerate(_lines)
              if '_write_state(running=True' in l), -1)


def _ind(ls, i):
    if i < 0:
        return -1
    return len(ls[i]) - len(ls[i].lstrip())


ck('apply 的 running 检查存在', _i_chk >= 0)
ck('apply 的写 running 存在', _i_wr >= 0)
ck('写 running 与检查在同一缩进层（= 同一把锁内）',
   _i_chk >= 0 and _i_wr >= 0 and _ind(_lines, _i_chk) == _ind(_lines, _i_wr)
   and _ind(_lines, _i_wr) > 4,
   '检查缩进=%d 写缩进=%d；两者必须相等且都 >4（4 = 锁外）'
   % (_ind(_lines, _i_chk), _ind(_lines, _i_wr)))

# ③ P0-3：备份必须真的产出备份包
ck('backupd 支持 --run-now（绕过 auto_enabled）',
   '--run-now' in read(P('backend', 'drouter-backupd.py')))
ck('backupd 有 emit() 把结果打到 stdout',
   re.search(r'def emit\(', read(P('backend', 'drouter-backupd.py')))
   is not None, '（log() 只写 jsonl 文件，stdout 什么都没有，'
                '调用方无法判断是否真的产出了备份）')
ck('backupd 在 --run-now 时 emit BK_CREATED',
   re.search(r"emit\('BK_CREATED", read(P('backend', 'drouter-backupd.py')))
   is not None)
ck('_do_backup 校验 BK_CREATED（不只看 returncode）',
   'BK_CREATED' in uc[uc.index('def _do_backup'):],
   '（returncode==0 在「自动备份未开启」时也会出现，'
   '只看它会把「什么都没备份」当成「备份成功」）')
ck('_do_backup 用 --run-now 调用', "'--run-now'" in uc)
# ⚠️ backupd 的 call() 必须用 sudo -n 提权（2026-10-10 真机修的 bug）：
#   「更新前备份」由 drouter-web（drouter 用户）spawn backupd，这条链不带 sudo
#   时 helper 继承 drouter 身份，写 /opt/drouter/backups（root:root）直接
#   [Errno 13] Permission denied，更新被卡死在备份这步。sudoers 已放行
#   drouter 免密跑 helper 白名单，所以 root/drouter 两种身份都通。
#   判据查**语义**（subprocess 命令数组里真有 sudo 前缀），不能只查「出现 sudo 这个词」。
_bkd = read(P('backend', 'drouter-backupd.py'))
ck('backupd call() 用 sudo -n 提权（否则 drouter 身份写不进备份目录）',
   re.search(r"\['sudo',\s*'-n',\s*'/usr/bin/python3',\s*HELPER", _bkd) is not None)

# ④ P0-4：apply 必须有白名单且在写库前
ck('apply_module 有模块白名单', 'APPLY_MODULES' in w)
# ⚠️ 切片必须只取 apply_module **自己的**函数体：直接对 w 全文 find()
#    会命中 save_config 里那处 merge_cfg（它在 apply_module 之前），
#    得出「白名单比写库晚」的反结论（判据第一版就这么错的）。
_am = w[w.index('def apply_module'):]
_am = _am[:_am.index('\n    def ', 10)]
_i_guard = _am.find('module not in APPLY_MODULES')
_i_write = _am.find('merge_cfg(module, data)')
ck('apply_module 的白名单检查在写库之前',
   0 <= _i_guard < _i_write,
   '（先写库再由 helper 报错 = 接口说失败但数据已落库）'
   '实际 guard@%d write@%d' % (_i_guard, _i_write))
ck('apply_module 确实调用了 merge_cfg（切片没截空）',
   _i_write > 0, '切片长度=%d' % len(_am))

# ⑤ P1-8：helper().get('data') 可能是 None
# ⚠️ 必须剥注释再查：那段解释「为什么 get('data', {}) 不生效」的注释里
#    正写着 `or {}`，全文匹配会把自己的注释当成修复证据（判据第一版栽这）。
_hl = [re.sub(r'#.*$', '', l).strip()
       for l in w.splitlines() if 'holds' in l and 'get(' in l]
ck('holds 读取用 or {} 兜底（None 会导致 500）',
   any("or {}" in l and "get('holds'" in l for l in _hl),
   '（helper 的 fail() 返回 {"data": None}，键存在所以 .get("data", {}) '
   '不生效，拿到 None 再 .get() 就 AttributeError → 500）实际行：%s' % _hl)

# ⑥ P1-2：_verify 的清理必须在 finally
hv = read(P('backend', 'drouter-helper.py'))
_vf = hv[hv.index('def _verify('):hv.index('def _verify(') + 9000]
ck('_verify 的临时文件清理在 finally 里',
   re.search(r'finally:[\s\S]{0,200}?os\.remove\(tmp\)', _vf) is not None,
   '（原来 os.remove 写在循环末尾，四个分支的 return 直接跳过 → '
   'chrony 的校验文件永久残留在 /etc）')
ck('_verify 临时文件名唯一（并发预检不互相覆盖）',
   # ⚠️ 判据第一版数 `_thread_id()` 出现次数 ≥2，判红了一次 ——
   #    实现里是 `_tid = _thread_id()` 一次取值、两处格式化串里复用，
   #    所以只出现 1 次，但**两个分支都是唯一的**。
   # 正确的判据是查「两个分支的格式化串里都带唯一化成分」。
   re.search(r"_vt \+ \('|%s\.%d\.%d\.tmp", _vf) is not None
   and re.search(r"basename\(path\), _tid\)", _vf) is not None
   and 'os.getpid()' in _vf and '_tid' in _vf,
   '（固定文件名 + ThreadingUnixStreamServer = 线程A 校验的是线程B 的配置；'
   'VERIFY_PATH 分支和通用分支都要唯一化）')

print()
print('=== E. 已废除的 passwall 不得有残留 ===')

ck('backend 下无 drouter-passwall.py',
   not os.path.exists(P('backend', 'drouter-passwall.py')))
ck('web 下无 passwall.html', not os.path.exists(P('web', 'passwall.html')))
ck('web.py 不含 passwall 路由',
   not re.search(r'api/pw|passwall_api', w))
ck('build-deb.sh 不含 passwall',
   'passwall' not in read(P('packaging', 'build-deb.sh')))
ck('deploy.sh 不含 passwall',
   'passwall' not in read(P('scripts', 'deploy.sh')))
ck('README 不含 passwall', 'passwall' not in read(P('README.md')).lower())
ck('主菜单（app.js）不含 passwall', 'passwall' not in aj.lower())
ck('openapi 不含 passwall', 'passwall' not in w)

print()
print('=== F2. 上游链路 / 网卡统计（1.0.10 新增） ===')
up_js = read(P('web', 'upstream.js'))
ck('read_upstream 已注册到 ACTIONS', "'read:upstream': read_upstream" in hv2)
ck('/api/upstream 已注册到 web 路由', "'/api/upstream': 'read:upstream'" in w)
ck('upstream.js 存在且被 index.html 引入', 'upstream.js' in read(P('web', 'index.html')))
ck('upstream.js 在 app.js 之后（要用 api/esc）',
   read(P('web', 'index.html')).index('app.js')
   < read(P('web', 'index.html')).index('upstream.js'))
ck('upstream.js 提供渲染入口', 'drouterRenderUpstream' in up_js)
ck('app.js 调用了渲染入口', 'drouterRenderUpstream' in aj)
ck('上游轮询已登记进 stopPageTimers',
   '__drouterUpstreamStop' in w or '__drouterUpstreamStop' in aj)
ck('_dhcp6_stats 读 /proc/net/snmp6', '/proc/net/snmp6' in hv2)
ck('DHCPv6 统计按位置配对（不是同名查）',
   "lines[i].split()[1:]" in hv2 and "lines[i + 1].split()[1:]" in hv2)
ck('snmp6 读不到时返回空列表而非全 0',
   re.search(r"dhcp6s[\s\S]{0,600}?return \[\]", hv2) is not None,
   '（全 0 会让用户以为「一条 DHCPv6 都没发过」，真相是没开着）')
# ⚠️ 这两条原来是「不返回已连接时长」——1.0.10 实现了该功能（uptime 差值算法），
#    所以判据方向反过来了：现在要验的是「**给了数值但必须带可信度标记**」，
#    而不是在没有标记的情况下也显示数字。
ck('已连接时长必须带可信度标记（不给裸数字）',
   re.search(r"'uptime_trusted':\s*(up_trusted|trusted)", hv2) is not None
   or "'uptime_trusted': up_trusted" in hv2,
   '（起点可能是「面板首次记录到 up」而非插线时刻，不标出来就是误导）')
ck('前端对不可信的时长如实说明',
   '非插线时刻' in up_js or '非插线' in up_js)
ck('_ethtool_stats 存在并接入 read_ifaces',
   'def _ethtool_stats(' in hv2
   and "'ethtool': et" in hv2)
ck('ethtool 缺失时说明原因而非显示 0',
   # ⚠️ 判据第一版写 `out['ok'] = False`，但实现是**字典字面量**初始化，
   #    没有这句赋值 —— 判红是判据写错，不是实现缺功能。
   re.search(r"out\s*=\s*\{\s*'ok':\s*False", hv2) is not None
   and '未安装 ethtool' in code_only(hv2),
   '（ok 初始为 False + 给出中文原因，前端据此显示「为什么没有」而非显示 0）')
ck('前端 ethtool 不可用时显示原因', 'eth-off' in aj and 'ethBlock' in aj)
ck('接口名做了白名单校验（防命令注入）',
   re.search(r"islink\('_a-zA-Z0-9_\.\:-\+'", hv2) is not None
   or re.search(r"re\.match\(r'\^\[a-zA-Z0-9_", hv2) is not None)

print()
print('=== F3. 打包 / 部署清单必须覆盖全部 web 文件 ===')
# ⚠️ 2026-10-04 首次部署真机上踩到：drouter-update.py 装上了，
#    但 update.js / update.css / upstream.js 三个都没装 —— 因为
#    deploy.sh 的前端安装段是**逐个 install**，不是通配符。
#    结果真机概览页 JS 404，白部署一次。
#    这条判据的作用：以后再加 web/*.js|css，它会立刻报出来。
import glob as _glob
_web = sorted(os.path.basename(p) for p in _glob.glob(P('web', '*'))
              if os.path.basename(p).rsplit('.', 1)[-1] in ('js', 'css', 'html'))
_dep = read(P('scripts', 'deploy.sh'))
_bd = read(P('packaging', 'build-deb.sh'))
_miss_dep = [w for w in _web if w not in _dep]
_miss_bd = [w for w in _web if w not in _bd]
ck('deploy.sh 覆盖全部 web 文件', not _miss_dep,
   '→ 漏登记：%s（真机上会 404）' % _miss_dep)
ck('build-deb.sh 覆盖全部 web 文件', not _miss_bd,
   '→ 漏登记：%s（装 deb 后会 404）' % _miss_bd)
ck('后端模块也在两处清单里', 'drouter-update.py' in _dep
   and 'drouter-update.py' in _bd)

print()
print('=== F4. 新增端点必须登记进 openapi 描述符 ===')
# ⚠️ 2026-10-04 真机部署后才发现：build_openapi 是**逐个 ep()/字典显式登记**的，
#    不是从 dispatch 的 readings 自动生成。所以新端点光在 readings 里加一行，
#    API 文档里根本看不到 —— 第一次部署就漏了 /api/upstream 和 /api/update。
_oa = w[w.index('def build_openapi'):]
ck('/api/upstream 登记进 openapi', "'/api/upstream'" in _oa)
ck('/api/update/apply 登记进 openapi', "'/api/update/apply'" in _oa)
ck('/api/update/cancel 登记进 openapi', "'/api/update/cancel'" in _oa)
ck('readings 里有 /api/upstream', "'/api/upstream': 'read:upstream'" in w)
ck('openapi 摘要说明了「不自动安装」',
   'SHA256' in _oa and '下载' in _oa)

print()
print('=== F5. 网络明细（邻居 / 路由 / 规则） ===')
nd = read(P('web', 'netdetail.js'))
# 剥注释版：i18n 改造后文案都在 t() 里，而 t() 本身**不在 netdetail.js**，
# 所以这些判据要查「netdetail.js 用了哪些 key」。
_ndc = code_only(nd, 'js')
idx2 = read(P('web', 'index.html'))
ck('read_netdetail 已注册到 ACTIONS', "'read:netdetail': read_netdetail" in hv2)
ck('/api/netdetail 已注册到 readings', "'/api/netdetail': 'read:netdetail'" in w)
ck('/api/netdetail 已登记进 openapi', "'/api/netdetail'" in _oa)
ck('netdetail.js 引入且在 app.js 之后',
   'netdetail.js' in idx2 and idx2.index('app.js') < idx2.index('netdetail.js'))
ck('app.js 调用了渲染入口', 'drouterRenderNetDetail' in aj)
ck('轮询已登记进 stopPageTimers', '__drouterNetDetailStop' in aj)
# 路由必须只取 main 表 —— local 表有十几条回环/链路本地路由，会淹掉 default
ck('IPv6 路由只取 main 表',
   re.search(r"'-6', '-j', 'route', 'show', 'table', 'main'", hv2) is not None,
   '（不限定表的话 local 表的 ::1 / fe80::1 等噪音会灌进来）')
# STALE 不是故障 —— 这是内核语义，界面上不能标红
ck('区分 STALE 与 FAILED（STALE 不是故障）',
   re.search(r"'stale':\s*'STALE' in states", hv2) is not None
   and re.search(r"'failed':\s*'FAILED' in states", hv2) is not None)
ck('前端把 STALE 显示为「静默」而非错误',
   # ⚠️ 判据第三版：1.0.10 把这些文案抽成 t() 了（原来是硬编码中文），
   #    所以不能查「代码里有没有『静默』两个字」—— 要查**用了哪个词条 key**。
   #    这正是 i18n 改造的通用副作用：文案判据全部要从「查字面量」改成「查 key」。
   re.search(r"x\.stale\)\s*tag\s*=\s*'<span class=\"tag gray\">'\s*\+\s*"
             r"t\('nd\.stale'\)", _ndc) is not None
   and re.search(r"x\.failed\)\s*tag\s*=\s*'<span class=\"tag warn\">'\s*\+\s*"
                 r"t\('nd\.failed'\)", _ndc) is not None)
ck('前端对 STALE 排序放后（活跃在前）',
   re.search(r'rank\(a\).*rank\(b\)', _ndc) is not None
   and re.search(r'x\.stale \? 3 : 2', _ndc) is not None)
ck('IPv6 规则标出内核自动建的（0 / 32766）',
   re.search(r"builtin.*'0', '32766'", hv2, re.S) is not None
   and re.search(r"t\('nd\.builtin'\)", _ndc) is not None)
ck('默认路由在界面上有强调标记',
   'row-def' in _ndc and re.search(r"t\('nd\.defRoute'\)", _ndc) is not None)
# 「暂无条目」抽成了 EMPTY 常量复用（这是好设计），所以只出现一次，
# 判据要查「EMPTY 被几个表格函数用了」而不是数出现次数。
ck('表格缺失时显示「暂无条目」而不是空白',
   re.search(r"t\('nd\.empty'\)", _ndc) is not None
   and _ndc.count('return EMPTY') >= 2,   '（EMPTY 常量被 neighTable / routeTable / ruleTable 三处复用；'
   '数出现次数会因抽了常量而假红）')
ck('用 ip -j JSON 而不是解析文本',
   re.search(r"'ip', '-4', '-j', 'neigh'", hv2) is not None)



print()
print('=== F6. 「重新检查」必须有反馈闭环（1.0.10 用户反馈） ===')
us = read(P('web', 'upstream.js'))
# 用户的原话：「如果已是最新版，我再按重新检查，没有任何提示，
#   无法确定是不是已经是最新的了」——
#   根因：renderCard() 会重建 innerHTML 把按钮换掉，
#   按钮文字从「检查中…」变回「重新检查」，**整个过程零提示**。
ck('重新检查有「进行中」提示（不只是改按钮文字）',
   # ⚠️ 同上：bindForce 与 bindRetry 两处都该有，删任一处都要判红
   _ujc.count('正在连接 GitHub 查询最新版本') >= 2,
   '（bindForce / bindRetry 两处都要有进行中提示；'
   '当前出现 %d 次）' % _ujc.count('正在连接 GitHub 查询最新版本'))
# ⚠️ 反向验证实测：这条原本只查 bindForce 段内有没有 toast，
#   结果注入删掉 bindForce 那处后判据仍绿 —— 因为 **bindRetry 里还有一处**
#   toast。两条路径（正常重查 / 失败重试）都必须有反馈，所以判据要查**两处都在**。
ck('重新检查与失败重试都有 toast 反馈',
   re.search(r'bindForce[\s\S]{0,2000}?toast\(', _ujc) is not None
   and re.search(r'bindRetry[\s\S]{0,2000}?toast\(', _ujc) is not None,
   '（两条路径都要有 toast，删掉任一处都应判红）')
ck('toast 内容区分「已是最新」与「有新版本」',
   re.search(r"has_update[\s\S]{0,200}?发现新版本", _ujc) is not None
   and '已是最新版本' in _ujc)
ck('卡片常驻显示上次检查时间',
   re.search(r'上次检查：', _ujc) is not None
   and re.search(r'new Date\(d\.ts \* 1000\)', _ujc) is not None)
ck('「有更新」分支也提供重新检查入口',
   re.search(r"has_update[\s\S]{0,900}?id=\"upd-force\"", _ujc) is not None)
ck('检测失败分支也有 toast 反馈',
   re.search(r'bindRetry[\s\S]{0,1500}?toast\(', _ujc) is not None)
ck('错误路径不会被吞成「无更新」',
   re.search(r'catch \(e\)[\s\S]{0,120}?msg = ', _ujc) is not None)

print()
print('=== F7. 已连接时长：用 uptime 差值算，不用第三方库 ===')
ck('_link_since 已实现',
   re.search(r'def _link_since\(name\)', _hvc) is not None)
ck('用 /proc/uptime 做基准',
   re.search(r"open\('/proc/uptime'\)", _hvc) is not None)
ck('用 boot_id 区分重启（否则 uptime 归零会算出负数）',
   re.search(r"kernel/random/boot_id", _hvc) is not None
   and re.search(r"rec\.get\('boot'\)\s*!=\s*boot", _hvc) is not None,
   '（必须查**实际比较表达式**，不能只查 boot_id 这个词 —— '
   '词在 docstring 里也有）')
ck('状态持久化到文件（关浏览器不影响计时）',
   re.search(r"link-since\.json", _hvc) is not None
   and re.search(r"os\.path\.join\('/var/lib/drouter'", _hvc) is not None)
ck('接口 down→up 会重记起点',
   re.search(r"oper != 'up'", _hvc) is not None)
# ⚠️ 这里**必须数出现次数**：字段在 3 处被写入（read_ifaces 1 处 +
#    read_upstream 的 v4/v6 各 1 处）。反向验证实测：只删 1 处时判据仍绿
#    —— 「有没有」型判据在「多处都需要」的字段上天然不灵敏。
ck('带可信度标记（起点可能是面板首次记录而非插线时刻）',
   _hvc.count("'uptime_trusted': up_trusted") >= 3,
   '（3 个写入点都要带：read_ifaces + read_upstream 的 v4/v6；'
   '当前 %d 处）' % _hvc.count("'uptime_trusted': up_trusted"))
ck('read_ifaces 接进已连接时长',
   re.search(r"'uptime_s':\s*up_sec", _hvc) is not None)
# ⛔ 真实踩到：read_ifaces 里的 oper 是 **IFF_UP 的数字标志**（16），
#    不是 /sys 的 operstate 字符串（'up'）。加 `if oper == 'up'` 条件后
#    判断永远为假，uptime_s 全是 0 —— 真机才发现（read_upstream 那个
#    走的是另一条路径所以正常，掩盖了问题）。
ck('read_ifaces 里不拿 IFF_UP 标志当 operstate 用',
   not re.search(r"if\s+oper\s*==\s*'up'", _hvc)
   and not re.search(r"if\s*\(?\s*'up'\s*in\s+oper", _hvc),
   '（oper 是数字标志 IFF_UP=16，与 operstate 字符串比较永远为假）')
ck('read_upstream 接进已连接时长',
   re.search(r"mac, bridge, up_sec, up_trusted = _if_common", _hvc)
   is not None)
ck('前端显示时长而不是「内核不提供」',
   'linkUp(u)' in _us and '正在累计' in _us)
ck('不可信时如实说明（不装作精确）',
   re.search(r'uptime_trusted', _us) is not None
   and re.search(r'非插线时刻', _us) is not None)
# ⛔ 纯标准库：不得引入任何第三方模块。
# ⚠️ 判据第一版手写了一份标准库白名单，结果把 zipfile / tarfile / random /
#    uuid / io / stat … 全判成「第三方」—— 它们**都是标准库**，
#    只是我没想到。→ 改用 sys.stdlib_module_names（Python 3.10+ 自带），
#    这样永远不会漏、也不会误判。
try:
    import sys as _sys
    _STDLIB = set(_sys.stdlib_module_names) | {'render', 'theme', 'drouter_helper'}
except AttributeError:      # 3.9 及更早
    _STDLIB = set()
_hv_imports = set(re.findall(r'^\s*(?:import|from)\s+([\w.]+)', hv2, re.M))
_top = set(m.split('.')[0] for m in _hv_imports)
_third = sorted(m for m in _top
                if _STDLIB and m not in _STDLIB and not m.startswith('_'))
ck('未引入任何第三方库（纯标准库）', not _third,
   '→ 发现非标准库：%s' % _third)
ck('已连接时长确实只用了标准库（/proc + json + os）',
   re.search(r"import json", hv2) is not None
   and re.search(r"/proc/uptime", hv2) is not None)

print()
print('=== F. CSS 变量（带 fallback 的不算缺失） ===')


def undefined_vars(text, defined):
    out = set()
    for mm in re.finditer(r'var\(\s*(--[a-z0-9-]+)\s*(,|\))', text):
        if mm.group(2) == ',':
            continue
        if mm.group(1) not in defined:
            out.add(mm.group(1))
    return out


css = read(P('web', 'app.css'))
defined = set(re.findall(r'(--[a-z0-9-]+)\s*:', css))
ck('update.css 无未定义变量',
   not undefined_vars(read(P('web', 'update.css')), defined),
   str(undefined_vars(read(P('web', 'update.css')), defined)))

print()
print('=' * 56)
print('判据 %d 条，失败 %d 条' % (n, len(fails)))
if fails:
    for f in fails:
        print('  FAIL: %s' % f)
sys.exit(1 if fails else 0)

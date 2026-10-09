# -*- coding: utf-8 -*-
"""钉住「**所有**前端 JS 文件的界面文案都走了 t()」。

🔴 为什么需要（2026-10-06 的事故）：
   之前只查 `web/app.js`，于是
     · `realtime.js`（图形实时模块，275 行 / 53 行中文 / `t()` **0 个**）
     · `update.js`（版本卡片，442 行 / 151 行中文 / `t()` **0 个**）
   整份文件从来没被 i18n 覆盖过 —— 用户切英文后这两块仍是中文，
   而覆盖率判据报 100%。

   ⛔ 关键教训：**「扫描范围」和「覆盖率」同样重要**。扫得再仔细，
      漏了文件就等于没扫。

⛔ 判「是否已包 t()」必须**回看源码偏移**（`src[t['start']-4:t['start']`），
   不能在 token 内部找 —— `t('小时 ')` 的字面量 token 只是 `'小时 '`，
   `t(` 落在**这个 token 之外**。
   2026-10-05 在这条上栽过：覆盖率被算成 5.8%（真实 90%+）。

判据（对 5 个文件逐个查）：
  ① 无 CRLF / 无 U+FFFD
  ② 字面量定位与源码一致（委托 Node 真词法）
  ③ 含中文的**单/双引号**字面量都已包 t()（按偏移回看，排除注释/实体表）
  ④ 每个文件 t() 调用数 > 0（防止新增文件忘做）
"""
import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FILES = ('app.js', 'realtime.js', 'upstream.js', 'netdetail.js', 'update.js')

FAILS = []
N = 0


def chk(name, cond, detail=''):
    global N
    N += 1
    if cond:
        print('  ok    ' + name)
    else:
        FAILS.append('%s  %s' % (name, detail))
        print('  FAIL  ' + name + '  ' + detail)


# 不是界面文案的特征
NOT_UI = (
    'http://', 'https://', '/api/', 'data-', 'class=', 'style=',
    '&amp;', '&lt;', '&gt;', '&quot;', '&#39;',   # HTML 实体映射
    'console.', 'window.', 'document.',
)


def is_commented(raw, pos):
    """该位置是否在注释里（简易判定：本行 // 或 /* 之后，且没闭合）。
    ⚠️ pos 是**字节**偏移，全程在 raw 上切。"""
    ls = raw.rfind(b'\n', 0, pos) + 1
    line = raw[ls:pos].decode('utf-8', 'replace')
    # 块注释
    k = line.rfind('*/')
    if k >= 0 and line.find('/*', k) < 0:
        return True
    if line.rfind('/*') > line.rfind('*/'):
        return True
    return line.lstrip().startswith('//')


# ⭐ 一次 node 调用扫全部文件（2026-10-06 加 --multi 模式）。
#   原来逐个文件 fork node，5 个文件 = 5 次进程启动（~0.6s/次），
#   累计把全量回归撑到 590s 超时（62 项就被 SIGTERM 打断，rc=143）。
# 用**相对路径**（Node 的输出键就是命令行里给的字符串，# 换绝对路径会导致取不到）
_paths = ['web/' + f for f in FILES]
_all = subprocess.run(
    ['node', os.path.join(HERE, 'scan-tokens.js'), '--multi'] + _paths,
    capture_output=True, check=True)
TOKS = json.loads(_all.stdout.decode('utf-8'))

for js in FILES:
    path = os.path.join(ROOT, 'web', js)
    raw = open(path, 'rb').read()
    src = raw.decode('utf-8')
    print('[%s]' % js)
    chk('%s 无 CRLF' % js, b'\r\n' not in raw)
    chk('%s 无 U+FFFD' % js, chr(0xFFFD) not in src)

    toks = TOKS['web/' + js]   # Node 的键是命令行里给的路径
    mism = sum(1 for t in toks
               if raw[t['start']:t['end']].decode('utf-8', 'replace') != t['raw'])
    chk('%s 字面量定位与源码一致' % js, mism == 0, '%d 条错位' % mism)

    # 2026-10-07：区分「UI 文案」与「翻译 key 的比较值」。
    # 形如 if (a === '建议开启') return T('建议开启') —— 同一个字符串
    # 既是比较用的原文，又是 T() 的 key。这类**不该**被判成「漏做 i18n」，
    # 否则为了迎合判据就得把比较值也包 T()，逻辑反而坏掉。
    #
    # ⚠️ 判据必须按**字节偏移**判「这个字面量前面紧邻 t(/T(」，
    #    绝不能按「字符串值是否出现在某处 T() 参数里」——
    #    那样的话只要文件里任何地方T() 过它，别处的裸文案就会被误放行。
    #    反向验证实测过：把 T('重新体检') 的包装去掉后判据仍绿（值放行的坑）。
    _KEY_STARTS = set()
    for _m in re.finditer(r"""(?<![A-Za-z0-9_$.])[tT]\(\s*(['"])(.*?)\1""", src):
        # 捕获组 2 是引号，组 3 起是内容；用引号结束位置反推开引号起点
        _end = _m.end() - 1
        _open = src.rfind(_m.group(1), 0, _end)
        _KEY_STARTS.add(_open + 1)      # +1 = 跳过开引号，落在内容首字节
    # 同理：「判别用的匹配串」也不是 UI 文案。这类串集中在一个
    # 命名为 *_PREFIX / *_PATTERN / _PATTERNS 的常量对象里，
    # 用途只有 startsWith() / indexOf() 比较，不直接上屏。
    _PAT_ARGS = set()
    # 常量名的判定用**通用规则**，不用名字黑名单：
    #   形如 WIZ_* 或 全大写下划线（_PREFIX / _ADVICE / _ORIGIN …）
    #   否则下一个新名字（WIZ_V6_ADVICE）又会被漏掉 —— 黑名单是无底洞。
    _re_const = re.compile(
        r'''const\s+(?:WIZ_[A-Za-z0-9_$]*|[A-Z][A-Z0-9_$]{2,})\s*=\s*\{[\s\S]{0,2000}?\}\s*;''')
    for _m in _re_const.finditer(src):
        for _s in re.finditer(r"""(['"])([^'"]+)\1""", _m.group(0)):
            _PAT_ARGS.add(_s.group(2))
    # ⚠️ _PAT_ARGS 目前只是「*_PREFIX 常量里出现过的串」，是**候选**。
    #    常量里可能有根本没被用到的条目 —— 往常量里塞一条没人用的中文
    #    就能骗过判据（2026-10-07 反向验证实测）。
    # 正解：与「确实当作 startsWith()/indexOf() 实参」的集合求交集，只放行交集。
    _PAT_USED = set()
    # 形态一：直接拿字面量当比较操作数 —— .startsWith('x') / .indexOf('x')
    _re_used_lit = re.compile(
        r'''\.(?:startsWith|indexOf)\(\s*(['"])(.*?)\1\s*\)''')
    for _m in _re_used_lit.finditer(src):
        _PAT_USED.add(_m.group(2))
    # WIZ_XXX.YYY 形式的引用 → 回查常量对象里该字段的字面量
    _re_used_ref = re.compile(
        r'''(?:\.(?:startsWith|indexOf)\(\s*|===\s*|\.equals\(\s*)(WIZ_[A-Za-z0-9_$]*)\.([A-Za-z0-9_$]+)\s*[\)\s]''')
    for _m in _re_used_ref.finditer(src):
        _cref, _field = _m.group(1), _m.group(2)
        _c = re.search(r'const\s+' + _cref + r'\s*=\s*\{[\s\S]{0,2000}?\}\s*;', src)
        if not _c:
            continue
        _lit = re.search(r'\b' + _field + r'\s*:\s*(["\'])(.*?)\1',
                         _c.group(0), re.S)
        if _lit:
            _PAT_USED.add(_lit.group(2))
    _PAT_ARGS &= _PAT_USED

    # 「比对后端中文原文」的字面量豁免：行尾带 `i18n-keep-cn` 注释的行。
    #   后端下发的是中文（DDNS 的 region「国内/国际/通用」、网卡驱动来源
    #   「厂商官方(xxx)」「第三方(DKMS)」…），这些串只用于 === / indexOf 比对，
    #   **不上屏**。给它们包 t() 反而有害：英文界面下 T() 返回英文，
    #   就永远匹配不上后端中文 → 标签配色全部失效（真 bug）。
    #   ⚠️ 豁免按**行**生效，所以一行里只写需要豁免的串，别顺手塞别的文案。
    _KEEP_CN = {i + 1 for i, l in enumerate(src.split('\n'))
                if 'i18n-keep-cn' in l}
    # 形态二：**整张中文→英文对照表**豁免。表声明行带 `i18n-keep-cn-table`
    #   时，从该行到对象结束（遇到顶格的 `};`）之间的字面量全部放行。
    #   典型是 conntrack 状态表：key 必须是后端下发的中文原文，value 才是英文。
    #   —— key 包了 t() 的话，英文界面下 key 变成英文，就再也匹配不上后端中文。
    _lines = src.split('\n')
    for i, l in enumerate(_lines):
        if 'i18n-keep-cn-table' not in l:
            continue
        for j in range(i, min(i + 60, len(_lines))):
            _KEEP_CN.add(j + 1)
            if _lines[j].rstrip() in ('};', '},'):
                break

    bare = []
    for t in toks:
        if t['kind'] not in ('sq', 'dq'):
            continue
        body = t['raw'][1:-1]
        if not re.search(r'[\u4e00-\u9fff]{2,}', body):
            continue
        if any(x in body for x in NOT_UI):
            continue
        if is_commented(raw, t['start']):
            continue
        # 本字面量**前面紧邻 t(/T(** → 它就是那次调用的 key，不是待翻译文案。
        # ⚠️ 必须按偏移判，且**单位要统一**：
        #    t['start'] 是**字节**偏移，_KEY_STARTS 存的是 str 偏移，
        #    直接比会因代理对错位 → 假绿/假红（MEMORY 里记过这条坑）。
        if (len(raw[:t['start']].decode('utf-8', 'replace')) + 1) in _KEY_STARTS:
            continue
        # 判别用的匹配串（_*_PREFIX 之类常量里的），同上：不上屏
        if body in _PAT_ARGS:
            continue
        # ⛑ 关键：回看偏移，t( 在字面量**外面**。
        #    ⚠️ 必须用 **raw（字节）** 切片再解码 —— t['start'] 是**字节**偏移，
        #    直接拿去切 str 会错位（app.js 有代理对字符），导致 pre 取到别处、
        #    判据全红（2026-10-06 实测 782 处假红）。
        pre = raw[max(0, t['start'] - 4):t['start']].decode('utf-8', 'replace')
        # T 也是翻译函数（t 被形参遮蔽时的安全别名，见 app.js 顶部注释）
        if pre.endswith('t(') or pre.endswith('T('):
            continue
        if t['line'] in _KEEP_CN:
            continue
        bare.append('L%d %r' % (t['line'], body[:46]))
    chk('%s 的单/双引号文案都包了 t()' % js, not bare,
        '%d 处：%s' % (len(bare), bare[:3]))

    # 2026-10-07 起 app.js 的翻译调用统一用别名 T(（见 app.js 顶部注释），
    # 单认 t( 会把它判成「漏做了 i18n」。
    n_t = len(re.findall(r"(?<![A-Za-z0-9_$.])[tT]\('", src))
    chk('%s 有 t() 调用（不是漏做了 i18n）' % js, n_t > 0, '%d 处' % n_t)
    print()

# ---------- ⑤ 导航项必须有 nav.n.* / nav.t.* 词条 ----------
# 🔴 2026-10-09 的事故：新增「健康总览」时只在 NAV_GROUPS 里写了
#    `n: T('st.nav')`，忘了往 i18n 的 nav.n / nav.t 两张表登记。
#    于是 renderNav 里 `T('nav.n.' + k) || it.n` 的兜底**永远不触发** ——
#    因为 T() 查不到时返回的是 key 原文（`nav.n.health`，非空！），
#    菜单就显示成那个英文串。这条判据把「新增导航项」这件事钉住。
_APP = io.open(os.path.join(ROOT, 'web', 'app.js'), encoding='utf-8').read()
_I18N = io.open(os.path.join(ROOT, 'web', 'i18n.js'), encoding='utf-8').read()
_m = re.search(r'const NAV_GROUPS = \(\) => \(\[([\s\S]*?)\n\]\);', _APP)
chk('app.js 里找得到 NAV_GROUPS', bool(_m))
if _m:
    _navk = re.findall(r"\{ k: '([a-z0-9_]+)'", _m.group(1))
    chk('NAV_GROUPS 至少 30 个页面项', len(_navk) >= 30, '实际 %d' % len(_navk))
    # nav.n / nav.t 都是 `nav: { ... n: { ... } }` 里的子块，按子块取
    def _navblock(name):
        i = _I18N.find('\n    nav: {\n')
        if i < 0:
            return ''
        j = _I18N.find('\n      %s: {\n' % name, i)
        if j < 0:
            return ''
        k = _I18N.find('\n      },\n', j + 1)
        return _I18N[j:k] if k > 0 else ''

    for _grp in ('n', 't'):
        _blk = _navblock(_grp)
        chk('i18n 里取到 nav.%s 子块' % _grp, len(_blk) > 500, 'len=%d' % len(_blk))
        _miss = [k for k in _navk
                 if not re.search(r"'%s': *\{ zh:" % re.escape(k), _blk)]
        chk('每个导航项都有 nav.%s.<键> 词条' % _grp, not _miss, str(_miss))

    # T() 查不到会返回 key 原文 → 调用点必须用 navText 显式判「查没查到」
    chk('菜单名走 navText（不会漏词条时显示 key 原文）',
        "const iname = it => navText('nav.n.' + it.k, it.n);" in _APP)
    chk('页面标题走 navText', "navText('nav.t.' + p.k, p.t)" in _APP)

print('==== t-i18n-cover: %d 条判据, %d 失败 ====' % (N, len(FAILS)))
if FAILS:
    for f in FAILS:
        print('  - ' + f)
    sys.exit(1)
print('I18N_COVER_OK')

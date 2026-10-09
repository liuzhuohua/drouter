# -*- coding: utf-8 -*-
"""排版 / 多端适配回归。

背景（2026-10-09）：中英文全量翻译后，**英文标签普遍比中文长**，把几处
「裸 1fr 网格轨道」撑破了 —— CSS 里 `1fr` 的隐含最小宽度是 `auto`
（等价 `minmax(auto,1fr)`），轨道不能缩到内容 min-content 以下，
于是手机端整个内容区横向滚动（真机几何实测：NFS 页 #view 溢出 13px，
罪魁是 `grid-template-columns:1fr 1fr` 里长标签 "Local machine LAN Address"）。

判据：
  ① 网格轨道禁止裸 `1fr` / `repeat(N,1fr)`，必须 `minmax(0,1fr)`
  ② `.kv` 的标签必须可收缩（flex 收缩因子非 0），否则长标签撑破父级
  ③ 表格横向滚动容器 `.tw` 仍在（check-mobile 也守着，这里做交叉确认）
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, 'web', 'app.css')
APP = os.path.join(ROOT, 'web', 'app.js')

FAILS = []
N = 0


def chk(name, cond, detail=''):
    global N
    N += 1
    if cond:
        print('  ok    ' + name)
    else:
        FAILS.append('%s  %s' % (name, detail))
        print('  FAIL  %s  %s' % (name, detail))


css = io.open(CSS, encoding='utf-8').read()
app = io.open(APP, encoding='utf-8').read()

# ---- ① 网格轨道里的裸 1fr ----
# 只看 `grid-template-columns:` 后面的值；允许 minmax(0,1fr)。
bad_tracks = []
for src_name, src in (('app.css', css), ('app.js', app)):
    for m in re.finditer(r'grid-template-columns:([^;"\n}]*)', src):
        val = m.group(1)
        # 去掉 minmax(...) 之后再找裸 1fr
        stripped = re.sub(r'minmax\([^)]*\)', '', val)
        # 任何 fr 轨道（1fr / 1.6fr / repeat(N,1fr)）都必须包 minmax，
        # 否则隐含 min-width:auto 会被长英文标签撑破。
        if re.search(r'(^|[\s,])\d*\.?\d+fr(\s|$|,|;)', stripped) \
           or re.search(r'repeat\(\s*\d+\s*,\s*\d*\.?\d+fr\s*\)', stripped):
            bad_tracks.append('%s: %s' % (src_name, val.strip()[:70]))
chk('网格轨道没有裸 1fr（必须 minmax(0,1fr)）', not bad_tracks,
    ' | '.join(bad_tracks[:4]))

# ---- ② .kv 标签**不能**收缩，靠整行换行解决窄容器 ----
# 这条判据的方向改过（2026-10-09 第二轮）。原判据要求「标签可收缩」，
# 修的是「长英文标签撑破父级 → 整页横向滚动」；但那个修法过头了：
# flex:0 1 auto + min-width:0 会把标签压成**一字一行** ——
# 真机几何实测 390px 下「Boot Mode」被压成 9px 宽 / 9 行，就是「竖着的字」。
# 正解是 flex:0 0 auto（不收缩）+ .kv 自身 flex-wrap:wrap（放不下就换行）。
_kv = re.search(r'\.kv\{([^}]*)\}', css)
chk('.kv 存在', _kv is not None)
if _kv:
    chk('.kv 允许换行（flex-wrap:wrap）',
        'flex-wrap:wrap' in _kv.group(1).replace(' ', ''), _kv.group(1)[:90])
_kvb = re.search(r'\.kv b\{([^}]*)\}', css)
chk('.kv b 存在', _kvb is not None)
if _kvb:
    _b = _kvb.group(1).replace(' ', '')
    _f = re.search(r'flex:([0-9.]+)([0-9.]+)', _b)
    chk('.kv b 不收缩（flex-shrink:0，避免被压成竖排）',
        bool(_f) and float(_f.group(2)) == 0, _b[:90])
    chk('.kv b 有 max-width:100%（比整行还长时在空格处折行）',
        'max-width:100%' in _b, _b[:90])

# ---- ②b 卡片标题不得靠 padding-left 缩进 ----
# padding-left 只挪文字不挪盒子，标题会和下面的正文/表格左边缘错开，
# 一屏卡片读起来就是「标题歪了」。竖条要挂在卡片内边距里（left:-12px）。
chk('.card>h3 不带 padding-left（标题与正文左对齐）',
    re.search(r'\.card>h3\{[^}]*padding-left', css) is None)
chk('.card>h3::before 竖条挂在卡片内边距（left 为负）',
    re.search(r'\.card>h3::before\{[^}]*left:-', css) is not None)

# ---- ②c 禁止 overflow-wrap:anywhere ----
# anywhere 会把元素的 min-content 算成 1 个字符，flex 就能把它压成细柱 → 竖排。
# break-word 不影响 min-content，是正确选择。
chk('CSS 里没有 overflow-wrap:anywhere（会把 min-content 变成 1 字符）',
    'overflow-wrap:anywhere' not in css.replace(' ', ''))

# ---- ②d 多列日志行必须可换行 ----
# .lv 46 + .ts 130 + .mo 76 + gap 30 = 282px 是固定的；.ms 若写 flex:1，
# 窄屏下只剩 28px —— 实测一条日志被折成 24 行、每行 3 个字（「竖着的字」）。
_lg = re.search(r'\.logline\{([^}]*)\}', css)
chk('.logline 存在', _lg is not None)
if _lg:
    chk('.logline 允许换行（flex-wrap:wrap）',
        'flex-wrap:wrap' in _lg.group(1).replace(' ', ''), _lg.group(1)[:90])
_lms = re.search(r'\.logline \.ms\{([^}]*)\}', css)
chk('.logline .ms 存在', _lms is not None)
if _lms:
    _m = _lms.group(1).replace(' ', '')
    chk('.logline .ms 用 flex-basis 而不是裸 flex:1（放不下会整行换行）',
        'flex:11' in _m or 'flex-basis' in _m, _m[:90])
    chk('.logline .ms 不用 word-break:break-all（按词换行更好读）',
        'word-break:break-all' not in _m, _m[:90])

# ---- ②e 窄屏必须压过内联固定宽度 ----
# 视图里有几十处 style="flex:0 0 110px"，内联优先级高于样式表；
# 不 !important 的话手机上输入框仍是固定宽度左对齐，右边留一大片空白。
# ⚠️ 不要把它限定在「第一个 @media (max-width:760px) 块」里 ——
#    app.css 有好几个 760px 断点块，.row>* 那条在「手机/窄屏适配」那一块。
chk('窄屏 .row>* 用 !important 压过内联固定宽度',
    re.search(r'\.row>\*\{[^}]*flex:1 1 100% !important', css) is not None)

# ---- ②f 单元格 / 徽标 / 表单标签不得被压成竖排（2026-10-09 第三轮） ----
# 前两轮都只盯「卡片里的 .kv / .logline」，漏掉了**表格**：默认 CJK 可以在
# 任意两个字之间断行，于是单元格的 min-content 只有 1 个字宽，自动表格布局
# 会把这一列一路压到 ~25px。真机几何实测（_dev/live-text-audit.sh，800px）：
#   · 单元格里的徽标「虚拟磁盘」被压成 25px / 4 行（一列一个字）
#   · 表头「修改时间」32px / 4 行
#   · 表单标签「其它域名（逗号分隔）」34px / 5 行
# 统一用 word-break:keep-all 给一个「词」宽度的下限；表头再 nowrap 兜一层。
_td = re.search(r'\nth,td\{([^}]*)\}', css)
chk('th,td 规则存在', _td is not None)
if _td:
    _t = _td.group(1).replace(' ', '')
    chk('th,td 用 word-break:keep-all（否则表格列被压成一列一个字）',
        'word-break:keep-all' in _t, _t[:110])
_th = re.search(r'\nth\{([^}]*)\}', css)
chk('th 规则存在', _th is not None)
if _th:
    chk('th 用 white-space:nowrap（表头永不折成竖排）',
        'white-space:nowrap' in _th.group(1).replace(' ', ''), _th.group(1)[:110])
_tag = re.search(r'\n\.tag\{([^}]*)\}', css)
chk('.tag 规则存在', _tag is not None)
if _tag:
    _tg = _tag.group(1).replace(' ', '')
    chk('.tag 用 word-break:keep-all（徽标不被压成一列一个字）',
        'word-break:keep-all' in _tg, _tg[:110])
    chk('.tag 不用 white-space:nowrap（英文长徽标会顶出内容区）',
        'white-space:nowrap' not in _tg, _tg[:110])
chk('.row>label 用 word-break:keep-all（表单标签不被压成竖排）',
    re.search(r'\.row>label\{[^}]*word-break:keep-all', css) is not None)
chk('#al-rules>.dep-item 是块级（.dep-item 是横向 flex，直接塞块会把 .row 压成 26px）',
    re.search(r'#al-rules>\.dep-item\{[^}]*display:block', css) is not None)
chk('.dk-trow 单元格可断行（长设备名不撑破内容区）',
    re.search(r'\.dk-trow>\*\{[^}]*min-width:0', css) is not None)
chk('.ul-trow 单元格可断行（窄屏两列下长 MAC/IPv6 不撑破内容区）',
    re.search(r'\.ul-trow>\*\{[^}]*min-width:0', css) is not None)

# ---- ②g 依赖卡片：正文不能被标签/按钮挤成细柱（2026-10-10 第四轮） ----
# `.dep-grid{repeat(auto-fill,minmax(300px,1fr))}` → **屏越宽列数越多、卡片越窄**，
# 1920 下每张只有 ~307px。卡片里还有分组标签 + 必需/可选标签 + 「安装」按钮，
# 一行塞不下；`.dep-body{flex:1}`（= flex:1 1 0%，下限 0）就被压到 **27px**，
# 描述文字折成 23 行、每行 5 个字 —— 真机上就是「字是竖着的」。
# 而且窄屏（1 列、卡片 560px）反而看不出来，所以必须在 1920/2560 下量。
_di = re.search(r'\n\.dep-item\{([^}]*)\}', css)
chk('.dep-item 规则存在', _di is not None)
if _di:
    chk('.dep-item 允许换行（flex-wrap:wrap，否则正文被标签挤成细柱）',
        'flex-wrap:wrap' in _di.group(1).replace(' ', ''), _di.group(1)[:110])
_db = re.search(r'\.dep-item \.dep-body\{([^}]*)\}', css)
chk('.dep-item .dep-body 规则存在', _db is not None)
if _db:
    _d = _db.group(1).replace(' ', '')
    chk('.dep-item .dep-body 的 flex-basis 有像素下限（不能是裸 flex:1）',
        re.search(r'flex:11\d+px', _d) is not None, _d[:110])
chk('app.js 里没有内联 style="flex:1" 覆盖 .dep-body（内联优先级更高）',
    'class="dep-body" style="flex:1"' not in app)
# .switch 同理：inline-flex 的「拨杆 + 名称 + 说明」，名称会被说明挤成细柱
# （真机 390px：「不主动获取（without-acquire）」66px / 3 行）。
_sw = re.search(r'\n\.switch\{([^}]*)\}', css)
chk('.switch 规则存在', _sw is not None)
if _sw:
    chk('.switch 允许换行（flex-wrap:wrap）',
        'flex-wrap:wrap' in _sw.group(1).replace(' ', ''), _sw.group(1)[:110])
chk('.switch b 不收缩（flex:0 0 auto）',
    re.search(r'\.switch b\{[^}]*flex:0 0 auto', css) is not None)

# ---- ③ 横向滚动容器 ----
chk('.tw 横向滚动容器仍在', re.search(r'\.tw\{[^}]*overflow-x:auto', css) is not None)

print('\n==== t-layout: %d 条判据, %d 失败 ====' % (N, len(FAILS)))
if FAILS:
    for f in FAILS:
        print('  - ' + f)
    sys.exit(1)
print('LAYOUT_OK')

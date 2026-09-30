#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
theme —— Drouter「主题之家」主题引擎（纯逻辑，零第三方依赖，可离线单测）

职责：
  * 定义主题的数据结构（变量表 + 元信息）
  * 严格校验一个主题包是否合法（不合法给出中文原因，绝不应用）
  * 把主题包渲染成一段可用的 CSS（覆盖 :root 变量）
  * 打包 / 解包 .zip 主题包（打包用内置 zipfile，解包做目录穿越与炸弹防护）

设计要点：
  * 变量白名单（THEME_VARS）是唯一允许被主题覆盖的 CSS 自定义属性集合；
    这样即使用户上传的主题包含恶意内容，也无法注入到规则体外（值里有严格字符校验）。
  * 颜色值只接受 #rgb / #rrggbb / #rrggbbaa / rgb() / rgba() / hsl() / hsla()
    / 具名色，以及少数安全的关键字（transparent / currentColor / none）。
    任何 `;{}<>`、`url(`、`expression(`、`@`、反斜杠、注释符号都会被拒绝。
  * 数值类变量只接受「数字 + 单位」，单位白名单可控。

本模块被 drouter-helper.py import；不依赖 helper 的其它函数，便于单独测试。
"""

import io
import os
import re
import json
import time
import zipfile

# ------------------------------------------------------------------ 常量

MAX_THEME_BYTES = 512 * 1024          # 单个主题包解压后总大小上限（防 zip 炸弹）
MAX_ENTRY_BYTES = 256 * 1024          # 单个文件解压后大小上限
MAX_ENTRIES = 64                       # 主题包内文件数量上限
MAX_NAME_LEN = 48                      # 主题名长度
MAX_CSS_LEN = 64 * 1024                # 自定义 CSS 长度上限

THEME_EXT = '.drtheme'

# 主题变量白名单：变量名 -> 分类（用于前端分组显示）
# 只允许覆盖这些变量，其它变量主题无法触碰。
THEME_VARS = {
    # 基础配色
    '--bg': 'base', '--bg2': 'base', '--panel': 'base', '--panel2': 'base',
    '--line': 'base', '--line2': 'base',
    '--txt': 'base', '--txt2': 'base', '--txt3': 'base',
    # 主色与状态色
    '--pri': 'color', '--pri-d': 'color', '--pri-l': 'color',
    # 主色本身若太浅（如暖橙 #f97316），当链接/强调文字用会看不清；
    # --pri-text 是同一色调的「可读版本」，--pri 仍保留原色用于填充与按钮。
    '--pri-text': 'color',
    '--ok': 'color', '--ok-l': 'color',
    '--warn': 'color', '--warn-l': 'color',
    '--err': 'color', '--err-l': 'color',
    '--info': 'color', '--info-l': 'color',
    # 形状与阴影
    '--r': 'shape', '--sh': 'shape', '--grad': 'shape',
    '--side': 'layout', '--side-c': 'layout',
}

# 变量分类的中文名（前端下拉分组用）
THEME_CATS = [
    ('base', '基础配色'),
    ('color', '主色与状态色'),
    ('shape', '圆角 / 阴影 / 渐变'),
    ('layout', '布局尺寸'),
]

# 变量中文说明（前端提示）
THEME_VAR_CN = {
    '--bg': '页面背景', '--bg2': '悬停背景', '--panel': '卡片背景', '--panel2': '表头背景',
    '--line': '边框', '--line2': '浅分隔线',
    '--txt': '主文字', '--txt2': '次要文字', '--txt3': '弱化文字',
    '--pri': '主题主色', '--pri-d': '主色加深', '--pri-l': '主色浅底',
    '--pri-text': '主色（文字用，自动加深以保证可读）',
    '--ok': '成功色', '--ok-l': '成功浅底',
    '--warn': '警告色', '--warn-l': '警告浅底',
    '--err': '错误色', '--err-l': '错误浅底',
    '--info': '信息色', '--info-l': '信息浅底',
    '--r': '卡片圆角', '--sh': '卡片阴影', '--grad': '主按钮渐变',
    '--side': '侧栏宽度', '--side-c': '侧栏折叠宽度',
}

# 值校验：颜色
# 0~255 的整数写法（避免 [0-9]{1,3} 放行 300 这类越界值）
_N255 = r'(?:\d|[1-9]\d|1\d{2}|2[0-4]\d|25[0-5])'
# 透明度：0 / 1 / .5 / 0.35
_ALPHA = r'(?:0|1|0?\.\d{1,3})'
# 色相：0~360（可带小数或 deg 后缀）
_HUE = r'(?:\d|[1-9]\d|[12]\d{2}|3[0-5]\d|360)(?:\.\d+)?(?:deg)?'
# 百分比：0~100
_PCT = r'(?:\d|[1-9]\d|100)%'

_RE_HEX = re.compile(r'^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$')
_RE_RGB = re.compile(r'^rgba?\(\s*%s\s*,\s*%s\s*,\s*%s(?:\s*,\s*%s)?\s*\)$'
                     % (_N255, _N255, _N255, _ALPHA))
_RE_HSL = re.compile(r'^hsla?\(\s*%s\s*,\s*%s\s*,\s*%s(?:\s*,\s*%s)?\s*\)$'
                     % (_HUE, _PCT, _PCT, _ALPHA))
_NAMED = {
    'transparent', 'currentcolor', 'black', 'white', 'red', 'green', 'blue',
    'gray', 'grey', 'silver', 'navy', 'teal', 'olive', 'purple', 'orange',
    'yellow', 'pink', 'brown', 'lime', 'aqua', 'cyan', 'magenta', 'maroon',
    'fuchsia', 'gold', 'indigo', 'violet', 'tomato', 'salmon', 'khaki',
    'coral', 'crimson', 'seagreen', 'steelblue', 'slategray', 'slategrey',
    'darkgrey', 'darkgray', 'lightgrey', 'lightgray', 'whitesmoke', 'ivory',
    'beige', 'lavender', 'plum', 'orchid', 'turquoise', 'skyblue', 'royalblue',
    'midnightblue', 'dodgerblue', 'forestgreen', 'darkgreen', 'darkred',
    'darkorange', 'goldenrod', 'chocolate', 'sienna', 'peru', 'wheat',
}

# 值的最大长度（防止超长垃圾串）
MAX_VAL_LEN = 220

# 主题名允许的字符：中文、字母、数字、空格、- _ . ( )
_RE_NAME = re.compile(r'^[\u4e00-\u9fa5A-Za-z0-9 _.\-()]{1,%d}$' % MAX_NAME_LEN)
# 主题 id（用于目录名/文件名）：仅小写字母数字与 -
_RE_ID = re.compile(r'^[a-z0-9][a-z0-9\-]{1,63}$')
# 十六进制色（用于自动生成配色方案）
_RE_HEX_STRICT = re.compile(r'^#([0-9a-fA-F]{6})$')


# ------------------------------------------------------------------ 校验


def _bad(msg, code='BAD_THEME'):
    return {'ok': False, 'code': code, 'msg_cn': msg}


def valid_css_value(v, var=''):
    """校验一个 CSS 变量取值是否安全。返回 (ok, 中文原因)"""
    if v is None:
        return False, '取值不能为空'
    s = str(v).strip()
    if not s:
        return False, '取值不能为空'
    if len(s) > MAX_VAL_LEN:
        return False, '取值过长（最多 %d 字符）' % MAX_VAL_LEN
    low = s.lower()
    # 危险字符 / 语法
    for ch in (';', '{', '}', '<', '>', '@', '\\', '!', '`'):
        if ch in s:
            return False, '包含非法字符「%s」' % ch
    if '/*' in s or '*/' in s:
        return False, '不允许出现注释符号'
    if 'url(' in low or 'expression' in low or 'import' in low or 'javascript' in low:
        return False, '不允许使用 url() / expression / import 等外部引用'
    # 变量引用（--x: var(--y)）——只允许引用白名单内的变量
    if low.startswith('var('):
        m = re.match(r'^var\(\s*(--[a-z0-9\-]+)\s*(?:,[^)]*)?\)$', low)
        if not m:
            return False, 'var() 引用写法不正确'
        if m.group(1) not in THEME_VARS:
            return False, '不允许引用未开放的主题变量「%s」' % m.group(1)
        return True, ''
    # 颜色
    if s.startswith('#'):
        return (_RE_HEX.match(s) is not None or
                _RE_HSL.match(low) is not None, '颜色格式不正确（支持 #rgb / #rrggbb / #rrggbbaa）'
                if not _RE_HEX.match(s) else '')
    if low.startswith('rgb') or low.startswith('hsl'):
        if _RE_RGB.match(low) or _RE_HSL.match(low):
            return True, ''
        return False, '颜色函数格式不正确（示例：rgba(31,111,235,.35)）'
    if low in _NAMED:
        return True, ''
    # 数值 + 单位（圆角 / 宽度）
    if re.match(r'^\d+(?:\.\d+)?(px|rem|em|%|vh|vw|pt|ch)$', low):
        if var and var in ('--r', '--side', '--side-c'):
            if low.endswith(('vh', 'vw')):
                return False, '该变量不支持 vh / vw 单位'
        return True, ''
    if low == '0':
        return True, ''
    # 阴影 / 渐变等复合值：做保守的「字符白名单」检查
    if re.match(r'^[0-9a-zA-Z#(),.\s%\-]+$', s):
        if low.startswith('linear-gradient(') or low.startswith('radial-gradient('):
            if 'gradient(' not in low:
                return False, '渐变写法不正确'
            return True, ''
        # 阴影：允许 "0 1px 2px rgba(0,0,0,.1), 0 2px 8px rgba(...)"
        if re.match(r'^(?:(?:-?\d+(?:\.\d+)?(?:px|em|rem)?|rgba?\([^)]*\)|hsla?\([^)]*\)|'
                    r'#[0-9a-fA-F]{3,8}|inset|none|,|\s)+)$', s):
            return True, ''
    return False, '取值格式无法识别，请使用颜色 / 数值(带单位) / var(--已开放变量)'


def validate_theme(obj):
    """校验主题对象。返回 (ok, 错误消息 / 规范化后的主题)"""
    if not isinstance(obj, dict):
        return False, '主题内容必须是一个 JSON 对象'
    name = str(obj.get('name') or '').strip()
    if not name:
        return False, '缺少主题名称（name）'
    if not _RE_NAME.match(name):
        return False, ('主题名称只能包含中文、字母、数字、空格与 - _ . ( )，'
                       '且不超过 %d 个字符' % MAX_NAME_LEN)
    desc = str(obj.get('description') or '').strip()
    if len(desc) > 300:
        return False, '主题描述过长（最多 300 字）'
    author = str(obj.get('author') or '').strip()
    if len(author) > 64:
        return False, '作者名过长（最多 64 字）'
    ver = str(obj.get('version') or '1.0').strip()
    if len(ver) > 24:
        return False, '版本号过长'

    dark = bool(obj.get('dark'))

    vars_in = obj.get('vars')
    if vars_in is None:
        vars_in = {}
    if not isinstance(vars_in, dict):
        return False, 'vars 必须是一个「变量名 → 取值」的对象'
    if len(vars_in) > len(THEME_VARS):
        return False, '变量数量超出上限'
    clean_vars = {}
    for k, v in vars_in.items():
        kk = str(k).strip()
        if not kk.startswith('--'):
            kk = '--' + kk
        if kk not in THEME_VARS:
            return False, '不支持的变量「%s」（不在可定制变量清单内）' % kk
        if isinstance(v, dict):
            # 允许 {"value": "...", "on": true} 形式
            if v.get('on') is False:
                continue
            v = v.get('value')
        vv = str(v).strip() if v is not None else ''
        if not vv:
            continue
        good, why = valid_css_value(vv, kk)
        if not good:
            return False, '变量「%s」取值不合法：%s' % (kk, why)
        clean_vars[kk] = vv

    if not clean_vars:
        return False, '主题至少要定义 1 个变量'

    extra = str(obj.get('css') or '')
    if extra:
        if len(extra) > MAX_CSS_LEN:
            return False, '自定义 CSS 过长（最多 %d 字节）' % MAX_CSS_LEN
        # 自定义 CSS 只允许「选择器 { 声明 }」形式，禁止 @ 规则 / 脚本 / 外部引用
        bad, why = _check_extra_css(extra)
        if bad:
            return False, why

    theme = {
        'id': '',                       # 由调用方分配
        'name': name,
        'description': desc,
        'author': author,
        'version': ver,
        'dark': dark,
        'vars': clean_vars,
        'css': extra,
        'builtin': bool(obj.get('builtin')),
        'created_at': obj.get('created_at') or '',
        'updated_at': obj.get('updated_at') or '',
    }
    return True, theme


def _check_extra_css(css):
    """额外 CSS 的安全检查：只允许平面选择器 + 声明块，禁止嵌套与 at-rule。"""
    s = re.sub(r'/\*.*?\*/', '', css, flags=re.S)      # 去注释
    if '@' in s:
        return True, '自定义 CSS 不允许使用 @ 规则（如 @import / @media）'
    if '\\' in s:
        return True, '自定义 CSS 不允许使用转义字符'
    if re.search(r'(?i)(javascript\s*:|expression\s*\(|behavior\s*:|url\s*\()', s):
        return True, '自定义 CSS 不允许使用 url() / expression() / javascript: 等外部引用'
    # 粗略结构校验：整体必须能拆成 selector{...} 序列
    rest = s.strip()
    guard = 0
    while rest:
        guard += 1
        if guard > 200:
            return True, '自定义 CSS 结构过于复杂'
        m = re.match(r'^([^{}]+)\{([^{}]*)\}', rest, re.S)
        if not m:
            return True, '自定义 CSS 语法不正确：应为「选择器 { 属性: 取值; }」形式'
        sel = m.group(1).strip()
        body = m.group(2)
        if not sel:
            return True, '自定义 CSS 存在空选择器'
        if re.search(r'[<>]', sel):
            return True, '自定义 CSS 选择器包含非法字符'
        # 声明体：prop: value; 逐条检查
        for decl in body.split(';'):
            decl = decl.strip()
            if not decl:
                continue
            if ':' not in decl:
                return True, '自定义 CSS 声明缺少冒号：%s' % decl[:40]
            prop, val = decl.split(':', 1)
            prop = prop.strip().lower()
            if not re.match(r'^(--[a-z0-9\-]+|[a-z\-]+)$', prop):
                return True, '自定义 CSS 属性名不合法：%s' % prop[:40]
            if not re.match(r'^[0-9a-zA-Z#(),.\s%\-"/_]+$', val.strip()):
                return True, '自定义 CSS 取值含非法字符：%s' % decl[:40]
        rest = rest[m.end():].strip()
    return False, ''


# ------------------------------------------------------------------ 渲染


def theme_to_css(theme):
    """把主题渲染成一段 CSS 文本（可直接写盘并 <link> 引入）。

    始终包在 :root{...} 里；自定义 CSS 追加在后面。
    同时输出一份深/浅色标记注释，便于排查。
    """
    lines = []
    lines.append('/* Drouter 主题：%s%s —— 由「主题之家」生成，请勿手工编辑 */'
                 % (theme.get('name') or '未命名',
                    (' v' + theme['version']) if theme.get('version') else ''))
    lines.append('/* 生成时间：%s */' % time.strftime('%Y-%m-%d %H:%M:%S'))
    lines.append(':root{')
    for k in sorted(theme.get('vars') or {}):
        lines.append('  %s:%s;' % (k, (theme['vars'][k] or '').strip()))
    lines.append('}')
    dark = bool(theme.get('dark'))
    lines.append(':root{color-scheme:%s}' % ('dark' if dark else 'light'))
    extra = (theme.get('css') or '').strip()
    if extra:
        lines.append('')
        lines.append('/* ---- 作者自定义 CSS ---- */')
        lines.append(extra)
    lines.append('')
    return '\n'.join(lines)


# ------------------------------------------------------------------ 内置主题


def _hex_rgb(h):
    h = h.lstrip('#')
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_hex(r, g, b):
    return '#%02x%02x%02x' % (max(0, min(255, int(round(r)))),
                              max(0, min(255, int(round(g)))),
                              max(0, min(255, int(round(b)))))


def mix(c1, c2, t):
    """把 c1 往 c2 混合 t（0~1）"""
    r1, g1, b1 = _hex_rgb(c1)
    r2, g2, b2 = _hex_rgb(c2)
    return _rgb_hex(r1 + (r2 - r1) * t, g1 + (g2 - g1) * t, b1 + (b2 - b1) * t)


def tone(base, t, dark=False):
    """生成一个「基色 + 混合目标」的色调：t>0 提亮，t<0 加深"""
    if dark:
        return mix(base, '#ffffff', t) if t > 0 else mix(base, '#000000', -t)
    return mix(base, '#ffffff', t) if t > 0 else mix(base, '#000000', -t)


def _lum(rgb):
    def f(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = [f(x) for x in rgb]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg, bg):
    """WCAG 相对亮度对比度（1.0 ~ 21.0）"""
    la, lb = _lum(_hex_rgb(fg)), _lum(_hex_rgb(bg))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def ensure_contrast(fg, bg, target=4.5):
    """保证 fg 在 bg 上的对比度不低于 target（对任意主色都要成立）。

    用户可以在设计器里挑任意主色，派生色一旦压得太浅，文字就会糊。
    这里按「远离背景」的方向逐级调整直到达标，最多 40 级必然收敛。

    方向由背景自身的亮度决定（而不是调用方传的「是否深色模式」）——
    深色主题里 --pri-l 反而可能是浅色底，按模式判断会得到完全相反的调整方向。
    """
    try:
        _hex_rgb(fg)
        _hex_rgb(bg)
    except Exception:
        return fg
    if contrast(fg, bg) >= target:
        return fg
    to_white = _lum(_hex_rgb(bg)) < 0.18      # 背景偏暗 → 文字往白走
    step = 0.04
    for _ in range(40):
        fg = mix(fg, '#ffffff' if to_white else '#000000', step)
        if contrast(fg, bg) >= target:
            return fg
    return ('#ffffff' if to_white else '#000000')


def make_palette(primary, dark=False, name='自定义主题', desc='由主题设计器生成'):
    """由单一主色生成一套完整、可用的配色方案（保证对比度不会太离谱）。"""
    p = primary if _RE_HEX_STRICT.match(str(primary or '')) else '#1f6feb'
    if dark:
        # 深色主题里 pri-l 用作「选中项浅底」，应当是主色的深色微透明感的版本，
        # 而不是往白里混（那样会在深色背景上糊出一块亮斑）。
        pri_l = mix(p, '#0b1220', 0.84)
    else:
        pri_l = tone(p, 0.86)
    pri_d = tone(p, -0.18)
    if dark:
        bg = '#111827'
        bg2 = '#1a2437'
        panel = '#161f30'
        panel2 = '#1b2537'
        line = '#26314a'
        line2 = '#1f2a3f'
        txt = '#e8eef8'
        txt2 = '#a8b6cc'
        txt3 = '#7b8aa3'
        ok, warn, err, info = '#3ddc84', '#f5c451', '#ff6b5e', '#4fb8f0'
        ok_l, warn_l, err_l, info_l = '#12301f', '#3a2f10', '#3a1a17', '#0f2a3b'
    else:
        bg = mix(p, '#ffffff', 0.945)
        bg2 = mix(p, '#ffffff', 0.905)
        panel = '#ffffff'
        panel2 = mix(p, '#ffffff', 0.965)
        line = mix(p, '#ffffff', 0.83)
        line2 = mix(p, '#ffffff', 0.93)
        txt = '#161f30'
        txt2 = mix(p, '#556480', 0.35)
        txt3 = '#8894a8'
        ok, warn, err, info = '#12813f', '#9a6700', '#c0392b', '#0b6e99'
        ok_l, warn_l, err_l, info_l = '#e3f7ea', '#fff8e1', '#fdecea', '#e5f4fb'
    # 可读性兜底：底色是由主色稀释而来，主色越浅，派生文字色越容易看不清。
    # 三级文字统一按 WCAG AA（正文 4.5）校正，并用阶梯式目标保住层次感：
    # 主文字 7.0 / 次要 5.5 / 弱化 4.5 —— 都 ≥ 4.5 且不会被拉平到同一个色。
    txt = ensure_contrast(txt, panel, 7.0)
    txt = ensure_contrast(txt, bg, 7.0)
    txt2 = ensure_contrast(txt2, bg, 5.5)
    txt2 = ensure_contrast(txt2, panel, 5.5)
    txt3 = ensure_contrast(txt3, bg, 4.5)
    txt3 = ensure_contrast(txt3, panel, 4.5)
    # 主色本身也会被当文字用（选中菜单项、链接、强调色），
    # 必须保证它在自己的浅底 pri-l 上仍然清晰。
    _pri_safe = ensure_contrast(p, pri_l, 4.5)
    p_txt = _pri_safe          # 用于文字的主色
    grad = 'linear-gradient(135deg,%s 0%%,%s 45%%,%s 100%%)' % (tone(p, 0.16), p, pri_d)
    return {
        'name': name, 'description': desc, 'author': 'Drouter 内置', 'version': '1.0',
        'dark': dark,
        'vars': {
            '--bg': bg, '--bg2': bg2, '--panel': panel, '--panel2': panel2,
            '--line': line, '--line2': line2,
            '--txt': txt, '--txt2': txt2, '--txt3': txt3,
            '--pri': p, '--pri-d': pri_d, '--pri-l': pri_l,
            # p_txt：保证可读的主色变体；p 本身仍是「填充用」的原色
            '--pri-text': p_txt,
            '--ok': ok, '--ok-l': ok_l, '--warn': warn, '--warn-l': warn_l,
            '--err': err, '--err-l': err_l, '--info': info, '--info-l': info_l,
            '--r': '12px', '--side': '236px', '--side-c': '68px',
            '--sh': '0 1px 2px rgba(18,28,55,.05),0 2px 8px rgba(18,28,55,.05)',
            '--grad': grad,
        },
        'css': '',
    }


def builtin_themes():
    """返回内置主题列表（id 固定，不可删除）。"""
    out = []

    t = make_palette('#1f6feb', False, 'Drouter 经典蓝', '默认主题，蓝色系，浅色界面')
    t['id'] = 'default'
    t['builtin'] = True
    out.append(t)

    t = make_palette('#0d9488', False, '青竹', '清爽的青绿色，适合长时间查看')
    t['id'] = 'teal'
    t['builtin'] = True
    out.append(t)

    t = make_palette('#7c3aed', False, '紫罗兰', '高对比度的紫色调')
    t['id'] = 'violet'
    t['builtin'] = True
    out.append(t)

    t = make_palette('#0f766e', True, '暗夜墨绿', '深色主题，夜间查看更护眼')
    t['id'] = 'dark-green'
    t['builtin'] = True
    out.append(t)

    t = make_palette('#f97316', False, '暖阳', '暖橙色调，明亮活泼')
    t['id'] = 'warm'
    t['builtin'] = True
    out.append(t)

    t = make_palette('#e11d48', True, '暗夜绯红', '深色 + 绯红点缀')
    t['id'] = 'dark-rose'
    t['builtin'] = True
    out.append(t)

    for x in out:
        x['created_at'] = ''
        x['updated_at'] = ''
    return out


# ------------------------------------------------------------------ ZIP 打包 / 解包

THEME_MANIFEST = 'theme.json'


def pack_theme_zip(theme):
    """把主题打成 zip 字节流：theme.json + theme.css + README.txt"""
    if not isinstance(theme, dict):
        return None
    buf = io.BytesIO()
    manifest = {
        'schema': 'drouter.theme/1',
        'name': theme.get('name') or '未命名主题',
        'description': theme.get('description') or '',
        'author': theme.get('author') or '',
        'version': theme.get('version') or '1.0',
        'dark': bool(theme.get('dark')),
        'vars': theme.get('vars') or {},
        'css': theme.get('css') or '',
        'exported_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr(THEME_MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
        z.writestr('theme.css', theme_to_css(theme))
        z.writestr('README.txt', _readme_text(manifest))
    return buf.getvalue()


def _readme_text(m):
    return (
        'Drouter 主题包\n'
        '================\n\n'
        '名称：%s\n'
        '版本：%s\n'
        '作者：%s\n'
        '模式：%s\n\n'
        '说明：%s\n\n'
        '使用方法\n'
        '--------\n'
        '1. 打开 Drouter 管理台 → 系统 → 主题之家\n'
        '2. 点击「导入主题包」，选择本 .zip 文件\n'
        '3. 校验通过后点击「应用」即可\n\n'
        '包结构\n'
        '------\n'
        'theme.json   主题清单（变量与元信息，必需）\n'
        'theme.css    由 theme.json 渲染出的样式（仅供参考，导入时会重新生成）\n'
        'README.txt   本说明\n\n'
        '可定制的变量（仅下列变量会被接受，其它一律拒绝）：\n'
        '%s\n'
    ) % (m.get('name'), m.get('version'), m.get('author') or '未署名',
         '深色' if m.get('dark') else '浅色', m.get('description') or '（无）',
        '\n'.join('  %-12s %s' % (k, THEME_VAR_CN.get(k, '')) for k in sorted(THEME_VARS)))


def _safe_zip_member(name):
    """拒绝绝对路径与目录穿越。返回规范化后的相对路径。"""
    if not name:
        return None
    n = name.replace('\\', '/')
    if n.startswith('/') or n.startswith('~'):
        return None
    while n.startswith('./'):
        n = n[2:]
    parts = [x for x in n.split('/') if x not in ('', '.')]
    if any(x == '..' for x in parts):
        return None
    if not parts:
        return None
    return '/'.join(parts)


def unpack_theme_zip(data):
    """解析主题 zip。返回 (ok, 主题对象 / 中文错误)"""
    if not data:
        return False, '文件内容为空'
    if len(data) > 4 * 1024 * 1024:
        return False, '文件过大（上传上限 4 MB）'
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return False, '这不是一个有效的 ZIP 压缩包（请确认文件未损坏）'
    except Exception as e:
        return False, '无法读取压缩包：%s' % e
    with z:
        names = z.namelist()
        if len(names) > MAX_ENTRIES:
            return False, '压缩包内文件过多（上限 %d 个）' % MAX_ENTRIES
        total = 0
        manifest = None
        manifest_path = None
        for info in z.infolist():
            if info.is_dir():
                continue
            safe = _safe_zip_member(info.filename)
            if safe is None:
                return False, '压缩包内存在不安全的路径：%s' % info.filename
            if info.file_size > MAX_ENTRY_BYTES:
                return False, '压缩包内单个文件过大：%s' % info.filename
            total += info.file_size
            if total > MAX_THEME_BYTES:
                return False, '压缩包解压后总体积过大（上限 %d KB）' % (MAX_THEME_BYTES // 1024)
            base = safe.split('/')[-1].lower()
            if base in (THEME_MANIFEST, 'theme.json', 'drouter-theme.json', 'manifest.json'):
                if manifest is None or base == THEME_MANIFEST:
                    manifest_path = info.filename
        if manifest_path is None:
            return False, ('压缩包里没有找到 theme.json —— '
                           '请确认这是由「主题之家」导出的主题包，或按说明手工创建 theme.json')
        try:
            raw = z.read(manifest_path).decode('utf-8-sig')
        except UnicodeDecodeError:
            return False, 'theme.json 编码不是 UTF-8，请另存为 UTF-8 后重试'
        except Exception as e:
            return False, '读取 theme.json 失败：%s' % e
        try:
            manifest = json.loads(raw)
        except Exception as e:
            return False, 'theme.json 不是合法的 JSON：%s' % e

    good, res = validate_theme(manifest)
    if not good:
        return False, res
    res['imported_from'] = os.path.basename(str(manifest_path))
    return True, res


# ------------------------------------------------------------------ 工具


def slugify(name, fallback='theme'):
    """把主题名转成安全的目录/文件名片段。"""
    s = re.sub(r'[^A-Za-z0-9\u4e00-\u9fa5\-]+', '-', str(name or '')).strip('-').lower()
    s = re.sub(r'-{2,}', '-', s)
    if not s or len(s) < 2:
        return fallback
    return s[:48]


def theme_id_from_name(name, taken=()):
    """生成不冲突的主题 id（自定义主题用）。"""
    base = slugify(name)
    # id 只允许小写字母数字与 -
    base = re.sub(r'[^a-z0-9\-]+', '-', base).strip('-')
    if not base or len(base) < 2:
        base = 'theme'
    base = base[:48]
    if base not in taken and _RE_ID.match(base):
        return base
    i = 2
    while True:
        cand = '%s-%d' % (base[:40], i)
        if cand not in taken and _RE_ID.match(cand):
            return cand
        i += 1
        if i > 999:
            return 'theme-%d' % int(time.time())


def summarize(theme):
    """给前端用的精简结构（不含完整 css，减少传输量）。"""
    return {
        'id': theme.get('id') or '',
        'name': theme.get('name') or '',
        'description': theme.get('description') or '',
        'author': theme.get('author') or '',
        'version': theme.get('version') or '',
        'dark': bool(theme.get('dark')),
        'builtin': bool(theme.get('builtin')),
        'vars_count': len(theme.get('vars') or {}),
        'has_css': bool((theme.get('css') or '').strip()),
        'created_at': theme.get('created_at') or '',
        'updated_at': theme.get('updated_at') or '',
        # 缩略图需要的几个关键色
        'swatch': [
            (theme.get('vars') or {}).get('--pri', ''),
            (theme.get('vars') or {}).get('--bg', ''),
            (theme.get('vars') or {}).get('--panel', ''),
            (theme.get('vars') or {}).get('--txt', ''),
        ],
    }

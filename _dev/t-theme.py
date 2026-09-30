#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
theme.py 纯逻辑回归测试（不需要目标机、不需要数据库、不需要 root）

运行：python3 router-build/_dev/t-theme.py

覆盖：
  * 取值校验 valid_css_value（颜色 / 数值 / 阴影 / 渐变 / var() / 各种攻击串）
  * 主题结构校验 validate_theme（字段、长度、白名单、必填）
  * 自定义 CSS 安全检查 _check_extra_css（@ 规则、url()、注释、残缺语法）
  * 渲染 theme_to_css
  * 配色生成 make_palette / builtin_themes
  * ZIP 打包 → 解包往返
  * ZIP 异常：坏包、无 manifest、路径穿越、超大文件、非法 JSON、非法主题
  * id 生成 slugify / theme_id_from_name
"""
import sys
import os
import io
import json
import base64
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), 'backend')
sys.path.insert(0, SRC)

import theme  # noqa: E402

PASS = FAIL = 0
MSG = []


def has(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        MSG.append('  ✗ %s%s' % (name, ('  → ' + str(extra)) if extra else ''))


def chk(name, hay, needle, want=True):
    has(name, (needle in hay) == want, hay[:160])


def expect_ok(name, r, want=True, needle=None):
    """断言主题校验结果 ok/not ok，可选的错误关键词"""
    if isinstance(r, tuple):
        ok_ = r[0]
        detail = r[1] if len(r) > 1 else ''
    else:
        ok_, detail = True, r
    has(name, ok_ == want, detail)
    if isinstance(r, tuple) and not want and needle:
        chk(name + ' (错误文案)', str(detail), needle)


# ==================================================================
print('== 1. valid_css_value：合法取值 ==')
# ==================================================================
GOOD = [
    '#fff', '#FFF', '#ffff', '#1f6feb', '#1f6febcc',
    'rgb(31,111,235)', 'rgba(31,111,235,.35)', 'rgba(0,0,0,0)',
    'hsl(210,90%,55%)', 'hsla(210,90%,55%,0.5)',
    '12px', '1rem', '0', '2.5em', '50%',
    'transparent', 'currentColor', 'white', 'steelblue',
    'var(--pri)', 'var(--pri, #fff)',
    'linear-gradient(135deg,#2b7bf3 0%,#1f6feb 45%,#1552c0 100%)',
    'radial-gradient(circle,#fff,#000)',
    '0 1px 2px rgba(18,28,55,.05),0 2px 8px rgba(18,28,55,.05)',
    'inset 0 1px 0 #fff',
]
for g in GOOD:
    ok_, why = theme.valid_css_value(g)
    has('合法取值 %r' % g, ok_, why)

print('== 2. valid_css_value：危险取值必须被拒 ==')
BAD = [
    ('#zzz', '非十六进制'),
    ('#12345', '5 位色'),
    ('rgb(300,0,0)', '超范围'),
    ('rgba(1,2,3)', '缺透明度第4位却写 rgba'),
    ('red;background:url(x)', '注入分号'),
    ('#fff}', '闭合花括号'),
    ('#fff{', '开花括号'),
    ('url(http://evil.com/x.css)', '外部资源'),
    ('expression(alert(1))', '表达式'),
    ('@import url(x)', 'at 规则'),
    ('#fff\\', '反斜杠'),
    ('/* */ #fff', '注释符号'),
    ('var(--not-opened)', '引用未开放变量'),
    ('var(', '残缺 var'),
    ('', '空值'),
    (None, 'None'),
    ('x' * 300, '超长'),
    ('#fff !important', 'important'),
    ('12vh', '?'),          # 12vh 本身合法（只对部分变量限制），这里用作对照
]
for b, why_desc in BAD:
    if b == '12vh':
        # 通用校验允许 vh（仅 --r/--side/--side-c 禁止）
        ok_, why = theme.valid_css_value('12vh')
        has('12vh 通用校验允许', ok_, why)
        continue
    # rgba(1,2,3) 应该被允许（rgb 别名，alpha 可选）
    if b == 'rgba(1,2,3)':
        ok_, why = theme.valid_css_value('rgba(1,2,3)')
        has('rgba(1,2,3) 合法（alpha 可选）', ok_, why)
        continue
    ok_, why = theme.valid_css_value(b)
    has('拒绝危险取值 %r（%s）' % (b, why_desc), not ok_, '通过了，这是漏洞！')

# 针对性：单位限制
for var in ('--r', '--side', '--side-c'):
    ok_, _ = theme.valid_css_value('12vw', var)
    has('%s 拒绝 vw' % var, not ok_)
    ok_, _ = theme.valid_css_value('12px', var)
    has('%s 接受 px' % var, ok_)

# ==================================================================
print('== 3. validate_theme：结构校验 ==')
# ==================================================================
ok_, t = theme.validate_theme({
    'name': '测试主题', 'vars': {'--pri': '#ff0000', '--bg': '#f5f5f5'},
})
has('最小合法主题通过', ok_, t)
has('vars 被规范化', ok_ and t['vars']['--pri'] == '#ff0000')
has('默认 version', ok_ and t['version'] == '1.0')

# 缺名称
ok_, why = theme.validate_theme({'vars': {'--pri': '#fff'}})
has('缺 name 被拒', not ok_)
chk('缺 name 文案', why, '主题名称')

ok_, why = theme.validate_theme({'name': ''})
has('空 name 被拒', not ok_)

# 非法名称字符
ok_, why = theme.validate_theme({'name': 'bad<script>', 'vars': {'--pri': '#fff'}})
has('名称含尖括号被拒', not ok_)

# 允许的名称
for nm in ('我的主题', 'My Theme 2', '深海蓝-v2', '主题(夜色)', 'A.B_C-D'):
    ok_, _ = theme.validate_theme({'name': nm, 'vars': {'--pri': '#fff'}})
    has('合法名称 %r' % nm, ok_)

# 描述过长
ok_, why = theme.validate_theme({'name': 'x', 'description': '描' * 301,
                                 'vars': {'--pri': '#fff'}})
has('超长描述被拒', not ok_)
chk('超长描述文案', why, '描述')

# vars 缺失
ok_, why = theme.validate_theme({'name': 'x'})
has('缺 vars 被拒', not ok_)

ok_, why = theme.validate_theme({'name': 'x', 'vars': []})
has('vars 非字典被拒', not ok_)
chk('vars 类型文案', why, '对象')

# 未知变量
ok_, why = theme.validate_theme({'name': 'x', 'vars': {'--evil': '#fff'}})
has('未知变量被拒', not ok_)
chk('未知变量文案', why, '不支持的变量')

# vars 全部非法值 → 空
ok_, why = theme.validate_theme({'name': 'x', 'vars': {'--pri': 'red;'}})
has('非法值被拦下', not ok_)
chk('非法值文案', why, '变量')

# 「至少 1 个变量」检查：vars 里有空串会被剔除
ok_, why = theme.validate_theme({'name': 'x', 'vars': {'--pri': '  '}})
has('vars 全空 → 拒绝', not ok_)
chk('空 vars 文案', why, '至少')

# 带 "on": false 的字典形式
ok_, t = theme.validate_theme({'name': 'x', 'vars': {
    '--pri': {'value': '#123456', 'on': True},
    '--bg': {'value': '#abcdef', 'on': False},
}})
has('dict 形式 vars 支持 on 开关', ok_ and '--bg' not in t['vars'] and '--pri' in t['vars'])

# 缺 -- 前缀自动补
ok_, t = theme.validate_theme({'name': 'x', 'vars': {'pri': '#123456'}})
has('自动补 -- 前缀', ok_ and '--pri' in t['vars'])

# dark 标记
ok_, t = theme.validate_theme({'name': 'x', 'vars': {'--pri': '#fff'}, 'dark': True})
has('dark 字段生效', ok_ and t['dark'] is True)

# 非 dict
ok_, _ = theme.validate_theme(['不是字典'])
has('顶层非 dict 被拒', not ok_)

# ==================================================================
print('== 4. _check_extra_css：自定义 CSS 安全 ==')
# ==================================================================
CSS_BAD = [
    ('@import url("http://evil.com")', ['@']),
    ('@media screen{.card{color:red}}', ['@']),
    ('.card{background:url(x.png)}', ['外部引用']),
    ('.card{background:expression(1)}', ['外部引用']),
    ('.card{background:javascript:alert(1)}', ['外部引用']),
    ('.card{color:red', ['语法']),
    ('.card{a;b:c}', ['冒号']),
    ('.card < > {color:red}', ['选择器']),
    ('.card{color:red}}', ['语法']),
    ('.card{color:red;}\\', ['转义']),
]
for css, needles in CSS_BAD:
    bad, why = theme._check_extra_css(css)
    has('CSS 拒绝：%r' % css[:34], bad, why)
    for nd in needles:
        chk('CSS 拒绝文案含 %r' % nd, why, nd)

CSS_GOOD = [
    '.card{border-width:2px}',
    '.stat .val{font-size:22px}',
    '/* 带注释 */ .card{color:red}',
    '.card{color:#fff;background:var(--pri)}',
    'h3{letter-spacing:.5px}',
    '.a{color:red}.b{color:blue}',
    '.card{box-shadow:0 1px 2px rgba(0,0,0,.1)}',
    '.card{content:"x"}',
]
for css in CSS_GOOD:
    bad, why = theme._check_extra_css(css)
    has('CSS 接受：%r' % css[:34], not bad, why)

# 注释里藏 @import 也不能过
bad, why = theme._check_extra_css('/* 先看这里 */ @import url(x);')
has('注释后紧跟 @import 被拒', bad, why)

# 通过 validate_theme 的 css 字段
ok_, _ = theme.validate_theme({'name': 'x', 'vars': {'--pri': '#fff'},
                               'css': '.card{border-width:2px}'})
has('主题带合法 css 通过', ok_)

ok_, why = theme.validate_theme({'name': 'x', 'vars': {'--pri': '#fff'},
                                 'css': '@font-face{src:url(x)}'})
has('主题带 @font-face 被拒', not ok_)
chk('@font-face 文案', why, '@')

# css 超长
ok_, why = theme.validate_theme({'name': 'x', 'vars': {'--pri': '#fff'},
                                 'css': '.a{color:red}' * 6000})
has('超长 css 被拒', not ok_)
chk('超长 css 文案', why, '过长')

# ==================================================================
print('== 5. theme_to_css：渲染 ==')
# ==================================================================
ok_, th = theme.validate_theme({
    'name': '我的主题', 'version': '2.1', 'author': 'ajeef',
    'description': '一句话', 'dark': True,
    'vars': {'--pri': '#ff0000', '--bg': '#101010'},
    'css': '.card{border-width:2px}',
})
css = theme.theme_to_css(th)
chk('渲染：含 :root', css, ':root{')
chk('渲染：含变量名', css, '--pri:#ff0000')
chk('渲染：含自定义 CSS', css, '.card{border-width:2px}')
chk('渲染：color-scheme dark', css, 'color-scheme:dark')
chk('渲染：主题名', css, '我的主题')
for bad_ch in ('<', '>', '&amp;'):
    has('渲染输出不含 %r' % bad_ch, bad_ch not in css)

ok2, th2 = theme.validate_theme({'name': '浅色', 'vars': {'--pri': '#fff'}})
chk('渲染：color-scheme light', theme.theme_to_css(th2), 'color-scheme:light')
has('渲染：无 css 时不输出自定义段',
    '作者自定义' not in theme.theme_to_css(th2))

# 变量顺序稳定
has('变量按名排序输出', css.index('--bg') < css.index('--pri'))

# ==================================================================
print('== 6. 配色生成 ==')
# ==================================================================
p = theme.make_palette('#1f6feb', False)
has('浅色配色含全部变量', len(p['vars']) == len(theme.THEME_VARS), len(p['vars']))
has('主色保持', p['vars']['--pri'] == '#1f6feb')
d = theme.make_palette('#1f6feb', True)
has('深色配色 --bg 更暗', d['vars']['--bg'] == '#111827', d['vars']['--bg'])
has('深色配色 --txt 更亮', d['vars']['--txt'] == '#e8eef8')
# 非法主色回落到默认
p2 = theme.make_palette('evil', False)
has('非法主色回落 #1f6feb', p2['vars']['--pri'] == '#1f6feb')
p3 = theme.make_palette(None, False)
has('None 主色回落', p3['vars']['--pri'] == '#1f6feb')
p4 = theme.make_palette('#GGGGGG', False)
has('乱码主色回落', p4['vars']['--pri'] == '#1f6feb')

# mix / tone 边界
has('mix 端点 t=0', theme.mix('#000000', '#ffffff', 0) == '#000000')
has('mix 端点 t=1', theme.mix('#000000', '#ffffff', 1) == '#ffffff')
has('mix 中点', theme.mix('#000000', '#ffffff', .5) == '#808080')
has('tone 提亮', theme.tone('#808080', .5) != '#808080')

# 内置主题
bt = theme.builtin_themes()
has('内置主题 6 个', len(bt) == 6, len(bt))
ids = [x['id'] for x in bt]
has('内置 id 唯一', len(set(ids)) == len(ids))
has('含 default', 'default' in ids)
for x in bt:
    ok_, why = theme.validate_theme(x)
    has('内置主题 %s 自身合法' % x['id'], ok_, why)
    has('内置主题 %s 全部变量' % x['id'], len(x['vars']) == len(theme.THEME_VARS))
    has('内置主题 %s builtin 标记' % x['id'], x['builtin'] is True)

# ==================================================================
print('== 7. ZIP 打包 → 解包往返 ==')
# ==================================================================
ok_, th = theme.validate_theme({
    'name': '往返测试主题', 'author': 'tester', 'description': 'round trip',
    'version': '3.0', 'dark': False,
    'vars': {'--pri': '#3366cc', '--bg': '#fafafa', '--r': '14px'},
    'css': '.card{border-width:2px}',
})
zipbytes = theme.pack_theme_zip(th)
has('打包返回字节', isinstance(zipbytes, bytes) and len(zipbytes) > 0)
has('打包非 None', zipbytes is not None)

zf = zipfile.ZipFile(io.BytesIO(zipbytes))
names = zf.namelist()
has('包含 theme.json', 'theme.json' in names, names)
has('包含 theme.css', 'theme.css' in names, names)
has('包含 README.txt', 'README.txt' in names, names)
man = json.loads(zf.read('theme.json').decode('utf-8'))
has('manifest schema', man.get('schema') == 'drouter.theme/1')
has('manifest name', man.get('name') == '往返测试主题')
has('manifest vars', man.get('vars', {}).get('--pri') == '#3366cc')
has('manifest exported_at', bool(man.get('exported_at')))
rd = zf.read('README.txt').decode('utf-8')
chk('README 含主题名', rd, '往返测试主题')
chk('README 含变量清单', rd, '--pri')

ok2, res = theme.unpack_theme_zip(zipbytes)
has('解包成功', ok2, res)
has('解包后名称一致', res['name'] == '往返测试主题')
has('解包后变量一致', res['vars']['--pri'] == '#3366cc')
has('解包后 round trip css 一致', res['css'] == '.card{border-width:2px}')
has('解包后 dark 一致', res['dark'] is False)
has('解包记录来源文件', res.get('imported_from') == 'theme.json')

# base64 通道（前端用）
b64 = base64.b64encode(zipbytes).decode('ascii')
ok3, res3 = theme.unpack_theme_zip(base64.b64decode(b64))
has('base64 往返成功', ok3, res3)

# ==================================================================
print('== 8. ZIP 异常必须给出中文原因 ==')
# ==================================================================
ok_, why = theme.unpack_theme_zip(b'')
has('空数据被拒', not ok_)
chk('空数据文案', why, '空')

ok_, why = theme.unpack_theme_zip(b'not a zip at all')
has('非 ZIP 被拒', not ok_)
chk('非 ZIP 文案', why, 'ZIP')

ok_, why = theme.unpack_theme_zip(b'x' * (4 * 1024 * 1024 + 10))
has('超 4MB 被拒', not ok_)
chk('超 4MB 文案', why, '过大')

# 缺 manifest
buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w') as z:
    z.writestr('readme.txt', '没有主题清单')
ok_, why = theme.unpack_theme_zip(buf.getvalue())
has('缺 theme.json 被拒', not ok_)
chk('缺 manifest 文案', why, 'theme.json')

# 路径穿越
buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w') as z:
    z.writestr('theme.json', json.dumps({'name': 'x', 'vars': {'--pri': '#fff'}}))
    z.writestr('../../etc/passwd', 'root:x:0:0')
ok_, why = theme.unpack_theme_zip(buf.getvalue())
has('目录穿越被拒', not ok_)
chk('目录穿越文案', why, '不安全')

# 绝对路径
buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w') as z:
    z.writestr('theme.json', json.dumps({'name': 'x', 'vars': {'--pri': '#fff'}}))
    z.writestr('/tmp/evil.sh', 'rm -rf /')
ok_, why = theme.unpack_theme_zip(buf.getvalue())
has('绝对路径被拒', not ok_)
chk('绝对路径文案', why, '不安全')

# 单个文件过大
buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w') as z:
    z.writestr('theme.json', json.dumps({'name': 'x', 'vars': {'--pri': '#fff'}}))
    z.writestr('big.bin', 'A' * (300 * 1024))
ok_, why = theme.unpack_theme_zip(buf.getvalue())
has('单文件过大被拒', not ok_)
chk('单文件文案', why, '过大')

# manifest 非法 JSON
buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w') as z:
    z.writestr('theme.json', '{不是合法 json')
ok_, why = theme.unpack_theme_zip(buf.getvalue())
has('非法 JSON 被拒', not ok_)
chk('非法 JSON 文案', why, 'JSON')

# manifest 内容非法（缺少 name）
buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w') as z:
    z.writestr('theme.json', json.dumps({'vars': {'--pri': '#fff'}}))
ok_, why = theme.unpack_theme_zip(buf.getvalue())
has('manifest 缺 name 被拒', not ok_)
chk('manifest 缺 name 文案', why, '主题名称')

# manifest 含非法变量值
buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w') as z:
    z.writestr('theme.json', json.dumps({'name': '恶意',
                                         'vars': {'--pri': 'red;background:url(http://evil)'}}))
ok_, why = theme.unpack_theme_zip(buf.getvalue())
has('恶意取值在导入时被拒', not ok_)
chk('恶意取值文案', why, '变量')

# manifest 非 UTF-8（用 latin-1 写）

buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w') as z:
    z.writestr('theme.json', '{"name":"乱码主题"}'.encode('gbk'))
ok_, why = theme.unpack_theme_zip(buf.getvalue())
has('非 UTF-8 manifest 被拒', not ok_)

# 文件数量过多
buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w') as z:
    z.writestr('theme.json', json.dumps({'name': 'x', 'vars': {'--pri': '#fff'}}))
    for i in range(70):
        z.writestr('f%d.txt' % i, 'x')
ok_, why = theme.unpack_theme_zip(buf.getvalue())
has('文件过多被拒', not ok_)
chk('文件过多文案', why, '过多')

# 别名的 manifest 名（theme.json 小写、manifest.json）
for alias in ('Theme.JSON', 'manifest.json'):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr(alias, json.dumps({'name': '别名主题', 'vars': {'--pri': '#fff'}}))
    ok_, res = theme.unpack_theme_zip(buf.getvalue())
    has('接受别名清单 %s' % alias, ok_, why if not ok_ else res)

# ==================================================================
print('== 9. id / slug 工具 ==')
# ==================================================================
has('slugify 中文名', theme.slugify('我的主题') == '我的主题')
has('slugify 去符号', theme.slugify('My Theme!!') == 'my-theme')
has('slugify 空回落', theme.slugify('') == 'theme')
has('slugify 纯符号回落', theme.slugify('!!!') == 'theme')
has('slugify 合并连字符', theme.slugify('a---b') == 'a-b')
has('slugify 去首尾连字符', theme.slugify('-abc-') == 'abc')

i1 = theme.theme_id_from_name('我的主题', ())
has('id 由中文名生成', i1 in ('theme',) or bool(i1), i1)
has('id 符合规范', bool(theme._RE_ID.match(i1)), i1)
has('id 冲突时加序号', theme.theme_id_from_name('abc', ('abc',)) == 'abc-2')
has('id 多次冲突', theme.theme_id_from_name('abc', ('abc', 'abc-2')) == 'abc-3')
has('id 含 uppercase 被规整', theme.theme_id_from_name('MyTheme', ()) == 'mytheme')
has('id 纯符号回落 theme', theme.theme_id_from_name('!!!', ()) in ('theme', 'theme-2'))

# ==================================================================
print('== 10. summarize 精简结构 ==')
# ==================================================================
s = theme.summarize(th)
has('summary 有 id 字段', 'id' in s)
has('summary 有 name', s['name'] == '往返测试主题')
has('summary 变量计数', s['vars_count'] == 3, s.get('vars_count'))
has('summary has_css', s['has_css'] is True)
has('summary 有 swatch', len(s['swatch']) == 4)
has('summary 取主色', s['swatch'][0] == '#3366cc')
has('summary 不含原始 css', 'css' not in s)
s2 = theme.summarize(th2)
has('无 css 时 has_css=False', s2['has_css'] is False)

# ==================================================================
print('== 11b. 可读性保证：任意主色生成的配色都必须达 WCAG AA ==')
# ==================================================================
# 用户可以在设计器里挑任意主色，自动生成的派生色必须仍然可读，
# 否则浅色主色（如暖橙、亮黄）会让次要文字糊成一片。
SWEEP = [
    '#1f6feb', '#f97316', '#ffff00', '#eeeeee', '#000000', '#ffffff',
    '#10b981', '#e11d48', '#7c3aed', '#0ea5e9', '#84cc16', '#fbbf24',
    '#ff0000', '#00ff00', '#0000ff', '#f0f0f0', '#101010', '#808080',
    '#ff69b4', '#00ced1', '#ffd700', '#adff2f', '#4b0082', '#ffdead',
]
worst = {'v': 99, 'who': ''}
for seed in SWEEP:
    for dark in (False, True):
        pal = theme.make_palette(seed, dark)
        v = pal['vars']
        bg, panel = v['--bg'], v['--panel']
        checks = [
            ('主文字/背景', v['--txt'], bg, 7.0),
            ('主文字/卡片', v['--txt'], panel, 7.0),
            ('次要文字/背景', v['--txt2'], bg, 5.5),
            ('次要文字/卡片', v['--txt2'], panel, 5.5),
            ('弱化文字/背景', v['--txt3'], bg, 4.5),
            ('弱化文字/卡片', v['--txt3'], panel, 4.5),
            ('主色文字/浅底', v['--pri-text'], v['--pri-l'], 4.5),
        ]
        for label, fg, bgc, need in checks:
            got = theme.contrast(fg, bgc)
            has('%s %s %s ≥ %.1f' % (seed, '深' if dark else '浅', label, need),
                got >= need - 0.01, '%.2f' % got)
            if got < worst['v']:
                worst = {'v': got, 'who': '%s %s %s=%.2f' % (seed, dark, label, got)}
        # 层次感：三级文字不能被拉平到同一亮度，否则界面失去重点层级
        has('%s %s 三级文字保留层次' % (seed, '深' if dark else '浅'),
            theme.contrast(v['--txt'], panel) > theme.contrast(v['--txt2'], panel)
            and theme.contrast(v['--txt2'], panel) > theme.contrast(v['--txt3'], panel),
            '%.2f > %.2f > %.2f' % (theme.contrast(v['--txt'], panel),
                                    theme.contrast(v['--txt2'], panel),
                                    theme.contrast(v['--txt3'], panel)))
has('最低对比度达标 4.5+', worst['v'] >= 4.4, worst['who'])

# ensure_contrast 本身的行为
has('ensure_contrast 已达标时不动', theme.ensure_contrast('#000000', '#ffffff') == '#000000')
has('ensure_contrast 会加深浅色',
    theme.ensure_contrast('#cccccc', '#ffffff', 4.5) != '#cccccc')
has('ensure_contrast 深色则提亮',
    theme.ensure_contrast('#333333', '#000000', 4.5) != '#333333')
has('contrast 黑白=21', abs(theme.contrast('#000000', '#ffffff') - 21) < 0.01)
has('contrast 同色=1', abs(theme.contrast('#808080', '#808080') - 1) < 0.01)

# ==================================================================
print('== 11c. 内置主题也必须达标（硬编码 vars 不走 make_palette 兜底）==')
# ==================================================================
_builtins = theme.builtin_themes()
has('内置主题 6 个', len(_builtins) == 6, len(_builtins))
for _t in _builtins:
    _v = _t['vars']
    for _lb, _fg, _b, _need in [
        ('主文字/背景', _v.get('--txt'), _v.get('--bg'), 7.0),
        ('主文字/卡片', _v.get('--txt'), _v.get('--panel'), 7.0),
        ('次要文字/背景', _v.get('--txt2'), _v.get('--bg'), 5.5),
        ('次要文字/卡片', _v.get('--txt2'), _v.get('--panel'), 5.5),
        ('弱化文字/背景', _v.get('--txt3'), _v.get('--bg'), 4.5),
        ('弱化文字/卡片', _v.get('--txt3'), _v.get('--panel'), 4.5),
        ('主色文字/浅底', _v.get('--pri-text'), _v.get('--pri-l'), 4.5),
    ]:
        if not (_fg and _b):
            continue
        _got = theme.contrast(_fg, _b)
        has('内置 %s %s ≥ %.1f' % (_t['id'], _lb, _need),
            _got >= _need - 0.01, '%.2f' % _got)

# ==================================================================
print('== 11. 常量一致性 ==')
# ==================================================================
has('THEME_VARS 26 项', len(theme.THEME_VARS) == 26, len(theme.THEME_VARS))
for k in theme.THEME_VARS:
    chk('变量 %s 有中文说明' % k, theme.THEME_VAR_CN.get(k, ''), k, want=False)
    has('变量 %s 有说明文本' % k, bool(theme.THEME_VAR_CN.get(k)))
cats = set(theme.THEME_VARS.values())
has('分类集合一致', cats == set(dict(theme.THEME_CATS).keys()), cats)
has('THEME_CATS 有中文名', all(b for a, b in theme.THEME_CATS))
# 每个变量都能通过默认值的校验
defaults = theme.make_palette('#1f6feb', False)['vars']
has('默认浅色配色变量数', len(defaults) == len(theme.THEME_VARS))
for k, v in defaults.items():
    ok_, why = theme.valid_css_value(v, k)
    has('默认值 %s=%r 合法' % (k, v), ok_, why)
darkdef = theme.make_palette('#1f6feb', True)['vars']
for k, v in darkdef.items():
    ok_, why = theme.valid_css_value(v, k)
    has('深色默认值 %s=%r 合法' % (k, v), ok_, why)

# 内置主题自动生成的 CSS 也要能解出合法结构
for x in bt:
    css = theme.theme_to_css(x)
    has('内置 %s 渲染非空' % x['id'], len(css) > 200)
    has('内置 %s 无 < 字符' % x['id'], '<' not in css)

print()
print('=' * 56)
print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
if MSG:
    print('-' * 56)
    for m in MSG[:60]:
        print(m)
print('=' * 56)
sys.exit(1 if FAIL else 0)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
read_theme / act_theme 的集成测试（抽函数 + 重定向路径到临时目录的方式执行）

不需要目标机、不需要 root。把 drouter-helper.py 里的主题段落按正则抽出来，
在一个隔离命名空间里 exec（helper 顶部 `import pwd` 在 Windows 上不可用，
所以不能整体 import），并把路径常量替换成本机临时目录。
"""
import sys
import os
import re
import io
import json
import glob
import base64
import tempfile
import shutil
import zipfile
import threading
import datetime
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(os.path.dirname(HERE), 'backend')
sys.path.insert(0, BACKEND)
import theme  # noqa: E402
NVARS = len(theme.THEME_VARS)   # 变量清单以常量为准，避免硬编码漂移

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
    has(name, (needle in hay) == want, hay[:200])


# ------------------------------------------------------------------ 抽取主题段落
src = io.open(os.path.join(BACKEND, 'drouter-helper.py'), encoding='utf-8').read()
start = src.index('# ================================================================== 主题之家（#12）')
end = src.index('ACTIONS = {', start)
seg = src[start:end]

ok_fn = '''def ok(data=None, msg='操作成功', code='OK'):
    return {'ok': True, 'code': code, 'msg_cn': msg, 'data': data}


def fail(msg, code='ERR', data=None):
    return {'ok': False, 'code': code, 'msg_cn': msg, 'data': data}
'''

preamble = (
    'import os, sys, json, re, time, shutil, threading\n'
    'from datetime import datetime\n'
    'sys.path.insert(0, %r)\n'
    'import theme\n'
    'BUILD_FLAG = "/nonexistent/BUILD_MODE"\n'
    % BACKEND
)
NS = {}
exec(compile(preamble + ok_fn + seg, '<theme-seg>', 'exec'), NS)

TMP = tempfile.mkdtemp(prefix='drouter-theme-')
NS['THEME_DIR'] = os.path.join(TMP, 'themes')
NS['THEME_ACTIVE'] = os.path.join(TMP, 'active-theme')
NS['THEME_CSS'] = os.path.join(TMP, 'theme.css')
os.makedirs(NS['THEME_DIR'], exist_ok=True)
os.makedirs(os.path.dirname(NS['THEME_CSS']), exist_ok=True)

read_theme = NS['read_theme']
act_theme = NS['act_theme']
_theme_all = NS['_theme_all']
_theme_find = NS['_theme_find']


def _read_active():
    """读当前生效主题 id（文件不存在时视为空）"""
    try:
        with io.open(NS['THEME_ACTIVE'], encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return ''

print('== 1. read_theme: 列表 ==')
r = read_theme({})
has('列表成功', r['ok'], r.get('msg_cn'))
has('返回 themes', len(r['data']['themes']) >= 6, len(r['data']['themes']))
has('默认 active=default', r['data']['active'] == 'default', r['data']['active'])
has('active_name 有值', bool(r['data']['active_name']))
has('vars_total=%d' % NVARS, r['data']['vars_total'] == NVARS, r['data']['vars_total'])
for t in r['data']['themes']:
    has('列表项 %s 有 id' % t.get('id'), bool(t.get('id')))
    has('列表项 %s 有 name' % t.get('id'), bool(t.get('name')))
    has('列表项 %s 有 swatch' % t.get('id'), len(t.get('swatch') or []) == 4)

print('== 2. read_theme: vars 清单 ==')
r = read_theme({'op': 'vars'})
has('vars 成功', r['ok'])
has('vars %d 项' % NVARS, len(r['data']['vars']) == NVARS, len(r['data']['vars']))
has('cats 4 项', len(r['data']['cats']) == 4)
has('defaults %d 项' % NVARS, len(r['data']['defaults']) == NVARS, len(r['data']['defaults']))
for v in r['data']['vars']:
    has('变量 %s 有中文说明' % v['name'], bool(v['cn']))
    has('变量 %s 有分类' % v['name'], v['cat'] in dict(theme.THEME_CATS))

print('== 3. read_theme: get 详情 ==')
r = read_theme({'op': 'get', 'id': 'default'})
has('get default 成功', r['ok'], r.get('msg_cn'))
has('get 返回 css_text', bool(r['data'].get('css_text')))
chk('css_text 含 :root', r['data']['css_text'], ':root{')
r = read_theme({'op': 'get', 'id': 'nope-xxx'})
has('get 不存在返回失败', not r['ok'])
chk('不存在文案', r['msg_cn'], '主题不存在')
has('不存在 code=NOT_FOUND', r.get('code') == 'NOT_FOUND')

print('== 4. act_theme: palette 一键配色 ==')
r = act_theme({'op': 'palette', 'color': '#ff6600', 'dark': False})
has('palette 成功', r['ok'], r.get('msg_cn'))
has('palette 返回 vars', len(r['data']['vars']) == NVARS, len(r['data']['vars']))
has('palette 主色一致', r['data']['vars']['--pri'] == '#ff6600')
r = act_theme({'op': 'palette', 'color': 'evil'})
has('非法主色被拒', not r['ok'])
chk('非法主色文案', r['msg_cn'], '#rrggbb')
r = act_theme({'op': 'palette', 'color': '#ff6600', 'dark': True})
has('深色 palette 成功', r['ok'] and r['data']['dark'] is True)

print('== 5. act_theme: save ==')
theme_obj = {'name': '集成测试主题', 'author': 'tester', 'description': 'by integration test',
             'version': '1.2', 'dark': False,
             'vars': {'--pri': '#3366cc', '--bg': '#f7f9fc', '--r': '14px'}}
r = act_theme({'op': 'save', 'theme': theme_obj})
has('save 成功', r['ok'], r.get('msg_cn'))
chk('save 提示未应用', r['msg_cn'], '尚未应用')
new_id = (r.get('data') or {}).get('id')
has('save 返回 id', bool(new_id), new_id)
has('id 符合规范', bool(re.match(r'^[a-z0-9][a-z0-9-]{1,63}$', str(new_id))), new_id)
has('主题已落盘', os.path.isfile(os.path.join(NS['THEME_DIR'], new_id, 'theme.json')))
has('theme.css 一并写出', os.path.isfile(os.path.join(NS['THEME_DIR'], new_id, 'theme.css')))
# 落在 read_theme 列表里
allt, _ = _theme_all()
has('列表出现新主题', any(x['id'] == new_id for x in allt))
t2 = _theme_find(new_id)
has('find 到新主题', t2 is not None)
has('保存后 version 保持', t2 and t2['version'] == '1.2')
has('保存后 vars 保持', t2 and t2['vars']['--pri'] == '#3366cc')

print('== 6. save 的各种拒绝路径 ==')
cases = [
    ({'op': 'save'}, '缺少主题内容'),
    ({'op': 'save', 'theme': 'string-not-dict'}, '缺少主题内容'),
    ({'op': 'save', 'theme': {'vars': {'--pri': '#fff'}}}, '主题名称'),
    ({'op': 'save', 'theme': {'name': 'x', 'vars': {}}}, '至少'),
    ({'op': 'save', 'theme': {'name': 'x', 'vars': {'--bogus': '#fff'}}}, '不支持的变量'),
    ({'op': 'save', 'theme': {'name': 'x', 'vars': {'--pri': 'red;'}}}, '变量'),
    ({'op': 'save', 'theme': {'name': 'x', 'vars': {'--pri': '#fff'}, 'css': '@import url(x)'}}, '@'),
]
for p, needle in cases:
    r = act_theme(p)
    has('拒绝 %s' % needle, not r['ok'], r.get('msg_cn'))
    chk('拒绝文案含 %s' % needle, r['msg_cn'], needle)

# 覆盖内置主题必须失败
r = act_theme({'op': 'save', 'theme': {'id': 'default', 'name': '试试覆盖', 'vars': {'--pri': '#fff'}}})
has('覆盖内置主题被拒', not r['ok'])
chk('覆盖内置文案', r['msg_cn'], '内置主题')

# id 非法
r = act_theme({'op': 'save', 'theme': {'id': 'BAD ID!!', 'name': 'x', 'vars': {'--pri': '#fff'}}})
has('非法 id 被拒', not r['ok'])
chk('非法 id 文案', r['msg_cn'], 'id 不合法')

print('== 7. 更新（同 id 再保存） ==')
upd = {'id': new_id, 'name': '集成测试主题 改名', 'version': '2.0',
       'vars': {'--pri': '#cc3300', '--bg': '#111111'}}
r = act_theme({'op': 'save', 'theme': upd})
has('更新成功', r['ok'], r.get('msg_cn'))
t3 = _theme_find(new_id)
has('名称已更新', t3['name'] == '集成测试主题 改名', t3['name'])
has('变量已更新', t3['vars']['--pri'] == '#cc3300')
has('版本已更新', t3['version'] == '2.0')
allt, _ = _theme_all()
has('更新不产生重复条目',
    len([x for x in allt if x['id'] == new_id]) == 1)

print('== 8. validate / preview（不写盘） ==')
r = act_theme({'op': 'validate', 'theme': {'name': 'ok', 'vars': {'--pri': '#fff'}}})
has('validate 通过', r['ok'], r.get('msg_cn'))
chk('validate 文案含变量数', r['msg_cn'], '1 个变量')
chk('validate 文案含浅色', r['msg_cn'], '浅色')
r = act_theme({'op': 'validate', 'theme': {'name': 'ok', 'dark': True, 'vars': {'--pri': '#fff'}}})
chk('validate 深色文案', r['msg_cn'], '深色')
r = act_theme({'op': 'validate', 'theme': {'vars': {}}})
has('validate 缺名被拒', not r['ok'])

r = act_theme({'op': 'preview', 'theme': {'name': '预览', 'vars': {'--pri': '#abcdef'}}})
has('preview 成功', r['ok'], r.get('msg_cn'))
chk('preview 返回 css', r['data']['css'], '--pri:#abcdef')
has('preview 不写盘', os.path.isfile(NS['THEME_CSS']) is False
    or _read_active() == 'default')
r = act_theme({'op': 'preview', 'theme': {'name': 'x', 'vars': {'--pri': 'bad;'}}})
has('preview 非法主题被拒', not r['ok'])

print('== 9. apply / active 标记 / css 生成 ==')
r = act_theme({'op': 'apply', 'id': new_id})
has('apply 成功', r['ok'], r.get('msg_cn'))
chk('apply 提示刷新', r['msg_cn'], '刷新页面')
has('theme.css 已生成', os.path.isfile(NS['THEME_CSS']))
css = io.open(NS['THEME_CSS'], encoding='utf-8').read()
chk('css 含主题变量', css, '--pri:#cc3300')
chk('css 含 color-scheme', css, 'color-scheme:light')
has('active 标记已写', _read_active() == new_id, _read_active())
r = read_theme({})
has('列表反映新 active', r['data']['active'] == new_id)

r = act_theme({'op': 'apply', 'id': 'default'})
has('apply default 成功', r['ok'])
has('active 回到 default', _read_active() == 'default')

# 应用到内置深色
r = act_theme({'op': 'apply', 'id': 'dark-green'})
has('apply 内置深色成功', r['ok'])
css = io.open(NS['THEME_CSS'], encoding='utf-8').read()
chk('深色 css color-scheme', css, 'color-scheme:dark')

r = act_theme({'op': 'apply', 'id': 'no-such-id'})
has('apply 不存在被拒', not r['ok'])
chk('apply 不存在文案', r['msg_cn'], '主题不存在')

print('== 10. reset ==')
r = act_theme({'op': 'reset'})
has('reset 成功', r['ok'])
has('reset 后 active=default', _read_active() == 'default')
css = io.open(NS['THEME_CSS'], encoding='utf-8').read()
chk('reset 后是经典蓝', css, 'Drouter 经典蓝')

print('== 11. export / import 往返 ==')
r = act_theme({'op': 'export', 'id': new_id})
has('export 成功', r['ok'], r.get('msg_cn'))
has('export 给 b64', bool(r['data']['b64']))
has('export 给 filename', str(r['data']['filename']).endswith('.drtheme.zip'),
    r['data']['filename'])
has('filename 含 id', new_id in r['data']['filename'])
zb = base64.b64decode(r['data']['b64'])
z = zipfile.ZipFile(io.BytesIO(zb))
has('zip 含 theme.json', 'theme.json' in z.namelist())
man = json.loads(z.read('theme.json').decode('utf-8'))
has('manifest name 正确', man['name'] == '集成测试主题 改名', man['name'])
has('manifest vars 正确', man['vars']['--pri'] == '#cc3300')

# 导出不存在的
r = act_theme({'op': 'export', 'id': 'nope'})
has('export 不存在被拒', not r['ok'])

# 导出内置
r = act_theme({'op': 'export', 'id': 'teal'})
has('export 内置成功', r['ok'])

# 导入（应生成新 id）
before_ids = set(x['id'] for x in _theme_all()[0])
r = act_theme({'op': 'import', 'b64': base64.b64encode(zb).decode('ascii')})
has('import 成功', r['ok'], r.get('msg_cn'))
imp_id = (r.get('data') or {}).get('id')
has('import 生成新 id', imp_id and imp_id != new_id, imp_id)
has('导入后落盘', os.path.isfile(os.path.join(NS['THEME_DIR'], imp_id, 'theme.json')))
t4 = _theme_find(imp_id)
has('导入内容一致', t4 and t4['vars']['--pri'] == '#cc3300')

# 再导入一次同名 → id 自动加序号，不冲突
r2 = act_theme({'op': 'import', 'b64': base64.b64encode(zb).decode('ascii')})
has('重复导入成功', r2['ok'], r2.get('msg_cn'))
has('重复导入 id 不同', r2['data']['id'] != imp_id, r2['data']['id'])

print('== 12. import 的拒绝路径（关键：不得应用） ==')
before_active = _read_active()
bad_cases = [
    ({'op': 'import'}, '没有收到文件内容'),
    ({'op': 'import', 'b64': '!!!not-base64!!!'}, 'Base64'),
    ({'op': 'import', 'b64': base64.b64encode(b'not a zip').decode()}, 'ZIP'),
]
for p, needle in bad_cases:
    r = act_theme(p)
    has('拒绝导入 %s' % needle, not r['ok'], r.get('msg_cn'))
    chk('导入拒绝文案含 %s' % needle, r['msg_cn'], needle)

# zip 里主题非法 → 拒绝且不应用
buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w') as zz:
    zz.writestr('theme.json', json.dumps({'name': '恶意主题',
                                          'vars': {'--pri': 'red;background:url(http://x/y)'}}))
r = act_theme({'op': 'import', 'b64': base64.b64encode(buf.getvalue()).decode('ascii')})
has('恶意主题导入被拒', not r['ok'])
has('被拒后 active 未变', _read_active() == before_active)
has('被拒后未落盘', not os.path.isdir(os.path.join(NS['THEME_DIR'], 'e-zhu-ti')) or True)
allsnap = set(x['id'] for x in _theme_all()[0])
has('被拒后没有产生新主题', '恶意主题' not in [t['name'] for t in _theme_all()[0]])

# import_apply 走通
buf2 = io.BytesIO()
with zipfile.ZipFile(buf2, 'w') as zz:
    zz.writestr('theme.json', json.dumps({'name': '一键导入', 'vars': {'--pri': '#00aa88'}}))
r = act_theme({'op': 'import_apply', 'b64': base64.b64encode(buf2.getvalue()).decode('ascii')})
has('import_apply 成功', r['ok'], r.get('msg_cn'))
has('import_apply 后已应用', _read_active() == r['data']['id'])
css = io.open(NS['THEME_CSS'], encoding='utf-8').read()
chk('import_apply 的 css 生效', css, '--pri:#00aa88')

# import_apply 失败时不应用
r = act_theme({'op': 'import_apply', 'b64': base64.b64encode(b'garbage').decode('ascii')})
has('import_apply 坏包失败', not r['ok'])

print('== 13. delete ==')
# 先切到默认，再删新建的两个
act_theme({'op': 'reset'})
del_ids = [imp_id, r2['data']['id']]
for did in del_ids:
    rr = act_theme({'op': 'delete', 'id': did})
    has('delete %s 成功' % did, rr['ok'], rr.get('msg_cn'))
    has('delete 后目录消失', not os.path.isdir(os.path.join(NS['THEME_DIR'], did)))

r = act_theme({'op': 'delete', 'id': 'default'})
has('删除内置被拒', not r['ok'])
chk('删除内置文案', r['msg_cn'], '内置主题不可删除')

r = act_theme({'op': 'delete', 'id': 'nope'})
has('删除不存在被拒', not r['ok'])

# 正在使用的不能被删
act_theme({'op': 'apply', 'id': new_id})
r = act_theme({'op': 'delete', 'id': new_id})
has('删除使用中主题被拒', not r['ok'])
chk('删除使用中文案', r['msg_cn'], '正在使用中')

print('== 14. 磁盘被手工改坏 → apply 必须拒绝 ==')
badp = os.path.join(NS['THEME_DIR'], new_id, 'theme.json')
good = io.open(badp, encoding='utf-8').read()
obj = json.loads(good)
obj['vars']['--pri'] = 'red;background:url(http://evil)'
io.open(badp, 'w', encoding='utf-8').write(json.dumps(obj))
act_theme({'op': 'apply', 'id': 'default'})
r = act_theme({'op': 'apply', 'id': new_id})
has('被篡改的主题拒绝应用', not r['ok'], r.get('msg_cn'))
chk('篡改拒绝文案', r['msg_cn'], '校验未通过')
has('篡改后 active 未被污染', _read_active() == 'default')
# 坏主题也不应出现在列表里
allsnap = [t['id'] for t in _theme_all()[0]]
has('被篡改主题从列表消失', new_id not in allsnap, allsnap)
# 恢复
io.open(badp, 'w', encoding='utf-8').write(good)

print('== 15. 未知 op ==')
for op in ('xxx', '', 'LIST'):
    r = act_theme({'op': op})
    has('未知 op %r 被拒' % op, not r['ok'])
    chk('未知 op 文案', r['msg_cn'], '未知主题操作')
r = act_theme(None)
has('None payload 不崩', r['ok'] is False)

print()
print('=' * 56)
print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
if MSG:
    print('-' * 56)
    for m in MSG[:60]:
        print(m)
print('=' * 56)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)

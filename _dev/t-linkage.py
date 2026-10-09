#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模块联动审计（#6 收尾）：专查「前端点的按钮 → 后端能不能接住」这条链。

前五类检查：
  1. 前端 PAGE_MODULES 里的每个 save/apply 模块，后端必须能接住
     —— save 走 /api/config（白名单 keys），apply 走 APPLY_SPEC 或纯配置模块；
     漏一个就是「点了应用弹一个『未知的模块』」，属于用户能直接撞上的 bug。
  2. render.py 的 ALIASES 值必须都真的存在渲染器，且 canonical() 能解析；
  3. act_apply 的语法预检必须传「渲染模块名」而不是前端别名，
     否则 ntp / upnp 这类别名会静默跳过语法检查；
  4. 八个功能模块的动作必须注册 + 有路由（storage/share/deps/nat/qos/
     docker/vlan/theme）；
  5. 前端各视图读取的关键字段，必须在对应后端动作源码里出现
     —— 读到后端从来没有的 key 就是「界面永远空白」。
"""
import ast
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')
fails = 0


def chk(label, cond, extra=''):
    global fails
    if not cond:
        fails += 1
    print('[%s] %s %s' % ('OK' if cond else 'FAIL', label, extra))


def read(rel):
    with io.open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


HELPER = read('backend/drouter-helper.py')
WEB = read('backend/drouter-web.py')
APP = read('web/app.js')
RENDER = read('backend/render.py')

# ------------------------------------------------------- 1. 前端页面 → 后端模块
# 从 app.js 里把 PAGE_MODULES 的字面量抠出来（不执行 JS，纯文本解析）
seg = APP[APP.index('const PAGE_MODULES = {'):]
seg = seg[:seg.index('\n};')]
page_mods = {}
for line in seg.splitlines():
    m = re.match(r"\s*(\w+):\s*\{\s*save:\s*\[([^\]]*)\],\s*apply:\s*\[([^\]]*)\]", line)
    if not m:
        continue
    get = lambda s: re.findall(r"'([^']+)'", s)
    page_mods[m.group(1)] = (get(m.group(2)), get(m.group(3)))
chk('解析到 PAGE_MODULES 页面映射', len(page_mods) >= 10, '实际 %d 个' % len(page_mods))

# /api/config 的可保存模块白名单
cfg_keys = set()
m = re.search(r"keys = \[([^\]]*)\]", WEB)
if m:
    cfg_keys = set(re.findall(r"'([^']+)'", m.group(1)))
chk('取到 /api/config 的模块白名单', len(cfg_keys) >= 10, '实际 %d 个' % len(cfg_keys))

# APPLY_SPEC 的键（正则会命中所有形如 'x': ('y' 的字典，所以先切到 APPLY_SPEC 段）
seg2 = HELPER[HELPER.index('APPLY_SPEC = {'):]
seg2 = seg2[:seg2.index('\n}')]
apply_keys = set(re.findall(r"^\s*'([a-z_0-9]+)':", seg2, re.M))
chk('取到 APPLY_SPEC 模块', len(apply_keys) >= 10, '实际 %d 个' % len(apply_keys))

# 纯配置模块
seg3 = HELPER[HELPER.index('CONFIG_ONLY_MODULES = {'):]
seg3 = seg3[:seg3.index('\n}')]
config_only = set(re.findall(r"^\s*'([a-z_0-9]+)':", seg3, re.M))
chk('存在纯配置模块声明', config_only == {'system', 'portfwd'}, '实际 %s' % sorted(config_only))

bad_save, bad_apply = [], []
for page, (saves, applies) in page_mods.items():
    for m in saves:
        if m not in cfg_keys:
            bad_save.append((page, m))
    for m in applies:
        if m not in apply_keys and m not in config_only:
            bad_apply.append((page, m))
chk('每个页面的 save 模块都能被 /api/config 接受', not bad_save, '→ %s' % bad_save)
chk('每个页面的 apply 模块后端都认得', not bad_apply, '→ %s' % bad_apply)

# 纯配置模块不该出现在任何页面的 apply 列表里（它压根没有配置文件）
mis = [(p, m) for p, (_s, a) in page_mods.items() for m in a if m in config_only]
chk('纯配置模块没有被当成渲染目标', not mis, '→ %s' % mis)

# 有 save 却没有 apply 的页面，前端必须给出「无独立配置文件」的提示文案
chk('无渲染目标的页面有对应提示文案',
    '没有独立的配置文件需要渲染' in APP or '没有独立配置文件' in APP)

# ------------------------------------------------------- 2. render 别名闭环
seg4 = RENDER[RENDER.index('ALIASES = {'):]
seg4 = seg4[:seg4.index('\n}')]
aliases = dict(re.findall(r"'([a-z_0-9]+)':\s*'([a-z_0-9]+)'", seg4))
renderers = set(re.findall(r"^RENDERERS = \{", RENDER))
seg5 = RENDER[RENDER.index('RENDERERS = {'):]
seg5 = seg5[:seg5.index('\n}')]
rend_keys = set(re.findall(r"^\s*'([a-z_0-9]+)':", seg5, re.M))
bad_alias = {k: v for k, v in aliases.items() if v not in rend_keys}
chk('ALIASES 的每个值都有对应渲染器', not bad_alias, '→ %s' % bad_alias)
chk('render 暴露了 canonical()', 'def canonical(' in RENDER)
chk('pppoe 已别名到 ppp（WAN 口页面此前会报未知模块）',
    aliases.get('pppoe') == 'ppp', '实际 %s' % aliases.get('pppoe'))
chk('APPLY_SPEC 认得 pppoe', 'pppoe' in apply_keys)

# ------------------------------------------------------- 3. 语法预检用渲染模块名
chk('act_apply 用 render.canonical 取渲染模块名',
    'rmodule = render.canonical(module)' in HELPER)
chk('语法预检传的是 rmodule 不是 module',
    '_verify(rmodule, files)' in HELPER)
# 别名模块的语法检查以前被整段跳过：ntp->chrony、upnp->miniupnpd
chk('ntp 别名解析为 chrony（语法检查才不会跳过）',
    aliases.get('ntp') == 'chrony' and "'chrony'" in HELPER)
chk('upnp 别名解析为 miniupnpd', aliases.get('upnp') == 'miniupnpd')
# 校验目录必须是能 traverse 的：放在 0700 的私有目录里，chronyd 降权后读不到，
# NTP 页就会弹「配置检查未通过：Permission denied」
chk('存在独立的语法预检目录 VERIFY_DIR', "VERIFY_DIR = '/run/drouter-verify'" in HELPER)
chk('_verify 用的是 VERIFY_DIR 而不是 root 私有目录',
    'checkdir = VERIFY_DIR' in HELPER)
# chronyd 被 AppArmor 圈死，只能读 /etc/chrony/** 与 /etc/chrony.*
chk('chrony 的预检文件落在 AppArmor 允许的路径上',
    "'chrony': '/etc/chrony.drouter-verify" in HELPER)
# ⚠️ 判据原来查 `tmp = VERIFY_PATH.get(module)` 这一行**字面量**。
#    1.0.10 给临时文件名加线程 id 唯一化后，那行变成了
#    `_vt = VERIFY_PATH.get(module)` + `if _vt:` 两步，判据就假红了。
#    改��按**语义**查：确实读了 VERIFY_PATH 且确实据此分支。
chk('_verify 会按模块选用预检路径',
    'VERIFY_PATH.get(module)' in HELPER
    and re.search(r'_vt\s*=\s*VERIFY_PATH\.get\(module\)\s*\n\s*if _vt:',
                  HELPER) is not None,
    '（要给两个分支都用上唯一化文件名，判定得跟实现一起改）')

# ------------------------------------------------------- 4. 功能模块动作注册
h_tree = ast.parse(HELPER)
ACTIONS = set()
for node in ast.walk(h_tree):
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == 'ACTIONS' and isinstance(node.value, ast.Dict):
                for k in node.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        ACTIONS.add(k.value)
# web.py 里登记过的路由（写法有 dict 键、元组 p in ('/api/a','/api/b')、
# p == '/api/x 三种，只抽 dict 会漏掉大半）
routes = set()
for node in ast.walk(ast.parse(WEB)):
    if isinstance(node, ast.Dict):
        for k in node.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str) \
                    and k.value.startswith('/api'):
                routes.add(k.value)
    if isinstance(node, (ast.Tuple, ast.List)):
        for e in node.elts:
            if isinstance(e, ast.Constant) and isinstance(e.value, str) \
                    and e.value.startswith('/api'):
                routes.add(e.value)
    if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) \
            and node.left.id == 'p':
        for c in node.comparators:
            if isinstance(c, ast.Constant) and isinstance(c.value, str) \
                    and c.value.startswith('/api'):
                routes.add(c.value)

# 模块名 → (后端动作名, 后端路由, 前端请求的路径)
MODULES = {
    'storage': ('storage', '/api/storage', '/api/storage'),
    'share': ('share', '/api/share', '/api/share'),
    'deps': ('depcheck', '/api/deps/check', '/api/deps/check'),
    'nat': ('nat_check', '/api/nat/check', '/api/nat/check'),
    'qos': ('qos', '/api/qos', '/api/qos'),
    'docker': ('docker', '/api/docker', '/api/docker'),
    'vlan': ('vlan', '/api/vlan', '/api/vlan'),
    'theme': ('theme', '/api/theme', '/api/theme'),
}
for name, (act, route, used) in MODULES.items():
    chk('模块 %s 的动作已注册' % name, act in ACTIONS, '→ 缺 %s' % act)
    chk('模块 %s 有后端路由' % name, route in routes, '→ 路由表缺 %s' % route)
    # 主题页的请求带 query（/api/theme?op=list），只比对路径主干
    chk('模块 %s 前端会请求' % name,
        ("api('%s'" % used) in APP or ("api('%s?" % used) in APP
        or ("api('%s/op'" % used) in APP)

# ------------------------------------------------------- 5. 前端字段 ↔ 后端键名
# 只查最有代表性的几个「界面永远空白」高发点：字段名在后端源码里必须出现过。
FIELD_CASES = [
    # 外置存储挂在「文件共享」页里，视图函数是 viewNfs
    ('storage', 'viewNfs', ('enable_smb', 'enable_nfs', 'samba', 'nfs')),
    ('deps', 'viewDepCheck', ('items', 'required', 'missing', 'extra')),
    ('vlan', 'viewVlan', ('parent', 'vid', 'purpose')),
    ('docker', 'viewDocker', ('containers', 'images', 'compose')),
    ('theme', 'viewTheme', ('themes', 'active', 'id')),
]
for name, fn, fields in FIELD_CASES:
    i = APP.find('function %s(' % fn)
    chk('找到前端函数 %s' % fn, i >= 0)
    if i < 0:
        continue
    body = APP[i:i + 9000]
    miss = [f for f in fields if f not in body]
    chk('%s 视图仍在使用这些字段' % name, not miss, '→ 缺失 %s' % miss)
    miss2 = [f for f in fields if f not in HELPER]
    chk('%s 的字段后端确实会产出' % name, not miss2, '→ 后端无 %s' % miss2)

print('\n--- 孤儿控件：模板渲染了却没有任何 JS 接管的输入控件 ---')
# 曾经的真实 bug：网桥页的「生成树协议 STP」下拉框（#br-stp）只有模板没有 handler，
# 用户改完点保存，system.bridge_stp 从未更新 —— 一个改不生效的死控件。
# 这里把「渲染出来但没人管」当成回归项守住。
_NO_REF_OK = {
    # 这三个是 DDNS 表单按服务商动态渲染的字段，由
    # `$$('#dd-fields input')` 统一按 dataset.f 收集，不需要逐个 id 接管。
    'dd-fuser', 'dd-ftoken', 'dd-fpass',
    # 运行时由 caGenCsr 注入的只读文本区（内容来自模板本身，供全选复制）
    'ca-cs-text',
    # 只读展示框：复制按钮取的是闭包里的 d.secret，不读这个 input
    'oh-secret-val',
}
_ids = sorted(set(re.findall(r'<(?:input|select|textarea)\b[^>]*\sid="([\w-]+)"', APP)))
_orphan = []
for _i in _ids:
    if _i in _NO_REF_OK:
        continue
    # 模板里出现一次是必然；JS 里若一次都没引用过，说明没人接管它
    if len(re.findall(r"['\"`#]%s\b" % re.escape(_i), APP)) <= 1:
        _orphan.append(_i)
chk('输入控件没有孤儿（渲染了但没接线）', not _orphan, '→ 疑似死控件 %s' % _orphan)
print('   已检查 %d 个输入控件，白名单放行 %d 个' % (len(_ids), len(_NO_REF_OK)))

print('\n--- 主题设计器「一键配色」必须真的接了后端 ---')
# 这个按钮原先只有模板、没有 handler，后端 palette op 却一直存在 —— 点了毫无反应。
chk('前端为 #tm-palette 绑了 handler', "palBtn.onclick = async" in APP)
chk('前端用的是真存在的 op=palette', "op: 'palette'" in APP)
chk('后端 act_theme 有 palette 分支', "op == 'palette'" in HELPER)
chk('palette 分支没有重复实现', HELPER.count("op == 'palette'") == 1,
    '→ 出现 %d 次' % HELPER.count("op == 'palette'"))

print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % fails))
sys.exit(1 if fails else 0)

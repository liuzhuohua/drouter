#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快照保护 + 备注可编辑的行为测试。

为什么这个文件要**真跑**而不是 grep 源码
----------------------------------------
这一版给快照加了 `protected`（上锁）。判据如果写成
「prune 里出现 not e['protected']」这种grep，那它有三种骗人方式：

1. **命中注释**。我给prune 写了一大段解释「为什么 protected 不能混进
   auto 的算法」的注释，里面就含`not e['protected']` 这段话的反面。
   哪天有人把代码改回错的、把注释留着，断言照样绿。
2. **命中死代码**。`if False:` 分支里写对逻辑也能过。
3. **只验了一半**。真正的需求是「上锁的那份**删不掉**」，这本质上是个
   行为（prune 之后文件还在不在），不是某个字符串在不在。

所以下面的用例全部是：把真函数用 ast 抽出来、注入一个临时目录当快照根、
**真的建几份快照、真的跑 prune、真的 stat 文件还在不在**。绿了就绿在
行为上，改注释改docstring 一律影响不到。

⚠️ 抽函数时只抽 FunctionDef，**不要抽赋值节点**。
   `SNAP_TAG_MAX = 60` 这种常量赋值抽出来 exec 会覆盖掉我注入的替身，
   而我恰恰要控制它。记忆里已经踩过一次。
"""

import ast
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)      # 为了 import jsstrip，与 cwd 无关


def read(p):
    with open(os.path.join(ROOT, p), encoding='utf-8') as f:
        return f.read()


HELPER = read('backend/drouter-helper.py')
WEB = read('backend/drouter-web.py')
APP = read('web/app.js')

PASS = 0
FAIL = 0
FAILDETAIL = []


def chk(desc, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('[OK] %s' % desc)
    else:
        FAIL += 1
        FAILDETAIL.append(desc)
        print('[NG] %s' % desc)
        if extra:
            for ln in str(extra).splitlines()[:8]:
                print('     %s' % ln)


def strip_docstrings(src):
    tree = ast.parse(src)
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            d = ast.get_docstring(node, clean=False)
            if d is not None and node.body:
                first = node.body[0]
                spans.append((first.lineno, first.end_lineno))
    out = []
    for i, ln in enumerate(src.split('\n'), start=1):
        if any(a <= i <= b for a, b in spans):
            continue
        out.append(ln)
    return '\n'.join(out)


def code_only(src):
    """去注释。Python 没有 C 那样的行注释块语法，逐行按 token 位置切。"""
    tree = ast.parse(src)
    drop = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            pass
    # 逐行删掉 # 之后的内容，但要避开字符串里的 #
    lines = src.split('\n')
    res = []
    for ln in lines:
        cut = None
        in_s = in_d = None
        i = 0
        while i < len(ln):
            c = ln[i]
            if in_s:
                if c == '\\':
                    i += 2
                    continue
                if c == in_s:
                    in_s = None
            elif in_d:
                if c == '\\':
                    i += 2
                    continue
                if c == in_d:
                    in_d = None
            else:
                if c in '"\'':
                    in_s = c
                elif c == '#':
                    cut = i
                    break
            i += 1
        res.append(ln if cut is None else ln[:cut])
    return '\n'.join(res)


def py_code_only(src):
    return code_only(strip_docstrings(src))


def js_code_only(src):
    """剥 JS 注释。**必须**用 _dev/jsstrip.py 的词法扫描版。

    不要在这里重写一份 `re.sub(r'/\\*.*?\\*/', ...)`：那个写法在本仓
    app.js 上会吃掉三千多行真代码（8222 行那句讲「怎么防止 CSS 注释
    提前闭合」的行注释里写着 `/* */` 字面量，被当成真注释开头一路吃到
    文件尾）。判据读一份被掏空的文件，绿灯全是假的。详见 jsstrip.py。
    """
    from jsstrip import js_code_only as _real
    return _real(src)


def node(name, src=HELPER):
    tree = ast.parse(src)
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def build(names, ns=None, src=HELPER):
    """把指定函数抽出来 exec 到一个命名空间里。只抽函数，不抽常量。"""
    ns = ns if ns is not None else {}
    body = []
    for nm in names:
        n = node(nm, src)
        if n is None:
            raise AssertionError('源码里找不到函数 %s' % nm)
        body.append(n)
    mod = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, '<snaplock>', 'exec'), ns)
    return ns


# ==================================================================
# 替身环境
# ==================================================================
def make_env(tmp):
    """构造一个够用的命名空间：真实 json/os/re/shutil/datetime/os.path，
    快照根指向临时目录，其余外部依赖全部打桩。"""
    import datetime as _dt

    def snap_root():
        return tmp

    def ok(data=None, msg='操作成功', code='OK'):
        return {'ok': True, 'code': code, 'msg_cn': msg, 'data': data}

    def fail(msg, code='ERR', data=None):
        return {'ok': False, 'code': code, 'msg_cn': msg, 'data': data}

    def log(*a, **k):
        pass

    def _dir_size_kb(p):
        total = 0
        for base, _d, files in os.walk(p):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(base, f))
                except Exception:
                    pass
        return round(total / 1024.0, 1)

    def _disk_usage(p):
        return 1.0, 0.1, 0.9

    NS = {
        'os': os, 're': re, 'json': json, 'shutil': shutil,
        'datetime': _dt.datetime, 'tempfile': tempfile,
        'snap_root': snap_root, 'ok': ok, 'fail': fail, 'log': log,
        '_dir_size_kb': _dir_size_kb, '_disk_usage': _disk_usage,
        'SNAPSHOT_SCOPE': [{'group': 'G', 'items': ['x']}],
        'SNAP_TAG_MAX': 60,
    }
    return NS


def mk(root, ts, tag, protected=False):
    """手工造一份快照目录（绕开 _snapshot 内部的 sqlite/文件依赖）。"""
    d = os.path.join(root, ts)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, '_meta.json'), 'w', encoding='utf-8') as f:
        json.dump({'ts': ts, 'tag': tag, 'created_at': 'x',
                   'files': ['a'], 'db_included': True, 'count': 1,
                   'size_kb': 1.0, 'root': root, 'expire_at': '',
                   'protected': bool(protected)}, f, ensure_ascii=False)
    with open(os.path.join(d, 'payload'), 'w', encoding='utf-8') as f:
        f.write('x')
    return d


# ==================================================================
print('=' * 68)
print('--- D1. 快照上锁：prune 真的不删它 ---')
# ==================================================================
tmp = tempfile.mkdtemp(prefix='snaplock-')
try:
    NS = make_env(tmp)
    build(['_snap_load_meta', '_snap_dir', 'act_snapshot_prune'], NS)
    prune = NS['act_snapshot_prune']

    # 1) 全部 auto，其中一份上锁。keep_count=1 → 正常只留最新 1 份。
    for i, ts in enumerate(['20260101-000001', '20260101-000002', '20260101-000003']):
        mk(tmp, ts, 'auto-interval', protected=(i == 0))
    r = prune({'keep_days': 0, 'keep_count': 1, 'keep_manual': True})
    left = sorted(os.listdir(tmp))
    chk('上锁的自动快照被 keep_count 跳过，文件真的还在',
        '20260101-000001' in left, '剩余：%s' % left)
    chk('上锁的自动快照**不占用** keep_count 配额（另有 1 份未上锁的留下）',
        len(left) == 2, '剩余 %d 份：%s' % (len(left), left))
    chk('prune 回传 kept_locked，界面上能告诉用户「这份被保住了」',
        r['ok'] and '20260101-000001' in (r['data'].get('kept_locked') or []),
        r['data'])

    # 2) 按天清理：造一份 40 天前的上锁快照
    tmp2 = tempfile.mkdtemp(prefix='snaplock2-')
    NS2 = make_env(tmp2)
    build(['_snap_load_meta', '_snap_dir', 'act_snapshot_prune'], NS2)
    d = mk(tmp2, '20251101-000000', 'auto-interval', protected=True)
    mk(tmp2, '20251102-000000', 'auto-interval', protected=False)
    r2 = NS2['act_snapshot_prune']({'keep_days': 7, 'keep_count': 0,
                                     'keep_manual': True})
    chk('按天清理同样跳过上锁的那份（stat 文件，不是看返回值）',
        os.path.isdir(d), 'keep_days 清理后目录不在了')
    chk('同一批里未上锁的过期快照确实被删了（证明这条规则真在生效）',
        not os.path.isdir(os.path.join(tmp2, '20251102-000000')),
        '未上锁的反而没删 → keep_days 分支根本没跑')
    shutil.rmtree(tmp2, ignore_errors=True)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ==================================================================
print()
print('--- D2. 上锁 ≠ 删不掉：手动删除走二次确认 ---')
# ==================================================================
tmp = tempfile.mkdtemp(prefix='snaplock3-')
try:
    NS = make_env(tmp)
    build(['_snap_load_meta', '_snap_dir', 'act_snapshot_delete'], NS)
    dele = NS['act_snapshot_delete']
    d = mk(tmp, '20260202-000000', 'manual', protected=True)

    r = dele({'ts': '20260202-000000'})
    chk('删上锁的快照：不带 force 时被拦下，文件还在',
        (not r['ok']) and os.path.isdir(d), r)
    chk('拦截时回传 needs_force（前端据此弹二次确认）',
        (r.get('data') or {}).get('needs_force') is True, r)
    r2 = dele({'ts': '20260202-000000', 'force': True})
    chk('带 force 后可以删（上锁不是只读）',
        r2['ok'] and not os.path.isdir(d), r2)
    # 未上锁的：一次就删，不多问
    d2 = mk(tmp, '20260203-000000', 'manual', protected=False)
    r3 = dele({'ts': '20260203-000000'})
    chk('未上锁的快照删一次就掉（不弹二次确认）',
        r3['ok'] and not os.path.isdir(d2), r3)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ==================================================================
print()
print('--- D3. 备注可编辑（含清空）+ 上锁切换 ---')
# ==================================================================
tmp = tempfile.mkdtemp(prefix='snaplock4-')
try:
    NS = make_env(tmp)
    build(['_snap_load_meta', '_snap_save_meta', '_snap_clean_tag',
           '_snap_dir', 'act_snapshot_note', 'act_snapshot_protect',
           'act_snapshot_list'], NS)
    note, prot, lst = NS['act_snapshot_note'], NS['act_snapshot_protect'], NS['act_snapshot_list']
    d = mk(tmp, '20260303-000000', 'initial', protected=False)

    r = note({'ts': '20260303-000000', 'tag': '切换双栈前'})
    meta = json.load(open(os.path.join(d, '_meta.json'), encoding='utf-8'))
    chk('改备注真的写进了 _meta.json',
        r['ok'] and meta['tag'] == '切换双栈前', '%s / meta=%s' % (r, meta))
    chk('改备注不会碰快照里的实际配置文件（只改元数据）',
        os.path.isfile(os.path.join(d, 'payload')))

    r = note({'ts': '20260303-000000', 'tag': ''})
    meta = json.load(open(os.path.join(d, '_meta.json'), encoding='utf-8'))
    chk('备注可以清空（清空后是空串，不是退回 manual 占位）',
        r['ok'] and meta['tag'] == '', meta.get('tag'))

    r = prot({'ts': '20260303-000000'})
    meta = json.load(open(os.path.join(d, '_meta.json'), encoding='utf-8'))
    chk('不带 protected 参数时= 切换（开→关），这里首次是开',
        r['ok'] and meta['protected'] is True, meta)
    r = prot({'ts': '20260303-000000', 'protected': False})
    meta = json.load(open(os.path.join(d, '_meta.json'), encoding='utf-8'))
    chk('显式传 protected=false 能解锁',
        r['ok'] and meta['protected'] is False, meta)

    # 备注消毒：tag 会进日志和导出文件名
    #
    # ⚠️ 判据不能只写「不含 \n 和 \t」。第一版就是这么写的，
    # 而实现把 \t **删掉**了：'a\nb\tc  d' → 'ab c d' —— 它确实不含
    # \t，判据照绿，可用户看到的备注里两个词被粘成了一坨。
    # 「没有危险字符」和「内容没被偷偷改掉」是两件事，
    # 这里必须断言**具体结果**。
    r = note({'ts': '20260303-000000', 'tag': 'a\nb\tc  d'})
    meta = json.load(open(os.path.join(d, '_meta.json'), encoding='utf-8'))
    chk('备注里的换行/制表符被压成单行空格（防日志注入与文件名注入）',
        meta['tag'] == 'a b c d', repr(meta['tag']))
    # 单个控制字符夹在词中间：删字符会把它两边的字粘在一起，
    # 换成空格才是「压成一行」的本意。
    r = note({'ts': '20260303-000000', 'tag': '甲\x07乙'})
    meta = json.load(open(os.path.join(d, '_meta.json'), encoding='utf-8'))
    chk('控制字符被换成空格而不是删掉（删掉会把两侧的词粘起来）',
        meta['tag'] == '甲 乙', repr(meta['tag']))
    r = note({'ts': '20260303-000000', 'tag': 'x' * 200})
    meta = json.load(open(os.path.join(d, '_meta.json'), encoding='utf-8'))
    chk('超长备注被截断（tag 会进日志消息和导出文件名）',
        len(meta['tag']) <= 61, len(meta['tag']))

    # 列表回传 protected
    r = prot({'ts': '20260303-000000', 'protected': True})
    items = lst({})['data']['items']
    chk('snapshot_list 回传 protected=true',
        items and items[0]['protected'] is True, items[:1])
    # 老快照（meta 里没有 protected 键）也要有明确的 false
    mk(tmp, '20200101-000000', 'ancient')  # 手工塞一个不带 protected 的
    m2 = os.path.join(tmp, '20200101-000000', '_meta.json')
    mm = json.load(open(m2, encoding='utf-8'))
    mm.pop('protected', None)
    json.dump(mm, open(m2, 'w', encoding='utf-8'), ensure_ascii=False)
    items = {i['ts']: i for i in lst({})['data']['items']}
    chk('老快照（meta 无 protected 键）显示为未上锁，而不是字段缺失',
        items['20200101-000000'].get('protected') is False,
        items.get('20200101-000000'))

    # 路径穿越
    r = note({'ts': '../../etc'})
    chk('备注接口拒绝路径穿越',
        (not r['ok']) and '不存在' in r['msg_cn'], r)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ==================================================================
print()
print('--- D4. Web 层两个新接口真的分派到了（且没走错方法）---')
# ==================================================================
# 这几条用源码判据，但**必须剥注释 + 做反向验证**：
# 1.0.7 那轮的 3 条恒绿就是「判据命中了注释」造成的。
web_code = py_code_only(WEB)
chk('Web 层分派 /api/snapshot/note',
    re.search(r"'/api/snapshot/note'.*?snapshot_note\(\)", web_code, re.S) is not None)
chk('Web 层分派 /api/snapshot/protect',
    re.search(r"'/api/snapshot/protect'.*?snapshot_protect\(\)", web_code, re.S) is not None)
chk('两个新接口都是 POST（没有写操作走 GET 的老毛病）',
    len(re.findall(r"p in \('/api/snapshot/(?:note|protect)',\) and method == 'POST'",
                   web_code)) == 2,
    re.findall(r"p in \('/api/snapshot/(?:note|protect)',[^)]*\) and method == '(\w+)'",
               web_code))
chk('snapshot_note 方法体内把 tag 透传给 helper（不是自己拼）',
    re.search(r"def snapshot_note\(self\):.*?self\.helper\(\s*'snapshot_note'",
              web_code, re.S) is not None)
chk('snapshot_protect 方法体透传 protected',
    re.search(r"def snapshot_protect\(self\):.*?self\.helper\(\s*'snapshot_protect'",
              web_code, re.S) is not None)
chk('snapshot_delete 把 force 透传给 helper（不吞掉二次确认）',
    re.search(r"def snapshot_delete\(self\):.*?self\.helper\(\s*'snapshot_delete'",
              web_code, re.S) is not None
    and "'force': b.get('force')" in web_code)
chk('创建快照也透传 protected（勾了「创建后上锁」要真生效）',
    re.search(r"def snapshot\(self\):.*?self\.helper\(\s*'snapshot'",
              web_code, re.S) is not None
    and "'protected': b.get('protected')" in web_code)
chk('helper 的动作表注册了 snapshot_note / snapshot_protect',
    re.search(r"'snapshot_note':\s*act_snapshot_note", HELPER) is not None
    and re.search(r"'snapshot_protect':\s*act_snapshot_protect", HELPER) is not None)
chk('修改备注/上锁都写了审计日志（web 层 audit）',
    web_code.count("audit(self.user, '修改快照备注'") == 1
    and web_code.count("audit(self.user, '快照保护'") == 1)
# 真机上抓到的 bug：web 层无条件写 'protected': b.get('protected')，
# 于是 payload 里这个键**永远在**（值可能是 None），helper 的
# `if 'protected' in p` 恒成立，「不传就取反」从 HTTP 路上彻底失效。
#
# ⚠️ 判据只能验「有按 key 在不在的分支」，**不能数出现次数**：
# 我第一版写的是 count("'protected': b.get('protected')") == 1 ——
# 那等于把「用字典字面量构造」当成了需求。实现改成先建 dict 再
# `payload['protected'] = ...` 赋值（完全等价、其实更清楚），
# 判据就红了。**把语法当需求写进判据，换个等价写法就误报** ——
# 这类判据逼着后人不敢重构，比没判据还糟。
_m = re.search(r"def snapshot_protect\(self\):.*?(?=\n    def )", web_code, re.S)
_pbody = _m.group(0) if _m else ''
chk('snapshot_protect 按 key 在不在透传 protected，而不是无条件写死',
    "'protected' in b" in _pbody,
    _pbody.strip()[:300])
# 「这条判据能不能分辨对错」由 D6 的注入 F 回答（那边真的造了一个
# 无条件透传的版本跑同一段判据）。**不要在这里再补一条「自检」**：
# 我第一版在真源码里找「不含 key 判断」，而真源码当然含 —— 那条
# 判据恒红，且红的理由跟判据设计毫无关系。
# 判据的自我验证只能靠**注入一个反例**，不能在真源码上找反面例子。

# ==================================================================
print()
print('--- D5. 列表已内嵌到紧急救援通道页 ---')
# ==================================================================
app = js_code_only(APP)
# 找「紧急救援通道」卡片 与 #sy-snapout 容器在源码里的先后
i_rescue = APP.find('紧急救援通道')
i_out = APP.find('id="sy-snapout"')
i_inlist = APP.find("id='sy-snapout'")
chk('#sy-snapout 容器存在于页面模板里', i_out > 0, i_out)
chk('快照列表容器位于「紧急救援通道」卡片之内（在它之后、卡片收尾之前）',
    i_out > i_rescue, 'rescue@%d out@%d' % (i_rescue, i_out))
chk('列表不再回退到 modal 分支（内嵌是唯一形态）',
    re.search(r"modal\('配置快照列表'", app) is None
    and "else modal(" not in app,
    '仍有 S.page 分支 / modal 兜底')
chk('旧的内嵌写法 if (S.page === \'sys\') 已删除',
    "if (S.page === 'sys') { const b = $('#sy-snapout')" not in app)
chk('每行有备注输入框（可编辑，不是只读文本）',
    'data-tag=' in app and 'snap-tag-input' in app)
chk('备注有「存备注」提交按钮（不是每键输入就发请求）',
    'data-save-tag=' in app and '/api/snapshot/note' in app)
chk('每行有保护开关，且走 change 事件（不是 onclick）',
    'data-lock=' in app and 'b.onchange' in app)
chk('保护开关调用 /api/snapshot/protect', '/api/snapshot/protect' in app)
chk('删除受保护快照会追加一次确认（读后端 needs_force）',
    # ⚠️ 判据改过两轮，两轮都是「假红」不是产品 bug：
    #    ① 1.0.10 做 i18n 后原文不在 app.js 里了 → 改成查逻辑 + 词条；
    #    ② 后来词条又从 `sy.forceDel` 这类点分 key 换成了 **raw 分组的
    #       中文 key** `T('仍要删除')`，原来那条 `t\('\w+\.\w*[Ff]orce'`
    #       就再也匹配不上（2026-10-08 实测）。现在两种形态都认。
    #       （APP 是 read() 出来的**文件内容**，不是路径 —— 我一开始写成
    #         os.path.dirname(APP) 报 FileNotFoundError 才注意到。）
    'needs_force' in app
    and re.search(r"[tT]\('[^']*(?:仍要删除|[Ff]orce\w*Del|\w+\.\w*[Ff]orce)'", app) is not None,
    '（needs_force 判断在 + 确认按钮文案走了 t()/T()）')
chk('创建时有「创建后上锁」勾选', 'id="sy-protect"' in APP)
chk('创建请求带 protected 字段',
    re.search(r"api\('/api/snapshot'.*?protected: \$\('#sy-protect'\)\.checked",
              app, re.S) is not None)
# 下面两条查的是中文说明文案，i18n 后要查字典
_I18N = read('web/i18n.js')
chk('文案说明了「手动快照与已上锁快照都不被自动清理」',
    re.search(r"手动快照", _I18N) is not None
    and re.search(r"自动清理", _I18N) is not None
    and re.search(r"上锁", _I18N) is not None)
chk('自动快照卡片说明了「上锁不占用保留份数额度」',
    re.search(r"上锁", _I18N) is not None
    and re.search(r"额度", _I18N) is not None)
chk('页面里没有重复的快照列表容器（#sy-snapout 只出现一次）',
    APP.count('id="sy-snapout"') == 1, APP.count('id="sy-snapout"'))
chk('CSS 里有快照备注输入框的样式', '.snap-tag-input' in read('web/app.css'))

# ==================================================================
print()
print('--- D6. 反向验证：把真源码改坏，看判据抓不抓得到 ---')
# ==================================================================
# 少了这一节，上面所有绿灯只能证明「我没写错」，不能证明「判据能抓错」。
# 1.0.7 那轮的 3 条恒绿就是没做这一步的结果。
# 做法：拿真源码做文本替换造出「有 bug 的版本」，用**同一套行为测试**跑，
# 必须观察到「上锁的快照被删了」。观察不到 = 判据瞎了，那上面的绿是假的。
import datetime as _dtmod


def inject(src, old, new, why):
    """做一次带守卫的注入。

    ⛔ 守卫不是形式主义。第一版这里就是**裸 replace**，而它静默失配 ——
    注入串的缩进按 8 空格写，可真实代码在 `for e in list(entries):`
    里面是 12 空格。结果 replace 匹配 0 次、源码原封不动，
    「注入 bug 后测试还绿」被当成了「判据抓不到错」。
    实际上那一轮跑的是**没注入的代码**，绿灯当然绿 —— 恒绿长得和真绿
    一模一样，正是最难发现的那种。

    （顺带排除过一个疑似原因：**不是行尾**。Python 文本模式的
    universal newline 会把 CRLF 全转成 LF，内存里 `count('\\r\\n')` 是 0。
    真正的原因是缩进。这类「猜的根因」如果不实测就会写进注释里骗人。）

    所以每次注入都必须自己先回答：这段文本真的在源码里吗？改了吗？
    两条都成立才允许继续，否则直接把失败原因抛出来。
    """
    n = src.count(old)
    if n != 1:
        raise AssertionError('注入目标 %s 在源码里出现 %d 次（应为恰好 1 次）' % (why, n))
    out = src.replace(old, new, 1)
    if out == src:
        raise AssertionError('注入 %s 之后源码没变化' % why)
    if old in out:
        raise AssertionError('注入 %s 之后原文本仍在' % why)
    return out


PRUNE_BUG_A = inject(
    HELPER, "            if e['protected']:\n                continue\n", '',
    '按天清理前的 protected 跳过')
PRUNE_BUG_B = inject(
    HELPER,
    "autos = [e for e in entries if e['auto'] and not e['protected']]",
    "autos = [e for e in entries if e['auto']]",
    '上锁快照不占配额')
DELETE_BUG = inject(
    HELPER,
    "if locked and p.get('force') not in (True, '1', 'true', 'on', 1):",
    "if False:",
    'force 门禁')
# 这行在helper 里有**两处**（11275 快照 meta、16977 别的模块），
# 所以必须带上前后的 json.dump 才唯一 —— 只写 `os.replace(tmp, p)`
# 会被守卫挡下来（这个守卫两次都救了我：一次是缩进错，一次是这里）。
# 顺带一条判据设计经验：**注入目标越具体越好**，
# 短片段在真实仓库里撞车是常态，不是意外。
NOTE_BUG = inject(
    HELPER,
    "            json.dump(meta, f, ensure_ascii=False, indent=2)\n"
    "        os.replace(tmp, p)",
    "            json.dump(meta, f, ensure_ascii=False, indent=2)\n"
    "        shutil.copy2(tmp, p)",
    '快照 meta 原子写')
# 下面两个注入对应**真机上抓到的两个 bug**，都是「判据当时是绿的」：
#   E 备注消毒把控制字符删掉而不是换成空格 → 'a\nb\tc' 变 'ab c'，
#     相邻词被粘在一起。本地判据只写「不含 \n \t」，恰好放过。
#   F web 层无条件透传 protected → helper 的取反语义从 HTTP 路上失效。
# 把它们做成注入，是为了让这两条判据以后不再退化成「只要没崩就算过」。
CLEAN_BUG = inject(
    HELPER,
    "    s = ''.join(ch if ch.isprintable() else ' ' for ch in str(raw or ''))",
    "    s = str(raw or '').replace('\\r', ' ').replace('\\n', ' ')\n"
    "    s = ''.join(ch for ch in s if ch.isprintable())",
    '备注消毒：控制字符换成空格')
WEB_BUG = inject(
    WEB,
    "        payload = {'ts': b.get('ts')}\n"
    "        if 'protected' in b:\n"
    "            payload['protected'] = b.get('protected')\n",
    "        payload = {'ts': b.get('ts'),\n"
    "                   'protected': b.get('protected')}\n",
    'web 层按 key 在不在透传 protected')


def _survives(src, tag_names, ts, protect):
    """用给定的（可能已被改坏的）源码跑一次 keep_days=1 清理，
    返回上锁那份是否幸存。"""
    t = tempfile.mkdtemp(prefix='revchk-')
    try:
        ns = make_env(t)
        build(tag_names, ns, src=src)
        d = mk(t, '20200101-000000', 'auto-interval', protected=protect)
        ns['act_snapshot_prune']({'keep_days': 1, 'keep_count': 0,
                                  'keep_manual': True})
        return os.path.isdir(d)
    finally:
        shutil.rmtree(t, ignore_errors=True)


# 注入 A：删掉「按天清理前的 protected 跳过」
src_a = _survives(PRUNE_BUG_A, ['_snap_load_meta', '_snap_dir',
                                'act_snapshot_prune'], 'x', True)
chk('[反向] 注入「按天清理漏了 protected」后，行为测试确实红',
    src_a is False, '注入 bug 后上锁快照居然还在 → 判据抓不到错')
chk('[反向] 同一套判据在未注入的真源码上是绿的（证明上一条不是恒红）',
    _survives(HELPER, ['_snap_load_meta', '_snap_dir',
                       'act_snapshot_prune'], 'x', True) is True)

# 注入 B：让上锁的自动快照重新占用 keep_count 配额
t = tempfile.mkdtemp(prefix='revchk2-')
try:
    ns = make_env(t)
    build(['_snap_load_meta', '_snap_dir', 'act_snapshot_prune'], ns,
          src=PRUNE_BUG_B)
    for i, ts2 in enumerate(['20260101-000001', '20260101-000002', '20260101-000003']):
        mk(t, ts2, 'auto-interval', protected=(i == 0))
    ns['act_snapshot_prune']({'keep_days': 0, 'keep_count': 1, 'keep_manual': True})
    chk('[反向] 注入「上锁快照占用保留份数」后，行为测试确实红',
        len(os.listdir(t)) == 1,
        '注入 bug 后剩 %d 份（D2 那条配额断言会红）' % len(os.listdir(t)))
finally:
    shutil.rmtree(t, ignore_errors=True)

# 注入 C：删掉 force 门禁
t = tempfile.mkdtemp(prefix='revchk3-')
try:
    ns = make_env(t)
    build(['_snap_load_meta', '_snap_dir', 'act_snapshot_delete'], ns,
          src=DELETE_BUG)
    d = mk(t, '20260202-000000', 'manual', protected=True)
    r = ns['act_snapshot_delete']({'ts': '20260202-000000'})
    chk('[反向] 注入「删掉 force 门禁」后，needs_force 断言确实红',
        r['ok'] and not os.path.isdir(d),
        '注入 bug 后一次就删掉了 → D2 的 needs_force 断言抓不到')
finally:
    shutil.rmtree(t, ignore_errors=True)

# 注入 D：_snap_save_meta 不做原子替换（os.replace → copy2）
# 这一条**行为测试抓不到**：tmp 写在同目录、紧接着就 copy2，测试环境里
# 两者结果一样。所以它只能退回源码判据 —— 但源码判据必须剥注释，
# 而且要在下面如实标注它抓不到哪类 bug，别假装它万能源。
t = tempfile.mkdtemp(prefix='revchk4-')
try:
    ns = make_env(t)
    build(['_snap_load_meta', '_snap_save_meta', '_snap_clean_tag',
           '_snap_dir', 'act_snapshot_note'], ns, src=NOTE_BUG)
    d = mk(t, '20260303-000000', 'init', protected=False)
    ns['act_snapshot_note']({'ts': '20260303-000000', 'tag': 'A'})
    m1 = json.load(open(os.path.join(d, '_meta.json'), encoding='utf-8'))
    ns['act_snapshot_note']({'ts': '20260303-000000', 'tag': 'B'})
    m2 = json.load(open(os.path.join(d, '_meta.json'), encoding='utf-8'))
    chk('[反向·如实记录] 原子性在行为层测不出来（copy2 注入后两次写入仍正确）',
        m1['tag'] == 'A' and m2['tag'] == 'B',
        '两次写入结果：%s / %s' % (m1.get('tag'), m2.get('tag')))
    chk('快照 meta 用的是 os.replace 原子写（不是 copy2）',
        'shutil.copy2(tmp, p)' not in HELPER
        and 'shutil.copy2(tmp, p)' in NOTE_BUG,
        '注入后的源码里该行应当消失（判据能分辨真/假两种写法）')
finally:
    shutil.rmtree(t, ignore_errors=True)

# 注入 E：备注消毒退回「删掉控制字符」（真机上抓到的 bug）
# 这一条要盯的是**行为**：把真源码换成删字符的版本后，
# D3 里那条「甲\x07乙 → 甲 乙」的断言必须变红。
# 换句话说 —— 如果哪天有人把消毒改回删除，这条反向验证先红，
# 提醒他「你正在把用户备注里的两个词粘在一起」。
t = tempfile.mkdtemp(prefix='revchk5-')
try:
    ns = make_env(t)
    build(['_snap_load_meta', '_snap_save_meta', '_snap_clean_tag',
           '_snap_dir', 'act_snapshot_note'], ns, src=CLEAN_BUG)
    mk(t, '20260404-000000', 'init', protected=False)
    ns['act_snapshot_note']({'ts': '20260404-000000', 'tag': '甲\x07乙'})
    m = json.load(open(os.path.join(t, '20260404-000000', '_meta.json'),
                       encoding='utf-8'))
    chk('[反向] 注入「控制字符被删而不是换成空格」后，消毒断言确实红',
        m['tag'] != '甲 乙',
        '注入后得到 %r —— D3 的两条消毒断言应当抓得到' % m['tag'])
finally:
    shutil.rmtree(t, ignore_errors=True)

# 注入 F：web 层无条件透传 protected（真机上抓到的 bug）
# 这条是**源码判据**的注入：把「按 key 在不在」改回「无条件写」，
# D4 里那条判据必须变红。行为上它对应「HTTP 传 {} 时取反失效」。
_webc = py_code_only(WEB_BUG)
_mf = re.search(r"def snapshot_protect\(self\):.*?(?=\n    def )", _webc, re.S)
_pbf = _mf.group(0) if _mf else ''
chk('[反向] 注入「无条件透传 protected」后，源码判据确实红',
    "'protected' in b" not in _pbf,
    '注入后 D4 那条判据应当抓得到')

# ==================================================================
print()
print('=' * 68)
print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
if FAIL:
    print()
    print('失败项：')
    for x in FAILDETAIL:
        print('  · %s' % x)
print('=' * 68)
sys.exit(1 if FAIL else 0)

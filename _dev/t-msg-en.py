# -*- coding: utf-8 -*-
"""钉住「后端消息英文对照表」这条机制（MSG_EN / _MSG_RX / _msg_lookup / ok / fail）。

三段：
  A. 表本身正确（占位符、英文无汉字、码值透传）
  B. **查表真的生效** —— 抽出真函数 exec，不在源码里 grep 字面量
  C. 反向验证：逐个注入、逐个确认变红、确认还原

为什么 B 段非要 exec 真代码：这一轮第一版就栽在这 ——
判据写成「源码里有没有 `return _msg_lookup(cn) or cn`」，全绿；
而产品里 msg_en 其实一直是 None（只有 msg 走了查表）。
静态断言全绿、真功能是坏的，只有真跑才抓得到。

⚠️ 反向验证会**改写 backend/drouter-helper.py**，因此：
  - 子进程跑本文件时必须带 _TMSEN_CHILD=1，否则无限递归（实测直接超时）
  - 判据开头先查「源码无注入残留」—— 被 SIGTERM 打断时 finally 不执行，
    注入会留在文件里，后面的红全都不好解释（2026-10-05 真踩过）
  - 备份放 _dev 内：Windows Python 读不到 Git Bash 的 /tmp
"""
import ast
import io
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HELPER = os.path.join(ROOT, 'backend', 'drouter-helper.py')
BAK = os.path.join(HERE, '_tmsgen.bak')

FAILS = []
N = 0


def chk(name, cond, detail=''):
    global N
    N += 1
    if not cond:
        FAILS.append('%s  %s' % (name, detail))
        print('  FAIL  %s  %s' % (name, detail))
    else:
        print('  ok    %s' % name)


src = io.open(HELPER, encoding='utf-8').read()

# ---------------------------------------------------------------- A. 表结构
print('[A] 对照表结构')
# 只列**注入脚本真的会写进去**的标记。不要把自己在判据里写的探针也算进去 ——
# 否则探针会匹配到判据源码自己，恒红。
chk('源码无反向验证注入残留',
    'INJECT-BROKEN-LOOKUP' not in src and 'INJECT-BAD-LOOKUP' not in src)

tree = ast.parse(src)
msg_en_node = msg_rx_node = None
for n in tree.body:
    if isinstance(n, ast.Assign):
        tid = getattr(n.targets[0], 'id', None)
        if tid == 'MSG_EN':
            msg_en_node = n
        elif tid == '_MSG_RX':
            msg_rx_node = n
chk('MSG_EN 是模块级常量', msg_en_node is not None)
chk('_MSG_RX 是模块级常量', msg_rx_node is not None)
chk('MSG_EN 是 dict 字面量',
    msg_en_node is not None and isinstance(msg_en_node.value, ast.Dict))

tbl = {}
if msg_en_node is not None:
    for k, v in zip(msg_en_node.value.keys, msg_en_node.value.values):
        tbl[k.value] = v.value
chk('MSG_EN 非空', len(tbl) > 400, '实际 %d 条' % len(tbl))

bad_ph = []
for cn, en in tbl.items():
    a = len(re.findall(r'%[-+ #0-9.]*[sdf]', cn))
    b = len(re.findall(r'%[-+ #0-9.]*[sdf]', en))
    if a != b:
        bad_ph.append((cn, a, en, b))
chk('占位符个数一一对应', not bad_ph, str(bad_ph[:3]))

han = [(cn, en) for cn, en in tbl.items() if re.search(r'[\u4e00-\u9fff]', en)]
chk('英文值不含汉字', not han, str(han[:3]))

for code in ('NO_DOMAIN', 'NO_PUBLIC_IP', 'BAD_OP'):
    chk('码值透传 %s' % code, tbl.get(code) == code, '实际 %r' % tbl.get(code))

# _MSG_RX 里必须是**已编译正则对象**，不是字符串 ——
# _msg_lookup 写的是 `_rx.match(cn)`，存字符串会 AttributeError。
rx_bad = []
if msg_rx_node is not None and isinstance(msg_rx_node.value, ast.List):
    for el in msg_rx_node.value.elts:
        if not (isinstance(el, ast.Tuple) and len(el.elts) == 3):
            rx_bad.append('不是三元组')
            continue
        # re.compile 的 func 是 **Attribute**（value=Name('re'), attr='compile'），
        # 不是 Name —— 2026-10-05 踩过：用 getattr(func,'id','') 判，恒为 ''，
        # 判据对真实的 re.compile() 也报红。两种形态都放行。
        f = el.elts[1].func if isinstance(el.elts[1], ast.Call) else None
        is_re = (isinstance(f, ast.Attribute) and f.attr == 'compile'
                 and isinstance(f.value, ast.Name) and f.value.id == 're')
        is_re = is_re or (isinstance(f, ast.Name) and f.id == 're_compile')
        if not is_re:
            rx_bad.append('第 2 项不是 re.compile(): %s' % ast.dump(el.elts[1])[:60])
chk('_MSG_RX 每项都是 (tpl, re 对象, en)', not rx_bad, str(rx_bad[:2]))

# ---------------------------------------------------------------- B. 真跑
# 从真源码 ast 抽符号再 exec —— 测的是真代码。
print('\n[B] 真跑 _pick / _resolve / ok / fail')
ns = {'re': re}
FUNCS = ('_msg_lookup', '_pick', '_resolve', 'ok', 'fail')
CONSTS = ('MSG_EN', '_MSG_RX', '_cur_lang', '_DEFAULT_LANG')
picked = []
for n in tree.body:
    if isinstance(n, ast.Assign) and getattr(n.targets[0], 'id', None) in CONSTS:
        picked.append(n)
    elif isinstance(n, ast.FunctionDef) and n.name in FUNCS:
        picked.append(n)
chk('抽到 %d 个函数 + %d 个常量' % (len(FUNCS), len(CONSTS)),
    len(picked) == len(FUNCS) + len(CONSTS), '实际 %d 个' % len(picked))

exec(compile(ast.Module(body=picked, type_ignores=[]), HELPER, 'exec'), ns)
pick, ok, fail, lookup = ns['_pick'], ns['ok'], ns['fail'], ns['_msg_lookup']
cur = ns['_cur_lang']


def set_lang(v):
    cur[0] = v


set_lang('en-US')
chk('英文界面：字面量文案走精确键', pick('设置已保存', None) == 'Settings saved',
    '实际 %r' % pick('设置已保存', None))

set_lang('zh-CN')
chk('中文界面：显示中文原文', pick('设置已保存', None) == '设置已保存')
chk('中文界面：忽略 en 参数', pick('设置已保存', 'Override') == '设置已保存')

set_lang('en-US')
chk('英文界面：显式 en 参数优先', pick('设置已保存', 'Override') == 'Override')

# 模板正则：必须**比对完整英文**，不能只比前缀。
# 只比前缀时，「捕获组没回填」会红成「查不到」，红的原因会误导排查方向。
for cn_tpl, args, expect in [
        ('未知操作：%s', ('badop',), 'Unknown action: badop'),
        ('读取失败：%s', ('/etc/x',), 'Failed to read: /etc/x'),
        ('不支持的打印服务操作：%s', ('xyz',), 'Unsupported print service action: xyz'),
        ('受保护的系统账号，禁止删除：%s（UID %d）', ('root', 0),
         'Protected system account, deletion is forbidden: root (UID 0)'),
        ('未知操作：%s', ('another-op',), 'Unknown action: another-op'),
]:
    rendered = cn_tpl % args
    got = pick(rendered, None)
    chk('模板正则兜底+回填: %s' % cn_tpl[:18], got == expect,
        '渲染=%r 期望=%r 实得=%r' % (rendered, expect, got))
    chk('  └ 英文里不该残留占位符: %s' % cn_tpl[:14], '%s' not in (got or ''),
        '实得=%r' % got)

set_lang('en-US')
chk('查不到时回落中文（不空白）',
    pick('这是一条完全不在表里的文案', None) == '这是一条完全不在表里的文案')
chk('空串输入不炸', pick('', None) == '')

o = ok({'x': 1}, '设置已保存')
chk('ok(): msg_cn 保留中文原文', o['msg_cn'] == '设置已保存', repr(o))
chk('ok(): msg_en 为英文（前端按语言自选时读它）',
    o['msg_en'] == 'Settings saved', repr(o))
chk('ok(): msg 按语言选英文', o['msg'] == 'Settings saved', repr(o))
chk('ok(): data 透传', o['data'] == {'x': 1}, repr(o))

f = fail('快照不存在：%s' % 'snap1', 'NOT_FOUND')
chk('fail(): msg_cn 保留原文', f['msg_cn'] == '快照不存在：snap1', repr(f))
chk('fail(): msg_en 走模板正则并回填',
    f['msg_en'] == 'Snapshot does not exist: snap1', repr(f))
chk('fail(): msg 走模板正则', f['msg'] == 'Snapshot does not exist: snap1', repr(f))
set_lang('zh-CN')
chk('fail(): 中文界面 msg 仍是中文',
    fail('快照不存在：%s' % 'snap1', 'X')['msg'] == '快照不存在：snap1')

# 码值透传：英文界面下也照原样（这些是机器可读码，翻成句子反而破坏前端判断）
set_lang('en-US')
chk('码值 msg 透传', fail('NO_DOMAIN', 'NO_DOMAIN')['msg'] == 'NO_DOMAIN')

# 短模板不做正则兜底
mis = []
for probe in ('会话已保存', '服务已启动', '已处理 3 项', '计划 5 个'):
    if pick(probe, None) != probe:
        mis.append((probe, pick(probe, None)))
chk('短模板不做正则兜底（不误匹配）', not mis, str(mis))

# ---------------------------------------------------------------- 早退
IN_CHILD = os.environ.get('_TMSEN_CHILD') == '1'
if IN_CHILD:
    print('\n(子进程模式：只跑 A/B 段，供父进程的反向验证读取)')
    print('==== t-msg-en(child): %d 条判据, %d 失败 ====' % (N, len(FAILS)))
    sys.exit(1 if FAILS else 0)

# ---------------------------------------------------------------- C. 反向验证
print('\n[C] 反向验证（逐个注入 → 必须变红 → 还原）')
CHILD_ENV = dict(os.environ, _TMSEN_CHILD='1')


def run_child():
    r = subprocess.run([sys.executable, __file__], capture_output=True,
                       text=True, env=CHILD_ENV)
    return r.stdout + r.stderr


shutil.copyfile(HELPER, BAK)
orig = io.open(HELPER, encoding='utf-8').read()
try:
    # C1: 摘掉查表 —— 英文界面整体回落中文
    a1 = "    en = en or _msg_lookup(msg)"
    chk('C1 锚点在当前源码里存在', orig.count(a1) == 1,
        '锚点出现 %d 次' % orig.count(a1))
    inj = orig.replace(a1, "    en = en  # INJECT-BROKEN-LOOKUP", 1)
    chk('C1 注入点唯一', inj != orig and inj.count('INJECT-BROKEN-LOOKUP') == 1)
    io.open(HELPER, 'w', encoding='utf-8', newline='').write(inj)
    c = run_child()
    red = [l for l in c.split('\n') if 'FAIL' in l]
    chk('C1 摘掉查表 -> 判据变红', bool(red), '重跑后没有任何 FAIL 行')
    chk('C1 红在「msg_en 变 None」那条上（而非红在别处）',
        any('msg_en' in l for l in red),
        '期望红在 ok(): msg_en 为英文，实际红在：\n      ' + '\n      '.join(red)[:400])

    # C2: 破坏捕获组回填（第一版就是这么写的：直接 return _en）
    #     这条比 C1 隐蔽：C1 破坏后英文界面全变中文，一眼就看出来；
    #     C2 破坏后英文界面**大部分正常**，只有带占位符的那几条多出 %s。
    shutil.copyfile(BAK, HELPER)
    orig2 = io.open(HELPER, encoding='utf-8').read()
    a2 = "            return _en % m.groups()"
    chk('C2 锚点在当前源码里存在', orig2.count(a2) == 1,
        '锚点出现 %d 次' % orig2.count(a2))
    inj2 = orig2.replace(a2, "            return _en  # INJECT-BAD-LOOKUP", 1)
    chk('C2 注入点唯一', inj2 != orig2 and inj2.count('INJECT-BAD-LOOKUP') == 1)
    io.open(HELPER, 'w', encoding='utf-8', newline='').write(inj2)
    c2 = run_child()
    red2 = [l for l in c2.split('\n') if 'FAIL' in l]
    chk('C2 破坏回填 -> 判据变红', bool(red2), '重跑后没有任何 FAIL 行')
    chk('C2 红在「不该残留占位符」那条上',
        any('不该残留' in l for l in red2), '\n      '.join(red2)[:500])
finally:
    shutil.copyfile(BAK, HELPER)
    if os.path.exists(BAK):
        os.remove(BAK)

restored = io.open(HELPER, encoding='utf-8').read()
chk('注入还原后源码与原始一致', restored == orig)
c3 = run_child()
head = c3.split('[C]')[0]
chk('还原后 A/B 段全绿', 'FAIL' not in head,
    '\n'.join(l for l in head.split('\n') if 'FAIL' in l)[:600])

print('\n==== t-msg-en: %d 条判据, %d 失败 ====' % (N, len(FAILS)))
if FAILS:
    for x in FAILS:
        print('  - ' + x)
    sys.exit(1)
print('MSG_EN_OK')

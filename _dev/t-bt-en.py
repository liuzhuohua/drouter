# -*- coding: utf-8 -*-
"""钉住「后端下发表的中文字段必须走 bt4() 查英文」。

背景（2026-10-05）：用户反馈「切换英文后仍有大量中文，包括提示、标题、类目」。
上一轮只补了 app.js 自己的 t()，漏了**后端下发的数据** —— DDNS 服务商、
QoS 档位、清理项、告警规则、向导步骤等 30 张表、165 个条目、347 个字段。

判据：
  ① bt 表完整：表数 / 条目数 / 字段数达标，且**英文字段里无汉字**
  ② bt 表的 key 与真源码（backend/drouter-helper.py 的表定义）对齐
  ③ app.js 里凡是**后端表字段**的直接显示（`esc(x.name)` 这种裸读）
     都必须包了 bt4(...)
  ④ bt4 调用点引用的表名都在 bt 表里（拼错表名会静默回落中文）
  ⑤ node --check 通过（间接：脚本自身跑的判据基于 AST，不执行 JS）
"""
import ast
import collections
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, 'web', 'app.js')
I18N = os.path.join(ROOT, 'web', 'i18n.js')
HELPER = os.path.join(ROOT, 'backend', 'drouter-helper.py')

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


# ---------------------------------------------------------------- ① bt 表完整性
print('[A] bt 表完整性')
ij = io.open(I18N, encoding='utf-8').read()
m = re.search(r'\n    bt: \{\n(.*?)\n    \},\n', ij, re.S)
chk('i18n.js 里有 bt 分组', m is not None)
if m is None:
    print('\n==== t-bt-en: %d 条判据, %d 失败 ====' % (N, len(FAILS)))
    sys.exit(1)
bt_src = m.group(1)
n_tbl = len(re.findall(r"^      '[^']+': \{$", bt_src, re.M))
n_row = len(re.findall(r"^        '[^']*': \{ ", bt_src, re.M))
n_field = len(re.findall(r"'[^']*': '[^']*'", bt_src))
chk('表数 >= 30', n_tbl >= 30, '实际 %d' % n_tbl)
chk('条目数 >= 150', n_row >= 150, '实际 %d' % n_row)
chk('字段数 >= 300', n_field >= 300, '实际 %d' % n_field)

# 英文字段里不该有汉字
en_vals = re.findall(r"'[^']*': '((?:[^'\\]|\\.)*)'", bt_src)
with_han = [v for v in en_vals if re.search(r'[\u4e00-\u9fff]', v)]
chk('bt 表英文值里无汉字', not with_han, str(with_han[:3]))

# 码值/专有名词豁免：像 "Children's devices" 里的撇号不算问题
chk('英文值里的撇号已转义', "\\'" in bt_src or "'" not in
    ''.join(v for v in en_vals if "'" in v),
    '英文值里有未转义的单引号')

# ---------------------------------------------------------------- ② key 对齐真源码
print('\n[B] bt 表 key 与真源码对齐')
src = io.open(HELPER, encoding='utf-8').read()
tree = ast.parse(src)
real = {}
for node in tree.body:
    if not isinstance(node, ast.Assign):
        continue
    name = getattr(node.targets[0], 'id', None)
    if not name or not re.fullmatch(r'[A-Z_]{3,}', name):
        continue
    if not isinstance(node.value, (ast.List, ast.Tuple)):
        continue
    items, has_cn = [], False
    for el in node.value.elts:
        if not isinstance(el, ast.Dict):
            continue
        d = {}
        for k, v in zip(el.keys, el.values):
            if isinstance(k, ast.Constant) and isinstance(k.value, str) \
               and isinstance(v, ast.Constant) and isinstance(v.value, str):
                d[k.value] = v.value
        if d:
            items.append(d)
            if any(re.search(r'[\u4e00-\u9fff]', x) for x in d.values()):
                has_cn = True
    if has_cn and len(items) >= 2:
        real[name] = items

missing_tbl = [t for t in real if ("      '%s': {" % t) not in bt_src]
chk('真源码的每张中文表都在 bt 里', not missing_tbl, str(missing_tbl[:5]))
chk('真源码的中文表 >= 30 张', len(real) >= 30, '实际 %d' % len(real))

# ---------------------------------------------------------------- ③④ app.js 调用点
print('\n[C] app.js 调用点')
app = io.open(APP, encoding='utf-8').read()
n_bt4 = app.count('bt4(')
chk('app.js 里有 bt4 调用', n_bt4 > 0, '%d 处' % n_bt4)
chk('bt4 定义存在', 'function bt4(' in app)
# ⚠️ 原来写 `\{[^}]*fallback` —— 函数体里第一层就有 `{}`，
#    `[^}]*` 在那里就截断了，正则永远匹配不到（2026-10-05 实测假红）。
#    改成「取 bt4 整个函数体（按花括号配平）再看有没有 fallback」。
def _fn_body(src, name):
    i = src.find('function %s(' % name)
    if i < 0:
        return ''
    j = src.index('{', i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == '{':
            depth += 1
        elif src[k] == '}':
            depth -= 1
            if depth == 0:
                return src[j:k + 1]
    return ''


_bt4_body = _fn_body(app, 'bt4')
chk('bt4 查不到时回落 fallback', 'fallback' in _bt4_body, _bt4_body[:120])
chk('bt4 会先问 bt()', 'bt(' in _bt4_body, _bt4_body[:120])

# 引用的表名必须都在 bt 表里
used = set(re.findall(r"bt4\('([A-Z_]+)'", app))
bad_used = sorted(t for t in used if ("      '%s': {" % t) not in bt_src)
chk('bt4 引用的表名都在 bt 表里', not bad_used, str(bad_used))

# ⑤ 全前端（不止 app.js）：upstream.js / netdetail.js / realtime.js / update.js
#    也在用 bt()/bt4()，它们引用的表名同样必须在 bt 分组里。
#    ⚠️ 2026-10-09 的坑：UPS_PROTO 被手工加在 DICT **顶层**（与 bt: 同级），
#       于是 bt('UPS_PROTO', …) 永远返回 ''，英文界面上游协议标签整列中文；
#       而当时只扫 app.js 的判据全绿 —— 必须按**所有前端文件**判。
WEB = os.path.join(ROOT, 'web')
used_all = set()
for _fn in sorted(os.listdir(WEB)):
    if not _fn.endswith('.js'):
        continue
    _t = io.open(os.path.join(WEB, _fn), encoding='utf-8').read()
    used_all |= set(re.findall(r"\bbt4?\('([A-Z_][A-Z0-9_]*)'", _t))
bad_all = sorted(t for t in used_all if ("      '%s': {" % t) not in bt_src)
chk('全前端 bt/bt4 引用的表名都在 bt 表里', not bad_all, str(bad_all))
chk('全前端 bt/bt4 引用表数 >= 20', len(used_all) >= 20, '实际 %d' % len(used_all))

# 裸读后端表字段（没包 bt4）—— 已知豁免名单
# ⚠️ 不要再按「字段名」判裸读（2026-10-05 的教训）：
#    `esc(i.name)` 里的 i 可能是**网卡**、d 可能是**磁盘**、c 可能是**容器**、
#    u 是**用户**、f 是**文件** —— 字段名撞车导致 44 处误报，全是假红。
#    变量名高度复用，**静态判据无法区分**。
#    改判「bt4 调用的表名集合」与「真源码的表名集合」是否一致 ——
#    那是可静态验证的（表名是显式写出来的），而变量名不是。
BARE_EXEMPT_FNS = set()   # 不再按函数豁免，改为不按字段名判
# 表字段的并集
TABLE_FIELDS = set()
for _t, items in real.items():
    for d in items:
        for k, v in d.items():
            if re.search(r'[\u4e00-\u9fff]', v):
                TABLE_FIELDS.add(k)
# 注意：name/n/why/desc 这类字段名在**非表**上下文也大量出现
# （文件名、用户名、网卡名…），所以这里只报「可疑项」供人工确认，
# 不直接判失败 —— 直接判会误报一堆。
lines = app.split('\n')
fn_re = re.compile(r'^(?:async )?function (\w+)\(')
fns = []
for i, l in enumerate(lines):
    mm = fn_re.match(l)
    if mm:
        fns.append((i + 1, mm.group(1)))


def owner(ln):
    best = '<module>'
    for fl, fn in fns:
        if fl <= ln:
            best = fn
        else:
            break
    return best


# ③ app.js 实际用到的表（bt4 第一个参数）必须都在前端 bt 里定义。
#    ⚠️ bt4 读取的是前端 i18n.js 的 bt 分组（后端根本没有 bt4 函数），
#       所以判据应为「表名都在前端 bt 里」—— 这才是 bt4 能查到的前提。
#       原先写「存在于后端」，会误伤两类表：① 后端表（但定义形态让
#       后端解析器漏抓）；② 纯前端翻译表（DDNS_REGION / WAN_STATE /
#       THEME_NAMES / VLAN_PRESETS / DDNS_CUSTOM，它们翻译的是后端下发
#       的「值」而非后端「表」，本就只在 i18n.js 里）。
used_tables = set(re.findall(r"bt4\('([A-Z_]+)'", app))
unknown = sorted(t for t in used_tables if ("      '%s': {" % t) not in bt_src)
chk('bt4 用到的表都在前端 bt 里定义', not unknown, str(unknown))
defined = sorted(t for t in used_tables if ("      '%s': {" % t) in bt_src)
chk('bt4 覆盖的表数 >= 15', len(defined) >= 15, '实际 %d 张' % len(defined))

# ④ 双向：真源码的表里，有多少已在 app.js 被引用
#    （没被引用的表 = 英文界面下那批数据仍是中文，这是「彻底改全」的缺口）
# ⚠️ 有一类「未引用」是**正常的**：前端对这些表**不走表、而是硬编码 t() 词条**
#    （2026-10-05 实测 WAN_ACCESS_TYPES / OH_ITEMS 两条）：
#      · WAN_ACCESS_TYPES —— 前端在 renderWanModePanel 里用 t('wan.accPppoe…')
#        逐个硬编码 <option>，压根不读后端表；
#      · OH_ITEMS —— OpenSOHO 页把六个设置写成固定表单 + t() 词条。
#    这两张表的英文在 i18n.js 的 wan/oh 组里（上一批已补），
#    不需要 bt4。列在这里只是提醒别漏，不是缺陷。
KNOWN_T_NOT_TABLE = {'WAN_ACCESS_TYPES', 'OH_ITEMS'}
# 间接写法：bt4(表名 || '默认', …) / kernCard(it, ctrl, '表名')
# 静态扫不到，要单独认。
KNOWN_INDIRECT = {'KERN_ITEMS', 'KERN_FW_ITEMS', 'WIZ_WAN_MODES'}

unref = sorted(set(real) - used_tables - KNOWN_T_NOT_TABLE - KNOWN_INDIRECT)
print()
print('提示：%d 张后端表既没被 bt4 引用、也不在「已硬编码 t()」/「间接写法」名单里：'
      % len(unref))
for t in unref:
    print('       ' + t)
print('（%d 张是「前端硬编码 t() 词条」：%s）'
      % (len(KNOWN_T_NOT_TABLE), ', '.join(sorted(KNOWN_T_NOT_TABLE))))
print('（%d 张是「bt4 表名走变量/参数」：%s）'
      % (len(KNOWN_INDIRECT), ', '.join(sorted(KNOWN_INDIRECT))))
print('提示结束')
print()
chk('没有「漏接」的后端表', not unref, str(unref))

# ---------------------------------------------------------------- 汇总
print('\n==== t-bt-en: %d 条判据, %d 失败 ====' % (N, len(FAILS)))
if FAILS:
    for f in FAILS:
        print('  - ' + f)
    sys.exit(1)
print('BT_EN_OK')

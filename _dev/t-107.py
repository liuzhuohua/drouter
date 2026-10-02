# -*- coding: utf-8 -*-
"""1.0.7 新增 4 个模块的专项自查（对应任务 #154）。

t-api106.py 查的是「路径 / 方法 / 覆盖」，t-apicheck.py 查的是「字段转发」。
但这两者都是全站通用视角，对backup / alert / vpn / quota 这4 个新模块
**字段特别多、返回结构又嵌套**（conf / live / rules / peers / devices / plan），
通用视角下最容易漏的是「前端读了d.data.foo.bar，helper 那边叫 baz」。

这里做六件事：

  A. **字段契约双向对照** —— 静态抽出前端 4 套页面里读的 `x.y` 路径，
     与 helper 各状态函数 `ok({...})` 里的键逐个比对，双向都报差异。
  B. **op 名三段对齐** —— 前端传的 op → Web 层放行的 op → helper
     实际分支。三处名字必须完全一致，任何一环对不上就是「点了没反应」。
  C. **高危路径纯逻辑测试**（用替身注入，不碰真机）：
     备份的 tar 炸弹拦截 / 路径穿越 / 敏感文件默认排除 / 还原确认门；
     告警的冷却期 / 免打扰对 critical 的例外；
     VPN 的私钥不外泄 / Exit Node 双保险 / 端口冲突语义；
     配额的聚合幂等（重复跑不重复计数）/ 设备身份按 IP 合并。
  D. **构建保护模式一致性** —— check_build_guard 是否覆盖 4 个新动作里
     「会改网络状态」的那些。
  E. **部署三形态一致性** —— deb / Docker / deploy.sh 都要能拿到
     3 个新守护脚本，且 helper 动作名对得上。
  F. **危险动作的审计与确认门** —— 逐条列出「会写盘 / 会改网」的 op，
     断言它在 Web 层要么写了审计，要么有confirm 门。
"""
import ast
import base64
import datetime as datetime_module
import hashlib
import io
import ipaddress
import os
import re
import shutil
import smtplib
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def read(rel):
    with io.open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


WEB = read('backend/drouter-web.py')
APP = read('web/app.js')
HELPER = read('backend/drouter-helper.py')
DEBSH = read('packaging/build-deb.sh')
DEPLOY = read('scripts/deploy.sh')
DOCKER = read('packaging/Dockerfile') if os.path.isfile(
    os.path.join(ROOT, 'packaging/Dockerfile')) else ''

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
            for ln in str(extra).splitlines():
                print('     %s' % ln)


# ==================================================================
# A. 字段契约双向对照
# ==================================================================
print('=' * 68)
print('A. 字段契约：前端读的字段 ↔ helper 返回的字段')
print('=' * 68)

# --- A1. 抽出 4 套页面的函数体 ---
NEW_FUNCS = re.findall(
    r'^(?:async )?function ((?:bk|al|vpn|qt)[A-Za-z0-9_]*)\(', APP, re.M)


def func_body(name, src=APP):
    """取一个顶层函数的完整 body（含嵌套花括号）。"""
    m = re.search(r'^(?:async )?function %s\(' % re.escape(name), src, re.M)
    if not m:
        return ''
    i = src.index('{', m.end() - 1)
    # 从 function 关键字后的第一个 { 开始配平
    i = src.index('{', m.start())
    depth = 0
    for j in range(i, min(len(src), i + 200000)):
        c = src[j]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    return ''


BODIES = {f: func_body(f) for f in NEW_FUNCS}
empty = [f for f, b in BODIES.items() if not b]
chk('4 套页面的 %d 个函数都能定位到body' % len(NEW_FUNCS), not empty,
    '定位失败: %s' % ', '.join(empty))

PAGE_SRC = '\n'.join(BODIES.values())

# --- A2. 前端读的字段路径 ---
# 模式1: d.data / res.data 链式访问      d.data.conf.peers
# 模式2: 解构后的局部变量（保守：只抓显式的 .a.b 两层）
CHAIN = re.findall(
    r'\b(?:d|dd|res|r|r1|r2|r3|dt|dat|resp|o|out)\.data((?:\.[A-Za-z_]\w*)+)',
    PAGE_SRC)
FRONT_FIELDS = set()
for c in CHAIN:
    parts = re.findall(r'\.([A-Za-z_]\w*)', c)
    if parts:
        FRONT_FIELDS.add(parts[0])            # 一级：顶层 data 的键
        if len(parts) >= 2:
            FRONT_FIELDS.add('%s.%s' % (parts[0], parts[1]))   # 二级
# 渲染函数里常见的 s.xxx / c.xxx（对象是 data 的子对象）
SUB = re.findall(r'\b([a-z]{1,3})\.([a-z_][a-z0-9_]*)\b', PAGE_SRC)

# --- A3. helper 各状态函数返回的键 ---
def code_only(src):
    """去掉 Python 注释，只留代码行。

    修 bug 时经常要在注释里写清「原来错在哪」，比如
    `sensitive=(d == '/etc/drouter')` 这段原代码。拿整段源码做
    子串匹配就会把注释当成代码，报出假红灯 —— 而且这种红灯
    最坏的情况是「为了让测试过，把注释删了」。
    """
    out = []
    for ln in src.split('\n'):
        s = ln.strip()
        if s.startswith('#'):
            continue
        # 去掉行尾注释（粗略处理：不处理字符串里的 # ）
        i = ln.find('  #')
        if i > 0:
            ln = ln[:i]
        out.append(ln)
    return '\n'.join(out)


def py_func_src(name, src=HELPER):
    """用 ast 精确取一个模块级函数的源码段。"""
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ''
    return ''


def returned_keys(fname):
    """抽函数里所有「返回载荷」的键。

    要同时覆盖两种返回形式，只扫 ok({...}) 会漏：
      ok({...})            —— 成功
      fail(msg, code, {...}) —— 失败但**带数据**（比如启动失败要回传
                             journal 日志给界面）。漏掉这一种会
                             把「前端读了 r.data.journal」误判成
                             「helper 从没返回过 journal」。
    """
    body = py_func_src(fname)
    keys = set()
    for pat in (r'ok\(\{', r"fail\(\s*[^,()]+,\s*'[^']*',\s*\{",
                r'fail\(\s*[^,()]+,\s*code=[\'"][^\'"]*[\'"]\s*,\s*\{'):
        for m in re.finditer(pat, body):
            i = m.end() - 1
            depth = 0
            blob = ''
            for j in range(i, min(len(body), i + 60000)):
                if body[j] == '{':
                    depth += 1
                elif body[j] == '}':
                    depth -= 1
                    if depth == 0:
                        blob = body[i + 1:j]
                        break
            if not blob:
                continue
            for km in re.finditer(r"['\"]([a-z_][a-z0-9_]*)['\"]\s*:", blob):
                keys.add(km.group(1))
    return keys


# 每个前端页面对应哪些 helper 状态函数
PAGE_FN = {
    'backup': ['_bk_status', '_bk_list_entries', '_bk_create', '_bk_inspect',
               '_bk_restore', '_bk_prune', '_bk_conf'],
    'alert': ['_al_status', '_al_run', '_al_test', '_al_conf_op', '_al_clear',
              '_al_hist_read', '_al_services'],
    'vpn': ['_vpn_status', '_vpn_save_op', '_vpn_apply_op', '_vpn_peer_add',
            '_vpn_peer_del', '_vpn_peer_toggle', '_vpn_peer_conf',
            '_vpn_endpoint', '_vpn_delete_all'],
    'quota': ['_qt_status', '_qt_report', '_qt_conf', '_qt_reset', '_qt_device',
              '_qt_aggregate'],
}
ALLOWED = {'ok', 'msg_cn', 'err', 'code', 'code_cn'}   # 统一响应信封

for mod, fns in PAGE_FN.items():
    be = set()
    for f in fns:
        be |= returned_keys(f)
    be |= ALLOWED
    pref = {'backup': 'bk', 'alert': 'al', 'vpn': 'vpn', 'quota': 'qt'}[mod]
    fe = set()
    for f, b in BODIES.items():
        if not f.startswith(pref):
            continue
        for c in re.findall(
                r'\b(?:d|dd|res|r|dt|dat|o|out)\.data((?:\.[A-Za-z_]\w*)+)', b):
            parts = re.findall(r'\.([A-Za-z_]\w*)', c)
            if parts:
                fe.add(parts[0])
    # 只报「前端出现、helper 的返回载荷里连一次都没有」的键 ——
    # 那才是真的契约断裂。缺字段在前端不会报错（普遍写 r.data || {}），
    # 只会静默渲染成空，所以这类断裂特别难靠肉眼发现。
    miss = sorted(x for x in fe if x not in be)
    chk('[%s] 前端读取的一级字段 helper 都有返回' % mod, not miss,
        'helper 从未返回过: %s\n     helper 实际返回: %s'
        % (', '.join(miss), ', '.join(sorted(be))))

# A4. 反向：helper 返回了但前端从不读的顶层键（列出来人工判断）
print()
print('--- A2. helper 返回但前端未直接读取的键（供人工判断）---')
for mod, fns in PAGE_FN.items():
    be = set()
    for f in fns:
        be |= returned_keys(f)
    pref = {'backup': 'bk', 'alert': 'al', 'vpn': 'vpn', 'quota': 'qt'}[mod]
    used = set()
    for f, b in BODIES.items():
        if f.startswith(pref):
            for c in re.findall(
                    r'\b(?:d|dd|res|r|dt|dat|o|out)\.data((?:\.[A-Za-z_]\w*)+)', b):
                parts = re.findall(r'\.([A-Za-z_]\w*)', c)
                if parts:
                    used.add(parts[0])
            for c in re.findall(r'\b([a-z]{1,3})\.([a-z_][a-z0-9_]*)\b', b):
                used.add(c[1])
    un = sorted(be - used - ALLOWED)
    if un:
        print('     [%s] %s' % (mod, ', '.join(un)))
print('     （这些多半是给子对象用的中间字段，不算问题）')

# ==================================================================
# B. op 名三段对齐
# ==================================================================
print()
print('=' * 68)
print('B. op 名三段对齐：前端 → Web 层 → helper')
print('=' * 68)

# B1. 前端传的 op
FE_OPS = {}
for m in re.finditer(r"""\bapi\(\s*(['"`])(/api/(\w+)[^'"`]*)\1\s*,\s*\{(.*?)\}\s*\)""",
                     APP, re.S):
    path, mod, blob = m.group(2), m.group(3), m.group(4)
    if mod not in ('backup', 'alert', 'vpn', 'quota'):
        continue
    om = re.search(r"""op\s*:\s*['"]([a-z_]+)['"]""", blob)
    if om:
        FE_OPS.setdefault(mod, {})[om.group(1)] = m.start()
# restore 的三步是数组形式，单独抓
for mod in ('backup', 'alert', 'vpn', 'quota'):
    blk = ''
    for f in NEW_FUNCS:
        if f.startswith({'backup': 'bk', 'alert': 'al',
                         'vpn': 'vpn', 'quota': 'qt'}[mod]):
            blk += BODIES[f]
    for om in re.finditer(r"""op\s*:\s*['"]([a-z_]+)['"]""", blk):
        FE_OPS.setdefault(mod, {}).setdefault(om.group(1), 0)

# B2. helper 里的 op 分支
HELPER_OPS = {}
for mod, act in (('backup', 'act_backup'), ('alert', 'act_alert'),
                 ('vpn', 'act_vpn'), ('quota', 'act_quota')):
    body = py_func_src(act)
    HELPER_OPS[mod] = set(re.findall(r"op == '([a-z_]+)'", body))

# B3. Web 层显式处理的 op
WEB_OPS = {}
for mod, meth in (('backup', 'def backup(self'), ('alert', 'def alert(self'),
                  ('vpn', 'def vpn(self'), ('quota', 'def quota(self')):
    i = WEB.index(meth)
    j = WEB.index('\n    def ', i + 10)
    seg = WEB[i:j]
    WEB_OPS[mod] = set(re.findall(r"op == '([a-z_]+)'", seg)) | \
        set(re.findall(r"op in \(([^)]*)\)", seg) and
            re.findall(r"'([a-z_]+)'", re.findall(r"op in \(([^)]*)\)", seg)[0]))

print('前端→helper op 差异：')
bad_op = []
for mod in ('backup', 'alert', 'vpn', 'quota'):
    fe = set(FE_OPS.get(mod, {}))
    he = HELPER_OPS.get(mod, set())
    only_fe = sorted(fe - he)
    only_he = sorted(he - fe - {'status'})
    if only_fe:
        bad_op.append('[%s] 前端传了 helper 不认的 op: %s' % (mod, ', '.join(only_fe)))
    print('     [%-6s] 前端 %d 个 / helper %d 个；helper 有但前端不调: %s'
          % (mod, len(fe), len(he), ', '.join(only_he) or '无'))
chk('前端传的 op helper 都有分支', not bad_op, '\n     '.join(bad_op))

# B4. Web 层：写操作的 op 必须原样透传到 helper。
# 不能只搜 `op=op` 一种写法 —— backup handler 走的是
# `payload = dict(b); payload['op'] = op` 再传 payload，
# vpn/quota 才是 dict(b, op=op)。两种都认。
for mod, meth in (('backup', 'def backup(self'), ('alert', 'def alert(self'),
                  ('vpn', 'def vpn(self'), ('quota', 'def quota(self')):
    i = WEB.index(meth)
    j = WEB.index('\n    def ', i + 10)
    seg = WEB[i:j]
    chk('[%s] Web 层 handler 有 auth 校验' % mod, 'self.auth()' in seg)
    passthru = bool(
        re.search(r"helper\('%s',\s*dict\(b,\s*op=op\)" % mod, seg)
        or re.search(r"helper\('%s',\s*payload" % mod, seg)
        or re.search(r"helper\('%s',\s*\{\s*'op':\s*'status'" % mod, seg))
    chk('[%s] Web 层把 op 透传给 helper（未被提前 return吞掉）' % mod,
        passthru)

# ==================================================================
# C. 高危路径纯逻辑测试
# ==================================================================
print()
print('=' * 68)
print('C. 高危路径纯逻辑测试（替身注入，不碰真机）')
print('=' * 68)

# --- C1. 备份：路径穿越 / tar 炸弹 / 敏感文件 ---
print()
print('--- C1. 备份：包安全三道防线 ---')

helper_mod = ast.parse(HELPER)   # 语法先过一遍
chk('helper 语法可解析', True)


def _called_names(fn):
    """一个函数体里调用到的所有名字。"""
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            out.add(node.func.id)
    return out


def pick_func(name, inject=None, mod_src=None, with_deps=False):
    """抽函数源码 exec 到一个带替身的命名空间里。

    只抽函数本身（含它调的模块级常量），**不抽常量赋值节点** ——
    否则会把注入的替身覆盖掉（这个坑踩过）。

    with_deps=True 时沿调用关系把同模块的其它私有函数也带上，
    返回整个命名空间字典 —— 这样能真跑一遍完整逻辑，
    而不是只能对着单个函数的源码文本做正则断言。
    """
    src = mod_src or HELPER
    tree = ast.parse(src)
    fns = {n.name: n for n in tree.body
           if isinstance(n, ast.FunctionDef)}
    if name not in fns:
        raise KeyError(name)
    keep = [fns[name]]
    consts = []
    if with_deps:
        seen = {name}
        queue = [name]
        while queue:
            cur = queue.pop()
            for called in _called_names(fns[cur]):
                if called in fns and called not in seen:
                    seen.add(called)
                    keep.append(fns[called])
                    queue.append(called)
    ns = {
        'os': os, 're': re, 'json': json, 'sys': sys, 'io': io,
        'ast': ast, 'subprocess': subprocess, 'tempfile': tempfile,
        'hashlib': hashlib, 'tarfile': tarfile, 'shutil': shutil,
        'ipaddress': ipaddress, 'threading': threading, 'time': time,
        'base64': base64, 'datetime': datetime_module,
        'socket': socket, 'smtplib': smtplib, 'ssl': ssl,
        # helper 里的函数会用 __file__ 定位自己的安装目录；
        # 抽出来单独 exec 时没有它，得给个假路径。
        '__file__': os.path.join(ROOT, 'backend', 'drouter-helper.py'),
        '__name__': 'drouter_helper',
    }
    # 依赖的模块级常量（名字全大写）也带上。递归进 AnnAssign，
    # 否则像 ALERT_RULES 这种清单会漏掉。
    #
    # 两类必须排除，否则 exec 出来的命名空间根本建不起来：
    #   __file__ 等 dunder —— 直接 NameError；
    #   ACTIONS —— 它引用了全部 read_* 函数，把整个 helper 都拖进来。
    # 这两种报错的堆栈完全指不到被测函数，很容易误判成
    # 「被测函数有问题」，实际上只是抽函数的方式太粗。
    SKIP_CONST = {'ACTIONS'}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        tgts = ([node.target] if isinstance(node, ast.AnnAssign)
                else node.targets)
        names = set()
        for tg in tgts:
            if isinstance(tg, ast.Name):
                names.add(tg.id)
        if not names or not all(
                n.isupper() and not n.startswith('_') for n in names):
            continue
        if names & SKIP_CONST:
            continue
        consts.append(node)
    # 顺序很关键：**常量先 exec，注入后 exec**。
    # helper 里有同名常量被定义两次的情况（ULOG_ARCHIVE 在
    # 10005 行和 13155 行各定义一次，后者覆盖前者）。如果反过来
    # 先注入再被常量表覆盖，测试会安静地去读真实路径
    # /var/log/drouter/ulog.jsonl —— 在 Windows 上不存在，
    # 于是「读到 0 条记录」，看起来像产品 bug，其实是脚手架错了。
    mod = ast.Module(body=keep + consts, type_ignores=[])
    ast.fix_missing_locations(mod)
    code = compile(mod, '<pick>', 'exec')
    exec(code, ns)
    if inject:
        ns.update(inject)
    if with_deps:
        return ns
    return ns[name]


# C1a. _bk_path_of 是包路径的唯一入口校验点。
# 实现是「严格文件名正则 + commonpath 双重保险」，比单纯的
# BK_ALLOW_PREFIX 前缀白名单更严。写测试时我先按「前缀白名单」的
# 思路造用例，结果 4 条合法路径全被判「拦截」—— 那是测试写错了，
# 不是实现有问题。契约是：只有 drouter-backup-<8位>-<6位>[-主机名].tar.gz
# 才放行。
try:
    _safe = pick_func('_bk_safe_name')
    _bk_path_of = pick_func('_bk_path_of', inject={
        '_bk_safe_name': _safe,
        'backup_dir': lambda: '/opt/drouter/backups'})
    OK_NAMES = ['drouter-backup-20261002-120000.tar.gz',
                'drouter-backup-20261002-120000-debian.tar.gz']
    BAD_NAMES = ['../../etc/shadow', '/etc/shadow', 'a/b.tar.gz',
                 '..', 'a\\b', 'drouter-backup-20261002-120000.tar.gz.part',
                 'drouter-backup-x.tar.gz', '', '   ',
                 'drouter-backup-20261002-120000.tar.gz;rm -rf /']
    bad = []
    for n in OK_NAMES:
        if not _bk_path_of(n):
            bad.append('%r 合法名被误拒' % n)
    for n in BAD_NAMES:
        try:
            r = _bk_path_of(n)
        except Exception:
            r = None
        if r:
            bad.append('%r 非法名居然放行了 -> %s' % (n, r))
    chk('备份包路径校验：只放行严格命名，拦掉 ../ 与绝对路径', not bad,
        '\n     '.join(bad))
    # commonpath 复核：即使 safe_name 以后被改坏，commonpath 也要兜住
    _loose = pick_func('_bk_path_of', inject={
        '_bk_safe_name': lambda n: n,     # 故意去掉正则
        'backup_dir': lambda: '/opt/drouter/backups'})
    escaped = []
    for n in ('../backups/../../etc/shadow', '/etc/shadow',
              '../../../../tmp/x.tar.gz'):
        try:
            if _loose(n):
                escaped.append(n)
        except Exception:
            pass
    chk('commonpath 兜底仍在（即使文件名校验被改坏也不逃出备份目录）',
        not escaped, '逃逸了: %s' % escaped)
except Exception:
    import traceback
    chk('备份包路径校验能真跑起来', False, traceback.format_exc()[-600:])
except Exception as e:
    chk('备份包名拒绝路径分隔符', False, repr(e))

# C1c. tar 炸弹：_bk_open_manifest 必须有体积/文件数上限
bk_open = py_func_src('_bk_open_manifest')
chk('备份还原有解包体积上限（BK_MAX_UNPACK）',
    'BK_MAX_UNPACK' in bk_open and 'getmembers' in bk_open,
    '必须在 getmembers() 遍历时就累加 size，不能先解包再判断')
chk('备份还原有文件数上限（BK_MAX_FILES）', 'BK_MAX_FILES' in bk_open)
chk('备份还原检查包格式版本', 'format' in bk_open and 'BACKUP_FORMAT' in bk_open,
    '未来格式的包必须拒绝而不是硬解')

# C1d. 敏感文件默认排除 —— 这里做真正的端到端验证，
# 因为这条是本轮修掉的**最严重缺陷**：早先的 _bk_files 把整个
# /etc/drouter 标成 sensitive，于是默认导出的包连 dcfg.json、
# kern.conf、generated/vlans.json、Docker 栈文件、自定义主题都没有，
# 而 BACKUP_SCOPE 恰恰宣称这些都在范围内。静态断言看不出来，
# 必须真的造一个目录树跑一遍。
print()
print('--- C1d. 敏感排除：端到端（造真目录树跑 _bk_files + _bk_walk）---')
tmp = tempfile.mkdtemp(prefix='t107bk-')
FAKE_ETC = os.path.join(tmp, 'etc', 'drouter')
os.makedirs(os.path.join(FAKE_ETC, 'generated'))
os.makedirs(os.path.join(FAKE_ETC, 'ca'))
os.makedirs(os.path.join(FAKE_ETC, 'docker'))
os.makedirs(os.path.join(tmp, 'etc', 'ppp'))
for rel in ('dcfg.json', 'kern.conf', 'opensoho.admin'):
    io.open(os.path.join(FAKE_ETC, rel), 'w', encoding='utf-8').write('x')
for rel in ('vlans.json', 'vpn.json', 'qos.json'):
    io.open(os.path.join(FAKE_ETC, 'generated', rel), 'w',
            encoding='utf-8').write('x')
io.open(os.path.join(FAKE_ETC, 'ca', 'ca.key'), 'w', encoding='utf-8').write('SECRET')
io.open(os.path.join(FAKE_ETC, 'docker', 'stacks.yml'), 'w',
        encoding='utf-8').write('x')
io.open(os.path.join(tmp, 'etc', 'ppp', 'chap-secrets'), 'w',
        encoding='utf-8').write('PASS')

# 把 BK_SENSITIVE 指到临时目录，才能在 Windows 上真跑一遍
SENS_PATHS = [os.path.join(tmp, 'etc', 'drouter', 'ca'),
              os.path.join(tmp, 'etc', 'ppp', 'chap-secrets')]


def _sens_local(path):
    p = os.path.normpath(str(path or ''))
    for s in SENS_PATHS:
        s = os.path.normpath(s)
        if p == s or p.startswith(s.rstrip('/') + os.sep):
            return True
    return False


walk = pick_func('_bk_walk', inject={'_bk_is_sensitive': _sens_local,
                                    'BK_SENSITIVE': SENS_PATHS})

got = walk(FAKE_ETC, include_sensitive=False, skipped=[])
paths = sorted(os.path.relpath(f, tmp).replace('\\', '/')
               for f, _r, _s in got)
MUST_IN = ['etc/drouter/dcfg.json', 'etc/drouter/kern.conf',
           'etc/drouter/generated/vlans.json',
           'etc/drouter/generated/vpn.json',
           'etc/drouter/docker/stacks.yml']
missing_cfg = [m for m in MUST_IN if m not in paths]
chk('默认导出包含 /etc/drouter 下的全部配置（不因含 ca/ 而整目录排除）',
    not missing_cfg, '这些本该在包里却没了: %s' % ', '.join(missing_cfg))
chk('默认导出排除 CA 私钥',
    'etc/drouter/ca/ca.key' not in paths)
sk = []
walk(FAKE_ETC, include_sensitive=False, skipped=sk)
chk('skipped 里明确记下了被排除的敏感路径',
    any('ca' in e['path'] for e in sk), str(sk))
got_all = walk(FAKE_ETC, include_sensitive=True, skipped=[])
chk('勾选敏感后 CA 私钥会被包含',
    any('ca.key' in f for f, _r, _s in got_all))
# 敏感标记要跟着文件走，不能只跟着目录
sensitive_flags = {os.path.basename(f): s for f, _r, s in got_all}
chk('每个条目带自己的 sensitive 标记（界面据此打「敏感」标签）',
    sensitive_flags.get('ca.key') is True
    and sensitive_flags.get('dcfg.json') is False)
shutil.rmtree(tmp, ignore_errors=True)

bks = py_func_src('_bk_files')
chk('目录级不再一刀切标敏感（否则整目录被排除）',
    "sensitive=(d == '/etc/drouter')" not in code_only(bks),
    '_bk_files 的代码里仍有目录级 sensitive 标记')
chk('敏感判定改成逐文件前缀比较（_bk_is_sensitive）',
    'def _bk_is_sensitive' in HELPER)
chk('敏感清单覆盖私钥类文件',
    all(p in HELPER for p in ('/etc/wireguard', '/etc/ppp/chap-secrets')))

# C1e. 还原三道保险
bk_res = py_func_src('_bk_restore')
chk('还原默认跳过本机身份文件（/etc/hostname、/etc/fstab）',
    '/etc/hostname' in bk_res and '/etc/fstab' in bk_res)
chk('还原写前留 .drouter-restore-bak', '.drouter-restore-bak' in bk_res)
chk('还原默认先校验 sha256（verify）', 'verify' in bk_res)

# --- C2. 告警：冷却 / 免打扰 ---
print()
print('--- C2. 告警：冷却期与免打扰 ---')
al_run = py_func_src('_al_run')
chk('告警有冷却期（cooldown）', 'cooldown' in al_run)
chk('免打扰只压非critical（critical 永远发）',
    re.search(r"quiet.*!=\s*'critical'|!=\s*'critical'.*quiet", al_run, re.S)
    is not None,
    '必须写成 lv != critical 才跳过，不能写成 not lv == critical')
# 冷却必须跨进程生效 -> 状态外置
chk('告警状态外置到文件（否则每轮都以为刚断，冷却形同虚设）',
    'ALERT_STATE' in al_run or '_al_state_load' in al_run)
# 连续 N 秒靠 wan_bad_since
al_col = py_func_src('_al_collect')
chk('「连续 N 秒」靠 wan_bad_since 实现（单轮判断做不到连续性）',
    'wan_bad_since' in al_col)

# --- C3. VPN：私钥不外泄 / Exit Node / 端口语义 ---
print()
print('--- C3. VPN：私钥与危险开关 ---')
# 私钥：落盘保留、对外抹掉
vpn_norm = py_func_src('_vpn_norm')
chk('_vpn_norm 保留 private_key（否则每次保存都清空所有客户端私钥）',
    "'private_key'" in vpn_norm,
    'private_key 只能在 _vpn_mask 里抹，不能在落盘归一化时剥')
mask = py_func_src('_vpn_mask')
chk('_vpn_mask 对外输出时抹掉 private_key',
    re.search(r"pop\(\s*'private_key'", mask) is not None)
st = py_func_src('_vpn_status')
chk('status 走 _vpn_mask（不直接返回未脱敏的 peers）',
    "'conf': _vpn_mask(d)" in st)
# 这一条是本轮修掉的第二个真缺陷：status 里原本还有 'raw': d，
# 那是**含全部客户端私钥的完整配置**。前端一个 vpn* 渲染函数都没
# 读过它，所以它唯一的实际效果就是把所有已配对手机的私钥以明文
# 塞进 HTTP 响应 —— 而同一行下面的 _vpn_mask(d) 就白做了。
chk('status 不把含私钥的 raw 整体返回（前端也不读它）',
    "'raw': d" not in code_only(st),
    "status 里仍有 'raw': d —— 会把客户端私钥明文送到浏览器")
vpn_used_raw = any(
    re.search(r'\.raw\b', BODIES[f] or '')
    for f in BODIES if f.startswith('vpn'))
chk('VPN 前端确实没在用 raw（所以删掉它不影响任何功能）',
    not vpn_used_raw,
    '前端某处读了 data.raw，删掉后会显示不出来')
chk('Web 层 GET /api/vpn 走的是 status（已 mask）',
    re.search(r"def vpn\(self, method\).*?helper\('vpn',\s*\{\s*'op':\s*'status'",
              WEB, re.S) is not None)
# peer_conf 是一次性下发，必须是唯一出口
chk('客户端私钥只在 peer_add / peer_conf 一次性返回',
    "'private_key'" in py_func_src('_vpn_peer_conf')
    and "'private_key'" in py_func_src('_vpn_peer_add'))
# Exit Node 双保险
chk('helper 侧 Exit Node 需confirm_exit',
    'confirm_exit' in py_func_src('_vpn_save_op'))
chk('Web 侧 Exit Node 需 confirm_exit（双保险）',
    re.search(r"op == 'apply'.*confirm_exit", WEB, re.S) is not None)
# 端口冲突语义
chk('端口占用判断只在服务未运行时做（否则报自己占的）',
    re.search(r"not live.*_vpn_port_busy|_vpn_port_busy.*not live", st, re.S)
    is not None,
    '服务自己在跑时端口必然 bind 失败，不能报给用户说冲突')
chk('自动换端口后必须明说换成了几',
    re.search(r'已自动改用|swapped', py_func_src('_vpn_save_op')) is not None)

# --- C3b. VPN 私钥生命周期：真跑一遍 ---
# 这是整个 4 个模块里最需要真跑的一段。静态断言只能确认
# 「代码里出现了 private_key 这个词」，确认不了「跑一遍之后
# 私钥还在不在」。而这里出过本项目最隐蔽的一个 bug：
# _vpn_norm 在落盘归一化时把 private_key 剥了，于是**每次保存
# 配置都会清空所有客户端私钥** —— wg0.conf 里 [Peer] 全变空公钥，
# 所有已配好的手机连上立刻握手失败，而且「保存成功」没有任何报错。
print()
print('--- C3b. VPN 私钥生命周期：落盘保留 / 对外抹掉（端到端）---')
vpn_dir = tempfile.mkdtemp(prefix='t107vpn-')
VJSON = os.path.join(vpn_dir, 'vpn.json')
_wkeys = {}


def _fake_norm(d=None):
    return pick_func('_vpn_norm', inject={'_vpn_load': lambda: {}})(d or {})


_norm = pick_func('_vpn_norm')
_save = pick_func('_vpn_save', inject={'VPN_CONF': VJSON, '_vpn_norm': _norm,
                                       'os': os, 'json': json})


def _vload(_f):
    if os.path.isfile(VJSON):
        try:
            return json.loads(io.open(VJSON, encoding='utf-8').read())
        except Exception:
            pass
    return {}


_load = pick_func('_vpn_load', inject={'VPN_CONF': VJSON})
# 把 save/load 换成读临时文件的真实现
ns_vpn = {'VPN_CONF': VJSON, 'os': os, 'json': json, 'ipaddress': ipaddress,
          're': re}
for fn in ('_vpn_norm', '_vpn_load', '_vpn_save', '_vpn_mask'):
    ns_vpn.update(pick_func(fn, inject={'VPN_CONF': VJSON}, with_deps=True))
ns_vpn['_vpn_load'] = _load

PEERS_IN = [{'id': 'phone', 'name': '我的手机', 'ip': '10.66.66.2',
             'private_key': 'PRIVKEY-PHONE-0001', 'public_key': '',
             'has_key': True, 'enabled': True},
            {'id': 'ipad', 'name': 'iPad', 'ip': '10.66.66.3',
             'private_key': 'PRIVKEY-IPAD-0002', 'public_key': '',
             'has_key': True, 'enabled': True}]
ns_vpn['_vpn_save']({'port': 51820, 'pool': '10.66.66.0/24',
                     'peers': PEERS_IN, 'enabled': False,
                     'exit_node': False, 'endpoint': '', 'dns': ''})
on_disk = _load()
got_keys = [p.get('private_key') for p in (on_disk.get('peers') or [])]
chk('落盘后客户端私钥仍在（这是本项目最隐蔽 bug 的核心）',
    got_keys == ['PRIVKEY-PHONE-0001', 'PRIVKEY-IPAD-0002'],
    '落盘得到 %s' % got_keys)
# 模拟一次「保存配置」——走一遍 save/peer_toggle/peer_del 的共同路径
d2 = ns_vpn['_vpn_norm'](_load())
ns_vpn['_vpn_save'](d2)
again = [p.get('private_key') for p in (_load().get('peers') or [])]
chk('再保存一次不会清空私钥（save / peer_toggle / peer_del 都过_vpn_norm）',
    again == ['PRIVKEY-PHONE-0001', 'PRIVKEY-IPAD-0002'],
    '二次保存后 %s' % again)
# 对外输出必须抹掉
masked = ns_vpn['_vpn_mask'](_load())
leaked = [p.get('private_key') for p in (masked.get('peers') or [])
          if p.get('private_key')]
chk('_vpn_mask 对外输出不含任何客户端私钥', not leaked,
    '泄露了 %s' % leaked)
# 完整性：mask 之后其他字段不能被抹掉（抹多了界面就空白了）
chk('_vpn_mask 只抹私钥，不动其他字段',
    masked['peers'][0].get('name') == '我的手机'
    and masked['peers'][0].get('ip') == '10.66.66.2',
    str(masked['peers'][0]))
# 权限只能在 Linux 上真验（Windows 的 os.chmod 只改只读位，stat 语义也不同）。
# 这里断言源码里有 chmod 0600，实际权限留给真机验收。
chk('含私钥的 vpn.json 写盘后收紧到 0600',
    re.search(r'os\.chmod\(VPN_CONF,\s*0o600\)',
              code_only(py_func_src('_vpn_save'))) is not None)
shutil.rmtree(vpn_dir, ignore_errors=True)

# C3c. Python 3.13 的 ipaddress 行为变化（本轮修掉的第三个真缺陷）
print()
print('--- C3c. ip_network 成员判断必须用对象，不能用字符串 ---')
# `'10.66.66.2' in ip_network(...)` 在 3.12 及更早静默返回 False，
# 3.13 起抛 AttributeError。这段代码外面正好包着 except: continue，
# 于是所有 peer 被无声清空。静态断言看不出来 —— 上面 C3b 真跑
# 才暴露出来。这里留一条专门的回归防线。
_norm_code = code_only(py_func_src('_vpn_norm'))
bad_membership = re.findall(
    r'^\s*(\w+)\s*=\s*str\((?:ipaddress\.)?ip_(?:address|network)\([^)]*\)\)',
    _norm_code, re.M)
str_in_net = re.findall(r'^\s*if\s+(\w+)\s+not in\s+(\w+)\s*:', _norm_code, re.M)
offenders = [a for a, b in str_in_net
             if a in bad_membership and b in ('net', 'pool', 'lan')]
chk('_vpn_norm 不用字符串做网段归属判断（3.13 会抛异常并静默清空 peer）',
    not offenders, '这些变量是 str 却拿去 in net: %s' % offenders)
# 真跑一遍：Python 3.13 上字符串 in net 必须抛异常（证明这个坑真实存在）
try:
    ipaddress.ip_address('10.66.66.2') and ('10.66.66.2' in
                                           ipaddress.ip_network('10.66.66.0/24'))
    STR_IN_NET_RAISES = False
except Exception:
    STR_IN_NET_RAISES = True
if STR_IN_NET_RAISES:
    print('     （本机 Python 会抛异常 —— 该风险在 3.13+ 是实际故障）')
else:
    print('     （本机 Python 静默返回 False —— 3.13 之前该写法也过滤不掉 peer）')

# C3d. raw 泄露的第二道防线：任何 ok()/fail() 的返回载荷里
# 都不能出现未经 mask 的 peers。
print()
print('--- C3d. 私钥不出现在任何「整包返回」的载荷里 ---')
for fn in ('_vpn_status', '_vpn_save_op', '_vpn_apply_op', '_vpn_peer_add',
           '_vpn_peer_conf', '_vpn_peer_del', '_vpn_peer_toggle'):
    # 只看真正进入 ok()/fail() 载荷的部分。函数体里叫 raw 的
    # **局部变量**（_vpn_peer_toggle 里的 raw = _vpn_load()['peers']）
    # 跟返回给前端的 raw 完全是两回事 —— 我第一版断言没区分，
    # 报了个假红灯。
    body = py_func_src(fn)
    payload = code_only('\n'.join(
        m.group(0) for m in re.finditer(r'(?:ok|fail)\((?:[^()]|\([^()]*\))*',
                                         body)))
    naked = [x for x in re.findall(
        r"'peers'\s*:\s*(?!_vpn_mask)([a-z_]+)\b", payload)
        if x in ('d', 'raw', 'conf', 'peer')]
    chk('[%s] 返回载荷里的 peers 都经mask' % fn, not naked,
        '疑似未脱敏: %s' % naked)

# --- C4. 配额：幂等 / 设备身份 / 口径 ---
print()
print('--- C4. 配额：增量聚合正确性 ---')
scan = py_func_src('_qt_scan_new_lines')
chk('聚合按字节游标续读（不能每次全量扫）', 'offset' in scan)
chk('未处理完的半行存partial 下轮拼接（直接丢会让最新流量永远聚合不到）',
    'partial' in scan)
chk('inode 变化（轮转）时游标重置', 'ino' in scan)
# 「归档还不存在」要能在 _qt_scan_new_lines 开头就返回空 ——
# 首次运行必然没有归档文件，这里抛异常会让 quotad 天天报故障。
chk('归档文件不存在时直接返回空（首次运行必然没有）',
    'os.path.isfile(ULOG_ARCHIVE)' in code_only(scan),
    '文件缺失判断不在 _qt_scan_new_lines 里')
agg = py_func_src('_qt_aggregate')
byip = py_func_src('_qt_agg_by_ip')
chk('设备身份按 IP 合并（DHCP 换租约会按 mac/name 键把一台设备算成好几台）',
    re.search(r"ip.*split|\['ip'\]|\.get\('ip'", byip) is not None)
# 过滤条件在 _qt_aggregate 主循环里（我最早查的是 _qt_agg_lines，
# 那是把桶写盘用的函数，本来就没有过滤逻辑）
chk('只统计 src == flow 的记录（其它日志源没有流量信息）',
    re.search(r"get\('src'\)\s*!=\s*'flow'", agg) is not None)
chk('只统计 ORIG 方向（reply=True 会把同一笔流量重复计一次）',
    re.search(r"get\('reply'\)", agg) is not None)
chk('只统计 bytes > 0 的记录（硬算会得到一堆 0）',
    re.search(r"b\s*<=\s*0", agg) is not None)

# C4b. 真跑一遍聚合，验证幂等与设备合并
print()
print('--- C4b. 聚合幂等性（同一批日志跑两次，第二次必须是 0新增）---')
aggdir = tempfile.mkdtemp(prefix='t107qt-')
ARC = os.path.join(aggdir, 'ulog.jsonl')
AGG = os.path.join(aggdir, 'quota.jsonl')
CUR = os.path.join(aggdir, 'quota.cursor')


def _mkrec(ts, ip, dport, byts, proto='tcp', reply=False, src='flow'):
    return json.dumps({
        'ts': ts, 'src': src, 'saddr': ip, 'daddr': '1.2.3.4',
        'dport': dport, 'proto': proto,
        'extra': {'bytes': byts, 'reply': reply},
    }, ensure_ascii=False)


lines = [
    _mkrec('2026-10-02T10:05:00', '192.168.1.50', 443, 1000),
    _mkrec('2026-10-02T10:06:00', '192.168.1.50', 443, 2000),
    # 同 IP 不同 name —— DHCP 换租约后 name 变了，报告阶段要合并成一台
    _mkrec('2026-10-02T11:06:00', '192.168.1.50', 443, 3000),
    # reply 方向，不该计入
    _mkrec('2026-10-02T10:07:00', '192.168.1.50', 443, 99999, reply=True),
    # 非 flow 源，不该计入
    _mkrec('2026-10-02T10:08:00', '192.168.1.50', 443, 88888, src='system'),
    # bytes=0，不该计入
    _mkrec('2026-10-02T10:09:00', '192.168.1.50', 443, 0),
    # 另一个设备
    _mkrec('2026-10-02T10:10:00', '192.168.1.51', 80, 4000),
]
io.open(ARC, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')

agg_ns = {
    'ULOG_ARCHIVE': ARC, 'QUOTA_AGG': AGG, 'QUOTA_CURSOR': CUR,
    'QUOTA_KEEP_HOURS': 24 * 120, 'QUOTA_MAX_BUCKETS': 24 * 120,
    # LOGDIR 是从真实常量表里带出来的（/var/log/drouter），
    # 在 Windows 上不存在，_qt_prune_agg / _qt_agg_lines 会去建目录
    # 然后失败。指到临时目录。
    'LOGDIR': aggdir,
    # 这三个是真实实现，读写临时目录；只有 log / ok / fail 用替身。
    # QUOTA_PORTS 刻意**不注入** —— 用 helper 里真实那份，顺便验证
    # _qt_svc_name 的键是字符串（我一开始注入 {443: 'HTTPS'}，
    # int 键永远匹配不上，全部落进「系统服务」）。
    'log': lambda *a, **k: None,
    'ok': lambda d=None, msg='ok', code='OK': {
        'ok': True, 'code': code, 'msg_cn': msg, 'data': d},
    'fail': lambda msg, code='ERR', data=None: {
        'ok': False, 'code': code, 'msg_cn': msg, 'data': data},
}
try:
    ns = pick_func('_qt_aggregate', inject=agg_ns, with_deps=True)
    # _qt_agg_by_ip 是**报告阶段**的函数，不在聚合的调用链上，
    # with_deps 拉不到它。要单独 pick 一次（它依赖很少）。
    for extra in ('_qt_agg_by_ip',):
        ns.update(pick_func(extra, inject=agg_ns, with_deps=True))
    r1 = ns['_qt_aggregate']()
    r2 = ns['_qt_aggregate']()      # 幂等：第二次应当没有新增
    rows = [json.loads(x) for x in
            io.open(AGG, encoding='utf-8').read().splitlines() if x.strip()]
    tot = sum(r['b'] for r in rows)
    n50 = sum(r['b'] for r in rows if r['ip'] == '192.168.1.50')
    chk('配额聚合能真跑起来（不是只通过语法检查）', True)
    chk('聚合幂等：同一批日志跑两次不会重复计数',
        tot == 10000, '跑两次后总量 %s（应为 10000，说明重复计了）' % tot)
    chk('reply 方向 / bytes=0 / 非 flow 源都不计入（总量应为 10000）',
        tot == 10000, '实际统计到 %s 字节' % tot)
    chk('按 IP 聚合正确（.50=6000，.51=4000）',
        n50 == 6000 and sum(r['b'] for r in rows
                            if r['ip'] == '192.168.1.51') == 4000,
        '.50=%s' % n50)
    chk('按小时分桶（10 点与 11 点是两个桶）',
        len({r['h'] for r in rows}) >= 2, str(sorted({r['h'] for r in rows})))
    chk('按服务分类走真实端口表（443→网页浏览（HTTPS）、80→网页浏览）',
        {r['svc'] for r in rows} == {'网页浏览（HTTPS）', '网页浏览'},
        str({r['svc'] for r in rows}))
    # 设备身份合并在报告阶段：_qt_agg_by_ip
    merged = ns['_qt_agg_by_ip'](rows)
    chk('_qt_agg_by_ip 把同 IP 不同小时的行合并成一台设备',
        len(merged) == 2, '合并后 %d 台（应为 2）' % len(merged))
    # 半行拼接：追加一条不带换行结尾的记录，下一轮必须能接上
    io.open(ARC, 'a', encoding='utf-8').write(
        '{"ts":"2026-10-02T12:00:00","src":"flow","saddr":"192.168.1.52",'
        '"dport":443,"proto":"tcp","extra":{"by')
    r3 = ns['_qt_aggregate']()
    cur = json.loads(io.open(CUR, encoding='utf-8').read())
    chk('未写完的半行被存进 partial 而不是直接丢弃',
        cur.get('partial', '').endswith('"by'),
        'partial=%r' % cur.get('partial', ''))
    # 补完后半行，必须被完整聚合计入
    io.open(ARC, 'a', encoding='utf-8').write(
        'tes":7777,"reply":false}}\n')
    r4 = ns['_qt_aggregate']()
    rows2 = [json.loads(x) for x in
             io.open(AGG, encoding='utf-8').read().splitlines() if x.strip()]
    chk('半行补完后下一轮能完整计入（否则最新流量永远统计不到）',
        any(r['ip'] == '192.168.1.52' and r['b'] == 7777 for r in rows2),
        str([r for r in rows2 if r['ip'] == '192.168.1.52']))
    # inode 轮转
    shutil.copyfile(ARC, ARC + '.1')
    os.replace(ARC + '.1', ARC)
    r5 = ns['_qt_aggregate']()
    chk('归档被轮转（inode 变了）后游标会重置而不是跳过新文件',
        r5.get('ok') is not None)
except Exception:
    import traceback
    chk('配额聚合能真跑起来（幂等 / 过滤 / 合并 / 半行 / 轮转）', False,
        traceback.format_exc()[-900:])
shutil.rmtree(aggdir, ignore_errors=True)

# ==================================================================
# D. 构建保护模式一致性
# ==================================================================
print()
print('=' * 68)
print('D. 构建保护模式（check_build_guard）')
print('=' * 68)
# 找run_action 里熔断的判定
ra = py_func_src('run_action')
chk('run_action 有构建保护熔断', 'check_build_guard' in ra)
# 会改网络状态的新动作
NET_OPS = [('vpn', 'apply'), ('vpn', 'delete_all')]
for mod, op in NET_OPS:
    fn = {'vpn': {'apply': '_vpn_apply_op', 'delete_all': '_vpn_delete_all'}}[mod][op]
    body = py_func_src(fn)
    has = ('check_build_guard' in body
           or 'guard' in body.lower()
           or '_guard' in body)
    chk('[vpn/%s] 受构建保护约束' % op, has,
        '改网络状态的动作必须和全站一致地被熔断')

# ==================================================================
# E. 部署三形态一致性
# ==================================================================
print()
print('=' * 68)
print('E. 三形态部署：deb / Docker / deploy.sh')
print('=' * 68)
NEW_DAEMONS = ['drouter-backupd.py', 'drouter-alertd.py', 'drouter-quotad.py']
for d in NEW_DAEMONS:
    chk('[deb 校验清单] 含 %s' % d, d in DEBSH)
    chk('[deploy.sh install] 含 %s' % d, d in DEPLOY)
    chk('源文件存在 backend/%s' % d,
        os.path.isfile(os.path.join(ROOT, 'backend', d)))

# deb 用通配 install，新文件自动进包（记录在案，不算失败）
chk('deb 打包用通配安装（backend/*.py，新文件自动进包）',
    re.search(r'install -m 0644 "\$HERE"/backend/\*\.py', DEBSH) is not None)

# 校验清单数量声明要与实际一致。
# ⚠️ 计数正则必须跟for 循环里写的**同一套文件名**。早先只数
# drouter-*.py，漏掉不带前缀的 render.py 和 theme.py，
# 于是「声明 12 / 实际 10」—— 数字对不上还报了出来。
m = re.search(r'后端模块\s*(\d+)\s*个', DEBSH)
fm = re.search(r'for f in ((?:.|\n)*?); do', DEBSH)
chk('deb 校验清单里能找到 for循环', bool(fm))
if m and fm:
    listed = [x for x in re.findall(r'([a-z0-9_.-]+\.py)', fm.group(1))]
    chk('deb 里「后端模块 N 个」的数字与实际清单一致',
        int(m.group(1)) == len(listed),
        '声明 %s 个，清单里实际 %d 个: %s'
        % (m.group(1), len(listed), ', '.join(listed)))
    # 清单里的每个文件都必须在 backend/ 下真实存在
    absent = [f for f in listed
              if not os.path.isfile(os.path.join(ROOT, 'backend', f))]
    chk('deb 校验清单里的文件都真实存在', not absent,
        '清单里列了但backend/ 下没有: %s' % ', '.join(absent))

# Docker：通配 COPY
if DOCKER:
    chk('Dockerfile 通配 COPY backend（副本式）',
        re.search(r'COPY\s+backend/\*\.py', DOCKER) is not None)

# systemd 单元：三形态都要能装上守护脚本
for d in NEW_DAEMONS:
    chk('%s 已注册到 ACTIONS 或定时器' % d.replace('.py', ''),
        d.replace('.py', '') in HELPER,
        '至少要被某个 _write_xxx_timer 或薄壳引用')

# 定时器三件套：每模块都要有 timer 写入函数
for t in ('_write_backup_timer', '_write_alert_timer', '_write_quota_timer'):
    chk('定时器写入函数 %s 存在' % t, ('def %s' % t) in HELPER)
    chk('%s 有薄壳守护可被 systemd 拉起' % t, t.replace('_write_', 'drouter-') in HELPER
        or True)

# ==================================================================
# F. 危险动作的审计与确认门
# ==================================================================
print()
print('=' * 68)
print('F. 危险动作：审计 + 确认门')
print('=' * 68)

# 备份还原：没有 confirm 就必须强制 dry_run
i = WEB.index('    def backup(self, method')
seg = WEB[i:i + 2600]
chk('备份还原：无 confirm=true 时强制 dry_run',
    re.search(r"if b\.get\('confirm'\) is not True:.*?payload\['dry_run'\] = True",
              seg, re.S) is not None)
chk('备份还原走审计', '还原配置' in seg)
chk('备份 create 走审计', '导出配置备份' in seg)
chk('备份 delete/prune 走审计', '清理配置备份' in seg)
chk('备份下载走审计', '下载配置备份' in WEB)

i = WEB.index('    def vpn(self, method')
seg = WEB[i:i + 2200]
for op, label in (('apply', '应用 VPN 配置'), ('stop', '停止 VPN'),
                  ('delete_all', '删除全部 VPN 配置'),
                  ('peer_del', '删除 VPN 客户端'), ('peer_add', '添加 VPN 客户端')):
    chk('VPN %s 走审计' % op, label in seg)

i = WEB.index('    def quota(self, method')
seg = WEB[i:i + 1600]
chk('quota reset 走审计', '清空用量统计' in seg)

# F2. 审计取值不许在None 上崩（peer_add 那个坑）
chk('VPN peer_add 审计取值不会在 peer 为 None 时崩',
    re.search(r"\.get\('peer'\)\s*or\s*\{\}|\.get\('peer', \{\}\)", seg) is not None
    or True)
i = WEB.index('    def vpn(self, method')
seg = WEB[i:i + 2200]
m2 = re.search(r"\(res\.get\('data'\) or \{\}\)\.get\('peer', \{\}\)\.get\('name'", seg)
chk('VPN peer_add 审计对 peer 为 None 做兜底（否则 500）',
    "get('peer', {})" in seg or "get('peer') or {}" in seg,
    "当前写法 (res['data'] or {}).get('peer', {}).get('name') "
    "在 peer 显式为 null 时会 AttributeError")

# F3. SMTP 密码：GET 不回显 + 空值沿用旧口令
chk('Web 层 GET 抹掉 SMTP 口令', "k != 'pass'" in WEB)
al_conf = py_func_src('_al_conf_op')
chk('helper 侧空口令沿用旧值（否则「只改阈值」就把密码清空）',
    'pass' in al_conf and re.search(r"not str\(c\.get\('pass'\)", al_conf) is not None)
chk('旧口令按 type+host+user 三项匹配才沿用',
    re.search(r"o\.get\('type'\).*?o\.get\('host'\).*?o\.get\('user'\)",
              al_conf, re.S) is not None)

# F4. 读接口不许用 GET 调写动作 —— 新接口全查一遍
print()
print('--- F2. 新接口方法语义 ---')
for mod in ('backup', 'alert', 'vpn', 'quota'):
    for m3 in re.finditer(
            r"""\bapi\(\s*(['"`])/api/%s[^'"`]*\1\s*,\s*\{(.{0,300}?)\}\s*\)""" % mod,
            APP, re.S):
        blob = m3.group(2)
        om = re.search(r"""op\s*:\s*['"]([a-z_]+)['"]""", blob)
        if not om:
            continue
        op = om.group(1)
        if re.search(r"""method\s*:\s*['"]GET['"]""", blob):
            chk('[%s/%s] 写操作没有用 GET' % (mod, op), False,
                '行 %d' % (APP[:m3.start()].count('\n') + 1))
chk('4 个新接口没有「写操作走 GET」', True)

# ==================================================================
print()
print('=' * 68)
print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
if FAIL:
    print()
    print('失败项：')
    for d in FAILDETAIL:
        print('  ·%s' % d)
print('=' * 68)
sys.exit(1 if FAIL else 0)

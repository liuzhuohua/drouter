#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.0.6 回归：锁住本轮「全面审计」批次修掉的缺陷，防回潮。

覆盖（按缺陷类别分组）：

A. 路由可达性
   1. readings 不能吃掉有专属 POST 分支的路径（6 个模块保存全废的 P0）；
B. 前后端字段契约
   2. 静态租约写入字段名与渲染器读取字段名一致；
   3. read_leases 的 is_static 判定改用 v_bool；
   4. UPnP lan_address 在 apply / render 两条路径都注入；
   5. v_bool 统一开关判定（is not False 会把 'false' 当 True）；
   6. v_int 的 default= 形态（`or 1000` 会吞掉 0）；
C. 配置渲染
   7. dnsmasq DHCP 池三项校验（倒序 / 含本机 LAN / 静态绑定落池内）；
   8. PPPoE 的 ip-up/ip-down 钩子（否则 dns_mode=isp 时全家断网）；
   9. miniupnpd 网段取自 system.lan_address；
  10. local= 域跟随用户域名，不写死 /lan/；
D. 安全
  11. 救援通道 token：helper 生成 / 页面带 token / 服务端强制校验 / 失败限速；
  12. SNMP sysname/location/contact 过滤换行与 #；
  13. PPPoE 多拨 service_name 白名单；
  14. 救援 nft chain 生命周期（关闭时回收，别只加不删）；
E. 语义正确性
  15. check_only 预检不得写配置库；
  16. 构建保护模式下 rollback+reload 必须被拒；
  17. 防火墙预检前端必须传 check_only；
  18. numOr：空串返回 undefined（Number('')===0 会把 MTU 提交成 0）；
  19. DHCPv6 prefix_len 统一为数字；
  20. PAGE_MODULES 含 sys（否则没有保存按钮）；
  21. leaseTimer 在 rebuild() 开头停掉（闭包变量被覆盖 → interval 泄漏）；
  22. portfwd 草稿保护（pageDirty/pageIsDirty/pageClean）；
E. 资源与性能
  23. _ulog_prune 不得整文件读入内存；
  24. ping 不得在 _metric_lock() 内执行；
  25. 网桥口计数不得与成员口重复累加；
  26. logd 必须有磁盘将满保护（snapshotd 早就有了）。
"""
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


def block(src, start, n=None):
    """从 `start`（行首匹配的 def/函数名）起，按缩进切出完整函数体。

    不要用固定行数窗口：函数常有 200+ 行，一截断 ast.parse 就炸，
    断言也会因为看不到后半段而假阴性。
    """
    lines = src.splitlines()
    for i, ln in enumerate(lines):
        if not ln.startswith(start):
            continue
        ind = len(ln) - len(ln.lstrip())
        out = []
        for ln2 in lines[i:]:
            dedent = ln2.strip() and (len(ln2) - len(ln2.lstrip())) <= ind
            # JS 的收尾 `}` 与 def 同缩进，不能在那里截断
            if dedent and not ln2.startswith(('}', start)):
                break
            out.append(ln2)
            if n and len(out) >= n:
                break
        return '\n'.join(out)
    return ''


def code_of(src, start, n=200):
    """取函数体并剥掉注释与字符串字面量。

    断言「代码里不该出现某个模式」时必须先剥文档字符串 —— 修复说明里
    常常正好引用了旧写法（`f.read().splitlines()` 写在 docstring 里解释
    为什么不能那么干），直接匹配源码会把说明本身当成违规。
    """

    seg = block(src, start, n)
    if not seg:
        return ''
    try:
        tree = ast.parse(seg)
    except SyntaxError:
        # 截断导致语法不完整：退化为逐行剥注释，够用
        return '\n'.join(l for l in seg.splitlines()
                         if not l.strip().startswith('#'))
    for node in ast.walk(tree):
        # 只清空「裸表达式位置的字符串」—— 也就是 docstring。
        # 不能无脑清空所有 str 常量：那会把 'mru'、'^[A-Za-z0-9._-]*$'
        # 这些真正要断言的字面量也抹掉，断言就成了永远失败。
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            node.value.value = ''
    try:
        return ast.unparse(tree)
    except Exception:
        return seg


import ast   # noqa: E402  （放在 code_of 之后统一导入）


APP = read('web/app.js')
WEB = read('backend/drouter-web.py')
HELPER = read('backend/drouter-helper.py')
RENDER = read('backend/render.py')
RESCUE = read('backend/drouter-rescue.py')
LOGD = read('backend/drouter-logd.py')
SNAPD = read('backend/drouter-snapshotd.py')
DEPLOY = read('scripts/deploy.sh')

# ---------------------------------------------------------------- A
print('--- A. 路由可达性 ---')
chk('readings 分支排除有 POST 分支的路径',
    '_has_post_branch' in WEB and "p not in _has_post_branch" in WEB)
_m = re.search(r"_has_post_branch = \{(.*?)\}", WEB, re.S)
_paths = re.findall(r"'(/api/[a-z/]+)'", _m.group(1)) if _m else []
chk('六个曾失效模块都在排除名单里',
    set(_paths) >= {'/api/acl', '/api/share', '/api/docker',
                    '/api/ddns', '/api/theme', '/api/ulog'}, '→ %d 项' % len(_paths))
for p in _paths:
    chk('  %s 确有专属 POST 写分支' % p,
        re.search(r"p in \('%s'" % re.escape(p), WEB)
        or re.search(r"p == '%s'" % re.escape(p), WEB))
# readings 判定必须在所有写路由之前，且只对 POST 加了排除
_i_readings = WEB.find('if p in readings')
chk('readings 判定存在', _i_readings > 0)
chk('排除名单定义在 readings 判定之前',
    0 < WEB.find('_has_post_branch = {') < _i_readings)

# ---------------------------------------------------------------- B
print('--- B. 前后端字段契约 ---')
_ml = block(HELPER, 'def act_lease_make_static')
chk('静态租约写入 enabled 字段', "'enabled': True" in _ml and "'mac': mac" in _ml)
chk('静态租约写入 name 字段（渲染器读 name）', "'name': host" in _ml)
chk('read_leases 用 v_bool 判 enabled', "v_bool(sl.get('enabled'))" in HELPER)

_up = len(re.findall(r"cfg\['lan_address'\] = \(cfg\.get\('lan_address'\)\s*\n?\s*or syscfg\.get\('lan_address'\)", WEB))
chk('UPnP lan_address 在两条路径都注入（apply + preview）', _up >= 2, '→ %d 处' % _up)
chk('miniupnpd 取 system.lan_address', 'lan_address' in block(RENDER, 'def render_miniupnpd'))
chk('radvd pool_enabled 用 v_bool', re.search(
    r'def render_radvd.*?v_bool\(', RENDER, re.S) is not None)
chk('dhcpv6 enabled 用 v_bool（不再是 is not False）',
    "enabled') is not False" not in block(RENDER, 'def render_dhcpv6'))
chk('cache-size 用 v_int(default=) 而非 or 1000',
    re.search(r'cache-size=\{v_int\(cfg\.get\(\'dns_cache_size\'\).*?default=1000\)', RENDER) is not None
    and 'or 1000' not in code_of(RENDER, 'def render_dnsmasq', 400))

# ---------------------------------------------------------------- C
print('--- C. 配置渲染 ---')
_dn = block(RENDER, 'def render_dnsmasq', 200)
chk('DHCP 池起始 < 结束', re.search(r'pool.*?start.*?>.*?end|起.*?止', _dn) is not None
    and ('start > end' in _dn or '起始' in _dn or '起止' in _dn), '（含倒序校验文案）')
chk('池不得包含本机 LAN 地址', 'LAN' in _dn and 'ValidateError' in _dn)
chk('静态绑定必须落在池内', re.search(r'静态|static', _dn) is not None and 'ValidateError' in _dn)

_ppp = block(RENDER, 'def render_ppp', 200)
chk('render_ppp 带 ip-up-script 钩子',
    'ip-up-script /opt/drouter/bin/sync-isp-dns.sh' in _ppp)
chk('render_ppp 带 ip-down-script 钩子',
    'ip-down-script /opt/drouter/bin/sync-isp-dns.sh' in _ppp)
chk('sync-isp-dns.sh 在 deploy.sh 里安装',
    "cat > $OPT/bin/sync-isp-dns.sh" in DEPLOY)
_syn = DEPLOY[DEPLOY.find("cat > $OPT/bin/sync-isp-dns.sh"):][:2400]
chk('sync-isp-dns.sh 用 $TMP 比较后再 mv（避免无谓重启 dnsmasq）',
    'TMP=$DST.tmp.$$' in _syn and re.search(r'if \[ -f "\$DST" \] && diff', _syn) is not None
    and 'mv -f "$TMP" "$DST"' in _syn)
chk('sync-isp-dns.sh 仅在 dnsmasq.conf 引用该文件时才重载',
    re.search(r'if grep -qs .resolv-file=/etc/drouter/generated/isp-dns\.conf', _syn) is not None)

_chk_local = re.search(r"local=/\{?([\w%{}.\-]*)", _dn)
chk('dnsmasq local 域跟随用户域名', 'domain' in _dn and "local=/lan/" not in _dn)

# ---------------------------------------------------------------- D
print('--- D. 安全 ---')
chk('helper 有 _rescue_new_token', 'def _rescue_new_token' in HELPER)
chk('token 生成 6 位数字', re.search(
    r"def _rescue_new_token.*?return ''\.join\(str\(uuid\.uuid4\(\)\.int\)\[i\] for i in range\(6\)\)",
    HELPER, re.S) is not None)
chk('开启救援即轮换 token', "conf['token'] = token" in HELPER)
chk('关闭救援作废 token', re.search(
    r"else:.*?conf\['token'\] = ''|conf\.pop\('token'", HELPER, re.S) is not None)
chk('rescue.conf 权限 0640 属组 drouter', 'mode=0o640' in HELPER)
chk('rescue 服务端有 _token_ok', 'def _token_ok' in RESCUE)
chk('老配置无 token 时拒绝放行（不退化为无凭据）',
    re.search(r'def _token_ok.*?if not want:.*?return False', RESCUE, re.S) is not None)
chk('/api/snapshots 强制校验 token',
    re.search(r"/api/snapshots'.*?_token_ok\(", RESCUE, re.S) is not None)
chk('/api/restore 强制校验 token',
    re.search(r"/api/restore'.*?_token_ok\(body\.get\('token'\)\)", RESCUE, re.S) is not None)
chk('校验失败走计数并拒绝',
    RESCUE.count('self._bump_fails()') >= 2)
chk('令牌失败有计数与锁定', 'def _bump_fails' in RESCUE
    and 'n >= 10' in RESCUE and '_lock_until' in RESCUE)
chk('rescue 页面有 token 输入框', 'id="tok"' in RESCUE)
chk('rescue 前端还原时发送 token',
    re.search(r'restore[\s\S]{0,600}?token', RESCUE) is not None)
chk('主面板开启救援后展示 token', 'id="rs-token"' in APP)

_sn = block(HELPER, 'def _kern_save_op', 400)
chk('SNMP 三个字段过滤换行', re.search(
    r"for k in \('contact', 'location', 'sysname'\).*?re\.sub\(r'\[\\r\\n\]', ' '", _sn, re.S) is not None)
chk('SNMP 三个字段过滤 #', re.search(
    r"for k in \('contact', 'location', 'sysname'\).*?replace\('#', ' '\)", _sn, re.S) is not None)
chk('SNMP 三个字段截长 128', re.search(
    r"for k in \('contact', 'location', 'sysname'\).*?\[:128\]", _sn, re.S) is not None)

_pm = code_of(HELPER, 'def act_pppoe_multi', 400)
chk('多拨 service_name 白名单',
    re.search(r"re\.match\(r?'\^\[A-Za-z0-9\._-\]\*\$', svc\)", _pm) is not None)
chk('多拨 service_name 截长 64', '[:64]' in _pm)
chk('多拨 mru 独立取值（不再直接取 mtu）',
    re.search(r"mru = int\(s\.get\('mru'\) or mtu\)", _pm) is not None)
chk('多拨 mru 钳制 576-1500', 'max(576, min(1500, mru))' in _pm)

chk('救援放行走独立 chain', 'drouter_rescue' in HELPER
    and 'add chain inet drouter drouter_rescue' in HELPER)
chk('救援关闭时回收 chain', 'def _rescue_drop_nft_chain' in HELPER)
_drop = block(HELPER, 'def _rescue_drop_nft_chain', 60)
chk('回收时先删 input jump 再删 chain',
    _drop.find('jump drouter_rescue') < _drop.find("'delete', 'chain'")
    and "delete', 'rule', 'inet', 'drouter', 'input'" in _drop)

# ---------------------------------------------------------------- E
print('--- E. 语义正确性 ---')
_ap = block(WEB, '        check_only = bool(b.get', 20)
chk('apply_module 读 check_only', 'check_only' in _ap)
chk('check_only 时不写配置库',
    re.search(r"check_only = bool\(b\.get\('check_only'\)\)", WEB) is not None
    and 'if isinstance(data, dict) and not check_only:' in WEB)
chk('check_only 时用合并后的临时 cfg 渲染',
    re.search(r"if check_only and isinstance\(data, dict\):[\s\S]{0,160}?cfg = dict\(get_cfg",
              WEB) is not None)
chk('前端防火墙预检传 check_only',
    re.search(r"check_only: true, live: false", APP) is not None)

_bg = block(HELPER, 'def check_build_guard', 40)
chk('构建模式 rollback 区分 reload', 'if action ==' in _bg and "'rollback'" in _bg
    and "get('reload')" in _bg)
chk('rollback+reload 在构建模式被拒', 'return fail(' in _bg)

chk('numOr 空串返回 undefined', "return s === '' ? undefined : Number(s)" in APP)
for sel in ('#pp-mtu', '#pp-mru', '#pp-holdoff', '#pp-maxfail', '#ln-mtu', '#dd-ttl'):
    chk('  %s 走 numOr' % sel,
        re.search(re.escape(sel) + r"'\s*,\s*'[\w.]+'\s*,\s*numOr", APP) is not None)

chk('dhcpv6 prefix_len 用 Number 选中', 'Number(c.prefix_len || 60)' in APP)
chk('dhcpv6 prefix_len 绑定为数字', "bind('#d6-len','prefix_len',Number)" in APP
    or re.search(r"bind\('#d6-len',\s*'prefix_len',\s*Number\)", APP) is not None)
chk('PAGE_MODULES 含 sys', re.search(r"sys:\s*\{\s*save: \['system'\]", APP) is not None)
_vd = block(APP, 'function viewDhcp()', 220)
chk('viewDhcp.rebuild 开头停租约定时器',
    re.search(r"const rebuild = \(\) => \{\s*\n\s*stopLeaseTimer\(\);", _vd) is not None)
_vp = block(APP, 'function viewPortfwd()', 300)
chk('portfwd 编辑触发 pageDirty', "pageDirty('portfwd')" in _vp)
chk('portfwd 进页保留草稿', "pageIsDirty('portfwd')" in _vp)
chk('通用保存成功后清脏标记（PAGE_DIRTY 不再永久残留）',
    re.search(r'if \(allOk\) pageClean\(S\.page\);', APP) is not None)
chk('通用应用成功后清脏标记', re.search(r'if \(ok\) pageClean\(S\.page\);', APP) is not None)

# ---------------------------------------------------------------- F
print('--- F. 资源与性能 ---')
_up2 = code_of(HELPER, 'def _ulog_prune', 200)
chk('ulog_prune 不整文件读入', 'f.read()' not in _up2 and 'readlines' not in _up2)
chk('ulog_prune 走流式滑窗 + os.replace',
    'os.replace' in _up2 and 'for ln in f' in _up2)
chk('ulog_prune 先数行再决定是否重写',
    'if total <= keep_rows' in _up2)
def ast_pick(src, start):
    """返回切出的函数的 ast（找不到/语法错返回 None）。"""
    seg = block(src, start)
    if not seg:
        return None
    try:
        return ast.parse(seg)
    except SyntaxError:
        return None


def lock_bodies(tree):
    """所有 `with _metric_lock():` 代码块的源码文本。

    必须按 AST 取而不是正则扫行：`with` 块之后的兄弟语句（如 if/for）
    在 unparse 后同样带缩进，正则的「连续缩进行」会一路吃到函数末尾，
    连带把块外的 ping 算进来 —— 那是假的。
    """
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        names = [w.context_expr.func.id if isinstance(w.context_expr, ast.Call)
                 and isinstance(w.context_expr.func, ast.Name) else ''
                 for w in node.items]
        if '_metric_lock' in names:
            out.append('\n'.join(ast.unparse(s) for s in node.body))
    return out


_rl = code_of(HELPER, 'def _route_latency', 120)
_rl_tree = ast_pick(HELPER, 'def _route_latency')
chk('ping 不在 with _metric_lock 块内（无子进程持锁）',
    _rl_tree is not None and bool(lock_bodies(_rl_tree))
    and all('ping' not in b for b in lock_bodies(_rl_tree)))
chk('ping 结果仍写回缓存（锁内只更新字典）',
    re.search(r'with _metric_lock\(\):[\s\S]{0,200}?st\.update', _rl) is not None)
_nc = block(HELPER, 'def _net_counters', 60)
chk('网桥口跳过（避免 br0 与成员口重复累加）',
    '/bridge' in _nc)
chk('logd 有磁盘将满保护', 'def _disk_ok' in LOGD)
chk('logd 采集前检查磁盘', re.search(r'main\(\).*?_disk_ok', LOGD, re.S) is not None)
chk('snapshotd 同样有 _disk_ok（两侧一致）', 'def _disk_ok' in SNAPD)

print()
if fails:
    print('结果: %d 项失败' % fails)
    sys.exit(1)
print('结果: 全部通过')

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""前端深度修复回归（v1.0.5）：锁住这批修过的点，防回潮。

覆盖：
  1. 脏页保护：ACL / 文件共享 / DDNS 切走再回来不丢未保存草稿；
  2. 心跳定时器：boot() 重入不得叠加心跳；
  3. 离开 Web 终端页必须主动断开 PTY（不然白占 30 分钟）；
  4. 主题导出注释同时剥 /* 与 */（只剥一半照样能闭合注入）；
  5. value="${...}" 一律 esc()（防配置里的引号闭合属性注入）；
  6. href 只放行 http/https（javascript: 伪协议）；
  7. LAN 页网关/域名绑到 dnsmasq 模块（原先写进 system 死键，改了等于没改）；
  8. UPnP 映射表：查询不顺手起服务，走后端只读 read:upnpmap；
  9. 后台标签页降频：心跳跳过、终端轮询降频；
 10. keep_days/keep_rows 不被 || 吞 0。
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


APP = read('web/app.js')
HELPER = read('backend/drouter-helper.py')
WEB = read('backend/drouter-web.py')

print('--- 1. 脏页保护（ACL / NFS / DDNS） ---')
chk('PAGE_DIRTY 三件套已定义',
    all(s in APP for s in ('const PAGE_DIRTY = {};', 'function pageDirty(k)',
                           'function pageClean(k)', 'function pageIsDirty(k)')))
chk('viewAcl 有脏页守卫', "pageIsDirty('acl') && S.acl" in APP)
chk('viewNfs 有脏页守卫', "pageIsDirty('nfs') && S.share" in APP)
chk('viewDdns 用草稿合并渲染', "pageIsDirty('ddns') && S.ddnsDirty" in APP
    and 'Object.assign({}, d.cfg || {}, draft)' in APP)
chk('viewDdns 有草稿时不再重置 S.ddnsDirty', 'if (!draft) S.ddnsDirty = {' in APP)
# 三页各自的「改动」提示处都必须标脏（段落级统计）
_acl_seg = APP[APP.index('/* ===================== 访问控制'):APP.index('/* ================== 内网文件共享')]
_nfs_seg = APP[APP.index('/* ================== 内网文件共享'):APP.index('/* ============================ 外置存储设备')]
_dd_seg = APP[APP.index('async function viewDdns()'):APP.index('/* =', APP.index('function renderDdFields'))]
chk('ACL 段全部改动点标脏', _acl_seg.count("pageDirty('acl')") >= 14,
    '→ %d 处' % _acl_seg.count("pageDirty('acl')"))
chk('NFS 段全部改动点标脏', _nfs_seg.count("pageDirty('nfs')") >= 5,
    '→ %d 处' % _nfs_seg.count("pageDirty('nfs')"))
chk('DDNS 段全部改动点标脏', _dd_seg.count("pageDirty('ddns')") >= 7,
    '→ %d 处' % _dd_seg.count("pageDirty('ddns')"))
chk('保存/应用成功清脏（acl）', APP.count("pageClean('acl')") >= 3)
chk('保存/应用成功清脏（nfs）', APP.count("pageClean('nfs')") >= 3)
chk('DDNS 保存成功清脏', "pageClean('ddns'); setTimeout(viewDdns, 400)" in APP)

print('--- 2. 心跳定时器 ---')
chk('HEARTBEAT_TIMER 句柄已声明', 'let HEARTBEAT_TIMER = null;' in APP)
chk('boot() 起心跳前先清旧', 'if (HEARTBEAT_TIMER) clearInterval(HEARTBEAT_TIMER);' in APP)
chk('boot() 里不再有裸 setInterval 心跳',
    'HEARTBEAT_TIMER = setInterval(async () => {' in APP)

print('--- 3. 离开终端页断开 PTY ---')
chk('stopPageTimers 断开旧终端会话',
    "S.page === 'webshell' && typeof wsDisconnect === 'function' && WS.sid" in APP)

print('--- 4. 主题导出注释剥离 ---')
_m = re.search(r'const cmt = s => (.+);', APP)
chk('cmt 同时剥 /* 与 */', bool(_m) and '/\\/\\*/g' in _m.group(1) and '/\\*\\//g' in _m.group(1))

print('--- 5. value 属性转义扫尾 ---')
# 只对「可能携带外部数据」的插值强制 esc()：含点（obj.attr）、含函数调用、
# 含运算符拼接的一律要求 esc(...) 包裹。
# 纯裸标识符（map 回调形参等，取值来自源码里的字面量数组）放行，
# 否则 DHCPv6 前缀下拉 `[56,60,62,64].map(x => ...)` 这种零风险写法会一直误报。
_value_ok = lambda e: (e.strip().startswith('esc(')
                       or not re.search(r'[.()+\[\]]', e))
_leftover = [m.group(1) for m in re.finditer(r'value="\$\{([^{}]+)\}"', APP)
             if not _value_ok(m.group(1))]
chk('value="${...}" 含外部数据的已全部 esc()', not _leftover, '→ 漏网 %s' % _leftover[:5])

print('--- 6. href 伪协议 ---')
chk('httpUrl 辅助已定义', 'const httpUrl = u =>' in APP and '/^https?:\\/\\//i' in APP)
chk('console_url 过 httpUrl', 'href="${esc(httpUrl(d.console_url))}"' in APP)
chk('DDNS 文档链接过 httpUrl', 'href="${esc(httpUrl(prov.doc))}"' in APP)

print('--- 7. LAN 页假输入框 ---')
chk('lan 保存模块含 dnsmasq', "lan:     { save: ['system', 'dnsmasq'], apply: [] }," in APP)
chk('#ln-gw 显示 dnsmasq.option_gateway', "$w('dnsmasq').option_gateway" in APP)
chk('#ln-gw/#ln-dom 绑定到 dnsmasq 模块',
    "bindQ('#ln-gw', 'option_gateway'); bindQ('#ln-dom', 'domain');" in APP)
chk('system 死键不再被 LAN 页写入', "bind('#ln-gw', 'gateway')" not in APP)

print('--- 8. UPnP 映射表 ---')
chk('前端查询不再顺手 start miniupnpd',
    "body: { name: 'miniupnpd', op: 'start' }" not in APP)
chk('前端走 /api/upnpmap', "api('/api/upnpmap')" in APP)
chk('helper 实现 read_upnpmap', 'def read_upnpmap(_):' in HELPER)
chk('helper 注册 read:upnpmap', "'read:upnpmap': read_upnpmap," in HELPER)
chk('web 路由 /api/upnpmap', "'/api/upnpmap': 'read:upnpmap'," in WEB)
chk('read_upnpmap 不拉服务（无 start）',
    'def read_upnpmap' in HELPER
    and "'start'" not in HELPER[HELPER.index('def read_upnpmap'):HELPER.index('def read_leases')])

print('--- 9. 后台标签页降频 ---')
chk('心跳在后台跳过', 'if (document.hidden) return;' in APP)
chk('终端轮询后台降频', 'document.hidden ? 1500 : 120' in APP)

print('--- 10. keep_days/keep_rows 不吞 0 ---')
chk('keep_days 显式判空', 'u.conf.keep_days != null ? u.conf.keep_days : 7' in APP)
chk('keep_rows 显式判空', 'u.conf.keep_rows != null ? u.conf.keep_rows : 200000' in APP)

print('--- 11. 性能与资源收尾（v1.0.5 后端） ---')
chk('/api/config 单连接取多键', 'def get_cfg_many(keys, default=None):' in WEB
    and 'out = get_cfg_many(keys, {})' in WEB)
chk('get_cfg_many 用 IN 查询', "'SELECT key,value FROM settings WHERE key IN (%s)'" in WEB)
chk('cleanup 状态扫描有 TTL 缓存', '_CLEAN_STATUS_TTL = 30.0' in HELPER
    and '_clean_status_cache' in HELPER)
chk('保存清理配置即失效缓存',
    "indent=2) + '\\n')\n    _clean_status_cache_invalidate()" in HELPER)
chk('执行清理后失效缓存',
    'if not dry:\n        _clean_status_cache_invalidate()' in HELPER)
chk('fs 下载封顶与传输通道对齐（20MB）',
    "size > 20 * 1024 * 1024" in HELPER and '文件超过 512MB' not in HELPER)
chk('rescue 单元有内存闸（packaging）',
    'MemoryMax=150M' in read('packaging/deb/units/drouter-rescue.service'))
chk('rescue 单元有内存闸（deploy.sh）',
    'MemoryMax=150M' in read('scripts/deploy.sh'))
chk('snapshot 单元有内存闸（三处同步）',
    read('packaging/deb/units/drouter-snapshot.service').count('MemoryMax=300M') == 1
    and read('scripts/deploy.sh').count('MemoryMax=300M') >= 1
    and 'MemoryMax=300M' in HELPER[HELPER.index('def _write_snapshot_timer'):HELPER.index('def act_snapshotd_sync')])
chk('dhcp_release 已登记 DEPS（可选）',
    "'dhcp_release', False," in HELPER)

print('--- 12. 守护模块顶层「先用后定义」扫描 ---')
# v1.0.5 事故：往 drouter-shelld.py 顶部插 MAX_BUF 块时把 MAX_SESSIONS = 8
# 顶掉了，AST 抽片段的测试全绿，真机一启动就 NameError 崩溃。
# 这里按模块顶层语句顺序模拟执行：任何 Load 的名字必须已在前面赋过值
# （不进入函数/类/Lambda 体 —— 函数内的前向引用是合法的）。
import ast as _ast
import builtins as _builtins


def _top_level_names(stmt):
    """本语句内（不下降进函数/类/Lambda 体）所有 Load 的名字。
    推导式变量有自己的作用域，先收集再剔除，否则 `[f(s) for s in x]`
    会被误报成「s 先用后定义」。"""
    loads = []
    comp_vars = set()

    class V(_ast.NodeVisitor):
        def visit_Name(self, n):
            if isinstance(n.ctx, _ast.Load):
                loads.append(n.id)
        def visit_FunctionDef(self, n):
            pass          # 不进入函数体
        def visit_AsyncFunctionDef(self, n):
            pass
        def visit_ClassDef(self, n):
            for d in n.decorator_list:
                self.visit(d)
            for b in n.bases:
                self.visit(b)
        def visit_Lambda(self, n):
            pass
        def _comp(self, n):
            for g in n.generators:
                for t in _ast.walk(g.target):
                    if isinstance(t, _ast.Name):
                        comp_vars.add(t.id)
            self.generic_visit(n)
        visit_ListComp = _comp
        visit_SetComp = _comp
        visit_DictComp = _comp
        visit_GeneratorExp = _comp
    V().visit(stmt)
    return [x for x in loads if x not in comp_vars]


def _assigned_names(stmt):
    """本语句顶层（含 if/try/for 等块内，乐观视为已定义）赋出的名字。"""
    out = []
    class V(_ast.NodeVisitor):
        def visit_FunctionDef(self, n):
            out.append(n.name)
        def visit_AsyncFunctionDef(self, n):
            out.append(n.name)
        def visit_ClassDef(self, n):
            out.append(n.name)
        def visit_Import(self, n):
            out.extend((a.asname or a.name).split('.')[0] for a in n.names)
        def visit_ImportFrom(self, n):
            out.extend(a.asname or a.name for a in n.names)
        def visit_Name(self, n):
            if isinstance(n.ctx, (_ast.Store, _ast.Del)):
                out.append(n.id)
    V().visit(stmt)
    return out


_BUILTINS = set(dir(_builtins)) | {'__name__', '__file__', '__doc__'}
for rel in ('backend/drouter-shelld.py', 'backend/drouter-web.py',
            'backend/drouter-helpd.py', 'backend/drouter-logd.py',
            'backend/drouter-snapshotd.py', 'backend/drouter-rescue.py',
            'backend/drouter-helper.py'):
    tree = _ast.parse(read(rel), rel)
    defined = set(_BUILTINS)
    bad = []
    for stmt in tree.body:
        for nm in _top_level_names(stmt):
            if nm not in defined and nm not in bad:
                bad.append(nm)
        defined.update(_assigned_names(stmt))
    chk('%s 顶层无先用后定义' % rel.split('/')[-1], not bad, '→ %s' % bad)

print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % fails))
sys.exit(1 if fails else 0)

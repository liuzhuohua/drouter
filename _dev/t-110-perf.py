#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""页面载入性能：并发化 + 落盘缓存的判据。

盯三件事，全部**真跑一遍代码计时**，不是 grep 源码形状：
  ① 三块外部探测是并发的 —— 实测墙钟时间 ≈ 最慢那一条，不是三条之和
  ② 落盘缓存跨进程有效（fork 架构下进程内缓存等于没写）
  ③ 三个慢接口的耗时真的降到了可接受范围

⚠️ **为什么必须实测计时，不能只判源码形状**：
这一轮踩过的坑正是「改完一测确实快了，就以为好了」。
第一版只给 detect_public_ip 加了缓存，测完单个函数从 6s 降到 0.12s，
以为完事了 —— 真机一测 /api/pubip 仍要 9.8s，因为 check 分支里
另外两块（_echo_all_v4、_traceroute_first_hops）压根没走缓存，
每次都实时重打。缓存只挡住了三块里最便宜的那一块。

所以判据必须是「从入口到返回的总耗时」，不是「某个函数变快了」。
下面的用例用假的慢函数替换真实的外部探测，注入固定 sleep，
然后**真的调 act_pubip / _print_status**，量它们各自的墙钟时间。
"""
import ast
import io
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(ROOT, 'backend', 'drouter-helper.py')
APP = os.path.join(ROOT, 'web', 'app.js')

PASS = FAIL = 0


def chk(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('[OK]   %s %s' % (name, extra))
    else:
        FAIL += 1
        print('[FAIL] %s %s' % (name, extra))


def read(p):
    return io.open(p, encoding='utf-8').read()


def inject(src, old, new, why):
    """带守卫的注入。守卫不是形式主义 —— 静默失配的注入会造出
    「恒绿的绿灯」，那和真绿长得一模一样，最难发现。"""
    n = src.count(old)
    if n != 1:
        raise AssertionError('注入目标 %s 在源码里出现 %d 次（应为恰好 1 次）' % (why, n))
    out = src.replace(old, new, 1)
    if out == src:
        raise AssertionError('注入 %s 之后源码没变化' % why)
    if old in out:
        raise AssertionError('注入 %s 之后原文本仍在' % why)
    return out


def node(name, src):
    """抽出一个顶层函数（连它的 docstring 一起），返回可编译的源码片段。"""
    tree = ast.parse(src)
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(src, n)
    raise AssertionError('源码里找不到函数 %s' % name)


def const(name, src):
    """抽出一个顶层常量赋值。

    ⚠️ 判据/测试里**不要伪造这类常量**。
    第一版给环境塞了个 `PUBIP_VERDICT = {}`，结果 act_pubip 查表
    `PUBIP_VERDICT['unknown']` 直接 KeyError —— 测试红在一个跟
    「性能」毫无关系的地方，排查方向被带偏。
    凡是会影响被测函数控制流的东西，就该从真源码里取，取不到就说明
    依赖没被满足，而不是拿个空壳糊上去。
    """
    tree = ast.parse(src)
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.get_source_segment(src, n)
    raise AssertionError('源码里找不到常量 %s' % name)


# ===================== 替身：假的慢探测 =====================
#
# 固定 sleep 而不是真连网 —— 真连网的结果依赖目标机网络状况，
# CI 上更可能是「全部立即失败」，那就测不出串行还是并行了。
# 固定 sleep 让「串行 = 3×S，并发 = 1×S」成为可判定的确定事实。

SLEEP = 0.30          # 每个假探测函数的耗时
TOL = 0.15            # 计时判据的容差

# lpinfo 缓存里放的内容。格式照抄真实 lpinfo -v 的输出形态
# （「类型 URI」两列、空格分隔）—— 换成随便一行字符串的话，
# 解析出来是 0 个设备，判据就成了「本该 0 个」，红绿都不算数。
CACHE_TEXT = 'network beh\nfile cups-brf:/\nnetwork ipp\n'


def build_env(tmp, src):
    """造一个能跑 act_pubip / _print_status 的执行环境。

    只注入需要的名字：外部探测全部换成固定 sleep 的假实现，
    配置文件、状态文件全部指到临时目录，绝不碰真实系统。
    """
    ns = {
        'os': os, 're': __import__('re'), 'json': __import__('json'),
        'time': time, 'sys': sys, 'errno': __import__('errno'),
        'socket': __import__('socket'), 'shutil': __import__('shutil'),
        'tempfile': tempfile, '__name__': 'drouter_helper',
    }
    from concurrent.futures import ThreadPoolExecutor, as_completed
    ns['ThreadPoolExecutor'] = ThreadPoolExecutor
    ns['as_completed'] = as_completed

    # ---- 配置与状态的落点 ----
    setting_file = os.path.join(tmp, 'settings.json')
    ns['_load_setting'] = lambda k, d: {}
    ns['_save_setting'] = lambda *a, **k: None
    ns['SETTING_FILE'] = setting_file
    ns['in_build_mode'] = lambda: False
    ns['PRINT_CONF'] = os.path.join(tmp, 'print.conf')
    # ⚠️ PRINT_CONF 必须指到临时目录 —— 真源码里它是 /etc/drouter/print.conf。
    # 测试绝不能去读（更不能写）真实系统配置。
    # 其余常量从真源码抽，保证被测函数的控制流与线上一致。
    for _c in ('PRINT_PKGS', 'PRINT_DRIVERS', 'PRINT_ITEMS',
               'PRINT_RAW_SERVICE', 'PRINT_CUPSD'):
        try:
            exec(compile(const(_c, src), '<perf>', 'exec'), ns)
        except AssertionError:
            pass
    # 从真源码取判定表，不伪造空壳（伪造会让 act_pubip 查表 KeyError，
    # 测试红在一个跟性能无关的地方，把排查方向带偏）
    exec(compile(const('PUBIP_VERDICT', src), '<perf>', 'exec'), ns)

    # ---- 外部探测的替身在下面 exec 完真函数之后再装 ----
    ns['_slow'] = lambda: (time.sleep(SLEEP), ('', ''))[1]
    ns['_probe_state_load'] = lambda: {}
    ns['_probe_read_hits'] = lambda: []
    ns['_PROBE_TIMEOUT'] = 60
    ns['_pubip_disk_read'] = lambda: None
    ns['_pubip_ev_read'] = lambda: None
    ns['_pubip_ev_write'] = lambda *a: None
    ns['_is_private_v4'] = lambda ip: str(ip).startswith('10.')
    ns['_addr_class_of'] = lambda ip: 'public'

    # ---- 打印页用到的那些 ----
    ns['_print_load'] = lambda: {'mode': 'cups'}
    ns['_svc_states'] = lambda names: {
        n: {'active': 'inactive', 'enabled': 'disabled'} for n in names}
    ns['_print_lan_ip'] = lambda: '192.168.7.3'
    # _print_backups 会去扫真实的备份目录 —— 测试里必须替换掉，
    # 不能因为「只读」就放任它读真实系统目录
    ns['_print_backups'] = lambda: []
    ns['_print_c_locale_env'] = lambda: None
    ns['_print_parse_printers_conf'] = lambda t: []
    ns['_print_parse_lpstat_p'] = lambda t: []
    ns['_print_conf_path'] = lambda: ns['PRINT_CONF']
    ns['ok'] = lambda data=None, msg_cn='', **k: {
        'ok': True, 'data': data or {}, 'msg_cn': msg_cn}
    ns['_load_setting'] = lambda k, d: d

    # ⚠️ `_print_status` 用 `shutil.which('cupsd') or isfile('/usr/sbin/cupsd')`
    # 判 inst。**这在 Windows 上必然是 False**（没有 cupsd），
    # 于是 `_f_lpi` 根本不会被 submit，devices 恒为空 ——
    # 判据会红，但红的理由跟被测代码无关，是测试环境的问题。
    #
    # 第一版就踩了这个坑：注入 2 元组后 devices 变 0，看着像注入生效了，
    # 实际上**不注入也是 0**。这种「恒红的判据」比没有判据更糟 ——
    # 它会让人以为反向验证做过了。
    #
    # 做法：给执行环境一份**替身 shutil**，只放行 cupsd 这一项。
    # 不能去改真 os 模块的 isfile —— os 是全局共享的，改了会连自己
    # 读源码、读临时目录都一起骗。
    class _FakeShutil:
        @staticmethod
        def which(name):
            return '/usr/sbin/cupsd' if name == 'cupsd' else None

        @staticmethod
        def copy2(*a, **k):
            return None

        @staticmethod
        def rmtree(*a, **k):
            return None

        @staticmethod
        def disk_usage(p):
            class _U:
                total = free = used = 100 * 1024 ** 3
            return _U()
    ns['shutil'] = _FakeShutil

    # 把要测的函数按依赖顺序 exec 进去
    order = ['_echo_one', '_echo_public_ip', '_echo_all_v4',
             '_traceroute_first_hops', 'act_pubip', '_print_status']
    for fn in order:
        try:
            seg = node(fn, src)
        except AssertionError:
            continue    # 某些函数可能不存在（比如被改名），跳过不影响其它
        exec(compile(seg, '<perf>', 'exec'), ns)

    # ⚠️ 替身必须在 exec **之后**再装一遍。
    # 第一版把替身写在 exec 之前，结果真函数把假实现覆盖掉了 ——
    # 于是测的其实是真 `_echo_all_v4`，它去读 IP_ECHO_V4 报 NameError。
    #
    # 这个 bug 有个更坏的性质：**它不是恒绿，是恒红**，所以还能被发现。
    # 换成「判据写在真代码上、替身装在前面」这种组合时才会恒绿 ——
    # 那才是真正危险的。所以这里明确按「先 exec 真函数，再覆盖替身」写死。
    ns['detect_public_ip'] = lambda force=False, ttl=20: (
        time.sleep(SLEEP),
        {'v4_public': '1.2.3.4', 'v4_local': '10.0.0.2',
         'v6_public': '', 'v6_local': '', 'egress': 'eth0', 'gw': '10.0.0.1',
         'checked_at': '2026-10-03 12:00:00', 'combo': 'v4'},
    )[1]
    ns['_echo_all_v4'] = lambda: (
        time.sleep(SLEEP), {'1.2.3.4': ['a', 'b']})[1]
    ns['_traceroute_first_hops'] = lambda *a, **k: (
        time.sleep(SLEEP), ['1.2.3.4'])[1]
    ns['sh'] = lambda cmd, timeout=None, env=None: (
        time.sleep(SLEEP), (0, '', ''))[1]
    ns['_print_usb_printers'] = lambda: ([], [])
    ns['_print_listen_now'] = lambda: []
    ns['_print_lpinfo_cache_read'] = lambda: None
    ns['_print_lpinfo_cache_write'] = lambda t: None
    ns['_pubip_ev_read'] = lambda: None
    ns['_pubip_ev_write'] = lambda *a: None
    return ns


def timed(fn, *a, **k):
    t = time.time()
    r = fn(*a, **k)
    return time.time() - t, r


def main():
    src = read(HELPER)
    app = read(APP)
    tmp = tempfile.mkdtemp(prefix='drouter-perf-')

    print('--- ① act_pubip check：三块探测是并发的 ---')
    ns = build_env(tmp, src)
    if 'act_pubip' not in ns:
        chk('act_pubip 存在（否则后面全部无从测）', False)
        return
    # 缓存关掉，强制走「三块全探」那条路
    ns['_pubip_ev_read'] = lambda: None
    el, res = timed(ns['act_pubip'], {'op': 'check'})
    # 串行会是 3×SLEEP=0.9s，并发约 SLEEP=0.3s
    chk('三块外部探测并发执行（实测墙钟 ≈ 一条，不是三条之和）',
        el < SLEEP * 2,
        '串行需 %.2fs，并发需 %.2fs，实测 %.2fs' % (SLEEP * 3, SLEEP, el))
    chk('并发执行后判定结论仍然完整（四条证据都在）',
        len((res.get('data') or {}).get('evidence') or []) == 4,
        str(len((res.get('data') or {}).get('evidence') or [])))

    print('--- ② 落盘缓存命中时不重新探测 ---')
    ns2 = build_env(tmp, src)
    calls = {'echo': 0, 'trace': 0}

    def _cnt_echo():
        calls['echo'] += 1
        time.sleep(SLEEP)
        return {'1.2.3.4': ['a', 'b']}

    def _cnt_trace(*a, **k):
        calls['trace'] += 1
        time.sleep(SLEEP)
        return ['1.2.3.4']

    ns2['_echo_all_v4'] = _cnt_echo
    ns2['_traceroute_first_hops'] = _cnt_trace
    # 缓存里有东西
    ns2['_pubip_ev_read'] = lambda: {'seen': {'1.2.3.4': ['a', 'b']},
                                     'hops': ['1.2.3.4']}
    el2, res2 = timed(ns2['act_pubip'], {'op': 'check'})
    chk('命中证据缓存时不再调 _echo_all_v4', calls['echo'] == 0,
        '调了 %d 次' % calls['echo'])
    chk('命中证据缓存时不再调 _traceroute_first_hops',
        calls['trace'] == 0, '调了 %d 次' % calls['trace'])
    # ⚠️ 这里**不判耗时变快**，只判「探测函数没被调用」。
    # 三条替身都是本地 sleep，缓存省的是外部网络往返，本地量不出来；
    # 硬写一条「缓存版更快」的判据，结果必然是恒绿（两条都是一条 SLEEP）。
    # 真正的耗时收益由 ①（并发）与真机测速覆盖。
    chk('命中缓存时请求仍成功返回（四条证据完整）',
        len((res2.get('data') or {}).get('evidence') or []) == 4)
    chk('复用缓存时如实标记 evidence_cached',
        (res2.get('data') or {}).get('evidence_cached') is True)
    chk('全量探测时 evidence_cached 为 False',
        (res.get('data') or {}).get('evidence_cached') is False)

    print('--- ③ force=True 必须绕过缓存 ---')
    calls['echo'] = 0
    calls['trace'] = 0
    el3, res3 = timed(ns2['act_pubip'], {'op': 'check', 'force': True})
    chk('force=True 时强制重新探测（点「重新检测」不能没反应）',
        calls['echo'] == 1 and calls['trace'] == 1,
        'echo=%d trace=%d' % (calls['echo'], calls['trace']))
    chk('force=True 时 evidence_cached 为 False',
        (res3.get('data') or {}).get('evidence_cached') is False)

    print('--- ④ 半套缓存必须退化成全量探测 ---')
    # 只有 seen 没有 hops —— 拼不出完整证据，必须全量重探。
    # 半套缓存拼出来的「多源一致性」是残缺的，比没有更误导。
    for bad in ({'seen': {'1.2.3.4': ['a']}},
                {'hops': ['1.2.3.4']},
                {'seen': 'not-a-dict', 'hops': 'not-a-list'},
                {'seen': {'1.2.3.4': ['a']}, 'hops': None}):
        nsc = build_env(tmp, src)
        c = {'n': 0}

        def _ce(*a, **k):
            c['n'] += 1
            return {'1.2.3.4': ['a', 'b']}
        nsc['_echo_all_v4'] = _ce
        nsc['_traceroute_first_hops'] = lambda *a, **k: ['1.2.3.4']
        nsc['_pubip_ev_read'] = lambda b=bad: b
        nsc['act_pubip']({'op': 'check'})
        chk('残缺缓存 %s 被识别为不可用并全量重探' % json_ish(bad),
            c['n'] == 1, '实调 %d 次' % c['n'])

    print('--- ⑤ 打印页：五路查询并发 ---')
    ns3 = build_env(tmp, src)
    if '_print_status' in ns3:
        elp, resp = timed(ns3['_print_status'])
        # sh / usb / listen 三条各 SLEEP，加上 lpinfo（inst 为真时）
        # 串行 ≥4×SLEEP，并发约 SLEEP。这里只断言「不是全部之和」。
        chk('打印页慢查询并发（实测墙钟远小于串行之和）',
            elp < SLEEP * 3,
            '串行需 ≥%.2fs，并发约 %.2fs，实测 %.2fs' % (SLEEP * 4, SLEEP, elp))
        chk('打印页返回体完整（含并发取到的 listen / usb 字段）',
            'listen' in (resp.get('data') or {})
            and 'usb_printers' in (resp.get('data') or {}))
    else:
        chk('_print_status 存在', False)

    print('--- ⑥ 反向验证：注入回串行必须观察到红 ---')
    # 这一段回答的是「上面那些判据能不能分辨对错」。
    # 判据的自我验证只能靠**注入一个反例**，不能在真源码上找反面例子。
    SERIAL_BUG = inject(
        src,
        "    ev_cache = None if force else _pubip_ev_read()",
        "    ev_cache = None\n    force = True",
        '让 check 分支永远不读缓存')
    try:
        nsb = build_env(tmp, SERIAL_BUG)
        c = {'n': 0}

        def _ce2(*a, **k):
            c['n'] += 1
            time.sleep(SLEEP)
            return {'1.2.3.4': ['a', 'b']}
        nsb['_echo_all_v4'] = _ce2
        nsb['_traceroute_first_hops'] = lambda *a, **k: (
            time.sleep(SLEEP), ['1.2.3.4'])[1]
        elb, resb = timed(nsb['act_pubip'], {'op': 'check'})
        # ⚠️ 判据只能验「缓存被绕过了」，**不能验耗时变长**。
        # 第一版写的是 `elb > el2`，而实测 0.30s vs 0.30s —— 照样判绿。
        # 原因是：注入「不读缓存」之后，三块探测**仍然是并发的**，
        # 耗时还是一条 SLEEP。缓存省的是「外部网络往返」，
        # 而这里的三条都是本地 sleep —— 省不掉，也就量不出来。
        #
        # 「没有 X」和「做对了 Y」不是同一件事：这条判据只能证明前者。
        # 要证明后者得靠真实网络，那是真机验收的活，不是单测的活。
        chk('注入「不读缓存」后 evidence_cached 变 False（缓存确实被绕过）',
            resb.get('data', {}).get('evidence_cached') is False,
            '注入后标记=%s' % resb.get('data', {}).get('evidence_cached'))
        chk('注入版本与原版的差异确实体现在标记上（对照原为 True）',
            (res2.get('data') or {}).get('evidence_cached') is True
            and resb.get('data', {}).get('evidence_cached') is False)
    except AssertionError as e:
        chk('注入「不读缓存」可构造', False, str(e))

    # 注入回串行：把三块探测改回顺序执行
    try:
        SERIAL2 = inject(
            src,
            "    if ev_cache:\n"
            "        # 命中缓存：只剩 detect_public_ip 需要看，而它自己也有缓存，\n"
            "        # 所以这一路基本是纯本地读文件，毫秒级。\n"
            "        info = detect_public_ip(force)\n"
            "        _echo_seen, hops_all = seen_c, hops_c\n"
            "    else:\n"
            "        with ThreadPoolExecutor(max_workers=3) as _p:\n"
            "            _f_det = _p.submit(detect_public_ip, force)\n"
            "            _f_echo = _p.submit(_echo_all_v4)\n"
            "            _f_trace = _p.submit(_traceroute_first_hops)\n"
            "            info = _f_det.result()\n"
            "            _echo_seen = _f_echo.result()\n"
            "            hops_all = _f_trace.result()\n"
            "        _pubip_ev_write(_echo_seen, hops_all)\n",
            "    if ev_cache:\n"
            "        info = detect_public_ip(force)\n"
            "        _echo_seen, hops_all = seen_c, hops_c\n"
            "    else:\n"
            "        info = detect_public_ip(force)\n"
            "        _echo_seen = _echo_all_v4()\n"
            "        hops_all = _traceroute_first_hops()\n"
            "        _pubip_ev_write(_echo_seen, hops_all)\n",
            '把三块探测改回串行')
        # 注入后的源码必须仍是合法 Python。
        # ⚠️ 这条不是形式主义：注入文本少写一行 `_pubip_ev_write(...)` 时，
        # inject 的三条守卫**全部通过**（目标唯一匹配、源码变了、原文本没了），
        # 但产物是**缩进错乱的非法源码** —— 报错发生在几百行之后的 ast.parse，
        # 看起来像是「测试环境缺依赖」，跟「注入写错了」毫无关系。
        # 所以注入完必须自己先确认产物能解析。
        ast.parse(SERIAL2)
        nss = build_env(tmp, SERIAL2)
        nss['_pubip_ev_read'] = lambda: None
        els, _r = timed(nss['act_pubip'], {'op': 'check'})
        chk('注入「三块改回串行」后，并发判据能观察到红',
            els >= SLEEP * 3 - TOL,
            '串行应 ≥%.2fs，实测 %.2fs（原 %.2fs）' % (SLEEP * 3, els, el))
    except AssertionError as e:
        chk('注入「三块改回串行」可构造', False, str(e))

    # 注入回 2 元组 —— 真机上抓到的那个 bug。
    # 这一条比前两条更重要：前两条注入会让**耗时**变长，一测就露；
    # 这一条注入只会让**数据变空**而耗时照旧很快，不专门判就永远发现不了。
    try:
        TUPLE_BUG = inject(
            src, "            return 0, _c, ''\n", '            return 0, _c\n',
            '把 lpinfo 缓存命中分支改回 2 元组')
        ast.parse(TUPLE_BUG)
        ns_t = build_env(tmp, TUPLE_BUG)
        ns_t['_print_lpinfo_cache_read'] = lambda: CACHE_TEXT
        rt = ns_t['_print_status']()
        dt = rt.get('data') or {}
        chk('注入「返回 2 元组」后，设备列表判据能观察到红',
            len(dt.get('devices') or []) == 0
            and dt.get('devices_timed_out') is True,
            '注入后解析出 %d 个（原应为 3），timed_out=%r'
            % (len(dt.get('devices') or []), dt.get('devices_timed_out')))
    except AssertionError as e:
        chk('注入「返回 2 元组」可构造', False, str(e))

    print('--- ⑦ 落盘缓存必须跨进程 ---')
    chk('helper 是 fork 的一次性进程，缓存落盘到 /run/drouter/',
        "_PUBIP_DISK = '/run/drouter/pubip-cache.json'" in src)
    chk('证据缓存也落盘（进程内缓存在 fork 架构下等于没写）',
        "_PUBIP_EV_DISK = '/run/drouter/pubip-evidence.json'" in src)
    chk('lpinfo 缓存落盘', '_PRINT_LPINFO_DISK' in src)
    chk('落盘写用 tmp + os.replace（避免半截文件被当成有效缓存）',
        src.count("os.replace(tmp, _PUBIP_EV_DISK)") == 1
        and src.count("os.replace(tmp, _PRINT_LPINFO_DISK)") == 1)
    chk('lpinfo 失败不写缓存（否则一次网络抖动被固化 120 秒）',
        'if rc == 0:\n            _print_lpinfo_cache_write(out)' in src)

    print('--- ⑦b 缓存命中分支的返回值形状 ---')
    # ⚠️ 这一条是被真机 bug 逼出来的。
    # 第一版 `_q_lpinfo` 缓存命中时返回 2 元组 (0, cache)，
    # 而调用方按 `rc, lpv, _e = ...` 解包 → ValueError
    # → 被 `except Exception` 吞掉置 rc=1
    # → 页面 devices=[] 且 devices_timed_out=True，**数据是错的**，
    #   可耗时只有 0.08 秒 —— 「快」得很像修好了。
    #
    # 只判「快」永远发现不了它。这里直接**真的调一次缓存命中分支**，
    # 断言它能按 3 元组解包、且解出来的内容与缓存一致。
    ns4 = build_env(tmp, src)
    hit = {'n': 0}

    def _fake_read():
        hit['n'] += 1
        return CACHE_TEXT
    ns4['_print_lpinfo_cache_read'] = _fake_read
    # 只拦 lpinfo 的 sh 调用。
    # ⚠️ 第一版把 sh **整个**换掉，结果 dpkg-query 也被拦 ——
    # 报出来是「缓存命中时不该调 sh(lpinfo)」，可栈顶在 _q_dpkg，
    # 跟 lpinfo 半点关系没有。判据的报错必须指向它要判的那件事。
    _sh_calls = []

    def _spy_sh(cmd, timeout=None, env=None):
        _sh_calls.append((cmd or [None])[0])
        if (cmd or [None])[0] == 'lpinfo':
            raise AssertionError('缓存命中时不该再跑 lpinfo')
        return 0, '2.4.10', ''
    ns4['sh'] = _spy_sh
    r4 = ns4['_print_status']()
    d4 = r4.get('data') or {}
    chk('缓存命中分支能按 3 元组解包（第一版返回 2 元组会抛 ValueError）',
        len(d4.get('devices') or []) == 3,
        '解析出 %d 个（缓存里有 3 行）' % len(d4.get('devices') or []))
    chk('缓存命中时 devices_timed_out 为 False',
        d4.get('devices_timed_out') is False,
        '实为 %r' % d4.get('devices_timed_out'))
    chk('缓存命中时确实没再跑 lpinfo（否则缓存白做）',
        'lpinfo' not in _sh_calls,
        'sh 调用：%s' % _sh_calls)

    print('--- ⑧ 前端如实告知复用了缓存 ---')
    chk('前端展示 evidence_cached 提示', 'evidence_cached' in app)
    chk('提示里给了重测入口', 'id="pi-force2"' in app)
    chk('重测入口绑到了 force=true',
        "getElementById('pi-force2')" in app and 'pubipCheck(true)' in app)
    chk('载入文案不再宣称 10–30 秒（已经不成立）',
        '10–30 秒' not in app and '约需 10–30' not in app)
    chk('提示用的按钮 class 在 CSS 里真实存在（btn-xs 并不存在）',
        '.small' in io.open(os.path.join(ROOT, 'web', 'app.css'),
                            encoding='utf-8').read())

    print()
    print('=' * 60)
    print('结果：通过 %d / 失败 %d' % (PASS, FAIL))
    return 1 if FAIL else 0


def json_ish(o):
    import json as _j
    return _j.dumps(o, ensure_ascii=False)


if __name__ == '__main__':
    sys.exit(main())

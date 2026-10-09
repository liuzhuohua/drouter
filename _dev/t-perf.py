#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""性能回归（#85）：钉死本轮修掉的性能问题，防止改回去。

这个测试专门盯三类「看起来没事、实际一直在浪费」的问题：
  ① 页面切换后定时器泄漏（切走还在轮询 → 后端被空打）；
  ② 高频接口里的 subprocess 滥用（每次切页面都要 fork 好几次）；
  ③ 死代码分支（写了 quick 参数却两个分支一样）。

放这里而不是散在各模块测试里，是因为它们都是「跨模块的性能约定」，
改任何一个视图或读接口都可能破坏，需要一处集中守住。
"""
import io
import os
import re
import sys

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


HELPER_SRC = io.open(HELPER, encoding='utf-8').read()
APP_SRC = io.open(APP, encoding='utf-8').read()
HELPER_LINES = HELPER_SRC.split('\n')
APP_LINES = APP_SRC.split('\n')


def hseg(name):
    """取 helper 里某个顶层函数的函数体（按行切片）。"""
    try:
        i = next(k for k, l in enumerate(HELPER_LINES) if l.startswith('def %s(' % name))
    except StopIteration:
        return ''
    j = next((k for k in range(i + 1, len(HELPER_LINES))
              if HELPER_LINES[k].startswith(('def ', 'class ', '@'))), len(HELPER_LINES))
    return '\n'.join(HELPER_LINES[i:j])


def strip_comment_lines(text):
    """去掉整行注释与行尾注释，只留可执行代码。

    为什么需要：源码里保留「原来是 sh(['cat', '/proc/loadavg'])」这类**说明性
    注释**是好事（后人知道为什么改），但如果断言直接对原始文本做 substring
    匹配，就会被自己的注释骗过 —— 本轮 t-perf 已经踩过一次。
    这里做一个够用的近似：逐行截断到第一个未出现在字符串字面量里的 '#'。
    """
    out = []
    for line in text.split('\n'):
        q = None
        cut = len(line)
        for k, c in enumerate(line):
            if q:
                if c == '\\':
                    continue
                if c == q:
                    q = None
            elif c in ('"', "'"):
                q = c
            elif c == '#':
                cut = k
                break
        out.append(line[:cut])
    return '\n'.join(out)


def main():
    print('--- ① 页面切换不得泄漏定时器（前端）---')
    chk('定义了 stopPageTimers', 'function stopPageTimers(' in APP_SRC)
    chk('go() 首件事就是停表',
        re.search(r'function go\(k\) \{\s*\n\s*(//[^\n]*\n\s*)*stopPageTimers\(\);', APP_SRC) is not None)
    # 所有模块级 interval 定时器变量都必须出现在 stopPageTimers 里
    for v in ('DASH_TIMER', 'WANLOG_TIMER', 'FWL_TIMER', 'DK_TIMER'):
        seg = APP_SRC[APP_SRC.index('function stopPageTimers('):]
        seg = seg[:seg.index('\nfunction go(')]
        chk('stopPageTimers 里清了 %s' % v, v in seg)
    chk('stopPageTimers 调用了 pubip 的停止函数', 'pubipProbeStopPoll' in
        APP_SRC[APP_SRC.index('function stopPageTimers('):APP_SRC.index('\nfunction go(')])
    # 闭包内定时器通过全局钩子暴露
    chk('声明了 stopLeaseTimer 钩子', re.search(r'let stopLeaseTimer\s*=', APP_SRC) is not None)
    chk('声明了 stopV6Timer 钩子', re.search(r'let stopV6Timer\s*=', APP_SRC) is not None)
    chk('viewDhcp 挂上了 stopLeaseTimer', 'stopLeaseTimer = () =>' in APP_SRC)
    chk('viewV6Test 挂上了 stopV6Timer', 'stopV6Timer = () =>' in APP_SRC)

    print('\n--- ② 轮询函数必须先判 DOM 再发请求 ---')
    # liveWanLog：不能出现「先 api(...) 再 if (!box) return」
    seg = APP_SRC[APP_SRC.index('async function liveWanLog('):]
    seg = seg[:seg.index('\n}') + 2]
    i_dom = seg.find("$('#pp-live')")
    i_api = seg.find('api(')
    chk('liveWanLog 判 DOM 在发请求之前', 0 <= i_dom < i_api,
        'dom@%d api@%d' % (i_dom, i_api))
    # loadLeases 同理
    seg2 = APP_SRC[APP_SRC.index('const loadLeases = ()'):]
    seg2 = seg2[:seg2.index('stopLeaseTimer =')]
    chk('loadLeases 先判 #dh-leases 再请求',
        seg2.find("if (!$('#dh-leases'))") < seg2.find('api('))

    print('\n--- ③ 导航代次：旧请求不得覆盖新页面 ---')
    chk('定义了 NAV_GEN', re.search(r'let NAV_GEN\s*=\s*0', APP_SRC) is not None)
    chk('go() 里递增代次', re.search(r'function go\(k\) \{.*?NAV_GEN\+\+', APP_SRC, re.S) is not None)
    chk('有 navStale 判断函数', 'function navStale(' in APP_SRC)
    chk('渲染链路校验代次', 'if (navStale(gen)) return;' in APP_SRC)

    print('\n--- ④ 高频接口不得滥用 subprocess ---')
    seg_si = hseg('read_sysinfo')
    # 断言只看「可执行代码」，先剥掉注释——否则解释性注释里写一句
    # 「原来是 sh(['cat', '/proc/loadavg'])」就会把测试自己骗过（本轮已踩）。
    code_si = strip_comment_lines(seg_si)
    chk('read_sysinfo 不用 sh([\'hostname\'])', "sh(['hostname'])" not in code_si)
    chk("read_sysinfo 不用 sh(['cat', '/proc/loadavg'])",
        'loadavg' not in code_si or "sh(['cat'" not in code_si)
    chk('read_sysinfo 用 socket.gethostname()', 'socket.gethostname()' in seg_si)
    chk('read_sysinfo 用 os.uname()', 'os.uname()' in seg_si)
    chk('read_sysinfo 改用 _read_disks()', '_read_disks()' in seg_si)
    chk('read_sysinfo 不再直接调 df', "sh(['df'" not in code_si)

    print('\n--- ⑤ _read_disks 实现正确性 ---')
    seg_d = hseg('_read_disks')
    chk('_read_disks 读 /proc/mounts', "'/proc/mounts'" in seg_d)
    chk('_read_disks 用 statvfs', 'os.statvfs(' in seg_d)
    chk('_read_disks 过滤 tmpfs/devtmpfs',
        "'tmpfs'" in seg_d and "'devtmpfs'" in seg_d)
    chk('_read_disks 输出与 df 同结构（含 mount/pct）',
        "x['mount']" in seg_d and "'pct'" in seg_d)
    chk('_read_disks 根分区排最前', "x['mount'] != '/'" in seg_d)
    chk('_read_disks 注册在 read_sysinfo 之前',
        HELPER_SRC.index('def _read_disks(') < HELPER_SRC.index('def read_sysinfo('))

    print('\n--- ⑥ 死代码分支（quick 参数）---')
    seg_m = strip_comment_lines(hseg('read_metrics'))
    # 注意：不能只判 'if not quick else'，因为合法写法 `x if quick else y` 也存在。
    # 真正的「死代码」是**条件与两侧完全颠倒成同一结果**，即 `A if not quick else A`。
    chk('read_metrics 不再出现「两个分支一样」的写法',
        re.search(r'(\w[\w\.\(\)\[\]]*)\s+if\s+not\s+quick\s+else\s+\1\b', seg_m) is None)
    # quick 仍应被读取（保留参数兼容），但不得造成同义分支
    chk('quick 参数仍被读取（向后兼容）', "quick = bool(" in seg_m)

    print('\n--- ⑦ 批量取服务状态（不再逐条 fork systemctl）---')
    chk('定义了 _svc_states 批量函数', 'def _svc_states(' in HELPER_SRC)
    seg_ss = hseg('_svc_states')
    chk('_svc_states 用一次 systemctl show', "'show'" in seg_ss and "'--value'" in seg_ss)
    chk('_svc_states 同时请求两个属性',
        "'-p'" in seg_ss and 'ActiveState' in seg_ss and 'UnitFileState' in seg_ss)
    chk('_svc_states 按属性个数分段切分',
        'len(props)' in seg_ss)
    seg_rs = strip_comment_lines(hseg('read_services'))
    chk('read_services 不再循环调 systemctl is-active',
        "'is-active'" not in seg_rs and "'is-enabled'" not in seg_rs)
    chk('read_services 改用 _svc_states', '_svc_states(' in seg_rs)
    # read_share / read_logd / read_docker 三处同样的逐条 fork 也要收口
    for fn in ('read_share', 'read_logd', 'read_docker'):
        seg = strip_comment_lines(hseg(fn))
        chk('%s 不再逐条 systemctl is-active/is-enabled' % fn,
            "'is-active'" not in seg and "'is-enabled'" not in seg
            and '_svc_states(' in seg)
    # which / command -v 也不该在循环里反复 fork
    chk('read_ulog_conf 用 shutil.which（零 fork）',
        'shutil.which(' in strip_comment_lines(hseg('read_ulog_conf')))
    chk('_dep_installed 用 shutil.which',
        'shutil.which(' in strip_comment_lines(hseg('_dep_installed')))
    seg_dpi = strip_comment_lines(hseg('_dpi_installed'))
    chk('_dpi_installed 不再循环 fork bash -lc',
        "bash', '-lc'" not in seg_dpi)

    # ---- ⑧ 高频接口里的「阻塞 sleep / 逐包 fork」（2026-10-09 真机实测）----
    # 真机实测 read:docker 960ms、read:sysinfo 252ms，两者都在「每次切页面」
    # 的路径上，且都是纯浪费：878ms 是逐个 fork apt-cache（每次都要加载
    # 整个包索引），250ms 是 _read_cpu_usage() 里硬 sleep 的采样间隔。
    seg_si2 = strip_comment_lines(hseg('read_sysinfo'))
    chk('read_sysinfo 走 _cpu_pct_cached（不直接 _read_cpu_usage）',
        '_read_cpu_usage(' not in seg_si2 and '_cpu_pct_cached(' in seg_si2)
    seg_pa = strip_comment_lines(hseg('_pkgs_available'))
    chk('_pkgs_available 一次 apt-cache 查全部（不逐个 fork）',
        "'--no-all-versions'] + names" in seg_pa)
    chk('_pkgs_available 用 apt 索引指纹缓存（不是盲 TTL）',
        '_apt_lists_stamp()' in seg_pa and '_PKG_AVAIL_CACHE' in seg_pa)

    print('\n' + '=' * 58)
    print('通过 %d / 失败 %d' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())

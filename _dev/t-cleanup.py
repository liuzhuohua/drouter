#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""磁盘与日志清理（#3）：静态契约 + 清理逻辑单测。

这个模块是「会真删文件」的，测试重点必须放在安全边界上：
  ① 禁用前缀表能挡住一切配置库 / 代码 / 快照路径；
  ② 每一项的 glob 之间不重叠，否则同一个文件被两个项目重复计数（页面会虚报可释放量）；
  ③ 活跃日志（*.jsonl / *.log 无轮转后缀）永不进入候选集；
  ④ 后端动作 / 路由 / 前端三处接线齐全。
"""
import ast
import io
import os
import re
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(ROOT, 'backend', 'drouter-helper.py')
WEB = os.path.join(ROOT, 'backend', 'drouter-web.py')
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
WEB_SRC = io.open(WEB, encoding='utf-8').read()
APP_SRC = io.open(APP, encoding='utf-8').read()

TREE = ast.parse(HELPER_SRC)


def node(name):
    """按名字取顶层节点（Assign 或 FunctionDef）。"""
    for n in TREE.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return n
        elif isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def build(names, ns=None):
    """把若干顶层节点抽到隔离命名空间里 exec。"""
    ns = ns if ns is not None else {}
    body = []
    for nm in names:
        n = node(nm)
        if n is None:
            raise AssertionError('源码里找不到 %s' % nm)
        body.append(n)
    mod = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, '<cleanup>', 'exec'), ns)
    return ns


def main():
    print('--- 后端动作与路由 ---')
    chk('定义了 act_cleanup', 'def act_cleanup(' in HELPER_SRC)
    chk('注册到 ACTIONS', re.search(r"^\s*'cleanup':\s*act_cleanup,", HELPER_SRC, re.M) is not None)
    chk('Web 路由 /api/cleanup', "'/api/cleanup'" in WEB_SRC)
    chk('路由里区分 run 的超时', re.search(r"to = 180 if op == 'run' else 60", WEB_SRC) is not None)
    for op in ('status', 'save', 'run', 'auto'):
        chk('支持 op=%s' % op, ("op == '%s'" % op) in HELPER_SRC)
    chk('未知 op 返回 fail', "未知的清理操作" in HELPER_SRC)

    print('\n--- 清理项清单结构 ---')
    ns = build(['CLEAN_ITEMS', 'CLEAN_ITEM_KEYS', 'CLEAN_FORBIDDEN'])
    items = ns['CLEAN_ITEMS']
    keys = ns['CLEAN_ITEM_KEYS']
    forbidden = ns['CLEAN_FORBIDDEN']
    chk('清理项非空', len(items) >= 5, '%d 项' % len(items))
    chk('key 无重复', len(set(it['key'] for it in items)) == len(items))
    chk('CLEAN_ITEM_KEYS 与清单一致', keys == [it['key'] for it in items])

    for it in items:
        k = it['key']
        chk('%-12s 字段齐全' % k,
            all(x in it for x in ('name', 'kind', 'days', 'on', 'why', 'risk'))
            and it['kind'] in ('age', 'cmd'))
        chk('%-12s 有通俗说明与安全边界' % k,
            len(it.get('why') or '') >= 10 and len(it.get('risk') or '') >= 10)
        if it['kind'] == 'cmd':
            chk('%-12s cmd 项有 size_dirs 与 cmd' % k,
                bool(it.get('size_dirs')) and bool(it.get('cmd')))
            chk('%-12s cmd 项不带 peek（无可枚举的活跃文件）' % k, not it.get('peek'))
        else:
            chk('%-12s age 项有 paths' % k, bool(it.get('paths')))
        # peek 只统计不清理：必须和 paths 严格互斥，否则活跃文件会被计入可释放量
        if it.get('peek'):
            chk('%-12s peek 只出现在 age 项' % k, it['kind'] == 'age')

    print('\n--- 安全边界（最关键）---')
    # 本测试在 Windows 上跑，os.path.realpath 会给路径加盘符前缀，
    # 前缀比对必然失配。这里注入一个 posix 语义的替身，让判定逻辑仍可被验证。
    posixpath = __import__('posixpath')

    class _PosixOS(object):
        sep = '/'
        path = posixpath
        realpath = staticmethod(posixpath.normpath)

    ns2 = build(['CLEAN_FORBIDDEN', '_clean_safe'], {'os': _PosixOS})
    safe = ns2['_clean_safe']
    must_block = ['/etc/drouter/drouter.db', '/etc/chrony.conf', '/boot/vmlinuz',
                  '/usr/bin/python3', '/opt/drouter/backend/drouter-helper.py',
                  '/opt/drouter/web/app.js', '/opt/drouter/data/drouter.db',
                  '/opt/drouter/snapshots/20260928-154705/_meta.json',
                  '/root/.bashrc', '/home/ajeef/x', '/var/lib/dpkg/status',
                  '/var/cache/apt/archives/x.deb']
    for p in must_block:
        chk('拒绝删除 %s' % p, safe(p) is False)
    must_allow = ['/var/log/drouter/all.jsonl.1', '/var/log/drouter/ulog.jsonl.2.gz',
                  '/tmp/drouter-test/x', '/var/tmp/old', '/var/lib/drouter/trash/a/b',
                  '/var/lib/systemd/coredump/core.x', '/var/log/cups/access_log.2.gz']
    for p in must_allow:
        chk('允许清理 %s' % p, safe(p) is True)

    # 清理项自己的路径不能被禁用表挡住，否则那一项永远清不动
    print('\n--- 清理项目录不得落在禁用表内 ---')
    # 只校验 age 项的 paths —— cmd 项的删除走系统命令（journalctl / apt-get / find），
    # 不经过 Python 删除路径，它的 size_dirs 只是用来统计占用，故不受禁用表约束。
    for it in items:
        if it['kind'] != 'age':
            chk('%-12s cmd 项不带 paths（删除交给系统命令）' % it['key'],
                not (it.get('paths') or []))
            continue
        dirs = [sp['dir'] for sp in (it.get('paths') or [])]
        bad = [d for d in dirs if d and not safe(d)]
        chk('%-12s 目录均可用' % it['key'], not bad, bad)

    print('\n--- glob 之间不得重叠（防重复计数）---')
    ns3 = build(['CLEAN_ITEMS', '_clean_files_of', '_clean_excluded'],
                {'os': os, 'fnmatch': __import__('fnmatch'), 'stat': __import__('stat')})
    files_of = ns3['_clean_files_of']
    items2 = ns3['CLEAN_ITEMS']
    tmp = tempfile.mkdtemp(prefix='drover-clean-')
    try:
        logd = os.path.join(tmp, 'logd')
        os.makedirs(logd)
        names = ['ulog.jsonl', 'ulog.jsonl.1', 'ulog.jsonl.1.gz',
                 'all.jsonl', 'all.jsonl.1', 'system.jsonl.2',
                 'dnsmasq.log', 'dnsmasq.log.1']
        for n in names:
            with open(os.path.join(logd, n), 'w') as f:
                f.write('x' * 100)

        def hits(key):
            it = [x for x in items2 if x['key'] == key][0]
            out = set()
            for spec in (it.get('paths') or []):
                spec = dict(spec, dir=logd)
                for f in files_of(spec):
                    out.add(os.path.basename(f['path']))
            return out

        ulog_h = hits('ulog')
        dlog_h = hits('drouter_log')
        chk('ulog 项命中轮转后的流日志',
            {'ulog.jsonl.1', 'ulog.jsonl.1.gz'} <= ulog_h, sorted(ulog_h))
        chk('ulog 项不含活跃文件 ulog.jsonl', 'ulog.jsonl' not in ulog_h)
        chk('面板日志项不含 ulog 系列',
            not any(n.startswith('ulog') for n in dlog_h), sorted(dlog_h))
        chk('面板日志项命中自己的轮转件',
            {'all.jsonl.1', 'system.jsonl.2', 'dnsmasq.log.1'} <= dlog_h, sorted(dlog_h))
        chk('面板日志项不含活跃文件',
            not any(n in dlog_h for n in ('all.jsonl', 'dnsmasq.log')), sorted(dlog_h))
        chk('两项不重叠', not (ulog_h & dlog_h), sorted(ulog_h & dlog_h))

        def peek(key):
            it2 = [x for x in items2 if x['key'] == key][0]
            out = set()
            for spec in (it2.get('peek') or []):
                spec = dict(spec, dir=logd)
                for f in files_of(spec):
                    out.add(os.path.basename(f['path']))
            return out

        for k in ('ulog', 'drouter_log'):
            ph = peek(k)
            hh = hits(k)
            chk('%-12s peek 覆盖活跃文件' % k,
                any(('.' not in n.replace('.jsonl', '').replace('.log', '')) for n in ph),
                sorted(ph))
            chk('%-12s peek 与可清理集不重叠' % k, not (ph & hh), sorted(ph & hh))
        chk('两项 peek 不重叠', not (peek('ulog') & peek('drouter_log')),
            sorted(peek('ulog') & peek('drouter_log')))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print('\n--- exclude 语义 ---')
    exc = ns3['_clean_excluded']
    chk("'drouter/*' 命中目录 drouter 本身", exc('drouter', ['drouter/*']) is True)
    chk("'drouter/*' 命中 drouter/a", exc('drouter/a', ['drouter/*']) is True)
    chk("'drouter/*' 不命中 drx", exc('drx', ['drouter/*']) is False)
    chk("'ulog*' 命中 ulog.jsonl.1", exc('ulog.jsonl.1', ['ulog*']) is True)
    chk("'ulog*' 不命中 system.jsonl.1", exc('system.jsonl.1', ['ulog*']) is False)

    print('\n--- 按天筛选 ---')
    ns4 = build(['CLEAN_ITEMS', '_clean_item_stat', '_clean_files_of', '_clean_excluded',
                 '_clean_dir_bytes'],
                {'os': os, 'fnmatch': __import__('fnmatch'), 'stat': __import__('stat'),
                 'time': time})
    item_stat = ns4['_clean_item_stat']
    tmp2 = tempfile.mkdtemp(prefix='drover-age-')
    try:
        d = os.path.join(tmp2, 'x')
        os.makedirs(d)
        new_f = os.path.join(d, 'new.log.1')
        old_f = os.path.join(d, 'old.log.1')
        for p in (new_f, old_f):
            with open(p, 'w') as f:
                f.write('x' * 1000)
        os.utime(old_f, (time.time() - 20 * 86400, time.time() - 20 * 86400))
        fake = {'key': 't', 'name': 't', 'kind': 'age',
                'paths': [{'dir': d, 'glob': '*.log.1'}]}
        st10 = item_stat(fake, 10)
        st30 = item_stat(fake, 30)
        chk('保留 10 天时只回收过期那个', st10['hit_count'] == 1 and st10['reclaimable'] == 1000)
        chk('保留 30 天时无可回收', st30['hit_count'] == 0 and st30['reclaimable'] == 0)
        chk('总占用与可释放分开统计', st10['total'] == 2000 and st10['files'] == 2)
        chk('days=0 表示全部回收', item_stat(fake, 0)['hit_count'] == 2)
        chk('无 peek 时 active 为 0', item_stat(fake, 10).get('active') == 0)
        fake2 = dict(fake, peek=[{'dir': d, 'glob': '*.log'}])
        with open(os.path.join(d, 'live.log'), 'w') as f:
            f.write('y' * 4096)
        st_p = item_stat(fake2, 10)
        chk('peek 只计入 active，不进可释放量',
            st_p.get('active') == 4096 and st_p['reclaimable'] == 1000,
            'active=%s reclaimable=%s' % (st_p.get('active'), st_p['reclaimable']))
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)

    print('\n--- 默认策略 ---')
    ns5 = build(['_clean_defaults', 'CLEAN_ITEMS'])
    dft = ns5['_clean_defaults']()
    chk('默认开启自动清理', dft['enabled'] is True)
    chk('默认触发方式为 water+timer', dft['trigger'] == 'both')
    chk('默认水位阈值 85%', dft['disk_percent'] == 85)
    chk('默认单次不限量', dft['max_mb_per_run'] == 0)
    chk('默认每项都开启', all(v['on'] for v in dft['items'].values()))
    chk('默认天数取自清单',
        all(dft['items'][it['key']]['days'] == it['days'] for it in ns5['CLEAN_ITEMS']))
    chk('默认执行时间在 0-23', 0 <= dft['hour'] <= 23)

    print('\n--- 定时器接线 ---')
    chk('定义了 wrapper 脚本路径', "CLEAN_WRAPPER = '/opt/drouter/scripts/cleanup-auto.sh'" in HELPER_SRC)
    chk('wrapper 用单引号包住 JSON（绕开 systemd 引号解析）',
        """cleanup '{\\"op\\":\\"auto\\"}'""" in HELPER_SRC)
    chk('service 单元 ExecStart 指向 wrapper',
        "ExecStart=/bin/sh %s" in HELPER_SRC)
    chk('timer 每小时检查一次', 'OnUnitActiveSec=1h' in HELPER_SRC)
    chk('auto 分支会先判断水位与时间',
        "if trig == 'disk':" in HELPER_SRC and "go = watermark or due" in HELPER_SRC)

    print('\n--- 前端接线 ---')
    chk('菜单里有 cleanup', re.search(r"k: 'cleanup'", APP_SRC) is not None)
    chk('菜单文案含「磁盘与日志清理」', '磁盘与日志清理' in APP_SRC)
    chk('VIEWS 注册 cleanup', re.search(r"^\s*cleanup: viewCleanup,", APP_SRC, re.M) is not None)
    chk('定义了 viewCleanup', 'async function viewCleanup(' in APP_SRC)
    for fn in ('cleanupLoad', 'cleanupRenderDisk', 'cleanupRenderPolicy',
               'cleanupRenderItems', 'cleanupSave', 'cleanupRun', 'cleanupSelectAll'):
        chk('前端函数 %s' % fn, ('function %s(' % fn) in APP_SRC)
    _seg = APP_SRC[APP_SRC.index('async function viewCleanup'):
                   APP_SRC.index('async function cleanupRun')]
    chk('清理前有二次确认', 'confirm(' in _seg)
    chk('提供试运行入口', "op: 'run', dry: !!dry" in APP_SRC)

    print('\n--- 依赖登记 ---')
    chk('coreutils（df/du）已登记',
        re.search(r"\('coreutils'.*?'coreutils'", HELPER_SRC, re.S) is not None)

    print('\n--- 结果文案 ---')
    chk('小数量改用 KB 而不是 0.0 MB',
        "('%d KB' % max(1, round(freed / 1024.0)))" in HELPER_SRC)
    chk('试运行文案区别于真删', '试运行，未真正删除' in HELPER_SRC)

    print('\n--- 自动触发的时刻判定（0 点陷阱）---')
    # 前端 Number(v) || 4 和后端 int(cfg.get('hour') or 4) 都会把用户选的
    # 「每天 00:00」当成「没填」而改成 04:00 —— 而且前端那条还顺手把 0 也一起吞了，
    # 于是这个设置在界面上根本存不下来。三处都要锁死。
    # 注意：要先把注释剥掉再比对 —— 解释「为什么不能这么写」的注释里
    # 恰恰会引用这段错误写法，直接子串匹配会误报（第一版就踩了）。
    def code_only(src, marker):
        return '\n'.join(ln.split(marker, 1)[0] for ln in src.splitlines())

    chk('后端不再用 or 4 取执行小时',
        "int(cfg.get('hour') or 4)" not in code_only(HELPER_SRC, '#'))
    chk('后端显式区分 None 与 0', "_raw_hour in (None, '')" in HELPER_SRC)
    chk('前端回显不再用 c.hour || 4',
        'c.hour || 4' not in code_only(APP_SRC, '//'))
    chk('前端保存不再用 Number(...) || 4',
        "Number($('#cl-hour').value) || 4" not in code_only(APP_SRC, '//'))

    class _Now(object):
        def __init__(self, hour, today, wd):
            self.hour = hour
            self._today = today
            self._wd = wd

        def weekday(self):
            return self._wd

        def strftime(self, fmt):
            return self._today

    class _DT(object):
        def __init__(self, now):
            self._now = now

        def now(self):
            return self._now

    def run_auto(hour, now_hour, last_date, today='2026-10-01', schedule='daily',
                 trig='both', wd=1, pct=10.0, enabled=True):
        """返回 (是否真执行了清理, 结果)。today 与 last_date 必须分开传！"""
        fired = []
        ns = {
            'datetime': _DT(_Now(now_hour, today, wd)),
            'ok': lambda d=None, m='ok': {'ok': True, 'data': d, 'msg_cn': m},
            '_clean_load': lambda: {'enabled': enabled, 'trigger': trig,
                                    'disk_percent': 85, 'schedule': schedule,
                                    'hour': hour, 'max_mb_per_run': 0},
            '_disk_usage': lambda p: (100, pct, 100 - pct),
            '_clean_state_load': lambda: {'last_date': last_date},
            '_clean_do': lambda p, dry=False, auto=False: (fired.append(p), {'ok': True})[1],
        }
        ns = build(['_clean_auto'], ns)
        # 必须先调用再取 fired —— 写成 `return bool(fired), ns['_clean_auto']()`
        # 的话，元组是从左到右求值的，bool(fired) 会在函数真正执行前就算好。
        result = ns['_clean_auto']()
        return bool(fired), result

    chk('hour=0、当前 0 点 → 触发（0 不能被当成没填）',
        run_auto(0, 0, '2026-09-30')[0])
    chk('hour=0 且今天跑过 → 不重复触发', not run_auto(0, 0, '2026-10-01')[0])
    chk('hour=4、当前 3 点 → 未到点不触发', not run_auto(4, 3, '2026-09-30')[0])
    chk('hour=4、当前 5 点 → 当天补做（关机错过 4 点也不漏）',
        run_auto(4, 5, '2026-09-30')[0])
    chk('hour=23、当前 5 点 → 未到点不触发', not run_auto(23, 5, '2026-09-30')[0])
    # datetime.weekday()：周一=0，周日=6（注意别按「周日=0」的直觉写）
    chk('每周一：周一 5 点（计划 4 点）补做',
        run_auto(4, 5, '2026-09-30', schedule='weekly', wd=0)[0])
    chk('每周一：周二不触发',
        not run_auto(4, 5, '2026-09-30', schedule='weekly', wd=1)[0])
    chk('每周一：周三不触发',
        not run_auto(4, 5, '2026-09-30', schedule='weekly', wd=2)[0])
    chk('水位触发与时刻无关',
        run_auto(4, 3, '2026-10-01', trig='disk', pct=90.0)[0])
    chk('总开关关了就不跑', not run_auto(0, 0, '2026-09-30', enabled=False)[0])

    print('\n' + '=' * 60)
    print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())

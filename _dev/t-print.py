#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打印服务 CUPS / USB RAW 直通（#7）：静态契约 + 纯函数 + 互斥逻辑单测。

盯五件事：
  ① CUPS 与 RAW 互斥 —— 它们抢同一个 USB 设备。切到 RAW 必须先把
     cups / cups.socket / cups-browsed 停干净，切到 CUPS 必须停掉 RAW 服务；
  ② 出厂默认「允许远程管理」必须是关的 —— 打印服务器不该让同网段任何人
     都能增删队列；
  ③ cupsctl --remote-any 会写成 `Allow all`（放行一切来源，WAN 侧也能进），
     本页必须把它收窄成 `Allow @LOCAL`；
  ④ 队列名与设备 URI 必须有白名单 —— lpadmin 虽然走列表调用不过 shell，
     但这些值会落进 printers.conf，不能让奇怪的字符进去；
  ⑤ 保存只写配置，构建保护模式下绝不启停服务。
"""
import ast
import io
import os
import re
import sys
import json
import shutil
import tempfile

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
    for n in TREE.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return n
        elif isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def build(names, ns=None):
    ns = ns if ns is not None else {}
    body = []
    for nm in names:
        n = node(nm)
        if n is None:
            raise AssertionError('源码里找不到 %s' % nm)
        body.append(n)
    mod = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, '<print>', 'exec'), ns)
    return ns


# ---------------------------------------------------------------- 测试替身
CALLS = []


def fake_sh(argv, timeout=10, input_data=None):
    CALLS.append(list(argv))
    # cupsd -t / cupsctl：一律假装成功，避免真去改目标机
    return (0, 'ok', '')


class ValidateError(Exception):
    def __init__(self, msg, code='BAD_ARG'):
        Exception.__init__(self, msg)
        self.msg_cn = msg
        self.code = code


def _ok(data=None, msg='ok'):
    return {'ok': True, 'msg_cn': msg, 'data': data or {}}


def _fail(msg, code='ERR'):
    return {'ok': False, 'msg_cn': msg, 'code': code}


NS = build([
    'PRINT_DEFAULTS', 'PRINT_ITEMS', 'PRINT_DRIVERS', 'PRINT_PKGS',
    'PRINT_URI_SCHEMES', 'PRINT_NAME_RE', 'PRINT_USB_IFCLASS_PRINTER',
    'PRINT_RAW_UNIT_BODY',
    '_print_parse_lsusb',
    '_print_parse_printers_conf', '_print_parse_jobs',
    '_print_parse_lpstat_p', '_print_c_locale_env',
    '_print_patch_location', '_print_cupsctl_args',
    '_print_queue_name', '_print_uri',
    '_print_load', '_print_save', '_atomic_write',
    '_print_backup', '_print_prune_backups', '_print_backups',
], {
    're': re, 'os': os, 'json': json, 'shutil': shutil, 'sh': fake_sh,
    'tempfile': tempfile,
    'ValidateError': ValidateError,
    # 源码是 `from datetime import datetime`，注入的必须是类不是模块
    'datetime': __import__('datetime').datetime,
    # PRINT_RAW_UNIT_BODY 是拼接出来的，依赖这个值；先占位，build 完再换成临时路径
    'PRINT_RAW_WRAPPER': '/opt/drouter/scripts/printer-raw.sh',
    # _print_usb_ifclass 的默认参数；build 完再换成临时目录
    'PRINT_USB_CLASS_DIR': '/sys/bus/usb/devices',
})
# 路径常量一律重定向到临时目录 —— 绝不抽源码里的常量节点（会覆盖这里注入的值，
# 让测试读写真实路径造成跨次污染）
TMP = tempfile.mkdtemp(prefix='t-print-')
NS['PRINT_CONF'] = os.path.join(TMP, 'print.conf')
NS['PRINT_CUPSD'] = os.path.join(TMP, 'cupsd.conf')
NS['PRINT_BAK_DIR'] = os.path.join(TMP, 'bak')
NS['PRINT_BAK_KEEP'] = 5
NS['PRINT_RAW_SERVICE'] = 'drouter-printer-raw.service'
NS['PRINT_RAW_WRAPPER'] = os.path.join(TMP, 'printer-raw.sh')
NS['PRINT_USB_CLASS_DIR'] = os.path.join(TMP, 'sysfs')

# ------------------------------------------------- 假 sysfs（给 usb_ifclass 用）
# 结构：设备名 → {接口目录名: bInterfaceClass}
FAKE_SYSFS = {
    '1-1': {'1-1:1.0': '07\n'},                    # 打印机
    '1-2': {'1-2:1.0': '08\n'},                    # U 盘（大容量存储）
    '1-3': {'1-3:1.0': '03\n'},                    # 键鼠（HID）
    '1-4': {},                                     # 没接口目录（例如 hub）
    '1-5': {'1-5:1.0': '03\n', '1-5:1.1': '07\n'},  # 多接口，其一命中
}


class _ShimPath(object):
    """源码走的是 os.path.isdir，所以替身必须挂在 path 上，不能只挂 os.isdir。"""
    join = staticmethod(os.path.join)
    basename = staticmethod(os.path.basename)

    @staticmethod
    def isdir(d):
        rel = os.path.basename(str(d).rstrip('/\\'))
        return rel == 'devices' or rel in FAKE_SYSFS


class _ShimOS(object):
    path = _ShimPath

    @staticmethod
    def listdir(d):
        rel = os.path.basename(d.rstrip('/\\'))
        if rel == 'devices':
            return sorted(FAKE_SYSFS.keys())
        if rel in FAKE_SYSFS:
            return sorted(FAKE_SYSFS[rel].keys())
        raise OSError('no such directory: %s' % d)

    @staticmethod
    def isdir(d):
        return _ShimPath.isdir(d)


class _ShimFile(object):
    def __init__(self, path):
        parts = path.replace('\\', '/').rstrip('/').split('/')
        self._v = FAKE_SYSFS[parts[-3]][parts[-2]]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._v


def _shim_open(path, *a, **k):
    return _ShimFile(path)


usb_ifclass = build(['_print_usb_ifclass'], {
    'os': _ShimOS, 'open': _shim_open,
    'PRINT_USB_IFCLASS_PRINTER': NS['PRINT_USB_IFCLASS_PRINTER'],
    'PRINT_USB_CLASS_DIR': '/sys/bus/usb/devices',
})['_print_usb_ifclass']

DEFAULTS = NS['PRINT_DEFAULTS']
ITEMS = NS['PRINT_ITEMS']
DRIVERS = NS['PRINT_DRIVERS']
PKGS = NS['PRINT_PKGS']
URI_SCHEMES = NS['PRINT_URI_SCHEMES']
NAME_RE = NS['PRINT_NAME_RE']
UNIT_BODY = NS['PRINT_RAW_UNIT_BODY']
IFCLASS = NS['PRINT_USB_IFCLASS_PRINTER']

parse_lsusb = NS['_print_parse_lsusb']
parse_conf = NS['_print_parse_printers_conf']
parse_lpstat_p = NS['_print_parse_lpstat_p']
c_locale_env = NS['_print_c_locale_env']
parse_jobs = NS['_print_parse_jobs']
patch_loc = NS['_print_patch_location']
cupsctl_args = NS['_print_cupsctl_args']
q_name = NS['_print_queue_name']
q_uri = NS['_print_uri']
p_load = NS['_print_load']
p_save = NS['_print_save']
p_backup = NS['_print_backup']
p_backups = NS['_print_backups']


def sech(text, allow='@LOCAL'):
    return patch_loc(text, allow)


def main():
    print('--- 后端动作与路由 ---')
    chk('定义了 act_print', 'def act_print(' in HELPER_SRC)
    chk('注册到 ACTIONS', re.search(r"^\s*'print':\s*act_print,", HELPER_SRC, re.M) is not None)
    chk('Web 路由 /api/print', "'/api/print'" in WEB_SRC)
    chk('save 给了长超时', re.search(r"to = 150 if op == 'save'", WEB_SRC) is not None)
    chk('testpage / queue_add 给了长超时',
        "100 if op in ('testpage', 'queue_add')" in WEB_SRC)
    chk('未知 op 返回 fail', '不支持的打印服务操作' in HELPER_SRC)
    chk('配置文件路径正确', "PRINT_CONF = '/etc/drouter/print.conf'" in HELPER_SRC)
    chk('cupsd.conf 路径正确', "PRINT_CUPSD = '/etc/cups/cupsd.conf'" in HELPER_SRC)

    print('\n--- 默认值（我替用户做的判断）---')
    chk('默认 CUPS 模式（功能最全，手机免驱）', DEFAULTS['mode'] == 'cups')
    chk('默认允许局域网访问（CUPS 出厂只听 127.0.0.1，等于白装）',
        DEFAULTS['cups']['listen'] == 'lan')
    chk('默认共享打印机', DEFAULTS['cups']['share'] is True)
    chk('默认开 CUPS Web 界面（方便排障）', DEFAULTS['cups']['web_iface'] is True)
    chk('默认【禁止】远程管理 —— 安全红线', DEFAULTS['cups']['remote_admin'] is False)
    chk('默认开 cups-browsed 自动发现', DEFAULTS['cups']['browsed'] is True)
    chk('RAW 默认设备 /dev/usb/lp0', DEFAULTS['raw']['device'] == '/dev/usb/lp0')
    chk('RAW 默认端口 9100（打印机的行业标准端口）', DEFAULTS['raw']['port'] == 9100)
    chk('RAW 默认监听全部网卡', DEFAULTS['raw']['bind'] == '0.0.0.0')

    print('\n--- 说明条目 PRINT_ITEMS ---')
    keys = [i['key'] for i in ITEMS]
    chk('六个开关都有说明', len(ITEMS) == 6, 'keys=%s' % keys)
    chk('覆盖全部可配置项',
        set(keys) == set(['mode', 'listen', 'share', 'web_iface',
                          'remote_admin', 'browsed']))
    chk('每条都有 why 与 impact',
        all(i.get('why') and isinstance(i.get('impact'), list)
            and len(i['impact']) >= 2 for i in ITEMS))
    chk('说了 CUPS/RAW 抢设备这件事',
        any('抢同一个 USB' in i['why'] for i in ITEMS if i['key'] == 'mode'))
    chk('说了没插打印机时 RAW 没意义',
        any('RAW 模式没有意义' in x for i in ITEMS if i['key'] == 'mode'
            for x in i['impact']))
    chk('说了 @LOCAL 不会暴露到公网',
        any('@LOCAL' in x for i in ITEMS if i['key'] == 'listen'
            for x in i['impact']))
    chk('依赖包清单完整',
        set(PKGS) == set(['cups', 'cups-client', 'cups-daemon', 'cups-browsed']))
    chk('两种驱动（免驱 / 原样转发）',
        [d['key'] for d in DRIVERS] == ['everywhere', 'raw'])

    print('\n--- lsusb 解析 ---')
    LSUSB = ('Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub\n'
             'Bus 001 Device 002: ID 0627:0001 Adomax Technology Co., Ltd QEMU Tablet\n'
             'Bus 002 Device 003: ID 03f0:5a11 HP OfficeJet Pro 7740\n'
             'garbage line\n')
    u = parse_lsusb(LSUSB)
    chk('解析出 3 台设备（跳过垃圾行）', len(u) == 3, 'n=%d' % len(u))
    chk('VID/PID 正确且转小写', u[1]['vid'] == '0627' and u[1]['pid'] == '0001')
    chk('设备名正确', u[2]['name'] == 'HP OfficeJet Pro 7740')
    chk('总线与设备号正确', u[2]['bus'] == '002' and u[2]['dev'] == '003')
    chk('空输入不炸', parse_lsusb('') == [] and parse_lsusb(None) == [])

    print('\n--- USB Printer Class 识别（只认 07）---')
    # 接口目录名形如「1-1:1.0」，带冒号 —— Windows 上建不出这种目录，
    # 所以给 os / open 注入一份假 sysfs，逻辑照测且不受平台限制。
    got = usb_ifclass('/sys/bus/usb/devices')
    chk('只挑出打印机类设备', got == ['1-1', '1-5'], 'got=%s' % got)
    chk('U 盘（08 大容量存储）没被误认成打印机', '1-2' not in got)
    chk('键鼠（03 HID）没被误认成打印机', '1-3' not in got)
    chk('没有接口目录的设备跳过', '1-4' not in got)
    chk('多接口设备只要有一个是打印机就命中', '1-5' in got)
    chk('识别码常量含 07 与 7', '07' in IFCLASS and '7' in IFCLASS)
    chk('目录不存在时返回空列表', usb_ifclass('/no/such/dir') == [])

    print('\n--- printers.conf 解析（不用 lpstat：它会被本地化）---')
    PCONF = """
# Printer configuration file for CUPS
<DefaultPrinter HP_OfficeJet>
UUID urn:uuid:6b1f2a53-0000-0000-0000-000000000000
Info HP OfficeJet Pro 7740 series
Location 客厅
MakeModel HP OfficeJet Pro 7740 series
DeviceURI ipp://HP_OfficeJet._ipp._tcp.local/
State Idle
StateTime 1759100000
Accepting Yes
Shared No
JobSheets none none
QuotaPeriod 0
PageLimit 0
</Printer>
<Printer Laser>
DeviceURI socket://192.168.7.50:9100
Accepting No
Shared Yes
State Stopped
</Printer>
"""
    qs = parse_conf(PCONF)
    chk('解析出 2 个队列', len(qs) == 2, 'n=%d' % len(qs))
    q0 = qs[0]
    chk('队列名正确', q0['name'] == 'HP_OfficeJet', q0.get('name'))
    chk('默认队列被标记', q0['default'] is True and qs[1]['default'] is False)
    chk('URI 正确', q0['uri'] == 'ipp://HP_OfficeJet._ipp._tcp.local/')
    chk('Info / Location / MakeModel 正确',
        q0['info'] == 'HP OfficeJet Pro 7740 series'
        and q0['location'] == '客厅'
        and q0['make_model'] == 'HP OfficeJet Pro 7740 series')
    chk('Accepting Yes → True', q0['accepting'] is True)
    chk('这台机器实测的 Shared No → False', q0['shared'] is False)
    chk('第二个队列 Accepting No → False', qs[1]['accepting'] is False)
    chk('第二个队列 Shared Yes → True', qs[1]['shared'] is True)
    chk('socket URI 也能解析', qs[1]['uri'] == 'socket://192.168.7.50:9100')
    chk('空输入不炸', parse_conf('') == [] and parse_conf(None) == [])

    print('\n--- lpstat -p 解析（必须配 LC_ALL=C，否则输出是中文）---')
    LPP = ('printer drouter-probe is idle.  enabled since Tue Sep 29 23:57:46 2026\n'
           'printer HP_7740 is printing.  enabled since Tue Sep 29 23:00:00 2026\n'
           'printer Old_Laser disabled since Mon Sep 28 10:00:00 2026\n'
           'garbage\n')
    lp = parse_lpstat_p(LPP)
    chk('解析出 3 个队列', len(lp) == 3, 'n=%d' % len(lp))
    chk('队列名正确', [x['name'] for x in lp]
        == ['drouter-probe', 'HP_7740', 'Old_Laser'])
    chk('状态正确（停用的那种没有 is xxx 段，状态留空）',
        [x['state'] for x in lp] == ['idle', 'printing', ''])
    chk('enabled → accepting=True', lp[0]['accepting'] is True)
    chk('disabled → accepting=False', lp[2]['accepting'] is False)
    chk('空输入不炸', parse_lpstat_p('') == [] and parse_lpstat_p(None) == [])
    env = c_locale_env()
    chk('强制 LC_ALL=C（否则 lpstat 输出中文没法解析）', env.get('LC_ALL') == 'C')
    chk('LANG 一并置为 C', env.get('LANG') == 'C')
    chk('清掉 LANGUAGE（它的优先级比 LC_ALL 还高）', 'LANGUAGE' not in env)
    chk('保留原有环境变量（PATH 之类不能丢）', 'PATH' in env)
    chk('源码里 lpstat -p 确实带了 env',
        re.search(r"sh\(\['lpstat', '-p'\].*env=", HELPER_SRC) is not None)
    chk('增删队列后会重启 cups 逼它落盘',
        HELPER_SRC.count("sh(['systemctl', 'restart', 'cups'], timeout=60)") >= 3)

    print('\n--- lpstat 任务解析（第一列是「队列名-任务号」，不受本地化影响）---')
    JOBS = ('HP_OfficeJet-42      root        1024   2026年09月29日 10:00:00\n'
            'Laser-7              ajeef      51200  自从 2026年09月29日 开始\n'
            'not-a-job\n')
    js = parse_jobs(JOBS)
    chk('解析出 2 个任务', len(js) == 2, 'n=%d' % len(js))
    chk('任务号完整', js[0]['id'] == 'HP_OfficeJet-42')
    chk('队列名与号拆分正确', js[0]['queue'] == 'HP_OfficeJet' and js[0]['job'] == '42')
    chk('用户名正确', js[0]['user'] == 'root')
    chk('中文时间列不影响解析', js[1]['queue'] == 'Laser' and js[1]['job'] == '7')
    chk('垃圾行被跳过', all('not-a-job' not in j['id'] for j in js))
    chk('空输入不炸', parse_jobs('') == [] and parse_jobs(None) == [])

    print('\n--- 收窄 cupsctl 的 Allow all（关键安全边界）---')
    before = ('Port 631\n'
              '<Location />\n'
              '  Order allow,deny\n'
              '  Allow all\n'
              '</Location>\n'
              '<Location /admin>\n'
              '  Allow all\n'
              '</Location>\n')
    after = sech(before)
    chk('<Location /> 里的 Allow all 被换成 @LOCAL', '  Allow @LOCAL' in after)
    # 只断言「根 Location 段里」没有 Allow all —— /admin 段本来就该保留原样
    root_blk = after.split('<Location />', 1)[1].split('</Location>', 1)[0]
    chk('根 Location 段里不再有 Allow all', 'Allow all' not in root_blk,
        repr(root_blk))
    chk('只改 <Location />，/admin 段不动',
         re.search(r'<Location /admin>\n\s*Allow all', after) is not None)
    chk('Port 631 保留', 'Port 631' in after)
    chk('Order allow,deny 保留', 'Order allow,deny' in after)

    multi = ('<Location />\n  Allow all\n  Allow 192.168.7.0/24\n</Location>\n')
    m2 = sech(multi)
    chk('多余的 Allow 行被合并成一条', m2.count('Allow @LOCAL') == 1)
    chk('不再残留 192.168.7.0/24', '192.168.7.0/24' not in m2)

    none = 'Port 631\nListen localhost:631\n'
    n2 = sech(none)
    chk('文件里没有 <Location /> 时会补一整段', '<Location />' in n2)
    chk('补的段里带 Allow @LOCAL', 'Allow @LOCAL' in n2)
    chk('补的段完整闭合', n2.rstrip().endswith('</Location>'))

    print('\n--- cupsctl 参数生成 ---')
    a = cupsctl_args({'cups': {'listen': 'lan', 'share': True,
                               'web_iface': True, 'remote_admin': False}})
    chk('第一个参数是 cupsctl', a[0] == 'cupsctl')
    chk('listen=lan → --remote-any', '--remote-any' in a)
    chk('share → --share-printers', '--share-printers' in a)
    chk('web_iface → WebInterface=Yes', 'WebInterface=Yes' in a)
    chk('remote_admin=关 → --no-remote-admin', '--no-remote-admin' in a)
    a2 = cupsctl_args({'cups': {'listen': 'local', 'share': False,
                                'web_iface': False, 'remote_admin': True}})
    chk('listen=local → --no-remote-any', '--no-remote-any' in a2)
    chk('不共享 → --no-share-printers', '--no-share-printers' in a2)
    chk('关界面 → WebInterface=No', 'WebInterface=No' in a2)
    chk('允许远程管理 → --remote-admin', '--remote-admin' in a2)
    chk('参数个数固定为 5', len(a) == 5 and len(a2) == 5)
    chk('空配置不炸', cupsctl_args({})[0] == 'cupsctl')
    chk('缺 cups 键不炸', cupsctl_args(None)[0] == 'cupsctl')

    print('\n--- 队列名与 URI 白名单 ---')
    for good in ('HP_7740', 'laser-1', 'a', 'Office.Jet', 'Q:1'):
        try:
            q_name(good)
            chk('合法队列名通过：%s' % good, True)
        except ValidateError:
            chk('合法队列名通过：%s' % good, False)
    for bad in ('', '  ', '-abc', '1abc/../x', 'has space', 'a' * 65, 'x;rm -rf'):
        try:
            q_name(bad)
            chk('非法队列名被拒：%r' % bad, False)
        except ValidateError:
            chk('非法队列名被拒：%r' % bad, True)
    chk('最长 64 字符的队列名放行', q_name('a' * 64) == 'a' * 64)

    for good in ('ipp://HP._ipp._tcp.local/', 'socket://192.168.7.50:9100',
                 'usb://HP/OfficeJet', 'ipps://printer.local/ipp/print',
                 'dnssd://HP%20OfficeJet._ipp._tcp.local/'):
        try:
            q_uri(good)
            chk('合法 URI 通过：%s' % good, True)
        except ValidateError:
            chk('合法 URI 通过：%s' % good, False)
    for bad in ('', 'ftp://x/', 'ipp://a b', 'ipp://x; rm -rf /',
                'ipp://x|ls', 'ipp://x$(id)', 'random-string'):
        try:
            q_uri(bad)
            chk('非法 URI 被拒：%r' % bad, False)
        except ValidateError:
            chk('非法 URI 被拒：%r' % bad, True)
    chk('URI 方案白名单含 ipp/socket/usb/dnssd',
        all(s in URI_SCHEMES for s in ('ipp://', 'socket://', 'usb://', 'dnssd://')))
    chk('URI 长度上限 500', q_uri('ipp://' + 'a' * 400) is not None)
    try:
        q_uri('ipp://' + 'a' * 600)
        chk('超长 URI 被拒', False)
    except ValidateError:
        chk('超长 URI 被拒', True)

    print('\n--- 模式互斥（本页最核心的一条）---')

    def apply_with(mode, browsed=True):
        CALLS[:] = []
        hits = {'raw': 0, 'cups': 0, 'stop': 0}

        def fake_raw(cfg, errs):
            hits['raw'] += 1
            return True

        def fake_cups(cfg, errs):
            hits['cups'] += 1
            return True

        def fake_stop(errs):
            hits['stop'] += 1

        ns = {
            'sh': fake_sh,
            'PRINT_RAW_SERVICE': 'drouter-printer-raw.service',
            '_print_apply_raw': fake_raw,
            '_print_apply_cups': fake_cups,
            '_print_stop_all': fake_stop,
        }
        build(['_print_apply'], ns)
        ns['_print_apply']({'mode': mode, 'cups': {'browsed': browsed}}, [])
        return hits, list(CALLS)

    h, calls = apply_with('raw')
    stopped = [' '.join(c) for c in calls if 'stop' in c or 'disable' in c]
    chk('RAW 模式会走 RAW 应用流程', h['raw'] == 1)
    chk('RAW 模式不会走 CUPS 流程', h['cups'] == 0 and h['stop'] == 0)
    chk('RAW 模式停掉 cups.service',
        any('systemctl stop cups.service' in s for s in stopped))
    chk('RAW 模式停掉 cups.socket（不然会被 socket 激活拉起来）',
        any('systemctl stop cups.socket' in s for s in stopped))
    chk('RAW 模式停掉 cups-browsed',
        any('systemctl stop cups-browsed.service' in s for s in stopped))
    chk('RAW 模式把 cups 系列也 disable 掉',
        sum(1 for s in stopped if 'disable' in s) >= 3)

    h, calls = apply_with('cups')
    chk('CUPS 模式会走 CUPS 流程', h['cups'] == 1)
    chk('CUPS 模式不会走 RAW 流程', h['raw'] == 0 and h['stop'] == 0)
    chk('CUPS 模式停掉 RAW 服务',
        any('stop drouter-printer-raw.service' in ' '.join(c) for c in calls))
    chk('CUPS 模式把 RAW 服务 disable 掉',
        any('disable drouter-printer-raw.service' in ' '.join(c) for c in calls))
    chk('CUPS 模式 browsed=开 → 启 cups-browsed',
        any('restart cups-browsed.service' in ' '.join(c) for c in calls))

    h, calls = apply_with('cups', browsed=False)
    chk('browsed=关 → 停并禁用 cups-browsed',
        any('stop cups-browsed.service' in ' '.join(c) for c in calls)
        and any('disable cups-browsed.service' in ' '.join(c) for c in calls))

    h, calls = apply_with('off')
    chk('关闭模式走 _print_stop_all', h['stop'] == 1)
    chk('关闭模式不启动任何一侧', h['raw'] == 0 and h['cups'] == 0)

    print('\n--- 从「关闭」切回 CUPS：必须先把 cupsd 拉起来 ---')
    # cupsctl 是连到 cupsd 的 socket 下命令的，不是直接改文件。cups 没在跑时
    # 它必然失败（真机实测报「无法连接服务器：错误的文件描述符」），
    # 而且失败后直接 return 会让 cups 一直停着 —— 切回 CUPS 模式等于切了个寂寞。

    CUPSD_T = os.path.join(TMP, 'cupsd.conf')

    def apply_cups_with(active, cupsctl_rc=0, cupsd_rc=0):
        # 给一份能被 _print_patch_location 处理的 cupsd.conf
        with open(CUPSD_T, 'w', encoding='utf-8') as f:
            f.write('Listen localhost:631\n'
                    '<Location />\n  Order allow,deny\n  Allow all\n</Location>\n')
        CALLS[:] = []

        def fake_sh2(argv, timeout=10, input_data=None):
            CALLS.append(list(argv))
            if argv[:1] == ['cupsctl']:
                return (cupsctl_rc, '', 'boom' if cupsctl_rc else '')
            if argv[:2] == ['cupsd', '-t']:
                return (cupsd_rc, '', 'bad config' if cupsd_rc else '')
            return (0, 'x', '')

        def fake_service(name):
            return {'active': 'active' if active else 'inactive',
                    'enabled': 'enabled' if active else 'disabled'}

        def fake_svc_states(names):
            # _print_apply_cups 现在走批量接口 _svc_states（一次 systemctl show）。
            # 测试替身按同一语义返回：把所有请求的单元都映射成同一个状态。
            return {n: fake_service(n) for n in names}

        ns = {
            'sh': fake_sh2, 'os': os, 'shutil': shutil, 're': re,
            'PRINT_CUPSD': CUPSD_T,
            '_print_service': fake_service,
            '_svc_states': fake_svc_states,
            '_print_backup': lambda: '',
            '_print_cupsctl_args': cupsctl_args,
            '_print_patch_location': patch_loc,
        }
        build(['_print_apply_cups'], ns)
        errs = []
        okk = ns['_print_apply_cups']({'cups': {'listen': 'lan'}}, errs)
        # 注意用 argv 精确比对：'restart' 里也含 'start' 子串，用 in 会误判
        return okk, errs, CALLS

    okk, errs, calls = apply_cups_with(active=False)
    chk('cups 没在跑时会先 enable', ['systemctl', 'enable', 'cups.service'] in calls)
    chk('cups 没在跑时会先 start', ['systemctl', 'start', 'cups.service'] in calls)
    istart = next((i for i, c in enumerate(calls)
                   if c == ['systemctl', 'start', 'cups.service']), -1)
    ictl = next((i for i, c in enumerate(calls) if c and c[0] == 'cupsctl'), -1)
    chk('start 严格排在 cupsctl 之前', istart >= 0 and ictl > istart,
        'start@%d cupsctl@%d' % (istart, ictl))
    chk('拉起来之后才下 cupsctl 命令', okk is True and not errs, '→ %s' % errs)
    chk('收窄照样生效',
        'Allow @LOCAL' in io.open(CUPSD_T, encoding='utf-8').read())

    okk, errs, calls = apply_cups_with(active=True)
    chk('cups 已在跑时不重复 start',
        ['systemctl', 'start', 'cups.service'] not in calls)
    chk('cups 已在跑时直接下命令', okk is True and not errs, '→ %s' % errs)

    okk, errs, calls = apply_cups_with(active=False, cupsctl_rc=1)
    chk('cupsctl 失败会被记进错误', okk is False and any('cupsctl 执行失败' in e for e in errs))
    chk('cupsctl 失败时不会继续改配置',
        not any(c[:2] == ['cupsd', '-t'] for c in calls))

    okk, errs, calls = apply_cups_with(active=True, cupsd_rc=1)
    chk('cupsd -t 不通过会被记进错误',
        okk is False and any('配置校验未通过' in e for e in errs))

    print('\n--- RAW 直通服务单元 ---')
    chk('单元里有 Description', 'Description=' in UNIT_BODY)
    chk('单元里显式写全 PATH（systemd 默认 PATH 没有 /usr/sbin）',
        'Environment=PATH=' in UNIT_BODY and '/usr/sbin' in UNIT_BODY)
    chk('单元里有 ExecStart', 'ExecStart=' in UNIT_BODY)
    chk('失败自动重启', 'Restart=on-failure' in UNIT_BODY)
    chk('开机自启挂 multi-user.target', 'WantedBy=multi-user.target' in UNIT_BODY)
    chk('等网络就绪再起', 'network-online.target' in UNIT_BODY)
    chk('等 udev settle（USB 设备节点要晚一点才出现）',
        'systemd-udev-settle.service' in UNIT_BODY)
    chk('源码里用 GOPEN 而不是 FILE（FILE 开字符设备会截断）',
        'GOPEN:%s' in HELPER_SRC and 'GOPEN' in HELPER_SRC)
    chk('RAW wrapper 用 socat', 'socat TCP-LISTEN' in HELPER_SRC)
    chk('socat 带 fork（支持多客户端）', 'fork' in HELPER_SRC)
    chk('socat 带 reuseaddr', 'reuseaddr' in HELPER_SRC)

    print('\n--- 配置读写 ---')
    cfg = p_load()
    chk('无配置文件时返回出厂默认', cfg['mode'] == 'cups'
        and cfg['cups']['listen'] == 'lan', json.dumps(cfg, ensure_ascii=False)[:80])
    p_save({'mode': 'raw', 'cups': dict(DEFAULTS['cups']),
            'raw': {'device': '/dev/usb/lp1', 'port': 9101, 'bind': '192.168.7.3'}})
    cfg2 = p_load()
    chk('保存后能读回 mode=raw', cfg2['mode'] == 'raw')
    chk('保存后能读回 RAW 设备', cfg2['raw']['device'] == '/dev/usb/lp1')
    chk('保存后能读回端口', cfg2['raw']['port'] == 9101)
    chk('保存后能读回绑定地址', cfg2['raw']['bind'] == '192.168.7.3')
    with open(NS['PRINT_CONF'], 'w', encoding='utf-8') as f:
        f.write('{ this is not json')
    chk('配置文件损坏时回落默认，不崩', p_load()['mode'] == 'cups')
    with open(NS['PRINT_CONF'], 'w', encoding='utf-8') as f:
        f.write('{"mode": "bogus"}')
    chk('非法 mode 值被忽略', p_load()['mode'] == 'cups')
    os.remove(NS['PRINT_CONF'])

    print('\n--- cupsd.conf 备份与还原 ---')
    with open(NS['PRINT_CUPSD'], 'w', encoding='utf-8') as f:
        f.write('Port 631\n')
    b1 = p_backup()
    chk('能备份 cupsd.conf', bool(b1) and os.path.isfile(b1))
    chk('备份文件名带时间戳',
        re.match(r'^cupsd-\d{8}-\d{6}\.conf$', os.path.basename(b1)) is not None)
    chk('备份内容与原文件一致',
        io.open(b1, encoding='utf-8').read() == 'Port 631\n')
    # 时间戳只精确到秒，同一秒里连续备份会互相覆盖 —— 所以这里手工铺 8 份
    # 不同时间戳的备份来验证滚动裁剪，而不是靠 sleep 拖慢测试。
    for i in range(1, 9):
        with open(os.path.join(NS['PRINT_BAK_DIR'],
                               'cupsd-2026092%d-10101%d.conf' % (i % 9, i % 9)),
                  'w') as f:
            f.write('bak %d\n' % i)
    NS['_print_prune_backups']()
    lst = p_backups()
    chk('备份能列出', len(lst) > 1)
    chk('备份按 PRINT_BAK_KEEP=5 滚动裁剪', len(lst) == 5, 'n=%d' % len(lst))
    chk('保留的是最新的 5 份',
        [x['file'] for x in lst] == sorted([x['file'] for x in lst], reverse=True)[:5])
    chk('列表按时间倒序（新的在前）',
        [x['file'] for x in lst] == sorted([x['file'] for x in lst], reverse=True))
    chk('列表带大小与可读时间',
        all(isinstance(x.get('size'), int) and ':' in (x.get('mtime') or '')
            for x in lst))
    os.remove(NS['PRINT_CUPSD'])
    chk('cupsd.conf 不存在时不备份也不崩', p_backup() == '')

    print('\n--- 还原：文件名白名单 + 防目录穿越 ---')
    ns = {
        're': re, 'os': os, 'shutil': shutil, 'sh': fake_sh,
        'PRINT_BAK_DIR': NS['PRINT_BAK_DIR'], 'PRINT_CUPSD': NS['PRINT_CUPSD'],
        'fail': _fail, 'ok': _ok,
        'log': lambda *a, **k: None,
        'in_build_mode': lambda: True,
        '_print_backup': lambda: '',
        '_print_status': lambda: _ok(),
        '_print_save_op': lambda p: _ok(),
        '_print_queue_ops': lambda p: _ok(),
    }
    build(['act_print'], ns)
    act_print = ns['act_print']
    good = os.path.basename(p_backups()[0]['file'])
    for bad in ('', '../cupsd.conf', '../../etc/shadow', 'cupsd.conf',
                'cupsd-20260101-000000.txt', 'x' * 200, 'cupsd-2026-1-1.conf'):
        r = act_print({'op': 'restore', 'file': bad})
        chk('还原拒绝非法文件名：%r' % bad[:30], (not r['ok'])
            and r.get('code') in ('BAD_FILE', 'NOT_FOUND'))
    with open(os.path.join(NS['PRINT_BAK_DIR'], good), 'w') as f:
        f.write('Port 631\n# restored\n')
    r = act_print({'op': 'restore', 'file': good})
    chk('合法备份能还原', r['ok'], r.get('msg_cn', ''))
    chk('还原后内容写进 cupsd.conf', '# restored' in io.open(
        NS['PRINT_CUPSD'], encoding='utf-8').read())
    chk('构建保护模式下还原不重启 CUPS',
        not any('restart' in ' '.join(c) for c in CALLS[-3:]))
    chk('未知 op 被拒', act_print({'op': 'nope'})['ok'] is False)

    print('\n--- 构建保护模式：保存只写盘，不启停服务 ---')

    def save_with(build_mode):
        CALLS[:] = []
        applied = {'n': 0}

        def fake_apply(cfg, errs):
            applied['n'] += 1

        ns = {
            'sh': fake_sh, 'os': os, 'json': json,
        'PRINT_CONF': os.path.join(TMP, 'bm.conf'), 'PRINT_PKGS': PKGS,
        'in_build_mode': lambda: build_mode,
            'log': lambda *a, **k: None,
            'ok': _ok, 'fail': _fail,
            '_print_load': lambda: {'mode': 'cups',
                                    'cups': dict(DEFAULTS['cups']),
                                    'raw': dict(DEFAULTS['raw'])},
            '_print_save': lambda c: None,
            '_print_apply': fake_apply,
            '_print_load_installed': lambda: True,
        }
        build(['_print_save_op'], ns)
        return ns['_print_save_op']({'mode': 'cups'}), applied['n'], list(CALLS)

    r, n, calls = save_with(True)
    chk('构建保护模式下不启停任何服务', n == 0)
    chk('构建保护模式下的消息说清楚了', '未启停' in (r.get('msg_cn') or ''),
        r.get('msg_cn', ''))
    chk('构建保护模式仍有 ok=True（配置写盘了）', r['ok'] is True)
    r, n, calls = save_with(False)
    chk('正常模式下会真的应用', n == 1)
    chk('正常模式下消息是「已保存并生效」', '已保存并生效' in (r.get('msg_cn') or ''))

    print('\n--- 未装 CUPS 时的提示 ---')
    ns = {
        'sh': fake_sh, 'os': os, 'json': json,
        'PRINT_CONF': os.path.join(TMP, 'ni.conf'), 'PRINT_PKGS': PKGS,
        'in_build_mode': lambda: False,
        'log': lambda *a, **k: None,
        'ok': _ok, 'fail': _fail,
        '_print_load': lambda: {'mode': 'cups',
                                'cups': dict(DEFAULTS['cups']),
                                'raw': dict(DEFAULTS['raw'])},
        '_print_save': lambda c: None,
        '_print_apply': lambda c, e: None,
        '_print_load_installed': lambda: False,
    }
    build(['_print_save_op'], ns)
    r = ns['_print_save_op']({'mode': 'cups'})
    chk('CUPS 模式但未安装 → 拒绝并给安装命令',
        (not r['ok']) and 'apt-get install' in (r.get('msg_cn') or ''))
    chk('错误码是 NOT_INSTALLED', r.get('code') == 'NOT_INSTALLED')
    ns2 = dict(ns)
    ns2['_print_load'] = lambda: {'mode': 'raw',
                                  'cups': dict(DEFAULTS['cups']),
                                  'raw': dict(DEFAULTS['raw'])}
    build(['_print_save_op'], ns2)
    r = ns2['_print_save_op']({'mode': 'raw'})
    chk('RAW 模式不要求装 CUPS（只靠 socat）', r['ok'] is True)

    print('\n--- 端口与绑定地址校验 ---')
    ns = {
        're': re, 'os': os, 'sh': fake_sh, 'shutil': shutil,
        'PRINT_RAW_SERVICE': 'drouter-printer-raw.service',
        'PRINT_RAW_WRAPPER': os.path.join(TMP, 'w.sh'),
        'PRINT_RAW_UNIT': os.path.join(TMP, 'w.service'),
        'PRINT_RAW_UNIT_BODY': UNIT_BODY,
    }
    build(['_print_apply_raw'], ns)
    apply_raw = ns['_print_apply_raw']
    for bad, why in (({'device': 'relative/path'}, '设备路径非 /dev/'),
                     ({'bind': 'not-an-ip'}, '绑定地址非法'),
                     ({'port': 'abc'}, '端口非数字')):
        cfg = {'raw': {'device': '/dev/usb/lp0', 'port': 9100, 'bind': '0.0.0.0'}}
        cfg['raw'].update(bad)
        errs = []
        okk = apply_raw(cfg, errs)
        chk('拒绝 %s' % why, okk is False and bool(errs))
    # 端口不做「拒绝」而是钳制到 1–65535：页面上是个 number 输入框，
    # 用户手滑填个 0 不该让整次保存失败（和 dcfg 那边的处理保持一致）
    for got, want, why in ((0, 1, '端口 0 钳到 1'), (99999, 65535, '端口越界钳到 65535'),
                           (-5, 1, '负端口钳到 1')):
        if os.path.exists(os.path.join(TMP, 'w.sh')):
            os.remove(os.path.join(TMP, 'w.sh'))
        apply_raw({'raw': {'device': '/dev/usb/lp0', 'port': got,
                           'bind': '0.0.0.0'}}, [])
        txt = io.open(os.path.join(TMP, 'w.sh'), encoding='utf-8').read()
        chk(why, ('TCP-LISTEN:%d,' % want) in txt, txt.strip()[-60:])
    errs = []
    apply_raw({'raw': {'device': '/dev/usb/lp0', 'port': 9100,
                       'bind': '192.168.7.3'}}, errs)
    chk('合法 RAW 配置能写出 wrapper', os.path.isfile(os.path.join(TMP, 'w.sh')))
    chk('wrapper 内容含绑定地址与端口',
        'TCP-LISTEN:9100,bind=192.168.7.3' in io.open(
            os.path.join(TMP, 'w.sh'), encoding='utf-8').read())
    chk('wrapper 是可执行的 /bin/sh 脚本',
        io.open(os.path.join(TMP, 'w.sh'), encoding='utf-8').read().startswith('#!/bin/sh'))

    print('\n--- 前端接线 ---')
    chk('菜单里有「打印服务」', "k: 'print'" in APP_SRC and '打印服务' in APP_SRC)
    chk('挂到「服务」分组', re.search(r"k: 'print'.*needSave: false", APP_SRC) is not None)
    chk('VIEWS 注册 print', re.search(r'^\s*print: viewPrint,', APP_SRC, re.M) is not None)
    chk('有 viewPrint 入口', 'async function viewPrint()' in APP_SRC)
    chk('有 printLoad / printRender / printBind / printSave',
        all(x in APP_SRC for x in ('async function printLoad()',
                                   'function printRender()',
                                   'function printBind()',
                                   'async function printSave()')))
    chk('保存走 /api/print', "'/api/print'" in APP_SRC)
    chk('保存不再传多余的 timeout 参数（api() 只收两个参数）',
        re.search(r"api\('/api/print'.*\}\ \}\)", APP_SRC) is not None)
    chk('三选一 radio（off/cups/raw）',
        'name="pt-mode"' in APP_SRC
        and re.search(r"\{ k: 'off', n: '关闭打印服务'", APP_SRC) is not None
        and re.search(r"\{ k: 'cups',", APP_SRC) is not None
        and re.search(r"\{ k: 'raw',", APP_SRC) is not None)
    chk('没检测到 USB 打印机时如实提示（这台虚拟机就是这种情况）',
        '没有检测到 USB 打印机' in APP_SRC)
    chk('页面讲清了两种模式抢设备的原理', '抢同一个设备' in APP_SRC)
    chk('有 RAW 设备输入框', 'pt-raw-dev' in APP_SRC)
    chk('有共享开关', 'pt-share' in APP_SRC)
    chk('未安装时给出安装提示', 'need_install' in APP_SRC)
    chk('只听 127.0.0.1 时给出警告', '只监听 127.0.0.1' in APP_SRC)

    print('\n--- 安全红线：不许碰的东西 ---')
    chk('打印模块没有动 5900（VNC）', '5900' not in HELPER_SRC[
        HELPER_SRC.index('PRINT_CONF'):HELPER_SRC.index('def read_share')])
    chk('打印模块没有启停 DHCP', 'dnsmasq' not in HELPER_SRC[
        HELPER_SRC.index('PRINT_CONF'):HELPER_SRC.index('def read_share')])
    chk('打印模块没有改防火墙', 'nft' not in HELPER_SRC[
        HELPER_SRC.index('PRINT_CONF'):HELPER_SRC.index('def read_share')])
    chk('打印模块没有动默认路由', 'ip route' not in HELPER_SRC[
        HELPER_SRC.index('PRINT_CONF'):HELPER_SRC.index('def read_share')])

    shutil.rmtree(TMP, ignore_errors=True)
    print('\n================ 通过 %d / 失败 %d ================' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())

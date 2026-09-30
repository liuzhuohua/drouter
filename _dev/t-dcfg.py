#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Docker 引擎配置 daemon.json（#5）：静态契约 + 合并逻辑单测。

盯四件事：
  ① 用户自己写在 daemon.json 里的、本页不管的键必须原样保留 ——
     静默丢掉别人的 data-root / insecure-registries 是最糟的体验；
  ② 保存只写盘，绝不自动重启 Docker（重启会中断所有容器，是重动作）；
  ③ IPv6 默认关、默认用 fd00::/8 私有段 —— 开了不等于能上 v6 外网，
     公网段还需要上游做 NDP 代理，默认给公网段是坑；
  ④ 页面显示的「配置代码」必须和真正写进文件的逐字节一致，
     所以预览一律调后端 op=preview，前端不许自己再拼一份。
"""
import ast
import io
import os
import re
import sys
import json
import ipaddress

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

# 假的 `ip -o -4 addr show` 输出：ens18 是 192.168.7.3/24（这台路由器的真实 LAN 口）
FAKE_IP_OUT = (
    '1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n'
    '2: ens18    inet 192.168.7.3/24 brd 192.168.7.255 scope global ens18\\       valid_lft forever\n'
)


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
    exec(compile(mod, '<dcfg>', 'exec'), ns)
    return ns


NS = build([
    'DCFG_MANAGED', 'DCFG_DEFAULTS', 'DCFG_MIRRORS',
    '_dcfg_gen_ula', '_dcfg_check_v6_prefix', '_dcfg_check_bip',
    '_dcfg_parse_ipv4_addrs', '_dcfg_subnet_in_use', '_dcfg_check_mirror_url',
    '_dcfg_log_opts', '_dcfg_build', '_dcfg_render',
], {
    're': re, 'os': os, 'json': json, 'ipaddress': ipaddress,
    'sh': lambda *a, **k: (0, FAKE_IP_OUT, ''),
})

MANAGED = NS['DCFG_MANAGED']
DEFAULTS = NS['DCFG_DEFAULTS']
MIRRORS = NS['DCFG_MIRRORS']
build_cfg = NS['_dcfg_build']
render_cfg = NS['_dcfg_render']
gen_ula = NS['_dcfg_gen_ula']
chk_v6 = NS['_dcfg_check_v6_prefix']
chk_bip = NS['_dcfg_check_bip']
chk_url = NS['_dcfg_check_mirror_url']
log_opts = NS['_dcfg_log_opts']


def cfg_of(**kw):
    c = dict(DEFAULTS)
    c.update(kw)
    return c


def main():
    print('--- 后端动作与路由 ---')
    chk('定义了 act_dcfg', 'def act_dcfg(' in HELPER_SRC)
    chk('注册到 ACTIONS', re.search(r"^\s*'dcfg':\s*act_dcfg,", HELPER_SRC, re.M) is not None)
    chk('Web 路由 /api/dcfg', "'/api/dcfg'" in WEB_SRC)
    chk('测速给了长超时', re.search(r"to = 200 if op == 'mirror_test'", WEB_SRC) is not None)
    chk('重启给了长超时', "190 if op == 'restart'" in WEB_SRC)
    chk('未知 op 返回 fail', '不支持的引擎配置操作' in HELPER_SRC)
    chk('配置文件路径正确', "DCFG_FILE = '/etc/docker/daemon.json'" in HELPER_SRC)

    print('\n--- 默认值（我替用户做的判断）---')
    chk('IPv6 默认关（开了不等于能上 v6 外网）', DEFAULTS['ipv6'] is False)
    chk('fixed-cidr-v6 默认留空（首次开启自动生成）', DEFAULTS['fixed_cidr_v6'] == '')
    chk('experimental 默认关（26+ 不需要，个别版本会拒绝启动）',
        DEFAULTS['experimental'] is False)
    chk('镜像源默认不选（直连官方）', DEFAULTS['mirrors'] == [])
    chk('日志滚动默认开（容器日志是撑爆小硬盘头号元凶）', DEFAULTS['log_rotate'] is True)
    chk('live-restore 默认开（重启 dockerd 别杀容器）', DEFAULTS['live_restore'] is True)
    chk('bip 默认不动（用 Docker 默认 172.17.0.1/16）', DEFAULTS['bip'] == '')

    print('\n--- ULA 私有段生成 ---')
    ulas = set()
    for _ in range(30):
        u = gen_ula()
        ulas.add(u)
        n = ipaddress.IPv6Network(u)
        if not (str(n).startswith('fd') and n.prefixlen == 48 and n.is_private):
            chk('生成的 ULA 合规：%s' % u, False)
            break
    else:
        chk('30 次生成的都是 fd00::/8 私有 /48', True)
    chk('每次生成都不一样（随机 40 bit）', len(ulas) > 1, '%d 种' % len(ulas))

    print('\n--- fixed-cidr-v6 校验 ---')
    ok, cidr, err, warn = chk_v6('fd12:3456:789a::/64')
    chk('合法 ULA /64 通过', ok is True and cidr == 'fd12:3456:789a::/64')
    chk('合法 ULA 无警告', warn == '')
    ok, cidr, err, warn = chk_v6('fd00::1/64')
    chk('带主机位也能归一化', ok is True and cidr == 'fd00::/64', cidr)
    ok, _c, err, _w = chk_v6('fd12:3456:789a::')
    chk('缺前缀长度被拒', ok is False and '前缀/长度' in err)
    ok, _c, err, _w = chk_v6('fd12:3456:789a::/32')
    chk('/32 被拒（超出 48–64）', ok is False and '/48' in err)
    ok, _c, err, _w = chk_v6('fd12:3456:789a::/96')
    chk('/96 被拒', ok is False)
    ok, _c, err, _w = chk_v6('')
    chk('空值被拒', ok is False)
    ok, _c, err, _w = chk_v6('这不是ipv6/64')
    chk('非法串被拒', ok is False)
    ok, cidr, err, warn = chk_v6('2408:8207:2410::/64')
    chk('公网段允许但有警告', ok is True and '公网段' in warn, warn)
    ok, cidr, err, warn = chk_v6('fd12:3456:789a::/56')
    chk('非 /64 提醒改回 /64', ok is True and '/64' in warn, warn)

    print('\n--- 默认网桥网段 bip 校验 ---')
    ok, cidr, err, warn = chk_bip('')
    chk('留空直接通过且不写', ok is True and cidr == '')
    ok, cidr, err, warn = chk_bip('172.17.0.1/16')
    chk('不冲突的段无警告', ok is True and warn == '' and cidr == '172.17.0.0/16')
    ok, cidr, err, warn = chk_bip('192.168.7.1/24')
    chk('与 ens18 同段时给出冲突警告', ok is True and '重叠' in warn, warn)
    ok, _c, err, _w = chk_bip('这不是ip')
    chk('非法网段被拒', ok is False)
    ok, _c, err, _w = chk_bip('10.0.0.1/33')
    chk('/33 被拒', ok is False)
    ok, _c, err, _w = chk_bip('10.0.0.1/31')
    chk('/31 被拒（上限 /30）', ok is False)
    ok, _c, err, _w = chk_bip('10.0.0.1/4')
    chk('/4 被拒（下限 /8）', ok is False)
    addrs = NS['_dcfg_parse_ipv4_addrs'](FAKE_IP_OUT)
    chk('能从 ip -o -4 addr 里抽出网段',
        any(str(a) == '192.168.7.0/24' for a in addrs), [str(a) for a in addrs])
    chk('网段重叠判定准确',
        NS['_dcfg_subnet_in_use'](ipaddress.IPv4Network('192.168.7.0/24'), addrs) is True
        and NS['_dcfg_subnet_in_use'](ipaddress.IPv4Network('172.17.0.0/16'), addrs) is False)

    print('\n--- 镜像源地址校验 ---')
    ok, u, err = chk_url('https://docker.nju.edu.cn')
    chk('标准 https 地址通过', ok is True and u == 'https://docker.nju.edu.cn')
    ok, u, err = chk_url('docker.nju.edu.cn')
    chk('缺协议时自动补 https', ok is True and u == 'https://docker.nju.edu.cn')
    ok, u, err = chk_url('https://docker.nju.edu.cn/')
    chk('结尾斜杠被去掉', ok is True and u == 'https://docker.nju.edu.cn')
    ok, _u, err = chk_url('http://docker.nju.edu.cn')
    chk('明文 http 被拒', ok is False and 'https' in err)
    ok, _u, err = chk_url('https://a b.com')
    chk('含空格被拒', ok is False)
    ok, _u, err = chk_url('https://a.com"x')
    chk('含引号被拒', ok is False)
    ok, _u, err = chk_url('')
    chk('空值被拒', ok is False)
    ok, _u, err = chk_url('https://' + 'a' * 320)
    chk('过长被拒', ok is False)
    ok, _u, err = chk_url('https://.bad')
    chk('主机名以点开头被拒', ok is False)
    ok, _u, err = chk_url('https://a..b')
    chk('主机名含连续点被拒', ok is False)

    print('\n--- 预设镜像源清单 ---')
    chk('至少 5 个预设', len(MIRRORS) >= 5, '%d 个' % len(MIRRORS))
    chk('key 不重复', len(set(m['key'] for m in MIRRORS)) == len(MIRRORS))
    for m in MIRRORS:
        chk('%-8s 有名称/地址/说明' % m['key'],
            bool(m.get('name')) and bool(m.get('url')) and len(m.get('note') or '') >= 10)
    ali = [m for m in MIRRORS if '<' in (m.get('url') or '')]
    chk('阿里云是占位地址（不参与写入与测速）', len(ali) == 1,
        [m['key'] for m in ali])

    print('\n--- 合并逻辑：保留用户自己的键 ---')
    existing = {'data-root': '/mnt/ssd/docker', 'insecure-registries': ['10.0.0.5:5000']}
    d, errs, warns = build_cfg(cfg_of(), existing)
    chk('未纳管的键原样保留',
        d.get('data-root') == '/mnt/ssd/docker'
        and d.get('insecure-registries') == ['10.0.0.5:5000'])
    chk('默认配置只有日志滚动与 live-restore',
        sorted(d.keys()) == sorted(['data-root', 'insecure-registries',
                                    'log-driver', 'log-opts', 'live-restore']),
        sorted(d.keys()))
    chk('默认不写 ipv6', 'ipv6' not in d)
    chk('默认不写 registry-mirrors', 'registry-mirrors' not in d)

    print('\n--- 合并逻辑：纳管的键要能被彻底关掉 ---')
    existing2 = {'ipv6': True, 'fixed-cidr-v6': 'fd00::/64',
                 'ip6tables': True, 'experimental': True,
                 'registry-mirrors': ['https://old.example.com'],
                 'log-driver': 'journald'}
    d, errs, warns = build_cfg(cfg_of(), existing2)
    for k in ('ipv6', 'fixed-cidr-v6', 'ip6tables', 'experimental', 'registry-mirrors'):
        chk('关掉后 %s 不再残留' % k, k not in d)
    chk('旧的 log-driver 被本页的覆盖', d.get('log-driver') == 'json-file')

    print('\n--- 合并逻辑：IPv6 开关 ---')
    d, errs, warns = build_cfg(cfg_of(ipv6=True), {})
    chk('开 IPv6 写 ipv6: true', d.get('ipv6') is True)
    chk('开 IPv6 自动生成 fixed-cidr-v6',
        (d.get('fixed-cidr-v6') or '').startswith('fd'), d.get('fixed-cidr-v6'))
    chk('开 IPv6 同时写 ip6tables', d.get('ip6tables') is True)
    chk('默认不写 experimental', 'experimental' not in d)
    d, errs, warns = build_cfg(cfg_of(ipv6=True, experimental=True), {})
    chk('勾上 experimental 才写', d.get('experimental') is True)
    d, errs, warns = build_cfg(cfg_of(ipv6=True, fixed_cidr_v6='fdab:cdef:1234::/64'), {})
    chk('指定网段时用它', d.get('fixed-cidr-v6') == 'fdab:cdef:1234::/64')
    d, errs, warns = build_cfg(cfg_of(ipv6=True, fixed_cidr_v6='瞎写的'), {})
    chk('网段非法时报错拦住', bool(errs) and d.get('ipv6') is None, errs)

    print('\n--- 合并逻辑：镜像源 ---')
    d, errs, warns = build_cfg(cfg_of(mirrors=['nju', 'tencent']), {})
    chk('按选择顺序写 registry-mirrors',
        d.get('registry-mirrors') == ['https://docker.nju.edu.cn',
                                      'https://mirror.ccs.tencentyun.com'],
        d.get('registry-mirrors'))
    d, errs, warns = build_cfg(cfg_of(mirrors=['nju'], mirror_custom='https://x.mirror.aliyuncs.com'), {})
    chk('自定义源排在预设之后',
        d.get('registry-mirrors') == ['https://docker.nju.edu.cn',
                                      'https://x.mirror.aliyuncs.com'])
    d, errs, warns = build_cfg(cfg_of(mirrors=['nju'], mirror_custom='https://docker.nju.edu.cn'), {})
    chk('自定义源与预设重复时去重',
        d.get('registry-mirrors') == ['https://docker.nju.edu.cn'])
    d, errs, warns = build_cfg(cfg_of(mirrors=['aliyun']), {})
    chk('占位地址不会被写进配置', 'registry-mirrors' not in d)
    d, errs, warns = build_cfg(cfg_of(mirror_custom='http://明文.com'), {})
    chk('自定义源非法时报错', bool(errs) and 'registry-mirrors' not in d, errs)

    print('\n--- 合并逻辑：日志滚动 ---')
    d, errs, warns = build_cfg(cfg_of(log_rotate=True), {})
    chk('写 json-file 驱动', d.get('log-driver') == 'json-file')
    chk('默认 10m / 3 份', d.get('log-opts') == {'max-size': '10m', 'max-file': '3'})
    chk('日志参数非法时回落到 10m',
        log_opts({'log_max_size': '很大', 'log_max_file': 'x'}) == {'max-size': '10m', 'max-file': '3'})
    chk('份数钳制在 1–20',
        log_opts({'log_max_size': '5m', 'log_max_file': 999})['max-file'] == '20'
        and log_opts({'log_max_size': '5m', 'log_max_file': 0})['max-file'] == '1')
    d, errs, warns = build_cfg(cfg_of(log_rotate=False), {'log-driver': 'journald'})
    chk('关掉日志滚动时不写 log-opts', 'log-opts' not in d)

    print('\n--- 渲染结果 ---')
    text = render_cfg({'ipv6': True, 'live-restore': True})
    chk('是缩进 2 格的 JSON', text.startswith('{\n  "ipv6"'), repr(text[:20]))
    chk('结尾有换行', text.endswith('\n'))
    chk('能被解析回来', json.loads(text) == {'ipv6': True, 'live-restore': True})
    chk('中文不被转义', render_cfg({'x': '中文'}) == '{\n  "x": "中文"\n}\n')

    print('\n--- 安全：保存绝不动 Docker ---')
    i_save = HELPER_SRC.index('def _dcfg_save_op(p):')
    i_restart = HELPER_SRC.index('def _dcfg_restart_op(p):')
    seg_save = HELPER_SRC[i_save:i_restart]
    chk('保存流程里没有 systemctl', 'systemctl' not in seg_save)
    chk('保存流程里没有 docker 命令调用', 'docker restart' not in seg_save
        and 'dockerd' not in seg_save)
    chk('重启需要显式确认', "p.get('confirm')" in HELPER_SRC
        and 'NEED_CONFIRM' in HELPER_SRC)
    chk('构建保护模式下拒绝重启', 'BUILD_MODE' in HELPER_SRC
        and '已拒绝重启 Docker' in HELPER_SRC)
    chk('Docker 未安装时拒绝重启', '无需重启' in HELPER_SRC)
    chk('解析不了现有文件时拒绝保存（不覆盖用户数据）',
        '本页不会覆盖它' in HELPER_SRC)
    chk('备份文件名做了白名单校验',
        re.search(r"r'\^daemon-\\d\{8\}-\\d\{6\}\\.json\$'", HELPER_SRC) is not None)
    chk('还原做了 realpath 防穿越',
        'os.path.realpath(os.path.join(DCFG_BAK_DIR, fn))' in HELPER_SRC
        and 'src.startswith(root + os.sep)' in HELPER_SRC)
    chk('测速带 --noproxy（不被环境代理掩盖）', "'--noproxy', '*'" in HELPER_SRC)
    chk('测速认 200 与 401 为可用', "'200', '401'" in HELPER_SRC)
    chk('缺少 curl 时给出明确提示', '缺少 curl' in HELPER_SRC)

    print('\n--- 前端接线 ---')
    chk('菜单里有 dcfg', re.search(r"k: 'dcfg'", APP_SRC) is not None)
    chk('菜单文案含「Docker 引擎配置」', 'Docker 引擎配置' in APP_SRC)
    chk('VIEWS 注册 dcfg', re.search(r"^\s*dcfg: viewDcfg,", APP_SRC, re.M) is not None)
    for fn in ('viewDcfg', 'dcfgLoad', 'dcfgRender', 'dcfgSave', 'dcfgPreview',
               'dcfgForm', 'dcfgBind', 'dcfgAskRestart'):
        chk('前端函数 %s' % fn, ('function %s(' % fn) in APP_SRC)
    chk('调用 /api/dcfg', "'/api/dcfg'" in APP_SRC)
    chk('配置代码实时显示', 'id="dc-code-pre"' in APP_SRC)
    chk('预览走后端（前端不自己拼 JSON）',
        "dcfgForm('preview')" in APP_SRC)
    chk('保存后用服务端返回内容校准预览',
        "pre.textContent = d.content" in APP_SRC)
    chk('重启前有二次确认弹窗', '重启 Docker 使配置生效' in APP_SRC)
    chk('重启确认文案说明会中断容器', '中断所有正在运行的容器' in APP_SRC)
    chk('Docker 页面有跳转入口', "onclick=\"go('dcfg')\"" in APP_SRC)
    chk('未安装 Docker 时给出说明', '还没有安装 Docker' in APP_SRC)
    chk('未纳管键会提示用户会保留', '原样保留' in APP_SRC)
    chk('experimental 有背景说明（哪版需要）', '24 / 25' in APP_SRC)

    print('\n' + '=' * 60)
    print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())

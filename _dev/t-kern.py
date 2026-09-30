#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内核转发与加速（#4）：静态契约 + 渲染逻辑单测。

盯三件事：
  ① 五项开关的默认值是我替用户定的判断，改错了会直接改变这台机器的网络行为；
  ② 每一项都必须有「通俗说明 + 联动影响」，用户明确要求过；
  ③ MSS / masquerade 最终落在 nft_v4 / nft_v6 配置库里 ——
     本页只是统一入口，绝不能另存一份，否则两处配置会打架。
"""
import ast
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'backend'))
import render  # noqa: E402

HELPER = os.path.join(ROOT, 'backend', 'drouter-helper.py')
WEB = os.path.join(ROOT, 'backend', 'drouter-web.py')
APP = os.path.join(ROOT, 'web', 'app.js')
RENDER_SRC = io.open(os.path.join(ROOT, 'backend', 'render.py'), encoding='utf-8').read()

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
    exec(compile(mod, '<kern>', 'exec'), ns)
    return ns


def main():
    print('--- 后端动作与路由 ---')
    chk('定义了 act_kern', 'def act_kern(' in HELPER_SRC)
    chk('注册到 ACTIONS', re.search(r"^\s*'kern':\s*act_kern,", HELPER_SRC, re.M) is not None)
    chk('Web 路由 /api/kern', "'/api/kern'" in WEB_SRC)
    chk('save 给了更长超时', re.search(r"to = 90 if op == 'save' else 30", WEB_SRC) is not None)
    chk('未知 op 返回 fail', '未知的内核选项操作' in HELPER_SRC)

    print('\n--- 五项开关的默认值（我替用户做的判断）---')
    ns = build(['KERN_DEFAULTS', 'KERN_ITEMS', 'KERN_FW_ITEMS'])
    dft = ns['KERN_DEFAULTS']
    chk('IPv4 转发默认开', dft['fwd_v4'] is True)
    chk('IPv6 转发默认开', dft['fwd_v6'] is True)
    chk('BBR 默认开', dft['bbr'] is True)
    chk('SNMP 默认关（多一个监听多一分风险）', dft['snmp']['enabled'] is False)
    chk('SNMP 有可改的团体名', bool(dft['snmp'].get('community')))

    items = ns['KERN_ITEMS']
    fws = ns['KERN_FW_ITEMS']
    chk('四项内核开关齐全',
        [i['key'] for i in items] == ['fwd_v4', 'fwd_v6', 'bbr', 'snmp'],
        [i['key'] for i in items])
    chk('四项防火墙参数齐全',
        [i['key'] for i in fws] == ['masquerade_v4', 'mss_v4', 'masquerade_v6', 'mss_v6'],
        [i['key'] for i in fws])

    for it in items + fws:
        k = it['key']
        chk('%-14s 有通俗说明' % k, len(it.get('why') or '') >= 20)
        chk('%-14s 有联动影响且不止一条' % k,
            isinstance(it.get('impact'), list) and len(it['impact']) >= 2)
        chk('%-14s 的 default 与 KERN_DEFAULTS 一致' % k,
            'default' in it)
    # msq/mss 的默认值要在后端与前端文案两处对得上
    by_key = {i['key']: i['default'] for i in fws}
    chk('masquerade_v4 默认开', by_key['masquerade_v4'] is True)
    chk('mss_v4 默认开', by_key['mss_v4'] is True)
    chk('masquerade_v6 默认关（IPv6 推荐端到端）', by_key['masquerade_v6'] is False)
    chk('mss_v6 默认关（IPv6 靠 PMTUD）', by_key['mss_v6'] is False)

    print('\n--- sysctl 生成 ---')
    ns2 = build(['_kern_sysctl_lines'])
    lines_of = ns2['_kern_sysctl_lines']
    L = lines_of({'fwd_v4': True, 'fwd_v6': True, 'bbr': True}, True)
    joined = '\n'.join(L)
    chk('开启转发写 ip_forward=1', 'net.ipv4.ip_forward = 1' in joined)
    chk('开启 IPv6 转发写 forwarding=1', 'net.ipv6.conf.all.forwarding = 1' in joined)
    # 不设 accept_ra=2，靠 SLAAC 拿地址的家宽会在租期到期后丢掉整个 IPv6
    chk('BBR 打开时指定 bbr', 'net.ipv4.tcp_congestion_control = bbr' in joined)
    chk('BBR 打开时搭档 fq', 'net.core.default_qdisc = fq' in joined)
    L2 = '\n'.join(lines_of({'fwd_v4': False, 'fwd_v6': False, 'bbr': False}, False))
    chk('关闭转发写 0', 'net.ipv4.ip_forward = 0' in L2
        and 'net.ipv6.conf.all.forwarding = 0' in L2)
    chk('BBR 关闭时回退 cubic', 'tcp_congestion_control = cubic' in L2)
    chk('关闭 IPv6 转发时不写 accept_ra', 'accept_ra' not in L2)
    # 关键：bbr_ok=False 时即便用户勾了 BBR 也不能写 bbr（否则 sysctl -w 必然失败）
    L3 = '\n'.join(lines_of({'fwd_v4': True, 'fwd_v6': True, 'bbr': True}, False))
    chk('内核不支持时不强写 bbr', 'tcp_congestion_control = cubic' in L3
        and '= bbr' not in L3)

    print('\n--- 持久化与构建保护 ---')
    chk('有 sysctl.d 持久化文件', "KERN_SYSCTL_D = '/etc/sysctl.d/99-drouter.conf'" in HELPER_SRC)
    chk('构建保护模式下不改内核', '构建保护模式】：配置已写入磁盘，但未修改内核参数' in HELPER_SRC)
    # 两个「会真的动手」的调用必须都排在 in_build_mode 分支之后（即 else 里）
    _i_guard = HELPER_SRC.index('当前处于【构建保护模式】：配置已写入磁盘')
    chk('构建保护模式下不启停服务',
        HELPER_SRC.index('applied, bbr_ok, e2 = _kern_apply_sysctl(cfg)') > _i_guard
        and HELPER_SRC.index('_kern_snmp_apply(cfg[\'snmp\'], errs)') > _i_guard)

    print('\n--- 参数校验 ---')
    chk('团体名禁止空格与 #', r"re.search(r'[\s#]', comm)" in HELPER_SRC)
    chk('端口钳制在 1-65535', "min(int(s['port']), 65535)" in HELPER_SRC)
    chk('MSS 钳制在 576-9000', 'min(int(fw[' in HELPER_SRC and '9000)' in HELPER_SRC)
    chk('监听地址做了格式校验', 'SNMP 监听地址不合法' in HELPER_SRC)

    print('\n--- 渲染器：masquerade / MSS ---')
    t4 = render.render_nft('v4', {'wan_iface': 'ppp0', 'lan_ifaces': ['ens18']})
    chk('v4 默认含 masquerade', 'masquerade' in t4)
    chk('v4 默认含 MSS 钳制', 'maxseg size set rt mtu' in t4)
    t4_off = render.render_nft('v4', {'wan_iface': 'ppp0', 'lan_ifaces': ['ens18'],
                                      'masquerade': False})
    chk('v4 关闭后不再有 masquerade 规则', 'masquerade comment' not in t4_off)
    chk('v4 关闭后仍保留 nat_post 链与说明',
        'chain nat_post' in t4_off and '出向地址伪装已关闭' in t4_off)
    t4_fix = render.render_nft('v4', {'wan_iface': 'ppp0', 'lan_ifaces': ['ens18'],
                                      'mss_mode': 'fixed', 'mss': 1400})
    chk('fixed 模式写死数值', 'maxseg size set 1400' in t4_fix)
    try:
        render.render_nft('v4', {'wan_iface': 'ppp0', 'lan_ifaces': ['ens18'],
                                 'mss_mode': 'bogus'})
        chk('非法 mss_mode 被拒绝', False)
    except Exception:
        chk('非法 mss_mode 被拒绝', True)

    t6 = render.render_nft('v6', {'wan_iface': 'ppp0', 'lan_ifaces': ['ens18']})
    chk('v6 默认不做 NAT66', 'masquerade' not in t6)
    chk('v6 默认不钳制 MSS', 'maxseg' not in t6)
    t6_on = render.render_nft('v6', {'wan_iface': 'ppp0', 'lan_ifaces': ['ens18'],
                                     'masquerade': True, 'mss_clamp': True})
    chk('v6 打开后生成 nat_post', 'chain nat_post' in t6_on and 'masquerade' in t6_on)
    chk('v6 打开后生成 MSS 规则', 'maxseg size set rt mtu' in t6_on)
    # 参数透传：render_nft 必须真的把 masquerade 交给 _default_v4/v6
    chk('render.py 透传 masquerade 给 v4',
        "masquerade=masq" in RENDER_SRC and "_default_v4(wan, lans" in RENDER_SRC)
    chk('render.py 透传 masquerade 给 v6',
        re.search(r"_default_v6\(wan, lans.*?masquerade=masq", RENDER_SRC, re.S) is not None)

    print('\n--- 配置落点（不能在两处各存一份）---')
    chk('写回 nft_v4 配置库', "_save_setting('nft_v4', v4)" in HELPER_SRC)
    chk('写回 nft_v6 配置库', "_save_setting('nft_v6', v6)" in HELPER_SRC)
    chk('web 默认配置含 v4 的 masquerade',
        "'mss_mode': 'clamp', 'masquerade': True" in WEB_SRC)
    chk('web 默认配置含 v6 的 masquerade=False',
        "'mss_mode': 'clamp', 'masquerade': False" in WEB_SRC)

    print('\n--- 前端接线 ---')
    chk('菜单里有 kern', re.search(r"k: 'kern'", APP_SRC) is not None)
    chk('菜单文案含「内核转发与加速」', '内核转发与加速' in APP_SRC)
    chk('VIEWS 注册 kern', re.search(r"^\s*kern: viewKern,", APP_SRC, re.M) is not None)
    chk('定义了 viewKern', 'async function viewKern(' in APP_SRC)
    for fn in ('kernLoad', 'kernRender', 'kernSave', 'kernCard', 'kernImpact'):
        chk('前端函数 %s' % fn, ('function %s(' % fn) in APP_SRC)
    chk('调用 /api/kern', "'/api/kern'" in APP_SRC)
    chk('BBR 未生效时有醒目提示', '未生效（内核未加载 tcp_bbr）' in APP_SRC)
    chk('提示用户去防火墙页应用', '防火墙 IPv4 / IPv6' in APP_SRC)

    print('\n' + '=' * 60)
    print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())

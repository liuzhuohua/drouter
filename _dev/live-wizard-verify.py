#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""部署后冒烟：确认新手向导在目标机上真的活着（只读，不改任何配置）。

用法（必须在目标机上跑）：
    python3 _dev/live-wizard-verify.py
"""
import json
import ssl
import sys
import urllib.request

BASE = 'https://127.0.0.1:8443'
AUTH = ('admin', 'admin123')
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

PASS = FAIL = 0
TOKEN = ['']


def chk(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('[OK]   %s %s' % (name, extra))
    else:
        FAIL += 1
        print('[FAIL] %s %s' % (name, extra))


def post(path, body=None, token=None):
    data = json.dumps(body or {}).encode('utf-8')
    req = urllib.request.Request(BASE + path, data=data, method='POST')
    req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('X-Token', token)
    with urllib.request.urlopen(req, context=CTX, timeout=120) as r:
        return json.loads(r.read().decode('utf-8'))


def login():
    """先登录拿 token —— 接口认的是 X-Token 头，不是 HTTP Basic。

    登录体字段是 username / password（不是 user / pass）。
    """
    r = post('/api/login', {'username': AUTH[0], 'password': AUTH[1]})
    tok = (r.get('data') or {}).get('token') or r.get('token')
    TOKEN[0] = tok or ''
    return bool(tok)


def call(path, body=None):
    return post(path, body, TOKEN[0])


def main():
    print('=== 新手向导 · 部署后冒烟（只读，不改配置）===')
    chk('登录取得 token', login())
    if not TOKEN[0]:
        print('\n无法登录，后续检查跳过')
        return 1
    r = call('/api/wizard', {'op': 'probe'})
    chk('接口返回 ok', r.get('ok') is True, str(r.get('msg') or r.get('error') or ''))
    d = r.get('data') or {}

    steps = [s.get('k') for s in (d.get('steps') or [])]
    chk('四个步骤齐全且顺序正确', steps == ['wan', 'lan', 'dns', 'v6'], str(steps))
    chk('每步都有标题', all(s.get('n') for s in (d.get('steps') or [])))
    chk('每步都有说明', all(s.get('d') for s in (d.get('steps') or [])))

    # probe 的返回是分层结构：wan / lan / dns / v6 各一个子对象
    lan = d.get('lan') or {}
    wan = d.get('wan') or {}
    chk('探测到 LAN 接口', bool(lan.get('iface')), lan.get('iface') or '')
    chk('探测到 LAN IP', bool(lan.get('lan_ip')), lan.get('lan_ip') or '')
    chk('LAN 接口与配置库一致（ens18）', lan.get('iface') == 'ens18', lan.get('iface') or '')
    chk('LAN IP 与 ens18 实际地址一致（192.168.7.3）',
        lan.get('lan_ip') == '192.168.7.3', lan.get('lan_ip') or '')
    chk('地址池预设已给出（起止）',
        bool(lan.get('pool_start')) and bool(lan.get('pool_end')),
        '%s - %s' % (lan.get('pool_start'), lan.get('pool_end')))
    chk('LAN 能报出 DHCP 是否在跑', isinstance(lan.get('dhcp_enabled'), bool),
        str(lan.get('dhcp_enabled')))
    chk('WAN 能报出接口与默认路由',
        bool(wan.get('iface')) and isinstance(wan.get('has_default_route'), bool),
        '%s / route=%s' % (wan.get('iface'), wan.get('has_default_route')))
    chk('能报出外网可达性', isinstance(d.get('internet_ok'), bool), str(d.get('internet_ok')))
    chk('能报出构建保护模式状态', isinstance(d.get('build_mode'), bool),
        str(d.get('build_mode')))

    modes = [m.get('k') for m in (d.get('wan_modes') or [])]
    chk('三种上网方式齐全', modes == ['pppoe', 'dhcp', 'static'], str(modes))
    chk('每种上网方式都有说明+适用场景',
        all(m.get('d') and m.get('when') for m in (d.get('wan_modes') or [])))

    presets = d.get('dns_presets') or []
    chk('DNS 预设 >= 5 家', len(presets) >= 5, '%d 家' % len(presets))
    # "跟随运营商"这一项的地址本来就是空的（由 PPPoE 下发），不算缺失
    chk('DNS 预设每家都有名字', all(p.get('n') for p in presets))
    chk('DNS 预设的公共 DNS 都带地址',
        all(p.get('v4') for p in presets if p.get('k') != 'isp'),
        str([p.get('k') for p in presets if p.get('k') != 'isp' and not p.get('v4')]))
    chk('DNS 预设每家都有人话说明', all(p.get('d') for p in presets))

    pool = d.get('pool_hint') or {}
    chk('地址池提示含「为什么」', bool(pool.get('why')))
    chk('地址池提示含规则清单', len(pool.get('rules') or []) >= 3,
        '%d 条' % len(pool.get('rules') or []))

    chk('网卡下拉（WAN）非空', bool(d.get('wan_ifaces')), '%d 个' % len(d.get('wan_ifaces') or []))
    chk('网卡下拉（LAN）非空', bool(d.get('lan_ifaces')), '%d 个' % len(d.get('lan_ifaces') or []))
    chk('网卡下拉每项带名字与 MAC',
        all(x.get('name') and x.get('mac')
            for x in (d.get('wan_ifaces') or []) + (d.get('lan_ifaces') or [])))
    chk('待办清单存在（可为空）', isinstance(d.get('todo'), list), str(d.get('todo')))

    print('\n  ── 只读校验：非法输入必须被拦住（不写盘）──')
    r2 = call('/api/wizard', {'op': 'apply_step', 'step': 'lan',
                              'cfg': {'pool_start': '192.168.7.3', 'pool_end': '192.168.7.50'}})
    chk('地址池圈进本机自己 → 拒绝', r2.get('ok') is False,
        str(r2.get('msg') or '')[:80])

    r3 = call('/api/wizard', {'op': 'apply_step', 'step': 'lan',
                              'cfg': {'pool_start': '10.0.0.100', 'pool_end': '10.0.0.200'}})
    chk('地址池跨网段 → 拒绝', r3.get('ok') is False, str(r3.get('msg') or '')[:80])

    r4 = call('/api/wizard', {'op': 'apply_step', 'step': 'wan', 'cfg': {'mode': 'pppoe'}})
    chk('PPPoE 缺账号 → 拒绝', r4.get('ok') is False, str(r4.get('msg') or '')[:80])

    r5 = call('/api/wizard', {'op': 'apply_step', 'step': 'nosuch', 'cfg': {}})
    chk('未知步骤 → 拒绝', r5.get('ok') is False, str(r5.get('msg') or '')[:80])

    r6 = call('/api/wizard', {'op': 'dial', 'action': 'connect'})
    chk('拨号不带确认 → 拒绝（防止误断网）', r6.get('ok') is False,
        str(r6.get('msg') or '')[:80])

    print('\n' + '=' * 56)
    print('通过 %d / 失败 %d' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())

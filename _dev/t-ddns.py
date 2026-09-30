# -*- coding: utf-8 -*-
"""DDNS 纯逻辑测试：公网/私网判定 + 四种组合 + 记录名拼接。"""
import re, sys, io

SRC = r"C:/Users/lyrz-pve-win10/WorkBuddy/2026-09-27-19-57-00/router-build/backend/drouter-helper.py"
src = io.open(SRC, encoding='utf-8').read()

def grab(name):
    m = re.search(r'^def %s\(.*?(?=\n(?:def |[A-Z_]+ *=|# ---|class ))'
                  % re.escape(name), src, re.S | re.M)
    if not m:
        raise SystemExit('NOT FOUND: ' + name)
    return m.group(0)

def grab_var(name):
    m = re.search(r'^%s = \{.*?^\}' % re.escape(name), src, re.S | re.M)
    if not m:
        m = re.search(r'^%s = \[.*?^\]' % re.escape(name), src, re.S | re.M)
    if not m:
        raise SystemExit('NOT FOUND VAR: ' + name)
    return m.group(0)

NS = {'re': re}
exec(grab('_is_private_v4'), NS)
exec(grab('_is_global_v6'), NS)
exec(grab('_ddns_record_name'), NS)
exec(grab_var('PUBIP_COMBO_NOTE'), NS)

P, G, R, NOTE = NS['_is_private_v4'], NS['_is_global_v6'], NS['_ddns_record_name'], NS['PUBIP_COMBO_NOTE']

fails = 0
def chk(label, got, want):
    global fails
    ok = got == want
    if not ok: fails += 1
    print('[%s] %-46s got=%-6s want=%s' % ('OK' if ok else 'FAIL', label, got, want))

print('=== IPv4 私网/公网判定 ===')
for ip, want in [('192.168.1.1', True), ('10.0.0.1', True), ('172.16.5.5', True),
                 ('172.32.5.5', False), ('100.64.0.1', True), ('100.128.0.1', False),
                 ('169.254.1.1', True), ('127.0.0.1', True), ('8.8.8.8', False),
                 ('1.1.1.1', False), ('223.5.5.5', False), ('224.0.0.1', True),
                 ('116.25.100.7', False)]:
    chk('v4 ' + ip, P(ip), want)

print('\n=== IPv6 全球单播判定 ===')
for ip, want in [('2408:8207:1234::1', True), ('2001:4860:4860::8888', True),
                 ('fe80::1', False), ('fc00::1', False), ('fd12:3456::1', False),
                 ('ff02::1', False), ('::1', False), ('2400:3200::1', True),
                 ('240e:3b7:1234::abcd', True)]:
    chk('v6 ' + ip, G(ip), want)

print('\n=== DDNS 记录名拼接 ===')
for cfg, want in [({'subdomain': 'home', 'domain': 'example.com'}, 'home.example.com'),
                  ({'subdomain': '', 'domain': 'example.com'}, 'example.com'),
                  ({'subdomain': '@', 'domain': 'example.com'}, 'example.com'),
                  ({'subdomain': '.www.', 'domain': 'a.cn'}, 'www.a.cn')]:
    chk('rec ' + str(cfg), R(cfg), want)

print('\n=== 四种组合说明齐全性 ===')
for k in ('both', 'v4only', 'v6only', 'neither'):
    has = k in NOTE and NOTE[k].get('conclusion') and len(NOTE[k].get('advice') or []) >= 3
    chk('combo ' + k, bool(has), True)

print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % fails))
sys.exit(1 if fails else 0)

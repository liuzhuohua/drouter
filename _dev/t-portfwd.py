# -*- coding: utf-8 -*-
"""端口转发 / DMZ 渲染测试。"""
import sys
sys.path.insert(0, r"C:/Users/lyrz-pve-win10/WorkBuddy/2026-09-27-19-57-00/router-build/backend")
import render

fails = 0
def chk(label, cond, extra=''):
    global fails
    if not cond: fails += 1
    print('[%s] %s %s' % ('OK' if cond else 'FAIL', label, extra))

cfg4 = {'wan_iface': 'ppp0', 'lan_ifaces': ['ens18'], 'mss_clamp': True, 'mss': 1452,
    'portfwd': {'enable': True, 'rules': [
        {'name': '群晖DSM', 'proto': 'tcp', 'ext_port': 5000, 'int_ip': '192.168.7.10', 'int_port': 5000, 'enable': True},
        {'name': '游戏', 'proto': 'tcp/udp', 'ext_port': 27015, 'int_ip': '192.168.7.20', 'int_port': 27016, 'enable': True},
        {'name': '禁用', 'proto': 'tcp', 'ext_port': 8080, 'int_ip': '192.168.7.30', 'int_port': 8080, 'enable': False},
    ], 'dmz': {'enable': False, 'host': ''}}}
o4 = render.render_nft('v4', cfg4)
chk('IPv4 DNAT 同端口省略端口号', 'dnat to 192.168.7.10 comment' in o4)
chk('IPv4 DNAT 异端口带端口号', 'dnat to 192.168.7.20:27016' in o4)
chk('IPv4 禁用规则不生成', '192.168.7.30' not in o4)
chk('IPv4 生成 nat_pre/dstnat 链', 'nat_pre' in o4 and 'priority dstnat' in o4)
chk('IPv4 放通规则在 forward 链', 'tcp dport 5000 ip daddr 192.168.7.10 accept' in o4)
chk('IPv4 tcp/udp 展开两条', o4.count('dport 27015') >= 2)

cfg6 = {'wan_iface': 'ppp0', 'lan_ifaces': ['ens18'],
    'portfwd': {'enable': True, 'rules': [
        {'name': 'NAS', 'proto': 'tcp', 'ext_port': 8443, 'int_ip': '2408:8207:1234::10', 'int_port': 443, 'enable': True}],
        'dmz': {'enable': True, 'host': '2408:8207:1234::99'}}}
o6 = render.render_nft('v6', cfg6)
chk('IPv6 DNAT 方括号包裹', 'dnat to [2408:8207:1234::10]:443' in o6)
chk('IPv6 DMZ 全端口映射', 'DMZ 全端口映射' in o6)
chk('IPv6 DMZ 放通', 'ip6 daddr 2408:8207:1234::99 accept' in o6)
chk('IPv6 放通用 ip6 关键字', 'tcp dport 443 ip6 daddr 2408:8207:1234::10 accept' in o6)

# 关闭时不应有 nat_pre
o4off = render.render_nft('v4', {'wan_iface': 'ppp0', 'lan_ifaces': ['ens18'],
    'portfwd': {'enable': False, 'rules': [], 'dmz': {'enable': False, 'host': ''}}})
chk('关闭时无 nat_pre 链', 'nat_pre' not in o4off)

# 校验错误
for bad, want in [(('999.1.1.1',), 'IPv4'), (('2408::gg',), 'IPv6')]:
    try:
        render.render_nft('v4' if want == 'IPv4' else 'v6', {'wan_iface': 'ppp0', 'lan_ifaces': ['ens18'],
            'portfwd': {'enable': True, 'rules': [{'name': 'x', 'proto': 'tcp', 'ext_port': 80,
            'int_ip': bad[0], 'int_port': 80}]}})
        chk('拒绝非法 ' + want, False)
    except render.ValidateError as e:
        chk('拒绝非法 ' + want, True, '→ ' + e.msg_cn)
try:
    render.render_nft('v6', {'wan_iface': 'ppp0', 'lan_ifaces': ['ens18'],
        'portfwd': {'enable': True, 'rules': [{'name': 'x', 'proto': 'tcp', 'ext_port': 80,
        'int_ip': 'fe80::1', 'int_port': 80}]}})
    chk('拒绝链路本地 IPv6', False)
except render.ValidateError as e:
    chk('拒绝链路本地 IPv6', True, '→ ' + e.msg_cn)
# 非法协议
try:
    render.render_nft('v4', {'wan_iface': 'ppp0', 'lan_ifaces': ['ens18'],
        'portfwd': {'enable': True, 'rules': [{'name': 'x', 'proto': 'sctp', 'ext_port': 80,
        'int_ip': '192.168.7.1', 'int_port': 80}]}})
    chk('拒绝非法协议', False)
except render.ValidateError as e:
    chk('拒绝非法协议', True, '→ ' + e.msg_cn)

print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % fails))
sys.exit(1 if fails else 0)

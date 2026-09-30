# -*- coding: utf-8 -*-
"""本地纯逻辑测试：从 drouter-helper.py 抽取 WAN 日志翻译相关函数，不依赖 pwd/网络。"""
import re, sys, io

SRC = r"C:/Users/lyrz-pve-win10/WorkBuddy/2026-09-27-19-57-00/router-build/backend/drouter-helper.py"
src = io.open(SRC, encoding='utf-8').read()

def grab(name):
    m = re.search(r'^def %s\(.*?(?=\n(?:def |[A-Z_]+ *=|# ---))' % re.escape(name), src, re.S | re.M)
    if not m:
        raise SystemExit('NOT FOUND: ' + name)
    return m.group(0)

def grab_var(name):
    m = re.search(r'^%s = \[.*?^\]' % re.escape(name), src, re.S | re.M)
    if not m:
        raise SystemExit('NOT FOUND VAR: ' + name)
    return m.group(0)

NS = {'re': re}
for v in ('PPP_TRANS', 'DHCP_TRANS', 'STATIC_TRANS', 'IPV6_TRANS', 'BRIDGE_TRANS'):
    exec(grab_var(v), NS)
for f in ('_wan_level', '_wan_translate', '_translate_ppp'):
    exec(grab(f), NS)

T = NS['_wan_translate']
L = NS['_wan_level']

cases = [
    # (接入方式, 原始日志, 期望中文包含)
    ('dhcp', 'DHCPDISCOVER on ens19 to 255.255.255.255 port 67 interval 3', '寻找 DHCP 服务器'),
    ('dhcp', 'DHCPOFFER of 192.168.1.100 from 192.168.1.1', '提供地址'),
    ('dhcp', 'DHCPREQUEST for 192.168.1.100 on ens19 to 192.168.1.1 port 67', '请求地址'),
    ('dhcp', 'DHCPACK of 192.168.1.100 from 192.168.1.1', '租约生效'),
    ('dhcp', 'DHCPNAK from 192.168.1.1', '拒绝请求'),
    ('dhcp', 'bound to 192.168.1.100 -- renewal in 3600 seconds', '将在 3600 秒后续租'),
    ('dhcp', 'No DHCPOFFERS received.', '未收到任何 DHCPOFFER'),
    ('static', 'ens19 Link is Up', '链路已连接'),
    ('static', 'ens19 NIC Link is Down', '物理链路已断开'),
    ('static', 'default via 192.168.1.1 dev ens19', '已添加默认路由'),
    ('static', 'duplicate address detected', 'IP 冲突'),
    ('static', 'carrier lost', '载波丢失'),
    ('pppoe', 'sent [PADI]', '寻找宽带接入服务器'),
    ('pppoe', 'recv [PADS]', '会话已建立'),
    ('pppoe', 'PAP authentication failed', '认证失败'),
    ('pppoe_dhcp6', 'IA_PD prefix 2408:8207::/60', '前缀'),
    ('ipoe_dhcp6', 'Router Advertisement on ens19', '路由器通告'),
    ('bridge', 'br0: entered forwarding state', '网桥端口进入'),
]
TAB = {'pppoe': NS['PPP_TRANS'], 'pppoe_dhcp6': NS['IPV6_TRANS'],
       'dhcp': NS['DHCP_TRANS'], 'ipoe_dhcp6': NS['IPV6_TRANS'],
       'static': NS['STATIC_TRANS'], 'bridge': NS['BRIDGE_TRANS']}

fail = 0
for i, (acc, raw, want) in enumerate(cases, 1):
    got = T(raw, TAB[acc])
    ok = want in got
    if not ok:
        fail += 1
    print('[%s] %02d %-12s %-60s -> %s' % ('OK' if ok else 'FAIL', i, acc, raw[:58], got[:70]))
print('\n翻译用例: %d/%d 通过' % (len(cases) - fail, len(cases)))

# 级别判定
lv_cases = [
    ('PAP authentication failed', 'err'),
    ('DHCPNAK from 1.1.1.1', 'err'),
    ('DHCPDISCOVER on ens19', 'warn'),
    ('DHCPACK of 1.1.1.1 from 2.2.2.2', 'ok'),
    ('ens19 Link is Up', 'ok'),
    ('some random kernel message', 'info'),
]
lf = 0
for raw, want in lv_cases:
    g = L(raw)
    if g != want:
        lf += 1
        print('[FAIL] level %-40s want=%s got=%s' % (raw[:40], want, g))
print('级别用例: %d/%d 通过' % (len(lv_cases) - lf, len(lv_cases)))

# 接入方式目录
cat = re.search(r'^WAN_ACCESS_TYPES = \[(.*?)^\]', src, re.S | re.M)
n = len(re.findall(r"\{'v':", cat.group(1))) if cat else 0
print('接入方式条目: %d (期望 6) %s' % (n, 'OK' if n == 6 else 'FAIL'))

sys.exit(1 if (fail or lf or n != 6) else 0)

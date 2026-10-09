#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 NAV_GROUPS 自动生成 i18n 词条（导航菜单）。

为什么自动生成：47 个页面 × 3 个字段（k/n/t）= 141 处中文，
手工抄进字典必然漏/错，而且以后新增页面又要手动同步一遍。

产出：
  nav.g.<key>  分组名（8 条）
  nav.n.<key>  页面名（47 条）
  nav.t.<key>  页面说明（47 条）

英文翻译在 TRANSLATE 表里手工维护（机器翻译质量不够，
导航是每天看的界面，翻译质量直接影响可用性）。
"""
import io
import json
import os
import re
import subprocess
import sys

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = 'web/app.js'
I18N = 'web/i18n.js'
NODE = None
for base in (r'C:\Users\lyrz-pve-win10\.workbuddy\binaries\node\versions',
             '/c/Users/lyrz-pve-win10/.workbuddy/binaries/node/versions'):
    if os.path.isdir(base):
        for d in sorted(os.listdir(base), reverse=True):
            p = os.path.join(base, d, 'node.exe' if os.name == 'nt' else 'node')
            if os.path.isfile(p):
                NODE = p
                break
    if NODE:
        break

# ---------- 1. 从 app.js 里抽出 NAV_GROUPS ----------
app = io.open(APP, encoding='utf-8').read()
i = app.index('const NAV_GROUPS = [')
# 惰性写法（顶层常量切语言后仍中文 → 改成 () => ([...])）
if i < 0:
    i = app.index('const NAV_GROUPS = () => ([')
j = app.index('\n];', i) + 3
block = app[i:j]

groups = []
for gm in re.finditer(r"g:\s*'([^']*)'.*?items:\s*\[(.*?)\n    \]", block, re.S):
    gname, items_src = gm.group(1), gm.group(2)
    items = []
    for im in re.finditer(
            r"\{\s*k:\s*'([^']+)'\s*,\s*n:\s*'((?:[^'\\]|\\.)*)'\s*,\s*"
            r"t:\s*'((?:[^'\\]|\\.)*)'", items_src):
        items.append((im.group(1), im.group(2), im.group(3)))
    groups.append((gname, items))

print('解析到 %d 个分组、%d 个页面' % (len(groups), sum(len(x[1]) for x in groups)))
if len(groups) != 8:
    print('！分组数不对，请检查正则'); sys.exit(1)

# ---------- 2. 英文翻译表（手工维护）----------
# 分组名
G_EN = {
    '概览': 'Overview',
    '接口': 'Interfaces',
    '寻址与路由': 'Addressing & Routing',
    '安全': 'Security',
    '服务': 'Services',
    '工具': 'Tools',
    '日志与审计': 'Logs & Audit',
    '系统': 'System',
}
# 页面名 + 说明
NT_EN = {
 'wizard': ('Setup Wizard', 'First time on this machine? Four steps: connect '
    'to the internet, hand out LAN addresses, set up DNS, decide on IPv6. '
    'Each step has examples and a health check.'),
 'dash': ('System Overview', 'System Overview'),
 'netstat': ('Network Status / Acceleration', 'Status · NAT detection · '
    'software acceleration'),
 'iface': ('NICs & Bridge', 'NICs and bridging'),
 'wan': ('WAN Port', 'WAN port settings'),
 'pppmulti': ('PPPoE Multi-WAN', 'PPPoE multi-WAN / session aggregation'),
 'lan': ('LAN Port', 'LAN port settings'),
 'vlan': ('VLAN / IPTV', 'VLAN and IPTV segmentation'),
 'wol': ('Wake-on-LAN (WOL)', 'Wake NICs remotely with WOL'),
 'dhcp': ('DHCP Service', 'DHCP service and options'),
 'dns': ('DNS Service', 'DNS service'),
 'ipv6': ('IPv6 / RA', 'IPv6 and router advertisements'),
 'dhcpv6': ('DHCPv6 / Prefix Delegation', 'DHCPv6 and prefix delegation'),
 'ddns': ('Dynamic DNS (DDNS)', 'Dynamic DNS for IPv4 and IPv6, with domestic '
     'and overseas providers'),
 'pubip': ('Real Public IP Check', 'Determine whether the outbound IP can '
     'actually be reached inbound (measured)'),
 'fw4': ('Firewall IPv4', 'IPv4 firewall'),
 'fw6': ('Firewall IPv6', 'IPv6 firewall'),
 'portfwd': ('Port Forwarding / DMZ', 'Port forwarding and DMZ (nftables DNAT)'),
 'upnp': ('UPnP / NAT-PMP', 'UPnP / NAT-PMP'),
 'acl': ('Access Control / Time Groups', 'Access control and parental time '
     'groups'),
 'vpn': ('WireGuard VPN', 'Reach your LAN and NAS from elsewhere: WireGuard '
     'is built into the Debian 13 kernel, each device gets its own config'),
 'qos': ('Smart Rate Limit (QoS)', 'Smart rate limiting and traffic shaping '
     '(CAKE / HTB)'),
 'dpi': ('App Detection (DPI)', 'App identification and rule-set updates '
     '(nDPI)'),
 'ntp': ('NTP Time Sync', 'NTP time synchronisation'),
 'nfs': ('File Sharing SMB/NFS', 'LAN file sharing (SMB / NFS, '
     'cross-platform templates)'),
 'print': ('Print Service', 'CUPS print server / USB printer RAW passthrough '
     '(mutually exclusive), shared to phones and PCs'),
 'opensoho': ('AC/AP Controller', 'OpenSOHO wireless controller: central '
     'management of OpenWRT AP Wi-Fi · VLAN · PoE'),
 'webshell': ('Web Terminal / Files', 'Web SSH terminal and file manager'),
 'api': ('General API', 'Public API reference with call examples'),
 'diag': ('Network Diagnostics', 'Network diagnostic tools'),
 'v6test': ('IPv6 Connectivity Test', 'IPv6 connectivity test'),
 'docker': ('Docker / Compose', 'Docker and Docker Compose management panel'),
 'dcfg': ('Docker Engine Config', 'daemon.json visual config: one-click IPv6 '
     '· mirror source switching and speed test · log rotation · default '
     'bridge subnet'),
 'log': ('System Logs', 'Log viewer'),
 'alert': ('Alerts & Notifications', 'Proactive push on outage / full disk / '
     'high temperature: Bark · email · chat bot, with cooldown and '
     'do-not-disturb'),
 'flowlog': ('Connection & Flow Logs', 'Connection tracking and traffic logs '
     '(unified logging)'),
 'quota': ('Usage & Billing', 'Monthly traffic per device and service, split '
     'line costs in a studio; incremental background aggregation that does '
     'not slow the machine'),
 'depcheck': ('Dependency Check & Install', 'Run dependency checks and '
     'install in one click'),
 'cleanup': ('Disk & Log Cleanup', 'Reclaim logs / caches / temp files, with '
     'thresholds for automatic cleanup so a small disk never fills up'),
 'kern': ('Kernel Forwarding & Tuning', 'IP forwarding · outbound '
     'masquerade · MSS clamping · BBR · SNMP — five kernel-level switches, '
     'each with notes on interactions'),
 'ca': ('Certificates / SSL', 'CA management: build your own CA · issue '
     'server certificates · import · deploy to the console; includes an '
     'SSL/TLS handshake doctor'),
 'backup': ('Config Backup & Restore', 'Export every setting into one '
     'downloadable, restorable package, with a manifest and per-file '
     'checks; pre-flight check before restoring'),
 'power': ('Power Control', 'Power control'),
 'user': ('Users & Keys', 'System users and SSH public keys'),
 'sys': ('System Settings', 'System settings'),
 'theme': ('Theme Studio', 'Theme studio (web design · offline preview · '
     'import/export)'),
 'update': ('Upgrade & Protection', 'System upgrade and RealVNC protection'),
}

missing = []
for _g, items in groups:
    for k, n, _t in items:
        if k not in NT_EN:
            missing.append(k)
if missing:
    print('！缺少英文翻译的页面：%s' % missing)
    print('→ 请在 TRANSLATE 表里补上再跑'); sys.exit(1)

# ---------- 3. 生成词条块 ----------
# 分组用**拼音无关的稳定 key**：直接用分组名下标（g0/g1/…）。
# 为什么不用中文做 key：中文会随文案改动而变，key 就跟着变，
# 旧词条会变成死条目。序号稳定。
def _gkey_for(gname, all_groups):
    for idx, (g, _it) in enumerate(all_groups):
        if g == gname:
            return 'g%d' % idx
    return 'g?'


lines = []
lines.append("    /* ---- 导航菜单（1.0.10 第 2 批，**自动生成，别手改**）----")
lines.append("       由 _dev/gen-nav-i18n.py 从 app.js 的 NAV_GROUPS 生成。")
lines.append("       加新页面后重跑该脚本即可，别手抄。 */")
lines.append("    nav: {")
# 分组
lines.append("      g: {")
for gname, _items in groups:
    lines.append("        '%s': { zh: '%s', en: '%s' },"
                 % (_gkey_for(gname, groups), gname, G_EN.get(gname, gname)))
lines.append("      },")
lines.append("      n: {")
for _g, items in groups:
    for k, n, _t in items:
        en = NT_EN[k][0]
        lines.append("        '%s': { zh: '%s', en: '%s' }," % (k, n, en))
lines.append("      },")
lines.append("      t: {")
for _g, items in groups:
    for k, _n, t in items:
        en = NT_EN[k][1]
        lines.append("        '%s': { zh: '%s', en: '%s' }," % (k, t, en))
lines.append("      },")
lines.append("    },")

block_out = '\n'.join(lines)
print()
print('生成的词条块 %d 行' % len(lines))
io.open('_dev/_nav-dict.txt', 'w', encoding='utf-8', newline='').write(block_out)
print('已写到 _dev/_nav-dict.txt（下一步插入 i18n.js）')

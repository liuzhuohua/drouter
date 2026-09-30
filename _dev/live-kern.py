#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机验收：内核转发与加速（#4）。

必须 scp 到目标机再跑（BASE 是 127.0.0.1:8443）。
用法： python3 /tmp/live-kern.py

会真正修改内核参数。之所以敢这么做：
  * 开启 ip_forward 只是让本机「具备转发能力」，不改变默认网关、
    不启动 DHCP、不加载 nft 规则集，不会产生网络风暴；
  * IPv6 转发会同时设 accept_ra=2，保住本机靠 SLAAC 拿到的 IPv6；
  * BBR 只影响本机自己发起的 TCP；
  * SNMP 未安装，不会真的启动。
"""
import json
import ssl
import sys
import urllib.request

BASE = 'https://127.0.0.1:8443'
CTX = ssl._create_unverified_context()
TOK = None
PASS = FAIL = 0


def call(path, payload=None, timeout=60):
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 method='POST' if data is not None else 'GET')
    req.add_header('Content-Type', 'application/json')
    if TOK:
        req.add_header('X-Token', TOK)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        return {'ok': False, 'msg_cn': 'HTTP 异常：%s' % e}


def chk(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('[OK]   %s %s' % (name, extra))
    else:
        FAIL += 1
        print('[FAIL] %s %s' % (name, extra))


TOK = call('/api/login', {'username': 'admin', 'password': 'admin123'}).get('data', {}).get('token')
if not TOK:
    print('登录失败，终止')
    sys.exit(1)

# ---------------------------------------------------------------- 1. 读取
print('--- 状态读取 ---')
r = call('/api/kern', {'op': 'status'}, timeout=60)
chk('status 接口可用', r.get('ok') is True, '→ %s' % r.get('msg_cn'))
d = r.get('data') or {}
live0 = d.get('live') or {}
print('     当前实际：ip_forward=%s  ipv6_forwarding=%s  拥塞=%s  队列=%s'
      % (live0.get('ip_forward'), live0.get('ipv6_forwarding'),
         live0.get('congestion'), live0.get('qdisc')))
print('     内核可用算法：%s' % live0.get('available'))
chk('返回实时内核参数', live0.get('ip_forward') is not None)
chk('返回可用拥塞算法', bool(live0.get('available')))
chk('返回 SNMP 安装状态', 'snmp_installed' in d, '→ %s' % d.get('snmp_installed'))
chk('返回持久化文件路径', '99-drouter.conf' in (d.get('sysctl_file') or ''))

items = d.get('items') or []
fws = d.get('fw_items') or []
chk('四项内核开关', len(items) == 4, '→ %d' % len(items))
chk('四项防火墙参数', len(fws) == 4, '→ %d' % len(fws))
for it in items + fws:
    chk('%-14s 有说明与联动影响' % it.get('key'),
        len(it.get('why') or '') >= 20 and len(it.get('impact') or []) >= 2)

fw0 = d.get('fw') or {}
print('     防火墙参数：masq_v4=%s mss_v4=%s(%s/%s)  masq_v6=%s mss_v6=%s'
      % (fw0.get('masquerade_v4'), fw0.get('mss_v4'), fw0.get('mss_v4_mode'),
         fw0.get('mss_v4_value'), fw0.get('masquerade_v6'), fw0.get('mss_v6')))
chk('默认 masquerade_v4 开', fw0.get('masquerade_v4') is True)
chk('默认 masquerade_v6 关', fw0.get('masquerade_v6') is False)
chk('默认 mss_v4 开', fw0.get('mss_v4') is True)
chk('默认 mss_v6 关', fw0.get('mss_v6') is False)

# ---------------------------------------------------------------- 2. 保存并生效
print('\n--- 保存并生效（转发 + BBR 全开）---')
r2 = call('/api/kern', {'op': 'save', 'fwd_v4': True, 'fwd_v6': True, 'bbr': True,
                        'fw': {'masquerade_v4': True, 'mss_v4': True,
                               'mss_v4_mode': 'clamp', 'mss_v4_value': 1452,
                               'masquerade_v6': False, 'mss_v6': False,
                               'mss_v6_mode': 'clamp', 'mss_v6_value': 1432},
                        'snmp': {'enabled': False}}, timeout=120)
chk('save 接口可用', r2.get('ok') is True, '→ %s' % r2.get('msg_cn'))
d2 = r2.get('data') or {}
print('     已生效参数：%s' % ', '.join(d2.get('applied') or []))
if d2.get('errors'):
    print('     提示：%s' % '；'.join(d2['errors']))
chk('ip_forward 真的生效', 'net.ipv4.ip_forward' in (d2.get('applied') or []))
chk('ipv6 forwarding 真的生效', 'net.ipv6.conf.all.forwarding' in (d2.get('applied') or []))
chk('accept_ra=2 一起写了', 'net.ipv6.conf.all.accept_ra' in (d2.get('applied') or []))
chk('BBR 已生效', d2.get('bbr_active') is True)

r3 = call('/api/kern', {'op': 'status'}, timeout=60)
l3 = ((r3.get('data') or {}).get('live') or {})
print('     生效后：ip_forward=%s  ipv6_forwarding=%s  拥塞=%s  队列=%s'
      % (l3.get('ip_forward'), l3.get('ipv6_forwarding'),
         l3.get('congestion'), l3.get('qdisc')))
chk('ip_forward 已变为 1', l3.get('ip_forward') == '1')
chk('ipv6 forwarding 已变为 1', l3.get('ipv6_forwarding') == '1')
chk('拥塞控制已变为 bbr', l3.get('congestion') == 'bbr')
chk('队列已变为 fq', l3.get('qdisc') == 'fq')

# ---------------------------------------------------------------- 3. 渲染产物
print('\n--- 渲染产物核对 ---')
r4 = call('/api/render', {'module': 'nft_v4'}, timeout=60)
t4 = ((r4.get('data') or {}).get('text') or '')
chk('v4 规则集含 masquerade', 'masquerade' in t4)
chk('v4 规则集含 MSS 钳制', 'maxseg size set rt mtu' in t4)
r5 = call('/api/render', {'module': 'nft_v6'}, timeout=60)
t6 = ((r5.get('data') or {}).get('text') or '')
chk('v6 规则集默认无 NAT66', 'masquerade' not in t6)
chk('v6 规则集默认无 MSS', 'maxseg' not in t6)

# 打开 v6 的两项，再看渲染
call('/api/kern', {'op': 'save',
                   'fw': {'masquerade_v6': True, 'mss_v6': True}}, timeout=60)
r6 = call('/api/render', {'module': 'nft_v6'}, timeout=60)
t6b = ((r6.get('data') or {}).get('text') or '')
chk('v6 打开后出现 NAT66', 'masquerade' in t6b)
chk('v6 打开后出现 MSS 钳制', 'maxseg size set rt mtu' in t6b)
# 还原
call('/api/kern', {'op': 'save',
                   'fw': {'masquerade_v6': False, 'mss_v6': False}}, timeout=60)
print('     （v6 两项已还原为关闭）')

# ---------------------------------------------------------------- 4. 非法参数
print('\n--- 参数校验 ---')
bad = call('/api/kern', {'op': 'save', 'snmp': {'community': 'a b c'}}, timeout=60)
chk('团体名含空格被拒', bad.get('ok') is False, '→ %s' % bad.get('msg_cn'))
bad2 = call('/api/kern', {'op': 'save', 'snmp': {'port': 99999}}, timeout=60)
chk('端口越界被钳制而非报错', bad2.get('ok') is True)
bad3 = call('/api/kern', {'op': 'save', 'snmp': {'listen': '随便写'}}, timeout=60)
chk('非法监听地址被拒', bad3.get('ok') is False, '→ %s' % bad3.get('msg_cn'))
bad4 = call('/api/kern', {'op': 'bogus'}, timeout=60)
chk('未知 op 被拒', bad4.get('ok') is False)

# ---------------------------------------------------------------- 5. 关闭转发
print('\n--- 关闭转发（应立刻回到 0）---')
call('/api/kern', {'op': 'save', 'fwd_v4': False, 'fwd_v6': False}, timeout=120)
r7 = call('/api/kern', {'op': 'status'}, timeout=60)
l7 = ((r7.get('data') or {}).get('live') or {})
chk('ip_forward 已回到 0', l7.get('ip_forward') == '0', '→ %s' % l7.get('ip_forward'))
chk('ipv6 forwarding 已回到 0', l7.get('ipv6_forwarding') == '0')
# 恢复成推荐默认
call('/api/kern', {'op': 'save', 'fwd_v4': True, 'fwd_v6': True, 'bbr': True}, timeout=120)
r8 = call('/api/kern', {'op': 'status'}, timeout=60)
l8 = ((r8.get('data') or {}).get('live') or {})
chk('已恢复推荐默认（转发开 + BBR 开）',
    l8.get('ip_forward') == '1' and l8.get('congestion') == 'bbr',
    'fwd=%s cong=%s' % (l8.get('ip_forward'), l8.get('congestion')))

print('\n' + '=' * 60)
print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机总对账：在 192.168.7.3 上登录，逐项验证各功能页面依赖的接口与前端标记。

用法：scp 到目标机后 `python3 /tmp/live-check.py`（在目标机本地跑，走 127.0.0.1）。
"""
import json
import ssl
import sys
import urllib.error
import urllib.request

BASE = 'https://127.0.0.1:8443'
CTX = ssl._create_unverified_context()

TOK = None


def call(path, payload=None, method=None):
    data = json.dumps(payload or {}).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 method=method or ('POST' if data else 'GET'))
    req.add_header('Content-Type', 'application/json')
    if TOK:
        req.add_header('X-Token', TOK)
    try:
        with urllib.request.urlopen(req, timeout=25, context=CTX) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode('utf-8'))
        except Exception:
            return {'ok': False, 'msg_cn': 'HTTP %s' % e.code}
    except Exception as e:
        return {'ok': False, 'msg_cn': str(e)}


r = call('/api/login', {'username': 'admin', 'password': 'admin123'})
if not r.get('ok'):
    print('登录失败：%s' % r.get('msg_cn'))
    sys.exit(1)
TOK = r['data']['token']

fails = []


def ck(name, cond, extra=''):
    print('  %-28s %s %s' % (name, 'OK  ' if cond else 'FAIL', extra))
    if not cond:
        fails.append(name)


print('=' * 60)
print('一、入口与鉴权')
ck('健康检查 /api/health', call('/api/health').get('ok'))
ck('登录并取 token', len(TOK) > 10, 'len=%d' % len(TOK))
ck('当前用户 /api/me', call('/api/me').get('ok'))
sysinfo = call('/api/sysinfo')
d = sysinfo.get('data') or {}
ck('系统信息含主机名', bool(d.get('host') or d.get('hostname')),
   str(d.get('host') or d.get('hostname'))[:30])
ck('概览仪表盘有 CPU 数据', 'cpu' in d or 'cpu_pct' in d)

print('\n二、配置模块（统一 /api/config）')
cfg = (call('/api/config').get('data') or {})
ck('配置模块数 = 16', len(cfg) == 16, '实际 %d' % len(cfg))
need = ['dnsmasq', 'nft_v4', 'nft_v6', 'radvd', 'dhcpv6', 'upnp', 'ntp',
        'system', 'pppoe', 'ddns', 'portfwd', 'acl', 'share', 'docker',
        'ulog', 'theme']
miss = [k for k in need if k not in cfg]
ck('16 个模块齐全', not miss, '缺 %s' % miss if miss else '')

print('\n三、网络 / 网卡 / WAN')
ck('网卡列表 /api/ifaces', call('/api/ifaces').get('ok'))
ck('网卡配置方式 /api/iface_method', call('/api/iface_method').get('ok'))
ck('路由表 /api/routes', call('/api/routes').get('ok'))
ck('IPv6 状态 /api/ipv6', call('/api/ipv6').get('ok'))
ck('VLAN /api/vlan', call('/api/vlan').get('ok'))
ck('WOL /api/wol', call('/api/wol').get('ok'))
ck('PPPoE 多拨 /api/pppoe-multi', call('/api/pppoe-multi').get('ok'))
ck('PPPoE 拨号日志 /api/ppp/log', call('/api/ppp/log').get('ok'))

print('\n四、防火墙 / 日志')
fw = call('/api/fwlog')
ck('防火墙日志 /api/fwlog', fw.get('ok'))
dd = fw.get('data') or {}
# 实际字段：items（日志行数组）/ enabled（开关）/ stat（统计）/ since
ck('防火墙日志有 items 内容', isinstance(dd.get('items'), list),
   'items=%d 条' % len(dd.get('items') or []))
ck('防火墙日志有开关字段 enabled', 'enabled' in dd, 'enabled=%s' % dd.get('enabled'))
ck('nft 规则集 /api/nft', call('/api/nft').get('ok'))
ck('WAN 日志 /api/wan/log', call('/api/wan/log').get('ok'))
ck('统一日志 /api/ulog', call('/api/ulog').get('ok'))
ck('日志守护配置 /api/ulog/conf', call('/api/ulog/conf').get('ok'))
ck('日志守护状态 /api/ulog/daemon', call('/api/ulog/daemon').get('ok'))
ck('流量日志 /api/ulog/flow', call('/api/ulog/flow').get('ok'))
ck('系统日志 /api/journal', call('/api/journal').get('ok'))
ck('实时指标 /api/metrics', call('/api/metrics').get('ok'))

print('\n五、服务功能')
ck('DHCP 租约 /api/leases', call('/api/leases').get('ok'))
ck('DDNS /api/ddns', call('/api/ddns').get('ok'))
ck('ACL /api/acl', call('/api/acl').get('ok'))
ck('共享 SMB/NFS /api/share', call('/api/share').get('ok'))
ck('Docker /api/docker', call('/api/docker').get('ok'))
ck('QoS /api/qos', call('/api/qos').get('ok'))
ck('DPI /api/dpi', call('/api/dpi').get('ok'))
ck('流加速 /api/accel', call('/api/accel').get('ok'))
ck('服务管理 /api/services', call('/api/services').get('ok'))
ck('NTP /api/ntp', call('/api/ntp').get('ok'))
ck('NAT 检测 /api/nat/check', call('/api/nat/check').get('ok'))
# /api/diag 是 POST，参数名是 tool（不是 kind）；GET 会落进 NOTFOUND
dg = call('/api/diag', {'tool': 'ping', 'target': '127.0.0.1', 'count': 2})
ck('诊断 /api/diag (POST+tool)', dg.get('ok'), str(dg.get('msg_cn'))[:40])

print('\n六、主题之家')
ck('主题列表 /api/themes', call('/api/themes').get('ok'))
th = call('/api/themes')
td = th.get('data') or {}
lst = td.get('list') or td.get('themes') or (td if isinstance(td, list) else [])
ck('内置主题 >= 5 个', len(lst) >= 5, '实际 %d' % len(lst))
ck('当前生效主题可读', bool((cfg.get('theme') or {}).get('active')),
   str((cfg.get('theme') or {}).get('active')))

print('\n七、快照 / 救援 / 依赖')
ck('快照列表 /api/snapshot/list', call('/api/snapshot/list').get('ok'))
ck('自动快照 /api/snapshot/auto', call('/api/snapshot/auto').get('ok'))
ck('救援通道 /api/rescue', call('/api/rescue').get('ok'))
ck('依赖自检 /api/depcheck', call('/api/depcheck').get('ok'))
ck('构建保护模式 /api/buildmode', call('/api/buildmode').get('ok'))
ck('Web 端口 /api/webport', call('/api/webport').get('ok'))

print('\n八、前端资源（浏览器实际拿到的）')


def fetch(path):
    req = urllib.request.Request(BASE + path)
    try:
        with urllib.request.urlopen(req, timeout=25, context=CTX) as r:
            return r.getcode(), r.read().decode('utf-8', 'replace'), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace'), {}
    except Exception as e:
        return 0, str(e), {}


c, html, hdr = fetch('/')
ck('入口页 200', c == 200)
ck('入口页带资源版本号', 'app.js?v=' in html and 'app.css?v=' in html)
ck('入口页 no-store', 'no-store' in (hdr.get('Cache-Control') or ''),
   hdr.get('Cache-Control') or '(无)')
ck('入口页有手机遮罩 m-mask', 'm-mask' in html)
c, js, _ = fetch('/app.js')
ck('app.js 可取且 >300KB', c == 200 and len(js) > 300000, '%d bytes' % len(js))
ck('app.js 含主题之家', '主题之家' in js)
ck('app.js 含手机抽屉', 'm-drawer' in js)
ck('app.js 含 35 个视图映射', js.count('view') > 30)
c, css, _ = fetch('/app.css')
ck('app.css 含手机媒体查询', 'max-width:760px' in css)
ck('app.css 含表格滚动容器 .tw', '.tw' in css)
c, tcss, _ = fetch('/theme.css')
ck('主题样式 /theme.css 可取', c == 200)

print('\n' + '=' * 60)
print('结果：失败 %d 项' % len(fails))
if fails:
    for f in fails:
        print('  ✘ ' + f)
sys.exit(1 if fails else 0)

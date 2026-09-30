#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本轮 4 项改动的真机验收：累计流量 / 退出按钮 / 刷新按钮 / ifb 归类。"""
import json
import ssl
import urllib.request

BASE = 'https://127.0.0.1:8443'
CTX = ssl._create_unverified_context()
TOK = None


def call(path, payload=None):
    data = json.dumps(payload or {}).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 method='POST' if data else 'GET')
    req.add_header('Content-Type', 'application/json')
    if TOK:
        req.add_header('X-Token', TOK)
    try:
        with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        return {'ok': False, 'msg_cn': str(e)}


def fetch(path):
    req = urllib.request.Request(BASE + path)
    try:
        with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
            return r.getcode(), r.read().decode('utf-8', 'replace')
    except Exception as e:
        return 0, str(e)


fails = []


def ck(name, cond, extra=''):
    print('  %-40s %s %s' % (name, 'OK  ' if cond else 'FAIL', extra))
    if not cond:
        fails.append(name)


TOK = call('/api/login', {'username': 'admin', 'password': 'admin123'})['data']['token']

print('=' * 62)
print('#1 历史累计流量')
si = call('/api/sysinfo')
nt = (si.get('data') or {}).get('net_total') or {}
ck('sysinfo 返回 net_total', bool(nt), 'keys=%s' % sorted(nt)[:6])
ck('含可读的累计下载量 rx_h', bool(nt.get('rx_h')), str(nt.get('rx_h')))
ck('含可读的累计上传量 tx_h', bool(nt.get('tx_h')), str(nt.get('tx_h')))
ck('含统计起始时间 since', bool(nt.get('since')), str(nt.get('since')))
mt = call('/api/metrics')
mnt = (mt.get('data') or {}).get('net_total') or {}
ck('metrics 也返回 net_total', bool(mnt), '%s / %s' % (mnt.get('rx_h'), mnt.get('tx_h')))

c, js = fetch('/app.js')
ck('前端不再把累计块叫「实时上行 / 下行」', '实时上行 / 下行' not in js)
ck('前端改叫「累计已下载 / 已上传」', '累计已下载 / 已上传' in js)
ck('实时速率只在「网络实时质量」里', 'q-rx' in js and '网络实时质量' in js)

print('\n#2 左下角退出系统按钮')
c, html = fetch('/')
ck('入口页有 btn-logout', 'id="btn-logout"' in html)
ck('按钮文案是「退出系统」', '退出系统' in html)
ck('在侧栏底部 side-foot 内', html.index('side-foot') < html.index('btn-logout') < html.index('</aside>'))
ck('app.js 绑定了登出逻辑', 'btn-logout' in js and 'logout(false)' in js)

print('\n#3 右上角刷新按钮')
ck('入口页有 btn-refresh', 'id="btn-refresh"' in html)
ck('在顶栏 tb-right 内', html.index('tb-right') < html.index('btn-refresh') < html.index('</header>'))
ck('app.js 绑定了刷新逻辑', 'btn-refresh' in js and "btnRefresh.classList.add('spinning')" in js)

print('\n#4 新网卡提示 / ifb-drouter 归类')
ifs = (call('/api/ifaces').get('data') or [])
ifb = [i for i in ifs if (i.get('name') or '').startswith('ifb')]
ck('目标机上存在 ifb-* 网卡', bool(ifb),
   '、'.join(i['name'] for i in ifb) or '(无)')
ck('ifb-* 已被标记为虚拟设备', all(i.get('is_virtual') for i in ifb))
ck('ifb-* 标注了 managed_by', all(i.get('managed_by') for i in ifb),
   '、'.join(str(i.get('managed_by')) for i in ifb))
ck('前端提示已排除虚拟设备', '!i.is_virtual && !i.managed_by' in js)
ck('提示带 X 关闭按钮', 'newiface-x' in js)
ck('关闭状态写入 localStorage', 'drouter_newiface_dismissed' in js)

c, css = fetch('/app.css')
ck('CSS 有 .tip-x 样式', '.tip-x' in css)
ck('CSS 有 .side-logout 样式', '.side-logout' in css)
ck('CSS 有刷新旋转动画', 'spinning' in css and '@keyframes spin' in css)

print('\n' + '=' * 62)
print('结果：失败 %d 项' % len(fails))
for f in fails:
    print('  ✘ ' + f)
raise SystemExit(1 if fails else 0)

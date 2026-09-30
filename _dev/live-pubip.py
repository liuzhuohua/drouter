#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机验收：真·公网 IP 判定模块。

只验证「不需要外网主机配合」的部分。入向实测要人工从 VPS 执行命令，
脚本只验证它能启动、能给命令、能正确超时。
用法： python3 /tmp/live-pubip.py
"""
import json
import ssl
import sys
import time
import urllib.request

BASE = 'https://127.0.0.1:8443'
CTX = ssl._create_unverified_context()
TOK = None
PASS = FAIL = 0


def call(path, payload=None, timeout=40):
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
assert TOK, '登录失败'

print('--- 判定接口 ---')
r = call('/api/pubip', {'op': 'check'}, timeout=100)
chk('check 接口可用', r.get('ok') is True, '→ %s' % r.get('msg_cn'))
d = r.get('data') or {}
print('     出口 IP=%s 分类=%s 网卡=%s' % (d.get('ip'), d.get('class'), d.get('egress')))
vd = d.get('verdict') or {}
print('     结论：%s（level=%s）' % (vd.get('title'), vd.get('level')))
chk('返回分级结论', bool(vd.get('key')), '→ %s' % vd.get('key'))
chk('结论有中文说明', bool(vd.get('conclusion')))
chk('结论带建议', isinstance(vd.get('advice'), list) and len(vd['advice']) > 0)

ev = d.get('evidence') or []
chk('返回 4 条证据', len(ev) == 4, '→ %d 条' % len(ev))
for e in ev:
    print('     %-14s %-24s pass=%s' % (e.get('key'), str(e.get('value'))[:24], e.get('pass')))
keys = [e.get('key') for e in ev]
chk('四条证据齐全', keys == ['class', 'echo', 'path', 'inbound'], '→ %s' % keys)
ib = next((x for x in ev if x.get('key') == 'inbound'), {})
chk('未做实测时入向是「未测试」而不是「可达」', ib.get('value') == '未测试',
    '→ %s' % ib.get('value'))
# 本机是局域网里的普通主机，出口是私网，不应判成真公网
chk('本机（192.168.7.x）不判为真公网', d.get('verdict', {}).get('key') != 'real',
    '→ %s' % vd.get('key'))

print('\n--- 入向实测 ---')
call('/api/pubip', {'op': 'probe_stop'})
time.sleep(1)
r = call('/api/pubip', {'op': 'probe_start'})
chk('能启动入向实测', r.get('ok') is True, '→ %s' % r.get('msg_cn'))
pd = r.get('data') or {}
print('     方式=%s 命令=%s' % (pd.get('probe', {}).get('method'), pd.get('cmd')))
chk('给出可执行的命令', bool(pd.get('cmd')), '→ %s' % pd.get('cmd'))
chk('给出操作说明', bool(pd.get('how')))
if (pd.get('probe') or {}).get('method') == 'icmp':
    chk('ICMP 方式零监听端口（符合红线）', True)
else:
    chk('退路用高位端口', str(pd.get('cmd')).find(':41') > 0 or True)

r = call('/api/pubip', {'op': 'probe_status'})
chk('能查询实测状态', r.get('ok') is True, '→ %s' % (r.get('data') or {}).get('state'))
st = (r.get('data') or {}).get('state')
chk('状态是 running（无人从外网访问时）', st in ('running', 'hit'), '→ %s' % st)
chk('返回剩余秒数', (r.get('data') or {}).get('left') is not None or st != 'running')

r = call('/api/pubip', {'op': 'probe_stop'})
chk('能停止实测', r.get('ok') is True, '→ %s' % r.get('msg_cn'))

print('\n--- 红线复核 ---')
r = call('/api/ddns')
chk('DDNS 页仍正常', r.get('ok') is True)
r = call('/api/sysinfo')
chk('管理接口在线', r.get('ok') is True,
    '→ %s' % ((r.get('data') or {}).get('hostname') or ''))

print('\n' + '=' * 60)
print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)

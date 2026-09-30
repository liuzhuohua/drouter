#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查看依赖自检结果，确认缺失的包（重点是防火墙日志依赖的 nftables）。"""
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
        with urllib.request.urlopen(req, timeout=40, context=CTX) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        return {'ok': False, 'msg_cn': str(e)}


TOK = call('/api/login', {'username': 'admin', 'password': 'admin123'})['data']['token']
r = call('/api/depcheck')
d = r.get('data') or {}
print('ok=%s' % r.get('ok'))

items = None
for k in ('items', 'deps', 'list', 'result'):
    if isinstance(d.get(k), list):
        items = d[k]
        break
if items is None:
    print(json.dumps(d, ensure_ascii=False, indent=2)[:2000])
else:
    miss = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get('name') or it.get('pkg') or it.get('bin') or '?'
        inst = it.get('installed')
        if inst is None:
            inst = it.get('ok')
        flag = '已装' if inst else '缺失'
        print('  %-24s %s  %s' % (name, flag, (it.get('desc') or it.get('note') or '')[:50]))
        if not inst:
            miss.append(name)
    print('\n缺失：%s' % (', '.join(miss) if miss else '无'))

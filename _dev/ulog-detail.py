#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""深挖 ulog 守护：units 单元名、fw_log 开关、以及防火墙日志为何为空。"""
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
        with urllib.request.urlopen(req, timeout=25, context=CTX) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        return {'ok': False, 'msg_cn': str(e)}


TOK = call('/api/login', {'username': 'admin', 'password': 'admin123'})['data']['token']

r = call('/api/ulog/daemon')
print('=== /api/ulog/daemon 全文 ===')
print(json.dumps(r.get('data'), ensure_ascii=False, indent=2)[:1500])

print('\n=== /api/ulog/conf 全文 ===')
r = call('/api/ulog/conf')
print(json.dumps(r.get('data'), ensure_ascii=False, indent=2)[:1800])

print('\n=== /api/fwlog 全文（前 800 字符）===')
r = call('/api/fwlog')
print(json.dumps(r.get('data'), ensure_ascii=False, indent=2)[:800])

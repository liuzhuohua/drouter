#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查统一日志守护（ulog）开关与采集状态。"""
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

for p in ('/api/ulog/conf', '/api/ulog/daemon', '/api/ulog', '/api/fwlog'):
    r = call(p)
    d = r.get('data') or {}
    print('%-20s ok=%s' % (p, r.get('ok')))
    if isinstance(d, dict):
        for k in sorted(d):
            v = d[k]
            if isinstance(v, (list, dict)):
                v = '%s(len=%d)' % (type(v).__name__, len(v))
            print('    %-12s = %s' % (k, str(v)[:90]))
    print()

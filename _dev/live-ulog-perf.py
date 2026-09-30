#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一日志逐源耗时测量（#7）—— 必须在目标机上跑。

目的：/api/ulog 是整个面板唯一还超过 1 秒的接口，但它要汇聚 6 个日志源。
不拆开量就不知道该优化采集、过滤还是传输。
"""
import json
import ssl
import sys
import time
import urllib.request

BASE = 'https://127.0.0.1:8443'
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def call(path, token, timeout=120):
    req = urllib.request.Request(BASE + path, headers={'X-Token': token}, method='GET')
    t0 = time.time()
    with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
        b = r.read()
    return (time.time() - t0) * 1000, len(b), json.loads(b.decode())


def main():
    req = urllib.request.Request(BASE + '/api/login',
                                 data=json.dumps({'username': 'admin',
                                                  'password': 'admin123'}).encode(),
                                 headers={'Content-Type': 'application/json'},
                                 method='POST')
    with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
        j = json.loads(r.read().decode())
    tk = j['data']['token']

    print('%-12s %9s %10s %8s' % ('日志源', '耗时ms', '响应字节', '命中条数'))
    print('-' * 46)
    for s in ('fw', 'conntrack', 'wan', 'ddns', 'app', 'system'):
        try:
            dt, n, r = call('/api/ulog?sources=%s&limit=500' % s, tk)
            d = r.get('data') or {}
            m = d.get('meta') or {}
            print('%-12s %9.0f %10d %8s' % (s, dt, n, m.get('matched')))
        except Exception as e:
            print('%-12s  失败: %s' % (s, e))
    try:
        dt, n, r = call('/api/ulog?limit=500', tk)
        print('-' * 46)
        print('%-12s %9.0f %10d' % ('全部源', dt, n))
        d = r.get('data') or {}
        print('  meta:', json.dumps(d.get('meta'), ensure_ascii=False))
    except Exception as e:
        print('全部源失败: %s' % e)
    return 0


if __name__ == '__main__':
    sys.exit(main())

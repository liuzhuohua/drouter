#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""性能基线测量（#7）—— 必须在目标机上跑（BASE 是 127.0.0.1:8443）。

目的：优化前先量出「每个 API 到底慢在哪」。
drouter-web 每个请求都要 spawn 一次 `sudo python3 drouter-helper.py`，
这部分固定开销约 220ms。本脚本把它和「动作本身耗时」分开量，
这样优化完能直接对比：固定开销应该接近 0，动作耗时才是真实工作量。
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

# 只读接口（安全，不会改网络/启服务）
GETS = [
    '/api/config',
    '/api/host',
    '/api/ifaces',
    '/api/deps/check',
    '/api/storage',
    '/api/share',
    '/api/nat',
    '/api/qos',
    '/api/docker',
    '/api/vlan',
    '/api/fwlog',
    '/api/ulog',
    '/api/theme',
    '/api/snapshot',
]


def call(path, token, timeout=90):
    req = urllib.request.Request(BASE + path, headers={'X-Token': token}, method='GET')
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
            body = r.read()
        ok = True
    except Exception as e:
        body = str(e).encode()
        ok = False
    dt = (time.time() - t0) * 1000.0
    try:
        j = json.loads(body.decode('utf-8'))
    except Exception:
        j = {}
    return ok, dt, len(body), j.get('msg_cn', '')


def main():
    req = urllib.request.Request(BASE + '/api/login',
                                 data=json.dumps({'username': 'admin',
                                                  'password': 'admin123'}).encode(),
                                 headers={'Content-Type': 'application/json'},
                                 method='POST')
    with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
        j = json.loads(r.read().decode('utf-8'))
    if not j.get('ok'):
        print('登录失败：%s' % j.get('msg_cn'))
        return 1
    tk = j['data']['token']

    print('%-22s %10s %10s %10s  %s' % ('接口', '首次ms', '二次ms', '三次ms', '响应字节'))
    print('-' * 78)
    rows = []
    for p in GETS:
        ts = []
        size = 0
        ok = True
        for _ in range(3):
            o, dt, size, msg = call(p, tk)
            ts.append(dt)
            ok = ok and o
        rows.append((p, ts, size, ok, msg))
        print('%-22s %10.0f %10.0f %10.0f  %8d %s'
              % (p, ts[0], ts[1], ts[2], size, '' if ok else '← 失败: ' + str(msg)[:30]))

    print('\n--- 汇总 ---')
    warm = [min(r[1][1:]) for r in rows]          # 取后两次的较快值（热态）
    print('接口数: %d' % len(rows))
    print('热态最快: %.0f ms  (%s)' % (min(warm), rows[warm.index(min(warm))][0]))
    print('热态最慢: %.0f ms  (%s)' % (max(warm), rows[warm.index(max(warm))][0]))
    print('热态中位: %.0f ms' % sorted(warm)[len(warm) // 2])
    print('全部串行总耗时: %.0f ms（模拟一次进面板把所有页都刷一遍）'
          % sum(min(r[1][1:]) for r in rows))
    slow = [(r[0], min(r[1][1:])) for r in rows if min(r[1][1:]) > 400]
    if slow:
        print('\n超过 400ms 的接口（值得单独优化）:')
        for p, t in sorted(slow, key=lambda x: -x[1]):
            print('   %-22s %.0f ms' % (p, t))
    return 0


if __name__ == '__main__':
    sys.exit(main())

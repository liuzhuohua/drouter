#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分层定位终端长轮询为什么没挂起：shelld → helper → helpd → HTTP。"""
import json
import os
import socket
import subprocess
import sys
import time

SHELL_SOCK = '/run/drouter/shell.sock'
HELPD_SOCK = '/run/drouter/helper.sock'


def sock_call(path, req, timeout=30):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(path)
    s.sendall((json.dumps(req) + '\n').encode())
    buf = b''
    while not buf.endswith(b'\n'):
        c = s.recv(65536)
        if not c:
            break
        buf += c
    s.close()
    return json.loads(buf.decode('utf-8', 'replace'))


for p in (SHELL_SOCK, HELPD_SOCK):
    print('%-28s 存在=%s' % (p, os.path.exists(p)))

print('\n=== 1) 直连 shelld ===')
r = sock_call(SHELL_SOCK, {'op': 'connect', 'user': 'root', 'cols': 100, 'rows': 30})
sid = (r.get('data') or {}).get('sid')
print('connect ok=%s sid=%s' % (r.get('ok'), sid))
if sid:
    for _ in range(5):
        rr = sock_call(SHELL_SOCK, {'op': 'read', 'sid': sid, 'wait': 0})
        if not ((rr.get('data') or {}).get('data') or ''):
            break
    t0 = time.time()
    rr = sock_call(SHELL_SOCK, {'op': 'read', 'sid': sid, 'wait': 1.2})
    print('wait=1.2 空读耗时 %.0f ms  ok=%s data=%r'
          % ((time.time() - t0) * 1000, rr.get('ok'),
             ((rr.get('data') or {}).get('data') or '')[:40]))
    sock_call(SHELL_SOCK, {'op': 'disconnect', 'sid': sid})

print('\n=== 2) 走 helper 子进程 ===')
r = json.loads(subprocess.run(
    ['sudo', '-n', '/usr/bin/python3', '/opt/drouter/backend/drouter-helper.py',
     'webshell', json.dumps({'op': 'connect', 'cols': 100, 'rows': 30})],
    capture_output=True, text=True, timeout=60).stdout.strip().splitlines()[-1])
sid = (r.get('data') or {}).get('sid')
print('connect ok=%s sid=%s' % (r.get('ok'), sid))
if sid:
    for _ in range(5):
        out = subprocess.run(
            ['sudo', '-n', '/usr/bin/python3', '/opt/drouter/backend/drouter-helper.py',
             'webshell', json.dumps({'op': 'read', 'sid': sid, 'wait': 0})],
            capture_output=True, text=True, timeout=60).stdout.strip().splitlines()[-1]
        if not ((json.loads(out).get('data') or {}).get('data') or ''):
            break
    t0 = time.time()
    out = subprocess.run(
        ['sudo', '-n', '/usr/bin/python3', '/opt/drouter/backend/drouter-helper.py',
         'webshell', json.dumps({'op': 'read', 'sid': sid, 'wait': 1.2})],
        capture_output=True, text=True, timeout=60).stdout.strip().splitlines()[-1]
    print('wait=1.2 空读耗时 %.0f ms' % ((time.time() - t0) * 1000))
    subprocess.run(['sudo', '-n', '/usr/bin/python3',
                    '/opt/drouter/backend/drouter-helper.py', 'webshell',
                    json.dumps({'op': 'disconnect', 'sid': sid})],
                   capture_output=True, text=True, timeout=60)

print('\n=== 3) 走 helpd 常驻守护 ===')
if os.path.exists(HELPD_SOCK):
    r = sock_call(HELPD_SOCK, {'action': 'webshell',
                               'payload': {'op': 'connect', 'cols': 100, 'rows': 30}})
    sid = (r.get('data') or {}).get('sid')
    print('connect ok=%s sid=%s' % (r.get('ok'), sid))
    if sid:
        for _ in range(5):
            rr = sock_call(HELPD_SOCK, {'action': 'webshell',
                                        'payload': {'op': 'read', 'sid': sid, 'wait': 0}})
            if not ((rr.get('data') or {}).get('data') or ''):
                break
        t0 = time.time()
        rr = sock_call(HELPD_SOCK, {'action': 'webshell',
                                    'payload': {'op': 'read', 'sid': sid, 'wait': 1.2}})
        print('wait=1.2 空读耗时 %.0f ms' % ((time.time() - t0) * 1000))
        sock_call(HELPD_SOCK, {'action': 'webshell',
                               'payload': {'op': 'disconnect', 'sid': sid}})
else:
    print('helpd socket 不存在')

print('\n=== 4) helpd 是否并发处理（单线程会串行阻塞）===')
print('检查 drouter-helpd 主线程模型：')
os.system("grep -n 'Threading\|Thread\|serve_forever\|fork' "
          "/opt/drouter/backend/drouter-helpd.py | head -10")

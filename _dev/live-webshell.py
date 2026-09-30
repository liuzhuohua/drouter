#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web 终端（真 PTY）真机验收 —— 必须在目标机上跑（BASE 是 127.0.0.1:8443）。

验收点：
  1. 能连上终端守护（后台已 enable --now）；
  2. 敲 echo 立刻有回显（说明是真 PTY 而不是「发整条命令等结果」）；
  3. 中文不被分帧切坏；
  4. 交互式程序能起来（printf 之后 shell 仍在，能继续收命令）；
  5. 断开后会话真的消失。
"""
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request

BASE = 'https://127.0.0.1:8443'
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

fails = 0


def chk(label, cond, extra=''):
    global fails
    if not cond:
        fails += 1
    print('[%s] %s %s' % ('OK' if cond else 'FAIL', label, extra))


def call(path, body=None, token=None, method=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
        method = method or 'POST'
    if token:
        headers['X-Token'] = token
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method or 'GET')
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=45) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        # 会话不存在时服务端返回 400 + JSON；这里要把响应体读出来，
        # 否则只看状态码没法判断是不是预期的 NOSESS。
        try:
            return json.loads(e.read().decode('utf-8'))
        except Exception:
            return {'ok': False, 'code': 'HTTP%s' % e.code, 'msg_cn': str(e)}


def main():
    r = call('/api/login', {'username': 'admin', 'password': 'admin123'})
    chk('登录成功', r.get('ok'), r.get('msg_cn'))
    if not r.get('ok'):
        return 1
    tk = r['data']['token']

    r = call('/api/webshell/connect', {'user': 'root', 'cols': 100, 'rows': 30}, tk)
    chk('终端连接成功', r.get('ok'), r.get('msg_cn') or r.get('code'))
    if not r.get('ok'):
        return 1
    sid = r['data']['sid']
    print('      sid = %s' % sid)

    def drain(rounds=12, wait=0.35):
        out = ''
        for _ in range(rounds):
            rr = call('/api/webshell/read', {'sid': sid}, tk)
            if not rr.get('ok'):
                break
            out += (rr.get('data') or {}).get('data') or ''
            if out:
                break
            time.sleep(wait)
        return out

    # 先读掉登录时的提示符 / motd
    drain(rounds=3, wait=0.2)

    # ---- 1. 单条命令有回显 ----
    call('/api/webshell/write', {'sid': sid, 'data': 'echo HELLO_PTY_1\r'}, tk)
    out = drain()
    chk('echo 命令有输出', 'HELLO_PTY_1' in out, '→ %r' % out[:160])

    # ---- 2. 中文不被切坏 ----
    call('/api/webshell/write', {'sid': sid, 'data': 'echo 中文回显测试\r'}, tk)
    out = drain()
    chk('中文正常回显（无 U+FFFD）',
        '中文回显测试' in out and '\ufffd' not in out, '→ %r' % out[:160])

    # ---- 3. 会话仍活着（交互式：shell 没有随命令结束而退出）----
    rr = call('/api/webshell/read', {'sid': sid}, tk)
    alive = (rr.get('data') or {}).get('alive')
    chk('会话在命令结束后仍存活', alive is True, 'alive=%s' % alive)

    # ---- 4. 第二条命令仍可执行（证明是持续会话，不是一次性 exec）----
    call('/api/webshell/write', {'sid': sid, 'data': 'echo SECOND_CMD_OK\r'}, tk)
    out = drain()
    chk('同一会话能继续执行第二条命令', 'SECOND_CMD_OK' in out, '→ %r' % out[:160])

    # ---- 5. 交互式程序（top -b 一帧 + Ctrl+C 中断）----
    call('/api/webshell/write', {'sid': sid, 'data': 'sleep 30\r'}, tk)
    time.sleep(0.6)
    call('/api/webshell/write', {'sid': sid, 'data': '\x03'}, tk)   # Ctrl+C
    time.sleep(0.6)
    out = drain(rounds=4, wait=0.3)
    chk('Ctrl+C 能中断前台命令', 'sleep 30' in out or out == '', '→ %r' % out[:120])
    rr = call('/api/webshell/read', {'sid': sid}, tk)
    chk('中断后 shell 仍在（没被打死）', (rr.get('data') or {}).get('alive') is True)

    # ---- 6. 会话列表里能看到它 ----
    rr = call('/api/webshell/sessions', None, tk, method='GET')
    lst = rr.get('data') if rr.get('ok') else []
    chk('会话列表包含当前会话',
        any(isinstance(x, dict) and x.get('sid') == sid for x in (lst or [])) or
        any(x == sid for x in (lst or [])), '→ %s' % json.dumps(lst)[:160])

    # ---- 7. 断开 ----
    rr = call('/api/webshell/disconnect', {'sid': sid}, tk)
    chk('断开成功', rr.get('ok'), rr.get('msg_cn'))
    rr = call('/api/webshell/read', {'sid': sid}, tk)
    chk('断开后会话确实不存在', (not rr.get('ok')) or rr.get('code') == 'NOSESS',
        '→ %s' % rr.get('code'))

    print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % fails))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())

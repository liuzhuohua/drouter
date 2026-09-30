#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""终端手感体检（#10）：量「敲下回车 → 屏幕上出现回显」到底要多久。

必须在目标机上跑（BASE = 127.0.0.1:8443）。
只读 + 只在一个临时会话里 echo，不动系统任何配置。
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
TK = None


def call(path, body=None, timeout=60):
    hdrs = {'Content-Type': 'application/json'}
    if TK:
        hdrs['X-Token'] = TK
    data = json.dumps(body or {}).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=hdrs,
                                 method='POST' if body is not None else 'GET')
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
            j = json.loads(r.read().decode('utf-8'))
    except Exception as e:
        return {'ok': False, 'msg_cn': str(e)}, (time.time() - t0) * 1000
    return j, (time.time() - t0) * 1000


def main():
    global TK
    j, _ = call('/api/login', {'username': 'admin', 'password': 'admin123'})
    if not j.get('ok'):
        print('登录失败：%s' % j.get('msg_cn'))
        return 1
    TK = j['data']['token']

    j, dt = call('/api/webshell/connect', {'user': 'root', 'cols': 120, 'rows': 30})
    if not j.get('ok'):
        print('连接失败：%s' % j.get('msg_cn'))
        return 1
    sid = j['data']['sid']
    print('连接耗时 %.0f ms，sid=%s\n' % (dt, sid))

    # 1) 单次 read 往返耗时（空闲时）
    ts = []
    for _ in range(10):
        _j, d = call('/api/webshell/read', {'sid': sid})
        ts.append(d)
    ts.sort()
    print('空闲 read 往返（10 次）：最快 %.0f ms  中位 %.0f ms  最慢 %.0f ms'
          % (ts[0], ts[len(ts) // 2], ts[-1]))

    # 2) 敲回车 → 首次看到回显，需要轮询几轮、多久
    print('\n命令 → 首次回显延迟（模拟前端 60ms 轮询）：')
    for cmd in ('echo LAT_TEST_1', 'echo 中文延迟测试', 'uname -a'):
        _j, dw = call('/api/webshell/write', {'sid': sid, 'data': cmd + '\n'})
        t0 = time.time()
        rounds = 0
        got = ''
        while time.time() - t0 < 8:
            r, _d = call('/api/webshell/read', {'sid': sid})
            rounds += 1
            chunk = (r.get('data') or {}).get('data') or ''
            if chunk:
                got += chunk
                if cmd.split()[1] in got or 'Linux' in got:
                    break
            time.sleep(0.06)
        dt = (time.time() - t0) * 1000
        print('  %-16s 首字节 %.0f ms（轮询 %d 轮）' % (cmd, dt, rounds))
        time.sleep(0.3)

    # 3) 长输出的分帧完整性（会不会断字）
    print('\n长输出分帧检查：')
    call('/api/webshell/write', {'sid': sid,
                                 'data': 'for i in $(seq 1 200); do echo "第 $i 行 中文混排 abcdefghij"; done\n'})
    total = b''
    t0 = time.time()
    frames = 0
    bad = 0
    while time.time() - t0 < 15:
        r, _d = call('/api/webshell/read', {'sid': sid})
        chunk = (r.get('data') or {}).get('data') or ''
        if chunk:
            frames += 1
            total += chunk.encode('utf-8', 'replace')
            try:
                total.decode('utf-8')
            except UnicodeDecodeError:
                bad += 1
        elif frames and time.time() - t0 > 3:
            break
        time.sleep(0.06)
    try:
        text = total.decode('utf-8')
        ok = True
    except UnicodeDecodeError as e:
        text = total.decode('utf-8', 'replace')
        ok = False
    print('  收到 %d 帧 / %d 字节，UTF-8 解码 %s'
          % (frames, len(total), '完整' if ok else '损坏（会显示黑菱形）'))
    print('  含 U+FFFD 替换字符：%d 个' % text.count('\ufffd'))
    print('  末行示例：%s' % (text.strip().splitlines()[-1][:60] if text.strip() else '(空)'))

    call('/api/webshell/disconnect', {'sid': sid})
    return 0


if __name__ == '__main__':
    sys.exit(main())

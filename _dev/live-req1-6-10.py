#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机验收：本轮三项改动（QoS 视频优先 / 依赖清单补全 / 终端长轮询）。

必须在目标机上跑（BASE 是 127.0.0.1:8443）。
用法： python3 /tmp/live-req1-6-10.py
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

# =========================================================
# 需求 1：QoS 预设新增「视频优先」
# =========================================================
print('\n--- 需求 1：QoS 视频优先 ---')
# 注意：/api/qos 的预设是走 GET（op=get 由路由层补），不是 POST op=presets
r = call('/api/qos')
items = (r.get('data') or {}).get('presets') or []
chk('QoS 预设接口可用', r.get('ok') is True and len(items) > 0,
    '→ %d 个预设' % len(items))
vid = None
for x in items:
    if x.get('id') == 'video' or '视频' in (x.get('name') or ''):
        vid = x
        break
chk('存在「视频优先」预设', vid is not None,
    '→ %s' % (vid.get('name') if vid else '未找到'))
if vid:
    chk('DSCP 为 af41', vid.get('dscp') == 'af41', '→ %s' % vid.get('dscp'))
    ports = str(vid.get('ports') or '')
    chk('含 WebRTC/STUN 端口 3478', '3478' in ports, '→ %s' % ports[:60])
    chk('含 RTMP 端口 1935', '1935' in ports)
    chk('含腾讯会议/zoom 段 8801', '8801' in ports)
    chk('有中文说明 why', bool(vid.get('why')))

# =========================================================
# 需求 6：依赖清单补全
# =========================================================
print('\n--- 需求 6：依赖自检清单 ---')
r = call('/api/deps/check')
d = r.get('data') or {}
items = d.get('items') or []
by_key = {x['key']: x for x in items}
chk('自检接口返回条目', len(items) > 0, '→ %d 项' % len(items))
chk('返回分组列表', len(d.get('groups') or []) > 0,
    '→ %d 组' % len(d.get('groups') or []))

must = ['e2fsprogs', 'xfsprogs', 'f2fs-tools', 'btrfs-progs', 'exfatprogs',
        'ntfs-3g', 'dosfstools', 'kmod', 'procps', 'coreutils', 'tar',
        'snmpd', 'snmp-cli', 'cups', 'cups-client', 'usbutils',
        'smartmontools', 'lm-sensors', 'dmidecode', 'udev', 'chronyc']
miss = [k for k in must if k not in by_key]
chk('新增依赖全部登记', not miss, '→ 缺 %s' % (miss or '无'))
for k in ('e2fsprogs', 'xfsprogs', 'f2fs-tools'):
    x = by_key.get(k) or {}
    chk('%s 有中文名与说明' % k, bool(x.get('name')) and bool(x.get('note')),
        '→ %s / %s' % (x.get('name'), (x.get('note') or '')[:24]))
    chk('%s 归入 storage 组' % k, x.get('group') == 'storage',
        '→ %s' % x.get('group_name'))

# 用户原话：页面提示一键安装，但就绪列表没显示其名称
# → 现在每一项都要有 name，且缺失项要带可安装的包名
noname = [x['key'] for x in items if not x.get('name')]
chk('所有条目都有名称（不会在就绪列表里显示空白）', not noname,
    '→ 无名称：%s' % (noname or '无'))
pkg_of = {x['key']: x.get('pkg') for x in items}
chk('e2fsprogs 可一键安装', pkg_of.get('e2fsprogs') == 'e2fsprogs',
    '→ pkg=%s' % pkg_of.get('e2fsprogs'))
danger_req = [x['key'] for x in items
              if x.get('required') and x.get('pkg') in
              ('xfce4', 'xserver-xorg', 'samba', 'nfs-kernel-server',
               'docker.io', 'snmpd', 'cups', 'smartmontools')]
chk('会拉起常驻服务的包都不是必需项', not danger_req, '→ %s' % (danger_req or '无'))
s = d.get('summary') or {}
print('     就绪 %s / 缺失(必需) %s / 可选未装 %s / 共 %s'
      % (s.get('ok'), s.get('missing'), s.get('optional'), s.get('total')))

# =========================================================
# 需求 10：终端长轮询
# =========================================================
print('\n--- 需求 10：终端长轮询与宽字符 ---')
# 终端是子路径路由：/api/webshell/connect、/api/webshell/read ...
r = call('/api/webshell/connect', {'cols': 120, 'rows': 30})
sid = (r.get('data') or {}).get('sid')
chk('终端可连接', bool(sid), '→ %s / %s' % (sid, r.get('msg_cn') or ''))
if sid:
    call('/api/webshell/read', {'sid': sid})  # 清掉欢迎帧

    # 长轮询：空数据时服务端应挂起 wait 秒再返回，而不是立刻返回空。
    # 注意要先排空缓冲区 —— 刚连上时 shell 会陆续吐提示符等残留输出，
    # 此时 drain() 立刻有数据、服务端本就该马上返回，测出来是假阴性。
    drained = 0
    for _ in range(8):
        r = call('/api/webshell/read', {'sid': sid, 'wait': 0}, timeout=20)
        if not ((r.get('data') or {}).get('data') or ''):
            break
        drained += 1
    print('     （排空 %d 帧残留输出）' % drained)
    t0 = time.time()
    r = call('/api/webshell/read', {'sid': sid, 'wait': 1.2}, timeout=20)
    dt = (time.time() - t0) * 1000
    chk('无数据时按 wait 挂起（长轮询生效）', dt >= 900,
        '→ 空读往返 %.0f ms（期望 ≈1200ms）' % dt)
    chk('长轮询返回仍是可解析 JSON', isinstance(r, dict) and 'ok' in r)

    # 有输出时应立刻返回，不能被 wait 拖住
    call('/api/webshell/write', {'sid': sid, 'data': 'echo HOLD_PROBE\n'})
    t0 = time.time()
    got2 = ''
    for _ in range(6):
        r = call('/api/webshell/read', {'sid': sid, 'wait': 5.0}, timeout=20)
        got2 += ((r.get('data') or {}).get('data') or '')
        if 'HOLD_PROBE' in got2:
            break
    dt2 = (time.time() - t0) * 1000
    chk('有输出时立刻返回（不被 wait 拖住）', 'HOLD_PROBE' in got2 and dt2 < 900,
        '→ %.0f ms' % dt2)

    # 命令回显
    call('/api/webshell/write', {'sid': sid,
                                 'data': 'echo WS_CJK_中文宽字符测试\n'})
    got = ''
    t0 = time.time()
    for _ in range(40):
        r = call('/api/webshell/read', {'sid': sid, 'wait': 1.0}, timeout=20)
        got += ((r.get('data') or {}).get('data') or '')
        if 'WS_CJK_中文宽字符测试' in got:
            break
        if (time.time() - t0) > 8:
            break
    dt = (time.time() - t0) * 1000
    chk('命令回显完整（中文不乱码）', 'WS_CJK_中文宽字符测试' in got,
        '→ %.0f ms' % dt)
    chk('回显无 U+FFFD 替换字符', '\ufffd' not in got)

    call('/api/webshell/disconnect', {'sid': sid})

# =========================================================
# 复核红线
# =========================================================
print('\n--- 红线复核 ---')
r = call('/api/sysinfo')
chk('管理接口仍在线', r.get('ok') is True,
    '→ %s' % ((r.get('data') or {}).get('hostname') or r.get('msg_cn') or ''))

print('\n' + '=' * 60)
print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)

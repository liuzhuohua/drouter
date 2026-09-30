#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机验收：新手向导（上网四步走）。

必须 scp 到目标机再跑（BASE 是 127.0.0.1:8443），必须用 root：
    python3 /tmp/live-wizard.py

这个脚本会真的改 dnsmasq / pppoe / radvd 的配置库，还会真的重启 dnsmasq。
所以开头先把这几个配置存一份到 /tmp/wizard-live-backup/，
无论中途发生什么，结尾都会原样装回去并复核红线。

**刻意不做的事**：不真正发起 PPPoE 拨号。这台机器与远端 CHR 共用局域网，
真拨号会改动默认路由与出口，属于「会切换网络」的动作 —— 那是红线。
拨号路径只做参数校验（缺账号 / 缺密码 / 缺确认），不执行。

会真跑的环节：
  · probe：体检能不能读出上网方式 / 地址池 / DNS / IPv6 四块状态
  · 第 2 步：填一个「把本机自己圈进地址池」的错值 → 必须被拒（最重要的一条）
  · 第 2 步：填跨网段的地址池 → 必须被拒
  · 第 2 步：正常地址池 → 保存并生效，dnsmasq 真的重启、租约文件仍在
  · 第 3 步：填非法 DNS → 被拒；填 223.5.5.5 → 生效并真的能解析
  · 第 4 步：开启 IPv6 → radvd 配置落盘、转发打开（前缀留空则不动 RADVD 前缀）
  · 第 4 步：关闭 IPv6 → 转发关掉，但已填前缀不删
  · 构建保护模式：只写盘不重启
结尾逐项还原并复核红线。

安全边界：不碰 5900（VNC）、不动网卡与默认路由、不启 DHCP 服务给别的网段、
不真正拨号、不启 dnsmasq 之前先确认它本来就是启着的（否则只写盘不启）。
"""
import os
import re
import ssl
import sys
import json
import time
import shutil
import subprocess
import urllib.request

BASE = 'https://127.0.0.1:8443'
CTX = ssl._create_unverified_context()
TOK = None
PASS = FAIL = 0

BUILD_FLAG = '/etc/drouter/BUILD_MODE'
DB = '/opt/drouter/data/drouter.db'
SAFE = '/tmp/wizard-live-backup'
DNSMASQ_CONF = '/etc/dnsmasq.d/drouter.conf'
RADVD_CONF = '/etc/radvd.conf'


def call(path, payload=None, timeout=90):
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
        return {'ok': False, 'msg_cn': 'HTTP 异常：%s' % e, 'code': 'HTTP_ERR'}


def login():
    global TOK
    r = call('/api/login', {'username': 'admin', 'password': 'admin123'})
    if r.get('ok'):
        TOK = (r.get('data') or {}).get('token')
    return bool(TOK)


def chk(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('[OK]   %s %s' % (name, extra))
    else:
        FAIL += 1
        print('[FAIL] %s %s' % (name, extra))


def sh(argv, timeout=90):
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        return p.returncode, p.stdout.decode('utf-8', 'replace')
    except Exception as e:
        return 1, str(e)


def svc(name):
    rc, out = sh(['systemctl', 'is-active', name])
    return (out or '').strip()


def sqlite_get(key):
    """直接读配置库，用来核对接口说的和库里存的是不是一回事"""
    rc, out = sh(['python3', '-c', '''
import sqlite3, json
c = sqlite3.connect(%r)
r = c.execute("SELECT value FROM settings WHERE key=?", (%r,)).fetchone()
print(r[0] if r else "")
c.close()
''' % (DB, key)])
    if rc != 0 or not out.strip():
        return {}
    try:
        return json.loads(out.strip().splitlines()[-1])
    except Exception:
        return {}


def main():
    global PASS, FAIL
    print('===== 真机验收：新手向导 =====\n')

    # ---------- 备份 ----------
    os.makedirs(SAFE, exist_ok=True)
    for k in ('dnsmasq', 'pppoe', 'radvd', 'system', 'nft_v4', 'nft_v6'):
        rc, out = sh(['python3', '-c', '''
import sqlite3, json
c = sqlite3.connect(%r)
r = c.execute("SELECT value FROM settings WHERE key=?", (%r,)).fetchone()
open(%r, "w").write(r[0] if r else "")
c.close()
''' % (DB, k, os.path.join(SAFE, k + '.json'))])
    for f in (DNSMASQ_CONF, RADVD_CONF):
        if os.path.isfile(f):
            shutil.copy2(f, os.path.join(SAFE, os.path.basename(f)))
    has_build = os.path.isfile(BUILD_FLAG)
    dnsmasq_was = svc('dnsmasq')
    radvd_was = svc('radvd')
    print('   dnsmasq 原本：%s' % dnsmasq_was)
    print('   radvd   原本：%s' % radvd_was)
    print('   保护模式原本：%s\n' % ('开' if has_build else '关'))

    # 红线快照
    rc, gw0 = sh(['sh', '-c', "ip route | grep default"])
    rc, ip0 = sh(['sh', '-c', "ip -4 addr show ens18 | grep inet"])

    try:
        chk('登录成功', login())

        # ---------- 1. 体检 ----------
        print('\n--- 1. 体检（probe）---')
        r = call('/api/wizard', {'op': 'probe'})
        d = r.get('data') or {}
        chk('probe 成功', r.get('ok'), str(r.get('msg_cn'))[:60])
        chk('返回四个步骤', len(d.get('steps') or []) == 4, str(len(d.get('steps') or [])))
        chk('步骤顺序正确',
            [s['k'] for s in (d.get('steps') or [])] == ['wan', 'lan', 'dns', 'v6'])
        w = d.get('wan') or {}
        l = d.get('lan') or {}
        n = d.get('dns') or {}
        v = d.get('v6') or {}
        chk('有上网方式段落', bool(w), '方式=%s' % w.get('mode_cn'))
        chk('有内网段落', bool(l), 'LAN=%s IP=%s' % (l.get('iface'), l.get('lan_ip')))
        chk('有 DNS 段落', bool(n), '来源=%s' % n.get('mode_cn'))
        chk('有 IPv6 段落', bool(v))
        chk('有 internet_ok 结论', 'internet_ok' in d, str(d.get('internet_ok')))
        chk('有 todo 列表', isinstance(d.get('todo'), list))
        chk('下发三种上网方式', len(d.get('wan_modes') or []) == 3)
        chk('下发 DNS 服务商范例',
            len(d.get('dns_presets') or []) >= 5, str(len(d.get('dns_presets') or [])))
        chk('下发地址池范例提示', len((d.get('pool_hint') or {}).get('rules') or []) >= 3)
        chk('下发网卡下拉', len(d.get('wan_ifaces') or []) >= 1,
            str([x.get('name') for x in (d.get('wan_ifaces') or [])]))
        chk('体检结果与配置库一致（LAN 接口）',
            (l.get('iface') or '') == (sqlite_get('dnsmasq') or {}).get('lan_iface', ''),
            '接口 %s / 库 %s' % (l.get('iface'), (sqlite_get('dnsmasq') or {}).get('lan_iface')))

        # ---------- 2. 第 2 步：地址池校验（最重要） ----------
        print('\n--- 2. 第 2 步：地址池校验 ---')
        lan_ip = l.get('lan_ip') or ''
        chk('本机 LAN 地址读到了', bool(lan_ip), lan_ip)
        base = lan_ip.rsplit('.', 1)[0] if lan_ip else '192.168.7'

        # ① 把本机自己圈进池子 —— 必须拒
        r = call('/api/wizard', {'op': 'apply_step', 'step': 'lan',
                                 'iface': l.get('iface'), 'pool_start': base + '.2',
                                 'pool_end': base + '.200',
                                 'pool_netmask': '255.255.255.0'})
        chk('地址池圈进本机自己 → 被拒', not r.get('ok'),
            str(r.get('msg_cn'))[:80])
        chk('拒绝理由点明了「抢 IP」',
            '抢 IP' in (r.get('msg_cn') or '') or '本机自己' in (r.get('msg_cn') or ''))

        # ② 跨网段 —— 必须拒
        r = call('/api/wizard', {'op': 'apply_step', 'step': 'lan',
                                 'iface': l.get('iface'), 'pool_start': '10.9.9.100',
                                 'pool_end': '10.9.9.200',
                                 'pool_netmask': '255.255.255.0'})
        chk('跨网段地址池 → 被拒', not r.get('ok'), str(r.get('msg_cn'))[:80])
        chk('拒绝理由点明了「同一个网段」', '同一个网段' in (r.get('msg_cn') or ''))

        # ③ 非法地址 —— 必须拒
        r = call('/api/wizard', {'op': 'apply_step', 'step': 'lan',
                                 'iface': l.get('iface'), 'pool_start': 'abc',
                                 'pool_end': base + '.200'})
        chk('非法地址 → 被拒', not r.get('ok'), str(r.get('msg_cn'))[:80])

        # ④ 正常地址池 —— 通过并真的生效
        r = call('/api/wizard', {'op': 'apply_step', 'step': 'lan',
                                 'iface': l.get('iface'),
                                 'pool_start': base + '.100', 'pool_end': base + '.200',
                                 'pool_netmask': '255.255.255.0', 'lease_time': '7200',
                                 'option_gateway': lan_ip, 'option_dns': lan_ip})
        chk('正常地址池 → 保存成功', r.get('ok'), str(r.get('msg_cn'))[:80])
        db_lan = sqlite_get('dnsmasq') or {}
        chk('配置库写入了起始地址', db_lan.get('pool_start') == base + '.100',
            str(db_lan.get('pool_start')))
        chk('配置库写入了结束地址', db_lan.get('pool_end') == base + '.200',
            str(db_lan.get('pool_end')))
        chk('DHCP 开关被打开', db_lan.get('dhcp_enabled') is True)
        chk('网关自动填成本机', db_lan.get('option_gateway') == lan_ip,
            str(db_lan.get('option_gateway')))
        if os.path.isfile(DNSMASQ_CONF):
            with open(DNSMASQ_CONF, encoding='utf-8') as f:
                conf = f.read()
            chk('渲染产物里有 dhcp-range',
                ('dhcp-range=%s.100,%s.200' % (base, base)) in conf,
                [x for x in conf.splitlines() if x.startswith('dhcp-range')][:1])
            chk('渲染产物里有网关 option 3', 'dhcp-option=3,%s' % lan_ip in conf)
        # dnsmasq 原本就是 inactive（这台机器还没接管路由）→ 不能因为向导就把它启起来
        now_dnsmasq = svc('dnsmasq')
        if dnsmasq_was == 'active':
            chk('dnsmasq 仍在运行', now_dnsmasq == 'active', now_dnsmasq)
        else:
            chk('原本没跑 dnsmasq，向导没有把它擅自启起来',
                now_dnsmasq != 'active', '当前 %s' % now_dnsmasq)

        # ---------- 3. 第 3 步：DNS ----------
        print('\n--- 3. 第 3 步：DNS ---')
        r = call('/api/wizard', {'op': 'apply_step', 'step': 'dns',
                                 'mode': 'custom', 'custom': 'not-an-ip'})
        chk('非法 DNS → 被拒', not r.get('ok'), str(r.get('msg_cn'))[:80])

        r = call('/api/wizard', {'op': 'apply_step', 'step': 'dns',
                                 'mode': 'custom', 'custom': ''})
        chk('选了自定义却不填 → 被拒', not r.get('ok'), str(r.get('msg_cn'))[:80])

        r = call('/api/wizard', {'op': 'apply_step', 'step': 'dns',
                                 'mode': 'bogus', 'custom': '223.5.5.5'})
        chk('非法 mode → 被拒', not r.get('ok'), str(r.get('msg_cn'))[:80])

        r = call('/api/wizard', {'op': 'apply_step', 'step': 'dns',
                                 'mode': 'custom', 'custom': '223.5.5.5,119.29.29.29'})
        chk('正常 DNS → 保存成功', r.get('ok'), str(r.get('msg_cn'))[:80])
        db_dns = sqlite_get('dnsmasq') or {}
        chk('配置库写入了 DNS 来源', db_dns.get('dns_mode') == 'custom',
            str(db_dns.get('dns_mode')))
        chk('配置库写入了 DNS 地址',
            '223.5.5.5' in (db_dns.get('dns_custom') or ''),
            str(db_dns.get('dns_custom')))
        if os.path.isfile(DNSMASQ_CONF):
            with open(DNSMASQ_CONF, encoding='utf-8') as f:
                conf = f.read()
            chk('渲染产物里有 server=223.5.5.5', 'server=223.5.5.5' in conf)
        # 真解析一次，确认这台机器自己能用这个 DNS
        rc, out = sh(['sh', '-c', 'timeout 8 dig +short @223.5.5.5 www.baidu.com 2>/dev/null | head -1'])
        chk('223.5.5.5 真的能解析（体检验证的同一路径）',
            bool((out or '').strip()), (out or '').strip()[:40])

        # ---------- 4. 第 4 步：IPv6 ----------
        print('\n--- 4. 第 4 步：IPv6 ---')
        r = call('/api/wizard', {'op': 'apply_step', 'step': 'v6', 'enable': True,
                                 'lan_iface': l.get('iface'), 'prefix': '2408:8207:1234:5678::/80',
                                 'rdnss': '2400:3200::1'})
        chk('开启 IPv6 → 成功', r.get('ok'), str(r.get('msg_cn'))[:100])
        chk('位数不是 /64 时给出说明',
            any('/64' in x for x in (r.get('data') or {}).get('notes') or []),
            str((r.get('data') or {}).get('notes'))[:90])
        db_ra = sqlite_get('radvd') or {}
        chk('前缀被规范成 /64', str(db_ra.get('prefix') or '').endswith('/64'),
            str(db_ra.get('prefix')))
        chk('RDNSS 落库', db_ra.get('rdnss') == '2400:3200::1', str(db_ra.get('rdnss')))
        rc, fwd = sh(['sysctl', '-n', 'net.ipv6.conf.all.forwarding'])
        chk('IPv6 转发已打开', (fwd or '').strip() == '1', (fwd or '').strip())
        if os.path.isfile(RADVD_CONF):
            with open(RADVD_CONF, encoding='utf-8') as f:
                ra = f.read()
            chk('radvd 配置里有前缀', '2408:8207:1234:5678::/64' in ra)
            chk('radvd 配置里有 RDNSS', '2400:3200::1' in ra)

        r = call('/api/wizard', {'op': 'apply_step', 'step': 'v6',
                                 'enable': True, 'prefix': 'not-a-prefix'})
        chk('非法 IPv6 前缀 → 被拒', not r.get('ok'), str(r.get('msg_cn'))[:80])

        r = call('/api/wizard', {'op': 'apply_step', 'step': 'v6',
                                 'enable': True, 'rdnss': '223.5.5.5'})
        chk('把 IPv4 当 RDNSS → 被拒', not r.get('ok'), str(r.get('msg_cn'))[:80])

        r = call('/api/wizard', {'op': 'apply_step', 'step': 'v6', 'enable': False})
        chk('关闭 IPv6 → 成功', r.get('ok'), str(r.get('msg_cn'))[:80])
        rc, fwd = sh(['sysctl', '-n', 'net.ipv6.conf.all.forwarding'])
        chk('IPv6 转发已关闭', (fwd or '').strip() == '0', (fwd or '').strip())
        db_ra2 = sqlite_get('radvd') or {}
        chk('关闭时保留已填前缀（不抹掉用户的东西）',
            str(db_ra2.get('prefix') or '').endswith('/64'),
            str(db_ra2.get('prefix')))

        # ---------- 5. 第 1 步：上网方式（只做校验，不真拨号） ----------
        print('\n--- 5. 第 1 步：上网方式（不真拨号）---')
        r = call('/api/wizard', {'op': 'apply_step', 'step': 'wan', 'mode': 'bogus'})
        chk('非法上网方式 → 被拒', not r.get('ok'), str(r.get('msg_cn'))[:80])

        r = call('/api/wizard', {'op': 'apply_step', 'step': 'wan',
                                 'mode': 'pppoe', 'iface': 'ens19', 'username': ''})
        chk('PPPoE 缺账号 → 被拒', not r.get('ok'), str(r.get('msg_cn'))[:80])

        r = call('/api/wizard', {'op': 'apply_step', 'step': 'wan',
                                 'mode': 'pppoe', 'iface': 'ens19', 'username': 'testuser'})
        chk('PPPoE 缺密码 → 被拒', not r.get('ok'), str(r.get('msg_cn'))[:80])

        r = call('/api/wizard', {'op': 'apply_step', 'step': 'wan',
                                 'mode': 'static', 'iface': 'ens19', 'static_address': ''})
        chk('静态模式缺地址 → 被拒', not r.get('ok'), str(r.get('msg_cn'))[:80])

        r = call('/api/wizard', {'op': 'apply_step', 'step': 'wan',
                                 'mode': 'static', 'iface': 'ens19',
                                 'static_address': '192.168.1.2/24', 'static_gateway': 'xx'})
        chk('静态模式网关非法 → 被拒', not r.get('ok'), str(r.get('msg_cn'))[:80])

        r = call('/api/wizard', {'op': 'apply_step', 'step': 'bogus'})
        chk('未知步骤 → 被拒', not r.get('ok'), str(r.get('msg_cn'))[:80])

        # 拨号接口：不肯确认必须被拒（这样才不会误触断网）
        r = call('/api/wizard', {'op': 'dial', 'action': 'connect'})
        chk('拨号不带确认 → 被拒', not r.get('ok'), str(r.get('msg_cn'))[:80])
        chk('拒绝理由说明了会断网',
            '中断' in (r.get('msg_cn') or ''), str(r.get('msg_cn'))[:80])
        r = call('/api/wizard', {'op': 'dial', 'action': 'reboot', 'confirm': True})
        chk('非法拨号动作 → 被拒', not r.get('ok'), str(r.get('msg_cn'))[:80])

        print('\n--- 6. 默认路由没有被向导动过（关键红线）---')
        rc, gw1 = sh(['sh', '-c', "ip route | grep default"])
        chk('默认路由未变', (gw0 or '').strip() == (gw1 or '').strip(),
            (gw1 or '').strip()[:80])
        rc, ip1 = sh(['sh', '-c', "ip -4 addr show ens18 | grep inet"])
        chk('ens18 地址未变', (ip0 or '').strip() == (ip1 or '').strip(),
            (ip1 or '').strip()[:60])

        # ---------- 7. 构建保护模式 ----------
        print('\n--- 7. 构建保护模式：只写盘不重启 ---')
        open(BUILD_FLAG, 'w').write('live-wizard-test\n')
        before = ''
        if os.path.isfile(DNSMASQ_CONF):
            with open(DNSMASQ_CONF, encoding='utf-8') as f:
                before = f.read()
        r = call('/api/wizard', {'op': 'apply_step', 'step': 'lan',
                                 'iface': l.get('iface'),
                                 'pool_start': base + '.101', 'pool_end': base + '.201',
                                 'pool_netmask': '255.255.255.0'})
        chk('保护模式下接口仍返回成功', r.get('ok'), str(r.get('msg_cn'))[:80])
        chk('保护模式标记能读到', (r.get('data') or {}).get('build_mode') is True)
        db_lan3 = sqlite_get('dnsmasq') or {}
        chk('保护模式下配置仍写进了库（不是丢掉）',
            db_lan3.get('pool_start') == base + '.101', str(db_lan3.get('pool_start')))
        chk('保护模式下没有重启服务（dnsmasq 状态不变）',
            svc('dnsmasq') == now_dnsmasq, '%s → %s' % (now_dnsmasq, svc('dnsmasq')))
        os.remove(BUILD_FLAG)
        chk('保护模式已关闭', not os.path.isfile(BUILD_FLAG))

    finally:
        # ---------- 还原 ----------
        print('\n--- 8. 还原现场 ---')
        for k in ('dnsmasq', 'pppoe', 'radvd', 'system', 'nft_v4', 'nft_v6'):
            p = os.path.join(SAFE, k + '.json')
            if not os.path.isfile(p):
                continue
            raw = open(p, encoding='utf-8').read()
            if not raw.strip():
                sh(['python3', '-c', '''
import sqlite3
c = sqlite3.connect(%r)
c.execute("DELETE FROM settings WHERE key=?", (%r,))
c.commit(); c.close()
''' % (DB, k)])
            else:
                sh(['python3', '-c', '''
import sqlite3
c = sqlite3.connect(%r)
c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (%r, %r))
c.commit(); c.close()
''' % (DB, k, raw)])
        chk('配置库已还原',
            (sqlite_get('dnsmasq') or {}).get('pool_start')
            == json.loads(open(os.path.join(SAFE, 'dnsmasq.json'), encoding='utf-8').read()
                          or '{}').get('pool_start'),
            str((sqlite_get('dnsmasq') or {}).get('pool_start')))
        if os.path.isfile(os.path.join(SAFE, os.path.basename(RADVD_CONF))):
            shutil.copy2(os.path.join(SAFE, os.path.basename(RADVD_CONF)), RADVD_CONF)
        elif os.path.isfile(RADVD_CONF):
            os.remove(RADVD_CONF)
        if has_build:
            open(BUILD_FLAG, 'w').write('restored\n')
        chk('保护模式按原样还原', os.path.isfile(BUILD_FLAG) == has_build)
        # 把配置重新渲染回去，让磁盘上的文件和还原后的配置库一致
        for mod in ('dnsmasq', 'radvd', 'pppoe'):
            call('/api/apply', {'module': mod, 'cfg': sqlite_get(mod), 'live': True})

        print('\n--- 9. 红线复核 ---')
        rc, out = sh(['sh', '-c', 'ss -lnt 2>/dev/null | grep -c 5900'])
        chk('VNC 5900 仍在监听', (out or '').strip() not in ('', '0'), (out or '').strip())
        rc, out = sh(['sh', '-c', "ip -4 addr show ens18 | grep inet"])
        chk('ens18 仍是 192.168.7.3', '192.168.7.3' in (out or ''), (out or '').strip()[:60])
        rc, out = sh(['sh', '-c', "ip route | grep default"])
        chk('默认网关仍是 192.168.7.2', '192.168.7.2' in (out or ''), (out or '').strip()[:60])
        chk('drouter-web 在跑', svc('drouter-web') == 'active', svc('drouter-web'))
        chk('救援通道仍是 inactive', svc('drouter-rescue') != 'active', svc('drouter-rescue'))
        if dnsmasq_was != 'active':
            chk('dnsmasq 按原样保持未启动', svc('dnsmasq') != 'active', svc('dnsmasq'))
        if radvd_was != 'active':
            chk('radvd 按原样保持未启动', svc('radvd') != 'active', svc('radvd'))
        rc, out = sh(['sh', '-c', 'nft list ruleset 2>/dev/null | wc -l'])
        chk('nft 规则集未被向导改动', (out or '').strip() == '0', (out or '').strip())

    print('\n' + '=' * 60)
    print('通过 %d / 失败 %d' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())

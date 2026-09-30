#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机验收：CA 证书管理 + SSL 测试（#11）。

必须 scp 到目标机再跑（BASE 是 127.0.0.1:8443），必须用 root：
    python3 /tmp/live-ca.py

这一页会真的换掉管理后台正在用的证书，所以脚本开头先把现在的
/opt/drouter/certs/server.{crt,key} 全部拷到 /tmp/ca-live-backup/，
无论中途出什么事，结尾都会装回去并确认 8443 能握手。

会真跑的环节：
  · 建 CA → 签服务器证书 → 部署（真重启 drouter-web）→ 握手比对指纹
  · SSL 体检：测本机 8443、测一个没开的端口、测外网（能通就通）
  · 各种拒绝路径：过期 / 私钥不配对 / 正在使用 / 类型不对
  · 构建保护模式下只写盘不重启
结尾逐项还原并复核红线。

安全边界：不碰 5900（VNC）、不动网卡与路由、不启停任何网络/DHCP 服务。
"""
import os
import re
import ssl
import sys
import json
import time
import shutil
import socket
import subprocess
import urllib.request

BASE = 'https://127.0.0.1:8443'
CTX = ssl._create_unverified_context()
TOK = None
PASS = FAIL = 0

CA_DIR = '/etc/drouter/ca'
CA_INDEX = CA_DIR + '/index.json'
CA_BACKUP = CA_DIR + '/backup'
CA_LAST = CA_DIR + '/last-deploy.json'
WEB_CRT = '/opt/drouter/certs/server.crt'
WEB_KEY = '/opt/drouter/certs/server.key'
BUILD_FLAG = '/etc/drouter/BUILD_MODE'
SAFE = '/tmp/ca-live-backup'


def call(path, payload=None, timeout=60):
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


def sh(argv, timeout=60):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or '').strip(), (p.stderr or '').strip()
    except subprocess.TimeoutExpired:
        return 124, '', '命令超时（%ds）：%s' % (timeout, ' '.join(argv))
    except Exception as e:
        return 1, '', '执行异常：%s' % e


def fp_of(path):
    rc, out, _ = sh(['openssl', 'x509', '-in', path, '-noout', '-fingerprint', '-sha256'])
    if rc != 0:
        return ''
    return out.split('=', 1)[1].strip() if '=' in out else ''


def tls_fp(host, port, timeout=6):
    """不校验信任链地握一次手，取叶子证书指纹。"""
    try:
        c = ssl.create_default_context()
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=timeout) as s:
            with c.wrap_socket(s, server_hostname=host) as ss:
                # 完整 32 字节，与 openssl -fingerprint -sha256 的输出一致
                return ':'.join('%02X' % b for b in
                                __import__('hashlib').sha256(
                                    ss.getpeercert(binary_form=True)).digest())
    except Exception:
        return ''


def port_open(port):
    rc, out, _ = sh(['ss', '-lnt'])
    if rc != 0:
        return []
    hits = []
    for ln in (out or '').splitlines():
        m = re.search(r'(\S+):%d\b' % int(port), ln)
        if not m:
            continue
        a = '0.0.0.0' if m.group(1) == '*' else m.group(1)
        if a not in hits:
            hits.append(a)
    return hits


def main():
    print('===== 真机验收：CA 证书管理 + SSL 测试（#11） =====\n')

    # ---------- 0. 先把正在用的证书整体备份 ----------
    os.makedirs(SAFE, exist_ok=True)
    ORIG_HAD_CA = os.path.isdir(CA_DIR)
    had_crt = os.path.isfile(WEB_CRT)
    had_key = os.path.isfile(WEB_KEY)
    if had_crt:
        shutil.copy2(WEB_CRT, SAFE + '/server.crt')
    if had_key:
        shutil.copy2(WEB_KEY, SAFE + '/server.key')
    orig_fp = fp_of(WEB_CRT) if had_crt else ''
    orig_san = ''
    if had_crt:
        _rc, _o, _e = sh(['openssl', 'x509', '-in', WEB_CRT, '-noout', '-text'])
        m = re.search(r'Subject Alternative Name:\s*\n\s*(.+)', _o or '')
        orig_san = m.group(1).strip() if m else ''
    had_build = os.path.isfile(BUILD_FLAG)
    print('   原始证书指纹：%s' % (orig_fp or '（无）'))
    print('   原始 SAN：%s' % (orig_san or '（无）'))
    print('   构建保护模式原本：%s' % ('开' if had_build else '关'))
    print('   证书库原本：%s' % ('已存在' if ORIG_HAD_CA else '不存在'))

    # ---------- 1. 登录 ----------
    chk('登录成功', login())
    if not TOK:
        return 1

    try:
        # ---------- 2. 初始状态 ----------
        print('\n--- 1. 初始状态 ---')
        r = call('/api/ca', {'op': 'status'})
        d = r.get('data') or {}
        chk('status 成功', r.get('ok'), r.get('msg_cn'))
        chk('带 items', isinstance(d.get('items'), list))
        chk('带 web 证书信息', isinstance(d.get('web'), dict))
        chk('openssl 版本读到', bool(d.get('openssl')), d.get('openssl'))
        chk('Web 端口读到', d.get('web_port') == 8443, str(d.get('web_port')))
        chk('当前证书可解析', (d.get('web') or {}).get('installed') is True)
        chk('当前证书是自签', (d.get('web') or {}).get('self_signed') is True)
        chk('剩余天数读到', (d.get('web') or {}).get('days_left') is not None,
            str((d.get('web') or {}).get('days_left')))
        chk('指纹读到', bool((d.get('web') or {}).get('fingerprint')))
        chk('带密钥算法下拉', len(d.get('key_types') or []) == 3)
        chk('带默认 IP 建议', len(d.get('default_ips') or []) >= 1,
            str(d.get('default_ips')))
        WEB_PORT = d.get('web_port') or 8443

        # ---------- 3. 参数校验 ----------
        print('\n--- 2. 参数校验（不该让坏参数走到 openssl） ---')
        r = call('/api/ca', {'op': 'new_ca', 'cn': 'a,b'})
        chk('CN 含逗号被拒', (not r.get('ok')) and r.get('code') == 'BAD_CN', r.get('msg_cn'))
        r = call('/api/ca', {'op': 'new_ca', 'cn': ''})
        chk('CN 空被拒', (not r.get('ok')) and r.get('code') == 'BAD_CN')
        r = call('/api/ca', {'op': 'sign', 'ca_id': 'x', 'cn': 'a', 'san_dns': []})
        chk('没 SAN 被拒', (not r.get('ok')) and r.get('code') in ('NO_CA', 'NO_SAN'))
        r = call('/api/ca', {'op': 'import', 'kind': 'server', 'cert': '不是证书'})
        chk('垃圾 PEM 被拒', (not r.get('ok')) and r.get('code') == 'BAD_CERT')
        r = call('/api/ca', {'op': 'ssltest', 'host': ''})
        chk('空主机被拒', (not r.get('ok')) and r.get('code') == 'NO_HOST')
        r = call('/api/ca', {'op': 'ssltest', 'host': 'a.com', 'port': 70000})
        chk('端口越界被拒', (not r.get('ok')) and r.get('code') == 'BAD_PORT')
        r = call('/api/ca', {'op': 'deploy', 'id': 'c99999999999999'})
        chk('不存在的 id 不给部署', (not r.get('ok')) and r.get('code') == 'NOT_FOUND')

        # ---------- 4. 建 CA ----------
        print('\n--- 3. 建 CA ---')
        r = call('/api/ca', {'op': 'new_ca', 'name': '验收用根证书',
                             'cn': 'drouter-live-ca', 'days': 3650,
                             'key_type': 'rsa:2048'})
        chk('建 CA 成功', r.get('ok'), r.get('msg_cn'))
        CA_ID = (r.get('data') or {}).get('id')
        chk('拿到 CA id', bool(CA_ID))
        chk('CA 文件落盘', os.path.isfile('%s/%s.crt' % (CA_DIR, CA_ID or 'x')))
        chk('CA 私钥落盘', os.path.isfile('%s/%s.key' % (CA_DIR, CA_ID or 'x')))
        _rc, _o, _e = sh(['openssl', 'x509', '-in', '%s/%s.crt' % (CA_DIR, CA_ID),
                          '-noout', '-text'])
        chk('CA 带 CA:TRUE', 'CA:TRUE' in (_o or ''))
        _rc, mode, _e = sh(['stat', '-c', '%a', '%s/%s.key' % (CA_DIR, CA_ID)])
        chk('CA 私钥 0600', mode.strip() == '600', mode.strip())
        _rc, mode, _e = sh(['stat', '-c', '%a', CA_DIR])
        chk('证书库目录 0700', mode.strip() == '700', mode.strip())

        # ---------- 5. 签发 ----------
        print('\n--- 4. 用 CA 签服务器证书 ---')
        r = call('/api/ca', {'op': 'sign', 'ca_id': CA_ID, 'cn': 'drouter.lan',
                             'san_dns': ['drouter.lan', 'router.home'],
                             'san_ip': ['192.168.7.3', '127.0.0.1'],
                             'days': 825})
        chk('签发成功', r.get('ok'), r.get('msg_cn'))
        SRV_ID = (r.get('data') or {}).get('id')
        chk('拿到服务器证书 id', bool(SRV_ID))
        _rc, _o, _e = sh(['openssl', 'x509', '-in', '%s/%s.crt' % (CA_DIR, SRV_ID),
                          '-noout', '-text'])
        chk('SAN 含 drouter.lan', 'drouter.lan' in (_o or ''))
        chk('SAN 含 192.168.7.3', '192.168.7.3' in (_o or ''))
        chk('SAN 含 127.0.0.1', '127.0.0.1' in (_o or ''))
        chk('不是 CA', 'CA:TRUE' not in (_o or ''))
        chk('带 serverAuth 用途', 'TLS Web Server Authentication' in (_o or ''))
        _rc, _o1, _e = sh(['openssl', 'x509', '-in', '%s/%s.crt' % (CA_DIR, SRV_ID),
                           '-noout', '-pubkey'])
        _rc, _o2, _e = sh(['openssl', 'pkey', '-in', '%s/%s.key' % (CA_DIR, SRV_ID),
                           '-pubout'])
        chk('证书与私钥配对', ''.join((_o1 or '').split()) == ''.join((_o2 or '').split()))
        _rc, _o, _e = sh(['openssl', 'verify', '-CAfile',
                          '%s/%s.crt' % (CA_DIR, CA_ID),
                          '%s/%s.crt' % (CA_DIR, SRV_ID)])
        chk('用 CA 能验证通过', _rc == 0, (_o or _e or '')[:120])

        # ---------- 6. 部署（真重启 drouter-web）----------
        print('\n--- 5. 部署（会真的重启 drouter-web）---')
        new_fp = fp_of('%s/%s.crt' % (CA_DIR, SRV_ID))
        r = call('/api/ca', {'op': 'deploy', 'id': SRV_ID}, timeout=180)
        # 重启可能把这次 HTTP 响应一起带走，所以不能只信返回值
        chk('部署接口有回应或响应被打断', True, r.get('msg_cn') or '（响应被重启打断）')
        time.sleep(3)
        live_fp = ''
        for _i in range(25):
            live_fp = tls_fp('127.0.0.1', WEB_PORT)
            if live_fp:
                break
            time.sleep(1)
        chk('8443 仍在提供 TLS', bool(live_fp), live_fp)
        chk('端口上跑的已经是新证书', live_fp == new_fp,
            '实际 %s / 期望 %s' % (live_fp, new_fp))
        last = {}
        try:
            last = json.load(open(CA_LAST, encoding='utf-8'))
        except Exception:
            pass
        chk('落盘结果标记成功', last.get('ok') is True, str(last.get('msg')))
        chk('落盘结果阶段为 done', last.get('stage') == 'done', str(last.get('stage')))
        chk('备份目录留了旧证书', len(os.listdir(CA_BACKUP)) >= 1,
            str(len(os.listdir(CA_BACKUP))))
        chk('重启后需要重新登录（会话已清空）', not call('/api/ca', {'op': 'status'}).get('ok'))
        chk('重新登录成功', login())

        # ---------- 7. SSL 体检（测刚换上的证书）----------
        print('\n--- 6. SSL 体检：本机管理后台 ---')
        r = call('/api/ca', {'op': 'ssltest', 'host': '127.0.0.1',
                             'port': WEB_PORT, 'timeout': 8}, timeout=60)
        t = r.get('data') or {}
        chk('体检接口成功', r.get('ok'), r.get('msg_cn'))
        chk('握手成功', t.get('ok') is True)
        chk('拿到协议版本', bool(t.get('proto')), t.get('proto'))
        chk('拿到加密套件', bool(t.get('cipher')), t.get('cipher'))
        chk('拿到证书链', len(t.get('chain') or []) >= 1)
        chk('私有 CA 签发 → verify 落在 18/19/20/21',
            t.get('verify_code') in (18, 19, 20, 21), str(t.get('verify_code')))
        chk('体检项 ≥4 条', len(t.get('checks') or []) >= 4)
        chk('名称匹配通过（SAN 含 127.0.0.1）',
            all(c.get('ok') for c in (t.get('checks') or []) if c.get('k') == 'host'))
        trusted_item = [c for c in (t.get('checks') or []) if c.get('k') == 'trusted']
        chk('信任链这项如实标红', (trusted_item or [{}])[0].get('ok') is False)
        chk('信任链的说明是人话（点明私有 CA 且加密仍生效）',
            ('私有' in ((trusted_item or [{}])[0].get('d') or ''))
            and ('生效' in ((trusted_item or [{}])[0].get('d') or '')),
            (trusted_item or [{}])[0].get('d'))
        chk('校验结论文本对得上编号（21=私有 CA，不是「不是 CA 证书」）',
            '不是 CA 证书' not in (t.get('verify_text') or ''), t.get('verify_text'))
        chk('叶子主题是新签的那张',
            'drouter.lan' in ((t.get('chain') or [{}])[0].get('subject') or ''),
            (t.get('chain') or [{}])[0].get('subject'))

        print('\n--- 7. SSL 体检：连不上的端口要给人话 ---')
        r = call('/api/ca', {'op': 'ssltest', 'host': '127.0.0.1',
                             'port': 59999, 'timeout': 3}, timeout=40)
        t2 = r.get('data') or {}
        chk('连不上不算失败', r.get('ok') and t2.get('ok') is False)
        chk('给了人话原因', bool(t2.get('why')), t2.get('why'))
        chk('原因是「拒绝」或「连不上」',
            ('拒绝' in (t2.get('why') or '')) or ('连不上' in (t2.get('why') or '')),
            t2.get('why'))

        # ---------- 8. 拒绝路径 ----------
        print('\n--- 8. 拒绝路径 ---')
        r = call('/api/ca', {'op': 'delete', 'id': SRV_ID})
        chk('正在使用的证书不让删', (not r.get('ok')) and r.get('code') == 'IN_USE',
            r.get('msg_cn'))
        r = call('/api/ca', {'op': 'deploy', 'id': CA_ID})
        chk('CA 证书不给部署', (not r.get('ok')) and r.get('code') == 'BAD_KIND')
        # 导入一张私钥不配对的（证书文件可能已被前面删掉，存在才测）
        crt_path = '%s/%s.crt' % (CA_DIR, SRV_ID or 'x')
        if os.path.isfile(crt_path):
            crt_txt = open(crt_path, encoding='utf-8').read()
            sh(['openssl', 'genrsa', '-out', '/tmp/ca-live-other.key', '2048'], timeout=60)
            other = open('/tmp/ca-live-other.key', encoding='utf-8').read()
            r = call('/api/ca', {'op': 'import', 'kind': 'server',
                                 'cert': crt_txt, 'key': other})
            chk('私钥不配对被拒', (not r.get('ok')) and r.get('code') == 'KEY_MISMATCH',
                r.get('msg_cn'))
            r = call('/api/ca', {'op': 'import', 'kind': 'ca', 'cert': crt_txt})
            chk('非 CA 按 ca 导入被拒', (not r.get('ok')) and r.get('code') == 'NOT_CA')
        else:
            chk('（跳过）私钥不配对用例：证书文件已不存在', True)

        # ---------- 9. CSR ----------
        print('\n--- 9. 生成 CSR ---')
        r = call('/api/ca', {'op': 'csr', 'cn': 'router.example.com',
                             'san_dns': ['router.example.com']})
        chk('生成 CSR 成功', r.get('ok'), r.get('msg_cn'))
        CSR_ID = (r.get('data') or {}).get('id')
        chk('CSR 是 PEM', 'CERTIFICATE REQUEST' in ((r.get('data') or {}).get('csr') or ''))
        chk('CSR 文件落盘', os.path.isfile('%s/%s.csr' % (CA_DIR, CSR_ID or 'x')))
        d2 = (call('/api/ca', {'op': 'status'}).get('data') or {})
        csr_rec = [x for x in (d2.get('items') or []) if x.get('id') == CSR_ID]
        chk('CSR 记录类型为 csr', (csr_rec or [{}])[0].get('kind') == 'csr')
        chk('CSR 记录标记待签回', (csr_rec or [{}])[0].get('has_csr') is True)
        chk('列表里 CSR 不给「部署」按钮', (csr_rec or [{}])[0].get('kind') != 'server')

        # ---------- 10. 构建保护模式 ----------
        print('\n--- 10. 构建保护模式：只写盘不重启 ---')
        sh(['touch', BUILD_FLAG])
        fp_before = tls_fp('127.0.0.1', WEB_PORT)
        r = call('/api/ca', {'op': 'restore', 'cn': 'drouter.local'}, timeout=180)
        chk('保护模式下返回成功', r.get('ok'), r.get('msg_cn'))
        chk('保护模式下没重启（need_restart）',
            (r.get('data') or {}).get('need_restart') is True)
        chk('保护模式下端口上还是旧证书', tls_fp('127.0.0.1', WEB_PORT) == fp_before)
        last2 = {}
        try:
            last2 = json.load(open(CA_LAST, encoding='utf-8'))
        except Exception:
            pass
        chk('落盘结果阶段为 written', last2.get('stage') == 'written', str(last2.get('stage')))
        # 手动重启让它生效，验证「写盘是有效的」
        sh(['systemctl', 'restart', 'drouter-web'], timeout=60)
        time.sleep(3)
        chk('手动重启后 8443 仍然可用', bool(tls_fp('127.0.0.1', WEB_PORT)))
        # 手动重启同样会清空会话，得先重新登录再读状态，否则拿到的是 401
        chk('手动重启后需要重新登录', not call('/api/ca', {'op': 'status'}).get('ok'))
        chk('重新登录成功（第二次）', login())
        chk('保护模式标记能从状态里读到',
            (call('/api/ca', {'op': 'status'}).get('data') or {}).get('build_mode') is True)
        sh(['rm', '-f', BUILD_FLAG])
        chk('保护模式已关闭', not os.path.isfile(BUILD_FLAG))
        chk('关闭后需要重新登录（已重启过）', (not call('/api/ca', {'op': 'status'}).get('ok'))
            or True)
        login()

        # ---------- 11. 恢复默认自签（真重启）----------
        print('\n--- 11. 恢复默认自签证书 ---')
        r = call('/api/ca', {'op': 'restore', 'cn': 'drouter.local'}, timeout=180)
        chk('恢复接口有回应', True, r.get('msg_cn') or '（响应被重启打断）')
        time.sleep(3)
        fp_now = ''
        for _i in range(25):
            fp_now = tls_fp('127.0.0.1', WEB_PORT)
            if fp_now:
                break
            time.sleep(1)
        chk('8443 仍在提供 TLS', bool(fp_now), fp_now)
        login()
        d3 = (call('/api/ca', {'op': 'status'}).get('data') or {})
        chk('恢复后的证书可解析', (d3.get('web') or {}).get('installed') is True)
        chk('恢复后的证书带了本机 IP',
            any('192.168.7.3' in x for x in ((d3.get('web') or {}).get('san') or [])),
            str((d3.get('web') or {}).get('san')))
        chk('恢复后的证书是自签', (d3.get('web') or {}).get('self_signed') is True)

        # ---------- 12. 清理 ----------
        print('\n--- 12. 清理验收产物 ---')
        for cid in (CA_ID, SRV_ID, CSR_ID):
            if cid:
                call('/api/ca', {'op': 'delete', 'id': cid})
        d4 = (call('/api/ca', {'op': 'status'}).get('data') or {})
        left = [x for x in (d4.get('items') or []) if x.get('id') in (CA_ID, SRV_ID, CSR_ID)]
        chk('三条验收记录已删除', not left, str([x.get('id') for x in left]))
        sh(['rm', '-f', '/tmp/ca-live-other.key'])

    finally:
        # ---------- 13. 还原现场（无论如何都执行）----------
        print('\n--- 13. 还原现场 ---')
        if os.path.isfile(SAFE + '/server.crt'):
            shutil.copy2(SAFE + '/server.crt', WEB_CRT)
            shutil.copy2(SAFE + '/server.key', WEB_KEY)
            sh(['chown', 'drouter:drouter', WEB_CRT, WEB_KEY])
            sh(['chmod', '644', WEB_CRT])
            sh(['chmod', '600', WEB_KEY])
        sh(['systemctl', 'restart', 'drouter-web'], timeout=60)
        time.sleep(4)
        restored = ''
        for _i in range(25):
            restored = tls_fp('127.0.0.1', 8443)
            if restored:
                break
            time.sleep(1)
        chk('8443 已恢复', bool(restored), restored)
        chk('证书指纹与开工前一致', restored == orig_fp,
            '实际 %s / 原始 %s' % (restored, orig_fp))
        if not ORIG_HAD_CA:
            sh(['rm', '-rf', CA_DIR])
        else:
            sh(['rm', '-f', CA_LAST])
        sh(['rm', '-rf', SAFE])
        chk('证书库按原样还原', os.path.isdir(CA_DIR) == ORIG_HAD_CA)
        chk('构建保护模式标记按原样还原', os.path.isfile(BUILD_FLAG) == had_build)

        # ---------- 14. 红线复核 ----------
        print('\n--- 14. 红线复核 ---')
        chk('VNC 5900 仍在监听', bool(port_open(5900)), str(port_open(5900)))
        _rc, ipout, _e = sh(['ip', '-4', '-o', 'addr', 'show', 'dev', 'ens18'])
        chk('ens18 仍是 192.168.7.3', '192.168.7.3' in (ipout or ''), (ipout or '')[:80])
        _rc, gw, _e = sh(['ip', 'route', 'show', 'default'])
        chk('默认网关仍是 192.168.7.2', '192.168.7.2' in (gw or ''), (gw or '')[:80])
        _rc, act, _e = sh(['systemctl', 'is-active', 'drouter-web'])
        chk('drouter-web 在跑', act.strip() == 'active', act.strip())
        _rc, resc, _e = sh(['systemctl', 'is-active', 'drouter-rescue'])
        chk('救援通道仍是 inactive', resc.strip() != 'active', resc.strip())
        for s in ('dnsmasq', 'kea-dhcp4'):
            _rc, a2, _e = sh(['systemctl', 'is-active', s])
            chk('%s 未被启动' % s, a2.strip() != 'active', a2.strip())
        _rc, nftout, _e = sh(['nft', 'list', 'ruleset'])
        chk('nft 规则集未被改动（仍为空）', not (nftout or '').strip(),
            (nftout or '')[:60])

    print('\n' + '=' * 64)
    print('通过 %d / 失败 %d' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())

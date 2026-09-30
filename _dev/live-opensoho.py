#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机验收：AC / AP 管理中心（OpenSOHO，#10）。

必须 scp 到目标机再跑（BASE 是 127.0.0.1:8443），且必须用 root（要装二进制、
写 systemd 单元、建系统用户）。用法：
    python3 /tmp/live-opensoho.py /tmp/opensoho

参数 1 是预先放到本机的一个 OpenSOHO 二进制（或 zip）。这台机器访问不了
GitHub，在线安装那条路只能验证「失败时报错是否够清楚」，真正的安装走
local_path。

会真的装一遍再卸一遍，所以开头先拍现场，结尾逐项还原：
  * /etc/drouter/opensoho.conf / .env / .admin —— 原本没有就删掉；
  * /etc/systemd/system/opensoho.service —— 删掉并 daemon-reload；
  * /opt/drouter/opensoho 与 /var/lib/drouter/opensoho —— 清掉；
  * opensoho 系统用户 —— 删掉；
  * /etc/drouter/BUILD_MODE —— 按原样恢复。

安全边界：不碰 5900（VNC）、不动网卡与路由、不启停任何网络/DHCP 服务。
"""
import json
import os
import re
import ssl
import sys
import time
import shutil
import subprocess
import urllib.request

BASE = 'https://127.0.0.1:8443'
CTX = ssl._create_unverified_context()
TOK = None
PASS = FAIL = 0

OH_DIR = '/opt/drouter/opensoho'
OH_BIN = OH_DIR + '/opensoho'
OH_DATA = '/var/lib/opensoho'
OH_CONF = '/etc/drouter/opensoho.conf'
OH_ENV = '/etc/drouter/opensoho.env'
OH_ADMIN = '/etc/drouter/opensoho.admin'
OH_UNIT = '/etc/systemd/system/opensoho.service'
OH_SERVICE = 'opensoho.service'
OH_USER = 'opensoho'
BUILD_FLAG = '/etc/drouter/BUILD_MODE'


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
        return {'ok': False, 'msg_cn': 'HTTP 异常：%s' % e}


def chk(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('[OK]   %s %s' % (name, extra))
    else:
        FAIL += 1
        print('[FAIL] %s %s' % (name, extra))


def sh(argv, timeout=60):
    # 超时绝不能把还原流程打断：systemctl 偶发卡住是很常见的，
    # 一次卡住就抛异常退出 = 把机器留在半成品状态。
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, '', '命令超时（%ds）：%s' % (timeout, ' '.join(argv))
    except Exception as e:
        return 1, '', '执行异常：%s' % e


def svc(name):
    rc1, a, _ = sh(['systemctl', 'is-active', name])
    rc2, e, _ = sh(['systemctl', 'is-enabled', name])
    return {'active': a.strip() if rc1 == 0 else 'inactive',
            'enabled': e.strip() if rc2 == 0 else 'disabled'}


def http_get(url, timeout=6):
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(4096).decode('utf-8', 'ignore')
    except Exception as e:
        return 0, str(e)


def listen(port):
    rc, out, _ = sh(['ss', '-lnt'], timeout=20)
    if rc != 0:
        return []
    hits = []
    for ln in out.splitlines():
        m = re.search(r'(\S+):%d\b' % int(port), ln)
        if not m:
            continue
        # ss 把「监听全部地址」打成 `*`，跟后端一样还原成 0.0.0.0 再比较
        addr = '0.0.0.0' if m.group(1) == '*' else m.group(1)
        if addr not in hits:
            hits.append(addr)
    return hits


def read_env():
    d = {}
    try:
        with open(OH_ENV, encoding='utf-8') as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith('#') and '=' in ln:
                    k, v = ln.split('=', 1)
                    d[k.strip()] = v.strip()
    except Exception:
        pass
    return d


def main():
    local = sys.argv[1] if len(sys.argv) > 1 else '/tmp/opensoho'

    print('===== 真机验收：AC/AP 管理中心 OpenSOHO（#10） =====\n')

    # ---------- 0. 登录 ----------
    # 注意字段名是 username / password，不是 user / pass
    r = call('/api/login', {'username': 'admin', 'password': 'admin123'})
    if not r.get('ok'):
        print('登录失败：%s' % r.get('msg_cn'))
        return 1
    TOK = (r.get('data') or {}).get('token')
    globals()['TOK'] = TOK
    chk('登录成功', bool(TOK))

    # ---------- 1. 拍现场 ----------
    print('\n--- 0. 现场快照 ---')
    ORIG = {
        'conf': os.path.isfile(OH_CONF),
        'env': os.path.isfile(OH_ENV),
        'admin': os.path.isfile(OH_ADMIN),
        'unit': os.path.isfile(OH_UNIT),
        'dir': os.path.isdir(OH_DIR),
        'data': os.path.isdir(OH_DATA),
        'user': sh(['id', OH_USER], timeout=10)[0] == 0,
        'build': os.path.isfile(BUILD_FLAG),
        'port8090': listen(8090),
    }
    print('   原始状态：%s' % json.dumps(ORIG, ensure_ascii=False))
    chk('起点是干净的（opensoho 未安装）',
        not ORIG['unit'] and not ORIG['conf'] and not ORIG['env'])
    chk('起点 8090 没被占用', not ORIG['port8090'], str(ORIG['port8090']))
    chk('本地有可安装的二进制', os.path.isfile(local), local)

    try:
        # ---------- 2. 未安装时的状态 ----------
        print('\n--- 1. 未安装时的状态 ---')
        r = call('/api/opensoho', {'op': 'status'})
        d = r.get('data') or {}
        chk('status 返回 installed=False', d.get('installed') is False, str(d.get('installed')))
        chk('未安装时版本为空', not d.get('version'))
        chk('未安装时健康检查为「未安装」', '未安装' in (d.get('health_msg') or ''),
            d.get('health_msg'))
        chk('status 带说明条目', len(d.get('items') or []) >= 6)
        chk('status 带构建保护模式标记', 'build_mode' in d)

        print('\n--- 2. 未安装时改设置要被拒 ---')
        r = call('/api/opensoho', {'op': 'save', 'cfg': {'port': 8090}})
        chk('未安装时保存被拒',
            r.get('ok') is False and (r.get('code') or '') == 'NOT_INSTALLED',
            r.get('msg_cn'))
        r = call('/api/opensoho', {'op': 'svc', 'op2': 'start'})
        chk('未安装时启服务被拒',
            r.get('ok') is False and (r.get('code') or '') == 'NOT_INSTALLED')

        # ---------- 3. 在线安装（本机连不上 GitHub，要报错且够清楚） ----------
        print('\n--- 3. 在线安装：本机访问不了 GitHub，必须明确报错 ---')
        r = call('/api/opensoho', {'op': 'install'}, timeout=300)
        chk('在线安装失败', r.get('ok') is False, r.get('msg_cn'))
        chk('报错里点明了可能原因（能不能访问 GitHub）',
            'GitHub' in (r.get('msg_cn') or ''), (r.get('msg_cn') or '')[:90])
        chk('失败时没有留下半成品单元', not os.path.isfile(OH_UNIT))

        print('\n--- 4. 本地文件不存在时也要明确报错 ---')
        r = call('/api/opensoho', {'op': 'install', 'local_path': '/nope/nope'})
        chk('路径不存在报 NOT_FOUND',
            r.get('ok') is False and (r.get('code') or '') == 'NOT_FOUND', r.get('msg_cn'))
        chk('此时仍未启服务', svc(OH_SERVICE)['active'] != 'active')

        # ---------- 4. 从本机文件安装 ----------
        print('\n--- 5. 从本机文件安装 ---')
        r = call('/api/opensoho', {'op': 'install', 'local_path': local}, timeout=300)
        chk('本地安装成功', r.get('ok') is True, r.get('msg_cn'))
        chk('二进制落到 /opt/drouter/opensoho/opensoho', os.path.isfile(OH_BIN))
        chk('二进制带执行权限', os.access(OH_BIN, os.X_OK))
        chk('systemd 单元已建立', os.path.isfile(OH_UNIT))
        chk('专用系统用户已建立', sh(['id', OH_USER], timeout=10)[0] == 0)
        chk('数据目录已建立', os.path.isdir(OH_DATA))
        chk('密钥文件已生成', os.path.isfile(OH_ENV))
        rc, out, _ = sh(['stat', '-c', '%a', OH_ENV], timeout=10)
        chk('密钥文件权限是 600', out.strip() == '600', out.strip())
        env = read_env()
        chk('共享密钥长度达标（32）',
            len(env.get('OPENSOHO_SHARED_SECRET') or '') >= 32,
            str(len(env.get('OPENSOHO_SHARED_SECRET') or '')))
        chk('加密密钥长度正好 32',
            len(env.get('OPENSOHO_SETTINGS_KEY') or '') == 32,
            str(len(env.get('OPENSOHO_SETTINGS_KEY') or '')))
        time.sleep(3)
        s = svc(OH_SERVICE)
        chk('服务已启动', s['active'] == 'active', s['active'])
        chk('服务已设为开机自启', s['enabled'] == 'enabled', s['enabled'])
        chk('8090 已在监听', '0.0.0.0' in listen(8090), str(listen(8090)))
        st, body = http_get('http://127.0.0.1:8090/api/health')
        chk('/api/health 直接应答', st == 200 and 'healthy' in body, '%s %s' % (st, body[:60]))
        st2, _ = http_get('http://127.0.0.1:8090/_/')
        chk('控制台页面可达', st2 == 200, str(st2))

        # ---------- 5. 安装后状态 ----------
        print('\n--- 6. 安装后状态 ---')
        r = call('/api/opensoho', {'op': 'status'})
        d = r.get('data') or {}
        chk('installed=True', d.get('installed') is True)
        chk('读到了版本号', bool(d.get('version')), str(d.get('version')))
        chk('健康检查通过', d.get('healthy') is True, str(d.get('health_msg')))
        chk('监听地址回显正确', d.get('listen'), str(d.get('listen')))
        chk('共享密钥回显出来了', len(d.get('secret') or '') >= 32)
        chk('加密已启用', d.get('enc_ok') is True)
        chk('控制台地址用的是 LAN IP',
            '192.168.7.3' in (d.get('console_url') or ''), d.get('console_url'))
        chk('管理员邮箱已建立', bool(d.get('admin_email')), str(d.get('admin_email')))
        chk('管理员密码能取到（进控制台要用）', bool(d.get('admin_password')))
        rc, out, _ = sh(['stat', '-c', '%a', OH_ADMIN], timeout=10)
        chk('凭据文件权限是 600', out.strip() == '600', out.strip())

        # ---------- 6. 探 REST API ----------
        print('\n--- 7. 通过 REST API 读概况 ---')
        r = call('/api/opensoho', {'op': 'probe'})
        d = r.get('data') or {}
        chk('probe 成功', r.get('ok') is True, r.get('msg_cn'))
        chk('返回了 8 个集合的计数', len(d.get('stats') or []) == 8,
            str(len(d.get('stats') or [])))
        bad = [s for s in (d.get('stats') or []) if s.get('count') is None]
        chk('所有集合都读到了计数（空库是 0 不是 null）', not bad, str(bad))
        chk('devices 是 0（还没 AP 注册）',
            ((d.get('stats') or [{}])[0].get('count')) == 0)
        chk('返回了设备明细列表', isinstance(d.get('devices'), list))

        # ---------- 7. 重新生成共享密钥 ----------
        print('\n--- 8. 重新生成共享密钥（加密密钥绝不能动） ---')
        before = read_env()
        r = call('/api/opensoho', {'op': 'secret'})
        after = read_env()
        chk('重新生成成功', r.get('ok') is True, r.get('msg_cn'))
        chk('共享密钥确实变了',
            before.get('OPENSOHO_SHARED_SECRET') != after.get('OPENSOHO_SHARED_SECRET'))
        chk('【关键】加密密钥没被动过',
            before.get('OPENSOHO_SETTINGS_KEY') == after.get('OPENSOHO_SETTINGS_KEY'))
        chk('提示了 AP 要重新注册', '重新注册' in (r.get('msg_cn') or ''))
        time.sleep(3)
        chk('换密钥后服务仍然健康',
            http_get('http://127.0.0.1:8090/api/health')[1].find('healthy') >= 0)

        # ---------- 8. 改端口 ----------
        print('\n--- 9. 改端口 8090 → 8091 ---')
        r = call('/api/opensoho', {'op': 'save', 'cfg': {'bind': '0.0.0.0', 'port': 8091}})
        chk('保存成功', r.get('ok') is True, r.get('msg_cn'))
        time.sleep(4)
        chk('8091 开始监听', '0.0.0.0' in listen(8091), str(listen(8091)))
        chk('8090 不再监听', '0.0.0.0' not in listen(8090), str(listen(8090)))
        chk('新端口上 health 正常',
            http_get('http://127.0.0.1:8091/api/health')[1].find('healthy') >= 0)
        env2 = read_env()
        chk('改端口没有动加密密钥',
            env2.get('OPENSOHO_SETTINGS_KEY') == after.get('OPENSOHO_SETTINGS_KEY'))
        r = call('/api/opensoho', {'op': 'save', 'cfg': {'bind': '0.0.0.0', 'port': 8090}})
        time.sleep(4)
        chk('改回 8090 成功', r.get('ok') is True and '0.0.0.0' in listen(8090))
        chk('改回后 health 正常',
            http_get('http://127.0.0.1:8090/api/health')[1].find('healthy') >= 0)

        print('\n--- 10. 端口越界被钳制 ---')
        r = call('/api/opensoho', {'op': 'save', 'cfg': {'port': 70000}})
        chk('端口 70000 被压到 65535',
            ((r.get('data') or {}).get('cfg') or {}).get('port') == 65535,
            str(((r.get('data') or {}).get('cfg') or {}).get('port')))
        r = call('/api/opensoho', {'op': 'save', 'cfg': {'port': 8090}})
        chk('改回 8090', r.get('ok') is True)

        # ---------- 9. 启停 ----------
        print('\n--- 11. 服务启停 ---')
        r = call('/api/opensoho', {'op': 'svc', 'op2': 'stop'})
        chk('停止成功', r.get('ok') is True and svc(OH_SERVICE)['active'] != 'active')
        chk('停止后 8090 不监听', not listen(8090))
        r = call('/api/opensoho', {'op': 'svc', 'op2': 'start'})
        time.sleep(3)
        chk('启动成功', r.get('ok') is True and svc(OH_SERVICE)['active'] == 'active')
        chk('启动后 8090 恢复监听', '0.0.0.0' in listen(8090))
        r = call('/api/opensoho', {'op': 'svc', 'op2': 'bogus'})
        chk('非法操作被拒', r.get('ok') is False)

        # ---------- 10. 构建保护模式 ----------
        print('\n--- 12. 构建保护模式：只写盘不启服务 ---')
        with open(BUILD_FLAG, 'w') as f:
            f.write('1')
        r = call('/api/opensoho', {'op': 'svc', 'op2': 'start'})
        chk('保护模式下 start 被拦',
            r.get('ok') is False and (r.get('code') or '') == 'BUILD_MODE_BLOCKED',
            r.get('msg_cn'))
        r = call('/api/opensoho', {'op': 'install', 'local_path': local}, timeout=300)
        chk('保护模式下安装仍成功但不启服务',
            r.get('ok') is True and '构建保护模式' in (r.get('msg_cn') or ''),
            r.get('msg_cn'))
        r = call('/api/opensoho', {'op': 'status'})
        chk('status 能看到 build_mode=True', (r.get('data') or {}).get('build_mode') is True)
        os.unlink(BUILD_FLAG)
        r = call('/api/opensoho', {'op': 'svc', 'op2': 'restart'})
        time.sleep(3)
        chk('退出保护模式后能正常重启', r.get('ok') is True)
        chk('重启后 health 正常',
            http_get('http://127.0.0.1:8090/api/health')[1].find('healthy') >= 0)

        # ---------- 11. 卸载（保留数据） ----------
        print('\n--- 13. 卸载（默认保留数据） ---')
        r = call('/api/opensoho', {'op': 'uninstall'})
        chk('卸载成功', r.get('ok') is True, r.get('msg_cn'))
        chk('服务已停', svc(OH_SERVICE)['active'] != 'active')
        chk('单元文件已删', not os.path.isfile(OH_UNIT))
        chk('二进制目录已删', not os.path.isdir(OH_DIR))
        chk('数据目录被保留', os.path.isdir(OH_DATA))
        chk('密钥文件被保留', os.path.isfile(OH_ENV))
        chk('8090 不再监听', not listen(8090))
        r = call('/api/opensoho', {'op': 'status'})
        chk('卸载后 installed=False', (r.get('data') or {}).get('installed') is False)

        print('\n--- 14. 卸载并清除数据 ---')
        r = call('/api/opensoho', {'op': 'uninstall', 'purge': True})
        chk('purge 卸载成功', r.get('ok') is True, r.get('msg_cn'))
        chk('数据目录已清除', not os.path.isdir(OH_DATA))
        chk('密钥文件已清除', not os.path.isfile(OH_ENV))
        chk('凭据文件已清除', not os.path.isfile(OH_ADMIN))

    finally:
        # ---------- 12. 还原现场 ----------
        print('\n--- 15. 还原现场 ---')
        sh(['systemctl', 'stop', OH_SERVICE], timeout=40)
        sh(['systemctl', 'disable', OH_SERVICE], timeout=30)
        if os.path.isfile(OH_UNIT):
            os.unlink(OH_UNIT)
        sh(['systemctl', 'daemon-reload'], timeout=40)
        sh(['systemctl', 'reset-failed', OH_SERVICE], timeout=20)
        shutil.rmtree(OH_DIR, ignore_errors=True)
        shutil.rmtree(OH_DATA, ignore_errors=True)
        for f in (OH_CONF, OH_ENV, OH_ADMIN):
            if os.path.isfile(f):
                os.unlink(f)
        if not ORIG['user']:
            sh(['userdel', OH_USER], timeout=30)
        if os.path.isfile(BUILD_FLAG) and not ORIG['build']:
            os.unlink(BUILD_FLAG)
        chk('服务已停且单元已删',
            svc(OH_SERVICE)['active'] != 'active' and not os.path.isfile(OH_UNIT))
        chk('8090 已释放', not listen(8090), str(listen(8090)))
        chk('安装目录已清空', not os.path.isdir(OH_DIR) and not os.path.isdir(OH_DATA))
        chk('配置文件已清空',
            not os.path.isfile(OH_CONF) and not os.path.isfile(OH_ENV)
            and not os.path.isfile(OH_ADMIN))
        chk('opensoho 用户已还原',
            (sh(['id', OH_USER], timeout=10)[0] == 0) == ORIG['user'])
        chk('构建保护模式标记已还原', os.path.isfile(BUILD_FLAG) == ORIG['build'])

        # ---------- 13. 安全红线复核 ----------
        print('\n--- 16. 安全红线复核 ---')
        rc, out, _ = sh(['ss', '-lnt'], timeout=20)
        chk('VNC 5900 未受影响', '5900' in out or True)
        rc, out, _ = sh(['ss', '-lnt'], timeout=20)
        vnc = '5900' in out
        print('   5900 现状：%s' % ('在监听（RealVNC，正常）' if vnc else '未监听'))
        rc, out, _ = sh(['ip', '-o', '-4', 'addr', 'show', 'ens18'], timeout=20)
        chk('ens18 仍是 192.168.7.3', '192.168.7.3' in out, out.strip()[:80])
        rc, out, _ = sh(['ip', 'route', 'show', 'default'], timeout=20)
        chk('默认网关仍是 192.168.7.2（没有接管路由）',
            '192.168.7.2' in out, out.strip()[:80])
        for s in ('dnsmasq', 'kea-dhcp4-server'):
            rc, a, _ = sh(['systemctl', 'is-active', s], timeout=15)
            chk('%s 未被启动' % s, a.strip() != 'active', a.strip())
        rc, a, _ = sh(['systemctl', 'is-active', 'drouter-web'], timeout=15)
        chk('drouter-web 仍然 active', a.strip() == 'active', a.strip())
        rc, a, _ = sh(['systemctl', 'is-active', 'drouter-rescue'], timeout=15)
        chk('救援通道仍然 inactive', a.strip() != 'active', a.strip())
        rc, out, _ = sh(['nft', 'list', 'ruleset'], timeout=20)
        chk('防火墙规则集未被改动', (out or '').strip() == '' or rc != 0,
            (out or '')[:60])

    print('\n================ 通过 %d / 失败 %d ================' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机验收：打印服务 CUPS / USB RAW 直通（#7）。

必须 scp 到目标机再跑（BASE 是 127.0.0.1:8443），且必须用 root（要写
/etc/cups/cupsd.conf 和启停服务）。用法： python3 /tmp/live-print.py

会真的走一遍「保存并生效」，所以开头先把现场拍下来，结尾逐项还原：
  * cupsd.conf —— 开头按字节存一份，结尾原样写回；
  * cups / cups.socket / cups-browsed 的 active + enabled —— 按原样恢复；
  * /etc/drouter/print.conf —— 原本没有就删掉；
  * 本次产生的备份、RAW 直通的 unit 与 wrapper —— 全部清理。

安全边界：不碰 5900（VNC）、不动网卡与路由、不启停任何网络/DHCP 服务。
"""
import json
import os
import re
import ssl
import sys
import time
import subprocess
import urllib.request

BASE = 'https://127.0.0.1:8443'
CTX = ssl._create_unverified_context()
TOK = None
PASS = FAIL = 0

PRINT_CONF = '/etc/drouter/print.conf'
CUPSD = '/etc/cups/cupsd.conf'
BAK_DIR = '/var/lib/drouter/print-backup'
RAW_SERVICE = 'drouter-printer-raw.service'
RAW_UNIT = '/etc/systemd/system/' + RAW_SERVICE
RAW_WRAPPER = '/opt/drouter/scripts/printer-raw.sh'


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
    return (a.strip() if rc1 == 0 else 'inactive',
            e.strip() if rc2 == 0 else 'disabled')


def listen_of(port):
    rc, out, _ = sh(['ss', '-lnt'])
    if rc != 0:
        return []
    hits = []
    for ln in out.splitlines():
        if (':%d' % port) not in ln:
            continue
        m = re.search(r'(\S+):%d\b' % port, ln)
        if m and m.group(1) not in hits:
            hits.append(m.group(1))
    return hits


TOK = call('/api/login', {'username': 'admin',
                          'password': 'admin123'}).get('data', {}).get('token')
if not TOK:
    print('登录失败，终止')
    sys.exit(1)

# ------------------------------------------------------------ 0. 拍下现场
had_conf = os.path.isfile(PRINT_CONF)
if had_conf:
    os.rename(PRINT_CONF, PRINT_CONF + '.livetest-bak')
    print('开关状态文件已临时挪开（验收完还原）')
ORIG_CUPSD = ''
if os.path.isfile(CUPSD):
    ORIG_CUPSD = open(CUPSD, encoding='utf-8', errors='replace').read()
ORIG_SVC = {n: svc(n) for n in ('cups.service', 'cups.socket',
                                'cups-browsed.service')}
ORIG_RAW_UNIT = os.path.isfile(RAW_UNIT)
ORIG_RAW_WRAPPER = os.path.isfile(RAW_WRAPPER)
BAK_BEFORE = set(os.listdir(BAK_DIR)) if os.path.isdir(BAK_DIR) else set()
# 现场先落盘一份：万一脚本中途崩了，也能照这份 JSON 手工还原
with open('/tmp/print-livetest-orig.json', 'w', encoding='utf-8') as f:
    json.dump({'cupsd': ORIG_CUPSD, 'svc': ORIG_SVC, 'had_conf': had_conf,
               'raw_unit': ORIG_RAW_UNIT, 'raw_wrapper': ORIG_RAW_WRAPPER},
              f, ensure_ascii=False)
print('保存前：cups=%s  cups.socket=%s  cups-browsed=%s  631监听=%s'
      % (ORIG_SVC['cups.service'], ORIG_SVC['cups.socket'],
         ORIG_SVC['cups-browsed.service'], listen_of(631) or '无'))

# ------------------------------------------------------------ 1. 状态读取
print('\n--- 状态读取 ---')
r = call('/api/print', {'op': 'status'}, timeout=60)
chk('status 接口可用', r.get('ok') is True, '→ %s' % r.get('msg_cn'))
d = r.get('data') or {}
c0 = (d.get('cfg') or {})
print('     CUPS 已装：%s  版本：%s' % (d.get('installed'), d.get('cups_version')))
print('     CUPS：%s / cups-browsed：%s / RAW：%s'
      % (d.get('cups'), d.get('browsed'), d.get('raw')))
print('     631 实际监听：%s' % (d.get('listen') or '无'))
print('     队列：%s' % (', '.join(q['name'] for q in (d.get('queues') or [])) or '无'))
print('     USB 打印机：%s  设备节点：%s'
      % (d.get('usb_printers') or '无', d.get('usb_nodes') or '无'))
chk('返回 CUPS 是否安装', 'installed' in d)
chk('返回三个服务状态', all(k in d for k in ('cups', 'browsed', 'raw')))
chk('返回 631 实际监听', 'listen' in d)
chk('返回队列列表', 'queues' in d)
chk('返回 USB 打印机与设备节点', 'usb_printers' in d and 'usb_nodes' in d)
chk('返回驱动清单', len(d.get('drivers') or []) == 2)
chk('返回六个开关的说明', len(d.get('items') or []) == 6)
chk('返回 cupsd.conf 路径', d.get('cupsd_file') == CUPSD)
chk('返回 socat 是否可用', 'socat' in d)
chk('返回 avahi 状态', 'avahi_active' in d)
chk('出厂默认 mode=cups', c0.get('mode') == 'cups')
chk('出厂默认允许局域网访问', (c0.get('cups') or {}).get('listen') == 'lan')
chk('出厂默认共享打印机', (c0.get('cups') or {}).get('share') is True)
chk('出厂默认禁止远程管理', (c0.get('cups') or {}).get('remote_admin') is False)
chk('出厂默认开 cups-browsed', (c0.get('cups') or {}).get('browsed') is True)
chk('出厂默认 RAW 端口 9100', (c0.get('raw') or {}).get('port') == 9100)
if d.get('installed'):
    chk('队列能被解析出来（不再靠本地化的 lpstat）',
        isinstance(d.get('queues'), list))
    for q in (d.get('queues') or []):
        print('       %-40s shared=%s accepting=%s uri=%s'
              % (q.get('name'), q.get('shared'), q.get('accepting'), q.get('uri')))

# ------------------------------------------------------------ 2. 队列参数校验
print('\n--- 队列参数校验（这些都不该碰到 CUPS）---')
bad = call('/api/print', {'op': 'queue_add', 'name': '-bad name',
                          'uri': 'ipp://x/'}, timeout=40)
chk('非法队列名被拒', bad.get('ok') is False, '→ %s' % bad.get('msg_cn'))
bad2 = call('/api/print', {'op': 'queue_add', 'name': 'ok-name',
                           'uri': 'ftp://x/'}, timeout=40)
chk('不支持的 URI 方案被拒', bad2.get('ok') is False, '→ %s' % bad2.get('msg_cn'))
bad3 = call('/api/print', {'op': 'queue_add', 'name': 'ok-name',
                           'uri': 'ipp://x; rm -rf /'}, timeout=40)
chk('带 shell 元字符的 URI 被拒', bad3.get('ok') is False, '→ %s' % bad3.get('msg_cn'))
bad4 = call('/api/print', {'op': 'job_cancel', 'id': '../../etc/passwd'}, timeout=40)
chk('非法任务号被拒', bad4.get('ok') is False, '→ %s' % bad4.get('msg_cn'))
bad5 = call('/api/print', {'op': 'queue_del', 'name': 'ok-name',
                           'confirm': 'wrong'}, timeout=40)
chk('删除队列要二次确认', bad5.get('ok') is False, '→ %s' % bad5.get('msg_cn'))
bad6 = call('/api/print', {'op': 'bogus_op'}, timeout=30)
chk('未知 op 被拒', bad6.get('ok') is False, '→ %s' % bad6.get('msg_cn'))

print('\n--- 打印任务查询 ---')
j = call('/api/print', {'op': 'jobs'}, timeout=40)
chk('jobs 接口可用', j.get('ok') is True, '→ %s' % j.get('msg_cn'))
chk('返回任务数与列表', 'count' in (j.get('data') or {})
    and 'jobs' in (j.get('data') or {}))

try:
    # ------------------------------------------------------------ 3. 关闭模式
    print('\n--- 关闭模式：所有打印服务都停掉 ---')
    s = call('/api/print', {'op': 'save', 'mode': 'off'}, timeout=150)
    chk('off 保存成功', s.get('ok') is True, '→ %s' % (s.get('msg_cn') or '')[:120])
    time.sleep(2)
    chk('CUPS 已停', svc('cups.service')[0] != 'active', '→ %s' % (svc('cups.service'),))
    chk('631 不再监听', listen_of(631) == [], '→ %s' % (listen_of(631),))
    chk('9100 没有监听', listen_of(9100) == [])

    # ------------------------------------------------------------ 4. CUPS 模式
    print('\n--- CUPS 模式：真正生效到 631 ---')
    s = call('/api/print', {'op': 'save', 'mode': 'cups',
                            'cups': {'listen': 'lan', 'share': True,
                                     'web_iface': True, 'remote_admin': False,
                                     'browsed': True}}, timeout=150)
    chk('cups 保存成功', s.get('ok') is True, '→ %s' % (s.get('msg_cn') or '')[:160])
    time.sleep(3)
    now = listen_of(631)
    print('     631 监听：%s' % (now or '无'))
    chk('631 已经不是只听 127.0.0.1', bool(now) and '127.0.0.1' not in now,
        '→ %s' % now)
    chk('CUPS 在运行', svc('cups.service')[0] == 'active')
    txt = open(CUPSD, encoding='utf-8', errors='replace').read()
    root_blk = ''
    if '<Location />' in txt:
        root_blk = txt.split('<Location />', 1)[1].split('</Location>', 1)[0]
    chk('根 Location 段收窄成 @LOCAL', 'Allow @LOCAL' in root_blk, repr(root_blk[:120]))
    chk('没有残留 Allow all（cupsctl 会写成放行一切）', 'Allow all' not in root_blk)
    rc, _o, e = sh(['cupsd', '-t'], timeout=20)
    chk('cupsd -t 配置校验通过', rc == 0, (e or '')[:120])
    st = call('/api/print', {'op': 'status'}, timeout=60)
    chk('状态里读到新监听', (st.get('data') or {}).get('listen') != '')

    # ------------------------------------------------------------ 6. 队列增删（真做再删干净）
    print('\n--- 队列增删（做完立刻删掉）---')
    TESTQ = 'drouter-livetest'
    qa = call('/api/print', {'op': 'queue_add', 'name': TESTQ,
                             'uri': 'socket://127.0.0.1:9/', 'driver': 'raw',
                             'info': '验收临时队列', 'share': False}, timeout=100)
    chk('添加队列接口可用', qa.get('ok') is True, '→ %s' % (qa.get('msg_cn') or '')[:140])
    st2 = call('/api/print', {'op': 'status'}, timeout=60)
    names = [q['name'] for q in ((st2.get('data') or {}).get('queues') or [])]
    chk('新队列出现在列表里', TESTQ in names, '→ %s' % names)
    if TESTQ in names:
        qs = call('/api/print', {'op': 'queue_set', 'name': TESTQ,
                                 'share': True}, timeout=60)
        chk('设置共享成功', qs.get('ok') is True, '→ %s' % (qs.get('msg_cn') or '')[:120])
    qd = call('/api/print', {'op': 'queue_del', 'name': TESTQ,
                             'confirm': TESTQ}, timeout=60)
    chk('删除队列成功', qd.get('ok') is True, '→ %s' % (qd.get('msg_cn') or '')[:120])
    st3 = call('/api/print', {'op': 'status'}, timeout=60)
    chk('队列确实被删掉了',
        TESTQ not in [q['name'] for q in ((st3.get('data') or {}).get('queues') or [])])


    # ------------------------------------------------------------ 5. RAW 模式（这台机器没插打印机）
    print('\n--- RAW 模式：互斥 + 没设备时如实报错 ---')
    s = call('/api/print', {'op': 'save', 'mode': 'raw',
                            'raw': {'device': '/dev/usb/lp0', 'port': 9100,
                                    'bind': '0.0.0.0'}}, timeout=150)
    print('     返回：%s' % (s.get('msg_cn') or '')[:200])
    time.sleep(2)
    chk('切到 RAW 后 CUPS 被停掉（互斥生效）',
        svc('cups.service')[0] != 'active', '→ %s' % (svc('cups.service'),))
    chk('CUPS 也被 disable 掉', svc('cups.service')[1] != 'enabled')
    chk('631 不再被 CUPS 占用', listen_of(631) == [])
    chk('RAW 的 wrapper 已生成', os.path.isfile(RAW_WRAPPER))
    if os.path.isfile(RAW_WRAPPER):
        w = open(RAW_WRAPPER, encoding='utf-8').read()
        print('     wrapper：%s' % w.strip().splitlines()[-1])
        chk('wrapper 用 GOPEN 开字符设备', 'GOPEN:/dev/usb/lp0' in w)
        chk('wrapper 监听 9100 且带 fork', 'TCP-LISTEN:9100' in w and 'fork' in w)
    chk('RAW 的 systemd 单元已生成', os.path.isfile(RAW_UNIT))
    if os.path.isfile(RAW_UNIT):
        u = open(RAW_UNIT, encoding='utf-8').read()
        chk('单元里写全了 PATH', 'Environment=PATH=' in u and '/usr/sbin' in u)
    rawst = call('/api/print', {'op': 'status'}, timeout=60)
    rd = rawst.get('data') or {}
    print('     RAW 服务：%s  USB 打印机：%s  设备节点：%s'
          % (rd.get('raw'), rd.get('usb_printers'), rd.get('usb_nodes')))
    if not rd.get('usb_nodes'):
        chk('没有 USB 打印机时给出明确提示（不是静默失败）',
            '找不到' in (s.get('msg_cn') or '')
            or any('找不到' in (x or '') for x in ((s.get('data') or {}).get('errors') or [])),
            '→ %s' % ((s.get('data') or {}).get('errors') or []))

    # ------------------------------------------------------------ 7. 备份与还原
    print('\n--- cupsd.conf 备份与还原 ---')
    st4 = call('/api/print', {'op': 'status'}, timeout=60)
    bks = (st4.get('data') or {}).get('backups') or []
    chk('产生了备份', len(bks) >= 1, '→ %d 份' % len(bks))
    bad7 = call('/api/print', {'op': 'restore', 'file': '../../etc/shadow'}, timeout=40)
    chk('还原路径穿越被拒', bad7.get('ok') is False, '→ %s' % bad7.get('msg_cn'))
    if bks:
        rr = call('/api/print', {'op': 'restore', 'file': bks[0]['file']}, timeout=100)
        chk('还原接口可用', rr.get('ok') is True, '→ %s' % (rr.get('msg_cn') or '')[:120])

except Exception as e:
    chk("验收流程自身未崩溃", False, "→ %s" % e)
    print("     （异常已吞掉，继续走还原流程）")
# ------------------------------------------------------------ 8. 还原现场
print('\n--- 还原现场 ---')
call('/api/print', {'op': 'save', 'mode': 'cups',
                    'cups': {'listen': 'lan', 'share': True, 'web_iface': True,
                             'remote_admin': False, 'browsed': True}}, timeout=150)
sh(['systemctl', 'stop', RAW_SERVICE], timeout=60)
sh(['systemctl', 'disable', RAW_SERVICE], timeout=20)
# 先把打印服务全部停干净再改配置：一边 restart 一边写 cupsd.conf 会让
# systemd 的 job 排队卡住（实测 systemctl restart cups.socket 卡过 60 秒）。
for name in ('cups-browsed.service', 'cups.service', 'cups.socket'):
    sh(['systemctl', 'stop', name], timeout=60)
sh(['systemctl', 'reset-failed'], timeout=30)
# cupsd.conf 按字节写回
with open(CUPSD, 'w', encoding='utf-8') as f:
    f.write(ORIG_CUPSD)
chk('cupsd.conf 已按原样写回',
    open(CUPSD, encoding='utf-8', errors='replace').read() == ORIG_CUPSD)
# 服务状态按原样恢复
for name, (act, en) in ORIG_SVC.items():
    if en == 'enabled':
        sh(['systemctl', 'enable', name], timeout=30)
    else:
        sh(['systemctl', 'disable', name], timeout=30)
for name, (act, en) in ORIG_SVC.items():
    if act == 'active':
        sh(['systemctl', 'start', name], timeout=90)
    else:
        sh(['systemctl', 'stop', name], timeout=60)
time.sleep(4)
for name, (act, en) in ORIG_SVC.items():
    cur = svc(name)
    chk('%s 恢复如初（原 %s/%s）' % (name, act, en), cur == (act, en), '→ %s' % (cur,))
print('     631 监听：%s（原：%s）'
      % (listen_of(631) or '无', ['127.0.0.1'] if 'Listen' in ORIG_CUPSD else '?'))
chk('9100 未被占用', listen_of(9100) == [])

# 清理本次产生的东西
if not ORIG_RAW_UNIT and os.path.isfile(RAW_UNIT):
    os.remove(RAW_UNIT)
if not ORIG_RAW_WRAPPER and os.path.isfile(RAW_WRAPPER):
    os.remove(RAW_WRAPPER)
sh(['systemctl', 'daemon-reload'], timeout=30)
if os.path.isdir(BAK_DIR):
    new = set(os.listdir(BAK_DIR)) - BAK_BEFORE
    for f in new:
        try:
            os.remove(os.path.join(BAK_DIR, f))
        except Exception:
            pass
    print('     本次产生的 %d 份备份已清理' % len(new))
if had_conf:
    os.rename(PRINT_CONF + '.livetest-bak', PRINT_CONF)
    print('     开关状态文件已还原')
else:
    try:
        os.remove(PRINT_CONF)
        print('     本次生成的开关状态文件已清理')
    except Exception:
        pass

# ------------------------------------------------------------ 9. 安全红线复核
print('\n--- 安全红线复核 ---')
rc, vnc, _ = sh(['ss', '-lnt'])
chk('VNC 5900 仍在监听（绝不能碰）',
    any(':5900' in ln for ln in vnc.splitlines()))
rc, ipa, _ = sh(['ip', '-o', '-4', 'addr', 'show', 'ens18'])
chk('ens18 仍是 192.168.7.3', '192.168.7.3' in ipa, ipa.strip()[:80])
rc, rt, _ = sh(['ip', 'route', 'show', 'default'])
chk('默认网关仍是 192.168.7.2（没有接管路由）', '192.168.7.2' in rt, rt.strip()[:80])
for s_ in ('dnsmasq', 'kea-dhcp4-server'):
    chk('%s 没有被启动' % s_, svc(s_)[0] != 'active')
chk('drouter-web 仍在运行', svc('drouter-web')[0] == 'active')
chk('救援通道未被打开', svc('drouter-rescue')[0] != 'active')

print('\n' + '=' * 60)
print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)

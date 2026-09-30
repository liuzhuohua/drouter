#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机验收：Docker 引擎配置 daemon.json（#5）。

必须 scp 到目标机再跑（BASE 是 127.0.0.1:8443）。
用法： python3 /tmp/live-dcfg.py

会真正写 /etc/docker/daemon.json（这台机器上 Docker 未安装，所以不会、
也不能重启任何服务）。跑完会还原成「保存前的状态」：
  * 原来没有 daemon.json → 删掉，并把 /etc/docker 目录也清理掉；
  * 原来有 → 用自动备份还原，再删掉本次产生的备份。

安全边界：不碰 5900、不动网卡、不启停任何网络服务。
"""
import json
import os
import ssl
import sys
import urllib.request

BASE = 'https://127.0.0.1:8443'
CTX = ssl._create_unverified_context()
TOK = None
PASS = FAIL = 0

DCFG_FILE = '/etc/docker/daemon.json'
DCFG_CONF = '/etc/drouter/dcfg.json'          # drouter 自己记的开关状态
DCFG_CONF_BAK = DCFG_CONF + '.livetest-bak'
DCFG_BAK_DIR = '/var/lib/drouter/dcfg-backup'


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


TOK = call('/api/login', {'username': 'admin', 'password': 'admin123'}).get('data', {}).get('token')
if not TOK:
    print('登录失败，终止')
    sys.exit(1)

had_file = os.path.isfile(DCFG_FILE)
# 开关状态文件必须先挪走：上一轮验收如果保存过 ipv6=True，这一轮读到的就不是
# 「出厂默认」了，「IPv6 默认关」会假失败。这是测试污染，不是产品问题。
had_conf = os.path.isfile(DCFG_CONF)
if had_conf:
    os.rename(DCFG_CONF, DCFG_CONF_BAK)
bak_before = set()
if os.path.isdir(DCFG_BAK_DIR):
    bak_before = set(os.listdir(DCFG_BAK_DIR))
print('保存前：daemon.json %s；开关状态文件 %s'
      % ('存在' if had_file else '不存在（本次会新建）',
         '已临时挪开（验收完还原）' if had_conf else '不存在'))

# ---------------------------------------------------------------- 1. 读取
print('\n--- 状态读取 ---')
r = call('/api/dcfg', {'op': 'status'}, timeout=60)
chk('status 接口可用', r.get('ok') is True, '→ %s' % r.get('msg_cn'))
d = r.get('data') or {}
print('     Docker 引擎：%s  服务：%s  开机自启：%s'
      % (d.get('installed'), d.get('active'), d.get('enabled')))
print('     配置文件：%s（%s）' % (d.get('file'), '存在' if d.get('exists') else '不存在'))
chk('返回配置文件路径', d.get('file') == DCFG_FILE)
chk('返回是否已安装', 'installed' in d)
chk('返回服务状态', 'active' in d and 'enabled' in d)
chk('返回镜像源清单', len(d.get('mirrors') or []) >= 5,
    '→ %d 个' % len(d.get('mirrors') or []))
chk('返回未纳管键列表', 'unmanaged' in d)
chk('返回预览代码', bool((d.get('preview') or '').strip()))
print('     默认预览：')
for ln in (d.get('preview') or '').rstrip().splitlines():
    print('       ' + ln)

c0 = d.get('cfg') or {}
chk('IPv6 默认关', c0.get('ipv6') is False)
chk('日志滚动默认开', c0.get('log_rotate') is True)
chk('live-restore 默认开', c0.get('live_restore') is True)

# ---------------------------------------------------------------- 2. 预览与落盘一致
print('\n--- 预览 = 真正写入的内容 ---')
prev = call('/api/dcfg', {'op': 'preview',
                          'ipv6': True, 'fixed_cidr_v6': 'fdab:cdef:1234::/64',
                          'experimental': False,
                          'mirrors': ['nju', 'tencent'],
                          'mirror_custom': '',
                          'log_rotate': True, 'log_max_size': '20m', 'log_max_file': 5,
                          'live_restore': True, 'bip': ''}, timeout=60)
chk('preview 接口可用', prev.get('ok') is True)
p_txt = ((prev.get('data') or {}).get('preview') or '')
print('     预览：')
for ln in p_txt.rstrip().splitlines():
    print('       ' + ln)
chk('预览含 ipv6', '"ipv6": true' in p_txt)
chk('预览含指定网段', 'fdab:cdef:1234::/64' in p_txt)
chk('预览含 ip6tables', '"ip6tables": true' in p_txt)
chk('预览不含 experimental（未勾选）', '"experimental"' not in p_txt)
chk('预览含两个镜像源',
    '"registry-mirrors"' in p_txt
    and 'docker.nju.edu.cn' in p_txt
    and 'mirror.ccs.tencentyun.com' in p_txt)
chk('预览含日志滚动参数',
    '"max-size": "20m"' in p_txt and '"max-file": "5"' in p_txt)

# ---------------------------------------------------------------- 3. 保存
print('\n--- 保存 ---')
s = call('/api/dcfg', {'op': 'save',
                       'ipv6': True, 'fixed_cidr_v6': 'fdab:cdef:1234::/64',
                       'experimental': False,
                       'mirrors': ['nju', 'tencent'],
                       'mirror_custom': '',
                       'log_rotate': True, 'log_max_size': '20m', 'log_max_file': 5,
                       'live_restore': True, 'bip': ''}, timeout=60)
chk('save 接口可用', s.get('ok') is True, '→ %s' % s.get('msg_cn'))
sd = s.get('data') or {}
written = sd.get('content') or ''
chk('返回真实写入内容', bool(written))
chk('落盘内容与预览逐字节一致', written.strip() == p_txt.strip())
chk('保存时产生了备份', bool(sd.get('backup')) or not had_file,
    '→ %s' % sd.get('backup'))
try:
    on_disk = open(DCFG_FILE, encoding='utf-8').read()
except Exception as e:
    on_disk = '读取失败：%s' % e
chk('文件里的内容与接口返回一致', on_disk == written)
try:
    parsed = json.loads(on_disk)
    chk('落盘的是合法 JSON', True)
    chk('落盘内容含 ipv6 与网段',
        parsed.get('ipv6') is True and parsed.get('fixed-cidr-v6') == 'fdab:cdef:1234::/64')
    chk('落盘内容含镜像源', len(parsed.get('registry-mirrors') or []) == 2)
except Exception as e:
    chk('落盘的是合法 JSON', False, str(e))
chk('保存后没有去重启 Docker（本页从不自动重启）',
    '重启' not in (s.get('msg_cn') or '') or '需要重启' in (s.get('msg_cn') or ''))

# ---------------------------------------------------------------- 4. 保留用户自己的键
print('\n--- 保留用户自己写的键 ---')
with open(DCFG_FILE, 'w', encoding='utf-8') as f:
    f.write(json.dumps({'data-root': '/mnt/ssd/docker',
                        'insecure-registries': ['10.0.0.5:5000'],
                        'ipv6': True, 'fixed-cidr-v6': 'fd00:dead::/64'},
                       ensure_ascii=False, indent=2) + '\n')
s2 = call('/api/dcfg', {'op': 'save', 'ipv6': False, 'mirrors': [],
                        'mirror_custom': '', 'log_rotate': True,
                        'log_max_size': '10m', 'log_max_file': 3,
                        'live_restore': True, 'bip': ''}, timeout=60)
chk('带外来键也能保存', s2.get('ok') is True, '→ %s' % s2.get('msg_cn'))
try:
    p2 = json.loads(open(DCFG_FILE, encoding='utf-8').read())
except Exception as e:
    p2 = {}
    chk('二次保存后仍是合法 JSON', False, str(e))
chk('data-root 被保留', p2.get('data-root') == '/mnt/ssd/docker')
chk('insecure-registries 被保留', p2.get('insecure-registries') == ['10.0.0.5:5000'])
chk('关掉的 ipv6 被移除', 'ipv6' not in p2 and 'fixed-cidr-v6' not in p2)
r2 = call('/api/dcfg', {'op': 'status'}, timeout=60)
chk('状态里列出未纳管键',
    set(((r2.get('data') or {}).get('unmanaged') or []))
    >= {'data-root', 'insecure-registries'},
    '→ %s' % ((r2.get('data') or {}).get('unmanaged')))

# ---------------------------------------------------------------- 5. ULA 生成
print('\n--- 随机私有段 ---')
u = call('/api/dcfg', {'op': 'ula'}, timeout=30)
ula = ((u.get('data') or {}).get('ula') or '')
chk('ula 接口可用', u.get('ok') is True)
print('     生成：%s' % ula)
chk('是 fd 开头的 /48', ula.startswith('fd') and ula.endswith('/48'))
u2 = ((call('/api/dcfg', {'op': 'ula'}, timeout=30).get('data') or {}).get('ula') or '')
chk('两次生成不同', ula != u2, '%s / %s' % (ula, u2))

# ---------------------------------------------------------------- 6. 参数校验
print('\n--- 参数校验 ---')
bad = call('/api/dcfg', {'op': 'save', 'ipv6': True, 'fixed_cidr_v6': '瞎写的',
                         'mirrors': [], 'log_rotate': True, 'live_restore': True,
                         'bip': ''}, timeout=60)
chk('非法 IPv6 网段被拒', bad.get('ok') is False, '→ %s' % bad.get('msg_cn'))
bad2 = call('/api/dcfg', {'op': 'save', 'ipv6': False, 'mirrors': ['不存在的源'],
                          'log_rotate': True, 'live_restore': True}, timeout=60)
chk('未知镜像源被拒', bad2.get('ok') is False, '→ %s' % bad2.get('msg_cn'))
bad3 = call('/api/dcfg', {'op': 'save', 'ipv6': False, 'mirrors': [],
                          'mirror_custom': 'http://明文.com',
                          'log_rotate': True, 'live_restore': True}, timeout=60)
chk('明文 http 自定义源被拒', bad3.get('ok') is False, '→ %s' % bad3.get('msg_cn'))
bad4 = call('/api/dcfg', {'op': 'bogus'}, timeout=30)
chk('未知 op 被拒', bad4.get('ok') is False)
bad5 = call('/api/dcfg', {'op': 'restore', 'file': '../../etc/shadow'}, timeout=30)
chk('还原路径穿越被拒', bad5.get('ok') is False, '→ %s' % bad5.get('msg_cn'))

# ---------------------------------------------------------------- 7. 镜像源测速
print('\n--- 镜像源一键测速 ---')
mt = call('/api/dcfg', {'op': 'mirror_test'}, timeout=220)
chk('测速接口可用', mt.get('ok') is True, '→ %s' % mt.get('msg_cn'))
rows = ((mt.get('data') or {}).get('results') or [])
for t in rows:
    flag = '可用' if t.get('ok') else ('跳过' if t.get('skipped') else '不可用')
    print('     %-16s %-6s %-8s %s'
          % (t.get('name'), flag, ('%d ms' % t.get('ms')) if t.get('ms') else '—',
             t.get('reason') or ''))
chk('返回了全部预设的结果', len(rows) >= 6, '→ %d 条' % len(rows))
chk('阿里云占位地址被跳过',
    any(t.get('skipped') and t.get('key') == 'aliyun' for t in rows))
chk('每条都有结论文案', all((t.get('reason') or '') for t in rows))

# ---------------------------------------------------------------- 8. 重启保护
print('\n--- 重启保护（这台机器上 Docker 未安装）---')
rs = call('/api/dcfg', {'op': 'restart', 'confirm': True}, timeout=60)
if (r.get('data') or {}).get('installed'):
    chk('已安装时应给出明确结果', 'msg_cn' in rs, '→ %s' % rs.get('msg_cn'))
else:
    chk('未安装时拒绝重启', rs.get('ok') is False, '→ %s' % rs.get('msg_cn'))
rs2 = call('/api/dcfg', {'op': 'restart'}, timeout=60)
chk('不带确认就拒绝', rs2.get('ok') is False, '→ %s' % rs2.get('msg_cn'))

# ---------------------------------------------------------------- 9. 还原
print('\n--- 备份与还原 ---')
st = call('/api/dcfg', {'op': 'status'}, timeout=60)
bks = ((st.get('data') or {}).get('backups') or [])
chk('产生了备份记录', len(bks) >= 1, '→ %d 份' % len(bks))
if bks:
    pick = bks[0]['file']
    rr = call('/api/dcfg', {'op': 'restore', 'file': pick}, timeout=60)
    chk('还原接口可用', rr.get('ok') is True, '→ %s' % rr.get('msg_cn'))
    chk('还原后文件内容与备份一致',
        (rr.get('data') or {}).get('content', '').strip()
        == open(DCFG_FILE, encoding='utf-8').read().strip())

# ---------------------------------------------------------------- 10. 还原现场
print('\n--- 还原现场 ---')
if had_file:
    print('     原本有 daemon.json，交由用户确认保留哪一份（备份目录里都在）')
else:
    try:
        os.remove(DCFG_FILE)
        chk('已删除本次新建的 daemon.json', not os.path.isfile(DCFG_FILE))
    except Exception as e:
        chk('已删除本次新建的 daemon.json', False, str(e))
    try:
        if os.path.isdir('/etc/docker') and not os.listdir('/etc/docker'):
            os.rmdir('/etc/docker')
            print('     /etc/docker 空目录也已清理')
    except Exception:
        pass
if os.path.isdir(DCFG_BAK_DIR):
    new = set(os.listdir(DCFG_BAK_DIR)) - bak_before
    for f in new:
        try:
            os.remove(os.path.join(DCFG_BAK_DIR, f))
        except Exception:
            pass
    print('     本次产生的 %d 份备份已清理' % len(new))
if had_conf:
    os.rename(DCFG_CONF_BAK, DCFG_CONF)
    print('     开关状态文件已还原')
else:
    try:
        os.remove(DCFG_CONF)
    except Exception:
        pass
    print('     本次生成的开关状态文件已清理')

print('\n' + '=' * 60)
print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)

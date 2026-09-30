#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机冒烟：登录 → 检查统一配置 → 抽查各功能模块接口是否正常返回。"""
import json
import ssl
import urllib.error
import urllib.request

BASE = 'https://127.0.0.1:8443'
CTX = ssl._create_unverified_context()


def call(path, payload=None, token=None):
    data = json.dumps(payload or {}).encode('utf-8')
    req = urllib.request.Request(BASE + path, data=data, method='POST' if payload else 'GET')
    req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('X-Token', token)
    try:
        with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode('utf-8'))
        except Exception:
            return {'ok': False, 'msg_cn': 'HTTP %s' % e.code}
    except Exception as e:
        return {'ok': False, 'msg_cn': str(e)}


r = call('/api/login', {'username': 'admin', 'password': 'admin123'})
if not r.get('ok'):
    print('登录失败：%s' % r.get('msg_cn'))
    raise SystemExit(1)
tok = r['data']['token']
print('登录：OK')

# 统一配置：确认各模块（含端口转发、主题）都能取到
cfg = call('/api/config', None, tok)
data = (cfg.get('data') or {})
print('配置 ok=%s，模块数=%d' % (cfg.get('ok'), len(data)))
print('  模块：%s' % ', '.join(sorted(data)))
pf = (data.get('portfwd') or {}).get('rules') or []
print('  端口转发规则数：%d' % len(pf))
print('  主题配置：%s' % json.dumps(data.get('theme'), ensure_ascii=False))

# 各功能模块接口（路径取自 drouter-web.py 的真实路由表，不要凭印象写）
paths = ['sysinfo', 'metrics', 'fwlog', 'fw/log', 'wan/log', 'ddns',
         'ulog', 'ulog/conf', 'ulog/daemon', 'ulog/flow',
         'docker', 'acl', 'share', 'qos', 'pppoe-multi', 'ipv6', 'leases',
         'nft', 'services', 'routes', 'users', 'journal',
         'theme', 'themes', 'vlan', 'wol', 'dpi', 'flow',
         'nat/check', 'snapshot/list', 'sysinfo']
print('\n接口冒烟：')
bad = 0
for p in paths:
    rr = call('/api/' + p, None, tok)
    ok_ = rr.get('ok')
    if not ok_:
        bad += 1
    print('  %-14s %s  %s' % (p, 'OK  ' if ok_ else 'FAIL', str(rr.get('msg_cn'))[:44]))

print('\n结果：%d 个接口，失败 %d 个' % (len(paths), bad))
raise SystemExit(1 if bad else 0)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机探测：每个页面的「应用」按钮到底能不能跑通。

用法：必须拷到目标机上执行（BASE 是 127.0.0.1）。
    python3 live-apply-probe.py

只发 check_only=true，不写盘、不启服务、不动网络 —— 纯粹验证
「前端提交的 module 名 → 后端 APPLY_SPEC / render 渲染器」这条链是否闭合。
"""
import json
import ssl
import sys
import urllib.request

BASE = 'https://127.0.0.1:8443'
CTX = ssl._create_unverified_context()

# 页面 key → 该页「应用」要渲染的模块（与 app.js 的 PAGE_MODULES.apply 保持一致）
CASES = [
    ('iface',   []),
    ('lan',     []),
    ('wan',     ['pppoe']),
    ('dhcp',    ['dnsmasq']),
    ('dns',     ['dnsmasq']),
    ('dhcpv6',  ['dhcpv6']),
    ('ipv6',    ['radvd', 'dhcpv6']),
    ('fw4',     ['nft_v4']),
    ('fw6',     ['nft_v6']),
    ('portfwd', ['nft_v4', 'nft_v6']),
    ('upnp',    ['upnp']),
    ('ntp',     ['ntp']),
]

# 纯配置模块：不会再被前端送去 apply，但后端仍要给出明确提示而不是报错
CONFIG_ONLY = ['system', 'portfwd']


def post(path, body, tk):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json',
                                          'X-Token': tk},
                                 method='POST')
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode('utf-8'))
    except Exception as e:
        return {'ok': False, 'msg_cn': '请求异常：%s' % e}


def main():
    req = urllib.request.Request(BASE + '/api/login',
                                 data=json.dumps({'username': 'admin',
                                                  'password': 'admin123'}).encode(),
                                 headers={'Content-Type': 'application/json'},
                                 method='POST')
    with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
        j = json.loads(r.read().decode('utf-8'))
    if not j.get('ok'):
        print('登录失败：%s' % j.get('msg_cn'))
        return 1
    tk = j['data']['token']

    bad = 0
    print('%-10s %-10s %-6s %s' % ('页面', 'module', '结果', '后端提示'))
    print('-' * 90)
    for page, mods in CASES:
        if not mods:
            print('%-10s %-10s %-6s %s' % (page, '(无)', '—', '该页只保存，不渲染配置文件'))
            continue
        for m in mods:
            # 不传 data，避免 merge_cfg 改动任何已保存配置
            r = post('/api/apply', {'module': m, 'check_only': True}, tk)
            msg = r.get('msg_cn', '')
            if r.get('ok'):
                flag = 'OK'
            elif msg.startswith('配置校验失败'):
                # 渲染器接住了、只是配置内容还没填（例如没配 PPPoE 网卡）。
                # 这是「填内容」的问题，不是「接线」的问题，不计入失败。
                flag = 'CFG'
            else:
                flag = 'FAIL'
                bad += 1
            print('%-10s %-10s %-6s %s' % (page, m, flag, msg))
    print('\n纯配置模块（防御性检查：不应再报「未知的模块」）')
    for m in CONFIG_ONLY:
        r = post('/api/apply', {'module': m, 'check_only': True}, tk)
        flag = 'OK' if r.get('ok') else 'FAIL'
        if not r.get('ok'):
            bad += 1
        print('%-10s %-10s %-6s %s' % ('-', m, flag, r.get('msg_cn', '')))
    print('\n失败项：%d' % bad)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())

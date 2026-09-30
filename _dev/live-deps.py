#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""依赖自检（#5）真机验收 —— 必须在目标机上跑（BASE 是 127.0.0.1:8443）。

只看三件事：
  1. 清单里新加的项都能被正确探测（有就是有、没有就是没有，别因为 PATH 少
     /usr/sbin 而把已装的 mkfs.* 报成缺失）；
  2. 缺失的可选大件（samba / nfs / docker）不会出现在「一键安装」的范围里；
  3. 页面能拿到完整清单。
"""
import json
import ssl
import sys
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


def call(path, body=None, token=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if token:
        headers['X-Token'] = token
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method='POST' if body is not None else 'GET')
    with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
        return json.loads(r.read().decode('utf-8'))


def main():
    r = call('/api/login', {'username': 'admin', 'password': 'admin123'})
    chk('登录成功', r.get('ok'), r.get('msg_cn'))
    if not r.get('ok'):
        return 1
    tk = r['data']['token']

    r = call('/api/deps/check', None, tk)
    chk('自检接口返回成功', r.get('ok'), r.get('msg_cn'))
    d = r.get('data') or {}
    items = d.get('items') or []
    by = {x['key']: x for x in items}
    s = d.get('summary') or {}
    print('     总计 %s 项：已就绪 %s / 必需缺失 %s / 可选未装 %s'
          % (s.get('total'), s.get('ok'), s.get('missing'), s.get('optional')))

    for k in ('util-linux', 'tc', 'journalctl', 'mtr', 'samba', 'nfs-server',
              'docker', 'docker-compose', 'exfatprogs', 'ntfs-3g', 'dosfstools',
              'btrfs-progs', 'xfsprogs', 'f2fs-tools', 'etherwake', 'tcpdump',
              'nmcli', 'networkctl'):
        chk('清单含 %s' % k, k in by)

    # PATH 陷阱：这些命令都在 /sbin / /usr/sbin，helper 必须已归一化 PATH
    for k in ('util-linux', 'tc'):
        x = by.get(k) or {}
        chk('%s 被正确探测为已安装（PATH 含 /sbin）' % k, x.get('ok') is True,
            '→ %s' % (x.get('where') or x.get('target')))

    # 本机确实没装的，应如实报缺失（而不是靠猜）
    for k in ('samba', 'nfs-server', 'docker'):
        x = by.get(k) or {}
        chk('%s 如实报为未安装' % k, x.get('ok') is False)

    # 危险项不给安装包
    for k in ('nmcli', 'networkctl', 'systemd', 'vnc'):
        x = by.get(k) or {}
        chk('%s 不提供一键安装（pkg 为空）' % k, not (x.get('pkg') or ''))

    # 可选大件绝不能是「必需」，否则会被一键安装带上
    for k in ('samba', 'nfs-server', 'docker', 'xfce', 'xorg'):
        x = by.get(k) or {}
        chk('%s 是可选（不进一键安装）' % k, x.get('required') is False)

    print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % fails))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())

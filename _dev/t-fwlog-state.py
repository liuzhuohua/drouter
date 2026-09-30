#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""防火墙日志「为什么一条都没有」的状态判定测试。

用户看到空白的防火墙日志页时，最自然的反应是「这功能没做」。所以后端必须能
区分两种成因并分开提示：
  1) 规则没载入内核（nft 规则集里没有 drouter 表）—— 本机还没接管路由；
  2) 规则已载入，但「记录被拒绝的包」开关没开 —— 规则里不含 log 语句。

本测试从 drouter-helper.py 抽取相关函数，把 nft 调用和 /proc 文件都换成假的。
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'backend', 'drouter-helper.py')
code = open(SRC, encoding='utf-8').read()
tree = ast.parse(code)

WANT = ['_fw_ruleset_applied', '_ip_forwarding_on']
picked = [n for n in tree.body
          if isinstance(n, ast.FunctionDef) and n.name in WANT]
got = {n.name for n in picked}
missing = set(WANT) - got
assert not missing, '未能从源码抽取：%s' % ', '.join(sorted(missing))

# ---- 测试替身 -------------------------------------------------------------
STATE = {'nft_out': '', 'nft_rc': 0, 'proc': {}}


def fake_sh(cmd, timeout=None):
    """只认 nft list ruleset，其余一律当作失败。"""
    if cmd[:2] == ['nft', 'list'] and cmd[2] == 'ruleset':
        return (STATE['nft_rc'], STATE['nft_out'], '')
    return (1, '', 'unexpected command: %s' % cmd)


class FakeFile:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_open(path, *a, **k):
    if path in STATE['proc']:
        return FakeFile(STATE['proc'][path])
    raise OSError(2, 'No such file', path)


ns = {'os': os, 'sh': fake_sh}
ns['open'] = fake_open
src = '\n'.join(ast.unparse(n) for n in picked)
exec(compile(src, '<extracted>', 'exec'), ns)

applied = ns['_fw_ruleset_applied']
forwarding = ns['_ip_forwarding_on']

fails = []


def ck(name, cond, extra=''):
    print('  %-42s %s %s' % (name, 'OK  ' if cond else 'FAIL', extra))
    if not cond:
        fails.append(name)


print('=' * 60)
print('防火墙日志状态判定')

# 1) 规则集为空（本机还没接管路由）
STATE['nft_out'] = ''
STATE['nft_rc'] = 0
ck('空规则集 → 判定为未载入', applied() is False)

# 2) 已载入 drouter4 表
STATE['nft_out'] = 'table ip drouter4 {\n\tchain input {\n\t}\n}'
ck('含 drouter4 表 → 判定为已载入', applied() is True)

# 3) 已载入 drouter6 表（只启用 IPv6 的情况）
STATE['nft_out'] = 'table ip6 drouter6 {\n\tchain input {\n\t}\n}'
ck('含 drouter6 表 → 判定为已载入', applied() is True)

# 4) 有其他表但没有 drouter 的表
STATE['nft_out'] = 'table inet filter {\n\tchain input {\n\t}\n}'
ck('只有别的表 → 判定为未载入', applied() is False)

# 5) nft 执行失败（例如 PATH 里没有 nft）不应误判成「已载入」
STATE['nft_out'] = 'table ip drouter4 {}'
STATE['nft_rc'] = 1
ck('nft 执行失败 → 不误判为已载入', applied() is False)
STATE['nft_rc'] = 0

# 6) IP 转发：v4 开
STATE['proc'] = {'/proc/sys/net/ipv4/ip_forward': '1\n',
                 '/proc/sys/net/ipv6/conf/all/forwarding': '0\n'}
ck('v4 转发开启 → 转发中', forwarding() is True)

# 7) IP 转发：仅 v6 开
STATE['proc'] = {'/proc/sys/net/ipv4/ip_forward': '0\n',
                 '/proc/sys/net/ipv6/conf/all/forwarding': '1\n'}
ck('仅 v6 转发开启 → 转发中', forwarding() is True)

# 8) IP 转发：都关（虚拟机 /proc 里可能根本没有这个文件，也应判 False 而不是崩）
STATE['proc'] = {'/proc/sys/net/ipv4/ip_forward': '0\n',
                 '/proc/sys/net/ipv6/conf/all/forwarding': '0\n'}
ck('v4/v6 都关 → 未转发', forwarding() is False)

STATE['proc'] = {}
ck('/proc 文件不存在 → 返回 False 而不抛异常', forwarding() is False)

# 9) 提示文案必须区分两种成因
src_all = code
i = src_all.find('def read_fw_log')
body = src_all[i:i + 4000]
ck('read_fw_log 会先判「规则未载入」再判「开关未开」',
   body.find('not applied') < body.find('not enabled') and 'not applied' in body)
ck('read_fw_log 回传 applied 字段', "'applied': applied" in body)
ck('read_fw_log 回传 forwarding 字段', "'forwarding': forwarding" in body)
ck('未载入时的提示指向「防火墙页点应用」', '点一次「应用」' in body)

# 10) 前端必须把这两个状态显示出来
js = open(os.path.join(HERE, '..', 'web', 'app.js'), encoding='utf-8').read()
ck('前端显示「规则已载入 / 未载入」', '规则未载入' in js)
ck('前端显示「IP 转发已开启 / 未开启」', 'IP 转发未开启' in js)
ck('前端读取 d.applied', 'd.applied' in js)
ck('前端读取 d.forwarding', 'd.forwarding' in js)

print('=' * 60)
print('结果：失败 %d 项' % len(fails))
sys.exit(1 if fails else 0)

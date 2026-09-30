#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
drouter-web.py 内部契约检查（不需要目标机、不需要起服务）：

1) GET /api/config 返回的模块清单  ==  save_config 允许保存的模块集合
   两者一旦走偏，就会出现「能保存但读不回来」的模块（theme 曾漏过）。
2) 所有 settings 默认键（cfg_defaults）都应在上面两份清单里，
   否则新加的配置模块会永远读不到。
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(os.path.dirname(HERE), 'backend')
WEB = os.path.join(BACKEND, 'drouter-web.py')

src = io.open(WEB, encoding='utf-8').read()

PASS = FAIL = 0
MSG = []


def has(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        MSG.append('  ✗ %s%s' % (name, ('  → ' + str(extra)) if extra else ''))


# ---- 1. GET /api/config 的 keys ----
m = re.search(r"p == '/api/config' and method == 'GET':.*?keys = \[(.*?)\]", src, re.S)
has('能定位 GET /api/config 的 keys', bool(m))
get_keys = set(re.findall(r"'([a-z0-9_]+)'", m.group(1))) if m else set()

# ---- 2. save_config 的 allowed ----
m2 = re.search(r"def save_config\(self\):.*?allowed = \{(.*?)\}", src, re.S)
has('能定位 save_config 的 allowed', bool(m2))
post_keys = set(re.findall(r"'([a-z0-9_]+)'", m2.group(1))) if m2 else set()

has('GET 与 POST 模块清单完全一致', get_keys == post_keys,
    'GET 多: %s / POST 多: %s' % (sorted(get_keys - post_keys), sorted(post_keys - get_keys)))

# ---- 3. cfg_defaults 的键都应被覆盖 ----
m3 = re.search(r'cfg_defaults = \{(.*?)\n    \}', src, re.S)
has('能定位 cfg_defaults', bool(m3))
# 只取顶层键（cfg_defaults 里缩进 8 空格）；嵌套的如 share.samba / share.nfs 不算模块
default_keys = set(re.findall(r"^ {8}'([a-z0-9_]+)':\s*\{", m3.group(1), re.M)) if m3 else set()
has('cfg_defaults 非空', len(default_keys) > 0, len(default_keys))
missing = sorted(default_keys - get_keys)
has('默认配置模块都在 GET 清单里', not missing, missing)

print('=' * 60)
print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
if FAIL:
    print('-' * 60)
    for x in MSG:
        print(x)
sys.exit(1 if FAIL else 0)

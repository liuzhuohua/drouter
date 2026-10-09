#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NAT 检测详情渲染契约测试。

事故：后端 act_nat_check 的 detail 是**字典**（local_ip / probes / servers_used），
前端却写 esc(d.detail) —— 页面上直接显示一个 "[object Object]" 方块，用户不知道
那是什么、也没法排查。

这里锁住三件事：
  1) 后端 detail 确实是结构化字典，且含排障必需的字段；
  2) 前端不再把 detail 当纯文本渲染，而是结构化展示（含 STUN 探测明细表）；
  3) esc() 收到对象时不再吐 "[object Object]"（最后一道兜底）。
"""
import ast
import glob
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')
SRC = os.path.join(ROOT, 'backend', 'drouter-helper.py')
JS = os.path.join(ROOT, 'web', 'app.js')
# ⚠️ 不要硬编码 node 的版本目录 —— WorkBuddy 升级后路径会变，
#    2026-10-06 实测：写死 22.22.2-3 时实际是 22.22.2-6，
#    全量回归里 t-nat-detail 直接 FileNotFoundError。
NODE = None
for _c in sorted(glob.glob(os.path.join(
        os.path.expanduser('~'), '.workbuddy', 'binaries', 'node',
        'versions', '*', 'node.exe')), reverse=True):
    if os.path.isfile(_c):
        NODE = _c
        break
if NODE is None:                    # 退回 PATH
    NODE = shutil.which('node') or 'node'
code = open(SRC, encoding='utf-8').read()
js = open(JS, encoding='utf-8').read()

fails = []


def ck(name, cond, extra=''):
    print('  %-48s %s %s' % (name, 'OK  ' if cond else 'FAIL', extra))
    if not cond:
        fails.append(name)


print('=' * 66)
print('一、后端：NAT detail 的结构')

# 直接跑 act_nat_check 需要联网，改为静态解析其返回的 detail 结构
i = code.find('def act_nat_check')
body = code[i:i + 2600]
ck('act_nat_check 里 detail 是字典字面量', "detail = {" in body)
for k in ('local_ip', 'probes', 'servers_used'):
    ck('detail 含字段 %s' % k, ("'%s'" % k) in body)
ck('无探测结果时会写入 reason', "detail['reason']" in body)
ck('probes 里含 server/ip/port',
   all(x in body for x in ("'server'", "'ip'", "'port'")))

print('二、前端：不再把 detail 当纯文本渲染')
ck('不再出现 esc(d.detail)', 'esc(d.detail)' not in js)
ck('存在结构化渲染函数 natDetailHtml', 'function natDetailHtml' in js)
ck('renderNat 调用 natDetailHtml', 'natDetailHtml(d.detail)' in js)
ck('展示本机出口地址', "detail.local_ip" in js)
ck('展示 STUN 探测明细表', 'detail.probes' in js)
ck('区分私网 / 公网出口', 'detail.local_private' in js)
ck('兼容 detail 为纯字符串的情况',
   "typeof detail === 'string'" in js)
# IPv6 测试与审计的 detail 本来就是字符串，不受影响；这里确认它们仍是字符串来源
i2 = code.find("item = {'key': t['key']")
ck('IPv6 测试的 detail 仍是字符串（未受影响）',
   "'detail': ''" in code[i2:i2 + 200])
ck('审计详情入库时已 str() 化（未受影响）',
   'str(detail)[:2000]' in open(os.path.join(ROOT, 'backend',
                                             'drouter-web.py'), encoding='utf-8').read())

print('三、esc() 的兜底行为（在 node 里真实执行）')
i3 = js.find('const esc = s => {')
if i3 < 0:
    i3 = js.find('const esc = s =>')
j3 = js.find('};', i3)
snippet = js[i3:j3 + 2]
probe = snippet + """
const cases = {
  obj: esc({ a: 1, b: 'x' }),
  arr: esc([1, 'two']),
  str: esc('<b>&"x"</b>'),
  nul: esc(null),
  num: esc(42),
};
console.log(JSON.stringify(cases));
"""
r = subprocess.run([NODE, '-e', probe], capture_output=True, text=True,
                   encoding='utf-8', errors='replace')
if r.returncode != 0:
    ck('esc 片段可在 node 中执行', False, r.stderr[-300:])
else:
    import json
    got = json.loads(r.stdout.strip().splitlines()[-1])
    ck('对象不再渲染成 [object Object]',
       '[object Object]' not in got['obj'], got['obj'])
    ck('对象退化为可读 JSON', got['obj'].startswith('{'), got['obj'])
    ck('数组不再渲染成 [object Object]',
       '[object Object]' not in got['arr'], got['arr'])
    ck('字符串仍正确转义', got['str'] == '&lt;b&gt;&amp;&quot;x&quot;&lt;/b&gt;',
       got['str'])
    ck('null 转为空串', got['nul'] == '', got['nul'])
    ck('数字正常显示', got['num'] == '42', got['num'])

print('=' * 66)
print('结果：失败 %d 项' % len(fails))
sys.exit(1 if fails else 0)

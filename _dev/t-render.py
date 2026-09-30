# -*- coding: utf-8 -*-
"""read_render_text 逻辑测试（本地模拟，helper 因 pwd 无法在 Windows 导入）。"""
import sys, re, io
sys.path.insert(0, r"C:/Users/lyrz-pve-win10/WorkBuddy/2026-09-27-19-57-00/router-build/backend")
import render

src = io.open(r"C:/Users/lyrz-pve-win10/WorkBuddy/2026-09-27-19-57-00/router-build/backend/drouter-helper.py",
              encoding='utf-8').read()
m = re.search(r'^def read_render_text\(.*?(?=\n\ndef act_apply)', src, re.S | re.M)
assert m, 'read_render_text 未找到'
NS = {'render': render, 'ValidateError': render.ValidateError, 'str': str}
exec('def ok(data=None, msg="ok", code="OK"):\n    return {"ok": True, "code": code, "msg_cn": msg, "data": data}\n', NS)
exec('def fail(msg, code="ERR", data=None):\n    return {"ok": False, "code": code, "msg_cn": msg, "data": data}\n', NS)
exec(m.group(0), NS)
fn = NS['read_render_text']

fails = 0
def chk(label, cond, extra=''):
    global fails
    if not cond: fails += 1
    print('[%s] %s %s' % ('OK' if cond else 'FAIL', label, extra))

cfg = {'wan_iface': 'ppp0', 'lan_ifaces': ['ens18'],
       'portfwd': {'enable': True, 'rules': [{'name': 'NAS', 'proto': 'tcp', 'ext_port': 5000,
       'int_ip': '192.168.7.10', 'int_port': 5000, 'enable': True}],
       'dmz': {'enable': True, 'host': '192.168.7.50'}}}
r = fn({'module': 'nft_v4', 'cfg': cfg})
chk('nft_v4 预览成功', r['ok'])
lines = [x for x in r['data']['text'].split('\n') if 'dnat' in x or '放通转发' in x]
for x in lines: print('    ', x.strip())
chk('含 dnat 规则', any('dnat to 192.168.7.10' in x for x in lines))
chk('含 DMZ 规则', any('DMZ' in x for x in lines))
chk('含放通规则', any('放通转发' in x for x in lines))

r2 = fn({'module': 'nft_v4', 'cfg': {'wan_iface': 'ppp0', 'lan_ifaces': ['ens18'],
    'portfwd': {'enable': True, 'rules': [{'name': 'x', 'proto': 'tcp', 'ext_port': 80,
    'int_ip': '1.2.3.999', 'int_port': 80}]}}})
chk('非法 IPv4 返回中文错误', (not r2['ok']) and 'IPv4' in r2['msg_cn'], '→ ' + r2['msg_cn'])

r3 = fn({'module': 'dnsmasq', 'cfg': {'lan_iface': 'ens18', 'domain': 'lan'}})
chk('dnsmasq 预览成功', r3['ok'] and len(r3['data']['text']) > 50,
    '长度 %s' % (len(r3['data']['text']) if r3['ok'] else '-'))

r4 = fn({'module': 'radvd', 'cfg': {'iface': 'ens18', 'prefix': '2408:8207:1234::/64'}})
chk('radvd 预览成功', r4['ok'] and r4['data']['text'])

r5 = fn({})
chk('空模块名被拒绝', (not r5['ok']) and r5['code'] == 'NO_MODULE', '→ ' + r5['msg_cn'])

r6 = fn({'module': 'nonsense'})
chk('未知模块返回中文错误', (not r6['ok']) and '未知' in r6['msg_cn'], '→ ' + r6['msg_cn'])

print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % fails))
sys.exit(1 if fails else 0)

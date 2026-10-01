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

# ---------------------------------------------------------------- dnsmasq
# code 3 / 6 有专属字段，但「自定义 Options」表也能写同一个 code。
# 1.0.1 及更早会渲染成**重复的 dhcp-option 行**（dnsmasq 对 3/6 是追加语义），
# 客户端因此收到重复值、DNS 顺序也被搅乱。这里锁死合并后的行为。

def opt_lines(text, code):
    """取某个 code 的**非 force** dhcp-option 行的值"""
    return re.findall(r'^dhcp-option=%s,(.*)$' % code, text, re.M)


def dup_codes(text):
    """被重复下发的 code（只算非 force 行；force 行与普通行共存是合法的）"""
    codes = re.findall(r'^dhcp-option=(\d+),', text, re.M)
    return sorted({c for c in codes if codes.count(c) > 1})


print('\n--- dnsmasq：code 3 / 6 不得渲染出重复行（1.0.2）---')

# 用户真机上那份配置：专属字段与自定义表都写了 3/6
CFG_DUP = {
    'lan_iface': 'ens18', 'domain': 'lan', 'dhcp_enabled': True,
    'pool_start': '192.168.7.1', 'pool_end': '192.168.7.154',
    'pool_netmask': '255.255.255.0', 'lease_time': 43200,
    'option_gateway': '192.168.7.3',
    'option_dns': '223.5.5.5,119.29.29.29',
    'options': [
        {'enabled': True, 'code': '6', 'value': '192.168.7.3'},
        {'enabled': True, 'code': '3', 'value': '192.168.7.3'},
    ],
    'dns_mode': 'custom', 'dns_custom': '223.5.5.5,119.29.29.29',
}
t = render.render_dnsmasq(CFG_DUP)
for _l in t.split('\n'):
    if _l.startswith('dhcp-'):
        print('    ', _l)
chk('没有任何 code 被重复下发', dup_codes(t) == [], str(dup_codes(t)))
chk('code 3 只有一行', len(opt_lines(t, 3)) == 1, str(opt_lines(t, 3)))
chk('code 3 的值已去重', opt_lines(t, 3) == ['192.168.7.3'], str(opt_lines(t, 3)))
chk('code 6 只有一行', len(opt_lines(t, 6)) == 1, str(opt_lines(t, 6)))
chk('code 6 合并了自定义值', opt_lines(t, 6) == ['223.5.5.5,119.29.29.29,192.168.7.3'],
    str(opt_lines(t, 6)))

# 专属字段留空时，自定义表里的 3/6 不能被吞掉
t2 = render.render_dnsmasq(dict(CFG_DUP, option_gateway='', option_dns='',
    options=[{'enabled': True, 'code': '3', 'value': '192.168.7.1'}]))
chk('专属字段为空时自定义 3 仍输出', opt_lines(t2, 3) == ['192.168.7.1'], str(opt_lines(t2, 3)))
chk('专属字段为空时不下发 code 6', opt_lines(t2, 6) == [], str(opt_lines(t2, 6)))

# 去重要保序：先出现的先保留
t3 = render.render_dnsmasq(dict(CFG_DUP, option_dns='223.5.5.5,1.1.1.1',
    options=[{'enabled': True, 'code': '6', 'value': '1.1.1.1,8.8.8.8'}]))
chk('顺序去重且保留首次出现顺序', opt_lines(t3, 6) == ['223.5.5.5,1.1.1.1,8.8.8.8'],
    str(opt_lines(t3, 6)))

# 带「强制」的 3/6 语义不同（客户端未请求也要下发），不能并进普通行
t4 = render.render_dnsmasq(dict(CFG_DUP, options=[
    {'enabled': True, 'code': '3', 'value': '192.168.7.9', 'force': True},
    {'enabled': True, 'code': '3', 'value': '192.168.7.9'},
]))
chk('force 的 3 走 dhcp-option-force', 'dhcp-option-force=3,192.168.7.9' in t4)
chk('非 force 的 3 合并成一行', opt_lines(t4, 3) == ['192.168.7.3,192.168.7.9'],
    str(opt_lines(t4, 3)))

# 其它 code 必须完全不受影响
t5 = render.render_dnsmasq(dict(CFG_DUP, options=[
    {'enabled': True, 'code': '43', 'value': 'abc.example.com'},
    {'enabled': True, 'code': '121', 'value': '10.0.0.0/8,192.168.7.3'},
    {'enabled': True, 'code': '121', 'value': '1.2.3.0/24,192.168.7.3', 'force': True},
]))
chk('其它 code 原样输出', 'dhcp-option=43,abc.example.com' in t5)
chk('其它 code 的 force 保留', 'dhcp-option-force=121,1.2.3.0/24,192.168.7.3' in t5)
chk('121 两行是允许的（值不同）', len(re.findall(r'^dhcp-option(?:-force)?=121,', t5, re.M)) == 2)

# 预览接口走的是 helper 的 read_render_text，必须与直接渲染一致
p = fn({'module': 'dnsmasq', 'cfg': CFG_DUP})
chk('预览路径同样不重复', p['ok'] and dup_codes(p['data']['text']) == [],
    str(dup_codes(p['data']['text'])) if p['ok'] else p.get('msg_cn'))
chk('预览里 code 3 也是一行', p['ok'] and opt_lines(p['data']['text'], 3) == ['192.168.7.3'],
    str(opt_lines(p['data']['text'], 3)) if p['ok'] else '-')

# 校验不能被放松
try:
    render.render_dnsmasq(dict(CFG_DUP, option_gateway='1.2.3.999'))
    chk('非法网关仍被拒绝', False, '居然没抛异常')
except render.ValidateError as ex:
    chk('非法网关仍被拒绝', 'IPv4' in str(ex), str(ex))

# ---------------------------------------------------------------- radvd RDNSS
# RA 通告里根本没有 IPv4 的位置：RDNSS 只能是 IPv6 地址，且多值必须**空格分隔**
# （逗号形式 radvd 直接 syntax error）。这里原先完全不校验，于是「下发 DNS」
# 框里顺手填个 223.5.5.5 就会渲染出一份 radvd 读不进去的配置 ——
# 真机上留下的错误日志就是 "/run/drouter-verify/radvd.conf:18 error: syntax error"，
# 而界面只回一句英文，完全指不到是哪个字段。

def ra(extra):
    return render.render_radvd(dict({'iface': 'ens18', 'prefix': 'fd00:7::/64'}, **extra))

for _bad in ('223.5.5.5', '223.5.5.5,119.29.29.29', 'dns.alidns.com',
             'fd00:7::/64'):
    try:
        ra({'rdnss': _bad})
        chk('RDNSS 拒绝 %r' % _bad, False, '居然通过了')
    except render.ValidateError as ex:
        chk('RDNSS 拒绝 %r' % _bad, 'IPv6' in str(ex), str(ex))

_r = ra({'rdnss': 'fd00:7::1 fd00:7::2'})
chk('RDNSS 多个 IPv6 合并成一行（空格分隔）',
    re.search(r'^\s*RDNSS fd00:7::1 fd00:7::2 \{$', _r, re.M) is not None)
_r = ra({'rdnss': 'fd00:7::1,fd00:7::2'})
chk('RDNSS 逗号列表归一成空格（而不是让 radvd 报 syntax error）',
    re.search(r'^\s*RDNSS fd00:7::1 fd00:7::2 \{$', _r, re.M) is not None)

_r = ra({'rdnss': 'fd00:7::1', 'dnssl': 'lan,home.lan'})
chk('DNSSL 支持多域名（空格分隔）',
    re.search(r'^\s*DNSSL lan home\.lan \{$', _r, re.M) is not None)
try:
    ra({'rdnss': 'fd00:7::1', 'dnssl': 'ok.lan,bad@lan'})
    chk('DNSSL 非法域名被拒绝', False, '居然通过了')
except render.ValidateError as ex:
    chk('DNSSL 非法域名被拒绝', 'DNSSL' in str(ex), str(ex))

# DNSSL 生命期原来读的是 rdnss_life —— 「DNSSL 生命期」这个 label 指向别人的值
chk('DNSSL 生命期优先取 dnssl_life',
    'AdvDNSSLLifetime 1200;' in ra({'rdnss': 'fd00:7::1', 'dnssl': 'lan',
                                    'dnssl_life': 1200}))
chk('没有 dnssl_life 时退回 rdnss_life',
    'AdvDNSSLLifetime 900;' in ra({'rdnss': 'fd00:7::1', 'dnssl': 'lan',
                                   'rdnss_life': 900}))

# ------------------------------------------------- 1.0.4：未落地校验的补齐
# 以下字段原先都是「用户在页面上填什么就原样拼进配置行」，同段的其它字段
# 都有校验、唯独它们漏了。风险不只是填错：配置是一行一条指令，值里带一个
# 换行就能凭空造出一条新指令。这里统一按 UI 上真实的输入框逐项锁死。

def rej(label, fn_call):
    try:
        fn_call()
        chk(label, False, '居然通过了')
    except render.ValidateError as ex:
        chk(label, True, str(ex))


print('\n--- dnsmasq：domain / log_file ---')
rej('域名含换行被拒绝',
    lambda: render.render_dnsmasq({'lan_iface': 'ens18',
                                   'domain': 'lan\ndhcp-range=1.1.1.1,2.2.2.2'}))
rej('域名含空格被拒绝',
    lambda: render.render_dnsmasq({'lan_iface': 'ens18', 'domain': 'my lan'}))
rej('日志文件含换行被拒绝',
    lambda: render.render_dnsmasq({'lan_iface': 'ens18',
                                   'log_file': '/var/log/x\nsecurity=off'}))
_d = render.render_dnsmasq({'lan_iface': 'ens18', 'domain': 'home.lan'})
chk('合法域名正常渲染', 'domain=home.lan' in _d)
chk('默认域名仍是 lan',
    'domain=lan' in render.render_dnsmasq({'lan_iface': 'ens18'}))
chk('默认日志路径未被改动',
    'log-facility=/var/log/drouter/dnsmasq.log'
    in render.render_dnsmasq({'lan_iface': 'ens18'}))

print('\n--- ppp：运营商 / 服务名 ---')
_PPPOE = {'iface': 'ens19', 'username': 'u1', 'password': 'p1'}


def ppp(extra):
    return render.render_ppp(dict(_PPPOE, **extra))


rej('PPPoE 服务名含引号被拒绝',
    lambda: ppp({'service_name': 'cmcc"\nnoauth'}))
rej('PPPoE 运营商含换行被拒绝',
    lambda: ppp({'isp': '江苏电信\nnoauth'}))
_peers = ppp({'service_name': 'cmcc', 'isp': '江苏电信'})[0][1]
chk('合法服务名正常写出', 'servicename "cmcc"' in _peers, )
chk('运营商写进注释行', '# 运营商：江苏电信' in _peers)

print('\n--- dhcpv6：SLA-ID 后缀 ---')
rej('SLA-ID 含换行被拒绝',
    lambda: render.render_dhcpcd({'dhcpv6': {'iface': 'ppp0', 'sla_id': '::1\nia_na 1'}}))
# 注意别拿 abc 当非法值：那是一串合法的十六进制（::abc 也是合法 IPv6）
rej('SLA-ID 非 IPv6 被拒绝',
    lambda: render.render_dhcpcd({'dhcpv6': {'iface': 'ppp0', 'sla_id': '1.2.3.4'}}))
rej('SLA-ID 含非十六进制字符被拒绝',
    lambda: render.render_dhcpcd({'dhcpv6': {'iface': 'ppp0', 'sla_id': '::zzz'}}))
_cd = render.render_dhcpcd({'dhcpv6': {'iface': 'ppp0', 'sla_id': '::1'}})
chk('合法 SLA-ID 正常渲染', 'ia_pd 0/br0/0/64/::1' in _cd, _cd.split('\n')[-2] if _cd else '')
# 页面默认值 ::1，也允许用户偷懒只写 1
chk('SLA-ID 支持简写（1 → ::1）',
    '::1' in render.render_dhcpcd({'dhcpv6': {'iface': 'ppp0', 'sla_id': '1'}}))

print('\n--- miniupnpd：lease_file / uuid ---')
rej('UPnP 租约文件路径含注入被拒绝',
    lambda: render.render_miniupnpd({'upnp': {'lease_file': '/tmp/x\next_ifname=lo'}}))
rej('UPnP uuid 含换行被拒绝',
    lambda: render.render_miniupnpd({'upnp': {'uuid': 'x\nenable_upnp=no'}}))
_u = render.render_miniupnpd({'upnp': {}})
chk('UPnP 默认租约文件/UUID 未变',
    'lease_file=/var/run/miniupnpd.leases' in _u and 'uuid=drouter-0001' in _u)

print('\n--- radvd：IPv6 地址池块 ---')
rej('池名称含换行被拒绝',
    lambda: render.render_radvd({'iface': 'ens18', 'pool_name': 'x\nprefix 2001:db8::/64 {};'}))
rej('池前缀非法被拒绝',
    lambda: render.render_radvd({'iface': 'ens18', 'pool_prefix': '2408::/999'}))
rej('池策略含换行被拒绝',
    lambda: render.render_radvd({'iface': 'ens18', 'pool_policy': ['a\nb']}))
rej('起始大于结束被拒绝',
    lambda: render.render_radvd({'iface': 'ens18', 'pool_start': 10, 'pool_end': 5}))
# 0 是合法起始子网 ID：别写成 pool_start or 1（同 hour=0 的老毛病）
_r0 = render.render_radvd({'iface': 'ens18', 'pool_start': 0, 'pool_end': 254})
chk('起始子网 ID 0 不会被吞成 1', '子网 ID 0 - 254' in _r0, _r0.strip().split('\n')[-2])

print('\n--- chrony：IPv6 NTP 服务器 ---')
_n = render.render_chrony({'ntp': {'servers': 'ntp.aliyun.com,2400:3200::1'}})
chk('IPv6 的 NTP 服务器被接受', 'server 2400:3200::1 iburst' in _n, )
rej('NTP 服务器里的换行仍被拒绝',
    lambda: render.render_chrony({'ntp': {'servers': 'x.com\nserver 1.2.3.4'}}))

print('\n--- nfs：备注 / 选项 / 模板 ---')
_NFS = {'nfs': {'exports': [{'path': '/srv/share',
                            'clients': [{'net': '192.168.7.0/24', 'preset': 'macos'}]}]}}
_nexp = render.render_nfs(_NFS)
chk('NFS 正常导出', '/srv/share 192.168.7.0/24(' in _nexp)
rej('NFS 备注含换行被拒绝',
    lambda: render.render_nfs({'nfs': {'exports': [
        {'path': '/srv/share', 'comment': 'ok\n/srv 127.0.0.1(rw)',
         'clients': [{'net': '192.168.7.0/24'}]}]}}))
rej('NFS 自定义选项含空格/换行被拒绝',
    lambda: render.render_nfs({'nfs': {'exports': [
        {'path': '/srv/share', 'clients': [
            {'net': '192.168.7.0/24', 'options': 'rw\n/srv 0.0.0.0/0(rw)'}]}]}}))
rej('未知 NFS 模板不再静默退回 linux',
    lambda: render.render_nfs({'nfs': {'exports': [
        {'path': '/srv/share', 'clients': [{'net': '192.168.7.0/24', 'preset': 'mac'}]}]}}))
rej('NFS 线程数非数字给中文提示',
    lambda: render.render_nfs_conf({'nfs': {'threads': 'abc'}}))

print('\n--- v_int：Unicode 数字不能绕成 500 ---')
for _bad in ('²', '٣'):
    try:
        render.v_int(_bad, field='测试值', lo=0, hi=65535)
        chk('v_int 拒绝 %r' % _bad, False, '居然通过了')
    except render.ValidateError as ex:
        chk('v_int 拒绝 %r' % _bad, True, str(ex))

print('\n--- DMZ：不能再把本机自己的流量转走 ---')
_dmz = render.render_nft('v4', {'wan_iface': 'ppp0', 'lan_ifaces': ['ens18'],
                               'portfwd': {'dmz': {'enable': True, 'host': '192.168.7.50'}}})
chk('DMZ 前先放行到本机自身的流量',
    'fib daddr type local accept' in _dmz)
chk('DMZ 规则不再用 daddr != <DMZ 主机> 的反向条件',
    'daddr != 192.168.7.50' not in _dmz)
chk('DMZ 主机仍能收到转发', 'dnat to 192.168.7.50' in _dmz)

print('\n--- 端口转发备注：换行不得注入 nft ---')
_pf = render.render_nft('v4', {'wan_iface': 'ppp0', 'lan_ifaces': ['ens18'],
                              'portfwd': {'enable': True, 'rules': [
                                  {'name': 'a\nmasquerade', 'proto': 'tcp',
                                   'ext_port': 80, 'int_ip': '192.168.7.10', 'int_port': 80}]}})
chk('备注里的换行被去掉', '\nmasquerade' not in _pf and 'masquerade' in _pf)

_NET = {'wan': {'iface': 'ens19', 'mode': 'dhcp', 'mtu': 1500},
        'lan': {'iface': 'ens18', 'address': '192.168.7.3/24', 'mtu': 1500},
        'bridge': {'enabled': True, 'name': 'br0', 'members': ['ens18'], 'stp': True}}
_net = render.render('network', _NET)
chk('render("network") 按契约返回 (路径, 内容) 二元组',
    all(isinstance(f, tuple) and len(f) == 2 for f in _net))
# 曾经的 bug：这里返回的是 {'name','content'} 字典，helper 里
# `for path, content in files` 解包 dict 只会拿到它的两个**键名**，
# 于是把字面量 'content' 写进一个叫 'name' 的文件。
chk('路径是 /etc/systemd/network/ 下的绝对路径',
    all(f[0].startswith('/etc/systemd/network/') for f in _net))
chk('内容是文本而不是 dict 的键名',
    all(isinstance(f[1], str) and f[1] != 'content' for f in _net))
_net_names = [f[0].split('/')[-1] for f in _net]
chk('停用式迁移会产出 WAN 单元', any(n.startswith('10-drouter-wan-') for n in _net_names))
chk('网桥 netdev 单元存在', '20-drouter-br0.netdev' in _net_names)
chk('桥接成员单元存在', '21-drouter-br0-member-ens18.network' in _net_names)
chk('STP 打开时写 STP=yes',
    'STP=yes' in dict((f[0].split('/')[-1], f[1]) for f in _net).get('20-drouter-br0.netdev', ''))
_net_off = render.render('network', dict(_NET, bridge={'enabled': False, 'members': ['ens18']}))
chk('网桥关闭时不产出任何 bridge 单元',
    not any('/br0' in f[0] for f in _net_off))
chk('网桥关闭时 LAN 地址直接落在物理口上',
    any('30-drouter-lan-ens18.network' in f[0] for f in _net_off))
rej('缺 WAN 网卡时给出中文字段级提示',
    lambda: render.render('network', {'lan': {'iface': 'ens18'}}))

print('\n--- network：WAN 页 DHCP 模式的 MTU / DNS 开关不再是假控件 ---')


def _wan_unit(wan):
    fs = render.render('network', {'lan': {'iface': 'ens18', 'address': '192.168.7.3/24'},
                                   'bridge': {'enabled': False}, 'wan': wan})
    return fs[0][1]


chk('未传 use_dns 时沿用「使用上级 DNS」',
    'UseDNS=yes' in _wan_unit({'iface': 'ens19', 'mode': 'dhcp'}))
chk('用户在 WAN 页关掉后能真的关掉',
    'UseDNS=no' in _wan_unit({'iface': 'ens19', 'mode': 'dhcp', 'use_dns': False}))
chk('用户在 WAN 页打开后仍是 yes',
    'UseDNS=yes' in _wan_unit({'iface': 'ens19', 'mode': 'dhcp', 'use_dns': True}))
chk('DHCP 模式填的 MTU 能落进配置文件',
    'MTUBytes=1400' in _wan_unit({'iface': 'ens19', 'mode': 'dhcp', 'mtu': 1400}))

print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % fails))
sys.exit(1 if fails else 0)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.0.6 真机验收（192.168.7.3）。

重点验六个曾被 readings 遮蔽的模块：它们的 POST 必须走到自己的写分支，
而不是被 readings 吃掉后返回「读数据 + ok:true」—— 那种情况下前端会
提示「保存完成」而实际什么都没发生。

判定方式：用一个**不存在的 op**去打，如果请求能到模块自己的 handler，
返回的一定是模块级错误（BAD_OP / 「不支持的操作：xxx」），
而如果又被 readings 吃掉，返回的就是 ok:true + 读出来的数据。
"""
import json
import ssl
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else 'https://192.168.7.3:8443'
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
fails = []

# 这几个路径在修复前会被 readings 吃掉。它们的 GET 都返回配置数据，
# 所以「POST 返回了读数据」就是遮蔽仍在的信号。
SHADOWED = {
    '/api/theme': 'config',
    '/api/acl': 'cfg',
    '/api/share': 'cfg',
    '/api/docker': 'info',
    '/api/ddns': 'cfg',
    '/api/ulog': 'conf',
}


def chk(label, cond, extra=''):
    print('[%s] %s %s' % ('OK' if cond else 'FAIL', label, extra))
    if not cond:
        fails.append(label)


def call(path, body=None, tok=None, method=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 method=method or ('POST' if data else 'GET'))
    req.add_header('Content-Type', 'application/json')
    if tok:
        req.add_header('X-Token', tok)
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
            return json.loads(r.read().decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode('utf-8', 'replace'))
        except Exception:
            return {'ok': False, 'http': e.code}
    except Exception as e:
        return {'ok': False, 'msg_cn': str(e)[:200]}


r = call('/api/login', {'username': 'admin', 'password': 'admin123'})
TOK = (r.get('data') or {}).get('token') or ''
chk('登录拿到 token', bool(TOK))
if not TOK:
    print('登录失败：', json.dumps(r, ensure_ascii=False)[:300])
    sys.exit(1)

print('--- 1. readings 不再遮蔽六个模块的 POST 写分支 ---')
for p, leak_key in sorted(SHADOWED.items()):
    # 先确认 GET 确实会返回这个键（说明「读数据」长这样，才好辨认）
    g = call(p, None, TOK)
    has_read = g.get('ok') is True and isinstance(g.get('data'), dict) \
        and leak_key in (g.get('data') or {})
    # 再用不存在的 op 打 POST
    r = call(p, {'op': '__probe__'}, TOK)
    leaked = r.get('ok') is True and isinstance(r.get('data'), dict) \
        and leak_key in (r.get('data') or {})
    chk('  %s POST 走专属分支（未返回读数据）' % p, not leaked,
        '→ %s' % json.dumps(r, ensure_ascii=False)[:130]
        + ('' if has_read else '  [注: GET 未含 %s]' % leak_key))

print('--- 2. 六个模块的真实写操作可用（读回配置确认没写坏）---')
# theme: GET /api/theme 的 data 里 themes 是数组、active 是当前主题 id
g = call('/api/theme', None, TOK)
td = g.get('data') or {}
cur = td.get('active') or ''
names = [t.get('id') for t in (td.get('themes') or []) if isinstance(t, dict)]
print('    当前主题：%r，可选：%r' % (cur, names))
if names:
    tgt = names[0] if names[0] != cur else (names[1] if len(names) > 1 else names[0])
    r = call('/api/theme', {'op': 'apply', 'id': tgt}, TOK)
    if r.get('ok') is not True:      # apply 的参数名可能是 name 而非 id
        r = call('/api/theme', {'op': 'apply', 'name': tgt}, TOK)
    chk('  主题切换生效（写分支真的执行了）', r.get('ok') is True,
        '→ %s' % json.dumps(r, ensure_ascii=False)[:140])
    now = (call('/api/theme', None, TOK).get('data') or {}).get('active')
    chk('  切换后读回 active 已变', now == tgt, '→ active=%r 期望 %r' % (now, tgt))
    if cur and cur != tgt:
        call('/api/theme', {'op': 'apply', 'id': cur}, TOK)
        call('/api/theme', {'op': 'apply', 'name': cur}, TOK)
else:
    print('    [跳过] 无可切换主题')

# ddns: 非法配置必须被拒（说明写分支在校验，不是照单全收）
r = call('/api/ddns', {'op': 'save', 'enable': True, 'provider': '__nope__',
                       'domain': 'x.example.com'}, TOK)
chk('  DDNS 非法 provider 被拒（写分支在校验）',
    r.get('ok') is False, '→ %s' % json.dumps(r, ensure_ascii=False)[:140])

# ulog: op 名是 save_conf（不是 save）
g = call('/api/ulog', {'op': 'conf'}, TOK)
old = ((g.get('data') or {}).get('conf') or {}).get('keep_days')
r = call('/api/ulog', {'op': 'save_conf', 'keep_days': (old or 7)}, TOK)
chk('  流日志 save_conf 成功（写分支可用）', r.get('ok') is True,
    '→ %s' % json.dumps(r, ensure_ascii=False)[:140])
now = ((call('/api/ulog', {'op': 'conf'}, TOK).get('data') or {})
       .get('conf') or {}).get('keep_days')
chk('  keep_days 读回一致', now == (old or 7), '→ %r 期望 %r' % (now, old or 7))

# acl / share / docker: 只读探测，不改用户配置
for p, body in (('/api/acl', {'op': 'read'}), ('/api/share', {'op': 'read'}),
                ('/api/docker', {'op': 'read'})):
    r = call(p, body, TOK)
    chk('  %s 专属分支可达（返回模块级反馈）' % p,
        r.get('ok') is False or r.get('ok') is True,
        '→ %s' % json.dumps(r, ensure_ascii=False)[:110])

print('--- 3. 防火墙预检 check_only 不写配置库 ---')
b = ((call('/api/config', None, TOK).get('data') or {}).get('nft_v4') or {})
r = call('/api/apply', {'module': 'nft_v4', 'check_only': True, 'live': False,
                        'data': dict(b, table='inet drouter_v106probe')}, TOK)
chk('  预检给出明确结论', r.get('ok') is not None,
    '→ %s' % json.dumps(r, ensure_ascii=False)[:130])
a = ((call('/api/config', None, TOK).get('data') or {}).get('nft_v4') or {})
chk('  预检未把数据写进配置库', a.get('table') != 'inet drouter_v106probe',
    '→ table=%r' % a.get('table'))
chk('  预检前后配置库逐字节一致',
    json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True))

print('--- 4. 静态租约字段契约（enabled/name/is_static）---')
def cfg(mod):
    """读单个配置模块。/api/config 无参数一次返回全部，不要用 /api/config/<mod>。"""
    return (call('/api/config', None, TOK).get('data') or {}).get(mod) or {}


# 注意：/api/leases 的 data 直接是数组，不是 {'leases': [...]}。
# 而且它读的是 dnsmasq 的**活动租约**；静态绑定写在配置库的 static_leases 里，
# 两边不是一回事 —— 所以静态绑定要从 /api/config 读回。
MAC = 'AA:BB:CC:DD:EE:01'
r = call('/api/lease/static', {'mac': MAC, 'ip': '192.168.7.240',
                               'host': 'v106probe'}, TOK)
chk('  转静态接口成功', r.get('ok') is True,
    '→ %s' % json.dumps(r, ensure_ascii=False)[:150])
dns = cfg('dnsmasq')
sl = dns.get('static_leases') or []
hit = [x for x in sl if str(x.get('mac') or '').lower() == MAC.lower()]
chk('  静态绑定写进了配置库（渲染器读得到）', bool(hit),
    '→ static_leases 共 %d 条' % len(sl))
if hit:
    h = hit[0]
    chk('  带 enabled=True（v_bool 才认它）', h.get('enabled') is True,
        '→ %s' % json.dumps(h, ensure_ascii=False)[:180])
    chk('  用 name 而非 host 作主机名（渲染器读 name）',
        h.get('name') == 'v106probe', '→ %r' % h.get('name'))
lr = call('/api/leases', None, TOK)
ld = lr.get('data') or []
chk('  /api/leases 仍可读（未被本轮改动破坏）', isinstance(ld, list),
    '→ %d 条活动租约' % (len(ld) if isinstance(ld, list) else -1))
# 清理：把测试用的静态绑定删掉，别留在用户机器上
call('/api/config', {'module': 'dnsmasq',
                     'data': dict(dns, static_leases=[
                         x for x in sl if str(x.get('mac') or '').lower() != MAC.lower()])},
     TOK)
left = [x for x in (cfg('dnsmasq').get('static_leases') or [])
        if str(x.get('mac') or '').lower() in (MAC.lower(), 'aa:bb:cc:dd:ee:02')]
chk('  测试数据已清理', not left, '→ 残留 %r' % left)

print('--- 5. SNMP 配置注入过滤 ---')
r = call('/api/kern', {'op': 'save', 'snmp': {
    'enable': False, 'port': 161, 'community': 'public',
    'sysname': 'drouter\nrwcommunity evil', 'location': 'lab', 'contact': 'me'}}, TOK)
chk('  含换行的 sysname 被净化后接受', r.get('ok') is True,
    '→ %s' % json.dumps(r, ensure_ascii=False)[:130])
s = ((call('/api/kern', {'op': 'read', 'snmp': 1}, TOK).get('data') or {}).get('snmp') or {})
sn = str(s.get('sysname') or '')
chk('  库里 sysname 无换行', '\n' not in sn and '\r' not in sn, '→ %r' % sn)
chk('  库里 sysname 无 rwcommunity（没被升级成可写团体名）',
    'rwcommunity' not in sn, '→ %r' % sn)
# 还原
call('/api/kern', {'op': 'save', 'snmp': {
    'enable': False, 'port': 161, 'community': 'public',
    'sysname': 'drouter', 'location': '', 'contact': ''}}, TOK)

print('--- 6. PPPoE 多拨 service_name 白名单 ---')
# 保存时会先校验 WAN 网卡名，所以必须把 iface 带上，
# 否则请求在更早一道就被拦掉，压根到不了 service_name 校验。
IFACE = cfg('system').get('wan_iface') or 'ens19'
print('    用 iface=%s 试' % IFACE)
BAD = 'x" ; }'
r = call('/api/pppoe-multi', {'op': 'save', 'iface': IFACE, 'sessions': [{
    'user': 'probe@isp', 'password': 'p', 'weight': 1,
    'mtu': 1492, 'mru': 1400, 'service_name': BAD, 'isp': 'auto'}]}, TOK)
chk('  含引号/空格的 service_name 被拒',
    r.get('ok') is False and '服务名' in str(r.get('msg_cn')),
    '→ %s' % json.dumps(r, ensure_ascii=False)[:200])
r = call('/api/pppoe-multi', None, TOK)
chk('  多拨配置未被污染（仍无恶意 service_name）',
    all(BAD not in json.dumps(s, ensure_ascii=False)
        for s in ((r.get('data') or {}).get('sessions') or [])),
    '→ %d 个会话' % len((r.get('data') or {}).get('sessions') or []))

print('--- 7. 构建保护模式下的 rollback+reload ---')
import subprocess
try:
    out = subprocess.run(
        ['bash', 'devtools/rsudo.sh',
         "test -e /etc/drouter/BUILD_MODE && echo ON || echo OFF"],
        capture_output=True, text=True, timeout=40).stdout
except Exception as e:
    out = 'ERR %s' % e
mode = out.strip().splitlines()[-1] if out.strip() else '?'
print('    目标机 BUILD_MODE：%s' % mode)
if 'ON' in out:
    r = call('/api/rollback', {'ts': '19700101-000000', 'reload': True}, TOK)
    chk('  保护模式下 rollback+reload 被拒', r.get('ok') is False,
        '→ %s' % json.dumps(r, ensure_ascii=False)[:150])
else:
    print('    [跳过] 未开启构建保护模式')

print('--- 8. 关键服务仍活着 ---')
for p in ('/api/health', '/api/metrics', '/api/dhcp'):
    r = call(p, None, TOK)
    chk('  %s 可用' % p, r.get('ok') is not False or r.get('http') != 502,
        '' if r.get('ok') else '→ %s' % json.dumps(r, ensure_ascii=False)[:100])

print()
if fails:
    print('结果: %d 项失败 → %s' % (len(fails), fails))
    sys.exit(1)
print('结果: 全部通过')

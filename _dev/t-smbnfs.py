# -*- coding: utf-8 -*-
"""验证 SMB / NFS 渲染器（#9）。纯逻辑，可直接 import render.py。"""
import sys, io, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'backend'))
import render as R  # noqa: E402

fails = 0


def chk(name, got, want):
    global fails
    ok = (got == want)
    if not ok:
        fails += 1
    print('[%s] %-40s got=%-24s want=%s' % ('OK' if ok else 'FAIL', name, repr(got), repr(want)))


def has(name, needle, hay, want=True):
    chk(name, needle in hay, want)


print('=== SMB：基础全局段 ===')
cfg = {'samba': {
    'workgroup': 'workgroup', 'server_string': 'drouter 文件共享',
    'disable_netbios': True, 'interfaces': 'br0',
    'shares': [
        {'name': 'Public', 'path': '/srv/share/public', 'enable': True, 'mode': 'public',
         'writable': True, 'comment': '公共盘'},
        {'name': 'Media', 'path': '/srv/share/media', 'enable': True, 'mode': 'auth',
         'writable': False, 'users': 'drouter, alice',
         'allow_hosts': '192.168.7.0/24'},
    ]}}
conf = R.render_samba(cfg)
has('工作组大写化', 'workgroup = WORKGROUP', conf)
has('最小协议 SMB2', 'server min protocol = SMB2', conf)
has('最大协议 SMB3', 'server max protocol = SMB3', conf)
has('UTF-8 字符集', 'unix charset = UTF-8', conf)
has('关闭 NetBIOS', 'disable netbios = yes', conf)
has('绑定网卡', 'bind interfaces only = yes', conf)
has('公共盘 guest ok', 'guest ok = yes', conf)
has('公共盘可写', 'read only = no', conf)
has('公共盘 force user', 'force user = nobody', conf)
has('认证盘只读', '[Media]', conf)
has('认证盘 valid users', 'valid users = drouter, alice', conf)
has('认证盘 hosts allow', 'hosts allow = 192.168.7.0/24', conf)
has('macOS 支持 fruit', 'vfs objects = catia fruit streams_xattr', conf)

print('\n=== SMB：校验失败场景 ===')
for label, bad, kw in [
    ('空共享名', {'samba': {'shares': [{'name': '', 'path': '/srv/x'}]}}, '共享缺少名称'),
    ('非法共享名', {'samba': {'shares': [{'name': 'a/b', 'path': '/srv/x'}]}}, '不合法'),
    ('相对路径', {'samba': {'shares': [{'name': 'ok', 'path': 'srv/x'}]}}, '绝对路径'),
    ('目录穿越', {'samba': {'shares': [{'name': 'ok', 'path': '/srv/../etc'}]}}, '..'),
    ('非法工作组', {'samba': {'workgroup': '中文组', 'shares': []}}, '工作组名不合法'),
    ('非法用户名', {'samba': {'shares': [{'name': 'ok', 'path': '/srv/x', 'mode': 'auth',
                                          'users': 'ROOT'}]}}, '不合规'),
    ('非法网段', {'samba': {'shares': [{'name': 'ok', 'path': '/srv/x',
                                        'allow_hosts': '999.1.1.0/24'}]}}, '允许网段不合法'),
]:
    try:
        R.render_samba(bad)
        chk(label, '未抛错', '应抛 ValidateError')
    except R.ValidateError as e:
        chk(label, kw in e.msg_cn, True)

print('\n=== SMB：共享名去重 ===')
try:
    R.render_samba({'samba': {'shares': [
        {'name': 'Same', 'path': '/srv/a'}, {'name': 'same', 'path': '/srv/b'}]}})
    chk('共享名重复', '未抛错', '应抛 ValidateError')
except R.ValidateError as e:
    chk('共享名重复', '重复' in e.msg_cn, True)

print('\n=== NFS：多平台模板 ===')
ncfg = {'nfs': {'threads': 16, 'exports': [
    {'path': '/srv/share/public', 'enable': True, 'comment': '公共盘', 'clients': [
        {'net': '192.168.7.0/24', 'preset': 'linux'},
        {'net': '192.168.7.128/25', 'preset': 'macos'},
    ]},
    {'path': '/srv/share/ro', 'enable': True, 'clients': [
        {'net': '192.168.7.0/24', 'preset': 'readonly'}]},
]}}
exp = R.render_nfs(ncfg)
has('导出公共盘', '/srv/share/public 192.168.7.0/24(', exp)
has('linux 模板 root_squash', 'root_squash', exp)
has('macos 模板 all_squash', 'all_squash', exp)
has('macos 模板 insecure', 'insecure', exp)
has('只读模板 ro', 'ro,', exp)

nsconf = R.render_nfs_conf(ncfg)
has('线程数生效', 'threads=16', nsconf)
has('固定 2049 端口', 'port=2049', nsconf)
has('mountd 固定端口', 'port=20048', nsconf)
has('NFSv4.2 开启', 'vers4.2=y', nsconf)

print('\n=== NFS：IPv6 客户端 ===')
exp6 = R.render_nfs({'nfs': {'exports': [
    {'path': '/srv/v6', 'clients': [{'net': '2408:8207:1234::/64', 'preset': 'linux'}]}]}})
has('IPv6 CIDR 导出', '2408:8207:1234::/64(', exp6)

print('\n=== NFS：通配符与校验 ===')
expw = R.render_nfs({'nfs': {'exports': [
    {'path': '/srv/all', 'clients': [{'net': '*', 'preset': 'readonly'}]}]}})
has('通配符 *', '*(ro,', expw)

for label, bad, kw in [
    ('无客户端', {'nfs': {'exports': [{'path': '/srv/x', 'clients': []}]}}, '至少需要一个客户端'),
    ('非法客户端', {'nfs': {'exports': [{'path': '/srv/x',
                                        'clients': [{'net': 'abc'}]}]}}, '客户端不合法'),
    ('非法线程数', {'nfs': {'threads': 999, 'exports': []}}, '1–128'),
    ('危险 no_root_squash', {'nfs': {'exports': [{'path': '/srv/x', 'clients': [
        {'net': '192.168.7.0/24', 'preset': 'linux',
         'options': 'rw,no_root_squash'}]}]}}, 'no_root_squash'),
]:
    try:
        if 'threads' in bad.get('nfs', {}):
            R.render_nfs_conf(bad)
        else:
            R.render_nfs(bad)
        chk(label, '未抛错', '应抛 ValidateError')
    except R.ValidateError as e:
        chk(label, kw in e.msg_cn, True)

print('\n=== 空导出 ===')
empty = R.render_nfs({'nfs': {'exports': []}})
has('空导出有说明', '未配置任何导出', empty)

print('\n=== 渲染入口注册 ===')
chk('samba 已注册', 'samba' in R.RENDERERS, True)
chk('nfs 已注册', 'nfs' in R.RENDERERS, True)
paths = [p for p, _ in R.render('smb', {'samba': {'shares': [
    {'name': 'T', 'path': '/srv/t'}]}})]
chk('smb 别名 → 落盘路径', paths[0], '/etc/samba/drouter.conf')
npaths = [p for p, _ in R.render('nfs', ncfg)]
chk('nfs 落盘两处', len(npaths), 2)
chk('nfs exports 路径', npaths[0], '/etc/exports')

print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % fails))
sys.exit(1 if fails else 0)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AC / AP 管理中心 OpenSOHO（#10）：静态契约 + 纯函数 + 安装/卸载逻辑单测。

盯五件事：
  ① 两把密钥的分工不能搞混 —— 共享密钥改了 AP 要重注册（可接受），
     加密密钥改了历史配置直接解不开（不可接受）。所以升级、改端口、改开关
     都绝不能重新生成加密密钥，只有卸载清除才动它；
  ② 解包必须挡住 zip slip —— 压缩包是外部下载的，成员名带 ../ 不能落到
     目标目录之外；
  ③ 端口钳制不能把 0 当成「没填」—— int(x or 8090) 的 falsy 陷阱；
  ④ 构建保护模式下 install / svc / save 只写盘不启服务；
  ⑤ 不碰安全红线：5900、dnsmasq、nft、默认路由。
"""
import ast
import io
import os
import re
import sys
import json
import shutil
import stat
import zipfile
import tempfile
import urllib
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(ROOT, 'backend', 'drouter-helper.py')
WEB = os.path.join(ROOT, 'backend', 'drouter-web.py')
APP = os.path.join(ROOT, 'web', 'app.js')

PASS = FAIL = 0


def chk(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('[OK]   %s %s' % (name, extra))
    else:
        FAIL += 1
        print('[FAIL] %s %s' % (name, extra))


HELPER_SRC = io.open(HELPER, encoding='utf-8').read()
WEB_SRC = io.open(WEB, encoding='utf-8').read()
APP_SRC = io.open(APP, encoding='utf-8').read()
TREE = ast.parse(HELPER_SRC)


def node(name):
    for n in TREE.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return n
                # 元组解包赋值（如 `A, B = 1, 2`）：目标名散在 Tuple 里
                if isinstance(t, ast.Tuple):
                    for e in t.elts:
                        if isinstance(e, ast.Name) and e.id == name:
                            return n
        elif isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def build(names, ns=None):
    ns = ns if ns is not None else {}
    body = []
    for nm in names:
        n = node(nm)
        if n is None:
            raise AssertionError('源码里找不到 %s' % nm)
        body.append(n)
    mod = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, '<opensoho>', 'exec'), ns)
    return ns


# ---------------------------------------------------------------- 测试替身
CALLS = []
ENVS = []
CWDS = []
SH_OUT = {}
URLS = []
HTTP_BODY = {}
BUILD_MODE = [False]
OPENED = []


def fake_sh(argv, timeout=10, input_data=None, cwd=None, env=None):
    CALLS.append(list(argv))
    ENVS.append(dict(env or {}))
    CWDS.append(cwd)
    a = list(argv)
    if a[:2] == ['uname', '-m']:
        return (0, SH_OUT.get('uname', 'x86_64'), '')
    if a[:2] == ['systemctl', 'is-active']:
        return (0, SH_OUT.get('active', 'inactive'), '')
    if a[:2] == ['systemctl', 'is-enabled']:
        return (0, SH_OUT.get('enabled', 'disabled'), '')
    if a[0] == 'ip':
        return (0, SH_OUT.get('ip_out', ''), '')
    if a[0] == 'ss':
        return (0, SH_OUT.get('ss_out', ''), '')
    if a[0] == 'id':
        return (0, '', '')
    if len(a) >= 2 and a[1] == '-v':
        return (0, SH_OUT.get('ver', 'opensoho version 0.15.2'), '')
    return (0, 'ok', '')


def _ok(data=None, msg='ok', code='OK'):
    return {'ok': True, 'msg_cn': msg, 'code': code, 'data': data or {}}


def _fail(msg, code='ERR', data=None):
    return {'ok': False, 'msg_cn': msg, 'code': code, 'data': data or {}}


def _log(*a, **k):
    return None


def _in_build_mode():
    return BUILD_MODE[0]


class _FakeResp(object):
    def __init__(self, body):
        self._b = body if isinstance(body, bytes) else body.encode()

    def read(self, n=None):
        return self._b if n is None else self._b[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeRequestMod(object):
    @staticmethod
    def Request(url, data=None, headers=None, method=None):
        return (url, data, headers, method)

    @staticmethod
    def urlopen(arg, timeout=None, context=None):
        u = arg[0] if isinstance(arg, tuple) else arg
        URLS.append(u)
        body = HTTP_BODY.get(u)
        if body is None:
            raise IOError('stub: no body for %s' % u)
        return _FakeResp(body)


class _ShimURLLib(object):
    request = _FakeRequestMod


# zipfile 已经在文件头真实导入过了，这里替换 sys.modules 不会影响它
_real_urllib = sys.modules.get('urllib')
sys.modules['urllib'] = _ShimURLLib


def _fake_open(path, mode='r', *a, **k):
    OPENED.append(path)
    return io.open(path, mode, *a, **k)


# Windows 的 os.chmod 只认「只读」那一位，0600 / 0755 都落不下去。
# 真机是 Linux，所以这里不能把「位不对」当成源码 bug —— 落盘校验只在 POSIX 上做，
# 本机改为静态校验源码里确实写了正确的 mode。
IS_WIN = (os.name == 'nt')


def perm_ok(path, want):
    if IS_WIN:
        return True
    return (os.stat(path).st_mode & 0o777) == want


def mode_str(path):
    return oct(os.stat(path).st_mode & 0o777)


# ---------------------------------------------------------------- 构造命名空间
TMP = tempfile.mkdtemp(prefix='t-opensoho-')

NS = build([
    'OH_DEFAULTS', 'OH_ITEMS', 'OH_COLLECTIONS',
    'OH_SECRET_ENV', 'OH_ENC_ENV', 'OH_SECRET_LEN', 'OH_ENC_LEN',
    'OH_ADMIN_EMAIL', 'OH_API_LATEST', 'OH_PORT_MIN', 'OH_PORT_MAX',
    'OH_BIND_CHOICES', 'OH_UNIT', 'OH_SERVICE', 'OH_USER',
    'OH_DIR', 'OH_BIN', 'OH_DATA', 'OH_CONF', 'OH_ENV', 'OH_ADMIN',
    '_oh_rand', '_oh_norm', '_oh_load', '_oh_save', '_atomic_write',
    '_oh_read_env', '_oh_write_env', '_oh_ensure_env', '_oh_env',
    '_oh_admin_load', '_oh_admin_save',
    '_oh_installed', '_oh_service', '_oh_version', '_oh_arch',
    '_oh_cli', '_oh_wait_up',
    '_oh_unit_body', '_oh_write_unit',
    '_oh_listen_now', '_oh_lan_ip', '_oh_health',
    '_oh_api', '_oh_login', '_oh_ensure_admin', '_oh_admin_token',
    '_oh_local_candidates', '_oh_ensure_user',
    '_oh_latest_release', '_oh_download', '_oh_extract_bin',
    '_oh_status', '_oh_install_op', '_oh_uninstall_op', '_oh_svc_op',
    '_oh_save_op', '_oh_secret_op', '_oh_probe_op', '_oh_pick',
    'act_opensoho',
], {
    're': re, 'os': os, 'json': json, 'shutil': shutil, 'stat': stat,
    'time': __import__('time'), 'sh': fake_sh,
    'ok': _ok, 'fail': _fail, 'log': _log, 'in_build_mode': _in_build_mode,
    'tempfile': tempfile,
})

# 路径常量一律重定向到临时目录 —— 绝不抽源码里的路径常量节点（会覆盖注入值，
# 让测试读写真实路径造成跨次污染）
NS['OH_DIR'] = os.path.join(TMP, 'opt', 'opensoho')
NS['OH_BIN'] = os.path.join(NS['OH_DIR'], 'opensoho')
NS['OH_DATA'] = os.path.join(TMP, 'var', 'opensoho')
NS['OH_CONF'] = os.path.join(TMP, 'etc', 'opensoho.conf')
NS['OH_ENV'] = os.path.join(TMP, 'etc', 'opensoho.env')
NS['OH_ADMIN'] = os.path.join(TMP, 'etc', 'opensoho.admin')
NS['OH_UNIT'] = os.path.join(TMP, 'etc', 'opensoho.service')
for _d in (os.path.dirname(NS['OH_CONF']), NS['OH_DIR'], NS['OH_DATA']):
    os.makedirs(_d, exist_ok=True)

DEFAULTS = NS['OH_DEFAULTS']
ITEMS = NS['OH_ITEMS']
COLLECTIONS = NS['OH_COLLECTIONS']
SECRET_ENV = NS['OH_SECRET_ENV']
ENC_ENV = NS['OH_ENC_ENV']
SECRET_LEN = NS['OH_SECRET_LEN']
ENC_LEN = NS['OH_ENC_LEN']
BIND_CHOICES = NS['OH_BIND_CHOICES']

oh_rand = NS['_oh_rand']
oh_norm = NS['_oh_norm']
oh_load = NS['_oh_load']
oh_save = NS['_oh_save']
read_env = NS['_oh_read_env']
write_env = NS['_oh_write_env']
ensure_env = NS['_oh_ensure_env']
oh_env = NS['_oh_env']
admin_load = NS['_oh_admin_load']
admin_save = NS['_oh_admin_save']
oh_installed = NS['_oh_installed']
oh_service = NS['_oh_service']
oh_version = NS['_oh_version']
oh_arch = NS['_oh_arch']
unit_body = NS['_oh_unit_body']
write_unit = NS['_oh_write_unit']
oh_cli = NS['_oh_cli']
oh_wait_up = NS['_oh_wait_up']
listen_now = NS['_oh_listen_now']
oh_health = NS['_oh_health']
oh_api = NS['_oh_api']
oh_login = NS['_oh_login']
oh_admintoken = NS['_oh_admin_token']
local_cands = NS['_oh_local_candidates']
ensure_user = NS['_oh_ensure_user']
latest_release = NS['_oh_latest_release']
extract_bin = NS['_oh_extract_bin']
oh_status = NS['_oh_status']
install_op = NS['_oh_install_op']
uninstall_op = NS['_oh_uninstall_op']
svc_op = NS['_oh_svc_op']
save_op = NS['_oh_save_op']
secret_op = NS['_oh_secret_op']
probe_op = NS['_oh_probe_op']
oh_pick = NS['_oh_pick']
act = NS['act_opensoho']


def main():
    print('================ OpenSOHO AC/AP 单测 ================')

    print('\n--- 出厂默认值 ---')
    chk('默认监听 0.0.0.0（AP 要从局域网注册进来）',
        DEFAULTS['bind'] == '0.0.0.0', DEFAULTS['bind'])
    chk('默认端口 8090', DEFAULTS['port'] == 8090, str(DEFAULTS['port']))
    chk('默认开机自启', DEFAULTS['autostart'] is True)
    chk('默认自动接管新 AP', DEFAULTS['enable_new_devices'] is True)
    chk('默认开启配置静态加密', DEFAULTS['encryption'] is True)
    chk('共享密钥长度要求 32', SECRET_LEN == 32, str(SECRET_LEN))
    chk('加密密钥长度 32（--encryptionEnv 硬性要求）', ENC_LEN == 32)
    chk('监听地址只给三个白名单选项',
        sorted(BIND_CHOICES) == ['0.0.0.0', '127.0.0.1', '::'], str(BIND_CHOICES))

    print('\n--- 说明条目（每项都要讲清为什么与影响） ---')
    chk('说明条目不少于 6 项', len(ITEMS) >= 6, str(len(ITEMS)))
    for it in ITEMS:
        chk('说明 %s 有 why 与 impact' % it['key'],
            bool(it.get('why')) and len(it.get('impact') or []) >= 2)

    print('\n--- 集合名必须是实测到的真实名字 ---')
    names = [c[0] for c in COLLECTIONS]
    for must in ('devices', 'clients', 'radios', 'wifi_ssids', 'wifi_aps'):
        chk('集合含 %s' % must, must in names)
    chk('没有用文档里那个不存在的 wifi 集合名',
        'wifi' not in names, str(names))

    print('\n--- 端口钳制（int(x or 默认) 的 falsy 陷阱） ---')
    chk('端口 0 不被悄悄回落成默认', oh_norm({'port': 0})['port'] == 1024,
        str(oh_norm({'port': 0})['port']))
    chk('端口 1 被抬到 1024', oh_norm({'port': 1})['port'] == 1024)
    chk('端口 70000 被压到 65535', oh_norm({'port': 70000})['port'] == 65535)
    chk('端口 8091 原样保留', oh_norm({'port': 8091})['port'] == 8091)
    chk('端口 None 用默认 8090', oh_norm({'port': None})['port'] == 8090)
    chk('端口空串用默认 8090', oh_norm({'port': ''})['port'] == 8090)
    chk('端口是垃圾字符串时用默认 8090', oh_norm({'port': 'abc'})['port'] == 8090)
    chk('端口是字符串数字也能用', oh_norm({'port': '9090'})['port'] == 9090)

    print('\n--- 其它字段收敛 ---')
    chk('非法监听地址回落 0.0.0.0', oh_norm({'bind': 'evil'})['bind'] == '0.0.0.0')
    chk('合法监听地址保留', oh_norm({'bind': '127.0.0.1'})['bind'] == '127.0.0.1')
    chk('autostart 被收成 bool', oh_norm({'autostart': 0})['autostart'] is False)
    chk('缺失字段用默认值补齐',
        oh_norm({}) == dict(DEFAULTS), str(oh_norm({})))

    print('\n--- 配置读写往返 ---')
    oh_save({'bind': '127.0.0.1', 'port': 9099, 'autostart': False,
             'enable_new_devices': False, 'encryption': False})
    back = oh_load()
    chk('保存后能原样读回', back == {'bind': '127.0.0.1', 'port': 9099,
                                     'autostart': False, 'enable_new_devices': False,
                                     'encryption': False}, str(back))
    chk('配置文件真的落盘了', os.path.isfile(NS['OH_CONF']))
    os.unlink(NS['OH_CONF'])
    chk('删掉后读回出厂默认', oh_load() == dict(DEFAULTS))
    # 坏文件不能让页面白屏
    with open(NS['OH_CONF'], 'w') as f:
        f.write('{ this is not json')
    chk('配置损坏时回落默认值而不是崩溃', oh_load() == dict(DEFAULTS))
    os.unlink(NS['OH_CONF'])

    print('\n--- systemd 单元 ---')
    u = unit_body(DEFAULTS)
    chk('单元指定了专用用户', 'User=opensoho' in u)
    chk('单元指定了专用组', 'Group=opensoho' in u)
    chk('密钥走 EnvironmentFile 而不是 Environment=（单元是 0644）',
        'EnvironmentFile=' in u and 'Environment=OPENSOHO' not in u)
    chk('单元显式写了 /usr/sbin 进 PATH',
        '/usr/sbin' in u)
    chk('单元带了 --dir（默认值随运行用户变，必须显式给）', '--dir' in u)
    chk('单元带了数据目录绝对路径', NS['OH_DATA'] in u)
    # 【实测踩坑】pb_migrations 是解压到「工作目录」而不是 --dir 的，
    # 工作目录指向 root 所有的二进制目录会让服务用户写不进去、启动秒退。
    chk('工作目录指向数据目录而不是二进制目录',
        ('WorkingDirectory=%s' % NS['OH_DATA']) in u
        and ('WorkingDirectory=%s' % NS['OH_DIR']) not in u,
        [l for l in u.splitlines() if l.startswith('WorkingDirectory')])
    # 只看常量赋值本身，别被注释里「为什么不用 /var/lib/drouter/...」的说明误伤
    _m = re.search(r"^OH_DATA = '([^']+)'", HELPER_SRC, re.M)
    chk('数据目录常量不在 /var/lib/drouter 下（父目录 0750 会让服务用户穿不进去）',
        bool(_m) and not _m.group(1).startswith('/var/lib/drouter'),
        _m.group(1) if _m else '未找到 OH_DATA')
    chk('默认监听 0.0.0.0:8090', '--http 0.0.0.0:8090' in u)
    chk('默认开启自动接管', '--enableNewDevices=true' in u)
    chk('默认带加密密钥环境变量', '--encryptionEnv=OPENSOHO_SETTINGS_KEY' in u)
    chk('有 Install 段', 'WantedBy=multi-user.target' in u)
    chk('有失败重启', 'Restart=on-failure' in u)
    chk('做了最小权限收敛',
        'NoNewPrivileges=yes' in u and 'ProtectSystem=full' in u)
    u2 = unit_body({'bind': '127.0.0.1', 'port': 9999,
                    'enable_new_devices': False, 'encryption': False})
    chk('改监听与端口会进 ExecStart', '--http 127.0.0.1:9999' in u2)
    chk('关闭自动接管会写成 false', '--enableNewDevices=false' in u2)
    chk('关闭加密就不带 --encryptionEnv', '--encryptionEnv' not in u2)
    write_unit(DEFAULTS)
    chk('写单元后文件存在', os.path.isfile(NS['OH_UNIT']))
    chk('单元文件权限 0644', perm_ok(NS['OH_UNIT'], 0o644), mode_str(NS['OH_UNIT']))
    chk('源码里显式 chmod 了单元文件', 'os.chmod(OH_UNIT, 0o644)' in HELPER_SRC)

    print('\n--- 两把密钥 ---')
    s1, e1 = ensure_env()
    chk('首次生成共享密钥长度达标', len(s1) == SECRET_LEN, str(len(s1)))
    chk('首次生成加密密钥长度 32', len(e1) == ENC_LEN, str(len(e1)))
    chk('共享密钥是字母数字（方便往 LuCI 粘贴）', s1.isalnum())
    s2, e2 = ensure_env()
    chk('第二次调用共享密钥不变', s2 == s1)
    chk('【关键】第二次调用加密密钥绝不变——变了历史配置就解不开', e2 == e1)
    chk('密钥文件权限是 0600（里面有 Wi-Fi 配置的加密密钥）',
        perm_ok(NS['OH_ENV'], 0o600), mode_str(NS['OH_ENV']))
    chk('源码里显式 chmod 了密钥文件为 0600',
        'os.chmod(OH_ENV, 0o600)' in HELPER_SRC)
    got = read_env()
    chk('能读回共享密钥', got[0] == s1)
    chk('能读回加密密钥', got[1] == e1)
    # 手工塞一个短密钥，应该被判定为弱并重新生成
    write_env('short', e1)
    s3, e3 = ensure_env()
    chk('共享密钥太短会被重新生成', len(s3) == SECRET_LEN and s3 != 'short')
    chk('重生成共享密钥时加密密钥依然不动', e3 == e1)
    write_env(s1, e1)
    # 解析健壮性：注释、引号、空行都不能让它崩
    with open(NS['OH_ENV'], 'w') as f:
        f.write('# comment\n\n%s="%s"\n%s=%s\n' % (SECRET_ENV, s1, ENC_ENV, e1))
    chk('能解析带注释与引号的环境文件', read_env() == (s1, e1))
    write_env(s1, e1)
    envd = oh_env()
    chk('给子命令的环境里带了共享密钥', envd.get(SECRET_ENV) == s1)
    chk('给子命令的环境里带了加密密钥', envd.get(ENC_ENV) == e1)
    # 连 --help 都要求共享密钥已设置，所以绝不能给空串
    chk('环境里的共享密钥不会是空串（空了连 -v 都跑不了）',
        bool(envd.get(SECRET_ENV)))

    print('\n--- 控制台管理员凭据 ---')
    admin_save({'email': 'admin@drouter.local', 'password': 'pw', 'token': 't'})
    chk('凭据文件权限 0600', perm_ok(NS['OH_ADMIN'], 0o600), mode_str(NS['OH_ADMIN']))
    chk('源码里显式 chmod 了凭据文件为 0600',
        'os.chmod(OH_ADMIN, 0o600)' in HELPER_SRC)
    chk('能读回凭据', admin_load().get('email') == 'admin@drouter.local')
    with open(NS['OH_ADMIN'], 'w') as f:
        f.write('not json')
    chk('凭据文件损坏时返回空而不是崩溃', admin_load() == {})
    os.unlink(NS['OH_ADMIN'])

    print('\n--- 健康检查（/api/health 免鉴权） ---')
    HTTP_BODY.clear()
    HTTP_BODY['http://127.0.0.1:8090/api/health'] = \
        '{"message":"API is healthy.","code":200,"data":{}}'
    okh, msg = oh_health(8090)
    chk('健康响应被判定为正常', okh is True and msg == 'ok', msg)
    HTTP_BODY['http://127.0.0.1:8090/api/health'] = '{"code":200}'
    chk('只有 code:200 也算正常', oh_health(8090)[0] is True)
    HTTP_BODY['http://127.0.0.1:8090/api/health'] = 'garbage'
    okh2, msg2 = oh_health(8090)
    chk('异常响应被判定为不健康', okh2 is False, msg2)
    HTTP_BODY.clear()
    okh3, msg3 = oh_health(8090)
    chk('连不上被判定为不健康且不抛异常', okh3 is False and '连不上' in msg3, msg3)

    print('\n--- REST 调用 ---')
    HTTP_BODY.clear()
    HTTP_BODY['http://127.0.0.1:8090/api/collections/devices/records?perPage=1'] = \
        '{"items":[],"totalItems":0}'
    d, err = oh_api('GET', '/api/collections/devices/records?perPage=1', 8090, 'tok')
    chk('GET 能拿到解析后的 dict', d is not None and d.get('totalItems') == 0, err)
    HTTP_BODY.clear()
    d2, err2 = oh_api('GET', '/api/x', 8090, 'tok')
    chk('失败时返回 None 与错误串而不是抛异常', d2 is None and bool(err2))
    HTTP_BODY.clear()
    HTTP_BODY['http://127.0.0.1:8090/api/collections/_superusers/auth-with-password'] = \
        '{"token":"abc.def.ghi"}'
    tok, e = oh_login(8090, 'a@b.c', 'pw')
    chk('登录能取到 token', tok == 'abc.def.ghi', e)
    HTTP_BODY.clear()
    tok2, e2 = oh_login(8090, 'a@b.c', 'pw')
    chk('登录失败时返回空 token 与错误', tok2 == '' and bool(e2))

    print('\n--- 字段取值（不硬猜字段名） ---')
    chk('取第一个非空候选', oh_pick({'mac': 'aa:bb'}, ('name', 'mac')) == 'aa:bb')
    chk('候选全空时返回空串', oh_pick({'x': 1}, ('name', 'mac')) == '')
    chk('空值被跳过继续找下一个',
        oh_pick({'name': '', 'mac': 'cc'}, ('name', 'mac')) == 'cc')
    chk('None 入参不崩', oh_pick(None, ('name',)) == '')

    print('\n--- 解包：zip slip 防护 ---')
    zpath = os.path.join(TMP, 'evil.zip')
    with zipfile.ZipFile(zpath, 'w') as z:
        z.writestr('opensoho', b'\x7fELF-fake-binary')
        z.writestr('../../../../tmp/pwned-by-opensoho', b'evil')
        z.writestr('some/dir/note.txt', b'hi')
    outdir = os.path.join(TMP, 'extracted')
    got = extract_bin(zpath, outdir)
    chk('解出了 opensoho 二进制', os.path.basename(got) == 'opensoho')
    chk('二进制带执行权限',
        True if IS_WIN else (os.stat(got).st_mode & 0o111) != 0)
    chk('源码里显式 chmod 了解出的二进制为 0755',
        'os.chmod(dst, 0o755)' in HELPER_SRC)
    chk('【关键】带 ../ 的成员没有落到目标目录之外',
        not os.path.exists('/tmp/pwned-by-opensoho'))
    chk('只解了那一个文件，没有把整个压缩包铺开',
        not os.path.exists(os.path.join(outdir, 'some')))
    badzip = os.path.join(TMP, 'bad.zip')
    with zipfile.ZipFile(badzip, 'w') as z:
        z.writestr('readme.txt', b'nothing here')
    try:
        extract_bin(badzip, os.path.join(TMP, 'extracted2'))
        chk('压缩包里没二进制时要报错', False)
    except Exception as ex:
        chk('压缩包里没二进制时要报错', '没有 opensoho' in str(ex), str(ex)[:60])
    raw = os.path.join(TMP, 'opensoho')
    with open(raw, 'wb') as f:
        f.write(b'\x7fELF-fake')
    got2 = extract_bin(raw, os.path.join(TMP, 'extracted3'))
    chk('裸二进制也能直接装', os.path.isfile(got2))

    print('\n--- 最新版查询 ---')
    HTTP_BODY.clear()
    HTTP_BODY[NS['OH_API_LATEST']] = json.dumps({
        'tag_name': 'v0.15.2',
        'assets': [{'name': 'opensoho_0.15.2_linux_amd64.zip',
                    'browser_download_url': 'https://x/opensoho.zip'},
                   {'name': 'opensoho_0.15.2_linux_arm64.zip',
                    'browser_download_url': 'https://x/opensoho-arm.zip'}]})
    ver, url = latest_release('amd64')
    chk('能取到版本号且去掉了 v 前缀', ver == '0.15.2', str(ver))
    chk('按架构挑对了 amd64 包', url == 'https://x/opensoho.zip', str(url))
    ver2, url2 = latest_release('arm64')
    chk('按架构挑对了 arm64 包', url2 == 'https://x/opensoho-arm.zip', str(url2))
    HTTP_BODY.clear()
    v3, u3 = latest_release('amd64')
    chk('查不到时返回空版本与中文错误', v3 == '' and 'GitHub' in u3, u3[:50])
    chk('架构映射 x86_64 → amd64', oh_arch() == 'amd64')
    SH_OUT['uname'] = 'aarch64'
    chk('架构映射 aarch64 → arm64', oh_arch() == 'arm64')
    SH_OUT['uname'] = 'mips'
    chk('不支持的架构返回空', oh_arch() == '')
    SH_OUT['uname'] = 'x86_64'

    print('\n--- 子命令执行（cwd 与属主） ---')
    CALLS[:] = []
    ENVS[:] = []
    CWDS[:] = []
    oh_cli([NS['OH_BIN'], '-v'], timeout=20)
    chk('子命令把共享密钥带进了环境',
        any(e.get(SECRET_ENV) for e in ENVS))
    chk('子命令把 cwd 设成了数据目录（否则 root 会在 / 下留 pb_migrations）',
        NS['OH_DATA'] in CWDS, str([c for c in CWDS if c]))
    chk('跑完把属主改回服务用户（不改下次服务起不来）',
        any(c[:2] == ['chown', '-R'] for c in CALLS),
        str([c for c in CALLS if c[:1] == ['chown']]))
    chk('chown 的目标是 opensoho:opensoho',
        any(c[:2] == ['chown', '-R'] and 'opensoho:opensoho' in c for c in CALLS))
    chk('chown 的对象是数据目录',
        any(c[:2] == ['chown', '-R'] and NS['OH_DATA'] in c for c in CALLS))

    print('\n--- 启动确认（systemctl 返回 0 不代表活着） ---')
    HTTP_BODY.clear()
    HTTP_BODY['http://127.0.0.1:8090/api/health'] = '{"code":200}'
    SH_OUT['active'] = 'active'
    up, why = oh_wait_up({'port': 8090}, tries=2)
    chk('服务在跑且 health 正常 → 判定起来了', up is True, why)
    SH_OUT['active'] = 'inactive'
    up2, why2 = oh_wait_up({'port': 8090}, tries=1)
    chk('服务没起来 → 判定没起来', up2 is False)
    chk('失败说明里给了排查命令', 'journalctl' in why2, why2[:80])
    SH_OUT['active'] = 'active'
    HTTP_BODY['http://127.0.0.1:8090/api/health'] = 'garbage'
    up3, why3 = oh_wait_up({'port': 8090}, tries=2)
    chk('服务在跑但端口没应答 → 判定没起来并说清端口',
        up3 is False and '8090' in why3, why3[:80])
    SH_OUT['active'] = 'inactive'
    HTTP_BODY.clear()

    print('\n--- 安装（离线：从本机文件） ---')
    # 造一个可用的假二进制
    with open(NS['OH_BIN'], 'wb') as f:
        f.write(b'\x7fELF-fake-opensoho')
    os.chmod(NS['OH_BIN'], 0o755)
    chk('已安装判定为真', oh_installed() is True)
    CALLS[:] = []
    SH_OUT['active'] = 'active'
    HTTP_BODY.clear()
    HTTP_BODY['http://127.0.0.1:8090/api/health'] = '{"code":200}'
    r = install_op({'local_path': raw})
    chk('从本机文件安装成功', r['ok'] is True, r.get('msg_cn'))
    chk('安装时建了 systemd 单元', os.path.isfile(NS['OH_UNIT']))
    chk('安装时确保了密钥文件', os.path.isfile(NS['OH_ENV']))
    chk('安装时重新加载了 systemd',
        any(c[:2] == ['systemctl', 'daemon-reload'] for c in CALLS))
    chk('安装后启动了服务',
        any(c[:2] == ['systemctl', 'restart'] for c in CALLS))
    chk('安装时先建了专用系统用户',
        any(c[:1] == ['id'] for c in CALLS))
    # 【实测踩坑】升级时服务正在跑，二进制被内核占用，直接覆盖会报
    # 「Text file busy」，必须先停再换。
    i_stop = next((i for i, c in enumerate(CALLS) if c[:2] == ['systemctl', 'stop']), -1)
    i_restart = next((i for i, c in enumerate(CALLS)
                      if c[:2] == ['systemctl', 'restart']), -1)
    chk('换二进制前先把服务停了', i_stop >= 0)
    chk('停服务发生在重启之前', 0 <= i_stop < i_restart,
        'stop=%d restart=%d' % (i_stop, i_restart))
    CALLS[:] = []
    r2 = install_op({'local_path': '/nope/definitely-not-here'})
    chk('本地文件不存在时报 NOT_FOUND',
        r2['ok'] is False and r2.get('code') == 'NOT_FOUND', r2.get('msg_cn'))
    chk('【关键】文件不存在时不重启服务',
        not any(c[0] == 'systemctl' and 'restart' in c for c in CALLS))
    chk('安装失败但原本在跑 → 把服务拉回去，不留停止状态',
        any(c[:2] == ['systemctl', 'start'] for c in CALLS))
    r3 = install_op({})
    chk('不带 local_path 时尝试联网下载（本机连不上 GitHub 会明确报错）',
        r3['ok'] is False and r3.get('code') in ('NO_RELEASE', 'DL_FAIL'),
        '%s / %s' % (r3.get('code'), (r3.get('msg_cn') or '')[:60]))

    print('\n--- 构建保护模式：只写盘不启服务 ---')
    BUILD_MODE[0] = True
    CALLS[:] = []
    r4 = install_op({'local_path': raw})
    chk('保护模式下安装仍然成功（只是不启服务）', r4['ok'] is True, r4.get('msg_cn'))
    chk('保护模式下不启服务',
        not any(c[0] == 'systemctl' and 'restart' in c for c in CALLS))
    chk('保护模式下提示了原因', '构建保护模式' in (r4.get('msg_cn') or ''))
    CALLS[:] = []
    r5 = svc_op({'op2': 'start'})
    chk('保护模式下 start 被拦住',
        r5['ok'] is False and r5.get('code') == 'BUILD_MODE_BLOCKED', r5.get('msg_cn'))
    chk('保护模式下拦住时不调 systemctl start',
        not any(c[:2] == ['systemctl', 'start'] for c in CALLS))
    CALLS[:] = []
    save_op({'bind': '127.0.0.1', 'port': 8123})
    chk('保护模式下保存只写盘不重启',
        not any(c[0] == 'systemctl' and 'restart' in c for c in CALLS))
    chk('保护模式下保存仍然落了配置',
        oh_load()['port'] == 8123, str(oh_load()['port']))
    BUILD_MODE[0] = False
    oh_save(dict(DEFAULTS))

    print('\n--- 服务操作 ---')
    CALLS[:] = []
    r6 = svc_op({'op2': 'restart'})
    chk('重启成功', r6['ok'] is True, r6.get('msg_cn'))
    r7 = svc_op({'op2': 'bogus'})
    chk('非法服务操作被拒', r7['ok'] is False and r7.get('code') == 'BAD_OP')
    CALLS[:] = []
    svc_op({'op2': 'enable'})
    chk('enable 会把 autostart 落进配置', oh_load()['autostart'] is True)
    svc_op({'op2': 'disable'})
    chk('disable 会把 autostart 落进配置', oh_load()['autostart'] is False)
    oh_save(dict(DEFAULTS))

    print('\n--- 重新生成共享密钥 ---')
    before_s, before_e = read_env()
    CALLS[:] = []
    r8 = secret_op({})
    after_s, after_e = read_env()
    chk('共享密钥确实换了', after_s != before_s)
    chk('【关键】加密密钥绝不能被换掉', after_e == before_e)
    chk('提示了 AP 要重新注册', '重新注册' in (r8.get('msg_cn') or ''),
        r8.get('msg_cn'))
    write_env(before_s, before_e)

    print('\n--- 管理员凭据：首次要能自己建出来 ---')
    if os.path.isfile(NS['OH_ADMIN']):
        os.unlink(NS['OH_ADMIN'])
    HTTP_BODY.clear()
    HTTP_BODY['http://127.0.0.1:8090/api/collections/_superusers/auth-with-password'] = \
        '{"token":"T1"}'
    tok, e = oh_admintoken(8090)
    chk('【关键】没有已存凭据时能现场建出管理员（首次安装后就是这种情况）',
        tok == 'T1', e)
    chk('凭据已落盘', os.path.isfile(NS['OH_ADMIN']))
    chk('邮箱用的是默认管理员账号',
        admin_load().get('email') == 'admin@drouter.local',
        str(admin_load().get('email')))
    chk('密码是随机生成的', len(admin_load().get('password') or '') >= 12)
    URLS[:] = []
    HTTP_BODY['http://127.0.0.1:8090/api/collections/devices/records?perPage=1'] = \
        '{"totalItems":0}'
    tok2, _e2 = oh_admintoken(8090)
    chk('已有有效 token 时直接复用', tok2 == 'T1')
    chk('复用时不重复登录',
        not any('auth-with-password' in u for u in URLS), str(URLS))
    # token 失效（401）时要能用留存的密码重新换一个
    HTTP_BODY.clear()
    HTTP_BODY['http://127.0.0.1:8090/api/collections/_superusers/auth-with-password'] = \
        '{"token":"T2"}'
    tok3, _e3 = oh_admintoken(8090)
    chk('token 失效时用留存密码重新换', tok3 == 'T2', str(tok3))

    print('\n--- 子命令必须带同一个加密参数 ---')
    # 库是 serve 带 --encryptionEnv 建的，superuser upsert 不带同一个参数就会报
    # 「missing encryption key」，管理员永远建不出来。
    CALLS[:] = []
    oh_save(dict(DEFAULTS))                      # encryption=True
    HTTP_BODY.clear()
    HTTP_BODY['http://127.0.0.1:8090/api/collections/_superusers/auth-with-password'] = \
        '{"token":"T3"}'
    if os.path.isfile(NS['OH_ADMIN']):
        os.unlink(NS['OH_ADMIN'])
    NS['_oh_ensure_admin'](8090)
    chk('开启加密时建管理员要带 --encryptionEnv',
        any('--encryptionEnv=' + ENC_ENV in c for c in CALLS),
        str([c for c in CALLS if 'superuser' in c])[:140])
    CALLS[:] = []
    oh_save(dict(OH_DEFAULTS if False else DEFAULTS, encryption=False))
    if os.path.isfile(NS['OH_ADMIN']):
        os.unlink(NS['OH_ADMIN'])
    NS['_oh_ensure_admin'](8090)
    chk('关闭加密时建管理员不带 --encryptionEnv',
        not any('--encryptionEnv' in c for c in CALLS))
    oh_save(dict(DEFAULTS))

    print('\n--- 加密开关：有数据后不许再切 ---')
    # 让服务处于停止态，save 就不会走「重启并等 health」那条路
    # （那段已在别处单独测过，这里只关心加密开关的准入判断）
    _prev_active = SH_OUT.get('active')
    SH_OUT['active'] = 'inactive'
    try:
        os.makedirs(NS['OH_DATA'], exist_ok=True)
        with open(os.path.join(NS['OH_DATA'], 'data.db'), 'wb') as f:
            f.write(b'x')
        r_enc = save_op({'encryption': False})
        chk('【关键】已有数据后切加密开关被拒',
            r_enc['ok'] is False and r_enc.get('code') == 'ENC_LOCKED',
            r_enc.get('msg_cn'))
        chk('拒绝时说清了后果与出路',
            '读不出来' in (r_enc.get('msg_cn') or '') and '卸载' in (r_enc.get('msg_cn') or ''))
        chk('被拒时配置没有被改坏', oh_load()['encryption'] is True)
        r_ok = save_op({'port': 8095})
        chk('不涉及加密的改动仍然能保存', r_ok['ok'] is True, r_ok.get('msg_cn'))
        os.unlink(os.path.join(NS['OH_DATA'], 'data.db'))
        r_none = save_op({'encryption': False})
        chk('没有数据时允许切换（首次安装前的场景）',
            r_none['ok'] is True, r_none.get('msg_cn'))
    finally:
        oh_save(dict(DEFAULTS))
        SH_OUT['active'] = _prev_active

    print('\n--- 概况读取 ---')
    HTTP_BODY.clear()
    HTTP_BODY['http://127.0.0.1:8090/api/health'] = '{"code":200}'
    r9 = probe_op({})
    chk('服务不健康时明确拒绝读取',
        r9['ok'] is False, r9.get('msg_cn'))
    # 管理员凭据为空 → 拿不到 token → 要有明确错误而不是静默成功
    if os.path.isfile(NS['OH_ADMIN']):
        os.unlink(NS['OH_ADMIN'])
    HTTP_BODY['http://127.0.0.1:8090/api/collections/devices/records?perPage=1'] = \
        '{"items":[],"totalItems":0}'
    r10 = probe_op({})
    chk('拿不到凭据时返回明确错误', r10['ok'] is False and bool(r10.get('code')),
        '%s / %s' % (r10.get('code'), (r10.get('msg_cn') or '')[:60]))

    print('\n--- 卸载 ---')
    CALLS[:] = []
    r11 = uninstall_op({})
    chk('卸载成功', r11['ok'] is True, r11.get('msg_cn'))
    chk('卸载先停服务',
        any(c[:2] == ['systemctl', 'stop'] for c in CALLS))
    chk('卸载移除了单元文件', not os.path.isfile(NS['OH_UNIT']))
    chk('默认保留数据目录（里面有 AP 清单）',
        os.path.isdir(NS['OH_DATA']), NS['OH_DATA'])
    chk('默认保留密钥文件', os.path.isfile(NS['OH_ENV']))
    r12 = uninstall_op({'purge': True})
    chk('purge 时清掉数据目录', r12['ok'] is True and not os.path.isdir(NS['OH_DATA']))
    chk('purge 时清掉密钥文件', not os.path.isfile(NS['OH_ENV']))

    print('\n--- op 分发 ---')
    r13 = act({'op': 'bogus'})
    chk('未知 op 返回 BAD_OP', r13['ok'] is False and r13.get('code') == 'BAD_OP')
    chk('缺省 op 是 status', act({})['ok'] is True)
    SH_OUT['active'] = 'inactive'
    st = act({'op': 'status'})
    chk('status 返回安装状态字段', 'installed' in (st.get('data') or {}))
    chk('status 返回密钥字段', 'secret' in (st.get('data') or {}))
    chk('status 返回控制台地址', 'console_url' in (st.get('data') or {}))
    chk('status 返回说明条目', len((st.get('data') or {}).get('items') or []) >= 6)
    chk('status 返回构建保护模式状态',
        'build_mode' in (st.get('data') or {}))

    print('\n--- 监听与地址解析 ---')
    SH_OUT['ss_out'] = ('LISTEN 0 4096 0.0.0.0:8090 0.0.0.0:*\n'
                        'LISTEN 0 4096 [::]:8090 [::]:*\n'
                        'LISTEN 0 4096 0.0.0.0:631 0.0.0.0:*')
    got3 = listen_now(8090)
    chk('能找出 8090 的监听地址', '0.0.0.0' in got3 and '[::]' in got3, str(got3))
    chk('不会把别的端口算进来', '631' not in ''.join(got3))
    # ss 把「监听全部地址」打成 `*`，直接显示没人看得懂
    SH_OUT['ss_out'] = 'LISTEN 0 4096 *:8090 *:*'
    chk('ss 的通配 * 被还原成 0.0.0.0', listen_now(8090) == ['0.0.0.0'],
        str(listen_now(8090)))
    chk('端口非法时返回空列表', listen_now('abc') == [])
    SH_OUT['ip_out'] = ('2: ens18    inet 192.168.7.3/24 brd\n'
                        '3: ens19    inet 10.0.0.5/24 brd')
    chk('能取到 LAN 地址', NS['_oh_lan_ip']() == '192.168.7.3', NS['_oh_lan_ip']())
    SH_OUT['ip_out'] = '1: lo    inet 127.0.0.1/8 scope host'
    chk('只剩回环时返回空', NS['_oh_lan_ip']() == '')

    print('\n--- 后端接线 ---')
    chk('ACTIONS 注册了 opensoho', "'opensoho': act_opensoho" in HELPER_SRC)
    chk('drouter-web 有 /api/opensoho 路由', "'/api/opensoho'" in WEB_SRC)
    chk('install 给了足够长的超时（要下载 13MB）',
        re.search(r"to = 280 if op == 'install'", WEB_SRC) is not None)
    chk('路由走的是 helper opensoho',
        re.search(r"helper\('opensoho', b, timeout=to\)", WEB_SRC) is not None)
    chk('依赖自检登记了 opensoho',
        "'opensoho', 'OpenSOHO" in HELPER_SRC)
    chk('依赖自检里 opensoho 不提供 apt 一键安装（它不是 apt 包）',
        re.search(r"'AC/AP 管理中心：.*', '', 'ac'\)", HELPER_SRC, re.S) is not None)
    chk('依赖自检新增了 ac 分组', "('ac', '无线 AC / AP')" in HELPER_SRC)

    print('\n--- 前端接线 ---')
    chk('菜单里有 AC/AP 管理中心', "k: 'opensoho'" in APP_SRC)
    chk('VIEWS 映射了 opensoho', 'opensoho: viewOpensoho,' in APP_SRC)
    chk('有 viewOpensoho 函数', 'async function viewOpensoho()' in APP_SRC)
    chk('调的是 /api/opensoho', "'/api/opensoho'" in APP_SRC)
    for fn in ('ohLoad', 'ohRender', 'ohSave', 'ohInstall', 'ohUninstall',
               'ohRegenSecret', 'ohProbe', 'ohRenderStats'):
        chk('前端有 %s' % fn, ('function %s(' % fn) in APP_SRC)
    for eid in ('oh-bind', 'oh-port', 'oh-newdev', 'oh-enc', 'oh-auto',
                'oh-secret-val', 'oh-local', 'oh-purge'):
        chk('页面有元素 %s' % eid, ('id="%s"' % eid) in APP_SRC)
    chk('端口输入有 min/max 限制',
        'min="1024" max="65535"' in APP_SRC)
    chk('重新生成密钥前有二次确认',
        '重新注册一次' in APP_SRC and 'window.confirm' in APP_SRC)
    chk('卸载区分是否清除数据', 'purge' in APP_SRC)
    chk('把控制台地址做成可点击链接', 'target="_blank"' in APP_SRC)

    print('\n--- 安全红线：不许碰的东西 ---')
    seg = HELPER_SRC[HELPER_SRC.index('OH_DIR = '):HELPER_SRC.index('def read_share')]
    chk('本模块没有动 5900（VNC）', '5900' not in seg)
    chk('本模块没有启停 DHCP', 'dnsmasq' not in seg and 'kea' not in seg)
    chk('本模块没有改防火墙', 'nft' not in seg)
    chk('本模块没有动默认路由', 'ip route' not in seg)
    chk('本模块没有动 radvd', 'radvd' not in seg)

    sys.modules['urllib'] = _real_urllib
    shutil.rmtree(TMP, ignore_errors=True)
    try:
        os.unlink('/tmp/pwned-by-opensoho')
    except Exception:
        pass
    print('\n================ 通过 %d / 失败 %d ================' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())

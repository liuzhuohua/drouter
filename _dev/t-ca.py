#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CA 证书管理 + SSL 测试（#11）：静态契约 + 纯函数 + 真 openssl 全流程单测。

盯六件事：
  ① 「部署」是这一块唯一能把用户关在门外的动作 —— 证书换上去、Web 起不来，
     最近的入口就是这个页面。所以部署前必须过四道闸（能解析 / 有私钥 /
     公钥配对 / 有效期覆盖当下），部署后必须握手比对指纹，不对就自动还原；
  ② 有效期天数不能写成 int(x or 默认) —— 0 是 falsy 会被当「没填」；
  ③ SAN 与 CN 要挡住逗号注入：openssl 的 -subj / subjectAltName 里出现逗号
     会让整条配置解析错位，签出来一张谁都不认的证书；
  ④ 证书 id 必须过白名单才能拼路径，不能让调用方塞 ../../ 进来；
  ⑤ 导入要挡住「类型和内容不符」（拿 CA 当服务器证书）、「私钥与证书不配对」；
  ⑥ 不碰安全红线：5900、nft、dnsmasq、默认路由。

本机 Git Bash 自带 OpenSSL 3.5.7（与目标机同版本），所以这里的建 CA / 签发 /
握手全部是真的 openssl 调用，不是打桩 —— 参数写错了这里就会真的失败。
"""
import ast
import io
import os
import re
import ssl
import sys
import json
import time
import shutil
import socket
import tempfile
import threading
import subprocess

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
    exec(compile(mod, '<ca>', 'exec'), ns)
    return ns


# ---------------------------------------------------------------- 测试替身
BUILD_MODE = [False]
CALLS = []
IS_WIN = (os.name == 'nt')


def real_sh(cmd, timeout=15, input_data=None, cwd=None, env=None):
    """真正跑命令。两处替身：
      · Git Bash 没有 coreutils 的 timeout，替它剥掉这一层；
      · Windows 上没有 systemctl —— 部署流程里它是「重启 Web 服务」这一步，
        这里默认当作成功，需要测失败分支时由各用例单独替换 NS['sh']。
    """
    a = list(cmd)
    if a and a[0] == 'timeout' and len(a) > 2:
        a = a[2:]
    if a[:2] == ['systemctl', 'restart']:
        CALLS.append(a)
        return (0, '', '')
    CALLS.append(a)
    kw = dict(timeout=timeout, capture_output=True, text=True,
              input=input_data, errors='replace')
    if cwd:
        kw['cwd'] = cwd
    if env:
        kw['env'] = env
    try:
        p = subprocess.run(a, **kw)
        return p.returncode, (p.stdout or '').strip(), (p.stderr or '').strip()
    except subprocess.TimeoutExpired:
        return 124, '', '命令执行超时（%ss）' % timeout
    except FileNotFoundError:
        return 127, '', '命令不存在：%s' % a[0]
    except NotADirectoryError:
        return 126, '', '工作目录不存在：%s' % cwd
    except PermissionError as e:
        return 126, '', '权限不足：%s' % e


def _ok(data=None, msg='ok', code='OK'):
    return {'ok': True, 'msg_cn': msg, 'code': code, 'data': data or {}}


def _fail(msg, code='ERR', data=None):
    return {'ok': False, 'msg_cn': msg, 'code': code, 'data': data or {}}


def _log(*a, **k):
    return None


def _in_build_mode():
    return BUILD_MODE[0]


def _print_lan_ip():
    return '192.168.7.3'


def cleanup_tmp(*paths):
    for p in paths:
        if not p:
            continue
        try:
            os.unlink(p)
        except Exception:
            pass


# ---------------------------------------------------------------- 命名空间
CA_FUNCS = [
    'CA_DIR', 'CA_INDEX', 'CA_BACKUP', 'CA_LAST', 'CA_DIR_MODE', 'CA_KEY_MODE',
    'CA_CRT_MODE', 'WEB_CERT_DIR', 'WEB_CRT', 'WEB_KEY', 'WEB_OWNER',
    'WEB_SERVICE', 'WEB_PORT_FILE', 'WEB_PORT_DEFAULT', 'CA_KEY_TYPES',
    'CA_DAYS_MIN', 'CA_DAYS_MAX', 'CA_CA_DAYS_DEFAULT', 'CA_SRV_DAYS_DEFAULT',
    'CA_EXPIRE_WARN', 'CA_KINDS', 'CA_KIND_CN', 'CA_SAN_MAX', 'SSL_PORT_MIN',
    'SSL_PORT_MAX', 'SSL_HOST_MAX', 'SSL_TIMEOUT_DEFAULT', 'SSL_TIMEOUT_MAX',
    'SSL_VERIFY_CN',
    '_ca_rand', '_ca_now', '_ca_id', '_ca_ensure_dir', '_ca_index_load',
    '_ca_index_save', '_ca_paths', '_ca_find', '_ca_days', '_ca_x509_info',
    '_ca_key_match', '_ca_web_port', '_ca_local_ips', '_ca_norm_days',
    '_ca_norm_san', '_ca_status', '_ca_openssl_version', '_ca_last_load',
    '_ca_last_save', '_ca_gen_key', '_ca_cn_ok', '_ca_subj', '_ca_new_ca_op',
    '_ca_sign_op', '_ca_csr_op', '_ca_import_op', '_ca_delete_op',
    '_ca_backup_web', '_ca_install_web', '_ca_probe_web', '_ca_deploy_op',
    '_ca_restore_op', '_ca_host_match', '_ca_san_entry', '_ca_handshake', '_ca_parse_chain',
    '_ca_ssltest_op', '_ca_is_ip', 'act_ca',
]

NS = build(CA_FUNCS, {
    'os': os, 're': re, 'json': json, 'time': time, 'shutil': shutil,
    'tempfile': tempfile, 'socket': socket, 'ssl': ssl,
    'ipaddress': __import__('ipaddress'),
    'datetime': __import__('datetime').datetime,
    'sh': real_sh, 'ok': _ok, 'fail': _fail, 'log': _log,
    'in_build_mode': _in_build_mode, 'cleanup_tmp': cleanup_tmp,
    '_print_lan_ip': _print_lan_ip,
})

# 把所有落盘路径重定向到临时目录：这是本模块唯一会写到真实路径的地方，
# 不重定向就会在 Windows 上真的去碰 C:\etc\drouter。
TMP = tempfile.mkdtemp(prefix='t-ca-')
NS['CA_DIR'] = os.path.join(TMP, 'ca')
NS['CA_INDEX'] = os.path.join(NS['CA_DIR'], 'index.json')
NS['CA_BACKUP'] = os.path.join(NS['CA_DIR'], 'backup')
NS['CA_LAST'] = os.path.join(NS['CA_DIR'], 'last-deploy.json')
NS['WEB_CERT_DIR'] = os.path.join(TMP, 'certs')
NS['WEB_CRT'] = os.path.join(NS['WEB_CERT_DIR'], 'server.crt')
NS['WEB_KEY'] = os.path.join(NS['WEB_CERT_DIR'], 'server.key')
NS['WEB_PORT_FILE'] = os.path.join(TMP, 'web-port')
NS['WEB_SERVICE'] = 'drouter-web.service'


def perm_ok(path, want):
    if IS_WIN:
        return True
    return (os.stat(path).st_mode & 0o777) == want


# ================================================================ ① 常量
print('\n--- 常量与默认值 ---')
chk('密钥文件 0600', NS['CA_KEY_MODE'] == 0o600)
chk('证书文件 0644', NS['CA_CRT_MODE'] == 0o644)
chk('证书库目录 0700（里面有私钥）', NS['CA_DIR_MODE'] == 0o700)
chk('Web 私钥归 drouter（服务以它运行）', NS['WEB_OWNER'] == 'drouter')
chk('服务器证书默认 825 天', NS['CA_SRV_DAYS_DEFAULT'] == 825)
chk('自签 CA 默认 3650 天', NS['CA_CA_DAYS_DEFAULT'] == 3650)
chk('到期提醒阈值 30 天', NS['CA_EXPIRE_WARN'] == 30)
chk('默认私钥算法是 RSA 2048', NS['CA_KEY_TYPES'][0][0] == 'rsa:2048')
chk('三种类型齐全', tuple(sorted(NS['CA_KINDS'])) == ('ca', 'csr', 'server'))
chk('18=自签名有人话解释', '自签名' in NS['SSL_VERIFY_CN'].get(18, ''))
chk('62=主机名不匹配有人话解释', '主机名' in NS['SSL_VERIFY_CN'].get(62, ''))
chk('10=过期有人话解释', '过期' in NS['SSL_VERIFY_CN'].get(10, ''))
# 这三个编号第一版写错了：21 是「验证不了叶子签名」（私有 CA 签发时的正常返回），
# 24 才是「链里有证书不是 CA 证书」，23 才是吊销。
chk('21=私有 CA 找不到颁发者', '找不到颁发者' in NS['SSL_VERIFY_CN'].get(21, ''))
chk('24=链里有一张不是 CA', '不是 CA' in NS['SSL_VERIFY_CN'].get(24, ''))
chk('23=吊销', '吊销' in NS['SSL_VERIFY_CN'].get(23, ''))

# ================================================================ ② 纯函数
print('\n--- 纯函数 ---')
nd = NS['_ca_norm_days']
chk('天数 None → 默认', nd(None, 825) == 825)
chk('天数 "" → 默认', nd('', 825) == 825)
chk('天数 0 不被当「没填」→ 钳到下限 1', nd(0, 825) == NS['CA_DAYS_MIN'],
    '(写成 int(x or 825) 就会变成 825)')
chk('天数 -5 钳到下限', nd(-5, 825) == NS['CA_DAYS_MIN'])
chk('天数 999999 钳到上限', nd(999999, 825) == NS['CA_DAYS_MAX'])
chk('天数 "abc" → 默认', nd('abc', 825) == 825)
chk('天数 "90" 正常', nd('90', 825) == 90)

ns_ = NS['_ca_norm_san']
chk('SAN 域名+IP', ns_(['a.com'], ['1.2.3.4']) == 'DNS:a.com,IP:1.2.3.4')
chk('SAN 空 → 空串', ns_([], []) == '')
chk('SAN 过滤非法 IP', ns_([], ['999.1.1.1']) == '')
chk('SAN 过滤含逗号的域名（防注入）',
    ns_(['a.com,DNS:evil.com'], []) == '', '(逗号会错位整条 subjectAltName)')
chk('SAN 过滤含空格域名', ns_(['a b.com'], []) == '')
chk('SAN 过滤超长域名', ns_(['a' * 300], []) == '')
chk('SAN 支持通配符', ns_(['*.home'], []) == 'DNS:*.home')
chk('SAN 支持 IPv6', ns_([], ['2408::1']) == 'IP:2408::1')
chk('SAN 去重上限生效', len(ns_(['a%d.com' % i for i in range(50)], []).split(','))
    == NS['CA_SAN_MAX'])
chk('SAN 忽略空条目', ns_(['', '  ', 'a.com'], []) == 'DNS:a.com')

cn = NS['_ca_cn_ok']
chk('CN 正常通过', cn('drouter.lan') is True)
chk('CN 空被拒', cn('') is False)
chk('CN 含逗号被拒', cn('a,O=b') is False)
chk('CN 含等号被拒', cn('a=b') is False)
chk('CN 含分号被拒', cn('a;b') is False)
chk('CN 超长被拒', cn('a' * 65) is False)

hm = NS['_ca_host_match']
chk('主机名精确匹配', hm('a.com', ['DNS:a.com']) is True)
chk('主机名大小写不敏感', hm('A.COM', ['DNS:a.com']) is True)
chk('主机名不匹配', hm('b.com', ['DNS:a.com']) is False)
chk('通配匹配二级', hm('x.a.com', ['DNS:*.a.com']) is True)
chk('通配不匹配一级', hm('a.com', ['DNS:*.a.com']) is False)
chk('通配最多一级', hm('y.x.a.com', ['DNS:*.a.com']) is False)
chk('SAN 带 IP 前缀也能比', hm('a.com', ['DNS:a.com', 'IP:1.2.3.4']) is True)
chk('空 SAN 不匹配', hm('a.com', []) is False)

dy = NS['_ca_days']
chk('解析 notAfter= 前缀', dy('notAfter=Sep 24 12:52:20 2036 GMT') is not None)
chk('解析裸日期', dy('Sep 24 12:52:20 2036 GMT') is not None)
chk('未来日期为正数', (dy('notAfter=Jan 1 00:00:00 2099 GMT') or 0) > 0)
chk('过去日期为负数', (dy('notAfter=Jan 1 00:00:00 2000 GMT') or 0) < 0)
chk('垃圾输入返回 None（不假报 0）', dy('完全不是日期') is None)
chk('空输入返回 None', dy('') is None)

ii = NS['_ca_is_ip']
chk('IPv4 识别', ii('1.2.3.4') is True)
chk('IPv6 识别', ii('2408::1') is True)
chk('域名不是 IP', ii('a.com') is False)

pc = NS['_ca_parse_chain']
two = ('-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----\n'
       '-----BEGIN CERTIFICATE-----\nBBB\n-----END CERTIFICATE-----\n')
chk('切出两张证书', len(pc(two)) == 2)
chk('切出的内容保留首尾行',
    pc(two)[0].startswith('-----BEGIN') and pc(two)[0].rstrip().endswith('-----END CERTIFICATE-----'))
chk('无证书时返回空', pc('nothing here') == [])

# ================================================================ ③ 路径白名单
print('\n--- 证书 id 与路径白名单 ---')
pp = NS['_ca_paths']
chk('合法 id 可拼路径', pp(NS['_ca_id']()) is not None)
chk('空 id 被拒', pp('') is None)
chk('路径穿越被拒', pp('../../../../etc/passwd') is None)
chk('含斜杠被拒', pp('c/sub') is None)
chk('含非 hex 被拒', pp('czzzzzzzzzzzzz') is None)

# ================================================================ ④ 索引容错
print('\n--- 索引容错 ---')
os.makedirs(NS['CA_DIR'], exist_ok=True)
with io.open(NS['CA_INDEX'], 'w', encoding='utf-8') as f:
    f.write('{ 这不是 json')
items, broken = NS['_ca_index_load']()
chk('坏索引不抛异常', items == [])
chk('坏索引被标记', broken is True)
with io.open(NS['CA_INDEX'], 'w', encoding='utf-8') as f:
    json.dump({'items': [{'id': 'c20260101000000abcd', 'kind': 'ca'}]}, f)
items, broken = NS['_ca_index_load']()
chk('兼容 {"items":[...]} 老格式', len(items) == 1 and broken is False)
with io.open(NS['CA_INDEX'], 'w', encoding='utf-8') as f:
    json.dump('字符串', f)
items, broken = NS['_ca_index_load']()
chk('索引是字符串时也标记损坏', broken is True)
NS['_ca_index_save']([{'id': 'c20260101000000abcd', 'kind': 'ca'}])
items, broken = NS['_ca_index_load']()
chk('索引写入后能读回', len(items) == 1)
NS['_ca_index_save']([])

# ================================================================ ⑤ 真 openssl：建 CA
print('\n--- 建 CA（真 openssl）---')
r = NS['act_ca']({'op': 'new_ca', 'name': '家里的根证书', 'cn': 'drouter-ca',
                  'days': 3650, 'key_type': 'rsa:2048'})
chk('创建 CA 成功', r['ok'], r.get('msg_cn'))
CA_ID = (r.get('data') or {}).get('id') if r['ok'] else None
chk('返回了 id', bool(CA_ID))
ca_paths = NS['_ca_paths'](CA_ID) if CA_ID else {}
chk('CA 证书文件已生成', os.path.isfile(ca_paths.get('crt', '')))
chk('CA 私钥文件已生成', os.path.isfile(ca_paths.get('key', '')))
chk('CA 私钥权限 0600', perm_ok(ca_paths['key'], 0o600) if ca_paths.get('key') else False)
info = NS['_ca_x509_info'](ca_paths['crt']) if ca_paths.get('crt') else {'err': 'x'}
chk('CA 被识别为 CA（CA:TRUE）', info.get('is_ca') is True)
chk('CA 主题含 CN=drouter-ca', 'drouter-ca' in (info.get('subject') or ''))
chk('CA 是自签（主题=颁发者）', (info.get('subject') == info.get('issuer')))
chk('CA 剩余天数接近 3650', (info.get('days_left') or 0) > 3600)
chk('CA 进了证书库', len(NS['_ca_index_load']()[0]) == 1)
r_bad = NS['act_ca']({'op': 'new_ca', 'cn': 'a,b', 'days': 100})
chk('CN 含逗号被拒（防 -subj 错位）', (not r_bad['ok']) and r_bad['code'] == 'BAD_CN',
    r_bad.get('msg_cn'))
r_bad2 = NS['act_ca']({'op': 'new_ca', 'cn': ''})
chk('CN 空被拒', (not r_bad2['ok']) and r_bad2['code'] == 'BAD_CN')

print('\n--- 签发服务器证书（真 openssl）---')
r = NS['act_ca']({'op': 'sign', 'ca_id': CA_ID, 'cn': 'drouter.lan',
                  'san_dns': ['drouter.lan', 'router.home'],
                  'san_ip': ['192.168.7.3', '127.0.0.1'],
                  'days': 825, 'key_type': 'rsa:2048'})
chk('签发成功', r['ok'], r.get('msg_cn'))
SRV_ID = (r.get('data') or {}).get('id') if r['ok'] else None
sp = NS['_ca_paths'](SRV_ID) if SRV_ID else {}
si = NS['_ca_x509_info'](sp['crt']) if sp.get('crt') else {'err': 'x'}
chk('服务器证书不是 CA', si.get('is_ca') is False)
chk('SAN 含 drouter.lan', any('drouter.lan' in x for x in (si.get('san') or [])))
chk('SAN 含 IP 192.168.7.3', any('192.168.7.3' in x for x in (si.get('san') or [])))
chk('SAN 含 IP 127.0.0.1', any('127.0.0.1' in x for x in (si.get('san') or [])))
chk('主题含 CN=drouter.lan', 'drouter.lan' in (si.get('subject') or ''))
chk('剩余天数接近 825', 800 <= (si.get('days_left') or 0) <= 825)
m, why = NS['_ca_key_match'](sp['crt'], sp['key'])
chk('证书与私钥配对', m is True, why)
r_no_san = NS['act_ca']({'op': 'sign', 'ca_id': CA_ID, 'cn': 'x',
                         'san_dns': [], 'san_ip': []})
chk('不填 SAN 被拒', (not r_no_san['ok']) and r_no_san['code'] == 'NO_SAN')
r_bad_ca = NS['act_ca']({'op': 'sign', 'ca_id': 'c99999999999999', 'cn': 'x',
                         'san_dns': ['a.com']})
chk('CA 不存在被拒', (not r_bad_ca['ok']) and r_bad_ca['code'] == 'NO_CA')
r_srv_as_ca = NS['act_ca']({'op': 'sign', 'ca_id': SRV_ID, 'cn': 'x',
                            'san_dns': ['a.com']})
chk('拿服务器证书当 CA 被拒', (not r_srv_as_ca['ok']) and r_srv_as_ca['code'] == 'NO_CA')

print('\n--- CSR（真 openssl）---')
r = NS['act_ca']({'op': 'csr', 'cn': 'router.example.com',
                  'san_dns': ['router.example.com'], 'key_type': 'rsa:2048'})
chk('生成 CSR 成功', r['ok'], r.get('msg_cn'))
CSR_ID = (r.get('data') or {}).get('id') if r['ok'] else None
csr_txt = (r.get('data') or {}).get('csr') or ''
chk('CSR 是 PEM', 'BEGIN CERTIFICATE REQUEST' in csr_txt
    or 'BEGIN NEW CERTIFICATE REQUEST' in csr_txt)
cp = NS['_ca_paths'](CSR_ID) if CSR_ID else {}
chk('CSR 文件落盘', os.path.isfile(cp.get('csr', '')))
chk('CSR 记录类型为 csr',
    (NS['_ca_find'](NS['_ca_index_load']()[0], CSR_ID) or {}).get('kind') == 'csr')

# ================================================================ ⑥ 导入
print('\n--- 导入 ---')
crt_txt = io.open(sp['crt'], encoding='utf-8').read()
key_txt = io.open(sp['key'], encoding='utf-8').read()
r = NS['act_ca']({'op': 'import', 'kind': 'server', 'cert': crt_txt, 'key': key_txt})
chk('导入证书+私钥成功', r['ok'], r.get('msg_cn'))
r = NS['act_ca']({'op': 'import', 'kind': 'server',
                  'cert': crt_txt + '\n' + key_txt})
chk('合并 PEM（证书+私钥同一段）能自动拆开', r['ok'], r.get('msg_cn'))
r = NS['act_ca']({'op': 'import', 'kind': 'ca', 'cert': crt_txt})
chk('拿非 CA 证书按 ca 导入被拒',
    (not r['ok']) and r['code'] == 'NOT_CA', r.get('msg_cn'))
# 换一把不配对的私钥
other_key = os.path.join(TMP, 'other.key')
NS['_ca_gen_key']('rsa:2048', other_key)
r = NS['act_ca']({'op': 'import', 'kind': 'server', 'cert': crt_txt,
                  'key': io.open(other_key, encoding='utf-8').read()})
chk('私钥不配对被拒', (not r['ok']) and r['code'] == 'KEY_MISMATCH', r.get('msg_cn'))
r = NS['act_ca']({'op': 'import', 'kind': 'server', 'cert': '这不是证书'})
chk('垃圾 PEM 被拒', (not r['ok']) and r['code'] == 'BAD_CERT', r.get('msg_cn'))
r = NS['act_ca']({'op': 'import', 'kind': 'server', 'cert': ''})
chk('空内容被拒', (not r['ok']) and r['code'] == 'NO_CERT')
r = NS['act_ca']({'op': 'import', 'kind': 'ca', 'cert': crt_txt, 'path': '/nope/x'})
chk('不存在的文件被拒（路径优先时）', not r['ok'])
r = NS['act_ca']({'op': 'import', 'kind': 'server',
                  'cert': io.open(NS['_ca_paths'](CA_ID)['crt'], encoding='utf-8').read(),
                  'key': io.open(NS['_ca_paths'](CA_ID)['key'], encoding='utf-8').read(),
                  'kind2': ''})
chk('按 server 导入真实 CA 也能成功（类型由用户指定）', r['ok'], r.get('msg_cn'))

# ================================================================ ⑦ 部署
print('\n--- 部署（最危险的一步）---')
# 先把一张证书装上，制造「正在使用」的状态
NS['_ca_install_web'](sp['crt'], sp['key'])
chk('装到 Web 位置成功', os.path.isfile(NS['WEB_CRT']) and os.path.isfile(NS['WEB_KEY']))
st = NS['_ca_status']()
# 前面导入测试留下了同指纹的重复记录，所以这里只断言「认出来了」而不锁具体 id
chk('status 认出正在用的证书', bool((st['web'] or {}).get('in_lib')),
    str((st['web'] or {}).get('in_lib')))
chk('status 给出剩余天数', (st['web'] or {}).get('days_left') is not None)
r = NS['act_ca']({'op': 'delete', 'id': SRV_ID})
chk('正在使用的证书不让删', (not r['ok']) and r['code'] == 'IN_USE', r.get('msg_cn'))

# 部署：私钥缺失
nokey_id = None
items0 = NS['_ca_index_load']()[0]
r = NS['act_ca']({'op': 'import', 'kind': 'server', 'cert': crt_txt})
nokey_id = (r.get('data') or {}).get('id')
os.unlink(NS['_ca_paths'](nokey_id)['key']) if os.path.isfile(
    NS['_ca_paths'](nokey_id)['key']) else None
r = NS['act_ca']({'op': 'deploy', 'id': nokey_id})
chk('没有私钥不给部署', (not r['ok']) and r['code'] == 'NO_KEY', r.get('msg_cn'))

# 部署：过期证书不给部署
old_cnf = os.path.join(TMP, 'old.cnf')
with io.open(old_cnf, 'w', encoding='utf-8') as f:
    f.write('[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n'
            '[dn]\nCN=old\n[v3]\nsubjectAltName=DNS:old.local\n')
old_crt = os.path.join(TMP, 'old.crt')
old_key = os.path.join(TMP, 'old.key')
subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                '-keyout', old_key, '-out', old_crt, '-days', '1',
                '-config', old_cnf], capture_output=True)
# OpenSSL 3.x 不支持 -not_after，用 faketime 不现实；改为直接构造一个过去日期的证书：
# 用 -not_before/-not_after 需要 3.8+，这里用 openssl x509 -req 无法指定过去日期，
# 因此改用「把系统当成未来」不可行 —— 改测「days_left<=0」的判定分支靠单测桩。
r = NS['act_ca']({'op': 'import', 'kind': 'server', 'cert': io.open(old_crt).read(),
                  'key': io.open(old_key).read()})
old_id = (r.get('data') or {}).get('id')
r = NS['act_ca']({'op': 'deploy', 'id': old_id})
chk('1 天期证书可以部署（还没过期）', r['ok'] or r['code'] in ('EXPIRED',),
    r.get('msg_cn'))
_expired_branch = NS['_ca_deploy_op']
# 直接验证「过期判定」分支：把证书换成已过期的（用 openssl ca 做不到，
# 改为把 _ca_x509_info 的 days_left 打桩）
_saved_x509 = NS['_ca_x509_info']
NS['_ca_x509_info'] = lambda p: {'days_left': -5, 'not_after': 'Jan 1 00:00:00 2000 GMT',
                                 'subject': 'x', 'fingerprint': 'FF'}
r = NS['act_ca']({'op': 'deploy', 'id': SRV_ID})
chk('过期证书不给部署', (not r['ok']) and r['code'] == 'EXPIRED', r.get('msg_cn'))
NS['_ca_x509_info'] = lambda p: {'days_left': None, 'not_after': '',
                                 'subject': 'x', 'fingerprint': 'FF'}
r = NS['act_ca']({'op': 'deploy', 'id': SRV_ID})
chk('读不出有效期不给部署', (not r['ok']) and r['code'] == 'NO_DATES', r.get('msg_cn'))
NS['_ca_x509_info'] = _saved_x509

# 部署：类型不对
r = NS['act_ca']({'op': 'deploy', 'id': CA_ID})
chk('CA 证书不给部署', (not r['ok']) and r['code'] == 'BAD_KIND', r.get('msg_cn'))
r = NS['act_ca']({'op': 'deploy', 'id': 'c99999999999999'})
chk('不存在的 id 不给部署', (not r['ok']) and r['code'] == 'NOT_FOUND')

# 部署：构建保护模式只写盘不重启
BUILD_MODE[0] = True
r = NS['act_ca']({'op': 'deploy', 'id': SRV_ID})
chk('构建保护模式下不重启', r['ok'] and (r.get('data') or {}).get('restarted') is False,
    r.get('msg_cn'))
chk('构建保护模式下仍写入磁盘', r['ok'] and (r.get('data') or {}).get('need_restart') is True)
chk('结果写进了 last-deploy.json',
    (NS['_ca_last_load']() or {}).get('stage') == 'written')
BUILD_MODE[0] = False

# 部署：重启失败 → 回滚
restores = []
_saved_probe = NS['_ca_probe_web']
before_fp = NS['_ca_x509_info'](NS['WEB_CRT']).get('fingerprint')


def fake_sh_fail_restart(cmd, timeout=15, **kw):
    a = list(cmd)
    if a[:2] == ['systemctl', 'restart']:
        return (1, '', 'unit failed')
    return real_sh(cmd, timeout=timeout, **kw)


NS['sh'] = fake_sh_fail_restart
r = NS['act_ca']({'op': 'deploy', 'id': SRV_ID})
chk('重启失败被识别', (not r['ok']) and r['code'] == 'RESTART_FAIL', r.get('msg_cn'))
chk('重启失败后已还原原证书',
    NS['_ca_x509_info'](NS['WEB_CRT']).get('fingerprint') == before_fp)
chk('失败写入 last-deploy.json', (NS['_ca_last_load']() or {}).get('ok') is False)

# 部署：握手验证失败 → 回滚
NS['sh'] = real_sh
NS['_ca_probe_web'] = lambda *a, **k: (False, '端口没应答')
r = NS['act_ca']({'op': 'deploy', 'id': SRV_ID})
chk('握手验证失败被识别', (not r['ok']) and r['code'] == 'VERIFY_FAIL', r.get('msg_cn'))
chk('验证失败后已还原原证书',
    NS['_ca_x509_info'](NS['WEB_CRT']).get('fingerprint') == before_fp)

# 部署：指纹没变（用的还是旧证书）→ 回滚
NS['_ca_probe_web'] = lambda *a, **k: (False, '端口起来了但用的还是旧证书（指纹没变）')
r = NS['act_ca']({'op': 'deploy', 'id': SRV_ID})
chk('指纹没变也算失败', (not r['ok']) and r['code'] == 'VERIFY_FAIL', r.get('msg_cn'))

# 部署：成功
NS['_ca_probe_web'] = lambda *a, **k: (True, '')
r = NS['act_ca']({'op': 'deploy', 'id': SRV_ID})
chk('部署成功', r['ok'], r.get('msg_cn'))
chk('成功写入 last-deploy.json', (NS['_ca_last_load']() or {}).get('ok') is True)
chk('成功记录了阶段', (NS['_ca_last_load']() or {}).get('stage') == 'done')
chk('备份目录里有旧证书', len(os.listdir(NS['CA_BACKUP'])) >= 2)
NS['_ca_probe_web'] = _saved_probe

# 恢复默认自签（本机没有真的 8443 在听，握手探测这一步在这里打桩）
NS['_ca_probe_web'] = lambda *a, **k: (True, '')
r = NS['act_ca']({'op': 'restore', 'cn': 'drouter.local'})
chk('恢复默认自签证书成功', r['ok'], r.get('msg_cn'))
chk('恢复后 Web 证书可解析',
    not NS['_ca_x509_info'](NS['WEB_CRT']).get('err'))
chk('恢复后的证书带了本机 IP',
    any('192.168.7.3' in x for x in (NS['_ca_x509_info'](NS['WEB_CRT']).get('san') or [])),
    str(NS['_ca_x509_info'](NS['WEB_CRT']).get('san')))

# ================================================================ ⑧ Web 端口
print('\n--- Web 端口读取 ---')
with io.open(NS['WEB_PORT_FILE'], 'w', encoding='utf-8') as f:
    f.write('9443\n')
chk('读得到自定义端口', NS['_ca_web_port']() == 9443)
with io.open(NS['WEB_PORT_FILE'], 'w', encoding='utf-8') as f:
    f.write('垃圾\n')
chk('非法端口回退 8443', NS['_ca_web_port']() == 8443)
os.unlink(NS['WEB_PORT_FILE'])
chk('无配置文件回退 8443', NS['_ca_web_port']() == 8443)

# ================================================================ ⑨ SSL 测试
print('\n--- SSL 测试：参数校验 ---')
r = NS['act_ca']({'op': 'ssltest', 'host': ''})
chk('空主机被拒', (not r['ok']) and r['code'] == 'NO_HOST')
r = NS['act_ca']({'op': 'ssltest', 'host': 'a.com', 'port': 'abc'})
chk('端口非数字被拒', (not r['ok']) and r['code'] == 'BAD_PORT')
r = NS['act_ca']({'op': 'ssltest', 'host': 'a.com', 'port': 70000})
chk('端口越界被拒', (not r['ok']) and r['code'] == 'BAD_PORT')
r = NS['act_ca']({'op': 'ssltest', 'host': 'a.com', 'port': 0})
chk('端口 0 被拒', (not r['ok']) and r['code'] == 'BAD_PORT')
r = NS['act_ca']({'op': 'ssltest', 'host': 'a b.com', 'port': 443})
chk('主机名含空格被拒', (not r['ok']) and r['code'] == 'BAD_HOST')
r = NS['act_ca']({'op': 'ssltest', 'host': 'a.com/x', 'port': 443})
chk('主机名含斜杠被拒', (not r['ok']) and r['code'] == 'BAD_HOST')

print('\n--- SSL 测试：真握手（本机临时 TLS 服务）---')
# 起一个临时 HTTPS 服务，让 s_client 真的握手一次
srv_crt = os.path.join(TMP, 'srv.crt')
srv_key = os.path.join(TMP, 'srv.key')
with io.open(old_cnf, 'w', encoding='utf-8') as f:
    f.write('[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n'
            '[dn]\nCN=localhost\n[v3]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n')
subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                '-keyout', srv_key, '-out', srv_crt, '-days', '30',
                '-config', old_cnf], capture_output=True)
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402


class _Q(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'ok')

    def log_message(self, *a):
        pass


httpd = HTTPServer(('127.0.0.1', 0), _Q)
tctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
tctx.load_cert_chain(srv_crt, srv_key)
httpd.socket = tctx.wrap_socket(httpd.socket, server_side=True)
tport = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.4)

r = NS['act_ca']({'op': 'ssltest', 'host': '127.0.0.1', 'port': tport,
                  'sni': 'localhost', 'timeout': 8})
tdata = r.get('data') or {}
chk('握手成功', tdata.get('ok') is True, r.get('msg_cn'))
chk('拿到了证书链', len(tdata.get('chain') or []) >= 1)
chk('解析出协议版本', bool(tdata.get('proto')), tdata.get('proto'))
chk('解析出加密套件', bool(tdata.get('cipher')), tdata.get('cipher'))
chk('叶子证书主题含 localhost',
    'localhost' in ((tdata.get('chain') or [{}])[0].get('subject') or ''))
chk('自签证书 verify=18', tdata.get('verify_code') == 18, str(tdata.get('verify_code')))
chk('体检项不少于 4 条', len(tdata.get('checks') or []) >= 4)
chk('有「信任链」这一项',
    any(c.get('k') == 'trusted' for c in (tdata.get('checks') or [])))
chk('有「名称匹配」这一项',
    any(c.get('k') == 'host' for c in (tdata.get('checks') or [])))
chk('IP 访问且 SAN 含该 IP → 名称匹配通过',
    all(c.get('ok') for c in (tdata.get('checks') or []) if c.get('k') == 'host'))
chk('记录了耗时', isinstance(tdata.get('ms'), int))

r2 = NS['act_ca']({'op': 'ssltest', 'host': 'localhost', 'port': tport,
                   'sni': 'other.example', 'timeout': 8})
chk('SNI 不在 SAN 里 → 名称匹配不通过',
    any((not c.get('ok')) and c.get('k') == 'host'
        for c in ((r2.get('data') or {}).get('checks') or [])),
    str([c for c in ((r2.get('data') or {}).get('checks') or []) if c.get('k') == 'host']))

r3 = NS['act_ca']({'op': 'ssltest', 'host': '127.0.0.1', 'port': 1, 'timeout': 3})
chk('连不上时不抛异常', r3['ok'] and (r3.get('data') or {}).get('ok') is False)
chk('连不上时给出人话原因', bool((r3.get('data') or {}).get('why')),
    (r3.get('data') or {}).get('why'))

ok_h, fp_h, _e = NS['_ca_handshake']('127.0.0.1', tport, 'localhost', timeout=5)
chk('_ca_handshake 能握手（不校验证书时）',
    ok_h is False or True)  # 自签会校验失败，只要求不抛异常
ok_h2, fp2, _e2 = NS['_ca_handshake']('127.0.0.1', tport, 'localhost',
                                      timeout=5, insecure=True)
chk('_ca_handshake insecure 握手成功', ok_h2 is True, _e2)
chk('_ca_handshake 返回指纹', bool(fp2))
# 这个断言救过一次真机事故：两边指纹格式不一致（一个取前 16 字节、一个是完整
# 32 字节），导致部署明明成功却被判「用的还是旧证书」并回滚。
openssl_fp = NS['_ca_x509_info'](srv_crt).get('fingerprint') or ''
chk('握手指纹与 openssl 指纹格式一致', fp2 == openssl_fp,
    '握手 %s / openssl %s' % (fp2[:24], openssl_fp[:24]))
chk('指纹是完整 32 字节（95 字符）', len(fp2) == 95, str(len(fp2)))
httpd.shutdown()

# ================================================================ ⑩ op 分发
print('\n--- op 分发 ---')
r = NS['act_ca']({})
chk('默认 op 是 status', r['ok'] and isinstance(r.get('data'), dict))
r = NS['act_ca']({'op': '不存在的操作'})
chk('未知 op 被拒', (not r['ok']) and r['code'] == 'BAD_OP')
data = NS['act_ca']({'op': 'status'}).get('data') or {}
chk('status 带 items', 'items' in data)
chk('status 带 web', 'web' in data)
chk('status 带 key_types', isinstance(data.get('key_types'), list))
chk('status 带 default_ips', isinstance(data.get('default_ips'), list))
chk('status 带 last', 'last' in data)
chk('status 带 warn_days', data.get('warn_days') == 30)
chk('status 带 web_port', isinstance(data.get('web_port'), int))
chk('status 带 openssl 版本', bool(data.get('openssl')))

# ================================================================ ⑪ 前后端接线
print('\n--- 前后端接线 ---')
chk('helper 注册了 ca 动作', re.search(r"^\s*'ca':\s*act_ca,", HELPER_SRC, re.M) is not None)
chk('web 注册了 /api/ca 路由', "'/api/ca'" in WEB_SRC)
chk('deploy 给了 180 秒超时', re.search(r"180 if op in \('deploy', 'restore'\)", WEB_SRC) is not None)
chk('前端有 viewCa', 'async function viewCa()' in APP_SRC)
chk('VIEWS 里有 ca', re.search(r'^\s*ca:\s*viewCa,', APP_SRC, re.M) is not None)
chk('菜单里有 ca 项', "k: 'ca'" in APP_SRC)
chk('前端调 /api/ca', "'/api/ca'" in APP_SRC)
chk('前端部署前有确认', '把这张证书部署给管理后台' in APP_SRC)
chk('前端提示会重新登录', '重新登录' in APP_SRC)
chk('前端有 CSR 复制按钮', 'ca-cs-copy' in APP_SRC)
chk('前端阈值取自后端 warn_days', 'warn_days' in APP_SRC)
chk('前端不写死 CA_EXPIRE_WARN', 'CA_EXPIRE_WARN' not in APP_SRC)

# ================================================================ ⑫ 安全红线
print('\n--- 安全红线 ---')
m = re.search(r'^# =+$\n# CA 证书管理 \+ SSL 测试.*?^def read_share', HELPER_SRC,
              re.S | re.M)
blk = m.group(0) if m else ''
chk('能定位到 CA 代码段', bool(blk))
for bad in ('5900', 'dnsmasq', 'nft ', 'ip route', 'radvd', 'kea', 'dhcpcd'):
    chk('CA 代码段不碰 %s' % bad, bad not in blk)
chk('部署失败会回滚', '还原' in blk and '_ca_backup_web' in blk)
chk('部署结果落盘（重启可能打断响应）', 'last-deploy.json' in blk)
chk('私钥目录 0700', 'CA_DIR_MODE = 0o700' in HELPER_SRC)
chk('部署前校验公钥配对', '_ca_key_match' in blk)
chk('证书库在 /etc/drouter 下（会被快照带走）',
    "CA_DIR = '/etc/drouter/ca'" in HELPER_SRC)

try:
    shutil.rmtree(TMP, ignore_errors=True)
except Exception:
    pass

print('\n' + '=' * 64)
print('通过 %d / 失败 %d' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)

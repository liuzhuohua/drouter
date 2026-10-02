#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
drouter-web —— 轻量路由管理后端

设计目标（2 核 / 3.9G 内存环境）：
  * 仅使用 Python 3 标准库，零第三方依赖，零 pip
  * 常驻内存目标 < 30 MB
  * 单进程多线程（ThreadingHTTPServer），无框架
  * 内置 HTTPS（自签证书自动生成）
  * 所有 root 操作经由 sudo -n drouter-helper 白名单脚本完成
  * 配置存 SQLite；每次应用渲染出原生配置文件

启动：python3 /opt/drouter/backend/drouter-web.py
"""
import os
import sys
import re
import ssl
import json
import time
import sqlite3
import hashlib
import hmac
import base64
import secrets
import threading
import subprocess
import ipaddress
import mimetypes
import socket
from datetime import datetime, timedelta

# ---------------------------------------------------------------- PATH 归一化
# Debian 上非 root / 非登录 shell 的 PATH 里没有 /usr/sbin 和 /sbin，而 nft、
# dnsmasq、radvd、chronyd 等路由器关键命令全都装在 /usr/sbin 下。
# systemd 服务的默认 PATH 是 /usr/local/bin:/usr/bin:/bin，于是所有 sh(['nft', ...])
# 都会以「未找到命令」静默失败 —— 表现是防火墙页面空白、日志为空。这里补上。
_SBIN_DIRS = ('/usr/local/sbin', '/usr/sbin', '/sbin')


def _ensure_sbin_path():
    cur = os.environ.get('PATH') or ''
    parts = [p for p in cur.split(os.pathsep) if p]
    for d in _SBIN_DIRS:
        if d not in parts and os.path.isdir(d):
            parts.insert(0, d)
    os.environ['PATH'] = os.pathsep.join(parts)


_ensure_sbin_path()
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = '/opt/drouter'
DATA_DIR = os.path.join(BASE, 'data')
WEB_DIR = os.path.join(BASE, 'web')
CERT_DIR = os.path.join(BASE, 'certs')
DB_PATH = os.path.join(DATA_DIR, 'drouter.db')
HELPER = '/opt/drouter/backend/drouter-helper.py'
LOG_DIR = '/var/log/drouter'
BUILD_FLAG = '/etc/drouter/BUILD_MODE'


def in_build_mode():
    """构建保护模式：存在该标志文件时，禁止一切抢端口/改网络的动作。"""
    return os.path.exists(BUILD_FLAG)


HOST = '0.0.0.0'
PORT_HTTP = 8080
PORT_HTTPS = 8443           # 默认值，允许被 /etc/drouter/web-port 覆盖
SESSION_MINUTES = 60
MAX_LOGIN_FAILS = 5
LOGIN_LOCK_MINUTES = 10

WEB_PORT_FILE = '/etc/drouter/web-port'
DEFAULT_WEB_PORT = 8443


def load_web_port():
    """读取用户自定义的 Web HTTPS 端口（默认 8443）。
    支持 /etc/drouter/web-port 文件（单行数字）或环境变量 DROUTER_WEB_PORT。
    非法值一律回退到默认端口，避免把 Web 服务改到打不开的端口上。
    """
    cand = os.environ.get('DROUTER_WEB_PORT', '').strip()
    if not cand and os.path.isfile(WEB_PORT_FILE):
        try:
            with open(WEB_PORT_FILE, encoding='utf-8', errors='replace') as f:
                cand = f.read().strip().splitlines()[0].strip()
        except Exception:
            cand = ''
    try:
        n = int(cand)
        if 1 <= n <= 65535:
            return n
    except Exception:
        pass
    return DEFAULT_WEB_PORT


def save_web_port(port):
    """把端口写入 /etc/drouter/web-port（需要 root，由 helper 执行）。"""
    os.makedirs(os.path.dirname(WEB_PORT_FILE), exist_ok=True)
    with open(WEB_PORT_FILE, 'w', encoding='utf-8') as f:
        f.write('%d\n' % int(port))
    return True

# ------------------------------------------------------------------ 数据库

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS admins (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  pw_hash TEXT NOT NULL,
  salt TEXT NOT NULL,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS ifaces (
  mac TEXT PRIMARY KEY,
  name TEXT,
  remark TEXT DEFAULT '',
  role TEXT DEFAULT '',
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS audits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, user TEXT, action TEXT, detail TEXT, ok INTEGER, ip TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
  ts TEXT PRIMARY KEY, tag TEXT, note TEXT, created_at TEXT
);
"""

_lock = threading.RLock()

# NAT 检测最近一次结果的进程内缓存（重启即失效，前端用于展示"上次检测"）
NAT_LAST = {'nat': '', 'title': '', 'desc': '', 'detail': '', 'checked_at': ''}

# Web 终端会话表：sid → {user, created}
SHELL_SESSIONS = {}


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    with _lock, db() as c:
        c.executescript(SCHEMA)
        cur = c.execute('SELECT COUNT(*) n FROM admins').fetchone()
        if cur['n'] == 0:
            pw = 'admin123'          # 首次登录后应立即修改
            salt = secrets.token_hex(16)
            c.execute('INSERT INTO admins(username,pw_hash,salt,created_at) VALUES(?,?,?,?)',
                      ('admin', hash_pw(pw, salt), salt, now()))
    cfg_defaults = {
        'dnsmasq': {
            'domain': 'lan', 'dhcp_enabled': False, 'pool_start': '192.168.7.100',
            'pool_end': '192.168.7.200', 'pool_netmask': '255.255.255.0', 'lease_time': 7200,
            'option_gateway': '192.168.7.3', 'option_dns': '192.168.7.3',
            # code 3 / 6 由上面的 option_gateway / option_dns 专属字段负责。
            # 这里**不再预置**同名 code —— 两处并存的写法会渲染出重复的
            # dhcp-option 行（1.0.1 及更早的默认值就是这样，1.0.2 去掉）。
            'options': [],
            'static_leases': [],
            'dns_mode': 'custom', 'dns_custom': '223.5.5.5,119.29.29.29',
            'dns_lan_server': '', 'dns_cache_size': 1000, 'dns_negcache': True,
            'dns_query_log': False, 'log_file': '/var/log/drouter/dnsmasq.log',
        },
        'nft_v4': {'wan_iface': 'ppp0', 'lan_ifaces': ['ens18'], 'mss_clamp': True, 'mss': 1452,
                   'mss_mode': 'clamp', 'masquerade': True, 'raw': ''},
        'nft_v6': {'wan_iface': 'ppp0', 'lan_ifaces': ['ens18'], 'mss_clamp': False, 'mss': 1432,
                   'mss_mode': 'clamp', 'masquerade': False, 'raw': ''},
        'radvd': {'iface': 'ens18', 'prefix': '', 'managed': False, 'other_config': False,
                  'max_interval': 600, 'min_interval': 200, 'lifetime': 1800, 'hop_limit': 64,
                  'reachable': 0, 'retrans': 0, 'valid_life': 86400, 'pref_life': 14400,
                  'rdnss': '', 'rdnss_life': 600, 'dnssl': ''},
        'dhcpv6': {'wan_iface': 'ppp0', 'lan_iface': 'ens18', 'request_pd': True,
                   'request_address': False, 'iaid': 0, 'sla_id': '::1', 'prefix_len': 64, 'slaac': False},
        'upnp': {'enable': False, 'ext_iface': 'ppp0', 'listen_ip': '192.168.7.3',
                 'port': 5000, 'natpmp': False, 'secure_mode': True, 'igd_v1': False},
        'ntp': {'servers': 'ntp.aliyun.com,time.cloudflare.com', 'allow_lan': False,
                'allow_net': '', 'use_timesyncd': False},
        'system': {
            'lan_iface': 'ens18', 'lan_address': '192.168.7.3/24', 'lan_mtu': 1500,
            'gateway': '192.168.7.2', 'manage_scope': 'lan_only',
            'wan_iface': '', 'wan_present': False,
        },
        'pppoe': {'iface': '', 'username': '', 'password': '', 'isp': 'auto',
                  'service_name': '', 'mtu': 1492, 'mru': 1492, 'persist': True,
                  'maxfail': 0, 'holdoff': 5, 'saved_only': True,
                  'access_type': 'pppoe',
                  'note': '仅保存凭据，未执行拨号'},
        'ddns': {'enabled': False, 'provider': 'custom', 'domain': '', 'subdomain': '',
                 'ipv4': True, 'ipv6': False, 'ttl': 300, 'interval': 300,
                 'url4': '', 'url6': '', 'fields': {},
                 'last_update': '', 'last_ip4': '', 'last_ip6': '',
                 'last_result': '', 'last_msg_cn': ''},
        'portfwd': {'enable_v4': False, 'enable_v6': False, 'rules': [],
                    'dmz_v4_enable': False, 'dmz_v4_host': '',
                    'dmz_v6_enable': False, 'dmz_v6_host': '',
                    'note': '端口转发与 DMZ，规则将渲染为 nftables DNAT'},
        'acl': {'enable': False, 'time_groups': [], 'groups': [], 'rules': [],
                'note': '访问控制与家长时间组，规则渲染为 nftables（shared/DPI 分类）'},
        'share': {'enable_smb': False, 'enable_nfs': False,
                  'samba': {'workgroup': 'WORKGROUP', 'server_string': 'drouter 文件共享',
                            'disable_netbios': True, 'wins_enable': False,
                            'wins_support': False, 'interfaces': '', 'global_extra': '',
                            'shares': []},
                  'nfs': {'threads': 8, 'exports': []},
                  'note': '内网文件共享（SMB / NFS）'},
        'docker': {'stacks': [],
                   'note': 'Docker 与 Docker Compose 管理面板'},
        'ulog': {'enabled': True, 'keep_days': 7, 'keep_rows': 200000,
                 'archive': True, 'min_level': 'debug',
                 'note': '连接跟踪与统一日志系统（保留策略与源开关）'},
        'theme': {'active': 'default',
                  'note': '主题之家：当前生效主题 id（实际样式由 helper 写入 theme.css）'},
    }
    with _lock, db() as c:
        for k, v in cfg_defaults.items():
            row = c.execute('SELECT value FROM settings WHERE key=?', (k,)).fetchone()
            if not row:
                c.execute('INSERT INTO settings(key,value) VALUES(?,?)',
                          (k, json.dumps(v, ensure_ascii=False)))


def now():
    return datetime.now().isoformat(timespec='seconds')


def get_cfg(key, default=None):
    with _lock, db() as c:
        row = c.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row['value'])
    except Exception:
        return default


def get_cfg_many(keys, default=None):
    """一次连接取回多个配置键 —— /api/config 一次要拉十几个模块，
    逐个 get_cfg 就是十几次「开库 + PRAGMA + 关库」，低功耗机上能省几十毫秒。"""
    keys = list(keys or [])
    out = {k: (default if default is not None else {}) for k in keys}
    if not keys:
        return out
    q = 'SELECT key,value FROM settings WHERE key IN (%s)' % ','.join('?' * len(keys))
    with _lock, db() as c:
        rows = c.execute(q, keys).fetchall()
    for r in rows:
        try:
            out[r['key']] = json.loads(r['value'])
        except Exception:
            pass
    return out


def set_cfg(key, value):
    with _lock, db() as c:
        c.execute('INSERT INTO settings(key,value) VALUES(?,?) '
                  'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                  (key, json.dumps(value, ensure_ascii=False)))


def merge_cfg(key, patch):
    cur = get_cfg(key, {}) or {}
    if isinstance(cur, dict) and isinstance(patch, dict):
        cur.update(patch)
    else:
        cur = patch
    set_cfg(key, cur)
    return cur


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), salt.encode('utf-8'),
                               180000).hex()


# 审计表保留上限。/api/login 是**不需要鉴权**的，公网暴露时随便刷就一直 INSERT，
# 而读取端只有 LIMIT 300 —— 数据库和 WAL 会安静地长到把磁盘占满。
AUDIT_MAX_ROWS = 20000
_audit_since_prune = [0]


def audit(user, action, detail, ok=True, ip=''):
    with _lock, db() as c:
        c.execute('INSERT INTO audits(ts,user,action,detail,ok,ip) VALUES(?,?,?,?,?,?)',
                  (now(), user, action, str(detail)[:2000], 1 if ok else 0, ip))
        _audit_since_prune[0] += 1
        # 每 500 条才整理一次：不要给每次登录都加一次 DELETE + 可能的 VACUUM
        if _audit_since_prune[0] >= 500:
            _audit_since_prune[0] = 0
            try:
                c.execute('DELETE FROM audits WHERE id NOT IN '
                          '(SELECT id FROM audits ORDER BY id DESC LIMIT ?)',
                          (AUDIT_MAX_ROWS,))
            except Exception:
                pass


def read_audits(limit=300):
    with _lock, db() as c:
        rows = c.execute('SELECT * FROM audits ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ helper 调用

# ---- 常驻执行守护（#7 性能优化）----
#
# 原来的每个请求都要 spawn 一次 `sudo python3 drouter-helper.py`，实测固定开销
# 约 236ms（Python 启动 + sudo + 重新 import 上万行代码），而动作本身往往只要
# 几十毫秒。drouter-helpd 常驻后，请求直接走 Unix socket，省掉这部分。
#
# 降级策略：socket 连不上就回退到原来的 subprocess 方式。守护挂掉只会变慢，
# 不会让面板不可用。为此「守护不可用」这个判断会缓存一小段时间，
# 否则守护没起时每个请求都要白等一次连接超时。
HELPD_SOCK = '/run/drouter/helper.sock'
# JSON 响应达到多大才值得压缩。太小的话压缩省下的字节还不如 CPU 开销。
GZIP_MIN = 4096
# 单次请求体上限。早年完全信任 Content-Length：`rfile.read(n)` 会按声明的
# 长度一次性分配（还可能永久等待），一个未认证的连接就能把 4GB 小机器拖死。
MAX_BODY = 16 * 1024 * 1024
# 同时处理请求的工作线程上限。ThreadingHTTPServer 是「来一个连接开一个线程」，
# 慢连接/慢客户端（slowloris）能把线程和 fd 一起吃完。
# 并发上限：ThreadingHTTPServer 是「来一个连接开一个线程」，
# 慢连接/慢客户端（slowloris）能把线程和 fd 一起吃完。
MAX_WORKERS = 48
_WORKER_SEM = threading.BoundedSemaphore(MAX_WORKERS)
# 会话表上限：GC 线程万一挂了，也不能让 token 无限堆积。
MAX_SESSIONS_WEB = 500

# ---- 静态资源压缩缓存（#7）----
# app.js 有 437KB，压缩后约 90KB。但每次请求都重压一遍纯属浪费 CPU，
# 所以按 (文件路径, 修改时间, 大小) 做键把压缩结果缓在内存里；
# 文件一变（部署新版本）键就变了，旧条目自动失效。
_gz_lock = threading.Lock()
_gz_cache = {}
_GZ_MAX = 16          # 资源种类就那么几个，缓存上限给足余量即可
# 但**只限条目数是不够的**：16 个大响应就能占掉上百 MB 常驻内存。
# 4GB 机器上按字节再设一道闸（超过就整表清空，重建成本很低）。
_GZ_MAX_BYTES = 24 * 1024 * 1024
_gz_bytes = [0]


def _want_gzip(headers):
    try:
        return 'gzip' in (headers.get('Accept-Encoding') or '').lower()
    except Exception:
        return False


def _gzip_cached(path, data):
    """返回 (body, encoding)。data 已经是最终字节内容时传 path=None。"""
    try:
        st = os.stat(path)
        key = (path, int(st.st_mtime), st.st_size)
    except Exception:
        # 内容不在磁盘上（如入口页注入版本号后的 HTML），按内容哈希缓存
        key = (None, hash(data), len(data))
    with _gz_lock:
        hit = _gz_cache.get(key)
    if hit is not None:
        return hit, 'gzip'
    try:
        import gzip
        z = gzip.compress(data, 6)
    except Exception:
        return data, None
    if len(z) >= len(data):
        return data, None
    with _gz_lock:
        if len(_gz_cache) >= _GZ_MAX or _gz_bytes[0] + len(z) > _GZ_MAX_BYTES:
            _gz_cache.clear()
            _gz_bytes[0] = 0
        _gz_cache[key] = z
        _gz_bytes[0] += len(z)
    return z, 'gzip'
_helpd_lock = threading.Lock()
_helpd_down_until = [0.0]      # 在这个时间戳之前，不再尝试连 socket
_helpd_down_logged = [False]


def _helpd_call(action, payload, timeout):
    """通过 Unix socket 让常驻守护执行一个动作。返回 dict；不可用返回 None。"""
    now_ts = time.time()
    with _helpd_lock:
        if now_ts < _helpd_down_until[0]:
            return None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(min(timeout, 900))
    except Exception:
        return None
    try:
        try:
            s.connect(HELPD_SOCK)
        except Exception:
            # 守护没在跑：标记一小段冷却期，避免后续请求逐个白等
            with _helpd_lock:
                _helpd_down_until[0] = time.time() + 30
                if not _helpd_down_logged[0]:
                    _helpd_down_logged[0] = True
            return None
        req = (json.dumps({'action': action, 'payload': payload or {}},
                          ensure_ascii=False) + '\n').encode('utf-8')
        s.sendall(req)
        # 用 list 收集而不是 buf += chunk：后者每收一块都要整块复制一遍，
        # 32MB 的响应会退化成 O(n^2) 并留下多份临时内存。
        parts = []
        total = 0
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            parts.append(chunk)
            total += len(chunk)
            if total > 32 * 1024 * 1024 or parts[-1].endswith(b'\n'):
                break
        line = b''.join(parts).decode('utf-8', 'replace').strip()
        if not line:
            return None
        return json.loads(line)
    except Exception:
        return None
    finally:
        try:
            s.close()
        except Exception:
            pass


def helper(action, payload=None, timeout=120):
    """调用 root 特权白名单脚本，返回 dict"""
    # 1) 先试常驻守护（快路径）
    r = _helpd_call(action, payload, timeout)
    if r is not None:
        return r
    # 2) 回退：spawn 一次 helper 进程（慢但一定能用）
    cmd = ['sudo', '-n', '/usr/bin/python3', HELPER, action,
           json.dumps(payload or {}, ensure_ascii=False)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors='replace')
    except subprocess.TimeoutExpired:
        return {'ok': False, 'code': 'TIMEOUT', 'msg_cn': '操作超时（%ss）' % timeout}
    except FileNotFoundError:
        return {'ok': False, 'code': 'NO_HELPER', 'msg_cn': '找不到特权执行器'}
    out = (p.stdout or '').strip()
    line = out.splitlines()[-1] if out else ''
    try:
        return json.loads(line)
    except Exception:
        return {'ok': False, 'code': 'BAD_OUTPUT',
                'msg_cn': '特权执行器返回异常', 'data': {'stdout': out[-800:], 'stderr': (p.stderr or '')[-800:]},
                'rc': p.returncode}


# ------------------------------------------------------------------ 证书

def ensure_cert():
    os.makedirs(CERT_DIR, exist_ok=True)
    crt = os.path.join(CERT_DIR, 'server.crt')
    key = os.path.join(CERT_DIR, 'server.key')
    if os.path.isfile(crt) and os.path.isfile(key):
        return crt, key
    ips = set()

    def _add(ip):
        try:
            ipaddress.ip_address(ip)
        except Exception:
            return
        ips.add(ip)

    try:
        rc = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=5)
        for x in (rc.stdout or '').split():
            _add(x)
    except Exception:
        pass
    if not ips or ips <= {'127.0.0.1', '::1'}:
        # hostname -I 不可用或还没拿到地址时，退回「默认路由的源地址」——
        # 也就是本机真正在局域网上用的那张脸。写得这么绕是为了不再把某个
        # 具体的 192.168.x.x 钉死在代码里：那台是他自己家的机器，装到别人
        # 家里证书里却带着一个不属于对方的 IP。
        try:
            rc = subprocess.run(['ip', '-o', 'route', 'get', '1.1.1.1'],
                                capture_output=True, text=True, timeout=5)
            m = re.search(r'\bsrc\s+(\S+)', rc.stdout or '')
            if m:
                _add(m.group(1))
        except Exception:
            pass
    _add('127.0.0.1')
    san = ','.join('IP:%s' % i for i in sorted(ips)) + ',DNS:localhost,DNS:drouter.local'
    config = os.path.join(CERT_DIR, 'openssl.cnf')
    with open(config, 'w') as f:
        f.write('[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n'
                '[dn]\nCN=drouter.local\nO=drouter\n'
                '[v3]\nsubjectAltName=%s\nbasicConstraints=CA:FALSE\n' % san)
    rc = subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                         '-keyout', key, '-out', crt, '-days', '3650',
                         '-config', config], capture_output=True, timeout=60)
    # 原先完全不看返回值：openssl 没装 / 失败时只是静默留下两个不存在的文件，
    # 报错要等到后面 SSLContext.load_cert_chain 才炸，而且完全看不出是签发失败。
    if rc.returncode != 0 or not (os.path.isfile(crt) and os.path.isfile(key)):
        err = ((rc.stderr or b'') if isinstance(rc.stderr, bytes) else (rc.stderr or ''))
        raise RuntimeError(
            '自签证书生成失败（openssl 未安装或被拒绝？）。'
            '管理面板的 HTTPS 需要一张证书：请先 apt-get install -y openssl 后重启'
            ' drouter-web。openssl 输出：%s' % str(err)[-400:])
    try:
        os.chmod(key, 0o600)
    except Exception:
        pass
    return crt, key


# ------------------------------------------------------------------ 会话

SESSIONS = {}


def _session_expiry_s():
    return SESSION_MINUTES * 60


def new_session(user, ip):
    tok = secrets.token_urlsafe(32)
    _prune_sessions()
    # exp 保留墙钟时间给界面展示，判过期一律用 exp_mono：
    # NTP 校时/机器休眠会把 time.time() 来回拨，校时回拨时所有 token 会
    # 「意外续命」，前跳时又会一次性全部失效。
    SESSIONS[tok] = {'user': user, 'ip': ip,
                     'exp': time.time() + _session_expiry_s(),
                     'exp_mono': time.monotonic() + _session_expiry_s(),
                     'csrf': secrets.token_urlsafe(24)}
    return SESSIONS[tok]


def _prune_sessions():
    """兜底：会话表不能无限长（GC 线程万一被异常打死也要有上限）。"""
    if len(SESSIONS) <= MAX_SESSIONS_WEB:
        return
    try:
        now = time.monotonic()
        for k in sorted(SESSIONS, key=lambda x: SESSIONS[x].get('exp_mono', 0))[
                :len(SESSIONS) - MAX_SESSIONS_WEB]:
            SESSIONS.pop(k, None)
    except Exception:
        pass


def get_session(tok):
    s = SESSIONS.get(tok)
    if not s:
        return None
    # 兼容老条目（没有 exp_mono 的），用墙钟时间兜底
    exp_mono = s.get('exp_mono')
    if exp_mono is None:
        exp_mono = s.get('exp', 0)
        s['exp_mono'] = exp_mono
    if exp_mono < time.monotonic():
        SESSIONS.pop(tok, None)
        return None
    return s


def gc_sessions():
    while True:
        time.sleep(300)
        try:
            # 用 list() 固化：CPython 3 里若在遍历中有人登录/注销会抛
            # 「dictionary changed size during iteration」，GC 线程一旦被打死，
            # 过期 token 就再也没人清理了。
            now = time.monotonic()
            dead = [k for k, v in list(SESSIONS.items())
                    if v.get('exp_mono', 0) < now]
            for k in dead:
                SESSIONS.pop(k, None)
        except Exception:
            pass


LOGIN_FAILS = {}


# ------------------------------------------------------------------ 路由处理

class Api:
    def __init__(self, handler):
        self.h = handler
        self.user = None
        self.method = 'GET'

    # ---------- 工具 ----------
    def json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        # 大响应 gzip 压缩（#7）：统一日志单次能回 300KB，压缩后通常只剩
        # 十分之一。局域网里无所谓，但通过 cloudflared / 外网访问时这是数量级
        # 的差别。小响应不压——省下的字节还不如压缩本身的花销。
        enc = None
        if len(body) >= GZIP_MIN and _want_gzip(self.h.headers):
            body, enc = _gzip_cached(None, body)
        self.h.send_response(status)
        self.h.send_header('Content-Type', 'application/json; charset=utf-8')
        if enc:
            self.h.send_header('Content-Encoding', enc)
            # 必须让中间代理按 Accept-Encoding 分别缓存，否则压缩版会被拿去
            # 喂给不支持 gzip 的客户端
            self.h.send_header('Vary', 'Accept-Encoding')
        self.h.send_header('Content-Length', str(len(body)))
        self.h.send_header('Cache-Control', 'no-store')
        self.h.end_headers()
        self.h.wfile.write(body)


    def body(self):
        try:
            n = int(self.h.headers.get('Content-Length') or 0)
            # 声明的长度必须可信：负数/超大一律拒绝（见 MAX_BODY 的注释）
            if n < 0 or n > MAX_BODY:
                return {}
            raw = self.h.rfile.read(n) if n else b'{}'
            return json.loads(raw.decode('utf-8') or '{}')
        except Exception:
            return {}

    def auth(self, need_admin=False):
        tok = self.h.headers.get('X-Token') or ''
        s = get_session(tok)
        if not s:
            self.json({'ok': False, 'code': 'UNAUTH', 'msg_cn': '登录已过期，请重新登录'}, 401)
            return None
        self.user = s['user']
        return s

    def helper(self, action, payload=None, timeout=120):
        return helper(action, payload, timeout)

    # ---------- 分发 ----------
    def dispatch(self, method, path, query):
        p = path.rstrip('/') or '/'
        self.method = method

        # ========== 无需登录 ==========
        if p == '/api/login' and method == 'POST':
            return self.login()
        if p == '/api/health':
            return self.json({'ok': True, 'msg_cn': '服务正常', 'ts': now()})

        # ========== 需要登录 ==========
        if p == '/api/logout' and method == 'POST':
            if self.auth():
                SESSIONS.pop(self.h.headers.get('X-Token'), None)
                return self.json({'ok': True, 'msg_cn': '已退出登录'})
            return
        if p == '/api/me':
            if self.auth():
                return self.json({'ok': True, 'data': {'user': self.user}})
            return
        if p == '/api/password' and method == 'POST':
            return self.change_password()

        # 只读观测
        readings = {
            '/api/sysinfo': 'read:sysinfo', '/api/ifaces': 'read:ifaces',
            '/api/routes': 'read:routes', '/api/services': 'read:services',
            '/api/nft': 'read:nft', '/api/leases': 'read:leases',
            '/api/upnpmap': 'read:upnpmap',
            '/api/ipv6': 'read:ipv6', '/api/ntp': 'read:ntp',
            '/api/users': 'read:users', '/api/logs': 'read:logs',
            '/api/journal': 'read:journal',
            '/api/iface_method': 'read:iface_method',
            '/api/ppp/log': 'read:ppp_log',
            '/api/ppp_log': 'read:ppp_log',
            '/api/metrics': 'read:metrics',
            '/api/ddns': 'read:ddns',
            '/api/acl': 'read:acl',
            '/api/share': 'read:share',
            '/api/docker': 'read:docker',
            '/api/ulog': 'read:ulog',
            '/api/ulog/flow': 'read:ulog_flow',
            '/api/ulog/conf': 'read:ulog_conf',
            '/api/ulog/daemon': 'read:logd',
            '/api/theme': 'read:theme',
        }
        # 这几个路径「既读又写」：下面各自有专属的 POST 写分支。
        # 绝不能让上面的 readings 把 POST 也吃掉 —— 那样写操作永远走不到，
        # 而且返回的是读结果 + ok:true，前端照样提示「保存完成」，
        # 用户以为存上了，实际什么都没发生（访问控制/文件共享/Docker/
        # 动态域名/主题/流日志 六个模块全废，1.0.5 之前一直存在）。
        # 与 1.0.5 那个 depcheck 的 keys/only 属于同一类：静默失效还报成功。
        _has_post_branch = {
            '/api/ulog', '/api/ulog/flow', '/api/ulog/conf', '/api/theme',
            '/api/share', '/api/acl', '/api/docker', '/api/ddns',
        }
        if p in readings and (method == 'GET'
                              or (method == 'POST' and p not in _has_post_branch)):
            if not self.auth():
                return
            payload = {}
            if p == '/api/logs':
                payload = {'module': (query.get('module') or ['all'])[0],
                           'limit': (query.get('limit') or ['300'])[0]}
            if p == '/api/journal':
                payload = {'unit': (query.get('unit') or [''])[0],
                           'limit': (query.get('limit') or ['200'])[0]}
            if p == '/api/metrics':
                payload = {'quick': (query.get('quick') or ['0'])[0] in ('1', 'true', 'yes')}
            if p in ('/api/ulog', '/api/ulog/flow', '/api/ulog/conf'):
                for k in ('since', 'limit', 'level', 'src', 'action', 'proto',
                          'addr', 'port', 'q', 'sources'):
                    if query.get(k):
                        payload[k] = query[k][0]
            res = self.helper(readings[p], payload)
            return self.json(res)

        # ========== 连接跟踪与统一日志（#11） ==========
        if p in ('/api/ulog', '/api/ulog/op', '/api/ulog/flow',
                 '/api/ulog/conf', '/api/flow') and method in ('GET', 'POST'):
            if not self.auth():
                return
            b = self.body() if method == 'POST' else {}
            for k in ('since', 'limit', 'level', 'src', 'action', 'proto',
                      'addr', 'port', 'q', 'sources'):
                if k in query and k not in b:
                    b[k] = query[k][0]
            op = (b.get('op') or '').strip()
            if p in ('/api/ulog/flow', '/api/flow'):
                return self.json(self.helper('read:ulog_flow', b, timeout=40))
            if p == '/api/ulog/conf':
                return self.json(self.helper('read:ulog_conf', b, timeout=25))
            if not op:
                op = 'flow' if b.get('view') == 'flow' else 'query'
            to = 60 if op in ('export', 'prune', 'save_conf') else 45
            res = self.helper('ulog', dict(b, op=op), timeout=to)
            # 保存日志设置后，同步重建采集定时器（按 enabled/archive 启停）
            if op == 'save_conf' and res.get('ok'):
                sync = self.helper('logd_sync', {
                    'interval_sec': b.get('interval_sec') or 60}, timeout=45)
                if sync.get('ok'):
                    res['msg_cn'] = '%s；%s' % (res.get('msg_cn', ''),
                                              sync.get('msg_cn', ''))
            return self.json(res)

        # ========== 主题之家（#12） ==========
        # 只读：列表 / 单个主题 / 变量清单
        if p in ('/api/theme', '/api/themes') and method == 'GET':
            if not self.auth():
                return
            op = (query.get('op') or ['list'])[0]
            payload = {'op': op, 'id': (query.get('id') or [''])[0]}
            return self.json(self.helper('read:theme', payload, timeout=30))
        # 写操作：save / delete / apply / reset / export / import / preview
        if p in ('/api/theme', '/api/theme/op') and method == 'POST':
            if not self.auth():
                return
            b = self.body()
            op = str(b.get('op') or '').strip()
            readonly = {'list', 'get', 'vars'}
            if not op:
                return self.json({'ok': False, 'code': 'NOOP', 'msg_cn': '缺少 op 参数'})
            if op in readonly:
                return self.json(self.helper('read:theme', b, timeout=30))
            # 导入可能带较大 base64，给足超时
            to = 180 if op in ('import', 'import_apply') else 60
            res = self.helper('theme', b, timeout=to)
            audit(self.user, '主题之家', '%s -> %s' % (op, res.get('msg_cn')),
                  res.get('ok', False), self.h.client_address[0])
            return self.json(res)

        # ========== WAN 接入方式统一日志（#4） ==========
        if p in ('/api/wan/log', '/api/wanlog', '/api/wan/access') and method in ('GET', 'POST'):
            if not self.auth():
                return
            b = self.body() if method == 'POST' else {}
            payload = {
                'access': b.get('access') or (query.get('access') or ['pppoe'])[0],
                'iface': b.get('iface') or (query.get('iface') or [''])[0],
                'limit': b.get('limit') or (query.get('limit') or ['120'])[0],
                'since': b.get('since') or (query.get('since') or ['2 hours ago'])[0],
            }
            return self.json(self.helper('read:wan_log', payload, timeout=45))

        # ========== 配置渲染预览（只读，不写盘） ==========
        if p in ('/api/render', '/api/preview') and method == 'POST':
            if not self.auth():
                return
            b = self.body()
            module = (b.get('module') or '').strip()
            if module not in ('dnsmasq', 'nft_v4', 'nft_v6', 'radvd', 'dhcpv6',
                              'upnp', 'ntp', 'system', 'ppp', 'portfwd'):
                return self.json({'ok': False, 'code': 'BAD_MODULE',
                                  'msg_cn': '不支持预览该模块：%s' % module})
            # portfwd 不是独立的渲染模块，它的规则并入 nft_v4/nft_v6
            if module == 'portfwd':
                module = 'nft_v4'
            cfg = get_cfg(module, {}) or {}
            if isinstance(b.get('data'), dict):
                cfg = dict(cfg, **b['data'])
            syscfg = get_cfg('system', {}) or {}
            if module == 'upnp':
                # 与 apply 路径保持一致，否则预览出来的 allow 网段
                # 和真正生效的不一样（预览永远显示 192.168.0.0/16）
                cfg['lan_address'] = (cfg.get('lan_address')
                                      or syscfg.get('lan_address') or '')
            if module in ('nft_v4', 'nft_v6'):
                cfg['wan_iface'] = cfg.get('wan_iface') or syscfg.get('wan_iface') or 'ppp0'
                cfg['lan_ifaces'] = cfg.get('lan_ifaces') or [syscfg.get('lan_iface') or 'ens18']
                pf = get_cfg('portfwd', {}) or {}
                if module == 'nft_v4':
                    cfg['portfwd'] = {'enable': pf.get('enable_v4', False),
                                      'rules': [r for r in (pf.get('rules') or [])
                                                if (r.get('family') or 'v4') != 'v6'],
                                      'dmz': {'enable': pf.get('dmz_v4_enable', False),
                                              'host': pf.get('dmz_v4_host') or ''}}
                else:
                    cfg['portfwd'] = {'enable': pf.get('enable_v6', False),
                                      'rules': [r for r in (pf.get('rules') or [])
                                                if (r.get('family') or 'v4') == 'v6'],
                                      'dmz': {'enable': pf.get('dmz_v6_enable', False),
                                              'host': pf.get('dmz_v6_host') or ''}}
            try:
                res = self.helper('render_text', {'module': module, 'cfg': cfg}, timeout=20)
                return self.json(res)
            except Exception as e:
                return self.json({'ok': False, 'code': 'RENDER_FAIL',
                                  'msg_cn': '渲染失败：%s' % e})

        # ========== 内网文件共享 SMB / NFS（#9） ==========
        if p in ('/api/share', '/api/share/op') and method in ('GET', 'POST'):
            if not self.auth():
                return
            b = self.body() if method == 'POST' else {}
            op = (b.get('op') or 'get').strip()
            to = 60 if op in ('save', 'on', 'off') else 25
            return self.json(self.helper('share', dict(b, op=op), timeout=to))

        # ========== 外置存储设备（USB / Type-C / 雷电） ==========
        # 挂载用 40s、格式化给 900s：大容量硬盘格式化可能要好几分钟，
        # 超时设短了会出现「后台其实还在跑，前端却报失败」的错位。
        if p in ('/api/storage', '/api/storage/op') and method in ('GET', 'POST'):
            if not self.auth():
                return
            b = self.body() if method == 'POST' else {}
            op = (b.get('op') or 'get').strip()
            to = 900 if op == 'format' else (40 if op in ('mount', 'umount') else 25)
            return self.json(self.helper('storage', dict(b, op=op), timeout=to))

        # ========== 访问控制 / 家长时间组（#8） ==========
        if p in ('/api/acl', '/api/acl/op') and method in ('GET', 'POST'):
            if not self.auth():
                return
            b = self.body() if method == 'POST' else {}
            op = (b.get('op') or ('get' if method == 'GET' else 'get')).strip()
            if op == 'save':
                return self.json(self.helper('acl', b, timeout=30))
            if op == 'plan':
                return self.json(self.helper('acl', dict(b, op='plan'), timeout=30))
            to = 20
            return self.json(self.helper('acl', dict(b, op=op), timeout=to))

        # ========== Docker / Docker Compose 管理（#10） ==========
        if p in ('/api/docker', '/api/docker/op') and method in ('GET', 'POST'):
            if not self.auth():
                return
            b = self.body() if method == 'POST' else {}
            op = (b.get('op') or 'get').strip()
            # 这些操作耗时较长，放长超时
            slow = ('stack_up', 'stack_down', 'stack_restart', 'stack_pull',
                    'prune', 'logs', 'inspect')
            to = 620 if op in slow else (70 if op != 'get' else 60)
            return self.json(self.helper('docker', dict(b, op=op), timeout=to))

        # ========== 动态域名 DDNS（#6） ==========
        if p in ('/api/ddns', '/api/ddns/op') and method == 'POST':
            if not self.auth():
                return
            b = self.body()
            op = (b.get('op') or 'get').strip()
            if op == 'save':
                # 校验服务商与必填字段
                prov = b.get('provider') or 'custom'
                cfg = {'enabled': bool(b.get('enabled')), 'provider': prov,
                       'domain': (b.get('domain') or '').strip(),
                       'subdomain': (b.get('subdomain') or '').strip(),
                       'ipv4': b.get('ipv4', True), 'ipv6': b.get('ipv6', False),
                       'ttl': b.get('ttl') or 300, 'interval': b.get('interval') or 300,
                       'url4': b.get('url4') or '', 'url6': b.get('url6') or '',
                       'fields': b.get('fields') or {}}
                return self.json(self.helper('ddns', dict(cfg, op='save'), timeout=30))
            to = 60 if op in ('test', 'update') else 20
            return self.json(self.helper('ddns', b, timeout=to))

        # ========== 真·公网 IP 判定（#2） ==========
        # check 要并发打 5 个外部回显服务 + 一次 traceroute，给足超时；
        # probe_* 只是启动 / 轮询状态，必须秒回，否则前端倒计时会卡。
        if p in ('/api/pubip', '/api/pubip/op') and method in ('GET', 'POST'):
            if not self.auth():
                return
            b = self.body() if method == 'POST' else {}
            op = (b.get('op') or 'check').strip()
            to = 90 if op == 'check' else 15
            return self.json(self.helper('pubip', b, timeout=to))

        # ========== 磁盘与日志清理（#3） ==========
        # run 要遍历 /var/log、/tmp 并执行 apt-get clean，扫盘慢，给足超时；
        # status 只读 + 扫描，也要 30s（/var/log 递归）。
        if p in ('/api/cleanup', '/api/cleanup/op') and method in ('GET', 'POST'):
            if not self.auth():
                return
            b = self.body() if method == 'POST' else {}
            op = (b.get('op') or 'status').strip()
            to = 180 if op == 'run' else 60
            return self.json(self.helper('cleanup', b, timeout=to))

        # ========== 内核转发与加速（#4） ==========
        # save 会 modprobe + sysctl -w + systemctl 启停 snmpd，给足超时；
        # status 只是读 /proc/sys 和几个 systemctl is-active。
        if p in ('/api/kern', '/api/kern/op') and method in ('GET', 'POST'):
            if not self.auth():
                return
            b = self.body() if method == 'POST' else {}
            op = (b.get('op') or 'status').strip()
            to = 90 if op == 'save' else 30
            return self.json(self.helper('kern', b, timeout=to))

        # ========== Docker 引擎配置 daemon.json（#5） ==========
        # mirror_test 要逐个 curl 七个源（每个最多 8 秒），restart 要等 docker 起来；
        # 其余都是读文件或写文件，很快。
        if p in ('/api/dcfg', '/api/dcfg/op') and method in ('GET', 'POST'):
            if not self.auth():
                return
            b = self.body() if method == 'POST' else {}
            op = (b.get('op') or 'status').strip()
            to = 200 if op == 'mirror_test' else (190 if op == 'restart' else 40)
            return self.json(self.helper('dcfg', b, timeout=to))

        # ========== 打印服务 CUPS / USB RAW 直通（#7） ==========
        # save 会 cupsctl + 校验 + 重启 cups / cups-browsed / RAW 服务，给足超时；
        # testpage 要 lp 排队；其余都是读文件与 lpstat。
        if p in ('/api/print', '/api/print/op') and method in ('GET', 'POST'):
            if not self.auth():
                return
            b = self.body() if method == 'POST' else {}
            op = (b.get('op') or 'status').strip()
            to = 150 if op == 'save' else (100 if op in ('testpage', 'queue_add') else 40)
            return self.json(self.helper('print', b, timeout=to))

        # ========== AC / AP 管理中心 OpenSOHO（#10） ==========
        # install 要下载并解压 13MB 压缩包、建用户、写单元、起服务，给足超时；
        # probe 要挨个查 8 个集合，svc/save/secret 会 systemctl；status 只是读状态。
        if p in ('/api/opensoho', '/api/opensoho/op') and method in ('GET', 'POST'):
            if not self.auth():
                return
            b = self.body() if method == 'POST' else {}
            op = (b.get('op') or 'status').strip()
            to = 280 if op == 'install' else (90 if op in ('svc', 'save', 'secret') else 45)
            return self.json(self.helper('opensoho', b, timeout=to))

        # ========== CA 证书管理 + SSL 测试（#11） ==========
        # deploy 要备份 + 落盘 + 重启 drouter-web + 握手验证（最多再等 20 秒），
        # 失败还要回滚再启一次 —— 给它 180 秒；ssltest 是外网握手，给 45 秒。
        if p in ('/api/ca', '/api/ca/op') and method in ('GET', 'POST'):
            if not self.auth():
                return
            b = self.body() if method == 'POST' else {}
            op = (b.get('op') or 'status').strip()
            to = 180 if op in ('deploy', 'restore') else (60 if op == 'ssltest' else 45)
            return self.json(self.helper('ca', b, timeout=to))

        # ========== 新手向导 ==========
        # probe 要 ping 外网 + dig 一个域名（各几秒）；apply_step 最多一次提交
        # 四个模块的重启；dial 是真正的 pppd 拨号，要等它协商完。
        if p in ('/api/wizard', '/api/wizard/op') and method in ('GET', 'POST'):
            if not self.auth():
                return
            b = self.body() if method == 'POST' else {}
            op = (b.get('op') or 'probe').strip()
            to = 120 if op == 'dial' else (110 if op == 'apply_step' else 60)
            return self.json(self.helper('wizard', b, timeout=to))

        # ========== 防火墙滚动日志（#1） ==========
        if p in ('/api/fw/log', '/api/fwlog') and method in ('GET', 'POST'):
            if not self.auth():
                return
            b = self.body() if method == 'POST' else {}
            payload = {
                'family': b.get('family') or (query.get('family') or ['all'])[0],
                'limit': b.get('limit') or (query.get('limit') or ['200'])[0],
                'action': b.get('action') or (query.get('action') or [''])[0],
                'q': b.get('q') or (query.get('q') or [''])[0],
                'since': b.get('since') or (query.get('since') or ['60 min ago'])[0],
            }
            return self.json(self.helper('read:fw_log', payload))

        # 配置读写
        if p == '/api/config' and method == 'GET':
            if not self.auth():
                return
            # 必须与 save_config 的 allowed 集合保持一致，否则会出现
            # 「能保存但读不回来」的配置模块（theme 曾漏在这里）。
            keys = ['dnsmasq', 'nft_v4', 'nft_v6', 'radvd', 'dhcpv6', 'upnp',
                    'ntp', 'system', 'pppoe', 'ddns', 'portfwd', 'acl', 'share',
                    'docker', 'ulog', 'theme']
            out = get_cfg_many(keys, {})
            return self.json({'ok': True, 'data': out})
        if p == '/api/config' and method == 'POST':
            return self.save_config()

        if p == '/api/iface/save' and method == 'POST':
            return self.iface_save()
        if p == '/api/iface/meta' and method == 'GET':
            if not self.auth():
                return
            with _lock, db() as c:
                rows = c.execute('SELECT * FROM ifaces').fetchall()
            return self.json({'ok': True, 'data': [dict(r) for r in rows]})

        if p == '/api/buildmode' and method == 'POST':
            return self.build_mode()
        # 前端也会用 GET 读取构建保护模式状态
        if p == '/api/buildmode' and method == 'GET':
            if not self.auth():
                return
            bm = in_build_mode()
            return self.json({'ok': True, 'data': {'enabled': bm, 'build_mode': bm},
                              'msg_cn': '构建保护模式已开启' if bm else '构建保护模式已关闭'})
        if p == '/api/apply' and method == 'POST':
            return self.apply_module()
        if p == '/api/apply/raw' and method == 'POST':
            return self.apply_raw()

        if p == '/api/service' and method == 'POST':
            return self.service_op()
        if p == '/api/power' and method == 'POST':
            return self.power_op()
        if p == '/api/user' and method == 'POST':
            return self.user_op()
        if p == '/api/diag' and method == 'POST':
            return self.diag()
        if p == '/api/pkg' and method == 'POST':
            return self.pkg()
        if p == '/api/snapshot' and method == 'POST':
            return self.snapshot()
        if p in ('/api/snapshot/list', '/api/snapshots') and method == 'GET':
            return self.snapshot_list()
        if p in ('/api/snapshot/delete',) and method == 'POST':
            return self.snapshot_delete()
        if p in ('/api/snapshot/prune',) and method == 'POST':
            return self.snapshot_prune()
        # 自动快照策略（查询 / 保存 / 立即执行）
        if p in ('/api/snapshot/auto',) and method in ('GET', 'POST'):
            return self.auto_snapshot()
        # 快照下载到本地电脑
        if p.startswith('/api/snapshot/download'):
            return self.snapshot_download(query)
        # 紧急救援通道（查询 / 配置）
        if p in ('/api/rescue',) and method in ('GET', 'POST'):
            return self.rescue()
        if p == '/api/rollback' and method == 'POST':
            return self.rollback()
        if p in ('/api/audit', '/api/audits') and method == 'GET':
            if not self.auth():
                return
            return self.json({'ok': True, 'data': read_audits()})
        if p == '/api/migrate/preview' and method == 'POST':
            return self.migrate_preview()
        # Web 管理端口：读取 / 修改
        if p in ('/api/webport',) and method == 'GET':
            if not self.auth():
                return
            return self.json({'ok': True, 'data': {'port': load_web_port(),
                                                   'default': DEFAULT_WEB_PORT}})
        if p in ('/api/webport',) and method == 'POST':
            return self.set_web_port()
        if p in ('/api/lease/static', '/api/lease/make_static') and method == 'POST':
            return self.lease_make_static()
        if p in ('/api/lease/release',) and method == 'POST':
            return self.lease_release()
        if p in ('/api/ppp', '/api/ppp/control') and method == 'POST':
            return self.ppp_control()
        if p == '/api/upgrade/info' and method == 'GET':
            return self.upgrade_info()
        # 只读：查询当前已保存的 PPPoE 配置（不返回明文密码）
        if p == '/api/pppoe/saved' and method == 'GET':
            if not self.auth():
                return
            c_ = get_cfg('pppoe', {}) or {}
            safe = {k: v for k, v in c_.items() if k != 'password'}
            safe['has_password'] = bool(c_.get('password'))
            return self.json({'ok': True, 'data': safe})

        # ========== NAT 类型自动检测（#20） ==========
        if p in ('/api/nat/check', '/api/natcheck') and method in ('GET', 'POST'):
            if not self.auth():
                return
            res = self.helper('nat_check', {}, timeout=40)
            if res.get('ok') and res.get('data'):
                try:
                    NAT_LAST.update({k: res['data'].get(k, '') for k in
                                     ('nat', 'title', 'desc', 'detail', 'checked_at')})
                except Exception:
                    pass
            return self.json(res)
        # 历史结果缓存（前端画"上次检测"用）
        if p == '/api/nat/last' and method == 'GET':
            if not self.auth():
                return
            # 必须读落盘的那份：NAT_LAST 只是本进程的内存变量，Web 服务一重启
            # （每次部署都会）就清空，页面上的「上次检测」会凭空消失，看起来像
            # 从来没检测过。helper 在每次检测后已经写进 settings 表了。
            saved = {}
            try:
                r = self.helper('read:nat_last', {}, timeout=20)
                if r.get('ok') and isinstance(r.get('data'), dict):
                    saved = r['data']
            except Exception:
                saved = {}
            if saved.get('nat') and saved.get('checked_at'):
                return self.json({'ok': True, 'data': saved})
            # 落盘为空时退回本进程内存（本次启动后刚检测过、还没走完落盘的情况）
            return self.json({'ok': True, 'data': NAT_LAST})

        # ========== nftables flowtable 软加速（#22，与 NAT 检测同页） ==========
        if p in ('/api/accel',) and method in ('GET', 'POST'):
            if not self.auth():
                return
            b = self.body() if method == 'POST' else {}
            op = (b.get('op') or (query.get('op') or ['get'])[0]).strip()
            payload = {'op': op}
            if b.get('devices'):
                payload['devices'] = b.get('devices')
            if 'v4' in b:
                payload['v4'] = bool(b.get('v4'))
            if 'v6' in b:
                payload['v6'] = bool(b.get('v6'))
            return self.json(self.helper('accel', payload, timeout=40))

        # ========== VLAN（#21） ==========
        if p in ('/api/vlan',) and method in ('GET', 'POST'):
            if not self.auth():
                return
            if method == 'GET':
                return self.json(self.helper('vlan', {'op': 'get'}))
            b = self.body()
            return self.json(self.helper('vlan', b, timeout=60))

        # ========== WOL 唤醒（#21） ==========
        if p in ('/api/wol',) and method in ('GET', 'POST'):
            if not self.auth():
                return
            if method == 'GET':
                return self.json(self.helper('wol', {'op': 'get'}))
            b = self.body()
            return self.json(self.helper('wol', b, timeout=40))

        # ========== PPPoE 多拨（#23） ==========
        if p in ('/api/pppoe-multi', '/api/pppomulti') and method in ('GET', 'POST'):
            if not self.auth():
                return
            if method == 'GET':
                return self.json(self.helper('pppoe_multi', {'op': 'get'}))
            b = self.body()
            # apply 会真正拨号并改路由，给足超时
            to = 180 if (b.get('op') == 'apply') else 60
            return self.json(self.helper('pppoe_multi', b, timeout=to))
        if p == '/api/pppoe-multi/log' and method in ('GET', 'POST'):
            if not self.auth():
                return
            b = self.body() if method == 'POST' else {}
            idx = b.get('idx') or (query.get('idx') or ['1'])[0]
            return self.json(self.helper('pppoe_multi', {'op': 'log', 'idx': idx}, timeout=40))

        # ========== QoS 智能限速（#24） ==========
        if p in ('/api/qos',) and method in ('GET', 'POST'):
            if not self.auth():
                return
            if method == 'GET':
                return self.json(self.helper('qos', {'op': 'get'}))
            b = self.body()
            to = 120 if b.get('op') in ('on', 'off') else 60
            return self.json(self.helper('qos', b, timeout=to))

        # ========== DPI 识别库（#25） ==========
        if p in ('/api/dpi',) and method in ('GET', 'POST'):
            if not self.auth():
                return
            if method == 'GET':
                return self.json(self.helper('dpi', {'op': 'get'}))
            b = self.body()
            # update 可能拉几百 MB，给足超时
            to = 900 if b.get('op') == 'update' else 120
            return self.json(self.helper('dpi', b, timeout=to))
        if p == '/api/dpi/log' and method == 'GET':
            if not self.auth():
                return
            return self.json(self.helper('dpi', {'op': 'log'}, timeout=30))

        # ========== 运行依赖自检 + 一键安装（#19） ==========
        if p in ('/api/deps/check', '/api/depcheck') and method in ('GET', 'POST'):
            if not self.auth():
                return
            return self.json(self.helper('depcheck', {'op': 'check'}, timeout=90))
        if p == '/api/deps/install' and method == 'POST':
            if not self.auth():
                return
            b = self.body()
            # 前端逐项「安装」发的是 keys；只认 only 会把 keys 丢掉，
            # helper 收到空列表就走「只装必需项」分支——必需项全齐时
            # 直接返回「无需操作」，用户点了安装却什么都没装（1.0.5 实测 bug）。
            keys = b.get('keys') or b.get('only') or []
            return self.json(self.helper('depcheck', {'op': 'install',
                                                      'only': keys},
                                         timeout=900))
        if p == '/api/deps/log' and method == 'GET':
            if not self.auth():
                return
            return self.json(self.helper('depcheck', {'op': 'log'}))

        # ========== 对外通用 API 说明 + 文本范例（#17） ==========
        if p in ('/api/openapi', '/api/openapi.json') and method == 'GET':
            return self.openapi_descriptor()
        if p in ('/api/examples',) and method == 'GET':
            return self.api_examples()

        # ========== Web 终端（#18） ==========
        if p.startswith('/api/webshell'):
            if not self.auth():
                return
            return self.webshell_api(p)

        # ========== Web 远程文件管理（#18） ==========
        if p.startswith('/api/files'):
            if not self.auth():
                return
            return self.files_api(p, query, method)

        return self.json({'ok': False, 'code': 'NOTFOUND',
                          'msg_cn': '接口不存在：%s' % p}, 404)

    # ---------- 具体实现 ----------
    def login(self):
        ip = self.h.client_address[0]
        f = LOGIN_FAILS.get(ip)
        if f and f['n'] >= MAX_LOGIN_FAILS and f['until'] > time.time():
            left = int((f['until'] - time.time()) / 60) + 1
            return self.json({'ok': False, 'code': 'LOCKED',
                              'msg_cn': '登录失败次数过多，请 %d 分钟后再试' % left}, 429)
        b = self.body()
        u = (b.get('username') or '').strip()
        pw = b.get('password') or ''
        with _lock, db() as c:
            row = c.execute('SELECT * FROM admins WHERE username=?', (u,)).fetchone()
        if not row or hash_pw(pw, row['salt']) != row['pw_hash']:
            rec = LOGIN_FAILS.setdefault(ip, {'n': 0, 'until': 0})
            rec['n'] += 1
            rec['until'] = time.time() + LOGIN_LOCK_MINUTES * 60 if rec['n'] >= MAX_LOGIN_FAILS else 0
            audit(u or '(空)', '登录失败', ip, False, ip)
            left = MAX_LOGIN_FAILS - rec['n']
            return self.json({'ok': False, 'code': 'BADCRED',
                              'msg_cn': '用户名或密码错误' + ('（还可尝试 %d 次）' % left if left > 0 else '')}, 401)
        LOGIN_FAILS.pop(ip, None)
        s = new_session(u, ip)
        tok = [k for k, v in SESSIONS.items() if v is s][0]
        audit(u, '登录成功', ip, True, ip)
        return self.json({'ok': True, 'msg_cn': '登录成功',
                          'data': {'token': tok,
                                   'csrf': s['csrf'], 'user': u,
                                   'expires_in': SESSION_MINUTES * 60}})

    def change_password(self):
        if not self.auth():
            return
        b = self.body()
        old = b.get('old_password') or ''
        new = b.get('new_password') or ''
        if len(new) < 8:
            return self.json({'ok': False, 'code': 'WEAK', 'msg_cn': '新密码至少 8 位'})
        with _lock, db() as c:
            row = c.execute('SELECT * FROM admins WHERE username=?', (self.user,)).fetchone()
            if not row or hash_pw(old, row['salt']) != row['pw_hash']:
                return self.json({'ok': False, 'code': 'BADOLD', 'msg_cn': '原密码不正确'})
            salt = secrets.token_hex(16)
            c.execute('UPDATE admins SET pw_hash=?,salt=? WHERE username=?',
                      (hash_pw(new, salt), salt, self.user))
        audit(self.user, '修改Web密码', '成功')
        return self.json({'ok': True, 'msg_cn': 'Web 管理密码已修改'})

    def save_config(self):
        if not self.auth():
            return
        b = self.body()
        module = b.get('module')
        data = b.get('data')
        allowed = {'dnsmasq', 'nft_v4', 'nft_v6', 'radvd', 'dhcpv6', 'upnp', 'ntp', 'system', 'pppoe', 'ddns', 'portfwd', 'acl', 'share', 'docker', 'ulog', 'theme'}
        if module not in allowed:
            return self.json({'ok': False, 'code': 'BADMODULE', 'msg_cn': '未知配置模块'})
        if not isinstance(data, dict):
            return self.json({'ok': False, 'code': 'BADDATA', 'msg_cn': '配置格式不正确'})
        merge_cfg(module, data)
        audit(self.user, '保存配置', module)
        return self.json({'ok': True, 'msg_cn': '已保存（尚未应用）', 'data': get_cfg(module)})

    def iface_save(self):
        if not self.auth():
            return
        b = self.body()
        mac = (b.get('mac') or '').strip().lower()
        if not re.match(r'^([0-9a-f]{2}:){5}[0-9a-f]{2}$', mac):
            return self.json({'ok': False, 'code': 'BADMAC', 'msg_cn': 'MAC 地址格式不正确'})
        name = (b.get('name') or '').strip()
        remark = (b.get('remark') or '').strip()[:128]
        role = (b.get('role') or '').strip()
        ALLOWED_ROLES = ('', 'wan', 'lan', 'bridge', 'guest', 'iot', 'dmz', 'mgmt', 'unused')
        if role not in ALLOWED_ROLES:
            return self.json({'ok': False, 'code': 'BADROLE',
                              'msg_cn': '角色取值不合法，请从下拉列表中选择'})
        with _lock, db() as c:
            c.execute('INSERT INTO ifaces(mac,name,remark,role,updated_at) VALUES(?,?,?,?,?) '
                      'ON CONFLICT(mac) DO UPDATE SET name=excluded.name, remark=excluded.remark, '
                      'role=excluded.role, updated_at=excluded.updated_at',
                      (mac, name, remark, role, now()))
        # 角色联动到 system 配置
        syscfg = get_cfg('system', {}) or {}
        if role == 'wan':
            syscfg['wan_iface'] = name
            syscfg['wan_present'] = True
        elif role == 'lan':
            syscfg['lan_iface'] = name
        set_cfg('system', syscfg)
        audit(self.user, '编辑网卡', '%s %s 备注=%s 角色=%s' % (mac, name, remark, role))
        return self.json({'ok': True, 'msg_cn': '网卡信息已保存'})

    def apply_module(self):
        if not self.auth():
            return
        b = self.body()
        module = b.get('module')
        data = b.get('data')
        live = bool(b.get('live'))          # 默认 False：只写盘不生效
        check_only = bool(b.get('check_only'))
        # 预检绝不写配置库：只该校验「传进来的这份数据能不能渲染成合法配置」，
        # 不该把这份数据变成「当前配置」。否则点一次「仅语法检查」
        # 就等于悄悄保存了一次，用户完全没察觉。
        if isinstance(data, dict) and not check_only:
            merge_cfg(module, data)
        if check_only and isinstance(data, dict):
            # 预检只看「这次提交的这份」，以它为准，不受库里旧值影响
            cfg = dict(get_cfg(module, {}) or {}, **data)
        else:
            cfg = get_cfg(module, {})
        # 注入必要的上下文（LAN/WAN 接口名）
        syscfg = get_cfg('system', {}) or {}
        if module == 'dnsmasq':
            cfg['lan_iface'] = cfg.get('lan_iface') or syscfg.get('lan_iface') or 'ens18'
        elif module in ('nft_v4', 'nft_v6'):
            cfg['wan_iface'] = cfg.get('wan_iface') or syscfg.get('wan_iface') or 'ppp0'
            cfg['lan_ifaces'] = cfg.get('lan_ifaces') or [syscfg.get('lan_iface') or 'ens18']
            # 注入端口转发 / DMZ 配置（#7），由 render.py 生成 DNAT 与放通规则
            pf = get_cfg('portfwd', {}) or {}
            if module == 'nft_v4':
                cfg['portfwd'] = {'enable': pf.get('enable_v4', pf.get('enable', False)),
                                  'rules': [r for r in (pf.get('rules') or [])
                                            if (r.get('family') or 'v4') != 'v6'],
                                  'dmz': {'enable': pf.get('dmz_v4_enable', False),
                                          'host': pf.get('dmz_v4_host') or ''}}
            else:
                cfg['portfwd'] = {'enable': pf.get('enable_v6', False),
                                  'rules': [r for r in (pf.get('rules') or [])
                                            if (r.get('family') or 'v4') == 'v6'],
                                  'dmz': {'enable': pf.get('dmz_v6_enable', False),
                                          'host': pf.get('dmz_v6_host') or ''}}
        elif module == 'radvd':
            cfg['iface'] = cfg.get('iface') or syscfg.get('lan_iface') or 'ens18'
        elif module == 'dhcpv6':
            cfg['wan_iface'] = cfg.get('wan_iface') or syscfg.get('wan_iface') or 'ppp0'
            cfg['lan_iface'] = cfg.get('lan_iface') or syscfg.get('lan_iface') or 'ens18'
        elif module == 'upnp':
            cfg['ext_iface'] = cfg.get('ext_iface') or syscfg.get('wan_iface') or 'ppp0'
            # lan_address 也要：miniupnpd 的 allow 规则网段取自它，
            # 不注入的话自定义网段（10.x/172.x）的用户 UPnP 永远被 deny，
            # 而界面上开关是开的、没有任何提示。
            cfg['lan_address'] = (cfg.get('lan_address')
                                  or syscfg.get('lan_address') or '')
        elif module in ('ppp', 'pppoe'):
            # ⚠️ 这里必须同时认 'pppoe'：前端 PAGE_MODULES 里声明的模块名就是
            # pppoe（`web/app.js` 的 `wan: { save:['pppoe'], apply:['pppoe'] }`），
            # 而渲染器里的别名才叫 ppp。早先只写 'ppp'，这条回落永远不生效 ——
            # 界面上「拨号网卡」看着是选中的，点保存却报「PPPoE 网卡不能为空」。
            # PPPoE 拨号网卡：优先用用户填写值，其次回落到已分配的 WAN 口；
            # 若都没有，且当前只有一个物理网卡，则直接用它（单口调试场景）
            cfg['iface'] = (cfg.get('iface') or '').strip() or syscfg.get('wan_iface') or ''
            if not cfg['iface']:
                # 从已保存的网卡备注中找角色为 wan 的
                with _lock, db() as c:
                    row = c.execute("SELECT name FROM ifaces WHERE role='wan' LIMIT 1").fetchone()
                if row:
                    cfg['iface'] = row['name']
            if not cfg['iface']:
                return self.json({
                    'ok': False, 'code': 'NO_WAN_IFACE',
                    'msg_cn': '尚未指定 PPPoE 拨号网卡。请先在「网卡与桥接」中把某张网卡角色设为 WAN，'
                              '或在本页的「拨号网卡」下拉框中手动选择。'})
        res = self.helper('apply', {'module': module, 'cfg': cfg,
                                    'live': live, 'check_only': check_only})
        audit(self.user, '应用配置', '%s live=%s -> %s' % (module, live, res.get('msg_cn')),
              res.get('ok', False), self.h.client_address[0])
        return self.json(res)

    def apply_raw(self):
        if not self.auth():
            return
        b = self.body()
        module = b.get('module')
        if module not in ('nft_v4', 'nft_v6'):
            return self.json({'ok': False, 'code': 'BADMODULE', 'msg_cn': '仅支持防火墙规则原文应用'})
        raw = b.get('raw') or ''
        if len(raw) > 200000:
            return self.json({'ok': False, 'code': 'TOOBIG', 'msg_cn': '规则内容过大'})
        cfg = get_cfg(module, {}) or {}
        cfg['raw'] = raw
        set_cfg(module, cfg)
        res = self.helper('apply', {'module': module, 'cfg': cfg,
                                    'live': bool(b.get('live')),
                                    'check_only': bool(b.get('check_only'))})
        audit(self.user, '应用防火墙原文', module, res.get('ok', False), self.h.client_address[0])
        return self.json(res)

    def service_op(self):
        if not self.auth():
            return
        b = self.body()
        res = self.helper('service', {'name': b.get('name'), 'op': b.get('op')})
        audit(self.user, '服务操作', '%s %s' % (b.get('name'), b.get('op')), res.get('ok', False))
        return self.json(res)

    def power_op(self):
        if not self.auth():
            return
        b = self.body()
        op = b.get('op')
        if op == 'force_reboot' and not b.get('confirm_text') == '确认强制重启':
            return self.json({'ok': False, 'code': 'NEEDCONFIRM',
                              'msg_cn': '强制重启风险极高，请输入「确认强制重启」以继续'})
        audit(self.user, '电源操作', op, True, self.h.client_address[0])
        return self.json(self.helper('power', {'op': op}))

    def user_op(self):
        if not self.auth():
            return
        b = self.body()
        res = self.helper('user', b)
        audit(self.user, '用户管理', '%s %s' % (b.get('op'), b.get('name')), res.get('ok', False))
        return self.json(res)

    def diag(self):
        if not self.auth():
            return
        b = self.body()
        res = self.helper('diag', b, timeout=90)
        audit(self.user, '诊断工具', '%s %s' % (b.get('tool'), b.get('target') or b.get('iface') or ''),
              res.get('ok', False))
        return self.json(res)

    def pkg(self):
        if not self.auth():
            return
        b = self.body()
        res = self.helper('pkg', b, timeout=960)
        audit(self.user, '软件包管理', '%s %s' % (b.get('op'), ','.join(b.get('packages') or [])[:200]),
              res.get('ok', False))
        return self.json(res)

    def snapshot(self):
        if not self.auth():
            return
        b = self.body()
        res = self.helper('snapshot', {'tag': b.get('tag') or 'manual'})
        audit(self.user, '创建快照', res.get('msg_cn'), res.get('ok', False))
        return self.json(res)

    def snapshot_list(self):
        if not self.auth():
            return
        return self.json(self.helper('snapshot_list'))

    def snapshot_delete(self):
        if not self.auth():
            return
        b = self.body()
        res = self.helper('snapshot_delete', {'ts': b.get('ts')})
        if res.get('ok'):
            audit(self.user, '删除快照', b.get('ts'), True)
        return self.json(res)

    def snapshot_prune(self):
        if not self.auth():
            return
        b = self.body()
        res = self.helper('snapshot_prune', {
            'keep_days': b.get('keep_days'), 'keep_count': b.get('keep_count'),
            'keep_manual': b.get('keep_manual', True)})
        if res.get('ok'):
            audit(self.user, '清理快照', res.get('msg_cn'), True)
        return self.json(res)

    def auto_snapshot(self):
        if not self.auth():
            return
        b = self.body() if self._is_post() else {}
        op = b.get('op') or 'get'
        payload = dict(b)
        payload['op'] = op
        res = self.helper('auto_snapshot', payload)
        if res.get('ok') and op == 'set':
            audit(self.user, '自动快照策略', res.get('msg_cn'), True)
        return self.json(res)

    def snapshot_download(self, query):
        """把一份快照打包成 tar.gz 供用户下载到本地电脑。"""
        if not self.auth():
            return
        ts = (query.get('ts') or [''])[0].strip()
        if not re.match(r'^\d{8}-\d{6}$', ts):
            return self.json({'ok': False, 'msg_cn': '快照编号格式不正确'})
        res = self.helper('snapshot_pack', {'ts': ts})
        if not res.get('ok'):
            return self.json(res)
        path = (res.get('data') or {}).get('path') or ''
        if not path or not os.path.isfile(path):
            return self.json({'ok': False, 'msg_cn': '打包文件生成失败'})
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except Exception as e:
            return self.json({'ok': False, 'msg_cn': '读取打包文件失败：%s' % e})
        fn = 'drouter-snapshot-%s.tar.gz' % ts
        self.h.send_response(200)
        self.h.send_header('Content-Type', 'application/gzip')
        self.h.send_header('Content-Disposition', 'attachment; filename="%s"' % fn)
        self.h.send_header('Content-Length', str(len(data)))
        self.h.send_header('Cache-Control', 'no-store')
        self.h.end_headers()
        try:
            self.h.wfile.write(data)
        except Exception:
            pass
        audit(self.user, '下载快照', ts, True)

    def rescue(self):
        if not self.auth():
            return
        b = self.body() if self._is_post() else {}
        op = b.get('op') or 'get'
        payload = dict(b)
        payload['op'] = op
        # 开关救援通道需要显式确认
        if op == 'set' and b.get('enabled') is True and b.get('confirm') is not True:
            return self.json({'ok': False, 'code': 'NEEDCONFIRM',
                              'msg_cn': '开启紧急救援通道会在网卡上新增虚拟地址，请确认后重试'})
        res = self.helper('rescue', payload)
        if res.get('ok') and op == 'set':
            audit(self.user, '紧急救援通道', res.get('msg_cn'), True,
                  self.h.client_address[0])
        return self.json(res)

    def _is_post(self):
        return getattr(self, 'method', 'GET') == 'POST'

    def rollback(self):
        if not self.auth():
            return
        b = self.body()
        res = self.helper('rollback', {'ts': b.get('ts')})
        audit(self.user, '回滚配置', b.get('ts'), res.get('ok', False))
        return self.json(res)

    def migrate_preview(self):
        if not self.auth():
            return
        b = self.body()
        res = self.helper('migrate_networkd', {'dry_run': True, 'cfg': b.get('cfg') or {}})
        return self.json(res)

    def build_mode(self):
        if not self.auth():
            return
        b = self.body()
        op = b.get('op') or 'status'
        if op == 'off' and b.get('confirm') is not True:
            return self.json({'ok': False, 'code': 'NEEDCONFIRM',
                              'msg_cn': '关闭构建保护模式需二次确认'})
        res = self.helper('build_mode', {'op': op, 'confirm': b.get('confirm')})
        audit(self.user, '构建保护模式', op, res.get('ok', False), self.h.client_address[0])
        return self.json(res)

    def ppp_control(self):
        if not self.auth():
            return
        b = self.body()
        op = b.get('op')
        if op in ('connect', 'disconnect') and b.get('confirm') is not True:
            return self.json({'ok': False, 'code': 'NEEDCONFIRM',
                              'msg_cn': '拨号/断开会影响全网，请确认后重试'})
        res = self.helper('ppp_control', {'op': op})
        audit(self.user, 'PPPoE 控制', op, res.get('ok', False), self.h.client_address[0])
        return self.json(res)

    def set_web_port(self):
        if not self.auth():
            return
        b = self.body()
        raw = b.get('port')
        try:
            port = int(raw)
        except Exception:
            return self.json({'ok': False, 'code': 'BADPORT', 'msg_cn': '端口必须是 1-65535 的数字'})
        if not (1 <= port <= 65535):
            return self.json({'ok': False, 'code': 'BADPORT', 'msg_cn': '端口必须是 1-65535 的数字'})
        if port in (8080, 80, 53, 67, 68, 5900):
            return self.json({'ok': False, 'code': 'RESERVED',
                              'msg_cn': '该端口已被系统其它服务占用（如 8080/5900/53/67），请换一个'})
        res = self.helper('web_port_set', {'port': port})
        if res.get('ok'):
            audit(self.user, '修改 Web 端口', str(port), True)
        return self.json(res)

    def lease_make_static(self):
        if not self.auth():
            return
        b = self.body()
        res = self.helper('lease_make_static',
                          {'mac': b.get('mac'), 'ip': b.get('ip'), 'host': b.get('host')})
        audit(self.user, '租约转静态', '%s → %s' % (b.get('mac'), b.get('ip')), res.get('ok', False))
        return self.json(res)

    def lease_release(self):
        if not self.auth():
            return
        b = self.body()
        if b.get('confirm') is not True:
            return self.json({'ok': False, 'code': 'NEEDCONFIRM',
                              'msg_cn': '回收地址会让该客户端暂时断网，请确认后重试'})
        res = self.helper('lease_release', {'ip': b.get('ip'), 'mac': b.get('mac')})
        audit(self.user, '回收 DHCP 地址', b.get('ip'), res.get('ok', False))
        return self.json(res)

    def upgrade_info(self):
        if not self.auth():
            return
        # 只读：检查是否有 apt 更新 + RealVNC 状态
        out = {}
        try:
            p = subprocess.run(['apt', 'list', '--upgradable'], capture_output=True,
                               text=True, timeout=60, errors='replace')
            rows = [l for l in (p.stdout or '').splitlines()[1:] if '/' in l]
            risky = []
            for l in rows:
                nm = l.split('/')[0]
                if re.match(r'^(xserver-xorg|xorg|linux-image|linux-headers|xfce4|realvnc|vnc)', nm):
                    risky.append(l)
            out['upgradable'] = rows
            out['risky'] = risky
            out['notice'] = ('发现 %d 个可升级包，其中 %d 个可能影响桌面/RealVNC，'
                             '升级前请先创建快照。' % (len(rows), len(risky)))
        except Exception as e:
            out['notice'] = '检查失败：%s' % e
        try:
            p2 = subprocess.run(['systemctl', 'is-active', 'vncserver-x11-serviced'],
                                capture_output=True, text=True, timeout=5)
            out['vnc_active'] = (p2.stdout or '').strip() == 'active'
        except Exception:
            out['vnc_active'] = None
        out['holds'] = helper('pkg', {'op': 'holds'}).get('data', {}).get('holds', [])
        return self.json({'ok': True, 'data': out})

    # -------------------------------------------------- 对外通用 API（#17）

    def _api_base(self):
        host = self.h.headers.get('Host') or ('%s:%d' % (HOST, PORT_HTTPS))
        sch = 'https' if isinstance(self.h.connection, ssl.SSLSocket) else 'http'
        return '%s://%s' % (sch, host)

    def openapi_descriptor(self):
        """返回一份 OpenAPI 3.0 风格的接口描述（无需登录，便于第三方自发现）。"""
        base = self._api_base()
        desc = build_openapi(base)
        return self.json({'ok': True, 'data': desc})

    def api_examples(self):
        base = self._api_base()
        return self.json({'ok': True, 'data': build_examples(base)})

    # -------------------------------------------------- Web 终端（#18）

    def webshell_api(self, p):
        """Web 终端（真 PTY）。

        协议变成交互式的了：
          connect → 拿到 sid；之后前端每敲一个键就 write，再高频 read 取回显。
        所以这里不再做「每请求一次慢超时」，read/write 必须快，否则打字会卡顿。
        """
        sub = p[len('/api/webshell'):].strip('/')
        b = self.body() if self.method == 'POST' else {}
        if sub == 'connect':
            res = self.helper('webshell', {'op': 'connect', **b}, timeout=40)
            if res.get('ok'):
                sid = (res.get('data') or {}).get('sid')
                if sid:
                    with _lock:
                        SHELL_SESSIONS[sid] = {'user': self.user,
                                               'created': time.time()}
            return self.json(res)
        if sub in ('read', 'write', 'resize'):
            sid = b.get('sid')
            with _lock:
                ok_sess = sid in SHELL_SESSIONS
            if not ok_sess:
                return self.json({'ok': False, 'code': 'NOSESS',
                                  'msg_cn': '会话不存在或已过期，请重新连接'}, 400)
            # read 支持长轮询（前端带 wait，最多 5 秒），超时要留出余量；
            # write / resize 则是打字路径上的，必须立刻返回
            tmo = 20 if sub == 'read' else 15
            return self.json(self.helper('webshell', {'op': sub, **b}, timeout=tmo))
        if sub == 'disconnect':
            sid = b.get('sid')
            with _lock:
                SHELL_SESSIONS.pop(sid, None)
            return self.json(self.helper('webshell', {'op': 'disconnect', **b}, timeout=20))
        if sub == 'sessions':
            # 关键：先在锁内把快照复制出来再返回。早先这里是
            # `with _lock: return self.json(...)` —— 而 self.json 会阻塞在
            # wfile.write 上，慢客户端（或半开连接）就能把全局 _lock 按住，
            # 连带把数据库、配置、审计、终端全堵死，表现为「整个面板卡住」。
            with _lock:
                rows = [{'sid': k, 'age': int(time.time() - v['created'])}
                        for k, v in SHELL_SESSIONS.items()]
            return self.json({'ok': True, 'data': rows})
        return self.json({'ok': False, 'code': 'NOTFOUND',
                          'msg_cn': '终端接口不存在：%s' % sub}, 404)

    # -------------------------------------------------- 文件管理（#18）

    def files_api(self, p, query, method):
        """Web 文件管理器后端：全部委托给特权执行器（目录白名单在 helper 内校验）。"""
        sub = p[len('/api/files'):].strip('/')
        g = lambda k, d='': (query.get(k) or [d])[0]
        if sub in ('list', '') and method == 'GET':
            return self.json(self.helper('fs', {'op': 'list', 'path': g('path', '/')}))
        if sub == 'download' and method == 'GET':
            return self.fs_download(g('path'))
        if sub == 'read' and method == 'GET':
            return self.json(self.helper('fs', {'op': 'read', 'path': g('path'),
                                                'max': g('max', '262144')}))
        if sub == 'mkdir' and method == 'POST':
            b = self.body()
            return self.json(self.helper('fs', {'op': 'mkdir',
                                                'parent': b.get('parent') or b.get('path'),
                                                'name': b.get('name')}))
        if sub == 'rename' and method == 'POST':
            b = self.body()
            return self.json(self.helper('fs', {'op': 'rename',
                                                'path': b.get('path'),
                                                'name': b.get('name'),
                                                'to': b.get('to')}))
        if sub == 'delete' and method == 'POST':
            b = self.body()
            return self.json(self.helper('fs', {'op': 'delete',
                                                'paths': b.get('paths') or []}))
        if sub == 'zip' and method == 'POST':
            b = self.body()
            return self.json(self.helper('fs', {'op': 'zip',
                                                'paths': b.get('paths') or [],
                                                'name': b.get('name') or ''}, timeout=300))
        if sub == 'write' and method == 'POST':
            b = self.body()
            return self.json(self.helper('fs', {'op': 'write',
                                                'path': b.get('path'),
                                                'name': b.get('name'),
                                                'b64': b.get('b64'),
                                                'size': b.get('size')}, timeout=600))
        if sub == 'hash' and method == 'GET':
            return self.json(self.helper('fs', {'op': 'hash', 'path': g('path')}))
        if sub == 'search' and method == 'GET':
            return self.json(self.helper('fs', {'op': 'search', 'path': g('path', '/'),
                                                'kw': g('kw'), 'limit': g('limit', '200')}))
        return self.json({'ok': False, 'code': 'NOTFOUND',
                          'msg_cn': '文件接口不存在：%s' % sub}, 404)

    def fs_download(self, path):
        """下载文件：走特权执行器把内容 base64 取回再以流式落盘。"""
        res = self.helper('fs', {'op': 'get', 'path': path, 'b64': True}, timeout=300)
        if not res.get('ok'):
            return self.json(res, 404)
        d = res.get('data') or {}
        raw = base64.b64decode(d.get('b64') or '')
        name = d.get('name') or 'download.bin'
        quoted = name.encode('utf-8').decode('latin-1', 'ignore')
        self.h.send_response(200)
        self.h.send_header('Content-Type', 'application/octet-stream')
        self.h.send_header('Content-Length', str(len(raw)))
        self.h.send_header('Content-Disposition',
                           "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                           % (quoted, _url_quote(name)))
        self.h.send_header('Cache-Control', 'no-store')
        self.h.end_headers()
        self.h.wfile.write(raw)


def _url_quote(s):
    from urllib.parse import quote
    return quote(s or '')


def build_openapi(base):
    """构造对外通用 API 的机器可读描述（OpenAPI 3.0.3 子集）。"""
    sec = [{'X-Token': []}]
    def ep(path, method, summary, params=None, body=None, auth=True, tag='通用'):
        node = {'tags': [tag], 'summary': summary,
                'responses': {'200': {'description': '统一 JSON 响应',
                                      'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Resp'}}}}}}
        if auth:
            node['security'] = sec
        if params:
            node['parameters'] = [{'name': k, 'in': 'query', 'required': False,
                                   'schema': {'type': 'string'}, 'description': v}
                                  for k, v in params.items()]
        if body is not None:
            node['requestBody'] = {'content': {'application/json': {
                'schema': {'type': 'object', 'properties': {
                    k: {'type': v} for k, v in (body or {}).items()}}}}}
        return {path: {method: node}}

    paths = {}
    paths['/api/health'] = {'get': {'tags': ['通用'], 'summary': '健康检查（无需登录）',
        'responses': {'200': {'description': '服务状态'}}}}
    paths['/api/login'] = {'post': {'tags': ['鉴权'], 'summary': '登入获取 Token',
        'requestBody': {'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Login'}}}},
        'responses': {'200': {'description': '返回 token'}}}}

    reads = {
        '/api/sysinfo': '系统概览（CPU / 内存 / 磁盘 / 进程 / 温度 / 虚拟化）',
        '/api/ifaces': '网络接口列表',
        '/api/routes': '路由表',
        '/api/services': '受管服务状态',
        '/api/nft': '防火墙规则快照',
        '/api/leases': 'DHCP 租约',
        '/api/ipv6': 'IPv6 地址与 RA 状态',
        '/api/ntp': '时间同步状态',
        '/api/logs': '日志',
        '/api/journal': '系统日志',
    }
    for path, summary in reads.items():
        paths.update(ep(path, 'get', summary, tag='只读观测'))

    acts = {
        '/api/nat/check': ('get', 'NAT 类型自动检测（STUN）', None, '网络诊断'),
        '/api/accel': ('post', 'nftables flowtable 软加速 开关/查询', {'op': 'string: get|on|off'}, '网络加速'),
        '/api/vlan': ('post', 'VLAN 接口 查询/保存/应用/删除', {'op': 'string: get|save|apply|delete', 'id': 'string', 'parent': 'string', 'vid': 'integer', 'name': 'string'}, '二层网络'),
        '/api/wol': ('post', 'WOL 网络唤醒 / 网卡能力查询', {'op': 'string: get|iface|save|wake', 'mac': 'string', 'iface': 'string'}, '二层网络'),
        '/api/pppoe-multi': ('post', 'PPPoE 多拨：查询/保存/应用/启停/单会话操作',
                             {'op': 'string: get|save|apply|stop|session_start|session_stop|session_restart',
                              'strategy': 'string: balance|primary_backup|weighted',
                              'iface': 'string', 'sessions': 'array', 'confirm': 'boolean',
                              'duplicate_account': 'boolean', 'idx': 'integer'}, 'WAN 接入'),
        '/api/pppoe-multi/log': ('post', 'PPPoE 单会话日志与排错建议', {'idx': 'integer'}, 'WAN 接入'),
        '/api/qos': ('post', 'QoS 智能限速：查询/保存/启用/停用/重置',
                     {'op': 'string: get|save|on|off|reset',
                      'mode': 'string: diffserv3|diffserv4|besteffort',
                      'wan_iface': 'string', 'up_mbit': 'integer', 'down_mbit': 'integer',
                      'ips': 'array', 'dscp': 'array', 'connlimit': 'boolean',
                      'connlimit_max': 'integer', 'confirm': 'boolean'}, '服务'),
        '/api/dpi': ('post', 'DPI 识别库：状态/代理测速/更新',
                     {'op': 'string: get|test|update|plan|log',
                      'mirror': 'string: direct|ghproxy|gh-proxy|moeyy|ghfast|ghproxy-net|gitclone|custom',
                      'custom_prefix': 'string', 'only': 'array', 'which': 'string'}, '服务'),
        '/api/dpi/log': ('get', 'DPI 更新日志', None, '服务'),
        '/api/wan/log': ('get', 'WAN 接入方式统一实时日志与状态（PPPoE/DHCP/静态/双栈/桥接）',
                         {'access': 'string: pppoe|dhcp|static|pppoe_dhcp6|ipoe_dhcp6|bridge',
                          'iface': 'string', 'limit': 'integer', 'since': 'string'}, 'WAN 接入'),
        '/api/ddns': ('post', '动态域名 DDNS：查询/保存/启停/公网检测/立即更新',
                      {'op': 'string: get|save|on|off|test|update',
                       'provider': 'string', 'domain': 'string', 'subdomain': 'string',
                       'ipv4': 'boolean', 'ipv6': 'boolean', 'ttl': 'integer',
                       'fields': 'object'}, '寻址与路由'),
        '/api/portfwd': ('post', '端口转发与 DMZ：查询/保存（渲染为 nftables DNAT）',
                         {'enable_v4': 'boolean', 'enable_v6': 'boolean',
                          'rules': 'array', 'dmz_v4_enable': 'boolean',
                          'dmz_v4_host': 'string', 'dmz_v6_enable': 'boolean',
                          'dmz_v6_host': 'string'}, '安全'),
        '/api/acl': ('post', '访问控制与家长时间组：查询/保存/启停/渲染预览（nftables）',
                     {'op': 'string', 'enable': 'boolean', 'time_groups': 'array',
                      'groups': 'array', 'rules': 'array'}, '安全'),
        '/api/share': ('post', '内网文件共享 SMB / NFS：查询/保存/启停/建目录/渲染预览',
                       {'op': 'string', 'enable_smb': 'boolean', 'enable_nfs': 'boolean',
                        'samba': 'object', 'nfs': 'object'}, '服务'),
        '/api/storage': ('post', '外置存储设备（USB / Type-C / 雷电）：识别设备、挂载、卸载、'
                                 '格式化（ext4/xfs/btrfs/f2fs/exfat/ntfs/vfat）、开机自动挂载',
                         {'op': 'string: get|mount|umount|format|fstab_add|fstab_del',
                          'name': 'string', 'fs': 'string', 'label': 'string',
                          'confirm': 'string', 'target': 'string', 'uuid': 'string'}, '服务'),
        '/api/docker': ('post', 'Docker / Compose 面板：状态查询、容器运维、镜像/网络/卷、'
                                'Compose 项目管理、docker run → compose 转换',
                        {'op': 'string: get|start|stop|restart|pause|unpause|rm|kill|logs|'
                               'inspect|prune|convert|stack_list|stack_save|stack_get|'
                               'stack_up|stack_down|stack_restart|stack_pull|stack_ps|stack_delete',
                         'id': 'string', 'name': 'string', 'cmd': 'string',
                         'content': 'string', 'what': 'string'}, '工具'),
        '/api/ulog': ('post', '连接跟踪与统一日志：查询/当前连接表/保存保留策略/清理/导出',
                      {'op': 'string: query|flow|conf|save_conf|prune|export|clear_archive',
                       'view': 'string: log|flow', 'src': 'string', 'level': 'string',
                       'action': 'string', 'proto': 'string', 'addr': 'string',
                       'port': 'string', 'q': 'string', 'since': 'string',
                       'limit': 'integer', 'sources': 'array', 'format': 'string: json|csv',
                       'keep_days': 'integer', 'keep_rows': 'integer'}, '日志与审计'),
        '/api/ulog/flow': ('get', '当前连接跟踪表快照（conntrack -L，含传输字节）',
                           {'proto': 'string', 'q': 'string', 'limit': 'integer'},
                           '日志与审计'),
        '/api/ulog/conf': ('get', '统一日志配置与采集能力自检', None, '日志与审计'),
        '/api/theme': ('get', '主题列表 / 主题详情 / 可定制变量清单',
                       {'op': 'list|get|vars', 'id': 'string'}, '主题之家'),
        '/api/theme/op': ('post', '主题保存 / 删除 / 应用 / 复位 / 导出 / 导入 / 预览 / 配色',
                          {'op': 'save|delete|apply|reset|export|import|import_apply|preview|validate|palette',
                           'id': 'string', 'theme': 'object', 'b64': 'string'}, '主题之家'),
        '/api/render': ('post', '配置渲染预览（只读，不写盘）',
                        {'module': 'string', 'data': 'object'}, '配置管理'),
        '/api/deps/check': ('get', '运行依赖自检', None, '系统维护'),
        '/api/deps/install': ('post', '一键安装缺失依赖', {'only': 'array'}, '系统维护'),
        '/api/config': ('get', '读取全部结构化配置', None, '配置管理'),
        '/api/apply': ('post', '应用配置（渲染 + 校验 + 回滚保护）', {'module': 'string'}, '配置管理'),
        '/api/snapshot': ('post', '创建快照', {'tag': 'string', 'note': 'string'}, '系统维护'),
        '/api/snapshot/list': ('get', '快照列表', None, '系统维护'),
        '/api/rollback': ('post', '回滚到指定快照', {'ts': 'string'}, '系统维护'),
        '/api/audit': ('get', '审计日志', None, '审计'),
    }
    for path, (m, summary, body, tag) in acts.items():
        paths.update(ep(path, m, summary, body=body, tag=tag))

    fs_eps = {
        '/api/files/list': '列目录',
        '/api/files/download': '下载文件',
        '/api/files/read': '读取文本（预览）',
        '/api/files/hash': '文件校验值',
        '/api/files/search': '关键词搜索',
        '/api/files/mkdir': '新建目录',
        '/api/files/rename': '重命名',
        '/api/files/delete': '删除（走回收站）',
        '/api/files/zip': '打包 zip',
    }
    for path, summary in fs_eps.items():
        paths.update(ep(path, 'get' if path.endswith(('list', 'read', 'hash', 'search', 'download')) else 'post',
                        summary, params={'path': '目标路径'} if not path.endswith(('list',)) else None,
                        tag='文件管理'))

    return {
        'openapi': '3.0.3',
        'info': {
            'title': 'Drouter 通用管理 API',
            'version': '1.0.6',
            'description': ('Drouter（Debian 13 拼装主路由）的统一 REST 接口。'
                            '任意语言 / 框架（curl、Python、Node、Go、PHP、Java、.NET、'
                            'Shell、Postman、工单系统、IoT 网关）均可直接调用。'
                            '响应固定为 JSON：{"ok": bool, "msg_cn": str, "data": any, "code": str}。'
                            '鉴权：POST /api/login 取 token，之后每个请求带 X-Token 头。'),
            'license': {'name': 'MIT'},
        },
        'servers': [{'url': base}],
        'components': {
            'securitySchemes': {
                'X-Token': {'type': 'apiKey', 'in': 'header', 'name': 'X-Token',
                            'description': '登入后返回的会话令牌'}},
            'schemas': {
                'Resp': {'type': 'object', 'properties': {
                    'ok': {'type': 'boolean'}, 'msg_cn': {'type': 'string'},
                    'code': {'type': 'string'}, 'data': {}}},
                'Login': {'type': 'object', 'required': ['username', 'password'],
                          'properties': {'username': {'type': 'string'},
                                         'password': {'type': 'string'}}},
            },
        },
        'paths': paths,
    }


def build_examples(base):
    """多语言调用范例（文本块，前后端都可直接展示/复制）。"""
    return {
        'base': base,
        'note': '所有接口统一返回 {"ok":..,"msg_cn":..,"data":..}；失败时 ok=false。'
                '除 /api/health、/api/login 外均需 X-Token 头。',
        'examples': [
            {'lang': 'curl', 'title': 'cURL（最通用，Linux/macOS/Windows 均可）', 'code':
                "BASE=\"%s\"\n"
                "# 1) 登入拿 token\n"
                "TOKEN=$(curl -sk -X POST \"$BASE/api/login\" \\\n"
                "  -H 'Content-Type: application/json' \\\n"
                "  -d '{\"username\":\"admin\",\"password\":\"admin123\"}' \\\n"
                "  | python3 -c 'import sys,json;print(json.load(sys.stdin)[\"data\"][\"token\"])')\n\n"
                "# 2) 读取系统概览\n"
                "curl -sk \"$BASE/api/sysinfo\" -H \"X-Token: $TOKEN\"\n\n"
                "# 3) 一键开启软加速\n"
                "curl -sk -X POST \"$BASE/api/accel\" -H \"X-Token: $TOKEN\" \\\n"
                "  -H 'Content-Type: application/json' -d '{\"op\":\"on\"}'\n" % base},

            {'lang': 'python', 'title': 'Python（仅用标准库 requests 可省）', 'code':
                "import json, urllib.request, ssl\n\n"
                "BASE = \"%s\"\n"
                "CTX = ssl._create_unverified_context()   # 自签证书\n\n"
                "def call(path, data=None, token=None, method=None):\n"
                "    body = json.dumps(data).encode() if data is not None else None\n"
                "    req = urllib.request.Request(BASE + path, data=body,\n"
                "                                 method=method or ('POST' if data else 'GET'))\n"
                "    req.add_header('Content-Type', 'application/json')\n"
                "    if token:\n"
                "        req.add_header('X-Token', token)\n"
                "    with urllib.request.urlopen(req, context=CTX, timeout=30) as r:\n"
                "        return json.load(r)\n\n"
                "tok = call('/api/login', {'username': 'admin', 'password': 'admin123'})['data']['token']\n"
                "print(call('/api/sysinfo', token=tok)['data']['hostname'])\n"
                "print(call('/api/nat/check', token=tok)['data']['title'])\n" % base},

            {'lang': 'javascript', 'title': 'JavaScript / Node.js / 浏览器 fetch', 'code':
                "const BASE = '%s';\n\n"
                "async function call(path, data, token) {\n"
                "  const r = await fetch(BASE + path, {\n"
                "    method: data ? 'POST' : 'GET',\n"
                "    headers: { 'Content-Type': 'application/json', ...(token ? { 'X-Token': token } : {}) },\n"
                "    body: data ? JSON.stringify(data) : undefined,\n"
                "  });\n"
                "  return r.json();\n"
                "}\n\n"
                "(async () => {\n"
                "  const { data } = await call('/api/login', { username: 'admin', password: 'admin123' });\n"
                "  const info = await call('/api/sysinfo', null, data.token);\n"
                "  console.log(info.data.hostname, info.data.virt.name);\n"
                "})();\n" % base},

            {'lang': 'go', 'title': 'Go（net/http 标准库）', 'code':
                "package main\n\n"
                "import (\n\t\"bytes\"\n\t\"crypto/tls\"\n\t\"encoding/json\"\n\t\"fmt\"\n\t\"io\"\n\t\"net/http\"\n)\n\n"
                "const base = \"%s\"\n\n"
                "func call(path string, payload any, token string) map[string]any {\n"
                "\tvar buf io.Reader\n"
                "\tif payload != nil {\n\t\tb, _ := json.Marshal(payload)\n\t\tbuf = bytes.NewReader(b)\n\t}\n"
                "\tmethod := \"GET\"\n\tif payload != nil {\n\t\tmethod = \"POST\"\n\t}\n"
                "\treq, _ := http.NewRequest(method, base+path, buf)\n"
                "\treq.Header.Set(\"Content-Type\", \"application/json\")\n"
                "\tif token != \"\" {\n\t\treq.Header.Set(\"X-Token\", token)\n\t}\n"
                "\ttr := &http.Transport{TLSClientConfig: &tls.Config{InsecureSkipVerify: true}}\n"
                "\tresp, _ := (&http.Client{Transport: tr}).Do(req)\n"
                "\tdefer resp.Body.Close()\n"
                "\tvar out map[string]any\n\tjson.NewDecoder(resp.Body).Decode(&out)\n\treturn out\n"
                "}\n\n"
                "func main() {\n"
                "\tlogin := call(\"/api/login\", map[string]string{\"username\": \"admin\", \"password\": \"admin123\"}, \"\")\n"
                "\ttok := login[\"data\"].(map[string]any)[\"token\"].(string)\n"
                "\tinfo := call(\"/api/sysinfo\", nil, tok)\n"
                "\tfmt.Println(info[\"data\"].(map[string]any)[\"hostname\"])\n"
                "}\n" % base},

            {'lang': 'java', 'title': 'Java 11+（HttpClient）', 'code':
                "import java.net.URI;\nimport java.net.http.*;\nimport java.time.Duration;\n\n"
                "public class Drouter {\n"
                "  static final String BASE = \"%s\";\n\n"
                "  public static void main(String[] a) throws Exception {\n"
                "    HttpClient c = HttpClient.newBuilder()\n"
                "        .sslContext(javax.net.ssl.SSLContext.getDefault())\n"
                "        .connectTimeout(Duration.ofSeconds(10)).build();\n"
                "    String login = \"{\\\"username\\\":\\\"admin\\\",\\\"password\\\":\\\"admin123\\\"}\";\n"
                "    HttpRequest rq = HttpRequest.newBuilder(URI.create(BASE + \"/api/login\"))\n"
                "        .header(\"Content-Type\", \"application/json\")\n"
                "        .POST(HttpRequest.BodyPublishers.ofString(login)).build();\n"
                "    System.out.println(c.send(rq, HttpResponse.BodyHandlers.ofString()).body());\n"
                "  }\n}\n" % base},

            {'lang': 'php', 'title': 'PHP（file_get_contents / curl 通用写法）', 'code':
                "<?php\n"
                "$base = '%s';\n\n"
                "function call($path, $data = null, $token = null) {\n"
                "    global $base;\n"
                "    $ch = curl_init($base . $path);\n"
                "    $h = ['Content-Type: application/json'];\n"
                "    if ($token) $h[] = 'X-Token: ' . $token;\n"
                "    curl_setopt_array($ch, [\n"
                "        CURLOPT_RETURNTRANSFER => true,\n"
                "        CURLOPT_SSL_VERIFYPEER => false,\n"
                "        CURLOPT_HTTPHEADER => $h,\n"
                "        CURLOPT_POST => $data !== null,\n"
                "        CURLOPT_POSTFIELDS => $data !== null ? json_encode($data) : null,\n"
                "    ]);\n"
                "    return json_decode(curl_exec($ch), true);\n"
                "}\n"
                "$t = call('/api/login', ['username' => 'admin', 'password' => 'admin123'])['data']['token'];\n"
                "echo call('/api/sysinfo', null, $t)['data']['os'];\n" % base},

            {'lang': 'shell', 'title': 'Shell（wget 版，无 curl 环境）', 'code':
                "#!sh\n"
                "BASE=\"%s\"\n"
                "TOKEN=$(wget -qO- --no-check-certificate --header='Content-Type: application/json' \\\n"
                "  --post-data='{\"username\":\"admin\",\"password\":\"admin123\"}' \\\n"
                "  \"$BASE/api/login\" | sed -n 's/.*\"token\":\"\\([^\"]*\\)\".*/\\1/p')\n"
                "wget -qO- --no-check-certificate --header=\"X-Token: $TOKEN\" \"$BASE/api/sysinfo\"\n" % base},

            {'lang': 'powershell', 'title': 'PowerShell（Windows）', 'code':
                "$base = '%s'\n"
                "$login = Invoke-RestMethod -Method Post -Uri \"$base/api/login\" `\n"
                "  -Body (@{username='admin';password='admin123'} | ConvertTo-Json) `\n"
                "  -ContentType 'application/json' -SkipCertificateCheck\n"
                "$info = Invoke-RestMethod -Uri \"$base/api/sysinfo\" -Headers @{'X-Token'=$login.data.token} `\n"
                "  -SkipCertificateCheck\n"
                "$info.data.hostname\n" % base},

            {'lang': 'http', 'title': '原始 HTTP 报文（任何 HTTP 客户端 / 工单系统 / IoT）', 'code':
                "POST /api/login HTTP/1.1\n"
                "Host: %s\n"
                "Content-Type: application/json\n"
                "Content-Length: 44\n\n"
                "{\"username\":\"admin\",\"password\":\"admin123\"}\n\n"
                "--- 拿到 token 后 ---\n\n"
                "GET /api/sysinfo HTTP/1.1\n"
                "Host: %s\n"
                "X-Token: <上一步返回的 token>\n" % (base.replace('https://', '').replace('http://', ''),
                                                      base.replace('https://', '').replace('http://', ''))},
        ],
    }


# ------------------------------------------------------------------ HTTP 处理

_ASSET_RE = re.compile(
    r'(\b(?:src|href)\s*=\s*["\'])(/[^"\']+\.(?:js|css))(["\'])')


def _inject_asset_version(raw):
    """给入口页里的本地 js/css 引用追加 ?v=<文件mtime>，用于部署后自动破缓存。

    只处理「以 / 开头、还没有查询串」的同源资源；带 ? 的（如 theme.css?v=...）
    保持原样，避免覆盖前端自己维护的版本参数。
    """
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw  # 非 utf-8 的入口页不处理，原样返回

    cache = {}

    def ver(rel):
        if rel not in cache:
            try:
                cache[rel] = int(os.path.getmtime(os.path.join(WEB_DIR, rel.lstrip('/'))))
            except OSError:
                cache[rel] = 0
        return cache[rel]

    def sub(m):
        pre, url, post = m.group(1), m.group(2), m.group(3)
        if '?' in url:
            return m.group(0)
        return '%s%s?v=%d%s' % (pre, url, ver(url), post)

    return _ASSET_RE.sub(sub, text).encode('utf-8')


class _Server(ThreadingHTTPServer):
    """静默掉「客户端自己先断开」这类噪声。

    socketserver 默认的 handle_error 会把**任何**异常连整段 traceback 打到
    stderr（也就是 journal）。而浏览器切页、关标签、取消长请求时都会抛出
    BrokenPipeError / ConnectionResetError —— 那不是服务端的错，却足以把
    journal 刷满，让真正的报错被淹掉（本机实测 3 天攒了几十条）。
    这里只吞掉这三类「对面先挂了」的异常，其余异常照旧往外抛。
    """

    daemon_threads = True
    # 等待队列也别给太大：排队排到几百个再逐个超时，体验比直接拒绝更糟
    request_queue_size = 64

    def process_request(self, request, client_address):
        """名额用完就立刻关掉这条连接，而不是无限开线程。"""
        if not _WORKER_SEM.acquire(blocking=False):
            try:
                request.close()
            except Exception:
                pass
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            _WORKER_SEM.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            _WORKER_SEM.release()

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[0]
        if exc is not None and issubclass(
                exc, (BrokenPipeError, ConnectionResetError,
                      ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = 'drouter/1.0'
    protocol_version = 'HTTP/1.1'
    # 连接级读写超时：一条连接挂在这里 30 秒没动静就断开。
    # 不设的话，慢客户端能永久占住一个工作线程（MAX_WORKERS 个名额很快被吃满）。
    timeout = 30

    def log_message(self, fmt, *args):
        pass  # 静默，避免刷日志

    def _serve_static(self, path):
        if path in ('/', '/index.html'):
            path = '/index.html'
        rel = path.lstrip('/')
        if '..' in rel:
            return self._send_text(403, '禁止访问')
        full = os.path.join(WEB_DIR, rel)
        if not os.path.isfile(full):
            return self._send_text(404, '资源不存在')
        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        if full.endswith('.html'):
            ctype = 'text/html; charset=utf-8'
        elif full.endswith('.js'):
            ctype = 'application/javascript; charset=utf-8'
        elif full.endswith('.css'):
            ctype = 'text/css; charset=utf-8'

        # ETag 协商缓存：没变就回 304，省掉整个响应体（app.js 437KB）。
        # 键里带 mtime 和大小，部署新版本后自动失效。
        try:
            st = os.stat(full)
            etag = '"%s-%s"' % (int(st.st_mtime), st.st_size)
        except Exception:
            etag = ''
        if etag and (self.headers.get('If-None-Match') or '') == etag:
            self.send_response(304)
            if etag:
                self.send_header('ETag', etag)
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            return

        with open(full, 'rb') as f:
            data = f.read()

        # 入口页注入资源版本号：部署后用户浏览器不必手动强刷就能拿到新前端。
        # 之前只发 Cache-Control: no-cache（无 ETag/Last-Modified 校验器），
        # 部分浏览器仍按启发式规则继续使用旧副本，表现为「部署完了还是老界面」。
        if full.endswith('index.html'):
            data = _inject_asset_version(data)

        enc = None
        if _want_gzip(self.headers) and len(data) >= GZIP_MIN:
            data, enc = _gzip_cached(full, data)

        self.send_response(200)
        self.send_header('Content-Type', ctype)
        if enc:
            self.send_header('Content-Encoding', enc)
            self.send_header('Vary', 'Accept-Encoding')
        if etag:
            self.send_header('ETag', etag)
        self.send_header('Content-Length', str(len(data)))
        if full.endswith('.html'):
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        else:
            self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    THEME_CSS_FILE = '/etc/drouter/generated/theme.css'

    def _serve_theme_css(self, qver=None):
        """返回当前生效主题的 CSS（主题之家 #12）。

        -cache 由 drouter-helper 生成后写盘，静态返回即可
        -v=<ts> 查询串用于前端主动绕过浏览器缓存
        - 文件不存在时返回空样式（而不是 404），保证首屏不会缺样式
        """
        path = self.THEME_CSS_FILE
        if not os.path.isfile(path):
            data = u'/* 尚未生成任何主题，使用内置默认样式 */\n'.encode('utf-8')
        else:
            try:
                with open(path, 'rb') as f:
                    data = f.read()
            except Exception:
                data = u'/* 主题样式读取失败，回落到默认样式 */\n'.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/css; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, code, text):
        data = ('<!doctype html><meta charset="utf-8">'
                '<body style="font-family:system-ui;padding:40px">%s</body>' % text).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        # 主题样式：无需登录（登录页也要用自己的主题），只读、无副作用
        if path == '/theme.css' and method == 'GET':
            # v=<ts> 只在查询串里用于绕过浏览器缓存，函数本身不需要它
            return self._serve_theme_css()
        if path.startswith('/api/'):
            try:
                api = Api(self)
                api.dispatch(method, path, query)
            except Exception as e:
                import traceback
                traceback.print_exc()
                try:
                    api.json({'ok': False, 'code': 'EXCEPTION',
                              'msg_cn': '服务器内部错误：%s' % e}, 500)
                except Exception:
                    pass
            return
        if method != 'GET':
            return self._send_text(405, '不支持的请求方法')
        self._serve_static(path)

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Allow', 'GET, POST, OPTIONS')
        self.send_header('Content-Length', '0')
        self.end_headers()


# 登录成功后需要把 token 返回给前端：在 Api.json 拦截不方便，
# 因此在 dispatch 登录分支里直接注入 token。
_orig_login = Api.login


def login_with_token(self):
    ip = self.h.client_address[0]
    f = LOGIN_FAILS.get(ip)
    if f and f['n'] >= MAX_LOGIN_FAILS and f['until'] > time.time():
        left = int((f['until'] - time.time()) / 60) + 1
        return self.json({'ok': False, 'code': 'LOCKED',
                          'msg_cn': '登录失败次数过多，请 %d 分钟后再试' % left}, 429)
    b = self.body()
    u = (b.get('username') or '').strip()
    pw = b.get('password') or ''
    with _lock, db() as c:
        row = c.execute('SELECT * FROM admins WHERE username=?', (u,)).fetchone()
    if not row or hash_pw(pw, row['salt']) != row['pw_hash']:
        # 失败记录里那些「试过一次就再也没来」的来源地址要定期清掉：
        # 公网暴露 + IPv6 大地址空间下，这个字典会长期无界增长。
        if len(LOGIN_FAILS) > 2000:
            now0 = time.time()
            for k in [k for k, v in list(LOGIN_FAILS.items())
                      if not v.get('until') or v['until'] < now0]:
                LOGIN_FAILS.pop(k, None)
        rec = LOGIN_FAILS.setdefault(ip, {'n': 0, 'until': 0})
        rec['n'] += 1
        rec['until'] = time.time() + LOGIN_LOCK_MINUTES * 60 if rec['n'] >= MAX_LOGIN_FAILS else 0
        audit(u or '(空)', '登录失败', ip, False, ip)
        left = MAX_LOGIN_FAILS - rec['n']
        return self.json({'ok': False, 'code': 'BADCRED',
                          'msg_cn': '用户名或密码错误' + ('（还可尝试 %d 次）' % left if left > 0 else '')}, 401)
    LOGIN_FAILS.pop(ip, None)
    sess = new_session(u, ip)
    tok = next(k for k, v in SESSIONS.items() if v is sess)
    audit(u, '登录成功', ip, True, ip)
    return self.json({'ok': True, 'msg_cn': '登录成功',
                      'data': {'token': tok, 'user': u, 'expires_in': SESSION_MINUTES * 60}})


Api.login = login_with_token


# ------------------------------------------------------------------ 启动

def main():
    init_db()
    global PORT_HTTPS
    PORT_HTTPS = load_web_port()
    crt, key = ensure_cert()
    threading.Thread(target=gc_sessions, daemon=True).start()

    httpd = _Server((HOST, PORT_HTTP), Handler)
    httpd.daemon_threads = True

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.load_cert_chain(crt, key)
        httpsd = _Server((HOST, PORT_HTTPS), Handler)
        httpsd.socket = ctx.wrap_socket(httpsd.socket, server_side=True)
        httpsd.daemon_threads = True
        threading.Thread(target=httpsd.serve_forever, daemon=True).start()
        print('HTTPS 监听 %s:%d' % (HOST, PORT_HTTPS), flush=True)
    except Exception as e:
        print('HTTPS 启动失败：%s' % e, flush=True)

    print('HTTP 监听 %s:%d' % (HOST, PORT_HTTP), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()

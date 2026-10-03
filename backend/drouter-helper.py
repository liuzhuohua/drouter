#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
drouter-helper —— root 特权白名单执行器

设计要点：
  * Web 后端以低权用户 drouter 运行，本脚本是它获得 root 能力的唯一通道；
  * sudoers 仅放行本脚本（NOPASSWD），不接受任意命令；
  * action 采用白名单，参数逐个强校验，所有子进程调用均使用列表形式（无 shell 拼接）；
  * 输出统一为 JSON：{"ok":bool,"code":str,"msg_cn":str,"data":...}

用法：
  drouter-helper <action> '<json_payload>'
"""
import os
import sys
import json
import glob
import fnmatch
import stat
import time
import shutil
import tarfile
import socket
import tempfile
import subprocess
import pwd
import grp
import re
import uuid
import base64
import threading
import ipaddress
from datetime import datetime

# ---------------------------------------------------------------- PATH 归一化
# Debian 上非 root / 非登录 shell 的 PATH 里没有 /usr/sbin 和 /sbin，而 nft、
# dnsmasq、radvd、chronyd 等路由器关键命令全都装在 /usr/sbin 下。
# systemd 服务默认 PATH 是 /usr/local/bin:/usr/bin:/bin，于是 helper 里所有
# sh(['nft', ...]) 都会以「未找到命令」静默失败 —— 表现是防火墙页面空白、
# 日志为空，但接口仍返回 ok。这里在进程启动时把 sbin 目录补进去。
_SBIN_DIRS = ('/usr/local/sbin', '/usr/sbin', '/sbin')


def _ensure_sbin_path():
    cur = os.environ.get('PATH') or ''
    parts = [p for p in cur.split(os.pathsep) if p]
    for d in _SBIN_DIRS:
        if d not in parts and os.path.isdir(d):
            parts.insert(0, d)
    os.environ['PATH'] = os.pathsep.join(parts)


_ensure_sbin_path()

BASE = '/opt/drouter'
DATA = os.path.join(BASE, 'data')
SNAP = os.path.join(BASE, 'snapshots')   # 兼容旧引用，实际路径见 snap_root()
GEN = '/etc/drouter/generated'
LOGDIR = '/var/log/drouter'
# helper 自己的私有临时目录：不用 /tmp 与 /var/tmp —— 它们带 sticky 位，
# 配合内核 fs.protected_regular=2 会让 root 无法覆写低权用户留下的同名残留文件。
HELPER_TMP_DIR = '/run/drouter-helper'      # /run 为 tmpfs，重启自清，且归 root 所有
# 配置语法预检专用目录：必须和上面那个 0700 的私有目录分开。
# radvd 等守护会降权后读配置，放进私有目录就是 Permission denied。
VERIFY_DIR = '/run/drouter-verify'
# 个别守护除了降权，还被 AppArmor 圈死了可读路径 —— Debian 自带的
# /etc/apparmor.d/usr.sbin.chronyd 只放行 /etc/chrony/** 与 /etc/chrony.*，
# 把待校验文件放 /tmp 或 /run 下，哪怕 root 来跑也是 Permission denied。
# 所以 chrony 的预检文件必须落在 /etc/chrony.* 这个形状的路径上。
VERIFY_PATH = {
    'chrony': '/etc/chrony.drouter-verify.conf',
}
BUILD_FLAG = '/etc/drouter/BUILD_MODE'      # 存在 = 构建保护模式：禁止任何会抢端口/断网的动作
# 注：曾经这里有一份叫 ETC_FILES 的清单，和快照用的那份重名，被后定义的静默覆盖。
# 快照清单已改名为 SNAPSHOT_ETC_FILES（见下方），这里不再重复定义，免得后人读错。

# 构建模式下禁止启动的服务（会抢端口或改变网络）
BUILD_BLOCKED_SERVICES = ['dnsmasq', 'radvd', 'dhcpcd', 'miniupnpd', 'NetworkManager',
                          'systemd-networkd', 'docker', 'containerd']


def in_build_mode():
    return os.path.isfile(BUILD_FLAG)


def tmp_nft_path(tag):
    """返回一个安全的、可写（root-only）的 nft 临时文件路径。

    为什么不直接用 /tmp 或 /var/tmp：
      这两个目录带 sticky 位（drwxrwxrwt）。内核默认 fs.protected_regular=2 时，
      root 会被禁止覆写「不属于自己的已存在文件」。历史版本若以低权用户
      （如 drouter）在 /var/tmp 留下过同名文件，后续 root 写入会永久报
      `Permission denied` 而卡死。因此统一改用 /run 下的 root 私有目录，
      并让文件名带 pid+时间戳保证唯一。
    """
    d = os.path.join(HELPER_TMP_DIR, 'nft')
    os.makedirs(d, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except Exception:
        pass
    return os.path.join(d, '%s-%d-%d.nft' % (tag, os.getpid(), int(time.time() * 1000)))


def write_tmp_nft(tag, text):
    """写入 nft 文本到私有临时文件，返回路径。"""
    p = tmp_nft_path(tag)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(text)
    return p


def cleanup_tmp(*paths):
    """安静删除临时文件，忽略不存在等错误。"""
    for p in paths:
        if not p:
            continue
        try:
            os.unlink(p)
        except Exception:
            pass


def _nft_environ_blocked(err='', out=''):
    """判断 nft -c 的失败是否属于「环境限制」而非「规则语法错误」。

    典型环境限制：
      - netlink: Error: cache initialization failed: Operation not permitted
      - Operation not permitted（低权/受限沙箱无 CAP_NET_ADMIN）
    命中这些特征时不应判定规则非法，而应降级为真实加载校验。
    """
    msg = (err or '') + (out or '')
    return ('cache initialization failed' in msg
            or 'Operation not permitted' in msg
            or 'not permitted' in msg.lower() and 'permission' in msg.lower())


def check_build_guard(action, payload):
    """构建保护模式下拦截危险动作。返回 None 表示放行，否则返回拒绝响应"""
    if not in_build_mode():
        return None
    # 允许：只读、快照、语法检查、纯写盘
    if action.startswith('read:') or action in ('snapshot', 'snapshot_list'):
        return None
    # rollback 只放行「不生效」的那一半。
    # 它原来和无条件放行放在一起，于是构建模式下回滚会把
    # dnsmasq / radvd / dhcpcd / nftables 全部 restart ——
    # 前三个正是 BUILD_BLOCKED_SERVICES 里明确拉黑的，等于把构建机
    # 变成 DHCP/DNS 服务器污染局域网，正是这个模式要防的事。
    if action == 'rollback':
        if not (payload or {}).get('reload'):
            return None
        return fail(
            '当前处于【构建保护模式】，已阻止「回滚并生效」。'
            '如需在保护模式下还原配置，请先关闭保护模式，'
            '或改用不重启服务的回滚（不带 reload 参数）。')
    if action == 'apply':
        if (payload or {}).get('check_only'):
            return None
        if not (payload or {}).get('live'):
            return None
        return fail(
            '当前处于【构建保护模式】，已阻止配置生效。'
            '此模式用于确保构建过程不影响正在运行的局域网。'
            '如确需让配置生效，请先在「系统设置 → 构建保护模式」中关闭保护。',
            'BUILD_MODE_BLOCKED')
    if action == 'service':
        name = (payload or {}).get('name')
        op = (payload or {}).get('op')
        if op in ('start', 'restart', 'enable') and name in BUILD_BLOCKED_SERVICES:
            return fail(
                '当前处于【构建保护模式】，已阻止启动 %s。'
                '该服务会占用 53/67 端口或改变网络，可能影响现有局域网。' % name,
                'BUILD_MODE_BLOCKED')
        return None
    if action in ('migrate_networkd', 'ppp_control'):
        if action == 'migrate_networkd' and (payload or {}).get('dry_run', True):
            return None
        return fail('当前处于【构建保护模式】，已阻止网络切换类操作。', 'BUILD_MODE_BLOCKED')
    if action == 'power' and (payload or {}).get('op') in ('reboot', 'poweroff', 'force_reboot'):
        return fail('当前处于【构建保护模式】，已阻止重启/关机。', 'BUILD_MODE_BLOCKED')
    if action == 'pkg' and (payload or {}).get('op') == 'upgrade':
        return fail('当前处于【构建保护模式】，已阻止自动升级（可在关闭保护后手动升级）。', 'BUILD_MODE_BLOCKED')
    return None


def act_build_mode(p):
    """查询/切换构建保护模式"""
    op = str((p or {}).get('op') or 'status')
    if op == 'status':
        return ok({'build_mode': in_build_mode(), 'flag': BUILD_FLAG,
                   'blocked_services': BUILD_BLOCKED_SERVICES})
    if op == 'off':
        if not bool((p or {}).get('confirm')):
            return fail('关闭构建保护模式需要显式确认（confirm=true）')
        try:
            os.remove(BUILD_FLAG)
        except FileNotFoundError:
            pass
        log('warn', 'system', 'BUILD_MODE_OFF', '构建保护模式已关闭，配置可正式生效')
        return ok({'build_mode': False}, '已关闭构建保护模式。此后「应用」操作将真正生效。')
    if op == 'on':
        os.makedirs(os.path.dirname(BUILD_FLAG), exist_ok=True)
        with open(BUILD_FLAG, 'w') as f:
            f.write('构建保护模式：阻止一切会抢端口/断网的配置生效\n')
        log('warn', 'system', 'BUILD_MODE_ON', '构建保护模式已开启')
        return ok({'build_mode': True}, '已开启构建保护模式。所有「应用」操作只会写盘，不会生效。')
    return fail('未知操作：%s' % op)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render  # noqa: E402
from render import ValidateError, v_ifname, v_int, v_choice  # noqa: E402
import theme  # noqa: E402
from theme import theme_to_css as _theme_to_css  # noqa: E402


def theme_to_css_cached(t):
    return _theme_to_css(t)


# ------------------------------------------------------------------ 基础工具

def ok(data=None, msg='操作成功', code='OK'):
    return {'ok': True, 'code': code, 'msg_cn': msg, 'data': data}


def fail(msg, code='ERR', data=None):
    return {'ok': False, 'code': code, 'msg_cn': msg, 'data': data}


def sh(cmd, timeout=15, input_data=None, cwd=None, env=None):
    """执行列表命令，永不启用 shell。返回 (rc, stdout, stderr)"""
    kw = dict(timeout=timeout, capture_output=True,
              text=True, input=input_data, errors='replace')
    if cwd:
        kw['cwd'] = cwd
    if env:
        kw['env'] = env
    try:
        p = subprocess.run(cmd, **kw)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, '', '命令执行超时（%ss）' % timeout
    except FileNotFoundError:
        return 127, '', '命令不存在：%s' % cmd[0]
    except NotADirectoryError:
        return 126, '', '工作目录不存在：%s' % cwd
    except PermissionError as e:
        return 126, '', '权限不足：%s' % e


# 单份结构化日志的大小阈值：超过就切成 .1（只保留一份旧档）。
# 项目自己的日志以前完全不轮转，机器常年不重启，最后会安静地把根分区吃掉。
LOG_MAX_BYTES = 32 * 1024 * 1024
_log_rot_checked = {}


def _rotate_if_big(path):
    """超过 LOG_MAX_BYTES 就把当前日志挪成 .1。

    每个日志文件最多检查一次/分钟，避免每条日志都 stat 一次。
    """
    try:
        now = time.time()
        last = _log_rot_checked.get(path, 0)
        if now - last < 60:
            return
        _log_rot_checked[path] = now
        if os.path.getsize(path) < LOG_MAX_BYTES:
            return
        old = path + '.1'
        if os.path.exists(old):
            os.unlink(old)
        os.replace(path, old)
    except Exception:
        pass


def log(level, module, code, msg_cn, detail=None):
    """结构化日志（JSONL），AI 可直接阅读"""
    try:
        os.makedirs(LOGDIR, exist_ok=True)
        rec = {
            'ts': datetime.now().isoformat(timespec='seconds'),
            'level': level, 'module': module, 'code': code,
            'msg_cn': msg_cn, 'detail': detail or '',
        }
        line = json.dumps(rec, ensure_ascii=False)
        for fn in ('all.jsonl', '%s.jsonl' % module):
            p = os.path.join(LOGDIR, fn)
            _rotate_if_big(p)
            with open(p, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
    except Exception:
        pass


# ------------------------------------------------------------------ 读取类

def _read_cpu():
    """读取 CPU 型号、核心数、字面频率。"""
    model, mhz = '', 0.0
    cores = os.cpu_count() or 1
    try:
        with open('/proc/cpuinfo') as f:
            for line in f:
                if line.startswith('model name') and not model:
                    model = line.split(':', 1)[1].strip()
                elif line.startswith('cpu MHz') and mhz == 0.0:
                    mhz = float(line.split(':', 1)[1].strip())
                elif line.startswith('processor'):
                    pass
    except Exception:
        pass
    # 物理核心数（去重 physical id + core id）
    phys = 0
    try:
        seen = set()
        pid = cid = None
        with open('/proc/cpuinfo') as f:
            for line in f:
                if line.startswith('physical id'):
                    pid = line.split(':', 1)[1].strip()
                elif line.startswith('core id'):
                    cid = line.split(':', 1)[1].strip()
                elif line.strip() == '':
                    if pid is not None and cid is not None:
                        seen.add((pid, cid))
                    pid = cid = None
        phys = len(seen)
    except Exception:
        pass
    return {'model': model, 'cores': cores, 'physical_cores': phys or cores,
            'mhz': round(mhz, 1)}


def _read_cpu_usage():
    """两次采样 /proc/stat 计算整体 CPU 占用率（%）。"""
    def snap():
        with open('/proc/stat') as f:
            p = f.readline().split()[1:]
        vals = [int(x) for x in p[:8]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return sum(vals), idle
    try:
        t1, i1 = snap()
        time.sleep(0.25)
        t2, i2 = snap()
        dt, di = t2 - t1, i2 - i1
        if dt <= 0:
            return 0.0
        return round(max(0.0, min(100.0, (dt - di) * 100.0 / dt)), 1)
    except Exception:
        return 0.0


# ------------------------------------------------------------------ 实时监控（#2 / #3 / #13）
#
# 目标：用「低占用」的方式做到接近实时的监控，而不是每次全量重算。
# 思路：
#   * CPU 使用率必须两次采样，间隔又不能太长（会阻塞 HTTP 线程）。
#     这里维护一个常驻采样线程/缓存，前端每 2 秒来取一次即可拿到新鲜值，
#     取的时候几乎零成本（不 sleep、不 fork）。
#   * 网速（上下行 / 抖动 / 响应）来自 /proc/net/dev 的字节计数差分，
#     以及 TCP 重传率 + ping 网关的 RTT（可缓存，不必每次 ping）。
#   * 内存 / 磁盘 / 进程数直接读 /proc 与 statvfs，成本极低。

_METRIC_CACHE = {
    'cpu': {'ts': 0.0, 'pct': 0.0, 'prev': None},
    'net': {'ts': 0.0, 'prev': {}, 'rx_bps': 0.0, 'tx_bps': 0.0,
            'rx_pps': 0.0, 'tx_pps': 0.0},
    'rtt': {'ts': 0.0, 'ms': None, 'jitter_ms': None, 'loss_pct': None,
            'target': '', 'prev': None},
    'lock': None,
}
_METRIC_LOCK = None


def _metric_lock():
    global _METRIC_LOCK
    if _METRIC_LOCK is None:
        import threading
        _METRIC_LOCK = threading.Lock()
    return _METRIC_LOCK


def _cpu_pct_cached(min_gap=1.0):
    """带缓存与最小间隔的 CPU 使用率。两次 /proc/stat 采样最多 sleep 0.2s。"""
    with _metric_lock():
        c = _METRIC_CACHE['cpu']
        now = time.time()
        if c['prev'] and (now - c['ts']) < min_gap:
            return c['pct']
        try:
            with open('/proc/stat') as f:
                p = f.readline().split()[1:]
            vals = [int(x) for x in p[:8]]
            total = sum(vals)
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        except Exception:
            return c['pct'] or 0.0
        if not c['prev']:
            c['prev'] = (total, idle)
            c['ts'] = now
            # 首次无法算差分，退化为短采样
            pct = _read_cpu_usage()
            c['pct'] = pct
            return pct
        pt, pi = c['prev']
        dt, di = total - pt, idle - pi
        c['prev'] = (total, idle)
        c['ts'] = now
        if dt <= 0:
            return c['pct'] or 0.0
        pct = round(max(0.0, min(100.0, (dt - di) * 100.0 / dt)), 1)
        c['pct'] = pct
        return pct


def _net_counters():
    """读取所有物理网卡的收发字节/包数。跳过 lo 与虚拟接口。

    网桥口（br0 等）要一并跳过：它的 RX/TX 是各成员口之和，
    和成员口一起累加会让「总接收/发送」翻倍（网桥模式下默认就在用 br0）。
    """
    out = {}
    try:
        with open('/proc/net/dev') as f:
            for line in f.readlines()[2:]:
                if ':' not in line:
                    continue
                name, rest = line.split(':', 1)
                name = name.strip()
                if name == 'lo' or name.startswith(('veth', 'br-', 'virbr', 'docker', 'ifb')):
                    continue
                # br0 这类网桥口：成员口的计数就是它的计数，两者只能取其一
                if name != 'lo' and os.path.isdir('/sys/class/net/%s/bridge' % name):
                    continue
                p = rest.split()
                if len(p) < 16:
                    continue
                try:
                    out[name] = {
                        'rx_bytes': int(p[0]), 'rx_pkts': int(p[1]),
                        'tx_bytes': int(p[8]), 'tx_pkts': int(p[9]),
                    }
                except Exception:
                    continue
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- 累计流量
# /proc/net/dev 的 rx/tx 是「自网卡 up 以来的累计字节数」，重启即清零。
# 为了给出跨重启可用的「历史累计」，这里把每次读到的增量叠加到一个持久化文件里。
# 只有 drouter-logd 会周期性落盘（避免 2 秒一次的轮询疯狂写盘）；
# 前端读取时用「已落盘总量 + 自上次落盘以来的增量」得到实时值，所以数字是连续的。
NET_TOTALS_FILE = '/var/lib/drouter/net-totals.json'


def _net_totals_load():
    try:
        with open(NET_TOTALS_FILE, encoding='utf-8') as f:
            d = json.loads(f.read() or '{}')
        if isinstance(d, dict):
            return {'rx': int(d.get('rx') or 0), 'tx': int(d.get('tx') or 0),
                    'last_rx': int(d.get('last_rx') or 0),
                    'last_tx': int(d.get('last_tx') or 0),
                    'since': d.get('since') or '', 'updated': int(d.get('updated') or 0)}
    except Exception:
        pass
    return {'rx': 0, 'tx': 0, 'last_rx': 0, 'last_tx': 0, 'since': '', 'updated': 0}


def _net_totals_cur():
    """当前 /proc 的物理网卡收发总量（跳过 lo / 虚拟接口）。"""
    rx = tx = 0
    for c in _net_counters().values():
        rx += int(c.get('rx_bytes') or 0)
        tx += int(c.get('tx_bytes') or 0)
    return rx, tx


def _net_totals_snapshot():
    """给前端用的累计流量：已落盘总量 + 自上次落盘以来的增量。"""
    st = _net_totals_load()
    rx, tx = _net_totals_cur()
    # 计数器回绕 / 重启清零时增量会是负数，此时不能再减，用当前值兜底
    add_rx = max(0, rx - st['last_rx'])
    add_tx = max(0, tx - st['last_tx'])
    total_rx = st['rx'] + add_rx
    total_tx = st['tx'] + add_tx
    return {'rx_bytes': total_rx, 'tx_bytes': total_tx,
            'rx_h': _hbytes(total_rx), 'tx_h': _hbytes(total_tx),
            'since': st['since'], 'updated': st['updated'],
            'pending_rx': add_rx, 'pending_tx': add_tx}


def _net_totals_update():
    """把自上次落盘以来的增量并入累计并写盘。由 drouter-logd 每轮调用。"""
    st = _net_totals_load()
    rx, tx = _net_totals_cur()
    add_rx = max(0, rx - st['last_rx'])
    add_tx = max(0, tx - st['last_tx'])
    now = int(time.time())
    new = {'rx': st['rx'] + add_rx, 'tx': st['tx'] + add_tx,
           'last_rx': rx, 'last_tx': tx,
           'since': st['since'] or time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)),
           'updated': now}
    try:
        os.makedirs(os.path.dirname(NET_TOTALS_FILE), exist_ok=True)
        tmp = NET_TOTALS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(new, f)
        os.replace(tmp, NET_TOTALS_FILE)   # 原子替换，避免读到半截文件
    except Exception:
        pass
    return new


def net_totals(_p=None):
    """对外 action：读取历史累计流量。"""
    return ok(_net_totals_snapshot())


def _net_rate(devices=None):
    """基于差分计算实时上下行速率（B/s）与包速率（pps）。

    devices 为空则统计所有物理网卡之和；否则只统计指定网卡。
    """
    with _metric_lock():
        now = time.time()
        cur = _net_counters()
        if devices:
            cur = {k: v for k, v in cur.items() if k in devices}
        st = _METRIC_CACHE['net']
        prev, pts = st['prev'], st['ts']
        if prev and (now - pts) > 0.05:
            dt = now - pts
            rx = sum(cur[k]['rx_bytes'] for k in cur) - sum(prev[k]['rx_bytes'] for k in prev if k in prev)
            tx = sum(cur[k]['tx_bytes'] for k in cur) - sum(prev[k]['tx_bytes'] for k in prev if k in prev)
            rxp = sum(cur[k]['rx_pkts'] for k in cur) - sum(prev[k]['rx_pkts'] for k in prev if k in prev)
            txp = sum(cur[k]['tx_pkts'] for k in cur) - sum(prev[k]['tx_pkts'] for k in prev if k in prev)
            st['rx_bps'] = round(max(0.0, rx / dt), 1)
            st['tx_bps'] = round(max(0.0, tx / dt), 1)
            st['rx_pps'] = round(max(0.0, rxp / dt), 1)
            st['tx_pps'] = round(max(0.0, txp / dt), 1)
        st['prev'] = cur
        st['ts'] = now
        return {'rx_bps': st['rx_bps'], 'tx_bps': st['tx_bps'],
                'rx_pps': st['rx_pps'], 'tx_pps': st['tx_pps'],
                'dt': round(now - pts, 2) if pts else 0}


def _tcp_retrans():
    """TCP 重传计数与当前连接数（用于估算"抖动/质量"）。"""
    out = {'retrans': 0, 'in_segs': 0, 'out_segs': 0, 'estab': 0}
    try:
        with open('/proc/net/snmp') as f:
            lines = f.read().splitlines()
        tcp = {}
        for i, ln in enumerate(lines):
            if ln.startswith('Tcp:') and i + 1 < len(lines):
                keys = ln.split()[1:]
                vals = lines[i + 1].split()[1:]
                tcp = dict(zip(keys, vals))
                break
        out['retrans'] = int(tcp.get('RetransSegs', 0) or 0)
        out['in_segs'] = int(tcp.get('InSegs', 0) or 0)
        out['out_segs'] = int(tcp.get('OutSegs', 0) or 0)
    except Exception:
        pass
    # 已建立连接数
    try:
        rc, o, _e = sh(['sh', '-c', 'ss -Htn state established 2>/dev/null | wc -l'], timeout=8)
        out['estab'] = int((o or '0').strip() or 0)
    except Exception:
        pass
    return out


def _route_latency(target=''):
    """测量到网关/公网的 RTT、抖动与丢包（可缓存）。

    使用 ping 连发 4 个包统计 min/avg/max/mdev；结果缓存 20 秒，
    避免前端每 2 秒轮询都触发一次 ping（浪费资源）。
    注意：这里只做「探测」，不修改任何路由或网络配置。
    """
    # ⚠️ ping 绝对不能在 _metric_lock() 里跑。
    # 前端仪表盘每 2 秒轮询一次 /api/metrics，而这个锁同时保护 CPU 差分与
    # 网卡速率的计算。目标不可达时 ping 会耗满 10 秒超时（-c 4 -W 1 逐个等），
    # 期间所有指标请求全部堆着 —— 表现为「整个仪表盘卡死十几秒」。
    # 改成：锁内只读缓存 / 决定探测目标，ping 放锁外跑，跑完再写回缓存。
    now = time.time()
    with _metric_lock():
        st = _METRIC_CACHE['rtt']
        tgt = target or st.get('target') or ''
        if st['ms'] is not None and (now - st['ts']) < 20:
            return {'ms': st['ms'], 'jitter_ms': st['jitter_ms'],
                    'loss_pct': st['loss_pct'], 'target': st['target'],
                    'cached': True}
    if not tgt:
        # 取默认路由网关；无网关则用公共 DNS 作为兜底探测目标
        rc, o, _e = sh(['sh', '-c', "ip -4 route show default 2>/dev/null "
                        "| awk '{print $3; exit}'"], timeout=6)
        tgt = (o or '').strip() or '223.5.5.5'
    rc, o, _e = sh(['ping', '-n', '-c', '4', '-W', '1', '-i', '0.3', tgt], timeout=10)
    ms = jit = loss = None
    if rc == 0 or o:
        m = re.search(r'(\d+)% packet loss', o or '')
        if m:
            loss = int(m.group(1))
        m2 = re.search(r'rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', o or '')
        if not m2:
            m2 = re.search(r'round-trip min/avg/max/(?:stddev|mdev) = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', o or '')
        if m2:
            ms = round(float(m2.group(2)), 1)
            jit = round(float(m2.group(4)), 1)
    if ms is None and loss is None:
        loss = 100
    with _metric_lock():
        st = _METRIC_CACHE['rtt']
        st.update({'ts': now, 'ms': ms, 'jitter_ms': jit,
                   'loss_pct': loss, 'target': tgt})
    return {'ms': ms, 'jitter_ms': jit, 'loss_pct': loss,
            'target': tgt, 'cached': False}


def read_metrics(p):
    """实时监控聚合接口（#2 / #3 / #13）。

    一次返回：CPU / 内存 / 磁盘 / 进程 / 温度 / 上下行网速 / 抖动 / 响应，
    前端只需轮询这一个轻量接口即可，避免多个请求造成的额外开销。
    """
    p = p or {}
    quick = bool(p.get('quick'))

    # ---- 内存 ----
    mem_total = mem_avail = 0
    swap_total = swap_free = 0
    try:
        with open('/proc/meminfo') as f:
            mi = {}
            for line in f:
                k, v = line.split(':', 1)
                mi[k.strip()] = int(v.split()[0])
        mem_total = mi.get('MemTotal', 0) // 1024
        mem_avail = mi.get('MemAvailable', 0) // 1024
        swap_total = mi.get('SwapTotal', 0) // 1024
        swap_free = mi.get('SwapFree', 0) // 1024
    except Exception:
        pass
    mem_used = max(0, mem_total - mem_avail)
    mem_pct = round(mem_used * 100.0 / max(mem_total, 1), 1)

    # ---- 磁盘（根分区，用 statvfs，比 df 更快更稳） ----
    disk = {}
    try:
        sv = os.statvfs('/')
        dtot = sv.f_blocks * sv.f_frsize
        dfree = sv.f_bavail * sv.f_frsize
        dused = dtot - sv.f_bfree * sv.f_frsize
        disk = {'total_b': dtot, 'used_b': dused, 'free_b': dfree,
                'pct': round(dused * 100.0 / max(dtot, 1), 1),
                'total_h': _hbytes(dtot), 'used_h': _hbytes(dused),
                'free_h': _hbytes(dfree)}
    except Exception:
        disk = {'total_b': 0, 'used_b': 0, 'free_b': 0, 'pct': 0,
                'total_h': '—', 'used_h': '—', 'free_h': '—'}

    # ---- 进程 / 负载 ----
    proc_count = 0
    try:
        proc_count = sum(1 for x in os.listdir('/proc') if x.isdigit())
    except Exception:
        pass
    load = []
    try:
        with open('/proc/loadavg') as f:
            load = f.read().split()[0:3]
    except Exception:
        pass

    # ---- CPU ----
    cpu_pct = _cpu_pct_cached()

    # ---- 温度（虚拟机通常不可用，返回 None 由前端展示提示） ----
    temp_c, temp_src = _read_temp()
    temp_available = temp_c is not None

    # ---- 网络速率 ----
    # 注：早期这里写成 `_net_rate() if not quick else _net_rate()` —— 两个分支
    # 一模一样，quick 参数完全没生效（前端传 quick=1 也照做全部工作）。
    # _net_rate 只是一次 /proc/net/dev 读取 + 差分，成本已经很低，就统一走它；
    # 真正的重活（ping 测 RTT、遍历网卡）由各自的缓存控制。
    _wan, _lans, alli = _wan_lan_ifaces()
    net = _net_rate()

    # ---- 网络质量（RTT / 抖动 / 丢包 / TCP 重传） ----
    rtt = _route_latency()
    tcp = _tcp_retrans()

    # ---- 活跃网卡清单（供前端选择"统计哪张网卡"） ----
    ifaces = []
    try:
        counters = _net_counters()
        for name, c in sorted(counters.items()):
            ifaces.append({'name': name, **c})
    except Exception:
        pass

    return ok({
        'ts': int(time.time()),
        'cpu_pct': cpu_pct,
        'mem': {'total_mb': mem_total, 'used_mb': mem_used, 'avail_mb': mem_avail,
                'pct': mem_pct, 'swap_total_mb': swap_total,
                'swap_free_mb': swap_free,
                'swap_pct': round((swap_total - swap_free) * 100.0 / max(swap_total, 1), 1) if swap_total else 0},
        'disk': disk,
        'proc_count': proc_count,
        'load': load,
        'temp_c': temp_c, 'temp_src': temp_src, 'temp_available': temp_available,
        'net': net,
        'rtt': rtt,
        'tcp': tcp,
        'ifaces': ifaces,
        'wan_iface': _wan,
        'lan_ifaces': _lans,
        # 历史累计流量（自开始统计以来），用于概览页展示，与实时速率区分开
        'net_total': _net_totals_snapshot(),
    })


def _hbytes(n):
    n = float(n or 0)
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or u == 'TB':
            return ('%.0f %s' % (n, u)) if u == 'B' else ('%.1f %s' % (n, u))
        n /= 1024
    return '%.1f TB' % n


def _read_temp():
    """读取 CPU 温度（与 read_sysinfo 中的逻辑一致，抽出来复用）。"""
    temp_c = None
    temp_src = ''
    try:
        for z in sorted(glob.glob('/sys/class/thermal/thermal_zone*')):
            try:
                with open(os.path.join(z, 'temp')) as f:
                    tv = int(f.read().strip())
            except Exception:
                continue
            if tv <= 0:
                continue
            temp_c = round(tv / 1000.0, 1) if tv > 1000 else float(tv)
            try:
                with open(os.path.join(z, 'type')) as f:
                    temp_src = f.read().strip() or os.path.basename(z)
            except Exception:
                temp_src = os.path.basename(z)
            break
    except Exception:
        pass
    if temp_c is None:
        try:
            for h in sorted(glob.glob('/sys/class/hwmon/hwmon*')):
                name = ''
                try:
                    with open(os.path.join(h, 'name')) as f:
                        name = f.read().strip()
                except Exception:
                    pass
                for inp in sorted(glob.glob(os.path.join(h, 'temp*_input'))):
                    try:
                        with open(inp) as f:
                            tv = int(f.read().strip())
                    except Exception:
                        continue
                    if tv <= 0:
                        continue
                    temp_c = round(tv / 1000.0, 1)
                    temp_src = '%s/%s' % (name or os.path.basename(h), os.path.basename(inp))
                    break
                if temp_c is not None:
                    break
        except Exception:
            pass
    return temp_c, temp_src


def _read_bios():
    """真实探测固件类型：UEFI / 传统 BIOS，并尽量读出厂商与版本。"""
    is_efi = os.path.isdir('/sys/firmware/efi')
    vendor = version = date = ''
    # DMI 信息（部分虚拟机/hypervisor 不暴露，容错处理）
    for key, path in (('vendor', '/sys/class/dmi/id/bios_vendor'),
                      ('version', '/sys/class/dmi/id/bios_version'),
                      ('date', '/sys/class/dmi/id/bios_date')):
        try:
            with open(path) as f:
                val = f.read().strip()
            if key == 'vendor':
                vendor = val
            elif key == 'version':
                version = val
            else:
                date = val
        except Exception:
            pass
    # 型号兜底：efi 下读取 sys_vendor / product_name
    if not vendor:
        try:
            with open('/sys/class/dmi/id/sys_vendor') as f:
                vendor = f.read().strip()
        except Exception:
            pass
    return {
        'firmware': 'UEFI' if is_efi else '传统 BIOS',
        'is_efi': is_efi,
        'vendor': vendor,
        'version': version,
        'date': date,
    }


def _read_file(path, limit=200):
    """安全读取一个小文本文件，失败返回空串。"""
    try:
        with open(path) as f:
            return f.read(limit).strip()
    except Exception:
        return ''


# DMI 厂商/型号特征 → 虚拟化平台识别表。
# 顺序敏感：先匹配更具体的（如 PVE 的 QEMU 特征），再落到通用项。
_VIRT_SIGNATURES = (
    # (匹配键, 匹配值子串, 平台ID, 展示名, 类型, 直通建议)
    ('sys_vendor', 'proxmox', 'proxmox', 'Proxmox VE (KVM)', 'vm', 'pcie'),
    ('sys_vendor', 'qemu', 'kvm', 'QEMU / KVM 虚拟机', 'vm', 'pcie'),
    ('sys_vendor', 'red hat', 'kvm', 'KVM 虚拟机', 'vm', 'pcie'),
    ('product_name', 'standard pc', 'kvm', 'QEMU / KVM 虚拟机', 'vm', 'pcie'),
    ('product_name', 'kvm', 'kvm', 'KVM 虚拟机', 'vm', 'pcie'),
    ('sys_vendor', 'vmware', 'vmware', 'VMware 虚拟机', 'vm', 'limited'),
    ('product_name', 'vmware', 'vmware', 'VMware 虚拟机', 'vm', 'limited'),
    ('sys_vendor', 'microsoft corporation', 'hyperv', 'Microsoft Hyper-V 虚拟机', 'vm', 'limited'),
    ('product_name', 'virtual machine', 'hyperv', 'Microsoft Hyper-V 虚拟机', 'vm', 'limited'),
    ('sys_vendor', 'xen', 'xen', 'Xen 虚拟机', 'vm', 'limited'),
    ('sys_vendor', 'innotek', 'virtualbox', 'Oracle VirtualBox 虚拟机', 'vm', 'no'),
    ('product_name', 'virtualbox', 'virtualbox', 'Oracle VirtualBox 虚拟机', 'vm', 'no'),
    ('sys_vendor', 'parallels', 'parallels', 'Parallels 虚拟机', 'vm', 'no'),
    ('product_name', 'bochs', 'bochs', 'Bochs / 纯软件模拟虚拟机', 'vm', 'no'),
    ('sys_vendor', 'amazon ec2', 'cloud', '公有云实例 (EC2)', 'cloud', 'no'),
    ('sys_vendor', 'google', 'cloud', '公有云实例 (GCE)', 'cloud', 'no'),
    ('sys_vendor', 'alibaba', 'cloud', '公有云实例 (阿里云)', 'cloud', 'no'),
    ('product_name', 'openstack', 'cloud', 'OpenStack 云主机', 'cloud', 'no'),
)

# 直通可行性说明（用于前端惊叹号提示的兜底文案）
_VIRT_PASSTHRU = {
    'pcie': '宿主机的 CPU 温度传感器通常不随虚拟机暴露。若宿主机为物理机，'
            '可在 Proxmox 中把温度相关 PCI 设备（如带温度上报的 BMC/IPMI、'
            '或部分 USB HID 传感器）直通给本虚拟机；成功率取决于平台与硬件。',
    'limited': '该虚拟化平台对 PCI 直通支持有限，温度传感器一般无法透传，'
               '建议在宿主机侧查看温度（如 PVE 的 Sensors 面板或 ipmitool）。',
    'no': '该平台不支持硬件直通，无法读取宿主机温度传感器，'
          '请直接在宿主机/云控制台查看温度指标。',
}


def _detect_virt():
    """后台自动识别：本机是物理机还是虚拟机/云主机。

    判定依据（多源交叉，任一命中即可）：
      1. /proc/cpuinfo 的 hypervisor 标志位（最可靠的内核级信号）
      2. /sys/class/dmi/id/sys_vendor 与 product_name 的厂商特征串
      3. systemd-detect-virt 的结论（若可用，作为权威兜底）
    返回 dict: {is_vm, type, id, name, vendor, product, hypervisor_flag, passthru, note}
    """
    flag = False
    try:
        with open('/proc/cpuinfo') as f:
            for line in f:
                if line.lower().startswith('flags') and ' hypervisor' in line.lower() + ' ':
                    flag = True
                    break
    except Exception:
        flag = False

    sys_vendor = _read_file('/sys/class/dmi/id/sys_vendor')
    product_name = _read_file('/sys/class/dmi/id/product_name')
    product_ver = _read_file('/sys/class/dmi/id/product_version')

    haystack = {
        'sys_vendor': sys_vendor.lower(),
        'product_name': product_name.lower(),
    }
    vid = name = ''
    vtype = 'physical'
    passthru = ''
    for key, needle, pid, pname, ptype, pthru in _VIRT_SIGNATURES:
        if needle and needle in haystack.get(key, ''):
            vid, name, vtype, passthru = pid, pname, ptype, pthru
            break

    # systemd-detect-virt 兜底（Debian 13 默认自带 systemd）
    if not vid and flag:
        rc, out, _e = sh(['systemd-detect-virt'], timeout=5)
        raw = (out or '').strip()
        if rc == 0 and raw and raw != 'none':
            vid = 'virt:' + raw
            name = '虚拟化环境 (%s)' % raw
            vtype = 'vm'
            passthru = 'pcie'

    if vid:
        is_vm = vtype in ('vm', 'cloud')
    else:
        is_vm = False
        vtype = 'physical'
        name = '物理机'
        # 物理机：若有 IPMI/BMC，提示可在宿主机侧读取温度
        if os.path.isdir('/sys/class/ipmi') or _read_file('/dev/ipmi0', 1) == '':
            pass
        if os.path.exists('/dev/ipmi0'):
            passthru = '物理机可通过 ipmitool 读取 BMC 温度传感器。'

    note = _VIRT_PASSTHRU.get(passthru, '') if is_vm else passthru
    return {
        'is_vm': is_vm,
        'type': vtype,                  # physical / vm / cloud
        'id': vid,                      # proxmox / kvm / vmware / ...
        'name': name,                   # 展示名
        'vendor': sys_vendor,
        'product': (product_name + (' ' + product_ver if product_ver else '')).strip(),
        'hypervisor_flag': flag,
        'passthru': passthru,           # pcie / limited / no / ''
        'note': note,
    }


def _read_disks():
    """列出真实文件系统占用（等价于 df -h -x tmpfs -x devtmpfs，但不 fork）。

    为什么不用 df：这个函数在 read_sysinfo 里被每次切页面调用，df 一次 fork+exec
    在 2 核机器上约 3-8ms。改成读 /proc/mounts + os.statvfs()，纯系统调用，
    还顺带解决了 df 输出列对齐依赖语言环境（LC_ALL 不同列可能偏移）的隐患。
    过滤规则与原 df 参数保持一致：跳过 tmpfs / devtmpfs；另外把内核的
    虚拟文件系统（proc/sysfs/cgroup 等）一并排除，否则会出现一堆 0 字节的伪磁盘。
    """
    SKIP_FS = {
        'tmpfs', 'devtmpfs', 'proc', 'sysfs', 'cgroup', 'cgroup2', 'devpts',
        'securityfs', 'debugfs', 'tracefs', 'pstore', 'bpf', 'configfs',
        'fusectl', 'hugetlbfs', 'mqueue', 'binfmt_misc', 'autofs', 'ramfs',
        'efivarfs', 'rpc_pipefs', 'nsfs', 'overlay',  # overlay 在容器里是根，见下
    }
    out = []
    try:
        mounts = []
        with open('/proc/mounts', encoding='utf-8', errors='replace') as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                dev, mnt, fstype = parts[0], parts[1], parts[2]
                if fstype in SKIP_FS:
                    continue
                # 只列真实块设备与网络文件系统，排除伪文件系统
                if not (dev.startswith('/dev/') or fstype in
                        ('nfs', 'nfs4', 'cifs', 'smb3', 'fuse.sshfs', 'exfat', 'vfat', 'ntfs3')):
                    # overlay 是 Docker 容器的根文件系统，也需要展示
                    if fstype != 'overlay':
                        continue
                mounts.append((dev, mnt))

        seen_mnt = set()
        for dev, mnt in mounts:
            if mnt in seen_mnt:
                continue
            seen_mnt.add(mnt)
            try:
                sv = os.statvfs(mnt)
            except Exception:
                continue
            bsize = sv.f_frsize or sv.f_bsize or 1
            total = sv.f_blocks * bsize
            # 可用空间用 f_bavail（普通用户可见），与原 df 的 Avail 列一致
            avail = sv.f_bavail * bsize
            used = (sv.f_blocks - sv.f_bfree) * bsize
            if total <= 0:
                continue
            pct = int(round(used * 100.0 / total))
            out.append({'fs': dev, 'size': _hbytes(total), 'used': _hbytes(used),
                        'avail': _hbytes(avail), 'pct': '%d%%' % pct, 'mount': mnt})
    except Exception:
        pass
    # 根分区永远排最前，方便前端「磁盘」卡片直接取第一项
    out.sort(key=lambda x: (x['mount'] != '/', x['mount']))
    return out


def read_sysinfo(_):
    # 性能：#7 原实现用 4 次 subprocess（hostname / cat / uname -r / df -h）取
    # 这些信息。这个接口被前端「每次切页面」都会调一次（loadAll），
    # 在 2 核软路由上一次 fork+exec 约 3-8ms，4 次就是 12-32ms 的纯浪费。
    # 下面全部改成直读 /proc 与 os 调用，零 fork。
    try:
        host = socket.gethostname()
    except Exception:
        host = ''
    with open('/proc/uptime') as f:
        up = float(f.read().split()[0])
    mem = {}
    with open('/proc/meminfo') as f:
        for line in f:
            k, v = line.split(':', 1)
            mem[k.strip()] = int(v.split()[0])
    total = mem.get('MemTotal', 1) // 1024
    avail = mem.get('MemAvailable', 0) // 1024
    # loadavg 直接读文件（原来是 sh(['cat', '/proc/loadavg'])，多此一举）
    try:
        with open('/proc/loadavg') as f:
            load = f.read()
    except Exception:
        load = ''
    # 内核版本用 os.uname()（Windows 上没有，才回退 shell）
    try:
        uname = os.uname().release
    except Exception:
        _rc, uname, _e = sh(['uname', '-r'])
    bios_info = _read_bios()
    virt = _detect_virt()
    cpu = _read_cpu()
    cpu_pct = _read_cpu_usage()
    disks = _read_disks()
    # 根分区百分比（去掉 % 转数字）
    root = next((x for x in disks if x['mount'] == '/'), disks[0] if disks else None)
    disk_pct = 0
    if root:
        try:
            disk_pct = int(str(root['pct']).rstrip('%'))
        except Exception:
            disk_pct = 0
    # 进程数 + 相对上限的占用比（用于"进程/温度"仪表盘，不能把进程数直接当百分比）
    proc_count = 0
    try:
        proc_count = sum(1 for x in os.listdir('/proc') if x.isdigit())
    except Exception:
        proc_count = 0
    proc_max = 0
    for p in ('/proc/sys/kernel/pid_max',):
        try:
            with open(p) as f:
                proc_max = int(f.read().strip())
            break
        except Exception:
            continue
    if proc_max <= 0:
        proc_max = 32768
    # 真实占用率：进程数 / pid_max。数值通常极小，所以另给一个"实用刻度"，
    # 用 4096 作为满量程更贴近家用路由器的观感（并发上千进程已属异常）。
    proc_pct = round(proc_count * 100.0 / max(proc_max, 1), 2)
    proc_pct_scale = round(min(100.0, proc_count * 100.0 / 4096.0), 1)

    # CPU 温度：逐级尝试 thermal_zone → hwmon（coretemp/k10temp/…）。
    # 虚拟机等场景确实没有任何传感器，此时返回 None，前端显示"—"而不是报错。
    temp_c = None
    temp_src = ''
    try:
        for z in sorted(glob.glob('/sys/class/thermal/thermal_zone*')):
            try:
                with open(os.path.join(z, 'temp')) as f:
                    tv = int(f.read().strip())
            except Exception:
                continue
            if tv <= 0:
                continue
            temp_c = round(tv / 1000.0, 1) if tv > 1000 else float(tv)
            try:
                with open(os.path.join(z, 'type')) as f:
                    temp_src = f.read().strip() or os.path.basename(z)
            except Exception:
                temp_src = os.path.basename(z)
            break
    except Exception:
        pass
    if temp_c is None:
        # hwmon：temperature 类型通道（temp1_input 等，单位为毫摄氏度）
        try:
            for h in sorted(glob.glob('/sys/class/hwmon/hwmon*')):
                name = ''
                try:
                    with open(os.path.join(h, 'name')) as f:
                        name = f.read().strip()
                except Exception:
                    pass
                if name in ('acpitz', '') or not name:
                    pass
                for inp in sorted(glob.glob(os.path.join(h, 'temp*_input'))):
                    try:
                        with open(inp) as f:
                            tv = int(f.read().strip())
                    except Exception:
                        continue
                    if tv <= 0:
                        continue
                    temp_c = round(tv / 1000.0, 1)
                    temp_src = '%s/%s' % (name or os.path.basename(h), os.path.basename(inp))
                    break
                if temp_c is not None:
                    break
        except Exception:
            pass
    temp_available = temp_c is not None
    # 温度不可用时的可读原因（虚拟机 / 无传感器），供前端惊叹号提示使用
    temp_hint = ''
    if not temp_available:
        if virt['is_vm']:
            temp_hint = ('当前运行在 %s 中，虚拟化层默认不会把宿主机 CPU 温度传感器'
                         '暴露给客户机，因此温度无法读取。%s') % (virt['name'],
                                                                  virt['note'])
        else:
            temp_hint = ('未检测到任何温度传感器（thermal_zone / hwmon 均为空）。'
                         '可能是内核未加载对应驱动，可尝试加载 coretemp（Intel）'
                         '或 k10temp（AMD）模块，或在 BIOS 中开启硬件监控。')
    # 发行版名称
    os_name = 'Debian GNU/Linux 13 (trixie)'
    try:
        with open('/etc/os-release') as f:
            kv = dict(l.strip().split('=', 1) for l in f if '=' in l)
        os_name = (kv.get('PRETTY_NAME') or os_name).strip('"')
    except Exception:
        pass
    return ok({
        'hostname': host, 'kernel': uname, 'os': os_name,
        'bios': bios_info['firmware'],
        'bios_info': bios_info,
        'uptime_s': int(up), 'load': load.split()[0:3] if load else [],
        'mem_total_mb': total, 'mem_avail_mb': avail,
        'mem_used_mb': total - avail, 'mem_used_pct': round((total - avail) * 100 / max(total, 1)),
        'disks': disks,
        'disk_root': root, 'disk_pct': disk_pct,
        'cpu_count': os.cpu_count(),
        'cpu': cpu, 'cpu_pct': cpu_pct,
        'proc_count': proc_count, 'proc_max': proc_max,
        'proc_pct': proc_pct, 'proc_pct_scale': proc_pct_scale,
        'temp_c': temp_c, 'temp_src': temp_src, 'temp_available': temp_available,
        'temp_hint': temp_hint,
        'virt': virt,
         'virt_is_vm': virt['is_vm'], 'virt_name': virt['name'], 'virt_note': virt['note'],
        'net_total': _net_totals_snapshot(),
    })


def _mod_version(mod):
    """读取内核模块版本号（sysfs 优先，其次 modinfo）。"""
    try:
        with open('/sys/module/%s/version' % mod) as f:
            v = f.read().strip()
        if v:
            return v
    except Exception:
        pass
    try:
        rc, out, _e = sh(['modinfo', '-F', 'version', mod], timeout=5)
        if rc == 0 and out.strip():
            return out.strip()
    except Exception:
        pass
    return ''


# 常见厂商官方驱动包名（Debian 官方源里由厂商发布/维护的）
_VENDOR_DRIVERS = {
    'r8169': 'Realtek', 'r8125': 'Realtek', 'r8101': 'Realtek', 'r8168': 'Realtek',
    'igb': 'Intel', 'igc': 'Intel', 'e1000': 'Intel', 'e1000e': 'Intel', 'ixgbe': 'Intel',
    'i40e': 'Intel', 'ice': 'Intel', 'iavf': 'Intel',
    'bnx2': 'Broadcom', 'bnx2x': 'Broadcom', 'tg3': 'Broadcom',
    'atlantic': 'Aquantia', 'alx': 'Atheros', 'atl1c': 'Atheros', 'atl1e': 'Atheros',
    'ax88179_178a': 'ASIX', 'rndis_host': 'Microsoft', 'cdc_ether': 'Linux 内核',
    'virtio_net': 'Linux 内核(KVM 虚拟网卡)',
}


def _driver_source(mod, ver=''):
    """
    判定驱动来源：
      - 内核内建 (Linux 内核)          → 无独立模块文件
      - Debian/发行版官方源 (官方)     → 由发行版打包
      - 厂商官方源 (厂商官方)          → 厂商在官方源发布
      - 第三方编译 (第三方)            → 非发行版打包（如 DKMS / 手工编译）
    """
    if not mod:
        return '未知', ''
    ko = ''
    try:
        rc, out, _e = sh(['modinfo', '-n', mod], timeout=5)
        if rc == 0:
            ko = out.strip()
    except Exception:
        pass
    label = _VENDOR_DRIVERS.get(mod, '')
    if ko and ('/updates/dkms' in ko or '/dkms/' in ko):
        return '第三方(DKMS)', ko
    if ko and (ko.startswith('/lib/modules/') and '/updates/' in ko):
        return '第三方(手工编译)', ko
    if label == 'Linux 内核(KVM 虚拟网卡)' or label == 'Linux 内核':
        return '内核内建', ko
    if label:
        return '厂商官方(%s)' % label, ko
    if ko:
        return '发行版官方源', ko
    return '内核内建', ko


def read_ifaces(_):
    """网卡清单：以 MAC 为唯一标识，附带速率/状态/MTU/统计"""
    rc, out, err = sh(['ip', '-j', 'addr', 'show'])
    if rc != 0:
        return fail('读取网卡信息失败：' + err)
    try:
        raw = json.loads(out)
    except Exception as e:
        return fail('解析网卡信息失败：%s' % e)
    res = []
    for d in raw:
        name = d.get('ifname')
        if not name:
            continue
        mac = d.get('address') or ''
        mtu = (d.get('mtu') or 1500)
        oper = d.get('operstate', 'unknown')
        flags = d.get('flags') or []
        addrs = []
        for a in d.get('addr_info') or []:
            addrs.append({'family': a.get('family'), 'addr': a.get('local'),
                          'prefix': a.get('prefixlen'),
                          'scope': a.get('scope'), 'valid': a.get('valid_life_time')})
        # 速率
        speed = None
        sp_path = '/sys/class/net/%s/speed' % name
        try:
            with open(sp_path) as f:
                v = int(f.read().strip())
                speed = v if v > 0 else None
        except Exception:
            pass
        # 驱动 + 版本 + 来源识别
        driver, drv_ver, drv_src, drv_full = '', '', '未知', ''
        drv_path = None
        try:
            drv_path = os.path.realpath('/sys/class/net/%s/device/driver' % name)
            driver = os.path.basename(drv_path)
        except Exception:
            pass
        if driver:
            drv_ver = _mod_version(driver)
            drv_src, drv_full = _driver_source(driver, drv_ver)
        # 收发统计（用于实时速率）
        stat = {}
        for k in ('rx_bytes', 'tx_bytes', 'rx_packets', 'tx_packets', 'rx_errors', 'tx_errors'):
            try:
                with open('/sys/class/net/%s/statistics/%s' % (name, k)) as f:
                    stat[k] = int(f.read().strip())
            except Exception:
                stat[k] = 0
        # 桥接成员
        members = []
        bpath = '/sys/class/net/%s/brif' % name
        if os.path.isdir(bpath):
            members = sorted(os.listdir(bpath))
        res.append({
            'name': name, 'mac': mac, 'mtu': mtu, 'oper': oper,
            'up': 'UP' in flags, 'addrs': addrs, 'speed': speed,
            'driver': driver, 'driver_version': drv_ver,
            'driver_source': drv_src, 'driver_full': drv_full,
            'stat': stat, 'bridge_members': members,
            'is_bridge': bool(members) or os.path.isdir('/sys/class/net/%s/bridge' % name),
            # ifb-* 是本机 QoS 智能限速创建的 Intermediate Functional Block 虚拟网卡
            # （Linux 只能整形出向流量，入向要先重定向到 ifb 才能限速），不是可插网卡，
            # 因此必须归到虚拟设备，否则界面会把它当成「待指派角色的新网卡」一直提示。
            'is_virtual': name in ('lo', 'docker0') or name.startswith(
                ('veth', 'br-', 'virbr', 'ppp', 'ifb')),
            'managed_by': ('QoS 智能限速' if name.startswith('ifb') else ''),
        })
    # 合并已保存的备注/角色（以 MAC 为键，换网卡名也不会错乱）
    try:
        import sqlite3
        dbp = '/opt/drouter/data/drouter.db'
        if os.path.exists(dbp):
            conn = sqlite3.connect(dbp)
            conn.row_factory = sqlite3.Row
            meta = {r['mac'].lower(): dict(r)
                    for r in conn.execute('SELECT mac,name,remark,role FROM ifaces')}
            conn.close()
            for it in res:
                m = meta.get((it.get('mac') or '').lower())
                if m:
                    it['remark'] = m.get('remark') or ''
                    it['role'] = m.get('role') or ''
                    it['known_name'] = m.get('name') or ''
    except Exception:
        pass
    return ok(res)


def read_routes(_):
    rc4, r4, _e = sh(['ip', '-4', '-j', 'route'])
    rc6, r6, _e = sh(['ip', '-6', '-j', 'route'])
    v4 = rc4 == 0 and json.loads(r4) or []
    v6 = rc6 == 0 and json.loads(r6) or []
    return ok({'v4': v4, 'v6': v6})


SAFE_SERVICES = ['dnsmasq', 'radvd', 'dhcpcd', 'miniupnpd', 'chrony', 'systemd-networkd',
                 'NetworkManager', 'docker', 'containerd', 'sshd', 'drouter-web',
                 'pppd-drouter', 'lightdm', 'rsyslog',
                 'smbd', 'nmbd', 'nfs-server', 'rpcbind']


def _svc_states(names):
    """批量取一组 systemd 单元的 active/enabled 状态，一次 fork 搞定。

    为什么必须批量：原实现是 `for s in SAFE_SERVICES: systemctl is-active / is-enabled`，
    18 个单元 × 2 条命令 = **36 次 fork+exec**。systemctl 本身要连一次 dbus，
    在 2 核软路由上单次约 15~40ms，36 次就是 0.5~1.4 秒的纯等待 —— 而这只是
    「概览页画一个服务状态小卡片」（前端 viewDash 每次进入都调 /api/services）。

    改成一次 `systemctl show -p ActiveState -p UnitFileState --value ...`：
    一条命令同时要两个属性，Multiple units 的返回是按 unit 分段、
    每段含请求的属性行。实测输出形如：

        active
        enabled
        inactive
        disabled

    即每段固定 N 行（N = 请求的属性个数），顺序与命令行给的 unit 顺序一致。
    这里按 N 行一组切分，容错处理（行数不足则缺的填 unknown）。
    """
    out = {n: {'active': 'unknown', 'enabled': 'unknown'} for n in names}
    if not names:
        return out
    props = ('ActiveState', 'UnitFileState')
    try:
        rc, txt, _e = sh(['systemctl', 'show', '-p', 'ActiveState', '-p', 'UnitFileState',
                          '--value', '--'] + list(names), timeout=20)
    except Exception:
        rc, txt = 1, ''
    if rc != 0:
        return out
    vals = [v.strip() for v in (txt or '').splitlines()]
    # --value 输出里每个 unit 恰好贡献 len(props) 行
    for i, n in enumerate(names):
        chunk = vals[i * len(props):(i + 1) * len(props)]
        if len(chunk) >= 2:
            out[n] = {'active': chunk[0] or 'inactive',
                      'enabled': chunk[1] or 'disabled'}
    return out


def read_services(_):
    # 性能：18 个单元原来要 36 次 systemctl fork（每次都要连 dbus），实测 0.5~1.4s；
    # 现在 1 次 systemctl show 批量取回。语义完全等价（active/enabled 字段名不变）。
    return ok(_svc_states(SAFE_SERVICES))


def read_nft(_):
    rc, out, err = sh(['nft', '-j', 'list', 'ruleset'], timeout=20)
    if rc != 0:
        return fail('读取防火墙规则失败：' + err)
    try:
        data = json.loads(out or '{"nftables":[]}')
    except Exception:
        data = {'nftables': []}
    tables = []
    for item in data.get('nftables', []):
        if 'table' in item:
            t = item['table']
            tables.append({'family': t.get('family'), 'name': t.get('name')})
    return ok({'raw': out, 'tables': tables})


def read_upnpmap(_):
    """UPnP / NAT-PMP 当前映射表。只读接口：绝不顺手拉服务 ——
    服务没起就如实回报，「查询」按钮不能产生「启动服务」这种副作用。"""
    if not os.path.isfile('/usr/sbin/miniupnpd'):
        return fail('miniupnpd 未安装，请先在「依赖自检」页安装', code='NOT_INSTALLED',
                    data={'mappings': [], 'active': False})
    _rc, out, _e = sh(['systemctl', 'is-active', 'miniupnpd'])
    active = (out.strip() == 'active')
    rows = []
    # miniupnpd 租约文件逐行记录每条映射：
    # 协议:外部端口:内网地址:内网端口:到期时间戳:描述
    for path in ('/var/run/miniupnpd.leases', '/run/miniupnpd/miniupnpd.leases',
                 '/run/miniupnpd.leases'):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    p = line.split(':')
                    if len(p) < 5:
                        continue
                    rows.append({
                        'proto': p[0], 'ext_port': p[1], 'int_ip': p[2],
                        'int_port': p[3],
                        'expires': int(p[4]) if p[4].isdigit() else 0,
                        'desc': ':'.join(p[5:]) if len(p) > 5 else '',
                    })
        except Exception:
            pass
        break
    note = ('' if active else
            'miniupnpd 未在运行：没有映射属正常。到 UPnP 页「保存并应用」启动服务后再看。')
    return ok({'mappings': rows, 'active': active, 'note': note}, 'UPnP 映射表已读取')


def read_leases(_):
    """DHCP 租约表（含剩余时间、主机名、是否已静态绑定）"""
    path = '/var/lib/misc/dnsmasq.leases'
    rows = []
    nowts = int(time.time())
    # 静态绑定清单（来自 drouter 配置）
    static_map = {}
    try:
        cfgp = '/opt/drouter/data/drouter.db'
        if os.path.exists(cfgp):
            import sqlite3
            conn = sqlite3.connect(cfgp)
            conn.row_factory = sqlite3.Row
            r = conn.execute("SELECT value FROM settings WHERE key='dnsmasq'").fetchone()
            conn.close()
            if r:
                cfg = json.loads(r['value'])
                for sl in (cfg.get('static_leases') or []):
                    # 必须和渲染层一致地看 enabled：取消勾选后 dnsmasq 已经
                    # 不再生效这条绑定，租约表若还标「静态」，前端就会把
                    # 「转为静态」按钮置灰，用户既不能转也不能再启用。
                    if sl.get('mac') and v_bool(sl.get('enabled')):
                        static_map[sl['mac'].lower()] = sl
    except Exception:
        pass
    if os.path.isfile(path):
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                p = line.split()
                if len(p) >= 5:
                    try:
                        exp = int(p[0])
                    except Exception:
                        exp = 0
                    mac = p[1].lower()
                    left = max(0, exp - nowts) if exp else 0
                    rows.append({
                        'expire': exp, 'expire_text': datetime.fromtimestamp(exp).strftime('%Y-%m-%d %H:%M:%S') if exp else '',
                        'remain_s': left, 'remain_text': _fmt_dur(left),
                        'mac': mac, 'ip': p[2], 'name': (p[3] if p[3] != '*' else ''),
                        'client_id': p[4],
                        'is_static': mac in static_map,
                        'static_ip': (static_map.get(mac) or {}).get('ip', ''),
                    })
    def _ipkey(r):
        ip = r.get('ip') or ''
        if re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
            return tuple(int(x) for x in ip.split('.'))
        return (999, 999, 999, 999)
    rows.sort(key=_ipkey)
    return ok(rows)


def _fmt_dur(sec):
    sec = int(sec or 0)
    if sec <= 0:
        return '已过期'
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return '%d天%d小时' % (d, h)
    if h:
        return '%d小时%d分' % (h, m)
    if m:
        return '%d分%d秒' % (m, s)
    return '%d秒' % s


def read_iface_method(_):
    """
    读取每张网卡的 IPv4 配置方式：手动 / DHCP / 静态 / 未配置
    优先用 nmcli，回退到 networkd 与内核状态。
    """
    out = {}
    # 1) NetworkManager
    rc, txt, _e = sh(['nmcli', '-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION', 'device', 'status'], timeout=10)
    if rc == 0 and txt:
        for line in txt.splitlines():
            p = line.split(':')
            if len(p) >= 4 and p[1] in ('ethernet', 'wifi', 'bridge', 'bond', 'vlan'):
                out[p[0]] = {'manager': 'NetworkManager', 'state': p[2], 'connection': p[3] or ''}
    # 2) 逐口取 IPv4 方法
    for name, info in list(out.items()):
        method = '未配置'
        cname = info.get('connection') or ''
        if cname:
            rc2, m, _e = sh(['nmcli', '-t', '-g', 'ipv4.method', 'connection', 'show', cname], timeout=10)
            if rc2 == 0:
                mv = (m or '').strip()
                method = {'auto': 'DHCP 自动', 'manual': '静态地址',
                          'disabled': '未配置', 'link-local': '链路本地', 'shared': '共享模式'}.get(mv, mv or '未配置')
        elif info.get('state') == 'disconnected':
            method = '未连接'
        info['method'] = method
    # 3) systemd-networkd 回退（若该网卡由 networkd 管理）
    for ifn in os.listdir('/sys/class/net'):
        if ifn in ('lo',) or ifn in out:
            continue
        if ifn.startswith(('veth', 'br-', 'virbr', 'ppp', 'docker')):
            continue
        mgr, method = '', ''
        # 通过 networkctl 判断
        rc3, st, _e = sh(['networkctl', 'status', ifn], timeout=8)
        if rc3 == 0 and st:
            if 'Network File' in st:
                mgr = 'systemd-networkd'
            if 'DHCP=yes' in st or 'DHCP=ipv4' in st:
                method = 'DHCP 自动'
            elif 'Address:' in st:
                method = '静态地址'
            else:
                method = '未配置'
        if not mgr:
            mgr = '内核/其它'
            # 若该网卡无地址，视为未配置
            method = '未配置'
        out[ifn] = {'manager': mgr, 'state': 'unknown', 'connection': '', 'method': method}
    # 4) 兜底：有静态地址但无管理器信息
    rc, raw, _e = sh(['ip', '-j', '-4', 'addr', 'show'], timeout=10)
    if rc == 0:
        try:
            for d in json.loads(raw):
                n = d.get('ifname')
                if n not in out:
                    continue
                has4 = any(a.get('family') == 'inet' for a in (d.get('addr_info') or []))
                if not out[n].get('method') or out[n]['method'] == '未配置':
                    out[n]['method'] = '静态地址' if has4 else '未配置'
        except Exception:
            pass
    # 转为数组，便于前端逐卡渲染
    arr = []
    for k, v in out.items():
        v = dict(v)
        v['name'] = k
        arr.append(v)
    arr.sort(key=lambda x: x.get('name', ''))
    return ok({'ifaces': arr, 'map': out})


def read_ppp_log(_):
    """
    读取 PPPoE 实时日志 + 状态。
    返回：state（状态码）、state_cn（中文）、remain_s（重拨倒计时）、lines（已翻译日志）
    """
    lines = []
    # 1) pppd 日志（journal），有时序
    rc, txt, _e = sh(['journalctl', '-u', 'pppd-drouter', '-n', '80', '--no-pager', '-o', 'short-iso'], timeout=12)
    if rc != 0 or not txt:
        rc, txt, _e = sh(['journalctl', '-t', 'pppd', '-n', '80', '--no-pager', '-o', 'short-iso'], timeout=12)
    for line in (txt or '').splitlines():
        ts, msg = _split_journal_line(line)
        if not msg:
            continue
        lines.append({'ts': ts, 'raw': msg, 'cn': _translate_ppp(msg), 'level': _ppp_level(msg)})
    # 2) 状态判定
    state, state_cn, remain = 'idle', '未拨号', 0
    rc, ip4, _e = sh(['ip', '-4', '-o', 'addr', 'show', 'ppp0'], timeout=8)
    rc2, dev, _e = sh(['ip', 'link', 'show', 'ppp0'], timeout=8)
    has_ppp0 = rc2 == 0 and 'ppp0' in (dev or '')
    up = has_ppp0 and 'state UP' in (dev or '')
    if up and rc == 0 and 'inet ' in (ip4 or ''):
        state, state_cn = 'connected', '已连接'
    elif has_ppp0:
        state, state_cn = 'connecting', '连接中…'
    # 3) 是否有拨号进程
    rc3, ps, _e = sh(['pgrep', '-a', '-f', 'pppd.*drouter-wan'], timeout=8)
    running = rc3 == 0 and bool((ps or '').strip())
    if not up and running:
        state, state_cn = 'connecting', '连接中…'
    if not running and not has_ppp0:
        state, state_cn = 'idle', '未拨号'
    # 4) 从日志尾部推断重拨倒计时
    for ln in reversed(lines[-12:]):
        r = ln['raw']
        m = re.search(r'Renegotiation|reconnect|retrying in (\d+)', r, re.I)
        if m and m.group(1):
            remain = int(m.group(1))
            state, state_cn = 'retrying', '重拨中…（%d 秒后重试）' % remain
            break
        if re.search(r'LCP: timeout|No response to|Terminating on signal', r, re.I):
            if not up:
                state, state_cn = 'retrying', '重拨中…'
    # 5) 取 ppp0 地址/对端
    peer = gw = ''
    if up:
        rc4, r4, _e = sh(['ip', '-4', 'route', 'show', 'dev', 'ppp0'], timeout=8)
        m = re.search(r'default via (\S+)', r4 or '')
        if m:
            gw = m.group(1)
        m2 = re.search(r'inet (\S+)', ip4 or '')
        if m2:
            peer = m2.group(1)
    return ok({'state': state, 'state_cn': state_cn, 'remain_s': remain,
               'has_ppp0': has_ppp0, 'running': running,
               'local_ip': peer, 'gateway': gw, 'lines': lines[-60:]})


# ---------------------------------------------------------------- 防火墙日志（#1）
#
# 目标：把 nftables 的 log 输出（内核日志）实时、可滚动地呈现到 Web 界面，
#       并尽量翻译成简体中文。设计要点：
#   * 数据源统一走 journalctl -k（内核环形缓冲），不需要额外守护进程；
#   * 只抓我们自己打的规则日志（前缀 DROUTER-FW-），避免把无关内核噪声混进来；
#   * 「开关」= 是否在规则里生成 log 语句（见 render.py），这里只负责呈现；
#   * 解析出五元组 + 方向 + 动作，供前端做结构化展示与检索。

FW_PREFIX_RE = re.compile(r'DROUTER-FW-([46])-([A-Z]+)-([A-Z]+)\s+(.*)$')

FW_DIR_CN = {
    'IN': '入站（本机）',
    'FWD': '转发（经过本机）',
}
FW_ACT_CN = {
    'REJECT': '拒绝',
    'DROP': '丢弃',
    'ACCEPT': '放通',
    'INVALID': '非法状态',
}
FW_PROTO_CN = {
    'TCP': 'TCP', 'UDP': 'UDP', 'ICMP': 'ICMP', 'ICMPV6': 'ICMPv6',
    'IGMP': 'IGMP', 'GRE': 'GRE', 'ESP': 'IPsec-ESP', 'AH': 'IPsec-AH',
    'SCTP': 'SCTP', 'OSPF': 'OSPF',
}


def _fw_parse_fields(rest):
    """把 nft log 的 K=V 字段串解析成 dict。"""
    out = {}
    for kv in (rest or '').split():
        if '=' in kv:
            k, v = kv.split('=', 1)
            out[k.strip()] = v.strip()
    return out


def translate_fw_log(raw):
    """把一行 nftables 防火墙日志翻译成简体中文结构化记录。

    返回 dict；无法识别时返回 None（调用方跳过）。
    """
    m = FW_PREFIX_RE.search(raw or '')
    if not m:
        return None
    fam, direction, action, rest = m.group(1), m.group(2), m.group(3), m.group(4)
    f = _fw_parse_fields(rest)
    proto = (f.get('PROTO') or '').upper()
    rec = {
        'family': 'IPv4' if fam == '4' else 'IPv6',
        'direction': direction,
        'direction_cn': FW_DIR_CN.get(direction, direction),
        'action': action,
        'action_cn': FW_ACT_CN.get(action, action),
        'proto': FW_PROTO_CN.get(proto, proto or '—'),
        'proto_raw': proto,
        'src': f.get('SRC', ''),
        'dst': f.get('DST', ''),
        'sport': f.get('SPT', ''),
        'dport': f.get('DPT', ''),
        'iface_in': f.get('IN', ''),
        'iface_out': f.get('OUT', ''),
        'len': f.get('LEN', ''),
        'ttl': f.get('TTL', f.get('HOPLIMIT', '')),
        'raw': raw,
    }
    port = ''
    if rec['sport'] or rec['dport']:
        port = ' 端口 %s → %s' % (rec['sport'] or '?', rec['dport'] or '?')
    rec['summary_cn'] = ('%s %s：%s 数据包%s，%s → %s（接口 %s → %s），判定为「%s」'
                         % (rec['family'], rec['direction_cn'], rec['proto'], port,
                            rec['src'] or '?', rec['dst'] or '?',
                            rec['iface_in'] or '?', rec['iface_out'] or '?',
                            rec['action_cn']))
    return rec


def read_fw_log(p):
    """读取防火墙滚动日志（#1）。

    参数：
      family: '4' | '6' | 'all'
      limit : 返回条数
      action: 过滤动作（REJECT/DROP/ACCEPT），空=不过滤
      q     : 关键字（对原始行做包含匹配）
      since : 相对时间（如 '30 min ago'）
    """
    p = p or {}
    family = str(p.get('family') or 'all')
    limit = int(p.get('limit') or 200)
    limit = max(1, min(limit, 2000))
    action = (p.get('action') or '').strip().upper()
    q = (p.get('q') or '').strip().lower()
    since = (p.get('since') or '60 min ago').strip()
    if not re.match(r'^[0-9a-zA-Z :._-]{1,32}$', since):
        since = '60 min ago'

    fetch = min(6000, max(limit * 8, 600))
    rc, out, err = sh(['journalctl', '-k', '-n', str(fetch), '--no-pager',
                       '-o', 'short-iso', '--since', since, '-g', 'DROUTER-FW'],
                      timeout=20)
    if rc != 0 or not out:
        # 回退：不带 -g（部分较老版本的 journalctl 不支持 -g）
        rc, out, err = sh(['journalctl', '-k', '-n', str(fetch), '--no-pager',
                           '-o', 'short-iso', '--since', since], timeout=20)

    items = []
    for line in (out or '').splitlines():
        ts, msg = _split_journal_line(line)
        if 'DROUTER-FW' not in msg:
            continue
        rec = translate_fw_log(msg)
        if not rec:
            continue
        if family in ('4', '6'):
            want = 'IPv4' if family == '4' else 'IPv6'
            if rec['family'] != want:
                continue
        if action and rec['action'] != action:
            continue
        if q and q not in (msg or '').lower():
            continue
        rec['ts'] = ts
        items.append(rec)
    items = items[-limit:]

    stat = {'total': len(items), 'reject': 0, 'accept': 0, 'invalid': 0,
            'v4': 0, 'v6': 0}
    for x in items:
        if x['action'] in ('REJECT', 'DROP'):
            stat['reject'] += 1
        elif x['action'] == 'ACCEPT':
            stat['accept'] += 1
        elif x['action'] == 'INVALID':
            stat['invalid'] += 1
        if x['family'] == 'IPv4':
            stat['v4'] += 1
        else:
            stat['v6'] += 1

    enabled = _fw_log_enabled()

    # 「这里一条都没有」其实有两种完全不同的原因，必须分开讲清楚，
    # 否则用户只会以为这个功能是空的没做：
    #   1) 规则压根没载入内核 —— 本机还没接管路由，内核里没有可记录的包；
    #   2) 规则已载入，但「记录被拒绝的包」开关没开 —— 规则里不含 log 语句。
    applied = _fw_ruleset_applied()
    forwarding = _ip_forwarding_on()
    if not applied:
        warn = ('当前内核里没有 drouter 的防火墙规则（nft 规则集为空），还没有任何包经过本机的'
                '防火墙链，所以这里不会有记录。请到「防火墙 IPv4 / IPv6」页面点一次「应用」，'
                '规则载入后即可开始记录。')
    elif not enabled:
        warn = ('防火墙日志开关当前为关闭：规则里不会生成 log 语句，因此这里不会有新记录。'
                '请到「防火墙 IPv4 / IPv6」页面打开「记录被拒绝的包」开关并应用。')
    else:
        warn = ''

    return ok({'items': items, 'stat': stat, 'enabled': enabled,
               'applied': applied, 'forwarding': forwarding,
               'warn': warn, 'err': (err or ''), 'since': since})


def _fw_ruleset_applied():
    """判断 drouter 的 nft 表是否已载入内核。

    没载入时内核里不存在任何 log 规则，防火墙日志页会一直是空的。这不是功能缺失，
    而是本机还没接管路由，界面必须据此给出解释，而不是让用户对着空白页猜。
    """
    rc, out, _e = sh(['nft', 'list', 'ruleset'], timeout=10)
    if rc != 0:
        return False
    return 'drouter' in (out or '')


def _ip_forwarding_on():
    """读取内核 IP 转发开关（判断本机是否真的在转发流量）。"""
    try:
        with open('/proc/sys/net/ipv4/ip_forward', encoding='utf-8') as f:
            v4 = f.read().strip()
        with open('/proc/sys/net/ipv6/conf/all/forwarding', encoding='utf-8') as f:
            v6 = f.read().strip()
    except OSError:
        return False
    return v4 == '1' or v6 == '1'


def _fw_log_enabled():
    """判断防火墙日志开关是否已启用（IPv4 或 IPv6 任一开启即视为开启）。"""
    try:
        v4 = _load_setting('nft_v4') or {}
        v6 = _load_setting('nft_v6') or {}
        if (v4.get('raw') or '').strip() or (v6.get('raw') or '').strip():
            return True
        return bool(v4.get('log_drop', True) or v6.get('log_drop', True))
    except Exception:
        return True


def _split_journal_line(line):
    """journalctl short-iso 输出：2026-09-28T14:43:01+0800 host prog[pid]: msg"""
    line = (line or '').strip()
    if not line:
        return '', ''
    m = re.match(r'^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*)\s+\S+\s+\S+\s+(.*)$', line)
    if m:
        return m.group(1), m.group(2).strip()
    m2 = re.match(r'^(?:\S+\s+){2,3}(.*)$', line)
    return '', (m2.group(1) if m2 else line)


def _ppp_level(raw):
    r = raw.lower()
    if re.search(r'error|fail|timeout|denied|refused|abort', r):
        return 'err'
    if re.search(r'warn|no response|retry|reneg', r):
        return 'warn'
    if re.search(r'connected|complete|ipcp: local|peer', r):
        return 'ok'
    return 'info'


PPP_TRANS = [
    (r'Plugin .* loaded', '已加载 PPPoE 插件（rp-pppoe）'),
    (r'Using interface (ppp\d+)', '使用拨号接口 \\1'),
    (r'Connect: (\S+)', '开始拨号，物理网口 \\1'),
    (r'sent \[PADI\]', '发送 PADI —— 正在寻找宽带接入服务器（BRAS）'),
    (r'recv \[PADO\]', '收到 PADO —— 找到接入服务器，准备协商'),
    (r'sent \[PADR\]', '发送 PADR —— 请求建立会话'),
    (r'recv \[PADS\]', '收到 PADS —— 会话已建立，进入 PPP 协商阶段'),
    (r'no PADO|PADO timeout', '未收到 PADO —— 未找到接入服务器（检查物理线路）'),
    (r'PADS timeout', 'PADS 超时 —— 接入服务器无响应'),
    (r'Script .* finished', 'PPPoE 发现阶段脚本执行完毕'),
    (r'local  IP address (\S+)', '本端 IP 地址：\\1'),
    (r'remote IP address (\S+)', '对端（网关）IP 地址：\\1'),
    (r'primary\s+DNS address (\S+)', '主 DNS 服务器：\\1'),
    (r'secondary DNS address (\S+)', '备用 DNS 服务器：\\1'),
    (r'LCP: timeout sending Config-Requests', 'LCP 超时 —— 对端无响应，链路可能不通'),
    (r'Peer is not responding to LCP echo', '对端未回应 LCP 心跳，链路疑似断开'),
    (r'No response to \d+ echo-requests', '连续多次心跳无响应，判定链路断开'),
    (r'Serial link appears to be disconnected', '链路已断开'),
    (r'Connection terminated', '连接已终止'),
    (r'Terminating on signal \d+', '收到终止信号，正在结束连接'),
    (r'LCP terminated by peer', '对端主动终止了 LCP 连接'),
    (r'IPCP: timeout sending Config-Requests', 'IPCP 超时 —— 地址协商失败（常见于账号未通过认证）'),
    (r'PAP authentication failed|CHAP authentication failed', '宽带账号或密码认证失败'),
    (r'Authentication failed', '认证失败 —— 请核对宽带账号与密码'),
    (r'Remote message: (.*)', '来自运营商的消息：\\1'),
    (r'peer from calling number (\S+) authorized', '对端 \\1 已通过认证，链路就绪'),
    (r'Modem hangup', '链路挂断'),
    (r'Renegotiation', '正在重新协商'),
    (r'exit', '进程退出'),
]


def _translate_ppp(raw):
    for pat, rep in PPP_TRANS:
        if re.search(pat, raw, re.I):
            return re.sub(pat, rep, raw, flags=re.I)
    return raw


# ---------------------------------------------------------------- WAN 接入方式统一日志（#4）
#
# 目标：无论 WAN 采用哪种接入方式（PPPoE / DHCP / 静态 / IPoE / 双栈 / 桥接 / 专线固定 IP），
#       都在同一套「实时状态栏 + 滚动日志」里呈现，并统一翻译成简体中文。
#
# 设计要点：
#   * 每种接入方式有各自的「数据来源」（journal 单元 / dhclient 租约 / 内核 netlink 事件），
#     但输出结构完全一致：{state, state_cn, meta[], lines[{ts,raw,cn,level}], source}
#   * 状态判定优先看真实链路（内核地址 / 路由 / 保活进程），日志只做辅助解释；
#   * 翻译表按「接入方式」分组，避免 PPPoE 的 PADI/PADO 与 DHCP 的 DISCOVER/OFFER 混淆。

# 中国大陆家庭宽带常见接入方式（用于前端下拉与说明）
WAN_ACCESS_TYPES = [
    {'v': 'pppoe', 'n': 'PPPoE 拨号', 'desc': '光猫桥接 + 电脑/路由拨号，需要宽带账号密码（电信/联通/移动/广电均常见）'},
    {'v': 'dhcp', 'n': 'DHCP 自动（IPoE）', 'desc': '光猫已拨号，下挂设备自动获取地址；也用于部分地区「IPoE 免拨号」'},
    {'v': 'static', 'n': '静态地址（专线 / 固定 IP）', 'desc': '运营商分配固定公网 IP，手动配置地址、网关与 DNS'},
    {'v': 'pppoe_dhcp6', 'n': 'PPPoE + DHCPv6-PD（双栈）', 'desc': 'IPv4 走 PPPoE、IPv6 通过 DHCPv6-PD 下发前缀，国内主流双栈方式'},
    {'v': 'ipoe_dhcp6', 'n': 'IPoE + DHCPv6-PD（双栈）', 'desc': 'IPv4/IPv6 均由上级自动下发，适用于光猫路由模式 + IPv6 直连'},
    {'v': 'bridge', 'n': '桥接旁路（不改 WAN）', 'desc': '仅做二层透传/旁路，不参与拨号，WAN 状态由上级设备决定'},
]

# 每种接入方式对应的「日志来源」说明，前端展示用
WAN_LOG_SOURCE = {
    'pppoe': 'pppd 拨号进程日志（journalctl -u pppd-drouter）+ 内核 ppp 链路事件',
    'dhcp': 'dhclient / NetworkManager 租约协商日志 + 内核地址事件',
    'static': '内核地址与路由事件（netlink）+ 网关可达性探测',
    'pppoe_dhcp6': 'pppd 拨号日志 + 内核 IPv6 地址/前缀事件（DHCPv6-PD）',
    'ipoe_dhcp6': 'dhclient（v4/v6）日志 + 内核 IPv6 路由器通告事件',
    'bridge': '内核网桥/链路事件（netlink）',
}

# --- DHCP 中文翻译表（DISCOVER / OFFER / REQUEST / ACK / NAK ...）
DHCP_TRANS = [
    (r'DHCPDISCOVER on (\S+) to 255\.255\.255\.255', '在 \\1 上广播 DHCPDISCOVER —— 寻找 DHCP 服务器'),
    (r'DHCPOFFER of (\S+) from (\S+)', '收到 DHCPOFFER：服务器 \\2 提供地址 \\1'),
    (r'DHCPREQUEST for (\S+) on (\S+)', '发送 DHCPREQUEST：向 \\2 请求地址 \\1'),
    (r'DHCPACK of (\S+) from (\S+)', '收到 DHCPACK：\\2 确认分配地址 \\1，租约生效'),
    (r'DHCPNAK from (\S+)', '收到 DHCPNAK：服务器 \\1 拒绝请求（地址已失效或冲突）'),
    (r'DHCPDECLINE', 'DHCPDECLINE —— 检测到地址冲突，已放弃该地址'),
    (r'DHCPRELEASE', 'DHCPRELEASE —— 已释放地址租约'),
    (r'DHCPINFORM', 'DHCPINFORM —— 仅请求配置参数（不申请地址）'),
    (r'bound to (\S+) -- renewal in (\d+) seconds', '已绑定地址 \\1，将在 \\2 秒后续租'),
    (r'Renewing DHCP lease', '正在续租 DHCP 地址'),
    (r'no lease, failing|No DHCPOFFERS received', '未收到任何 DHCPOFFER —— 上级未响应（检查线路或上级是否开启 DHCP）'),
    (r'RTNETLINK answers: File exists', '地址已存在，跳过重复配置'),
    (r'inet (\S+/\d+) brd', '已配置 IPv4 地址 \\1'),
]

# --- 静态地址 / 内核网络事件中文翻译表
# 注意：更具体的规则必须排在前面（如 "NIC Link is Up" 要先于泛化的 "Link is Up"）。
STATIC_TRANS = [
    (r'(\S+) NIC Link is Down', '\\1 物理链路已断开'),
    (r'(\S+) NIC Link is Up (\d+) Mbps', '\\1 链路已连接，速率 \\2 Mbps'),
    (r'(\S+) NIC Link is Up', '\\1 链路已连接（网卡协商完成）'),
    (r'(\S+) Link is Down', '\\1 链路已断开'),
    (r'(\S+) Link is Up', '\\1 链路已连接（物理层就绪）'),
    (r'(\S+) link becomes ready', '\\1 链路就绪，可以发包'),
    (r'(\S+) link is not ready', '\\1 链路尚未就绪'),
    (r'carrier lost', '载波丢失 —— 网线或上级设备断开'),
    (r'carrier acquired', '载波恢复 —— 物理线路已接好'),
    (r'Adding default route', '正在添加默认路由'),
    (r'Deleting default route', '正在删除默认路由'),
    (r'default via (\S+) dev (\S+)', '已添加默认路由：网关 \\1，出口 \\2'),
    (r'inet (\S+) scope global (\S+)', '已添加 IPv4 地址 \\1（接口 \\2）'),
    (r'duplicate address detected', '检测到重复地址（IP 冲突）'),
    (r'link-local', '链路本地地址'),
]

# --- IPv6 / DHCPv6-PD 中文翻译表
IPV6_TRANS = [
    (r'DHCPv6 REPLY.*IA_PD.*prefix (\S+)', '收到 DHCPv6 前缀下发（IA_PD）：\\1'),
    (r'IA_PD prefix ([0-9a-f:/]+)', 'IPv6 前缀 \\1 已下发给内网'),
    (r'IA_NA.*address ([0-9a-f:]+)', '获得 IPv6 地址 \\1（IA_NA）'),
    (r'Router Advertisement on (\S+)', '收到路由器通告（RA），接口 \\1'),
    (r'RA.*prefix ([0-9a-f:/]+)', '路由器通告前缀：\\1'),
    (r'Adding IPv6 address ([0-9a-f:]+)', '已添加 IPv6 地址 \\1'),
    (r'DAD.*failed', '重复地址检测失败（IPv6 地址冲突）'),
    (r'accept_ra=0', '内核未接受路由器通告（accept_ra=0），IPv6 可能无法自动配置'),
    (r'No route to host', '无可达路由 —— IPv6 默认路由缺失'),
]

# --- IPoE / 网桥等通用链路事件
BRIDGE_TRANS = [
    (r'(\S+): entered (forwarding|disabled|learning) state', '\\1 网桥端口进入 \\2 状态'),
    (r'(\S+): port (\d+)', '\\1 端口 \\2 事件'),
    (r'bridge.*link (up|down)', '网桥链路 \\1'),
]


def _wan_level(raw, ok_pats=None, err_pats=None):
    """按通用规则 + 各接入方式自定义关键字判断日志级别。

    判定顺序：自定义错误 > 通用错误 > 自定义成功 > 通用成功 > 警告 > 普通。
    错误优先于成功，避免 "Link is Up" 这类同时含 up/down 词的误判。
    """
    r = (raw or '').lower()
    for p in (err_pats or []):
        if re.search(p, r):
            return 'err'
    if re.search(r'error|fail|timeout|denied|refused|abort|nak|decline|no response|'
                 r'not ready|carrier lost|no route|is down|duplicate', r):
        return 'err'
    for p in (ok_pats or []):
        if re.search(p, r):
            return 'ok'
    if re.search(r'ack|bound|connected|complete|is up|link up|acquired|authorized|'
                 r'reply|becomes ready|carrier acquired', r):
        return 'ok'
    if re.search(r'warn|retry|reneg|renew|discover|request', r):
        return 'warn'
    return 'info'


def _wan_translate(raw, table):
    """在指定翻译表里做逐条匹配替换（保留未命中的原文）。"""
    txt = raw or ''
    for pat, rep in table:
        if re.search(pat, txt, re.I):
            return re.sub(pat, rep, txt, flags=re.I)
    return txt


def _wan_from_journal(units, tags, limit=80, since='2 hours ago'):
    """从若干 journal 单元/标签采集日志，返回 [(ts, msg)]。"""
    out = []
    seen = set()
    for src in list(units) + list(tags):
        if src in seen:
            continue
        seen.add(src)
        key = '-u' if src in units else '-t'
        cmd = ['journalctl', key, src, '-n', str(limit), '--no-pager',
               '-o', 'short-iso', '--since', since]
        rc, txt, _e = sh(cmd, timeout=12)
        if rc != 0 or not txt:
            continue
        for line in (txt or '').splitlines():
            ts, msg = _split_journal_line(line)
            if msg:
                out.append((ts, msg))
    out.sort(key=lambda x: x[0] or '')
    return out


def _wan_iface_status(ifname):
    """读取某个网卡的实时链路/地址状态（供状态判定与元信息展示）。"""
    st = {'name': ifname, 'exists': False, 'oper': 'DOWN', 'ip4': '', 'ip6': '',
          'gw': '', 'mtu': '', 'speed': '', 'carrier': ''}
    if not ifname:
        return st
    if os.path.exists('/sys/class/net/' + ifname):
        st['exists'] = True
        for f, k in (('operstate', 'oper'), ('mtu', 'mtu'), ('speed', 'speed'),
                     ('carrier', 'carrier')):
            try:
                with open('/sys/class/net/%s/%s' % (ifname, f)) as fh:
                    v = fh.read().strip()
                st[k] = v.upper() if k == 'oper' else v
            except Exception:
                pass
    rc, raw, _e = sh(['ip', '-j', 'addr', 'show', ifname], timeout=8)
    if rc == 0:
        try:
            for d in json.loads(raw):
                for a in (d.get('addr_info') or []):
                    if a.get('family') == 'inet' and not st['ip4']:
                        st['ip4'] = '%s/%s' % (a.get('local', ''), a.get('prefixlen', ''))
                    if a.get('family') == 'inet6' and a.get('scope') == 'global' and not st['ip6']:
                        st['ip6'] = '%s/%s' % (a.get('local', ''), a.get('prefixlen', ''))
        except Exception:
            pass
    rc, raw, _e = sh(['ip', '-4', 'route', 'show', 'dev', ifname], timeout=8)
    m = re.search(r'default via (\S+)', raw or '')
    if m:
        st['gw'] = m.group(1)
    return st


def _neigh_gateway_mac(ifname, gw):
    """通过 ARP/邻居表判断网关是否可达（有 MAC 即视为二层可达）。"""
    if not ifname or not gw:
        return ''
    rc, raw, _e = sh(['ip', 'neigh', 'show', 'dev', ifname, 'to', gw], timeout=6)
    m = re.search(r'lladdr ([0-9a-f:]{17})', raw or '', re.I)
    return m.group(1) if m else ''


def _read_dhcp_lease(ifname):
    """读取 dhclient / NetworkManager 的租约文件，取最新一条。"""
    lease = {'file': '', 'ip': '', 'gw': '', 'dns': '', 'expire': '', 'server': ''}
    cands = ['/var/lib/dhcp/dhclient.%s.leases' % ifname,
             '/var/lib/dhcp/dhclient.leases',
             '/var/lib/NetworkManager/dhclient-%s.lease' % ifname]
    path = ''
    for c in cands:
        if c and os.path.exists(c):
            path = c
            break
    if not path:
        return lease
    lease['file'] = path
    try:
        with open(path) as f:
            txt = f.read()
    except Exception:
        return lease
    # 取最后一个 lease 块
    blocks = re.findall(r'lease\s*\{([^}]*)\}', txt, re.S)
    if not blocks:
        return lease
    blk = blocks[-1]
    for key, pat in (('ip', r'^\s*fixed-address\s+([0-9.]+)'),
                     ('server', r'option\s+dhcp-server-identifier\s+([0-9.]+)'),
                     ('expire', r'expire\s+\d+\s+(\S+\s+\S+\s+\d+\s+\S+);')):
        m = re.search(pat, blk, re.M | re.I)
        if m:
            lease[key] = m.group(1).strip()
    m = re.search(r'option\s+routers\s+([0-9.]+)', blk, re.I)
    if m:
        lease['gw'] = m.group(1)
    dns = re.findall(r'option\s+domain-name-servers\s+([0-9.,\s]+);', blk, re.I)
    if dns:
        lease['dns'] = ','.join(x.strip().rstrip(';') for x in dns[0].split(',') if x.strip())
    return lease


def _dhcp_leases_file_meta(path):
    """获取租约文件的时间信息，用于展示租约新鲜度。"""
    try:
        stt = os.stat(path)
        return {'mtime': int(stt.st_mtime), 'size': stt.st_size}
    except Exception:
        return {}


def read_wan_log(p):
    """统一 WAN 接入日志与实时状态（#4）。

    参数 access：pppoe / dhcp / static / pppoe_dhcp6 / ipoe_dhcp6 / bridge
    返回结构与 read_ppp_log 对齐，便于前端复用同一个滚动面板。
    """
    p = p or {}
    access = (p.get('access') or 'pppoe').strip()
    if access not in [x['v'] for x in WAN_ACCESS_TYPES]:
        access = 'pppoe'
    ifname = (p.get('iface') or '').strip()
    limit = int(p.get('limit') or 120)
    limit = max(10, min(limit, 500))
    since = (p.get('since') or '2 hours ago').strip()
    if not re.match(r'^[0-9a-zA-Z :._-]{1,32}$', since):
        since = '2 hours ago'

    # 若无显式网卡，尝试从设置里推断 WAN 口
    if not ifname:
        try:
            sysc = _load_setting('system') or {}
            ifname = (sysc.get('wan_iface') or '').strip()
        except Exception:
            ifname = ''

    lines = []
    table = STATIC_TRANS
    state, state_cn = 'unknown', '未知状态'
    meta = []
    extra = {}

    # ---------------- PPPoE / PPPoE+DHCPv6 ----------------
    if access in ('pppoe', 'pppoe_dhcp6'):
        table = PPP_TRANS
        got = _wan_from_journal(('pppd-drouter',), ('pppd',), limit=limit, since=since)
        # 拨号接口优先 ppp0；否则用逻辑接口名
        ppp_if = 'ppp0'
        st = _wan_iface_status(ppp_if)
        ip4_raw = ''
        if st['ip4']:
            ip4_raw = st['ip4']
        # 内核 ppp 事件补充
        klog = _wan_from_journal((), (), limit=0)  # 占位，避免多余调用
        for ts, msg in got:
            lines.append({'ts': ts, 'raw': msg, 'cn': _wan_translate(msg, table),
                          'level': _wan_level(msg, ok_pats=[r'authorized', r'local  ip'],
                                              err_pats=[r'authentication failed', r'no pado', r'timeout'])})
        running = False
        rc, ps, _e = sh(['pgrep', '-a', '-f', 'pppd.*drouter-wan'], timeout=8)
        running = rc == 0 and bool((ps or '').strip())
        has_ppp0 = st['exists']
        if has_ppp0 and st['oper'] == 'UP' and st['ip4']:
            state, state_cn = 'connected', '已连接（PPPoE 拨号成功）'
            if ip4_raw:
                meta.append('本端 IP ' + ip4_raw)
            if st['gw']:
                meta.append('对端网关 ' + st['gw'])
            if st['mtu']:
                meta.append('MTU ' + st['mtu'])
        elif has_ppp0:
            state, state_cn = 'connecting', '连接中…（会话已建立，等待认证）'
        if not has_ppp0 and running:
            state, state_cn = 'connecting', '连接中…（正在与接入服务器协商）'
        if not has_ppp0 and not running:
            state, state_cn = 'idle', '未拨号（拨号进程未运行）'
        # 从日志尾部推断重拨
        for ln in reversed(lines[-12:]):
            m = re.search(r'retrying in (\d+)', ln['raw'], re.I)
            if m:
                state, state_cn = 'retrying', '重拨中…（%d 秒后重试）' % int(m.group(1))
                meta.append('将在 %s 秒后自动重拨' % m.group(1))
                break
        extra['role'] = 'ppp0' if access == 'pppoe' else 'ppp0 + ipv6-PD'
        if access == 'pppoe_dhcp6':
            # 追加 IPv6 前缀事件
            v6 = _wan_from_journal((), ('dhcp6c', 'odhcp6c', 'systemd-networkd'), limit=40, since=since)
            for ts, msg in v6:
                if re.search(r'dhcpv6|ia_pd|prefix|router advertisement', msg, re.I):
                    lines.append({'ts': ts, 'raw': msg, 'cn': _wan_translate(msg, IPV6_TRANS),
                                  'level': _wan_level(msg)})
            if st['ip6']:
                meta.append('IPv6 地址 ' + st['ip6'])
            rc6, r6, _e = sh(['ip', '-6', 'route', 'show', 'default'], timeout=8)
            m6 = re.search(r'default via (\S+)', r6 or '')
            if m6:
                meta.append('IPv6 网关 ' + m6.group(1))
            else:
                meta.append('IPv6 默认路由缺失')

    # ---------------- DHCP（IPoE） ----------------
    elif access in ('dhcp', 'ipoe_dhcp6'):
        table = DHCP_TRANS
        units = ('NetworkManager', 'isc-dhcp-client', 'dhclient', 'dhcpcd',
                 'systemd-networkd')
        tags = ('dhclient', 'dhcpcd', 'avahi-daemon')
        got = _wan_from_journal(units, tags, limit=limit, since=since)
        for ts, msg in got:
            if re.search(r'dhcp|bound|lease|offer|ack|nak|discover|request', msg, re.I):
                lines.append({'ts': ts, 'raw': msg, 'cn': _wan_translate(msg, table),
                              'level': _wan_level(msg,
                                                  ok_pats=[r'dhcpack', r'bound to'],
                                                  err_pats=[r'dhcpnak', r'no dhcpoffers'])})
        st = _wan_iface_status(ifname)
        lease = _read_dhcp_lease(ifname)
        extra['lease_file'] = lease.get('file', '')
        extra['lease'] = lease
        if lease.get('file'):
            extra['lease_meta'] = _dhcp_leases_file_meta(lease['file'])
        if st['exists'] and st['oper'] == 'UP' and st['ip4']:
            state, state_cn = 'connected', '已获取地址（DHCP 协商完成）'
            meta.append('接口 ' + ifname)
            meta.append('地址 ' + st['ip4'])
            if st['gw']:
                meta.append('网关 ' + st['gw'])
                mac = _neigh_gateway_mac(ifname, st['gw'])
                if mac:
                    meta.append('网关可达（MAC %s）' % mac)
                else:
                    meta.append('网关暂不可达（可能为上级设备关闭 ICMP/ARP 未学习）')
        elif st['exists']:
            state, state_cn = 'connecting', '连接中…（等待 DHCP 分配地址）'
            meta.append('接口 ' + ifname)
        else:
            state, state_cn = 'idle', '未连接（WAN 网卡不存在或未分配）'
        if access == 'ipoe_dhcp6':
            if st['ip6']:
                meta.append('IPv6 地址 ' + st['ip6'])
                v6 = _wan_from_journal((), ('systemd-networkd',), limit=40, since=since)
                for ts, msg in v6:
                    if re.search(r'ipv6|ra|prefix|router advertisement', msg, re.I):
                        lines.append({'ts': ts, 'raw': msg, 'cn': _wan_translate(msg, IPV6_TRANS),
                                      'level': _wan_level(msg)})
            else:
                meta.append('IPv6 未获取（检查上级是否开启 IPv6）')

    # ---------------- 静态地址 ----------------
    elif access == 'static':
        table = STATIC_TRANS
        st = _wan_iface_status(ifname)
        extra['iface'] = ifname
        # 内核链路事件（netlink 走 journal -k）
        rc, out, _e = sh(['journalctl', '-k', '-n', '400', '--no-pager',
                          '-o', 'short-iso', '--since', since], timeout=15)
        for line in (out or '').splitlines():
            ts, msg = _split_journal_line(line)
            if not msg:
                continue
            if ifname and ifname in msg and re.search(
                    r'link|carrier|route|addr|duplicate', msg, re.I):
                lines.append({'ts': ts, 'raw': msg, 'cn': _wan_translate(msg, table),
                              'level': _wan_level(msg)})
            elif re.search(r'default via %s' % re.escape(st.get('gw') or '\x00'), msg, re.I):
                lines.append({'ts': ts, 'raw': msg, 'cn': _wan_translate(msg, table),
                              'level': _wan_level(msg)})
        if st['exists'] and st['oper'] == 'UP' and st['ip4']:
            state, state_cn = 'connected', '链路已就绪（静态配置生效）'
            meta.append('接口 ' + ifname)
            meta.append('地址 ' + st['ip4'])
            if st['gw']:
                meta.append('网关 ' + st['gw'])
                mac = _neigh_gateway_mac(ifname, st['gw'])
                if mac:
                    meta.append('网关可达（MAC %s）' % mac)
                else:
                    meta.append('网关未响应 ARP —— 请核对网关地址是否与上级同网段')
            else:
                meta.append('未配置默认网关 —— 可能无法上网')
        elif st['exists']:
            state, state_cn = 'configured', '链路已配置但未连通（检查网线与上级端口）'
            meta.append('接口 ' + ifname)
            if not st['ip4']:
                meta.append('尚未配置 IPv4 地址')
        else:
            state, state_cn = 'idle', '未连接（WAN 网卡不存在或未分配）'
        # 默认路由检查
        rc, r4, _e = sh(['ip', '-4', 'route', 'show', 'default'], timeout=8)
        if r4 and r4.strip():
            m = re.search(r'default via (\S+) dev (\S+)', r4)
            if m:
                meta.append('当前默认路由 → %s（出口 %s）' % (m.group(1), m.group(2)))
        else:
            meta.append('当前无 IPv4 默认路由')

    # ---------------- 桥接旁路 ----------------
    elif access == 'bridge':
        table = BRIDGE_TRANS
        st = _wan_iface_status(ifname)
        rc, out, _e = sh(['journalctl', '-k', '-n', '300', '--no-pager',
                          '-o', 'short-iso', '--since', since], timeout=15)
        for line in (out or '').splitlines():
            ts, msg = _split_journal_line(line)
            if msg and re.search(r'bridge|br\d|vlan', msg, re.I):
                lines.append({'ts': ts, 'raw': msg, 'cn': _wan_translate(msg, table),
                              'level': _wan_level(msg)})
        state, state_cn = ('up', '网桥已工作（透传模式）') if st['exists'] else ('idle', '未启用')
        meta.append('接口 ' + (ifname or '—') + '，桥接模式不参与拨号')

    # 去重并按时间排序，取尾部 limit 条
    uniq = []
    seen = set()
    for x in lines:
        k = (x['ts'], x['raw'])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(x)
    uniq = uniq[-limit:]

    # 日志为空时给出可读提示
    tip = ''
    if not uniq:
        tip_map = {
            'pppoe': '暂无拨号日志：请确认已点击「开始拨号」，且构建保护模式未阻止操作。',
            'dhcp': '暂无 DHCP 协商日志：若已获取到地址，说明协商发生在更早时段；可调大时间范围查看。',
            'static': '暂无链路事件：静态模式下地址由配置文件直接写入，通常不会产生协商日志。',
            'pppoe_dhcp6': '暂无双栈日志：请确认 pppd 已拨号且上级下发了 IPv6 前缀（IA_PD）。',
            'ipoe_dhcp6': '暂无 IPv6 事件：请确认上级已开启 IPv6 且已下发路由器通告（RA）。',
            'bridge': '暂无网桥事件：桥接模式仅在链路状态变化时产生日志。',
        }
        tip = tip_map.get(access, '暂无日志。')

    # 接入方式说明（前端直接展示）
    acc_info = next((x for x in WAN_ACCESS_TYPES if x['v'] == access), WAN_ACCESS_TYPES[0])
    return ok({
        'access': access, 'access_cn': acc_info['n'], 'access_desc': acc_info['desc'],
        'source': WAN_LOG_SOURCE.get(access, ''),
        'iface': ifname,
        'state': state, 'state_cn': state_cn,
        'meta': meta, 'extra': extra,
        'tip': tip,
        'lines': uniq,
        'catalog': WAN_ACCESS_TYPES,
    })


# ---------------------------------------------------------------- 动态域名 DDNS（#6）
#
# 目标：完整的 DDNS 支持，覆盖 IPv4 / IPv6，国内外主流服务商接入方式，
#       并自动检测「IPv4 是否有公网 / IPv6 是否有公网」，给出 4 种组合的明确提示。
#
# 关键设计：
#   * 公网检测不依赖外部服务做唯一判据，而是「本地地址判定 + 外部回显比对」双重验证：
#       - 本地：/proc/net/route 默认路由出口 + 该口 IPv4 是否为 RFC1918 私网；
#               IPv6 是否为 2000::/3 全球单播（排除 fc00::/7 ULA、fe80::/10 链路本地）
#       - 外部：请求多个 IP 回显服务，取「出口地址」作为真实公网地址
#   * 四种组合：双公网 / 仅 IPv4 公网 / 仅 IPv6 公网 / 双私网（NAT 后），
#     每种都给出「能不能用 DDNS、该怎么用」的中文结论。
#   * 状态「实时」：每次读取都重新探测（结果带短缓存，默认 20 秒，避免频繁打外部接口）。

# ---- 主流 DDNS 服务商目录（API 依据 2026 年现行公开文档整理）
DDNS_PROVIDERS = [
    {'v': 'custom', 'n': '自定义（URL 模板）', 'region': '通用',
     'desc': '填入服务商给出的更新 URL，支持占位符 {ip} {ipv6} {domain} {token} {user} {pass}',
     'fields': ['domain', 'url4', 'url6', 'token', 'user', 'pass'],
     'doc': '按服务商文档填写；适用于任何提供 HTTP 更新的服务商'},
    {'v': 'aliyun', 'n': '阿里云解析 DNS', 'region': '国内',
     'desc': 'AccessKey 签名（HMAC-SHA1）调用 Alidns OpenAPI，支持 IPv4/IPv6 双记录',
     'fields': ['domain', 'subdomain', 'access_key_id', 'access_key_secret', 'ttl'],
     'doc': 'https://help.aliyun.com/zh/dns/api-alidns-2015-01-09-update-domain-record'},
    {'v': 'dnspod', 'n': 'DNSPod（腾讯云）', 'region': '国内',
     'desc': '使用 DNSPod Token（ID,Token）调用 Record.Ddns，国内家用最广泛',
     'fields': ['domain', 'subdomain', 'token', 'ttl'],
     'doc': 'https://docs.dnspod.cn/api/update-dynamic-dns/'},
    {'v': 'dnspod_tencent', 'n': '腾讯云 DNSPod（SecretId/Key）', 'region': '国内',
     'desc': '腾讯云 API 3.0 签名（TC3-HMAC-SHA256），适合已有腾讯云账号',
     'fields': ['domain', 'subdomain', 'secret_id', 'secret_key', 'ttl'],
     'doc': 'https://cloud.tencent.com/document/api/1427/56166'},
    {'v': 'huaweicloud', 'n': '华为云 DNS', 'region': '国内',
     'desc': '华为云 IAM + DNS API（AK/SK 签名），支持双栈记录集',
     'fields': ['domain', 'subdomain', 'access_key', 'secret_key', 'region', 'ttl'],
     'doc': 'https://support.huaweicloud.com/api-dns/dns_api_64001.html'},
    {'v': 'cloudflare', 'n': 'Cloudflare', 'region': '国际',
     'desc': 'API Token + Zone ID，支持 A / AAAA 记录，免费版即可',
     'fields': ['domain', 'subdomain', 'api_token', 'zone_id', 'proxied'],
     'doc': 'https://developers.cloudflare.com/api/operations/dns-records-for-a-zone-update-dns-record'},
    {'v': 'noip', 'n': 'No-IP', 'region': '国际',
     'desc': '经典 HTTP Basic 更新接口 dynupdate.no-ip.com',
     'fields': ['domain', 'subdomain', 'user', 'pass'],
     'doc': 'https://www.noip.com/integrate/request'},
    {'v': 'dyndns', 'n': 'DynDNS / Dyn', 'region': '国际',
     'desc': '标准 dyndns2 协议，兼容大量路由器与第三方服务',
     'fields': ['domain', 'subdomain', 'user', 'pass'],
     'doc': 'https://help.dyn.com/remote-access-api/perform-update/'},
    {'v': 'duckdns', 'n': 'DuckDNS', 'region': '国际',
     'desc': '免费，Token + 域名，更新接口极简',
     'fields': ['domain', 'subdomain', 'token'],
     'doc': 'https://www.duckdns.org/spec.jsp'},
    {'v': 'freedns', 'n': 'FreeDNS (afraid.org)', 'region': '国际',
     'desc': '免费，使用自动生成的更新哈希',
     'fields': ['domain', 'token'],
     'doc': 'https://freedns.afraid.org/scripts/freedns.clients.php'},
    {'v': 'namecheap', 'n': 'Namecheap', 'region': '国际',
     'desc': 'Dynamic DNS Password + 主机名',
     'fields': ['domain', 'subdomain', 'pass'],
     'doc': 'https://www.namecheap.com/support/knowledgebase/article.aspx/29/11/how-to-dynamically-update-the-hostname-with-a-custom-domain-pointing-to-us/'},
    {'v': 'googledomains', 'n': 'Google Domains（已迁移 Squarespace）', 'region': '国际',
     'desc': '旧 Google Domains 动态 DNS 凭据，现由 Squarespace 承接',
     'fields': ['domain', 'subdomain', 'user', 'pass'],
     'doc': 'https://support.squarespace.com/hc/en-us/articles/4404182484877'},
    {'v': 'godaddy', 'n': 'GoDaddy', 'region': '国际',
     'desc': 'GoDaddy API Key + Secret，更新 A / AAAA 记录',
     'fields': ['domain', 'subdomain', 'api_key', 'api_secret', 'ttl'],
     'doc': 'https://developer.godaddy.com/doc/endpoint/domains'},
    {'v': 'dynv6', 'n': 'dynv6', 'region': '国际',
     'desc': '免费，原生支持 IPv6（AAAA）与 IPv4，Token 更新',
     'fields': ['domain', 'token'],
     'doc': 'https://dynv6.com/docs/apis'},
    {'v': 'he', 'n': 'Hurricane Electric (dns.he.net)', 'region': '国际',
     'desc': '免费，使用 DDNS Key，IPv6 友好',
     'fields': ['domain', 'subdomain', 'pass'],
     'doc': 'https://dns.he.net/docs'},
    {'v': 'ovh', 'n': 'OVH', 'region': '国际',
     'desc': 'OVH API（AK/AS/CK 三密钥签名）',
     'fields': ['domain', 'subdomain', 'app_key', 'app_secret', 'consumer_key'],
     'doc': 'https://api.ovh.com/console/'},
    {'v': 'porkbun', 'n': 'Porkbun', 'region': '国际',
     'desc': 'API Key + Secret Key，更新 A / AAAA',
     'fields': ['domain', 'subdomain', 'api_key', 'api_secret'],
     'doc': 'https://porkbun.com/api/json/v3/documentation'},
]

# ---- 公网 IP 回显服务（多路并发/顺序回退，避免单点失效）
IP_ECHO_V4 = [
    ('ipip.net', 'https://myip.ipip.net/s', r'(\d{1,3}(?:\.\d{1,3}){3})'),
    ('3322.org', 'http://ip.3322.net/', r'(\d{1,3}(?:\.\d{1,3}){3})'),
    ('ipsb.io', 'https://api-ipv4.ip.sb/ip', r'(\d{1,3}(?:\.\d{1,3}){3})'),
    ('ipify.org', 'https://api.ipify.org', r'(\d{1,3}(?:\.\d{1,3}){3})'),
    ('cloudflare', 'https://1.1.1.1/cdn-cgi/trace', r'ip=(\d{1,3}(?:\.\d{1,3}){3})'),
]
IP_ECHO_V6 = [
    ('ipsb.io', 'https://api-ipv6.ip.sb/ip', r'([0-9a-fA-F:]{6,})'),
    ('ipv6.icanhazip', 'https://ipv6.icanhazip.com', r'([0-9a-fA-F:]{6,})'),
    ('cloudflare', 'https://[2606:4700:4700::1111]/cdn-cgi/trace', r'ip=([0-9a-fA-F:]+)'),
    ('test-ipv6', 'https://ipv6.test-ipv6.com/ip/', r'([0-9a-fA-F:]{6,})'),
]


def _is_private_v4(ip):
    """判断 IPv4 是否属于私有 / 保留 / 运营商级 NAT 段。"""
    try:
        a, b = [int(x) for x in ip.split('.')[:2]]
    except Exception:
        return True
    if a == 10:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    if a == 100 and 64 <= b <= 127:      # CGNAT 运营商级 NAT（100.64.0.0/10）
        return True
    if a == 169 and b == 254:            # 链路本地
        return True
    if a == 127 or a == 0:
        return True
    if a >= 224:                         # 组播 / 保留
        return True
    return False


def _is_global_v6(ip):
    """判断 IPv6 是否属于全球单播（2000::/3），排除 ULA / 链路本地 / 组播。"""
    s = (ip or '').lower().split('%')[0]
    if not s or ':' not in s:
        return False
    head = s.split(':')[0]
    if head == '':
        # 形如 ::1 / ::ffff:... 需要展开首位
        s2 = s.lstrip(':')
        if not s2:
            return False
        head = s2.split(':')[0]
    try:
        first = int(head or '0', 16)
    except Exception:
        return False
    if first == 0xfe80 or (first & 0xffc0) == 0xfe80:   # fe80::/10 链路本地
        return False
    if (first & 0xfe00) == 0xfc00:                       # fc00::/7 ULA
        return False
    if (first & 0xff00) == 0xff00:                       # ff00::/8 组播
        return False
    return (first & 0xe000) == 0x2000                    # 2000::/3 全球单播


def _http_get_text(url, timeout=6):
    """极简 HTTP(S) GET，返回正文文本（失败返回空串）。"""
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Drouter-DDNS/1.0', 'Accept': '*/*'})
        ctx = None
        if url.startswith('https'):
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read(4096).decode('utf-8', 'ignore')
    except Exception:
        return ''


def _echo_public_ip(family='4'):
    """通过多个回显服务获取本机出口公网地址。返回 (ip, source) 或 ('', '')。"""
    table = IP_ECHO_V4 if family == '4' else IP_ECHO_V6
    for name, url, pat in table:
        txt = _http_get_text(url, timeout=6)
        if not txt:
            continue
        m = re.search(pat, txt)
        if m:
            ip = m.group(1)
            # IPv6 综合校验，避免把服务名之类误判
            if family == '6' and not _is_global_v6(ip):
                continue
            if family == '4' and _is_private_v4(ip):
                continue
            return ip, name
    return '', ''


# 公网能力探测结果短缓存（默认 20 秒），兼顾「实时」与「不打爆外部接口」
_PUBIP_CACHE = {'ts': 0.0, 'data': None}
_PUBIP_LOCK = threading.Lock()


def detect_public_ip(force=False, ttl=20):
    """综合检测 IPv4 / IPv6 公网能力。

    返回：
      v4_local   本机默认路由出口的 IPv4（来源网卡地址）
      v4_public  外部回显得到的真实公网 IPv4（若为 NAT 后则与出口地址不同）
      v4_has     是否有公网 IPv4（出口地址为公网 且 回显成功）
      v4_nat     是否处于 NAT 后（出口是私网但能上网）
      v6_local   本机全局 IPv6 地址
      v6_public  外部回显的 IPv6
      v6_has     是否有公网 IPv6
      combo      四种组合之一：both / v4only / v6only / neither
    """
    import time as _t
    now = _t.time()
    if not force and _PUBIP_CACHE['data'] and (now - _PUBIP_CACHE['ts']) < ttl:
        return _PUBIP_CACHE['data']

    res = {'v4_local': '', 'v4_public': '', 'v4_has': False, 'v4_nat': False,
           'v6_local': '', 'v6_public': '', 'v6_has': False,
           'v4_source': '', 'v6_source': '',
           'combo': 'neither', 'egress': '', 'checked_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

    # 1) 本地默认路由出口
    rc, r4, _e = sh(['ip', '-4', 'route', 'show', 'default'], timeout=6)
    m = re.search(r'default via (\S+) dev (\S+)', r4 or '')
    if m:
        res['egress'] = m.group(2)
        res['gw'] = m.group(1)
    # 2) 出口网卡上的 IPv4（若无默认路由，取第一张 UP 的物理口）
    dev = res['egress']
    rc, raw, _e = sh(['ip', '-4', '-o', 'addr', 'show'], timeout=6)
    cands = []
    for line in (raw or '').splitlines():
        mm = re.match(r'\d+:\s+(\S+)\s+inet\s+(\S+)', line)
        if mm:
            cands.append((mm.group(1), mm.group(2).split('/')[0]))
    if dev:
        for n, ip in cands:
            if n == dev:
                res['v4_local'] = ip
                break
    if not res['v4_local']:
        for n, ip in cands:
            if n not in ('lo',) and not n.startswith(('docker', 'br-', 'veth', 'virbr')):
                res['v4_local'] = ip
                res['egress'] = res['egress'] or n
                break
    # 3) IPv6 全局地址
    rc, raw6, _e = sh(['ip', '-6', '-o', 'addr', 'show', 'scope', 'global'], timeout=6)
    for line in (raw6 or '').splitlines():
        mm = re.match(r'\d+:\s+(\S+)\s+inet6\s+(\S+)', line)
        if mm:
            ip6 = mm.group(2).split('/')[0]
            if _is_global_v6(ip6):
                res['v6_local'] = ip6
                res['v6_iface'] = mm.group(1)
                break
    # 4) 外部回显
    ip4, src4 = _echo_public_ip('4')
    if ip4:
        res['v4_public'] = ip4
        res['v4_source'] = src4
    ip6, src6 = _echo_public_ip('6') if res['v6_local'] else ('', '')
    if ip6:
        res['v6_public'] = ip6
        res['v6_source'] = src6

    # 5) 判定
    local_is_public = bool(res['v4_local']) and not _is_private_v4(res['v4_local'])
    res['v4_has'] = bool(ip4) and (local_is_public or ip4 != res['v4_local'])
    if local_is_public and res['v4_public']:
        res['v4_has'] = True
    res['v4_nat'] = (not local_is_public) and bool(res['v4_local'])
    res['v6_has'] = bool(res['v6_local']) and (bool(ip6) or _is_global_v6(res['v6_local']))

    if res['v4_has'] and res['v6_has']:
        res['combo'] = 'both'
    elif res['v4_has']:
        res['combo'] = 'v4only'
    elif res['v6_has']:
        res['combo'] = 'v6only'
    else:
        res['combo'] = 'neither'

    with _PUBIP_LOCK:
        _PUBIP_CACHE['ts'] = now
        _PUBIP_CACHE['data'] = res
    return res


# 四种组合的中文结论（前端直接展示，避免各页面重复措辞）
PUBIP_COMBO_NOTE = {
    'both': {
        'title': '双栈公网（IPv4 + IPv6 均有公网能力）',
        'level': 'ok',
        'conclusion': '你的宽带同时具备公网 IPv4 与公网 IPv6，DDNS 可同时解析 A（IPv4）与 AAAA（IPv6）记录，'
                      '对外访问兼容性最好。',
        'advice': ['建议同时启用 A 与 AAAA 记录，客户端会优先走 IPv6（延迟更低）',
                   'IPv6 地址通常是动态前缀（DHCPv6-PD），需开启前缀变化检测并重新下发',
                   '公网 IPv4 若为动态，请把 TTL 设为 300 秒以内，加快解析生效'],
    },
    'v4only': {
        'title': '仅 IPv4 有公网能力',
        'level': 'warn',
        'conclusion': '当前只有公网 IPv4，没有可用的公网 IPv6。DDNS 只能用 A 记录对外提供服务。',
        'advice': ['只配置 A 记录即可，不需要 AAAA',
                   '若需要 IPv6 访问，需向运营商申请开启 IPv6 或改用支持 IPv6 的接入方式',
                   '如检测到 IPv4 位于 CGNAT（100.64.0.0/10）之后，公网端口映射可能不生效'],
    },
    'v6only': {
        'title': '仅 IPv6 有公网能力（IPv4 在 NAT 之后）',
        'level': 'warn',
        'conclusion': 'IPv4 处于运营商 NAT / 大内网之后（没有公网 IPv4），但 IPv6 是公网地址。'
                      '这是当前国内三大运营商大面积推进的形态（IPv4 稀缺、IPv6 普惠）。',
        'advice': ['DDNS 请重点配置 AAAA 记录；A 记录即使配置了也无法从公网直连',
                   '对外提供 Web/服务时优先用 IPv6 域名访问（需对端也支持 IPv6）',
                   'IPv6 前缀由运营商动态下发（DHCPv6-PD），务必开启前缀变更后自动更新DDNS',
                   '如需公网 IPv4，可致电运营商申请（多数地区家庭宽带已不再分配）'],
    },
    'neither': {
        'title': '双私网（IPv4 与 IPv6 均无公网能力）',
        'level': 'err',
        'conclusion': '当前 IPv4 与 IPv6 都没有公网能力，本机处于多级 NAT 之后。'
                      '此时 DDNS 无法生效——域名解析到公网地址也访问不到本机。',
        'advice': ['先确认光猫是否已改为桥接（由本机拨号），多数情况下「光猫桥接 + 本机 PPPoE」可获得公网 IPv4',
                   '检查是否被运营商做了 CGNAT（100.64.0.0/10 地址段即为大内网）',
                   '若确需外部访问，可考虑内网穿透方案（FRP / Cloudflare Tunnel / Tailscale）',
                   'IPv6 若完全未获取，请到「IPv6 / RA」页面检查是否已开启'],
    },
}


# ==================================================================
# 真·公网 IP 判定（#2）
#
# 起因：用户的出口地址 101.70.131.227 是联通的公网段，外部回显也一致，
# 按「地址是不是公网段」判会得出「有公网 IP」，但从另一台 VPS 却 ping 不通。
# 原因：有没有公网地址 ≠ 外网能不能主动连进来。很多宽带（尤其城域网 /
# 企业专线 / 部分家宽）给的是「公网地址 + 入向封锁」，还有的是 CGNAT。
#
# 所以这里把判定拆成四条各自独立的证据，最后给出分级结论：
#   ① 地址段   RFC1918 / CGNAT(100.64/10) / 保留段 → 硬否定
#   ② 多源一致性 多个彼此独立的回显服务是否返回同一个地址
#   ③ 首跳链路 traceroute 第一跳是不是私网 → 多级 NAT 的证据
#   ④ 入向实测 让外部主机真的连一次进来（唯一能拍板的证据）
# ==================================================================

PUBIP_VERDICT = {
    'real': {
        'title': '真·公网 IP（外网可主动连入）',
        'level': 'ok',
        'conclusion': '出口地址是公网地址，且已实测到来自外网的入向报文。'
                      '这个地址可以被互联网上的其它主机主动访问，'
                      'DDNS 解析到它之后端口映射能真正生效。',
        'advice': ['DDNS 与端口转发可以正常使用',
                   '注意暴露端口等于暴露服务，请配合防火墙只放行必要端口',
                   '地址可能仍是动态的，建议 DDNS 的 TTL 设为 300 秒以内'],
    },
    'likely': {
        'title': '疑似公网（地址是公网段，入向未验证）',
        'level': 'warn',
        'conclusion': '出口地址属于公网段、多个外部服务回显也一致，但还没有实测过'
                      '「外网能不能主动连进来」。这一步不能省 —— '
                      '不少宽带给的是公网地址却在入向做了封锁，'
                      '这种情况 DDNS 能更新成功、域名也解析得到，但别人就是连不上。',
        'advice': ['点下方「开始入向实测」，按提示从一台外网主机 ping / 访问一次',
                   '没有外网主机时，可用手机蜂窝网络（关掉 Wi-Fi）访问一次',
                   '实测前不要急着配置端口转发，先确认入向是通的'],
    },
    'blocked': {
        'title': '有公网地址，但入向被拦（外网连不进来）',
        'level': 'err',
        'conclusion': '地址确实是公网段的，外部回显也一致，但实测外网的入向报文'
                      '根本到不了本机 —— 运营商或上级网关在入向做了封锁。'
                      '这正是「看着有公网 IP、从别的 VPS 却 ping 不通」的情况。',
        'advice': ['DDNS 可以更新成功，但解析出去别人仍然连不上，'
                   '配了端口转发也不会生效',
                   '需要外网访问时改用内网穿透（EasyTier / Tailscale / FRP 一类）',
                   '或联系运营商确认是否可开通入向（部分专线需报备）',
                   '若只需要在家访问，走 IPv6 往往可行 —— IPv6 通常没有入向封锁'],
    },
    'cgnat': {
        'title': '运营商级 NAT（CGNAT）之后',
        'level': 'err',
        'conclusion': '出口地址落在运营商级 NAT 保留段（100.64.0.0/10）或经过多级私网 NAT，'
                      '这个地址在互联网上不可路由，外网无法主动连入。',
        'advice': ['无论 DDNS 怎么配都无法从外网直连，请改用内网穿透',
                   '可致电运营商咨询是否能分配公网 IPv4（多数家宽已不再提供）',
                   '优先使用 IPv6：CGNAT 只影响 IPv4，IPv6 通常仍是真公网'],
    },
    'private': {
        'title': '私有地址（本机未直接出网）',
        'level': 'err',
        'conclusion': '默认出口上的地址是 RFC1918 私有地址，且外部回显没能拿到地址，'
                      '说明本机目前没有互联网出口或出口不在默认路由上。',
        'advice': ['确认 WAN 口已接好并获取到地址',
                   '若上级还有一层路由，请在那台设备上做端口映射或改成桥接'],
    },
    'unknown': {
        'title': '无法判定（外部探测全部失败）',
        'level': 'warn',
        'conclusion': '所有外部回显服务都没响应，可能是网络暂时不通、'
                      'DNS 未就绪或这些域名被拦截。此时不给出结论，'
                      '避免用一个不可靠的结果误导你。',
        'advice': ['先确认本机能否正常上网',
                   '可在「诊断工具」里测试 DNS 与外网连通性后重试'],
    },
}

# 入向实测：抓 ICMP echo-request（即别人 ping 本机的包）。
# 选它是因为它复现的就是「从另一台 VPS ping 一下」这个最直觉的验证动作，
# 而且不需要在本机开任何监听端口（零网络改动，符合红线）。
_PROBE_DIR = '/run/drouter'
_PROBE_STATE = os.path.join(_PROBE_DIR, 'pubip-probe.json')

_PROBE_TIMEOUT = 180          # 最多等 3 分钟，够用户切到 VPS 上敲一条命令
_PROBE_PORTS = (41000, 41001, 41002, 41003, 41004, 41005)  # 备选 TCP 端口（tcpdump 不可用时的退路）


def _probe_running():
    st = _probe_state_load()
    if not st or st.get('state') != 'running':
        return None
    # 超时自动作废
    if time.time() - float(st.get('started_at') or 0) > _PROBE_TIMEOUT:
        return None
    return st


def _probe_state_load():
    try:
        with open(_PROBE_STATE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _probe_state_save(st):
    try:
        os.makedirs(_PROBE_DIR, exist_ok=True)
        tmp = _PROBE_STATE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, _PROBE_STATE)
        os.chmod(_PROBE_STATE, 0o644)
    except Exception:
        pass


def _local_subnets():
    """本机直连网段列表 —— 用来把「局域网内 ping 本机」排除掉。"""
    nets = []
    rc, out, _e = sh(['ip', '-4', '-o', 'route', 'show', 'scope', 'link'], timeout=6)
    for line in (out or '').splitlines():
        m = re.search(r'(\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})', line)
        if m:
            nets.append(m.group(1))
    return nets


def _probe_inbound_tcp(ip, token):
    """tcpdump 不可用时的退路：起一个一次性 TCP 监听，等外部连一次。

    只用预先列好的高位端口，绝不动 5900 / 8443 等已知占用端口。
    """
    script = (
        "import socket,sys,json,time\n"
        "port=None;srv=None\n"
        "for p in %r:\n"
        "    try:\n"
        "        s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
        "        s.bind(('0.0.0.0',p));s.listen(5);s.settimeout(%d)\n"
        "        srv=s;port=p;break\n"
        "    except Exception:\n"
        "        try: s.close()\n"
        "        except Exception: pass\n"
        "if srv is None:\n"
        "    sys.exit(3)\n"
        "open(%r,'w').write(json.dumps({'port':port}))\n"
        "end=time.time()+%d\n"
        "hit=[]\n"
        "while time.time()<end:\n"
        "    try:\n"
        "        c,a=srv.accept()\n"
        "    except socket.timeout:\n"
        "        break\n"
        "    except Exception:\n"
        "        break\n"
        "    hit.append(a[0])\n"
        "    try:\n"
        "        c.recv(4096)\n"
        "        c.sendall(b'HTTP/1.0 200 OK\\r\\nContent-Length: 2\\r\\n\\r\\nok')\n"
        "    except Exception:\n"
        "        pass\n"
        "    try: c.close()\n"
        "    except Exception: pass\n"
        "    if len(hit)>=1: break\n"
        "try: srv.close()\n"
        "except Exception: pass\n"
        "open(%r,'w').write(json.dumps({'hits':hit}))\n"
    ) % (_PROBE_PORTS, _PROBE_TIMEOUT,
         _PROBE_STATE + '.port', _PROBE_TIMEOUT, _PROBE_STATE + '.hits')
    try:
        subprocess.Popen(['timeout', str(_PROBE_TIMEOUT + 10), 'python3', '-c', script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
        return True
    except Exception:
        return False


def _probe_read_hits():
    """读取入向实测结果（ICMP 从 pcap 文本里数，TCP 从 hits 文件里读）。"""
    hits = []
    # TCP 退路的命中文件
    try:
        with open(_PROBE_STATE + '.hits', encoding='utf-8') as f:
            d = json.load(f)
        hits += [x for x in (d.get('hits') or []) if x]
    except Exception:
        pass
    # ICMP：用 tcpdump 文本模式重读 pcap 太麻烦，改成抓的时候直接写文本日志
    try:
        with open(_PROBE_STATE + '.icmp', encoding='utf-8') as f:
            for line in f:
                m = re.search(r'(\d{1,3}(?:\.\d{1,3}){3})\s*>\s*', line)
                if m:
                    hits.append(m.group(1))
    except Exception:
        pass
    # 去重保持顺序
    seen = set()
    out = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def _traceroute_first_hops(target='223.5.5.5', max_hops=4):
    """取到公网的头几跳，用来看本机是不是又套了一层私网 NAT。"""
    hops = []
    rc, out, _e = sh(['traceroute', '-n', '-w', '2', '-q', '1', '-m', str(max_hops), target],
                     timeout=25)
    if rc != 0 or not out:
        # 没装 traceroute 就用 mtr 的单次模式，再不行就放弃（这条只是旁证）
        rc, out, _e = sh(['mtr', '-n', '-r', '-c', '1', '-m', str(max_hops), target],
                         timeout=25)
    for line in (out or '').splitlines()[1:]:
        m = re.search(r'(\d{1,3}(?:\.\d{1,3}){3})', line)
        if m:
            hops.append(m.group(1))
    return hops[:max_hops]


def _addr_class_of(ip):
    """给地址分类：public / cgnat / private / reserved / empty。"""
    if not ip:
        return 'empty'
    if _is_private_v4(ip):
        try:
            a, b = [int(x) for x in ip.split('.')[:2]]
        except Exception:
            return 'private'
        if a == 100 and 64 <= b <= 127:
            return 'cgnat'
        if a == 169 and b == 254:
            return 'reserved'
        if a >= 224:
            return 'reserved'
        return 'private'
    return 'public'


def act_pubip(p):
    """真·公网 IP 判定：check / probe_start / probe_status / probe_stop。"""
    p = p or {}
    op = (p.get('op') or 'check').strip()

    if op == 'probe_start':
        force = bool(p.get('force'))
        st = _probe_running()
        if st and not force:
            return ok({'state': 'running', 'probe': st,
                       'msg_cn': '入向实测已在等待中，按下面的命令操作即可'})
        info = detect_public_ip(force=True)
        ip = info.get('v4_public') or info.get('v4_local') or ''
        if not ip:
            return fail('拿不到出口 IPv4 地址，无法做入向实测')
        iface = info.get('egress') or ''
        # 关键：每次开始都清掉上一次的命中记录。早先只覆盖写 .icmp、从不删 .hits，
        # 于是「第一次命中」之后，「外网可以主动连入」这个结论会永久卡在通过状态 ——
        # 用户据此配端口转发，实际根本不通。
        for suffix in ('.hits', '.icmp', '.port'):
            try:
                os.unlink(_PROBE_STATE + suffix)
            except OSError:
                pass

        token = uuid.uuid4().hex[:12]
        method = ''
        okstart = False
        # 首选 tcpdump 抓 ICMP：零监听、零端口、零网络改动
        if _dep_installed('cmd', 'tcpdump')[0]:
            # -w 写 pcap 前端读不了，改成同时写一份文本，便于直接数命中
            try:
                os.makedirs(_PROBE_DIR, exist_ok=True)
            except Exception:
                pass
            filt = ('icmp and icmp[icmptype] = 8 and dst host %s' % ip)
            for net in _local_subnets():
                filt += ' and not src net %s' % net
            cmd = ['timeout', str(_PROBE_TIMEOUT), 'sh', '-c',
                   'tcpdump -l -n -i %s -c 20 %s > %s 2>/dev/null'
                   % (iface or 'any', filt, _PROBE_STATE + '.icmp')]
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
                okstart, method = True, 'icmp'
            except Exception:
                okstart = False
        if not okstart:
            if _probe_inbound_tcp(ip, token):
                method = 'tcp'
                okstart = True
        if not okstart:
            return fail('入向实测启动失败：既没有可用的 tcpdump，也开不了临时监听端口')

        st = {'state': 'running', 'ip': ip, 'iface': iface, 'token': token,
              'method': method, 'started_at': time.time(),
              'timeout': _PROBE_TIMEOUT}
        _probe_state_save(st)
        port = None
        for _i in range(20):
            try:
                with open(_PROBE_STATE + '.port', encoding='utf-8') as f:
                    port = json.load(f).get('port')
                break
            except Exception:
                if method != 'tcp':
                    break
                time.sleep(0.3)
        if method == 'icmp':
            cmdline = 'ping -c 3 %s' % ip
            how = ('在另一台能上外网的主机（你的 VPS、云主机，或手机开热点连的电脑）上执行：')
        else:
            cmdline = ('curl -s -m 8 http://%s:%s/%s' % (ip, port or _PROBE_PORTS[0], token))
            how = '在另一台能上外网的主机上执行（tcpdump 不可用，改用临时端口回连）：'
        return ok({'state': 'running', 'probe': st, 'cmd': cmdline, 'how': how,
                   'timeout': _PROBE_TIMEOUT,
                   'msg_cn': '入向实测已启动，请在 %d 秒内从外网执行下面的命令'
                             % _PROBE_TIMEOUT})

    if op == 'probe_status':
        st = _probe_state_load()
        if not st:
            return ok({'state': 'idle', 'hits': [], 'msg_cn': '当前没有进行中的入向实测'})
        hits = _probe_read_hits()
        if hits:
            st['state'] = 'hit'
            st['hits'] = hits
            st['finished_at'] = time.time()
            _probe_state_save(st)
            return ok({'state': 'hit', 'hits': hits, 'probe': st,
                       'msg_cn': '已收到来自 %s 的入向报文 —— 外网可以主动访问本机'
                                 % '、'.join(hits[:3])})
        if time.time() - float(st.get('started_at') or 0) > _PROBE_TIMEOUT:
            st['state'] = 'timeout'
            _probe_state_save(st)
            return ok({'state': 'timeout', 'hits': [], 'probe': st,
                       'msg_cn': '等待超时，没有收到任何来自外网的入向报文'})
        left = int(_PROBE_TIMEOUT - (time.time() - float(st.get('started_at') or 0)))
        return ok({'state': 'running', 'hits': [], 'probe': st, 'left': left,
                   'msg_cn': '仍在等待外网报文（还剩 %d 秒）' % left})

    if op == 'probe_stop':
        try:
            sh(['pkill', '-f', 'pubip-probe'], timeout=8)
        except Exception:
            pass
        try:
            sh(['pkill', '-f', 'tcpdump.*icmp\\[icmptype\\]'], timeout=8)
        except Exception:
            pass
        st = _probe_state_load() or {}
        st['state'] = 'stopped'
        _probe_state_save(st)
        return ok({'state': 'stopped', 'msg_cn': '入向实测已停止'})

    # ================= op == 'check' =================
    force = bool(p.get('force'))
    info = detect_public_ip(force=force)
    v4 = info.get('v4_public') or info.get('v4_local') or ''

    evidence = []

    # ① 地址段
    cls = _addr_class_of(v4)
    evidence.append({
        'key': 'class', 'name': '出口地址段',
        'value': v4 or '（未获取）',
        'pass': cls == 'public',
        'detail': {'public': '属于公网地址段，在互联网上可路由',
                   'cgnat': '属于运营商级 NAT 保留段 100.64.0.0/10',
                   'private': '属于 RFC1918 私有地址段',
                   'reserved': '属于保留 / 链路本地地址段',
                   'empty': '没有取到 IPv4 地址'}.get(cls, ''),
    })

    # ② 多源一致性
    seen = {}
    for name, url, pat in IP_ECHO_V4:
        txt = _http_get_text(url, timeout=6)
        m = re.search(pat, txt or '') if txt else None
        if m and not _is_private_v4(m.group(1)):
            seen.setdefault(m.group(1), []).append(name)
    addrs = sorted(seen.keys(), key=lambda k: -len(seen[k]))
    consistent = len(addrs) == 1 and len(seen[addrs[0]]) >= 2 if addrs else False
    evidence.append({
        'key': 'echo', 'name': '多源回显一致性',
        'value': ('、'.join(addrs) if addrs else '（全部失败）'),
        'pass': bool(addrs),
        'detail': ('%d 个独立服务都返回同一个地址，出口地址可信'
                   % len(seen[addrs[0]])) if consistent
                  else ('不同服务返回了不同地址：%s —— 可能有多出口或负载均衡'
                        % '；'.join('%s(%s)' % (a, '+'.join(seen[a])) for a in addrs))
                  if addrs else '所有外部回显服务都取不到地址，无法交叉验证',
        'sources': {a: seen[a] for a in addrs},
    })

    # ③ 首跳链路
    hops = _traceroute_first_hops()
    nat_hops = [h for h in hops if _is_private_v4(h)]
    evidence.append({
        'key': 'path', 'name': '首跳链路',
        'value': ' → '.join(hops) if hops else '（未取到）',
        'pass': not nat_hops,
        'detail': ('出网第一跳是私网地址（%s），说明本机上面至少还有一层 NAT'
                   % nat_hops[0]) if nat_hops
                  else ('出网第一跳就是公网地址，本机直接对接运营商'
                        if hops else 'traceroute 不可用，跳过这条证据'),
        'hops': hops,
    })

    # ④ 入向实测
    st = _probe_state_load() or {}
    pstate = st.get('state') or 'idle'
    hits = _probe_read_hits()
    if pstate == 'hit' or hits:
        inbound = True
        detail = '实测收到来自 %s 的入向报文' % '、'.join(hits[:3])
    elif pstate == 'timeout':
        inbound = False
        detail = ('已做过入向实测：等待 %d 秒没有收到任何来自外网的报文' % _PROBE_TIMEOUT)
    else:
        inbound = None
        detail = '尚未做入向实测 —— 这是唯一能拍板「外网能不能主动连进来」的证据'
    evidence.append({
        'key': 'inbound', 'name': '入向可达性实测',
        'value': {True: '可达', False: '不可达', None: '未测试'}[inbound],
        'pass': inbound is True,
        'detail': detail, 'probed': inbound is not None,
    })

    # ---- 综合结论 ----
    if cls == 'cgnat':
        verdict = 'cgnat'
    elif cls in ('private', 'reserved', 'empty'):
        # 私网地址 + 能上网 = 上面有 NAT；这里不再细分为 CGNAT，避免臆断
        verdict = 'private' if not addrs else 'cgnat'
    elif not addrs and inbound is None:
        verdict = 'unknown'
    elif inbound is True:
        verdict = 'real'
    elif inbound is False:
        verdict = 'blocked'
    else:
        verdict = 'likely'

    v = dict(PUBIP_VERDICT.get(verdict, PUBIP_VERDICT['unknown']))
    v['key'] = verdict
    # 首跳出现私网地址是个很实在的疑点：本机上面至少还有一层私网转发。
    # 光靠「回显拿到公网地址」不足以否定它 —— 有些运营商内部路由就用私网地址，
    # 入向照样是通的。所以这里只加一句提示，不下死结论。
    if nat_hops and verdict in ('likely', 'real'):
        v['conclusion'] += ('（注意：出网路径上出现了私网地址 %s，'
                            '说明本机上面至少还有一层私网转发；'
                            '是否影响入向，以实测结果为准。）' % nat_hops[0])
        v['advice'] = list(v['advice']) + [
            '出网路径上有私网跳（%s），建议务必做一次入向实测再决定要不要配端口转发'
            % nat_hops[0]]
    return ok({
        'ip': v4, 'v6': info.get('v6_public') or info.get('v6_local') or '',
        'egress': info.get('egress') or '', 'gw': info.get('gw') or '',
        'v4_local': info.get('v4_local') or '',
        'class': cls, 'verdict': v, 'evidence': evidence,
        'probe_state': pstate,
        'combo': info.get('combo') or 'neither',
        'checked_at': info.get('checked_at') or '',
        'msg_cn': '判定完成：' + v['title'],
    })


def read_ddns(p):
    """读取 DDNS 配置 + 公网检测 + 实时状态（#6）。"""
    p = p or {}
    cfg = _load_setting('ddns', {}) or {}
    force = bool(p.get('force'))
    pub = detect_public_ip(force=force)

    # 合并配置
    out = {
        'enabled': bool(cfg.get('enabled')),
        'provider': cfg.get('provider') or 'custom',
        'domain': cfg.get('domain') or '',
        'subdomain': cfg.get('subdomain') or '',
        'ipv4': cfg.get('ipv4', True),
        'ipv6': cfg.get('ipv6', False),
        'ttl': cfg.get('ttl') or 300,
        'interval': cfg.get('interval') or 300,
        'url4': cfg.get('url4') or '',
        'url6': cfg.get('url6') or '',
        'fields': {k: v for k, v in (cfg.get('fields') or {}).items()},
        'last_update': cfg.get('last_update') or '',
        'last_ip4': cfg.get('last_ip4') or '',
        'last_ip6': cfg.get('last_ip6') or '',
        'last_result': cfg.get('last_result') or '',
        'last_msg_cn': cfg.get('last_msg_cn') or '',
    }
    prov = next((x for x in DDNS_PROVIDERS if x['v'] == out['provider']), DDNS_PROVIDERS[0])
    out['provider_cn'] = prov['n']
    out['provider_region'] = prov['region']
    out['provider_desc'] = prov['desc']
    out['provider_fields'] = prov['fields']
    out['provider_doc'] = prov['doc']

    # 公网能力结论
    note = PUBIP_COMBO_NOTE.get(pub['combo'], PUBIP_COMBO_NOTE['neither'])
    # 兼容性提示：配置与公网能力是否匹配
    warn = ''
    if out['enabled']:
        if out['ipv4'] and not pub['v4_has']:
            warn = ('已启用 IPv4 DDNS，但检测到 IPv4 没有公网地址（可能处于 NAT/CGNAT 之后），'
                    'A 记录即使更新成功，外部也无法访问。' +
                    ('建议改用 IPv6（AAAA）记录。' if pub['v6_has'] else '建议先解决公网 IPv4 问题。'))
        elif out['ipv6'] and not pub['v6_has']:
            warn = ('已启用 IPv6 DDNS，但未检测到公网 IPv6 地址，AAAA 记录无法生效。'
                    '请到「IPv6 / RA」页面确认已开启并获取到 IPv6 地址。')
    elif pub['combo'] == 'v6only':
        warn = '检测到你的 IPv4 在 NAT 之后、IPv6 是公网地址。建议直接启用 IPv6（AAAA）方式的 DDNS。'

    return ok({'cfg': out, 'public': pub, 'note': note, 'warn': warn,
               'providers': DDNS_PROVIDERS,
               'catalog_note': '服务商接口依据 2026 年现行公开文档整理，接入方式如有调整请以官方文档为准。'})


def act_ddns(p):
    """DDNS 操作：save / on / off / test（公网检测） / update（立即更新一条记录）。"""
    p = p or {}
    op = (p.get('op') or 'get').strip()
    cfg = _load_setting('ddns', {}) or {}

    if op == 'get':
        return read_ddns(p)

    if op == 'test':
        pub = detect_public_ip(force=True)
        note = PUBIP_COMBO_NOTE.get(pub['combo'], PUBIP_COMBO_NOTE['neither'])
        return ok({'public': pub, 'note': note,
                   'msg_cn': '公网能力检测完成：' + note['title']})

    if op == 'save':
        allow = {'enabled', 'provider', 'domain', 'subdomain', 'ipv4', 'ipv6', 'ttl',
                 'interval', 'url4', 'url6', 'fields'}
        for k in allow:
            if k in p:
                cfg[k] = p[k]
        # 校验
        prov = cfg.get('provider') or 'custom'
        if prov not in [x['v'] for x in DDNS_PROVIDERS]:
            return fail('BAD_PROVIDER', '未知的 DDNS 服务商：%s' % prov)
        dom = (cfg.get('domain') or '').strip()
        if cfg.get('enabled') and not dom:
            return fail('NO_DOMAIN', '启用 DDNS 前必须填写主域名（例如 example.com）')
        cfg['ttl'] = max(60, min(int(cfg.get('ttl') or 300), 86400))
        cfg['interval'] = max(60, min(int(cfg.get('interval') or 300), 86400))
        cfg['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        _save_setting('ddns', cfg)
        return ok({'cfg': cfg}, 'DDNS 配置已保存' + ('（已启用）' if cfg.get('enabled') else '（未启用）'))

    if op in ('on', 'off'):
        cfg['enabled'] = (op == 'on')
        if cfg['enabled'] and not (cfg.get('domain') or '').strip():
            return fail('NO_DOMAIN', '启用 DDNS 前必须先在页面上填写主域名')
        _save_setting('ddns', cfg)
        return ok({'enabled': cfg['enabled']},
                  'DDNS 已%s' % ('启用' if cfg['enabled'] else '停用'))

    if op == 'update':
        pub = detect_public_ip(force=True)
        prov = cfg.get('provider') or 'custom'
        targets = []
        if cfg.get('ipv4', True) and pub['v4_public']:
            targets.append(('A', pub['v4_public']))
        if cfg.get('ipv6') and pub['v6_public']:
            targets.append(('AAAA', pub['v6_public']))
        if not targets:
            return fail('NO_PUBLIC_IP',
                       '没有可用于更新的公网地址（IPv4/公网能力=%s，IPv6=%s）。请先运行「检测公网能力」。'
                       % ('有' if pub['v4_has'] else '无', '有' if pub['v6_has'] else '无'))
        # 生成更新计划（真实下发由各服务商适配器完成；此处记录并返回计划，供上层脚本执行）
        plan = []
        for rtype, ip in targets:
            plan.append({'type': rtype, 'ip': ip,
                         'record': _ddns_record_name(cfg),
                         'provider': prov})
        cfg['last_update'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for rtype, ip in targets:
            if rtype == 'A':
                cfg['last_ip4'] = ip
            else:
                cfg['last_ip6'] = ip
        cfg['last_result'] = 'planned'
        cfg['last_msg_cn'] = '已生成 %d 条记录更新计划' % len(plan)
        _save_setting('ddns', cfg)
        return ok({'plan': plan, 'public': pub},
                  '已生成更新计划：%s（真实下发由 ddns 服务执行）'
                  % '、'.join('%s→%s' % (x['type'], x['ip']) for x in plan))

    return fail('BAD_OP', '不支持的操作：%s' % op)


def _ddns_record_name(cfg):
    sub = (cfg.get('subdomain') or '').strip().strip('.')
    dom = (cfg.get('domain') or '').strip()
    if not sub or sub == '@':
        return dom
    return sub + '.' + dom


# ================================================ WireGuard VPN（1.0.7）
#
# ── 为什么只做 WireGuard，不做 OpenVPN ──────────────────────────────────────
# Debian 13 的内核**自带** WireGuard（5.6 之后并入上游内核），不需要装任何
# 第三方软件，也不���要 DKMS 编译模块。这台是 4GB/2CPU 的软路由，让用户为了
# 远程回家访问 NAS 去编译内核模块是不能接受的。wg-quick 是 systemd 单元，
# 配置就是 INI 格式，几十行就够。
#
# ── 安全设计 ────────────────────────────────────────────────────────────────
# * 私钥只存在于服务端配置文件（0600），**永不通过 Web 下发**；
#   客户端拿到的只有自己的那一份（每台设备独立密钥对）。
# * AllowedIPs 默认只放行内网段（10.0.0.0/8 之类由用户选），
#   想要「全部流量都走回家」时才勾 ExitNode —— 那个选项会让家里所有流量
#   都从家里出去，必须显式二次确认。
# * 监听端口只开放在 WAN 侧；内网侧不开放（内网的人不需要连 VPN 回来）。
# * 容器形态没有 systemd：wg-quick 用不了，回退到 `wg-quick` 二进制直接
#   up/down，或退到 `ip link add` 手工建接口。这一条必须做，否则
#   「装了 deb 一切正常、跑镜像 VPN 起不来」。

WG_CONF_DIR = '/etc/wireguard'
WG_AUTOLOAD = '/etc/modules-load.d/drouter-wireguard.conf'
VPN_CONF = '/etc/drouter/generated/vpn.json'
VPN_UID = 51820          # WireGuard 默认用户
VPN_IFACE = 'wg0'
# 单个客户端的地址池，从 .1 开始（.1 留给服务器自己）
VPN_POOL_DEFAULT = '10.66.66.0/24'


def _vpn_load():
    try:
        with open(VPN_CONF, encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {}


def _vpn_save(d):
    os.makedirs(os.path.dirname(VPN_CONF), exist_ok=True)
    with open(VPN_CONF, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.chmod(VPN_CONF, 0o600)      # 含客户端私钥派生材料


def _vpn_norm(d):
    """归一化 VPN 配置。"""
    d = d if isinstance(d, dict) else {}
    out = {
        'enabled': d.get('enabled') is True,
        'port': 0,
        'listen': '',
        'pool': VPN_POOL_DEFAULT,
        'endpoint_host': '',
        'keepalive': 25,
        'exit_node': False,
        'lan_allow': True,
        'dns': '',
        'peers': [],
    }
    try:
        out['port'] = int(d.get('port') or 0)
    except Exception:
        out['port'] = 0
    if not (1 <= out['port'] <= 65535):
        # 0 表示自动挑一个空闲端口。写在默认里而不是报错 ——
        # 用户第一次进来还没决定用哪个端口是很正常的。
        out['port'] = 0
    out['listen'] = str(d.get('listen') or '')
    try:
        out['keepalive'] = max(0, min(int(d.get('keepalive') or 25), 300))
    except Exception:
        out['keepalive'] = 25
    try:
        net = ipaddress.ip_network(str(d.get('pool') or VPN_POOL_DEFAULT),
                                   strict=False)
        # 必须是私有段，且不能和内网段撞（撞了会导致路由黑洞）
        if (net.version != 4 or not net.is_private
                or net.prefixlen < 16 or net.prefixlen > 30):
            net = ipaddress.ip_network(VPN_POOL_DEFAULT, strict=False)
    except Exception:
        net = ipaddress.ip_network(VPN_POOL_DEFAULT, strict=False)
    out['pool'] = str(net)
    out['endpoint_host'] = str(d.get('endpoint_host') or '')[:200]
    out['dns'] = str(d.get('dns') or '')[:100]
    out['exit_node'] = d.get('exit_node') is True
    out['lan_allow'] = d.get('lan_allow') is not False
    peers = []
    seen = set()
    for p in (d.get('peers') or []):
        if not isinstance(p, dict):
            continue
        pid = re.sub(r'[^a-z0-9_-]', '', str(p.get('id') or '').lower())[:32]
        if not pid or pid in seen:
            continue
        seen.add(pid)
        ip = str(p.get('ip') or '')
        # ⚠️ 归属判断必须拿 **ipaddress 对象**做，不能拿字符串。
        # `'10.66.66.2' in ip_network(...)` 在 Python 3.12 及更早
        # 会静默返回 False（保留全部 peer），3.13 起直接抛
        # AttributeError —— 而这行下面就跟着 except: continue，
        # 于是**所有客户端被无声无息地清空**：界面看着一切正常，
        # wg0.conf 里 [Peer] 全没了，配好的手机立刻连不上，
        # 而且没有任何报错。先用对象判断，再转成字符串存。
        aobj = None
        try:
            aobj = ipaddress.ip_address(ip)
        except Exception:
            continue
        if aobj not in net:
            continue
        a = str(aobj)
        row = {
            'id': pid,
            'name': str(p.get('name') or pid)[:60],
            'ip': a,
            'note': str(p.get('note') or '')[:200],
            'enabled': p.get('enabled') is not False,
            'created': str(p.get('created') or ''),
            'public_key': str(p.get('public_key') or '')[:80],
            # ⚠️ private_key **必须**在这里保留。早先的版本没保留，
            # 结果是每次保存配置（save / peer_toggle / peer_del）都会把
            # 所有客户端私钥清空 —— 界面看着一切正常，wg0.conf 里的
            # [Peer] 全变成空公钥，所有已配好的手机在连上后立刻握手失败，
            # 而且因为「保存成功」没有任何报错，极难定位。
            # 私钥只在 _vpn_mask()（对外输出）时才抹掉，不是在落盘时。
            'private_key': str(p.get('private_key') or '')[:80],
        }
        row['has_key'] = bool(row['private_key'])
        peers.append(row)
    out['peers'] = peers
    return out


def _vpn_hostkey():
    """服务端私钥。只在服务端配置文件里，绝不下发。"""
    path = os.path.join(WG_CONF_DIR, VPN_IFACE + '.key')
    try:
        with open(path, encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return ''


def _vpn_pubkey():
    pk = _vpn_hostkey()
    if not pk:
        return ''
    rc, o, e = sh(['wg', 'pubkey'], timeout=8, input_data=pk + '\n')
    return o.strip() if rc == 0 else ''


def _wg_genkey():
    rc, o, e = sh(['wg', 'genkey'], timeout=10)
    if rc != 0 or not o.strip():
        return ''
    return o.strip()


def _vpn_installed():
    """兼容壳：只取四态里的 state。判定逻辑全在 _vpn_env()。"""
    return _vpn_env()['state']


def _vpn_env():
    """WireGuard 可用性诊断（分四态）。

    ⚠️ 这段判据改过一轮才对。早先只有两个二值检查：
        /sys/module/wireguard 在不在  → 模块**已加载**吗
        /usr/bin/wg 在不在             → 工具装了吗
    两个都没有就报「内核不支持 WireGuard」，前端照着写死一句
    「通常说明跑在精简容器里」。2026-10-03 被用户当场问住：
    PVE 里的 KVM 虚拟机（systemd-detect-virt 明确返回 kvm，
    是虚拟机、不是容器）被说成精简容器。而那台机的内核
    6.12.107+deb13-amd64 里 wireguard.ko.xz 好好地躺在
    /lib/modules/ 下，只是**没被加载**（/etc/modules 与
    /etc/modules-load.d/ 里都没写 wireguard，也没有别的触发点），
    `modprobe wireguard` 直接 RC=0 成功。

    根因：**从没查过 /lib/modules/ 下的模块文件**，于是
    「模块在但没加载」和「内核裁掉了模块」被压进同一个 no，
    再配一句写死的「精简容器」，把排查方向整个带偏 ——
    用户会去查容器镜像，而真正该做的是 modprobe 一下。

    四态（前端文案与一键修复按钮都按这四态分支）：
        ready        模块已加载 + 工具已装 → 可以直接用
        need_module  模块文件在，只是没加载 → modprobe 即可（本地秒级）
        need_tool    模块已就绪，只缺 wireguard-tools → 需要下载安装
        unsupported  内核里确实没有 wireguard 模块（真精简镜像 / 自编内核）
    """
    kver = os.uname().release
    mod_loaded = os.path.isdir('/sys/module/wireguard')
    tool = bool(shutil.which('wg'))
    # 查模块**文件**在不在 —— 这一级以前完全没有，是本轮误报的根因。
    # 不能只认 .ko：Debian 默认压成 .ko.xz，个别发行版用 .ko.zst。
    mod_file = ''
    for _n in ('wireguard.ko', 'wireguard.ko.xz', 'wireguard.ko.zst'):
        _p = os.path.join('/lib/modules', kver,
                          'kernel/drivers/net/wireguard', _n)
        if os.path.isfile(_p):
            mod_file = _p
            break
    if mod_loaded and tool:
        state = 'ready'
    elif mod_loaded:
        state = 'need_tool'
    elif mod_file:
        state = 'need_module'
    else:
        state = 'unsupported'
    # 虚拟化形态只用于文案说明（kvm 是虚拟机，不是容器 —— 别再写反了）
    virt = ''
    if shutil.which('systemd-detect-virt'):
        rc, o, _e = sh(['systemd-detect-virt'], timeout=6)
        if rc == 0:
            virt = (o or '').strip()
    return {
        'state': state,
        'mod_loaded': mod_loaded,
        'mod_file': mod_file,
        'tool': tool,
        'kernel': kver,
        'virt': virt,
        'autoload': os.path.isfile(WG_AUTOLOAD),
        'fixable': state in ('need_module', 'need_tool'),
    }


def _vpn_write_autoload():
    """把 wireguard 写进 /etc/modules-load.d/，重启后自动加载。

    为什么要自己写：Debian 的 wireguard 走的是 **udev 按需加载**
    （有人 `ip link add type wireguard` 时才自动 modprobe），
    /etc/modules 默认不写它。于是每次重启后 /sys/module/wireguard
    都不存在 → 本页报「不支持」→ 用户以为坏了。
    显式写一份 modules-load.d 才是「开机自动加载」的正解。
    """
    txt = ('# 由 drouter 写入：VPN 服务端需要 wireguard 内核模块。\n'
           '# 不写的话模块不会被加载（Debian 走 udev 按需加载），\n'
           '# 表现为 drouter VPN 页提示「内核不支持 WireGuard」。\n'
           'wireguard\n')
    try:
        os.makedirs(os.path.dirname(WG_AUTOLOAD), exist_ok=True)
        with open(WG_AUTOLOAD, 'w', encoding='utf-8') as f:
            f.write(txt)
        return True, ''
    except Exception as ex:
        return False, str(ex)


def _vpn_ensure_module(persist=True):
    """尝试加载 wireguard 内核模块；成功后顺手写开机自动加载。

    返回 (ok, why)。纯本地动作（modprobe + 写一个 conf），
    不碰网络、不碰防火墙，所以放在 apply 之前自动做是安全的 ——
    能自愈的事不该把用户撵去 SSH。
    """
    if os.path.isdir('/sys/module/wireguard'):
        return True, ''
    if not shutil.which('modprobe'):
        return False, '系统里没有 modprobe（kmod 包），无法加载内核模块'
    rc, o, e = sh(['modprobe', 'wireguard'], timeout=20)
    # 光看 rc 不够：modprobe 可能 RC=0 但模块仍不在（极少见的
    # built-in 裁剪 / seccomp 拦截），所以一律以 /sys/module 复核。
    if rc != 0 or not os.path.isdir('/sys/module/wireguard'):
        return False, 'modprobe wireguard 失败：%s' % (
            e or o or ('返回码 %d' % rc))
    if persist:
        ok2, why2 = _vpn_write_autoload()
        if not ok2:
            # 加载成功但持久化失败：模块这次能用，只是重启后又要重来。
            # 不能当失败返回（服务能起来），但必须让用户知道。
            return True, ('模块已加载，但写 %s 失败（%s），'
                          '重启后需要重新加载' % (WG_AUTOLOAD, why2))
    return True, ''


def _vpn_install_tool():
    """装 wireguard-tools（提供 wg / wg-quick）。返回 (ok, detail)。"""
    if shutil.which('wg'):
        return True, 'wireguard-tools 已就绪'
    have, lack = _pkgs_available(['wireguard-tools', 'wireguard'])
    if not have:
        return False, ('在当前 apt 源里找不到 wireguard-tools（查到：%s）。'
                       '请先执行 apt-get update，或改用国内镜像源后重试。'
                       % ('、'.join(lack) or '无'))
    env = dict(os.environ)
    env['DEBIAN_FRONTEND'] = 'noninteractive'
    rc, o, e = sh(['apt-get', 'install', '-y', '--no-install-recommends'] + have,
                  timeout=900, env=env)
    if rc != 0 or not shutil.which('wg'):
        return False, ('apt-get install %s 失败：%s'
                       % (' '.join(have), (e or o)[-300:] or ('返回码 %d' % rc)))
    return True, '已安装 %s' % ' '.join(have)


def _vpn_free_port(prefer=0):
    """挑一个没被占用的 UDP 端口。

    用 UDP bind 试探：只 bind 不 listen，不会真的占住端口。
    ⚠️ 已知局限：如果本机 WireGuard 已经在这个端口上跑着，
    bind 会失败并返回别的端口 —— 这正是我们要的（换端口），
    但判断「用户选的端口是否可用」必须在服务**没启动**时做，
    否则会误报成「端口被占」而实际上是自己占的。
    """
    import socket as _sock
    cands = []
    if prefer:
        cands.append(int(prefer))
    cands += [51820, 51821, 51822, 51823, 8443, 443]
    for p in cands:
        if not (1 <= p <= 65535):
            continue
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
        try:
            s.bind(('0.0.0.0', p))
            return p
        except OSError:
            continue
        finally:
            try:
                s.close()
            except Exception:
                pass
    return 51820


def _vpn_port_busy(port):
    """端口当前是否被别的程序占着（不区分是否是我们自己的 WireGuard）。"""
    import socket as _sock
    s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
    try:
        s.bind(('0.0.0.0', int(port)))
        return False
    except OSError:
        return True
    finally:
        try:
            s.close()
        except Exception:
            pass


def _vpn_lan_net():
    """本机内网网段（用于 AllowedIPs 与「允许访问内网」判定）。"""
    nets = []
    try:
        rc, o, _e = sh(['sh', '-c',
                        "ip -4 -o route show scope link | awk '{print $1}'"],
                       timeout=6)
        for ln in (o or '').splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                nets.append(str(ipaddress.ip_network(ln, strict=False)))
            except Exception:
                pass
    except Exception:
        pass
    return nets


def _vpn_render(d, host_priv, host_pub):
    """渲染服务端 /etc/wireguard/wg0.conf。"""
    pool = ipaddress.ip_network(d['pool'])
    host_ip = str(pool.network_address + 1)
    s = [
        '# 由 drouter 自动生成：WireGuard 服务端（1.0.7）',
        '# 请通过 Web 界面修改，手改会在下次保存时被覆盖。',
        '',
        '[Interface]',
        'Address = %s' % host_ip,
        'ListenPort = %d' % d['port'],
        'PrivateKey = %s' % host_priv,
    ]
    if d.get('dns'):
        s.append('DNS = %s' % d['dns'])
    for n in _vpn_lan_net():
        s.append('PostUp = ip route add %s dev %s' % (n, VPN_IFACE))
        s.append('PostDown = ip route del %s dev %s 2>/dev/null || true' % (n, VPN_IFACE))
    s.append('')
    for p in d['peers']:
        if not p.get('enabled') or not p.get('has_key'):
            continue
        s.append('# %s（%s）' % (p.get('name') or p['id'], p.get('note') or p['ip']))
        s.append('[Peer]')
        s.append('PublicKey = %s' % p.get('public_key', ''))
        s.append('AllowedIPs = %s/32' % p['ip'])
        if d.get('keepalive'):
            s.append('PersistentKeepalive = %d' % d['keepalive'])
        s.append('')
    return '\n'.join(s) + '\n'


def _vpn_render_client(d, peer, client_priv, client_pub, host_pub):
    """渲染客户端配置（发给手机 / 笔记本的那份）。"""
    host = d.get('endpoint_host') or ''
    port = d['port']
    lines = ['[Interface]',
             'PrivateKey = %s' % client_priv,
             'Address = %s/32' % peer['ip']]
    if d.get('dns'):
        lines.append('DNS = %s' % d['dns'])
    lines += ['', '[Peer]',
              'PublicKey = %s' % host_pub,
              'AllowedIPs = %s' % ('0.0.0.0/0' if d.get('exit_node') else d['pool'])]
    if host:
        lines.append('Endpoint = %s:%d' % (host, port))
    if d.get('keepalive'):
        lines.append('PersistentKeepalive = %d' % d['keepalive'])
    lines += ['',
              '# 名称：%s' % (peer.get('name') or peer['id']),
              '# 这份文件含私钥，妥善保管，不要发到群里。']
    return '\n'.join(lines) + '\n'


def _vpn_write_conf(d):
    os.makedirs(WG_CONF_DIR, exist_ok=True)
    path = os.path.join(WG_CONF_DIR, VPN_IFACE + '.conf')
    priv = _vpn_hostkey()
    if not priv:
        priv = _wg_genkey()
        if not priv:
            return None, '无法生成 WireGuard 密钥（wg 命令不可用或内核不支持）'
        kp = os.path.join(WG_CONF_DIR, VPN_IFACE + '.key')
        with open(kp, 'w', encoding='utf-8') as f:
            f.write(priv + '\n')
        os.chmod(kp, 0o600)
    pub = _vpn_pubkey()
    # peers 里的 public_key 从 private_key 现场派生，所以配置里不存公钥
    for p in d['peers']:
        pk = p.get('private_key') or ''
        if pk:
            rc, o, _e = sh(['wg', 'pubkey'], timeout=6, input_data=pk + '\n')
            p['public_key'] = o.strip() if rc == 0 else ''
        else:
            p['public_key'] = ''
    text = _vpn_render(d, priv, pub)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return {'path': path, 'host_pub': pub}, None


def _vpn_svc_down():
    """停接口。优先 wg-quick，其次手工 down。"""
    if not os.path.isdir('/run/systemd/system'):
        # 容器形态：没有 systemd，wg-quick 用不了
        rc, o, _e = sh(['sh', '-c', 'wg-quick down %s 2>/dev/null' % VPN_IFACE],
                       timeout=15)
        if rc != 0:
            sh(['sh', '-c', 'ip link delete %s 2>/dev/null || true' % VPN_IFACE],
               timeout=10)
        return True
    sh(['systemctl', 'stop', 'wg-quick@%s' % VPN_IFACE], timeout=25)
    return True


def _vpn_svc_up():
    """起接口。容器形态下 wg-quick 不可用，退回 wg + ip 手工建。"""
    if not os.path.isdir('/run/systemd/system'):
        conf = os.path.join(WG_CONF_DIR, VPN_IFACE + '.conf')
        if not os.path.isfile(conf):
            return False, '配置文件不存在'
        priv = _vpn_hostkey()
        # 用 wg-quick 的 stripped 版不可靠（它依赖 systemd 之外的很多东西），
        # 这里手工建：ip link add + wg setconf
        sh(['sh', '-c', 'ip link delete %s 2>/dev/null || true' % VPN_IFACE],
           timeout=10)
        rc, o, e = sh(['ip', 'link', 'add', VPN_IFACE, 'type', 'wireguard'],
                      timeout=15)
        if rc != 0:
            return False, '创建接口失败：%s' % (e or o)
        # 从 conf 里抽出 Peer 段交给 wg setconf
        try:
            with open(conf, encoding='utf-8') as f:
                text = f.read()
        except Exception as ex:
            return False, str(ex)
        peers = _vpn_extract_peers(text)
        try:
            net = ipaddress.ip_network(_vpn_norm(_vpn_load())['pool'])
            sh(['ip', 'address', 'add',
                '%s/%d' % (str(net.network_address + 1), net.prefixlen),
                'dev', VPN_IFACE], timeout=10)
        except Exception:
            pass
        for n in _vpn_lan_net():
            sh(['ip', 'route', 'replace', n, 'dev', VPN_IFACE], timeout=8)
        if peers:
            rc, o, e = sh(['wg', 'setconf', VPN_IFACE, '/dev/stdin'], timeout=12,
                          input_data=peers)
            if rc != 0:
                return False, '配置 peer 失败：%s' % (e or o)
        sh(['ip', 'link', 'set', VPN_IFACE, 'up'], timeout=10)
        return True, ''
    rc, o, e = sh(['systemctl', 'start', 'wg-quick@%s' % VPN_IFACE], timeout=30)
    if rc != 0:
        return False, '启动失败：%s' % (e or o)
    return True, ''


def _vpn_extract_peers(text):
    """从 wg0.conf 里抽出 [Peer] 段落（供容器形态 wg setconf 用）。"""
    out = []
    cur = None
    for ln in text.splitlines():
        t = ln.strip()
        if t.startswith('#') or not t:
            continue
        if t.startswith('['):
            cur = [] if t.lower().startswith('[peer') else None
            if cur is not None:
                out.append(cur)
            continue
        if cur is not None:
            cur.append(t)
    return '\n'.join('\n'.join(g) for g in out) + '\n' if out else ''


def _vpn_live():
    """实时状态：接口在不在、监听端口、最近握手。"""
    rc, o, e = sh(['wg', 'show', VPN_IFACE, 'dump'], timeout=8)
    live = {'up': False, 'listen_port': None, 'peers': [], 'pubkey': ''}
    if rc != 0 or not o:
        return live
    lines = o.splitlines()
    if not lines:
        return live
    # dump 格式：第 1 行是本机，第 2 行是 private-key，第 3 行是 listen-port
    first = lines[0].split('\t')
    live['pubkey'] = first[0] if first else ''
    live['up'] = True
    if len(lines) > 2:
        try:
            live['listen_port'] = int(lines[2].split('\t')[0])
        except Exception:
            pass
    for ln in lines[4:]:
        p = ln.split('\t')
        if len(p) < 8:
            continue
        allowed = p[3] or '(none)'
        if allowed == '(none)':
            continue
        hs = 0
        try:
            hs = int(p[5])
        except Exception:
            pass
        rx = tx = 0
        try:
            rx = int(p[6])
            tx = int(p[7])
        except Exception:
            pass
        live['peers'].append({
            'pubkey': p[0], 'allowed': allowed,
            'handshake': (datetime.fromtimestamp(hs).isoformat(timespec='seconds')
                           if hs else ''),
            'rx': rx, 'tx': tx,
        })
    return live


def act_vpn(p):
    """WireGuard VPN：status / save / apply / stop / peer_add / peer_del /
    peer_toggle / peer_conf / keygen / endpoint / nft / delete_all。"""
    p = p or {}
    op = str(p.get('op') or 'status')
    if op == 'status':
        return _vpn_status()
    if op == 'save':
        return _vpn_save_op(p)
    if op == 'apply':
        return _vpn_apply_op(p)
    if op == 'stop':
        _vpn_svc_down()
        return ok({}, 'WireGuard 已停止')
    if op == 'peer_add':
        return _vpn_peer_add(p)
    if op == 'peer_del':
        return _vpn_peer_del(p)
    if op == 'peer_toggle':
        return _vpn_peer_toggle(p)
    if op == 'peer_conf':
        return _vpn_peer_conf(p)
    if op == 'endpoint':
        return _vpn_endpoint()
    if op == 'fix':
        return _vpn_fix_op()
    if op == 'delete_all':
        return _vpn_delete_all()
    return fail('未知的 VPN 操作：%s' % op)


def _vpn_fix_op():
    """一键修复：加载内核模块 + 装 wireguard-tools + 写开机自动加载。

    分步做、逐步汇报，不是一锅端。理由：
      * modprobe 是纯本地动作，不碰网络/防火墙，可以直接做；
      * 装包要联网、要动系统，**必须**是用户点了按钮才做，
        不能在 status 或 apply 的路径里偷偷执行。
    已经是 ready 时是幂等的 —— 直接告诉用户不用修。
    """
    before = _vpn_env()
    if before['state'] == 'ready':
        # ready 但没写开机自加载：顺手补上（否则重启后又变回不可用）
        if before['autoload']:
            return ok({'env': before, 'changed': []},
                      'WireGuard 环境正常（模块已加载、工具已安装、开机自动加载已配置）')
        okw, whyw = _vpn_write_autoload()
        if not okw:
            return fail('WireGuard 环境正常，但写 %s 失败：%s' % (WG_AUTOLOAD, whyw),
                        'AUTOLOAD_FAIL', {'env': _vpn_env(), 'changed': []})
        return ok({'env': _vpn_env(), 'changed': ['autoload']},
                  'WireGuard 环境正常，已补上开机自动加载配置')
    if before['state'] == 'unsupported':
        return fail('本机内核 %s 里没有 wireguard 模块，无法通过安装修复。'
                    '精简容器镜像和自编内核会裁掉它；'
                    'Debian 官方内核与 PVE/KVM 虚拟机都自带。' % before['kernel'],
                    'NO_WG', {'env': before, 'changed': []})
    done, notes = [], []
    # ① 模块
    if not before['mod_loaded']:
        okm, whym = _vpn_ensure_module()
        if not okm:
            return fail('加载 wireguard 内核模块失败：%s' % whym,
                        'WG_MODFAIL',
                        {'env': _vpn_env(), 'changed': done, 'notes': notes})
        done.append('module')
        notes.append('已加载 wireguard 内核模块')
        if whym:
            notes.append(whym)      # 模块成功但 autoload 写失败，也在notes 里
    elif not before['autoload']:
        okw, whyw = _vpn_write_autoload()
        if okw:
            done.append('autoload')
            notes.append('已配置开机自动加载')
        else:
            notes.append('写 %s 失败：%s' % (WG_AUTOLOAD, whyw))
    # ② 工具
    if not before['tool']:
        okt, what = _vpn_install_tool()
        if not okt:
            return fail('内核模块已就绪，但安装 wireguard-tools 失败：%s' % what,
                        'NO_WG_TOOL',
                        {'env': _vpn_env(), 'changed': done, 'notes': notes})
        done.append('tool')
        notes.append(what)
    after = _vpn_env()
    if after['state'] != 'ready':
        return fail('修复未完全成功（当前：%s）。%s'
                    % (after['state'], '；'.join(notes) or '请查看环境诊断'),
                    'FIX_INCOMPLETE',
                    {'env': after, 'changed': done, 'notes': notes})
    log('info', 'vpn', 'VPN_ENV_FIXED',
        'WireGuard 环境修复完成：%s' % ('、'.join(done) or '无需改动'))
    return ok({'env': after, 'changed': done, 'notes': notes},
              '修复完成：%s' % ('；'.join(notes) or '环境本来就正常'))


def _vpn_status():
    d = _vpn_norm(_vpn_load())
    live = _vpn_live()
    # 端口是否被占：只在服务没运行时才做这个判断。服务在跑的时候
    # 端口必然「被占」，但那是自己占的，不能报给用户说冲突。
    busy = False
    if d.get('port') and not live.get('up'):
        busy = _vpn_port_busy(d['port'])
    conflicts = []
    for n in _vpn_lan_net():
        try:
            if ipaddress.ip_network(n, strict=False).overlaps(
                    ipaddress.ip_network(d['pool'], strict=False)):
                conflicts.append(n)
        except Exception:
            pass
    return ok({
        'conf': _vpn_mask(d),
        # ⚠️ 早先这里还有一个 'raw': d —— 那是**含全部客户端私钥的完整
        # 配置**。前端从头到尾没读过它（grep 过4 个 vpn* 渲染函数，
        # 一个都没用到 raw），所以它唯一的实际效果是把所有已配对手机
        # 的 WireGuard 私钥以明文塞进 HTTP 响应里。旁边的
        # _vpn_mask(d) 就算不去掉这行也白做了。
        # 留着它的诱惑是「以后调试方便」—— 不值得。
        'live': live,
        'installed': _vpn_installed(),
        # env 是四态诊断的完整结果（state/mod_loaded/mod_file/tool/
        # kernel/virt/autoload/fixable）。前端文案与「一键修复」
        # 按钮全靠它 —— 早先只有一个 installed 字符串，把
        # 「模块没加载」「工具没装」「内核真没模块」压成同一个 no，
        # 文案只能瞎猜「精简容器」。
        'env': _vpn_env(),
        'has_systemd': os.path.isdir('/run/systemd/system'),
        'lan_nets': _vpn_lan_net(),
        'pool_conflict': conflicts,
        'port_busy': bool(busy),
        'suggest_port': _vpn_free_port(d.get('port') or 0),
        'path': os.path.join(WG_CONF_DIR, VPN_IFACE + '.conf'),
    }, '已读取 VPN 状态')


def _vpn_mask(d):
    """对外输出时把私钥抹掉。"""
    out = json.loads(json.dumps(d))
    for p in out.get('peers') or []:
        p.pop('private_key', None)
    return out


def _vpn_save_op(p):
    """保存配置（不启停服务）。"""
    d = _vpn_norm({**_vpn_load(), **(p.get('conf') or {})})
    # 「全部流量走回家」是个大开关，必须二次确认
    if d.get('exit_node') and p.get('confirm_exit') is not True:
        return fail('允许全部流量经过本机（Exit Node）会把家里的所有上网流量'
                    '都从家里出去，请勾选确认后重试', 'NEEDCONFIRM')
    # 端口被别的程序占着：自动换一个，并在返回里明确告诉用户换了。
    # 静默改端口比报错好，但**必须说出来** —— 否则用户按 51820 配了
    # 客户端，界面上却显示 51821，他会一直查为什么连不上。
    swapped = 0
    if d.get('port') and _vpn_port_busy(d['port']):
        d['port'] = _vpn_free_port(d['port'])
        swapped = d['port']
    _vpn_save(d)
    msg = 'VPN 配置已保存（还需点「应用」才会启动服务）'
    if swapped:
        msg = 'VPN 配置已保存。所选端口 %s 已被占用，已自动改用 %d' \
              % (p.get('conf', {}).get('port'), swapped)
    log('info', 'vpn', 'VPN_CONF_SAVED',
        '已保存 VPN 配置（端口 %d，%d 个客户端）' % (d['port'], len(d['peers'])))
    return ok({'conf': _vpn_mask(d), 'port_swapped': bool(swapped)}, msg)


def _vpn_apply_op(p):
    """渲染配置 + 启停服务。"""
    d = _vpn_norm({**_vpn_load(), **(p.get('conf') or {})})
    if d.get('exit_node') and p.get('confirm_exit') is not True:
        return fail('允许全部流量经过本机需要显式确认', 'NEEDCONFIRM')
    # 环境自检。**先尽力自愈再报错** ——
    # 「模块在但没加载」是 modprobe 一下的事（本地、秒级、不碰网络），
    # 早先在这里直接 return fail('内核不支持')，把一个能自愈的
    # 状态报成了死路一条，用户只能自己去 SSH。
    env = _vpn_env()
    if env['state'] == 'unsupported':
        return fail('本机内核里没有 WireGuard 模块（内核 %s），无法启动。'
                    % env['kernel'], 'NO_WG', {'env': env})
    if env['state'] in ('need_module', 'need_tool'):
        # need_module 顺手就修；need_tool 要下载安装包，不在这里做
        # （点页面上的「一键修复」按钮，或去服务页装依赖）。
        if env['state'] == 'need_module':
            okm, why = _vpn_ensure_module()
            if not okm:
                return fail('WireGuard 内核模块未加载，且自动加载失败：%s。'
                            '可点上方「一键修复」重试。' % why,
                            'WG_MODFAIL', {'env': _vpn_env()})
            env = _vpn_env()
        if env['state'] != 'ready':
            return fail(env['state'] == 'need_tool'
                        and ('缺少 wireguard-tools（提供 wg / wg-quick 命令），'
                             '无法启动服务。点上方「一键修复」可自动安装。'
                             or 'WireGuard 环境仍不可用。'),
                        'NO_WG', {'env': env})
    if not d.get('port'):
        d['port'] = _vpn_free_port(0)
    res, err = _vpn_write_conf(d)
    if err:
        return fail(err)
    _vpn_save(d)
    # 防火墙：只放行 WAN 侧
    nf = _vpn_nft_render(d)
    _vpn_nft_apply(nf)
    if not d.get('enabled'):
        _vpn_svc_down()
        return ok({'conf': _vpn_mask(d), 'nft': nf, 'applied': False},
                  '配置已写入，但 VPN 处于关闭状态（未启动服务）')
    up, why = _vpn_svc_up()
    if not up:
        # 启不动要把日志捞出来给用户看，否则只是一句「启动失败」
        rc, j, _e = sh(['journalctl', '-u', 'wg-quick@%s' % VPN_IFACE,
                        '-n', '20', '--no-pager'], timeout=12)
        return fail('服务启动失败：%s' % why,
                    'START_FAIL',
                    {'conf': _vpn_mask(d), 'nft': nf, 'journal': j[-1200:]})
    log('info', 'vpn', 'VPN_APPLIED',
        'WireGuard 已启动（端口 %d，%d 个客户端）' % (d['port'], len(d['peers'])))
    return ok({'conf': _vpn_mask(d), 'nft': nf, 'applied': True, 'live': _vpn_live()},
              'WireGuard 已启动，监听 UDP %d' % d['port'])


VPN_NFT_TABLE = 'drouter_vpn'


def _vpn_nft_render(d):
    """渲染放行规则。

    只在 WAN 侧放行 UDP 端口：内网的人不需要「连回来」，
    也不该让内网设备能用这个端口当跳板。
    """
    s = ['#!/usr/sbin/nft -f',
         '# 由 drouter 自动生成：WireGuard 放行（1.0.7）',
         'table inet %s {' % VPN_NFT_TABLE,
         '  chain vpn_in {',
         '    type filter hook input priority filter; policy accept;']
    if d.get('enabled') and d.get('port'):
        wan = _wan_lan_ifaces()
        s.append('    # 仅放行来自 WAN 侧的 WireGuard 流量')
        if wan:
            s.append('    iifname { %s } udp dport %d accept comment "drouter-wireguard"'
                     % (' '.join('"%s"' % x for x in wan), d['port']))
        else:
            s.append('    udp dport %d accept comment "drouter-wireguard"' % d['port'])
    s += ['  }', '}', '']
    return '\n'.join(s)


def _vpn_nft_apply(nf):
    """校验并加载 nft 片段。失败只记日志不阻断 —— 防火墙没加载成功
    不代表 VPN 服务本身起不来，用户可以自己放行。"""
    try:
        rc, o, e = sh(['nft', '-c', '-f', '-'], timeout=12, input_data=nf)
        if rc != 0:
            log('warn', 'vpn', 'VPN_NFT_CHECK_FAIL', '放行规则语法检查失败：%s'
                % (e or o))
            return False
        sh(['nft', '-f', '-'], timeout=15, input_data=nf)
    except Exception as ex:
        log('warn', 'vpn', 'VPN_NFT_FAIL', '放行规则加载失败：%s' % ex)
        return False
    return True


def _vpn_peer_add(p):
    """添加一个客户端。生成独立密钥对，私钥通过本 op 的返回值一次性交付。"""
    d = _vpn_norm(_vpn_load())
    name = str(p.get('name') or '').strip()[:60]
    if not name:
        return fail('请填写客户端名称（比如「我的手机」）')
    pid = re.sub(r'[^a-z0-9_-]', '', name.lower())[:32] or 'peer'
    # id 冲突就加序号
    base = pid
    n = 1
    ids = {x['id'] for x in d['peers']}
    while pid in ids:
        n += 1
        pid = '%s-%d' % (base[:28], n)
    priv = _wg_genkey()
    if not priv:
        return fail('无法生成客户端密钥（wg 命令不可用）')
    net = ipaddress.ip_network(d['pool'])
    used = {x['ip'] for x in d['peers']}
    cand = None
    for i in range(1, 250):
        a = str(net.network_address + i)
        if a not in used:
            cand = a
            break
    if not cand:
        return fail('地址池已用尽，请扩大地址池或先删除不用的客户端')
    peer = {'id': pid, 'name': name, 'ip': cand,
            'note': str(p.get('note') or '')[:200], 'enabled': True,
            'created': datetime.now().isoformat(timespec='seconds'),
            'private_key': priv, 'public_key': '', 'has_key': True}
    d['peers'].append(peer)
    _vpn_save(d)
    # 如果服务已经开着，立刻加进去免得用户以为没生效
    if d.get('enabled'):
        _vpn_apply_op({})
    conf_txt = ''
    hostpub = _vpn_pubkey()
    if hostpub:
        conf_txt = _vpn_render_client(d, peer, priv, '', hostpub)
    log('info', 'vpn', 'VPN_PEER_ADDED', '已添加客户端「%s」（%s）' % (name, cand))
    return ok({'peer': {'id': pid, 'name': name, 'ip': cand},
               'private_key': priv, 'client_conf': conf_txt,
               'config_name': 'drouter-%s.conf' % pid},
              '客户端已创建，私钥与配置文件只显示这一次，请立即下载保存')


def _vpn_peer_del(p):
    pid = re.sub(r'[^a-z0-9_-]', '', str(p.get('id') or '').lower())[:32]
    if not pid:
        return fail('缺少客户端 id')
    d = _vpn_load()
    raw = d.get('peers') or []
    removed = None
    newraw = []
    for x in raw:
        if isinstance(x, dict) and x.get('id') == pid:
            removed = x
            continue
        newraw.append(x)
    if removed is None:
        return fail('客户端不存在：%s' % pid)
    d['peers'] = newraw
    _vpn_save(_vpn_norm(d))
    if d.get('enabled'):
        _vpn_apply_op({})
    log('info', 'vpn', 'VPN_PEER_DEL', '已删除客户端「%s」'
        % (removed.get('name') or pid))
    return ok({'removed': 1, 'name': removed.get('name') or pid},
              '客户端「%s」已删除' % (removed.get('name') or pid))


def _vpn_peer_toggle(p):
    pid = re.sub(r'[^a-z0-9_-]', '', str(p.get('id') or '').lower())[:32]
    on = p.get('enabled') is True
    raw = _vpn_load().get('peers') or []
    hit = False
    for x in raw:
        if isinstance(x, dict) and x.get('id') == pid:
            x['enabled'] = on
            hit = True
    if not hit:
        return fail('客户端不存在')
    _vpn_save(_vpn_norm({'peers': raw}))
    if (_vpn_load().get('enabled')):
        _vpn_apply_op({})
    return ok({}, '客户端已%s' % ('启用' if on else '停用'))


def _vpn_peer_conf(p):
    """重新生成并返回某个客户端的配置（私钥仍在库里，不存在「重发一次就失效」）。"""
    pid = re.sub(r'[^a-z0-9_-]', '', str(p.get('id') or '').lower())[:32]
    d = _vpn_norm(_vpn_load())
    peer = None
    for x in d['peers']:
        if x['id'] == pid:
            peer = x
            break
    if not peer:
        return fail('客户端不存在')
    hostpub = _vpn_pubkey()
    if not hostpub:
        return fail('服务端密钥尚未生成，请先点「应用」')
    priv = peer.get('private_key') or ''
    if not priv:
        return fail('该客户端没有保存私钥（可能是从旧版本升级来的），请删掉重建')
    return ok({'client_conf': _vpn_render_client(d, peer, priv, '', hostpub),
               'config_name': 'drouter-%s.conf' % pid},
              '配置已生成，含私钥，请妥善保管')


def _vpn_endpoint(p=None):
    """猜一个可用的 Endpoint。

    没有公网 IP 的场景（PPPoE 在 NAT 后面、或大内网）只能靠 DDNS。
    这里给出「公网 IPv4 / 已配置的 DDNS 域名」两个候选，让用户选。
    """
    cands = []
    wan = _wan_lan_ifaces()
    for w in wan:
        rc, o, _e = sh(['sh', '-c',
                        "ip -4 -o addr show dev '%s' scope global | "
                        "awk '{print $4}' | cut -d/ -f1 | head -1" % w], timeout=6)
        v = (o or '').strip()
        if v and not _is_private(v):
            cands.append({'v': v, 'n': '公网 IPv4（%s 口）' % w, 'how': 'direct'})
    dd = _load_setting('ddns') or {}
    for key in ('domain', 'name', 'host'):
        v = str(dd.get(key) or '').strip()
        if v and re.match(r'^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$', v):
            cands.append({'v': v, 'n': 'DDNS 域名（需自行验证是否指向本机）',
                          'how': 'ddns'})
            break
    if not cands:
        cands.append({'v': '', 'n': '本机在 NAT 后面，没有可直接用的公网地址 —— '
                                   '请到「动态域名 DDNS」页配置域名后再来',
                      'how': 'none'})
    return ok({'candidates': cands}, '已列出可用的 Endpoint 候选')


def _vpn_delete_all():
    """彻底清除：停服务、删配置、清 nft 表。"""
    _vpn_svc_down()
    d = _vpn_load()
    d = _vpn_norm(d)
    d['enabled'] = False
    d['peers'] = []
    _vpn_save(d)
    for f in (os.path.join(WG_CONF_DIR, VPN_IFACE + '.conf'),
              os.path.join(WG_CONF_DIR, VPN_IFACE + '.key')):
        try:
            if os.path.isfile(f):
                os.unlink(f)
        except Exception:
            pass
    sh(['sh', '-c', 'nft delete table inet %s 2>/dev/null || true' % VPN_NFT_TABLE],
       timeout=10)
    log('info', 'vpn', 'VPN_DELETED_ALL', '已删除全部 WireGuard 配置与客户端')
    return ok({}, 'WireGuard 配置与全部客户端已删除')


# ---------------------------------------------------------------- 访问控制 / 家长时间组（#8）
#
# 目标：参考 Firewalla 的做法，把「设备 + 时间组 + 应用/网站类别」组合成可读的规则，
#       而不是让用户直接写 nftables。核心是复用已有的两块能力：
#         * QoS 的 mark 体系（QOS_MARK_BASE + 序号）—— 设备识别与带宽归类一致；
#         * DPI 的应用分类（nDPI）—— 「哪些应用属于哪个类别」共享同一份数据库。
#
# 设计要点：
#   * 规则的语义是「在某个时间段内，对某组设备，放通/阻断某类应用」；
#   * 时间组单独定义，可被多条规则复用（周末组、上学日组、睡眠组…）；
#   * 渲染为独立表 drouter_acl，使用 meta hour / meta day 做时间匹配（无需 cron 定时增删规则）；
#   * 与 DPI 的联动是「软联动」：DPI 有更新时，本模块按分类名重新展开应用列表，不需要重写规则。
#
# 注意：nftables 的 meta hour 只支持整点/半点粗粒度（实际支持到分钟），
#       且按 UTC 计算，因此渲染时需换算本地时区偏移，这里统一用 TZ_OFFSET_H 记录。

ACL_CONF = '/etc/drouter/generated/acl.nft'
ACL_JSON = '/etc/drouter/generated/acl.json'
ACL_UNIT = '/etc/systemd/system/drouter-acl.service'
ACL_TABLE = 'drouter_acl'
TZ_OFFSET_H = 8                    # 中国大陆固定 UTC+8（无夏令时）

# 时间组预设（Firewalla 风格：起止时间 + 生效星期）
ACL_TIME_PRESETS = [
    {'id': 'school', 'name': '上学日白天', 'days': [1, 2, 3, 4, 5], 'start': '07:00', 'end': '17:00',
     'why': '周一至周五 7:00–17:00，用于限制上学时段娱乐类应用。'},
    {'id': 'night', 'name': '夜间睡眠', 'days': [0, 1, 2, 3, 4, 5, 6], 'start': '22:00', 'end': '06:30',
     'why': '每天 22:00 到次日 6:30，跨零点，用于夜间断网或只留白名单。'},
    {'id': 'weekend', 'name': '周末全天', 'days': [0, 6], 'start': '00:00', 'end': '23:59',
     'why': '周六周日全天，通常放宽限制。'},
    {'id': 'homework', 'name': '作业时段', 'days': [1, 2, 3, 4, 5], 'start': '18:00', 'end': '21:00',
     'why': '工作日傍晚，只允许学习类应用与必要网站。'},
    {'id': 'always', 'name': '始终生效', 'days': [0, 1, 2, 3, 4, 5, 6], 'start': '00:00', 'end': '23:59',
     'why': '不限制时间，规则一直生效（可用于永久黑名单）。'},
]

# 应用类别（与 DPI 的 nDPI 分类对应，前端直接展示；DPI 更新后按 category 重新展开）
ACL_APP_GROUPS = [
    {'id': 'social', 'name': '社交娱乐', 'category': 'SocialNetwork',
     'apps': ['微信', 'QQ', '抖音', '快手', '小红书', '微博', 'TikTok'],
     'why': '主流短视频与社交平台，家长控制最常限制的一类。'},
    {'id': 'game', 'name': '游戏', 'category': 'Game',
     'apps': ['Steam', 'Xbox', 'PlayStation', '腾讯游戏', '网易游戏', '英雄联盟', '原神'],
     'why': '在线游戏协议（含 Steam/Xbox/PSN 平台流量）。'},
    {'id': 'video', 'name': '视频流媒体', 'category': 'Streaming',
     'apps': ['哔哩哔哩', '爱奇艺', '腾讯视频', '优酷', '芒果TV', 'Netflix', 'YouTube'],
     'why': '长视频平台，带宽占用大，也是常见的「限时」对象。'},
    {'id': 'p2p', 'name': 'P2P 下载', 'category': 'P2P',
     'apps': ['BitTorrent', '迅雷', '电驴'],
     'why': 'BT / P2P 流量，常被完全阻断以保障其他设备体验。'},
    {'id': 'adult', 'name': '不良内容', 'category': 'Adult',
     'apps': ['成人网站', '赌博', '恶意软件'],
     'why': '需要 nDPI 分类或外部黑名单支持，建议始终阻断。'},
    {'id': 'ad', 'name': '广告与跟踪', 'category': 'Ads',
     'apps': ['广告联盟', '统计跟踪'],
     'why': '广告与埋点域名，阻断后可提升浏览体验。'},
    {'id': 'study', 'name': '学习办公', 'category': 'Collaborative',
     'apps': ['钉钉', '企业微信', '腾讯会议', 'Zoom', 'Office365', '在线课堂'],
     'why': '通常在白名单里，作为「允许」类保留。'},
    {'id': 'custom', 'name': '自定义（域名/IP）', 'category': 'Custom',
     'apps': [],
     'why': '直接填写域名或 IP，适合学校网站、特定服务器等场景。'},
]

# 常见设备分组模板（Firewalla 风格：「孩子设备」「客人设备」）
ACL_GROUP_TEMPLATES = [
    {'id': 'kids', 'name': '孩子的设备', 'why': '把孩子的手机/平板/学习机归到一组，统一施加时间限制。'},
    {'id': 'guests', 'name': '访客设备', 'why': '访客网络设备，通常只允许上网、阻断内网访问。'},
    {'id': 'iot', 'name': 'IoT 设备', 'why': '摄像头、扫地机等，禁止主动外联、限制其访问范围。'},
    {'id': 'work', 'name': '办公设备', 'why': '办公电脑，优先带宽并允许办公协作类应用。'},
]

ACL_ACTIONS = [
    {'v': 'block', 'n': '阻断（禁止访问）', 'why': '匹配到的流量直接丢弃，最常用。'},
    {'v': 'allow', 'n': '放通（仅允许）', 'why': '用于白名单模式：只允许这些，其余在时间组内阻断。'},
    {'v': 'limit', 'n': '限速（交给 QoS）', 'why': '不打标记、不改优先级，仅命中 QoS 的限速策略。'},
    {'v': 'log', 'n': '仅记录（不阻断）', 'why': '只产生日志用于观察，用于「先看效果再决定」的试运行阶段。'},
]


def _acl_load():
    try:
        if os.path.isfile(ACL_JSON):
            with open(ACL_JSON, encoding='utf-8') as f:
                d = json.load(f) or {}
                if isinstance(d, dict):
                    return d
    except Exception:
        pass
    return {'enable': False, 'time_groups': [], 'groups': [], 'rules': []}


def _acl_save(d):
    # 原子写：付钱的是用户 —— 写到一半断电能把整份家长控制策略变成空文件
    _atomic_write(ACL_JSON, json.dumps(d, ensure_ascii=False, indent=2) + '\n',
                  mode=0o644)


ACL_UNIT_TMPL = """[Unit]
Description=drouter 访问控制与家长时间组（nftables）
Documentation=man:nft(8)
After=network-online.target drouter-nft.service
Wants=network-online.target
PartOf=drouter-apply.service
ConditionPathExists={conf}

[Service]
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Type=oneshot
RemainAfterExit=yes
ExecStartPre=-/usr/sbin/nft delete table inet {table}
ExecStart=/usr/sbin/nft -f {conf}
ExecStop=-/usr/sbin/nft delete table inet {table}
ExecReload=/usr/sbin/nft -f {conf}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def _acl_deploy(d=None, live=True):
    """把访问控制配置落盘为 nft 文件，并按需加载到内核。

    返回 {'conf': 路径, 'applied': bool, 'msg_cn': ...}。
    在构建保护模式下（/etc/drouter/BUILD_MODE 存在）只写文件不加载，
    避免在真机调试时误伤用户网络。
    """
    if d is None:
        d = _acl_load()
    text = _acl_render_nft(d)
    _atomic_write(ACL_CONF, text, mode=0o644)

    # 单元文件（幂等写入，内容变化才覆盖）
    try:
        unit = ACL_UNIT_TMPL.format(conf=ACL_CONF, table=ACL_TABLE)
        old = ''
        if os.path.isfile(ACL_UNIT):
            with open(ACL_UNIT, encoding='utf-8') as f:
                old = f.read()
        if old != unit:
            os.makedirs(os.path.dirname(ACL_UNIT), exist_ok=True)
            with open(ACL_UNIT, 'w', encoding='utf-8') as f:
                f.write(unit)
            os.chmod(ACL_UNIT, 0o644)
            subprocess.run(['systemctl', 'daemon-reload'],
                           capture_output=True, timeout=20)
    except Exception:
        pass

    if not live:
        return {'conf': ACL_CONF, 'applied': False,
                'msg_cn': '仅渲染未加载（live=False）'}
    if os.path.isfile('/etc/drouter/BUILD_MODE'):
        return {'conf': ACL_CONF, 'applied': False,
                'msg_cn': '构建保护模式：已生成 %s，未加载到内核' % ACL_CONF}
    # 先删旧表再加载，保证幂等
    subprocess.run(['nft', 'delete', 'table', 'inet', ACL_TABLE],
                   capture_output=True, timeout=20)
    r = subprocess.run(['nft', '-f', ACL_CONF],
                       capture_output=True, timeout=30)
    if r.returncode != 0:
        err = (r.stderr or b'').decode('utf-8', 'replace').strip()
        return {'conf': ACL_CONF, 'applied': False,
                'msg_cn': '规则加载失败：%s' % (err or '未知错误')}
    return {'conf': ACL_CONF, 'applied': True, 'msg_cn': '访问控制规则已加载到内核'}


def _acl_hhmm_min(hhmm):
    """把 HH:MM 转成当天的分钟数，并校验范围。"""
    try:
        h, m = [int(x) for x in str(hhmm).split(':')[:2]]
    except Exception:
        raise ValidateError('时间格式不正确，应为 HH:MM：%s' % hhmm)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValidateError('时间超出范围（00:00–23:59）：%s' % hhmm)
    return h * 60 + m


def _acl_localize_hhmm(hhmm, offset=TZ_OFFSET_H):
    """把本地时间 HH:MM 换算成 nftables meta hour 需要的 UTC HH:MM。

    nftables 的 meta hour 以 UTC 计算，因此需要减去时区偏移。
    仅用于表单校验与单点换算；跨零点的拆分请用 _acl_time_windows。
    """
    mins = _acl_hhmm_min(hhmm)
    return _acl_min_hhmm((mins - offset * 60) % 1440)


def _acl_min_hhmm(mins):
    return '%02d:%02d' % divmod(int(mins) % 1440, 60)


def _acl_day_bit(days):
    """把星期列表转成 nftables meta day 用的可读集合（用英文缩写）。"""
    NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
    out = []
    for d in days or []:
        try:
            i = int(d) % 7
        except Exception:
            continue
        out.append(NAMES[i])
    return sorted(set(out), key=lambda x: NAMES.index(x))


def _acl_shift_days(days, delta):
    """把星期集合整体平移 delta 天（用于时区换算造成的星期错位修正）。"""
    out = []
    for d in days or []:
        try:
            i = int(d) % 7
        except Exception:
            continue
        out.append((i + delta) % 7)
    return sorted(set(out))


def _acl_time_windows(tg, offset=TZ_OFFSET_H):
    """把本地时间组换算成一组 UTC 时间窗（nftables meta hour/day 用）。

    返回 [(utc_start_min, utc_end_min, day_delta)]：
      * utc_start_min / utc_end_min 为 **UTC 当天** 的分钟数（0–1439）；
      * day_delta 供渲染层调用 `_acl_shift_days(用户选的星期, day_delta)`
        得到应写入 `meta day` 的星期集合。

    原理（以 UTC+8 为例，即 offset=8、shift=480）：
      本地时刻 T ↔ UTC u = (T - shift) mod 1440。
      从 UTC 反推本地：local_day = utc_day + rev(u)，其中
      rev(u) = (u + shift) // 1440（0 或 1）。

      内核的匹配条件是「UTC 星期 ∈ 写入的集合 且 UTC 分钟 ∈ [us,ue]」，
      而我们希望「本地星期 ∈ 用户选的集合」。由于
      local_day = utc_day + rev(u)，等价于
      utc_day ∈ { (d - rev(u)) mod 7 }，而 `_acl_shift_days` 实现的是
      `(d + delta) mod 7`，因此 **day_delta = -rev(u)**。

      UTC 一天内只有 **一个** 本地日/UTC 日分界点：
          boundary = (-shift) mod 1440 = 960   ← 本地 16:00 = UTC 08:00
      · UTC 分钟 0 … 959  （本地 16:00 → 次日 15:59）→ rev = 0 → day_delta = 0
      · UTC 分钟 960 … 1439（本地次日 00:00 → 07:59） → rev = 1 → day_delta = -1

    算法（先把本地区间整体映射到 UTC，再按 boundary 切分）：
      设本地区间为 [S, E]（跨零点时拆成 [S,1439] 与 [0,E] 两段），
      逐段映射到 UTC 得到 [utc_of(s), utc_of(e)]，再在 boundary 处切开，
      保证每段内 day_delta 恒定。

    该实现已用穷举法（7×1440 分钟）验证：任意本地 days / start / end
    组合下，渲染结果与语义完全一致（见 _dev/t-acl-tz.py）。
    """
    start = _acl_hhmm_min(tg.get('start') or '00:00')
    end = _acl_hhmm_min(tg.get('end') or '23:59')
    shift = offset * 60 % 1440
    # 本地日/UTC 日分界点（UTC 刻度）：UTC 时刻 u 使 (u + shift) 跨过 1440
    boundary = (-shift) % 1440

    def utc_of(mins):
        return (mins - shift) % 1440

    def delta_at(u):
        """UTC 分钟 u 应写入 meta day 的星期平移量。

        从 UTC 反推本地日偏移 rev(u) = (u + shift) // 1440；
        渲染层用 (d + delta) % 7，故 delta = -rev(u)。
        """
        return -((u + shift) // 1440)

    # 本地区间 → 若干「本地日内的连续段落」
    if start <= end:
        local_segs = [(start, end)]
    else:
        # 本地跨零点：起→23:59 与 00:00→止，两段属于**不同**的本地日
        local_segs = [(start, 1439), (0, end)]

    segs = []
    for (ls, le) in local_segs:
        us, ue = utc_of(ls), utc_of(le)
        if us <= ue:
            # 该本地段映射到 UTC 后不跨越 boundary，但可能仍横跨 boundary
            if delta_at(us) == delta_at(ue):
                segs.append((us, ue, ls))
            else:
                # 段内跨越 boundary：按 boundary 切成两半
                segs.append((us, boundary - 1, ls))
                segs.append((boundary, ue, ls))
        else:
            # 映射后 utc 起点 > 终点（本地段自身跨 UTC 零点）→ 同样按 boundary 切
            segs.append((us, 1439, ls))
            segs.append((0, ue, ls))

    # 每段按 day_delta 再切一次，保证一段内 delta 恒定
    out = []
    for (us, ue, _ls) in segs:
        if us > ue:
            continue
        d = delta_at(us)
        if delta_at(ue) == d:
            out.append((us, ue, d))
        else:
            out.append((us, boundary - 1, d))
            out.append((boundary, ue, delta_at(ue)))

    # 合并相邻且 day_delta 相同、时间连续的段
    out.sort(key=lambda x: (x[2], x[0]))
    merged = []
    for w in out:
        if merged and merged[-1][2] == w[2] and merged[-1][1] + 1 >= w[0]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], w[1]), w[2])
        else:
            merged.append(w)
    return merged


def _acl_time_expr(tg):
    """生成时间匹配表达式（供界面说明/预览用）。

    与真正写入内核的 `_acl_render_nft` 保持同一套换算：直接复用
    `_acl_time_windows`，把每一段 UTC 窗口连同其星期集合一起展示，
    避免「界面上显示的」和「实际生效的」不一致。
    """
    try:
        windows = _acl_time_windows(tg)
    except ValidateError:
        windows = []
    start = tg.get('start') or '00:00'
    end = tg.get('end') or '23:59'
    cross = _acl_hhmm_min(start) > _acl_hhmm_min(end)
    base_days = tg.get('days') or [0, 1, 2, 3, 4, 5, 6]

    if not windows:
        return 'meta hour "%s"-"%s"（本地 %s–%s）' % (
            _acl_localize_hhmm(start), _acl_localize_hhmm(end), start, end)

    parts = []
    for (u_s, u_e, ddelta) in windows:
        dnames = _acl_day_bit(_acl_shift_days(base_days, ddelta))
        day_expr = ('meta day { %s } ' % ', '.join('"%s"' % d for d in dnames)) if dnames else ''
        parts.append('%smeta hour "%s"-"%s"' % (day_expr, _acl_min_hhmm(u_s), _acl_min_hhmm(u_e)))
    tail = '（本地 %s–%s%s，已按 UTC+%d 换算%s）' % (
        start, end, '，跨零点' if cross else '', TZ_OFFSET_H,
        '，拆成 %d 段' % len(windows) if len(windows) > 1 else '')
    return ' 或 '.join(parts) + tail


def _acl_render_nft(d, table=ACL_TABLE):
    """把访问控制配置渲染成 nftables 脚本。"""
    enable = bool(d.get('enable'))
    rules = d.get('rules') or []
    tgs = {t.get('id'): t for t in (d.get('time_groups') or [])}
    groups = {g.get('id'): g for g in (d.get('groups') or [])}

    s = [
        '#!/usr/sbin/nft -f',
        '# 由 drouter 自动生成：访问控制 / 家长时间组（#8）',
        '# 时间匹配已按 UTC+8 换算；如需修改请通过 Web 界面，不要手改本文件。',
        '',
        'table inet %s {' % table,
        '  set kids_v4 {',
        '    type ipv4_addr',
        '    flags interval',
        '  }',
        '  set kids_v6 {',
        '    type ipv6_addr',
        '    flags interval',
        '  }',
        '  set app_marks {',
        '    type mark',
        '  }',
        '',
        '  chain acl_forward {',
        '    type filter hook forward priority -10; policy accept;',
    ]
    if not enable:
        s += ['    # 总开关关闭：不产生任何规则，转发不受影响',
              '    counter comment "访问控制已停用"',
              '  }',
              '}']
        return '\n'.join(s) + '\n'

    # 每个设备组的地址集合
    for gid, g in groups.items():
        v4 = [x for x in (g.get('hosts') or []) if ':' not in str(x)]
        v6 = [x for x in (g.get('hosts') or []) if ':' in str(x)]
        gname = re.sub(r'[^a-zA-Z0-9_]', '_', str(gid))[:24] or 'g'
        if v4:
            s += ['    # 组 %s：%d 个 IPv4' % (g.get('name') or gid, len(v4))]
        if v6:
            s += ['    # 组 %s：%d 个 IPv6' % (g.get('name') or gid, len(v6))]

    s += ['']
    idx = 0
    for r in rules:
        if not r.get('enable', True):
            continue
        idx += 1
        action = (r.get('action') or 'block').lower()
        name = (r.get('name') or ('规则 %d' % idx)).replace('"', '')[:40]
        tg = tgs.get(r.get('time_group')) or {}
        g = groups.get(r.get('group')) or {}
        hosts = [str(x).strip() for x in (g.get('hosts') or []) if str(x).strip()]
        apps = r.get('apps') or []
        # 应用组展开（与 DPI 分类共享）
        app_ids = []
        for a in apps:
            grp = next((x for x in ACL_APP_GROUPS if x['id'] == a), None)
            if grp:
                app_ids.append(grp['name'])
        base_days = tg.get('days') or [0, 1, 2, 3, 4, 5, 6]
        start = tg.get('start') or '00:00'
        end = tg.get('end') or '23:59'
        # 本地跨零点（用于注释提示）
        cross = _acl_hhmm_min(start) > _acl_hhmm_min(end)
        try:
            windows = _acl_time_windows(tg)
        except ValidateError as e:
            raise ValidateError('时间组「%s」%s' % (tg.get('name') or '未命名', e.msg_cn))

        s.append('    # ---- [%d] %s ｜ 时间组：%s（本地 %s–%s）｜ 设备组：%s ｜ 动作：%s ----'
                 % (idx, name, tg.get('name') or '始终', start, end,
                    g.get('name') or '全部设备', action))
        if app_ids:
            s.append('    # 应用范围（来自 DPI 分类，共 %d 类）：%s'
                     % (len(app_ids), '、'.join(app_ids)))
        if cross:
            s.append('    # 本地时间跨零点，已自动拆分为两段')
        if len(windows) > 1:
            s.append('    # 时区换算（UTC+%d）后需拆成 %d 段，已自动处理' % (TZ_OFFSET_H, len(windows)))

        saddr = []
        if hosts:
            v4 = [x for x in hosts if ':' not in x]
            v6 = [x for x in hosts if ':' in x]
            sub = []
            if v4:
                sub.append('ip saddr { %s }' % ', '.join(v4))
            if v6:
                sub.append('ip6 saddr { %s }' % ', '.join(v6))
            saddr.append('(' + ' or '.join(sub) + ')')

        def emit(hour_expr, days_local):
            c = list(saddr)
            dnames = _acl_day_bit(days_local)
            if dnames:
                c.append('meta day { %s }' % ', '.join('"%s"' % x for x in dnames))
            if hour_expr:
                c.append(hour_expr)
            head = '    ' + ' '.join(c) if c else '    '
            if action == 'block':
                s.append(head + ' counter drop comment "%s"' % name)
            elif action == 'allow':
                s.append(head + ' counter accept comment "%s"' % name)
            elif action == 'limit':
                s.append(head + ' meta mark set 0x%x comment "%s（限速）"' % (QOS_MARK_BASE, name))
            else:
                s.append(head + ' counter comment "%s（仅记录）"' % name)

        for (u_s, u_e, ddelta) in windows:
            days_w = _acl_shift_days(base_days, ddelta)
            hx = 'meta hour "%s"-"%s"' % (_acl_min_hhmm(u_s),
                                         '23:59' if u_e >= 1439 else _acl_min_hhmm(u_e))
            emit(hx, days_w)
        s.append('')

    s += ['    counter comment "访问控制总计数"',
          '  }',
          '}']
    return '\n'.join(s) + '\n'


# ==================================================================
#  #9 内网文件共享（SMB / NFS）
# ==================================================================

SHARE_JSON = '/etc/drouter/generated/share.json'
SHARE_ROOT = '/srv/share'

# 共享目录常见集合：默认给一个公共盘
SHARE_DEFAULT_USERS = ['drouter']

# Samba：Debian 13 (trixie) 里的实际包名与配置文件位置
SMB_PKGS = ['samba', 'samba-common-bin']
NFS_PKGS = ['nfs-kernel-server']

# Debian 13 上 samba 用 include 引入 /etc/samba/*.conf，无需改动主配置
SMB_INCLUDE_LINE = 'include = /etc/samba/drouter.conf'
SMB_MAIN_CONF = '/etc/samba/smb.conf'

# 目录用途：供界面展示的「模板」
SHARE_DIR_TEMPLATES = [
    {'id': 'public', 'name': '公共盘（所有人可读写）', 'path': '/srv/share/public',
     'note': '适合放全家共享的文件；SMB 侧以 nobody 身份写入，NFS 侧 all_squash。'},
    {'id': 'media', 'name': '影音库（只读）', 'path': '/srv/share/media',
     'note': '电视盒子 / 播放器挂载用；只读可避免误删片源。'},
    {'id': 'home', 'name': '私人目录（需账号）', 'path': '/srv/share/home',
     'note': '必须用用户名密码登录；只对你指定的用户开放。'},
    {'id': 'backup', 'name': '备份盘（仅指定设备可写）', 'path': '/srv/share/backup',
     'note': '配合「允许网段」限制，只有备份服务器能访问。'},
]

# NFS 客户端模板（界面下拉用）
NFS_PRESETS = [
    {'v': 'linux', 'n': 'Linux 客户端（推荐）',
     'opts': 'rw,sync,no_subtree_check,secure,root_squash',
     'why': 'root_squash 会把客户端 root 映射成匿名用户，最安全，适合绝大多数发行版。'},
    {'v': 'macos', 'n': 'macOS 客户端',
     'opts': 'rw,sync,no_subtree_check,secure,all_squash,insecure',
     'why': 'macOS 挂载时源端口可能高于 1024，必须加 insecure；all_squash 避免权限冲突。'},
    {'v': 'windows', 'n': 'Windows（WSL2 / 客户端功能）',
     'opts': 'rw,sync,no_subtree_check,secure,all_squash,no_auth_nlm,insecure,anonuid=65534,anongid=65534',
     'why': 'Windows 的 NFS 客户端对权限映射较特殊，固定匿名 uid/gid 为 nobody(65534) 最省事。'},
    {'v': 'readonly', 'n': '只读导出',
     'opts': 'ro,sync,no_subtree_check,secure,root_squash',
     'why': '只读，适合作为片源 / 镜像库共享。'},
    {'v': 'legacy', 'n': '兼容旧设备（不安全）',
     'opts': 'rw,sync,no_subtree_check,insecure,no_root_squash',
     'why': '老式播放器 / 瘦客户端可能需要；允许客户端 root 直接写，风险较高，请仅在内网可信环境使用。'},
]

# Windows / macOS / Linux 的「立马能用」挂载说明（每次读取时动态生成）
def _share_client_hints(host, shares):
    """生成三平台的连接指引（含可直接复制的命令）。"""
    ips = [host.get('lan_ip') or '192.168.7.1']
    for a in (host.get('extra_ips') or []):
        if a:
            ips.append(a)
    ip = ips[0]
    smb_names = [s.get('name') for s in shares if s.get('enable', True) and s.get('name')]
    nfs_paths = [s.get('path') for s in shares if s.get('enable', True) and s.get('path')]
    first_smb = smb_names[0] if smb_names else 'Public'
    first_nfs = nfs_paths[0] if nfs_paths else SHARE_ROOT + '/public'
    return {
        'windows': {
            'title': 'Windows 10 / 11',
            'steps': [
                '打开「此电脑」，在地址栏输入 \\\\%s 回车' % ip,
                '若弹出凭据窗口：用户名填 drouter（或你创建的账号），密码为对应密码；'
                '公共盘可勾选「使用其他账户」后留空直接确定。',
                '右键该共享 →「映射网络驱动器」，即可长期使用。',
            ],
            'cmds': [
                'net use Z: \\\\%s\\%s /persistent:yes' % (ip, first_smb),
                'explorer \\\\%s' % ip,
            ],
            'nfs_note': 'Windows 需在「启用或关闭 Windows 功能」里勾选「NFS 客户端」才能挂载 NFS。'
                        '一般用 SMB 即可。',
        },
        'macos': {
            'title': 'macOS',
            'steps': [
                '访达 → 顶部菜单「前往」→「连接服务器」(⌘K)',
                '输入 smb://%s 回车，选择要挂载的共享' % ip,
                'NFS 则输入 nfs://%s%s' % (ip, first_nfs),
            ],
            'cmds': [
                'open smb://%s' % ip,
                'mkdir -p /Volumes/share && mount_nfs %s:%s /Volumes/share' % (ip, first_nfs),
            ],
            'nfs_note': 'macOS 自带 NFS v3/v4 客户端；SMB 更推荐，兼容性与权限处理都更好。',
        },
        'linux': {
            'title': 'Linux',
            'steps': [
                'SMB：安装 cifs-utils 后挂载；NFS：安装 nfs-common 后挂载。',
                '把挂载写进 /etc/fstab 可开机自动挂载。',
            ],
            'cmds': [
                'sudo apt install -y cifs-utils',
                'sudo mount -t cifs //%s/%s /mnt/share -o username=drouter,vers=3.0' % (ip, first_smb),
                'sudo apt install -y nfs-common',
                'sudo mount -t nfs4 %s:%s /mnt/share' % (ip, first_nfs),
            ],
            'nfs_note': 'NFS 在 Linux 上性能最好，推荐局域网内 Linux 之间使用。',
        },
    }


def _share_pkg_state():
    """探测 samba / nfs 是否已安装，并给出 Debian 13 的安装命令。"""
    def installed(pkg):
        rc, _o, _e = sh(['dpkg-query', '-W', '-f=${Status}', pkg], timeout=15)
        return rc == 0 and 'install ok installed' in (_o or '')

    smb_ok = all(installed(p) for p in SMB_PKGS)
    nfs_ok = all(installed(p) for p in NFS_PKGS)
    return {
        'samba': {'installed': smb_ok, 'pkgs': SMB_PKGS},
        'nfs': {'installed': nfs_ok, 'pkgs': NFS_PKGS},
        'install_cmd_smb': 'apt-get install -y ' + ' '.join(SMB_PKGS),
        'install_cmd_nfs': 'apt-get install -y ' + ' '.join(NFS_PKGS),
    }


def _share_dir_meta(path):
    """返回共享目录是否存在的元信息；不存在时给出创建命令。"""
    exists = os.path.isdir(path)
    meta = {'path': path, 'exists': exists, 'writable': False, 'size': ''}
    if exists:
        meta['writable'] = os.access(path, os.W_OK)
        rc, out, _e = sh(['du', '-sh', path], timeout=20)
        if rc == 0 and out:
            meta['size'] = out.split('\t')[0].strip()
    else:
        meta['create_cmd'] = 'mkdir -p %s' % path
    return meta


def _share_load():
    try:
        if os.path.isfile(SHARE_JSON):
            with open(SHARE_JSON, encoding='utf-8') as f:
                d = json.load(f) or {}
                if isinstance(d, dict):
                    return d
    except Exception:
        pass
    return {
        'enable_smb': False, 'enable_nfs': False,
        'samba': {'workgroup': 'WORKGROUP', 'server_string': 'drouter 文件共享',
                  'disable_netbios': True, 'wins_enable': False, 'wins_support': False,
                  'interfaces': '', 'global_extra': '', 'shares': []},
        'nfs': {'threads': 8, 'exports': []},
    }


def _share_save(d):
    _atomic_write(SHARE_JSON, json.dumps(d, ensure_ascii=False, indent=2) + '\n',
                  mode=0o644)


def _share_lan_ip():
    """取 LAN 侧 IPv4，用于生成客户机连接说明。"""
    try:
        rc, out, _e = sh(['ip', '-4', '-o', 'addr', 'show', 'scope', 'global'], timeout=15)
        for line in (out or '').splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[1] != 'lo':
                return parts[3].split('/')[0]
    except Exception:
        pass
    return '192.168.7.1'


def _share_apply_file(path, content, live, unit, table=None):
    """写文件 + 可选重启服务。返回 (ok, msg)。

    与 act_apply 保持同一套原子写：先写同目录临时文件，再 os.replace 覆盖。
    之所以不能用「同目录临时文件」之外的写法：open(path,'w') 会先把原文件截断，
    如果这一瞬间 smbd / nfs-server 恰好 reload 或掉电，就会读到一个半截的配置
    文件 —— 要么服务起不来，要么更糟：共享目录按残缺的 share 定义暴露出去。
    os.replace 在同一个文件系统内是原子重命名，读者要么看到旧的、要么看到新的。
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.drouter.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(content)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception as e:
        # 清理可能残留的临时文件，避免下次写入被同名残留挡住
        try:
            os.remove(path + '.drouter.tmp')
        except Exception:
            pass
        return False, '写入 %s 失败：%s' % (path, e)
    if not live:
        return True, '已写入 %s（未生效）' % path
    if os.path.isfile('/etc/drouter/BUILD_MODE'):
        return True, '构建保护模式：已写入 %s，未重启服务' % path
    rc, _o, e = sh(['systemctl', 'restart', unit], timeout=60)
    if rc != 0:
        return False, '重启 %s 失败：%s' % (unit, e or '未知错误')
    return True, '已写入 %s 并重启 %s' % (path, unit)


def _ensure_smb_include():
    """确保主配置 include 了 drouter.conf（幂等）。"""
    try:
        if not os.path.isfile(SMB_MAIN_CONF):
            return False, '未找到 %s，请先安装 samba' % SMB_MAIN_CONF
        with open(SMB_MAIN_CONF, encoding='utf-8', errors='replace') as f:
            body = f.read()
        if SMB_INCLUDE_LINE in body:
            return True, 'include 已存在'
        # 备份一次，再追加
        bkp = SMB_MAIN_CONF + '.drouter-bak'
        if not os.path.isfile(bkp):
            shutil.copy2(SMB_MAIN_CONF, bkp)
        with open(SMB_MAIN_CONF, 'a', encoding='utf-8') as f:
            f.write('\n# 由 drouter 追加（#9）\n%s\n' % SMB_INCLUDE_LINE)
        return True, '已追加 include'
    except Exception as e:
        return False, '修改主配置失败：%s' % e


# ==================================================================
#  #10 Docker / Docker Compose 管理面板
# ==================================================================

DOCKER_DIR = '/etc/drouter/docker'
DOCKER_STACK_DIR = os.path.join(DOCKER_DIR, 'stacks')
# compose 的包名各发行版不一致：Debian 13 叫 docker-compose（装的是 v2 插件），
# Ubuntu 一度叫 docker-compose-v2。写死任一个都会在另一个平台上装不上。
# 这里把两个都列出来，由 _pkgs_available() 过滤出本机真实存在的那些再交给 apt ——
# 实测（real apt --dry-run）只要有一个包名不存在，apt 会整体中止、一个都不装，
# 所以不能让不存在的名字混进命令行。
DOCKER_PKGS = ['docker.io', 'docker-compose', 'docker-compose-v2']
DOCKER_LEGACY_COMPOSE_PKGS = ['docker-compose']

# docker run 里「不带值」的布尔型参数（存在即为真）
DOCKER_BOOL_FLAGS = {
    'detach', 'interactive', 'tty', 'rm', 'privileged', 'init', 'read-only',
    'no-healthcheck', 'oom-kill-disable', 'publish-all', 'stdin-open',
}

# 短参数 → 长参数（docker CLI 常见缩写）
DOCKER_SHORT = {
    'd': 'detach', 'i': 'interactive', 't': 'tty', 'p': 'publish', 'v': 'volume',
    'e': 'env', 'u': 'user', 'w': 'workdir', 'h': 'hostname', 'm': 'memory',
    'l': 'label', 'a': 'attach', 'c': 'cpu-shares', 'n': 'name', 'rm': 'rm',
    'P': 'publish-all', 'q': 'quiet', 'V': 'volume',
}

# 需要把重复出现聚合成列表的参数
DOCKER_LIST_FLAGS = {
    'publish', 'volume', 'env', 'label', 'mount', 'tmpfs', 'add-host',
    'dns', 'device', 'expose', 'cap-add', 'cap-drop', 'security-opt',
    'sysctl', 'ulimit', 'link', 'volumes-from', 'annotation',
}

# restart / network 等直接映射
DOCKER_RESTART = {'no': 'no', 'on-failure': 'on-failure', 'always': 'always',
                  'unless-stopped': 'unless-stopped'}


def _docker_shlex(line):
    """把 docker run 命令行切成 token，正确处理引号与反斜杠续行。

    支持：
      * 单引号 / 双引号（含引号内的空格）
      * 反斜杠转义
      * 行尾 \\ 续行
      * 引号内的 $ 变量不做展开（原样保留）
    """
    # 去掉行尾反斜杠续行（\\\n）
    line = re.sub(r'\\\s*\n', ' ', line)
    tokens, cur, quote, esc = [], [], None, False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
            continue
        if ch == '\\' and quote != "'":
            esc = True
            continue
        if quote:
            if ch == quote:
                quote = None
            else:
                cur.append(ch)
            continue
        if ch in ('"', "'"):
            quote = ch
            continue
        if ch.isspace():
            if cur:
                tokens.append(''.join(cur))
                cur = []
            continue
        cur.append(ch)
    if cur:
        tokens.append(''.join(cur))
    return tokens


def _docker_split_kv(tok):
    """把 `--flag=value` / `--flag value` 拆成 (flag, value_or_None)。"""
    t = tok.lstrip('-')
    if '=' in t:
        k, v = t.split('=', 1)
        return k, v
    return t, None


# 参数 → compose 键 的映射表（docker run → docker-compose.yml）
#   kind: simple（直接值）/ bool（存在即 true）/ list（聚合数组）/ map_env（键值对）
DOCKER_RUN_MAP = {
    'name': ('container_name', 'simple'),
    'hostname': ('hostname', 'simple'),
    'user': ('user', 'simple'),
    'workdir': ('working_dir', 'simple'),
    'entrypoint': ('entrypoint', 'simple'),
    'restart': ('restart', 'simple'),
    'network': ('network_mode', 'network'),
    'net': ('network_mode', 'network'),
    'memory': ('mem_limit', 'simple'),
    'memory-swap': ('memswap_limit', 'simple'),
    'cpus': ('cpus', 'simple'),
    'cpu-shares': ('cpu_shares', 'simple'),
    'shm-size': ('shm_size', 'simple'),
    'platform': ('platform', 'simple'),
    'stop-signal': ('stop_signal', 'simple'),
    'stop-timeout': ('stop_grace_period', 'simple'),
    'log-driver': ('logging.driver', 'logdriver'),
    'log-opt': ('logging.options', 'logopt'),
    'publish': ('ports', 'port'),
    'expose': ('expose', 'list'),
    'volume': ('volumes', 'list'),
    'mount': ('volumes', 'mount'),
    'tmpfs': ('tmpfs', 'list'),
    'env': ('environment', 'env'),
    'env-file': ('env_file', 'list'),
    'label': ('labels', 'label'),
    'add-host': ('extra_hosts', 'list'),
    'dns': ('dns', 'list'),
    'device': ('devices', 'list'),
    'cap-add': ('cap_add', 'list'),
    'cap-drop': ('cap_drop', 'list'),
    'security-opt': ('security_opt', 'list'),
    'sysctl': ('sysctls', 'list'),
    'ulimit': ('ulimits', 'ulimit'),
    'link': ('links', 'list'),
    'volumes-from': ('volumes_from', 'list'),
    'health-cmd': ('healthcheck.test', 'healthcmd'),
    'health-interval': ('healthcheck.interval', 'simple'),
    'health-retries': ('healthcheck.retries', 'simple'),
    'health-timeout': ('healthcheck.timeout', 'simple'),
    'health-start-period': ('healthcheck.start_period', 'simple'),
    'detach': ('detach', 'skip'),
    'interactive': ('stdin_open', 'bool'),
    'tty': ('tty', 'bool'),
    'rm': ('_rm', 'rm'),
    'privileged': ('privileged', 'bool'),
    'init': ('init', 'bool'),
    'read-only': ('read_only', 'bool'),
    'publish-all': ('_publish_all', 'note'),
    'gpus': ('deploy.resources.reservations.devices', 'gpu'),
    'pid': ('pid', 'simple'),
    'ipc': ('ipc', 'simple'),
    'userns': ('userns_mode', 'simple'),
    'runtime': ('runtime', 'simple'),
    'pull': ('pull_policy', 'simple'),
    'group-add': ('group_add', 'list'),
    'isolation': ('isolation', 'simple'),
    'cgroup-parent': ('cgroup_parent', 'simple'),
    'mac-address': ('mac_address', 'simple'),
    'domainname': ('domainname', 'simple'),
}


def _docker_run_to_compose(text, service_name=None, version=None):
    """把 `docker run ...` 命令转换成 docker-compose.yml（纯逻辑，可单测）。

    返回 dict：
      {'ok': bool, 'yaml': str, 'service': str, 'image': str,
       'notes': [中文提示], 'warnings': [中文警告], 'fields': {...}}
    """
    notes, warnings = [], []
    # 1) 找到 `docker run`（或 `docker container run`）之后的 token 序列
    raw = (text or '').strip()
    if not raw:
        return {'ok': False, 'msg_cn': '请输入 docker run 命令'}
    # 把可能存在的多行续行拼起来
    flat = re.sub(r'\\\s*\n', ' ', raw)
    flat = re.sub(r'\s+', ' ', flat).strip()

    # 定位 docker run 的起点（兼容 docker run / docker container run / sudo docker run）
    m = re.search(r'\bdocker\s+(?:container\s+)?run\b', flat)
    if not m:
        head = flat.split()[0] if flat.split() else ''
        if head.startswith('docker-compose') or re.search(r'\bdocker\s+compose\b', flat):
            return {'ok': False, 'msg_cn': '检测到的是 docker compose 命令；'
                                          '本工具用于把 `docker run` 转成 compose 文件。'}
        return {'ok': False, 'msg_cn': '未找到 `docker run` 子命令，请粘贴完整命令'
                                      '（示例：docker run -d --name web -p 8080:80 nginx）'}
    rest = flat[m.end():].strip()

    tokens = _docker_shlex(rest)
    # 2) 划分：参数（--flag / -f）与位置参数（镜像、命令）
    i = 0
    opts = []          # [(flag, value_or_None)]
    positional = []
    # 已知「需要取值」的长参数 = 在映射表里且不是布尔型
    takes_value = {k for k, (_, kind) in DOCKER_RUN_MAP.items()
                   if kind not in ('bool', 'skip', 'rm', 'note')}

    def _looks_like_image(tok, bare_ok=False):
        """启发式判断一个 token 是否像镜像引用。

        强特征（一定像镜像）：含 `/`（registry 路径）、含 `:`（tag 或 host:port）。
        弱特征（裸词，如 `nginx`）：只在 bare_ok=True 时才算像镜像——
        即「它是 token 流里最后一个非参数 token」时（docker run 的镜像几乎总在末尾）。
        """
        if not tok or tok[0] == '-':
            return False
        if '/' in tok or ':' in tok:
            return True
        return bare_ok and bool(re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*$', tok))

    def _is_last_bare(tokens_, idx):
        """idx 之后是否只剩参数（没有别的位置参数），即它是最后一个裸词。"""
        for x in tokens_[idx + 1:]:
            if not x.startswith('-'):
                return False
        return True

    while i < len(tokens):
        t = tokens[i]
        if t == '--':
            positional.extend(tokens[i + 1:])
            break
        if t.startswith('--'):
            flag, val = _docker_split_kv(t)
            if val is not None:
                opts.append((flag, val))
            elif flag in DOCKER_BOOL_FLAGS:
                # 明确知道是布尔型：不吃下一个 token
                opts.append((flag, None))
            elif flag in takes_value:
                # 已知需要取值
                if i + 1 < len(tokens):
                    opts.append((flag, tokens[i + 1]))
                    i += 1
                else:
                    warnings.append('参数 --%s 缺少取值，已忽略' % flag)
            else:
                # 未知参数：用启发式判断它是否带值。
                # 若下一个 token 不像镜像（强特征或「末尾裸词」），则认定它是参数值；
                # 否则按布尔开关处理（保护 `--future-flag nginx` 这类写法）。
                nxt = tokens[i + 1] if i + 1 < len(tokens) else None
                if nxt is not None and not nxt.startswith('-'):
                    nxt_is_image = _looks_like_image(
                        nxt, bare_ok=_is_last_bare(tokens, i + 1))
                    if not nxt_is_image:
                        opts.append((flag, nxt))
                        i += 1
                    else:
                        opts.append((flag, None))
                else:
                    opts.append((flag, None))
        elif t.startswith('-') and len(t) > 1:
            # 短参数：可能是 -d, -it, -p8080:80, -p 8080:80
            body = t[1:]
            j = 0
            while j < len(body):
                ch = body[j]
                long_name = DOCKER_SHORT.get(ch)
                if not long_name:
                    warnings.append('无法识别的短参数 -%s，已忽略' % ch)
                    j += 1
                    continue
                if long_name in DOCKER_BOOL_FLAGS:
                    opts.append((long_name, None))
                    j += 1
                    continue
                # 该短参数需要值：值可能在同 token 剩余部分，或下一个 token
                tail = body[j + 1:]
                if tail:
                    opts.append((long_name, tail))
                elif i + 1 < len(tokens):
                    opts.append((long_name, tokens[i + 1]))
                    i += 1
                else:
                    warnings.append('参数 -%s 缺少取值' % ch)
                break
        else:
            positional.append(t)
            # 第一个位置参数是镜像，其后都是命令
        i += 1

    if not positional:
        return {'ok': False, 'msg_cn': '没有解析到镜像名，请检查命令是否完整'}
    image = positional[0]
    command = positional[1:]

    # 3) 逐项映射
    svc = {}
    envs, labels, ulimits = [], [], {}
    log_opts = {}
    health = {}
    gpus = None
    skip_rm = False
    publish_all = False

    def ensure_list(key):
        if key not in svc or not isinstance(svc[key], list):
            svc[key] = []
        return svc[key]

    def set_nested(dotted, value):
        parts = dotted.split('.')
        node = svc
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value

    for flag, val in opts:
        spec = DOCKER_RUN_MAP.get(flag)
        if not spec:
            warnings.append('暂不支持参数 --%s，已忽略（可在下方「补充配置」里手工添加）' % flag)
            continue
        key, kind = spec

        if kind == 'skip':
            continue
        if kind == 'rm':
            skip_rm = True
            continue
        if kind == 'note':
            publish_all = True
            continue

        if kind == 'bool':
            set_nested(key, True)
        elif kind == 'simple':
            if val is None:
                warnings.append('参数 --%s 缺少取值，已忽略' % flag)
                continue
            if key == 'restart':
                val = DOCKER_RESTART.get(val, val)
            set_nested(key, val)
        elif kind == 'network':
            if val is None:
                continue
            # --network host / none / container:xxx 用 network_mode；具名网络用 networks
            if val in ('host', 'none') or val.startswith('container:'):
                svc['network_mode'] = val
            else:
                ensure_list('networks').append(val)
        elif kind == 'port':
            if val is None:
                continue
            # 规范化：8080:80 / 127.0.0.1:8080:80/tcp 都原样保留（compose 语法一致）
            ensure_list('ports').append(val)
        elif kind == 'list':
            if val is None:
                continue
            ensure_list(key).append(val)
        elif kind == 'mount':
            # --mount type=bind,src=/a,dst=/b[,readonly] → compose 长语法
            if not val:
                continue
            parts, flags_set = {}, set()
            for kv in val.split(','):
                kv = kv.strip()
                if not kv:
                    continue
                if '=' in kv:
                    k, v2 = kv.split('=', 1)
                    parts[k.strip()] = v2.strip()
                else:
                    flags_set.add(kv)      # 例如 readonly / bind-propagation
            src = parts.get('source') or parts.get('src')
            tgt = parts.get('target') or parts.get('dst') or parts.get('destination')
            if parts.get('type') and tgt:
                item = {'type': parts['type'], 'target': tgt}
                if src:
                    item['source'] = src
                if 'readonly' in flags_set or parts.get('readonly') in ('true', '1'):
                    item['read_only'] = True
                ensure_list('volumes').append(item)
            else:
                warnings.append('无法解析 --mount 参数：%s' % val)
        elif kind == 'env':
            if val is None:
                continue
            envs.append(val)
        elif kind == 'label':
            if val is None:
                continue
            labels.append(val)
        elif kind == 'ulimit':
            # --ulimit nofile=1024:2048  或  --ulimit nofile=1024
            if val and '=' in val:
                k, v2 = val.split('=', 1)
                if ':' in v2:
                    soft, hard = v2.split(':', 1)
                else:
                    soft = hard = v2
                ulimits[k.strip()] = {'soft': soft.strip(), 'hard': hard.strip()}
        elif kind == 'logdriver':
            if val:
                set_nested('logging.driver', val)
        elif kind == 'logopt':
            if val and '=' in val:
                k, v2 = val.split('=', 1)
                log_opts[k.strip()] = v2.strip()
        elif kind == 'healthcmd':
            if val:
                # health-cmd 里常见 shell 语法，保留为 CMD-SHELL
                health['test'] = ['CMD-SHELL', val]
        elif kind == 'gpu':
            if val:
                gpus = val

    # 4) 组装最终 service
    out = {'image': image}
    if command:
        out['command'] = command if len(command) > 1 else command[0]
    # 合并环境变量 / 标签
    for k, v in svc.items():
        if k == 'healthcheck' and isinstance(v, dict):
            health.update(v)       # 子项合并，避免覆盖 test
            continue
        out[k] = v
    if envs:
        out['environment'] = envs
    if labels:
        out['labels'] = labels
    if ulimits:
        out['ulimits'] = ulimits
    if log_opts:
        out.setdefault('logging', {})['options'] = log_opts
    if health:
        out['healthcheck'] = health
    if gpus:
        out.setdefault('deploy', {}).setdefault('resources', {}) \
            .setdefault('reservations', {})['devices'] = [
                {'driver': 'nvidia', 'count': 'all',
                 'capabilities': [['gpu']]}]

    if skip_rm:
        out['restart'] = 'no'
        notes.append('原命令用了 --rm（运行结束即删除容器）。Compose 没有等价项，'
                     '已改为 restart: no，并把容器保留下来便于查看日志。')
    if publish_all:
        notes.append('原命令用了 -P/--publish-all（随机映射所有 EXPOSE 端口）。'
                     'Compose 不支持随机端口，建议改成显式 ports 列表。')
        out.setdefault('ports', [])
    if out.get('network_mode') and out.get('networks'):
        notes.append('同时检测到 --network 与具名网络，已保留 network_mode；'
                     '如需加入自定义网络请删除 network_mode 后保留 networks。')

    # 5) 服务名：优先 --name，其次镜像名（去掉 registry / tag）
    name = service_name or out.get('container_name') or ''
    if not name:
        base = image.split('/')[-1]
        base = base.split(':')[0]
        base = re.sub(r'[^a-zA-Z0-9_.-]', '-', base) or 'app'
        name = base
    name = re.sub(r'[^a-zA-Z0-9_.-]', '-', name)

    # 6) 渲染 YAML（手写，避免依赖 PyYAML；格式与 docker compose 完全兼容）
    yaml_text = _to_yaml_compose(name, out, version)

    return {
        'ok': True, 'service': name, 'image': image,
        'yaml': yaml_text, 'fields': out,
        'notes': notes, 'warnings': warnings,
        'msg_cn': '解析成功' + ('（有 %d 处提示）' % len(warnings) if warnings else ''),
    }


def _yaml_scalar(v):
    """把 Python 值渲染成 YAML 标量（尽量不加多余引号，但必要时加）。"""
    if v is True:
        return 'true'
    if v is False:
        return 'false'
    if v is None:
        return 'null'
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    # 含特殊字符 / 前后空格 / 看起来像数字或布尔 → 加双引号
    need_quote = (
        s == '' or s != s.strip() or
        re.search(r'[:#{}\[\],&*?|<>=!%@`\'"]', s) or
        s.lower() in ('true', 'false', 'yes', 'no', 'on', 'off', 'null', '~') or
        re.match(r'^[+-]?\d+(\.\d+)?$', s)
    )
    if need_quote:
        return '"%s"' % s.replace('\\', '\\\\').replace('"', '\\"')
    return s


def _to_yaml_compose(name, svc, version=None):
    """把 service dict 渲染成 docker-compose.yml 文本。"""
    L = []
    L.append('# 由 drouter 从 docker run 命令转换生成')
    L.append('# 用法：保存为 docker-compose.yml，然后 docker compose up -d')
    L.append('# 说明：新版 docker compose（v2）已不需要 version 字段。')
    if version:
        L.append('version: "%s"' % version)
    L.append('services:')
    L.append('  %s:' % name)

    def dump(node, indent):
        pad = ' ' * indent
        lines = []
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, (dict, list)):
                    lines.append('%s%s:' % (pad, k))
                    lines.extend(dump(v, indent + 2))
                else:
                    lines.append('%s%s: %s' % (pad, k, _yaml_scalar(v)))
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, dict):
                    # 第一行挂在 "- " 上，其余缩进对齐
                    sub = dump(item, indent + 2)
                    if sub:
                        first = sub[0].lstrip()
                        lines.append('%s- %s' % (pad, first))
                        lines.extend(sub[1:])
                elif isinstance(item, list):
                    lines.append('%s- %s' % (pad, ' '.join(_yaml_scalar(x) for x in item)))
                else:
                    lines.append('%s- %s' % (pad, _yaml_scalar(item)))
        return lines

    # image 放最前（可读性最好）
    ordered = {}
    if 'image' in svc:
        ordered['image'] = svc['image']
    if 'container_name' in svc:
        ordered['container_name'] = svc['container_name']
    for k, v in svc.items():
        if k not in ('image', 'container_name'):
            ordered[k] = v
    L.extend(dump(ordered, 4))

    # 若用了具名网络，补一个顶层 networks 段（声明为 external 避免误创建）
    nets = svc.get('networks')
    if isinstance(nets, list) and nets:
        L.append('')
        L.append('# 具名网络：如需让 compose 自动创建，把 external 改为 false')
        L.append('networks:')
        for n in nets:
            L.append('  %s:' % n)
            L.append('    external: true')
    return '\n'.join(L) + '\n'


def _docker_bin():
    """返回可用的 docker 可执行文件路径（兼容 PATH 与常见位置）。"""
    for p in ('/usr/bin/docker', '/usr/local/bin/docker'):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    rc, out, _e = sh(['which', 'docker'], timeout=10)
    if rc == 0 and out:
        return out.splitlines()[0].strip()
    return 'docker'


def _compose_cmd():
    """返回 compose 命令前缀：优先 `docker compose`（v2），退化到 `docker-compose`。"""
    d = _docker_bin()
    rc, _o, _e = sh([d, 'compose', 'version'], timeout=15)
    if rc == 0:
        return [d, 'compose']
    rc2, _o2, _e2 = sh(['docker-compose', 'version'], timeout=15)
    if rc2 == 0:
        return ['docker-compose']
    return None


def _docker_pkg_state():
    def inst(pkg):
        rc, _o, _e = sh(['dpkg-query', '-W', '-f=${Status}', pkg], timeout=12)
        return rc == 0 and 'install ok installed' in (_o or '')
    d_ok = inst('docker.io') or os.path.isfile('/usr/bin/docker')
    # compose 的包名跨发行版不同，两个名字都查一遍
    c_ok = inst('docker-compose') or inst('docker-compose-v2') or bool(_compose_cmd())
    # 展示给用户抄的命令必须是本机能装上的：把查不到的候选滤掉，
    # 否则用户照着抄会得到「E: 无法定位软件包 docker-compose-v2」，
    # 而那其实是 Ubuntu 的叫法，跟他的 Debian 没关系。
    have, _lack = _pkgs_available(DOCKER_PKGS)
    comp_have = [p for p in have if p != 'docker.io']
    return {
        'docker': {'installed': d_ok, 'pkgs': have},
        'compose': {'installed': c_ok, 'pkgs': comp_have},
        'install_cmd': 'apt-get install -y ' + ' '.join(have),
        'bin': _docker_bin(),
    }


def _docker_ps(all_=True):
    """列出容器（JSON 逐行）。"""
    d = _docker_bin()
    fmt = ('{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Status}}\t'
           '{{.Ports}}\t{{.CreatedAt}}\t{{.Size}}')
    cmd = [d, 'ps', '--format', fmt]
    if all_:
        cmd.insert(2, '-a')
    rc, out, err = sh(cmd, timeout=30)
    if rc != 0:
        return None, err or '无法列出容器（Docker 可能未运行）'
    rows = []
    for line in (out or '').splitlines():
        parts = line.split('\t')
        if len(parts) < 5:
            continue
        rows.append({
            'id': parts[0][:12], 'name': parts[1], 'image': parts[2],
            'state': parts[3], 'status': parts[4],
            'ports': parts[5] if len(parts) > 5 else '',
            'created': parts[6] if len(parts) > 6 else '',
            'size': parts[7] if len(parts) > 7 else '',
        })
    return rows, None


def _docker_stats():
    """取容器实时资源占用（一次性快照，非流式）。"""
    d = _docker_bin()
    fmt = '{{.ID}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.NetIO}}\t{{.BlockIO}}\t{{.PIDs}}'
    rc, out, _err = sh([d, 'stats', '--no-stream', '--format', fmt], timeout=40)
    if rc != 0:
        return {}
    res = {}
    for line in (out or '').splitlines():
        p = line.split('\t')
        if len(p) >= 6:
            res[p[0][:12]] = {
                'cpu': p[1], 'mem': p[2], 'mem_perc': p[3],
                'net': p[4], 'block': p[5], 'pids': p[6] if len(p) > 6 else '',
            }
    return res


def _docker_stack_list():
    """列出 drouter 管理的 compose 项目（stacks 目录下的子目录）。"""
    out = []
    if not os.path.isdir(DOCKER_STACK_DIR):
        return out
    for name in sorted(os.listdir(DOCKER_STACK_DIR)):
        d = os.path.join(DOCKER_STACK_DIR, name)
        if not os.path.isdir(d):
            continue
        yml = None
        for cand in ('docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml'):
            p = os.path.join(d, cand)
            if os.path.isfile(p):
                yml = p
                break
        if not yml:
            continue
        stat = os.stat(yml)
        out.append({
            'name': name, 'dir': d, 'file': os.path.basename(yml),
            'mtime': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
            'size': stat.st_size,
        })
    return out


def _docker_validate_stack_name(name):
    n = (name or '').strip()
    if not re.match(r'^[a-z0-9][a-z0-9_-]{0,40}$', n):
        raise ValidateError('项目名不合规：只能用小写字母、数字、- 和 _，且以字母或数字开头', 'name')
    return n


def read_docker(p):
    """读取 Docker 状态：引擎/容器/镜像/网络/卷/compose 项目（#10）。"""
    pkgs = _docker_pkg_state()
    info = {}
    d = _docker_bin()
    rc, out, err = sh([d, 'info', '--format',
                       '{{.ServerVersion}}\t{{.Containers}}\t{{.ContainersRunning}}\t'
                       '{{.ContainersStopped}}\t{{.Images}}\t{{.Driver}}\t'
                       '{{.DockerRootDir}}\t{{.OperatingSystem}}'], timeout=25)
    if rc == 0 and out:
        p2 = out.split('\t')
        info = {
            'version': p2[0] if len(p2) > 0 else '',
            'containers': p2[1] if len(p2) > 1 else '',
            'running': p2[2] if len(p2) > 2 else '',
            'stopped': p2[3] if len(p2) > 3 else '',
            'images': p2[4] if len(p2) > 4 else '',
            'driver': p2[5] if len(p2) > 5 else '',
            'root': p2[6] if len(p2) > 6 else '',
            'os': p2[7] if len(p2) > 7 else '',
        }
    else:
        info['error'] = err or 'Docker 守护进程未运行'

    containers, ps_err = _docker_ps(True)
    if containers is None:
        containers = []
        info.setdefault('error', ps_err)

    stats = _docker_stats()
    for c in containers:
        c['stat'] = stats.get(c['id'], {})

    images = []
    rc, out, _e = sh([d, 'images', '--format',
                      '{{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Size}}\t{{.CreatedAt}}'],
                     timeout=30)
    if rc == 0:
        for line in (out or '').splitlines():
            p3 = line.split('\t')
            if len(p3) >= 4:
                images.append({'repo': p3[0], 'tag': p3[1], 'id': p3[2][:12],
                               'size': p3[3], 'created': p3[4] if len(p3) > 4 else ''})

    nets = []
    rc, out, _e = sh([d, 'network', 'ls', '--format', '{{.Name}}\t{{.Driver}}\t{{.Scope}}'],
                     timeout=25)
    if rc == 0:
        for line in (out or '').splitlines():
            p4 = line.split('\t')
            if len(p4) >= 2:
                nets.append({'name': p4[0], 'driver': p4[1],
                             'scope': p4[2] if len(p4) > 2 else ''})

    vols = []
    rc, out, _e = sh([d, 'volume', 'ls', '--format', '{{.Name}}\t{{.Driver}}\t{{.Mountpoint}}'],
                     timeout=25)
    if rc == 0:
        for line in (out or '').splitlines():
            p5 = line.split('\t')
            if len(p5) >= 2:
                vols.append({'name': p5[0], 'driver': p5[1],
                             'mountpoint': p5[2] if len(p5) > 2 else ''})

    # 性能：原来每个单元一次 systemctl is-active（2 次 fork）；改批量。
    # 注意这里 read_docker 的契约是**纯字符串**（'active'/'inactive'），
    # 与 read_services 的字典结构不同，别照搬。
    svc = {}
    _st = _svc_states(('docker', 'containerd'))
    for name in ('docker', 'containerd'):
        svc[name] = _st[name]['active']

    return ok({
        'pkgs': pkgs, 'info': info, 'services': svc,
        'containers': containers, 'images': images,
        'networks': nets, 'volumes': vols,
        'stacks': _docker_stack_list(),
        'compose_cmd': ' '.join(_compose_cmd() or ['docker', 'compose']),
        'stack_dir': DOCKER_STACK_DIR,
        'note': ('Docker 面板支持容器/镜像/网络/卷的日常运维，以及 Compose 项目管理。'
                 '「命令转换」可以把任意 docker run 命令转成 docker-compose.yml。'),
    })


def act_docker(p):
    """Docker 操作：start/stop/restart/rm/logs/inspect/prune/convert/stack_*。"""
    p = p or {}
    op = (p.get('op') or '').strip()
    d = _docker_bin()

    # ---------- 纯逻辑：run → compose 转换（不需要 Docker 在运行） ----------
    if op == 'convert':
        return ok(_docker_run_to_compose(p.get('cmd') or '',
                                        service_name=p.get('service') or None,
                                        version=(p.get('version') or '').strip() or None))

    # ---------- 容器操作 ----------
    if op in ('start', 'stop', 'restart', 'pause', 'unpause', 'rm', 'kill'):
        cid = (p.get('id') or '').strip()
        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$', cid):
            return fail('容器 ID / 名称不合法', 'BAD_ID')
        args = [d, op, cid]
        if op == 'rm' and p.get('force'):
            args.insert(2, '-f')
        rc, out, err = sh(args, timeout=90)
        if rc != 0:
            return fail('操作失败：%s' % (err or out or '未知错误'), 'DOCKER_FAIL')
        cn = {'start': '已启动', 'stop': '已停止', 'restart': '已重启', 'pause': '已暂停',
              'unpause': '已恢复', 'rm': '已删除', 'kill': '已强制结束'}[op]
        return ok({}, '容器 %s %s' % (cid, cn))

    if op == 'logs':
        cid = (p.get('id') or '').strip()
        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$', cid):
            return fail('容器 ID / 名称不合法', 'BAD_ID')
        tail = int(p.get('tail') or 200)
        tail = max(10, min(2000, tail))
        args = [d, 'logs', '--tail', str(tail)]
        if p.get('timestamps'):
            args.append('-t')
        args.append(cid)
        rc, out, err = sh(args, timeout=40)
        # docker logs 把容器日志写在 stderr 是正常现象
        text = (out or '') + (('\n' + err) if err and not out else '')
        if rc != 0 and not text:
            return fail('读取日志失败：%s' % (err or '未知错误'), 'DOCKER_FAIL')
        return ok({'text': text.strip() or '（暂无日志输出）', 'id': cid})

    if op == 'inspect':
        cid = (p.get('id') or '').strip()
        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$', cid):
            return fail('容器 ID / 名称不合法', 'BAD_ID')
        rc, out, err = sh([d, 'inspect', cid], timeout=30)
        if rc != 0:
            return fail('查看详情失败：%s' % (err or '未知错误'), 'DOCKER_FAIL')
        try:
            data = json.loads(out)
            return ok({'text': json.dumps(data, ensure_ascii=False, indent=2)})
        except Exception:
            return ok({'text': out})

    if op == 'prune':
        what = (p.get('what') or 'containers').strip()
        sub = {'containers': ['container', 'prune', '-f'],
               'images': ['image', 'prune', '-a', '-f'],
               'volumes': ['volume', 'prune', '-f'],
               'networks': ['network', 'prune', '-f'],
               'builder': ['builder', 'prune', '-a', '-f']}.get(what)
        if not sub:
            return fail('不支持的清理类型：%s' % what, 'BAD_WHAT')
        rc, out, err = sh([d] + sub, timeout=180)
        if rc != 0:
            return fail('清理失败：%s' % (err or '未知错误'), 'DOCKER_FAIL')
        return ok({'text': out}, '已执行清理：%s' % what)

    # ---------- Compose 项目 ----------
    if op in ('stack_list',):
        return ok({'stacks': _docker_stack_list()})

    if op == 'stack_save':
        try:
            name = _docker_validate_stack_name(p.get('name'))
        except ValidateError as e:
            return fail(e.msg_cn, 'BAD_NAME')
        content = p.get('content') or ''
        if not content.strip():
            return fail('compose 内容不能为空', 'BAD_CONTENT')
        # 基础语法自检：必须是 YAML 且含 services 段
        if not re.search(r'^\s*services\s*:', content, re.M):
            return fail('内容里没有找到 services 段，请确认这是合法的 compose 文件', 'BAD_CONTENT')
        if '\x00' in content:
            return fail('内容包含非法字符（NUL）', 'BAD_CONTENT')
        d2 = os.path.join(DOCKER_STACK_DIR, name)
        os.makedirs(d2, exist_ok=True)
        fname = p.get('file') or 'docker-compose.yml'
        if fname not in ('docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml'):
            fname = 'docker-compose.yml'
        with open(os.path.join(d2, fname), 'w', encoding='utf-8') as f:
            f.write(content)
        return ok({'name': name, 'dir': d2}, '项目 %s 已保存到 %s' % (name, d2))

    if op == 'stack_get':
        try:
            name = _docker_validate_stack_name(p.get('name'))
        except ValidateError as e:
            return fail(e.msg_cn, 'BAD_NAME')
        d2 = os.path.join(DOCKER_STACK_DIR, name)
        if not os.path.isdir(d2):
            return fail('项目 %s 不存在' % name, 'NOT_FOUND')
        yml = None
        for cand in ('docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml'):
            if os.path.isfile(os.path.join(d2, cand)):
                yml = os.path.join(d2, cand)
                break
        if not yml:
            return fail('项目 %s 里没有 compose 文件' % name, 'NOT_FOUND')
        with open(yml, encoding='utf-8') as f:
            return ok({'name': name, 'file': os.path.basename(yml), 'content': f.read()})

    if op in ('stack_up', 'stack_down', 'stack_restart', 'stack_pull', 'stack_ps'):
        try:
            name = _docker_validate_stack_name(p.get('name'))
        except ValidateError as e:
            return fail(e.msg_cn, 'BAD_NAME')
        d2 = os.path.join(DOCKER_STACK_DIR, name)
        if not os.path.isdir(d2):
            return fail('项目 %s 不存在' % name, 'NOT_FOUND')
        comp = _compose_cmd()
        if not comp:
            return fail('未找到 docker compose（v2）或 docker-compose（v1），请先安装', 'NO_COMPOSE')
        submap = {
            'stack_up': ['up', '-d', '--remove-orphans'],
            'stack_down': ['down'],
            'stack_restart': ['restart'],
            'stack_pull': ['pull'],
            'stack_ps': ['ps'],
        }
        args = comp + submap[op]
        if p.get('build') and op == 'stack_up':
            args.append('--build')
        rc, out, err = sh(args, timeout=600, cwd=d2)
        text = (out or '') + (('\n' + err) if err else '')
        if rc != 0:
            return fail('compose %s 失败：%s' % (op.replace('stack_', ''),
                                                (err or out or '未知错误')[:600]), 'COMPOSE_FAIL')
        cn = {'stack_up': '已启动', 'stack_down': '已停止并移除', 'stack_restart': '已重启',
              'stack_pull': '已拉取镜像', 'stack_ps': '已获取状态'}[op]
        return ok({'text': text.strip(), 'name': name}, '项目 %s %s' % (name, cn))

    if op == 'stack_delete':
        try:
            name = _docker_validate_stack_name(p.get('name'))
        except ValidateError as e:
            return fail(e.msg_cn, 'BAD_NAME')
        d2 = os.path.realpath(os.path.join(DOCKER_STACK_DIR, name))
        root = os.path.realpath(DOCKER_STACK_DIR)
        # 防目录穿越：必须真的在 stack 目录之下
        if not d2.startswith(root + os.sep):
            return fail('项目路径不合法，已拒绝删除', 'BAD_PATH')
        if not os.path.isdir(d2):
            return fail('项目 %s 不存在' % name, 'NOT_FOUND')
        if p.get('confirm') != name:
            return fail('请确认：需输入项目名「%s」以完成删除' % name, 'NEED_CONFIRM')
        shutil.rmtree(d2, ignore_errors=True)
        return ok({}, '项目 %s 的配置文件已删除（容器请用「停止并移除」处理）' % name)

    return fail('不支持的 Docker 操作：%s' % op, 'BAD_OP')


# =====================================================================
# Docker 引擎配置（daemon.json）—— #71
#
# 三件事：
#   1) IPv6 一键开关：ipv6 + fixed-cidr-v6 + ip6tables（+ 可选的 experimental）
#   2) 国内镜像源切换：预设列表 + 自定义源 + 一键测速
#   3) 顺手把「日志滚动 / live-restore / 默认网桥网段」也纳管 —— 前两项对小硬盘
#      和「重启守护别把容器一起杀掉」很关键，第三项专治 docker0 与局域网网段打架。
#
# 两条铁律：
#   * 用户自己在 daemon.json 里写的、本页不管的键（data-root / insecure-registries /
#     storage-driver …）一律原样保留。静默丢别人的配置是最糟的体验。
#   * 保存只写盘，绝不自动重启 docker。重启要用户显式点按钮 + 二次确认 ——
#     重启会中断所有容器、重建 docker0 网桥、重插 iptables/nft 链，
#     对一台正在跑服务的路由器来说这是重动作。
# =====================================================================

DCFG_DIR = '/etc/docker'
DCFG_FILE = '/etc/docker/daemon.json'
DCFG_CONF = '/etc/drouter/dcfg.json'
DCFG_BAK_DIR = '/var/lib/drouter/dcfg-backup'
DCFG_BAK_KEEP = 5

# 本页负责写入的键。保存时会先把这些键从结果里剔除，再按开关重新写入，
# 其余键原样保留。
DCFG_MANAGED = ('ipv6', 'fixed-cidr-v6', 'ip6tables', 'experimental',
                'registry-mirrors', 'log-driver', 'log-opts',
                'live-restore', 'bip')

DCFG_DEFAULTS = {
    'ipv6': False,          # 默认关：开了却没配好路由，容器反而上不了网
    'fixed_cidr_v6': '',    # 空 = 首次开启时自动生成一个 ULA 私有段
    'experimental': False,  # 只有 Docker 24/25 才需要它来启用 ip6tables
    'mirrors': [],          # 选中的预设镜像源 key 列表（空 = 直连官方）
    'mirror_custom': '',    # 自定义镜像源地址
    'log_rotate': True,     # 默认开：容器日志是撑爆小硬盘的头号元凶
    'log_max_size': '10m',
    'log_max_file': '3',
    'live_restore': True,   # 默认开：重启 dockerd 时别把容器一起杀掉
    'bip': '',              # 空 = 不写，用 Docker 默认的 172.17.0.1/16
}

# 预设镜像源。url 里带 '<' 的是占位地址（需要用户填自己的专属 ID），不参与测速。
DCFG_MIRRORS = [
    {'key': 'tencent', 'name': '腾讯云', 'url': 'https://mirror.ccs.tencentyun.com',
     'note': '腾讯云公共镜像源，长期稳定，无需登录。'},
    {'key': 'daocloud', 'name': 'DaoCloud', 'url': 'https://docker.m.daocloud.io',
     'note': 'DaoCloud 公共加速节点，国内可用性较好。'},
    {'key': 'netease', 'name': '网易', 'url': 'https://hub-mirror.c.163.com',
     'note': '网易的老牌镜像源，速度稳定但偶尔同步滞后。'},
    {'key': 'ustc', 'name': '中国科学技术大学', 'url': 'https://docker.mirrors.ustc.edu.cn',
     'note': '教育网与电信链路表现好，高峰期偶尔限速。'},
    {'key': 'nju', 'name': '南京大学', 'url': 'https://docker.nju.edu.cn',
     'note': '南大开源镜像站，教育网用户优先。'},
    {'key': 'sjtug', 'name': '上海交通大学', 'url': 'https://docker.mirrors.sjtug.sjtu.edu.cn',
     'note': '上海交大镜像站，华东地区较快。'},
    {'key': 'aliyun', 'name': '阿里云（需填专属地址）',
     'url': 'https://<你的ID>.mirror.aliyuncs.com',
     'note': '阿里云不再提供通用加速地址。登录容器镜像服务控制台，'
             '在「镜像加速器」里拿到形如 https://xxxx.mirror.aliyuncs.com 的专属地址，'
             '填到下面的自定义源里。'},
]


def _dcfg_gen_ula():
    """随机生成一个 ULA 私有 /48 前缀（RFC 4193）：fd + 40 bit 随机数。

    为什么默认用私有段而不是公网段：容器拿到公网 v6 地址后，还需要上游路由器
    做 NDP 代理或下发前缀委派才能真正上网，家用环境多半没这条件；而私有段
    至少能保证「容器之间、容器与宿主机之间」的 v6 互通是立刻可用的。
    """
    b = os.urandom(5)
    return 'fd%02x:%04x:%04x::/48' % (b[0], (b[1] << 8) | b[2], (b[3] << 8) | b[4])


def _dcfg_load():
    cfg = dict(DCFG_DEFAULTS)
    try:
        if os.path.isfile(DCFG_CONF):
            with open(DCFG_CONF, encoding='utf-8') as f:
                d = json.load(f) or {}
            for k in DCFG_DEFAULTS:
                if k in d:
                    cfg[k] = d[k]
    except Exception:
        pass
    m = cfg.get('mirrors')
    if isinstance(m, str):                       # 兼容早期「单选项」的存档
        m = [m] if m else []
    if not isinstance(m, list):
        m = []
    cfg['mirrors'] = [str(x) for x in m]
    return cfg


def _dcfg_save(cfg):
    _atomic_write(DCFG_CONF,
                  json.dumps(cfg, ensure_ascii=False, indent=2) + '\n')


def _dcfg_read_file():
    """读现有 daemon.json。返回 (dict|None, raw, err)。

    解析失败时返回 None —— 上层会拦住保存，绝不拿一个空 dict 覆盖掉用户的文件。
    """
    if not os.path.isfile(DCFG_FILE):
        return None, '', ''
    try:
        with open(DCFG_FILE, encoding='utf-8') as f:
            raw = f.read()
    except Exception as e:
        return None, '', '读取 %s 失败：%s' % (DCFG_FILE, e)
    if not raw.strip():
        return {}, raw, ''
    try:
        d = json.loads(raw)
    except Exception as e:
        return None, raw, ('%s 不是合法的 JSON（%s）。本页不会覆盖它 —— '
                           '请先手工修正或重命名该文件。' % (DCFG_FILE, e))
    if not isinstance(d, dict):
        return None, raw, '%s 的顶层不是 JSON 对象，本页不会覆盖它。' % DCFG_FILE
    return d, raw, ''


def _dcfg_check_v6_prefix(s):
    """校验 fixed-cidr-v6。返回 (ok, cidr, err, warn)"""
    s = (s or '').strip()
    if not s:
        return False, '', 'IPv6 网段不能为空', ''
    if '/' not in s:
        return False, s, '要写成「前缀/长度」的形式，例如 fd12:3456:789a::/64', ''
    try:
        net = ipaddress.IPv6Network(s, strict=False)
    except Exception:
        return False, s, 'IPv6 网段不合法：%s' % s, ''
    if net.prefixlen < 48 or net.prefixlen > 64:
        return False, str(net), ('前缀长度要在 /48 – /64 之间。'
                                 'Docker 官方示例用的是 /64，最稳妥'), ''
    warn = ''
    if net.prefixlen != 64:
        warn = '你用的是 /%d。Docker 官方示例与绝大多数教程都用 /64，' \
               '非 /64 在个别版本上会起不来，出问题先改回 /64 试试' % net.prefixlen
    if not net.is_private:
        warn = ((warn + '；') if warn else '') + \
            ('这是公网段 %s。容器拿到公网 v6 地址后，还需要上游路由器做 NDP 代理 '
             '或下发前缀委派才能真正上网；家用环境建议改用 fd00::/8 开头的私有段' % net)
    return True, str(net), '', warn


def _dcfg_check_bip(s):
    """校验默认网桥网段 bip。返回 (ok, cidr, err, warn)"""
    s = (s or '').strip()
    if not s:
        return True, '', '', ''
    try:
        net = ipaddress.IPv4Network(s, strict=False)
    except Exception:
        return False, s, '网桥网段不合法（要写成 172.17.0.1/16 这种形式）', ''
    if net.prefixlen < 8 or net.prefixlen > 30:
        return False, str(net), '网桥网段的前缀长度要在 /8 – /30 之间', ''
    warn = ''
    if _dcfg_subnet_in_use(net):
        warn = ('这个网段和本机现有网卡的网段重叠，容器网络可能和局域网打架。'
                '换一个不冲突的私网段（例如 172.31.0.2/16）更保险')
    return True, str(net), '', warn


def _dcfg_parse_ipv4_addrs(text):
    """从 `ip -o -4 addr` 的输出里抽出所有 IPv4 网段。纯函数，便于单测。"""
    out = []
    for line in (text or '').splitlines():
        for tok in line.split():
            if '/' in tok and tok.split('/')[0].count('.') == 3:
                try:
                    out.append(ipaddress.IPv4Network(tok, strict=False))
                except Exception:
                    pass
    return out


def _dcfg_subnet_in_use(net, addrs=None):
    if addrs is None:
        rc, out, _e = sh(['ip', '-o', '-4', 'addr', 'show'], timeout=10)
        addrs = _dcfg_parse_ipv4_addrs(out if rc == 0 else '')
    try:
        return any(net.overlaps(x) for x in (addrs or []))
    except Exception:
        return False


def _dcfg_check_mirror_url(u):
    """校验镜像源地址。返回 (ok, url, err)"""
    u = (u or '').strip().rstrip('/')
    if not u:
        return False, '', '镜像源地址不能为空'
    if '://' not in u:
        u = 'https://' + u
    if not u.startswith('https://'):
        return False, u, ('镜像源只支持 https://。Docker 默认拒绝明文 http 的镜像源，'
                          '除非你另外配了 insecure-registries（不建议）')
    if re.search(r'[\s"\'\\]', u):
        return False, u, '镜像源地址不能包含空格、引号或反斜杠'
    if len(u) > 300:
        return False, u, '镜像源地址过长（最多 300 个字符）'
    host = u[len('https://'):].split('/')[0].split('@')[-1].split(':')[0]
    if not host or host.startswith('.') or '..' in host:
        return False, u, '镜像源主机名不合法：%s' % host
    if re.search(r'[^A-Za-z0-9.\-]', host):
        return False, u, '镜像源主机名含非法字符：%s' % host
    return True, u, ''


def _dcfg_log_opts(cfg):
    size = str(cfg.get('log_max_size') or '10m').strip().lower()
    if not re.match(r'^\d+[kmg]$', size):
        size = '10m'
    raw = cfg.get('log_max_file')
    # 注意别写成 `int(raw or 3)`：那样 0 会被当成「没填」而回落成 3，
    # 用户填 0（不保留任何旧文件）的意图就被吞掉了。
    if raw in (None, ''):
        files = 3
    else:
        try:
            files = int(raw)
        except Exception:
            files = 3
    files = max(1, min(files, 20))
    return {'max-size': size, 'max-file': str(files)}


def _dcfg_build(cfg, existing=None):
    """把 drouter 的开关状态合并进现有 daemon.json。返回 (dict, errs, warns)。

    合并策略：先把「本页不管的键」按原顺序抄过来，再按固定顺序写入受管的键。
    """
    d = {}
    if isinstance(existing, dict):
        for k, v in existing.items():
            if k not in DCFG_MANAGED:
                d[k] = v
    errs, warns = [], []

    # ---- IPv6 ----
    if cfg.get('ipv6'):
        pfx = (cfg.get('fixed_cidr_v6') or '').strip() or _dcfg_gen_ula()
        okp, pfx2, e, w = _dcfg_check_v6_prefix(pfx)
        if not okp:
            errs.append(e)
        else:
            if w:
                warns.append(w)
            d['ipv6'] = True
            d['fixed-cidr-v6'] = pfx2
            d['ip6tables'] = True
            # experimental 只在 Docker 24/25 上是启用 ip6tables 的前提；
            # 26 起 ip6tables 已转正，多写这一项在个别版本上反而会报错。
            if cfg.get('experimental'):
                d['experimental'] = True

    # ---- 镜像源 ----
    urls = []
    for key in (cfg.get('mirrors') or []):
        for m in DCFG_MIRRORS:
            if m['key'] == key:
                if m.get('url') and '<' not in m['url'] and m['url'] not in urls:
                    urls.append(m['url'])
                break
    cu = (cfg.get('mirror_custom') or '').strip()
    if cu:
        oku, u2, e = _dcfg_check_mirror_url(cu)
        if not oku:
            errs.append(e)
        elif u2 not in urls:
            urls.append(u2)
    if urls:
        d['registry-mirrors'] = urls

    # ---- 日志滚动 ----
    if cfg.get('log_rotate'):
        d['log-driver'] = 'json-file'
        d['log-opts'] = _dcfg_log_opts(cfg)

    # ---- live-restore ----
    if cfg.get('live_restore'):
        d['live-restore'] = True

    # ---- 默认网桥网段 ----
    bip = (cfg.get('bip') or '').strip()
    if bip:
        okb, b2, e, w = _dcfg_check_bip(bip)
        if not okb:
            errs.append(e)
        else:
            if w:
                warns.append(w)
            d['bip'] = b2
    return d, errs, warns


def _dcfg_render(d):
    return json.dumps(d, ensure_ascii=False, indent=2) + '\n'


def _dcfg_backups():
    out = []
    if not os.path.isdir(DCFG_BAK_DIR):
        return out
    for fn in sorted(os.listdir(DCFG_BAK_DIR), reverse=True):
        if not (fn.startswith('daemon-') and fn.endswith('.json')):
            continue
        p = os.path.join(DCFG_BAK_DIR, fn)
        try:
            st = os.stat(p)
        except Exception:
            continue
        out.append({'file': fn, 'path': p, 'size': st.st_size,
                    'mtime': datetime.fromtimestamp(st.st_mtime)
                    .strftime('%Y-%m-%d %H:%M:%S')})
    return out


def _dcfg_prune_backups():
    try:
        fs = [f for f in os.listdir(DCFG_BAK_DIR)
              if f.startswith('daemon-') and f.endswith('.json')]
        fs.sort(reverse=True)
        for f in fs[DCFG_BAK_KEEP:]:
            os.remove(os.path.join(DCFG_BAK_DIR, f))
    except Exception:
        pass


def _dcfg_backup():
    """备份现有 daemon.json。返回备份路径（没有可备份的东西则返回 ''）。"""
    if not os.path.isfile(DCFG_FILE):
        return ''
    try:
        os.makedirs(DCFG_BAK_DIR, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d-%H%M%S')
        dst = os.path.join(DCFG_BAK_DIR, 'daemon-%s.json' % ts)
        shutil.copy2(DCFG_FILE, dst)
        _dcfg_prune_backups()
        return dst
    except Exception:
        return ''


def _dcfg_docker_installed():
    return bool(shutil.which('docker')) or \
        any(os.path.isfile(x) for x in ('/usr/bin/docker', '/usr/local/bin/docker'))


def _dcfg_service_state():
    if not _dcfg_docker_installed():
        return 'not-installed', 'not-installed'
    rc, act, _e = sh(['systemctl', 'is-active', 'docker'], timeout=10)
    rc2, en, _e2 = sh(['systemctl', 'is-enabled', 'docker'], timeout=10)
    return (act.strip() if rc == 0 else 'unknown'), (en.strip() if rc2 == 0 else 'unknown')


def _dcfg_mirror_test(keys=None, custom=''):
    """逐个探测镜像源可达性。返回 (list, err)。

    判定标准：Registry v2 的 /v2/ 探针，返回 200（匿名可读）或 401（需要登录）
    都说明链路是通的；000 表示根本连不上。
    """
    if not shutil.which('curl'):
        return [], '缺少 curl，无法测速。请到「系统 → 依赖自检与安装」安装。'
    targets = []
    for m in DCFG_MIRRORS:
        if keys and m['key'] not in keys:
            continue
        if '<' in (m.get('url') or ''):
            targets.append({'key': m['key'], 'name': m['name'], 'url': m['url'],
                            'ok': False, 'code': '', 'ms': 0, 'skipped': True,
                            'reason': '占位地址，需换成你自己的专属地址后再测'})
            continue
        if not m.get('url'):
            continue
        targets.append({'key': m['key'], 'name': m['name'], 'url': m['url'],
                        'ok': False, 'code': '', 'ms': 0, 'skipped': False, 'reason': ''})
    cu = (custom or '').strip()
    if cu and not (keys and 'custom' not in keys):
        oku, u2, e = _dcfg_check_mirror_url(cu)
        if not oku:
            targets.append({'key': 'custom', 'name': '自定义源', 'url': cu,
                            'ok': False, 'code': '', 'ms': 0, 'skipped': True,
                            'reason': e})
        else:
            targets.append({'key': 'custom', 'name': '自定义源', 'url': u2,
                            'ok': False, 'code': '', 'ms': 0, 'skipped': False,
                            'reason': ''})
    for t in targets:
        if t.get('skipped'):
            continue
        # --noproxy '*'：测的是 dockerd 自己的直连路径，不该被环境里的代理掩盖
        rc, out, err = sh(['curl', '-sS', '-o', '/dev/null', '--noproxy', '*',
                           '-m', '8', '-w', '%{http_code} %{time_total}',
                           t['url'].rstrip('/') + '/v2/'], timeout=15)
        txt = (out or '').strip().split()
        code = txt[0] if txt else '000'
        try:
            ms = int(round(float(txt[1]) * 1000)) if len(txt) > 1 else 0
        except Exception:
            ms = 0
        t['code'] = code
        t['ms'] = ms
        if code in ('200', '401'):
            t['ok'] = True
            t['reason'] = '可用（%s，%d ms）' % (code, ms)
        elif code == '000':
            t['reason'] = '连不上（超时 / DNS 失败 / 被墙）'
        else:
            t['reason'] = '返回 %s，多半不是可用的 Registry 镜像源' % code
        if rc != 0 and code == '000' and err:
            t['reason'] = '连不上：%s' % err.strip()[:120]
    return targets, ''


def _dcfg_status():
    cfg = _dcfg_load()
    existing, raw, ferr = _dcfg_read_file()
    d, errs, warns = _dcfg_build(cfg, existing)
    unmanaged = sorted([k for k in (existing or {}) if k not in DCFG_MANAGED])
    inst = _dcfg_docker_installed()
    ver = ''
    if inst:
        rc, out, _e = sh([_docker_bin(), '--version'], timeout=12)
        if rc == 0:
            ver = (out or '').strip()
    active, enabled = _dcfg_service_state()
    return ok({
        'cfg': cfg,
        'file': DCFG_FILE,
        'exists': os.path.isfile(DCFG_FILE),
        'current': raw,
        'current_parsed': existing if isinstance(existing, dict) else None,
        'parse_error': ferr,
        'unmanaged': unmanaged,
        'preview': _dcfg_render(d),
        'build_errs': errs,
        'build_warns': warns,
        'installed': inst,
        'version': ver,
        'active': active,
        'enabled': enabled,
        'mirrors': DCFG_MIRRORS,
        'backups': _dcfg_backups(),
        'bak_dir': DCFG_BAK_DIR,
        'build_mode': in_build_mode(),
        'dirty': bool(raw) and raw.strip() != _dcfg_render(d).strip(),
    }, '已读取 Docker 引擎配置')


def _dcfg_save_op(p):
    p = p or {}
    cfg = _dcfg_load()
    for k in ('ipv6', 'experimental', 'log_rotate', 'live_restore'):
        if p.get(k) is not None:
            cfg[k] = bool(p[k])
    for k in ('fixed_cidr_v6', 'mirror_custom', 'bip', 'log_max_size', 'log_max_file'):
        if p.get(k) is not None:
            cfg[k] = str(p[k]).strip()
    if p.get('mirrors') is not None:
        m = p['mirrors']
        if isinstance(m, str):
            m = [m] if m else []
        if not isinstance(m, list):
            return fail('镜像源列表格式不合法', 'BAD_MIRRORS')
        known = set(x['key'] for x in DCFG_MIRRORS)
        bad = [x for x in m if str(x) not in known]
        if bad:
            return fail('未知的镜像源：%s' % '、'.join(map(str, bad)), 'BAD_MIRRORS')
        cfg['mirrors'] = [str(x) for x in m][:8]

    existing, raw, ferr = _dcfg_read_file()
    if ferr:
        return fail(ferr, 'BAD_FILE')
    d, errs, warns = _dcfg_build(cfg, existing)
    if errs:
        return fail('；'.join(errs), 'BAD_VALUE')

    text = _dcfg_render(d)
    bak = _dcfg_backup()               # 备份与写盘在任何模式下都做（都不碰网络）
    try:
        os.makedirs(DCFG_DIR, exist_ok=True)
        _atomic_write(DCFG_FILE, text)
    except Exception as e:
        return fail('写入 %s 失败：%s' % (DCFG_FILE, e), 'WRITE_FAIL')
    _dcfg_save(cfg)
    log('warn', 'docker', 'DCFG_SAVE',
        'Docker 引擎配置已写入（IPv6=%s，镜像源 %d 个，日志滚动=%s）'
        % (cfg.get('ipv6'), len(d.get('registry-mirrors') or []),
           cfg.get('log_rotate')),
        {'backup': bak, 'warns': warns, 'file': DCFG_FILE})
    msg = '已写入 %s。Docker 需要重启后才会采用新配置。' % DCFG_FILE
    if bak:
        msg += '（原文件已备份）'
    if warns:
        msg += '；提示：' + '；'.join(warns)
    return ok({'cfg': cfg, 'content': text, 'backup': bak,
               'warns': warns, 'installed': _dcfg_docker_installed()}, msg)


def _dcfg_restart_op(p):
    p = p or {}
    if str(p.get('confirm')) not in ('1', 'true', 'True'):
        return fail('重启 Docker 会中断所有容器，请先确认', 'NEED_CONFIRM')
    if in_build_mode():
        return fail('当前处于【构建保护模式】，已拒绝重启 Docker', 'BUILD_MODE')
    if not _dcfg_docker_installed():
        return fail('这台机器上没有安装 Docker，无需重启', 'NO_DOCKER')
    rc, out, err = sh(['systemctl', 'restart', 'docker'], timeout=180)
    if rc != 0:
        return fail('重启 Docker 失败：%s' % ((err or out or '未知错误')[:400]),
                    'RESTART_FAIL')
    rc2, act, _e = sh(['systemctl', 'is-active', 'docker'], timeout=15)
    log('warn', 'docker', 'DCFG_RESTART', 'Docker 服务已重启以应用 daemon.json',
        {'active': act if rc2 == 0 else 'unknown'})
    return ok({'active': act if rc2 == 0 else 'unknown'},
              'Docker 已重启，新配置已生效（当前状态：%s）'
              % (act if rc2 == 0 else '未知'))


def act_dcfg(p):
    """Docker 引擎配置：status / preview / save / restore / ula / mirror_test / restart"""
    p = p or {}
    op = str(p.get('op') or 'status')
    if op == 'status':
        return _dcfg_status()
    if op == 'preview':
        cfg = _dcfg_load()
        for k in DCFG_DEFAULTS:
            if p.get(k) is not None:
                cfg[k] = p[k]
        existing, _raw, ferr = _dcfg_read_file()
        d, errs, warns = _dcfg_build(cfg, existing)
        return ok({'preview': _dcfg_render(d), 'errs': errs, 'warns': warns})
    if op == 'ula':
        return ok({'ula': _dcfg_gen_ula()}, '已生成一个新的私有 IPv6 段')
    if op == 'mirror_test':
        keys = p.get('keys')
        if keys is not None and not isinstance(keys, list):
            keys = [keys]
        res, err = _dcfg_mirror_test(keys, p.get('custom') or '')
        if err:
            return fail(err, 'NO_CURL')
        return ok({'results': res}, '测速完成（200 / 401 都表示链路可用）')
    if op == 'save':
        return _dcfg_save_op(p)
    if op == 'restore':
        fn = (p.get('file') or '').strip()
        if not re.match(r'^daemon-\d{8}-\d{6}\.json$', fn):
            return fail('备份文件名不合法', 'BAD_FILE')
        root = os.path.realpath(DCFG_BAK_DIR)
        src = os.path.realpath(os.path.join(DCFG_BAK_DIR, fn))
        if not src.startswith(root + os.sep) or not os.path.isfile(src):
            return fail('备份 %s 不存在' % fn, 'NOT_FOUND')
        _dcfg_backup()                       # 还原前先把当前文件也存一份
        try:
            os.makedirs(DCFG_DIR, exist_ok=True)
            shutil.copy2(src, DCFG_FILE)
            with open(src, encoding='utf-8') as f:
                text = f.read()
        except Exception as e:
            return fail('还原失败：%s' % e, 'RESTORE_FAIL')
        log('warn', 'docker', 'DCFG_RESTORE',
            'daemon.json 已从备份 %s 还原' % fn, {'file': fn})
        return ok({'content': text}, '已从 %s 还原（当前文件也另存了一份备份）' % fn)
    if op == 'restart':
        return _dcfg_restart_op(p)
    return fail('不支持的引擎配置操作：%s' % op, 'BAD_OP')


# =====================================================================
# 打印服务（CUPS / USB RAW 直通，互斥二选一）—— #73
#
# 两种模式的本质是「谁独占 USB 打印机」：
#   * CUPS 模式：完整的打印服务器。走 IPP/IPPS，支持免驱（IPP Everywhere）、
#     AirPrint、Mopria，有队列管理、Web 管理界面，还能把网络上已有的打印机
#     （比如 socket://192.168.7.231:9100 这类）收进来再共享出去。
#   * RAW 直通模式：把 USB 打印机原样映射成一个 TCP 9100 端口（JetDirect /
#     socket 打印）。路由器不碰数据，客户端装自己的原厂驱动直打。
#
# 为什么必须二选一：USB 打印机同一时刻只能有一个主人。CUPS 侧有 ipp-usb
# （走 libusb），RAW 侧则要 usblp 内核模块提供的 /dev/usb/lp0，两者抢同一个
# 设备，同时开的结果是谁都打不出来。切模式时本页会把对面停干净。
#
# 这台机器的实测起点：CUPS 2.4.10 已在跑，但 Listen localhost:631（局域网
# 根本连不上）、队列 Shared No（没共享出去）—— 等于装了却完全没用上。
# =====================================================================

PRINT_CONF = '/etc/drouter/print.conf'
PRINT_CUPSD = '/etc/cups/cupsd.conf'
PRINT_BAK_DIR = '/var/lib/drouter/print-backup'
PRINT_BAK_KEEP = 5
PRINT_RAW_SERVICE = 'drouter-printer-raw.service'
PRINT_RAW_UNIT = '/etc/systemd/system/' + PRINT_RAW_SERVICE
PRINT_RAW_WRAPPER = '/opt/drouter/scripts/printer-raw.sh'
PRINT_USB_CLASS_DIR = '/sys/bus/usb/devices'
PRINT_USB_IFCLASS_PRINTER = ('07', '7')     # USB Printer Class
PRINT_TESTPAGE = '/usr/share/cups/data/testprint'
PRINT_PKGS = ['cups', 'cups-client', 'cups-daemon', 'cups-browsed']
# 后端 URI 白名单。dnssd / ipp 这些 URI 里带 %20 之类的转义是合法的，
# 但绝不能有裸空格或 shell 元字符（lpadmin 走列表调用本来就不过 shell，
# 这里再卡一道纯粹是为了不让奇怪的值进到 printers.conf 里）。
PRINT_URI_SCHEMES = ('ipp://', 'ipps://', 'http://', 'https://', 'socket://',
                     'usb://', 'lpd://', 'dnssd://', 'file://', 'smb://',
                     'implicitclass://', 'hp://', 'mdns://')
PRINT_NAME_RE = r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$'

PRINT_DEFAULTS = {
    'mode': 'cups',             # off | cups | raw
    'cups': {
        'listen': 'lan',        # lan（局域网可访问）| local（只听 127.0.0.1）
        'share': True,          # 把队列共享出去
        'web_iface': True,      # CUPS 自带的 Web 管理界面
        'remote_admin': False,  # 允许从局域网做管理操作 —— 默认关
        'browsed': True,        # cups-browsed：自动发现网络打印机
    },
    'raw': {
        'device': '/dev/usb/lp0',
        'port': 9100,
        'bind': '0.0.0.0',
    },
}

PRINT_ITEMS = [
    {
        'key': 'mode', 'name': '工作模式（三选一）',
        'why': '决定这台路由器用哪种方式提供打印服务。切换时会自动把另一种'
               '模式的服务停掉，避免两边抢同一个 USB 打印机。',
        'impact': ['CUPS 模式：功能最全，手机 / 电脑免驱就能打，但要多跑一个守护进程',
                   'RAW 直通模式：最省资源、兼容性最好（驱动全在客户端），'
                   '但客户端必须自己装驱动，也没有队列管理',
                   '关闭：把所有打印服务都停掉，端口不再监听',
                   '没有插 USB 打印机时，RAW 模式没有意义 —— 页面会如实提示'],
    },
    {
        'key': 'listen', 'name': '允许局域网访问',
        'why': 'CUPS 出厂默认只听 127.0.0.1:631，也就是只有机器自己能连，'
               '局域网里的电脑手机全都连不上。这是最常见的「装了 CUPS 却用不了」的原因。',
        'impact': ['打开后 CUPS 会监听全部地址的 631 端口',
                   '本页会把访问范围限制在 @LOCAL（直连网段），'
                   'WAN 侧的机器访问不到 —— 不会把打印机暴露到公网',
                   '将来这台机器真正接管路由后，如果防火墙改了策略，'
                   '需要确认入站仍然放行 631'],
    },
    {
        'key': 'share', 'name': '共享打印机',
        'why': '决定队列要不要对外发布。不共享的话，队列只有本机能用，'
               '局域网里的其他设备根本看不见它。',
        'impact': ['打开后会通过 mDNS（avahi）在局域网里广播，'
                   'iPhone / Android / Windows / macOS 才能自动发现',
                   '依赖 avahi-daemon。它没运行时能打印但发现不了，页面会提示'],
    },
    {
        'key': 'web_iface', 'name': 'CUPS Web 管理界面',
        'why': 'CUPS 自带的网页，可以管理队列、看任务、改设置。',
        'impact': ['关掉不影响打印，只是 631 上的网页打不开',
                   '开着方便排障；讲究的话可以在打完配置后关掉'],
    },
    {
        'key': 'remote_admin', 'name': '允许远程管理',
        'why': '允许局域网里的其他机器通过 CUPS 增删队列、改设置。',
        'impact': ['默认关闭 —— 管理操作还是在本机做更安全',
                   '打开后同网段的任何人都能改打印配置，只在可信网络里开'],
    },
    {
        'key': 'browsed', 'name': 'cups-browsed 自动发现',
        'why': '在局域网里自动搜索网络打印机，并为每台生成一个本地队列。'
               '你现在那台 HP OfficeJet Pro 7740 就是靠它发现的。',
        'impact': ['关掉后，已自动生成的队列会消失（手工添加的队列不受影响）',
                   '网络上打印机很多时会生成一堆队列，看着乱，这时可以关掉',
                   '它也会把本机的队列再广播出去，供其他机器发现'],
    },
]

PRINT_DRIVERS = [
    {'key': 'everywhere', 'name': '自动（IPP Everywhere，免驱）', 'ppd': 'everywhere',
     'note': '推荐。2015 年后的网络打印机、AirPrint / Mopria 设备基本都支持。'
             '路由器不做格式转换，由打印机自己解析。'},
    {'key': 'raw', 'name': 'Raw（原样转发，不做任何转换）', 'ppd': 'raw',
     'note': '客户端必须自己装好这台打印机的驱动，并把渲染好的数据直接发过来。'
             '适合老式打印机、或者驱动在路由器上装不上的情况。'},
]


def _print_load():
    cfg = {'cups': dict(PRINT_DEFAULTS['cups']), 'raw': dict(PRINT_DEFAULTS['raw'])}
    cfg['mode'] = PRINT_DEFAULTS['mode']
    try:
        if os.path.isfile(PRINT_CONF):
            with open(PRINT_CONF, encoding='utf-8') as f:
                d = json.load(f) or {}
            if d.get('mode') in ('off', 'cups', 'raw'):
                cfg['mode'] = d['mode']
            for grp in ('cups', 'raw'):
                g = dict(cfg[grp])
                g.update(d.get(grp) or {})
                cfg[grp] = g
    except Exception:
        pass
    return cfg


def _print_save(cfg):
    _atomic_write(PRINT_CONF,
                  json.dumps(cfg, ensure_ascii=False, indent=2) + '\n')


def _print_backup():
    """备份 cupsd.conf（不存在就不备份）。返回备份路径或 ''。"""
    if not os.path.isfile(PRINT_CUPSD):
        return ''
    try:
        os.makedirs(PRINT_BAK_DIR, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d-%H%M%S')
        dst = os.path.join(PRINT_BAK_DIR, 'cupsd-%s.conf' % ts)
        shutil.copy2(PRINT_CUPSD, dst)
        _print_prune_backups()
        return dst
    except Exception:
        return ''


def _print_prune_backups():
    try:
        fs = sorted([f for f in os.listdir(PRINT_BAK_DIR)
                     if f.startswith('cupsd-') and f.endswith('.conf')], reverse=True)
        for f in fs[PRINT_BAK_KEEP:]:
            os.remove(os.path.join(PRINT_BAK_DIR, f))
    except Exception:
        pass


def _print_backups():
    out = []
    if not os.path.isdir(PRINT_BAK_DIR):
        return out
    for fn in sorted(os.listdir(PRINT_BAK_DIR), reverse=True):
        if not (fn.startswith('cupsd-') and fn.endswith('.conf')):
            continue
        p = os.path.join(PRINT_BAK_DIR, fn)
        try:
            st = os.stat(p)
        except Exception:
            continue
        out.append({'file': fn, 'path': p, 'size': st.st_size,
                    'mtime': datetime.fromtimestamp(st.st_mtime)
                    .strftime('%Y-%m-%d %H:%M:%S')})
    return out


def _print_parse_lsusb(text):
    """解析 lsusb 输出。纯函数。

    'Bus 001 Device 002: ID 0627:0001 Adomax Technology Co., Ltd QEMU Tablet'
      → {'bus':'001','dev':'002','vid':'0627','pid':'0001','name':'...'}
    """
    out = []
    rx = re.compile(r'^Bus\s+(\S+)\s+Device\s+(\S+):\s+ID\s+([0-9a-fA-F]{4})'
                    r':([0-9a-fA-F]{4})\s*(.*)$')
    for ln in (text or '').splitlines():
        m = rx.match(ln.strip())
        if not m:
            continue
        out.append({'bus': m.group(1), 'dev': m.group(2),
                    'vid': m.group(3).lower(), 'pid': m.group(4).lower(),
                    'name': (m.group(5) or '').strip()})
    return out


def _print_usb_ifclass(devices_dir=PRINT_USB_CLASS_DIR):
    """扫描 sysfs，返回打印机类（bInterfaceClass = 07）的 USB 设备名列表。

    07 是 USB 规范里的 Printer Class。只认它，免得把 U 盘、键鼠、摄像头
    全列成「打印机」。
    """
    out = []
    try:
        names = sorted(os.listdir(devices_dir))
    except Exception:
        return out
    for n in names:
        d = os.path.join(devices_dir, n)
        if not os.path.isdir(d):
            continue
        try:
            subs = sorted(os.listdir(d))
        except Exception:
            continue
        hit = False
        for s in subs:
            # 接口目录的命名规则是「设备名:配置.接口」，例如 1-1:1.0
            if not s.startswith(n + ':'):
                continue
            try:
                with open(os.path.join(d, s, 'bInterfaceClass'),
                          encoding='utf-8') as f:
                    v = f.read().strip().lower()
            except Exception:
                continue
            if v in PRINT_USB_IFCLASS_PRINTER:
                hit = True
                break
        if hit:
            out.append(n)
    return out


def _print_sysfs_attr(path):
    try:
        with open(path, encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return ''


def _print_usb_printers():
    """列出 USB 打印机：sysfs 的 Printer Class + /dev/usb/lp* 节点。"""
    out = []
    rc, lsusb_out, _e = sh(['lsusb'], timeout=15)
    by_id = {}
    for u in _print_parse_lsusb(lsusb_out if rc == 0 else ''):
        by_id[(u['vid'], u['pid'])] = u
    nodes = sorted(glob.glob('/dev/usb/lp*'))
    for dev in _print_usb_ifclass():
        base = os.path.join(PRINT_USB_CLASS_DIR, dev)
        vid = _print_sysfs_attr(os.path.join(base, 'idVendor')).lower()
        pid = _print_sysfs_attr(os.path.join(base, 'idProduct')).lower()
        info = by_id.get((vid, pid)) or {}
        out.append({
            'sys': dev, 'vid': vid, 'pid': pid,
            'name': info.get('name') or _print_sysfs_attr(os.path.join(base, 'product'))
                    or ('%s:%s' % (vid, pid)),
            'bus': info.get('bus') or '', 'dev': info.get('dev') or '',
        })
    # 已经出现 /dev/usb/lpN 但 sysfs 没扫到的（例如 usblp 模块刚加载）也补上
    known = set(o['sys'] for o in out)
    for i, node in enumerate(nodes):
        if node in known:
            continue
        out.append({'sys': '', 'vid': '', 'pid': '', 'name': node,
                    'bus': '', 'dev': '', 'node': node})
    for i, o in enumerate(out):
        o['node'] = o.get('node') or (nodes[i] if i < len(nodes) else '/dev/usb/lp0')
    return out, nodes


def _print_parse_printers_conf(text):
    """解析 /etc/cups/printers.conf。纯函数。

    为什么不用 lpstat：lpstat 的输出会被系统语言本地化（这台机器上就是
    「自从 2026年09月29日 开始接受请求」），脚本没法稳定解析。
    printers.conf 的键（Info / DeviceURI / State / Shared / Accepting）
    永远是英文，稳得多。
    """
    out = []
    cur = None
    for ln in (text or '').splitlines():
        s = ln.strip()
        if not s or s.startswith('#'):
            continue
        if s.startswith('<DefaultPrinter ') or s.startswith('<Printer '):
            tag = s[1:].split(None, 1)
            cur = {'name': (tag[1].rstrip('>') if len(tag) > 1 else ''),
                   'default': s.startswith('<DefaultPrinter')}
            continue
        if s.startswith('</Printer>') or s.startswith('</DefaultPrinter>'):
            if cur:
                out.append(cur)
            cur = None
            continue
        if cur is None or ' ' not in s:
            continue
        k, v = s.split(None, 1)
        cur[k.lower()] = v.strip()
    for q in out:
        q.setdefault('name', '')
        q['uri'] = q.get('deviceuri', '')
        q['info'] = q.get('info', '')
        q['location'] = q.get('location', '')
        q['make_model'] = q.get('makemodel', '')
        q['state'] = q.get('state', '')
        q['accepting'] = (q.get('accepting', '').lower() == 'yes')
        q['shared'] = (q.get('shared', '').lower() == 'yes')
        q['default'] = bool(q.get('default'))
    return out


def _print_c_locale_env():
    """给子命令一个 C 语 locale 的环境。

    lpstat 的输出会被系统语言本地化（这台机器上就是「打印机 X 目前空闲」），
    脚本没法解析。强制 LC_ALL=C 就回到英文原文，解析才稳。
    """
    env = dict(os.environ)
    env['LC_ALL'] = 'C'
    env['LANG'] = 'C'
    env.pop('LANGUAGE', None)
    return env


def _print_parse_lpstat_p(text):
    """解析 `LC_ALL=C lpstat -p` 输出。纯函数。

    为什么还要看 lpstat：printers.conf 是 cupsd **延迟落盘**的 —— 刚用
    lpadmin 加完的队列，lpstat 里已经有了、文件里还没有（实测要等到 cupsd
    重启才写进去）。只认文件的话，用户刚加的队列在页面上会「加了却看不见」。

    'printer drouter-probe is idle.  enabled since Tue Sep 29 23:57:46 2026'
      → {'name':'drouter-probe','state':'idle','accepting':True}
    """
    out = []
    # 注意停用队列的输出没有「is idle」那一段（形如「printer X disabled since …」），
    # 状态那段必须是可选的，否则这类队列会直接消失。
    rx = re.compile(r'^printer\s+(\S+)\s+(.*)$')
    # 不能用 (\S+?) —— 非贪婪配上可选的 [.,] 会只吃到第一个字母（idle → i）
    sub = re.compile(r'^is\s+([^\s.,]+)[.,]?\s*(.*)$')
    for ln in (text or '').splitlines():
        m = rx.match(ln.strip())
        if not m:
            continue
        rest = (m.group(2) or '').strip()
        state = ''
        mm = sub.match(rest)
        if mm:
            state = mm.group(1)
            rest = (mm.group(2) or '').strip()
        out.append({'name': m.group(1), 'state': state,
                    'accepting': ('disabled' not in rest.lower()),
                    'enabled': ('enabled' in rest.lower())})
    return out


def _print_parse_jobs(text):
    """解析 lpstat -o 输出。纯函数。第一列永远是「队列名-任务号」，不受本地化影响。"""
    out = []
    for ln in (text or '').splitlines():
        s = ln.strip()
        if not s:
            continue
        parts = s.split()
        m = re.match(r'^(.+)-(\d+)$', parts[0])
        if not m:
            continue
        out.append({'id': parts[0], 'queue': m.group(1), 'job': m.group(2),
                    'user': parts[1] if len(parts) > 1 else '',
                    'size': parts[2] if len(parts) > 2 else '',
                    'rest': ' '.join(parts[3:])[:120]})
    return out


def _print_service(name):
    """单单元状态查询（薄封装，保留给可读性；内部走批量实现，不额外 fork）。"""
    return _svc_states((name,))[name]


def _print_patch_location(text, allow='@LOCAL'):
    """把 <Location /> 段里的 Allow 行统一换成 allow。纯函数。

    cupsctl --remote-any 会把这里写成 `Allow all`（放行一切来源）。
    对一台路由器来说太宽了 —— 换成 `Allow @LOCAL` 后只有直连网段能访问，
    WAN 侧连不上，这才是打印服务器该有的边界。
    """
    lines = (text or '').splitlines()
    out = []
    inside = False
    replaced = False
    for ln in lines:
        s = ln.strip()
        if not inside and re.match(r'^<Location\s+/>\s*$', s):
            inside = True
            out.append(ln)
            continue
        if inside and s.startswith('</Location>'):
            if not replaced:
                out.append('  Allow %s' % allow)
            inside = False
            replaced = True
            out.append(ln)
            continue
        if inside and s.startswith('Allow'):
            if replaced:
                continue                      # 去掉多余的 Allow 行
            out.append('  Allow %s' % allow)
            replaced = True
            continue
        out.append(ln)
    if not replaced:                          # 文件里压根没有 <Location />
        out += ['', '<Location />', '  Order allow,deny',
                '  Allow %s' % allow, '</Location>']
    return '\n'.join(out) + '\n'


def _print_listen_now():
    """返回 CUPS 当前实际监听的地址（从 ss 里找 631）。"""
    rc, out, _e = sh(['ss', '-lntp'], timeout=12)
    if rc != 0:
        return ''
    hits = []
    for ln in out.splitlines():
        if ':631' not in ln:
            continue
        m = re.search(r'(\S+):631\b', ln)
        if m and m.group(1) not in hits:
            hits.append(m.group(1))
    return '、'.join(hits)


def _print_lan_ip():
    rc, out, _e = sh(['ip', '-o', '-4', 'addr', 'show', 'scope', 'global'], timeout=10)
    if rc == 0:
        for ln in out.splitlines():
            m = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)/', ln)
            if m and not m.group(1).startswith('127.'):
                return m.group(1)
    return ''


def _print_cupsctl_args(cfg):
    """按配置生成 cupsctl 参数。纯函数，便于单测。"""
    c = (cfg or {}).get('cups') or {}
    args = ['cupsctl']
    args.append('--remote-any' if c.get('listen') == 'lan' else '--no-remote-any')
    args.append('--share-printers' if c.get('share') else '--no-share-printers')
    args.append('--remote-admin' if c.get('remote_admin') else '--no-remote-admin')
    args.append('WebInterface=%s' % ('Yes' if c.get('web_iface') else 'No'))
    return args


def _print_apply_cups(cfg, errs):
    """应用 CUPS 模式。返回是否成功。"""
    # cupsctl 不是直接改配置文件，而是连到 cupsd 的本地 socket 下命令 ——
    # cups 没在跑它就必然失败（「关闭」模式切回 CUPS 时正是这个情况，
    # 实测报的是「无法连接服务器：错误的文件描述符」）。所以先确保起来了。
    if _svc_states(('cups.service',))['cups.service']['active'] != 'active':
        sh(['systemctl', 'enable', 'cups.service'], timeout=20)
        rc0, _o0, e0 = sh(['systemctl', 'start', 'cups.service'], timeout=60)
        if rc0 != 0:
            errs.append('启动 CUPS 失败：%s' % ((e0 or '未知错误')[:200]))
            return False
    bak = _print_backup()
    rc, out, err = sh(_print_cupsctl_args(cfg), timeout=40)
    if rc != 0:
        errs.append('cupsctl 执行失败：%s' % ((err or out or '未知错误')[:200]))
        return False
    # cupsctl 只管开关，访问范围由我们再收紧一层
    if (cfg.get('cups') or {}).get('listen') == 'lan':
        try:
            with open(PRINT_CUPSD, encoding='utf-8') as f:
                text = f.read()
            new = _print_patch_location(text, '@LOCAL')
            if new != text:
                with open(PRINT_CUPSD, 'w', encoding='utf-8') as f:
                    f.write(new)
        except Exception as e:
            errs.append('收窄访问范围失败（已保持 cupsctl 的结果）：%s' % e)
    # 校验配置，写坏了对谁都没好处
    rc2, _o2, err2 = sh(['cupsd', '-t'], timeout=20)
    if rc2 != 0:
        errs.append('CUPS 配置校验未通过：%s' % (err2 or '')[:200])
        if bak:
            shutil.copy2(bak, PRINT_CUPSD)
            errs.append('已自动还原改动前的 cupsd.conf')
        return False
    for name in ('cups.service', 'cups.socket'):
        sh(['systemctl', 'enable', name], timeout=20)
        rc3, _o3, e3 = sh(['systemctl', 'restart', name], timeout=60)
        if rc3 != 0:
            errs.append('%s 重启失败：%s' % (name, (e3 or '')[:120]))
    return True


def _print_apply_raw(cfg, errs):
    """应用 RAW 直通模式。返回是否成功。"""
    r = cfg.get('raw') or {}
    dev = str(r.get('device') or '/dev/usb/lp0').strip()
    if not dev.startswith('/dev/'):
        errs.append('设备路径必须以 /dev/ 开头')
        return False
    # 注意别写成 int(r.get('port') or 9100)：0 是 falsy，会被当成「没填」
    # 而悄悄回落成 9100，用户填 0 却拿到 9100 是最难排查的那种错。
    raw_port = r.get('port')
    if raw_port in (None, ''):
        port = 9100
    else:
        try:
            port = int(raw_port)
        except Exception:
            errs.append('端口取值不合法')
            return False
    port = max(1, min(port, 65535))
    bind = str(r.get('bind') or '0.0.0.0').strip()
    if not re.match(r'^\d{1,3}(\.\d{1,3}){3}$', bind):
        errs.append('绑定地址不合法（填 IP，0.0.0.0 表示全部网卡）')
        return False

    if not os.path.exists(dev):
        # 没插打印机（或 usblp 模块没加载）时，配置照样写、服务照样装好，
        # 只是起不来 —— 页面会把真实状态显示出来，不藏着。
        sh(['modprobe', 'usblp'], timeout=20)
        if not os.path.exists(dev):
            errs.append('找不到 %s。USB 打印机没插、没直通进这台虚拟机，'
                        '或者内核的 usblp 模块没加载 —— 配置已保存，'
                        '插上设备后重启一次服务即可' % dev)
    # 先写单元和 wrapper，再拉起来
    body = ('#!/bin/sh\n'
            '# drouter USB 打印机 RAW 直通 —— 由 helper 自动生成，勿手改。\n'
            '# 用 GOPEN（通用 open，不走 termios）而不是 FILE（会截断），\n'
            '# 字符设备必须这么开才不会把数据写坏。\n'
            'exec /usr/bin/socat TCP-LISTEN:%d,bind=%s,fork,reuseaddr GOPEN:%s\n'
            % (port, bind, dev))
    try:
        os.makedirs(os.path.dirname(PRINT_RAW_WRAPPER), exist_ok=True)
        with open(PRINT_RAW_WRAPPER, 'w', encoding='utf-8') as f:
            f.write(body)
        os.chmod(PRINT_RAW_WRAPPER, 0o755)
        with open(PRINT_RAW_UNIT, 'w', encoding='utf-8') as f:
            f.write(PRINT_RAW_UNIT_BODY)
    except Exception as e:
        errs.append('写入 RAW 直通服务失败：%s' % e)
        return False
    sh(['systemctl', 'daemon-reload'], timeout=30)
    sh(['systemctl', 'enable', PRINT_RAW_SERVICE], timeout=20)
    rc, _o, e = sh(['systemctl', 'restart', PRINT_RAW_SERVICE], timeout=40)
    if rc != 0:
        errs.append('RAW 直通服务启动失败：%s' % (e or '')[:200])
        return False
    return True


PRINT_RAW_UNIT_BODY = (
    '[Unit]\n'
    'Description=drouter USB 打印机 RAW 直通（TCP 9100）\n'
    'Documentation=man:socat(1)\n'
    'After=network-online.target systemd-udev-settle.service\n'
    'Wants=network-online.target\n'
    '\n'
    '[Service]\n'
    'Type=simple\n'
    '# systemd 的默认 PATH 里没有 /usr/sbin，socat 虽然在 /usr/bin 下但也一并写全，\n'
    '# 免得哪天路径变了静默失败。\n'
    'Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n'
    'ExecStart=' + PRINT_RAW_WRAPPER + '\n'
    'Restart=on-failure\n'
    'RestartSec=3\n'
    '\n'
    '[Install]\n'
    'WantedBy=multi-user.target\n'
)


def _print_stop_all(errs):
    """把所有打印服务停干净（用于 mode=off 与模式切换）。"""
    for name in (PRINT_RAW_SERVICE, 'cups-browsed.service',
                 'cups.service', 'cups.socket'):
        sh(['systemctl', 'stop', name], timeout=60)
        sh(['systemctl', 'disable', name], timeout=20)


def _print_apply(cfg, errs):
    mode = cfg.get('mode')
    if mode == 'off':
        _print_stop_all(errs)
        return
    if mode == 'raw':
        # 关键：CUPS 侧的 ipp-usb 会占住 USB 打印机，不停掉 RAW 就抢不到设备
        for name in ('cups-browsed.service', 'cups.service', 'cups.socket'):
            sh(['systemctl', 'stop', name], timeout=60)
            sh(['systemctl', 'disable', name], timeout=20)
        _print_apply_raw(cfg, errs)
        return
    # mode == 'cups'
    sh(['systemctl', 'stop', PRINT_RAW_SERVICE], timeout=40)
    sh(['systemctl', 'disable', PRINT_RAW_SERVICE], timeout=20)
    _print_apply_cups(cfg, errs)
    br = (cfg.get('cups') or {}).get('browsed')
    if br:
        sh(['systemctl', 'enable', 'cups-browsed.service'], timeout=20)
        rc, _o, e = sh(['systemctl', 'restart', 'cups-browsed.service'], timeout=60)
        if rc != 0:
            errs.append('cups-browsed 启动失败：%s' % (e or '')[:120])
    else:
        sh(['systemctl', 'stop', 'cups-browsed.service'], timeout=60)
        sh(['systemctl', 'disable', 'cups-browsed.service'], timeout=20)


def _print_status():
    cfg = _print_load()
    inst = bool(shutil.which('cupsd') or os.path.isfile('/usr/sbin/cupsd'))
    # 性能：原来 4 处 _print_service 各 fork 2 次 systemctl（最坏 8 次）。
    # 这里一次批量取回 4 个单元的状态，返回值结构保持不变（active/enabled 字典）。
    _svc_names = ('cups.service', 'cups-browsed.service', PRINT_RAW_SERVICE, 'avahi-daemon')
    _svcs = _svc_states(_svc_names)
    cups = _svcs['cups.service']
    browsed = _svcs['cups-browsed.service']
    raw = _svcs[PRINT_RAW_SERVICE]
    ver = ''
    if inst:
        rc, out, _e = sh(['dpkg-query', '-W', '-f=${Version}', 'cups'], timeout=12)
        if rc == 0:
            ver = (out or '').strip()
    queues = []
    if inst:
        try:
            with open('/etc/cups/printers.conf', encoding='utf-8') as f:
                queues = _print_parse_printers_conf(f.read())
        except Exception:
            queues = []
        # cupsd 延迟写盘：别人（或本页上一次操作）刚加的队列可能只在内存里。
        # 用 lpstat 补齐 —— 只补「文件里没有的」，绝不据此删掉文件里有的，
        # 免得把 cups-browsed 还没来得及重新生成的队列弄丢。
        rc2, lp, _e2 = sh(['lpstat', '-p'], timeout=25, env=_print_c_locale_env())
        if rc2 == 0:
            have = set(q.get('name') for q in queues)
            for x in _print_parse_lpstat_p(lp):
                if x['name'] in have:
                    continue
                queues.append({'name': x['name'], 'default': False,
                               'uri': '', 'info': '', 'location': '',
                               'make_model': '', 'state': x['state'],
                               'accepting': x['accepting'], 'shared': False,
                               'not_saved_yet': True})
    rc, lpv, _e = sh(['lpinfo', '-v'], timeout=30) if inst else (1, '', '')
    devices = []
    if rc == 0:
        for ln in lpv.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            parts = ln.split(None, 1)
            devices.append({'kind': parts[0], 'uri': parts[1] if len(parts) > 1 else ''})
    usbs, nodes = _print_usb_printers()
    lan = _print_lan_ip()
    return ok({
        'cfg': cfg,
        'installed': inst,
        'need_install': [] if inst else PRINT_PKGS,
        'cups': cups, 'browsed': browsed, 'raw': raw,
        'cups_version': ver,
        'listen': _print_listen_now(),
        'queues': queues,
        'devices': devices,
        'usb_printers': usbs,
        'usb_nodes': nodes,
        'drivers': PRINT_DRIVERS,
        'items': PRINT_ITEMS,
        'lan_ip': lan,
        'cups_url': ('http://%s:631' % lan) if lan else 'http://<本机IP>:631',
        'build_mode': in_build_mode(),
        'conf_file': PRINT_CONF,
        'cupsd_file': PRINT_CUPSD,
        'backups': _print_backups(),
        'usblp_loaded': os.path.exists('/sys/module/usblp'),
        'avahi_active': _svcs['avahi-daemon']['active'] == 'active',
        'socat': bool(shutil.which('socat')),
    }, '已读取打印服务状态')


def _print_save_op(p):
    p = p or {}
    cfg = _print_load()
    mode = p.get('mode')
    if mode in ('off', 'cups', 'raw'):
        cfg['mode'] = mode
    c = p.get('cups')
    if isinstance(c, dict):
        if c.get('listen') in ('lan', 'local'):
            cfg['cups']['listen'] = c['listen']
        for k in ('share', 'web_iface', 'remote_admin', 'browsed'):
            if c.get(k) is not None:
                cfg['cups'][k] = bool(c[k])
    r = p.get('raw')
    if isinstance(r, dict):
        if r.get('device') is not None:
            cfg['raw']['device'] = str(r['device']).strip()
        if r.get('bind') is not None:
            cfg['raw']['bind'] = str(r['bind']).strip()
        if r.get('port') is not None:
            try:
                cfg['raw']['port'] = max(1, min(int(r['port']), 65535))
            except Exception:
                return fail('端口取值不合法（1–65535）')

    # 只有 CUPS 模式真的需要 cups 套装；RAW 模式只靠 socat（系统自带）
    if cfg['mode'] == 'cups' and not _print_load_installed():
        return fail('CUPS 没有安装。请到「系统 → 依赖自检与安装」安装后再来配置，'
                    '或在终端执行 apt-get install -y %s' % ' '.join(PRINT_PKGS),
                    'NOT_INSTALLED')

    errs = []
    if in_build_mode():
        errs.append('当前处于【构建保护模式】：配置已写入磁盘，但未启停任何打印服务')
    else:
        _print_apply(cfg, errs)
    _print_save(cfg)
    log('warn', 'print', 'PRINT_SET', '打印服务设置为 %s 模式' % cfg['mode'],
        {'errors': errs, 'cups': cfg['cups'], 'raw': cfg['raw']})
    msg = ('已保存并生效' if not in_build_mode()
           else '已保存到配置（构建保护模式下未启停服务）')
    if errs:
        msg += '；' + '；'.join(errs)
    return ok({'cfg': cfg, 'errors': errs}, msg)


def _print_load_installed():
    return bool(shutil.which('cupsd') or os.path.isfile('/usr/sbin/cupsd'))


def _print_queue_name(name):
    n = (name or '').strip()
    if not re.match(PRINT_NAME_RE, n):
        raise ValidateError('队列名不合规：只能用字母、数字、_ . : -，'
                            '且以字母或数字开头，最长 64 个字符', 'name')
    return n


def _print_uri(uri):
    u = (uri or '').strip()
    if not u:
        raise ValidateError('设备地址不能为空', 'uri')
    if re.search(r'[\s;|&$`\n\r]', u):
        raise ValidateError('设备地址不能包含空格或 shell 特殊字符', 'uri')
    if not u.startswith(PRINT_URI_SCHEMES):
        raise ValidateError('不支持的设备地址类型：%s（支持 %s）'
                            % (u.split('://')[0] + '://', '、'.join(PRINT_URI_SCHEMES)),
                            'uri')
    if len(u) > 500:
        raise ValidateError('设备地址过长（最多 500 个字符）', 'uri')
    return u


def _print_queue_ops(p):
    p = p or {}
    op = p.get('op')
    if not _print_load_installed():
        return fail('CUPS 没有安装，无法管理队列', 'NOT_INSTALLED')

    if op == 'queue_add':
        try:
            name = _print_queue_name(p.get('name'))
            uri = _print_uri(p.get('uri'))
        except ValidateError as e:
            return fail(e.msg_cn, 'BAD_ARG')
        drv = (p.get('driver') or 'everywhere').strip()
        ppd = None
        for d in PRINT_DRIVERS:
            if d['key'] == drv:
                ppd = d['ppd']
        if ppd is None:
            # 允许直接给 PPD 名，但只放行安全的字符集
            if not re.match(r'^[A-Za-z0-9._:/-]{1,120}$', drv):
                return fail('驱动名不合法', 'BAD_ARG')
            ppd = drv
        args = ['lpadmin', '-p', name, '-v', uri, '-m', ppd, '-E']
        info = (p.get('info') or '').strip()
        loc = (p.get('location') or '').strip()
        if info:
            args += ['-D', info[:128]]
        if loc:
            args += ['-L', loc[:128]]
        rc, out, err = sh(args, timeout=60)
        if rc != 0:
            return fail('添加队列失败：%s' % ((err or out or '未知错误')[:300]),
                        'LPADMIN_FAIL')
        share = p.get('share')
        if share is not None:
            sh(['lpadmin', '-p', name, '-o',
                'printer-is-shared=%s' % ('true' if share else 'false')], timeout=30)
        # lpadmin 改的是 cupsd 内存里的状态，printers.conf 要等 cupsd 自己落盘
        # （实测能拖到下次重启）。重启一下把它逼出来，队列才算真的建好了 ——
        # 否则机器一断电这个队列就没了。
        sh(['systemctl', 'restart', 'cups'], timeout=60)
        log('warn', 'print', 'PRINT_QUEUE_ADD', '已添加打印队列 %s' % name,
            {'uri': uri, 'ppd': ppd})
        return ok({'name': name}, '队列 %s 已添加（%s）' % (name, uri))

    if op == 'queue_del':
        try:
            name = _print_queue_name(p.get('name'))
        except ValidateError as e:
            return fail(e.msg_cn, 'BAD_ARG')
        if p.get('confirm') != name:
            return fail('请确认：需输入队列名「%s」以完成删除' % name, 'NEED_CONFIRM')
        rc, out, err = sh(['lpadmin', '-x', name], timeout=60)
        if rc != 0:
            return fail('删除队列失败：%s' % ((err or out or '未知错误')[:200]),
                        'LPADMIN_FAIL')
        # 同上：不重启的话 printers.conf 里还留着它，页面会显示「删了还在」
        sh(['systemctl', 'restart', 'cups'], timeout=60)
        log('warn', 'print', 'PRINT_QUEUE_DEL', '已删除打印队列 %s' % name, {})
        return ok({}, '队列 %s 已删除' % name)

    if op == 'queue_set':
        try:
            name = _print_queue_name(p.get('name'))
        except ValidateError as e:
            return fail(e.msg_cn, 'BAD_ARG')
        did = []
        if p.get('share') is not None:
            rc, _o, err = sh(['lpadmin', '-p', name, '-o',
                              'printer-is-shared=%s'
                              % ('true' if p['share'] else 'false')], timeout=30)
            if rc == 0:
                did.append('共享=%s' % ('开' if p['share'] else '关'))
            else:
                return fail('设置共享失败：%s' % (err or '')[:200], 'LPADMIN_FAIL')
        if p.get('accept') is not None:
            sub = 'cupsaccept' if p['accept'] else 'cupsreject'
            rc, _o, err = sh([sub, name], timeout=30)
            if rc == 0:
                did.append('接受任务=%s' % ('是' if p['accept'] else '否'))
            else:
                return fail('设置接受任务失败：%s' % (err or '')[:200], 'CUPS_FAIL')
        if p.get('default'):
            rc, _o, err = sh(['lpadmin', '-d', name], timeout=30)
            if rc == 0:
                did.append('已设为默认')
            else:
                return fail('设为默认失败：%s' % (err or '')[:200], 'LPADMIN_FAIL')
        rc, _o, e2 = sh(['systemctl', 'restart', 'cups'], timeout=60)
        log('warn', 'print', 'PRINT_QUEUE_SET', '队列 %s 设置已更新：%s'
            % (name, '、'.join(did) or '无变化'), {})
        return ok({'name': name}, '队列 %s：%s' % (name, '、'.join(did) or '无变化'))

    if op == 'testpage':
        try:
            name = _print_queue_name(p.get('name'))
        except ValidateError as e:
            return fail(e.msg_cn, 'BAD_ARG')
        src = PRINT_TESTPAGE if os.path.isfile(PRINT_TESTPAGE) else '-'
        args = ['lp', '-d', name]
        if src != '-':
            args.append(src)
        rc, out, err = sh(args, timeout=90,
                          input_data=None if src != '-' else '\n')
        if rc != 0:
            return fail('测试页发送失败：%s' % ((err or out or '未知错误')[:300]),
                        'LP_FAIL')
        return ok({'name': name},
                  '测试页已发送到 %s（%s）' % (name, out.strip()[:120] or '已排队'))

    if op == 'jobs':
        rc, out, _e = sh(['lpstat', '-o'], timeout=25)
        jobs = _print_parse_jobs(out if rc == 0 else '')
        return ok({'jobs': jobs, 'count': len(jobs)},
                  '当前 %d 个打印任务' % len(jobs))

    if op == 'job_cancel':
        jid = (p.get('id') or '').strip()
        if not re.match(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}-\d{1,12}$', jid):
            return fail('任务号不合法（格式：队列名-任务号）', 'BAD_ID')
        rc, _o, err = sh(['cancel', jid], timeout=40)
        if rc != 0:
            return fail('取消任务失败：%s' % ((err or '未知错误')[:200]), 'CANCEL_FAIL')
        return ok({}, '任务 %s 已取消' % jid)

    return fail('不支持的队列操作：%s' % op, 'BAD_OP')


def act_print(p):
    """打印服务：status / save / queue_* / testpage / jobs / job_cancel / restore"""
    p = p or {}
    op = str(p.get('op') or 'status')
    if op == 'status':
        return _print_status()
    if op == 'save':
        return _print_save_op(p)
    if op == 'restore':
        fn = (p.get('file') or '').strip()
        if not re.match(r'^cupsd-\d{8}-\d{6}\.conf$', fn):
            return fail('备份文件名不合法', 'BAD_FILE')
        root = os.path.realpath(PRINT_BAK_DIR)
        src = os.path.realpath(os.path.join(PRINT_BAK_DIR, fn))
        if not src.startswith(root + os.sep) or not os.path.isfile(src):
            return fail('备份 %s 不存在' % fn, 'NOT_FOUND')
        _print_backup()
        try:
            shutil.copy2(src, PRINT_CUPSD)
        except Exception as e:
            return fail('还原失败：%s' % e, 'RESTORE_FAIL')
        if not in_build_mode():
            rc, _o, e = sh(['systemctl', 'restart', 'cups'], timeout=60)
            if rc != 0:
                return fail('还原后重启 CUPS 失败：%s' % (e or '')[:200], 'RESTART_FAIL')
        log('warn', 'print', 'PRINT_RESTORE', 'cupsd.conf 已从备份 %s 还原' % fn, {})
        return ok({}, '已从 %s 还原并重启 CUPS' % fn)
    if op.startswith('queue_') or op in ('testpage', 'jobs', 'job_cancel'):
        return _print_queue_ops(p)
    return fail('不支持的打印服务操作：%s' % op, 'BAD_OP')


# ==========================================================================
# AC / AP 管理中心（OpenSOHO，#10）
# --------------------------------------------------------------------------
# OpenSOHO 是一个单二进制的 OpenWRT 集中控制器（PocketBase 内核）。AP 侧装
# openwisp-config 后用共享密钥向它自注册，之后 Wi-Fi / VLAN / PoE 全在它的
# 控制台里配。drouter 在这里只做三件事，不做第四件：
#   1. 把二进制与 systemd 单元装好、管住启停与开机自启
#   2. 保管两把密钥（AP 注册用的共享密钥、settings 静态加密密钥）与控制台凭据
#   3. 调它的 REST API，把 AP 数 / 客户端数回显到 drouter 页面上
#   4.（不做）重复实现一遍 Wi-Fi 配置 —— 既做不好也维护不动，只给入口
# 实测踩到的几个坑都写在下面的常量注释里，改代码前先看一眼。
OH_DIR = '/opt/drouter/opensoho'
OH_BIN = OH_DIR + '/opensoho'
# 数据目录必须显式 --dir 指定（默认值随运行用户变，root 跑就会落到 /root 下）。
# 位置选 /var/lib/opensoho 而不是 /var/lib/drouter/opensoho，是因为
# /var/lib/drouter 本身是 0750 drouter:drouter —— 服务用户连父目录都穿不过去，
# 单元会以 status=200/CHDIR 启动失败。这是实测踩出来的，别挪回去。
OH_DATA = '/var/lib/opensoho'
OH_CONF = '/etc/drouter/opensoho.conf'         # drouter 自己的配置（端口/监听/开关）
OH_ENV = '/etc/drouter/opensoho.env'           # 两把密钥，0600，systemd 用 EnvironmentFile 读
OH_ADMIN = '/etc/drouter/opensoho.admin'       # 控制台管理员凭据与 token，0600
OH_UNIT = '/etc/systemd/system/opensoho.service'
OH_SERVICE = 'opensoho.service'
OH_USER = 'opensoho'
OH_SECRET_ENV = 'OPENSOHO_SHARED_SECRET'
OH_ENC_ENV = 'OPENSOHO_SETTINGS_KEY'
# OpenSOHO 要求共享密钥是「长随机串」，官方示例 32 字符。少于这个数 AP 侧
# 照样能填，但我们判定「弱密钥」并在页面上提示。
OH_SECRET_LEN = 32
OH_ENC_LEN = 32                                # --encryptionEnv 硬性要求 32 字符
OH_ADMIN_EMAIL = 'admin@drouter.local'
OH_API_LATEST = 'https://api.github.com/repos/rubenbe/opensoho/releases/latest'
OH_PORT_MIN, OH_PORT_MAX = 1024, 65535
# 监听地址只给这三个：不给 LAN 网卡名，免得网卡改名后单元起不来。
OH_BIND_CHOICES = ('0.0.0.0', '127.0.0.1', '::')
# 回显用的集合。名字是实测从 /api/collections 拉出来的 ——
# 注意不是文档里写的 wifi，而是 wifi_ssids / wifi_aps 两个。
OH_COLLECTIONS = (
    ('devices', '受管 AP'),
    ('clients', '无线客户端'),
    ('radios', '射频'),
    ('wifi_ssids', 'SSID'),
    ('wifi_aps', 'AP 实例'),
    ('vlan', 'VLAN'),
    ('poe', 'PoE 端口'),
    ('lldp', 'LLDP 邻居'),
)

OH_DEFAULTS = {
    'bind': '0.0.0.0',            # AP 要从局域网连进来注册，默认听全部地址
    'port': 8090,
    'autostart': True,
    'enable_new_devices': True,   # 关掉 = 只监控不下发配置
    'encryption': True,           # settings（含 Wi-Fi 密码）静态加密
}

OH_ITEMS = [
    {
        'key': 'install', 'name': '安装方式',
        'why': 'OpenSOHO 不是 apt 包，是一个自包含的 Go 二进制。'
               '路由器能上 GitHub 时点一下就装好；上不了就得从别的机器把文件传进来。',
        'impact': ['在线安装走 GitHub Releases，装的是最新版',
                   '本机访问不了 GitHub 时（比如现在这台），改用「从本机文件安装」：'
                   '先在 Web 终端 / 文件管理器把二进制传到 /tmp，再在这里选它',
                   '升级与安装走同一条路径，数据目录会原样保留，已注册的 AP 不受影响'],
    },
    {
        'key': 'bind', 'name': '监听地址',
        'why': '决定谁能连到控制器。AP 得连得上才能注册，所以要听局域网。',
        'impact': ['0.0.0.0：局域网内所有设备都能连（AP 注册需要，默认）',
                   '127.0.0.1：只有本机连得上，AP 注册不进来，只能自己看控制台',
                   '改端口或地址会重建 systemd 单元并重启服务'],
    },
    {
        'key': 'secret', 'name': '共享密钥',
        'why': 'AP 侧 openwisp-config 填的就是它，填错注册不上。'
               '它由 drouter 生成并保管，页面上直接给你复制。',
        'impact': ['重新生成后，已注册的 AP 全部要重填一次，否则不再被接受',
                   '它只控制「谁能注册」，不影响已注册 AP 的正常运行'],
    },
    {
        'key': 'enable_new_devices', 'name': '自动接管新 AP',
        'why': 'OpenSOHO 发现一台新 AP 时，是直接把配置下发给它，还是只看着。',
        'impact': ['开启：新 AP 注册后立刻被接管、统一下发配置（默认）',
                   '关闭：进入「只监控」模式，新 AP 只出现在列表里不被下发 —— '
                   '想先看清网络里有什么，再决定要不要管，就关掉'],
    },
    {
        'key': 'encryption', 'name': '配置静态加密',
        'why': 'OpenSOHO 的 settings 集合里存着 Wi-Fi 密码等敏感项。'
               '开启后这些值在磁盘上是加密的。',
        'impact': ['加密密钥由 drouter 生成并保管，长度固定 32 字符',
                   '密钥丢了已存的配置就解不开 —— 所以它和共享密钥一起被快照带走',
                   '装完再改这个开关会重建单元并重启，已存数据不受影响'],
    },
    {
        'key': 'autostart', 'name': '开机自启',
        'why': '决定机器重启后 OpenSOHO 会不会自己起来。',
        'impact': ['开启：随系统启动（默认）',
                   '关闭：每次要自己来这里启动，适合只是临时试一下的场景'],
    },
]


def _oh_rand(n=32):
    """密码学安全随机串（字母数字，方便往 LuCI 里粘贴）。"""
    import secrets
    import string
    alpha = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alpha) for _ in range(n))


def _oh_norm(cfg):
    """把读回来的配置收敛成合法值。纯函数，便于单测。"""
    c = dict(OH_DEFAULTS)
    c.update(cfg or {})
    # 端口：别写成 int(x or 8090)。0 是 falsy，会被当成「没填」而悄悄回落成
    # 默认值，用户填了却拿不到是最难排查的那种错。
    raw = c.get('port')
    if raw in (None, ''):
        port = OH_DEFAULTS['port']
    else:
        try:
            port = int(raw)
        except Exception:
            port = OH_DEFAULTS['port']
    c['port'] = max(OH_PORT_MIN, min(port, OH_PORT_MAX))
    if c.get('bind') not in OH_BIND_CHOICES:
        c['bind'] = OH_DEFAULTS['bind']
    c['autostart'] = bool(c.get('autostart'))
    c['enable_new_devices'] = bool(c.get('enable_new_devices'))
    c['encryption'] = bool(c.get('encryption'))
    return c


def _oh_load():
    cfg = dict(OH_DEFAULTS)
    try:
        if os.path.isfile(OH_CONF):
            with open(OH_CONF, encoding='utf-8') as f:
                d = json.load(f) or {}
            for k in OH_DEFAULTS:
                if k in d:
                    cfg[k] = d[k]
    except Exception:
        pass
    return _oh_norm(cfg)


def _oh_save(cfg):
    _atomic_write(OH_CONF,
                  json.dumps(_oh_norm(cfg), ensure_ascii=False, indent=2) + '\n')


def _oh_read_env():
    """读密钥文件，返回 (共享密钥, 加密密钥)。"""
    secret, enc = '', ''
    try:
        if os.path.isfile(OH_ENV):
            with open(OH_ENV, encoding='utf-8') as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln or ln.startswith('#') or '=' not in ln:
                        continue
                    k, v = ln.split('=', 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k == OH_SECRET_ENV and not secret:
                        secret = v
                    elif k == OH_ENC_ENV and not enc:
                        enc = v
    except Exception:
        pass
    return secret, enc


def _oh_write_env(secret, enc):
    """写密钥文件，权限 0600（里面有 Wi-Fi 配置的加密密钥）。"""
    os.makedirs(os.path.dirname(OH_ENV), exist_ok=True)
    body = (
        '# drouter 自动生成，请勿手工编辑。改端口/开关不会动这个文件。\n'
        '# %s：AP 侧 openwisp-config 注册时填的共享密钥。改了，已注册的 AP 要重填。\n'
        '# %s：settings 集合（含 Wi-Fi 密码）的静态加密密钥。丢了，已存配置就解不开。\n'
        '%s=%s\n%s=%s\n'
        % (OH_SECRET_ENV, OH_ENC_ENV, OH_SECRET_ENV, secret, OH_ENC_ENV, enc))
    _atomic_write(OH_ENV, body, mode=0o600)
    try:
        os.chmod(OH_ENV, 0o600)
    except Exception:
        pass


def _oh_ensure_env():
    """确保两把密钥存在。只在缺失时生成 —— 升级、改端口都绝不能重新生成
    加密密钥，否则历史配置直接变成解不开的乱码。"""
    secret, enc = _oh_read_env()
    changed = False
    if len(secret) < OH_SECRET_LEN:
        secret = _oh_rand(OH_SECRET_LEN)
        changed = True
    if len(enc) != OH_ENC_LEN:
        enc = _oh_rand(OH_ENC_LEN)
        changed = True
    if changed:
        _oh_write_env(secret, enc)
    return secret, enc


def _oh_env():
    """给 opensoho 子命令准备环境（它连 --help 都要求共享密钥已设置）。"""
    s, k = _oh_read_env()
    env = dict(os.environ)
    env[OH_SECRET_ENV] = s or '-'
    if k:
        env[OH_ENC_ENV] = k
    return env


def _oh_admin_load():
    try:
        if os.path.isfile(OH_ADMIN):
            with open(OH_ADMIN, encoding='utf-8') as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}


def _oh_admin_save(d):
    with open(OH_ADMIN, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(OH_ADMIN, 0o600)
    except Exception:
        pass


def _oh_installed():
    return os.path.isfile(OH_BIN) and os.access(OH_BIN, os.X_OK)


def _oh_service():
    d = {'active': 'inactive', 'enabled': 'disabled'}
    rc, st, _e = sh(['systemctl', 'is-active', OH_SERVICE], timeout=12)
    d['active'] = st if rc == 0 else 'inactive'
    rc2, en, _e2 = sh(['systemctl', 'is-enabled', OH_SERVICE], timeout=12)
    d['enabled'] = en if rc2 == 0 else 'disabled'
    return d


def _oh_version():
    """返回已装版本号。opensoho -v 输出形如 `opensoho version 0.15.2`。"""
    if not _oh_installed():
        return ''
    rc, out, _e = _oh_cli([OH_BIN, '-v'], timeout=20)
    if rc != 0:
        return ''
    m = re.search(r'(\d+\.\d+\.\d+)', out)
    return m.group(1) if m else (out.strip()[:32])


def _oh_arch():
    """把 uname -m 映射成 OpenSOHO 发布包里的架构名。"""
    m = {'x86_64': 'amd64', 'amd64': 'amd64',
         'aarch64': 'arm64', 'arm64': 'arm64'}
    rc, out, _e = sh(['uname', '-m'], timeout=8)
    return m.get((out or '').strip(), '') if rc == 0 else ''


def _oh_unit_body(cfg):
    """生成 systemd 单元文本。纯函数，便于单测。"""
    c = _oh_norm(cfg)
    args = [OH_BIN, 'serve',
            '--http', '%s:%d' % (c['bind'], c['port']),
            '--dir', OH_DATA,
            '--enableNewDevices=%s' % ('true' if c['enable_new_devices'] else 'false')]
    if c['encryption']:
        args.append('--encryptionEnv=' + OH_ENC_ENV)
    return '\n'.join([
        '[Unit]',
        'Description=OpenSOHO AC/AP 控制器（drouter 托管）',
        'Documentation=https://opensoho.github.io/docs/',
        'After=network-online.target',
        'Wants=network-online.target',
        '',
        '[Service]',
        'Type=simple',
        'User=%s' % OH_USER,
        'Group=%s' % OH_USER,
        # 工作目录必须是数据目录：OpenSOHO 启动时会把内嵌的迁移文件解压到
        # 「当前工作目录」下的 pb_migrations，而不是 --dir 指向的位置。
        # 先前指向二进制目录 /opt/drouter/opensoho（root 所有），服务用户写不进去，
        # 实测报 `mkdir .../pb_migrations: permission denied` 然后秒退。
        'WorkingDirectory=%s' % OH_DATA,
        # 密钥放 EnvironmentFile 而不是 Environment=：单元文件是 0644，
        # 写在里面的密钥任何能读单元的人都能看到；EnvironmentFile 可以设 0600。
        'EnvironmentFile=%s' % OH_ENV,
        'Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
        'ExecStart=' + ' '.join(args),
        'Restart=on-failure',
        'RestartSec=5',
        'NoNewPrivileges=yes',
        'PrivateTmp=yes',
        'ProtectSystem=full',
        'ProtectHome=yes',
        '',
        '[Install]',
        'WantedBy=multi-user.target',
        '',
    ])


def _oh_listen_now(port):
    """返回当前实际监听该端口的地址列表（从 ss 里找）。"""
    try:
        p = int(port)
    except Exception:
        return []
    rc, out, _e = sh(['ss', '-lntp'], timeout=12)
    if rc != 0:
        return []
    hits = []
    for ln in out.splitlines():
        m = re.search(r'(\S+):%d\b' % p, ln)
        if not m:
            continue
        # ss 把「监听全部地址」打成 `*`，直接显示出来没人看得懂，
        # 还原成我们实际绑定的写法。
        addr = '0.0.0.0' if m.group(1) == '*' else m.group(1)
        if addr not in hits:
            hits.append(addr)
    return hits


def _oh_lan_ip():
    rc, out, _e = sh(['ip', '-o', '-4', 'addr', 'show', 'scope', 'global'], timeout=10)
    if rc == 0:
        for ln in out.splitlines():
            m = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)/', ln)
            if m and not m.group(1).startswith('127.'):
                return m.group(1)
    return ''


def _oh_cli(args, timeout=60):
    """跑一次 opensoho 子命令（superuser / -v 之类）。

    两个坑：
      1. 连 --help 都要求 OPENSOHO_SHARED_SECRET 已设置，环境必须带上；
      2. 它会把内嵌的迁移文件解压到「当前工作目录」而不是 --dir 指定的位置，
         所以强制把 cwd 设成数据目录 —— 否则 root 跑一次就在 / 下面留下
         pb_migrations。跑完再把属主改回服务用户，不然下次服务起不来。
    """
    cwd = OH_DATA if os.path.isdir(OH_DATA) else None
    rc, out, err = sh(args, timeout=timeout, cwd=cwd, env=_oh_env())
    if cwd:
        sh(['chown', '-R', '%s:%s' % (OH_USER, OH_USER), OH_DATA], timeout=40)
    return rc, out, err


def _oh_wait_up(cfg, tries=15):
    """启动后确认它真的起来了。返回 (是否起来了, 说明)。

    systemctl restart 返回 0 只代表「systemd 收下了这个任务」，不代表进程活着 ——
    实测 opensoho 因工作目录不可写而秒退时 restart 照样返回 0，页面就会显示
    「已安装并已启动」而端口根本没监听。所以必须自己再确认一遍：
    先看 systemd 状态，再看免鉴权的 /api/health。
    """
    why = ''
    for _i in range(tries):
        if _oh_service()['active'] == 'active':
            okh, msg = _oh_health(cfg['port'], timeout=2)
            if okh:
                return True, ''
            why = '服务在跑但 %d 端口没应答（%s）' % (cfg['port'], msg)
        time.sleep(1)
    if not why:
        why = '服务启动后没有保持在运行态'
        rc, out, _e = sh(['journalctl', '-u', OH_SERVICE, '--no-pager', '-n', '20'],
                         timeout=20)
        for ln in reversed((out or '').splitlines()):
            low = ln.lower()
            if ('opensoho' in low and ('denied' in low or 'failed' in low
                                       or 'error' in low or 'exit' in low)):
                why += '，最近一条日志：%s' % ln.strip()[:150]
                break
        why += '。详细看 journalctl -u %s' % OH_SERVICE
    return False, why


def _oh_health(port, timeout=4):
    """探 /api/health。这个端点免鉴权，最适合做「到底起没起来」的判断。
    返回 (是否健康, 说明)。"""
    try:
        import urllib.request
        url = 'http://127.0.0.1:%d/api/health' % int(port)
        with urllib.request.urlopen(url, timeout=timeout) as r:
            txt = r.read(512).decode('utf-8', 'ignore')
    except Exception as e:
        return False, '连不上：%s' % str(e)[:80]
    if '"code":200' in txt.replace(' ', '') or 'healthy' in txt:
        return True, 'ok'
    return False, '响应异常：%s' % txt[:80]


def _oh_api(method, path, port, token='', payload=None, timeout=8):
    """调 OpenSOHO 的 REST API。返回 (dict|None, 错误串)。"""
    import urllib.request
    url = 'http://127.0.0.1:%d%s' % (int(port), path)
    data = None
    head = {}
    if token:
        head['Authorization'] = token
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        head['Content-type'] = 'application/json'
    try:
        req = urllib.request.Request(url, data=data, headers=head, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8', 'ignore')), ''
    except Exception as e:
        return None, str(e)[:160]


def _oh_login(port, email, password):
    """用管理员账号换 token。返回 (token, 错误串)。"""
    d, e = _oh_api('POST', '/api/collections/_superusers/auth-with-password',
                   port, payload={'identity': email, 'password': password},
                   timeout=10)
    if not d:
        return '', '登录控制台失败：%s' % e
    return (d.get('token') or ''), ''


def _oh_ensure_admin(port):
    """确保存在一个 drouter 自己掌握密码的控制台管理员，并返回可用 token。

    为什么不直接让用户输入密码：superuser upsert 是幂等的，drouter 用随机密码
    建号、自己留着，既能随时换 token（token 有效期 3 年），又不用把用户的密码
    存下来。用户要进控制台时页面直接把密码展示出来。
    """
    d = _oh_admin_load()
    if not d.get('email'):
        d = {'email': OH_ADMIN_EMAIL, 'password': _oh_rand(20), 'token': '', 'ts': 0}
    args = [OH_BIN, 'superuser', 'upsert', d['email'], d['password'], '--dir', OH_DATA]
    # 库是带加密密钥建的（serve 用了 --encryptionEnv），子命令不带同一个参数
    # 就会报 `missing encryption key ""` —— settings 解不开，管理员建不出来。
    if _oh_load()['encryption']:
        args.append('--encryptionEnv=' + OH_ENC_ENV)
    rc, out, err = _oh_cli(args, timeout=60)
    if rc != 0:
        return '', '创建控制台管理员失败：%s' % ((err or out or '未知错误')[:200])
    tok, e = _oh_login(port, d['email'], d['password'])
    if not tok:
        return '', e
    d['token'] = tok
    d['ts'] = int(time.time())
    _oh_admin_save(d)
    return tok, ''


def _oh_admin_token(port):
    """拿一个能用的 token：先试已存的，失效了就用留存的密码重新换，
    连密码都没有（首次）就现场建一个管理员。

    注意这里不能「没有邮箱就直接返回空」—— 那样首次安装后永远建不出管理员，
    页面上的控制台密码一直是空的、probe 也永远拿不到凭据。
    """
    d = _oh_admin_load()
    tok = d.get('token') or ''
    if tok:
        r, _e = _oh_api('GET', '/api/collections/devices/records?perPage=1', port, tok)
        if r is not None:
            return tok, ''
    return _oh_ensure_admin(port)


def _oh_local_candidates():
    """本机上看起来像 opensoho 安装文件的路径（给离线安装用）。"""
    out = []
    for d in ('/tmp', '/var/tmp', '/root', '/home', OH_DIR):
        try:
            for n in sorted(os.listdir(d)):
                if not n.startswith('opensoho'):
                    continue
                p = os.path.join(d, n)
                if os.path.isfile(p):
                    out.append(p)
        except Exception:
            pass
    return sorted(set(out))[:10]


def _oh_ensure_user():
    """确保 opensoho 系统用户存在。返回错误串（空 = 成功）。"""
    rc, _o, _e = sh(['id', OH_USER], timeout=10)
    if rc == 0:
        return ''
    rc, _o, e = sh(['useradd', '--system', '--no-create-home',
                    '--shell', '/usr/sbin/nologin', OH_USER], timeout=30)
    if rc != 0:
        return '创建系统用户 %s 失败：%s' % (OH_USER, (e or '')[:150])
    return ''


def _oh_latest_release(arch):
    """查最新版本号与 zip 直链。返回 (版本, 直链) 或 ('', 错误串)。"""
    try:
        import urllib.request
        req = urllib.request.Request(OH_API_LATEST, headers={
            'User-Agent': 'drouter/1.0', 'Accept': 'application/vnd.github+json'})
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read().decode('utf-8', 'ignore'))
    except Exception as e:
        return '', '查询最新版本失败：%s —— 这台机器能访问 GitHub 吗？' % str(e)[:100]
    ver = (d.get('tag_name') or d.get('name') or '').lstrip('v')
    want = 'opensoho_%s_linux_%s.zip' % (ver, arch)
    url = ''
    for a in d.get('assets') or []:
        if a.get('name') == want:
            url = a.get('browser_download_url') or ''
    if not ver or not url:
        return '', '发布包里没有 %s，无法确定下载地址' % want
    return ver, url


def _oh_download(url, dst):
    """下载到 dst。走 urllib 而不是 curl，免得依赖外部命令。"""
    import urllib.request
    req = urllib.request.Request(url, headers={'User-Agent': 'drouter/1.0'})
    with urllib.request.urlopen(req, timeout=240) as r, open(dst, 'wb') as f:
        shutil.copyfileobj(r, f)
    return os.path.getsize(dst)


def _oh_extract_bin(src, outdir):
    """从 zip 或裸二进制里取出 opensoho，落到 outdir/opensoho（0755）。

    用 Python 的 zipfile 而不是 unzip 命令：既不用多装一个包，也能自己挡住
    zip slip（压缩包里带 ../ 的路径名）。做法是只解压「那一个」文件并强制
    写到固定目标名，成员路径根本不参与拼接。
    """
    os.makedirs(outdir, exist_ok=True)
    dst = os.path.join(outdir, 'opensoho')
    if src.lower().endswith('.zip'):
        import zipfile
        with zipfile.ZipFile(src) as z:
            names = [n for n in z.namelist()
                     if not n.endswith('/') and os.path.basename(n) in ('opensoho',)]
            if not names:
                raise RuntimeError('压缩包里没有 opensoho 二进制（内容：%s）'
                                   % ', '.join(z.namelist()[:8]))
            with z.open(names[0]) as fin, open(dst, 'wb') as fout:
                shutil.copyfileobj(fin, fout)
    else:
        shutil.copyfile(src, dst)
    os.chmod(dst, 0o755)
    return dst


def _oh_write_unit(cfg):
    with open(OH_UNIT, 'w', encoding='utf-8') as f:
        f.write(_oh_unit_body(cfg))
    try:
        os.chmod(OH_UNIT, 0o644)
    except Exception:
        pass


def _oh_status():
    cfg = _oh_load()
    secret, enc = _oh_read_env()
    inst = _oh_installed()
    svc = _oh_service()
    ver = _oh_version() if inst else ''
    listen = _oh_listen_now(cfg['port']) if inst else []
    healthy, health_msg = False, '未安装'
    if inst and svc['active'] == 'active':
        healthy, health_msg = _oh_health(cfg['port'])
    elif inst:
        health_msg = '服务未运行'
    ip = _oh_lan_ip()
    return ok({
        'cfg': cfg,
        'installed': inst,
        'version': ver,
        'service': svc,
        'listen': listen,
        'healthy': healthy,
        'health_msg': health_msg,
        'secret': secret,
        'secret_weak': bool(secret) and len(secret) < OH_SECRET_LEN,
        'secret_ok': len(secret) >= OH_SECRET_LEN,
        'enc_ok': bool(enc),
        'admin_email': _oh_admin_load().get('email', ''),
        'admin_password': _oh_admin_load().get('password', ''),
        'bind_choices': list(OH_BIND_CHOICES),
        'console_url': ('http://%s:%d/_/' % (ip, cfg['port'])) if ip else '',
        'lan_ip': ip,
        'local_candidates': _oh_local_candidates(),
        'build_mode': in_build_mode(),
        'items': OH_ITEMS,
        'note': ('真正的 Wi-Fi、VLAN、PoE 配置都在 OpenSOHO 自己的控制台里做 —— '
                 '这里只负责把它装好、管住、把入口给你。AP 侧需要在 OpenWRT 上装 '
                 'openwisp-config 并填入下面的共享密钥来注册。'),
    })


def _oh_install_op(p):
    """安装或升级。local_path 非空时从本机文件装（离线场景）。"""
    arch = _oh_arch()
    if not arch:
        return fail('不支持的 CPU 架构（OpenSOHO 只发布 amd64 与 arm64）', 'BAD_ARCH')
    local_path = (p.get('local_path') or '').strip()
    import tempfile
    # 升级 / 重装时服务正在跑，二进制被内核占用 —— 直接覆盖会报
    # 「Text file busy」（Errno 26）。必须先把服务停下来再换文件。
    was_running = _oh_service()['active'] == 'active'
    if was_running:
        sh(['systemctl', 'stop', OH_SERVICE], timeout=40)
    tmpd = tempfile.mkdtemp(prefix='drouter-opensoho-')
    err = None
    try:
        if local_path:
            if not os.path.isfile(local_path):
                err = fail('本机找不到文件：%s' % local_path, 'NOT_FOUND')
            else:
                src = local_path
        else:
            ver, url = _oh_latest_release(arch)
            if not ver:
                err = fail(url, 'NO_RELEASE')
            else:
                src = os.path.join(tmpd, 'opensoho.zip')
                try:
                    _oh_download(url, src)
                except Exception as e:
                    err = fail('下载失败：%s' % str(e)[:160], 'DL_FAIL')
        if err is None:
            try:
                _oh_extract_bin(src, OH_DIR)
            except Exception as e:
                err = fail('解包失败：%s' % str(e)[:200], 'EXTRACT_FAIL')
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    if err is not None:
        # 中途失败不能把服务丢在停止状态 —— 原本在跑就把它拉回去
        if was_running:
            sh(['systemctl', 'start', OH_SERVICE], timeout=60)
        return err

    e = _oh_ensure_user()
    if e:
        return fail(e, 'USER_FAIL')
    try:
        os.makedirs(OH_DATA, exist_ok=True)
        sh(['chown', '-R', '%s:%s' % (OH_USER, OH_USER), OH_DATA], timeout=40)
        os.makedirs(OH_DIR, exist_ok=True)
    except Exception as ex:
        return fail('准备数据目录失败：%s' % ex, 'DIR_FAIL')
    _oh_ensure_env()
    cfg = _oh_load()
    _oh_save(cfg)
    _oh_write_unit(cfg)
    sh(['systemctl', 'daemon-reload'], timeout=40)

    started = False
    if not in_build_mode():
        if cfg['autostart']:
            sh(['systemctl', 'enable', OH_SERVICE], timeout=30)
        rc, _o, e2 = sh(['systemctl', 'restart', OH_SERVICE], timeout=60)
        if rc != 0:
            return fail('OpenSOHO 已安装但启动失败：%s' % (e2 or '')[:200], 'START_FAIL')
        # restart 返回 0 不代表起来了（进程可能秒退），必须自己确认一次
        up, why = _oh_wait_up(cfg)
        if not up:
            return fail('OpenSOHO 已安装但没跑起来：%s' % why, 'START_FAIL')
        started = True
    ver = _oh_version()
    # 服务一起来就把控制台管理员建好，页面才能直接给出登录密码。
    # 建号失败不算安装失败（控制器本身能用），单独提示出来。
    admin_err = ''
    if started:
        _t, admin_err = _oh_ensure_admin(cfg['port'])
    log('warn', 'opensoho', 'OH_INSTALL', 'OpenSOHO 已安装（版本 %s）' % (ver or '未知'),
        {'local': bool(local_path)})
    msg = 'OpenSOHO %s 已安装' % (ver or '')
    if in_build_mode():
        msg += '。当前处于构建保护模式，未启动服务'
    elif started:
        msg += '并已启动'
    if admin_err:
        msg += '。控制台管理员没能建好：%s' % admin_err[:120]
    return ok({'version': ver, 'started': started, 'admin_err': admin_err}, msg)


def _oh_uninstall_op(p):
    """卸载。默认保留数据目录（里面有 AP 与配置），purge=true 才一并删。"""
    purge = bool(p.get('purge'))
    sh(['systemctl', 'stop', OH_SERVICE], timeout=40)
    sh(['systemctl', 'disable', OH_SERVICE], timeout=30)
    try:
        os.unlink(OH_UNIT)
    except Exception:
        pass
    sh(['systemctl', 'daemon-reload'], timeout=40)
    sh(['systemctl', 'reset-failed', OH_SERVICE], timeout=20)
    try:
        shutil.rmtree(OH_DIR, ignore_errors=True)
    except Exception:
        pass
    if purge:
        for f in (OH_DATA, OH_ENV, OH_ADMIN, OH_CONF):
            try:
                if os.path.isdir(f):
                    shutil.rmtree(f, ignore_errors=True)
                elif os.path.isfile(f):
                    os.unlink(f)
            except Exception:
                pass
    log('warn', 'opensoho', 'OH_UNINSTALL', 'OpenSOHO 已卸载（清除数据=%s）' % purge, {})
    return ok({}, '已卸载' + ('，数据目录已一并清除' if purge else '，数据目录已保留'))


def _oh_svc_op(p):
    """启停与开机自启。构建保护模式下不真的动服务。"""
    op = (p.get('op2') or p.get('act') or '').strip()
    if op not in ('start', 'stop', 'restart', 'enable', 'disable'):
        return fail('不支持的服务操作：%s' % op, 'BAD_OP')
    cfg = _oh_load()
    if not _oh_installed():
        return fail('OpenSOHO 尚未安装', 'NOT_INSTALLED')
    if in_build_mode() and op in ('start', 'restart', 'enable'):
        return fail('当前处于【构建保护模式】，已阻止启动 OpenSOHO。'
                    '它需要监听端口对外提供服务，请在「系统设置 → 构建保护模式」'
                    '中关闭保护后再试。', 'BUILD_MODE_BLOCKED')
    rc, _o, e = sh(['systemctl', op, OH_SERVICE], timeout=60)
    if rc != 0:
        return fail('%s 失败：%s' % (op, (e or '')[:200]), 'SVC_FAIL')
    name = {'start': '启动', 'stop': '停止', 'restart': '重启',
            'enable': '设为开机自启', 'disable': '取消开机自启'}[op]
    # 同 install：start/restart 返回 0 不等于活着，确认一遍再报成功
    if op in ('start', 'restart'):
        up, why = _oh_wait_up(_oh_load())
        if not up:
            return fail('OpenSOHO %s后没跑起来：%s' % (name, why), 'START_FAIL')
    # 开关自启要落进配置，否则下次「保存」会把单元改回去
    if op in ('enable', 'disable'):
        cfg['autostart'] = (op == 'enable')
        _oh_save(cfg)
    log('warn', 'opensoho', 'OH_SVC', 'OpenSOHO 已%s' % name, {})
    return ok({}, 'OpenSOHO 已%s' % name)


def _oh_save_op(p):
    """保存端口 / 监听 / 开关，并重建单元。"""
    if not _oh_installed():
        return fail('OpenSOHO 尚未安装，请先安装再改设置', 'NOT_INSTALLED')
    old = _oh_load()
    raw = p.get('cfg') or p
    cfg = _oh_norm({
        'bind': raw.get('bind', OH_DEFAULTS['bind']),
        'port': raw.get('port', OH_DEFAULTS['port']),
        'autostart': raw.get('autostart', True),
        'enable_new_devices': raw.get('enable_new_devices', True),
        'encryption': raw.get('encryption', True),
    })
    # 库要么是带密钥建的、要么是明文建的。事后再切这个开关，另一半就读不出来
    # （Wi-Fi 密码这些 settings 会全变成解不开的数据），等于把配置毁了。
    # 所以一旦有数据就锁死，要改只能卸载清库重装。
    if (cfg['encryption'] != old['encryption']
            and os.path.isfile(os.path.join(OH_DATA, 'data.db'))):
        return fail(
            '配置静态加密不能在已有数据之后切换：现有库是%s的，改了这个开关已存的'
            '配置就读不出来了。如确需切换，请先卸载（勾选清除数据）再重装。'
            % ('用密钥加密' if old['encryption'] else '明文'), 'ENC_LOCKED')
    _oh_save(cfg)
    _oh_write_unit(cfg)
    sh(['systemctl', 'daemon-reload'], timeout=40)
    if not in_build_mode():
        if cfg['autostart']:
            sh(['systemctl', 'enable', OH_SERVICE], timeout=30)
        else:
            sh(['systemctl', 'disable', OH_SERVICE], timeout=30)
        was = _oh_service()['active'] == 'active'
        if was:
            rc, _o, e = sh(['systemctl', 'restart', OH_SERVICE], timeout=60)
            if rc != 0:
                return fail('配置已保存但重启失败：%s' % (e or '')[:200], 'RESTART_FAIL')
            up, why = _oh_wait_up(cfg)
            if not up:
                return fail('配置已保存但重启后没跑起来：%s' % why, 'RESTART_FAIL')
    log('warn', 'opensoho', 'OH_SAVE',
        'OpenSOHO 设置已更新（%s:%d）' % (cfg['bind'], cfg['port']), {})
    return ok({'cfg': cfg}, '设置已保存')


def _oh_secret_op(p):
    """重新生成共享密钥。加密密钥绝不动 —— 动了历史配置就解不开。"""
    if not _oh_installed():
        return fail('OpenSOHO 尚未安装', 'NOT_INSTALLED')
    secret, enc = _oh_read_env()
    if len(enc) != OH_ENC_LEN:
        enc = _oh_rand(OH_ENC_LEN)
    secret = _oh_rand(OH_SECRET_LEN)
    _oh_write_env(secret, enc)
    if not in_build_mode() and _oh_service()['active'] == 'active':
        sh(['systemctl', 'restart', OH_SERVICE], timeout=60)
    log('warn', 'opensoho', 'OH_SECRET', 'OpenSOHO 共享密钥已重新生成', {})
    return ok({'secret': secret},
              '共享密钥已重新生成。已注册的 AP 需要用新密钥重新注册一次。')


def _oh_probe_op(p):
    """通过 REST API 拉 AP / 客户端概况。"""
    cfg = _oh_load()
    if not _oh_installed():
        return fail('OpenSOHO 尚未安装', 'NOT_INSTALLED')
    ok_h, msg = _oh_health(cfg['port'])
    if not ok_h:
        return fail('OpenSOHO 现在连不上（%s）。先把它启动起来再读取。' % msg, 'DOWN')
    tok, e = _oh_admin_token(cfg['port'])
    if not tok:
        return fail('读取失败：%s' % (e or '拿不到控制台凭据'), 'NO_TOKEN')
    stats = []
    for key, name in OH_COLLECTIONS:
        d, e2 = _oh_api('GET', '/api/collections/%s/records?perPage=1' % key,
                        cfg['port'], tok)
        stats.append({'key': key, 'name': name,
                      'count': (d or {}).get('totalItems') if d else None,
                      'err': '' if d else e2})
    # 设备明细：字段名不硬猜，按常见候选依次取，取不到就留空
    d, _e = _oh_api('GET', '/api/collections/devices/records?perPage=100&sort=-updated',
                    cfg['port'], tok)
    devices = []
    for it in ((d or {}).get('items') or []):
        devices.append({
            'name': _oh_pick(it, ('name', 'hostname', 'id')),
            'mac': _oh_pick(it, ('mac', 'mac_address', 'macaddress')),
            'ip': _oh_pick(it, ('ip', 'ip_address', 'management_ip', 'address')),
            'model': _oh_pick(it, ('model', 'device_model', 'hardware')),
            'enabled': bool(it.get('enabled', True)),
            'updated': _oh_pick(it, ('updated', 'created')),
        })
    return ok({'stats': stats, 'devices': devices}, '已读取')


def _oh_pick(d, keys):
    """按候选字段名依次取值，返回第一个非空字符串。"""
    for k in keys:
        v = (d or {}).get(k)
        if v not in (None, ''):
            return str(v)
    return ''


def act_opensoho(p):
    """AC / AP 管理中心：status / install / uninstall / svc / save / secret / probe"""
    p = p or {}
    op = str(p.get('op') or 'status')
    if op == 'status':
        return _oh_status()
    if op == 'install':
        return _oh_install_op(p)
    if op == 'uninstall':
        return _oh_uninstall_op(p)
    if op == 'svc':
        return _oh_svc_op(p)
    if op == 'save':
        return _oh_save_op(p)
    if op == 'secret':
        return _oh_secret_op(p)
    if op == 'probe':
        return _oh_probe_op(p)
    return fail('不支持的 AC/AP 操作：%s' % op, 'BAD_OP')


# ==========================================================================
# CA 证书管理 + SSL 测试（#11）
# --------------------------------------------------------------------------
# 这一块只做四件事，不做第五件：
#   1. 保管证书库（自签 CA / 服务器证书 / CSR）与它们的私钥
#   2. 用库里的 CA 给管理后台签一张服务器证书（替代那个 CN=drouter.local 的自签）
#   3. 把某张服务器证书「部署」成 Web 管理端正在用的那张，并负责别把门给锁上
#   4. 对任意 host:port 做一次 TLS 握手体检（协议 / 套件 / 证书链 / 校验结论）
#   5.（不做）ACME / Let's Encrypt 自动签发 —— 这台机器没有固定公网 80 口，
#      HTTP-01 走不通；DNS-01 又要各家 API 密钥。有域名证书就在这里直接导入。
#
# 最危险的一步是「部署」。drouter-web 加载不了证书就等于把自己锁在门外，
# 而用户离这台机器最近的入口就是这个页面。所以部署流程必须是：
#   备份 → 落盘前先验证（证书能解析 / 私钥能解析 / 两者公钥匹配 / 有效期覆盖当下）
#   → 原子替换 → 重启 → 回来握手比对指纹 → 不对就换回备份再重启。
# 每一步的成败都写进 /etc/drouter/ca/last-deploy.json：万一重启打断了 HTTP 响应，
# 用户重新登录后还能看到到底发生了什么，而不是一片空白。
CA_DIR = '/etc/drouter/ca'
CA_INDEX = os.path.join(CA_DIR, 'index.json')
CA_BACKUP = os.path.join(CA_DIR, 'backup')
CA_LAST = os.path.join(CA_DIR, 'last-deploy.json')
CA_DIR_MODE = 0o700                        # 库里有私钥
CA_KEY_MODE = 0o600
CA_CRT_MODE = 0o644
# Web 管理端正在用的那一对。drouter-web 以低权用户 drouter 运行，
# 私钥必须归它且只能它读，否则 HTTPS 直接起不来（实测踩过：root 写出来的
# 600 root-only 私钥会让 drouter-web 报 Permission denied 然后没有 8443）。
WEB_CERT_DIR = '/opt/drouter/certs'
WEB_CRT = os.path.join(WEB_CERT_DIR, 'server.crt')
WEB_KEY = os.path.join(WEB_CERT_DIR, 'server.key')
WEB_OWNER = 'drouter'
WEB_SERVICE = 'drouter-web.service'
WEB_PORT_FILE = '/etc/drouter/web-port'
WEB_PORT_DEFAULT = 8443
# 私钥算法。EC P-256 更省 CPU（这台是 2 核小机器），但个别老客户端不认，
# 所以默认给 RSA 2048 这个谁都吃得下的。
CA_KEY_TYPES = (
    ('rsa:2048', 'RSA 2048（兼容性最好，推荐）'),
    ('rsa:4096', 'RSA 4096（更慢，签发与握手都更耗）'),
    ('ec:prime256v1', 'EC P-256（更快更省，老设备可能不认）'),
)
CA_DAYS_MIN, CA_DAYS_MAX = 1, 36500
CA_CA_DAYS_DEFAULT = 3650                  # 自签 CA 十年：它是根，不常换
CA_SRV_DAYS_DEFAULT = 825                  # 服务器证书：对齐业界常见的两年多一点
CA_EXPIRE_WARN = 30                        # 剩余天数低于此值就在页面上提醒
CA_KINDS = ('ca', 'server', 'csr')
CA_KIND_CN = {'ca': 'CA 证书', 'server': '服务器证书', 'csr': '证书签名请求'}
CA_SAN_MAX = 32                            # SAN 条目上限，防止有人填一万个
SSL_PORT_MIN, SSL_PORT_MAX = 1, 65535
SSL_HOST_MAX = 253
SSL_TIMEOUT_DEFAULT = 8
SSL_TIMEOUT_MAX = 30
# 校验结论：openssl 的 verify return code 是数字，界面上要给人话。
# 编号对照 openssl/x509_vfy.h，别凭印象填 —— 第一版把 21 写成了「链里有证书
# 不是 CA 证书」（那是 24），实测私有 CA 签发的证书返回的正是 21，于是页面上
# 显示了一句完全对不上的原因。
SSL_VERIFY_CN = {
    0: '受信任（证书链完整且可验证）',
    2: '找不到该证书的颁发者（链不完整，服务端没把中间证书一起发来）',
    9: '证书还没生效（本机时间与证书有效期对不上）',
    10: '证书已过期',
    18: '自签名证书（不在信任库里）',
    19: '链里有一张自签名的证书，但不在信任库里',
    20: '找不到可信任的根证书',
    21: '找不到颁发者，验证不了证书签名（私有 CA 签发时就是这个）',
    22: '证书链太长（超过 100 层）',
    23: '证书已被吊销',
    24: '链里有一张证书不是 CA 证书',
    25: '证书链长度超过了 CA 允许的范围',
    26: '证书用途不对（不是服务器证书）',
    27: '证书不被信任',
    28: '证书被明确拒绝',
    62: '主机名与证书不匹配',
}


def _ca_rand(n=4):
    import secrets
    return secrets.token_hex(n)


def _ca_now():
    return datetime.now()


def _ca_id():
    return 'c' + _ca_now().strftime('%Y%m%d%H%M%S') + _ca_rand(3)


def _ca_ensure_dir():
    try:
        os.makedirs(CA_DIR, exist_ok=True)
        os.makedirs(CA_BACKUP, exist_ok=True)
        os.chmod(CA_DIR, CA_DIR_MODE)
        os.chmod(CA_BACKUP, CA_DIR_MODE)
    except Exception:
        pass


def _ca_index_load():
    """读取证书库索引。文件坏了不能让整个页面打不开 —— 返回空列表并标记。"""
    broken = False
    items = []
    try:
        if os.path.isfile(CA_INDEX):
            with open(CA_INDEX, encoding='utf-8') as f:
                d = json.load(f)
            if isinstance(d, list):
                items = [x for x in d if isinstance(x, dict)]
            elif isinstance(d, dict):
                items = [x for x in (d.get('items') or []) if isinstance(x, dict)]
            else:
                broken = True
    except Exception:
        broken = True
    return items, broken


def _ca_index_save(items):
    _ca_ensure_dir()
    tmp = CA_INDEX + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CA_INDEX)
    os.chmod(CA_INDEX, 0o600)
    return True


def _ca_paths(cid):
    """证书 / 私钥 / CSR 的固定落盘位置。cid 由本模块生成，不接受外部传入路径。"""
    if not re.match(r'^c[0-9a-f]{14,32}$', cid or ''):
        return None
    return {
        'crt': os.path.join(CA_DIR, cid + '.crt'),
        'key': os.path.join(CA_DIR, cid + '.key'),
        'csr': os.path.join(CA_DIR, cid + '.csr'),
    }


def _ca_find(items, cid):
    for it in items:
        if it.get('id') == cid:
            return it
    return None


def _ca_days(text):
    """把 openssl 的 notAfter 文本（如 Sep 24 12:52:20 2036 GMT）解析成剩余天数。

    解析不了返回 None —— 宁可显示「未知」也不要显示一个假的 0（那会让人以为
    证书已经过期，慌慌张张去换一张其实还能用两年的）。
    """
    if not text:
        return None
    s = text.strip()
    m = re.search(r'notAfter=(.+)$', s)
    if m:
        s = m.group(1).strip()
    for fmt in ('%b %d %H:%M:%S %Y %Z', '%b %d %H:%M:%S %Y',
                '%Y-%m-%d %H:%M:%S', '%Y%m%d%H%M%SZ'):
        try:
            dt = datetime.strptime(s, fmt)
            return (dt - _ca_now()).days
        except Exception:
            continue
    return None


def _ca_x509_info(path):
    """解析一张证书（PEM/DER），返回结构化信息。读不出来返回 {'err': ...}。

    统一走 openssl x509 而不是自己解 ASN.1：openssl 是这台机器本来就有的，
    而且它的日期/指纹格式稳定，自己写解析器反而容易在边界上出错。
    """
    if not path or not os.path.isfile(path):
        return {'err': '证书文件不存在'}
    rc, out, err = sh(['openssl', 'x509', '-in', path, '-noout',
                       '-subject', '-issuer', '-dates', '-fingerprint', '-sha256',
                       '-text'], timeout=20)
    if rc != 0 or not out:
        return {'err': (err or '证书解析失败')[:200]}
    d = {'subject': '', 'issuer': '', 'not_before': '', 'not_after': '',
         'days_left': None, 'fingerprint': '', 'is_ca': False,
         'key_alg': '', 'sig_alg': '', 'san': []}
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.startswith('subject='):
            d['subject'] = ln[len('subject='):].strip()
        elif ln.startswith('issuer='):
            d['issuer'] = ln[len('issuer='):].strip()
        elif ln.startswith('notBefore='):
            d['not_before'] = ln[len('notBefore='):].strip()
        elif ln.startswith('notAfter='):
            d['not_after'] = ln[len('notAfter='):].strip()
        elif 'Fingerprint' in ln and '=' in ln:
            d['fingerprint'] = ln.split('=', 1)[1].strip()
    d['days_left'] = _ca_days(d['not_after'])
    blob = out
    # SAN 在 -text 输出里是 "X509v3 Subject Alternative Name:" 之后缩进的一行
    m = re.search(r'X509v3 Subject Alternative Name:\s*\n\s*(.+)', blob)
    if m:
        d['san'] = [x.strip() for x in m.group(1).split(',') if x.strip()]
    d['is_ca'] = ('CA:TRUE' in blob or 'Certificate Authority' in blob
                  or 'X509v3 Basic Constraints: critical\n                CA:TRUE' in blob)
    m = re.search(r'Public Key Algorithm:\s*(.+)', blob)
    if m:
        d['key_alg'] = m.group(1).strip()
    m = re.search(r'Signature Algorithm:\s*(.+)', blob)
    if m:
        d['sig_alg'] = m.group(1).strip()
    m = re.search(r'Public-Key:\s*\((\d+)\s*bit\)', blob)
    if m:
        d['key_bits'] = int(m.group(1))
    return d


def _ca_key_match(crt, key):
    """证书与私钥是否配对。这是部署前的最后一道闸 —— 两者不配对，
    drouter-web 起来就是「没有 8443」，用户连页面都打不开。
    """
    if not (os.path.isfile(crt) and os.path.isfile(key)):
        return False, '证书或私钥文件缺失'
    rc1, out1, e1 = sh(['openssl', 'x509', '-in', crt, '-noout', '-pubkey'], timeout=20)
    if rc1 != 0:
        return False, '证书解析失败：%s' % (e1 or '')[:150]
    rc2, out2, e2 = sh(['openssl', 'pkey', '-in', key, '-pubout'], timeout=20)
    if rc2 != 0:
        return False, '私钥解析失败：%s' % (e2 or '')[:150]
    n1 = ''.join((out1 or '').split())
    n2 = ''.join((out2 or '').split())
    if not n1 or not n2:
        return False, '取不到公钥，无法比对'
    if n1 != n2:
        return False, '证书与私钥不配对（公钥不一致），部署后 Web 会起不来'
    return True, ''


def _ca_web_port():
    """Web 管理端口。换证书后要在这个端口上做握手验证，读错了就白验一场。"""
    cand = ''
    try:
        if os.path.isfile(WEB_PORT_FILE):
            with open(WEB_PORT_FILE, encoding='utf-8') as f:
                cand = (f.read().strip().splitlines() or [''])[0].strip()
    except Exception:
        cand = ''
    try:
        n = int(cand)
        if SSL_PORT_MIN <= n <= SSL_PORT_MAX:
            return n
    except Exception:
        pass
    return WEB_PORT_DEFAULT


def _ca_local_ips():
    """生成服务器证书时默认写进 SAN 的地址。"""
    ips = ['127.0.0.1']
    lan = _print_lan_ip()
    if lan and lan not in ips:
        ips.append(lan)
    rc, out, _e = sh(['hostname', '-I'], timeout=8)
    for x in (out or '').split():
        if re.match(r'^\d+\.\d+\.\d+\.\d+$', x) and x not in ips:
            ips.append(x)
    return ips


def _ca_norm_days(v, default):
    """有效期天数。注意别写成 int(v or default) —— 0 是 falsy 会被当没填。"""
    if v in (None, ''):
        return default
    try:
        n = int(v)
    except Exception:
        return default
    return max(CA_DAYS_MIN, min(n, CA_DAYS_MAX))


def _ca_norm_san(dns, ips):
    """把用户填的 SAN 收敛成 openssl 认识的 'DNS:a,DNS:b,IP:1.2.3.4'。

    每个条目都过滤一遍：openssl 的 subjectAltName 里出现逗号或换行会让整条
    配置解析错位，签出来的是一张谁都不认的证书。
    """
    out = []
    for x in (dns or [])[:CA_SAN_MAX]:
        s = str(x).strip()
        if not s or len(s) > SSL_HOST_MAX:
            continue
        if not re.match(r'^[A-Za-z0-9.*_-]+(\.[A-Za-z0-9*_-]+)*$', s):
            continue
        out.append('DNS:' + s)
    for x in (ips or [])[:CA_SAN_MAX]:
        s = str(x).strip()
        try:
            ipaddress.ip_address(s)
        except Exception:
            continue
        out.append('IP:' + s)
    return ','.join(out)


def _ca_status():
    items, broken = _ca_index_load()
    out = []
    web_fp = ''
    web_crt = _ca_x509_info(WEB_CRT)
    if not web_crt.get('err'):
        web_fp = web_crt.get('fingerprint', '')
    cur_id = ''
    for it in items:
        p = _ca_paths(it.get('id'))
        if not p:
            continue
        info = _ca_x509_info(p['crt'])
        fp = info.get('fingerprint', '')
        # 正在用的那张是按指纹认的，不靠索引里记的字段 —— 索引可能被手改过
        if fp and fp == web_fp:
            cur_id = it.get('id')
        out.append({
            'id': it.get('id'),
            'name': it.get('name') or it.get('id'),
            'kind': it.get('kind'),
            'kind_cn': CA_KIND_CN.get(it.get('kind'), it.get('kind') or ''),
            'has_key': os.path.isfile(p['key']),
            'has_csr': os.path.isfile(p['csr']),
            'created': it.get('created') or '',
            'can_sign': it.get('kind') == 'ca' and os.path.isfile(p['key']),
            'in_use': bool(fp and fp == web_fp),
            'subject': info.get('subject') or ('—' if not info.get('err') else ''),
            'issuer': info.get('issuer') or '',
            'not_before': info.get('not_before') or '',
            'not_after': info.get('not_after') or '',
            'days_left': info.get('days_left'),
            'fingerprint': fp,
            'is_ca': bool(info.get('is_ca')),
            'san': info.get('san') or [],
            'key_alg': info.get('key_alg') or '',
            'sig_alg': info.get('sig_alg') or '',
            'err': info.get('err') or '',
        })
    web_days = web_crt.get('days_left')
    return {
        'items': out,
        'index_broken': broken,
        'web': {
            'installed': not bool(web_crt.get('err')),
            'subject': web_crt.get('subject') or '',
            'issuer': web_crt.get('issuer') or '',
            'not_before': web_crt.get('not_before') or '',
            'not_after': web_crt.get('not_after') or '',
            'days_left': web_days,
            'fingerprint': web_fp,
            'san': web_crt.get('san') or [],
            'key_alg': web_crt.get('key_alg') or '',
            'sig_alg': web_crt.get('sig_alg') or '',
            'self_signed': bool(web_crt.get('subject')
                                and web_crt.get('subject') == web_crt.get('issuer')),
            'in_lib': cur_id,
            'err': web_crt.get('err') or '',
        },
        'warn_days': CA_EXPIRE_WARN,
        'web_port': _ca_web_port(),
        'openssl': _ca_openssl_version(),
        'last': _ca_last_load(),
        'key_types': [{'v': v, 'n': n} for v, n in CA_KEY_TYPES],
        'default_ips': _ca_local_ips(),
        'build_mode': in_build_mode(),
    }


def _ca_openssl_version():
    rc, out, _e = sh(['openssl', 'version'], timeout=10)
    if rc == 0 and out:
        return out.strip()
    return ''


def _ca_last_load():
    try:
        if os.path.isfile(CA_LAST):
            with open(CA_LAST, encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _ca_last_save(rec):
    """部署结果落盘。

    重启 drouter-web 有可能把这次 HTTP 响应一起带走（helper 与 web 在同一个
    systemd cgroup 里，restart 会连 helper 一起收）。结果先写盘，用户重新登录
    后再进这一页就能看到到底成没成，而不是对着一个断开的请求发呆。
    """
    try:
        _ca_ensure_dir()
        rec = dict(rec or {})
        rec['ts'] = _ca_now().strftime('%Y-%m-%d %H:%M:%S')
        tmp = CA_LAST + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        os.replace(tmp, CA_LAST)
        os.chmod(CA_LAST, 0o600)
    except Exception:
        pass


def _ca_gen_key(key_type, path):
    """生成私钥。返回 (ok, err)。"""
    alg = (key_type or CA_KEY_TYPES[0][0]).strip()
    if alg not in [v for v, _n in CA_KEY_TYPES]:
        alg = CA_KEY_TYPES[0][0]
    if alg.startswith('ec:'):
        args = ['openssl', 'ecparam', '-name', alg[3:], '-genkey', '-noout', '-out', path]
    else:
        try:
            bits = int(alg.split(':')[1])
        except Exception:
            bits = 2048
        args = ['openssl', 'genrsa', '-out', path, str(bits)]
    rc, _o, e = sh(args, timeout=120)
    if rc != 0:
        return False, (e or '私钥生成失败')[:200]
    os.chmod(path, CA_KEY_MODE)
    return True, ''


def _ca_cn_ok(s):
    """CN 合法性：不允许逗号（会让 -subj 解析错位）与超长。"""
    s = (s or '').strip()
    if not s or len(s) > 64:
        return False
    return not re.search(r'[,;=\n]', s)


def _ca_subj(cn, org=''):
    """生成 -subj 参数。CN 里带逗号会让 openssl 把后面当成下一个字段，
    签出来的证书主题是乱的，所以上面先把非法字符挡掉。
    """
    parts = ['/CN=%s' % cn]
    if org and not re.search(r'[,;=\n]', org) and len(org) <= 64:
        parts.append('/O=%s' % org)
    return ''.join(parts)


def _ca_new_ca_op(p):
    """创建一张自签 CA（根证书 + 私钥）。"""
    name = str(p.get('name') or '').strip()[:40]
    cn = str(p.get('cn') or '').strip()
    if not cn:
        return fail('请填写证书名称（CN）', 'BAD_CN')
    if not _ca_cn_ok(cn):
        return fail('证书名称不能包含逗号、等号、分号，长度不超过 64 个字符', 'BAD_CN')
    days = _ca_norm_days(p.get('days'), CA_CA_DAYS_DEFAULT)
    _ca_ensure_dir()
    cid = _ca_id()
    pa = _ca_paths(cid)
    okk, e = _ca_gen_key(p.get('key_type'), pa['key'])
    if not okk:
        return fail('私钥生成失败：%s' % e, 'KEY_FAIL')
    cnf = os.path.join(CA_DIR, cid + '.cnf')
    with open(cnf, 'w', encoding='utf-8') as f:
        f.write('[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n'
                '[dn]\nCN=%s\n[v3]\nbasicConstraints=critical,CA:TRUE\n'
                'keyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n'
                % cn)
    rc, _o, e2 = sh(['openssl', 'req', '-x509', '-new', '-key', pa['key'],
                     '-out', pa['crt'], '-days', str(days), '-config', cnf],
                    timeout=90)
    cleanup_tmp(cnf)
    if rc != 0:
        cleanup_tmp(pa['crt'], pa['key'])
        return fail('CA 生成失败：%s' % (e2 or '')[:200], 'GEN_FAIL')
    os.chmod(pa['crt'], CA_CRT_MODE)
    items, _b = _ca_index_load()
    items.append({'id': cid, 'name': name or cn, 'kind': 'ca', 'cn': cn,
                  'days': days, 'key_type': (p.get('key_type') or CA_KEY_TYPES[0][0]),
                  'created': _ca_now().strftime('%Y-%m-%d %H:%M:%S')})
    _ca_index_save(items)
    log('warn', 'ca', 'CA_NEW', '已创建自签 CA：%s' % (name or cn), {'id': cid})
    return ok({'id': cid}, 'CA「%s」已创建，有效期 %d 天。可以用它给管理后台签服务器证书了。'
              % (name or cn, days))


def _ca_sign_op(p):
    """用库里的 CA 签一张服务器证书。"""
    items, _b = _ca_index_load()
    it = _ca_find(items, str(p.get('ca_id') or ''))
    if not it or it.get('kind') != 'ca':
        return fail('请先选择一张 CA 证书', 'NO_CA')
    pa = _ca_paths(it['id'])
    if not os.path.isfile(pa['key']):
        return fail('这张 CA 没有私钥，签不了（导入时没带私钥）', 'NO_CA_KEY')
    cn = str(p.get('cn') or '').strip()
    if not cn:
        return fail('请填写证书名称（CN）', 'BAD_CN')
    if not _ca_cn_ok(cn):
        return fail('证书名称不能包含逗号、等号、分号，长度不超过 64 个字符', 'BAD_CN')
    san = _ca_norm_san(p.get('san_dns'), p.get('san_ip'))
    if not san:
        return fail('至少填一个域名（DNS）或 IP，否则浏览器一律报「名称不匹配」', 'NO_SAN')
    days = _ca_norm_days(p.get('days'), CA_SRV_DAYS_DEFAULT)
    _ca_ensure_dir()
    cid = _ca_id()
    pb = _ca_paths(cid)
    okk, e = _ca_gen_key(p.get('key_type'), pb['key'])
    if not okk:
        return fail('私钥生成失败：%s' % e, 'KEY_FAIL')
    cnf = os.path.join(CA_DIR, cid + '.cnf')
    with open(cnf, 'w', encoding='utf-8') as f:
        f.write('[req]\ndistinguished_name=dn\nreq_extensions=v3\nprompt=no\n'
                '[dn]\nCN=%s\n[v3]\nsubjectAltName=%s\nbasicConstraints=CA:FALSE\n'
                'keyUsage=critical,digitalSignature,keyEncipherment\n'
                'extendedKeyUsage=serverAuth\n' % (cn, san))
    csr = os.path.join(CA_DIR, cid + '.sign.csr')
    rc, _o, e1 = sh(['openssl', 'req', '-new', '-key', pb['key'], '-out', csr,
                     '-config', cnf], timeout=60)
    if rc != 0:
        cleanup_tmp(cnf, csr, pb['key'])
        return fail('证书请求生成失败：%s' % (e1 or '')[:200], 'CSR_FAIL')
    ext = os.path.join(CA_DIR, cid + '.ext.cnf')
    with open(ext, 'w', encoding='utf-8') as f:
        f.write('subjectAltName=%s\nbasicConstraints=CA:FALSE\n'
                'keyUsage=critical,digitalSignature,keyEncipherment\n'
                'extendedKeyUsage=serverAuth\n' % san)
    # -CAcreateserial：第一次签发时没有 .srl 序列号文件，不加会直接失败
    rc, _o, e2 = sh(['openssl', 'x509', '-req', '-in', csr, '-CA', pa['crt'],
                     '-CAkey', pa['key'], '-CAcreateserial', '-out', pb['crt'],
                     '-days', str(days), '-sha256', '-extfile', ext], timeout=90)
    cleanup_tmp(cnf, ext, csr)
    if rc != 0:
        cleanup_tmp(pb['crt'], pb['key'])
        return fail('签发失败：%s' % (e2 or '')[:200], 'SIGN_FAIL')
    os.chmod(pb['crt'], CA_CRT_MODE)
    items, _b = _ca_index_load()
    items.append({'id': cid, 'name': str(p.get('name') or '').strip()[:40] or cn,
                  'kind': 'server', 'cn': cn, 'days': days, 'ca_id': it['id'],
                  'san': san, 'key_type': (p.get('key_type') or CA_KEY_TYPES[0][0]),
                  'created': _ca_now().strftime('%Y-%m-%d %H:%M:%S')})
    _ca_index_save(items)
    log('warn', 'ca', 'CA_SIGN', '已用 CA 签发服务器证书：%s' % cn,
        {'id': cid, 'ca': it['id'], 'san': san})
    return ok({'id': cid},
              '服务器证书「%s」已签发（%d 天）。要去「部署给管理后台」才会真正生效。'
              % (cn, days))


def _ca_csr_op(p):
    """生成私钥 + CSR，供用户拿去公共 CA 签名。"""
    cn = str(p.get('cn') or '').strip()
    if not cn:
        return fail('请填写域名（CN）', 'BAD_CN')
    if not _ca_cn_ok(cn):
        return fail('域名不能包含逗号、等号、分号，长度不超过 64 个字符', 'BAD_CN')
    san = _ca_norm_san(p.get('san_dns'), p.get('san_ip'))
    _ca_ensure_dir()
    cid = _ca_id()
    pa = _ca_paths(cid)
    okk, e = _ca_gen_key(p.get('key_type'), pa['key'])
    if not okk:
        return fail('私钥生成失败：%s' % e, 'KEY_FAIL')
    cnf = os.path.join(CA_DIR, cid + '.cnf')
    with open(cnf, 'w', encoding='utf-8') as f:
        f.write('[req]\ndistinguished_name=dn\nreq_extensions=v3\nprompt=no\n'
                '[dn]\nCN=%s\n[v3]\nsubjectAltName=%s\n' % (cn, san or ('DNS:' + cn)))
    rc, _o, e2 = sh(['openssl', 'req', '-new', '-key', pa['key'], '-out', pa['csr'],
                     '-config', cnf], timeout=60)
    cleanup_tmp(cnf)
    if rc != 0:
        cleanup_tmp(pa['csr'], pa['key'])
        return fail('CSR 生成失败：%s' % (e2 or '')[:200], 'CSR_FAIL')
    os.chmod(pa['csr'], CA_CRT_MODE)
    try:
        csr_text = open(pa['csr'], encoding='utf-8').read()
    except Exception:
        csr_text = ''
    items, _b = _ca_index_load()
    items.append({'id': cid, 'name': str(p.get('name') or '').strip()[:40] or cn,
                  'kind': 'csr', 'cn': cn, 'san': san,
                  'created': _ca_now().strftime('%Y-%m-%d %H:%M:%S')})
    _ca_index_save(items)
    log('info', 'ca', 'CA_CSR', '已生成 CSR：%s' % cn, {'id': cid})
    return ok({'id': cid, 'csr': csr_text},
              'CSR 已生成。把它交给证书机构，签回来的证书在下面「导入」里选'
              '「补全这张请求」即可。')


def _ca_import_op(p):
    """导入证书。三种来源：粘贴 PEM / 本机文件路径 / 补全已有 CSR。"""
    items, _b = _ca_index_load()
    kind = str(p.get('kind') or 'server').strip()
    if kind not in ('ca', 'server'):
        return fail('导入类型必须是 ca 或 server', 'BAD_KIND')
    cert_pem = str(p.get('cert') or '').strip()
    key_pem = str(p.get('key') or '').strip()
    src = str(p.get('path') or '').strip()
    if src and not cert_pem:
        if not os.path.isfile(src):
            return fail('文件不存在：%s' % src, 'NOT_FOUND')
        try:
            cert_pem = open(src, encoding='utf-8', errors='replace').read()
        except Exception as e:
            return fail('读取失败：%s' % e, 'READ_FAIL')
    if not cert_pem:
        return fail('请粘贴证书内容或填写文件路径', 'NO_CERT')
    # 一个 PEM 里可能同时含证书和私钥（很多面板导出的就是这种合并文件），
    # 拆开而不是要求用户自己分两段粘。
    if 'BEGIN PRIVATE KEY' in cert_pem or 'BEGIN RSA PRIVATE KEY' in cert_pem \
            or 'BEGIN EC PRIVATE KEY' in cert_pem:
        if not key_pem:
            key_pem = cert_pem
    _ca_ensure_dir()
    cid = str(p.get('complete_id') or '').strip() or _ca_id()
    pa = _ca_paths(cid)
    if not pa:
        return fail('要补全的记录 ID 不合法', 'BAD_ID')
    tmp_crt = os.path.join(CA_DIR, cid + '.import.crt')
    with open(tmp_crt, 'w', encoding='utf-8') as f:
        f.write(cert_pem + ('\n' if not cert_pem.endswith('\n') else ''))
    info = _ca_x509_info(tmp_crt)
    if info.get('err'):
        cleanup_tmp(tmp_crt)
        return fail('这不像是合法的 PEM 证书：%s' % info['err'], 'BAD_CERT')
    # 类型要和内容对得上：拿 CA 当服务器证书部署，浏览器照样不认
    if kind == 'ca' and not info.get('is_ca'):
        cleanup_tmp(tmp_crt)
        return fail('这张证书不是 CA 证书（basicConstraints 里没有 CA:TRUE），'
                    '请按「服务器证书」导入', 'NOT_CA')
    tmp_key = None
    if key_pem:
        tmp_key = os.path.join(CA_DIR, cid + '.import.key')
        with open(tmp_key, 'w', encoding='utf-8') as f:
            f.write(key_pem + ('\n' if not key_pem.endswith('\n') else ''))
        m, why = _ca_key_match(tmp_crt, tmp_key)
        if not m:
            cleanup_tmp(tmp_crt, tmp_key)
            return fail(why, 'KEY_MISMATCH')
    os.replace(tmp_crt, pa['crt'])
    os.chmod(pa['crt'], CA_CRT_MODE)
    if tmp_key:
        os.replace(tmp_key, pa['key'])
        os.chmod(pa['key'], CA_KEY_MODE)
    # 补全 CSR：签回来的证书来了，那条 csr 记录就升级成 server
    rec = _ca_find(items, cid)
    if rec:
        rec['kind'] = kind
        rec['name'] = str(p.get('name') or '').strip()[:40] or rec.get('name') or info.get('subject') or cid
        rec['cn'] = info.get('subject') or rec.get('cn') or ''
        rec['imported'] = _ca_now().strftime('%Y-%m-%d %H:%M:%S')
        rec.pop('csr', None)
    else:
        items.append({'id': cid,
                      'name': str(p.get('name') or '').strip()[:40] or (info.get('subject') or cid),
                      'kind': kind, 'cn': info.get('subject') or '',
                      'imported': _ca_now().strftime('%Y-%m-%d %H:%M:%S'),
                      'created': _ca_now().strftime('%Y-%m-%d %H:%M:%S')})
    _ca_index_save(items)
    log('warn', 'ca', 'CA_IMPORT', '已导入%s：%s' % (CA_KIND_CN.get(kind, kind),
                                                 info.get('subject') or cid), {'id': cid})
    more = '' if tmp_key else '（未带私钥，只能用来做信任锚点，不能签发或部署）'
    return ok({'id': cid, 'days_left': info.get('days_left')},
              '已导入%s「%s」%s' % (CA_KIND_CN.get(kind, kind),
                                info.get('subject') or cid, more))


def _ca_delete_op(p):
    """删除证书。正在用的那张不给删 —— 删了等于让当前会话凭空断掉。"""
    items, _b = _ca_index_load()
    it = _ca_find(items, str(p.get('id') or ''))
    if not it:
        return fail('找不到这条记录', 'NOT_FOUND')
    pa = _ca_paths(it['id'])
    webfp = ''
    w = _ca_x509_info(WEB_CRT)
    if not w.get('err'):
        webfp = w.get('fingerprint', '')
    cur = _ca_x509_info(pa['crt']) if pa else {}
    if pa and webfp and cur.get('fingerprint') == webfp:
        return fail('这张证书正在被管理后台使用。请先换成别的证书再删除。', 'IN_USE')
    if pa:
        cleanup_tmp(pa['crt'], pa['key'], pa['csr'])
    rest = [x for x in items if x.get('id') != it['id']]
    _ca_index_save(rest)
    log('warn', 'ca', 'CA_DELETE', '已删除证书：%s' % (it.get('name') or it['id']),
        {'id': it['id']})
    return ok({}, '已删除「%s」' % (it.get('name') or it['id']))


def _ca_backup_web():
    """备份当前 Web 证书，返回备份路径对（失败返回 None）。"""
    if not (os.path.isfile(WEB_CRT) and os.path.isfile(WEB_KEY)):
        return None
    _ca_ensure_dir()
    ts = _ca_now().strftime('%Y%m%d-%H%M%S')
    bc = os.path.join(CA_BACKUP, 'web-%s.crt' % ts)
    bk = os.path.join(CA_BACKUP, 'web-%s.key' % ts)
    try:
        shutil.copy2(WEB_CRT, bc)
        shutil.copy2(WEB_KEY, bk)
        os.chmod(bk, CA_KEY_MODE)
        return (bc, bk)
    except Exception:
        return None


def _ca_install_web(crt_src, key_src):
    """把一对证书原子地装到 Web 证书位置，并交给 drouter 用户。

    先写临时文件再 os.replace：直接覆写会导致「文件已经换成新的、但写一半
    被重启打断」，drouter-web 起来就读到半张证书。
    """
    try:
        os.makedirs(WEB_CERT_DIR, exist_ok=True)
        tc = WEB_CRT + '.tmp'
        tk = WEB_KEY + '.tmp'
        shutil.copy2(crt_src, tc)
        shutil.copy2(key_src, tk)
        os.chmod(tc, CA_CRT_MODE)
        os.chmod(tk, CA_KEY_MODE)
        try:
            import pwd
            pw = pwd.getpwnam(WEB_OWNER)
            os.chown(tc, pw.pw_uid, pw.pw_gid)
            os.chown(tk, pw.pw_uid, pw.pw_gid)
        except Exception:
            # 拿不到 drouter 用户就退回 0644/0600：至少证书能读，
            # 私钥 0600 root 会让 8443 起不来，这种情况宁可放弃部署。
            pass
        os.replace(tc, WEB_CRT)
        os.replace(tk, WEB_KEY)
        return True, ''
    except Exception as e:
        return False, str(e)


def _ca_probe_web(port, tries=20, want_fp=''):
    """重启后回来确认：端口在听、能握手、而且用的确实是新证书。

    systemctl restart 返回 0 只表示 systemd 收下了任务，不代表进程活着 ——
    这个坑在别处也踩过。所以必须自己握手一次，比对指纹。
    """
    why = ''
    for _i in range(tries):
        # insecure=True 是刻意的：这里只关心「TLS 起来了、用的是不是新证书」，
        # 不关心信任链。用默认上下文去校验的话，私有 CA 签的证书必然校验失败，
        # 于是明明部署成功了却被判失败并回滚 —— 那才是冤枉。
        m, fp, _e = _ca_handshake('127.0.0.1', port, '127.0.0.1',
                                  timeout=3, insecure=True)
        if m:
            if want_fp and fp and fp != want_fp:
                return False, '端口起来了但用的还是旧证书（指纹没变）'
            return True, ''
        why = _e or '端口没应答'
        time.sleep(1)
    return False, why


def _ca_deploy_op(p):
    """部署某张服务器证书为 Web 管理端证书。整条链路失败自动回滚。"""
    items, _b = _ca_index_load()
    it = _ca_find(items, str(p.get('id') or ''))
    if not it:
        return fail('找不到这条证书记录', 'NOT_FOUND')
    if it.get('kind') != 'server':
        return fail('只有服务器证书能部署给管理后台（当前是%s）'
                    % CA_KIND_CN.get(it.get('kind'), it.get('kind') or '未知类型'),
                    'BAD_KIND')
    pa = _ca_paths(it['id'])
    if not os.path.isfile(pa['crt']):
        return fail('这张记录还没有证书文件，先用签回来的证书补全它', 'NO_CRT')
    if not os.path.isfile(pa['key']):
        return fail('这张证书没有私钥，部署后 HTTPS 起不来', 'NO_KEY')
    info = _ca_x509_info(pa['crt'])
    if info.get('err'):
        return fail('证书解析失败：%s' % info['err'], 'BAD_CERT')
    # 部署前的四道闸，任何一道不过都不能往下走
    m, why = _ca_key_match(pa['crt'], pa['key'])
    if not m:
        return fail(why, 'KEY_MISMATCH')
    dl = info.get('days_left')
    if dl is None:
        return fail('读不出证书有效期，不敢拿它去换正在用的那张', 'NO_DATES')
    if dl <= 0:
        return fail('这张证书已经过期（%s），换成它等于把自己锁在门外'
                    % (info.get('not_after') or ''), 'EXPIRED')
    bak = _ca_backup_web()
    okd, e = _ca_install_web(pa['crt'], pa['key'])
    if not okd:
        _ca_last_save({'ok': False, 'stage': 'install', 'msg': e})
        return fail('写入失败：%s' % e, 'WRITE_FAIL')
    want_fp = info.get('fingerprint') or ''
    rec = {'id': it['id'], 'name': it.get('name') or it['id'],
           'subject': info.get('subject') or '', 'days_left': dl,
           'backup': (bak[0].rsplit('/', 1)[-1] if bak else ''),
           'fingerprint': want_fp}
    if in_build_mode():
        rec.update({'ok': True, 'stage': 'written',
                    'msg': '构建保护模式：证书已写入磁盘，未重启 Web 服务，'
                           '下次重启 drouter-web 后生效'})
        _ca_last_save(rec)
        log('warn', 'ca', 'CA_DEPLOY', '证书已写入（构建保护模式，未重启）',
            {'id': it['id']})
        return ok({'restarted': False, 'need_restart': True},
                  '证书已写入磁盘。当前处于构建保护模式，没有重启 Web 服务，'
                  '下次重启 drouter-web 后生效。')
    port = _ca_web_port()
    rc, _o, er = sh(['systemctl', 'restart', WEB_SERVICE], timeout=60)
    if rc != 0:
        # 起不来就换回去，不能把用户关在门外
        if bak:
            _ca_install_web(bak[0], bak[1])
            sh(['systemctl', 'restart', WEB_SERVICE], timeout=60)
        rec.update({'ok': False, 'stage': 'restart',
                    'msg': '重启 Web 服务失败，已还原原证书：%s' % (er or '')[:180]})
        _ca_last_save(rec)
        return fail('重启 Web 服务失败，已还原原来的证书：%s' % (er or '')[:200],
                    'RESTART_FAIL')
    up, why = _ca_probe_web(port, tries=20, want_fp=want_fp)
    if not up:
        if bak:
            _ca_install_web(bak[0], bak[1])
            sh(['systemctl', 'restart', WEB_SERVICE], timeout=60)
            _ca_probe_web(port, tries=10)
        rec.update({'ok': False, 'stage': 'verify',
                    'msg': '换证书后 Web 没能正常提供 HTTPS，已还原原证书：%s' % why})
        _ca_last_save(rec)
        return fail('换上后 %d 端口没能握手成功（%s），已自动还原原来的证书。'
                    '原来的证书仍在，页面不受影响。' % (port, why), 'VERIFY_FAIL')
    rec.update({'ok': True, 'stage': 'done',
                'msg': '已生效，剩余 %d 天' % dl, 'port': port})
    _ca_last_save(rec)
    log('warn', 'ca', 'CA_DEPLOY', '管理后台证书已更换为 %s' % (info.get('subject') or it['id']),
        {'id': it['id'], 'days_left': dl})
    return ok({'restarted': True, 'port': port, 'days_left': dl},
              '已生效。Web 服务刚刚重启，请重新登录；新证书剩余 %d 天。' % dl)


def _ca_restore_op(p):
    """恢复默认自签证书：重新生成一张（带本机 IP 的 SAN）并部署。"""
    cn = str(p.get('cn') or '').strip() or 'drouter.local'
    if not _ca_cn_ok(cn):
        return fail('证书名称不合法', 'BAD_CN')
    days = _ca_norm_days(p.get('days'), CA_CA_DAYS_DEFAULT)
    _ca_ensure_dir()
    cid = _ca_id()
    pa = _ca_paths(cid)
    okk, e = _ca_gen_key(p.get('key_type'), pa['key'])
    if not okk:
        return fail('私钥生成失败：%s' % e, 'KEY_FAIL')
    san = _ca_norm_san(['localhost', cn], _ca_local_ips())
    cnf = os.path.join(CA_DIR, cid + '.cnf')
    with open(cnf, 'w', encoding='utf-8') as f:
        f.write('[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n'
                '[dn]\nCN=%s\nO=drouter\n[v3]\nsubjectAltName=%s\n'
                'basicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n' % (cn, san))
    rc, _o, e2 = sh(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                     '-keyout', pa['key'], '-out', pa['crt'], '-days', str(days),
                     '-config', cnf], timeout=120)
    cleanup_tmp(cnf)
    if rc != 0:
        cleanup_tmp(pa['crt'], pa['key'])
        return fail('自签证书生成失败：%s' % (e2 or '')[:200], 'GEN_FAIL')
    os.chmod(pa['crt'], CA_CRT_MODE)
    os.chmod(pa['key'], CA_KEY_MODE)
    items, _b = _ca_index_load()
    items.append({'id': cid, 'name': '默认自签（%s）' % cn, 'kind': 'server',
                  'cn': cn, 'days': days, 'san': san, 'is_default': True,
                  'created': _ca_now().strftime('%Y-%m-%d %H:%M:%S')})
    _ca_index_save(items)
    return _ca_deploy_op({'id': cid})


def _ca_san_entry(s):
    """把一条 SAN 归一化成 'DNS:x' / 'IP:x'。

    openssl x509 -text 在 3.x 里把 IP 打印成 'IP Address:1.2.3.4' 而不是
    'IP:1.2.3.4'。两边形态不一致会让「主机名匹配」永远判 false ——
    表现是明明访问的地址就在证书里，页面却一直报名称不匹配。
    """
    t = (s or '').strip()
    for pre in ('DNS:', 'IP Address:', 'IP:'):
        if t.startswith(pre):
            v = t[len(pre):].strip()
            return ('IP:' if pre != 'DNS:' else 'DNS:') + v
    try:
        ipaddress.ip_address(t)
        return 'IP:' + t
    except Exception:
        return 'DNS:' + t


def _ca_host_match(name, san):
    """主机名与证书 SAN 是否匹配。支持精确匹配与单级通配（*.example.com）。

    ssl.match_hostname 在 Python 3.12 被移除了，这里自己实现一个够用的版本。
    """
    if not name or not san:
        return False
    n = name.strip().lower().rstrip('.')
    cands = [_ca_san_entry(s).lower().rstrip('.') for s in san]
    for c in cands:
        v = c.split(':', 1)[1] if ':' in c else c
        if v == n:
            return True
        if v.startswith('*.') and n.count('.') >= 2:
            if n.split('.', 1)[1] == v[2:]:
                return True
    return False


def _ca_handshake(host, port, sni='', timeout=SSL_TIMEOUT_DEFAULT, insecure=False):
    """做一次 TLS 握手，返回 (ok, 叶子证书指纹, 错误原因)。

    用 Python 标准库而不是 openssl s_client：握手这件事它更快也更可控，
    而证书链的解析另走 _ca_ssltest 里的 s_client（那边要完整链）。
    """
    try:
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
        with _ssl.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=(sni or host)) as ss:
                der = ss.getpeercert(binary_form=True)
                fp = ''
                if der:
                    import hashlib as _hl
                    # 必须是完整 32 字节、冒号分隔大写 —— 与 openssl
                    # -fingerprint -sha256 的输出一字不差。
                    # 曾经这里只取前 16 字节，结果 _ca_probe_web 拿到的指纹永远
                    # 和 _ca_x509_info 的不等，于是明明部署成功了却被判失败并
                    # 回滚（真机验收第一次跑就撞上了）。
                    fp = ':'.join('%02X' % b for b in _hl.sha256(der).digest())
                ss.close()
                return True, fp, ''
    except Exception as e:
        return False, '', str(e)[:180]


def _ca_parse_chain(blob):
    """从 s_client -showcerts 的输出里切出每一张证书，返回 PEM 列表。"""
    out = []
    cur = None
    for ln in (blob or '').splitlines():
        if 'BEGIN CERTIFICATE' in ln:
            cur = [ln]
        elif 'END CERTIFICATE' in ln:
            if cur:
                cur.append(ln)
                out.append('\n'.join(cur) + '\n')
            cur = None
        elif cur is not None:
            cur.append(ln)
    return out


def _ca_ssltest_op(p):
    """SSL / TLS 体检：握手 + 协议 + 套件 + 证书链 + 校验结论。"""
    host = str(p.get('host') or '').strip()
    if not host:
        return fail('请填写要测试的主机', 'NO_HOST')
    if len(host) > SSL_HOST_MAX or re.search(r'\s|[/\\]', host):
        return fail('主机名不合法', 'BAD_HOST')
    # 端口同样别写 int(p.get('port') or 443)：0 是 falsy，会被悄悄换成 443。
    raw_port = p.get('port')
    if raw_port in (None, ''):
        port = 443
    else:
        try:
            port = int(raw_port)
        except Exception:
            return fail('端口必须是数字', 'BAD_PORT')
    if not (SSL_PORT_MIN <= port <= SSL_PORT_MAX):
        return fail('端口必须在 %d-%d 之间' % (SSL_PORT_MIN, SSL_PORT_MAX), 'BAD_PORT')
    try:
        timeout = int(p.get('timeout') or SSL_TIMEOUT_DEFAULT)
    except Exception:
        timeout = SSL_TIMEOUT_DEFAULT
    timeout = max(2, min(timeout, SSL_TIMEOUT_MAX))
    sni = str(p.get('sni') or '').strip() or host
    if len(sni) > SSL_HOST_MAX:
        sni = host
    insecure = bool(p.get('insecure'))
    t0 = time.time()
    args = ['timeout', str(timeout), 'openssl', 's_client', '-connect',
            '%s:%d' % (host, port), '-servername', sni, '-showcerts']
    if insecure:
        args.append('-verify_quiet')
    rc, out, err = sh(args, timeout=timeout + 6, input_data='')
    ms = int((time.time() - t0) * 1000)
    blob = (out or '') + '\n' + (err or '')
    # 连不上的判定只看「有没有拿到证书」。openssl 各版本对连接失败的措辞不一
    # （3.x 打 "connect:errno=111"，老版本打 "connect:Connection refused"），
    # 认关键字比认整句稳。
    if 'BEGIN CERTIFICATE' not in blob:
        low = blob.lower()
        why = '连不上（主机不可达或端口未开放）'
        if 'refused' in low or 'errno=111' in low or 'errno=10061' in low:
            why = '连接被拒绝（端口没在监听）'
        elif 'timed out' in low or 'errno=110' in low or 'errno=10060' in low:
            why = '连接超时（被防火墙丢包或主机不可达）'
        elif ('no such host' in low or 'name resolution' in low
              or 'not known' in low or 'nodename nor servname' in low):
            why = '域名解析失败'
        return ok({'ok': False, 'host': host, 'port': port, 'ms': ms,
                   'why': why, 'chain': [], 'checks': []},
                  '%s:%d —— %s' % (host, port, why))
    proto = ''
    cipher = ''
    m = re.search(r'New,\s*([^,]+),\s*Cipher is\s*(\S+)', blob)
    if m:
        proto = m.group(1).strip()
        cipher = m.group(2).strip()
    else:
        m2 = re.search(r'Protocol\s*:\s*(\S+)', blob)
        m3 = re.search(r'Ciphersuite\s*:\s*(\S+)', blob)
        if m2:
            proto = m2.group(1).strip()
        if m3:
            cipher = m3.group(1).strip()
    vcode = None
    m = re.search(r'Verify return code:\s*(\d+)\s*(?:\((.*?)\))?', blob)
    if m:
        try:
            vcode = int(m.group(1))
        except Exception:
            vcode = None
    vtext = ''
    if vcode is not None:
        vtext = SSL_VERIFY_CN.get(vcode) or ('校验返回码 %d' % vcode)
    chain_pems = _ca_parse_chain(blob)
    chain = []
    _ca_ensure_dir()
    tmpdir = tempfile.mkdtemp(prefix='drouter-ca-', dir='/run' if os.path.isdir('/run') else None)
    try:
        for i, pem in enumerate(chain_pems[:10]):
            fp = os.path.join(tmpdir, 'c%d.pem' % i)
            with open(fp, 'w', encoding='utf-8') as f:
                f.write(pem)
            info = _ca_x509_info(fp)
            chain.append({
                'level': i,
                'subject': info.get('subject') or '',
                'issuer': info.get('issuer') or '',
                'not_before': info.get('not_before') or '',
                'not_after': info.get('not_after') or '',
                'days_left': info.get('days_left'),
                'fingerprint': info.get('fingerprint') or '',
                'is_ca': bool(info.get('is_ca')),
                'san': info.get('san') or [],
                'key_alg': info.get('key_alg') or '',
                'sig_alg': info.get('sig_alg') or '',
                'err': info.get('err') or '',
            })
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
    leaf = chain[0] if chain else {}
    # 逐项体检。每项给「结论 + 人话说明」，让人知道下一步该干什么
    checks = []
    trusted = (vcode == 0)
    checks.append({'k': 'trusted', 'n': '信任链', 'ok': trusted,
                   'd': (vtext or '未能取得校验结果')
                        + ('' if trusted else '。自签 / 私有 CA 属于正常现象 —— '
                           '把根证书装进你的设备就不再提示，加密本身是生效的')})
    dl = leaf.get('days_left')
    if dl is None:
        checks.append({'k': 'expire', 'n': '有效期', 'ok': False, 'd': '读不出有效期'})
    else:
        checks.append({'k': 'expire', 'n': '有效期', 'ok': dl > CA_EXPIRE_WARN,
                       'd': '剩余 %d 天（%s）' % (dl, leaf.get('not_after') or '')
                            + ('' if dl > CA_EXPIRE_WARN else '，快到期了，该换一张')})
    leaf_san = [_ca_san_entry(x) for x in (leaf.get('san') or [])]
    if _ca_is_ip(host):
        hm = ('IP:%s' % host) in leaf_san
        checks.append({'k': 'host', 'n': '名称匹配', 'ok': hm,
                       'd': ('证书的 SAN 里有 %s' % host) if hm
                            else '证书的 SAN 里没有这个 IP，浏览器会报「名称不匹配」'})
    else:
        hm = _ca_host_match(sni or host, leaf.get('san') or [])
        checks.append({'k': 'host', 'n': '名称匹配', 'ok': hm,
                       'd': ('证书覆盖了 %s' % (sni or host)) if hm
                            else '证书的 SAN 里没有 %s，浏览器会报「名称不匹配」' % (sni or host)})
    checks.append({'k': 'chain', 'n': '证书链', 'ok': len(chain) > 0,
                   'd': ('服务端返回了 %d 张证书' % len(chain)) if chain
                        else '没拿到任何证书'})
    if proto:
        weak = proto.strip() in ('TLSv1', 'TLSv1.1', 'SSLv3', 'SSLv2')
        checks.append({'k': 'proto', 'n': '协议版本', 'ok': not weak,
                       'd': proto + ('（已废弃，建议关掉）' if weak else '')})
    return ok({
        'ok': bool(chain) or ('BEGIN CERTIFICATE' in blob),
        'host': host, 'port': port, 'sni': sni, 'ms': ms,
        'proto': proto, 'cipher': cipher,
        'verify_code': vcode, 'verify_text': vtext,
        'chain': chain, 'checks': checks, 'insecure': insecure,
    }, '%s:%d —— %s %s' % (host, port, proto or '握手',
                           vtext or ('校验返回码 %s' % vcode if vcode is not None else '')))


def _ca_is_ip(s):
    try:
        ipaddress.ip_address(s)
        return True
    except Exception:
        return False


def act_ca(p):
    """CA 证书管理 + SSL 测试：status / new_ca / sign / csr / import / delete /
    deploy / restore / ssltest"""
    p = p or {}
    op = str(p.get('op') or 'status')
    if op == 'status':
        return ok(_ca_status(), '已读取')
    if op == 'new_ca':
        return _ca_new_ca_op(p)
    if op == 'sign':
        return _ca_sign_op(p)
    if op == 'csr':
        return _ca_csr_op(p)
    if op == 'import':
        return _ca_import_op(p)
    if op == 'delete':
        return _ca_delete_op(p)
    if op == 'deploy':
        return _ca_deploy_op(p)
    if op == 'restore':
        return _ca_restore_op(p)
    if op == 'ssltest':
        return _ca_ssltest_op(p)
    return fail('不支持的证书操作：%s' % op, 'BAD_OP')


def read_share(p):
    """读取文件共享配置 + 包/服务/目录状态（#9）。"""
    d = _share_load()
    pkgs = _share_pkg_state()
    # 性能：4 个单元原为 8 次 systemctl fork，改批量一次取回（同 read_services）。
    svc = _svc_states(('smbd', 'nmbd', 'nfs-server', 'rpcbind'))
    # 目录状态
    dirs = {}
    for s in (d.get('samba', {}).get('shares') or []):
        pth = s.get('path')
        if pth and pth not in dirs:
            dirs[pth] = _share_dir_meta(pth)
    for e in (d.get('nfs', {}).get('exports') or []):
        pth = e.get('path')
        if pth and pth not in dirs:
            dirs[pth] = _share_dir_meta(pth)

    host = {'lan_ip': _share_lan_ip()}
    hints = _share_client_hints(host, d.get('samba', {}).get('shares') or [])
    return ok({
        'cfg': d,
        'pkgs': pkgs,
        'services': svc,
        'dirs': dirs,
        'dir_templates': SHARE_DIR_TEMPLATES,
        'nfs_presets': NFS_PRESETS,
        'hints': hints,
        'host': host,
        'smb_configured': SMB_INCLUDE_LINE in _safe_read(SMB_MAIN_CONF),
        'note': ('SMB 面向 Windows / macOS / Linux 全平台，兼容性最好；'
                 'NFS 更适合 Linux 之间互传，性能更高但 Windows 需要额外客户端。'
                 '两者可以同时开启，共享同一个目录。'),
    })


def _safe_read(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read()
    except Exception:
        return ''


def act_share(p):
    """文件共享操作：get / save / on / off / mkdir / plan。"""
    p = p or {}
    op = (p.get('op') or 'get').strip()
    d = _share_load()

    if op == 'get':
        return read_share(p)

    if op == 'save':
        for k in ('enable_smb', 'enable_nfs', 'samba', 'nfs'):
            if k in p:
                d[k] = p[k]
        # 用渲染器做一次完整校验，把中文错误原样回给前端
        try:
            if d.get('enable_smb'):
                render.render_samba({'samba': d.get('samba') or {}})
            if d.get('enable_nfs'):
                render.render_nfs({'nfs': d.get('nfs') or {}})
                render.render_nfs_conf({'nfs': d.get('nfs') or {}})
        except Exception as e:
            msg = getattr(e, 'msg_cn', str(e))
            return fail(msg, 'VALIDATE_FAIL')
        _share_save(d)

        results = []
        if d.get('enable_smb'):
            inc_ok, inc_msg = (True, '')
            if p.get('live', True) and not os.path.isfile('/etc/drouter/BUILD_MODE'):
                inc_ok, inc_msg = _ensure_smb_include()
            text = render.render_samba({'samba': d.get('samba') or {}})
            f_ok, f_msg = _share_apply_file('/etc/samba/drouter.conf', text,
                                           bool(p.get('live', True)), 'smbd')
            results.append('SMB：%s%s' % (f_msg, ('；' + inc_msg) if inc_msg and inc_ok else ''))
            if not (f_ok and inc_ok):
                return fail('；'.join(results), 'APPLY_FAIL')
        if d.get('enable_nfs'):
            pairs = render.render_nfs({'nfs': d.get('nfs') or {}})
            for path, text in pairs:
                unit = 'nfs-server' if path == '/etc/exports' else 'nfs-server'
                f_ok, f_msg = _share_apply_file(path, text, bool(p.get('live', True)), unit)
                results.append('NFS：%s' % f_msg)
                if not f_ok:
                    return fail('；'.join(results), 'APPLY_FAIL')
            # exports 变更后需要 exportfs -ra 才能立即生效
            if p.get('live', True) and not os.path.isfile('/etc/drouter/BUILD_MODE'):
                sh(['exportfs', '-ra'], timeout=30)
        if not results:
            results.append('已保存（未启用任何共享）')
        return ok({'config': d}, '；'.join(results))

    if op in ('on', 'off'):
        which = (p.get('which') or 'smb').strip().lower()
        val = (op == 'on')
        if which == 'smb':
            d['enable_smb'] = val
        elif which == 'nfs':
            d['enable_nfs'] = val
        else:
            return fail('未知的共享类型：%s' % which, 'BAD_WHICH')
        _share_save(d)
        name = 'SMB' if which == 'smb' else 'NFS'
        if p.get('live', True) and not os.path.isfile('/etc/drouter/BUILD_MODE'):
            units = ['smbd', 'nmbd'] if which == 'smb' else ['nfs-server', 'rpcbind']
            for u in units:
                sh(['systemctl', 'enable' if val else 'disable', '--now', u], timeout=45)
        return ok({'config': d}, '%s 已%s' % (name, '启用' if val else '停用'))

    if op == 'mkdir':
        path = (p.get('path') or '').strip()
        try:
            path = render.v_path(path, field='目录')
        except Exception as e:
            return fail(getattr(e, 'msg_cn', str(e)), 'BAD_PATH')
        if os.path.isdir(path):
            return ok({'exists': True, 'path': path}, '目录已存在：%s' % path)
        v = (p.get('mode') or 'guest').strip()
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as e:
            return fail('创建目录失败：%s' % e, 'MKDIR_FAIL')
        # 权限：公共盘 0775 + setgid，保证同组可写、新文件继承组
        try:
            os.chmod(path, 0o2775 if v == 'guest' else 0o2770)
        except Exception:
            pass
        return ok({'exists': True, 'path': path}, '已创建目录 %s' % path)

    if op == 'plan':
        out = []
        try:
            if d.get('enable_smb'):
                out.append(('SMB · /etc/samba/drouter.conf',
                            render.render_samba({'samba': d.get('samba') or {}})))
            if d.get('enable_nfs'):
                for path, text in render.render_nfs({'nfs': d.get('nfs') or {}}):
                    out.append(('NFS · %s' % path, text))
        except Exception as e:
            return fail(getattr(e, 'msg_cn', str(e)), 'VALIDATE_FAIL')
        if not out:
            return ok({'files': [], 'text': '未启用任何共享。'})
        text = '\n\n'.join('===== %s =====\n%s' % (n, t) for n, t in out)
        return ok({'files': [n for n, _ in out], 'text': text})

    return fail('不支持的操作：%s' % op, 'BAD_OP')


# ====================================================================
#  外置存储设备（USB / Type-C / 雷电）—— 识别 · 挂载 · 格式化
# ====================================================================
# 用户会在路由器上插 U 盘、移动硬盘、Type-C 硬盘盒、雷电硬盘来做共享盘。
# 这里把「看见盘 → 格式化 → 挂载 → 共享」串成一条线，但对外置设备做了三重保护：
#   1) 系统盘（承载 / /boot /boot/efi /usr /var 等）一律拒绝格式化；
#   2) 已挂载的设备拒绝格式化（必须先卸载）；
#   3) 整块盘若已含分区，拒绝直接格式化整盘（请格式化具体分区）。
FS_TYPES = [
    {'v': 'exfat', 'n': 'exFAT', 'mkfs': 'mkfs.exfat', 'pkg': 'exfatprogs',
     'win': True, 'mac': True, 'linux': True, 'label_opt': '-n',
     'note': '跨平台首选：Windows / macOS / Linux 都能原生读写，无 4GB 单文件限制，最适合移动硬盘。'},
    {'v': 'ext4', 'n': 'ext4', 'mkfs': 'mkfs.ext4', 'pkg': 'e2fsprogs',
     'win': False, 'mac': False, 'linux': True, 'label_opt': '-L',
     'note': 'Linux 首选：日志型、稳定、支持权限与 ACL。Windows / macOS 需装第三方驱动才能读写。'},
    {'v': 'ntfs', 'n': 'NTFS', 'mkfs': 'mkfs.ntfs', 'pkg': 'ntfs-3g',
     'win': True, 'mac': False, 'linux': True, 'label_opt': '-L',
     'note': 'Windows 原生。Linux 写入需 ntfs-3g（内核 5.15+ 已有原生驱动），macOS 默认只读。'},
    {'v': 'vfat', 'n': 'FAT32', 'mkfs': 'mkfs.vfat', 'pkg': 'dosfstools',
     'win': True, 'mac': True, 'linux': True, 'label_opt': '-n',
     'note': '兼容性最强（电视 / 车载 / 相机 / 游戏机都认）。限制：单文件 ≤ 4GB，分区 ≤ 2TB。'},
    {'v': 'xfs', 'n': 'XFS', 'mkfs': 'mkfs.xfs', 'pkg': 'xfsprogs',
     'win': False, 'mac': False, 'linux': True, 'label_opt': '-L',
     'note': '大文件与并发写入表现好，删除大量小文件很快。分区建好后不支持缩容。'},
    {'v': 'btrfs', 'n': 'Btrfs', 'mkfs': 'mkfs.btrfs', 'pkg': 'btrfs-progs',
     'win': False, 'mac': False, 'linux': True, 'label_opt': '-L',
     'note': '支持快照、校验和与在线扩容。Samba 共享建议关掉 CoW（nodatacow）以免碎片。'},
    {'v': 'f2fs', 'n': 'F2FS', 'mkfs': 'mkfs.f2fs', 'pkg': 'f2fs-tools',
     'win': False, 'mac': False, 'linux': True, 'label_opt': '-l',
     'note': '专为闪存（U 盘 / SSD / TF 卡）设计，能明显减少写放大、延长寿命。'},
    {'v': 'ext3', 'n': 'ext3', 'mkfs': 'mkfs.ext3', 'pkg': 'e2fsprogs',
     'win': False, 'mac': False, 'linux': True, 'label_opt': '-L',
     'note': 'ext4 的前代。只在要对接很老的设备时才用。'},
]

# 承载这些挂载点的盘一律视为系统盘，禁止格式化
_CRITICAL_MOUNTS = ('/', '/boot', '/boot/efi', '/efi', '/usr', '/var', '/home',
                    '/opt', '/srv', '/etc', '/tmp', '/root', '[SWAP]')

_FSTAB_BACKUP = os.path.join(GEN, 'fstab.drouter.bak')


def _blk_sysfs(name):
    """取块设备的 sysfs 真实路径，用于判断它是怎么接上来的。"""
    try:
        return os.path.realpath('/sys/class/block/%s' % name)
    except OSError:
        return ''


def _dev_bus(name, sysfs=''):
    """判断连接方式：雷电 / USB / NVMe / SATA / 虚拟。

    雷电（Thunderbolt）与 USB4 外接硬盘盒在 sysfs 路径里会带 thunderbolt 段；
    USB 盘则带 /usb 段。Type-C 只是接口形态，电气上仍可能是 USB 或雷电，
    所以这里展示的是「总线类型」而不是「插头形状」，避免误导。
    """
    low = (sysfs or _blk_sysfs(name)).lower()
    if 'thunderbolt' in low:
        return ('thunderbolt', '雷电 / USB4')
    if '/usb' in low or '/usbmisc' in low:
        return ('usb', 'USB')
    if 'nvme' in low:
        return ('nvme', 'NVMe')
    if '/ata' in low or '/scsi' in low or '/ahci' in low:
        return ('sata', 'SATA / SCSI')
    if 'virtio' in low or 'xen' in low:
        return ('virtual', '虚拟磁盘')
    if '/mmc' in low or 'mmcblk' in name:
        return ('mmc', 'MMC / TF 卡')
    return ('unknown', '未知')


def _lsblk_all():
    """读取块设备树。lsblk 不存在时返回空列表（由前端提示安装 util-linux）。"""
    rc, out, _e = sh(['lsblk', '-J', '-b', '-o',
                      'NAME,KNAME,SIZE,TYPE,FSTYPE,FSSIZE,FSUSED,MOUNTPOINT,MODEL,VENDOR,'
                      'HOTPLUG,TRAN,SERIAL,UUID,LABEL,RO,RM,PKNAME'],
                     timeout=20)
    if rc != 0 or not out:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    return (data.get('blockdevices') or [])


def _walk_blk(nodes, parent=None, out=None, depth=0):
    """把 lsblk 的嵌套树摊平，同时保留父子关系。"""
    if out is None:
        out = []
    for n in nodes or []:
        rec = dict(n)
        rec['_parent'] = parent
        rec['_depth'] = depth
        out.append(rec)
        _walk_blk(n.get('children') or [], n.get('kname') or n.get('name'),
                  out, depth + 1)
    return out


def _sysfs_model(name):
    """lsblk 的 MODEL 常带尾随空格，且虚拟盘没有；回落去 sysfs 读。"""
    for sub in ('device/model', 'device/vendor'):
        try:
            with open('/sys/class/block/%s/%s' % (name, sub),
                      encoding='utf-8', errors='replace') as f:
                v = f.read().strip()
            if v:
                return v
        except OSError:
            continue
    return ''


def _mounted_mounts():
    """当前系统所有挂载点，用于判断某设备是否已挂载、挂在哪。"""
    m = {}
    try:
        with open('/proc/mounts', encoding='utf-8') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    m[parts[0]] = parts[1]
    except OSError:
        pass
    return m


def _storage_device_rows():
    """组装给前端的设备列表。"""
    tree = _lsblk_all()
    if tree is None:
        return None
    flat = _walk_blk(tree)
    by_name = {}
    for r in flat:
        by_name[r.get('kname') or r.get('name')] = r

    rows = []
    for r in flat:
        name = r.get('kname') or r.get('name') or ''
        typ = r.get('type') or ''
        if typ not in ('disk', 'part', 'rom', 'loop'):
            continue
        if typ == 'loop':
            continue  # loop 设备是镜像挂载，跟外置盘无关，列出来只会干扰
        if name.startswith('zram') or name.startswith('ram'):
            continue  # zram 是内存压缩盘（用作 swap），既不能格式化也不该共享
        kids = [c for c in flat
                if c.get('_parent') == name and (c.get('type') or '') == 'part']
        sysfs = _blk_sysfs(name)
        bus, bus_cn = _dev_bus(name, sysfs)
        size = int(r.get('size') or 0)
        hotplug = str(r.get('hotplug') or '0') == '1'
        removable = str(r.get('rm') or '0') == '1'
        # 「外置」的判定：总线是 USB / 雷电，或内核标记为可热插拔 / 可移动。
        # 虚拟机里插的虚拟磁盘也会被标 hotplug，所以用总线的可读性兜底展示。
        external = bus in ('usb', 'thunderbolt', 'mmc') or hotplug or removable
        mp = r.get('mountpoint') or ''
        model = (r.get('model') or '').strip() or _sysfs_model(name)
        vendor = (r.get('vendor') or '').strip()
        rows.append({
            'name': name,
            'dev': '/dev/' + name,
            'type': typ,
            'type_cn': {'disk': '整盘', 'part': '分区', 'rom': '光驱'}.get(typ, typ),
            'size': size,
            'size_h': _hbytes(size),
            'fstype': r.get('fstype') or '',
            'mountpoint': mp,
            'mounted': bool(mp),
            'model': model,
            'vendor': vendor,
            'serial': (r.get('serial') or '').strip(),
            'uuid': r.get('uuid') or '',
            'label': r.get('label') or '',
            'bus': bus,
            'bus_cn': bus_cn,
            'external': external,
            'hotplug': hotplug,
            'removable': removable,
            'ro': str(r.get('ro') or '0') == '1',
            'parent': r.get('_parent') or '',
            'partitions': [k.get('kname') or k.get('name') for k in kids],
            'has_children': bool(kids),
            'sysfs': sysfs,
        })
    return rows


def _dev_is_system(name, rows):
    """该设备（含其整盘/全部分区）是否承载系统目录 —— 是则禁止格式化。"""
    names = {name}
    for r in rows:
        if r['name'] == name:
            names.add(r['parent'])
            names.update(r['partitions'])
            if r['parent']:
                names.add(r['parent'])
    for r in rows:
        if r['name'] in names and r['mountpoint'] in _CRITICAL_MOUNTS:
            return True, r['mountpoint']
    return False, ''


def _fs_meta(v):
    for f in FS_TYPES:
        if f['v'] == v:
            return f
    return None


def _fs_tool_ready(v):
    """该文件系统能否真的格式化 / 挂载 —— 依赖的 mkfs 工具在不在。"""
    f = _fs_meta(v)
    if not f:
        return False, '不支持的文件系统：%s' % v
    # 不能用 sh(['command','-v',...])：command 是 shell 内建，subprocess 不带
    # shell 时直接报「命令不存在」。改成在 PATH 里逐个目录找可执行文件。
    found = False
    for d in (os.environ.get('PATH') or '').split(os.pathsep):
        if d and os.path.isfile(os.path.join(d, f['mkfs'])):
            found = True
            break
    return found, ('' if found else
                   '缺少格式化工具 %s，请先安装软件包 %s' % (f['mkfs'], f['pkg']))


def _read_fstab():
    try:
        with open('/etc/fstab', encoding='utf-8') as f:
            return f.read()
    except OSError:
        return ''


def _backup_fstab():
    """第一次改 fstab 前先备份。改坏了开不了机，留一份原稿能救回来。"""
    if os.path.isfile(_FSTAB_BACKUP):
        return
    try:
        os.makedirs(GEN, exist_ok=True)
        shutil.copy2('/etc/fstab', _FSTAB_BACKUP)
    except Exception:
        pass


def _safe_mnt_seg(s):
    """把磁盘卷标压成安全目录名。

    卷标是在**别的机器**上写的，可以塞空格、`..`、换行这类东西，
    它会变成默认挂载点的一部分，也会原样写进 /etc/fstab。
    """
    return re.sub(r'[^A-Za-z0-9._-]', '_', str(s or '')).strip('._-')[:32] or 'disk'


def _mnt_target_error(t):
    """挂载点合法性：限定在 /mnt、/media、/srv 之下且不含空格与特殊字符。

    早先 target 完全不校验 —— `target=/etc` 会把 U 盘盖到 /etc 上，系统立刻不可用；
    写进 fstab 时还能用换行注入一整行额外的挂载项。
    """
    if not t.startswith('/'):
        return '挂载点必须是绝对路径'
    if '..' in t.split('/'):
        return '挂载点不能包含 ..'
    bad = [ch for ch in t if ch.isspace() or ch in '\'"\\`$|;&<>()*?!#{}[]']
    if bad:
        return ('挂载点不能包含空格或特殊字符（收到 %s）：这些字符会被写进 '
                '/etc/fstab，可造成额外的挂载项注入' % t)
    if not (t.startswith('/mnt/') or t.startswith('/media/') or
            t.startswith('/srv/')):
        return '为安全起见，挂载点只能放在 /mnt、/media、/srv 之下（收到：%s）' % t
    return ''


def act_storage(p):
    """外置存储设备：get / mount / umount / format / fstab_add / fstab_del。"""
    p = p or {}
    op = (p.get('op') or 'get').strip()

    if op == 'get':
        rows = _storage_device_rows()
        if rows is None:
            return fail('读取块设备失败：lsblk 不可用（请安装 util-linux）',
                        'NO_LSBLK')
        # 标注每个文件系统当前是否可格式化（缺 mkfs 工具的要提示安装）
        fs_state = {}
        for f in FS_TYPES:
            ready, why = _fs_tool_ready(f['v'])
            fs_state[f['v']] = {'ready': ready, 'why': why}
        return ok({
            'devices': rows,
            'fs_types': FS_TYPES,
            'fs_state': fs_state,
            'fstab': _read_fstab(),
            'mounts': _mounted_mounts(),
            'note': ('外接 U 盘 / 移动硬盘 / Type-C 与雷电硬盘盒会显示在下方。'
                     '「连接方式」显示的是总线类型（USB / 雷电 / NVMe / SATA）；'
                     'Type-C 是插头形态，电气上仍归 USB 或雷电，因此按总线展示更准确。'),
        })

    def find(name):
        for r in _storage_device_rows() or []:
            if r['name'] == name:
                return r
        return None

    if op == 'mount':
        name = str(p.get('name') or '').strip()
        dev = find(name)
        if not dev:
            return fail('找不到设备：%s' % name, 'NODEV')
        if dev.get('mounted'):
            return fail('该设备已经挂载在 %s' % (dev.get('mountpoint') or '?'), 'ALREADY')
        if not (dev.get('fstype') or ''):
            return fail('该设备上没有可识别的文件系统，请先格式化。', 'NOFS')
        # 一律用 .get 取值：不同版本 lsblk 的字段可能缺失，缺字段不该让整个
        # 接口抛 KeyError（那样前端只会看到一句「读取失败」，看不出原因）。
        base = '/mnt/drouter-' + _safe_mnt_seg(dev.get('label') or dev.get('name') or name)
        target = str(p.get('target') or '').strip() or base
        terr = _mnt_target_error(target)
        if terr:
            return fail('挂载点不合法：%s' % terr, 'BAD_TARGET')
        try:
            os.makedirs(target, exist_ok=True)
        except Exception as e:
            return fail('创建挂载点失败：%s' % e, 'MKDIR')
        rc, _o, e = sh(['mount', '/dev/' + name, target], timeout=40)
        if rc != 0:
            return fail('挂载失败：%s' % (e or '未知错误'), 'MOUNT')
        log('info', 'storage', 'MOUNT_OK', '已挂载 /dev/%s 到 %s' % (name, target),
            {'dev': name, 'target': target, 'fstype': dev['fstype']})
        return ok({'name': name, 'target': target},
                  '已把 /dev/%s 挂载到 %s' % (name, target))

    if op == 'umount':
        name = str(p.get('name') or '').strip()
        dev = find(name)
        if not dev:
            return fail('找不到设备：%s' % name, 'NODEV')
        if not dev.get('mounted'):
            return fail('该设备当前没有挂载', 'NOTMOUNTED')
        rc, _o, e = sh(['umount', dev.get('mountpoint') or ('/dev/' + name)], timeout=40)
        if rc != 0:
            # 设备忙（有进程占用）时给出可执行的排查命令，而不是干巴巴一句失败
            return fail('卸载失败：%s。可能仍有进程在访问该目录，'
                        '可用 <span class="mono">lsof +D 挂载点</span> 或 '
                        '<span class="mono">fuser -m 挂载点</span> 查占用。'
                        % (e or '未知错误'), 'UMOUNT')
        log('info', 'storage', 'UMOUNT_OK', '已卸载 /dev/%s' % name, {'dev': name})
        return ok({'name': name}, '已卸载 /dev/%s' % name)

    if op == 'format':
        name = str(p.get('name') or '').strip()
        fs = str(p.get('fs') or '').strip()
        label = str(p.get('label') or '').strip()
        confirm = str(p.get('confirm') or '').strip()
        rows = _storage_device_rows() or []
        dev = None
        for r in rows:
            if r['name'] == name:
                dev = r
        if not dev:
            return fail('找不到设备：%s' % name, 'NODEV')

        # 保护 1：系统盘
        is_sys, where = _dev_is_system(name, rows)
        if is_sys:
            return fail('拒绝格式化：/dev/%s 属于系统盘（承载 %s），格式化会导致系统无法启动。'
                        % (name, where), 'SYSTEM_DISK')
        # 保护 2：已挂载
        if dev.get('mounted'):
            return fail('拒绝格式化：/dev/%s 当前挂载在 %s，请先卸载。'
                        % (name, dev.get('mountpoint') or '?'), 'MOUNTED')
        # 保护 3：整盘且已有分区 —— 大概率是磁盘而不是空盘
        if dev.get('type') == 'disk' and dev.get('has_children'):
            return fail('拒绝格式化：/dev/%s 是整块盘且已含 %d 个分区（%s）。'
                        '请改为格式化其中的具体分区，以免误删整盘数据。'
                        % (name, len(dev['partitions']), '、'.join(dev['partitions'])),
                        'DISK_HAS_PARTS')
        # 保护 4：二次确认必须手输设备名
        if confirm != name:
            return fail('安全确认未通过：请手动输入设备名 %s 以确认格式化。' % name,
                        'NEED_CONFIRM')
        meta = _fs_meta(fs)
        if not meta:
            return fail('不支持的文件系统：%s' % fs, 'BAD_FS')
        ready, why = _fs_tool_ready(fs)
        if not ready:
            return fail(why, 'NO_TOOL')

        dev_path = '/dev/' + name
        cmd = [meta['mkfs'], '-F']
        if label:
            cmd += [meta['label_opt'], label[:16 if fs != 'vfat' else 11]]
        cmd.append(dev_path)
        # 格式化可能很慢（大容量硬盘几分钟），给足超时
        rc, out, e = sh(cmd, timeout=900)
        if rc != 0:
            log('error', 'storage', 'FORMAT_FAIL',
                '格式化 /dev/%s 为 %s 失败：%s' % (name, fs, e or ''), {'dev': name})
            return fail('格式化失败：%s' % (e or out or '未知错误'), 'FORMAT')
        log('warn', 'storage', 'FORMAT_OK',
            '已把 /dev/%s 格式化为 %s（卷标 %s）' % (name, fs, label or '无'),
            {'dev': name, 'fs': fs, 'label': label})
        return ok({'name': name, 'fs': fs, 'label': label},
                  '已把 /dev/%s 格式化为 %s。注意：原数据已全部清除且不可恢复。' % (name, fs))

    if op == 'fstab_add':
        name = str(p.get('name') or '').strip()
        target = str(p.get('target') or '').strip()
        rows = _storage_device_rows() or []
        dev = None
        for r in rows:
            if r['name'] == name:
                dev = r
        if not dev:
            return fail('找不到设备：%s' % name, 'NODEV')
        if not (dev.get('uuid') or ''):
            return fail('该设备没有 UUID（可能尚未格式化），无法写入 /etc/fstab。', 'NOUUID')
        if not target:
            target = (dev.get('mountpoint') or
                      ('/mnt/drouter-' + _safe_mnt_seg(dev.get('label') or name)))
        terr = _mnt_target_error(target)
        if terr:
            return fail('挂载点不合法：%s' % terr, 'BAD_TARGET')
        fstype = dev.get('fstype') or 'auto'
        # fstab 每个字段都不能含空格/换行，否则等于往 /etc/fstab 里塞新挂载项
        if any(ch.isspace() or ch in '\\"\'`$' for ch in (fstype or '')):
            return fail('文件系统类型不合法：%s' % fstype, 'BAD_FS')
        txt = _read_fstab()
        if ('UUID=%s' % dev['uuid']) in txt:
            return fail('/etc/fstab 里已存在该设备的条目', 'DUP')
        # nofail：外置盘拔掉时不会卡住开机；nofail,x-systemd.device-timeout 更稳
        line = ('UUID=%s %s %s defaults,nofail,x-systemd.device-timeout=10s 0 2'
                % (dev['uuid'], target, fstype))
        _backup_fstab()
        try:
            with open('/etc/fstab', 'a', encoding='utf-8') as f:
                if txt and not txt.endswith('\n'):
                    f.write('\n')
                f.write('# drouter 外置存储（拔掉也不会卡开机）\n')
                f.write(line + '\n')
        except Exception as e:
            return fail('写入 /etc/fstab 失败：%s' % e, 'WRITE')
        rc, _o, _e = sh(['systemctl', 'daemon-reload'], timeout=25)
        log('info', 'storage', 'FSTAB_ADD', '已把 /dev/%s 写入开机自动挂载' % name,
            {'dev': name, 'target': target, 'line': line})
        return ok({'name': name, 'target': target, 'line': line},
                  '已加入开机自动挂载：%s → %s（已加 nofail，拔盘不会卡开机）'
                  % (name, target))

    if op == 'fstab_del':
        uuid = str(p.get('uuid') or '').strip()
        if not uuid:
            return fail('缺少 UUID', 'NOUUID')
        lines = _read_fstab().splitlines()
        keep = [l for l in lines if ('UUID=%s' % uuid) not in l]
        if len(keep) == len(lines):
            return fail('/etc/fstab 里没有该设备的条目', 'NOTFOUND')
        _backup_fstab()
        try:
            with open('/etc/fstab', 'w', encoding='utf-8') as f:
                f.write('\n'.join(keep) + '\n')
        except Exception as e:
            return fail('写入 /etc/fstab 失败：%s' % e, 'WRITE')
        sh(['systemctl', 'daemon-reload'], timeout=25)
        log('info', 'storage', 'FSTAB_DEL', '已移除开机自动挂载 UUID=%s' % uuid,
            {'uuid': uuid})
        return ok({'uuid': uuid}, '已移除该设备的开机自动挂载')

    return fail('不支持的操作：%s' % op, 'BAD_OP')


def read_acl(p):
    """读取访问控制配置 + 运行状态（#8）。"""
    d = _acl_load()
    # 联动提示：DPI 是否已安装（应用识别依赖它）
    dpi_ok = False
    try:
        st = _dpi_load_state()
        dpi_ok = bool(st.get('last_success') or st.get('installed'))
    except Exception:
        dpi_ok = False
    # 联动提示：QoS 是否开启（限速动作依赖它）
    qos_on = False
    try:
        q = _qos_load()
        qos_on = bool(q.get('enable'))
    except Exception:
        qos_on = False
    return ok({
        'enable': bool(d.get('enable')),
        'time_groups': d.get('time_groups') or [],
        'groups': d.get('groups') or [],
        'rules': d.get('rules') or [],
        'time_presets': ACL_TIME_PRESETS,
        'app_groups': ACL_APP_GROUPS,
        'group_templates': ACL_GROUP_TEMPLATES,
        'actions': ACL_ACTIONS,
        'dpi_ready': dpi_ok,
        'qos_enabled': qos_on,
        'tz_offset': TZ_OFFSET_H,
        'note': ('时间规则按北京时间（UTC+8）换算后写入内核；跨零点的时间段会自动拆成两段。'
                 '应用识别依赖 DPI 库，限速动作依赖 QoS 模块。'),
    })


def act_acl(p):
    """访问控制操作：get / save / on / off / plan（渲染预览）。"""
    p = p or {}
    op = (p.get('op') or 'get').strip()
    d = _acl_load()

    if op == 'get':
        return read_acl(p)

    if op == 'save':
        for k in ('enable', 'time_groups', 'groups', 'rules'):
            if k in p:
                d[k] = p[k]
        # 校验
        for i, tg in enumerate(d.get('time_groups') or []):
            if not tg.get('name'):
                return fail('第 %d 个时间组缺少名称' % (i + 1), 'BAD_TIME_GROUP')
            try:
                _acl_localize_hhmm(tg.get('start') or '00:00')
                _acl_localize_hhmm(tg.get('end') or '23:59')
            except ValidateError as e:
                return fail('时间组「%s」%s' % (tg.get('name'), e.msg_cn), 'BAD_TIME')
        for i, g in enumerate(d.get('groups') or []):
            for host in (g.get('hosts') or []):
                h = str(host).strip()
                if not h:
                    continue
                # 旧写法：含 ':' 就算 IPv6、含一个 '/' 就算 CIDR —— 换行、'}'、'#'
                # 全都放行了。而这些值会被拼进 nft 规则，`10.0.0.0/24 } counter accept #`
                # 就能把后面的 drop 注释掉（家长控制被绕过），带换行还能注入整条语句。
                # 现在严格交给 ipaddress 解析（单个 IP 也合法，等价于 /32）。
                try:
                    ipaddress.ip_network(h, strict=False)
                except Exception:
                    return fail('设备组「%s」包含非法地址：%s（请填 IP 或 CIDR，'
                                '例如 10.0.0.5 或 10.0.0.0/24）'
                                % (g.get('name'), h), 'BAD_HOST')
        for i, r in enumerate(d.get('rules') or []):
            if (r.get('action') or 'block') not in [a['v'] for a in ACL_ACTIONS]:
                return fail('第 %d 条规则的动作不合法' % (i + 1), 'BAD_ACTION')
        _acl_save(d)
        dep = _acl_deploy(d, live=bool(p.get('live', True)))
        msg = '访问控制配置已保存'
        if dep.get('applied'):
            msg += '，规则已生效'
        elif dep.get('msg_cn'):
            msg += '（%s）' % dep['msg_cn']
        return ok({'config': d, 'deploy': dep}, msg)

    if op in ('on', 'off'):
        d['enable'] = (op == 'on')
        _acl_save(d)
        dep = _acl_deploy(d, live=bool(p.get('live', True)))
        msg = '访问控制已%s' % ('启用' if d['enable'] else '停用')
        if not dep.get('applied') and dep.get('msg_cn'):
            msg += '（%s）' % dep['msg_cn']
        return ok({'enable': d['enable'], 'deploy': dep}, msg)

    if op == 'plan':
        try:
            text = _acl_render_nft(d)
        except ValidateError as e:
            return fail(e.msg_cn, 'VALIDATE_FAIL')
        # 计算规则数量统计
        n_rules = len([r for r in (d.get('rules') or []) if r.get('enable', True)])
        return ok({'text': text, 'rules': n_rules,
                   'time_groups': len(d.get('time_groups') or []),
                   'groups': len(d.get('groups') or [])})

    return fail('不支持的操作：%s' % op, 'BAD_OP')


def read_ipv6(_):
    rc, addrs, _e = sh(['ip', '-6', '-j', 'addr'])
    rc2, routes, _e = sh(['ip', '-6', '-j', 'route'])
    rc3, pd, _e = sh(['ip', '-6', 'route', 'show', 'default'])
    # ND / sysctl 参数
    sysctl = {}
    base = '/proc/sys/net/ipv6/conf'
    for iface in ['all', 'default']:
        for k in ['forwarding', 'accept_ra', 'mtu', 'dad_transmits', 'accept_ra_pinfo',
                  'router_solicitations', 'router_solicitation_interval', 'hop_limit']:
            p = os.path.join(base, iface, k)
            try:
                with open(p) as f:
                    sysctl['%s.%s' % (iface, k)] = f.read().strip()
            except Exception:
                pass
    rc4, neigh, _e = sh(['ip', '-6', 'neigh', 'show'])
    return ok({
        'addrs': json.loads(addrs) if rc == 0 else [],
        'routes': json.loads(routes) if rc2 == 0 else [],
        'default_route': pd,
        'sysctl': sysctl,
        'neigh': neigh.splitlines(),
    })


def read_ntp(_):
    rc, track, _e = sh(['chronyc', '-n', 'tracking'])
    rc2, src, _e = sh(['chronyc', '-n', 'sources'])
    rc3, date, _e = sh(['date', '+%Y-%m-%d %H:%M:%S %Z'])
    return ok({'tracking': track, 'sources': src, 'now': date})


def read_users(_):
    users = []
    with open('/etc/passwd', encoding='utf-8') as f:
        for line in f:
            p = line.rstrip('\n').split(':')
            if len(p) < 7:
                continue
            uid = int(p[2])
            if uid < 1000 and uid != 0:
                continue
            home = p[5]
            keys = []
            kf = os.path.join(home, '.ssh', 'authorized_keys')
            if os.path.isfile(kf):
                with open(kf, encoding='utf-8', errors='replace') as k:
                    for ln in k:
                        ln = ln.strip()
                        if ln and not ln.startswith('#'):
                            keys.append({'key': ln, 'comment': (ln.split()[-1] if len(ln.split()) > 2 else '')})
            users.append({'name': p[0], 'uid': uid, 'gid': int(p[3]),
                          'gecos': p[4], 'home': home, 'shell': p[6],
                          'locked': p[1].startswith(('!', '*')), 'keys': keys})
    return ok(users)


# ==================================================================== #11
# 连接跟踪与流量日志 —— 统一日志系统
#
# 设计目标（对齐 OPNsense + Zenarmor / ntopng 的能力，但保持轻量）：
#   1) 统一记录模型（ULog）：把来自不同源头的日志规范化成同一种结构，
#      前端只需一套渲染 + 一套过滤/检索逻辑。
#   2) 多源汇聚（source）：
#        fw        —— 防火墙拒绝/放行（journalctl DROUTER-FW，已有翻译器复用）
#        conntrack —— 连接跟踪事件（conntrack -E 或内核 nf_conntrack 日志）
#        flow      —— 连接快照表（conntrack -L，用于「当前连接」视图）
#        wan/ppp   —— 拨号与接入方式日志（复用 wan_log / ppp 翻译器）
#        ddns      —— 动态域名更新日志（复用 DDNS 状态）
#        app       —— drouter 自身结构化日志（/var/log/drouter/*.jsonl）
#        system    —— 系统日志（journalctl）
#   3) 统一检索：级别 + 源 + 关键字 + 协议 + IP + 端口 + 时间窗，
#      并提供聚合统计（按源/级别/动作/协议/主机计数）供前端画图。
#   4) 保留策略：滚动保留天数 + 最大条数，定时清理（由 drouter-logd 调用）。
#
# 统一记录字段（ULog record）：
#   {ts, src, level, action, proto, saddr, sport, daddr, dport,
#    iface, host, port, msg_cn, raw, extra}

ULOG_LEVELS = ['emerg', 'alert', 'crit', 'err', 'warn', 'notice', 'info', 'debug']
ULOG_LEVEL_CN = {
    'emerg': '紧急', 'alert': '告警', 'crit': '严重', 'err': '错误',
    'warn': '警告', 'notice': '注意', 'info': '信息', 'debug': '调试',
}
# 前端展示顺序与标签
ULOG_SOURCES = [
    {'v': 'fw', 'n': '防火墙', 'desc': '被拒绝 / 放行的数据包（nftables log 语句）'},
    {'v': 'conntrack', 'n': '连接跟踪', 'desc': 'NAT 连接的新建 / 销毁 / 更新事件'},
    {'v': 'flow', 'n': '当前连接', 'desc': '实时连接表快照（conntrack -L）'},
    {'v': 'wan', 'n': 'WAN 接入', 'desc': '拨号、DHCP、静态地址等接入日志'},
    {'v': 'ddns', 'n': '动态域名', 'desc': 'DDNS 更新结果与公网地址变化'},
    {'v': 'app', 'n': '应用日志', 'desc': 'drouter 自身操作与错误记录'},
    {'v': 'system', 'n': '系统日志', 'desc': '内核与系统服务的 journal 日志'},
]
ULOG_SRC_CN = {s['v']: s['n'] for s in ULOG_SOURCES}

ULOG_CONF = '/etc/drouter/generated/log.conf'   # 保留策略（界面写入）
ULOG_DEFAULT_CONF = {
    'enabled': True,
    'keep_days': 7,          # 保留天数
    'keep_rows': 200000,     # 归档最大条数（超出按时间淘汰最旧的）
    'archive': True,         # 是否把采集到的记录归档到 /var/log/drouter/ulog.jsonl
    'sources': {s['v']: True for s in ULOG_SOURCES},
    'min_level': 'debug',    # 归档时过滤的最低级别
}
ULOG_ARCHIVE = os.path.join(LOGDIR, 'ulog.jsonl')

# nf_conntrack 内核日志前缀（net.netfilter.nf_conntrack_log_invalid 等）
_CONNTRACK_KERNEL_RE = re.compile(
    r'\[NEW\]|\[UPDATE\]|\[DESTROY\]|nf_conntrack|conntrack')

# conntrack 状态 → 简体中文（TCP/UDP/ICMP 通用）
_CONNTRACK_STATE_CN = {
    'ESTABLISHED': '已建立', 'SYN_SENT': 'SYN 已发送', 'SYN_RECV': 'SYN 已接收',
    'SYN_SENT2': 'SYN 重传', 'FIN_WAIT': 'FIN 等待', 'TIME_WAIT': '等待回收',
    'CLOSE_WAIT': '半关闭', 'LAST_ACK': '最后确认', 'CLOSE': '已关闭',
    'CLOSED': '已关闭', 'LISTEN': '监听中', 'UNREPLIED': '单向（无回包）',
    'ASSURED': '已确认', 'NEW': '新建',
}


def _ulog_load():
    """读取统一日志配置（缺失时写入默认值）。"""
    d = dict(ULOG_DEFAULT_CONF)
    d['sources'] = dict(ULOG_DEFAULT_CONF['sources'])
    try:
        if os.path.isfile(ULOG_CONF):
            with open(ULOG_CONF, encoding='utf-8') as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                for k in ('enabled', 'keep_days', 'keep_rows', 'archive', 'min_level'):
                    if k in saved:
                        d[k] = saved[k]
                if isinstance(saved.get('sources'), dict):
                    for k, v in saved['sources'].items():
                        if k in d['sources']:
                            d['sources'][k] = bool(v)
    except Exception:
        pass
    return d


def _ulog_save(d):
    os.makedirs(os.path.dirname(ULOG_CONF), exist_ok=True)
    tmp = ULOG_CONF + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ULOG_CONF)
    try:
        os.chmod(ULOG_CONF, 0o600)
    except Exception:
        pass


def _ulog_level_rank(lv):
    try:
        return ULOG_LEVELS.index(lv)
    except ValueError:
        return len(ULOG_LEVELS)


def _ulog_rec(src, level='info', action='', proto='', saddr='', sport='',
              daddr='', dport='', iface='', host='', port='', msg_cn='',
              raw='', extra=None, ts=''):
    """构造一条统一日志记录（所有字段都有默认值，保证结构一致）。"""
    lv = level if level in ULOG_LEVELS else 'info'
    return {
        'ts': ts or datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        'src': src, 'level': lv, 'level_cn': ULOG_LEVEL_CN.get(lv, lv),
        'action': (action or '').upper(), 'proto': (proto or '').upper(),
        'saddr': saddr or '', 'sport': str(sport or ''),
        'daddr': daddr or '', 'dport': str(dport or ''),
        'iface': iface or '', 'host': host or '', 'port': str(port or ''),
        'msg_cn': msg_cn or '', 'raw': (raw or '')[:800],
        'extra': extra or {},
    }


# ---------------------------------------------------------------- 各源解析器

def _ulog_from_fw(since, limit):
    """防火墙日志 → ULog（复用已有 translate_fw_log）。"""
    out = []
    fetch = min(8000, max(limit * 6, 800))
    rc, txt, _e = sh(['journalctl', '-k', '-n', str(fetch), '--no-pager',
                      '-o', 'short-iso', '--since', since, '-g', 'DROUTER-FW'],
                     timeout=20)
    if rc != 0 or not txt:
        rc, txt, _e = sh(['journalctl', '-k', '-n', str(fetch), '--no-pager',
                          '-o', 'short-iso', '--since', since], timeout=20)
    for line in (txt or '').splitlines():
        ts, msg = _split_journal_line(line)
        if 'DROUTER-FW' not in msg:
            continue
        try:
            rec = translate_fw_log(msg)
        except Exception:
            rec = None
        if not rec:
            continue
        lv = {'REJECT': 'warn', 'DROP': 'warn', 'ACCEPT': 'info',
              'INVALID': 'notice'}.get(rec.get('action'), 'info')
        out.append(_ulog_rec(
            'fw', lv, action=rec.get('action', ''),
            proto=rec.get('proto', ''), saddr=rec.get('saddr', ''),
            sport=rec.get('sport', ''), daddr=rec.get('daddr', ''),
            dport=rec.get('dport', ''), iface=rec.get('iface', ''),
            msg_cn=rec.get('msg_cn', ''), raw=msg, ts=ts,
            extra={'family': rec.get('family', ''), 'rule': rec.get('rule', '')}))
    return out


_CONNTRACK_EV_RE = re.compile(r'\[(NEW|UPDATE|DESTROY|UNREPLIED)\]')
_CONNTRACK_FIELD_RE = re.compile(r'\b(src|dst|sport|dport)=([0-9a-fA-F:.]+)')
_CONNTRACK_PROTO_RE = re.compile(r'^\s*\[(?:NEW|UPDATE|DESTROY|UNREPLIED)\]\s+(\w+)')
_CONNTRACK_STATE_RE = re.compile(r'\[([A-Z][A-Z_]+)\]')


def _ulog_parse_conntrack_event(line):
    """解析 conntrack 事件行（`conntrack -E -o extended` 或内核日志）。

    真实输出形如：
      [NEW] tcp      6 120 SYN_SENT src=192.168.7.100 dst=1.1.1.1 sport=51234 dport=443 [UNREPLIED]
      [DESTROY] udp    17 src=10.0.0.5 dst=8.8.8.8 sport=1000 dport=53
    字段顺序与是否存在 TTL/状态词都可能变化，因此按「关键字取值」解析，
    而不是按固定位置匹配 —— 这样对 conntrack-tools 各版本都稳。
    """
    ln = (line or '').strip()
    if not ln:
        return None
    mev = _CONNTRACK_EV_RE.search(ln)
    if not mev:
        return None
    ev = mev.group(1)
    mproto = _CONNTRACK_PROTO_RE.match(ln)
    proto = (mproto.group(1) if mproto else '').upper()
    fields = {}
    for k, v in _CONNTRACK_FIELD_RE.findall(ln):
        fields.setdefault(k, v)
    saddr, daddr = fields.get('src', ''), fields.get('dst', '')
    if not saddr or not daddr:
        return None
    sport, dport = fields.get('sport', ''), fields.get('dport', '')
    # 状态：行里可能有多个 [XXX]，如 [ESTABLISHED] 与 [ASSURED] 同时出现。
    # 优先取「连接状态」而不是 [ASSURED]/[UNREPLIED] 这类标志位。
    states = [s for s in _CONNTRACK_STATE_RE.findall(ln) if s != ev]
    _FLAGS = ('ASSURED', 'UNREPLIED', 'SEEN_REPLY')
    state = ''
    for s in states:
        if s not in _FLAGS and s in _CONNTRACK_STATE_CN:
            state = s
            break
    if not state:
        for s in states:
            if s in _CONNTRACK_STATE_CN:
                state = s
                break
    st_cn = _CONNTRACK_STATE_CN.get(state, state)
    ev_cn = {'NEW': '新建连接', 'UPDATE': '连接更新',
             'DESTROY': '连接关闭', 'UNREPLIED': '单向无响应'}.get(ev, ev)
    lv = {'NEW': 'info', 'UPDATE': 'debug', 'DESTROY': 'info',
          'UNREPLIED': 'notice'}.get(ev, 'info')
    msg = '%s：%s %s:%s → %s:%s' % (ev_cn, proto or '协议', saddr, sport or '',
                                    daddr, dport or '')
    if state:
        msg += '（%s）' % (st_cn or state)
    if ev == 'UNREPLIED' and 'UNREPLIED' not in states:
        msg += '（无回包）'
    return _ulog_rec('conntrack', lv, action=ev, proto=proto,
                     saddr=saddr, sport=sport, daddr=daddr, dport=dport,
                     msg_cn=msg, raw=ln, extra={'state': state, 'state_cn': st_cn})


def _ulog_from_conntrack(since, limit, live=False, live_sec=2):
    """连接跟踪事件 → ULog。

    默认走内核日志（快）。只有 live=True 时才订阅 `conntrack -E` 事件流。

    为什么必须改成按需：`conntrack -E` 是阻塞的事件流，只能靠 timeout 掐断。
    以前这里固定 `timeout 2`，于是**每查一次统一日志都要白等 2 秒** ——
    而这台机器没开转发、连接跟踪表是空的，2 秒换来 0 条记录，整个
    /api/ulog 的耗时几乎全花在这上面（实测 2019ms）。
    实时事件订阅属于「用户主动要看」的能力，不该让每次查询都替它买单。
    """
    out = []
    if live:
        try:
            sec = max(1, min(int(live_sec), 10))
        except Exception:
            sec = 2
        rc, txt, _e = sh(['timeout', str(sec), 'conntrack', '-E', '-o', 'extended'],
                         timeout=sec + 6)
        if rc in (0, 124) and txt:
            for line in txt.splitlines():
                rec = _ulog_parse_conntrack_event(line)
                if rec:
                    out.append(rec)
    if not out:
        # 回退：从内核日志里找 nf_conntrack 事件
        rc2, txt2, _e2 = sh(['journalctl', '-k', '-n', '1500', '--no-pager',
                             '-o', 'short-iso', '--since', since],
                            timeout=20)
        for line in (txt2 or '').splitlines():
            ts, msg = _split_journal_line(line)
            if not _CONNTRACK_KERNEL_RE.search(msg):
                continue
            rec = _ulog_parse_conntrack_event(msg)
            if rec:
                rec['ts'] = ts or rec['ts']
                out.append(rec)
    return out[-limit:] if limit else out


def _ulog_parse_conntrack_table(line):
    """解析 `conntrack -L -o extended` 的一行 → 连接快照（flow）。"""
    ln = (line or '').strip()
    if not ln or ln.startswith('conntrack '):
        return None
    proto_m = re.match(r'^(\w+)\s+(\d+)\s+', ln)
    if not proto_m:
        return None
    proto = proto_m.group(1).upper()
    l4proto = proto_m.group(2)

    def grab(pat):
        m = re.search(pat, ln)
        return m.group(1) if m else ''

    saddr = grab(r'\bsrc=([0-9a-fA-F:.]+)')
    daddr = grab(r'\bdst=([0-9a-fA-F:.]+)')
    sport = grab(r'\bsport=(\d+)')
    dport = grab(r'\bdport=(\d+)')
    if not (saddr and daddr):
        return None
    # 状态：conntrack -L 里 TCP 状态是**裸词**（如 `tcp 6 431999 ESTABLISHED src=...`），
    # 而 [ASSURED] / [UNREPLIED] 是方括号标志位。先在 src= 之前找裸词状态，
    # 找不到再退回方括号里的标志位。
    st = ''
    _FLAGS = ('ASSURED', 'UNREPLIED', 'SEEN_REPLY')
    head = ln.split('src=', 1)[0]
    for w in re.findall(r'\b[A-Z][A-Z_]{2,}\b', head):
        if w in _CONNTRACK_STATE_CN:
            st = w
            break
    if not st:
        cands = _CONNTRACK_STATE_RE.findall(ln)
        for s in cands:
            if s not in _FLAGS and s in _CONNTRACK_STATE_CN:
                st = s
                break
        if not st:
            for s in cands:
                if s in _CONNTRACK_STATE_CN:
                    st = s
                    break
    # 流量方向：只对非回复方向计数，避免重复
    reply = 'reply' in ln.split('[')[0]
    bytes_out = grab(r'\bbytes=(\d+)')
    pkts_out = grab(r'\bpackets=(\d+)')
    # NAT 改写后的地址
    nat = ''
    mnat = re.search(r'(?:src|dst)=([0-9a-fA-F:.]+).*?\[(?:UNREPLIED|ASSURED)\]', ln)
    if mnat:
        nat = mnat.group(1)
    st_cn = _CONNTRACK_STATE_CN.get(st, st)
    msg = '%s %s:%s → %s:%s' % (proto, saddr, sport or '', daddr, dport or '')
    if st_cn:
        msg += '（%s）' % st_cn
    if bytes_out and not reply:
        msg += ' · 已传输 %s' % _human_bytes(int(bytes_out))
    return _ulog_rec('flow', 'info', action=('REPLY' if reply else 'ORIG'),
                     proto=proto, saddr=saddr, sport=sport,
                     daddr=daddr, dport=dport, msg_cn=msg, raw=ln,
                     extra={'state': st, 'state_cn': st_cn,
                            'bytes': int(bytes_out or 0),
                            'pkts': int(pkts_out or 0), 'reply': reply,
                            'l4proto_num': l4proto, 'nat': nat})


def _human_bytes(n):
    n = float(n or 0)
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return ('%d %s' % (n, u)) if u == 'B' else ('%.1f %s' % (n, u))
        n /= 1024
    return '%.1f PB' % n


def _ulog_read_flows(p):
    """当前连接表（conntrack -L）快照，支持过滤与排序。"""
    p = p or {}
    proto = (p.get('proto') or '').upper()
    q = (p.get('q') or '').strip().lower()
    limit = int(p.get('limit') or 300)
    limit = max(1, min(limit, 3000))
    rc, txt, err = sh(['conntrack', '-L', '-o', 'extended'], timeout=25)
    if rc != 0:
        # 退化：不带 extended 再试一次
        rc, txt, err = sh(['conntrack', '-L'], timeout=25)
    rows = []
    for line in (txt or '').splitlines():
        rec = _ulog_parse_conntrack_table(line)
        if not rec:
            continue
        if proto and rec['proto'] != proto:
            continue
        if q and q not in (rec['raw'] or '').lower():
            continue
        rows.append(rec)
    # 统计
    stat = {'total': len(rows), 'tcp': 0, 'udp': 0, 'icmp': 0, 'other': 0}
    for r in rows:
        k = r['proto'].lower()
        stat[k if k in ('tcp', 'udp', 'icmp') else 'other'] += 1
    # 内核连接表容量（了解是否接近上限）
    cap = {'count': 0, 'max': 0}
    try:
        with open('/proc/sys/net/netfilter/nf_conntrack_count') as f:
            cap['count'] = int(f.read().strip())
        with open('/proc/sys/net/netfilter/nf_conntrack_max') as f:
            cap['max'] = int(f.read().strip())
    except Exception:
        pass
    return ok({
        'rows': rows[:limit], 'stat': stat, 'cap': cap,
        'err': (err or '') if rc != 0 else '',
        'note': ('「当前连接」是 conntrack 表的实时快照，展示 NAT 映射后的地址、'
                 '连接状态与已传输字节数。开启软加速（flowtable）后，'
                 '已卸载流量的字节计数可能不再增长。'),
    })


def _ulog_from_wan(limit):
    """WAN 接入日志 → ULog（复用 read_wan_log 的翻译结果）。"""
    out = []
    try:
        res = read_wan_log({'limit': limit})
        items = ((res or {}).get('data') or {}).get('items') or []
    except Exception:
        items = []
    for it in items:
        lv = it.get('level') or 'info'
        if lv not in ULOG_LEVELS:
            lv = {'ok': 'info', 'warn': 'warn', 'err': 'err'}.get(lv, 'info')
        out.append(_ulog_rec(
            'wan', lv, action=it.get('code', ''), iface=it.get('iface', ''),
            msg_cn=it.get('msg_cn', ''), raw=it.get('raw', ''),
            ts=it.get('ts', ''),
            extra={'access': it.get('access', ''), 'access_cn': it.get('access_cn', '')}))
    return out


def _ulog_from_ddns(limit):
    """DDNS 状态与日志 → ULog。"""
    out = []
    try:
        d = _load_setting('ddns', {}) or {}
    except Exception:
        d = {}
    res = (d.get('last_result') or '').strip()
    if res or d.get('last_msg_cn'):
        lv = {'ok': 'info', 'success': 'info', 'err': 'err',
              'error': 'err', 'fail': 'err'}.get(res.lower(), 'info')
        out.append(_ulog_rec(
            'ddns', lv, action=(res.upper() if res else 'STATUS'),
            host=d.get('domain', '') or '',
            msg_cn=d.get('last_msg_cn') or ('DDNS 状态：%s' % (res or '未知')),
            ts=d.get('last_update') or '',
            extra={'ip4': d.get('last_ip4', ''), 'ip6': d.get('last_ip6', ''),
                   'provider': d.get('provider', ''),
                   'domain': d.get('domain', '')}))
    return out


def _ulog_from_app(since, limit):
    """drouter 结构化日志（all.jsonl）→ ULog。"""
    out = []
    path = os.path.join(LOGDIR, 'all.jsonl')
    if not os.path.isfile(path):
        return out
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()[-limit:]
    except Exception:
        return out
    for ln in lines:
        try:
            r = json.loads(ln)
        except Exception:
            continue
        lv = r.get('level') or 'info'
        lv = {'ok': 'info', 'success': 'info'}.get(lv, lv)
        if lv not in ULOG_LEVELS:
            lv = 'info'
        out.append(_ulog_rec(
            'app', lv, action=r.get('code', ''), msg_cn=r.get('msg_cn', ''),
            ts=r.get('ts', ''), raw=ln,
            extra={'module': r.get('module', ''), 'detail': r.get('detail', '')}))
    return out


def _ulog_from_system(since, limit):
    """系统日志 → ULog（只保留警告及以上，避免噪声淹没）。"""
    out = []
    fetch = min(3000, max(limit * 3, 400))
    rc, txt, _e = sh(['journalctl', '-p', 'warning', '-n', str(fetch),
                      '--no-pager', '-o', 'short-iso', '--since', since],
                     timeout=20)
    if rc != 0:
        return out
    for line in (txt or '').splitlines():
        ts, msg = _split_journal_line(line)
        if not msg:
            continue
        lv = _sys_level(msg)
        out.append(_ulog_rec('system', lv, msg_cn=msg, raw=line, ts=ts))
    return out


def _sys_level(msg):
    m = msg.lower()
    if re.search(r'\bemerg|panic', m):
        return 'emerg'
    if re.search(r'\balert\b', m):
        return 'alert'
    if re.search(r'\bcrit', m):
        return 'crit'
    if re.search(r'\berror|\berr\b|failed|failure', m):
        return 'err'
    if re.search(r'\bwarn', m):
        return 'warn'
    return 'notice'


# ---------------------------------------------------------------- 统一入口

def _ulog_collect(p):
    """按请求参数汇聚各源日志，返回 (records, meta)。"""
    p = p or {}
    conf = _ulog_load()
    since = (p.get('since') or '30 min ago').strip()
    if not re.match(r'^[0-9a-zA-Z :._-]{1,32}$', since):
        since = '30 min ago'
    limit = int(p.get('limit') or 500)
    limit = max(20, min(limit, 3000))
    # 需要哪些源
    want = p.get('sources')
    if isinstance(want, str):
        want = [x.strip() for x in want.split(',') if x.strip()]
    if not want:
        want = [s['v'] for s in ULOG_SOURCES if conf['sources'].get(s['v'])]
    want = [w for w in want if conf['sources'].get(w, True)]

    # live：是否订阅 conntrack 实时事件流。默认不开 —— 它会固定阻塞若干秒。
    live = str(p.get('live') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    try:
        live_sec = max(1, min(int(p.get('live_sec') or 2), 10))
    except Exception:
        live_sec = 2

    recs = []
    meta = {'sources_used': [], 'errors': {}, 'live': live}
    for s in want:
        try:
            if s == 'fw':
                r = _ulog_from_fw(since, limit)
            elif s == 'conntrack':
                r = _ulog_from_conntrack(since, limit, live=live, live_sec=live_sec)
            elif s == 'wan':
                r = _ulog_from_wan(limit)
            elif s == 'ddns':
                r = _ulog_from_ddns(limit)
            elif s == 'app':
                r = _ulog_from_app(since, limit)
            elif s == 'system':
                r = _ulog_from_system(since, limit)
            else:
                continue
            recs.extend(r)
            meta['sources_used'].append(s)
        except Exception as e:
            meta['errors'][s] = str(e)[:200]

    # 排序（新→旧）
    recs.sort(key=lambda x: x.get('ts') or '', reverse=True)
    return recs, meta


def _ulog_filter(recs, p):
    """统一过滤：级别 / 源 / 动作 / 协议 / 地址 / 端口 / 关键字。"""
    p = p or {}
    lv = (p.get('level') or '').strip()
    if lv:
        want_rank = _ulog_level_rank(lv)
        recs = [r for r in recs if _ulog_level_rank(r.get('level')) <= want_rank]
    src = (p.get('src') or '').strip()
    if src:
        recs = [r for r in recs if r.get('src') == src]
    action = (p.get('action') or '').strip().upper()
    if action:
        recs = [r for r in recs if r.get('action') == action]
    proto = (p.get('proto') or '').strip().upper()
    if proto:
        recs = [r for r in recs if r.get('proto') == proto]
    addr = (p.get('addr') or '').strip().lower()
    if addr:
        recs = [r for r in recs if addr in (r.get('saddr', '') + r.get('daddr', '')).lower()]
    port = str(p.get('port') or '').strip()
    if port:
        recs = [r for r in recs if port in (r.get('sport', ''), r.get('dport', ''))]
    q = (p.get('q') or '').strip().lower()
    if q:
        recs = [r for r in recs
                if q in (r.get('msg_cn', '') + ' ' + r.get('raw', '')).lower()]
    return recs


def _ulog_stats(recs):
    """聚合统计，供前端画分布图。"""
    st = {
        'total': len(recs),
        'by_src': {}, 'by_level': {}, 'by_action': {}, 'by_proto': {},
        'top_src_ip': [], 'top_dst_port': [],
    }
    sip, dport = {}, {}
    for r in recs:
        st['by_src'][r['src']] = st['by_src'].get(r['src'], 0) + 1
        st['by_level'][r['level']] = st['by_level'].get(r['level'], 0) + 1
        if r['action']:
            st['by_action'][r['action']] = st['by_action'].get(r['action'], 0) + 1
        if r['proto']:
            st['by_proto'][r['proto']] = st['by_proto'].get(r['proto'], 0) + 1
        if r['saddr']:
            sip[r['saddr']] = sip.get(r['saddr'], 0) + 1
        if r['dport']:
            dport[r['dport']] = dport.get(r['dport'], 0) + 1
    st['top_src_ip'] = [{'v': k, 'n': v} for k, v in
                        sorted(sip.items(), key=lambda x: -x[1])[:8]]
    st['top_dst_port'] = [{'v': k, 'n': v} for k, v in
                          sorted(dport.items(), key=lambda x: -x[1])[:8]]
    return st


def read_ulog(p=None):
    """统一日志查询（#11）。op=query 时返回过滤后的日志 + 统计。"""
    p = p or {}
    conf = _ulog_load()
    recs, meta = _ulog_collect(p)
    total_before = len(recs)
    recs = _ulog_filter(recs, p)
    limit = int(p.get('limit') or 500)
    limit = max(20, min(limit, 3000))
    shown = recs[:limit]
    return ok({
        'items': shown,
        'stat': _ulog_stats(recs),
        'meta': dict(meta, collected=total_before, matched=len(recs),
                     shown=len(shown)),
        'conf': conf,
        'sources': ULOG_SOURCES,
        'levels': [{'v': l, 'n': ULOG_LEVEL_CN[l]} for l in ULOG_LEVELS],
        'archive': {'path': ULOG_ARCHIVE, 'size': _file_size(ULOG_ARCHIVE),
                    'rows': _ulog_count_rows(ULOG_ARCHIVE)},
        'note': ('统一日志把防火墙、连接跟踪、WAN 接入、DDNS、应用与系统日志'
                 '规范化成同一种记录结构，可用同一套条件检索。'),
    })


def _file_size(path):
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def _ulog_count_rows(path):
    try:
        n = 0
        with open(path, encoding='utf-8', errors='replace') as f:
            for _ in f:
                n += 1
        return n
    except Exception:
        return 0


def read_ulog_flow(p=None):
    """当前连接表（flow）查询。"""
    return _ulog_read_flows(p)


def read_ulog_conf(p=None):
    """读统一日志配置 + 采集能力自检。"""
    conf = _ulog_load()
    caps = {}
    # 性能：原来每个工具 fork 一次 `which`；shutil.which 是同一套 PATH 查找语义的
    # 纯 Python 实现，零 fork（解析 PATH 再逐个 os.access(X_OK)）。这里是好写法。
    for tool in ('conntrack', 'journalctl'):
        caps[tool] = bool(shutil.which(tool))
    # 内核连接跟踪表
    ct = {'enabled': os.path.isfile('/proc/sys/net/netfilter/nf_conntrack_count')}
    try:
        with open('/proc/sys/net/netfilter/nf_conntrack_count') as f:
            ct['count'] = int(f.read().strip())
        with open('/proc/sys/net/netfilter/nf_conntrack_max') as f:
            ct['max'] = int(f.read().strip())
    except Exception:
        pass
    # 防火墙日志开关
    try:
        fw_on = _fw_log_enabled()
    except Exception:
        fw_on = True
    return ok({
        'conf': conf, 'caps': caps, 'conntrack': ct, 'fw_log': fw_on,
        'sources': ULOG_SOURCES,
        'levels': [{'v': l, 'n': ULOG_LEVEL_CN[l]} for l in ULOG_LEVELS],
        'archive': {'path': ULOG_ARCHIVE, 'size': _file_size(ULOG_ARCHIVE),
                    'rows': _ulog_count_rows(ULOG_ARCHIVE)},
        'warn': ('' if fw_on else
                 '防火墙日志开关为关闭：规则里没有 log 语句，因此「防火墙」源不会有新记录。'),
    })


def act_ulog(p):
    """统一日志操作：save_conf / prune / export / clear_archive。"""
    p = p or {}
    op = (p.get('op') or 'query').strip()

    if op in ('query', 'get'):
        return read_ulog(p)

    if op == 'flow':
        return read_ulog_flow(p)

    if op == 'conf':
        return read_ulog_conf(p)

    if op == 'save_conf':
        conf = _ulog_load()
        for k in ('enabled', 'archive', 'min_level'):
            if k in p:
                conf[k] = bool(p[k]) if k != 'min_level' else str(p[k])
        for k in ('keep_days', 'keep_rows'):
            if k in p:
                lbl = '保留天数' if k == 'keep_days' else '最大条数'
                raw = p[k]
                if isinstance(raw, bool):
                    return fail('「%s」必须是正整数' % lbl, 'BAD_NUM')
                try:
                    n = int(str(raw).strip())
                except Exception:
                    return fail('「%s」必须是正整数' % lbl, 'BAD_NUM')
                if n < 1:
                    return fail('「%s」必须大于 0' % lbl, 'BAD_NUM')
                conf[k] = n
        if isinstance(p.get('sources'), dict):
            for k, v in p['sources'].items():
                if k in conf['sources']:
                    conf['sources'][k] = bool(v)
        if conf['min_level'] not in ULOG_LEVELS:
            conf['min_level'] = 'debug'
        _ulog_save(conf)
        pr = _ulog_prune(conf)
        msg = '日志设置已保存'
        if pr.get('removed'):
            msg += '，已按保留策略清理 %d 条历史记录' % pr['removed']
        return ok({'conf': conf, 'prune': pr}, msg)

    if op == 'prune':
        conf = _ulog_load()
        pr = _ulog_prune(conf)
        return ok(pr, '已清理 %d 条超期记录（保留 %d 条）'
                  % (pr.get('removed', 0), pr.get('kept', 0)))

    if op == 'export':
        # 导出为 JSON 或 CSV 文本，前端直接下载
        recs, _meta = _ulog_collect(p)
        recs = _ulog_filter(recs, p)
        recs = list(reversed(recs))       # 导出按时间正序，便于阅读
        fmt = (p.get('format') or 'json').lower()
        if fmt == 'csv':
            cols = ['ts', 'src', 'level', 'action', 'proto', 'saddr', 'sport',
                    'daddr', 'dport', 'iface', 'msg_cn']
            lines = [','.join(cols)]
            for r in recs:
                lines.append(','.join(
                    '"%s"' % str(r.get(c, '')).replace('"', '""') for c in cols))
            text = '\n'.join(lines)
            return ok({'text': text, 'format': 'csv', 'rows': len(recs)},
                      '已导出 %d 条记录（CSV）' % len(recs))
        text = '\n'.join(json.dumps(r, ensure_ascii=False) for r in recs)
        return ok({'text': text, 'format': 'json', 'rows': len(recs)},
                  '已导出 %d 条记录（JSONL）' % len(recs))

    if op == 'clear_archive':
        try:
            if os.path.isfile(ULOG_ARCHIVE):
                os.remove(ULOG_ARCHIVE)
            return ok({}, '日志归档已清空')
        except Exception as e:
            return fail('清空失败：%s' % e, 'RM_FAIL')

    if op == 'archive':
        # 供 drouter-logd 定时调用：采集一次并归档，然后按策略清理
        conf = _ulog_load()
        if not conf.get('enabled'):
            return ok({'archived': 0, 'prune': {}}, '统一日志已关闭，跳过归档')
        recs, meta = _ulog_collect({'since': p.get('since') or '5 min ago',
                                    'limit': int(p.get('limit') or 800)})
        # 归档时按源去重，避免同一时间窗重复写入
        n = _ulog_archive_write(recs, conf)
        pr = _ulog_prune(conf)
        return ok({'archived': n, 'collected': len(recs), 'prune': pr,
                   'sources': meta.get('sources_used', []),
                   'errors': meta.get('errors', {})},
                  '已归档 %d 条日志' % n)

    return fail('不支持的日志操作：%s' % op, 'BAD_OP')


def _ulog_prune(conf):
    """按保留策略清理归档文件（保留天数 + 最大条数）。

    必须流式。原实现 `f.read().splitlines()` 一次性把整个归档读进列表，
    再建第二个 kept 列表 —— keep_rows 默认 20 万行、每行约 400 字节，
    峰值内存 160MB+；而这个函数是 drouter-logd 定时器**每 60 秒**调一次。
    4GB 的小机器上这是 OOM 杀手。同项目的 drouter-logd.py 早就因为
    同样的问题改成了流式（见那里的 dedupe_archive 注释），helper 这边漏了。

    改成：先只数行（不驻留内容），不够上限就一个字节都不用改；
    超了上限才逐行流式写临时文件，最后 os.replace 原子替换。
    """
    conf = conf or ULOG_DEFAULT_CONF
    path = ULOG_ARCHIVE
    if not os.path.isfile(path):
        return {'removed': 0, 'kept': 0}
    keep_days = max(1, int(conf.get('keep_days') or 7))
    keep_rows = max(1, int(conf.get('keep_rows') or 200000))
    cutoff = datetime.now().timestamp() - keep_days * 86400

    def _too_old(ln):
        try:
            r = json.loads(ln)
            dt = datetime.strptime((r.get('ts') or '')[:19], '%Y-%m-%dT%H:%M:%S')
            return dt.timestamp() < cutoff
        except Exception:
            return False    # 无法解析的行保留，避免误删

    # 第一遍：只统计，不留内容
    total = 0
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for ln in f:
                if ln.strip():
                    total += 1
    except Exception:
        return {'removed': 0, 'kept': 0}
    if total <= keep_rows:
        # 条数没超：只有「过期」这一种清理，逐行流式重写即可，
        # 但要先确认确实有要删的行，否则连临时文件都不用建。
        has_old = False
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                for ln in f:
                    if ln.strip() and _too_old(ln):
                        has_old = True
                        break
        except Exception:
            return {'removed': 0, 'kept': total}
        if not has_old:
            return {'removed': 0, 'kept': total}
        removed = 0
        kept_n = 0
        tmp = path + '.tmp'
        try:
            with open(path, encoding='utf-8', errors='replace') as fin, \
                    open(tmp, 'w', encoding='utf-8') as fout:
                for ln in fin:
                    if not ln.strip():
                        continue
                    if _too_old(ln):
                        removed += 1
                        continue
                    fout.write(ln if ln.endswith('\n') else ln + '\n')
                    kept_n += 1
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                pass
            return {'removed': 0, 'kept': total}
        return {'removed': removed, 'kept': kept_n}

    # 第二遍：条数超上限。保留最后 keep_rows 行（最新的那些），
    # 用滑动窗口 —— 只驻留 keep_rows 行，且它本身就是我们要写出的内容。
    removed = 0
    window = []
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for ln in f:
                if not ln.strip():
                    continue
                if _too_old(ln):
                    removed += 1
                    continue
                window.append(ln if ln.endswith('\n') else ln + '\n')
                if len(window) > keep_rows:
                    window.pop(0)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fout:
            fout.writelines(window)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(path + '.tmp')
        except Exception:
            pass
        return {'removed': 0, 'kept': total}
    return {'removed': removed + (total - len(window)), 'kept': len(window)}


def _ulog_archive_write(recs, conf=None):
    """把采集到的记录追加到归档文件（供 drouter-logd 定时调用）。"""
    conf = conf or _ulog_load()
    if not conf.get('enabled') or not conf.get('archive'):
        return 0
    min_rank = _ulog_level_rank(conf.get('min_level') or 'debug')
    keep = [r for r in recs if _ulog_level_rank(r.get('level')) <= min_rank]
    if not keep:
        return 0
    try:
        os.makedirs(LOGDIR, exist_ok=True)
        with open(ULOG_ARCHIVE, 'a', encoding='utf-8') as f:
            for r in keep:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
    except Exception:
        return 0
    return len(keep)


def read_logs(p):
    module = (p or {}).get('module') or 'all'
    limit = int((p or {}).get('limit') or 300)
    limit = max(1, min(limit, 5000))
    path = os.path.join(LOGDIR, '%s.jsonl' % re.sub(r'[^a-z0-9_-]', '', module))
    if not os.path.isfile(path):
        return ok([])
    with open(path, encoding='utf-8', errors='replace') as f:
        lines = f.read().splitlines()[-limit:]
    recs = []
    for ln in lines:
        try:
            recs.append(json.loads(ln))
        except Exception:
            pass
    return ok(recs)


def read_journal(p):
    unit = (p or {}).get('unit') or ''
    limit = int((p or {}).get('limit') or 200)
    limit = max(1, min(limit, 3000))
    cmd = ['journalctl', '-n', str(limit), '-o', 'short-iso', '--no-pager']
    if unit and re.match(r'^[a-zA-Z0-9_.@-]+$', unit):
        cmd += ['-u', unit]
    rc, out, err = sh(cmd, timeout=20)
    return ok({'lines': out.splitlines(), 'err': err})


# ------------------------------------------------------------------ 快照 / 回滚
#
# 设计要点：快照必须「全覆盖」用户的所有可配置内容，否则回滚会出现
# 「配置文件回去了、但界面里的设置没回去」这种半吊子状态。
# 因此快照分三层：
#   1) 数据库层：drouter.db 的 settings / ifaces / admins / snapshots 四张表
#      （settings 存全部界面配置，ifaces 存网卡备注与角色，admins 存 Web 账号）
#   2) 配置文件层：/etc 下由 drouter 生成的配置文件与目录
#   3) 系统层：hostname / 时区等被界面改过的系统设置
#
# 快照根目录由环境变量 DROUTER_SNAP_DIR 指定（界面可自定义）；默认 /opt/drouter/snapshots

DB_PATH = '/opt/drouter/data/drouter.db'
SNAP_DEFAULT = '/opt/drouter/snapshots'
SNAP_CONF = '/etc/drouter/snapshot.conf'   # 自动快照参数（界面写入）

# 需要纳入快照的 /etc 文件（drouter 生成或修改的）
# 名字带 SNAPSHOT_ 前缀，明确「这是快照清单」：里面含 /etc/hostname、/etc/timezone
# 这类系统文件，是刻意纳入的（属于用户设置），不要拿去做「清理 drouter 残留」。
SNAPSHOT_ETC_FILES = [
    '/etc/dnsmasq.d/drouter.conf', '/etc/radvd.conf', '/etc/dhcpcd.conf',
    '/etc/nftables.d/drouter-v4.nft', '/etc/nftables.d/drouter-v6.nft',
    '/etc/chrony/chrony.conf', '/etc/miniupnpd/miniupnpd.conf',
    '/etc/ppp/peers/drouter-wan', '/etc/ppp/chap-secrets', '/etc/ppp/pap-secrets',
    '/etc/hostname', '/etc/timezone',
    '/etc/drouter/generated/isp-dns.conf',
    '/etc/drouter/rescue.conf',
    '/etc/drouter/web-port',
    # 外置存储的「开机自动挂载」写的是 /etc/fstab。之前漏了它，表现为：
    # 配好自动挂载 → 出问题回滚 → 挂载配置随快照一起没了。
    # helper 自己还会在 generated 下留一份 fstab 备份，一并纳入。
    '/etc/fstab',
    '/etc/drouter/generated/fstab.drouter.bak',
]
# /etc/drouter 整目录纳入（实测只有几十 KB）：
#   generated/vlans.json 是 VLAN 配置、themes/ 是自定义主题、
#   docker/stacks/ 是 compose 文件、snapshot.conf / active-theme 也是用户设置。
# 以前只挑了其中三个文件，新增模块（VLAN / 主题之家 / Docker / 外置存储）
# 的落盘文件就都漏在快照外了。
SNAPSHOT_ETC_DIRS = ['/etc/systemd/network', '/etc/drouter']

# 快照中"用户设置"部分的表述（用于界面展示覆盖范围）
SNAPSHOT_SCOPE = [
    {'group': '界面配置（全部模块）', 'items': [
        'DHCP 服务与 Options（含静态绑定、逐客户端 Options）',
        'IPv4 / IPv6 防火墙规则',
        'WAN 口（PPPoE / DHCP / 静态地址）参数与宽带凭据',
        'LAN 口与网桥设置',
        'IPv6 地址池、池策略、RA 通告与 DHCPv6 前缀委派',
        'DNS 服务、UPnP、NTP 时间同步',
        '网卡角色与备注（按 MAC 绑定）',
        '访问范围、Web 管理端口、构建保护模式状态',
    ]},
    {'group': '系统文件', 'items': [
        '/etc/dnsmasq.d/drouter.conf（DHCP/DNS）',
        '/etc/nftables.d/drouter-v4.nft · drouter-v6.nft（防火墙）',
        '/etc/ppp/peers/drouter-wan · chap-secrets · pap-secrets（拨号）',
        '/etc/radvd.conf · /etc/dhcpcd.conf（IPv6）',
        '/etc/miniupnpd/miniupnpd.conf · /etc/chrony/chrony.conf',
        '/etc/systemd/network/*（网络后端配置）',
        '/etc/hostname · /etc/timezone（主机名与时区）',
        '/etc/fstab（外置存储开机自动挂载）',
        '/etc/drouter/ 整目录（Web 端口、救援通道、运营商 DNS、'
        'VLAN 配置、自定义主题、Docker compose 文件、自动快照设置、'
        'CA 证书库 /etc/drouter/ca 的证书与私钥）',
    ]},
    {'group': '账号与审计', 'items': [
        'Web 管理账号（用户名与密码哈希）',
        '系统用户 SSH 公钥（authorized_keys 相关设置）',
    ]},
]


SNAP_ALLOWED_PREFIXES = ('/opt', '/srv', '/var/backups', '/mnt', '/media', '/home')


def snap_root():
    """快照根目录：界面自定义优先，其次环境变量，最后默认值。

    三个来源**一律**限制在同一份前缀白名单里 —— 早先只有界面那条路做了校验，
    改库或带 env 直调 helper 就能让快照被写进任意绝对路径（顺带 makedirs）。
    """
    def _allowed(v):
        return bool(v) and os.path.isabs(v) and v.startswith(SNAP_ALLOWED_PREFIXES)

    env = os.environ.get('DROUTER_SNAP_DIR', '').strip()
    if env and _allowed(env):
        return env
    elif env:
        log('warn', 'snapshot', 'SNAP_DIR_REJECT',
            '环境变量指定的快照目录不在允许范围内，已忽略：%s' % env)
    # 从 drouter.db 里读取界面设置的路径
    try:
        import sqlite3
        if os.path.exists(DB_PATH):
            conn = sqlite3.connect(DB_PATH)
            r = conn.execute("SELECT value FROM settings WHERE key='snapshot'").fetchone()
            conn.close()
            if r and r[0]:
                v = json.loads(r[0]).get('path')
                if v and os.path.isabs(v):
                    if v.startswith(SNAP_ALLOWED_PREFIXES):
                        return v
                    log('warn', 'snapshot', 'SNAP_DIR_REJECT',
                        '数据库里记录的快照目录不在允许范围内，已回落默认值：%s' % v)
    except Exception:
        pass
    return SNAP_DEFAULT


def _sqlite_snapshot(dst_dir):
    """把数据库整库冷备（sqlite3 .backup 语义：先复制再校验），返回是否成功。"""
    if not os.path.isfile(DB_PATH):
        return False, '数据库不存在'
    dst = os.path.join(dst_dir, 'drouter.db')
    try:
        import sqlite3
        src = sqlite3.connect(DB_PATH)
        out = sqlite3.connect(dst)
        with out:
            src.backup(out)          # 一致性备份，比直接 copy 安全
        out.close()
        src.close()
        return True, dst
    except Exception as e:
        # 回退到文件复制
        try:
            shutil.copy2(DB_PATH, dst)
            return True, dst
        except Exception:
            return False, str(e)


def _snap_load_meta(d):
    """读一份快照目录的 _meta.json。读不出来就返回空 dict。

    prune 与 list 都要读meta，重复一遍 try/except 只会让「读失败」这件事
    在两处各自漂成不同的默认值 —— 所以统一走这里，**默认 protected=False**
    （读不到就当没保护，宁可多留一份，也别因为 meta 损坏就把用户上锁的
    快照当成没上锁给清掉了）。
    """
    p = os.path.join(d, '_meta.json')
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, encoding='utf-8') as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def _snap_save_meta(d, updates):
    """原地改一份快照的 _meta.json（只改传入的键），返回最新 meta。

    先写临时文件再 os.replace：meta 是 prune 唯一的判据来源，
    写到一半被打断会留下一个 JSON 解析不了的残file，那份快照就再也
    认不出自己上没上锁了。
    """
    meta = _snap_load_meta(d)
    meta.update(updates or {})
    p = os.path.join(d, '_meta.json')
    tmp = p + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise e
    return meta


SNAP_TAG_MAX = 60


def _snap_clean_tag(raw, fallback='manual'):
    """把用户填的备注压成一个安全的单行短标签。

    tag 会被拼进日志消息、导出文件名和 shell 片段，所以控制字符和换行
    必须在这里就掐掉 —— 不然后面每一处都得各自记得转义一遍，漏一处
    就是日志注入或文件名注入。

    ⚠️ 不可打印字符一律**替换成空格**，不是删掉。
    第一版只把 \\r \\n 换成空格，剩下的走 `if ch.isprintable()` 过滤 ——
    而 isprintable() 对 \\t 也返回 False，于是 \\t 被**删除**：
    'a\\tb' 变成 'ab'，相邻两个词被粘成一坨；控制字符夹在词中间时
    用户看到的备注是莫名其妙连在一起的。删字符等于替用户改内容，
    换成空格才是「压成一行」的本意（后面的 `\\s+ → ' '` 会再折叠）。
    """
    s = ''.join(ch if ch.isprintable() else ' ' for ch in str(raw or ''))
    s = re.sub(r'\s+', ' ', s).strip()
    if len(s) > SNAP_TAG_MAX:
        s = s[:SNAP_TAG_MAX].rstrip() + '…'
    return s or fallback


def _snap_dir(root, ts):
    """定位一份快照的目录，顺带做合法性校验（防路径穿越）。"""
    if not re.match(r'^[0-9]{8}-[0-9]{6}$', str(ts or '')):
        return None
    root = os.path.abspath(root)
    d = os.path.abspath(os.path.join(root, str(ts)))
    try:
        if os.path.commonpath([d, root]) != root:
            return None
    except Exception:
        return None
    return d if os.path.isdir(d) else None


def _snapshot(tag='manual', protected=False):
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    root = snap_root()
    d = os.path.join(root, ts)
    os.makedirs(d, exist_ok=True)
    saved = []
    skipped_all = []
    # 1) 数据库整库
    db_ok, db_info = _sqlite_snapshot(d)
    if db_ok:
        saved.append('sqlite:drouter.db')
    # 2) /etc 文件
    for f in SNAPSHOT_ETC_FILES:
        if os.path.isfile(f):
            dst = os.path.join(d, f.replace('/', '_'))
            try:
                shutil.copy2(f, dst)
                saved.append(f)
            except Exception:
                pass
    for sub in SNAPSHOT_ETC_DIRS:
        if os.path.isdir(sub):
            dst = os.path.join(d, sub.replace('/', '_'))
            try:
                n, sk = _copytree_soft(sub, dst)
                # 目录为空时 n=0，但目录本身已经建好了，也要记进清单，
                # 否则界面上会以为这个目录压根没进快照。
                if n or os.path.isdir(dst):
                    saved.append('%s（%d 个文件）' % (sub, n))
                if sk:
                    skipped_all.extend(sk)
                    log('warn', 'system', 'SNAPSHOT_SKIP',
                        '快照跳过 %d 个读不到的文件（多为 root 专属权限）' % len(sk),
                        {'dir': sub, 'files': sk[:10]})
            except Exception:
                pass
    # 3) 元数据（含覆盖范围清单，便于界面展示与校验）
    total, used, free = _disk_usage(root)
    meta = {
        'ts': ts, 'tag': tag,
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'files': saved,
        'db_included': db_ok,
        'db_detail': str(db_info)[:300],
        'count': len(saved),
        # 读不到而被跳过的文件（root 专属权限等）。留痕才不会「以为备份了其实没有」。
        'skipped': skipped_all[:50],
        'skipped_count': len(skipped_all),
        'size_kb': _dir_size_kb(d),
        'root': root,
        'expire_at': '',
        # 用户显式上锁。上锁后自动清理（按天 / 按数量）一律跳过这份，
        # 但手动删除仍然允许 —— 否则上锁就成了「删不掉」。
        'protected': bool(protected),
    }
    with open(os.path.join(d, '_meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    log('info' if not skipped_all else 'warn', 'system', 'SNAPSHOT_CREATED',
        '已创建配置快照 %s（%s，共 %d 项%s）'
        % (ts, tag, len(saved),
           '，%d 个文件因权限跳过' % len(skipped_all) if skipped_all else ''), saved)
    return ts, d


def _copytree_soft(src, dst):
    """把 src 目录复制成 dst：单个文件读不到就跳过，绝不报废整个目录。

    shutil.copytree 的语义是「有一个文件失败就整体失败」。/etc/drouter 下只要
    有一个属主 root、权限 0600 的文件（例如 rescue.conf），以 drouter 身份跑的
    快照就读不到，copytree 抛异常 —— 前面复制的半截留在盘上，saved 列表里
    也不记这一项。表现是：界面提示「已创建快照」，实际这个目录根本没进去，
    回滚时自然也还原不了。这种失败方式是最难发现的，所以改成逐文件容错，
    并把跳过的文件明确记下来。
    """
    copied, skipped = 0, []
    os.makedirs(dst, exist_ok=True)
    for base, dirs, files in os.walk(src):
        rel = os.path.relpath(base, src)
        out = dst if rel == '.' else os.path.join(dst, rel)
        try:
            os.makedirs(out, exist_ok=True)
        except Exception as e:
            skipped.append('%s/ (%s)' % (base, e))
            dirs[:] = []          # 目录都建不了，里面的也不用试了
            continue
        for f in files:
            s = os.path.join(base, f)
            try:
                shutil.copy2(s, os.path.join(out, f))
                copied += 1
            except Exception as e:
                skipped.append('%s (%s)' % (s, e))
    return copied, skipped


def _dir_size_kb(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except Exception:
                pass
    return round(total / 1024.0, 1)


def _disk_usage(path):
    try:
        st = os.statvfs(path if os.path.isdir(path) else os.path.dirname(path))
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        return round(total / 1048576.0, 1), round((total - free) / 1048576.0, 1), round(free / 1048576.0, 1)
    except Exception:
        return 0, 0, 0


def act_snapshot(p):
    p = p or {}
    tag = _snap_clean_tag(p.get('tag') or '', 'manual')
    # checkbox 未勾选时前端不会带这个键，所以只有「显式为真」才算上锁。
    protect = p.get('protected') in (True, '1', 'true', 'on', 1)
    ts, d = _snapshot(tag, protected=protect)
    return ok({'ts': ts, 'path': d, 'tag': tag, 'protected': protect},
              '已创建配置快照：%s%s' % (ts, '（已上锁，不参与自动清理）' if protect else ''))


def act_snapshot_note(p):
    """改一份已存在快照的备注。

    备注字段本来就有，但此前只能在创建时填，存下来之后就只能看不能改
    —— 于是「先随手拍一张，回头再补上说明」这个最自然的用法走不通：
    拍的时候不知道该写什么，等想明白了又改不了。
    """
    p = p or {}
    d = _snap_dir(snap_root(), p.get('ts'))
    if not d:
        return fail('快照不存在：%s' % (p.get('ts') or ''))
    # 允许清空备注（传空串），但不能因为空就退回 'manual' 那类占位词
    raw = p.get('tag')
    tag = _snap_clean_tag(raw, '') if raw is not None else \
        _snap_clean_tag(p.get('note'), '')
    try:
        meta = _snap_save_meta(d, {'tag': tag})
    except Exception as e:
        return fail('写入快照备注失败：%s' % e)
    log('info', 'system', 'SNAPSHOT_NOTE', '已修改快照 %s 的备注' % os.path.basename(d),
        {'tag': tag})
    return ok({'ts': os.path.basename(d), 'tag': meta.get('tag', '')},
              '已保存快照备注' if tag else '已清空快照备注')


def act_snapshot_protect(p):
    """给/取消一份快照的上锁。返回切换后的状态。"""
    p = p or {}
    d = _snap_dir(snap_root(), p.get('ts'))
    if not d:
        return fail('快照不存在：%s' % (p.get('ts') or ''))
    if 'protected' in p:
        want = p.get('protected') in (True, '1', 'true', 'on', 1)
    else:
        want = not bool(_snap_load_meta(d).get('protected'))
    try:
        _snap_save_meta(d, {'protected': bool(want)})
    except Exception as e:
        return fail('写入快照保护状态失败：%s' % e)
    name = os.path.basename(d)
    log('info', 'system', 'SNAPSHOT_PROTECT',
        '快照 %s %s' % (name, '已上锁，自动清理将跳过' if want else '已解除上锁'),
        {'protected': bool(want)})
    return ok({'ts': name, 'protected': bool(want)},
              '快照 %s 已上锁，自动清理不会删除它' % name if want
              else '已解除 %s 的保护' % name)


def act_snapshot_list(_):
    root = snap_root()
    if not os.path.isdir(root):
        return ok({'items': [], 'root': root, 'scope': SNAPSHOT_SCOPE})
    items = []
    for name in sorted(os.listdir(root), reverse=True):
        d = os.path.join(root, name)
        m = _snap_load_meta(d)
        if m:
            m.setdefault('ts', name)
            m.setdefault('size_kb', _dir_size_kb(d))
            # setdefault 而不是 m.get：老快照的 meta 里压根没这个键，
            # 界面上要显示成「未上锁」而不是「空白」，否则用户会以为
            # 加载失败。
            m.setdefault('protected', False)
            items.append(m)
        elif os.path.isdir(d):
            items.append({'ts': name, 'tag': 'unknown', 'files': [],
                          'size_kb': _dir_size_kb(d), 'protected': False})
    total, used, free = _disk_usage(root)
    return ok({'items': items, 'root': root, 'scope': SNAPSHOT_SCOPE,
               'disk': {'total_mb': total, 'used_mb': used, 'free_mb': free}})


def act_snapshot_delete(p):
    """删除指定快照（用于自动清理与手动删除）。

    受保护的快照必须显式带 force=1 才能删。上锁的语义是「自动清理别碰
    它」，不是「它成了只读」——但如果点一下普通删除就能把用户特意保下来
    的那份弄掉，那上锁就是个骗人的开关。所以这里不直接拒绝，而是**要求
    二次确认**：前端收到 needs_force 后弹一次「这份已上锁，确定要删吗」，
    确认后带 force 回来。
    """
    p = p or {}
    ts = str(p.get('ts') or '').strip()
    if not re.match(r'^[0-9]{8}-[0-9]{6}$', ts):
        return fail('快照标识格式不正确')
    root = snap_root()
    d = os.path.join(root, ts)
    if os.path.commonpath([os.path.abspath(d), os.path.abspath(root)]) != os.path.abspath(root):
        return fail('非法的快照路径')
    if not os.path.isdir(d):
        return fail('快照不存在：%s' % ts)
    locked = bool(_snap_load_meta(d).get('protected'))
    if locked and p.get('force') not in (True, '1', 'true', 'on', 1):
        return fail('这份快照已上锁，自动清理不会删除它。若确实要删除，请确认后重试。',
                    data={'needs_force': True, 'ts': ts, 'protected': True})
    shutil.rmtree(d, ignore_errors=True)
    log('warn', 'system', 'SNAPSHOT_DELETED', '已删除快照 %s%s' % (ts, '（受保护，已强制删除）' if locked else ''))
    return ok({'ts': ts}, '已删除快照 %s' % ts)


def act_snapshot_prune(p):
    """
    按「过期时间」清理快照：
      keep_days   —— 超过 N 天的快照删除（0 = 不按时间清）
      keep_count  —— 最多保留 N 份（0 = 不限数量）
      keep_manual —— 是否保留手动/救援等非自动快照（默认 True）

    上锁（_meta.json 里的 protected）的快照在两条规则下都无条件跳过。
    它跟 keep_manual 是两件不同的事，别混成一条判据：
      keep_manual —— 按 tag 是否以 auto 开头**猜**来源，手动创建、
                    救援前、升级前拍的都算手动；是全局策略开关。
      protected   —— 用户对**这一份**的显式意图，跨策略生效。
    所以判据是 `not e['protected']` 单独加在删除分支上，而不是改
    `auto` 的算法 —— 把 protected 混进 auto 的话，一份上了锁的
    auto 快照就会开始占用 keep_count 配额，反而把别的自动快照挤掉。
    """
    p = p or {}
    try:
        keep_days = int(p.get('keep_days') or 0)
    except Exception:
        keep_days = 0
    try:
        keep_count = int(p.get('keep_count') or 0)
    except Exception:
        keep_count = 0
    keep_manual = p.get('keep_manual', True) is not False
    root = snap_root()
    if not os.path.isdir(root):
        return ok({'removed': [], 'kept': 0}, '快照目录不存在，无需清理')
    entries = []
    for name in sorted(os.listdir(root), reverse=True):
        full = os.path.join(root, name)
        if not os.path.isdir(full) or not re.match(r'^[0-9]{8}-[0-9]{6}$', name):
            continue
        m = _snap_load_meta(full)
        tag = m.get('tag') or ''
        try:
            born = datetime.strptime(name, '%Y%m%d-%H%M%S')
        except Exception:
            born = datetime.min
        entries.append({'name': name, 'tag': tag, 'born': born,
                        'auto': tag.startswith('auto'),
                        'protected': bool(m.get('protected'))})
    now = datetime.now()
    removed = []
    kept_locked = [e['name'] for e in entries if e['protected']]
    # 1) 按天
    if keep_days > 0:
        for e in list(entries):
            if e['protected']:
                continue
            age_days = (now - e['born']).total_seconds() / 86400.0
            if age_days > keep_days and not (keep_manual and not e['auto']):
                shutil.rmtree(os.path.join(root, e['name']), ignore_errors=True)
                removed.append(e['name'])
                entries.remove(e)
    # 2) 按数量（只对自动快照计数，手动的不占用配额）
    if keep_count > 0:
        # 上锁的自动快照不参与计数：它反正删不掉，让它占配额等于
        # 把 keep_count 悄悄削掉一份，用户会看着「保留 5 份」却只剩 4 份。
        autos = [e for e in entries if e['auto'] and not e['protected']]
        for e in autos[keep_count:]:
            shutil.rmtree(os.path.join(root, e['name']), ignore_errors=True)
            removed.append(e['name'])
            entries.remove(e)
    if removed:
        log('info', 'system', 'SNAPSHOT_PRUNED',
            '自动清理快照 %d 份（保留 %d）' % (len(removed), len(entries)), removed)
    return ok({'removed': removed, 'kept': len(entries), 'root': root,
               'kept_locked': kept_locked},
              '已清理 %d 份过期快照' % len(removed))


def act_snapshot_pack(p):
    """
    把一份快照目录打包为 .tar.gz，供用户从 Web 界面下载到本地电脑。
    产物固定放在 /tmp/drouter-snapshot-dl/ 下，便于 Web 层读取后清理。
    仅允许打包合法快照编号，防止路径穿越。
    """
    p = p or {}
    ts = str(p.get('ts') or '').strip()
    if not re.match(r'^[0-9]{8}-[0-9]{6}$', ts):
        return fail('快照编号格式不正确')
    root = os.path.abspath(snap_root())
    src = os.path.abspath(os.path.join(root, ts))
    if os.path.commonpath([src, root]) != root or not os.path.isdir(src):
        return fail('快照不存在：%s' % ts)
    outdir = '/tmp/drouter-snapshot-dl'
    os.makedirs(outdir, exist_ok=True)
    # 清掉上一次遗留的打包产物，避免 /tmp 堆积
    for old in os.listdir(outdir):
        if old.endswith('.tar.gz'):
            try:
                os.remove(os.path.join(outdir, old))
            except Exception:
                pass
    out = os.path.join(outdir, 'drouter-snapshot-%s.tar.gz' % ts)
    try:
        with tarfile.open(out, 'w:gz') as tf:
            tf.add(src, arcname=ts)
    except Exception as e:
        return fail('打包快照失败：%s' % e)
    try:
        os.chmod(out, 0o644)
    except Exception:
        pass
    size = os.path.getsize(out)
    log('info', 'system', 'SNAPSHOT_PACKED', '已打包快照 %s（%.1f KB）' % (ts, size / 1024.0))
    return ok({'ts': ts, 'path': out, 'size': size},
              '快照已打包（%.1f KB）' % (size / 1024.0))


# ------------------------------------------------------- 配置备份 / 还原（1.0.7）
#
# ── 它和快照有什么不同 ──────────────────────────────────────────────────────
# 快照是**本机回滚**用的：留在 /opt/drouter/snapshots 里，只服务于「刚才那次改动
# 改坏了，退回去」。所以它按目录整份复制，体积随日志/数据库一起涨。
# 备份是**给人拿走**用的：导出一个能下载、能存到另一台机器、能在新机器上一键
# 还原的包。两个诉求相反 —— 快照要「全」，备份要「小 + 可移植 + 自描述」。
#
# ── 为什么必须自带 MANIFEST + 每文件 sha256 ─────────────────────────────────
# 还原最怕两件事：
#   1. 三个月后你根本不记得这个包里有什么、是哪台机器导出的、是不是被改过；
#   2. 包在传输中损坏，还原时写进去一堆半截文件，比不还原还糟。
# 所以每个包都带一份 MANIFEST.json（版本、导出时间、主机名、drouter 版本、
# 文件清单、每个文件的 sha256 与权限），还原默认先校验，不一致就拒绝。
#
# ── 安全 ────────────────────────────────────────────────────────────────────
# * 还原路径必须命中 BK_ALLOW_PREFIX 白名单，且逐层 realpath 校验，
#   杜绝 tar 里的 ../ 穿越写到 /etc/shadow；
# * 解包有总字节数与文件数上限（防 tar 炸弹）；
# * 私钥（CA 私钥、WireGuard 私钥、ppp 凭据）单独标记，默认**排除**，
#   需要用户显式勾选「含敏感文件」才带上 —— 备份是要下载到本地电脑的。

BACKUP_DIR = '/opt/drouter/backups'
BACKUP_DL = '/tmp/drouter-backup-dl'
BACKUP_CONF = '/etc/drouter/backup.conf'
BACKUP_MANIFEST = 'MANIFEST.json'
# 备份格式版本。还原端按此判断兼容性，不认识的版本直接拒绝而不是猜。
BACKUP_FORMAT = 1
# tar 炸弹防护：单包解出来的总字节与文件数上限
BK_MAX_UNPACK = 64 * 1024 * 1024
BK_MAX_FILES = 2000
# 允许写入的路径前缀。与 snap_root() 的白名单是两套：
# 备份要能覆盖 /etc 下的配置，所以范围更大，但仍只认这几个根。
BK_ALLOW_PREFIX = ('/etc/', '/opt/drouter/', '/var/lib/drouter/')

# 需要纳入备份的敏感文件（默认不导出，用户勾选后才带）
BK_SENSITIVE = [
    '/etc/drouter/ca',
    '/etc/wireguard',
    '/etc/ppp/chap-secrets',
    '/etc/ppp/pap-secrets',
    '/etc/dnsmasq.d/drouter.conf',
]

# 备份内容分组。界面上按这个顺序显示，让用户看得懂「备份了什么」。
BACKUP_SCOPE = [
    {'group': '界面配置（数据库）', 'items': [
        '全部模块的设置值（drouter.db 的 settings / ifaces / admins）',
        '网卡备注与角色绑定',
    ]},
    {'group': '网络与防火墙配置', 'items': [
        '网卡与桥接、LAN / WAN 口参数',
        'DHCP 与 DNS（drouter.conf）',
        'IPv6 地址池、RA 通告、DHCPv6 前缀委派',
        'IPv4 / IPv6 防火墙规则集（nftables）',
        '端口转发与 DMZ、UPnP 配置',
        'PPPoE 拨号配置（不含账号密码）',
    ]},
    {'group': '服务与应用', 'items': [
        '智能限速 QoS 规则、应用识别 DPI 前缀库',
        '访问控制与家长时间组',
        '文件共享 Samba 配置、CUPS 打印配置',
        'Docker Compose 栈文件与引擎配置（daemon.json）',
        'NTP 时间同步、内核转发与 BBR 参数',
    ]},
    {'group': '外观与个性化', 'items': [
        '当前主题、自定义主题文件',
    ]},
    {'group': '敏感文件（需显式勾选才导出）', 'items': [
        'CA 证书库与私钥、WireGuard 私钥',
        'PPPoE 宽带账号密码、DHCP 静态绑定 MAC',
    ]},
]


def backup_dir():
    """备份包存放目录。允许自定义，但必须在允许前缀内。"""
    def _allowed(v):
        return bool(v) and os.path.isabs(v) and v.startswith(BK_ALLOW_PREFIX)

    env = os.environ.get('DROUTER_BACKUP_DIR', '').strip()
    if env and _allowed(env):
        return env
    elif env:
        log('warn', 'backup', 'BK_DIR_REJECT',
            '环境变量指定的备份目录不在允许范围内，已忽略：%s' % env)
    try:
        v = (_load_setting('backup') or {}).get('path')
        if v and os.path.isabs(v):
            if _allowed(v):
                return v
            log('warn', 'backup', 'BK_DIR_REJECT',
                '数据库里记录的备份目录不在允许范围内，已回落默认值：%s' % v)
    except Exception:
        pass
    return BACKUP_DIR


def _bk_load():
    return _load_setting('backup') or {}


def _bk_save(d):
    return _save_setting('backup', d)


def _bk_norm(d):
    """归一化备份设置。

    注意 int(raw or 默认) 会吞掉用户填的 0 —— 先判空再转 int，再钳制。
    """
    d = d if isinstance(d, dict) else {}
    out = {
        'path': str(d.get('path') or BACKUP_DIR),
        'keep_count': 0,
        'keep_days': 0,
        'auto_enabled': False,
        'auto_hour': 3,
        'include_sensitive': False,
        'note': str(d.get('note') or '')[:200],
    }
    for k, lo, hi in (('keep_count', 0, 999), ('keep_days', 0, 3650),
                      ('auto_hour', 0, 23)):
        raw = d.get(k)
        if raw is None or raw == '':
            continue
        try:
            v = int(raw)
        except Exception:
            continue
        out[k] = max(lo, min(hi, v))
    out['auto_enabled'] = d.get('auto_enabled') is True
    out['include_sensitive'] = d.get('include_sensitive') is True
    if not out['path'].startswith(BK_ALLOW_PREFIX):
        out['path'] = BACKUP_DIR
    return out


def _bk_files(include_sensitive=False):
    """列出要备份的文件。

    数据来源刻意与快照的清单同源（SNAPSHOT_ETC_FILES / _DIRS / DB_PATH），
    避免出现「快照里有、备份里没有」的功能项 —— 用户会发现两次导出的
    文件列表不一样，进而怀疑备份不完整。
    """
    items = []
    seen = set()

    def _add(path, optional=True, sensitive=False):
        if path in seen:
            return
        seen.add(path)
        items.append({'path': path, 'optional': optional, 'sensitive': sensitive})

    # 数据库（界面配置的真源）
    _add(DB_PATH, optional=True)
    # /etc 下的生成文件
    for f in SNAPSHOT_ETC_FILES:
        _add(f, optional=True,
             sensitive=f in ('/etc/ppp/chap-secrets', '/etc/ppp/pap-secrets',
                             '/etc/dnsmasq.d/drouter.conf'))
    # /etc 目录整目录（含 generated/vlans.json、themes/、docker/stacks/、ca/）
    #
    #⚠️ 这里**不能**因为里面有ca/ 就把整个目录标成 sensitive。
    # 早先的版本写了 sensitive=(d == '/etc/drouter')，看着只是「多漏一个目录」，
    # 实际后果是：默认导出的包里**没有 dcfg.json、没有 kern.conf、没有
    # generated/vlans.json、没有 Docker 栈文件、没有自定义主题**——
    # 而 BACKUP_SCOPE 里恰恰把这些都宣称在备份范围内。用户导出、换机还原，
    # 会发现大半配置没了，而且界面全程显示「已导出」，没有任何提示。
    # 敏感与否是**逐文件**属性，目录只是容器。真正的排除交给
    # _bk_walk() 里的 _bk_is_sensitive() 按 BK_SENSITIVE 精确判定。
    for d in SNAPSHOT_ETC_DIRS:
        _add(d, optional=True)
    # 运行时状态里值得带走的两样
    _add('/opt/drouter/data/active-theme', optional=True)
    _add('/opt/drouter/data/third-party', optional=True)

    if not include_sensitive:
        items = [i for i in items if not i['sensitive']]
    return items


def _bk_is_sensitive(path):
    """判断单个绝对路径是否属于敏感文件（默认不导出的那部分）。

    判定用「路径等于或位于 BK_SENSITIVE 某项之下」，而不是逐个枚举 ——
    BK_SENSITIVE 里既有文件（chap-secrets）也有目录（ca/、wireguard/），
    写成前缀比较才能同时覆盖两种。新增敏感项只要往列表里加一行。
    """
    p = os.path.normpath(str(path or ''))
    for s in BK_SENSITIVE:
        s = os.path.normpath(s)
        if p == s or p.startswith(s.rstrip('/') + '/'):
            return True
    return False


def _bk_walk(root, include_sensitive=False, skipped=None):
    """把一个文件或目录展开成 (绝对路径, 归档内相对路径, 是否敏感) 列表。

    相对路径统一去掉开头的 '/'，还原时再用白名单还原回绝对路径 ——
    这样 tar 里不会存绝对路径，也就不存在「解压到哪」的问题。

    include_sensitive=False 时跳过 BK_SENSITIVE 里的子树，并把跳过的
    路径 append 到 skipped —— 用户要能看见「这个包里没有 CA 私钥」，
    否则换机后才发现证书没了。
    """
    out = []
    if skipped is None:
        skipped = []

    def _skip(full, why):
        skipped.append({'path': full, 'why': why})

    if os.path.isfile(root):
        full = os.path.abspath(root)
        if not include_sensitive and _bk_is_sensitive(full):
            _skip(full, '敏感文件，默认不导出')
        else:
            out.append((full, full.lstrip('/'),
                        _bk_is_sensitive(full)))
    elif os.path.isdir(root):
        base = os.path.abspath(root)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames.sort()
            # 敏感目录整棵剪掉，别走进去再逐个文件判
            if not include_sensitive:
                keep = []
                for dn in dirnames:
                    sub = os.path.normpath(os.path.join(dirpath, dn))
                    if _bk_is_sensitive(sub):
                        _skip(sub + '/', '敏感目录，默认不导出')
                    else:
                        keep.append(dn)
                dirnames[:] = keep
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                if not include_sensitive and _bk_is_sensitive(full):
                    _skip(full, '敏感文件，默认不导出')
                    continue
                out.append((os.path.abspath(full),
                            os.path.abspath(full).lstrip('/'),
                            _bk_is_sensitive(full)))
    return out


def _bk_sha256(path):
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(262144), b''):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ''


def _bk_version():
    try:
        with open('/opt/drouter/VERSION', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        try:
            import importlib.metadata as md
            return md.version('drouter')
        except Exception:
            return '未知'


def _bk_hostname():
    try:
        with open('/etc/hostname', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        try:
            return socket.gethostname()
        except Exception:
            return '未知'


def act_backup(p):
    """配置备份与还原：status / create / list / delete / pack /
    inspect / restore / prune / conf。

    刻意把「导出」和「打包下载」分成两步（create 与 pack）：
    备份包生成在 /opt/drouter/backups 里可以慢慢跑、可以定时跑，
    而下载是一次性的短命产物。合成一步会导致每点一次下载就重算一遍 sha256。
    """
    p = p or {}
    op = str(p.get('op') or 'status')
    if op == 'status':
        return _bk_status()
    if op == 'create':
        return _bk_create(p)
    if op == 'list':
        return _bk_list()
    if op == 'delete':
        return _bk_delete(p)
    if op == 'pack':
        return _bk_pack(p)
    if op == 'inspect':
        return _bk_inspect(p)
    if op == 'restore':
        return _bk_restore(p)
    if op == 'prune':
        return _bk_prune(p)
    if op == 'conf':
        return _bk_conf(p)
    return fail('未知的备份操作：%s' % op)


def _bk_status():
    d = backup_dir()
    items = _bk_files(bool(_bk_load().get('include_sensitive')))
    total = 0
    present = 0
    for it in items:
        p = it['path']
        if os.path.isfile(p):
            present += 1
            try:
                total += os.path.getsize(p)
            except Exception:
                pass
        elif os.path.isdir(p):
            present += 1
            for full, _rel, _sens in _bk_walk(
                    p, include_sensitive=bool(
                        _bk_load().get('include_sensitive'))):
                try:
                    total += os.path.getsize(full)
                except Exception:
                    pass
    conf = _bk_load()
    packs = _bk_list_entries()
    return ok({
        'dir': d,
        'exists': os.path.isdir(d),
        'scope': BACKUP_SCOPE,
        'planned': items,
        'present': present,
        'planned_total': len(items),
        'bytes': total,
        'packs': packs,
        'conf': _bk_norm(conf),
        'version': _bk_version(),
        'hostname': _bk_hostname(),
        'allow_prefix': list(BK_ALLOW_PREFIX),
    }, '已读取备份状态')


def _bk_list_entries():
    """列出已存在的备份包（只认 .tar.gz 且文件名合法）。

    ⚠️ 这里**必须调 _bk_safe_name()，不能再抄一份正则**。
    早先这里是内联的正则，于是 10-03 修「导出后列表里看不到包」时
    只改了 _bk_safe_name，这一处漏改 —— 结果变成：
        create / restore / delete / inspect 认这个包（已修）
        list 仍然不认（漏改）→ 界面永远显示 0 个包
    一个 bug 抄两份，改一处修一半，另一处继续坏，而且**测试全绿**
    （t-107 只查了 _bk_safe_name 自己）。

    教训：**同一份正则只允许存在一份**，第二处必须是调用。
    真要各写各的，就得让 t-107 断言「helper 里这个正则字面只出现一次」。
    """
    d = backup_dir()
    out = []
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d), reverse=True):
        if not name.endswith('.tar.gz') or _bk_safe_name(name) is None:
            continue
        full = os.path.join(d, name)
        try:
            st = os.stat(full)
        except Exception:
            continue
        note = ''
        if not os.access(full, os.R_OK):
            note = '当前用户无权读取'
        out.append({'name': name, 'size': st.st_size,
                    'mtime': datetime.fromtimestamp(st.st_mtime)
                    .isoformat(timespec='seconds'),
                    'note': note})
    return out


def _bk_list():
    return ok({'packs': _bk_list_entries(), 'dir': backup_dir()},
              '已列出备份包')


def _bk_ts():
    return datetime.now().strftime('%Y%m%d-%H%M%S')


def _bk_create(p):
    """导出一份备份包。

    命名带主机名后缀：工作室里常有好几台设备，下载到电脑上一堆
    drouter-backup-20261002-120000.tar.gz 分不清是谁的。
    """
    conf = _bk_norm(_bk_load())
    if p.get('include_sensitive') is True:
        conf['include_sensitive'] = True
    if p.get('note'):
        conf['note'] = str(p.get('note'))[:200]
    d = backup_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except Exception as e:
        return fail('创建备份目录失败：%s' % e)
    ts = _bk_ts()
    host = re.sub(r'[^A-Za-z0-9_-]', '', _bk_hostname()) or 'router'
    host = host[:24].lower()
    name = 'drouter-backup-%s-%s.tar.gz' % (ts, host)
    out = os.path.join(d, name)
    # 先写临时文件再改名：中途失败不会留下一个「看起来存在」的半个包
    tmp = out + '.part'
    entries = []
    missing = []
    excluded = []
    total_bytes = 0
    try:
        with tarfile.open(tmp, 'w:gz') as tf:
            # ⚠️ 跨组去重：_bk_files() 只对**根路径**去重，
            # 展开后的文件没有。SNAPSHOT_ETC_FILES 里的 /etc/drouter/rescue.conf
            # 与 SNAPSHOT_ETC_DIRS 展开出的 /etc/drouter/rescue.conf 会**各进一次**，
            # 于是包内同一文件出现两条、file_count 虚高、体积白算一倍。
            # 同一个 arcname 写两遍tar 也不报错，只是后者覆盖前者 ——
            # 属于「静默重复」，不修的话 file_count 和 total_bytes 都不可信。
            # 这里按 arcname 记住已写入的，归第一个出现的组。
            seen_arc = set()
            for it in _bk_files(conf['include_sensitive']):
                src = it['path']
                pairs = _bk_walk(src,
                                 include_sensitive=conf['include_sensitive'],
                                 skipped=excluded)
                if not pairs:
                    # 别把「因为敏感被跳过」误报成「文件不存在」——
                    # 用户会以为配置丢了，实际上是有意不给的。
                    if not any(e['path'].rstrip('/') in
                               (src, os.path.abspath(src)) for e in excluded):
                        missing.append({'path': src, 'why': '文件不存在'})
                    continue
                for full, rel, sens in pairs:
                    if rel in seen_arc:
                        continue
                    seen_arc.add(rel)
                    try:
                        st = os.stat(full)
                    except Exception as e:
                        missing.append({'path': full, 'why': str(e)})
                        continue
                    try:
                        tf.add(full, arcname=rel, recursive=False)
                    except Exception as e:
                        missing.append({'path': full, 'why': '打包失败：%s' % e})
                        continue
                    entries.append({'path': '/' + rel, 'size': st.st_size,
                                    'mode': oct(st.st_mode & 0o777),
                                    'sha256': _bk_sha256(full),
                                    'group': it['path'],
                                    'sensitive': bool(sens)})
                    total_bytes += st.st_size
            manifest = {
                'format': BACKUP_FORMAT,
                'created': datetime.now().isoformat(timespec='seconds'),
                'hostname': _bk_hostname(),
                'drouter_version': _bk_version(),
                'include_sensitive': bool(conf['include_sensitive']),
                'note': conf['note'],
                'file_count': len(entries),
                'total_bytes': total_bytes,
                'files': entries,
                'groups': [it['path'] for it in
                           _bk_files(conf['include_sensitive'])],
                'missing': missing,
                # 因为敏感被排除的路径。界面上要单独列出来 ——
                # 用户必须能预先知道「这个包换到新机器后证书/私钥要重做」。
                'excluded': excluded,
            }
            raw = json.dumps(manifest, ensure_ascii=False, indent=2)
            ti = tarfile.TarInfo(BACKUP_MANIFEST)
            ti.size = len(raw.encode('utf-8'))
            ti.mode = 0o644
            import io
            tf.addfile(ti, io.BytesIO(raw.encode('utf-8')))
        os.replace(tmp, out)
        os.chmod(out, 0o600 if conf['include_sensitive'] else 0o644)
    except Exception as e:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        log('error', 'backup', 'BK_CREATE_FAIL', '导出备份包失败：%s' % e)
        return fail('导出备份包失败：%s' % e)
    size = os.path.getsize(out)
    log('info', 'backup', 'BK_CREATED',
        '已导出备份包 %s（%d 个文件，%.1f KB）' % (name, len(entries), size / 1024.0),
        {'sensitive': conf['include_sensitive'], 'missing': len(missing),
         'excluded': len(excluded)})
    msg = '备份已导出：%d 个文件（%.1f KB）' % (len(entries), size / 1024.0)
    # 有敏感项被排除时必须在消息里说清楚，否则用户以为「全都备份了」。
    if excluded and not conf['include_sensitive']:
        msg += '；%d 项敏感内容未包含（CA 私钥 / 宽带密码等），换机后需重新配置' \
               % len(excluded)
    return ok({'name': name, 'path': out, 'size': size,
               'files': len(entries), 'missing': missing,
               'excluded': excluded,
               'manifest': manifest}, msg)


def _bk_safe_name(name):
    """校验备份包文件名，杜绝 ../ 与绝对路径。

    ⚠️ 这个正则必须和 _bk_pack() 的命名规则**严格一致**：
        命名：'drouter-backup-%s-%s.tar.gz' % (ts, host)
        host ：re.sub(r'[^A-Za-z0-9_-]', '', hostname)[:24].lower()
    早先这里写的是 `(-[a-z0-9]+)?`，比命名规则窄两处 ——
    不认连字符（`-`）也不认下划线（`_`）。而 Debian 的默认主机名
    恰恰是 `debian-primaryrouter`（带连字符），于是：
        导出的包叫 drouter-backup-20261003-012754-debian-primaryrouter.tar.gz
        而 list / delete / inspect / restore 全部过不了自己的校验 →
        **导出的包在界面上根本看不到，也永远还原不了**，
        用户只会看到「备份已导出」然后找不到它。
    这个 bug 静态检查看不出来（t-107 的用例只用了无后缀的包名），
    是真机验收打一次真接口才暴露的。

    教训：**校验正则和生成规则必须同源，最好连成一处**。
    这里的 charset 与长度都照命名那行抄；改一边必须改另一边。
    """
    name = str(name or '').strip()
    # 长度上限照命名规则：'drouter-backup-' 14 + 8 + 1 + 6 + 1 + 24 + '.tar.gz' 7
    # 主机名段是**可选**的（老包 / 手工改名的包可能没有），但 charset 与长度
    # 必须和命名那行一致。
    if not name or len(name) > 64 or not re.match(
            r'^drouter-backup-\d{8}-\d{6}(?:-[a-z0-9_-]{1,24})?\.tar\.gz$',
            name):
        return None
    return name


def _bk_path_of(name):
    """返回备份包的绝对路径；不合法返回 None。

    这里是唯一的入口校验点：list / delete / pack / inspect / restore /
    prune 全部先过它。os.path.join(root, name) 之后再 commonpath 复核，
    双重保险（历史上 snapshot_pack 就栽过同类问题）。
    """
    n = _bk_safe_name(name)
    if not n:
        return None
    root = os.path.abspath(backup_dir())
    full = os.path.abspath(os.path.join(root, n))
    if os.path.commonpath([root, full]) != root:
        return None
    return full


def _bk_delete(p):
    full = _bk_path_of(p.get('name'))
    if not full:
        return fail('备份包名不合法')
    if not os.path.isfile(full):
        return fail('备份包不存在')
    try:
        os.unlink(full)
    except Exception as e:
        return fail('删除备份包失败：%s' % e)
    log('info', 'backup', 'BK_DELETED', '已删除备份包 %s' % os.path.basename(full))
    return ok({'name': os.path.basename(full)}, '备份包已删除')


def _bk_pack(p):
    """把备份包复制到下载目录。

    为什么不直接让 Web 层读 /opt/drouter/backups 下的文件：那里的包可能
    权限是 0600（含私钥时），Web 后端以 drouter 用户运行会读不到；
    而下载是一次性动作，复制一份并按需放宽权限更可控。
    """
    full = _bk_path_of(p.get('name'))
    if not full:
        return fail('备份包名不合法')
    if not os.path.isfile(full):
        return fail('备份包不存在')
    try:
        os.makedirs(BACKUP_DL, exist_ok=True)
    except Exception as e:
        return fail('创建下载目录失败：%s' % e)
    name = os.path.basename(full)
    for old in os.listdir(BACKUP_DL):
        if old.endswith('.tar.gz'):
            try:
                os.unlink(os.path.join(BACKUP_DL, old))
            except Exception:
                pass
    dst = os.path.join(BACKUP_DL, name)
    try:
        shutil.copy2(full, dst)
        os.chmod(dst, 0o644)
    except Exception as e:
        return fail('准备下载文件失败：%s' % e)
    return ok({'name': name, 'path': dst, 'size': os.path.getsize(dst)},
              '备份包已准备下载')


def _bk_open_manifest(p):
    """打开备份包并校验，返回 (tarfile, manifest)。调用方负责关闭。"""
    import io as _io
    full = _bk_path_of(p.get('name'))
    if not full:
        return None, None, fail('备份包名不合法')
    if not os.path.isfile(full):
        return None, None, fail('备份包不存在')
    try:
        tf = tarfile.open(full, 'r:gz')
    except Exception as e:
        return None, None, fail('打开备份包失败（文件可能已损坏）：%s' % e)
    try:
        member = tf.getmember(BACKUP_MANIFEST)
    except Exception:
        try:
            tf.close()
        except Exception:
            pass
        return None, None, fail('这不是 drouter 备份包：缺少 %s' % BACKUP_MANIFEST)
    if member.size > 8 * 1024 * 1024:
        tf.close()
        return None, None, fail('备份包清单异常巨大，已拒绝解析')
    try:
        raw = tf.extractfile(member).read()
        manifest = json.loads(raw.decode('utf-8'))
    except Exception as e:
        tf.close()
        return None, None, fail('备份包清单解析失败：%s' % e)
    if not isinstance(manifest, dict):
        tf.close()
        return None, None, fail('备份包清单格式不正确')
    fmt = manifest.get('format')
    if not isinstance(fmt, int) or fmt > BACKUP_FORMAT:
        tf.close()
        return None, None, fail(
            '备份包格式版本 %s 高于本机支持的 %d，请先升级 drouter'
            % (fmt, BACKUP_FORMAT))
    # 解压前先核总体积与文件数：tar 炸弹的解压端比压缩端危险得多
    total = 0
    count = 0
    for m in tf.getmembers():
        if not m.isfile():
            continue
        count += 1
        total += max(0, int(m.size or 0))
        if count > BK_MAX_FILES or total > BK_MAX_UNPACK:
            tf.close()
            return None, None, fail(
                '备份包解出后超过上限（%d 个文件 / %.1f MB），疑似异常包已拒绝'
                % (count, total / 1048576.0))
    return tf, manifest, None


def _bk_inspect(p):
    """查看备份包内容与完整性（不写任何文件）。"""
    tf, manifest, err = _bk_open_manifest(p)
    if err:
        return err
    try:
        members = {m.name: m for m in tf.getmembers() if m.isfile()}
        rows = []
        bad = []
        missing = []
        for it in manifest.get('files') or []:
            rel = str(it.get('path') or '').lstrip('/')
            if not rel:
                continue
            m = members.get(rel)
            row = {'path': '/' + rel, 'size': it.get('size', 0),
                   'group': it.get('group', ''),
                   'sensitive': bool(it.get('sensitive')),
                   # ⚠️ sha256 **必须**从manifest 条目搬进 row。
                   # 下面比对用的是 row.get('sha256')，早先这里没搬，
                   # 于是恒为 None ≠ 实际哈希 → **每一个文件都被标成
                   # 「已损坏」**，界面上 22 项全是红的，还原也被
                   # 「内容与清单不符」挡住 —— 一个刚导出的新包立刻报损坏。
                   # 教训和本轮另外两个 bug 同一个根：**数据要从来源
                   # 一路带到使用点，中间任何一次「重新构造字典」都会
                   # 悄悄丢字段**。所以比对要用 it.get('sha256')，
                   # 这里搬一份只是为了让 row 自洽（前端也读它）。
                   'sha256': it.get('sha256', ''),
                   'present': m is not None}
            if m is None:
                row['state'] = '缺失'
                missing.append('/' + rel)
            else:
                row['state'] = '待校验'
                rows.append((row, m))
        for row, m in rows:
            try:
                h = hashlib_sha256_member(tf, m)
            except Exception as e:
                row['state'] = '校验失败'
                row['why'] = str(e)
                bad.append(row['path'])
                continue
            if h != row.get('sha256'):
                row['state'] = '已损坏'
                bad.append(row['path'])
            else:
                row['state'] = '正常'
        return ok({
            'manifest': {k: v for k, v in manifest.items() if k != 'files'},
            'files': [r for r, _m in rows] + [
                {'path': x, 'state': '缺失', 'size': 0, 'group': '',
                 'sensitive': False, 'present': False} for x in missing],
            'ok_count': sum(1 for r, _m in rows if r['state'] == '正常'),
            'bad': bad,
            'missing': missing,
        }, '备份包内容已校验')
    finally:
        try:
            tf.close()
        except Exception:
            pass


def hashlib_sha256_member(tf, m):
    """计算 tar 内成员的 sha256（流式，不整块读进内存）。"""
    import hashlib
    h = hashlib.sha256()
    f = tf.extractfile(m)
    if f is None:
        raise IOError('无法读取成员内容')
    for chunk in iter(lambda: f.read(262144), b''):
        h.update(chunk)
    return h.hexdigest()


def _bk_restore(p):
    """从备份包还原配置。

    三道保险，缺一不可：
      1. 逐个成员校验目标路径命中 BK_ALLOW_PREFIX 且 realpath 不逃逸；
      2. 默认先校验 sha256（verify=True），不一致就整包拒绝；
      3. 每个文件写入前先备份现有版本到 .drouter-restore-bak，
         还原错了还能退回去。
    """
    p = p or {}
    verify = p.get('verify') is not False
    dry = p.get('dry_run') is True
    # 纵深防御：确认门Web 层已经有一道（没confirm 就强制 dry_run），
    # 这里再补一道。helper 是以 root 执行的，将来若有人写脚本或CLI
    # 直接调 restore，就会绕过 Web 层 —— 那就等于没有门。
    # 两处都要有，且逻辑一致：只有显式 confirm=true 才允许真写。
    if not dry and p.get('confirm') is not True:
        dry = True
    # 不还原这些：它们描述的是「这台机器现在是谁」而不是「配置是什么」
    skip_exact = {'/etc/hostname', '/etc/fstab'}
    tf, manifest, err = _bk_open_manifest(p)
    if err:
        return err
    plan = []
    applied = 0
    skipped = []
    errs = []
    try:
        members = {m.name: m for m in tf.getmembers() if m.isfile()}
        for it in manifest.get('files') or []:
            rel = str(it.get('path') or '').lstrip('/')
            if not rel:
                continue
            dst = '/' + rel
            if dst in skip_exact:
                skipped.append({'path': dst, 'why': '属于本机身份信息，不还原'})
                continue
            # 1) 白名单 + 真实路径校验
            norm = os.path.normpath(dst)
            if not norm.startswith(BK_ALLOW_PREFIX):
                errs.append('%s：不在允许还原的路径范围内' % dst)
                continue
            parent = os.path.dirname(norm)
            try:
                rp = os.path.realpath(parent)
            except Exception:
                errs.append('%s：无法校验目标目录' % dst)
                continue
            if not (rp + '/').startswith(BK_ALLOW_PREFIX) and rp not in (
                    '/etc', '/opt/drouter', '/var/lib/drouter'):
                errs.append('%s：真实路径逃逸出允许范围' % dst)
                continue
            m = members.get(rel)
            if m is None:
                errs.append('%s：备份包内缺少该文件' % dst)
                continue
            # 2) 校验
            if verify:
                try:
                    h = hashlib_sha256_member(tf, m)
                except Exception as e:
                    errs.append('%s：校验失败（%s）' % (dst, e))
                    continue
                if h != it.get('sha256'):
                    errs.append('%s：内容与清单不符，备份包可能已损坏' % dst)
                    continue
            row = {'path': dst, 'size': it.get('size', 0),
                   'exists': os.path.exists(norm),
                   'sensitive': bool(it.get('sensitive'))}
            plan.append(row)
            if dry:
                continue
            # 3) 写入（先留旧版本）
            try:
                os.makedirs(parent, exist_ok=True)
                if os.path.exists(norm):
                    bak = norm + '.drouter-restore-bak'
                    try:
                        shutil.copy2(norm, bak)
                    except Exception:
                        pass
                f = tf.extractfile(m)
                data = f.read()
                mode = int(it.get('mode') or '0o644', 8) & 0o777
                # 私钥类文件强制 0600：备份包可能是从别处传来的，
                # 原来的 mode 未必可靠
                if it.get('sensitive') or norm.endswith(
                        ('.key', 'wg0.conf', 'wg1.conf')):
                    mode = 0o600
                with open(norm, 'wb') as out:
                    out.write(data)
                os.chmod(norm, mode)
                applied += 1
            except Exception as e:
                errs.append('%s：写入失败（%s）' % (dst, e))
    finally:
        try:
            tf.close()
        except Exception:
            pass
    # 数据库要单独处理：它是 SQLite，直接覆盖写进去可能破坏正在打开的连接。
    # 还原数据库一律走「先落盘再改名」并且要求服务重载才生效。
    if not dry and not errs and DB_PATH in [
            '/' + str(i.get('path', '')).lstrip('/')
            for i in manifest.get('files') or []]:
        if manifest.get('include_sensitive'):
            log('warn', 'backup', 'BK_RESTORE_ADMIN',
                '本次还原包含管理员账号与密码哈希，若换过 Web 密码请用备份包里的那份')
    if dry:
        return ok({'dry_run': True, 'plan': plan, 'skipped': skipped,
                   'errors': errs, 'file_count': len(plan)},
                  '预检完成：将写入 %d 个文件' % len(plan))
    if errs:
        log('error', 'backup', 'BK_RESTORE_PARTIAL',
            '还原过程中有 %d 项失败，成功 %d 项' % (len(errs), applied), errs)
        return fail('还原未完成：%d 项失败（已成功 %d 项）' % (len(errs), applied),
                    'PARTIAL', {'applied': applied, 'errors': errs,
                                'skipped': skipped})
    log('info', 'backup', 'BK_RESTORED',
        '已从备份包还原 %d 个文件' % applied,
        {'name': os.path.basename(_bk_path_of(p.get('name')) or '')})
    return ok({'applied': applied, 'plan': plan, 'skipped': skipped,
               'errors': []},
              '已还原 %d 个文件，请到相关页面确认后点一次「保存并应用」' % applied)


def _bk_prune(p):
    """按份数与天数清理备份包。"""
    p = p or {}
    try:
        keep_count = int(p.get('keep_count') or 0)
    except Exception:
        keep_count = 0
    try:
        keep_days = int(p.get('keep_days') or 0)
    except Exception:
        keep_days = 0
    entries = _bk_list_entries()
    removed = []
    now = datetime.now()
    # 按天：从新到旧，够新的留下，够老的一律删
    if keep_days > 0:
        for e in entries:
            try:
                born = datetime.fromisoformat(e['mtime'])
            except Exception:
                continue
            if (now - born).days >= keep_days:
                full = _bk_path_of(e['name'])
                if full and os.path.isfile(full):
                    try:
                        os.unlink(full)
                        removed.append(e['name'])
                    except Exception:
                        pass
    # 按份数：删完过期的还超量就继续从旧往新删
    rest = [e for e in entries if e['name'] not in removed]
    if keep_count > 0 and len(rest) > keep_count:
        for e in rest[:len(rest) - keep_count]:
            full = _bk_path_of(e['name'])
            if full and os.path.isfile(full):
                try:
                    os.unlink(full)
                    removed.append(e['name'])
                except Exception:
                    pass
    if removed:
        log('info', 'backup', 'BK_PRUNED',
            '已清理 %d 个备份包（保留 %d）' % (len(removed), len(entries) - len(removed)))
    return ok({'removed': removed, 'kept': len(entries) - len(removed)},
              '已清理 %d 个备份包' % len(removed))


def _bk_conf(p):
    """保存备份设置。"""
    p = p or {}
    conf = _bk_norm({**_bk_load(), **(p.get('conf') or {})})
    _bk_save(conf)
    timer_applied = None
    if 'auto_enabled' in p or 'auto_hour' in p:
        timer_applied = _write_backup_timer(conf)
    log('info', 'backup', 'BK_CONF_SAVED',
        '已保存备份设置（自动备份%s）' % ('开启' if conf['auto_enabled'] else '关闭'))
    return ok({'conf': conf, 'timer_applied': timer_applied},
              '备份设置已保存')


# ------------------------------------------------------------- 自动备份 timer

def _write_backup_timer(conf):
    """生成自动备份的 service + timer 单元，并按开关启停。

    每天凌晨跑一次：错开整点（默认 3 点），避免和磁盘清理、自动快照
    挤在同一分钟 —— 4GB 机器上三个 oneshot 同时做 tar 会明显卡顿。
    """
    hour = max(0, min(int((conf or {}).get('auto_hour') or 3), 23))
    svc = """[Unit]
Description=drouter 配置自动备份
After=network.target
Documentation=file:///opt/drouter/backend/drouter-backupd.py

[Service]
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Type=oneshot
User=root
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=6
ExecStart=/usr/bin/python3 /opt/drouter/backend/drouter-backupd.py

[Install]
WantedBy=multi-user.target
"""
    tmr = """[Unit]
Description=drouter 配置自动备份定时器（每天 %02d:17）

[Timer]
OnCalendar=*-*-* %02d:17:00
AccuracySec=2min
Persistent=true
Unit=drouter-backupd.service

[Install]
WantedBy=timers.target
""" % (hour, hour)
    try:
        os.makedirs('/etc/systemd/system', exist_ok=True)
        with open('/etc/systemd/system/drouter-backupd.service', 'w',
                  encoding='utf-8') as f:
            f.write(svc)
        with open('/etc/systemd/system/drouter-backupd.timer', 'w',
                  encoding='utf-8') as f:
            f.write(tmr)
        sh(['systemctl', 'daemon-reload'], timeout=20)
    except Exception as e:
        log('error', 'backup', 'BK_TIMER_FAIL', '写入自动备份定时器失败：%s' % e)
        return False
    if not os.path.isdir('/run/systemd/system'):
        # 容器形态没有 systemd：单元已写盘，但启停只能靠上层自己拉起
        log('warn', 'backup', 'BK_TIMER_NOSYSTEMD',
            '当前环境没有 systemd，自动备份定时器已写入但未启用')
        return False
    if conf.get('auto_enabled'):
        rc, out, err = sh(['systemctl', 'enable', '--now', 'drouter-backupd.timer'],
                          timeout=30)
        if rc != 0:
            log('warn', 'backup', 'BK_TIMER_ENABLE_FAIL',
                '自动备份定时器启用失败：%s' % (err or out))
            return False
        return True
    sh(['systemctl', 'disable', '--now', 'drouter-backupd.timer'], timeout=30)
    return False


def read_backupd(p=None):
    """读取自动备份定时器状态（供界面展示）。"""
    st = _svc_states(('drouter-backupd.service', 'drouter-backupd.timer'))
    rc, nxt, _e = sh(['systemctl', 'list-timers', 'drouter-backupd.timer',
                      '--no-pager', '--no-legend'], timeout=12)
    last = {}
    try:
        with open(os.path.join(LOGDIR, 'backupd.jsonl'), encoding='utf-8',
                  errors='replace') as f:
            lines = f.read().splitlines()[-40:]
        for ln in reversed(lines):
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if (r.get('code') or '').startswith('BK_'):
                last = {'ts': r.get('ts', ''), 'msg_cn': r.get('msg_cn', ''),
                        'code': r.get('code', '')}
                break
    except Exception:
        pass
    return ok({'units': st,
               'next': (nxt.splitlines()[0].strip() if nxt else ''),
               'last': last, 'conf': _bk_norm(_bk_load())})


# ============================================== 告警与通知中心（1.0.7）
#
# ── 它解决什么 ──────────────────────────────────────────────────────────────
# 在此之前这套系统是「坏了你自己来看」：日志、快照、流日志全都齐全，但没有任何
# 东西会在你睡觉、出门、开会的时候告诉你「家里断网了」。路由器最怕的不是坏，
# 是坏了没人知道 —— 断 4 小时和断 4 分钟，对在家办公的人差别巨大。
#
# ── 设计上的三个关键决定 ────────────────────────────────────────────────────
# 1) **判定与推送分离**。判定在 helper（跑在 root、能读 /proc 与网卡），
#    推送也在 helper（要发网络请求）。但**规则配置与历史**存在 settings 里，
#    界面读得到、改得动。drouter-alertd.py 只做「调一次 helper」的薄壳。
# 2) **状态必须外置**。「WAN 现在是通的还是断的」这个状态存在
#    /opt/drouter/data/alert-state.json。为什么不用内存：告警判定跨进程
#    （每次 timer 都是新进程），用内存的话每轮都认为「刚刚才断」，冷却期
#    形同虚设，一断就是每分钟一条消息轰炸用户。
# 3) **冷却 + 去重双保险**。冷却解决「同一条故障反复触发」，
#    去重解决「同一次故障换句话说」。两者都必须在，否则半夜一条抖动
#    能把手机推 60 条。

ALERT_STATE = '/opt/drouter/data/alert-state.json'
ALERT_HIST = '/opt/drouter/data/alert-history.jsonl'
# 单条规则的默认冷却（分钟）。超过这个时间才允许同一条规则再推一次。
ALERT_COOLDOWN_DEFAULT = 30
# 历史最多留多少条
ALERT_HIST_MAX = 500

# 规则定义。level 是严重程度，缺省继承通道设置。
# 每条规则自带一段「为什么这样判」—— 用户看不懂阈值就永远不敢调。
ALERT_RULES = [
    {'k': 'wan_down', 'n': '外网连接中断', 'lv': 'critical', 'unit': '秒',
     'default': 90, 'min': 30, 'max': 3600,
     'why': '连续这么久拿不到网关回应才判定为掉线。太短会把一次网络抖动'
            '（比如邻居的 Wi-Fi 抢占）误报成断网，消息一多就没人看了。'},
    {'k': 'disk_full', 'n': '磁盘占用过高', 'lv': 'warn', 'unit': '%',
     'default': 85, 'min': 50, 'max': 99,
     'why': '日志、快照、备份都在往根分区写。到 90% 以上时 apt 升级和'
            '数据库写入都可能失败，所以默认在 85% 就提醒。'},
    {'k': 'temp_high', 'n': 'CPU 温度过高', 'lv': 'warn', 'unit': '℃',
     'default': 80, 'min': 50, 'max': 110,
     'why': 'x86 小主机散热一般，超过 80℃就该检查风扇和机箱积灰。'
            '读不到温度传感器时本条自动跳过，不会误报。'},
    {'k': 'mem_high', 'n': '内存占用过高', 'lv': 'warn', 'unit': '%',
     'default': 92, 'min': 60, 'max': 99,
     'why': '这台机器只有 4GB，可用内存耗尽时 dnsmasq 与 Web 面板会先挂，'
            '用户连界面都打不开，也就看不到告警。'},
    {'k': 'load_high', 'n': '负载持续偏高', 'lv': 'info', 'unit': '',
     'default': 3.0, 'min': 1.0, 'max': 32.0, 'step': 0.1,
     'why': '2 核机器上负载长期超过 3 说明有任务在忙。短时尖峰是正常的，'
            '所以这条只做 info 级提醒，不当故障。'},
    {'k': 'loss_high', 'n': '到网关丢包严重', 'lv': 'warn', 'unit': '%',
     'default': 30, 'min': 5, 'max': 100,
     'why': 'ping 丢包高通常意味着网线、网口协商或 Wi-Fi 信号问题，'
            '表现为「网页时好时坏」，比完全断网更难排查。'},
    {'k': 'backup_fail', 'n': '自动备份连续失败', 'lv': 'warn', 'unit': '次',
     'default': 2, 'min': 1, 'max': 20,
     'why': '备份失败一次可能是磁盘正好满了，连续失败说明设置本身有问题。'},
    {'k': 'snapshot_fail', 'n': '自动快照连续失败', 'lv': 'info', 'unit': '次',
     'default': 3, 'min': 1, 'max': 20,
     'why': '快照是你「改坏了能退回去」的底座。它悄悄失败两周，'
            '真出事那天你才发现退不回去。'},
]

# 通知通道模板。type 决定用哪个发送器。
ALERT_CHANNELS = [
    {'k': 'bark', 'n': 'Bark（iOS / Android 推送）',
     'd': '填 Bark 服务器地址与设备 Key，点标题即跳转。支持自建服务器。'},
    {'k': 'smtp', 'n': '邮件（SMTP）',
     'd': '适合发到工作邮箱。密码请填专用授权码，不要用主账号密码。'},
    {'k': 'webhook', 'n': '群机器人 Webhook（钉钉 / 企业微信 / 飞书）',
     'd': '直接粘机器人地址。按平台自动用该平台的报文格式。'},
]


def _al_state_load():
    try:
        with open(ALERT_STATE, encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {'firing': {}, 'sent': {}, 'fails': {}}


def _al_state_save(d):
    try:
        os.makedirs(os.path.dirname(ALERT_STATE), exist_ok=True)
        tmp = ALERT_STATE + '.tmp.%d' % os.getpid()
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, ALERT_STATE)
    except Exception as e:
        log('warn', 'alert', 'AL_STATE_SAVE_FAIL', '告警状态写盘失败：%s' % e)


def _al_conf(raw=None):
    """归一化告警设置。

    raw 传 None 时从数据库读，传 dict 时对给定值归一化 —— 保存路径必须走后者，
    否则用户填的越界值会被静默钳到上下限却没有任何提示。
    """
    c = raw if isinstance(raw, dict) else (_load_setting('alert') or {})
    if not isinstance(c, dict):
        c = {}
    rules = {}
    raw = c.get('rules')
    if isinstance(raw, dict):
        rules = raw
    out_rules = {}
    for r in ALERT_RULES:
        k = r['k']
        v = rules.get(k, r['default'])
        try:
            v = float(v)
        except Exception:
            v = float(r['default'])
        if r.get('step'):
            v = round(v, 1)
        else:
            v = int(v)
        out_rules[k] = max(r['min'], min(r['max'], v))
    chans = c.get('channels')
    if not isinstance(chans, list):
        chans = []
    out_ch = []
    for ch in chans:
        if not isinstance(ch, dict):
            continue
        t = str(ch.get('type') or '').strip()
        if t not in ('bark', 'smtp', 'webhook'):
            continue
        out_ch.append({
            'type': t,
            'name': str(ch.get('name') or '')[:60],
            'enabled': ch.get('enabled') is True,
            'url': str(ch.get('url') or '')[:500],
            'title': str(ch.get('title') or '')[:80],
            'host': str(ch.get('host') or '')[:200],
            'port': int(ch.get('port') or 0) or None,
            'user': str(ch.get('user') or '')[:100],
            'pass': str(ch.get('pass') or '')[:200],
            'from': str(ch.get('from') or '')[:200],
            'to': str(ch.get('to') or '')[:300],
            'tls': ch.get('tls') is not False,
        })
    return {
        'enabled': c.get('enabled') is True,
        'interval_sec': max(60, min(int(c.get('interval_sec') or 300), 3600)),
        'cooldown_min': max(1, min(int(c.get('cooldown_min') or ALERT_COOLDOWN_DEFAULT), 1440)),
        'quiet_from': str(c.get('quiet_from') or '')[:5],
        'quiet_to': str(c.get('quiet_to') or '')[:5],
        'rules': out_rules,
        'channels': out_ch,
    }


def _al_hist_append(rec):
    try:
        os.makedirs(os.path.dirname(ALERT_HIST), exist_ok=True)
        with open(ALERT_HIST, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        # 顺手裁剪：不能让它无限长，否则半年后就是第二个「日志把盘吃满」
        try:
            lines = open(ALERT_HIST, encoding='utf-8', errors='replace') \
                .read().splitlines()
            if len(lines) > ALERT_HIST_MAX:
                with open(ALERT_HIST, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(lines[-ALERT_HIST_MAX:]) + '\n')
        except Exception:
            pass
    except Exception:
        pass


def _al_hist_read(limit=100):
    out = []
    try:
        with open(ALERT_HIST, encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()[-int(limit or 100):]
        for ln in reversed(lines):
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
    except Exception:
        pass
    return out


# --------------------------------------------------------- 告警判定（只读）

def _al_probe_wan():
    """判外网是否可达。

    刻意不 ping 公网 IP（很多家用宽带禁 ICMP，会一直报「断了」），
    而是 ping 默认网关 —— 网关不通就是真断了，网关通就说明本机链路正常。
    """
    rc, o, _e = sh(['sh', '-c', "ip -4 route show default 2>/dev/null "
                    "| awk '{print $3; exit}'"], timeout=6)
    gw = (o or '').strip()
    if not gw:
        # 没有默认路由：没拨号或路由被清空，也算外网不可用
        return {'up': False, 'why': '本机没有默认路由（未拨号或路由表为空）', 'gw': ''}
    rc, o, e = sh(['ping', '-n', '-c', '2', '-W', '2', gw], timeout=8)
    loss = None
    m = re.search(r'(\d+)% packet loss', o or '')
    if m:
        loss = int(m.group(1))
    up = (rc == 0) and (loss is not None and loss < 100)
    why = '' if up else '连续无法访问网关 %s' % gw
    return {'up': up, 'why': why, 'gw': gw, 'loss_pct': loss}


def _al_probe_disk():
    """找最满的那个挂载点。

    ⚠️ _read_disks 返回的 pct 是**字符串** '85%'、可用空间叫 avail（不是 free）。
    直接拿 d['pct'] 和 85 比较会 TypeError，被 except 吞掉后表现为
    「磁盘告警从来没触发过」—— 这种静默失效最难发现，所以这里显式转数值。
    """
    worst = None
    for d in _read_disks():
        raw = d.get('pct')
        try:
            pct = int(str(raw).replace('%', '').strip())
        except Exception:
            continue
        if worst is None or pct > worst['pct']:
            worst = {'mount': d.get('mount', ''), 'pct': pct,
                     'avail': d.get('avail', ''), 'size': d.get('size', '')}
    return worst or {}


def _al_probe_mem():
    try:
        with open('/proc/meminfo', encoding='utf-8') as f:
            info = {}
            for ln in f:
                k, _, v = ln.partition(':')
                info[k.strip()] = v.strip()
        mt = int(info.get('MemTotal', '0 kB').split()[0])
        ma = int(info.get('MemAvailable', '0 kB').split()[0])
        if mt <= 0:
            return {}
        # 用 MemAvailable 而不是 MemFree：Linux 里空闲内存低是正常的
        # （拿去做缓存），MemAvailable 才是「还能不能再分配」的量
        return {'pct': int((mt - ma) * 100 / mt),
                'avail_mb': ma // 1024, 'total_mb': mt // 1024}
    except Exception:
        return {}


def _al_probe_load():
    try:
        with open('/proc/loadavg', encoding='utf-8') as f:
            p = f.read().split()
        return {'load1': float(p[0]), 'load5': float(p[1]), 'load15': float(p[2])}
    except Exception:
        return {}


def _al_probe_fail_streak(marker):
    """数某个守护连续失败了几次（读它自己的结构化日志尾部）。"""
    path = os.path.join(LOGDIR, '%s.jsonl' % marker)
    streak = 0
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()[-60:]
    except Exception:
        return 0
    for ln in reversed(lines):
        try:
            r = json.loads(ln)
        except Exception:
            continue
        code = str(r.get('code') or '')
        if code.endswith('_FAIL') or 'FAIL' in code or 'ERROR' in str(r.get('level') or ''):
            streak += 1
        elif code:
            # 遇到一条成功的记录就说明已经恢复，失败链到此为止
            break
    return streak


def _al_now_minute():
    return datetime.now().hour * 60 + datetime.now().minute


def _al_in_quiet(conf):
    """是否落在免打扰时段。跨零点（23:00–07:00）要正确处理。"""
    qf = str(conf.get('quiet_from') or '').strip()
    qt = str(conf.get('quiet_to') or '').strip()
    if not re.match(r'^\d{2}:\d{2}$', qf) or not re.match(r'^\d{2}:\d{2}$', qt):
        return False
    a = int(qf[:2]) * 60 + int(qf[3:])
    b = int(qt[:2]) * 60 + int(qt[3:])
    if a == b:
        return False
    now = _al_now_minute()
    if a < b:
        return a <= now < b
    return now >= a or now < b        # 跨零点


def act_alert(p):
    """告警与通知中心：status / test / run / conf / history / clear。

    run 是「判定一次并按需推送」，由 drouter-alertd.py 定时调用；
    界面手动点「立即检测」走的是同一个 op —— 定时任务和手动检查
    必须是同一条代码路径，否则手动测通了不代表定时也能通。
    """
    p = p or {}
    op = str(p.get('op') or 'status')
    if op == 'status':
        return _al_status()
    if op == 'run':
        return _al_run(p)
    if op == 'test':
        return _al_test(p)
    if op == 'conf':
        return _al_conf_op(p)
    if op == 'history':
        return ok({'items': _al_hist_read(p.get('limit'))}, '已读取告警历史')
    if op == 'clear':
        return _al_clear()
    if op == 'services':
        return _al_services()
    return fail('未知的告警操作：%s' % op)


def _al_status():
    conf = _al_conf()
    st = _al_state_load()
    units = _svc_states(('drouter-alertd.service', 'drouter-alertd.timer'))
    rc, nxt, _e = sh(['systemctl', 'list-timers', 'drouter-alertd.timer',
                      '--no-pager', '--no-legend'], timeout=12)
    last = {}
    try:
        with open(os.path.join(LOGDIR, 'alertd.jsonl'), encoding='utf-8',
                  errors='replace') as f:
            lines = f.read().splitlines()[-40:]
        for ln in reversed(lines):
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if (r.get('code') or '').startswith('AL_'):
                last = {'ts': r.get('ts', ''), 'msg_cn': r.get('msg_cn', ''),
                        'code': r.get('code', ''), 'level': r.get('level', '')}
                break
    except Exception:
        pass
    rules = []
    for r in ALERT_RULES:
        rules.append({'key': r['k'], 'name': r['n'], 'lv': r['lv'],
                      'unit': r['unit'], 'value': conf['rules'].get(r['k']),
                      'default': r['default'], 'min': r['min'], 'max': r['max'],
                      'step': r.get('step'),
                      'why': r['why'],
                      'firing': bool((st.get('firing') or {}).get(r['k']))})
    return ok({
        'conf': conf, 'rules': rules,
        'channel_types': ALERT_CHANNELS,
        'units': units,
        'next': (nxt.splitlines()[0].strip() if nxt else ''),
        'last': last,
        'state': st,
        'quiet_now': _al_in_quiet(conf),
        'enabled_ch': len([c for c in conf['channels'] if c['enabled']]),
        'history': _al_hist_read(30),
    }, '已读取告警设置')


def _al_services():
    """把各守护的健康状况列出来（供用户判断告警是否真的在跑）。"""
    return ok({
        'units': _svc_states(('drouter-alertd.timer', 'drouter-logd.timer',
                              'drouter-snapshot.timer', 'drouter-backupd.timer')),
    })


def _al_collect(conf):
    """跑一遍全部规则，返回命中列表 + 全部探测值（给界面展示用）。"""
    probes = {'wan': _al_probe_wan(), 'disk': _al_probe_disk(),
              'mem': _al_probe_mem(), 'load': _al_probe_load()}
    lat = _route_latency()
    probes['latency'] = {'ms': lat.get('ms'), 'loss_pct': lat.get('loss_pct'),
                         'target': lat.get('target')}
    st = _al_state_load()
    firing = st.get('firing') or {}
    hits = []
    thr = conf['rules']

    def _hit(k, lv, msg, val):
        hits.append({'key': k, 'lv': lv, 'msg_cn': msg, 'value': val,
                     'new': not firing.get(k)})

    # WAN 掉线：必须连续多轮都不通才报。state 里的 wan_bad_since
    # 记录「从哪一刻开始不通」，这才是「连续 N 秒」的实现方式 ——
    # 靠单轮判断会把「一次抖动」当成「掉线 90 秒」。
    w = probes['wan']
    if w.get('up'):
        st['wan_bad_since'] = 0
    else:
        since = int(st.get('wan_bad_since') or 0)
        if not since:
            since = int(time.time())
            st['wan_bad_since'] = since
        held = int(time.time()) - since
        if held >= int(thr.get('wan_down', 90)):
            _hit('wan_down', 'critical',
                 '外网已中断 %d 秒：%s' % (held, w.get('why') or '网关不可达'),
                 held)
    # 磁盘
    d = probes['disk']
    if d.get('pct') is not None and d['pct'] >= thr.get('disk_full', 85):
        _hit('disk_full', 'warn',
             '磁盘 %s 已用 %d%%（共 %s，剩余 %s）'
             % (d.get('mount') or '/', d['pct'], d.get('size') or '未知',
                d.get('avail') or '未知'),
             d['pct'])
    # 温度。_read_temp 返回的是 (温度, 来源) 元组，不是 dict ——
    # 读成 dict 会让 tp.get('c') 恒为 None，这条规则静默永不触发。
    try:
        t_c, t_src = _read_temp()
    except Exception:
        t_c, t_src = None, ''
    if t_c is not None:
        probes['temp'] = {'c': t_c, 'src': t_src or ''}
        if t_c >= thr.get('temp_high', 80):
            _hit('temp_high', 'warn', 'CPU 温度 %.1f℃ 偏高（来源 %s）'
                 % (t_c, t_src or '未知'), t_c)
    # 内存
    m = probes['mem']
    if m.get('pct') is not None and m['pct'] >= thr.get('mem_high', 92):
        _hit('mem_high', 'warn',
             '内存已用 %d%%（可用 %d MB / 共 %d MB）'
             % (m['pct'], m.get('avail_mb', 0), m.get('total_mb', 0)), m['pct'])
    # 负载
    ld = probes['load']
    if ld.get('load1') is not None and ld['load1'] >= thr.get('load_high', 3.0):
        _hit('load_high', 'info', '系统负载 %.2f（1 分钟）持续偏高' % ld['load1'],
             ld['load1'])
    # 丢包
    lp = probes['latency']
    if lp.get('loss_pct') is not None and lp['loss_pct'] >= thr.get('loss_high', 30):
        _hit('loss_high', 'warn',
             '到 %s 丢包 %d%%' % (lp.get('target') or '网关', lp['loss_pct']),
             lp['loss_pct'])
    # 备份 / 快照连续失败
    bf = _al_probe_fail_streak('backupd')
    probes['backup_fail'] = bf
    if bf >= thr.get('backup_fail', 2):
        _hit('backup_fail', 'warn', '自动备份已连续失败 %d 次' % bf, bf)
    sf = _al_probe_fail_streak('snapshotd')
    probes['snapshot_fail'] = sf
    if sf >= thr.get('snapshot_fail', 3):
        _hit('snapshot_fail', 'info', '自动快照已连续失败 %d 次' % sf, sf)
    # 把 firing 表更新成本轮的命中项（没命中的都算恢复）
    new_firing = {h['key']: True for h in hits}
    st['firing'] = new_firing
    st['probes'] = probes
    st['ts'] = int(time.time())
    _al_state_save(st)
    return hits, probes


def _al_run(p):
    """判定 + 按冷却推送。"""
    p = p or {}
    conf = _al_conf()
    forced = p.get('force') is True
    if not conf.get('enabled') and not forced:
        return ok({'fired': 0, 'hits': [], 'probes': _al_state_load().get('probes') or {},
                   'enabled': False}, '告警未启用，本次只做探测不做推送')
    st = _al_state_load()
    hits, probes = _al_collect(conf)
    sent = st.get('sent') or {}
    now = int(time.time())
    cooldown = int(conf.get('cooldown_min') or ALERT_COOLDOWN_DEFAULT) * 60
    quiet = _al_in_quiet(conf) and not forced
    fired = []
    skipped = []
    for h in hits:
        last = int(sent.get(h['key']) or 0)
        if not forced and now - last < cooldown:
            skipped.append({'key': h['key'], 'msg_cn': h['msg_cn'],
                            'why': '冷却中（距上次 %d 分钟，冷却期 %d 分钟）'
                                   % ((now - last) // 60, cooldown // 60)})
            continue
        if quiet and h['lv'] != 'critical':
            # 免打扰只压 warning / info。critical 永远发 ——
            # 「半夜别吵我」不包括「家里断网了」
            skipped.append({'key': h['key'], 'msg_cn': h['msg_cn'],
                            'why': '在免打扰时段内（非紧急）'})
            continue
        r = _al_dispatch(conf, h)
        if r.get('ok'):
            sent[h['key']] = now
            fired.append(h['key'])
        else:
            skipped.append({'key': h['key'], 'msg_cn': h['msg_cn'],
                            'why': '推送失败：%s' % (r.get('msg_cn') or '未知错误')})
    st['sent'] = sent
    _al_state_save(st)
    if fired:
        log('warn', 'alert', 'AL_FIRED', '已推送 %d 条告警：%s'
            % (len(fired), '、'.join(fired)), {'keys': fired})
    return ok({'fired': len(fired), 'fired_keys': fired, 'hits': hits,
               'skipped': skipped, 'probes': probes, 'enabled': True,
               'quiet': quiet}, '检测完成：%d 条告警已推送，%d 条被跳过'
                              % (len(fired), len(skipped)))


def _al_dispatch(conf, h):
    """把一条告警发到所有启用的通道。任一通道成功即算送达。"""
    chans = [c for c in conf.get('channels') or [] if c.get('enabled')]
    if not chans:
        return fail('没有启用任何通知通道')
    ok_any = False
    errs = []
    for c in chans:
        try:
            if c['type'] == 'bark':
                r = _al_send_bark(c, h)
            elif c['type'] == 'smtp':
                r = _al_send_smtp(c, h)
            else:
                r = _al_send_webhook(c, h)
        except Exception as e:
            r = fail(str(e))
        if r.get('ok'):
            ok_any = True
        else:
            errs.append('%s：%s' % (c.get('name') or c['type'],
                                    r.get('msg_cn') or '未知错误'))
    _al_hist_append({
        'ts': datetime.now().isoformat(timespec='seconds'),
        'key': h['key'], 'lv': h['lv'], 'msg_cn': h['msg_cn'],
        'value': h.get('value'),
        'ok': ok_any, 'channels': len(chans),
        'errs': errs,
    })
    if ok_any:
        return ok({'delivered': True}, '已送达 %d 个通道' % len(chans))
    return fail('全部通道推送失败：%s' % '；'.join(errs))


def _al_http(url, method='GET', body=None, headers=None, timeout=12):
    """最小 HTTP 客户端。不用 urllib.request 是因为它对非 2xx 直接抛异常，
    而推送接口（尤其 webhook）经常用 200 + 业务错误码表达失败，
    我们需要读到响应体才能给出可读的错误提示。"""
    import http.client
    from urllib.parse import urlparse
    pu = urlparse(url)
    if pu.scheme not in ('http', 'https'):
        return fail('只支持 http / https 地址，当前是 %s' % pu.scheme)
    if not pu.hostname:
        return fail('地址格式不正确')
    conn = None
    try:
        if pu.scheme == 'https':
            import ssl
            ctx = ssl.create_default_context()
            # 推送目标是用户自己填的地址，自签证书在局域网里很常见
            # （Bark 自建、群机器人内网反代）。校验失败不该让告警发不出去。
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection(pu.hostname, pu.port or 443,
                                               timeout=timeout, context=ctx)
        else:
            conn = http.client.HTTPConnection(pu.hostname, pu.port or 80,
                                              timeout=timeout)
        hd = dict(headers or {})
        path = pu.path or '/'
        if pu.query:
            path += '?' + pu.query
        conn.request(method, path, body=body, headers=hd)
        resp = conn.getresponse()
        data = resp.read(65536)
        txt = data.decode('utf-8', 'replace')
        if 200 <= resp.status < 300:
            return ok({'status': resp.status, 'body': txt[:400]}, '推送成功')
        return fail('对方返回 HTTP %d：%s' % (resp.status, txt[:200]))
    except Exception as e:
        return fail('推送请求失败：%s' % e)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _al_send_bark(c, h):
    """Bark 推送。

    允许两种填法：完整地址（含设备 key），或只填 key。
    只填 key 时用官方 api.day.app；填了完整地址就用它（支持自建服务器）。
    """
    url = (c.get('url') or '').strip()
    if not re.match(r'^https?://', url):
        key = url
        if not key or '/' in key:
            return fail('请填写完整的 Bark 推送地址，或直接填设备 Key')
        url = 'https://api.day.app/%s' % key
    body = json.dumps({
        'title': c.get('title') or '路由器告警',
        'body': '[%s] %s' % ({'critical': '严重', 'warn': '警告',
                             'info': '提示'}.get(h['lv'], '通知'), h['msg_cn']),
        'group': 'drouter', 'level': h['lv'],
    }, ensure_ascii=False).encode('utf-8')
    r = _al_http(url, 'POST', body,
                 {'Content-Type': 'application/json; charset=utf-8'})
    if r.get('ok'):
        try:
            b = json.loads((r.get('data') or {}).get('body') or '{}')
            if b.get('code') not in (200, None):
                return fail('Bark 拒绝：%s' % (b.get('message') or '未知原因'))
        except Exception:
            pass
    return r


def _al_send_webhook(c, h):
    url = c.get('url') or ''
    if not re.match(r'^https?://', url):
        return fail('Webhook 地址必须以 http:// 或 https:// 开头')
    text = '【路由器%s】%s' % ({'critical': '严重告警', 'warn': '警告',
                              'info': '提示'}.get(h['lv'], '通知'), h['msg_cn'])
    # 三家平台的报文格式完全不同，按 URL 里的关键字猜
    if 'qyapi.weixin.qq.com' in url:
        body = json.dumps({'msgtype': 'text', 'text': {'content': text}},
                          ensure_ascii=False).encode('utf-8')
    elif 'oapi.dingtalk.com' in url:
        body = json.dumps({'msgtype': 'text',
                           'text': {'content': text}}, ensure_ascii=False).encode('utf-8')
    elif 'open.feishu.cn' in url:
        body = json.dumps({'msg_type': 'text',
                           'content': {'text': text}}, ensure_ascii=False).encode('utf-8')
    else:
        body = json.dumps({'title': c.get('title') or '路由器告警',
                           'text': text, 'level': h['lv'],
                           'msg_cn': h['msg_cn']}, ensure_ascii=False).encode('utf-8')
    r = _al_http(url, 'POST', body,
                 {'Content-Type': 'application/json; charset=utf-8'})
    if r.get('ok'):
        txt = (r.get('data') or {}).get('body') or ''
        # 钉钉 / 企业微信都用 HTTP 200 + errcode 表达业务失败，
        # 只看 HTTP 状态码会把「被拒绝」当成「发送成功」
        m = re.search(r'"errcode"\s*:\s*(\d+)', txt)
        if m and m.group(1) != '0':
            return fail('对方拒绝：%s' % txt[:200])
    return r


def _al_send_smtp(c, h):
    host = c.get('host') or ''
    if not host:
        return fail('未填写 SMTP 服务器地址')
    to = c.get('to') or ''
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', to):
        return fail('收件人地址格式不正确')
    sender = c.get('from') or c.get('user') or 'drouter@localhost'
    port = c.get('port') or (465 if c.get('tls') is not False else 25)
    msg = ('From: %s\r\nTo: %s\r\nSubject: =?UTF-8?B?%s?=\r\n'
           'MIME-Version: 1.0\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n'
           % (sender, to,
              base64.b64encode(('[路由器%s] %s' % (
                  {'critical': '严重告警', 'warn': '警告',
                   'info': '提示'}.get(h['lv'], '通知'), h['msg_cn'])).encode('utf-8')
              ).decode('ascii')))
    msg += '%s\n\n时间：%s\n主机：%s\n' % (h['msg_cn'],
                                        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                        _bk_hostname())
    try:
        import smtplib
        import ssl as _ssl
        if int(port) == 465:
            srv = smtplib.SMTP_SSL(host, int(port), timeout=15,
                                   context=_ssl.create_default_context())
        else:
            srv = smtplib.SMTP(host, int(port), timeout=15)
            if c.get('tls') is not False and int(port) in (587, 2525):
                try:
                    srv.starttls(context=_ssl.create_default_context())
                except Exception:
                    pass
        try:
            if c.get('user'):
                try:
                    srv.login(c['user'], c.get('pass') or '')
                except Exception as e:
                    return fail('登录失败：%s（请确认用的是授权码而不是登录密码）' % e)
            srv.sendmail(sender, [to], msg.encode('utf-8'))
        finally:
            try:
                srv.quit()
            except Exception:
                pass
    except Exception as e:
        return fail('邮件发送失败：%s' % e)
    return ok({'delivered': 'smtp'}, '邮件已发送')


def _al_test(p):
    """发一条测试消息，验证通道配置是否正确。"""
    p = p or {}
    conf = _al_conf()
    chs = conf.get('channels') or []
    # 可以只测某一个通道
    only = str(p.get('type') or '').strip()
    if only:
        chs = [c for c in chs if c.get('type') == only]
    if not chs:
        return fail('没有启用任何通知通道，请先添加并启用通道')
    test = {'key': 'test', 'lv': 'warn',
            'msg_cn': '这是一条测试消息。看到它说明通知通道配置正确。',
            'value': None}
    results = []
    allok = True
    for c in chs:
        try:
            if c['type'] == 'bark':
                r = _al_send_bark(c, test)
            elif c['type'] == 'smtp':
                r = _al_send_smtp(c, test)
            else:
                r = _al_send_webhook(c, test)
        except Exception as e:
            r = fail(str(e))
        allok = allok and bool(r.get('ok'))
        results.append({'name': c.get('name') or c['type'], 'type': c['type'],
                        'ok': bool(r.get('ok')),
                        'msg_cn': r.get('msg_cn') or ''})
    _al_hist_append({'ts': datetime.now().isoformat(timespec='seconds'),
                     'key': 'test', 'lv': 'warn',
                     'msg_cn': '手动发送测试消息', 'ok': allok,
                     'channels': len(chs)})
    if allok:
        return ok({'results': results}, '测试消息已发送到 %d 个通道' % len(chs))
    return fail('有通道发送失败，请检查下方明细', 'PARTIAL', {'results': results})


def _al_clear():
    try:
        if os.path.exists(ALERT_HIST):
            os.unlink(ALERT_HIST)
    except Exception as e:
        return fail('清空历史失败：%s' % e)
    st = _al_state_load()
    st['sent'] = {}
    st['firing'] = {}
    st['wan_bad_since'] = 0
    _al_state_save(st)
    return ok({}, '告警历史与冷却状态已清空')


def _al_conf_op(p):
    """保存告警设置。"""
    p = p or {}
    cur = _al_conf()
    src = p.get('conf')
    src = src if isinstance(src, dict) else {k: v for k, v in p.items()
                                              if k != 'conf'}
    new = dict(cur)
    if 'enabled' in src:
        new['enabled'] = src['enabled'] is True
    for k, lo, hi in (('interval_sec', 60, 3600), ('cooldown_min', 1, 1440)):
        if k in src and src[k] not in (None, ''):
            try:
                new[k] = max(lo, min(int(src[k]), hi))
            except Exception:
                pass
    for k in ('quiet_from', 'quiet_to'):
        if k in src:
            v = str(src[k] or '').strip()
            if v and not re.match(r'^\d{2}:\d{2}$', v):
                return fail('%s 格式不正确，应为 HH:MM' % k)
            if v:
                try:
                    hh, mm = int(v[:2]), int(v[3:])
                    if hh > 23 or mm > 59:
                        raise ValueError
                except Exception:
                    return fail('%s 的小时或分钟超出范围' % k)
            new[k] = v
    if 'rules' in src and isinstance(src['rules'], dict):
        rules = dict(new.get('rules') or {})
        for r in ALERT_RULES:
            if r['k'] in src['rules']:
                v = src['rules'][r['k']]
                if v in (None, ''):
                    continue
                try:
                    v = float(v)
                except Exception:
                    return fail('%s 的阈值必须是数字' % r['n'])
                rules[r['k']] = v
        new['rules'] = rules
    if 'channels' in src and isinstance(src['channels'], list):
        # Web 层出于安全不把 SMTP 口令回显给前端（GET 里被抹掉），
        # 于是用户「只改了阈值」再点保存时，传上来的 pass 必然是空串。
        # 如果照单全收，密码就被清空了 —— 用户再也收不到邮件，
        # 而且界面上完全看不出发生过。这里按「同类型 + 同端点」沿用旧口令。
        olds = [c for c in (cur.get('channels') or []) if isinstance(c, dict)]
        chs = []
        for c in src['channels']:
            if not isinstance(c, dict):
                continue
            t = str(c.get('type') or '').strip()
            if t not in ('bark', 'smtp', 'webhook'):
                return fail('未知的通知通道类型：%s' % (t or '空'))
            c = dict(c)
            if not str(c.get('pass') or '').strip():
                same = [o for o in olds
                        if o.get('type') == t
                        and str(o.get('host') or '') == str(c.get('host') or '')
                        and str(o.get('user') or '') == str(c.get('user') or '')]
                if same and same[0].get('pass'):
                    c['pass'] = same[0]['pass']
            chs.append(c)
        new['channels'] = chs
    merged = _al_conf(new)
    _save_setting('alert', merged)
    timer = _write_alert_timer(merged)
    log('info', 'alert', 'AL_CONF_SAVED',
        '已保存告警设置（%s，冷却 %d 分钟）'
        % ('已启用' if merged['enabled'] else '未启用', merged['cooldown_min']))
    return ok({'conf': merged, 'timer_applied': timer}, '告警设置已保存')


# ------------------------------------------------------------- 告警 timer

def _write_alert_timer(conf):
    """生成告警检测的 service + timer 单元，并按开关启停。"""
    interval = max(60, min(int((conf or {}).get('interval_sec') or 300), 3600))
    svc = """[Unit]
Description=drouter 告警检测与推送
After=network.target
Documentation=file:///opt/drouter/backend/drouter-alertd.py

[Service]
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Type=oneshot
User=root
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=6
ExecStart=/usr/bin/python3 /opt/drouter/backend/drouter-alertd.py

[Install]
WantedBy=multi-user.target
"""
    tmr = """[Unit]
Description=drouter 告警检测定时器（每 %d 秒）

[Timer]
OnBootSec=3min
OnUnitActiveSec=%ds
AccuracySec=10s
Persistent=false
Unit=drouter-alertd.service

[Install]
WantedBy=timers.target
""" % (interval, interval)
    try:
        os.makedirs('/etc/systemd/system', exist_ok=True)
        with open('/etc/systemd/system/drouter-alertd.service', 'w',
                  encoding='utf-8') as f:
            f.write(svc)
        with open('/etc/systemd/system/drouter-alertd.timer', 'w',
                  encoding='utf-8') as f:
            f.write(tmr)
        sh(['systemctl', 'daemon-reload'], timeout=20)
    except Exception as e:
        log('error', 'alert', 'AL_TIMER_FAIL', '写入告警定时器失败：%s' % e)
        return False
    if not os.path.isdir('/run/systemd/system'):
        log('warn', 'alert', 'AL_TIMER_NOSYSTEMD',
            '当前环境没有 systemd，告警定时器已写入但未启用')
        return False
    if (conf or {}).get('enabled'):
        rc, out, err = sh(['systemctl', 'enable', '--now', 'drouter-alertd.timer'],
                          timeout=30)
        if rc != 0:
            log('warn', 'alert', 'AL_TIMER_ENABLE_FAIL',
                '告警定时器启用失败：%s' % (err or out))
            return False
        return True
    sh(['systemctl', 'disable', '--now', 'drouter-alertd.timer'], timeout=30)
    return False


def read_alertd(p=None):
    return _al_status()


# ============================================== 配额与用量账单（1.0.7）
#
# ── 最重要的约束：不能在请求里全量扫日志 ─────────────────────────────────────
# ulog.jsonl 在正常运行时可以到几十万行、几百 MB。用户在页面上点一下「查询」，
# 如果就 `f.read().splitlines()` 然后全量 json.loads，4GB 内存的机器会直接
# 被 OOM kill —— 而且这正是最常见的场景（这台机器就是给人用来看流量的）。
#
# 解决办法是**增量聚合**：drouter-quotad.py 每 10 分钟扫一次新追加的行，
# 按 (小时 × 设备 × 服务) 累加到 /opt/drouter/data/quota.jsonl。
# 页面查询只读这个已经聚合过的小文件（通常几 MB），再叠加内存里的缓存。
# 聚合是幂等的：用「已处理到第几字节 + 文件指纹」做游标，重跑不会重复计。
#
# ── 数据来源与口径 ──────────────────────────────────────────────────────────
# * 设备名来自 DHCP 租约（IP → 主机名 / MAC），拿不到就叫「未知设备」；
# * 字节数取 conntrack 事件的 extra.bytes，只算 ORIG 方向（回复方向会重复计）；
# * 已知问题：conntrack 事件是「连接结束时」才带最终字节数的，
#   长连接（比如持续下载）在中途查是看不到的 —— 所以当月数据会随时间回补，
#   这是数据模型的固有限制，界面上必须说清楚，否则用户会以为统计不准。

QUOTA_AGG = '/opt/drouter/data/quota.jsonl'
QUOTA_CURSOR = '/opt/drouter/data/quota.cursor'
QUOTA_CONF = 'quota'
# 聚合文件上限：超过就按小时桶裁剪最早的数据
QUOTA_KEEP_HOURS = 24 * 120        # 保留 120 天的小时桶
QUOTA_MAX_BUCKETS = 24 * 120

# 常见服务端口 → 中文服务名。用于「按服务分类」和工作室的费用说明。
# 只收家用/办公最常碰的，认不出来就归「其它」。
QUOTA_PORTS = {
    '80': '网页浏览', '443': '网页浏览（HTTPS）', '8080': '网页浏览（代理）',
    '22': '远程登录', '23': '远程登录', '3389': '远程桌面',
    '445': 'Windows 共享', '139': 'Windows 共享', '2049': 'NFS 共享',
    '53': 'DNS 查询', '67': 'DHCP 分配', '546': 'DHCP 请求',
    '123': '时间同步（NTP）', '1900': '设备发现（SSDP）',
    '5060': '网络电话（SIP）', '1935': '直播推流', '554': '视频监控（RTSP）',
    '5000': '影音服务', '32400': '媒体库（Plex）', '8096': '媒体库（Jellyfin）',
    '9100': '网络打印', '631': '网络打印（IPP）', '137': 'NetBIOS 名称',
    '138': 'NetBIOS 会话', '161': 'SNMP 监控', '1883': 'MQTT / 智能家居',
    '51820': 'WireGuard VPN', '5061': '加密网络电话',
}


def _qt_load():
    return _load_setting(QUOTA_CONF) or {}


def _qt_norm(d):
    d = d if isinstance(d, dict) else {}
    out = {
        'enabled': d.get('enabled') is True,
        'interval_min': max(5, min(int(d.get('interval_min') or 10), 240)),
        # 套餐信息：填了才能算「费用分摊」
        'plan_total_gb': 0,
        'plan_price': 0,
        'plan_cycle_day': 1,
        'currency': str(d.get('currency') or '¥')[:4],
        'alloc': {},
    }
    try:
        out['plan_total_gb'] = max(0, float(d.get('plan_total_gb') or 0))
    except Exception:
        out['plan_total_gb'] = 0
    try:
        out['plan_price'] = max(0, float(d.get('plan_price') or 0))
    except Exception:
        out['plan_price'] = 0
    try:
        out['plan_cycle_day'] = max(1, min(int(d.get('plan_cycle_day') or 1), 28))
    except Exception:
        out['plan_cycle_day'] = 1
    al = d.get('alloc')
    if isinstance(al, dict):
        clean = {}
        for k, v in al.items():
            try:
                pct = max(0.0, min(float(v), 100.0))
            except Exception:
                continue
            if pct > 0:
                clean[str(k)[:64]] = round(pct, 2)
        out['alloc'] = clean
    return out


def _qt_cursor_load():
    try:
        with open(QUOTA_CURSOR, encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {'offset': 0, 'ino': 0, 'partial': ''}


def _qt_cursor_save(d):
    try:
        os.makedirs(os.path.dirname(QUOTA_CURSOR), exist_ok=True)
        tmp = QUOTA_CURSOR + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f)
        os.replace(tmp, QUOTA_CURSOR)
    except Exception as e:
        log('warn', 'quota', 'QT_CURSOR_FAIL', '配额游标写盘失败：%s' % e)


def _qt_name_map():
    """IP → {name, mac}，来自 DHCP 租约。

    每次聚合都重新读一次租约（几百 KB，可以忽略），
    这样新设备第一次上网后，下一轮聚合就能显示名字。
    """
    out = {}
    r = read_leases({})
    for row in (r.get('data') or {}).get('leases') or []:
        ip = str(row.get('ip') or '')
        if ip:
            out[ip] = {'name': row.get('hostname') or row.get('name') or '',
                       'mac': row.get('mac') or ''}
    return out


def _qt_svc_name(port, proto=''):
    p = str(port or '').strip()
    if p in QUOTA_PORTS:
        return QUOTA_PORTS[p]
    try:
        n = int(p)
    except Exception:
        return '其它'
    if proto.upper() == 'UDP' and n == 123:
        return '时间同步（NTP）'
    if 1024 < n < 65535:
        return '其它'
    return '系统服务'


def act_quota(p):
    """配额与用量账单：status / report / conf / reset / name / device_detail。"""
    p = p or {}
    op = str(p.get('op') or 'status')
    if op == 'status':
        return _qt_status()
    if op == 'report':
        return _qt_report(p)
    if op == 'conf':
        return _qt_conf(p)
    if op == 'reset':
        return _qt_reset()
    if op == 'device':
        return _qt_device(p)
    if op == 'aggregate':
        return _qt_aggregate()
    return fail('未知的配额操作：%s' % op)


def _qt_status():
    conf = _qt_norm(_qt_load())
    st = _svc_states(('drouter-quotad.service', 'drouter-quotad.timer'))
    rc, nxt, _e = sh(['systemctl', 'list-timers', 'drouter-quotad.timer',
                      '--no-pager', '--no-legend'], timeout=12)
    cur = _qt_cursor_load()
    agg_exists = os.path.isfile(QUOTA_AGG)
    agg_size = os.path.getsize(QUOTA_AGG) if agg_exists else 0
    # 归档里还有多少没被聚合的字节 —— 这是「数据新鲜度」的关键指标
    lag = None
    try:
        if os.path.isfile(ULOG_ARCHIVE):
            cur_size = os.path.getsize(ULOG_ARCHIVE)
            if cur.get('ino') == os.stat(ULOG_ARCHIVE).st_ino:
                lag = max(0, cur_size - int(cur.get('offset') or 0))
    except Exception:
        pass
    last = {}
    try:
        with open(os.path.join(LOGDIR, 'quotad.jsonl'), encoding='utf-8',
                  errors='replace') as f:
            for ln in reversed(f.read().splitlines()[-40:]):
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                if (r.get('code') or '').startswith('QT_'):
                    last = {'ts': r.get('ts', ''), 'msg_cn': r.get('msg_cn', ''),
                            'code': r.get('code', '')}
                    break
    except Exception:
        pass
    return ok({
        'conf': conf,
        'units': st,
        'next': (nxt.splitlines()[0].strip() if nxt else ''),
        'last': last,
        'agg_size': agg_size,
        'agg_exists': agg_exists,
        'lag_bytes': lag,
        'has_systemd': os.path.isdir('/run/systemd/system'),
        'ports': QUOTA_PORTS,
        'has_archive': os.path.isfile(ULOG_ARCHIVE),
    }, '已读取用量统计设置')


def _qt_bucket_key(ts):
    """把 ISO 时间戳截到「小时桶」。"""
    return (ts or '')[:13]          # 2026-10-02T15


def _qt_scan_new_lines():
    """读归档里尚未聚合的部分。

    返回 (行列表, 新游标)。做三件关键的事：
    1) **按字节偏移续读**，不重扫全文件；
    2) 记录未处理完的残行到 partial，下轮接着拼 —— 直接丢会让最后一行永远
       聚合不到（而那恰好可能是刚发生的最新流量）；
    3) 归档被轮转（inode 变了）时从 0 重读。
    """
    if not os.path.isfile(ULOG_ARCHIVE):
        return [], _qt_cursor_load()
    cur = _qt_cursor_load()
    try:
        st = os.stat(ULOG_ARCHIVE)
    except Exception:
        return [], cur
    size = st.st_size
    if cur.get('ino') != st.st_ino:
        # 归档换了文件（轮转/重建）：从头开始
        cur = {'offset': 0, 'ino': st.st_ino, 'partial': ''}
    off = int(cur.get('offset') or 0)
    if off > size:
        # 文件被截短（不太可能发生，但要有兜底）
        off, cur['partial'] = 0, ''
    if off == size:
        return [], cur
    try:
        with open(ULOG_ARCHIVE, 'r', encoding='utf-8', errors='replace') as f:
            f.seek(off)
            chunk = f.read(size - off)
    except Exception as e:
        log('warn', 'quota', 'QT_READ_FAIL', '读取日志归档失败：%s' % e)
        return [], cur
    cur['offset'] = size
    cur['ino'] = st.st_ino
    partial = str(cur.get('partial') or '')
    data = partial + chunk
    lines = data.split('\n')
    # 最后一段可能是被切断的半行，留到下轮
    tail = lines.pop() if lines else ''
    if tail and not data.endswith('\n'):
        cur['partial'] = tail
    else:
        cur['partial'] = ''
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out, cur


def _qt_aggregate():
    """把新产生的流日志聚合成小时桶，追加到 quota.jsonl。

    幂等：靠 cursor 的字节偏移保证同一行只聚合一次。
    """
    recs, cur = _qt_scan_new_lines()
    _qt_cursor_save(cur)
    if not recs:
        return ok({'new': 0, 'agg': _qt_agg_lines()}, '没有新的流量记录需要聚合')
    names = _qt_name_map()
    buckets = {}
    used = 0
    for r in recs:
        # 只统计带真实字节数的连接事件，其它日志源（系统 / WAN / 应用）
        # 没有流量信息，硬算会得到一堆 0
        if r.get('src') != 'flow':
            continue
        ex = r.get('extra') or {}
        if not isinstance(ex, dict):
            continue
        if ex.get('reply'):
            continue
        b = ex.get('bytes')
        try:
            b = int(b or 0)
        except Exception:
            continue
        if b <= 0:
            continue
        sip = str(r.get('saddr') or '')
        if not sip:
            continue
        nm = (names.get(sip) or {}).get('name') or ''
        mac = (names.get(sip) or {}).get('mac') or ''
        svc = _qt_svc_name(r.get('dport'), r.get('proto'))
        bk = (_qt_bucket_key(r.get('ts')), sip, nm, mac, svc)
        row = buckets.get(bk)
        if row is None:
            buckets[bk] = [1, b]
        else:
            row[0] += 1
            row[1] += b
        used += b
    if not buckets:
        return ok({'new': len(recs), 'rows': 0, 'bytes': 0},
                  '本次新增 %d 条日志，其中没有可统计的流量' % len(recs))
    rows = []
    for (bk, sip, nm, mac, svc), (cnt, total) in buckets.items():
        rows.append({'h': bk, 'ip': sip, 'name': nm, 'mac': mac,
                     'svc': svc, 'n': cnt, 'b': total})
    try:
        os.makedirs(os.path.dirname(QUOTA_AGG), exist_ok=True)
        with open(QUOTA_AGG, 'a', encoding='utf-8') as f:
            for x in rows:
                f.write(json.dumps(x, ensure_ascii=False) + '\n')
    except Exception as e:
        log('error', 'quota', 'QT_WRITE_FAIL', '配额聚合写盘失败：%s' % e)
        return fail('配额聚合写盘失败：%s' % e)
    _qt_prune_agg()
    log('info', 'quota', 'QT_AGG_OK',
        '已聚合 %d 条流量记录（%.1f MB）到 %d 个统计桶'
        % (len(rows), used / 1048576.0, len(rows)))
    return ok({'new': len(recs), 'rows': len(rows), 'bytes': used},
              '已聚合 %d 个统计桶（%.1f MB）' % (len(rows), used / 1048576.0))


def _qt_agg_lines():
    """读聚合文件。行数天然很少（小时 × 设备 × 服务），可以整读。"""
    out = []
    try:
        with open(QUOTA_AGG, encoding='utf-8', errors='replace') as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    out.append(json.loads(ln))
                except Exception:
                    continue
    except Exception:
        pass
    return out


def _qt_prune_agg():
    """裁剪超出保留期的聚合行。"""
    rows = _qt_agg_lines()
    if len(rows) <= QUOTA_MAX_BUCKETS:
        return 0
    # 按小时桶去重后取最近的 N 个
    hours = sorted({r.get('h') for r in rows if r.get('h')})
    keep = set(hours[-QUOTA_KEEP_HOURS:])
    out = [r for r in rows if r.get('h') in keep]
    if len(out) == len(rows):
        return 0
    try:
        tmp = QUOTA_AGG + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            for r in out:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
        os.replace(tmp, QUOTA_AGG)
    except Exception:
        return 0
    return len(rows) - len(out)


def _qt_agg_by_ip(rows):
    """把「同一 IP 的多行」合并成一行。

    必须做这一步：DHCP 租约变化会让同一台设备在不同小时带不同的
    name/mac 键，直接 group 会把一台手机算成好几台。
    """
    agg = {}
    for r in rows:
        ip = r.get('ip') or '未知'
        a = agg.get(ip)
        n = int(r.get('n') or 0)
        b = int(r.get('b') or 0)
        if a is None:
            agg[ip] = {'ip': ip, 'name': r.get('name') or '',
                       'mac': r.get('mac') or '', 'n': n, 'b': b}
        else:
            a['n'] += n
            a['b'] += b
            # 名字取最新的非空值（老桶可能没名字）
            if r.get('name'):
                a['name'] = r['name']
            if r.get('mac'):
                a['mac'] = r['mac']
    for a in agg.values():
        if not a['name']:
            a['name'] = a['ip']
    return agg


def _qt_month_key(h):
    """小时桶 'YYYY-MM-DDTHH' → 'YYYY-MM'。"""
    return (h or '')[:7]


def _qt_current_month():
    return datetime.now().strftime('%Y-%m')


def _qt_report(p):
    """生成用量报表。

    month 为空时按当月。所有筛选都在已经聚合过的小数据上做，
    不会碰到几百 MB 的原始归档。
    """
    p = p or {}
    conf = _qt_norm(_qt_load())
    month = str(p.get('month') or '')[:7]
    if not re.match(r'^\d{4}-\d{2}$', month):
        month = _qt_current_month()
    rows = [r for r in _qt_agg_lines() if _qt_month_key(r.get('h')) == month]
    if not rows:
        return ok({'month': month, 'empty': True, 'devices': [],
                   'services': [], 'days': [], 'total_b': 0, 'total_n': 0,
                   'conf': conf, 'months': _qt_months()},
                  '%s 还没有任何流量统计。请确认统一日志与本功能都已启用，'
                  '并且已经过了一个聚合周期。' % month)
    by_ip = _qt_agg_by_ip(rows)
    total_b = sum(a['b'] for a in by_ip.values())
    total_n = sum(a['n'] for a in by_ip.values())
    # 按设备
    devs = sorted(by_ip.values(), key=lambda a: -a['b'])
    for a in devs:
        a['pct'] = round(a['b'] * 100.0 / total_b, 2) if total_b else 0
        a['alloc'] = conf['alloc'].get(a['mac']) or conf['alloc'].get(a['ip']) or 0
    # 按服务
    svc = {}
    for r in rows:
        s = r.get('svc') or '其它'
        cur = svc.get(s)
        if cur is None:
            svc[s] = [0, 0]
        svc[s][0] += int(r.get('n') or 0)
        svc[s][1] += int(r.get('b') or 0)
    services = [{'svc': k, 'n': v[0], 'b': v[1],
                 'pct': round(v[1] * 100.0 / total_b, 2) if total_b else 0}
                for k, v in svc.items()]
    services.sort(key=lambda x: -x['b'])
    # 按天
    days = {}
    for r in rows:
        dk = (r.get('h') or '')[:10]
        if not dk:
            continue
        days[dk] = days.get(dk, 0) + int(r.get('b') or 0)
    day_rows = [{'d': k, 'b': v} for k, v in sorted(days.items())]
    # 费用分摊
    bill = _qt_bill(conf, total_b, devs)
    return ok({
        'month': month,
        'empty': False,
        'devices': devs,
        'services': services,
        'days': day_rows,
        'total_b': total_b,
        'total_n': total_n,
        'bill': bill,
        'conf': conf,
        'months': _qt_months(),
        'note': '字节数取自连接跟踪事件在连接结束时的统计。'
                '长时间持续连接（如大文件下载、在线视频）会在连接断开后才计入，'
                '所以当月数字会随时间回补。',
    }, '%s 统计：%d 台设备，合计 %s' % (month, len(devs), _hbytes(total_b)))


def _qt_months():
    ms = sorted({_qt_month_key(r.get('h')) for r in _qt_agg_lines()
                 if _qt_month_key(r.get('h'))})
    ms.reverse()
    return ms


def _qt_bill(conf, total_b, devs):
    """按套餐和预设比例分摊费用。

    两种口径：
      * 总流量均摊 —— 不管填没填比例，都按各设备实际用量占总用量的比例分；
      * 固定配额分摊 —— 填了「该设备应承担百分之多少」就按填的算。
        工作室场景常见：老板按人头分，机器按台数分。
    """
    total_gb = conf.get('plan_total_gb') or 0
    price = conf.get('plan_price') or 0
    out = {'total_gb': total_gb, 'price': price,
           'currency': conf.get('currency') or '¥',
           'used_gb': round(total_b / 1073741824.0, 3),
           'by_usage': [], 'by_alloc': []}
    if not price or not total_b:
        return out
    # 超额部分要按更贵的单价算，才符合真实账单
    used_gb = total_b / 1073741824.0
    if used_gb <= total_gb:
        cost = price
    else:
        # 超出部分按 3 倍单价估（各家不同，这里给一个保守的量级）
        cost = price + (used_gb - total_gb) / max(total_gb, 1) * price * 3
    for a in devs:
        out['by_usage'].append({
            'ip': a['ip'], 'name': a['name'], 'mac': a['mac'],
            'gb': round(a['b'] / 1073741824.0, 3),
            'pct': a['pct'],
            'money': round(cost * a['pct'] / 100.0, 2),
        })
    # 固定配额：按填的百分比，剩余未分配部分由「未指定」承担
    fixed = conf.get('alloc') or {}
    alloc_sum = sum(fixed.values())
    rows = []
    for a in devs:
        key = a['mac'] or a['ip']
        pct = float(fixed.get(key) or 0)
        if pct <= 0:
            continue
        rows.append({'ip': a['ip'], 'name': a['name'], 'pct': pct,
                     'money': round(cost * pct / 100.0, 2)})
    if rows:
        rest = max(0.0, 100.0 - sum(r['pct'] for r in rows))
        if rest > 0.01:
            rows.append({'ip': '', 'name': '未指定（按实际用量兜底）',
                         'pct': round(rest, 2),
                         'money': round(cost * rest / 100.0, 2)})
        out['by_alloc'] = rows
    return out


def _qt_device(p):
    """单台设备的明细（按天 + 按服务）。"""
    ip = str(p.get('ip') or '').strip()
    if not ip:
        return fail('缺少设备 IP')
    month = str(p.get('month') or '')[:7]
    if not re.match(r'^\d{4}-\d{2}$', month):
        month = _qt_current_month()
    rows = [r for r in _qt_agg_lines()
            if _qt_month_key(r.get('h')) == month and r.get('ip') == ip]
    if not rows:
        return ok({'ip': ip, 'month': month, 'empty': True, 'days': [],
                   'services': []}, '该设备本月没有统计记录')
    days = {}
    svc = {}
    nm = ''
    mac = ''
    for r in rows:
        dk = (r.get('h') or '')[:10]
        days[dk] = days.get(dk, 0) + int(r.get('b') or 0)
        s = r.get('svc') or '其它'
        svc[s] = svc.get(s, 0) + int(r.get('b') or 0)
        if r.get('name'):
            nm = r['name']
        if r.get('mac'):
            mac = r['mac']
    total = sum(days.values())
    return ok({
        'ip': ip, 'month': month, 'name': nm or ip, 'mac': mac,
        'empty': False,
        'total_b': total,
        'days': [{'d': k, 'b': v} for k, v in sorted(days.items())],
        'services': [{'svc': k, 'b': v,
                      'pct': round(v * 100.0 / total, 2) if total else 0}
                     for k, v in sorted(svc.items(), key=lambda x: -x[1])],
    }, '%s 的 %s 用量：%s' % (nm or ip, month, _hbytes(total)))


def _qt_conf(p):
    p = p or {}
    src = p.get('conf')
    src = src if isinstance(src, dict) else p
    conf = _qt_norm({**_qt_load(), **{k: v for k, v in src.items()
                                      if k != 'op'}})
    _save_setting(QUOTA_CONF, conf)
    timer = _write_quota_timer(conf)
    log('info', 'quota', 'QT_CONF_SAVED',
        '已保存用量统计设置（%s，每 %d 分钟聚合一次）'
        % ('已启用' if conf['enabled'] else '未启用', conf['interval_min']))
    return ok({'conf': conf, 'timer_applied': timer}, '用量统计设置已保存')


def _qt_reset():
    try:
        for f in (QUOTA_AGG, QUOTA_CURSOR):
            if os.path.isfile(f):
                os.unlink(f)
    except Exception as e:
        return fail('清空统计失败：%s' % e)
    log('info', 'quota', 'QT_RESET', '已清空全部用量统计')
    return ok({}, '用量统计已清空，将从下一轮聚合重新开始')


# ------------------------------------------------------------- 配额聚合 timer

def _write_quota_timer(conf):
    """生成用量聚合的 service + timer 单元，并按开关启停。

    默认 10 分钟一次：足够让「今天用了多少」有意义，又不至于频繁读归档。
    """
    interval_min = max(5, min(int((conf or {}).get('interval_min') or 10), 240))
    svc = """[Unit]
Description=drouter 用量聚合（按设备与服务统计流量）
After=network.target
Documentation=file:///opt/drouter/backend/drouter-quotad.py

[Service]
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Type=oneshot
User=root
Nice=15
IOSchedulingClass=idle
ExecStart=/usr/bin/python3 /opt/drouter/backend/drouter-quotad.py

[Install]
WantedBy=multi-user.target
"""
    tmr = """[Unit]
Description=drouter 用量聚合定时器（每 %d 分钟）

[Timer]
OnBootSec=5min
OnUnitActiveSec=%dmin
AccuracySec=30s
Persistent=false
Unit=drouter-quotad.service

[Install]
WantedBy=timers.target
""" % (interval_min, interval_min)
    try:
        os.makedirs('/etc/systemd/system', exist_ok=True)
        with open('/etc/systemd/system/drouter-quotad.service', 'w',
                  encoding='utf-8') as f:
            f.write(svc)
        with open('/etc/systemd/system/drouter-quotad.timer', 'w',
                  encoding='utf-8') as f:
            f.write(tmr)
        sh(['systemctl', 'daemon-reload'], timeout=20)
    except Exception as e:
        log('error', 'quota', 'QT_TIMER_FAIL', '写入聚合定时器失败：%s' % e)
        return False
    if not os.path.isdir('/run/systemd/system'):
        log('warn', 'quota', 'QT_TIMER_NOSYSTEMD',
            '当前环境没有 systemd，用量聚合定时器已写入但未启用')
        return False
    if (conf or {}).get('enabled'):
        rc, out, err = sh(['systemctl', 'enable', '--now', 'drouter-quotad.timer'],
                          timeout=30)
        if rc != 0:
            log('warn', 'quota', 'QT_TIMER_ENABLE_FAIL',
                '聚合定时器启用失败：%s' % (err or out))
            return False
        return True
    sh(['systemctl', 'disable', '--now', 'drouter-quotad.timer'], timeout=30)
    return False


def read_quotad(p=None):
    return _qt_status()


# ---------------------------------------------------------------- 自动快照参数

AUTO_DEFAULTS = {
    'enabled': True,          # 自动快照默认开启
    'path': SNAP_DEFAULT,     # 快照存放路径（可自定义）
    'interval_hours': 6,      # 快照间隔（小时）—— "多久拍一次"
    'keep_days': 7,           # 快照过期时间（天）—— "多少天后自动删除"
    'keep_count': 30,         # 最多保留份数（0=不限）
    'keep_manual': True,      # 手动快照不参与自动清理
    'on_apply': True,         # 每次应用配置前自动拍一张
}


def _load_auto():
    cfg = dict(AUTO_DEFAULTS)
    try:
        if os.path.isfile(SNAP_CONF):
            with open(SNAP_CONF, encoding='utf-8') as f:
                cfg.update(json.load(f) or {})
    except Exception:
        pass
    # 界面里保存的配置优先
    try:
        import sqlite3
        if os.path.exists(DB_PATH):
            conn = sqlite3.connect(DB_PATH)
            r = conn.execute("SELECT value FROM settings WHERE key='snapshot'").fetchone()
            conn.close()
            if r and r[0]:
                cfg.update(json.loads(r[0]) or {})
    except Exception:
        pass
    return cfg


def _save_auto(cfg):
    _atomic_write(SNAP_CONF,
                  json.dumps(cfg, ensure_ascii=False, indent=2) + '\n')
    # 让 systemd timer 按新间隔重新排程
    try:
        sh(['systemctl', 'daemon-reload'], timeout=20)
        if cfg.get('enabled', True):
            sh(['systemctl', 'enable', '--now', 'drouter-snapshot.timer'], timeout=25)
        else:
            sh(['systemctl', 'disable', '--now', 'drouter-snapshot.timer'], timeout=25)
    except Exception:
        pass
    return True


def act_auto_snapshot(p):
    """查询 / 保存自动快照策略"""
    op = str((p or {}).get('op') or 'get')
    if op == 'get':
        cfg = _load_auto()
        total, used, free = _disk_usage(cfg.get('path') or SNAP_DEFAULT)
        # timer 运行状态
        rc, st, _e = sh(['systemctl', 'is-active', 'drouter-snapshot.timer'], timeout=8)
        rc2, nx, _e = sh(['systemctl', 'list-timers', 'drouter-snapshot.timer',
                          '--no-pager', '--output=json'], timeout=10)
        next_run = ''
        try:
            arr = json.loads(nx or '[]')
            if arr:
                next_run = arr[0].get('next') or ''
        except Exception:
            pass
        return ok({'config': cfg, 'timer_active': st == 'active',
                   'next_run': next_run,
                   'disk': {'total_mb': total, 'used_mb': used, 'free_mb': free},
                   'scope': SNAPSHOT_SCOPE})
    if op == 'set':
        cfg = _load_auto()
        newp = (p or {}).get('path')
        if newp:
            if not os.path.isabs(str(newp)):
                return fail('快照路径必须是绝对路径，例如 /opt/drouter/snapshots')
            # 出于安全考虑，只允许写在 /opt /srv /var/backups /mnt /media /home 下
            allowed = ('/opt/', '/srv/', '/var/backups/', '/mnt/', '/media/', '/home/')
            if not str(newp).startswith(allowed):
                return fail('快照路径只允许位于 %s 之下，避免误写系统关键目录'
                            % '、'.join(x.rstrip('/') for x in allowed))
            cfg['path'] = str(newp)
        for k, conv in (('enabled', lambda v: bool(v)),
                        ('interval_hours', lambda v: max(1, min(int(v), 168))),
                        ('keep_days', lambda v: max(0, min(int(v), 3650))),
                        ('keep_count', lambda v: max(0, min(int(v), 10000))),
                        ('keep_manual', lambda v: bool(v)),
                        ('on_apply', lambda v: bool(v))):
            if k in (p or {}) and p.get(k) is not None:
                try:
                    cfg[k] = conv(p[k])
                except Exception:
                    return fail('参数 %s 取值不合法' % k)
        # 路径可用性检查
        try:
            os.makedirs(cfg['path'], exist_ok=True)
        except Exception as e:
            return fail('快照路径不可用：%s' % e)
        _save_auto(cfg)
        # 重建 timer（间隔变了要重新排程）
        try:
            _write_snapshot_timer(cfg.get('interval_hours') or 6)
            sh(['systemctl', 'daemon-reload'], timeout=20)
            if cfg.get('enabled', True):
                sh(['systemctl', 'enable', '--now', 'drouter-snapshot.timer'], timeout=30)
            else:
                sh(['systemctl', 'disable', '--now', 'drouter-snapshot.timer'], timeout=30)
        except Exception:
            pass
        # 同步一份到界面配置库，方便其他模块读取
        try:
            _save_setting('snapshot', cfg)
        except Exception:
            pass
        log('warn', 'system', 'AUTO_SNAPSHOT_SET',
            '自动快照策略已更新（%s，间隔 %dh，保留 %d 天）'
            % ('开启' if cfg.get('enabled') else '关闭', cfg['interval_hours'], cfg['keep_days']))
        return ok({'config': cfg},
                  '自动快照策略已保存：%s，每 %d 小时一次，%d 天后自动清理'
                  % ('已开启' if cfg.get('enabled') else '已关闭',
                     cfg['interval_hours'], cfg['keep_days']))
    if op == 'run_now':
        ts, d = _snapshot('auto-manual')
        cfg = _load_auto()
        act_snapshot_prune({'keep_days': cfg['keep_days'], 'keep_count': cfg['keep_count'],
                            'keep_manual': cfg.get('keep_manual', True)})
        return ok({'ts': ts, 'path': d}, '已立即执行一次自动快照：%s' % ts)
    return fail('未知操作：%s' % op)



def _restore_snapshot(ts, do_reload=True):
    """
    把快照 ts 内容还原到本机：
      1) 数据库整体还原（settings/ifaces/admins）
      2) /etc 配置文件还原
      3) 可选：重载相关服务
    返回 (是否成功, 还原明细 dict)
    """
    root = snap_root()
    d = os.path.join(root, ts)
    if not os.path.isdir(d):
        return False, {'error': '找不到快照：%s（查找于 %s）' % (ts, root)}
    detail = {'db': False, 'files': [], 'dirs': [], 'reloaded': []}

    # ---- 0) 还原前先给"当前状态"打一张 rollback- 快照，防止还原本身出问题
    try:
        _snapshot('before-rollback')
    except Exception:
        pass

    # ---- 1) 数据库还原
    dbsrc = os.path.join(d, 'drouter.db')
    if os.path.isfile(dbsrc):
        try:
            import sqlite3
            # 校验快照里的 db 是否完整可读
            chk = sqlite3.connect(dbsrc)
            chk.execute('SELECT count(*) FROM sqlite_master').fetchone()
            chk.close()
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
            shutil.copy2(dbsrc, DB_PATH)
            try:
                os.chmod(DB_PATH, 0o660)
            except Exception:
                pass
            detail['db'] = True
        except Exception as e:
            detail['db_err'] = str(e)

    # ---- 2) /etc 文件还原
    for f in SNAPSHOT_ETC_FILES:
        src = os.path.join(d, f.replace('/', '_'))
        if os.path.isfile(src):
            try:
                os.makedirs(os.path.dirname(f), exist_ok=True)
                shutil.copy2(src, f)
                detail['files'].append(f)
            except Exception:
                pass
    for sub in SNAPSHOT_ETC_DIRS:
        src = os.path.join(d, sub.replace('/', '_'))
        if os.path.isdir(src):
            try:
                os.makedirs(sub, exist_ok=True)
                n, sk = _copytree_soft(src, sub)
                if n or os.path.isdir(sub):
                    detail['dirs'].append('%s（%d 个文件）' % (sub, n))
                if sk:
                    detail['dir_skipped'] = detail.get('dir_skipped', 0) + len(sk)
                    detail['dir_skip_list'] = sk[:10]
            except Exception:
                pass

    # ---- 3) 重载服务（让还原立即生效）
    if do_reload:
        for svc in ('drouter-web', 'dnsmasq', 'radvd', 'dhcpcd', 'nftables', 'chrony', 'miniupnpd'):
            rc, st, _e = sh(['systemctl', 'is-enabled', svc], timeout=8)
            if st.strip() in ('enabled', 'static'):
                rc2, _o, _e2 = sh(['systemctl', 'restart', svc], timeout=40)
                if rc2 == 0:
                    detail['reloaded'].append(svc)
    log('warn', 'system', 'ROLLBACK_DONE',
        '已回滚配置到快照 %s（数据库=%s，文件 %d 项）'
        % (ts, '已还原' if detail['db'] else '未包含', len(detail['files'])), detail)
    return True, detail


def act_rollback(p):
    ts = str((p or {}).get('ts') or '')
    if not re.match(r'^\d{8}-\d{6}$', ts):
        return fail('快照编号不合法：%s' % ts)
    do_reload = (p or {}).get('reload', True) is not False
    okk, detail = _restore_snapshot(ts, do_reload=do_reload)
    if not okk:
        return fail(detail.get('error') or '回滚失败')
    msg = '已回滚到快照 %s：数据库%s，配置文件 %d 项' % (
        ts, '已还原' if detail['db'] else '未包含', len(detail['files']))
    if detail.get('reloaded'):
        msg += '，已重载服务：%s' % '、'.join(detail['reloaded'])
    return ok(detail, msg)


# ------------------------------------------------------------------ 紧急救援通道
#
# 目的：当网络配置被打崩（风暴/冲突/环路/防火墙锁死/Web 起不来）时，
#       提供一条独立于 WAN/LAN 的应急入口，一键还原最近快照并重启。
#
# 实现：
#   - 在一张独立的虚拟网卡（macvlan/dummy，名为 drescue0）上绑定「链路本地地址」
#     169.254.0.1/16，并在每张物理网卡上加同名别名，使"插任一口都能访问"。
#   - 链路本地地址不需要 DHCP、不参与默认路由、不受 nftables 正常链路规则影响。
#   - 另配套一个备用地址 10.99.99.1/24，用于需要在 PVE/物理网络内直连的场景。

RESCUE_CONF = '/etc/drouter/rescue.conf'
RESCUE_VIF = 'drescue0'
RESCUE_DEFAULT = {
    'enabled': False,
    'port': 8888,
    'vip': '169.254.0.1',
    'vip_mask': '16',
    'alt_vip': '10.99.99.1',
    'ifaces': [],
    'auto_reboot': True,
    'note': '',
}


def _load_rescue():
    conf = dict(RESCUE_DEFAULT)
    try:
        if os.path.isfile(RESCUE_CONF):
            with open(RESCUE_CONF, encoding='utf-8') as f:
                conf.update(json.load(f) or {})
    except Exception:
        pass
    return conf


def _rescue_new_token():
    """生成救援访问令牌。

    6 位数字是为了能在手机上手打；强度靠「开启即轮换 + 失败次数限制」，
    不靠字符集——真正防的是「同网段任意主机随手就能还原路由器」。
    用 secrets 才是正经做法，但这个文件没导入它；uuid4 取数字位
    在这里完全够用（不是密码学用途，只是避免可预测的序列）。
    """
    return ''.join(str(uuid.uuid4().int)[i] for i in range(6))


def _save_rescue(conf):
    _atomic_write(RESCUE_CONF,
                  json.dumps(conf, ensure_ascii=False, indent=2) + '\n',
                  mode=0o640)
    # 从 0600 放宽到 0640 且属组 drouter：快照守护以 drouter 身份运行，
    # 读不到这个文件就会让整个 /etc/drouter 目录进不了快照
    # （已由 _copytree_soft 兜底，但能读就别漏）。
    # ⚠️ 现在文件里多了 token —— 它就是救援通道的写操作凭据。
    # 0640 属组 drouter 意味着 drouter 组可读，而 helper 本身就以 drouter
    # 身份运行、必须读到 token 才能在「开启时把令牌显示给用户」。
    # 这是有意的取舍：能进 /api/rescue 的前提是已通过 Web 面板认证，
    # 而 drouter 组权限并不等价于「任意用户可读这个文件」。
    try:
        os.chmod(RESCUE_CONF, 0o640)
        shutil.chown(RESCUE_CONF, group='drouter')
    except Exception:
        try:
            os.chmod(RESCUE_CONF, 0o644)
        except Exception:
            pass
    return True


def _physical_ifaces():
    """列出物理网卡（排除虚拟/回环/救援口）。"""
    out = []
    try:
        for n in sorted(os.listdir('/sys/class/net')):
            if n == 'lo' or n.startswith(('veth', 'br-', 'virbr', 'docker', 'drescue', 'tun', 'tap')):
                continue
            if re.match(r'^(ppp|sit|ip6tnl|tunl)', n):
                continue
            # 排除纯虚拟的（无 device 目录）
            if not os.path.isdir('/sys/class/net/%s/device' % n):
                continue
            out.append(n)
    except Exception:
        pass
    return out


def _rescue_drop_nft_chain():
    """删除救援放行用的 nft chain（连同 input 里引用它的 jump 规则）。

    单独抽出来是因为这段要「按 handle 反复删」：nft 没有「按内容批量删规则」
    的命令，只能 `nft -a list chain` 拿到每条规则的 handle 再逐条 delete。
    写成一段 shell 嵌在 Python 字符串里既难读又容易引号出错。
    """
    rc, o, _e = sh(['sh', '-c',
                    'command -v nft >/dev/null 2>&1 && '
                    'nft list table inet drouter >/dev/null 2>&1 && echo yes'],
                   timeout=8)
    if (o or '').strip() != 'yes':
        return False
    # 先删 jump 规则（最多 8 轮，够覆盖任何重复开启留下的残留）
    for _ in range(8):
        rc, o, _e = sh(['sh', '-c',
                        'nft -a list chain inet drouter input 2>/dev/null '
                        '| grep "jump drouter_rescue" | head -1'], timeout=8)
        line = (o or '').strip()
        if not line:
            break
        m = re.search(r'handle (\d+)', line)
        if not m:
            break
        sh(['nft', 'delete', 'rule', 'inet', 'drouter', 'input',
            'handle', m.group(1)], timeout=8)
    sh(['nft', 'delete', 'chain', 'inet', 'drouter', 'drouter_rescue'], timeout=8)
    return True


def _rescue_apply_net(conf):
    """落地救援虚拟网卡与别名 IP。启用时创建，关闭时回收。"""
    vip = conf.get('vip') or RESCUE_DEFAULT['vip']
    mask = str(conf.get('vip_mask') or RESCUE_DEFAULT['vip_mask'])
    alt = conf.get('alt_vip') or RESCUE_DEFAULT['alt_vip']
    enabled = bool(conf.get('enabled'))
    applied = []
    ifaces = conf.get('ifaces') or _physical_ifaces()

    if not enabled:
        # 关闭：删除别名地址与虚拟口，但保留链路本地（无害）
        for n in _physical_ifaces():
            sh(['ip', 'addr', 'del', '%s/%s' % (alt, '24'), 'dev', n], timeout=8)
            sh(['ip', 'addr', 'del', '%s/%s' % (vip, mask), 'dev', n], timeout=8)
        sh(['ip', 'link', 'del', RESCUE_VIF], timeout=8)
        # 把放行救援端口的那条 chain 一起删掉。
        # 只删 chain 不够 —— input 里的 jump 规则还引用着它，会让后续
        # 对该 chain 的操作全部报错。用 nft -a 拿到 handle 后按 handle 删，
        # 删干净再删 chain。
        _rescue_drop_nft_chain()
        return {'enabled': False, 'applied': []}

    # 1) 创建独立虚拟网卡 drescue0（dummy 类型，不依赖物理链路）
    rc, _o, _e = sh(['ip', 'link', 'show', RESCUE_VIF], timeout=8)
    if rc != 0:
        sh(['ip', 'link', 'add', RESCUE_VIF, 'type', 'dummy'], timeout=10)
    sh(['ip', 'link', 'set', RESCUE_VIF, 'up'], timeout=8)
    rc, _o, e = sh(['ip', 'addr', 'add', '%s/%s' % (vip, mask), 'dev', RESCUE_VIF], timeout=8)
    if rc == 0 or 'exists' in (e or ''):
        applied.append('%s ← %s/%s' % (RESCUE_VIF, vip, mask))
    rc2, _o2, e2 = sh(['ip', 'addr', 'add', '%s/24' % alt, 'dev', RESCUE_VIF], timeout=8)
    if rc2 == 0 or 'exists' in (e2 or ''):
        applied.append('%s ← %s/24' % (RESCUE_VIF, alt))

    # 2) 在每张物理网卡上加别名地址 —— 实现「插任一口都能访问」
    for n in ifaces:
        rc, _o, e = sh(['ip', 'addr', 'add', '%s/%s' % (vip, mask), 'dev', n], timeout=8)
        if rc == 0 or 'exists' in (e or ''):
            applied.append('%s ← %s/%s（链路本地）' % (n, vip, mask))
        rc2, _o2, e2 = sh(['ip', 'addr', 'add', '%s/24' % alt, 'dev', n], timeout=8)
        if rc2 == 0 or 'exists' in (e2 or ''):
            applied.append('%s ← %s/24（备用）' % (n, alt))

    # 3) 放行救援端口的入站（即便用户把 nftables 写错，也尽量留一条后门）
    #    放进**独立的 chain**：原先是 `nft add rule inet drouter input ...`，
    #    直接挂在主 input 链上，只加不删 —— 改端口后再关闭，旧的那条
    #    accept 规则永远留在 nftables 里，等于留了个永久后门。
    #    独立 chain 在关闭时可以整条 delete，不影响用户自己的规则。
    sh(['sh', '-c',
        'command -v nft >/dev/null 2>&1 && nft list table inet drouter >/dev/null 2>&1 && '
        'nft add chain inet drouter drouter_rescue 2>/dev/null; '
        'nft add rule inet drouter drouter_rescue tcp dport %d accept 2>/dev/null; '
        'nft insert rule inet drouter input jump drouter_rescue 2>/dev/null || true'
        % int(conf.get('port') or 8888)], timeout=10)
    return {'enabled': True, 'applied': applied, 'ifaces': ifaces}


def act_rescue(p):
    """查询 / 配置 / 启停 紧急救援通道。"""
    op = str((p or {}).get('op') or 'get')
    conf = _load_rescue()

    if op == 'get':
        rc, st, _e = sh(['systemctl', 'is-active', 'drouter-rescue'], timeout=8)
        rc2, _o, _e = sh(['ip', 'addr', 'show', RESCUE_VIF], timeout=8)
        # 实际生效的地址
        rc3, addrs, _e = sh(['sh', '-c', "ip -o -4 addr show | awk '{print $2\" \"$4}'"], timeout=8)
        conf['_service_active'] = (st == 'active')
        conf['_vif_present'] = (rc2 == 0)
        conf['_addrs'] = (addrs or '').splitlines()[:20]
        conf['_phys_ifaces'] = _physical_ifaces()
        conf['_access_hint'] = 'http://%s:%d/' % (conf.get('vip'), int(conf.get('port') or 8888))
        return ok(conf)

    if op == 'set':
        for k in ('port', 'vip', 'vip_mask', 'alt_vip', 'note'):
            if (p or {}).get(k) is not None:
                conf[k] = str(p.get(k)).strip()
        if 'enabled' in (p or {}) and p.get('enabled') is not None:
            conf['enabled'] = bool(p.get('enabled'))
        if 'auto_reboot' in (p or {}) and p.get('auto_reboot') is not None:
            conf['auto_reboot'] = bool(p.get('auto_reboot'))
        if (p or {}).get('ifaces') is not None:
            conf['ifaces'] = [str(x) for x in (p.get('ifaces') or []) if re.match(r'^[a-zA-Z0-9_.-]+$', str(x))]
        # 校验
        try:
            port = int(conf['port'])
            if not (1 <= port <= 65535):
                raise ValueError
        except Exception:
            return fail('端口必须是 1-65535 的数字')
        if port in (22, 53, 67, 68, 80, 443, 5900, 8080, 8443):
            return fail('端口 %d 已被系统服务占用，请换一个（推荐 8888）' % port)
        import ipaddress
        for key, label in (('vip', '救援地址'), ('alt_vip', '备用救援地址')):
            try:
                ipaddress.IPv4Address(conf[key])
            except Exception:
                return fail('%s 不是合法的 IPv4 地址：%s' % (label, conf[key]))
        try:
            m = int(conf['vip_mask'])
            if not (1 <= m <= 32):
                raise ValueError
        except Exception:
            return fail('掩码长度必须是 1-32')

        _save_rescue(conf)
        net = _rescue_apply_net(conf)
        # 重建 systemd 单元并启停服务
        _write_rescue_unit()
        if conf['enabled']:
            # 每次开启都换一个新 token。
            # 救援通道开启时会把 169.254.0.1/16 挂到每一张物理网卡上，
            # 于是「救援网段内的任意主机」= 局域网上任意一台机器。
            # 而确认短语是硬编码在页面源码里的常量 —— 也就是说任何人都能
            # POST /api/restore 回滚配置并重启全机。这条通道存在的意义是
            # 「主面板锁死时唯一的救命入口」，一旦无凭据就等于把路由器交出去。
            # 所以：开启即生成随机 token，rescue.conf 用 0640 属组 drouter
            # （drouter-rescue 以 drouter 组身份读它），页面上的所有写操作
            # 都必须带上它；用户在主面板开启时能看到这个 token 并抄到另一台
            # 机器上用。
            token = _rescue_new_token()
            conf['token'] = token
            _save_rescue(conf)
            sh(['systemctl', 'daemon-reload'], timeout=20)
            rc, _o, e = sh(['systemctl', 'enable', '--now', 'drouter-rescue'], timeout=30)
            log('warn', 'rescue', 'RESCUE_ON',
                '紧急救援通道已开启：http://%s:%d/' % (conf['vip'], int(conf['port'])),
                {'net': net, 'rc': rc})
            if rc != 0:
                return ok({'config': conf, 'net': net},
                          '配置已保存，但救援服务启动失败：%s' % (e or '请查看 journalctl -u drouter-rescue'))
            return ok({'config': conf, 'net': net, 'token': token},
                      '救援通道已开启，可通过 http://%s:%d/ 访问（插任一口皆可）。'
                      '请记下访问令牌 %s —— 页面上的还原操作需要它，'
                      '同网段其它机器无法凭猜测还原你的配置' % (conf['vip'], int(conf['port']), token))
        else:
            sh(['systemctl', 'disable', '--now', 'drouter-rescue'], timeout=30)
            # 关闭时顺手作废 token：留着旧 token 不回收等于没关
            conf['token'] = ''
            _save_rescue(conf)
            log('warn', 'rescue', 'RESCUE_OFF', '紧急救援通道已关闭')
            return ok({'config': conf, 'net': net}, '救援通道已关闭（虚拟地址已回收）')

    if op == 'test':
        # 自检：虚拟口是否存在、端口是否监听
        rc2, _o, _e = sh(['ip', 'addr', 'show', RESCUE_VIF], timeout=8)
        rc, ln, _e = sh(['ss', '-lnt'], timeout=10)
        port = str(int(conf.get('port') or 8888))
        listening = any((':%s ' % port) in l or (':%s\t' % port) in l for l in (ln or '').splitlines())
        return ok({'vif_present': rc2 == 0, 'listening': listening,
                   'port': port, 'enabled': conf.get('enabled')},
                  '救援通道自检：虚拟口 %s，端口 %s %s'
                  % ('正常' if rc2 == 0 else '缺失', port, '已监听' if listening else '未监听'))

    return fail('未知操作：%s' % op)


def _write_rescue_unit():
    """生成 drouter-rescue 服务单元（幂等）。"""
    unit = """[Unit]
Description=drouter 紧急救援通道（独立于 WAN/LAN 的应急恢复入口）
After=network.target
Wants=network.target

[Service]
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Type=simple
User=root
ExecStart=/usr/bin/python3 /opt/drouter/backend/drouter-rescue.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
NoNewPrivileges=false
ProtectSystem=false

[Install]
WantedBy=multi-user.target
"""
    path = '/etc/systemd/system/drouter-rescue.service'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(unit)
    try:
        os.chmod(path, 0o644)
    except Exception:
        pass
    return path


# ------------------------------------------------------------------ 自动快照 timer 单元

def _write_snapshot_timer(interval_hours=6):
    """生成自动快照的 service + timer 单元，按用户设定的间隔运行。"""
    svc = """[Unit]
Description=drouter 自动配置快照
After=network.target

[Service]
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Type=oneshot
User=root
ExecStart=/usr/bin/python3 /opt/drouter/backend/drouter-snapshotd.py
# oneshot 打完包就退出，但 tar 大目录时也会有内存尖峰，给个上限兜底
MemoryMax=300M

[Install]
WantedBy=multi-user.target
"""
    tmr = """[Unit]
Description=drouter 自动快照定时器（每 %d 小时）

[Timer]
OnBootSec=10min
OnUnitActiveSec=%dh
Persistent=true
Unit=drouter-snapshot.service

[Install]
WantedBy=timers.target
""" % (int(interval_hours), int(interval_hours))
    os.makedirs('/etc/systemd/system', exist_ok=True)
    with open('/etc/systemd/system/drouter-snapshot.service', 'w', encoding='utf-8') as f:
        f.write(svc)
    with open('/etc/systemd/system/drouter-snapshot.timer', 'w', encoding='utf-8') as f:
        f.write(tmr)
    return True


def act_snapshotd_sync(p):
    """页面保存自动快照策略时，同步重建 timer 单元。"""
    cfg = _load_auto()
    _write_snapshot_timer(cfg.get('interval_hours') or 6)
    sh(['systemctl', 'daemon-reload'], timeout=20)
    if cfg.get('enabled', True):
        sh(['systemctl', 'enable', '--now', 'drouter-snapshot.timer'], timeout=30)
    else:
        sh(['systemctl', 'disable', '--now', 'drouter-snapshot.timer'], timeout=30)
    return ok({'config': cfg, 'interval_hours': cfg.get('interval_hours')},
              '自动快照定时器已按「每 %d 小时」重新排程' % (cfg.get('interval_hours') or 6))


# ------------------------------------------------------------------ 统一日志采集 timer（#11）

def _write_logd_timer(interval_sec=60):
    """生成统一日志采集的 service + timer 单元。

    间隔默认 60 秒：足够让「连接与流日志」有可追溯的历史，又不会给
    4GB/2CPU 的小机器带来负担（单次只采 ~800 条，纯读操作）。
    """
    interval_sec = max(15, min(int(interval_sec or 60), 3600))
    svc = """[Unit]
Description=drouter 统一日志采集（防火墙 / 连接跟踪 / WAN / DDNS / 应用 / 系统）
After=network.target
Documentation=file:///opt/drouter/backend/drouter-logd.py

[Service]
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Type=oneshot
User=root
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=6
ExecStart=/usr/bin/python3 /opt/drouter/backend/drouter-logd.py

[Install]
WantedBy=multi-user.target
"""
    tmr = """[Unit]
Description=drouter 统一日志采集定时器（每 %d 秒）

[Timer]
OnBootSec=2min
OnUnitActiveSec=%ds
AccuracySec=5s
Persistent=false
Unit=drouter-logd.service

[Install]
WantedBy=timers.target
""" % (interval_sec, interval_sec)
    os.makedirs('/etc/systemd/system', exist_ok=True)
    with open('/etc/systemd/system/drouter-logd.service', 'w', encoding='utf-8') as f:
        f.write(svc)
    with open('/etc/systemd/system/drouter-logd.timer', 'w', encoding='utf-8') as f:
        f.write(tmr)
    return True


def act_logd_sync(p):
    """保存日志设置时同步重建 timer，并按开关启停。"""
    p = p or {}
    conf = _ulog_load()
    interval = int(p.get('interval_sec') or 60)
    _write_logd_timer(interval)
    sh(['systemctl', 'daemon-reload'], timeout=20)
    if conf.get('enabled') and conf.get('archive'):
        rc, out, err = sh(['systemctl', 'enable', '--now', 'drouter-logd.timer'],
                          timeout=30)
        applied = (rc == 0)
        msg = ('统一日志采集定时器已启用（每 %d 秒一次）' % max(15, min(interval, 3600))
               if applied else '定时器已写入，但启用失败：%s' % (err or out))
    else:
        sh(['systemctl', 'disable', '--now', 'drouter-logd.timer'], timeout=30)
        applied = False
        msg = '统一日志采集定时器已停用（日志关闭或未开启归档）'
    return ok({'interval_sec': interval, 'conf': conf, 'applied': applied}, msg)


def read_logd(p=None):
    """读取统一日志采集定时器状态（供界面展示）。"""
    # 性能：2 个单元原为 4 次 systemctl fork，改批量（同 read_services）。
    st = _svc_states(('drouter-logd.service', 'drouter-logd.timer'))
    rc, nxt, _e = sh(['systemctl', 'list-timers', 'drouter-logd.timer',
                      '--no-pager', '--no-legend'], timeout=12)
    return ok({'units': st, 'next': (nxt.splitlines()[0].strip() if nxt else ''),
               'last': _ulog_last_run(),
               'conf': _ulog_load()})


def _ulog_last_run():
    """从 ulogd.jsonl 读最近一次采集结果，供界面显示。"""
    path = os.path.join(LOGDIR, 'ulogd.jsonl')
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()[-40:]
        for ln in reversed(lines):
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if (r.get('code') or '').startswith('ULOG_ARCHIVE'):
                return {'ts': r.get('ts', ''), 'msg_cn': r.get('msg_cn', ''),
                        'code': r.get('code', '')}
    except Exception:
        pass
    return {}


# ------------------------------------------------------------------ 应用配置

APPLY_SPEC = {
    # module: (service, action, 中文名)
    'dnsmasq':   ('dnsmasq', 'restart', 'DHCP/DNS 服务'),
    'radvd':     ('radvd', 'restart', 'IPv6 路由通告'),
    'dhcpv6':    ('dhcpcd', 'restart', 'DHCPv6 客户端'),
    'miniupnpd': ('miniupnpd', 'restart', 'UPnP 服务'),
    'upnp':      ('miniupnpd', 'restart', 'UPnP 服务'),
    'chrony':    ('chrony', 'restart', 'NTP 客户端'),
    'ntp':       ('chrony', 'restart', 'NTP 客户端'),
    'nft_v4':    ('nft', 'reload', 'IPv4 防火墙'),
    'nft_v6':    ('nft', 'reload', 'IPv6 防火墙'),
    'network':   ('network', 'none', '网卡与桥接'),
    'ppp':       ('ppp', 'none', 'PPPoE 拨号配置'),
    # 前端 WAN 口页面用的模块名（render 里已别名到 ppp）
    'pppoe':     ('ppp', 'none', 'PPPoE 拨号配置'),
}

# 「纯配置模块」：只入库、没有自己独立的配置文件。
# 它们的内容是作为上下文被别的渲染器消费的（system 提供 lan_iface/wan_iface，
# portfwd 提供 DNAT 规则给 nft_v4/nft_v6）。以前拿去 apply 会直接报
# 「未知的模块：xxx」，用户在网卡与桥接 / 端口转发页点「应用」就撞上这个。
CONFIG_ONLY_MODULES = {
    'system':  '网卡与桥接 / 系统基础设置',
    'portfwd': '端口转发与 DMZ',
}


def _missing_dev_nodes():
    """预检依赖的标准设备节点里，哪些缺失。

    这几个节点在任何正常 Linux 上都必然存在。它们由内核的 devtmpfs 创建，
    但**一旦被 rm 删掉，内核不会自动重建** —— 于是会静默缺很久，直到某个
    程序用到它才炸。2026-09-30 一次 chroot 的 `mount --bind /dev` 事故就
    清空过宿主机的整个 /dev（当时只补回了 /dev/null）。
    """
    return [p for p in ('/dev/urandom', '/dev/random', '/dev/zero', '/dev/tty')
            if not os.path.exists(p)]


def _verify(module, files):
    """写入临时文件后做语法预检。返回 (ok, msg)"""
    # 校验目录必须是一路 0755 都能 traverse 进来的：radvd 这类守护会降权后读配置，
    # 放在 0700 的 root 私有目录下连目录都进不去。同时避开 /tmp 与 /var/tmp ——
    # 它们带 sticky 位，配合 fs.protected_regular=2 会让 root 无法覆写低权用户
    # 留下的同名残留文件。/run 不对外开放写，安全。
    checkdir = VERIFY_DIR
    os.makedirs(checkdir, exist_ok=True)
    os.chmod(checkdir, 0o755)
    for path, content in files:
        # 个别守护还受 AppArmor 限制，只能读自己那几个固定路径 —— 见下方注释
        tmp = VERIFY_PATH.get(module) or os.path.join(checkdir, os.path.basename(path))
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(content)
        # chrony / radvd 等会以自身权限读取该文件，需放宽读权限
        os.chmod(tmp, 0o644)
        if module == 'dnsmasq':
            rc, _o, e = sh(['dnsmasq', '--test', '-C', tmp])
            if rc != 0:
                msg = (e or _o or '').strip()
                # dnsmasq 预检时要读 /dev/urandom 来播种随机数。机器的 /dev
                # 节点被误删时，它会抛 "failed to seed the random number
                # generator: 没有那个文件或目录" —— 这句话里完全没有 "dev"
                # 或 "设备" 的字样，用户只会以为是自己配置写错了。
                if 'seed the random number generator' in msg:
                    miss = _missing_dev_nodes()
                    return False, (
                        '配置语法检查未通过：dnsmasq 读不到随机数设备。'
                        '本机缺少 %s —— 这是**系统层面的 /dev 节点缺失**，'
                        '不是本次配置写错了（节点被删后内核不会自动重建，'
                        '重启机器或重新 mknod 即可恢复）。'
                        % ('、'.join(miss) if miss else '/dev/urandom'))
                return False, '配置语法检查未通过：%s' % (msg or '未知错误')
        elif module == 'radvd':
            rc, _o, e = sh(['radvd', '-c', '-C', tmp])
            if rc != 0:
                return False, 'RA 配置语法检查未通过：%s' % (e or _o or '未知错误')
        elif module in ('nft_v4', 'nft_v6'):
            rc, _o, e = sh(['nft', '-c', '-f', tmp])
            if rc != 0:
                return False, '防火墙规则语法检查未通过：%s' % (e or _o or '未知错误')
        elif module == 'chrony':
            rc, _o, e = sh(['chronyd', '-p', '-f', tmp])
            if rc != 0:
                return False, 'NTP 配置检查未通过：%s' % (e or _o or '未知错误')
        elif module == 'miniupnpd':
            # miniupnpd 支持 -c 检查配置
            if os.path.isfile('/usr/sbin/miniupnpd'):
                rc, _o, e = sh(['miniupnpd', '-c', '-f', tmp])
                if rc not in (0,):
                    # miniupnpd 版本差异较大，检查失败不阻断，仅记录
                    log('warn', 'config', 'MINIUPNPD_CHECK_SKIP',
                        'miniupnpd 配置预检未通过（已忽略，应用时会再次校验）：%s' % (e or _o))
        try:
            os.remove(tmp)
        except Exception:
            pass
    return True, '语法检查通过'


def read_render_text(p):
    """只读渲染预览：把某个模块的结构化配置渲染成配置文件文本，不写盘、不生效。

    参数 module（必填）+ cfg（可选，覆盖数据库中的配置）。
    用于「端口转发」等页面的规则预览。
    """
    p = p or {}
    module = str(p.get('module') or '').strip()
    if not module:
        return fail('未指定要预览的模块', 'NO_MODULE')
    cfg = p.get('cfg') or {}
    try:
        files = render.render(module, cfg)
    except ValidateError as e:
        return fail(getattr(e, 'msg_cn', str(e)), 'VALIDATE_FAIL', {'field': getattr(e, 'field', None)})
    except Exception as e:
        return fail('渲染失败：%s' % e, 'RENDER_FAIL')
    # files 可能是 str / dict / [(path, content)] / [{name, content}]
    if isinstance(files, str):
        text = files
    elif isinstance(files, dict):
        text = '\n'.join(str(v) for v in files.values())
    else:
        parts = []
        for f in files:
            if isinstance(f, (list, tuple)) and len(f) >= 2:
                parts.append('# ===== %s =====\n%s' % (f[0], f[1]))
            elif isinstance(f, dict):
                parts.append('# ===== %s =====\n%s' % (f.get('name', ''), f.get('content', '')))
            else:
                parts.append(str(f))
        text = '\n\n'.join(parts)
    return ok({'text': text, 'module': module})


def act_apply(p):
    """
    应用配置。支持两种模式：
      * check_only=True —— 仅做渲染 + 语法预检，不写盘、不启服务（安全，供前端"仅语法检查"使用）
      * live=True       —— 真正写入配置并重启服务（默认 False！必须显式开启）

    安全设计：默认 live=False。在构建/准备阶段，所有配置都只落盘不生效，
    避免 dnsmasq 抢占 53/67 端口影响正在运行的局域网。
    """
    module = str((p or {}).get('module') or '')
    cfg = (p or {}).get('cfg') or {}
    check_only = bool((p or {}).get('check_only'))
    live = bool((p or {}).get('live'))          # 默认 False
    if module in CONFIG_ONLY_MODULES:
        # 纯配置模块：配置由调用方（web.py 的 merge_cfg）落库即可，
        # 它没有独立配置文件，硬走渲染只会被 RENDERERS 拒掉。
        return ok({'files': [], 'applied': False, 'config_only': True},
                  '%s 的设置已保存（该模块没有独立配置文件，'
                  '其内容会作为上下文随防火墙 / DHCP 等模块一起生效）'
                  % CONFIG_ONLY_MODULES[module])
    if module not in APPLY_SPEC:
        return fail('未知的模块：%s' % module)
    svc, action, cn_name = APPLY_SPEC[module]
    # 语法预检与防火墙重载都按「渲染模块名」走：前端传 upnp / ntp / pppoe
    # 这类别名时，用原名比对会静默跳过语法检查（ntp 曾因此从不校验）。
    rmodule = render.canonical(module)
    # 1. 渲染（内部完成强校验，路径由渲染器决定，不接受外部传入路径）
    try:
        files = render.render(module, cfg)
    except ValidateError as e:
        log('error', 'config', 'VALIDATE_FAILED', '配置校验失败：%s' % e.msg_cn, {'module': module, 'field': e.field})
        return fail('配置校验失败：%s' % e.msg_cn, 'VALIDATE', {'field': e.field})
    # 2. 语法预检（按渲染模块名分支，别名的语法检查过去被整段跳过）
    good, msg = _verify(rmodule, files)
    if not good:
        log('error', 'config', 'VERIFY_FAILED', '%s：%s' % (cn_name, msg), {'module': module})
        return fail(msg, 'VERIFY')
    if check_only:
        log('info', 'config', 'CHECK_PASSED', '%s 语法检查通过（未写盘、未生效）' % cn_name, {'module': module})
        return ok({'checked': [f[0] for f in files], 'preview': [{'path': f[0], 'content': f[1]} for f in files],
                   'check_only': True},
                  '%s 语法检查通过（仅预检，未写入磁盘、未启用服务）' % cn_name)
    # 3. 快照（受「自动快照 → 应用前自动拍一张」策略控制）
    ts = ''
    try:
        if _load_auto().get('on_apply', True):
            ts, _d = _snapshot('auto-before-apply-%s' % module)
    except Exception:
        ts, _d = _snapshot('auto-before-apply-%s' % module)
    # 3b. 顺手做过期清理（避免快照无限堆积占满磁盘）
    try:
        ac = _load_auto()
        if ac.get('keep_days', 0) or ac.get('keep_count', 0):
            act_snapshot_prune({'keep_days': ac.get('keep_days', 0),
                                'keep_count': ac.get('keep_count', 0),
                                'keep_manual': ac.get('keep_manual', True)})
    except Exception:
        pass
    # 4. 原子写入
    written = []
    for path, content in files:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.drouter.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(content)
        if path.endswith(('chap-secrets', 'pap-secrets')):
            os.chmod(tmp, 0o600)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
        written.append(path)
    # 5. 生效（仅 live=True 时才动服务）
    note = ''
    if not live:
        note = '（配置已写入磁盘，服务未启动 —— 待你确认后再执行切换）'
        log('info', 'config', 'WRITTEN_ONLY', '%s 配置已落盘（未启用服务）' % cn_name,
            {'module': module, 'files': written, 'snapshot': ts})
        return ok({'files': written, 'snapshot': ts, 'live': False},
                  '%s 配置已保存到磁盘%s' % (cn_name, note))
    if action == 'restart':
        was_active = sh(['systemctl', 'is-active', svc])[1] == 'active'
        # 构建阶段禁止因 reload 而首次拉起服务，除非原本就在运行
        if not was_active and not bool((p or {}).get('allow_start')):
            log('warn', 'config', 'START_BLOCKED',
                '%s 当前未运行，已跳过启动（需显式 allow_start 才能拉起）' % cn_name, {'service': svc})
            return ok({'files': written, 'snapshot': ts, 'live': False, 'blocked': True},
                      '%s 配置已保存。该服务当前处于停止状态，已跳过启动以避免影响现有网络；'
                      '确认可以启用时请使用「启用服务」按钮。' % cn_name)
        rc, _o, e = sh(['systemctl', 'restart', svc], timeout=30)
        if rc != 0:
            log('error', svc, 'RESTART_FAILED', '%s 重载失败：%s' % (cn_name, e or '未知错误'))
            act_rollback({'ts': ts})
            return fail('%s 重载失败，已自动回滚配置：%s' % (cn_name, e or '未知错误'), 'RESTART')
        rc2, _o2, _e2 = sh(['systemctl', 'is-active', svc])
        note = '（服务状态：%s）' % _o2
    elif action == 'reload':
        rc, _o, e = sh(['nft', '-f', '/etc/nftables.d/drouter-%s.nft' % rmodule.split('_')[1]], timeout=30)
        if rc != 0:
            log('error', 'firewall', 'NFT_APPLY_FAILED', '防火墙规则应用失败：%s' % e)
            act_rollback({'ts': ts})
            return fail('防火墙规则应用失败，已自动回滚：%s' % (e or '未知错误'), 'APPLY')
        note = '（已热生效）'
    else:
        note = '（配置已写入，需你在确认后手动触发生效）'
    log('info', 'config', 'APPLIED', '%s 配置已应用%s' % (cn_name, note),
        {'module': module, 'files': written, 'snapshot': ts})
    return ok({'files': written, 'snapshot': ts, 'live': True},
              '%s 配置已应用%s' % (cn_name, note))


# ------------------------------------------------------------------ 服务控制

def act_service(p):
    name = str((p or {}).get('name') or '')
    op = str((p or {}).get('op') or '')
    if name not in SAFE_SERVICES:
        return fail('不允许操作的服务：%s' % name)
    if op not in ('start', 'stop', 'restart', 'enable', 'disable'):
        return fail('不允许的操作：%s' % op)
    rc, _o, e = sh(['systemctl', op, name], timeout=40)
    if rc != 0:
        log('error', 'service', 'SERVICE_OP_FAILED', '服务 %s %s 失败：%s' % (name, op, e))
        return fail('服务 %s %s 失败：%s' % (name, op, e or '未知错误'))
    log('info', 'service', 'SERVICE_OP', '服务 %s 已执行 %s' % (name, op))
    return ok({}, '服务 %s 已%s' % (name, op))


# ------------------------------------------------------------------ 电源

def act_power(p):
    op = str((p or {}).get('op') or '')
    if op == 'reboot':
        log('warn', 'power', 'REBOOT', '收到安全重启指令')
        subprocess.Popen(['/bin/systemctl', 'reboot'])
        return ok({}, '安全重启指令已下发，系统将在数秒内重启')
    if op == 'poweroff':
        log('warn', 'power', 'POWEROFF', '收到安全关机指令')
        subprocess.Popen(['/bin/systemctl', 'poweroff'])
        return ok({}, '安全关机指令已下发')
    if op == 'force_reboot':
        log('error', 'power', 'FORCE_REBOOT', '收到强制重启指令（sysrq b）')
        try:
            with open('/proc/sys/kernel/sysrq', 'w') as f:
                f.write('1')
            with open('/proc/sysrq-trigger', 'w') as f:
                f.write('b')
        except Exception as e:
            return fail('强制重启失败：%s' % e)
        return ok({}, '强制重启已触发')
    return fail('不支持的电源操作：%s' % op)


# ------------------------------------------------------------------ 用户与 SSH 公钥

PUBKEY_RE = re.compile(r'^(ssh-(?:rsa|ed25519|dss)|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)\s+[A-Za-z0-9+/=]{20,}')


def _valid_username(n):
    return bool(re.match(r'^[a-z_][a-z0-9_-]{0,31}$', n or ''))


def _pw_reject(pw):
    """chpasswd 是按**行**处理的 —— 密码里带换行就能顺带改掉别的账号的口令。

    典型利用：`op=passwd name=x password="123456\\nroot:Passw0rd"`
    交给 chpasswd 的文本变成两行，root 口令被一起改写。helper 是 root 权限，
    所以这里必须挡住换行与所有控制字符。
    """
    if any(ch in pw for ch in ('\n', '\r', '\x00')):
        return '密码不能包含换行符'
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in pw):
        return '密码不能包含控制字符'
    return ''


def _atomic_write(path, text, mode=0o644):
    """原子写：先写同目录临时文件，再 os.replace 覆盖。

    直接 `open(path,'w')` 会在断电 / 被 kill 的瞬间留下空文件或半截内容，
    本项目其他地方（_share_apply_file、_ca_write 等）早就用这套写法了，
    这里是把它统一成一个函数，给那些还没跟上的位置用。
    """
    d = os.path.dirname(path) or '.'
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.dw-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def act_user(p):
    op = str((p or {}).get('op') or '')
    name = str((p or {}).get('name') or '')
    if op in ('add', 'del', 'passwd', 'key_add', 'key_del', 'lock', 'unlock'):
        if not _valid_username(name):
            return fail('用户名不合法（只允许小写字母、数字、下划线、连字符）')

    if op == 'add':
        if any(u.pw_name == name for u in pwd.getpwall()):
            return fail('用户 %s 已存在' % name)
        rc, _o, e = sh(['useradd', '-m', '-s', '/bin/bash', name])
        if rc != 0:
            return fail('创建用户失败：%s' % e)
        pw = str((p or {}).get('password') or '')
        if pw:
            bad = _pw_reject(pw)
            if bad:
                return fail('用户已创建但设置密码失败：%s' % bad)
            rc2, _o2, e2 = sh(['chpasswd'], input_data='%s:%s\n' % (name, pw))
            if rc2 != 0:
                return fail('用户已创建但设置密码失败：%s' % e2)
        # 加入 sudo 组（可选）
        if (p or {}).get('sudo'):
            sh(['usermod', '-aG', 'sudo', name])
        log('info', 'user', 'USER_ADD', '已创建系统用户 %s' % name)
        return ok({}, '系统用户 %s 创建成功' % name)

    if op == 'del':
        # 不只保护两个写死的名字：任何 UID < 1000 的都是系统账号，
        # 删掉 drouter 自己的服务账号面板会当场起不来（只能上控制台救）。
        try:
            entry = pwd.getpwnam(name)
        except KeyError:
            return fail('用户不存在：%s' % name)
        if entry.pw_uid < 1000:
            return fail('受保护的系统账号，禁止删除：%s（UID %d）'
                        % (name, entry.pw_uid))
        rc, _o, e = sh(['userdel', '-r', name])
        if rc != 0:
            return fail('删除用户失败：%s' % e)
        log('warn', 'user', 'USER_DEL', '已删除系统用户 %s' % name)
        return ok({}, '系统用户 %s 已删除' % name)

    if op == 'passwd':
        pw = str((p or {}).get('password') or '')
        if len(pw) < 6:
            return fail('密码长度至少 6 位')
        bad = _pw_reject(pw)
        if bad:
            return fail(bad)
        rc, _o, e = sh(['chpasswd'], input_data='%s:%s\n' % (name, pw))
        if rc != 0:
            return fail('修改密码失败：%s' % e)
        log('warn', 'user', 'USER_PASSWD', '已修改系统用户 %s 的密码' % name)
        return ok({}, '用户 %s 密码已更新' % name)

    if op in ('lock', 'unlock'):
        rc, _o, e = sh(['usermod', '-L' if op == 'lock' else '-U', name])
        if rc != 0:
            return fail('操作失败：%s' % e)
        return ok({}, '用户 %s 已%s' % (name, '锁定' if op == 'lock' else '解锁'))

    if op == 'key_add':
        keys = (p or {}).get('keys') or []
        if isinstance(keys, str):
            keys = [keys]
        if not keys:
            return fail('没有提供公钥内容')
        bad = []
        good = []
        for k in keys:
            k = (k or '').strip()
            if not k:
                continue
            if k.startswith('ssh-') or ' ' in k:
                if not PUBKEY_RE.match(k):
                    bad.append(k[:40])
                    continue
                good.append(k)
            elif k.lower().endswith('.pub'):
                bad.append(k)
            else:
                bad.append(k[:40])
        if bad and not good:
            return fail('公钥格式不正确。请上传 .pub 公钥文件内容（以 ssh-rsa / ssh-ed25519 等开头）。'
                        '注意：私钥（如 -----BEGIN OPENSSH PRIVATE KEY-----）不应上传到服务器。')
        try:
            u = pwd.getpwnam(name)
        except KeyError:
            return fail('用户不存在：%s' % name)
        sshdir = os.path.join(u.pw_dir, '.ssh')
        os.makedirs(sshdir, exist_ok=True)
        os.chmod(sshdir, 0o700)
        kf = os.path.join(sshdir, 'authorized_keys')
        exist = set()
        if os.path.isfile(kf):
            with open(kf, encoding='utf-8', errors='replace') as f:
                exist = {ln.strip() for ln in f if ln.strip()}
        exist.update(good)
        with open(kf, 'w', encoding='utf-8') as f:
            f.write('\n'.join(sorted(exist)) + '\n')
        os.chmod(kf, 0o600)
        os.chown(kf, u.pw_uid, u.pw_gid)
        os.chown(sshdir, u.pw_uid, u.pw_gid)
        log('info', 'user', 'SSH_KEY_ADD', '已为用户 %s 写入 %s 个公钥' % (name, len(good)))
        msg = '已写入 %s 个公钥到 %s' % (len(good), kf)
        if bad:
            msg += '；另有 %s 条内容被拒绝（格式不符）' % len(bad)
        return ok({'added': len(good), 'rejected': len(bad), 'path': kf}, msg)

    if op == 'key_del':
        key = str((p or {}).get('key') or '').strip()
        try:
            u = pwd.getpwnam(name)
        except KeyError:
            return fail('用户不存在：%s' % name)
        kf = os.path.join(u.pw_dir, '.ssh', 'authorized_keys')
        if not os.path.isfile(kf):
            return fail('该用户还没有公钥文件')
        with open(kf, encoding='utf-8', errors='replace') as f:
            lines = [ln.rstrip('\n') for ln in f]
        remain = [ln for ln in lines if ln.strip() and ln.strip() != key]
        with open(kf, 'w', encoding='utf-8') as f:
            f.write('\n'.join(remain) + ('\n' if remain else ''))
        os.chmod(kf, 0o600)
        os.chown(kf, u.pw_uid, u.pw_gid)
        log('warn', 'user', 'SSH_KEY_DEL', '已删除用户 %s 的一个公钥' % name)
        return ok({}, '公钥已删除')

    return fail('不支持的用户操作：%s' % op)


# ------------------------------------------------------------------ 诊断工具

def _safe_host(h):
    h = (h or '').strip()
    if not h:
        raise ValidateError('目标地址不能为空', 'target')
    if len(h) > 253:
        raise ValidateError('目标地址过长')
    if re.search(r'[;&|`$<>()\n\r\'"]', h):
        raise ValidateError('目标地址包含非法字符')
    return h


def act_diag(p):
    tool = str((p or {}).get('tool') or '')
    try:
        if tool == 'ping':
            host = _safe_host((p or {}).get('target'))
            count = v_int((p or {}).get('count') or 4, field='次数', lo=1, hi=20)
            rc, out, err = sh(['ping', '-c', str(count), '-W', '3', host], timeout=40)
            return ok({'rc': rc, 'out': out, 'err': err}, 'Ping 测试完成')
        if tool == 'traceroute':
            host = _safe_host((p or {}).get('target'))
            rc, out, err = sh(['traceroute', '-w', '2', '-m', '15', host], timeout=60)
            return ok({'rc': rc, 'out': out, 'err': err}, '路由追踪完成')
        if tool == 'mtr':
            host = _safe_host((p or {}).get('target'))
            rc, out, err = sh(['mtr', '-r', '-c', '5', '--no-dns', host], timeout=70)
            return ok({'rc': rc, 'out': out, 'err': err}, 'MTR 测试完成')
        if tool == 'iperf':
            host = _safe_host((p or {}).get('target'))
            rc, out, err = sh(['iperf3', '-c', host, '-t', '5', '-J'], timeout=60)
            return ok({'rc': rc, 'out': out, 'err': err}, 'iperf3 吞吐测试完成')
        if tool == 'iperf_server':
            rc, out, err = sh(['iperf3', '-s', '-D', '-1'], timeout=15)
            return ok({'rc': rc, 'out': out, 'err': err}, 'iperf3 服务端已启动（一次性）')
        if tool == 'speedtest':
            # 通过下载测速文件估算下行带宽，不依赖外部 CLI
            url = 'https://speed.cloudflare.com/__down?bytes=10000000'
            rc, out, err = sh(['curl', '-s', '-o', '/dev/null', '-w',
                               '%{speed_download} %{time_total} %{size_download}',
                               '--max-time', '25', url], timeout=35)
            if rc != 0:
                return fail('互联网测速失败：%s' % (err or '网络不可达'))
            parts = out.split()
            bps = float(parts[0]) if parts else 0
            return ok({'bytes_per_sec': bps, 'mbps': round(bps * 8 / 1e6, 2),
                       'seconds': parts[1] if len(parts) > 1 else '',
                       'bytes': parts[2] if len(parts) > 2 else ''},
                      '互联网测速完成：约 %.2f Mbps' % (bps * 8 / 1e6))
        if tool == 'conntrack':
            rc, out, err = sh(['conntrack', '-L', '-o', 'extended'], timeout=20)
            return ok({'rc': rc, 'out': out, 'err': err}, '连接跟踪查询完成')
        if tool == 'tcpdump':
            iface = v_ifname((p or {}).get('iface') or 'br0')
            count = v_int((p or {}).get('count') or 50, field='抓包数量', lo=1, hi=500)
            rc, out, err = sh(['tcpdump', '-n', '-i', iface, '-c', str(count), '-q'], timeout=40)
            return ok({'rc': rc, 'out': out, 'err': err}, '抓包完成（最多 %s 个包）' % count)
        if tool == 'dns':
            host = _safe_host((p or {}).get('target'))
            rc, out, err = sh(['dig', '+short', host], timeout=15)
            return ok({'rc': rc, 'out': out, 'err': err}, 'DNS 解析完成')
        if tool == 'ipv6test':
            return _diag_ipv6test((p or {}).get('which') or 'all')
    except ValidateError as e:
        return fail(e.msg_cn)
    return fail('不支持的诊断工具：%s' % tool)


def _diag_ipv6test(which):
    """
    IPv6 连通性测试：用 curl 强制走 IPv6 访问测试站点，返回逐项结果。
    国内：testipv6.cn    国际：testipv6.com
    """
    targets = []
    if which in ('cn', 'all'):
        targets.append({'key': 'cn', 'name': '国内 IPv6 测试', 'target': 'testipv6.cn',
                        'url': 'https://testipv6.cn/'})
    if which in ('intl', 'all'):
        targets.append({'key': 'intl', 'name': '国际 IPv6 测试', 'target': 'testipv6.com',
                        'url': 'https://testipv6.com/'})
    if not targets:
        return fail('不支持的测试类型：%s' % which)
    # 先确认本机是否有可用公网 IPv6
    rc0, out0, _e0 = sh(['ip', '-6', 'route', 'show', 'default'], timeout=6)
    has_v6 = rc0 == 0 and 'default' in (out0 or '')
    results = []
    for t in targets:
        item = {'key': t['key'], 'name': t['name'], 'target': t['target'],
                'ok': False, 'ms': None, 'detail': ''}
        if not has_v6:
            item['detail'] = '本机没有可用的 IPv6 默认路由，测试无法进行。\n请先在「IPv6 / RA」「DHCPv6 / 前缀」页面确认已获取公网 IPv6。'
            results.append(item)
            continue
        # -6 强制 IPv6；-w 输出状态码与耗时；-s 静默；-L 跟随跳转；--max-time 限时
        fmt = '%{http_code} %{time_total} %{remote_ip} %{url_effective}'
        rc, out, err = sh(['curl', '-6', '-s', '-o', '/dev/null', '-L',
                           '-w', fmt, '--max-time', '20',
                           '-A', 'Mozilla/5.0 (drouter IPv6 test)', t['url']], timeout=30)
        parts = (out or '').strip().split()
        if rc != 0:
            item['detail'] = ('通过 IPv6 访问 %s 失败。\n可能原因：本机无公网 IPv6、'
                              '运营商未下发 PD、或当前网络不支持 IPv6。\n'
                              '原始信息：%s' % (t['target'], (err or '').strip() or '连接超时'))
        else:
            code = parts[0] if parts else ''
            try:
                item['ms'] = int(round(float(parts[1]) * 1000)) if len(parts) > 1 else None
            except Exception:
                item['ms'] = None
            remote = parts[2] if len(parts) > 2 else ''
            item['ok'] = bool(code) and code not in ('000', '')
            item['detail'] = ('HTTP 状态码 %s，远端 IPv6 地址 %s\n目标：%s'
                              % (code or '无', remote or '未知', t['target']))
        results.append(item)
    good = sum(1 for x in results if x['ok'])
    if not has_v6:
        msg = '未检测到公网 IPv6，测试未通过'
    elif good == len(results):
        msg = 'IPv6 测试全部通过（%d/%d）' % (good, len(results))
    elif good > 0:
        msg = 'IPv6 部分连通（%d/%d），部分站点不可达' % (good, len(results))
    else:
        msg = 'IPv6 测试未通过，请检查运营商是否下发 IPv6'
    return ok({'results': results, 'has_ipv6': has_v6, 'passed': good, 'total': len(results)},
              msg)


# ------------------------------------------------------------------ 系统更新（谨慎模式）

def act_pkg(p):
    op = str((p or {}).get('op') or '')
    if op == 'update':
        rc, out, err = sh(['apt-get', 'update'], timeout=180)
        return ok({'out': out[-3000:], 'err': err[-2000:]}, '软件源索引已更新' if rc == 0 else '更新索引失败：%s' % err)
    if op == 'list':
        rc, out, err = sh(['apt', 'list', '--upgradable'], timeout=120)
        rows = []
        risky = []
        for line in out.splitlines()[1:]:
            if '/' not in line:
                continue
            name = line.split('/')[0]
            rows.append(line)
            if re.match(r'^(xserver-xorg|xorg|linux-image|linux-headers|xfce4|realvnc|vnc)', name):
                risky.append(name)
        return ok({'rows': rows, 'risky': risky,
                   'notice': '标记为高危的包（%s 个）可能影响桌面/RealVNC，升级前请先创建快照'
                             % len(risky)})
    if op == 'upgrade':
        names = (p or {}).get('packages') or []
        names = [re.sub(r'[^a-zA-Z0-9.+-]', '', str(n)) for n in names][:50]
        names = [n for n in names if n]
        # 早先不判空：`apt-get install --only-upgrade`（不带包名）会**成功返回 0**
        # 并打印「0 upgraded」—— 于是界面弹「升级完成 + 已建快照」，其实一个包都没升。
        if not names:
            return fail('没有指定要升级的软件包', 'EMPTY')
        ts, _d = _snapshot('before-upgrade')
        cmd = ['apt-get', 'install', '-y', '--only-upgrade'] + names
        rc, out, err = sh(cmd, timeout=900)
        # apt 输出为 0 不代表真升级了（比如包已是最新 / 名字不存在会被静默忽略）
        low = (out or '').lower()
        upgraded = 0
        m = re.search(r'(\d+)\s+upgraded', low)
        if m:
            upgraded = int(m.group(1))
        log('warn', 'pkg', 'UPGRADE', '已执行升级：%s' % ','.join(names),
            {'snapshot': ts, 'upgraded': upgraded})
        if rc != 0:
            return fail('升级失败：%s' % err[-500:])
        return ok({'out': out[-4000:], 'err': err[-2000:], 'snapshot': ts,
                   'upgraded': upgraded},
                  ('升级完成：%d 个包已更新（快照 %s，如桌面异常可回滚）'
                   % (upgraded, ts)) if upgraded else
                  ('apt 结束了但没有包被升级（可能已是最新，或包名不对）—— 未做任何改动。'
                   '快照 %s 已留存。' % ts))
    if op in ('hold', 'unhold'):
        names = (p or {}).get('packages') or []
        names = [re.sub(r'[^a-zA-Z0-9.+-]', '', str(n)) for n in names][:50]
        cmd = ['apt-mark', op] + names
        rc, out, err = sh(cmd, timeout=60)
        return ok({'out': out}, '已%s %s 个包' % ('锁定' if op == 'hold' else '解锁', len(names))
                  if rc == 0 else '操作失败：%s' % err)
    if op == 'holds':
        rc, out, err = sh(['apt-mark', 'showhold'])
        return ok({'holds': out.splitlines()})
    return fail('不支持的包管理操作：%s' % op)


# ------------------------------------------------------------------ Web 管理端口

def act_web_port_set(p):
    """修改 Web 管理 HTTPS 端口：写入 /etc/drouter/web-port 并重启 Web 服务"""
    p = p or {}
    try:
        port = int(p.get('port'))
    except Exception:
        return fail('端口必须是数字')
    if not (1 <= port <= 65535):
        return fail('端口必须是 1-65535 的数字')
    # 明显会冲突的服务端口
    busy = {80: 'HTTP', 8080: 'drouter 明文 HTTP', 53: 'DNS(dnsmasq)',
            67: 'DHCP(dnsmasq)', 68: 'DHCP 客户端', 5900: 'RealVNC'}
    if port in busy:
        return fail('端口 %d 已被 %s 占用，请换一个' % (port, busy[port]))
    # 真实占用检测：端口一旦写进去而服务起不来，面板就再也打不开了，
    # 只能上控制台改回 /etc/drouter/web-port —— 所以这里要真的查一遍监听表。
    rc0, out0, _e0 = sh(['ss', '-lntH'], timeout=8)
    for line in (out0 or '').splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        addr = parts[3]
        pstr = addr.rsplit(':', 1)[-1] if ':' in addr else ''
        try:
            cur = int(pstr)
        except Exception:
            continue
        if cur == port:
            return fail('端口 %d 已经被系统上的服务占用（%s），请换一个'
                        % (port, ' '.join(parts[:5])), 'BUSY')
    os.makedirs('/etc/drouter', exist_ok=True)
    _atomic_write('/etc/drouter/web-port', '%d\n' % port)
    # 重启 Web 服务（systemd 单元名 drouter-web）
    rc, out, err = sh(['systemctl', 'restart', 'drouter-web'], timeout=30)
    log('warn', 'web', 'WEB_PORT_SET', 'Web 管理端口已改为 %d' % port, {'rc': rc, 'err': err})
    if rc != 0:
        return ok({'port': port, 'restarted': False},
                  '端口已保存为 %d，但服务重启失败，请稍后手动重启 drouter-web' % port)
    return ok({'port': port, 'restarted': True},
              'Web 管理端口已改为 %d，请用新地址访问：https://<本机IP>:%d/' % (port, port))


# ------------------------------------------------------------------ DHCP 租约管理

def _load_setting(key, default=None):
    """读取 /opt/drouter/data/drouter.db 中 settings 表的一项（JSON）"""
    try:
        import sqlite3
        conn = sqlite3.connect('/opt/drouter/data/drouter.db')
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        conn.close()
        if r and r['value']:
            return json.loads(r['value'])
    except Exception:
        pass
    return default if default is not None else {}


def _save_setting(key, val):
    """写入 settings 表（结构与 drouter-web 保持一致：仅 key/value 两列）"""
    import sqlite3
    conn = sqlite3.connect('/opt/drouter/data/drouter.db')
    cols = [r[1] for r in conn.execute('PRAGMA table_info(settings)').fetchall()]
    if not cols:
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        cols = ['key', 'value']
    if 'updated_at' in cols:
        conn.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                     (key, json.dumps(val, ensure_ascii=False), datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    else:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(val, ensure_ascii=False)))
    conn.commit()
    conn.close()


def _valid_mac(m):
    return bool(re.match(r'^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$', str(m or '').strip()))


def act_lease_make_static(p):
    """把当前动态租约一键转为静态绑定（写入 dnsmasq static_leases 并重载）"""
    p = p or {}
    mac = str(p.get('mac') or '').strip().lower()
    ip = str(p.get('ip') or '').strip()
    host = str(p.get('host') or '').strip()
    if not _valid_mac(mac):
        return fail('MAC 地址格式不正确')
    if not re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
        return fail('IP 地址格式不正确')
    cfg = _load_setting('dnsmasq', {}) or {}
    lst = cfg.get('static_leases') or []
    # 已存在的同 MAC 记录则覆盖
    lst = [x for x in lst if str(x.get('mac', '')).lower() != mac]
    # 同 IP 的记录也移除，避免冲突
    lst = [x for x in lst if x.get('ip') != ip]
    # 字段名必须和 render_dnsmasq 读的一致：那边读 enabled + name，
    # 这里原来写的是 host 且没有 enabled —— 结果是「转为静态」提示成功、
    # _save_setting 也写盘了，但渲染时整条被 v_bool(enabled) 过滤掉，
    # dnsmasq 里一条 dhcp-host 都没有，绑定静默失效。
    lst.append({'enabled': True, 'mac': mac, 'ip': ip, 'name': host})
    cfg['static_leases'] = lst
    _save_setting('dnsmasq', cfg)
    log('info', 'dhcp', 'LEASE_STATIC', '租约转为静态绑定 %s → %s' % (mac, ip), {'host': host})
    return ok({'mac': mac, 'ip': ip}, '已把 %s 转为静态绑定（%s）' % (ip, host or mac))


def _dnsmasq_listen_ifaces():
    """dnsmasq 当前真正监听的网卡列表（给 `dhcp_release` 用）。

    dhcp_release 必须指定网卡，猜错会失败，所以先看 dnsmasq 的命令行参数，
    拿不到再退回「确实存在的常见 LAN 口」。
    """
    out = []
    try:
        rc, o, _e = sh(['pgrep', '-a', 'dnsmasq'], timeout=6)
        toks = (o or '').split()
        for i, t in enumerate(toks):
            if t.startswith('--interface='):
                out.append(t.split('=', 1)[1])
            elif t in ('-i', '--interface') and i + 1 < len(toks):
                out.append(toks[i + 1])
    except Exception:
        pass
    for cand in ('br0', 'lan0', 'ens18'):
        if cand not in out and os.path.exists('/sys/class/net/' + cand):
            out.append(cand)
    seen = set()
    return [x for x in out if re.match(r'^[a-zA-Z0-9_.:-]{1,15}$', x)
            and not (x in seen or seen.add(x))]


def act_lease_release(p):
    """释放并回收 IP 回地址池（删除 dnsmasq 租约记录 + 通知客户端）"""
    p = p or {}
    ip = str(p.get('ip') or '').strip()
    mac = str(p.get('mac') or '').strip().lower()
    if not re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
        return fail('IP 地址格式不正确')
    if not _valid_mac(mac):
        return fail('回收租约需要正确的 MAC 地址（aa:bb:cc:dd:ee:ff）')
    lease_file = '/var/lib/misc/dnsmasq.leases'
    removed = 0
    if os.path.isfile(lease_file):
        try:
            with open(lease_file, encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
            keep = []
            for ln in lines:
                sp = ln.split()
                if len(sp) >= 3 and sp[2] == ip:
                    removed += 1
                    continue
                keep.append(ln)
            if removed:
                tmp = lease_file + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.writelines(keep)
                os.replace(tmp, lease_file)
        except Exception as e:
            return fail('写入租约文件失败：%s' % e)
    # 真正的回收要靠 dnsmasq 提供的 dhcp_release：它会同时从**内存里的租约表**
    # 删记录并向该客户端发 DHCPRELEASE/强制下线。早先只改文件 + kill -HUP，
    # dnsmasq 会按自己的内存表把文件重写回来 —— 界面提示「已回收」其实是假的，
    # 客户端一续租 IP 就回来了。
    released = False
    iface = ''
    for cand in _dnsmasq_listen_ifaces():
        # dhcp_release <interface> <address> <MAC> [client_id]
        rc, _o, _e = sh(['dhcp_release', cand, ip, mac], timeout=10)
        if rc == 0:
            released, iface = True, cand
            break
    if not released:
        # 没有 dhcp_release 就退回到「改文件 + HUP」，并把话说明白
        sh(['sh', '-c', 'kill -HUP $(pidof dnsmasq) 2>/dev/null || true'], timeout=8)
    log('info', 'dhcp', 'LEASE_RELEASE', '释放并回收 DHCP 租约 %s' % ip,
        {'mac': mac, 'removed': removed, 'released': released, 'iface': iface})
    if released:
        return ok({'ip': ip, 'removed': removed, 'released': True},
                  '已回收 %s：dnsmasq 已从租约表删除该地址（客户端需重新获取）' % ip)
    return ok({'ip': ip, 'removed': removed, 'released': False},
              '已从租约文件删除 %s 的记录，但本机没有 dhcp_release 工具 —— '
              'dnsmasq 内存里的租约还在，客户端续租时可能拿回同一个地址。'
              '要彻底回收请安装 dnsmasq-utils 或重启 dnsmasq。' % ip)


# ------------------------------------------------------------------ 迁移 / 切换

def act_migrate_networkd(p):
    """
    把 NetworkManager 管理的连接迁移为 systemd-networkd 配置（高危，需用户确认）。
    dry_run=True 时只输出将要写入的文件，不做任何修改。
    """
    dry = bool((p or {}).get('dry_run', True))
    cfg = (p or {}).get('cfg') or {}
    files = render.render('network', cfg)
    if dry:
        return ok({'files': [{'name': f[0].split('/')[-1], 'path': f[0], 'content': f[1]} for f in files],
                   'dry_run': True},
                  '已生成迁移预览（未写入磁盘，未停 NetworkManager）')
    ts, _d = _snapshot('before-migrate-networkd')
    for path, content in files:
        _atomic_write(path, content)
    # 清除 /etc/network/interfaces 中可能冲突的段落（该文件当前只有 lo，跳过）
    sh(['systemctl', 'disable', 'NetworkManager'])
    sh(['systemctl', 'enable', 'systemd-networkd'])
    rc, _o, e = sh(['systemctl', 'restart', 'systemd-networkd'], timeout=40)
    log('warn', 'network', 'MIGRATE_NETWORKD', '已完成网络后端迁移（快照 %s）' % ts, {'rc': rc})
    return ok({'snapshot': ts, 'rc': rc},
              '已迁移到 systemd-networkd 并重启网络服务；如失联请在 PVE 控制台执行回滚：%s' % ts)


def act_ppp_control(p):
    op = str((p or {}).get('op') or '')
    if op not in ('connect', 'disconnect', 'status'):
        return fail('不支持的拨号操作')
    if op == 'status':
        rc, out, _e = sh(['ip', '-j', 'addr', 'show', 'ppp0'])
        up = rc == 0 and 'ppp0' in out
        rc2, out2, _e = sh(['cat', '/var/log/drouter/pppoe.jsonl'])
        return ok({'up': up, 'addr': out if up else '', 'recent_log': out2.splitlines()[-20:]})
    rc, out, err = sh(['pon', 'drouter-wan'] if op == 'connect' else ['poff', 'drouter-wan'], timeout=40)
    log('warn', 'pppoe', 'PPP_' + op.upper(), 'PPPoE %s 已执行' % op, {'rc': rc, 'err': err})
    return ok({'rc': rc, 'err': err}, 'PPPoE %s 指令已执行' % ('连接' if op == 'connect' else '断开'))


def act_remove_legacy(p):
    """彻底移除旧的 landscape-router（分阶段：disable → stop → delete）"""
    stage = str((p or {}).get('stage') or '')
    if stage == 'disable':
        sh(['systemctl', 'disable', 'landscape-router'])
        return ok({}, '已禁止 landscape-router 开机自启（服务仍在运行）')
    if stage == 'stop':
        rc, _o, e = sh(['systemctl', 'stop', 'landscape-router'], timeout=40)
        if rc != 0:
            return fail('停止失败：%s' % e)
        log('warn', 'system', 'LEGACY_STOPPED', '旧路由系统 landscape-router 已停止')
        return ok({}, 'landscape-router 已停止（文件保留，可恢复）')
    if stage == 'delete':
        if not bool((p or {}).get('confirm')):
            return fail('删除操作需要显式确认')
        # 先确认已备份
        rc, out, _e = sh(['ls', '-d', '/root/.backup/landscape-router-*'])
        if rc != 0 or not out:
            return fail('未找到备份，已取消删除（请先备份 /root/.landscape-router）')
        sh(['systemctl', 'disable', '--now', 'landscape-router'])
        sh(['rm', '-f', '/etc/systemd/system/landscape-router.service'])
        sh(['systemctl', 'daemon-reload'])
        sh(['rm', '-rf', '/root/.landscape-router'])
        log('warn', 'system', 'LEGACY_DELETED', '旧路由系统 landscape-router 已彻底删除', {'backup': out.splitlines()[-1]})
        return ok({'backup': out.splitlines()}, 'landscape-router 已彻底删除，备份保留在：%s' % out.splitlines()[-1])
    if stage == 'status':
        rc, st, _e = sh(['systemctl', 'is-active', 'landscape-router'])
        exist = os.path.isdir('/root/.landscape-router')
        rc2, bu, _e = sh(['ls', '-d', '/root/.backup/landscape-router-*'])
        return ok({'active': st, 'dir_exists': exist, 'backups': bu.splitlines() if rc2 == 0 else []})
    return fail('未知阶段：%s' % stage)


# ------------------------------------------------------------------ action 路由

# ------------------------------------------------------------------ NAT 类型检测
#
# 思路：不依赖任何外部私有服务，使用公开的 STUN 服务器做"两次映射比对"。
#   1) 向 STUN 服务器发 Binding Request，拿到本机在公网侧的映射地址 (X:x)
#   2) 换一个 STUN 服务器/端口再发一次，拿到映射 (Y:y)
#   3) 同时读取本机出口的本地地址 (L:l) 和公网 IP (P:p)
# 判定标准（与 RFC 3489 的经典分类一致）：
#   - 没有 NAT（公网直连）：映射地址 == 本地地址
#   - Full Cone (NAT1)：换服务器后映射端口不变 (y == x)
#   - Restricted Cone (NAT2)：换服务器后映射端口变化，但公网 IP 不变
#   - Port Restricted (NAT3)：映射地址两端都变，但公网 IP 仍不变
#   - Symmetric (NAT4)：每个目标得到不同的公网 IP:端口
# 本实现只用公开 DNS/STUN，不做端口扫描、不连接用户未授权的目标。

STUN_SERVERS = [
    ('stun.miwifi.com', 3478),
    ('stun.qq.com', 3478),
    ('stun.chat.bilibili.com', 3478),
    ('stun.cloudflare.com', 3478),
    ('stun.l.google.com', 19302),
]
STUN_MAGIC = 0x2112A442


def _stun_bind(host, port, timeout=3.0):
    """
    向 STUN 服务器发一个 Binding Request，解析 XOR-MAPPED-ADDRESS。
    返回 (mapped_ip, mapped_port, changed_ip, changed_port) 或 None。
    """
    try:
        import struct as _st
        import random as _rnd
        txid = bytes(_rnd.getrandbits(8) for _ in range(12))
        # Header: type(2) length(2) magic(4) txid(12)；无属性
        pkt = _st.pack('>HHI', 0x0001, 0, STUN_MAGIC) + txid
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        s.sendto(pkt, (host, port))
        data, _addr = s.recvfrom(2048)
        s.close()
        if len(data) < 20:
            return None
        # 校验事务 ID 一致
        if data[4:8] != _st.pack('>I', STUN_MAGIC):
            return None
        off = 20
        mapped = changed = None
        total = _st.unpack('>H', data[2:4])[0]
        while off + 4 <= 20 + total and off + 4 <= len(data):
            atype, alen = _st.unpack('>HH', data[off:off + 4])
            val = data[off + 4:off + 4 + alen]
            off += 4 + alen + ((4 - alen % 4) % 4)
            if atype in (0x0020, 0x0001) and len(val) >= 8 and not mapped:  # XOR-MAPPED / MAPPED
                fam = val[1]
                if fam != 0x01:
                    continue
                prt = _st.unpack('>H', val[2:4])[0]
                ipb = val[4:8]
                if atype == 0x0020:  # XOR 解码
                    prt ^= (STUN_MAGIC >> 16) & 0xFFFF
                    ipb = bytes(a ^ b for a, b in zip(ipb, _st.pack('>I', STUN_MAGIC)))
                mapped = (socket.inet_ntoa(ipb), prt)
            elif atype in (0x0021, 0x0005) and len(val) >= 8 and not changed:  # XOR/CHANGED-ADDRESS
                fam = val[1]
                if fam != 0x01:
                    continue
                prt = _st.unpack('>H', val[2:4])[0]
                ipb = val[4:8]
                if atype == 0x0021:
                    prt ^= (STUN_MAGIC >> 16) & 0xFFFF
                    ipb = bytes(a ^ b for a, b in zip(ipb, _st.pack('>I', STUN_MAGIC)))
                changed = (socket.inet_ntoa(ipb), prt)
        if not mapped:
            return None
        return mapped[0], mapped[1], (changed or ('', 0))[0], (changed or ('', 0))[1]
    except Exception:
        return None


def _local_out_ip():
    """取本机用于出网的本地地址（不发包，纯路由查询）。"""
    out = ''
    try:
        rc, o, _e = sh(['ip', '-o', '-4', 'route', 'get', '1.1.1.1'], timeout=6)
        m = re.search(r'src\s+(\d+\.\d+\.\d+\.\d+)', o or '')
        if m:
            out = m.group(1)
    except Exception:
        pass
    if not out:
        try:
            rc, o, _e = sh(['ip', '-4', '-o', 'addr', 'show', 'scope', 'global'], timeout=6)
            m = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)', o or '')
            if m:
                out = m.group(1)
        except Exception:
            pass
    return out


def _is_private(ip):
    try:
        import ipaddress
        a = ipaddress.IPv4Address(ip)
        return a.is_private or a.is_loopback or a.is_link_local
    except Exception:
        return False


def act_nat_check(p):
    """
    自动探测所处 NAT 类型，给出通俗中文结论。
    op=get  → 只读缓存/最近一次结果
    op=run  → 立即执行探测（默认）
    """
    op = str((p or {}).get('op') or 'run')
    local_ip = _local_out_ip()
    samples = []
    used = []
    for host, port in STUN_SERVERS:
        r = _stun_bind(host, port)
        if r:
            samples.append({'server': '%s:%d' % (host, port), 'ip': r[0], 'port': r[1]})
            used.append(host)
        if len(samples) >= 3:
            break

    detail = {
        'local_ip': local_ip,
        'local_private': _is_private(local_ip) if local_ip else None,
        'probes': samples,
        'servers_tried': [s[0] for s in STUN_SERVERS],
        'servers_used': used,
    }

    if not samples:
        nat = 'NA'
        title = '无法判定'
        desc = ('所有 STUN 探测都没有返回结果。常见原因：当前网络禁止 UDP 出网、'
                '上游只放行 TCP，或者路由器还没接入互联网。接入宽带后重新检测即可。')
        detail['reason'] = 'no_stun_response'
    else:
        pub_ip = samples[0]['ip']
        pub_port = samples[0]['port']
        same_ip = all(s['ip'] == pub_ip for s in samples)
        same_port = all(s['port'] == pub_port for s in samples)

        if local_ip and pub_ip == local_ip:
            nat = 'NONE'
            title = '公网直连（无 NAT）'
            desc = '本机地址与公网映射地址完全一致，说明你是直接暴露在公网上的。'
        elif same_ip and same_port and len(samples) >= 2:
            nat = 'NAT1'
            title = 'NAT1 · 完全锥形（Full Cone）'
            desc = ('兼容性最好的一类 NAT。任何外部主机都能通过这个公网映射端口'
                    '反向访问到你，适合做 P2P、远程联机、自建服务对外发布。')
        elif same_ip and not same_port and len(samples) >= 2:
            nat = 'NAT2'
            title = 'NAT2 · 地址受限锥形（Restricted Cone）'
            desc = ('公网地址固定、映射端口随目标变化。只要你先主动访问过对方，'
                    '对方就能回连你。日常上网、游戏联机基本够用。')
        elif not same_ip and len(samples) >= 2 and _is_private(pub_ip):
            nat = 'NAT3'
            title = 'NAT3 · 端口受限锥形（Port Restricted）'
            desc = ('映射到的是运营商内网地址，说明你处在多层 NAT（运营商级 NAT）之下。'
                    'P2P 与端口映射的成功率会明显下降。')
        elif not same_ip and len(samples) >= 2:
            nat = 'NAT4'
            title = 'NAT4 · 对称型（Symmetric）'
            desc = ('每个不同目标的映射地址都不同，兼容性最差。'
                    '常见于企业网络或运营商的对称 NAT。P2P 基本上需要中继才能连通。')
        else:
            nat = 'NAT2' if same_ip else 'NAT4'
            title = 'NAT%d' % (2 if same_ip else 4)
            desc = '样本不足，仅根据首个探测结果粗略判断；多测几次会更准确。'
        detail['public_ip'] = pub_ip
        detail['public_port'] = pub_port
        detail['same_ip'] = same_ip
        detail['same_port'] = same_port

    result = {'nat': nat, 'title': title, 'desc': desc, 'detail': detail,
              'checked_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    try:
        _save_setting('nat_last', result)
    except Exception:
        pass
    return ok(result, 'NAT 检测完成：%s' % title)


# ------------------------------------------------------------------ 软加速（nftables flowtable）
#
# 原理：把「已建立连接」的后续数据包交给内核 flowtable 快速路径转发，
#       绕过大部分 netfilter 钩子，相当于 RouterOS 的 FastTrack。

ACCEL_CONF = '/etc/drouter/generated/accel.nft'
FLOWTABLE_NAME = 'drouter_fastpath'


def _wan_lan_ifaces():
    """从已保存配置里取 WAN/LAN 网卡名，供 flowtable 绑定。"""
    wan, lans = '', []
    try:
        cfg = _load_setting('pppoe') or {}
        wan = (cfg.get('iface') or '').strip()
    except Exception:
        pass
    if not wan:
        try:
            cfg = _load_setting('system') or {}
            wan = (cfg.get('wan_iface') or '').strip()
        except Exception:
            pass
    try:
        cfg = _load_setting('system') or {}
        if cfg.get('lan_iface'):
            lans = [cfg['lan_iface']]
        elif cfg.get('bridge_iface'):
            lans = [cfg['bridge_iface']]
    except Exception:
        pass
    if not lans:
        try:
            cfg = _load_setting('dnsmasq') or {}
            if cfg.get('iface'):
                lans = [cfg['iface']]
        except Exception:
            pass
    # 兜底：取所有物理网卡
    if not wan or not lans:
        phys = _physical_ifaces()
        if not wan and phys:
            wan = phys[0]
        if not lans:
            lans = phys[1:] if len(phys) > 1 else phys[:]
    # 去重、过滤
    alli = []
    for x in ([wan] + list(lans)):
        x = (x or '').strip()
        if x and re.match(r'^[a-zA-Z0-9_.-]+$', x) and x not in alli:
            alli.append(x)
    return wan, lans, alli


def _accel_devices():
    """flowtable 绑定的设备列表。"""
    _wan, _lans, alli = _wan_lan_ifaces()
    return alli


def _hw_offload_capable(devices):
    """判断这些网卡是否支持硬件流卸载（hw-tc-offload）。

    virtio / vmxnet3 等虚拟网卡一律为 off[fixed]，此时如果强行写 `flags offload`，
    内核会直接报 "Operation not supported" 导致规则加载失败。
    因此必须按实际能力决定是否附加该标志（软件 flowtable 无需该标志也能工作）。
    """
    if not devices:
        return False
    for d in devices:
        rc, o, _e = sh(['ethtool', '-k', d], timeout=8)
        if rc != 0 or not re.search(r'hw-tc-offload:\s*on', o or ''):
            return False
    return True


def _render_accel_nft(devices, hw_offload=False, v4=True, v6=True):
    """生成加速表（独立文件，与用户防火墙规则互不干扰）。

    hw_offload=False 时输出纯软件 flowtable —— 这是虚拟机/普通网卡下唯一可用的形态；
    hw_offload=True 时才附加 `flags offload` 交给支持硬件卸载的网卡。

    v4 / v6 决定加速覆盖的协议族（#5）：
      * flowtable 自身是 family=inet（同时含 IPv4/IPv6），但只有显式写下
        `ip protocol ... flow add @f` / `ip6 nexthdr ... flow add @f`，
        对应协议族的「已建立连接」才会真正被卸载到快速路径。
      * 之前只写了 IPv4 一条，导致 IPv6 流量仍逐包走 netfilter，加速对 v6 无效 —— 这里补上。
    """
    devs = ', '.join('"%s"' % d for d in devices)
    flag_line = '\n        flags offload' if hw_offload else ''
    note = ('已启用硬件卸载（flags offload）' if hw_offload
            else '纯软件 flowtable（网卡不支持硬件卸载，故未附加 flags offload）')
    scope = 'IPv4 + IPv6' if (v4 and v6) else ('仅 IPv4' if v4 else ('仅 IPv6' if v6 else '未启用'))
    rules = []
    if v4:
        rules.append('        ip protocol { tcp, udp } ct state established,related flow add @f')
    if v6:
        # IPv6 下一个头字段叫 nexthdr；为兼容扩展头，同时匹配常见上层协议
        rules.append('        ip6 nexthdr { tcp, udp, sctp, dccp, udplite } '
                     'ct state established,related flow add @f')
    if not rules:
        rules.append('        # 未选择任何协议族，本表不会卸载任何连接')
    return """# 由 Drouter 自动生成：nftables flowtable 软加速
# 作用：已建立连接的后续数据包走快速转发路径，显著降低 CPU 占用、提升吞吐。
# 模式：%s
# 覆盖协议族：%s
# 关闭方法：在「网络状态 / 加速」页点击关闭，或删除本文件后重载 nftables。
table inet drouter_accel {
    flowtable f {
        hook ingress priority 0
        devices = { %s }%s
    }
    chain forward {
        type filter hook forward priority 0; policy accept;
%s
    }
}
""" % (note, scope, devs, flag_line, '\n'.join(rules))


def _accel_status():
    """读取当前加速状态（内核态为准）。"""
    st = {'supported': False, 'enabled': False, 'devices': [], 'flows': 0,
          'kernel': '', 'offload_hw': False, 'family_v4': True, 'family_v6': True}
    rc, o, _e = sh(['uname', '-r'], timeout=6)
    st['kernel'] = o or ''
    # 内核是否支持 flowtable
    rc2, _o2, _e2 = sh(['sh', '-c', 'modinfo nf_flow_table >/dev/null 2>&1 '
                        '|| test -d /sys/module/nf_flow_table'], timeout=8)
    st['supported'] = (rc2 == 0)
    # 是否已加载该表
    rc3, o3, _e3 = sh(['nft', 'list', 'table', 'inet', 'drouter_accel'], timeout=10)
    st['enabled'] = (rc3 == 0 and 'flowtable' in (o3 or ''))
    if st['enabled']:
        m = re.search(r'devices\s*=\s*\{([^}]*)\}', o3 or '')
        if m:
            st['devices'] = [x.strip().strip('"') for x in m.group(1).split(',') if x.strip()]
        st['offload_hw'] = 'offload' in (o3 or '')
        # 从真实规则里推断覆盖的协议族（#5）
        txt = o3 or ''
        st['family_v4'] = bool(re.search(r'\bip\s+protocol\b', txt))
        st['family_v6'] = bool(re.search(r'\bip6\s+nexthdr\b', txt))
    else:
        # 未启用时，读已保存配置里的期望值
        try:
            sv = _load_setting('accel') or {}
            if sv.get('enabled'):
                st['family_v4'] = bool(sv.get('v4', True))
                st['family_v6'] = bool(sv.get('v6', True))
        except Exception:
            pass
    if st['family_v4'] and st['family_v6']:
        st['scope_cn'] = 'IPv4 + IPv6（双栈同时加速）'
    elif st['family_v4']:
        st['scope_cn'] = '仅 IPv4'
    elif st['family_v6']:
        st['scope_cn'] = '仅 IPv6'
    else:
        st['scope_cn'] = '未覆盖任何协议族'
    # 已卸载的连接数
    rc4, o4, _e4 = sh(['sh', '-c', 'conntrack -L 2>/dev/null | grep -c OFFLOAD || true'], timeout=10)
    try:
        st['flows'] = int((o4 or '0').strip() or 0)
    except Exception:
        st['flows'] = 0
    # 驱动是否支持硬件卸载
    hw = []
    for d in (st['devices'] or _accel_devices()):
        rc5, o5, _e5 = sh(['ethtool', '-k', d], timeout=8)
        if rc5 == 0 and re.search(r'hw-tc-offload:\s*on', o5 or ''):
            hw.append(d)
    st['hw_offload_ifaces'] = hw
    return st


# 协议族作用范围说明（#5）：软加速到底对 IPv4 生效、还是对 IPv4/IPv6 都生效？
ACCEL_SCOPE_NOTE = {
    'title': '软加速对 IPv4 与 IPv6 的作用范围',
    'conclusion': ('flowtable 本身属于 inet 家族，可以同时承载 IPv4 与 IPv6；'
                   '但「哪些连接的后续报文会被卸载到快速路径」取决于 forward 链里'
                   '显式写下的匹配规则。Drouter 默认同时对 IPv4 和 IPv6 卸载已建立连接，'
                   '你也可以只选其中一个协议族。'),
    'v4': {'label': 'IPv4', 'rule': 'ip protocol { tcp, udp } ct state established,related flow add @f',
           'scope': 'TCP / UDP 已建立连接 → 走快速路径'},
    'v6': {'label': 'IPv6', 'rule': 'ip6 nexthdr { tcp, udp, sctp, dccp, udplite } ct state established,related flow add @f',
           'scope': 'TCP / UDP / SCTP 等已建立连接 → 走快速路径'},
    'points': [
        'IPv4 与 IPv6 是两套独立的规则：只写 ip 则仅 IPv4 被加速，IPv6 仍逐包走 netfilter。',
        'IPv6 的下一个头字段是 nexthdr（不是 protocol）；使用扩展头的报文会在内核里被规范化后再匹配。',
        'ICMP / ICMPv6 等不属于"连接型"流量，默认不卸载，仍逐包经过防火墙（便于排障与限速）。',
        '若你的运营商不支持 IPv6，选择「仅 IPv4」可减少一条规则，几乎无差别。',
        '已卸载的连接不再逐包经过 forward 链，因此针对这些包的日志 / 计数 / DSCP 标记会失效。',
    ],
}

ACCEL_IMPACT = [
    {'name': '逐包防火墙日志 / 计数', 'level': 'warn', 'why':
     '已建立的连接不再逐包经过 forward 链，因此针对这些数据包的 log / counter 规则不会继续命中。'
     '如果你需要精确审计每条连接，建议不要开启。'},
    {'name': '复杂 ACL / 深度包检测', 'level': 'warn', 'why':
     '基于数据内容的规则（域名、关键字过滤等）只对新连接生效；长连接的后续数据包不再经过检测。'},
    {'name': 'QoS / 流量整形 / 限速', 'level': 'err', 'why':
     'tc 限速与 flowtable 快速路径协同不佳，可能导致限速失效。依赖限速时请勿开启，'
     '或把需要限速的流量排除在加速之外。'},
    {'name': '策略路由 / 多出口分流', 'level': 'warn', 'why':
     '依赖逐包标记的复杂分流策略需要一并复核，必要时把相关流量排除在 flowtable 之外。'},
    {'name': '连接跟踪统计 / 流量监控', 'level': 'info', 'why':
     'conntrack 仍会记录连接，但已卸载流量的字节计数可能不再刷新，监控图表会有偏差。'},
    {'name': 'NAT / 端口映射', 'level': 'info', 'why':
     '已有 NAT 规则可以保留，连接建立阶段仍会走完整 conntrack。开启后建议实测一次端口映射。'},
    {'name': 'UPnP 自动端口映射', 'level': 'ok', 'why':
     '新连接建立时仍会触发 UPnP，不受影响；长期映射开启后复核一次即可。'},
    {'name': 'IPv6 转发', 'level': 'ok', 'why':
     '当前加速表同时接管 IPv4/IPv6 的已建立连接，正常转发无影响。'},
]


def act_accel(p):
    """软加速：查询 / 开启 / 关闭。"""
    op = str((p or {}).get('op') or 'get')
    if op == 'get':
        st = _accel_status()
        _wan, _lans, alli = _wan_lan_ifaces()
        st['candidate_devices'] = alli
        st['phys_ifaces'] = _physical_ifaces()
        st['impact'] = ACCEL_IMPACT
        st['scope_note'] = ACCEL_SCOPE_NOTE
        st['family_v4'] = st.get('family_v4', True)
        st['family_v6'] = st.get('family_v6', True)
        try:
            st['hw_note'] = ('检测到 %s 支持硬件卸载，可在开启后进一步降低 CPU 占用。'
                             % '、'.join(st.get('hw_offload_ifaces') or [])) if st.get('hw_offload_ifaces') else \
                '当前网卡未报告支持硬件卸载，将使用软件快速路径（对家用场景已足够）。'
        except Exception:
            st['hw_note'] = ''
        return ok(st)

    if op in ('on', 'off'):
        st = _accel_status()
        if not st['supported']:
            return fail('当前内核未提供 nftables flowtable 支持，无法开启软加速。'
                        '请确认内核版本 ≥ 4.16 且已加载 nf_flow_table 模块。')
        devices = p.get('devices')
        # 协议族开关（#5）：默认 IPv4 + IPv6 都加速；用户可只选其一
        v4 = bool(p.get('v4', True))
        v6 = bool(p.get('v6', True))
        if op == 'on':
            if not v4 and not v6:
                return fail('请至少选择加速「IPv4」或「IPv6」中的一项。')
            devices = devices if isinstance(devices, list) and devices else _accel_devices()
            devices = [str(d) for d in devices if re.match(r'^[a-zA-Z0-9_.-]+$', str(d))]
            if len(devices) < 2:
                devices = _accel_devices()
            if len(devices) < 2:
                return fail('至少需要两张网卡（WAN + LAN）才能启用 flowtable 加速，'
                            '请先在「网卡与桥接」里完成接口配置。')
            os.makedirs(os.path.dirname(ACCEL_CONF), exist_ok=True)
            hw = _hw_offload_capable(devices)
            content = _render_accel_nft(devices, hw_offload=hw, v4=v4, v6=v6)
            # 语法预检：先写入私有临时文件用 nft -c 检查（见 tmp_nft_path 说明）
            tmp = write_tmp_nft('accel-check', content)
            try:
                rc, _o, e = sh(['nft', '-c', '-f', tmp], timeout=20)
                if rc != 0 and _nft_environ_blocked(e, _o):
                    # 环境不支持 check-only 预检 → 直接放行到真实加载环节校验
                    rc = 0
                if rc != 0:
                    # 若因 flags offload 不被支持而失败，自动降级为纯软件 flowtable 重试一次
                    if hw:
                        content = _render_accel_nft(devices, hw_offload=False, v4=v4, v6=v6)
                        with open(tmp, 'w', encoding='utf-8') as f:
                            f.write(content)
                        rc, _o, e = sh(['nft', '-c', '-f', tmp], timeout=20)
                        if rc == 0 or _nft_environ_blocked(e, _o):
                            hw = False
                            rc = 0
                if rc != 0:
                    return fail('加速规则语法检查未通过：%s' % (e or '未知错误'))
            finally:
                cleanup_tmp(tmp)
            with open(ACCEL_CONF, 'w', encoding='utf-8') as f:
                f.write(content)
            os.chmod(ACCEL_CONF, 0o644)
            # 先删旧表再加载，保证幂等
            sh(['nft', 'delete', 'table', 'inet', 'drouter_accel'], timeout=10)
            rc2, _o2, e2 = sh(['nft', '-f', ACCEL_CONF], timeout=20)
            if rc2 != 0:
                return fail('加载加速规则失败：%s' % (e2 or '未知错误'))
            # 顺带把网卡硬件卸载能力打开（失败不阻断；虚拟网卡此处通常为 off[fixed]）
            if hw:
                for d in devices:
                    sh(['ethtool', '-K', d, 'hw-tc-offload', 'on'], timeout=10)
            _save_setting('accel', {'enabled': True, 'devices': devices,
                                    'hw_offload': hw, 'v4': v4, 'v6': v6})
            fam_cn = ('IPv4 + IPv6' if (v4 and v6) else ('仅 IPv4' if v4 else '仅 IPv6'))
            log('warn', 'system', 'ACCEL_ON',
                '软加速已开启（flowtable%s，协议族 %s，设备：%s）'
                % ('+硬件卸载' if hw else '·软件', fam_cn, '、'.join(devices)))
            st2 = _accel_status()
            st2['hw_offload'] = hw
            st2['family_v4'] = v4
            st2['family_v6'] = v6
            return ok(st2, '软加速已开启：已建立连接将走快速转发路径（%s，覆盖 %s，设备：%s）'
                      % ('硬件卸载' if hw else '软件快速路径', fam_cn, '、'.join(devices)))
        else:
            sh(['nft', 'delete', 'table', 'inet', 'drouter_accel'], timeout=15)
            try:
                if os.path.isfile(ACCEL_CONF):
                    os.remove(ACCEL_CONF)
            except Exception:
                pass
            _save_setting('accel', {'enabled': False, 'devices': []})
            log('warn', 'system', 'ACCEL_OFF', '软加速已关闭')
            return ok(_accel_status(), '软加速已关闭：所有流量恢复逐包经过防火墙规则')

    return fail('未知操作：%s' % op)


# ------------------------------------------------------------------ PPPoE 多拨
#
# 对标爱快「多拨」。两种形态：
#   1) 单账号多会话（multidial）：同一个账号在同一物理口上发起 N 个 PPPoE 会话，
#      得到 N 个公网 IP —— 运营商允许同账号并发拨号时才能用（部分省份限制为 1）。
#   2) 多账号（multiaccount）：每个会话用独立账号，最稳妥、不会被判异常。
#
# 实现要点：
#   * 每个会话一个 ppp 接口（drouter-wan1 ... drouter-wanN），独立 pppd 实例 + 独立 unit；
#   * 每张会话配一条独立的默认路由，用路由表 + ip rule 做「源地址策略路由」，
#     保证从某条会话进来的回包原路返回（否则多 WAN 下必然回包错路而断流）；
#   * 出站方向用 ip route 的 nexthop weight 做加权负载均衡（等价 RouterOS 的 ECMP）；
#   * 并发失败（运营商拒绝）时给出可读原因，不静默重试。

PPPM_CONF = '/etc/drouter/generated/pppoe-multi.json'
PPPM_UNIT_DIR = '/etc/systemd/system'
PPPM_TABLE_BASE = 100          # 会话 i 的路由表号 = PPPM_TABLE_BASE + i

# 会话健康检查间隔（秒）。PPPoE 断线由 pppd 自己 persist 重拨，这里只做状态采集。
PPPM_STRATEGIES = [
    {'id': 'balance', 'name': '负载均衡（按会话均分）',
     'why': '所有会话同时承载流量，总吞吐 ≈ 各会话相加。适合运营商允许多拨的场景。'},
    {'id': 'primary_backup', 'name': '主备（故障自动切换）',
     'why': '只用第一条会话，其余待机；主链路断开时自动切到备用。适合多拨不稳定或想省资源。'},
    {'id': 'weighted', 'name': '加权（自定义各会话权重）',
     'why': '按权重分配流量，例如两条 500M + 一条 200M 可设 5:5:2。'},
]


def _pppm_load():
    """读取多拨配置（含账号、会话数、策略）。"""
    try:
        if os.path.isfile(PPPM_CONF):
            with open(PPPM_CONF, encoding='utf-8') as f:
                d = json.load(f) or {}
                if isinstance(d, dict):
                    return d
    except Exception:
        pass
    return {'enabled': False, 'sessions': [], 'strategy': 'balance', 'iface': ''}


def _pppm_save(d):
    os.makedirs(os.path.dirname(PPPM_CONF), exist_ok=True)
    with open(PPPM_CONF, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.chmod(PPPM_CONF, 0o600)   # 含密码，仅 root 可读


def _pppm_session_iface(i):
    return 'drouter-wan%d' % i


def _pppm_session_unit(i):
    return 'drouter-pppoe@%d.service' % i


def _pppm_live():
    """采集各会话实时状态：接口是否 UP、拿到的公网 IP、对端网关、默认路由。
    返回 {ifname: {...}}（通过 ip -j 解析，避免依赖 pppd 自身状态文件）。"""
    live = {}
    rc, out, _e = sh(['ip', '-j', '-4', 'addr', 'show'], timeout=8)
    if rc == 0:
        try:
            for lk in json.loads(out or '[]'):
                nm = lk.get('ifname') or ''
                if not re.match(r'^drouter-wan\d+$', nm):
                    continue
                ipv4 = ''
                for ai in (lk.get('addr_info') or []):
                    if ai.get('family') == 'inet':
                        ipv4 = '%s/%s' % (ai.get('local'), ai.get('prefixlen'))
                        break
                live[nm] = {'up': True, 'addr': ipv4, 'peer': ''}
        except Exception:
            pass
    # 对端网关（pppd 会把 peer 地址放进路由 / ppp 接口的点对点属性）
    rc, out, _e = sh(['ip', '-j', '-4', 'route', 'show'], timeout=8)
    if rc == 0:
        try:
            for rt in json.loads(out or '[]'):
                dev = rt.get('dev') or ''
                if dev in live and rt.get('gateway'):
                    live[dev]['peer'] = rt.get('gateway')
        except Exception:
            pass
    return live


def _pppm_iprule_dump():
    """读回当前由本模块创建的 ip rule（fwmark 1000+i）。"""
    rules = []
    rc, out, _e = sh(['ip', '-j', 'rule', 'show'], timeout=8)
    if rc == 0:
        try:
            for r in json.loads(out or '[]'):
                fw = r.get('fwmark')
                if fw is None:
                    continue
                try:
                    fwi = int(str(fw), 0)
                except Exception:
                    continue
                if 1000 <= fwi < 1100:
                    rules.append({'fwmark': fwi, 'table': str(r.get('table') or ''),
                                  'priority': r.get('priority')})
        except Exception:
            pass
    return rules


def _pppm_render_peer(acct, i):
    """渲染第 i 个会话的 pppd 参数文件。acct: {user, password, iface, mtu, ...}"""
    ifname = str(acct.get('iface') or 'ens19').strip()
    user = str(acct.get('user') or '').strip()
    mtu = int(acct.get('mtu') or 1492)
    mru = int(acct.get('mru') or 1492)
    service = str(acct.get('service_name') or '').strip()
    lines = [
        '# 由 drouter 自动生成：PPPoE 多拨会话 #%d' % i,
        'plugin rp-pppoe.so %s' % ifname,
        'name "%s"' % user,
        'noauth',
        'noipdefault',
        'defaultroute',          # 先要一条默认路由，稍后由策略路由改写
        'replacedefaultroute',   # 允许覆盖已存在的默认路由（多拨必需）
        'persist',               # 断线自动重拨
        'maxfail 0',
        'holdoff 5',
        'mtu %d' % mtu,
        'mru %d' % mru,
        'lcp-echo-interval 10',
        'lcp-echo-failure 3',
        'usepeerdns',
        'ip-up-script /etc/ppp/ip-up.d/drouter-pppm-route',
        'ip-down-script /etc/ppp/ip-down.d/drouter-pppm-route',
    ]
    if service:
        lines.append('rp_pppoe_service "%s"' % service)
    if acct.get('debug'):
        lines.append('debug')
    lines.append('')
    return '\n'.join(lines)


def _pppm_render_unit(i):
    """第 i 个会话的 systemd 单元。"""
    return '''[Unit]
Description=Drouter PPPoE 多拨会话 #%(i)d
After=network-online.target
Wants=network-online.target
ConditionPathExists=%(conf)s

[Service]
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Type=forking
ExecStart=/usr/sbin/pppd call %(peer)s nodetach
Restart=always
RestartSec=8
PIDFile=/run/ppp-%(peer)s.pid
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
''' % {'i': i, 'conf': PPPM_CONF, 'peer': _pppm_session_iface(i)}


def act_pppoe_multi(p):
    """PPPoE 多拨：查询 / 保存 / 应用 / 启停 / 状态。"""
    op = str((p or {}).get('op') or 'get')
    d = _pppm_load()
    live = _pppm_live()

    # ---------- 查询 ----------
    if op == 'get':
        sessions = d.get('sessions') or []
        out = []
        for i, s in enumerate(sessions, start=1):
            nm = _pppm_session_iface(i)
            st = live.get(nm) or {}
            out.append({
                'idx': i, 'iface': nm, 'user': s.get('user') or '',
                # 密码绝不回传明文，只回传是否已设置
                'has_password': bool(s.get('password')),
                'running': bool(st.get('up')),
                'addr': st.get('addr') or '',
                'peer': st.get('peer') or '',
                'weight': int(s.get('weight') or 1),
                'table': PPPM_TABLE_BASE + i,
                'fwmark': 1000 + i,
                'unit': _pppm_session_unit(i),
            })
        rc, o, _e = sh(['systemctl', 'is-enabled', 'drouter-pppoe@1.service'], timeout=6)
        return ok({
            'enabled': bool(d.get('enabled')),
            'strategy': d.get('strategy') or 'balance',
            'wan_iface': d.get('iface') or '',
            'sessions': out,
            'count': len(out),
            'online': len([x for x in out if x['running']]),
            'strategies': PPPM_STRATEGIES,
            'wan_ifaces': _physical_ifaces(),
            'ip_rules': _pppm_iprule_dump(),
            'unit_installed': (o or '').strip() not in ('', 'not-found'),
            # 关键提示：多播需要运营商允许，先给用户预期
            'notes': [
                '多拨能否成功取决于运营商是否允许同一账号并发拨号（部分省份限制为 1 条）。',
                '尝试 N 个会话前，建议先只用 1 条确认账号可正常拨通。',
                '若运营商拒绝并发，会话会反复重拨并在这页显示为「未连接」，属正常现象。',
                '多拨会改变出网路由，请确认已填好账号并显式点击「应用并连接」。',
            ],
        })

    # ---------- 保存配置 ----------
    if op == 'save':
        sessions_in = p.get('sessions')
        if not isinstance(sessions_in, list) or not sessions_in:
            return fail('请至少配置 1 个拨号会话')
        if len(sessions_in) > 8:
            return fail('会话数量最多 8 条（更多会话不会带来收益，反而增加运营商封禁风险）')
        wan = str(p.get('iface') or d.get('iface') or '').strip()
        if not re.match(r'^[a-zA-Z0-9_.-]+$', wan):
            return fail('WAN 物理网卡名称不合法：%s' % (wan or '未指定'))
        strategy = str(p.get('strategy') or 'balance')
        if strategy not in [s['id'] for s in PPPM_STRATEGIES]:
            return fail('不支持的多拨策略：%s' % strategy)

        clean = []
        for i, s in enumerate(sessions_in, start=1):
            s = s or {}
            user = str(s.get('user') or '').strip()
            pwd = s.get('password')
            # 支持「只改其他字段，不回传密码」：password 为空则沿用旧值
            if pwd is None or str(pwd) == '':
                old = (d.get('sessions') or [])
                pwd = (old[i - 1].get('password') if i <= len(old) else '') or ''
            pwd = str(pwd)
            if not user:
                return fail('第 %d 个会话缺少账号' % i)
            if not pwd:
                return fail('第 %d 个会话缺少密码' % i)
            if not re.match(r'^[A-Za-z0-9@._+\-]{1,64}$', user):
                return fail('第 %d 个会话的账号含有不支持的字符' % i)
            if len(pwd) > 128 or any(c in pwd for c in '\n\r"'):
                return fail('第 %d 个会话的密码含有不支持的字符' % i)
            try:
                w = int(s.get('weight') or 1)
            except Exception:
                w = 1
            w = max(1, min(100, w))
            try:
                mtu = int(s.get('mtu') or 1492)
            except Exception:
                mtu = 1492
            mtu = max(576, min(1500, mtu))
            # MRU 独立取值：原来直接写 'mru': mtu，用户在界面上填的 MRU
            # 被静默忽略。部分线路确实需要 mru < mtu，硬改成相等会让
            # 「我已经调小了 MRU」变成一句空话（界面上看不出任何异常）。
            try:
                mru = int(s.get('mru') or mtu)
            except Exception:
                mru = mtu
            mru = max(576, min(1500, mru))
            # service_name 会被拼成 servicename "…" 写进 pppd 参数文件。
            # 同函数里 user 有白名单、password 显式拒绝引号与换行，
            # 只有它只做了 strip+截断 —— 含引号就能闭合引号接着写 pppd 指令。
            # 单拨路径 render_ppp 早就用 v_text(pattern=...) 挡住了，这里漏了。
            svc = str(s.get('service_name') or '').strip()[:64]
            if not re.match(r'^[A-Za-z0-9._-]*$', svc):
                return fail('第 %d 个会话的服务名只能包含字母、数字、点、下划线和连字符' % i)
            clean.append({
                'user': user, 'password': pwd, 'weight': w, 'mtu': mtu,
                'mru': mru, 'service_name': svc,
                'isp': str(s.get('isp') or 'auto'),
            })
        # 单账号多拨：把第一条的账号复制到其余会话（多点几下就能展开）
        if p.get('duplicate_account') and clean:
            first = clean[0]
            clean = [dict(first) for _ in clean]
        # 会话数量变化时，删掉不再需要的旧会话配置
        newd = {'enabled': bool(d.get('enabled')), 'iface': wan, 'strategy': strategy,
                'sessions': clean, 'applied': False}
        _pppm_save(newd)
        _pppm_write_secrets(clean, wan)
        log('warn', 'system', 'PPPM_SAVED',
            '已保存 PPPoE 多拨配置：%d 个会话，策略 %s' % (len(clean), strategy))
        return ok({'sessions': len(clean)},
                  '已保存 %d 个会话（尚未连接，需点击「应用并连接」）' % len(clean))

    # ---------- 应用并连接 ----------
    # 注意：这一步会真正拨号并改变出网默认路由。必须显式传 confirm=true。
    if op == 'apply':
        if p.get('confirm') is not True:
            return fail('应用多拨会改变出网路由，请勾选确认后再执行')
        if os.path.exists(BUILD_FLAG):
            return fail('构建保护模式已开启，禁止执行会改变网络的动作（含拨号）。'
                        '请在页面顶部关闭保护模式后重试。')
        sessions = d.get('sessions') or []
        if not sessions:
            return fail('尚未保存任何拨号会话，请先保存配置')
        wan = (d.get('iface') or '').strip()
        rc, o, _e = sh(['ip', 'link', 'show', wan], timeout=6)
        if rc != 0:
            return fail('WAN 物理网卡 %s 不存在，请先在「WAN 口」里配置' % wan)

        # 1) 生成每个会话的参数文件
        for i, s in enumerate(sessions, start=1):
            path = '/etc/ppp/peers/%s' % _pppm_session_iface(i)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(_pppm_render_peer(s, i))
            os.chmod(path, 0o640)
            sh(['chown', 'root:dip', path], timeout=6)

        # 2) 生成 IP 上下线脚本（负责策略路由与默认路由的挂接）
        _pppm_write_hook()

        # 3) 生成 systemd 单元
        for i in range(1, len(sessions) + 1):
            upath = os.path.join(PPPM_UNIT_DIR, _pppm_session_unit(i))
            with open(upath, 'w', encoding='utf-8') as f:
                f.write(_pppm_render_unit(i))
            os.chmod(upath, 0o644)
        # 清理多余会话的旧单元
        _pppm_prune_units(len(sessions))

        rc, o, e = sh(['systemctl', 'daemon-reload'], timeout=25)
        if rc != 0:
            return fail('重载 systemd 失败：%s' % (e or o))

        # 4) 启动所有会话
        started, failed = [], []
        for i in range(1, len(sessions) + 1):
            unit = _pppm_session_unit(i)
            sh(['systemctl', 'enable', unit], timeout=20)
            rc, o, e = sh(['systemctl', 'restart', unit], timeout=45)
            (started if rc == 0 else failed).append({'idx': i, 'unit': unit,
                                                     'err': '' if rc == 0 else (e or o)})
        d['enabled'] = True
        d['applied'] = True
        _pppm_save(d)
        log('warn', 'pppoe', 'PPPM_APPLY',
            'PPPoE 多拨已应用：%d 条会话' % len(sessions),
            {'started': len(started), 'failed': len(failed)})
        msg = '已启动 %d 条会话' % len(started)
        if failed:
            msg += '，%d 条启动失败（请查看下方日志）' % len(failed)
        return ok({'started': started, 'failed': failed}, msg)

    # ---------- 断开全部会话 ----------
    if op == 'stop':
        sessions = d.get('sessions') or []
        n = 0
        for i in range(1, len(sessions) + 1):
            rc, _o, _e = sh(['systemctl', 'stop', _pppm_session_unit(i)], timeout=30)
            if rc == 0:
                n += 1
        # 清理本模块创建的策略路由与路由表
        _pppm_cleanup_routes(len(sessions))
        d['enabled'] = False
        _pppm_save(d)
        log('warn', 'pppoe', 'PPPM_STOP', 'PPPoE 多拨已停止 %d 条会话' % n)
        return ok({'stopped': n}, '已断开全部多拨会话，出网恢复为单一默认路由')

    # ---------- 单条会话操作 ----------
    if op in ('session_start', 'session_stop', 'session_restart'):
        try:
            i = int(p.get('idx') or 0)
        except Exception:
            return fail('会话序号不合法')
        sessions = d.get('sessions') or []
        if not (1 <= i <= len(sessions)):
            return fail('会话 #%d 不存在' % i)
        act = {'session_start': 'start', 'session_stop': 'stop',
               'session_restart': 'restart'}[op]
        rc, o, e = sh(['systemctl', act, _pppm_session_unit(i)], timeout=45)
        if rc != 0:
            return fail('会话 #%d %s 失败：%s' % (i, act, e or o))
        return ok({'idx': i}, '会话 #%d 已%s' % (i, {'start': '启动', 'stop': '停止',
                                                    'restart': '重启'}[act]))

    # ---------- 会话日志（给「一键排错」用） ----------
    if op == 'log':
        try:
            i = int(p.get('idx') or 1)
        except Exception:
            i = 1
        unit = _pppm_session_unit(i)
        rc, o, _e = sh(['journalctl', '-u', unit, '-n', '60', '--no-pager'], timeout=20)
        # 顺便给出可读化结论
        hint = ''
        low = (o or '').lower()
        if 'authentication failed' in low or 'chap authentication failed' in low:
            hint = '账号或密码错误。请核对「账号 / 密码」，注意部分省份需要在账号后加 @域名。'
        elif 'no pppoe' in low or 'timeout waiting for pado' in low:
            hint = '未收到运营商 PPPoE 应答。请确认 WAN 网线接在光猫的桥接口、且光猫已改为桥接模式。'
        elif 'no response to 3 echo-repeats' in low or 'lcp terminated' in low:
            hint = '链路被对端断开，常见于运营商检测到多拨并拒绝。建议减少会话数。'
        elif 'service name' in low and 'not' in low:
            hint = '运营商要求指定服务名，请在高级选项里填写 rp_pppoe_service。'
        elif (not o) or '-- no entries --' in low or 'no journal files' in low:
            # journalctl 在无任何记录时会返回 "-- No entries --"（非空字符串），
            # 所以不能只判 not o。
            hint = ('暂无日志，说明该会话还没启动过。请先点「保存配置」再点「应用并连接」。'
                    if '-- no entries --' in low else '暂无日志，说明该会话还没启动过。')
        return ok({'idx': i, 'unit': unit, 'log': o, 'hint': hint},
                  '已读取会话 #%d 的日志' % i)

    return fail('未知操作：%s' % op)


def _pppm_write_secrets(sessions, wan):
    """把多拨账号写入独立的 chap/pap secrets 文件片段（不覆盖用户原有内容）。"""
    chap, pap = [], []
    for i, s in enumerate(sessions, start=1):
        line = '"%s" * "%s" *' % (s.get('user'), s.get('password'))
        chap.append(line)
        pap.append(line)
    # 用 include 方式挂到主文件，避免破坏用户已有的 secrets
    for kind, body in (('chap', chap), ('pap', pap)):
        inc = '/etc/ppp/%s-secrets.drouter-multi' % kind
        with open(inc, 'w', encoding='utf-8') as f:
            f.write('# 由 drouter 自动生成：PPPoE 多拨账号（%d 条）\n' % len(sessions))
            f.write('\n'.join(body) + '\n')
        os.chmod(inc, 0o600)
        main = '/etc/ppp/%s-secrets' % kind
        try:
            cur = ''
            if os.path.isfile(main):
                with open(main, encoding='utf-8', errors='replace') as f:
                    cur = f.read()
            marker = 'include /etc/ppp/%s-secrets.drouter-multi' % kind
            if marker not in cur:
                with open(main, 'a', encoding='utf-8') as f:
                    f.write('\n# drouter 多拨账号\n' + marker + '\n')
        except Exception as e:
            log('warn', 'pppoe', 'PPPM_SECRETS', '写入 %s-secrets 失败：%s' % (kind, e))


def _pppm_write_hook():
    """写入 PPP 拨通/断开时执行的脚本：负责把每个会话绑定到独立路由表。

    这是多拨能真正工作的关键 —— 没有源地址策略路由，多会话会出现
    「A 会话出去的包从 B 会话回来」导致连接重置。"""
    script = '''#!/bin/sh
# 由 drouter 自动生成：PPPoE 多拨策略路由挂接
# pppd 调用参数：$1=interface $2=tty $3=speed $4=local_ip $5=peer_ip $6=ipparam
IFACE="$1"
IP="$4"
PEER="$5"
CONF="/etc/drouter/generated/pppoe-multi.json"

case "$IFACE" in
  drouter-wan*) ;;
  *) exit 0 ;;
esac

IDX=$(echo "$IFACE" | sed 's/^drouter-wan//')
TABLE=$((100 + IDX))
MARK=$((1000 + IDX))

# 从 JSON 里取策略（不用 jq，靠 python3 把结果打出来）
STRATEGY=$(python3 - <<'PY' 2>/dev/null
import json
try:
    with open('/etc/drouter/generated/pppoe-multi.json') as f:
        d = json.load(f)
    print(d.get('strategy') or 'balance')
except Exception:
    print('balance')
PY
)

case "$1" in
  "$IFACE")
    ip route flush table "$TABLE" 2>/dev/null
    ip route add default via "$PEER" dev "$IFACE" table "$TABLE" 2>/dev/null
    ip rule del fwmark "$MARK" table "$TABLE" 2>/dev/null
    ip rule add fwmark "$MARK" table "$TABLE" priority $((100 + IDX)) 2>/dev/null
    ip rule del from "$IP" table "$TABLE" 2>/dev/null
    ip rule add from "$IP" table "$TABLE" priority $((2000 + IDX)) 2>/dev/null
    ;;
esac
'''
    path = '/etc/ppp/ip-up.d/drouter-pppm-route'
    os.makedirs('/etc/ppp/ip-up.d', exist_ok=True)
    os.makedirs('/etc/ppp/ip-down.d', exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(script)
    os.chmod(path, 0o755)
    shutil.copyfile(path, '/etc/ppp/ip-down.d/drouter-pppm-route')
    os.chmod('/etc/ppp/ip-down.d/drouter-pppm-route', 0o755)


def _pppm_prune_units(keep):
    """删除超出 keep 数量的旧会话单元。"""
    try:
        for fn in os.listdir(PPPM_UNIT_DIR):
            m = re.match(r'^drouter-pppoe@(\d+)\.service$', fn)
            if m and int(m.group(1)) > keep:
                sh(['systemctl', 'disable', fn], timeout=15)
                os.remove(os.path.join(PPPM_UNIT_DIR, fn))
                log('info', 'pppoe', 'PPPM_PRUNE', '已移除多余会话单元 %s' % fn)
    except Exception as e:
        log('warn', 'pppoe', 'PPPM_PRUNE', '清理单元失败：%s' % e)


def _pppm_cleanup_routes(n):
    """删除本模块创建的 ip rule 与路由表。"""
    for i in range(1, max(n, 0) + 2):
        table = PPPM_TABLE_BASE + i
        mark = 1000 + i
        sh(['ip', 'rule', 'del', 'fwmark', str(mark), 'table', str(table)], timeout=6)
        sh(['ip', 'route', 'flush', 'table', str(table)], timeout=6)
    sh(['ip', 'rule', 'del', 'priority', '2001'], timeout=6)
    for i in range(1, max(n, 0) + 2):
        idx = 2000 + i
        rc, o, _e = sh(['ip', '-j', 'rule', 'show'], timeout=6)
        try:
            for r in json.loads(o or '[]'):
                if r.get('priority') == idx and r.get('table') == str(PPPM_TABLE_BASE + i):
                    sh(['ip', 'rule', 'del', 'priority', str(idx)], timeout=6)
        except Exception:
            pass


# ------------------------------------------------------------------ QoS 智能限速
#
# 对标爱快 QoS。四层能力，按「先简单后复杂」组织：
#   1) CAKE 智能整形 —— 上行直接挂在 WAN 上；下行必须靠 IFB 重定向（入口流量
#      无法直接排队），这是爱快「智能流控」的开源等效物；
#   2) HTB + nftables mark —— 按内网 IP 精确限速（保证带宽 + 峰值带宽）；
#   3) DSCP 打标 —— 游戏 / DNS / 语音插队，配合 CAKE 的 diffserv 队列生效；
#   4) nftables connlimit —— 单 IP 连接数限制。
#
# ⚠️ 与 flowtable 软加速的关系：flowtable 会让「已建立连接」绕过 netfilter 钩子，
#    导致 DSCP 打标与 mark 分类失效。所以开启 QoS 时应关闭软加速，本模块会主动检测并提示。

QOS_CONF = '/etc/drouter/generated/qos.nft'
QOS_UNIT = '/etc/systemd/system/drouter-qos.service'
QOS_SCRIPT = '/usr/local/sbin/drouter-qos-apply'
QOS_IFB = 'ifb-drouter'
QOS_MARK_BASE = 0x1000          # 每个内网 IP 的 mark 基址（0x1000 + 序号）

# CAKE 队列方案说明（differv 档位越多，CPU 开销略高）
QOS_CAKE_MODES = [
    {'id': 'diffserv3', 'name': 'diffserv3（三档，推荐）',
     'why': '把流量分为 语音/尽力/背景 三档，兼顾效果与 CPU 占用。'},
    {'id': 'diffserv4', 'name': 'diffserv4（四档）',
     'why': '更细的优先级（含视频档），CPU 略高，适合带宽充裕的场景。'},
    {'id': 'besteffort', 'name': 'besteffort（不分档）',
     'why': '只做公平分流、不做优先级，CPU 最省。'},
]

QOS_PRESETS = [
    {'id': 'gaming', 'name': '游戏优先', 'dscp': 'cs4',
     'ports': '27000-28999,3074,3478-3480',
     'why': '把 Steam / Xbox / PSN 常用端口标为高优先级，插队转发。'},
    {'id': 'voip', 'name': '语音优先', 'dscp': 'ef',
     'ports': '5060,5061,10000-20000',
     'why': 'SIP 与 RTP 语音流量最高优先级，避免通话卡顿。'},
    {'id': 'dns', 'name': 'DNS 优先', 'dscp': 'ef',
     'ports': '53',
     'why': '小包 DNS 优先，显著改善网页首包体验。'},
    {'id': 'video', 'name': '视频优先', 'dscp': 'af41',
     'ports': '1935,3478-3497,5004-5005,8000-8010,8801-8802,19302-19309',
     'why': '视频会议（腾讯会议 / Zoom / Teams / WebRTC）与直播推流的实时音视频。'
            'af41 比游戏低半档、比普通网页高，既保证画面不卡，'
            '又不会把下载和游戏挤下去。'},
    {'id': 'bt', 'name': 'BT / P2P 降级', 'dscp': 'cs1',
     'ports': '6881-6889,51413',
     'why': '把 P2P 降到最低优先级，不占用游戏/网页的带宽。'},
]

# 影响说明（前端直接渲染；级别 warn/err/info/ok）
QOS_IMPACT = [
    {'name': '与 flowtable 软加速冲突', 'level': 'warn',
     'why': '软加速让已建立连接绕过 netfilter，DSCP 打标与 mark 分类会失效。'
            '开启 QoS 前请先关闭「网络状态 / 加速」页的软加速，本模块会自动检测并提示。'},
    {'name': 'CAKE 会占用 CPU', 'level': 'warn',
     'why': 'CAKE 是逐包调度，带宽越高 CPU 开销越大。1Gbps 以内通常无压力；'
            '超过 2Gbps 建议改用 flowtable 或 XDP 方案（本机为虚拟机，实际可承受带宽更低）。'},
    {'name': 'HTB 按 IP 限速影响面', 'level': 'info',
     'why': '限速对一个内网 IP 的上下行同时生效。设「保证带宽」过低会让该设备一直慢，'
            '建议保证带宽 + 峰值带宽搭配使用。'},
    {'name': '加密流量无法识别应用', 'level': 'info',
     'why': 'HTTPS / QUIC 已加密，仅靠端口与 DSCP 无法准确区分「是哪个应用」。'
            '要按应用识别请启用 DPI 模块（nDPI），但它需要额外进程与 CPU。'},
    {'name': '连接数限制的取舍', 'level': 'info',
     'why': '限制单 IP 并发连接数能抑制异常设备拖垮路由，但设得过低会让'
            '迅雷/多标签浏览器等正常应用报错，建议 300 以上。'},
]


def _qos_load():
    try:
        if os.path.isfile(QOS_CONF.replace('.nft', '.json')):
            with open(QOS_CONF.replace('.nft', '.json'), encoding='utf-8') as f:
                d = json.load(f) or {}
                if isinstance(d, dict):
                    return d
    except Exception:
        pass
    return {'enabled': False, 'mode': 'diffserv3', 'wan_iface': '',
            'down_mbit': 0, 'up_mbit': 0, 'ips': [], 'dscp': [],
            'connlimit': False, 'connlimit_max': 500}


def _qos_save(d):
    """原子写入 QoS 配置。

    目标目录由部署脚本以 root 建立；helper 本身也以 root 运行。这里仍做两层
    保险：① 目录不存在则递归创建；② 直接写临时文件再 os.replace，避免半截文件。
    """
    p = QOS_CONF.replace('.nft', '.json')
    dpath = os.path.dirname(p)
    try:
        os.makedirs(dpath, exist_ok=True)
        try:
            os.chmod(dpath, 0o755)
        except Exception:
            pass
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)
    except Exception as ex:
        raise RuntimeError('写入 QoS 配置失败（%s）：%s' % (p, ex))


def _qos_modprobe():
    """加载所需内核模块，返回缺失项列表。"""
    missing = []
    for m in ('sch_cake', 'sch_htb', 'sch_fq_codel', 'ifb'):
        rc, _o, _e = sh(['modprobe', m], timeout=15)
        if rc != 0:
            missing.append(m)
    return missing


def _qos_ifb_ensure():
    """确保 IFB 设备存在且 UP（下行整形必须经过它）。"""
    rc, _o, _e = sh(['ip', 'link', 'show', QOS_IFB], timeout=6)
    if rc != 0:
        rc, o, e = sh(['ip', 'link', 'add', QOS_IFB, 'type', 'ifb'], timeout=10)
        if rc != 0:
            return False, (e or o)
    # 用独立命名空间承载下行流量，避免与业务桥冲突
    sh(['ip', 'link', 'set', QOS_IFB, 'up'], timeout=8)
    return True, ''


def _qos_live():
    """读取当前生效的 tc 规则概要。"""
    st = {'ingress': [], 'ifb': [], 'wan_root': []}
    d = _qos_load()
    wan = d.get('wan_iface') or ''
    if wan:
        rc, o, _e = sh(['tc', 'qdisc', 'show', 'dev', wan], timeout=8)
        if rc == 0:
            for line in (o or '').splitlines():
                if 'qdisc' in line:
                    st['ingress' if 'ingress' in line else 'wan_root'].append(line.strip())
    rc, o, _e = sh(['tc', 'qdisc', 'show', 'dev', QOS_IFB], timeout=8)
    if rc == 0:
        st['ifb'] = [l.strip() for l in (o or '').splitlines() if l.strip()]
    rc, o, _e = sh(['nft', 'list', 'table', 'inet', 'drouter_qos'], timeout=10)
    st['nft_ok'] = rc == 0
    st['nft_dump'] = o if rc == 0 else ''
    return st


def _qos_render_nft(d, table='drouter_qos'):
    """渲染 QoS 的 nftables 规则：DSCP 打标 + mark 分类 + 连接数限制。

    table 参数用于「加载校验」场景：用临时表名生成一份影子规则去真实加载，
    校验通过后立刻删除，绝不污染生产表。
    """
    lines = [
        '# 由 drouter 自动生成：QoS（DSCP 优先级 / IP 分类标记 / 连接数限制）',
        '# 与 CAKE 的 diffserv 队列配合生效。',
        'table inet %s {' % table,
    ]
    # --- DSCP 打标链（mangle，先于路由决策） ---
    lines += [
        '  chain dscp {',
        '    type filter hook forward priority mangle; policy accept;',
    ]
    for i, r in enumerate(d.get('dscp') or []):
        pid = str(r.get('preset') or r.get('id') or ('rule%d' % i))
        dscp = str(r.get('dscp') or 'cs1')
        if dscp not in ('cs0', 'cs1', 'cs2', 'cs3', 'cs4', 'cs5', 'cs6', 'cs7',
                        'ef', 'af11', 'af21', 'af31', 'af41'):
            continue
        ports = _qos_port_expr(r.get('ports'))
        if ports:
            # 双向匹配：无论上行还是下行都按同一优先级处理
            lines.append('    meta l4proto { tcp, udp } th dport { %s } ip dscp set %s comment "drouter:%s"'
                         % (ports, dscp, pid))
        ips = [str(x).strip() for x in (r.get('ips') or []) if str(x).strip()]
        for ip in ips[:32]:
            if not re.match(r'^\d+\.\d+\.\d+\.\d+(/\d+)?$', ip):
                continue
            lines.append('    ip daddr %s ip dscp set %s comment "drouter:%s"' % (ip, dscp, pid))
    lines += [
        '  }',
        '',
    ]
    # --- IP 分类标记链（给 HTB 用） ---
    # ⚠️ 链名不能叫 mark：mark 是 nftables 保留字，会报
    #    "syntax error, unexpected mark, expecting string or last"。统一用 marking。
    # ⚠️ priority 不支持算术表达式（mangle + 1 非法），必须写数值。
    #    nft 内置优先级：raw=-300、mangle=-150、dstnat=-100、filter=0。
    #    这里用 -149（= mangle 之后 1 位）保证 DSCP 打标先于它生效。
    ips = d.get('ips') or []
    if ips:
        lines += [
            '  chain marking {',
            '    type filter hook forward priority -149; policy accept;',
        ]
        for i, r in enumerate(ips, start=1):
            addr = str((r or {}).get('ip') or '').strip()
            if not re.match(r'^\d+\.\d+\.\d+\.\d+$', addr):
                continue
            mark = QOS_MARK_BASE + i
            # 上行（源）与下行（目的）都要打标 —— HTB 在 IFB 上只看一个方向
            lines.append('    ip saddr %s meta mark set 0x%x comment "drouter:ip%s-up"' % (addr, mark, i))
            lines.append('    ip daddr %s meta mark set 0x%x comment "drouter:ip%s-down"' % (addr, mark, i))
        lines += [
            '  }',
            '',
        ]
    # --- 连接数限制 ---
    if d.get('connlimit'):
        try:
            mx = max(10, min(20000, int(d.get('connlimit_max') or 500)))
        except Exception:
            mx = 500
        lan = '192.168.7.0/24'
        try:
            sysc = _load_setting('system') or {}
            a = str(sysc.get('lan_address') or '')
            if '/' in a and re.match(r'^\d+\.\d+\.\d+\.\d+/\d+$', a):
                lan = a
        except Exception:
            pass
        lines += [
            '  chain connlimit {',
            '    type filter hook forward priority filter; policy accept;',
            # 每条规则末尾必须带分号：nft 的 chain 块在 '}' 前要求分号，
            # 漏写会报 "syntax error, unexpected '}', expecting newline or semicolon"。
            '    ip saddr %s ct state new ct count over %d counter drop '
            'comment "drouter:connlimit";' % (lan, mx),
            '  }',
            '',
        ]
    lines += ['}', '']
    return '\n'.join(lines)


def _qos_port_expr(raw):
    """把 '80,443,27000-28999' 变成 nft 集合语法 '{ 80, 443, 27000-28999 }' 的内部内容。"""
    if not raw:
        return ''
    parts = []
    for tok in str(raw).split(','):
        tok = tok.strip()
        if not tok:
            continue
        if re.match(r'^\d{1,5}$', tok):
            parts.append(tok)
        elif re.match(r'^\d{1,5}-\d{1,5}$', tok):
            parts.append(tok)
    return ', '.join(parts[:64])


def _qos_render_script(d):
    """渲染应用脚本：CAKE 上行 + IFB 下行 + HTB 按 IP 限速。

    规则全部用 replace 语义，可重复执行（幂等）。"""
    wan = d.get('wan_iface') or 'ens19'
    mode = d.get('mode') or 'diffserv3'
    up = int(d.get('up_mbit') or 0)
    down = int(d.get('down_mbit') or 0)
    ips = d.get('ips') or []

    L = ['#!/bin/sh',
         '# 由 drouter 自动生成：QoS 规则应用脚本（可重复执行）',
         '# 用法：drouter-qos-apply apply|clear',
         'set -u',
         'WAN="%s"' % wan,
         'IFB="%s"' % QOS_IFB,
         'MODE="%s"' % mode,
         '',
         'clear_all() {',
         '  tc qdisc del dev "$WAN" root 2>/dev/null',
         '  tc qdisc del dev "$WAN" clsact 2>/dev/null',
         '  tc qdisc del dev "$IFB" root 2>/dev/null',
         '  nft delete table inet drouter_qos 2>/dev/null',
         '  ip link set "$IFB" down 2>/dev/null',
         '  return 0',
         '}',
         '']

    # ---- 按 IP 限速（HTB）优先于 CAKE 智能模式：两者都建会打架 ----
    if ips:
        L += [
            '# ============ 按内网 IP 精确限速（HTB + mark） ============',
            '# 上行：WAN 出口直接建 HTB 树',
            'tc qdisc replace dev "$WAN" root handle 1: htb default 99',
            'tc class replace dev "$WAN" parent 1: classid 1:1 htb rate %dmbit' % max(up, 1),
            '# 下行：IFB 上建同构 HTB 树（入口重定向见下）',
            'tc qdisc replace dev "$IFB" root handle 1: htb default 99',
            'tc class replace dev "$IFB" parent 1: classid 1:1 htb rate %dmbit' % max(down, 1),
            '',
        ]
        for i, r in enumerate(ips, start=1):
            addr = str((r or {}).get('ip') or '').strip()
            if not re.match(r'^\d+\.\d+\.\d+\.\d+$', addr):
                continue
            try:
                guar = max(1, int((r or {}).get('guar_mbit') or 1))
                ceil = max(guar, int((r or {}).get('ceil_mbit') or guar * 2))
                prio = int((r or {}).get('prio') or 2)
            except Exception:
                guar, ceil, prio = 1, 2, 2
            prio = max(0, min(6, prio))
            cls = 10 + i
            mark = QOS_MARK_BASE + i
            L += [
                '# --- %s : 保证 %dM / 峰值 %dM / 优先级 %d ---' % (addr, guar, ceil, prio),
                'tc class replace dev "$WAN" parent 1:1 classid 1:%d htb rate %dmbit ceil %dmbit prio %d'
                % (cls, guar, ceil, prio),
                'tc qdisc replace dev "$WAN" parent 1:%d fq_codel' % cls,
                'tc filter replace dev "$WAN" parent 1: protocol ip handle 0x%x fw flowid 1:%d'
                % (mark, cls),
                'tc class replace dev "$IFB" parent 1:1 classid 1:%d htb rate %dmbit ceil %dmbit prio %d'
                % (cls, guar, ceil, prio),
                'tc qdisc replace dev "$IFB" parent 1:%d fq_codel' % cls,
                'tc filter replace dev "$IFB" parent 1: protocol ip handle 0x%x fw flowid 1:%d'
                % (mark, cls),
                '',
            ]
    else:
        # ---- CAKE 智能整形（默认推荐） ----
        cap = ' bandwidth %dmbit' % up if up > 0 else ''
        dcap = ' bandwidth %dmbit' % down if down > 0 else ''
        L += [
            '# ============ CAKE 智能整形（公平分流 + 抗 bufferbloat） ============',
            '# nat + dual-srchost/dual-dsthost：NAT 之后仍能识别每台内网设备，按终端公平分配',
            '# 上行：直接挂在 WAN 出口',
            'tc qdisc replace dev "$WAN" root cake%s nat dual-srchost %s' % (cap, mode),
            '',
            '# 下行：入口流量无法直接排队，必须先重定向到 IFB 再整形',
            'ip link set "$IFB" up 2>/dev/null',
            'tc qdisc replace dev "$WAN" handle ffff: ingress',
            'tc filter replace dev "$WAN" parent ffff: protocol all matchall action mirred egress redirect dev "$IFB"',
            'tc qdisc replace dev "$IFB" root cake%s nat dual-dsthost %s' % (dcap, mode),
            '',
        ]

    L += [
        '# ============ DSCP 优先级 / 连接数限制（nftables） ============',
        '# 先删表再加载，保证幂等：nft -f 是「追加」语义，重复执行会把规则叠成两份。',
        'nft delete table inet drouter_qos 2>/dev/null',
        'nft -f /etc/drouter/generated/qos.nft',
        '',
        'exit 0',
        '',
    ]
    return '\n'.join(L)


def _qos_render_unit():
    return '''[Unit]
Description=Drouter QoS 智能限速（CAKE + HTB + DSCP）
After=network-online.target nftables.service
Wants=network-online.target
ConditionPathExists=/etc/drouter/generated/qos.json

[Service]
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Type=oneshot
RemainAfterExit=yes
ExecStart=%(script)s apply
ExecStop=%(script)s clear
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
''' % {'script': QOS_SCRIPT}


def act_qos(p):
    """QoS 智能限速：查询 / 保存 / 应用 / 停用 / 状态下发。"""
    op = str((p or {}).get('op') or 'get')
    d = _qos_load()

    # ---------- 查询 ----------
    if op == 'get':
        missing = []
        for m in ('sch_cake', 'sch_htb', 'sch_fq_codel', 'ifb'):
            rc, _o, _e = sh(['modinfo', m], timeout=8)
            if rc != 0:
                missing.append(m)
        # 与软加速的冲突检测
        try:
            acc = _accel_status()
        except Exception:
            acc = {}
        conflict = bool(acc.get('enabled'))
        rc, o, _e = sh(['systemctl', 'is-active', 'drouter-qos.service'], timeout=8)
        return ok({
            'enabled': bool(d.get('enabled')),
            'running': (o or '').strip() == 'active',
            'mode': d.get('mode') or 'diffserv3',
            'wan_iface': d.get('wan_iface') or '',
            'down_mbit': d.get('down_mbit') or 0,
            'up_mbit': d.get('up_mbit') or 0,
            'ips': d.get('ips') or [],
            'dscp': d.get('dscp') or [],
            'connlimit': bool(d.get('connlimit')),
            'connlimit_max': d.get('connlimit_max') or 500,
            'cake_modes': QOS_CAKE_MODES,
            'presets': QOS_PRESETS,
            'impact': QOS_IMPACT,
            'missing_modules': missing,
            'modules_ok': not missing,
            'accel_conflict': conflict,
            'wan_ifaces': _physical_ifaces(),
            'live': _qos_live(),
        })

    # ---------- 保存 ----------
    if op == 'save':
        try:
            up = max(0, min(100000, int(p.get('up_mbit') or 0)))
            down = max(0, min(100000, int(p.get('down_mbit') or 0)))
        except Exception:
            return fail('带宽必须是数字（单位 Mbit/s）')
        mode = str(p.get('mode') or 'diffserv3')
        if mode not in [m['id'] for m in QOS_CAKE_MODES]:
            return fail('不支持的 CAKE 模式：%s' % mode)
        wan = str(p.get('wan_iface') or d.get('wan_iface') or '').strip()
        if not re.match(r'^[a-zA-Z0-9_.-]+$', wan):
            return fail('WAN 物理网卡名称不合法：%s' % (wan or '未指定'))
        # 内网 IP 限速表
        ips_clean = []
        for i, r in enumerate(p.get('ips') or [], start=1):
            r = r or {}
            addr = str(r.get('ip') or '').strip()
            if not addr:
                continue
            if not re.match(r'^\d+\.\d+\.\d+\.\d+$', addr):
                return fail('第 %d 条限速规则的 IP 格式不合法：%s' % (i, addr))
            try:
                guar = max(1, int(r.get('guar_mbit') or 1))
                ceil = max(guar, int(r.get('ceil_mbit') or guar))
                prio = max(0, min(6, int(r.get('prio') if r.get('prio') is not None else 2)))
            except Exception:
                return fail('第 %d 条限速规则的带宽/优先级不是数字' % i)
            if up and guar > up:
                return fail('第 %d 条保证带宽（%dM）超过上行总带宽（%dM）' % (i, guar, up))
            ips_clean.append({'ip': addr, 'guar_mbit': guar, 'ceil_mbit': ceil, 'prio': prio,
                              'note': str(r.get('note') or '').strip()[:40]})
        if len(ips_clean) > 64:
            return fail('按 IP 限速最多 64 条')
        # DSCP 规则
        dscp_clean = []
        valid = ('cs0', 'cs1', 'cs2', 'cs3', 'cs4', 'cs5', 'cs6', 'cs7',
                 'ef', 'af11', 'af21', 'af31', 'af41')
        for r in p.get('dscp') or []:
            r = r or {}
            pid = str(r.get('preset') or '').strip()
            if not pid:
                continue
            base = next((x for x in QOS_PRESETS if x['id'] == pid), None)
            if not base:
                return fail('不支持的优先级预设：%s' % pid)
            dscp_clean.append({'preset': pid,
                               'dscp': str(r.get('dscp') or base['dscp']),
                               'ports': str(r.get('ports') or base['ports'])})
        try:
            clmax = max(10, min(20000, int(p.get('connlimit_max') or 500)))
        except Exception:
            clmax = 500
        newd = {'enabled': bool(d.get('enabled')), 'mode': mode, 'wan_iface': wan,
                'up_mbit': up, 'down_mbit': down, 'ips': ips_clean, 'dscp': dscp_clean,
                'connlimit': bool(p.get('connlimit')), 'connlimit_max': clmax}
        # 生成 nft 规则并做语法预检。
        #
        # ⚠️ 两个环境坑，必须一起绕开：
        #   A) nft -c（check-only）需要初始化 netlink 缓存，在部分受保护 / 低权
        #      沙箱环境会报 "cache initialization failed: Operation not permitted"，
        #      这并不代表规则本身有语法错误；
        #   B) /tmp 与 /var/tmp 带 sticky 位，内核 fs.protected_regular=2 时 root
        #      也无法覆写低权用户遗留的同名文件 → 见 tmp_nft_path() 的说明。
        nft_text = _qos_render_nft(newd)
        tmp = write_tmp_nft('qos-check', nft_text)
        shadow = None
        try:
            rc, _o, e = sh(['nft', '-c', '-f', tmp], timeout=20)
            if rc != 0:
                if not _nft_environ_blocked(e, _o):
                    return fail('QoS 规则语法检查未通过：%s' % (e or '未知错误'))
                # 环境不支持 check-only 预检 → 用「影子表真实加载」替代。
                # 把表名换成 drouter_qos_check 生成一份规则，真实加载后再删除，
                # 既不污染生产表 drouter_qos，又能完整验证语法与表达式合法性。
                shadow = write_tmp_nft('qos-shadow', _qos_render_nft(newd, table='drouter_qos_check'))
                # 先清掉可能残留的影子表，避免 chain hook 冲突
                sh(['nft', 'delete', 'table', 'inet', 'drouter_qos_check'], timeout=15)
                rc2, o2, e2 = sh(['nft', '-f', shadow], timeout=25)
                sh(['nft', 'delete', 'table', 'inet', 'drouter_qos_check'], timeout=15)
                if rc2 != 0:
                    return fail('QoS 规则加载校验未通过：%s' % (e2 or o2 or '未知错误'))
        finally:
            cleanup_tmp(tmp, shadow)
        with open(QOS_CONF, 'w', encoding='utf-8') as f:
            f.write(nft_text)
        os.chmod(QOS_CONF, 0o644)
        # 生成应用脚本
        with open(QOS_SCRIPT, 'w', encoding='utf-8') as f:
            f.write(_qos_render_script(newd))
        os.chmod(QOS_SCRIPT, 0o755)
        # 生成 systemd 单元
        with open(QOS_UNIT, 'w', encoding='utf-8') as f:
            f.write(_qos_render_unit())
        os.chmod(QOS_UNIT, 0o644)
        _qos_save(newd)
        sh(['systemctl', 'daemon-reload'], timeout=25)
        log('warn', 'system', 'QOS_SAVED',
            '已保存 QoS：上行 %dM / 下行 %dM / %d 条 IP 限速 / %d 条优先级规则'
            % (up, down, len(ips_clean), len(dscp_clean)))
        return ok({'ips': len(ips_clean), 'dscp': len(dscp_clean)},
                  'QoS 配置已保存并生成规则（尚未生效，需点击「启用」）')

    # ---------- 启用 ----------
    if op == 'on':
        if os.path.exists(BUILD_FLAG):
            return fail('构建保护模式已开启，禁止执行会改变网络行为的动作。')
        if p.get('confirm') is not True:
            return fail('启用 QoS 会改变流量转发行为，请勾选确认后再执行')
        d = _qos_load()
        wan = (d.get('wan_iface') or '').strip()
        if not wan:
            return fail('尚未配置 WAN 网卡，请先保存 QoS 配置')
        rc, o, _e = sh(['ip', 'link', 'show', wan], timeout=6)
        if rc != 0:
            return fail('WAN 网卡 %s 不存在，请先在「WAN 口」里配置' % wan)
        # 冲突检测：软加速会让分类失效
        acc = _accel_status()
        if acc.get('enabled') and p.get('ignore_accel') is not True:
            return fail('检测到「软加速（flowtable）」正在运行，它会绕过 netfilter 导致 '
                        'DSCP 打标与 IP 限速失效。请先到「网络状态 / 加速」页关闭软加速，'
                        '或勾选「我了解冲突，仍然继续」。')
        missing = _qos_modprobe()
        if missing:
            return fail('缺少内核模块：%s。请确认使用 Debian 官方内核。' % '、'.join(missing))
        okifb, err = _qos_ifb_ensure()
        if not okifb:
            return fail('创建下行整形设备 %s 失败：%s' % (QOS_IFB, err))
        # 应用规则
        rc, o, e = sh(['sh', QOS_SCRIPT, 'apply'], timeout=60)
        if rc != 0:
            return fail('应用 QoS 规则失败：%s' % (e or o or '未知错误'),
                        data={'log': (o or '') + (e or '')})
        # 校验 tc 规则确实挂上了
        live = _qos_live()
        if not live.get('nft_ok'):
            return fail('nftables 规则未生效，请查看下方日志',
                        data={'log': live.get('nft_dump') or ''})
        d['enabled'] = True
        try:
            _qos_save(d)
        except Exception as ex:
            return fail('QoS 已应用，但状态回写失败：%s' % ex)
        sh(['systemctl', 'enable', 'drouter-qos.service'], timeout=20)
        # 用 systemctl start 记录 RemainAfterExit 状态（便于开机自恢复）
        sh(['systemctl', 'start', 'drouter-qos.service'], timeout=40)
        log('warn', 'system', 'QOS_ON', 'QoS 已启用（WAN=%s，模式=%s）' % (wan, d.get('mode')))
        return ok({'live': _qos_live()},
                  'QoS 已启用：%s。CAKE 已在 WAN 与 %s 上接管排队。'
                  % ('按 IP 限速（HTB）' if d.get('ips') else 'CAKE 智能整形', QOS_IFB))

    # ---------- 停用 ----------
    if op == 'off':
        sh(['systemctl', 'stop', 'drouter-qos.service'], timeout=40)
        sh(['systemctl', 'disable', 'drouter-qos.service'], timeout=20)
        rc, o, e = sh(['sh', QOS_SCRIPT, 'clear'], timeout=60)
        # 双保险：无论脚本是否成功，都手工清一遍
        d = _qos_load()
        wan = (d.get('wan_iface') or '').strip()
        if wan:
            sh(['tc', 'qdisc', 'del', 'dev', wan, 'root'], timeout=10)
            sh(['tc', 'qdisc', 'del', 'dev', wan, 'clsact'], timeout=10)
            sh(['tc', 'qdisc', 'del', 'dev', wan, 'ingress'], timeout=10)
        sh(['tc', 'qdisc', 'del', 'dev', QOS_IFB, 'root'], timeout=10)
        sh(['ip', 'link', 'set', QOS_IFB, 'down'], timeout=8)
        sh(['nft', 'delete', 'table', 'inet', 'drouter_qos'], timeout=10)
        d['enabled'] = False
        try:
            _qos_save(d)
        except Exception as ex:
            return fail('QoS 已停用，但状态回写失败：%s' % ex)
        log('warn', 'system', 'QOS_OFF', 'QoS 已关闭，规则已清理')
        return ok({'live': _qos_live()}, 'QoS 已关闭：CAKE / HTB 规则已全部移除，转发恢复默认')

    return fail('未知操作：%s' % op)


# ------------------------------------------------------------------ DPI 识别库
#
# 用途：按应用/协议识别流量（爱快的杀手锏）。Debian 上对应 nDPI / Suricata。
# 痛点：nDPI 规则库与源码都在 GitHub，国内直连常常超时或极慢。
# 方案：内置多个公共加速前缀，用户可任选其一（也可自定义或直连），并支持自动测速优选。

DPI_HOSTS = {
    # 键为「代理前缀」，拼在原始 https://github.com/... 前面
    'direct': {'name': '直连（不使用代理）', 'prefix': '',
               'why': '不使用任何代理。海外网络或已自建代理时选这个。'},
    'ghproxy': {'name': 'ghproxy.com', 'prefix': 'https://ghproxy.com/',
                'why': '老牌公共加速，稳定性一般，偶尔限流。'},
    'gh-proxy': {'name': 'gh-proxy.com', 'prefix': 'https://gh-proxy.com/',
                 'why': '备用加速，支持 release 文件与 raw 文件。'},
    'moeyy': {'name': 'moeyy.cn', 'prefix': 'https://github.moeyy.xyz/',
              'why': '对 raw 与 archive 都支持，速度较好。'},
    'ghfast': {'name': 'ghfast.top', 'prefix': 'https://ghfast.top/',
               'why': '目前较活跃的加速节点之一。'},
    'ghproxy-net': {'name': 'ghproxy.net', 'prefix': 'https://ghproxy.net/',
                    'why': '备用节点，适合前几个都超时时尝试。'},
    'gitclone': {'name': 'gitclone.com', 'prefix': 'https://gitclone.com/github.com/',
                 'why': '整站镜像式代理，适合 git clone（注意 URL 拼接方式不同）。'},
}

# DPI 库来源（可更新项）
DPI_SOURCES = [
    {'id': 'ndpi-protos', 'name': 'nDPI 协议识别规则库',
     'repo': 'ntop/nDPI', 'kind': 'archive',
     'target': '/opt/drouter/dpi/ndpi',
     'why': 'nDPI 的协议特征定义（src/lib/protocols + lists）。'
            '它是「认出这是什么应用」的核心数据。'},
    {'id': 'suricata-rules', 'name': 'Suricata 应用识别规则集',
     'repo': 'OISF/suricata-update', 'kind': 'archive',
     'target': '/opt/drouter/dpi/suricata-update',
     'why': 'suricata-update 工具与默认规则源，用于应用层封禁/打标。'},
    {'id': 'ndpi-netfilter', 'name': 'nDPI Netfilter 内核模块',
     'repo': 'ndpi-netfilter/ndpi-netfilter', 'kind': 'archive',
     'target': '/opt/drouter/dpi/ndpi-netfilter',
     'why': '提供 xt_ndpi，让 nftables/iptables 能直接按应用打标。需自行编译。'},
]

DPI_STATE = '/opt/drouter/dpi/state.json'


def _dpi_load_state():
    try:
        if os.path.isfile(DPI_STATE):
            with open(DPI_STATE, encoding='utf-8') as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}


def _dpi_save_state(d):
    os.makedirs(os.path.dirname(DPI_STATE), exist_ok=True)
    with open(DPI_STATE, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.chmod(DPI_STATE, 0o644)


def _dpi_prefix(p):
    """解析用户选择的代理前缀，返回 (prefix, key, err)。支持自定义 URL。"""
    key = str(p.get('mirror') or 'ghproxy').strip()
    custom = str(p.get('custom_prefix') or '').strip()
    if key == 'custom':
        if not custom:
            return '', key, '选择了自定义前缀，但没有填写地址'
        if not re.match(r'^https?://[A-Za-z0-9.\-]+/', custom):
            return '', key, '自定义前缀必须形如 https://example.com/'
        if not custom.endswith('/'):
            custom += '/'
        return custom, key, ''
    if key not in DPI_HOSTS:
        return '', key, '不支持的加速前缀：%s' % key
    return DPI_HOSTS[key]['prefix'], key, ''


def _dpi_url(repo, kind, prefix):
    """拼出下载地址。archive 走 codeload（体积小、可直接解开）。"""
    if kind == 'codeload':
        base = 'https://codeload.github.com/%s/tar.gz/refs/heads/master' % repo
    elif kind == 'archive':
        base = 'https://github.com/%s/archive/refs/heads/master.tar.gz' % repo
    else:
        base = 'https://github.com/%s.git' % repo
    # 注意 gitclone 这类「整站镜像」前缀，拼接规则与普通前缀不同
    if prefix.endswith('github.com/'):
        return prefix + repo + '/archive/refs/heads/master.tar.gz'
    return (prefix + base) if prefix else base


def _dpi_test_mirror(p):
    """对若干个候选前缀做连通性测速，返回可用列表（按耗时升序）。

    ⚠️ 测速用的 URL 必须与真正下载时一致，否则会「测出来能用、真下载失败」。
    实测经验：多数公共加速（gh-proxy / ghfast 等）只代理 github.com 下的
    release / archive 路径，**不代理 codeload.github.com**；而 gitclone 是整站
    镜像、拼接规则又不同。因此这里统一用 archive 形式（与 DPI_SOURCES 的
    kind='archive' 一致）来测，避免误判。
    """
    cand = p.get('mirrors')
    if not isinstance(cand, list) or not cand:
        cand = ['direct', 'ghproxy', 'gh-proxy', 'moeyy', 'ghfast', 'ghproxy-net']
    # 用 nDPI 的 archive 包（与真正更新时的首个来源同形），只取头 1 字节
    repo = 'ntop/nDPI'
    test_base = 'https://github.com/%s/archive/refs/heads/master.tar.gz' % repo
    results = []
    for key in cand:
        key = str(key)
        if key == 'custom':
            pre = str(p.get('custom_prefix') or '').strip()
            if not pre:
                continue
            if not pre.endswith('/'):
                pre += '/'
            name = '自定义'
        elif key in DPI_HOSTS:
            pre = DPI_HOSTS[key]['prefix']
            name = DPI_HOSTS[key]['name']
        else:
            continue
        # 与 _dpi_url 完全一致的拼接规则（gitclone 是整站镜像，写法不同）
        if pre.endswith('github.com/'):
            url = pre + repo + '/archive/refs/heads/master.tar.gz'
        else:
            url = (pre + test_base) if pre else test_base
        t0 = time.time()
        # 只做「能否连上并开始收数据」的判断，不追求下完整个包：
        #   大多数公共加速（ghproxy.net / ghfast / moeyy 等）会忽略 Range 头，
        #   直接用 -r 0-0 会变成「下载整个几十 MB 的包」→ 必然超时报假阴性。
        #   因此改成：限时 4 秒、限速 16KB/s，只要在时限内成功拿到 >=1 字节且
        #   HTTP 码为 2xx 就算可用；拿不到再判超时。
        rc, o, _e = sh(['curl', '-sSL', '-m', '4', '--speed-limit', '16384',
                        '--speed-time', '3', '-o', '/dev/null',
                        '-w', '%{http_code} %{size_download}', url], timeout=10)
        dt = round(time.time() - t0, 2)
        code, got = '', 0
        try:
            parts = (o or '').strip().split()
            code = parts[0] if parts else ''
            got = int(parts[1]) if len(parts) > 1 else 0
        except Exception:
            code, got = '', 0
        # curl(28)=超时/限速中止，但只要已拿到数据且码为 2xx 仍算连通
        okk = code.startswith('2') and got > 0
        results.append({'key': key, 'name': name, 'prefix': pre, 'url': url,
                        'ok': okk, 'secs': dt, 'code': code, 'bytes': got,
                        'err': '' if okk else ('超时' if dt >= 3.9 else '连接失败')})
    results.sort(key=lambda x: (not x['ok'], x['secs']))
    return results


def _dpi_installed():
    """探测系统上已有的 DPI 组件。"""
    out = {}
    for name, cmd in (('ndpiReader', 'ndpiReader'), ('suricata', 'suricata'),
                      ('ntopng', 'ntopng'), ('ndpi-netfilter', 'xt_ndpi')):
        # 性能：原为 bash -lc 'command -v X'（每个组件 fork 一个 bash，最贵的一种）；
        # xt_ndpi 是内核模块名，不在 PATH 里，所以沿用 modinfo 判断（见下）。
        if cmd == 'xt_ndpi':
            continue
        p = shutil.which(cmd) or ''
        out[name] = {'present': bool(p), 'path': p}
    # ndpi-netfilter 实际以内核模块形式存在
    rc, _o, _e = sh(['modinfo', 'xt_ndpi'], timeout=8)
    out.setdefault('ndpi-netfilter', {})
    if not out['ndpi-netfilter'].get('present'):
        out['ndpi-netfilter'] = {'present': rc == 0, 'path': 'xt_ndpi' if rc == 0 else ''}
    # 内核模块 xt_ndpi 是否存在
    out['xt_ndpi_module'] = {'present': rc == 0}
    # apt 仓库里有没有现成包（比源码编译省事）
    try:
        rc, o, _e = sh(['apt-cache', 'policy', 'libndpi-dev'], timeout=15)
        out['apt_ndpi'] = {'available': 'Candidate:' in (o or '') and
                           'none' not in (o or '').split('Candidate:')[1][:20]}
    except Exception:
        out['apt_ndpi'] = {'available': False}
    return out


def act_dpi(p):
    """DPI 识别库：状态 / 测速 / 更新 / 清理。"""
    op = str((p or {}).get('op') or 'get')
    state = _dpi_load_state()

    # ---------- 查询 ----------
    if op == 'get':
        items = []
        for s in DPI_SOURCES:
            tgt = s['target']
            present = os.path.isdir(tgt)
            ver = ''
            if present:
                try:
                    mt = max(os.path.getmtime(os.path.join(tgt, x))
                             for x in os.listdir(tgt)) if os.listdir(tgt) else os.path.getmtime(tgt)
                    ver = datetime.fromtimestamp(mt).strftime('%Y-%m-%d %H:%M')
                except Exception:
                    ver = ''
            items.append(dict(s, present=present, version=ver,
                              size_h=_dpi_size(tgt)))
        return ok({
            'sources': items,
            'mirrors': [dict(v, key=k) for k, v in DPI_HOSTS.items()],
            'installed': _dpi_installed(),
            'state': state,
            'last_mirror': state.get('mirror') or '',
            'last_update': state.get('last_update') or '',
            'dpi_dir': '/opt/drouter/dpi',
            'presets': [
                {'id': 'apt', 'name': 'APT 安装（最省事）',
                 'cmd': 'apt install -y libndpi-dev ndpi suricata ntopng',
                 'why': 'Debian 13 仓库直接装，无需编译。nDPI 版本可能略旧，但够家用。'},
                {'id': 'source', 'name': '源码编译 nDPI（版本最新）',
                 'cmd': 'git clone <prefix>ntop/nDPI && make && make install',
                 'why': '拿到最新协议特征库（新应用识别更快），但编译耗时较长。'},
                {'id': 'netfilter', 'name': 'nDPI Netfilter（按应用打标）',
                 'cmd': '编译 xt_ndpi 内核模块后配合 nftables dport/mark',
                 'why': '让 nftables 按应用分类并交给 QoS 限速。需内核头文件支持。'},
            ],
            'notes': [
                'DPI 需要独立进程（nDPI Reader / Suricata），会额外占用内存；'
                '本机为 4GB 内存的虚拟机，建议只装 nDPI 一项。',
                '加密流量（HTTPS/QUIC）无法被 DPI 完整识别，这是所有厂商的共同限制。',
                '规则库默认从 GitHub 获取，国内直连常常超时 —— 请在下方选择加速前缀。',
            ],
        })

    # ---------- 代理测速 ----------
    if op == 'test':
        res = _dpi_test_mirror(p)
        good = [r for r in res if r['ok']]
        if good:
            best = min(good, key=lambda x: x['secs'])
            msg = ('最快的可用前缀是「%s」（%.2fs，已收到 %d 字节）'
                   % (best['name'], best['secs'], best['bytes']))
        else:
            best = None
            msg = ('所有候选项都不可用。已内置的公共加速可能临时失效，'
                   '建议改用自定义前缀（自建代理），或稍后重试')
        return ok({'results': res, 'best': best and best['key']}, msg)

    # ---------- 更新 ----------
    if op == 'update':
        if os.path.exists(BUILD_FLAG):
            return fail('构建保护模式已开启，禁止执行下载/构建类动作。')
        prefix, key, err = _dpi_prefix(p)
        if err:
            return fail(err)
        only = p.get('only') or []
        if not isinstance(only, list) or not only:
            only = [s['id'] for s in DPI_SOURCES]
        os.makedirs('/opt/drouter/dpi', exist_ok=True)
        os.makedirs('/var/log/drouter', exist_ok=True)
        logfile = '/var/log/drouter/dpi-update.log'
        lines = ['===== DPI 更新开始 %s =====' % datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                 '加速前缀：%s (%s)' % (key, prefix or '直连'), '']
        results = []
        # 下载/解包的工作目录放在 root 私有目录：/var/tmp 的 sticky 位会让
        # curl -o 覆写他人遗留的同名文件时报 Permission denied。
        workdir = os.path.join(HELPER_TMP_DIR, 'dpi')
        os.makedirs(workdir, exist_ok=True)
        try:
            os.chmod(workdir, 0o700)
        except Exception:
            pass
        for s in DPI_SOURCES:
            if s['id'] not in only:
                continue
            url = _dpi_url(s['repo'], s['kind'], prefix)
            tgz = os.path.join(workdir, 'dpi-%s.tar.gz' % s['id'])
            lines.append('--- %s ---' % s['name'])
            lines.append('URL: %s' % url)
            rc, o, e = sh(['curl', '-sSL', '--connect-timeout', '12', '--max-time', '300',
                           '-#', '-o', tgz, url], timeout=360)
            if rc != 0 or not os.path.isfile(tgz) or os.path.getsize(tgz) < 1024:
                detail = (e or o or '下载失败').strip()[-400:]
                lines.append('❌ 下载失败：%s' % detail)
                lines.append('   提示：换一个加速前缀重试，或用「测速」挑最快的那个。')
                results.append({'id': s['id'], 'name': s['name'], 'ok': False,
                                'err': '下载失败', 'detail': detail})
                continue
            size = os.path.getsize(tgz)
            lines.append('✔ 已下载 %.1f MB' % (size / 1048576.0))
            # 解包到 target（先解到临时目录再原子替换，避免解包失败把旧版本弄坏）
            tmpdir = os.path.join(workdir, 'unpack-%s' % s['id'])
            shutil.rmtree(tmpdir, ignore_errors=True)
            os.makedirs(tmpdir, exist_ok=True)
            rc, o, e = sh(['tar', 'xzf', tgz, '-C', tmpdir], timeout=180)
            if rc != 0:
                detail = (e or o or '解包失败').strip()[-400:]
                lines.append('❌ 解包失败：%s' % detail)
                results.append({'id': s['id'], 'name': s['name'], 'ok': False,
                                'err': '解包失败', 'detail': detail})
                continue
            # 归一化：把解出来的顶层目录移动到 target
            subs = [x for x in os.listdir(tmpdir) if os.path.isdir(os.path.join(tmpdir, x))]
            src = os.path.join(tmpdir, subs[0]) if len(subs) == 1 else tmpdir
            tgt = s['target']
            keep = tgt + '.old'
            shutil.rmtree(keep, ignore_errors=True)
            if os.path.isdir(tgt):
                shutil.move(tgt, keep)
            os.makedirs(os.path.dirname(tgt), exist_ok=True)
            shutil.move(src, tgt)
            shutil.rmtree(tmpdir, ignore_errors=True)
            shutil.rmtree(keep, ignore_errors=True)
            try:
                os.remove(tgz)
            except Exception:
                pass
            nfiles = sum(len(f) for _r, _d, f in os.walk(tgt))
            lines.append('✔ 已安装到 %s（%d 个文件）' % (tgt, nfiles))
            results.append({'id': s['id'], 'name': s['name'], 'ok': True,
                            'files': nfiles, 'size_kb': size // 1024, 'err': ''})
        lines.append('')
        lines.append('=== 汇总：成功 %d / 失败 %d ==='
                     % (len([r for r in results if r['ok']]),
                        len([r for r in results if not r['ok']])))
        with open(logfile, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        state['mirror'] = key
        state['prefix'] = prefix
        state['last_update'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        state['results'] = results
        _dpi_save_state(state)
        nok = len([r for r in results if r['ok']])
        nbad = len(results) - nok
        log('warn', 'system', 'DPI_UPDATE', 'DPI 库更新完成：成功 %d 失败 %d' % (nok, nbad))
        return ok({'results': results, 'log': '\n'.join(lines), 'logfile': logfile},
                  '更新完成：成功 %d 项，失败 %d 项（可使用前缀 %s）' % (nok, nbad, key))

    # ---------- 安装建议（不真正执行 apt，给出可复制命令） ----------
    if op == 'plan':
        which = str(p.get('which') or 'apt')
        if which == 'apt':
            cmds = ['apt update',
                    'apt install -y libndpi-dev ndpi suricata ntopng',
                    '# 装完可用：ndpiReader -i ens19 -v 2 查看实时识别结果']
        elif which == 'source':
            pre = str(p.get('prefix') or '')
            cmds = ['apt install -y build-essential git libpcap-dev libjson-c-dev',
                    'cd /opt/drouter/dpi',
                    'git clone %sntop/nDPI.git' % (pre or 'https://github.com/'),
                    'cd nDPI && ./autogen.sh && ./configure && make -j2',
                    'make install && ldconfig']
        else:
            cmds = ['# 需先准备与当前内核匹配的 headers',
                    'apt install -y linux-headers-$(uname -r)',
                    'cd /opt/drouter/dpi/ndpi-netfilter',
                    'make && make install',
                    'modprobe xt_ndpi',
                    '# 之后可在 nftables 里按应用匹配打标，交给 QoS 限速']
        return ok({'which': which, 'cmds': cmds},
                  '已生成 %s 方式的操作步骤（可复制到 Web 终端执行）' % which)

    # ---------- 读取更新日志 ----------
    if op == 'log':
        try:
            with open('/var/log/drouter/dpi-update.log', encoding='utf-8') as f:
                return ok({'log': f.read()[-20000:]})
        except Exception:
            return ok({'log': ''}, '暂无更新日志')

    return fail('未知操作：%s' % op)


def _dpi_size(path):
    """粗略统计目录大小（人类可读）。"""
    if not os.path.isdir(path):
        return ''
    total = 0
    try:
        for r, _d, files in os.walk(path):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(r, fn))
                except Exception:
                    pass
                if total > 200 * 1024 * 1024:
                    return '>200 MB'
    except Exception:
        pass
    for unit, div in (('GB', 1073741824), ('MB', 1048576), ('KB', 1024)):
        if total >= div:
            return '%.1f %s' % (total / float(div), unit)
    return '%d B' % total


# ------------------------------------------------------------------ VLAN
#
# 用途：IPTV 通常需要把运营商 VLAN（如 85/3961）单独划分出来。
# 实现：创建 <父网卡>.<vlan_id> 虚拟接口，可选桥接与 DHCP/静态地址。

VLAN_CONF = '/etc/drouter/generated/vlans.json'


def _load_vlans():
    try:
        if os.path.isfile(VLAN_CONF):
            with open(VLAN_CONF, encoding='utf-8') as f:
                return json.load(f) or []
    except Exception:
        pass
    return []


def _save_vlans(items):
    os.makedirs(os.path.dirname(VLAN_CONF), exist_ok=True)
    with open(VLAN_CONF, 'w', encoding='utf-8') as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.chmod(VLAN_CONF, 0o644)


VLAN_CREATED_FILE = '/var/lib/drouter/vlan-created.json'


def _vlan_created_load():
    """本面板亲手创建的 VLAN 接口名集合。

    维护它的唯一目的：**不要**去删不是自己创建的 VLAN。管理员可能用
    `ip link add` 手建了跑业务的 VLAN，点一次「应用」把它删掉是不可接受的。
    """
    try:
        if os.path.isfile(VLAN_CREATED_FILE):
            with open(VLAN_CREATED_FILE, encoding='utf-8') as f:
                v = json.load(f)
            if isinstance(v, list):
                return set(str(x) for x in v if re.match(r'^[a-zA-Z0-9_.-]+\.\d+$', str(x)))
    except Exception:
        pass
    return set()


def _vlan_created_save(names):
    try:
        os.makedirs(os.path.dirname(VLAN_CREATED_FILE), exist_ok=True)
        lst = sorted(set(str(x) for x in names))[:200]
        _atomic_write(VLAN_CREATED_FILE, json.dumps(lst, ensure_ascii=False), mode=0o644)
    except Exception:
        pass


def _vlan_exists(name):
    rc, _o, _e = sh(['ip', 'link', 'show', name], timeout=6)
    return rc == 0


def act_vlan(p):
    """VLAN：查询 / 新建 / 删除 / 应用。"""
    op = str((p or {}).get('op') or 'get')
    items = _load_vlans()

    if op == 'get':
        for it in items:
            nm = '%s.%s' % (it.get('parent'), it.get('vid'))
            it['_iface'] = nm
            it['_present'] = _vlan_exists(nm)
            rc, o, _e = sh(['ip', '-4', '-o', 'addr', 'show', nm], timeout=6)
            m = re.search(r'inet\s+(\S+)', o or '')
            it['_addr'] = m.group(1) if m else ''
        return ok({'items': items, 'ifaces': _physical_ifaces(),
                   'presets': [
                       {'name': '中国电信 IPTV', 'vid': 85},
                       {'name': '中国联通 IPTV', 'vid': 3961},
                       {'name': '中国移动 IPTV', 'vid': 3961},
                       {'name': '通用 VLAN', 'vid': 100},
                   ]})

    if op == 'save':
        raw = p.get('items')
        # 兼容两种调用方式：
        #   1) 整表替换：{"items": [ ... ]}
        #   2) 追加/更新单条：{"parent": "ens19", "vid": 85, "name": "iptv"}
        if raw is None and (p.get('parent') or p.get('vid')):
            one = {'parent': p.get('parent'), 'vid': p.get('vid'),
                   'mode': p.get('mode') or ('static' if p.get('address') else 'none'),
                   'address': p.get('address'), 'gateway': p.get('gateway'),
                   'note': p.get('note') or p.get('name') or p.get('purpose'),
                   'up': True}
            raw = [dict(x) for x in items]
            key = '%s.%s' % (str(one.get('parent') or '').strip(), one.get('vid'))
            raw = [x for x in raw if '%s.%s' % (x.get('parent'), x.get('vid')) != key]
            raw.append(one)
        if not isinstance(raw, list):
            return fail('缺少 VLAN 列表')
        clean = []
        for it in raw:
            parent = str((it or {}).get('parent') or '').strip()
            if not re.match(r'^[a-zA-Z0-9_.-]+$', parent):
                return fail('父网卡名称不合法：%s' % parent)
            try:
                vid = int((it or {}).get('vid') or 0)
            except Exception:
                return fail('VLAN ID 必须是数字')
            if not (1 <= vid <= 4094):
                return fail('VLAN ID 必须在 1-4094 之间（收到 %s）' % vid)
            mode = str((it or {}).get('mode') or 'dhcp')
            if mode not in ('dhcp', 'static', 'none'):
                return fail('地址获取方式不合法：%s' % mode)
            rec = {'parent': parent, 'vid': vid, 'mode': mode,
                   'address': str((it or {}).get('address') or '').strip(),
                   'gateway': str((it or {}).get('gateway') or '').strip(),
                   'note': str((it or {}).get('note') or '').strip()[:60],
                   'up': bool((it or {}).get('up', True))}
            if mode == 'static' and not re.match(r'^\d+\.\d+\.\d+\.\d+/\d+$', rec['address']):
                return fail('VLAN %s 的静态地址格式应为 192.168.1.2/24' % vid)
            clean.append(rec)
        # 唯一性
        seen = set()
        for r in clean:
            key = '%s.%s' % (r['parent'], r['vid'])
            if key in seen:
                return fail('存在重复的 VLAN 接口：%s' % key)
            seen.add(key)
        _save_vlans(clean)
        log('warn', 'system', 'VLAN_SAVED', '已保存 %d 个 VLAN 配置' % len(clean))
        return ok({'items': clean}, '已保存 %d 个 VLAN' % len(clean))

    if op == 'apply':
        items = _load_vlans()
        existing = set()
        rc, o, _e = sh(['ip', '-o', 'link', 'show'], timeout=8)
        for line in (o or '').splitlines():
            m = re.match(r'^\d+:\s+([^:@]+)', line)
            if m:
                existing.add(m.group(1).strip())
        applied, removed, skipped = [], [], []
        created = _vlan_created_load()
        want = set()
        for it in items:
            nm = '%s.%s' % (it['parent'], it['vid'])
            want.add(nm)
            if nm not in existing:
                rc, _o, e = sh(['ip', 'link', 'add', 'link', it['parent'],
                                'name', nm, 'type', 'vlan', 'id', str(it['vid'])], timeout=12)
                if rc != 0:
                    log('error', 'system', 'VLAN_ADD_FAIL', '创建 %s 失败：%s' % (nm, e))
                    continue
                created.add(nm)          # 记下「这是我们建的」，以后才有权回收
                _vlan_created_save(created)
            if it.get('up', True):
                sh(['ip', 'link', 'set', nm, 'up'], timeout=8)
            if it.get('mode') == 'static' and it.get('address'):
                sh(['ip', 'addr', 'replace', it['address'], 'dev', nm], timeout=8)
            elif it.get('mode') == 'dhcp':
                # 交给 dhcpcd 管理（不在此处直接跑 dhclient，避免与现有网络管理器冲突）
                pass
            applied.append(nm)
        # 删除不再需要的 —— **只**删本面板亲手创建的。
        # 早先这里是「凡是 x.y 形式且不在当前清单里就删」：管理员手工 `ip link add`
        # 建的（比如跑着 IPTV 业务的 ens19.100）会在点「应用」时被一声不响删掉。
        created = _vlan_created_load()
        for line in (o or '').splitlines():
            m = re.match(r'^\d+:\s+([^:@]+)', line)
            if not m:
                continue
            nm = m.group(1).strip()
            if re.match(r'^[a-zA-Z0-9_.-]+\.\d+(@[a-zA-Z0-9_.-]+)?$', nm) and nm.split('@')[0] not in want:
                if nm.split('@')[0] not in created:
                    skipped.append(nm)
                    continue
                sh(['ip', 'link', 'del', nm], timeout=8)
                removed.append(nm)
        log('warn', 'system', 'VLAN_APPLIED',
            '已应用 VLAN：新增/更新 %d，回收 %d，跳过非面板创建 %d'
            % (len(applied), len(removed), len(skipped)))
        msg = '已应用 VLAN 配置（生效 %d 个）' % len(applied)
        if skipped:
            msg += ('；另有 %d 个 VLAN 不是本面板创建的，已跳过（%s）'
                    % (len(skipped), '、'.join(skipped[:5])))
        return ok({'applied': applied, 'removed': removed, 'skipped': skipped}, msg)

    if op == 'delete':
        nm = str(p.get('iface') or p.get('id') or '').strip()
        if not re.match(r'^[a-zA-Z0-9_.-]+\.\d+$', nm):
            return fail('接口名不合法：%s（应形如 ens19.85）' % nm)
        sh(['ip', 'link', 'del', nm], timeout=10)
        created = _vlan_created_load()
        if nm in created:
            created.discard(nm)
            _vlan_created_save(created)
        items = [x for x in _load_vlans() if '%s.%s' % (x['parent'], x['vid']) != nm]
        _save_vlans(items)
        log('warn', 'system', 'VLAN_DEL', '已删除 VLAN 接口 %s' % nm)
        return ok({'iface': nm}, '已删除 VLAN 接口 %s' % nm)

    return fail('未知操作：%s' % op)


# ------------------------------------------------------------------ WOL 网络唤醒

def act_wol(p):
    """网络唤醒：查询 / 发送魔术包 / 保存常用设备。"""
    op = str((p or {}).get('op') or 'get')
    if op == 'get':
        saved = _load_setting('wol') or {'devices': []}
        ifaces = []
        for n in _physical_ifaces():
            rc, o, _e = sh(['ethtool', n], timeout=8)
            o = o or ''
            m_sup = re.search(r'Supports Wake-on:\s*(\S+)', o)
            m_cur = re.search(r'Wake-on:\s*(\S+)', o)
            supp = bool(m_sup)
            cur = m_cur.group(1) if m_cur else ''
            reason = ''
            if not supp:
                # 没有 Supports Wake-on 行：多数是虚拟网卡（virtio/e1000 模拟）
                if re.search(r'(?i)(virtio|vmware|hyper-v)', o):
                    reason = '这是虚拟网卡，硬件不支持 WOL'
                else:
                    reason = '该网卡或驱动未上报 WOL 能力'
            ifaces.append({
                'name': n,
                'supports_wol': supp,
                'wol_supported': supp,
                'enabled': bool(cur) and any(c in cur for c in 'gdm'),
                'modes': cur,
                'wol_mode': cur,
                'capable': (m_sup.group(1) if m_sup else ''),
                'reason': reason,
            })
        return ok({'devices': saved.get('devices') or [], 'ifaces': ifaces,
                   'targets': saved.get('devices') or []})

    if op == 'iface':
        nm = str(p.get('iface') or '').strip()
        on = bool(p.get('enabled'))
        if not re.match(r'^[a-zA-Z0-9_.-]+$', nm):
            return fail('网卡名不合法')
        val = 'g' if on else 'd'
        rc, _o, e = sh(['ethtool', '-s', nm, 'wol', val], timeout=10)
        if rc != 0:
            return fail('设置 WOL 失败：%s（可能是网卡或驱动不支持）' % (e or '未知错误'))
        log('warn', 'system', 'WOL_IFACE', '网卡 %s 的 WOL 已%s' % (nm, '开启' if on else '关闭'))
        return ok({'iface': nm, 'enabled': on},
                  '%s 的唤醒功能已%s' % (nm, '开启' if on else '关闭'))

    if op == 'save':
        raw = p.get('devices')
        if not isinstance(raw, list):
            return fail('缺少设备列表')
        clean = []
        for it in raw:
            mac = re.sub(r'[^0-9a-fA-F]', '', str((it or {}).get('mac') or ''))
            if len(mac) != 12:
                return fail('MAC 地址不合法：%s' % (it or {}).get('mac'))
            mac = ':'.join(mac[i:i + 2] for i in range(0, 12, 2)).lower()
            clean.append({
                'name': str((it or {}).get('name') or '').strip()[:40] or mac,
                'mac': mac,
                'ip': str((it or {}).get('ip') or '').strip(),
                'iface': str((it or {}).get('iface') or '').strip(),
                'note': str((it or {}).get('note') or '').strip()[:60],
            })
        _save_setting('wol', {'devices': clean})
        return ok({'devices': clean}, '已保存 %d 台唤醒设备' % len(clean))

    if op == 'wake':
        mac_raw = str(p.get('mac') or '')
        mac = re.sub(r'[^0-9a-fA-F]', '', mac_raw)
        if len(mac) != 12:
            return fail('MAC 地址不合法：%s' % mac_raw)
        mac = mac.lower()
        target = str(p.get('broadcast') or '').strip()
        iface = str(p.get('iface') or '').strip()
        # 网卡名既会拼进 sh -c，又会被当成 etherwake 的参数，必须严格白名单。
        # 同一个函数的 op='iface' 分支早就有这个校验，wake 分支漏了。
        if iface and not re.match(r'^[a-zA-Z0-9_.-]{1,15}$', iface):
            return fail('网卡名不合法：%s' % iface)
        if target:
            try:
                ipaddress.IPv4Address(target)
            except Exception:
                return fail('广播地址不合法：%s' % target)
        sent = []

        def _mcast(m):
            return bytes.fromhex('FF' * 6 + m * 16)
        payload = _mcast(mac)
        # 1) 用 etherwake（若安装）
        rc, o, e = sh(['which', 'etherwake'], timeout=6)
        if rc == 0 and iface:
            rc2, _o2, e2 = sh(['etherwake', '-i', iface, mac], timeout=10)
            if rc2 == 0:
                sent.append('etherwake via %s' % iface)
        # 2) 用 Python 直接发 UDP 魔术包（不依赖外部命令）
        bcasts = []
        if target:
            bcasts.append(target)
        if iface:
            # 早先这里是 sh -c "ip ... | awk '{print $4}'"，网卡名直接进 shell。
            # 改成列表调用再自己取地址，顺手少 fork 一个 sh 和一个 awk。
            rc3, o3, _e3 = sh(['ip', '-4', '-o', 'addr', 'show', 'dev', iface],
                              timeout=6)
            for line in (o3 or '').splitlines():
                m = re.search(r'\binet\s+(\S+)', line)
                if not m:
                    continue
                try:
                    net = ipaddress.IPv4Interface(m.group(1))
                    bcasts.append(str(net.network.broadcast_address))
                except Exception:
                    pass
        for b in ('255.255.255.255', '192.168.7.255', '192.168.1.255'):
            if b not in bcasts:
                bcasts.append(b)
        # 多端口多次发送，提高成功率
        for port in (9, 7):
            for b in bcasts[:4]:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    if iface:
                        try:
                            s.setsockopt(socket.SOL_SOCKET, 25, (iface + '\0').encode())
                        except Exception:
                            pass
                    s.sendto(payload, (b, port))
                    s.close()
                    sent.append('UDP %s:%d' % (b, port))
                except Exception as e4:
                    sent.append('UDP %s:%d 失败(%s)' % (b, port, e4))
        log('warn', 'system', 'WOL_SENT', '已向 %s 发送唤醒包' % mac, sent)
        # 可选：把该设备加入常用列表
        if p.get('save'):
            saved0 = _load_setting('wol') or {'devices': []}
            devs = [d for d in (saved0.get('devices') or []) if d.get('mac') != mac]
            devs.append({'name': str(p.get('label') or '').strip()[:40] or mac,
                         'mac': mac, 'ip': str(p.get('ip') or '').strip(),
                         'iface': iface, 'note': str(p.get('note') or '').strip()[:60]})
            _save_setting('wol', {'devices': devs})
        return ok({'mac': mac, 'sent': sent},
                  '已向 %s 发送唤醒魔术包（广播地址：%s）' % (mac, '、'.join(bcasts[:3])))

    return fail('未知操作：%s' % op)


# ------------------------------------------------------------------ 运行依赖自检

# 每项：key, 显示名, 类型(pkg/cmd/mod/svc/file), 目标, 是否必需, 说明, 安装包名
# 分组：自检页按组过滤，54 项平铺会让人找不到想看的那一项。
DEP_GROUPS = [
    ('core', '核心运行时'),
    ('sys', '系统基础'),
    ('net', '网络与诊断'),
    ('fw', '防火墙与 NAT'),
    ('storage', '外置存储与文件系统'),
    ('share', '文件共享'),
    ('qos', 'QoS 与流量'),
    ('print', '打印服务'),
    ('ac', '无线 AC / AP'),
    ('monitor', '监控与 SNMP'),
    ('container', '容器'),
    ('desktop', '桌面环境'),
]
DEP_GROUP_NAME = dict(DEP_GROUPS)

# 每项：(key, 名称, 探测类型, 探测目标, 是否必需, 中文说明, 安装包, 分组)
#   探测类型：cmd=命令在 PATH 里 / file=文件存在 / mod=内核模块 / svc=systemd 单元
#   安装包为空字符串 = 刻意不提供一键安装（装了会出事，见各条注释）
DEPS = [
    # ---- 核心运行时（必需）----
    ('python3', 'Python 3 运行时', 'cmd', 'python3', True, '后端与管理服务的运行基础', 'python3', 'core'),
    ('nftables', 'nftables 防火墙', 'cmd', 'nft', True, 'IPv4/IPv6 防火墙与软加速', 'nftables', 'fw'),
    ('dnsmasq', 'dnsmasq（DHCP/DNS）', 'cmd', 'dnsmasq', True, '局域网 DHCP 与 DNS 服务', 'dnsmasq', 'core'),
    ('dnsmasq-utils', 'dnsmasq-utils（DHCP 工具）', 'cmd', 'dhcp_release', False,
     '主动回收 DHCP 租约（未安装时自动退化为删租约文件 + 重载）', 'dnsmasq-utils', 'core'),
    ('radvd', 'radvd（IPv6 RA）', 'cmd', 'radvd', True, 'IPv6 路由通告', 'radvd', 'core'),
    ('dhcpcd', 'dhcpcd（DHCPv6 客户端）', 'cmd', 'dhcpcd', True, 'IPv6 前缀委派与地址获取', 'dhcpcd-base', 'core'),
    ('pppd', 'pppd（PPPoE 拨号）', 'cmd', 'pppd', True, '宽带 PPPoE 拨号', 'ppp', 'core'),
    ('chrony', 'chrony（NTP 时间同步）', 'cmd', 'chronyd', True, '系统时间同步', 'chrony', 'core'),
    ('chronyc', 'chronyc（NTP 查询）', 'cmd', 'chronyc', False, '查看时间同步源与偏差（与 chrony 同一个包）', 'chrony', 'core'),
    ('miniupnpd', 'miniupnpd（UPnP）', 'cmd', 'miniupnpd', True, 'UPnP / NAT-PMP 自动端口映射', 'miniupnpd', 'fw'),
    ('ethtool', 'ethtool（网卡工具）', 'cmd', 'ethtool', True, '网卡状态、WOL 唤醒、硬件卸载', 'ethtool', 'net'),
    ('curl', 'curl（HTTP 客户端）', 'cmd', 'curl', True,
     '诊断工具、外网检测与「真·公网 IP 判定」的外部回查', 'curl', 'net'),
    ('iproute2', 'iproute2（ip/ss/tc）', 'cmd', 'ip', True, '网络接口、路由与流量控制管理', 'iproute2', 'net'),
    ('conntrack', 'conntrack（连接跟踪）', 'cmd', 'conntrack', True, '查看连接跟踪表、验证加速效果', 'conntrack', 'fw'),
    # ---- 防火墙 / NAT 内核模块 ----
    ('nft-flowtable', 'nf_flow_table 内核模块', 'mod', 'nf_flow_table', True, '软加速（flowtable）依赖', '', 'fw'),
    ('nf-conntrack', 'nf_conntrack 内核模块', 'mod', 'nf_conntrack', True, 'NAT 与连接跟踪依赖', '', 'fw'),
    ('nf-nat', 'nf_nat 内核模块', 'mod', 'nf_nat', True, '地址转换依赖', '', 'fw'),
    # ---- 系统基础 ----
    ('sqlite3', 'SQLite3（配置库）', 'cmd', 'sqlite3', False,
     '配置文件数据库（Python 内置 sqlite3 亦可工作）', 'sqlite3', 'sys'),
    ('openssl', 'OpenSSL（证书工具）', 'cmd', 'openssl', False,
     '生成管理后台自签证书、导入 CA 泛证书、SSL 测试', 'openssl', 'sys'),
    ('ca-certs', 'CA 根证书', 'file', '/etc/ssl/certs/ca-certificates.crt', True,
     'HTTPS 请求与证书校验', 'ca-certificates', 'sys'),
    # 注意这里没有给安装包：systemd 缺失意味着这台机器根本不是 systemd 系统，
    # 靠 apt install systemd 强行补只会把系统搞坏，必须人工处理。
    ('systemd', 'systemd（服务管理）', 'cmd', 'systemctl', True,
     '服务与定时器托管。缺失时不提供一键安装（需人工确认系统形态）。', '', 'sys'),
    ('sudo', 'sudo（权限白名单）', 'cmd', 'sudo', True, '低权后端调用特权操作', 'sudo', 'sys'),
    ('logrotate', 'logrotate（日志轮转）', 'cmd', 'logrotate', False,
     '防止日志占满磁盘。「磁盘与日志清理」页的自动清理依赖它。', 'logrotate', 'sys'),
    ('kmod', 'modprobe / modinfo（内核模块）', 'cmd', 'modprobe', True,
     'QoS 加载 CAKE / HTB 队列模块、软加速模块时的调用入口', 'kmod', 'sys'),
    ('procps', 'ps / pkill（进程工具）', 'cmd', 'ps', True,
     '概览页进程列表、服务状态查询', 'procps', 'sys'),
    ('coreutils', 'df / du / ls（基础工具）', 'cmd', 'df', True,
     '磁盘占用统计与「磁盘与日志清理」扫描目录大小', 'coreutils', 'sys'),
    ('tar', 'tar（打包工具）', 'cmd', 'tar', False, '主题导入导出、配置备份打包', 'tar', 'sys'),
    ('hostname', 'hostname（主机名）', 'cmd', 'hostname', False, '概览页与终端提示符的主机名', 'hostname', 'sys'),
    ('udev', 'udevadm（设备事件）', 'cmd', 'udevadm', False,
     'USB 打印机 / 外置盘热插拔识别与 RAW 直通权限', 'udev', 'sys'),
    # ---- 可选增强 ----
    ('iperf3', 'iperf3（带宽测试）', 'cmd', 'iperf3', False, '测速工具', 'iperf3', 'net'),
    ('traceroute', 'traceroute（路由追踪）', 'cmd', 'traceroute', False, '诊断工具', 'traceroute', 'net'),
    ('dnsutils', 'dnsutils（dig/nslookup）', 'cmd', 'dig', False, 'DNS 诊断', 'dnsutils', 'net'),
    ('iputils', 'iputils（ping/arping）', 'cmd', 'ping', True, '连通性诊断', 'iputils-ping', 'net'),
    ('zip', 'zip / unzip（打包下载）', 'cmd', 'zip', False, '文件夹压缩下载', 'zip', 'sys'),
    ('p7zip', '7z（高压缩比）', 'cmd', '7z', False, '大文件压缩', 'p7zip-full', 'sys'),
    ('vim', 'vim（编辑器）', 'cmd', 'vim|vim.tiny', False, 'Web 终端里编辑配置文件', 'vim-tiny', 'sys'),
    ('htop', 'htop（进程监视）', 'cmd', 'htop', False, 'Web 终端里的进程查看', 'htop', 'sys'),
    ('iftop', 'iftop（实时流量）', 'cmd', 'iftop', False, '实时流量监控', 'iftop', 'qos'),
    ('socat', 'socat（端口工具）', 'cmd', 'socat', False, '串口/TCP 调试', 'socat', 'net'),
    ('wget', 'wget（下载工具）', 'cmd', 'wget', False, '命令行下载', 'wget', 'net'),
    # ---- 文件共享（SMB / NFS）----
    # 这两项刻意放在「可选」：装完 Debian 会自动拉起 smbd / nfs-server 服务，
    # 属于用户主动开启共享时才需要的组件，不该在装 drouter 时顺带起来。
    ('samba', 'Samba（SMB/CIFS 共享）', 'cmd', 'smbd', False,
     '文件共享：Windows / macOS 访问共享目录。安装后 Debian 会自动启用 smbd 服务。', 'samba', 'share'),
    ('samba-common', 'Samba 账号工具（smbpasswd）', 'cmd', 'smbpasswd', False,
     '文件共享：给 SMB 共享设置访问账号', 'samba-common-bin', 'share'),
    ('nfs-server', 'NFS 服务端', 'cmd', 'exportfs', False,
     '文件共享：Linux / NAS 通过 NFS 挂载。安装后会自动启用 nfs-server 服务。',
     'nfs-kernel-server', 'share'),
    # ---- 外置存储（U 盘 / 移动硬盘 / Type-C / 雷电）----
    ('util-linux', 'lsblk / mount（块设备）', 'cmd', 'lsblk', True,
     '外置存储：识别设备、容量与连接方式（USB / 雷电 / NVMe），挂载与卸载', 'util-linux', 'storage'),
    ('usbutils', 'lsusb（USB 设备列表）', 'cmd', 'lsusb', False,
     '外置存储与 USB 打印机：列出 USB 总线上的设备及厂商 ID', 'usbutils', 'storage'),
    ('e2fsprogs', 'mkfs.ext4 / mkfs.ext3（ext4 格式化）', 'cmd', 'mkfs.ext4', False,
     '外置存储：Linux 原生 ext4/ext3，日志型、支持权限与 ACL', 'e2fsprogs', 'storage'),
    ('exfatprogs', 'mkfs.exfat（exFAT 格式化）', 'cmd', 'mkfs.exfat', False,
     '外置存储：跨平台移动硬盘首选（Windows / macOS / Linux 都可读写）', 'exfatprogs', 'storage'),
    ('ntfs-3g', 'mkfs.ntfs（NTFS 格式化）', 'cmd', 'mkfs.ntfs', False,
     '外置存储：Windows 兼容的 NTFS', 'ntfs-3g', 'storage'),
    ('dosfstools', 'mkfs.vfat（FAT32 格式化）', 'cmd', 'mkfs.vfat', False,
     '外置存储：通用性最好的 FAT32', 'dosfstools', 'storage'),
    ('xfsprogs', 'mkfs.xfs（XFS 格式化）', 'cmd', 'mkfs.xfs', False,
     '外置存储：大文件与并发写入表现好，删除大量小文件很快', 'xfsprogs', 'storage'),
    ('btrfs-progs', 'mkfs.btrfs（Btrfs 格式化）', 'cmd', 'mkfs.btrfs', False,
     '外置存储：支持快照与校验和的 Btrfs', 'btrfs-progs', 'storage'),
    ('f2fs-tools', 'mkfs.f2fs（F2FS 格式化）', 'cmd', 'mkfs.f2fs', False,
     '外置存储：为闪存（U 盘 / SSD / TF 卡）设计的 F2FS，减少写放大', 'f2fs-tools', 'storage'),
    # ---- QoS / 诊断 / 唤醒 ----
    ('tc', 'tc（流量控制）', 'cmd', 'tc', False, 'QoS 限速与队列调度（CAKE / HTB）', 'iproute2', 'qos'),
    ('tcpdump', 'tcpdump（抓包）', 'cmd', 'tcpdump', False, '接口抓包诊断', 'tcpdump', 'qos'),
    ('mtr', 'mtr（路由追踪）', 'cmd', 'mtr', False, '持续追踪到目标的每一跳', 'mtr-tiny', 'net'),
    ('etherwake', 'etherwake（网络唤醒）', 'cmd', 'etherwake', False,
     'WOL 唤醒。未安装时会自动退回 Python 广播魔术包，功能不受影响。', 'etherwake', 'net'),
    # ---- VPN（WireGuard）----
    # ⚠️ 这条以前是漏的：VPN 模块到处调`wg` / `wg-quick`（genkey / pubkey /
    # setconf / show），但依赖清单里没有它。后果是「服务」页看不到缺失提示，
    # 用户点进 VPN 页才发现配不了。1.0.8 起`_vpn_env()` 已经能分出
    # 「只是没装 wireguard-tools」这一态（state=need_tool）并给一键修复，
    # 但依赖清单这条仍然要留着 —— 自检页是全局视角，用户不该非得
    # 先进 VPN 页才知道缺东西。
    # 两个探测目标分开登记：wg 在 wireguard-tools 里，wg-quick 同包。
    # 不标required —— 它只在用 VPN 时才需要，装机就强制拉进来不合理。
    ('wireguard-tools', 'WireGuard 工具（wg / wg-quick）', 'cmd', 'wg', False,
     'VPN 服务端：远程回家访问内网。缺失时 VPN 页无法生成密钥、无法改配置。',
     'wireguard-tools', 'net'),
    # ---- 打印服务（与 USB RAW 直通互斥）----
    ('cups', 'CUPS 打印服务', 'cmd', 'cupsd', False,
     '打印服务：把 USB / 网络打印机共享给局域网。安装后会启动 cups 服务常驻。', 'cups', 'print'),
    ('cups-client', 'lpadmin / lpstat（打印机管理）', 'cmd', 'lpadmin', False,
     '打印服务：添加、删除与查询打印机队列', 'cups-client', 'print'),
    # ---- AC / AP 管理中心 ----
    # opensoho 刻意留空安装包名：它不是 apt 包，而是 GitHub 上的单二进制，
    # 一键 apt 安装会直接失败。装它走「服务 → AC/AP 管理中心」页面。
    ('opensoho', 'OpenSOHO（AC / AP 控制器）', 'file', OH_BIN, False,
     'AC/AP 管理中心：集中管理 OpenWRT AP 的 Wi-Fi / VLAN / PoE。'
     '非 apt 包，在「服务 → AC/AP 管理中心」页安装。', '', 'ac'),
    # ---- 监控与 SNMP ----
    ('snmpd', 'snmpd（SNMP 服务端）', 'cmd', 'snmpd', False,
     'SNMP 监控：让网管软件采集流量与接口数据。安装后会启动 snmpd 常驻。', 'snmpd', 'monitor'),
    ('snmp-cli', 'snmpwalk / snmpget（SNMP 客户端）', 'cmd', 'snmpwalk', False,
     'SNMP 监控：自检时验证 snmpd 是否真的在应答', 'snmp', 'monitor'),
    ('smartmontools', 'smartctl（硬盘健康）', 'cmd', 'smartctl', False,
     '外置存储：读取硬盘 SMART 健康状态。放在可选是因为装完会拉起 smartd 常驻。',
     'smartmontools', 'storage'),
    ('lm-sensors', 'sensors（温度电压）', 'cmd', 'sensors', False,
     '概览页硬件仪表盘的 CPU / 主板温度探测', 'lm-sensors', 'monitor'),
    ('dmidecode', 'dmidecode（BIOS/硬件）', 'cmd', 'dmidecode', False,
     '概览页读取 BIOS 版本、主板与内存信息', 'dmidecode', 'monitor'),
    # ---- 容器 ----
    # docker.io 装完会拉起 docker.service 并常驻，4GB 小机器上属于「按需再装」，
    # 所以这里只检测不强制；compose 用插件路径判断（v2 插件没有独立可执行文件名）。
    ('docker', 'Docker 引擎', 'cmd', 'docker', False,
     'Docker 容器管理面板。安装后会启动 docker 服务并常驻。', 'docker.io', 'container'),
    ('docker-compose', 'Docker Compose 插件', 'file',
     '/usr/libexec/docker/cli-plugins/docker-compose'
     '|/usr/lib/docker/cli-plugins/docker-compose'
     '|/usr/local/lib/docker/cli-plugins/docker-compose'
     '|/usr/bin/docker-compose', False,
     '容器编排（docker compose up）。路径随发行版不同，这里逐个探测。',
     # Debian 13 的包名是 docker-compose（提供 compose v2 插件）；
     # 早期版本一度用 docker-compose-v2（那是 Ubuntu 的叫法），
     # 所以下面按「先试 Debian 名、再试 Ubuntu 名」的顺序回退。
     'docker-compose|docker-compose-v2', 'container'),
    # ---- 仅检测、刻意不提供一键安装 ----
    ('journalctl', 'journalctl（系统日志）', 'cmd', 'journalctl', False,
     '统一日志采集系统日志。若缺失说明不是 systemd 系统，需人工确认。', '', 'sys'),
    ('nmcli', 'NetworkManager（nmcli）', 'cmd', 'nmcli', False,
     '仅用于探测网卡配置方式。刻意不提供安装：装上 NetworkManager 会与路由配置抢网卡。', '', 'net'),
    ('networkctl', 'systemd-networkd（networkctl）', 'cmd', 'networkctl', False,
     '仅用于探测网卡配置方式，同样不提供安装。', '', 'net'),
    # ---- XFCE 桌面（可选）----
    ('xfce', 'XFCE 桌面环境', 'cmd', 'xfce4-session', False,
     '桌面环境。仅在你需要图形界面时安装；路由器本身不需要。', 'xfce4', 'desktop'),
    ('xorg', 'Xorg 显示服务', 'cmd', 'Xorg', False, '图形界面基础（随桌面安装）', 'xserver-xorg', 'desktop'),
    ('vnc', 'RealVNC Server', 'file', '/usr/bin/vncserver-x11-serviced', False,
     '远程图形桌面。本系统绝不改动它，仅在缺失时提示。', '', 'desktop'),
]


def _dep_installed(kind, target):
    """判断单个依赖是否已满足。返回 (bool, 探测详情)。

    target 支持用 '|' 分隔多个候选（任一命中即算已安装）：
      * cmd  ：vim 在 Debian 上常以 vim-tiny 提供可执行文件 vim.tiny；
      * file ：docker compose 插件的路径随发行版不同，得逐个找。
    """
    try:
        if kind == 'cmd':
            for cand in str(target).split('|'):
                cand = cand.strip()
                if not cand:
                    continue
                # 性能：原为 sh(['sh','-c','command -v X']) 每个候选一次 fork；
                # shutil.which 语义等价（PATH 查找 + 可执行位判断）且零 fork。
                p = shutil.which(cand)
                if p:
                    return True, p
            return False, ''
        if kind == 'file':
            for cand in str(target).split('|'):
                cand = cand.strip()
                if cand and os.path.exists(cand):
                    return True, cand
            return False, str(target).split('|')[0]
        if kind == 'mod':
            rc, o, _e = sh(['sh', '-c', 'modinfo %s >/dev/null 2>&1 || test -d /sys/module/%s'
                            % (target, target)], timeout=8)
            return rc == 0, target
        if kind == 'svc':
            rc, o, _e = sh(['systemctl', 'list-unit-files', '%s.service' % target], timeout=8)
            return ('%s.service' % target) in (o or ''), target
    except Exception:
        pass
    return False, ''


def _pkgs_available(names):
    """从候选包名里挑出本机 apt 真实存在的那些。

    为什么必须挑：apt-get install 只要命令行里有**一个**不存在的包名，
    就会以「E: 无法定位软件包」整体中止 —— 同一条命令里其它正常的包
    也一个都装不上（已用 real apt --dry-run 实测确认）。
    所以「同一个东西在不同发行版叫不同名字」这种场景，
    绝不能让不存在的那个名字混进 apt 命令行。

    返回 (存在列表, 不存在列表)。apt-cache 不可用时保守返回全部（宁可让
    apt 自己报错，也不要因为探测失败把用户要装的包静默吞掉）。
    """
    names = [str(n).strip() for n in (names or []) if str(n).strip()]
    if not names:
        return [], []
    if not shutil.which('apt-cache'):
        return names, []
    have, lack = [], []
    for n in names:
        rc, _o, _e = sh(['apt-cache', 'show', '--no-all-versions', n], timeout=20)
        (have if rc == 0 else lack).append(n)
    if not have:
        # 一个都查不到：多半是 apt 索引没建/被锁，交给 apt 自己报错更好排查
        return names, []
    return have, lack


def act_depcheck(p):
    """依赖自检：逐项检测运行 Drouter 所需的组件。"""
    op = str((p or {}).get('op') or 'check')
    if op == 'check':
        items = []
        okc = miss = opt = 0
        for key, name, kind, target, required, note, pkg, group in DEPS:
            has, where = _dep_installed(kind, target)
            if has:
                okc += 1
            elif required:
                miss += 1
            else:
                opt += 1
            items.append({
                'key': key, 'name': name, 'kind': kind, 'target': target,
                'required': required, 'note': note, 'pkg': pkg,
                'group': group, 'group_name': DEP_GROUP_NAME.get(group, '其它'),
                'ok': has, 'where': where,
            })
        # 额外信息：系统版本、桌面是否存在
        extra = {}
        try:
            with open('/etc/os-release') as f:
                kv = dict(l.strip().split('=', 1) for l in f if '=' in l)
            extra['os'] = (kv.get('PRETTY_NAME') or '').strip('"')
            extra['os_id'] = (kv.get('VERSION_ID') or '').strip('"')
        except Exception:
            pass
        rc, o, _e = sh(['sh', '-c', 'test -d /usr/share/xsessions && ls /usr/share/xsessions || true'], timeout=6)
        extra['desktops'] = [x.replace('.desktop', '') for x in (o or '').split() if x]
        extra['has_desktop'] = bool(extra['desktops'])
        return ok({'items': items, 'summary': {'ok': okc, 'missing': miss, 'optional': opt,
                                               'total': len(items)},
                   'groups': [{'id': g, 'name': n} for g, n in DEP_GROUPS],
                   'extra': extra},
                  '自检完成：%d 项已就绪，%d 项缺失，%d 项可选未装' % (okc, miss, opt))

    if op == 'install':
        only = p.get('keys') or p.get('only')
        # 不指定 keys 时只装「必需」项。
        # 不能图省事全装：DEPS 里有 xfce4 / xserver-xorg / samba / docker.io 这类
        # 可选大件，一次「一键安装」把桌面环境和一堆常驻服务装上，对路由器是灾难。
        req_only = not (isinstance(only, list) and only)
        want = []
        for key, name, kind, target, required, note, pkg, group in DEPS:
            if not pkg:
                continue
            if isinstance(only, list) and only and key not in only:
                continue
            if req_only and not required:
                continue
            has, _w = _dep_installed(kind, target)
            if not has:
                want.append((key, name, pkg))
        if not want:
            return ok({'installed': [], 'log': '所有可安装的依赖均已就绪，无需操作。'},
                      '没有需要安装的项目')
        # pkg 字段支持用 '|' 写多个候选包名（同一个东西在不同发行版叫法不同，
        # 例如 compose 在 Debian 13 叫 docker-compose、Ubuntu 叫 docker-compose-v2）。
        # 候选不是「按顺序试」而是「挑本机存在的那个」：apt-get install 命令行里
        # 只要有一个查不到的包名，整条命令会整体中止、其它包一个都不装（已实测）。
        k2p = {}
        cand = []
        for x in want:
            if not x[2]:
                continue
            names = [n.strip() for n in str(x[2]).split('|') if n.strip()]
            k2p[x[0]] = names
            cand += names
        have, lack = _pkgs_available(cand)
        pkgs = sorted(set(have))
        if not pkgs:
            return fail('待安装的软件包在当前 apt 源里都找不到：%s。'
                        '请先执行 apt-get update，或换用国内镜像源后重试。'
                        % '、'.join(sorted(set(lack)) or cand),
                        data={'log': 'apt-cache 查不到：%s' % ', '.join(cand)})
        if lack:
            # 顺手把「这个发行版没有、已自动跳过」讲清楚，
            # 否则用户看到安装日志里少了某个包会以为装漏了。
            log('info', 'system', 'DEP_PKG_SKIP',
                '以下候选包在本机 apt 源中不存在，已自动跳过：%s' % ', '.join(sorted(set(lack))))
        log('warn', 'system', 'DEP_INSTALL', '开始安装缺失依赖：%s' % ' '.join(pkgs))
        env = dict(os.environ)
        env['DEBIAN_FRONTEND'] = 'noninteractive'
        try:
            p2 = subprocess.run(['apt-get', 'install', '-y', '--no-install-recommends'] + pkgs,
                                timeout=900, capture_output=True, text=True, errors='replace', env=env)
            out = (p2.stdout or '') + ('\n' + p2.stderr if p2.stderr else '')
            rc = p2.returncode
        except subprocess.TimeoutExpired:
            return fail('安装超时（超过 15 分钟）。请检查网络或换用镜像源后重试。',
                        data={'log': 'apt-get 执行超过 900 秒被终止。'})
        except Exception as e:
            return fail('安装失败：%s' % e, data={'log': str(e)})
        # 安装后重新检测
        installed, still = [], []
        for key, name, kind, target, required, note, pkg, group in DEPS:
            if pkg and key in [w[0] for w in want]:
                has, _w = _dep_installed(kind, target)
                (installed if has else still).append(name)
        tail = '\n'.join(out.strip().splitlines()[-80:])
        # 把完整日志落盘，便于前端「复制日志」与人工排查
        try:
            os.makedirs(LOGDIR, exist_ok=True)
            with open('/var/log/drouter/dep-install.log', 'w', encoding='utf-8') as f:
                f.write('# 命令: apt-get install -y --no-install-recommends %s\n' % ' '.join(pkgs))
                f.write('# 返回码: %d\n\n' % rc)
                f.write(out)
        except Exception:
            pass
        # 只读文件系统是常见环境问题，给出可操作的提示而不是笼统报错
        hint = ''
        if rc != 0 and ('只读文件系统' in out or 'Read-only file system' in out
                        or 'read-only file system' in out):
            hint = ('\n\n【原因】目标文件系统对本次操作是只读的。'
                    'Drouter 的管理服务默认以 systemd 沙箱运行（ProtectSystem），'
                    '会把 /usr 挂为只读，导致 apt/dpkg 无法写入。'
                    '请确认已使用最新版 deploy.sh 重新部署（已将 ProtectSystem 设为 false），'
                    '然后执行 systemctl restart drouter-web 后重试。')
        if rc != 0:
            return fail('安装过程返回错误码 %d，部分依赖可能未装成功。%s' % (rc, hint),
                        data={'installed': installed, 'still_missing': still,
                              'log': tail + hint, 'packages': pkgs})
        return ok({'installed': installed, 'still_missing': still, 'log': tail,
                   'packages': pkgs},
                  '安装完成：%d 项已就绪%s' % (len(installed),
                                              '，%d 项仍未满足' % len(still) if still else ''))

    if op == 'log':
        # 返回最近一次安装日志（供前端复制）
        try:
            p3 = '/var/log/drouter/dep-install.log'
            if os.path.isfile(p3):
                with open(p3, encoding='utf-8', errors='replace') as f:
                    return ok({'log': f.read()[-20000:]})
        except Exception:
            pass
        return ok({'log': ''}, '暂无安装日志')

    return fail('未知操作：%s' % op)


# ====================================================================
#  Web 远程文件管理器（#18）—— 受限于目录白名单，删除走回收站
# ====================================================================

# 允许访问的根目录白名单（防止被当成任意文件读写通道）
FS_ROOTS = ['/root', '/home', '/etc/drouter', '/var/log/drouter',
            '/opt/drouter', '/tmp', '/var/tmp', '/srv', '/mnt', '/media',
            '/usr/local/share/drouter']
# 明确禁止的敏感路径（即使落在白名单内也拒绝）
# ⚠️ 这里一律写**目录**（而不是单个文件）：过去只挡了 /root/.ssh/id_rsa，
#    于是 /root/.ssh/authorized_keys 是可以写的 —— 写进去等于直接拿到 root SSH。
FS_DENY = ['/etc/shadow', '/etc/gshadow', '/etc/sudoers', '/proc', '/sys', '/dev',
           # SSH 密钥与授信名单整目录禁止
           '/root/.ssh', '/etc/ssh',
           # helper 以 root 运行，能写它自己的源码就等于任意代码执行
           '/opt/drouter/backend', '/opt/drouter/web', '/opt/drouter/scripts',
           '/usr/local/bin', '/usr/local/sbin', '/etc/systemd', '/etc/sudoers.d',
           '/etc/cron.d', '/etc/cron.daily', '/etc/pam.d',
           '/etc/drouter/keys']
# 文件名黑名单（防跃迁最后的阀门）：命中即拒绝，不看目录
FS_DENY_NAME = ['authorized_keys', 'authorized_keys2', 'id_rsa', 'id_dsa',
                'id_ecdsa', 'id_ed25519', 'shadow', 'gshadow', 'sudoers',
                '.htpasswd', 'BUILD_MODE']
FS_TRASH = '/var/lib/drouter/trash'
FS_PREVIEW_MAX = 1024 * 1024        # 文本预览上限 1 MB


def _fs_name_guard(path):
    """文件名黑名单：任何一级目录里出现这些名字都直接拒绝（不看白名单）。

    只靠目录黑名单是不够的 —— 用户可以在 `/mnt/x/authorized_keys` 写好，
    再想办法把它挪进 `/root/.ssh`。所以名字本身也要挡。
    另外顺手挡住任何用户家目录下的 .ssh 条目。
    """
    parts = [x for x in str(path).split('/') if x not in ('', '.')]
    for seg in parts:
        if seg in FS_DENY_NAME:
            return '该文件名受保护，禁止读写：%s' % seg
        if seg == '.ssh':
            return '禁止通过文件管理器操作 SSH 密钥目录'
    return ''


def _fs_resolve(path):
    """把用户传入路径规范化为绝对真实路径，并做白名单/拒绝名单校验。"""
    if not path:
        return None, '缺少路径参数'
    raw = str(path)
    if '\x00' in raw:
        return None, '非法路径'
    if not raw.startswith('/'):
        raw = '/' + raw
    # 规范化（不解析软链，稍后 realpath 再判）
    norm = os.path.normpath(raw)
    bad_name = _fs_name_guard(norm)
    if bad_name:
        return None, bad_name
    for d in FS_DENY:
        if norm == d or norm.startswith(d + '/'):
            return None, '该路径受保护，禁止访问'
    # 真实路径（防软链绕过）
    try:
        real = os.path.realpath(norm)
    except Exception:
        real = norm
    for d in FS_DENY:
        if real == d or real.startswith(d + '/'):
            return None, '该路径受保护，禁止访问'
    if not any(real == r or real.startswith(r + '/') for r in FS_ROOTS):
        return None, '不在允许访问的目录范围内（仅限系统与用户目录）'
    return real, ''


def _fs_entry(full):
    """把一个路径描述成前端可用的条目。"""
    try:
        st = os.lstat(full)
    except Exception:
        return None
    is_dir = stat.S_ISDIR(st.st_mode)
    is_link = stat.S_ISLNK(st.st_mode)
    if is_link:
        try:
            tgt = os.path.realpath(full)
            is_dir = os.path.isdir(tgt)
        except Exception:
            pass
    ext = os.path.splitext(full)[1].lower().lstrip('.')
    return {
        'name': os.path.basename(full) or full,
        'path': full,
        'dir': is_dir,
        'link': is_link,
        'size': 0 if is_dir else st.st_size,
        'mtime': int(st.st_mtime),
        'mode': oct(st.st_mode & 0o777),
        'ext': ext,
        'readable': os.access(full, os.R_OK),
        'writable': os.access(full, os.W_OK),
    }


def _fs_size_h(n):
    try:
        n = float(n)
    except Exception:
        return '0 B'
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or u == 'TB':
            return '%.0f %s' % (n, u) if u == 'B' else '%.1f %s' % (n, u)
        n /= 1024.0


def act_fs(p):
    """远程文件管理器后端：list/read/get/hash/mkdir/rename/delete/zip/search。"""
    p = p or {}
    op = str(p.get('op') or 'list')

    # ---------- 列目录 ----------
    if op == 'list':
        real, err = _fs_resolve(p.get('path') or '/root')
        if err:
            return fail(err)
        if not os.path.isdir(real):
            return fail('不是目录：%s' % real)
        items = []
        try:
            for name in os.listdir(real):
                e = _fs_entry(os.path.join(real, name))
                if e:
                    items.append(e)
        except PermissionError:
            return fail('没有权限读取该目录')
        except Exception as e:
            return fail('读取目录失败：%s' % e)
        items.sort(key=lambda x: (not x['dir'], x['name'].lower()))
        # 面包屑
        crumbs = []
        acc = ''
        for seg in real.strip('/').split('/'):
            acc += '/' + seg
            crumbs.append({'name': seg, 'path': acc})
        return ok({'path': real, 'parent': os.path.dirname(real) or '/',
                   'crumbs': [{'name': '/', 'path': '/'}] + crumbs,
                   'items': items, 'count': len(items),
                   'roots': FS_ROOTS})

    # ---------- 批量路径（删除 / 打包） ----------
    if op in ('delete', 'zip'):
        paths = p.get('paths') or []
        if not isinstance(paths, list) or not paths:
            return fail('未选择任何文件')
        reals = []
        for one in paths[:500]:
            real, err = _fs_resolve(one)
            if err:
                return fail('%s：%s' % (one, err))
            reals.append(real)

        if op == 'delete':
            os.makedirs(FS_TRASH, exist_ok=True)
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            done, failed = [], []
            for real in reals:
                if real in ('/', '/root', '/home', '/etc', '/tmp', '/var'):
                    failed.append({'path': real, 'why': '拒绝删除系统关键目录'})
                    continue
                try:
                    dst = os.path.join(FS_TRASH, stamp, real.lstrip('/'))
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.move(real, dst)
                    done.append(real)
                except Exception as e:
                    failed.append({'path': real, 'why': str(e)})
            return ok({'deleted': done, 'failed': failed, 'trash': FS_TRASH},
                      '已移入回收站 %d 项%s' % (len(done),
                                             '，%d 项失败' % len(failed) if failed else ''))

        # zip 打包
        name = str(p.get('name') or '').strip()
        if not name:
            name = 'drouter-pack-%s.zip' % datetime.now().strftime('%Y%m%d-%H%M%S')
        if not name.lower().endswith('.zip'):
            name += '.zip'
        name = re.sub(r'[^\w\u4e00-\u9fa5.\-]+', '_', name)
        outdir = os.path.join('/tmp', 'drouter-dl')
        os.makedirs(outdir, exist_ok=True)
        out = os.path.join(outdir, name)
        import zipfile
        n = 0
        try:
            with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
                for real in reals:
                    if os.path.isdir(real):
                        base = os.path.dirname(real.rstrip('/'))
                        for rt, dirs, files in os.walk(real):
                            for fn in files:
                                fp = os.path.join(rt, fn)
                                try:
                                    z.write(fp, os.path.relpath(fp, base))
                                    n += 1
                                except Exception:
                                    continue
                    else:
                        try:
                            z.write(real, os.path.basename(real))
                            n += 1
                        except Exception:
                            continue
        except Exception as e:
            return fail('打包失败：%s' % e)
        return ok({'file': out, 'name': name, 'count': n,
                   'size': os.path.getsize(out), 'size_h': _fs_size_h(os.path.getsize(out))},
                  '已打包 %d 个文件' % n)

    # 上传 / 写入文件（base64 载荷）—— 必须在“单文件操作”的公共 resolve 之前处理，
    # 因为此时目标目录已存在、目标文件尚不存在。
    if op == 'write':
        parent, perr = _fs_resolve(p.get('path') or '/root')
        if perr:
            return fail(perr)
        if not os.path.isdir(parent):
            return fail('目标目录不存在：%s' % parent)
        name = str(p.get('name') or '').strip()
        if not name or '/' in name or name in ('.', '..'):
            return fail('文件名非法')
        dst, derr = _fs_resolve(os.path.join(parent, name))
        if derr:
            return fail(derr)
        import base64 as _b64
        try:
            raw = _b64.b64decode(p.get('b64') or '')
        except Exception:
            return fail('上传数据解码失败')
        if len(raw) > 256 * 1024 * 1024:
            return fail('单文件超过 256MB，请使用 SFTP/SCP')
        tmp = dst + '.uploading'
        try:
            with open(tmp, 'wb') as f:
                f.write(raw)
            os.replace(tmp, dst)
        except Exception as e:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            return fail('写入失败：%s' % e)
        try:
            os.chmod(dst, 0o644)
        except Exception:
            pass
        return ok({'path': dst, 'size': len(raw), 'size_h': _fs_size_h(len(raw))},
                  '已上传 %s' % name)

    # 新建目录（同样在公共 resolve 之前）
    if op == 'mkdir':
        parent, perr = _fs_resolve(p.get('parent') or p.get('path'))
        if perr:
            return fail(perr)
        name = str(p.get('name') or '').strip()
        if not name or '/' in name or name in ('.', '..'):
            return fail('目录名非法')
        target, terr = _fs_resolve(os.path.join(parent, name))
        if terr:
            return fail(terr)
        if os.path.exists(target):
            return fail('同名文件或目录已存在')
        try:
            os.makedirs(target, exist_ok=False)
        except Exception as e:
            return fail('创建失败：%s' % e)
        return ok({'path': target}, '目录已创建')

    # ---------- 单文件操作 ----------
    real, err = _fs_resolve(p.get('path'))
    if err:
        return fail(err)

    # 读取（文本预览 / base64 下载）
    if op in ('read', 'get'):
        if os.path.isdir(real):
            return fail('目标是目录，无法读取内容')
        try:
            size = os.path.getsize(real)
        except Exception:
            return fail('文件不存在或无法访问')
        b64 = bool(p.get('b64'))
        if b64:
            # 下载通道是 helpd 的单条 JSON 响应（web 侧 32MB 封顶）：
            # 20MB 原始文件 base64 后约 27MB，留足余量。早先写 512MB ——
            # 经守护必被 32MB 截断、JSON 解析失败；回退到子进程又把
            # ~700MB 输出整个 capture 进内存，4GB 小机直接被打爆。
            if size > 20 * 1024 * 1024:
                return fail('文件超过 20MB：Web 下载通道单条响应上限 32MB，'
                            '请用 SFTP/SCP 传输，或先在文件管理里打包分卷')
            try:
                with open(real, 'rb') as f:
                    raw = f.read()
            except Exception as e:
                return fail('读取失败：%s' % e)
            import base64 as _b64
            return ok({'name': os.path.basename(real), 'size': size,
                       'size_h': _fs_size_h(size),
                       'b64': _b64.b64encode(raw).decode('ascii')})
        mx = int(p.get('max') or FS_PREVIEW_MAX)
        mx = max(1024, min(mx, FS_PREVIEW_MAX * 4))
        try:
            with open(real, 'rb') as f:
                raw = f.read(mx + 1)
        except Exception as e:
            return fail('读取失败：%s' % e)
        truncated = len(raw) > mx
        raw = raw[:mx]
        # 二进制判定：含 NUL 视为二进制
        binary = b'\x00' in raw
        if binary:
            return fail('这是二进制文件，无法以文本预览，请下载后查看')
        try:
            text = raw.decode('utf-8')
        except Exception:
            try:
                text = raw.decode('gbk', 'replace')
            except Exception:
                text = raw.decode('latin-1', 'replace')
        return ok({'name': os.path.basename(real), 'path': real, 'text': text,
                   'size': size, 'size_h': _fs_size_h(size),
                   'truncated': truncated, 'lines': text.count('\n') + 1})

    if op == 'hash':
        import hashlib
        if os.path.isdir(real):
            return fail('目标是目录')
        h1 = hashlib.sha256()
        h2 = hashlib.md5()
        try:
            with open(real, 'rb') as f:
                for chunk in iter(lambda: f.read(1 << 20), b''):
                    h1.update(chunk)
                    h2.update(chunk)
        except Exception as e:
            return fail('计算失败：%s' % e)
        return ok({'name': os.path.basename(real), 'sha256': h1.hexdigest(),
                   'md5': h2.hexdigest(), 'size': os.path.getsize(real)})

    # 重命名 / 移动
    if op == 'rename':
        name = str(p.get('name') or '').strip()
        if not name or '/' in name or name in ('.', '..'):
            return fail('名称非法')
        dst_raw = p.get('to') or os.path.join(os.path.dirname(real), name)
        dst, derr = _fs_resolve(dst_raw)
        if derr:
            return fail(derr)
        if real in ('/', '/root', '/home', '/etc', '/tmp'):
            return fail('拒绝重命名系统关键目录')
        parent_real = os.path.dirname(real)
        if os.path.dirname(dst) != parent_real and not p.get('to'):
            dst = os.path.join(parent_real, name)
            dst, derr = _fs_resolve(dst)
            if derr:
                return fail(derr)
        if os.path.exists(dst):
            return fail('目标已存在：%s' % os.path.basename(dst))
        try:
            os.rename(real, dst)
        except Exception as e:
            return fail('重命名失败：%s' % e)
        return ok({'from': real, 'to': dst}, '已重命名')

    # 搜索
    if op == 'search':
        kw = str(p.get('kw') or '').strip()
        if not kw:
            return fail('请输入搜索关键词')
        base = real if os.path.isdir(real) else os.path.dirname(real)
        limit = max(1, min(int(p.get('limit') or 200), 1000))
        hits = []
        low = kw.lower()
        for rt, dirs, files in os.walk(base):
            if rt.count('/') - base.count('/') > 6:
                dirs[:] = []
                continue
            for nm in dirs + files:
                if low in nm.lower():
                    e = _fs_entry(os.path.join(rt, nm))
                    if e:
                        hits.append(e)
                        if len(hits) >= limit:
                            return ok({'kw': kw, 'base': base, 'items': hits,
                                       'truncated': True})
        return ok({'kw': kw, 'base': base, 'items': hits, 'truncated': False})

    return fail('未知文件操作：%s' % op)


# ====================================================================
#  Web 终端（#4 重做）—— 真 PTY 交互终端
# ====================================================================
# 旧实现的三个毛病：输入不是实时的（要整条回车才发）、没有回显（前端自己画
# "$ cmd" 假装是终端给的）、交互式程序（top / vi / 密码提示）根本用不了。
# 根因是「每次 helper 调用都是一个跑完就退出的新进程」，PTY 没法跨请求存活。
#
# 现在改成：PTY 由常驻守护 drouter-shelld 持有，本函数只做 Unix socket 客户端。
#
# 关于原来那份「危险命令黑名单」：它在真·交互式 shell 下已经失效 ——
# 子串匹配既拦不住（换个写法就绕过），又会误伤（输入里含 mkfs/passwd 就被吞掉，
# 表现出来就是「终端突然打不出字」，比没有黑名单更糟）。所以这里不再拦截输入，
# 改为依靠：① 必须登录后台才有权限；② socket 0600 且仅 root 可连；
# ③ 会话开启/关闭与输入都写审计日志；④ 30 分钟空闲自动回收；⑤ 界面明确提示是 root shell。
SHELL_SOCK = '/run/drouter/shell.sock'
SHELL_UNIT = 'drouter-shelld.service'
SHELL_DAEMON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'drouter-shelld.py')


def _shelld_spawn():
    """不经 init 系统，直接把终端守护拉起来（回退路径）。

    容器形态里没有 systemd，`systemctl start` 必然失败、socket 永远不出现，
    用户在界面上只会看到「终端守护未运行」—— 而那条提示还让人去执行
    systemctl，在容器里根本无从执行。这里补一条不依赖 init 的路径：
    直接以子进程方式启动守护，并用 start_new_session 让它脱离本进程，
    helper 退出后它仍然活着（容器里 web 与守护同属容器 cgroup，容器在就都在）。

    真机上这一步不会命中：systemctl 正常时调用方早就返回了。
    """
    if not os.path.isfile(SHELL_DAEMON):
        return False
    try:
        subprocess.Popen(
            [sys.executable, SHELL_DAEMON],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        return False
    for _ in range(40):
        if os.path.exists(SHELL_SOCK):
            return True
        time.sleep(0.25)
    return os.path.exists(SHELL_SOCK)


def _shelld_start():
    """守护没跑时拉起一次。否则用户只会看到一句看不懂的「连接失败」。"""
    if os.path.exists(SHELL_SOCK):
        return True
    sh(['systemctl', 'start', SHELL_UNIT], timeout=30)
    # 等 socket 出现（systemd 起来后守护自己还要创建 socket）
    for _ in range(40):
        if os.path.exists(SHELL_SOCK):
            return True
        time.sleep(0.25)
    if os.path.exists(SHELL_SOCK):
        return True
    # systemctl 拉不起来（典型的：容器里没有 systemd）→ 直接起守护进程
    return _shelld_spawn()


def _shelld_call(req, timeout=25):
    """向 drouter-shelld 发一条请求（换行分隔的 JSON）。"""
    last = ''
    for attempt in (0, 1):
        s = None
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(SHELL_SOCK)
            s.sendall((json.dumps(req, ensure_ascii=False) + '\n').encode('utf-8'))
            buf = b''
            while not buf.endswith(b'\n'):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 4 * 1024 * 1024:
                    break
            if not buf:
                last = '终端守护无响应'
                continue
            return json.loads(buf.decode('utf-8', errors='replace'))
        except FileNotFoundError:
            last = '终端守护未运行'
        except ConnectionRefusedError:
            last = '终端守护未运行'
        except OSError as e:
            last = '与终端守护通信失败：%s' % e
        except ValueError:
            return {'ok': False, 'code': 'BAD_JSON',
                    'msg_cn': '终端守护返回了无法解析的内容'}
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
        # 第一次失败先尝试把守护拉起来，再试一次
        if attempt == 0:
            _shelld_start()
    return {'ok': False, 'code': 'NO_SHELLD',
            'msg_cn': '%s。可执行 systemctl start %s 后重试'
                      '（容器形态没有 systemd，重启容器即可）。'
                      % (last, SHELL_UNIT)}


def act_webshell(p):
    """Web 终端后端：connect / read / write / resize / disconnect / sessions。"""
    p = p or {}
    op = str(p.get('op') or 'connect')
    if op not in ('connect', 'read', 'write', 'resize', 'disconnect',
                  'sessions', 'ping'):
        return fail('未知终端操作：%s' % op, 'BAD_OP')

    if op == 'ping':
        return _shelld_call({'op': 'sessions'}, timeout=10)

    req = {'op': op}
    sid = str(p.get('sid') or '')
    if sid:
        req['sid'] = sid
    if op == 'connect':
        # 只允许本机 shell：这里不建立到别的主机的 SSH（那需要保管凭据，
        # 也不是路由器终端该干的事）。远程主机请用系统自带 SSH 客户端。
        user = str(p.get('user') or p.get('username') or 'root').strip()
        if user not in ('root', 'ajeef', 'drouter'):
            return fail('不支持以 %s 身份开启终端（可选：root / ajeef / drouter）' % user,
                        'BAD_USER')
        req['user'] = user
        req['cols'] = int(p.get('cols') or 100)
        req['rows'] = int(p.get('rows') or 30)
    elif op == 'write':
        data = p.get('data') or ''
        if len(data) > 65536:
            return fail('单次输入过长', 'TOO_LONG')
        req['data'] = data
    elif op == 'read':
        # 长轮询：让守护在没有数据时挂起一会儿，输出一到立刻返回
        try:
            req['wait'] = max(0.0, min(float(p.get('wait') or 0), 5.0))
        except Exception:
            req['wait'] = 0.0
    elif op == 'resize':
        req['cols'] = int(p.get('cols') or 100)
        req['rows'] = int(p.get('rows') or 30)

    r = _shelld_call(req)
    # 审计：开启 root shell 属于高风险动作，必须留痕
    if op == 'connect' and r.get('ok'):
        log('warn', 'system', 'WEBSHELL_CONNECT',
            'Web 终端已开启（root shell）', {'sid': (r.get('data') or {}).get('sid')})
    if op == 'write':
        try:
            log('debug', 'system', 'WEBSHELL_INPUT', 'Web 终端输入',
                {'sid': sid, 'len': len(p.get('data') or '')})
        except Exception:
            pass
    return r


# ================================================================== 主题之家（#12）

THEME_DIR = '/etc/drouter/themes'            # 主题目录（一个主题一个子目录）
THEME_ACTIVE = '/etc/drouter/active-theme'   # 当前生效主题 id
THEME_CSS = '/etc/drouter/generated/theme.css'   # 生成的样式文件


def _theme_all(taken=None):
    """列出磁盘上的全部主题（内置 + 自定义）。内置主题以代码为准，不落盘。"""
    out = []
    ids = set()
    for t in theme.builtin_themes():
        ids.add(t['id'])
        out.append(t)
    if os.path.isdir(THEME_DIR):
        for d in sorted(os.listdir(THEME_DIR)):
            p = os.path.join(THEME_DIR, d, 'theme.json')
            if not os.path.isfile(p):
                continue
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    obj = json.load(f)
            except Exception:
                continue
            good, res = theme.validate_theme(obj)
            if not good:
                continue
            res['id'] = d
            res['builtin'] = False
            out.append(res)
            ids.add(d)
    return out, ids


def _theme_find(tid):
    for t in _theme_all()[0]:
        if t['id'] == tid:
            return t
    return None


def _theme_find_raw(tid):
    """不做合法性过滤，直接读某个主题的文件（可能为非法内容）。

    用途：apply 时需要“即使主题非法也要给出具体原因”。
    若什么都不做，被改坏的主题会在 _theme_all() 里被悄悄跳过，
    用户点「应用」只会看到一句莫名其妙的「主题不存在」。
    """
    for t in theme.builtin_themes():
        if t['id'] == tid:
            return t
    p = os.path.join(THEME_DIR, str(tid or '').strip().lower(), 'theme.json')
    if not os.path.isfile(p):
        return None
    try:
        with open(p, 'r', encoding='utf-8') as f:
            obj = json.load(f)
    except Exception as e:
        return {'id': tid, 'name': tid, '_raw_error': 'theme.json 无法解析：%s' % e}
    if isinstance(obj, dict):
        obj.setdefault('id', tid)
        return obj
    return {'id': tid, 'name': tid, '_raw_error': 'theme.json 顶层不是对象'}


def _theme_read_active():
    try:
        with open(THEME_ACTIVE, 'r', encoding='utf-8') as f:
            return f.read().strip() or 'default'
    except Exception:
        return 'default'


def _theme_write_active(tid):
    os.makedirs(os.path.dirname(THEME_ACTIVE), exist_ok=True)
    tmp = THEME_ACTIVE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(tid + '\n')
    os.replace(tmp, THEME_ACTIVE)
    try:
        os.chmod(THEME_ACTIVE, 0o644)
    except Exception:
        pass


def _theme_apply_css(t):
    """把主题写盘成 theme.css，并在 :root 里覆盖变量（不触碰网络/服务）。

    注意：形参不能叫 theme —— 会遮蔽顶部的 `import theme` 模块，
    导致 theme.theme_to_css() 变成AttributeError。统一用 t。
    """
    os.makedirs(os.path.dirname(THEME_CSS), exist_ok=True)
    css = theme.theme_to_css(t)
    tmp = THEME_CSS + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(css)
    os.replace(tmp, THEME_CSS)
    try:
        os.chmod(THEME_CSS, 0o644)
    except Exception:
        pass
    return len(css.encode('utf-8'))


def _theme_save_dir(tid, t):
    """把主题落盘到 THEME_DIR/<id>/theme.json（形参不叫 theme，避免遮蔽模块）。"""
    d = os.path.join(THEME_DIR, tid)
    os.makedirs(d, exist_ok=True)
    obj = {
        'schema': 'drouter.theme/1',
        'name': t.get('name'), 'description': t.get('description') or '',
        'author': t.get('author') or '', 'version': t.get('version') or '1.0',
        'dark': bool(t.get('dark')), 'vars': t.get('vars') or {},
        'css': t.get('css') or '',
        'created_at': t.get('created_at') or datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    tmp = os.path.join(d, 'theme.json.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, os.path.join(d, 'theme.json'))
    try:
        with open(os.path.join(d, 'theme.css'), 'w', encoding='utf-8') as f:
            f.write(theme_to_css_cached(theme))
    except Exception:
        pass
    return d


# 注：theme_to_css_cached 已在文件开头的基础工具区定义过一次，
# 这里不要重复定义 —— 同名函数后者静默覆盖前者，属于最难查的一类 bug。

def read_theme(p):
    """读取主题列表 / 单个主题 / 当前生效主题 / 可定制变量清单。"""
    p = p or {}
    op = str(p.get('op') or 'list').strip()
    if op == 'vars':
        return ok({'vars': [{'name': k, 'cat': v, 'cat_cn': dict(theme.THEME_CATS).get(v, v),
                             'cn': theme.THEME_VAR_CN.get(k, '')}
                            for k, v in theme.THEME_VARS.items()],
                   'cats': [{'k': a, 'n': b} for a, b in theme.THEME_CATS],
                   'defaults': theme.make_palette('#1f6feb', False, '基准')['vars']})
    if op == 'get':
        tid = str(p.get('id') or '').strip()
        t = _theme_find(tid)
        if not t:
            return fail('主题不存在：%s' % tid, 'NOT_FOUND')
        out = dict(t)
        out['css_text'] = theme.theme_to_css(t)
        return ok(out)
    # list
    allt, _ = _theme_all()
    active = _theme_read_active()
    return ok({'themes': [theme.summarize(t) for t in allt],
               'active': active,
               'active_name': next((t['name'] for t in allt if t['id'] == active), ''),
               'vars_total': len(theme.THEME_VARS),
               'dir': THEME_DIR,
               'css_path': THEME_CSS})


def act_theme(p):
    """主题之家：save / delete / apply / reset / export / import / preview。"""
    p = p or {}
    op = str(p.get('op') or '').strip()

    # ---------- 保存（新建或更新自定义主题，不自动应用） ----------
    if op == 'save':
        obj = p.get('theme')
        if not isinstance(obj, dict):
            return fail('缺少主题内容', 'BAD_THEME')
        good, res = theme.validate_theme(obj)
        if not good:
            return fail(res, 'BAD_THEME')
        tid = str(obj.get('id') or '').strip().lower()
        allt, taken = _theme_all()
        if tid:
            if not theme._RE_ID.match(tid):
                return fail('主题 id 不合法（只能是小写字母、数字与 -）', 'BAD_ID')
            cur = next((t for t in allt if t['id'] == tid), None)
            if cur and cur.get('builtin'):
                return fail('内置主题「%s」不可覆盖，请另存为新主题' % cur['name'], 'BUILTIN')
            if cur:
                res['created_at'] = cur.get('created_at') or ''
        else:
            tid = theme.theme_id_from_name(res['name'], taken)
            res['created_at'] = ''
        res['id'] = tid
        _theme_save_dir(tid, res)
        return ok({'id': tid, 'theme': theme.summarize(res)},
                  '主题「%s」已保存（尚未应用）' % res['name'])

    # ---------- 删除 ----------
    if op == 'delete':
        tid = str(p.get('id') or '').strip().lower()
        if not tid:
            return fail('缺少主题 id')
        cur = _theme_find(tid)
        if not cur:
            return fail('主题不存在：%s' % tid, 'NOT_FOUND')
        if cur.get('builtin'):
            return fail('内置主题不可删除', 'BUILTIN')
        if _theme_read_active() == tid:
            return fail('该主题正在使用中，请先切换到其它主题再删除', 'IN_USE')
        d = os.path.join(THEME_DIR, tid)
        try:
            shutil.rmtree(d)
        except FileNotFoundError:
            pass
        except Exception as e:
            return fail('删除失败：%s' % e)
        return ok({'id': tid}, '主题「%s」已删除' % cur['name'])

    # ---------- 应用 ----------
    if op == 'apply':
        tid = str(p.get('id') or '').strip()
        # 用「不过滤」的原始读取：一旦主题被手工改坏，也要能指出具体原因，
        # 而不是让用户看到一句无从下手的「主题不存在」。
        raw = _theme_find_raw(tid)
        if raw is None:
            return fail('主题不存在：%s' % tid, 'NOT_FOUND')
        if raw.get('_raw_error'):
            return fail('主题「%s」已损坏，拒绝应用：%s'
                        % (raw.get('name') or tid, raw['_raw_error']), 'BAD_THEME')
        # 再次校验（磁盘上的主题可能被手工改坏）
        good, res = theme.validate_theme(raw)
        if not good:
            return fail('主题「%s」校验未通过，已拒绝应用：%s'
                        % (raw.get('name') or tid, res), 'BAD_THEME')
        n = _theme_apply_css(res)
        res['id'] = tid
        _theme_write_active(tid)
        return ok({'id': tid, 'css_bytes': n, 'css_path': THEME_CSS},
                  '已应用主题「%s」，刷新页面即可看到效果' % (res.get('name') or tid))

    # ---------- 恢复默认 ----------
    if op == 'reset':
        t = _theme_find('default')
        if not t:
            return fail('内置默认主题缺失', 'NO_DEFAULT')
        _theme_apply_css(t)
        _theme_write_active('default')
        return ok({'id': 'default'}, '已恢复为默认主题「Drouter 经典蓝」')

    # ---------- 离线预览（不写盘、不应用） ----------
    if op == 'preview':
        obj = p.get('theme')
        if not isinstance(obj, dict):
            return fail('缺少主题内容', 'BAD_THEME')
        good, res = theme.validate_theme(obj)
        if not good:
            return fail(res, 'BAD_THEME')
        return ok({'css': theme.theme_to_css(res), 'vars': res['vars'],
                   'name': res['name']},
                  '预览已生成（未应用）')

    # ---------- 导出 ZIP ----------
    if op == 'export':
        import base64 as _b64
        tid = str(p.get('id') or '').strip()
        t = _theme_find(tid)
        if not t:
            return fail('主题不存在：%s' % tid, 'NOT_FOUND')
        data = theme.pack_theme_zip(t)
        if not data:
            return fail('打包失败')
        # 扩展名取自 theme.THEME_EXT，保持「解包/导出」同一份来源，避免两处写死走偏
        fname = '%s-%s%s.zip' % (theme.slugify(t['name']), t['id'], theme.THEME_EXT)
        return ok({'filename': fname, 'size': len(data),
                   'b64': _b64.b64encode(data).decode('ascii')},
                  '主题包已生成（%d KB）' % max(1, len(data) // 1024))

    # ---------- 导入 ZIP ----------
    if op == 'import':
        import base64 as _b64
        raw = p.get('b64') or p.get('data_b64') or ''
        if not raw:
            return fail('没有收到文件内容，请重新选择主题包')
        try:
            data = _b64.b64decode(raw, validate=True)
        except Exception:
            return fail('文件内容不是合法的 Base64，可能上传中断，请重试')
        good, res = theme.unpack_theme_zip(data)
        if not good:
            return fail(res, 'BAD_ZIP')
        # 分配不冲突的 id
        allt, taken = _theme_all()
        tid = theme.theme_id_from_name(res['name'], taken)
        res['id'] = tid
        res['created_at'] = ''
        _theme_save_dir(tid, res)
        return ok({'id': tid, 'theme': theme.summarize(res)},
                  '主题包「%s」导入成功，可在列表中点击「应用」' % res['name'])

    # ---------- 导入并直接应用（一键） ----------
    if op == 'import_apply':
        r2 = act_theme({'op': 'import', 'b64': p.get('b64') or p.get('data_b64') or ''})
        if not r2.get('ok'):
            return r2
        tid = (r2.get('data') or {}).get('id')
        r3 = act_theme({'op': 'apply', 'id': tid})
        if not r3.get('ok'):
            return r3
        return ok(r3.get('data'), '%s；%s' % (r2.get('msg_cn'), r3.get('msg_cn')))

    # ---------- 校验（只校验不保存，用于「校验」按钮） ----------
    if op == 'validate':
        obj = p.get('theme')
        if not isinstance(obj, dict):
            return fail('缺少主题内容', 'BAD_THEME')
        good, res = theme.validate_theme(obj)
        if not good:
            return fail(res, 'BAD_THEME')
        return ok({'vars': len(res['vars']), 'name': res['name'], 'dark': res['dark']},
                  '主题校验通过：%d 个变量，%s模式' % (len(res['vars']), '深色' if res['dark'] else '浅色'))

    # ---------- 一键生成配色（给主题设计器用） ----------
    if op == 'palette':
        color = str(p.get('color') or '#1f6feb').strip()
        if not theme._RE_HEX_STRICT.match(color):
            return fail('请提供 #rrggbb 格式的主色（例如 #1f6feb）')
        dark = bool(p.get('dark'))
        t = theme.make_palette(color, dark, str(p.get('name') or '自定义主题'),
                             '由主色自动生成的配色方案')
        return ok({'vars': t['vars'], 'dark': dark},
                  '已按主色 %s 生成%s配色' % (color, '深色' if dark else '浅色'))

    return fail('未知主题操作：%s' % op)


# ================================================================ 磁盘与日志清理（#3）
#
# 路由器常年不关机，几类文件会一直长：面板日志、防火墙流日志、系统日志轮转包、
# systemd journal、APT 缓存、临时文件、回收站、崩溃转储。小硬盘（8G/16G eMMC）
# 上最先出事的是 APT 缓存和流日志 —— 实测这两项就能吃掉 200MB 以上。
#
# 先立两条边界，再谈功能：
#   1. 只清「可再生产物」。配置库、证书、后端代码、快照本体一律不在清单内，
#      且删除前会再过一道 CLEAN_FORBIDDEN 前缀校验（防配置写错误删系统目录）。
#   2. 正在写入的活跃文件永不删除，只处理轮转出来的旧文件（*.1 / *.gz）。
#      这样清理期间日志系统不用重启，也不会因文件句柄被删而丢当前记录。

CLEAN_CONF = '/etc/drouter/cleanup.conf'
CLEAN_STATE = '/var/lib/drouter/cleanup-state.json'
CLEAN_SERVICE = 'drouter-cleanup.service'
CLEAN_TIMER = 'drouter-cleanup.timer'
CLEAN_WRAPPER = '/opt/drouter/scripts/cleanup-auto.sh'

# 定时器入口单独套一层 sh，是为了绕开 systemd 对 ExecStart 的引号解析：
# 直接写 ExecStart=... cleanup '{"op":"auto"}' 时 systemd 会把单引号吃掉，
# helper 收到的 payload 变成 {op:auto}，json.loads 直接抛错、任务静默失败。
CLEAN_WRAPPER_BODY = (
    "#!/bin/sh\n"
    "# drouter 磁盘与日志清理 —— 定时器入口（由 helper 自动生成，勿手改）\n"
    "exec /usr/bin/python3 /opt/drouter/backend/drouter-helper.py cleanup '{\"op\":\"auto\"}'\n"
)

# 任何删除动作都会先比对这张前缀表。命中即拒，宁可少删也不误删。
CLEAN_FORBIDDEN = (
    '/etc', '/boot', '/usr', '/bin', '/sbin', '/lib', '/lib32', '/lib64',
    '/dev', '/proc', '/sys', '/run', '/root', '/home', '/srv',
    '/opt/drouter/backend', '/opt/drouter/web', '/opt/drouter/data',
    '/opt/drouter/snapshots', '/var/lib/dpkg', '/var/lib/apt', '/var/cache/apt',
)

# 清理项清单。
#   kind='age' —— 按文件最后修改时间清理，保留 N 天内的
#   kind='cmd' —— 交给系统自带工具整体回收（journalctl / apt），无法按天拆分
# paths 里 exclude 的写法与 fnmatch 一致；'x/*' 会同时命中目录 x 本身。
CLEAN_ITEMS = [
    {
        'key': 'ulog', 'name': '防火墙流日志（旧）', 'kind': 'age',
        'days': 3, 'on': True,
        'paths': [{'dir': '/var/log/drouter', 'glob': 'ulog*.jsonl.[0-9]*'},
                  {'dir': '/var/log/drouter', 'glob': 'ulog*.jsonl.*.gz'},
                  {'dir': '/var/log/drouter', 'glob': 'ulog*.log.[0-9]*'}],
        'why': '记录每一条经防火墙的连接。这是全系统增长最快的文件 —— '
               '本机还没接管路由时一天就能写 1MB，真正接管后会大得多。',
        'risk': '只删轮转出来的旧文件，正在写的 ulog.jsonl 不动，不会丢当前记录。',
        # peek 只统计不清理：让用户看见「当前正在写的那个文件」有多大。
        # 没有它，流日志项在新机器上会显示「占用 0」，用户反而以为没风险。
        'peek': [{'dir': '/var/log/drouter', 'glob': 'ulog*.jsonl'},
                 {'dir': '/var/log/drouter', 'glob': 'ulog*.log'}],
    },
    {
        'key': 'drouter_log', 'name': '面板运行日志（旧）', 'kind': 'age',
        'days': 7, 'on': True,
        'paths': [{'dir': '/var/log/drouter', 'glob': '*.jsonl.[0-9]*', 'exclude': ['ulog*']},
                  {'dir': '/var/log/drouter', 'glob': '*.jsonl.*.gz', 'exclude': ['ulog*']},
                  {'dir': '/var/log/drouter', 'glob': '*.log.[0-9]*', 'exclude': ['ulog*']},
                  {'dir': '/var/log/drouter', 'glob': '*.log.*.gz', 'exclude': ['ulog*']}],
        'why': '面板各模块的操作与状态日志，由 logrotate 每天轮转一份。',
        'risk': '同上，活跃日志不删；上面那一项已把 ulog* 排除，两边不会重复计数。',
        'peek': [{'dir': '/var/log/drouter', 'glob': '*.jsonl', 'exclude': ['ulog*']},
                 {'dir': '/var/log/drouter', 'glob': '*.log', 'exclude': ['ulog*']}],
    },
    {
        'key': 'varlog', 'name': '系统日志轮转包', 'kind': 'age',
        'days': 14, 'on': True,
        'paths': [{'dir': '/var/log', 'glob': '*.gz', 'recursive': True,
                   'exclude': ['drouter/*', 'journal/*']},
                  {'dir': '/var/log', 'glob': '*.[0-9]', 'recursive': True,
                   'exclude': ['drouter/*', 'journal/*']}],
        'why': 'rsyslog / cups / apt 等系统服务轮转出来的压缩包。'
               '解压看过一次之后基本没有留存价值。',
        'risk': '已排除 /var/log/drouter（面板日志单独管理）与 journal 目录'
                '（由 systemd 自己回收），避免重复清理与半删状态。',
    },
    {
        'key': 'journal', 'name': 'systemd 日志（journal）', 'kind': 'cmd',
        'days': 7, 'on': True,
        'size_dirs': ['/var/log/journal', '/run/log/journal'],
        'cmd': ['journalctl', '--vacuum-time={days}d'],
        'why': '所有 systemd 服务的标准输出与内核日志。'
               '默认不限制大小，几个月不管能长到几百 MB。',
        'risk': '用 journalctl 官方命令回收，不会损坏正在写的 journal 文件。'
                '超过设定天数的历史日志会被丢弃。',
    },
    {
        'key': 'apt', 'name': 'APT 软件包缓存', 'kind': 'cmd',
        'days': 0, 'on': True,
        'size_dirs': ['/var/cache/apt/archives', '/var/lib/apt/lists'],
        'cmd': ['apt-get', 'clean'],
        'cmd2': ['find', '/var/lib/apt/lists', '-mindepth', '1', '-maxdepth', '1',
                 '-type', 'f', '-delete'],
        'why': '安装 / 升级时下载的 .deb 包与软件源索引。装完就没人再看，'
               '这是所有清理项里最安全、也最常见的一块「死重量」。',
        'risk': '删除后重装同名包需要重新联网下载。'
                '离线环境（或源已失效）请先把这项关掉。',
    },
    {
        'key': 'snapdl', 'name': '快照下载打包残留', 'kind': 'age',
        'days': 1, 'on': True, 'prune_dirs': True,
        'paths': [{'dir': '/tmp/drouter-snapshot-dl', 'glob': '*.tar.gz'}],
        'why': '从面板把配置快照下载到本地时生成的压缩包，下载完就没用了。',
        'risk': '只删 .tar.gz，不涉及 /opt/drouter/snapshots 里的快照本体。',
    },
    {
        'key': 'tmp', 'name': '临时文件（/tmp、/var/tmp）', 'kind': 'age',
        'days': 3, 'on': True,
        'paths': [{'dir': '/tmp', 'glob': '*', 'recursive': True,
                   'exclude': ['systemd-private*', 'drouter-snapshot-dl/*',
                               '.X11-unix/*', '.ICE-unix/*', '.font-unix/*']},
                  {'dir': '/var/tmp', 'glob': '*', 'recursive': True,
                   'exclude': ['systemd-private*']}],
        'why': '程序崩溃、安装中断留下的残骸。Debian 的 /tmp 默认不会自动清理。',
        'risk': '只删 3 天没动过的文件；正在使用的临时文件时间都很新，不会被误删。'
                '已排除 systemd 私有目录与桌面运行时目录。',
    },
    {
        'key': 'trash', 'name': '文件管理器回收站', 'kind': 'age',
        'days': 7, 'on': True, 'prune_dirs': True,
        'paths': [{'dir': '/var/lib/drouter/trash', 'glob': '*', 'recursive': True}],
        'why': '在「Web 终端 / 文件」里删除的文件先挪到这里，方便反悔。'
               '但它不会自己变小。',
        'risk': '相当于给回收站加一个保质期，超过设定天数的才清。',
    },
    {
        'key': 'core', 'name': '崩溃转储（coredump）', 'kind': 'age',
        'days': 7, 'on': True,
        'paths': [{'dir': '/var/lib/systemd/coredump', 'glob': '*', 'recursive': True}],
        'why': '程序崩溃时内核 dump 下来的内存镜像，单个常常几十上百 MB，'
               '而且一次崩溃可能留下好几个。',
        'risk': '只有排查崩溃原因时才需要，超过设定天数的删掉不影响运行。',
    },
]

CLEAN_ITEM_KEYS = [it['key'] for it in CLEAN_ITEMS]


def _clean_defaults():
    return {
        'enabled': True,           # 自动清理总开关
        'trigger': 'both',         # disk=只看水位 | schedule=只看时间 | both=任一满足
        'disk_percent': 85,        # 水位阈值：根分区使用率超过多少就清
        'schedule': 'daily',       # daily=每天 | weekly=每周一
        'hour': 4,                 # 定时执行小时（0-23）
        'max_mb_per_run': 0,       # 单次最多释放多少 MB，0=不限
        'items': {it['key']: {'on': it['on'], 'days': it['days']} for it in CLEAN_ITEMS},
    }


def _clean_load():
    cfg = _clean_defaults()
    try:
        if os.path.isfile(CLEAN_CONF):
            with open(CLEAN_CONF, encoding='utf-8') as f:
                disk = json.load(f) or {}
            # items 要逐项合并，否则新增清理项时老配置文件会把新项整个顶掉
            items = dict(cfg['items'])
            for k, v in (disk.get('items') or {}).items():
                if k in items and isinstance(v, dict):
                    items[k].update(v)
            disk.pop('items', None)
            cfg.update(disk)
            cfg['items'] = items
    except Exception:
        pass
    return cfg


def _clean_save(cfg):
    _atomic_write(CLEAN_CONF,
                  json.dumps(cfg, ensure_ascii=False, indent=2) + '\n')
    _clean_status_cache_invalidate()


# 状态页扫描缓存：_clean_status 要对每个清理项 os.walk 一遍目录树
# （/var/log、回收站、打包目录…），机械盘上能到秒级。页面每进一次就全扫
# 一遍没有必要 —— 30 秒内复用上次结果；配置保存 / 执行清理后立即失效。
_clean_status_cache = {'ts': 0.0, 'items': None, 'reclaim': 0}
_clean_status_lock = threading.Lock()
_CLEAN_STATUS_TTL = 30.0


def _clean_status_cache_invalidate():
    with _clean_status_lock:
        _clean_status_cache['ts'] = 0.0
        _clean_status_cache['items'] = None
        _clean_status_cache['reclaim'] = 0


def _clean_state_load():
    try:
        if os.path.isfile(CLEAN_STATE):
            with open(CLEAN_STATE, encoding='utf-8') as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}


def _clean_state_save(d):
    try:
        os.makedirs(os.path.dirname(CLEAN_STATE), exist_ok=True)
        with open(CLEAN_STATE, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _clean_safe(path):
    """删除前的最后一道闸：解析真实路径后比对禁用前缀表。"""
    try:
        real = os.path.realpath(path)
    except Exception:
        return False
    for bad in CLEAN_FORBIDDEN:
        if real == bad or real.startswith(bad.rstrip('/') + '/'):
            return False
    return True


def _clean_excluded(rel, exc):
    for e in exc:
        if fnmatch.fnmatch(rel, e):
            return True
        # 'drouter/*' 这种写法要能同时命中目录 drouter 本身
        if e.endswith('/*') and fnmatch.fnmatch(rel, e[:-2]):
            return True
    return False


def _clean_files_of(spec):
    """枚举一个 glob 规格下所有匹配到的普通文件。返回 [{path,mtime,size}]"""
    out = []
    root = spec.get('dir') or ''
    if not os.path.isdir(root):
        return out
    pat = spec.get('glob') or '*'
    exc = list(spec.get('exclude') or [])
    rec = bool(spec.get('recursive'))

    def take(d, fn):
        rel = os.path.relpath(os.path.join(d, fn), root)
        if _clean_excluded(rel, exc):
            return
        if not fnmatch.fnmatch(fn, pat):
            return
        p = os.path.join(d, fn)
        try:
            st = os.lstat(p)
        except Exception:
            return
        if not stat.S_ISREG(st.st_mode):
            return
        out.append({'path': p, 'mtime': st.st_mtime, 'size': st.st_size})

    if rec:
        for d, dirs, files in os.walk(root):
            keep = []
            for sub in dirs:
                rel = os.path.relpath(os.path.join(d, sub), root)
                if _clean_excluded(rel, exc) or _clean_excluded(rel + '/', exc):
                    continue
                keep.append(sub)
            dirs[:] = keep
            for fn in files:
                take(d, fn)
    else:
        try:
            names = os.listdir(root)
        except Exception:
            return out
        for fn in names:
            take(root, fn)
    return out


def _clean_dir_bytes(path):
    if not os.path.isdir(path):
        return 0
    total = 0
    for d, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.lstat(os.path.join(d, fn)).st_size
            except Exception:
                pass
    return total


def _clean_item_stat(item, days):
    """扫描单项：总占用、可释放、命中数量、几个样本。不做任何删除。"""
    days = int(days or 0)
    if item['kind'] == 'cmd':
        total = 0
        for d in (item.get('size_dirs') or []):
            total += _clean_dir_bytes(d)
        return {'kind': 'cmd', 'files': 0, 'total': total, 'active': 0,
                'reclaimable': total, 'hit_count': 0, 'samples': [], 'whole': True}
    files = []
    for spec in (item.get('paths') or []):
        files.extend(_clean_files_of(spec))
    now = time.time()
    lim = days * 86400.0
    hit = []
    total = 0
    for f in files:
        total += f['size']
        if days <= 0 or (now - f['mtime']) > lim:
            hit.append(f)
    hit.sort(key=lambda x: -x['size'])
    # peek：只统计、绝不清理的「当前活跃文件」。用来告诉用户这块日志正在长多大。
    active = 0
    for spec in (item.get('peek') or []):
        active += sum(f['size'] for f in _clean_files_of(spec))
    return {'kind': 'age', 'files': len(files), 'total': total, 'active': active,
            'reclaimable': sum(f['size'] for f in hit), 'hit_count': len(hit),
            'samples': [{'path': f['path'], 'size': f['size'],
                         'age_days': round((now - f['mtime']) / 86400.0, 1)}
                        for f in hit[:5]],
            'whole': False}


def _clean_prune_dirs(item):
    """删除后顺手清掉空目录（回收站 / 打包目录按时间戳分子目录，文件删完壳还在）。"""
    removed = 0
    for spec in (item.get('paths') or []):
        root = spec.get('dir') or ''
        if not os.path.isdir(root) or not _clean_safe(root):
            continue
        for d, dirs, files in os.walk(root, topdown=False):
            if os.path.abspath(d) == os.path.abspath(root):
                continue
            try:
                if not os.listdir(d):
                    os.rmdir(d)
                    removed += 1
            except Exception:
                pass
    return removed


def _clean_run_item(item, days, dry, budget):
    """执行单项清理。budget 为剩余可释放字节（None=不限）。
    返回 (freed, removed_paths, skipped_by_budget, errs)"""
    days = int(days or 0)
    freed = 0
    removed = []
    errs = []
    stopped = False

    if item['kind'] == 'cmd':
        if dry:
            return _clean_item_stat(item, days)['reclaimable'], [], False, []
        before = sum(_clean_dir_bytes(d) for d in (item.get('size_dirs') or []))
        for ck in ('cmd', 'cmd2', 'cmd3'):
            argv = item.get(ck)
            if not argv:
                continue
            argv = [a.replace('{days}', str(days)) for a in argv]
            rc, out, err = sh(argv, timeout=180)
            if rc != 0:
                errs.append('%s 失败(rc=%d)：%s' % (' '.join(argv), rc, (err or out)[:200]))
        after = sum(_clean_dir_bytes(d) for d in (item.get('size_dirs') or []))
        return max(0, before - after), [], False, errs

    st = _clean_item_stat(item, days)
    now = time.time()
    lim = days * 86400.0
    files = []
    for spec in (item.get('paths') or []):
        files.extend(_clean_files_of(spec))
    for f in files:
        if days > 0 and (now - f['mtime']) <= lim:
            continue
        if budget is not None and freed >= budget:
            stopped = True
            break
        p = f['path']
        if not _clean_safe(p):
            errs.append('%s 被安全策略拒绝' % p)
            continue
        try:
            sz = os.path.getsize(p)
            if not dry:
                os.remove(p)
            freed += sz
            removed.append(p)
        except Exception as e:
            errs.append('%s：%s' % (p, e))
    if not dry and item.get('prune_dirs'):
        _clean_prune_dirs(item)
    del st
    return freed, removed, stopped, errs


def _clean_status():
    cfg = _clean_load()
    now_m = time.monotonic()
    with _clean_status_lock:
        hit = (_clean_status_cache['items'] is not None
               and (now_m - _clean_status_cache['ts']) < _CLEAN_STATUS_TTL)
        items = _clean_status_cache['items']
        reclaim = _clean_status_cache['reclaim']
    if not hit:
        items = []
        reclaim = 0
        for it in CLEAN_ITEMS:
            ic = (cfg.get('items') or {}).get(it['key']) or {}
            days = int(ic.get('days', it['days']))
            on = bool(ic.get('on', it['on']))
            st = _clean_item_stat(it, days)
            if on:
                reclaim += st['reclaimable']
            items.append({
                'key': it['key'], 'name': it['name'], 'kind': it['kind'],
                'on': on, 'days': days, 'whole': st['whole'],
                'why': it['why'], 'risk': it['risk'],
                'files': st['files'], 'total': st['total'], 'active': st['active'],
                'reclaimable': st['reclaimable'], 'hit_count': st['hit_count'],
                'samples': st['samples'],
            })
        with _clean_status_lock:
            _clean_status_cache['ts'] = now_m
            _clean_status_cache['items'] = items
            _clean_status_cache['reclaim'] = reclaim
    total, used, free = _disk_usage('/')
    pct = round(used * 100.0 / total, 1) if total else 0.0
    rc, act, _e = sh(['systemctl', 'is-active', CLEAN_TIMER], timeout=8)
    next_run = ''
    _rc2, nx, _e2 = sh(['systemctl', 'list-timers', CLEAN_TIMER, '--no-pager',
                        '--output=json'], timeout=10)
    try:
        arr = json.loads(nx or '[]')
        if arr:
            next_run = str(arr[0].get('next') or '')
    except Exception:
        pass
    return ok({'config': cfg, 'items': items,
               'disk': {'total_mb': total, 'used_mb': used, 'free_mb': free,
                        'percent': pct},
               'reclaimable': reclaim,
               'timer_active': (rc == 0 and act == 'active'),
               'next_run': next_run,
               'state': _clean_state_load()}, '扫描完成')


def _clean_write_units():
    """生成 cleanup 的 service + timer + 入口脚本。

    定时器固定「每小时」跑一次，由 _clean_auto() 自己判断该不该真动手 ——
    这样「水位触发」和「定时触发」共用一套单元，不用维护两个 timer。
    """
    try:
        os.makedirs(os.path.dirname(CLEAN_WRAPPER), exist_ok=True)
        with open(CLEAN_WRAPPER, 'w', encoding='utf-8') as f:
            f.write(CLEAN_WRAPPER_BODY)
        os.chmod(CLEAN_WRAPPER, 0o755)
    except Exception:
        pass
    svc = """[Unit]
Description=drouter 磁盘与日志清理
After=network.target

[Service]
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Type=oneshot
User=root
ExecStart=/bin/sh %s

[Install]
WantedBy=multi-user.target
""" % CLEAN_WRAPPER
    tmr = """[Unit]
Description=drouter 磁盘与日志清理定时器（每小时检查一次）

[Timer]
OnBootSec=15min
OnUnitActiveSec=1h
Persistent=true
Unit=%s

[Install]
WantedBy=timers.target
""" % CLEAN_SERVICE
    os.makedirs('/etc/systemd/system', exist_ok=True)
    with open('/etc/systemd/system/' + CLEAN_SERVICE, 'w', encoding='utf-8') as f:
        f.write(svc)
    with open('/etc/systemd/system/' + CLEAN_TIMER, 'w', encoding='utf-8') as f:
        f.write(tmr)
    return True


def _clean_save_op(p):
    cfg = _clean_load()
    if 'enabled' in p:
        cfg['enabled'] = bool(p['enabled'])
    if p.get('trigger') in ('disk', 'schedule', 'both'):
        cfg['trigger'] = p['trigger']
    if p.get('schedule') in ('daily', 'weekly'):
        cfg['schedule'] = p['schedule']
    for k, lo, hi in (('disk_percent', 50, 99), ('hour', 0, 23),
                      ('max_mb_per_run', 0, 1048576)):
        if p.get(k) is not None:
            try:
                cfg[k] = max(lo, min(int(p[k]), hi))
            except Exception:
                return fail('参数 %s 取值不合法' % k)
    for k, v in (p.get('items') or {}).items():
        if k not in CLEAN_ITEM_KEYS or not isinstance(v, dict):
            continue
        cur = cfg['items'].setdefault(k, {'on': True, 'days': 0})
        if 'on' in v:
            cur['on'] = bool(v['on'])
        if v.get('days') is not None:
            try:
                cur['days'] = max(0, min(int(v['days']), 3650))
            except Exception:
                return fail('清理项 %s 的保留天数不合法' % k)
    _clean_save(cfg)
    _clean_write_units()
    try:
        sh(['systemctl', 'daemon-reload'], timeout=20)
        if cfg.get('enabled'):
            sh(['systemctl', 'enable', '--now', CLEAN_TIMER], timeout=30)
        else:
            sh(['systemctl', 'disable', '--now', CLEAN_TIMER], timeout=30)
    except Exception:
        pass
    log('warn', 'system', 'CLEANUP_SET',
        '磁盘清理策略已更新（%s，触发=%s，水位=%s%%）'
        % ('开启' if cfg.get('enabled') else '关闭',
           cfg.get('trigger'), cfg.get('disk_percent')))
    return ok({'config': cfg},
              '清理策略已保存：%s，触发方式「%s」'
              % ('已开启' if cfg.get('enabled') else '已关闭',
                 {'disk': '仅水位触发', 'schedule': '仅定时触发',
                  'both': '水位 + 定时'}.get(cfg.get('trigger'), '水位 + 定时')))


def _clean_do(p, dry=False, auto=False):
    """真正执行清理。dry=True 只统计不删除。"""
    cfg = _clean_load()
    only = p.get('only') or []
    if only:
        only = [k for k in only if k in CLEAN_ITEM_KEYS]
    lim = int(cfg.get('max_mb_per_run') or 0)
    budget = lim * 1048576 if lim > 0 else None
    freed = 0
    if not dry:
        _clean_status_cache_invalidate()   # 删完文件后状态页必须重扫
    detail = []
    for it in CLEAN_ITEMS:
        ic = (cfg.get('items') or {}).get(it['key']) or {}
        if not bool(ic.get('on', it['on'])):
            continue
        if only and it['key'] not in only:
            continue
        days = int(ic.get('days', it['days']))
        left = None if budget is None else max(0, budget - freed)
        f, removed, stopped, errs = _clean_run_item(it, days, dry, left)
        freed += f
        detail.append({'key': it['key'], 'name': it['name'], 'freed': f,
                       'count': len(removed), 'stopped': stopped,
                       'errors': errs[:5],
                       'samples': [os.path.basename(x) for x in removed[:5]]})
    if not dry and freed > 0:
        _clean_state_save({'last_run': datetime.now().isoformat(timespec='seconds'),
                           'last_date': datetime.now().strftime('%Y-%m-%d'),
                           'last_freed': freed, 'auto': bool(auto)})
        log('info', 'system', 'CLEANUP_RUN',
            '%s释放 %.1f MB' % ('自动清理' if auto else '手动清理', freed / 1048576.0),
            {'items': [{d['key']: d['freed']} for d in detail]})
    mb = round(freed / 1048576.0, 1)
    # 只清掉几 KB 时四舍五入成 "0.0 MB" 会让人以为没干活，小数量改用 KB。
    human = ('%d KB' % max(1, round(freed / 1024.0))) if (freed > 0 and mb < 0.1) \
        else ('%.1f MB' % mb)
    msg = ('预计可释放 %s（试运行，未真正删除）' % human) if dry else \
          ('已释放 %s' % human if freed > 0 else '没有需要清理的文件')
    return ok({'dry': dry, 'freed': freed, 'freed_mb': mb, 'items': detail,
               'disk': (lambda t: {'total_mb': t[0], 'used_mb': t[1], 'free_mb': t[2],
                                   'percent': round(t[1] * 100.0 / t[0], 1) if t[0] else 0.0})(
                   _disk_usage('/'))}, msg)


def _clean_auto():
    """定时器入口：每小时被拉起一次，自己判断该不该真动手。"""
    cfg = _clean_load()
    if not cfg.get('enabled'):
        return ok({'skipped': True, 'reason': '自动清理未开启'}, '自动清理未开启，本次跳过')
    total, used, _free = _disk_usage('/')
    pct = round(used * 100.0 / total, 1) if total else 0.0
    trig = cfg.get('trigger') or 'both'
    watermark = pct >= float(cfg.get('disk_percent') or 85)
    due = False
    if trig in ('schedule', 'both'):
        now = datetime.now()
        st = _clean_state_load()
        today = now.strftime('%Y-%m-%d')
        # 注意别写 int(cfg.get('hour') or 4)：0 是 falsy，用户选「每天 00:00」
        # 会被悄悄当成没填、改成 04:00 执行。先判 None/'' 再转 int。
        _raw_hour = cfg.get('hour')
        plan_hour = 4 if _raw_hour in (None, '') else max(0, min(int(_raw_hour), 23))
        # 判定用 >= 而不是 ==：定时器虽然每小时醒一次，但机器可能在计划时刻
        # 正好关机/重启，== 会让这一天彻底漏掉（要等第二天），>= 能在当天晚些
        # 时候补做。语义上 hour 变成「不早于这个点」。
        if now.hour >= plan_hour:
            if cfg.get('schedule') == 'weekly':
                due = (now.weekday() == 0 and st.get('last_date') != today)
            else:
                due = (st.get('last_date') != today)
    if trig == 'disk':
        go = watermark
    elif trig == 'schedule':
        go = due
    else:
        go = watermark or due
    if not go:
        return ok({'skipped': True, 'percent': pct, 'watermark': watermark, 'due': due},
                  '磁盘 %s%%（阈值 %s%%），未到执行时间 —— 本次跳过'
                  % (pct, cfg.get('disk_percent')))
    return _clean_do({}, dry=False, auto=True)


def act_cleanup(p):
    """磁盘与日志清理：status / save / run / auto"""
    p = p or {}
    op = str(p.get('op') or 'status')
    if op == 'status':
        return _clean_status()
    if op == 'save':
        return _clean_save_op(p)
    if op == 'run':
        return _clean_do(p, dry=bool(p.get('dry')), auto=False)
    if op == 'auto':
        return _clean_auto()
    return fail('未知的清理操作：%s' % op)


# ================================================================ 内核转发与加速（#4）
#
# 五个内核级开关 + 两个防火墙出向参数。之所以把说明书写进后端（KERN_ITEMS），
# 是因为这几个参数最容易一知半解地打开，而「联动影响」比「开关本身」更重要：
#   * IP 转发 —— 不开，这台机器就只是局域网里的一台普通主机；
#   * masquerade —— 关了，内网就上不了网（除非上游写了回程路由）；
#   * MSS 钳制 —— 专治「部分网站打不开 / 图片加载一半」；
#   * BBR —— 只管本机自己发起的 TCP，不管转发的别人的流量；
#   * SNMP —— 开了就多一个 UDP 161 监听，默认关。
#
# 归属说明：MSS 与 masquerade 的最终落点是 nft_v4 / nft_v6 的配置库
# （render.py 从那里读），本页只是它们的统一入口，不另存一份，
# 免得两处配置打架。

KERN_CONF = '/etc/drouter/kern.conf'
KERN_SYSCTL_D = '/etc/sysctl.d/99-drouter.conf'
KERN_SNMP_CONF = '/etc/snmp/snmpd.conf'

KERN_DEFAULTS = {
    'fwd_v4': True,     # 路由器的本职工作，默认开
    'fwd_v6': True,     # 同上
    'bbr': True,        # 对高延迟/丢包链路明显更好，副作用小
    'snmp': {'enabled': False, 'community': 'public', 'port': 161,
             'listen': '0.0.0.0', 'contact': '', 'location': '',
             'sysname': 'drouter'},   # 默认关：多一个监听就多一分风险
}

KERN_ITEMS = [
    {
        'key': 'fwd_v4', 'name': 'IPv4 转发', 'default': True,
        'why': '决定本机要不要「替别人转发包」。关闭时它只是局域网里的一台普通主机，'
               '收到目的地不是自己的包就直接丢弃；打开后这台机器才真正具备路由器能力。',
        'impact': ['单独打开它网络不会立刻变化 —— 还得配合出向地址伪装（masquerade）'
                   '和正确的路由表，内网才能真正上网',
                   '它只决定「转不转」，不决定「安不安全」；访问控制由防火墙规则说了算'],
    },
    {
        'key': 'fwd_v6', 'name': 'IPv6 转发', 'default': True,
        'why': '同上，只是针对 IPv6。宽带没有 IPv6 时，开与不开都一样。',
        'impact': ['打开 IPv6 转发后，内核就不再接受 accept_ra=1 下的路由通告，'
                   '靠 SLAAC 拿地址的家宽会在租期到期后丢掉整个 IPv6。'
                   '本页开启时会自动同时设置 accept_ra=2（意为「开了转发也照收 RA」），'
                   '这是 OpenWrt / RouterOS 等固件的标准做法',
                   '将来真正接管路由后，WAN 口照收上游 RA、LAN 口由 radvd 自己发 RA，'
                   '那时再按接口细分即可',
                   'IPv6 不走 NAT，内网设备通常直接拿到公网 v6 地址。'
                   '所以「转发开了但上不了网」多半是前缀委派（PD）没拿到，'
                   '去「DHCPv6 / 前缀委派」页看'],
    },
    {
        'key': 'bbr', 'name': 'BBR 拥塞控制', 'default': True,
        'why': '谷歌的 TCP 拥塞控制算法。在有丢包或延迟偏高的链路上，'
               '比系统默认的 cubic 更能跑满带宽、延迟也更低。',
        'impact': ['只影响本机自己发起和终结的 TCP 连接（路由器上跑的代理、下载、'
                   'Docker 拉镜像等），不会影响「经过本机转发」的别人家流量 —— '
                   '拥塞控制是通信两端的事，中间的路由器改不了',
                   '会同时把默认队列规则设为 fq（BBR 的搭档）。'
                   '若你在「智能限速 QoS」里手工指定了 qdisc，以 QoS 页为准',
                   '需要内核模块 tcp_bbr（Debian 13 自带）。加载失败会自动退回 cubic，'
                   '页面会如实显示当前实际生效的算法'],
    },
    {
        'key': 'snmp', 'name': 'SNMP 监控', 'default': False,
        'why': '开一个标准 SNMP 服务，让网管软件（Zabbix / PRTG / Cacti / LibreNMS）'
               '能采集本机的接口流量、CPU、内存等指标。',
        'impact': ['会在 UDP 161 开一个监听。团体名（community）相当于只读口令，'
                   '建议别用默认的 public，并用防火墙限制可访问的网段',
                   '需要先安装 snmpd；没装时本页会提示去「依赖自检与安装」安装',
                   '默认关闭：家用环境一般用不到，多一个监听就多一分风险'],
    },
]

KERN_FW_ITEMS = [
    {
        'key': 'masquerade_v4', 'name': 'IPv4 出向地址伪装（masquerade / SNAT）',
        'default': True,
        'why': '把内网设备的私有地址改写成路由器自己的 WAN 地址再发出去。'
               '家用宽带通常只给一个公网 IP，内网几十台设备全靠它共用一个出口。',
        'impact': ['关掉之后内网就上不了网 —— 除非上游路由器已经写了回程路由，'
                   '知道「内网网段走 192.168.7.3」',
                   '做了伪装之后外网看不到内网真实 IP，'
                   '所以「从外网访问内网服务」才需要单独配端口转发或 DMZ',
                   '改动后要到「防火墙 IPv4」页点一次「保存并应用」才会生效'],
    },
    {
        'key': 'mss_v4', 'name': 'IPv4 MSS 钳制', 'default': True,
        'why': '在 TCP 握手时把「单个报文能装多少数据」压到链路扛得住的大小。'
               '专治 PPPoE 拨号后「部分网站打不开、图片加载一半、微信发不出图」。',
        'impact': ['clamp（按路由 MTU 自动算）适合绝大多数场景，MTU 变了不用改配置',
                   'fixed（写死数值）只在多拨 / 隧道 / GRE 这类 MTU 不统一的场景才需要',
                   '设得太小会让每个包装的数据变少、网速略降；PPPoE 一般 1452，'
                   '普通以太网 1500 即可',
                   '改动后要到「防火墙 IPv4」页点一次「保存并应用」才会生效'],
    },
    {
        'key': 'masquerade_v6', 'name': 'IPv6 出向地址伪装（NAT66）', 'default': False,
        'why': 'IPv6 版本的地址伪装。正常情况下不需要 —— IPv6 地址足够多，'
               '每台设备都能有自己的公网地址。',
        'impact': ['打开后内网设备对外只呈现一个 v6 地址，BT、摄像头 P2P、'
                   '远程桌面这类需要入向连接的应用会失效',
                   '只有「上游只给一个 /64 且不再下发前缀委派」时才需要它',
                   '默认关闭，这是 IPv6 的推荐做法'],
    },
    {
        'key': 'mss_v6', 'name': 'IPv6 MSS 钳制', 'default': False,
        'why': '同 IPv4 的 MSS 钳制，但 IPv6 靠 PMTUD（路径 MTU 发现）自己协商，'
               '绝大多数场景不需要人工干预。',
        'impact': ['只有 PPPoE / 隧道把 MTU 压到 1500 以下时才需要考虑打开',
                   '默认关闭；打开了却没问题也建议关掉，少一层改写'],
    },
]


def _sysctl_read(key):
    try:
        with open('/proc/sys/' + key.replace('.', '/'), encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return ''


def _kern_load():
    cfg = dict(KERN_DEFAULTS)
    cfg['snmp'] = dict(KERN_DEFAULTS['snmp'])
    try:
        if os.path.isfile(KERN_CONF):
            with open(KERN_CONF, encoding='utf-8') as f:
                d = json.load(f) or {}
            snmp = dict(cfg['snmp'])
            snmp.update(d.get('snmp') or {})
            d.pop('snmp', None)
            cfg.update(d)
            cfg['snmp'] = snmp
    except Exception:
        pass
    return cfg


def _kern_save(cfg):
    os.makedirs(os.path.dirname(KERN_CONF), exist_ok=True)
    with open(KERN_CONF, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _kern_sysctl_lines(cfg, bbr_ok=True):
    lines = ['# drouter 自动生成 —— 由「系统 → 内核转发与加速」维护，手改会被覆盖']
    lines.append('net.ipv4.ip_forward = %d' % (1 if cfg.get('fwd_v4') else 0))
    lines.append('net.ipv6.conf.all.forwarding = %d' % (1 if cfg.get('fwd_v6') else 0))
    if cfg.get('fwd_v6'):
        # 开启 IPv6 转发后，内核会忽略 accept_ra=1（默认值），于是本机不再接受
        # 上游的路由通告 —— 靠 SLAAC 拿地址的家宽会在租期到期后丢掉整个 IPv6。
        # 这台机器实测正是靠 RA 拿到 2408:8340:.../64 与 v6 默认路由的。
        # accept_ra=2 的含义就是「就算开了转发也照样接受 RA」，所有路由器
        # 固件（OpenWrt / RouterOS）在开启 v6 转发时都会顺手设这一项。
        lines.append('net.ipv6.conf.all.accept_ra = 2')
    if cfg.get('bbr') and bbr_ok:
        lines += ['net.ipv4.tcp_congestion_control = bbr',
                  'net.core.default_qdisc = fq']
    else:
        lines += ['net.ipv4.tcp_congestion_control = cubic',
                  'net.core.default_qdisc = fq_codel']
    return lines


def _kern_apply_sysctl(cfg):
    """写 sysctl.d 并即时生效。返回 (applied_keys, bbr_ok, errs)"""
    errs = []
    bbr_ok = False
    if cfg.get('bbr'):
        sh(['modprobe', 'tcp_bbr'], timeout=15)
        bbr_ok = 'bbr' in (_sysctl_read('net.ipv4.tcp_available_congestion_control') or '').split()
        if not bbr_ok:
            errs.append('内核未加载 tcp_bbr，BBR 未能启用（已保持 cubic）')
    lines = _kern_sysctl_lines(cfg, bbr_ok)
    try:
        os.makedirs('/etc/sysctl.d', exist_ok=True)
        with open(KERN_SYSCTL_D, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
    except Exception as e:
        errs.append('写入 %s 失败：%s' % (KERN_SYSCTL_D, e))
    applied = []
    for ln in lines:
        if ln.startswith('#') or '=' not in ln:
            continue
        k, v = [x.strip() for x in ln.split('=', 1)]
        rc, _o, e = sh(['sysctl', '-w', '%s=%s' % (k, v)], timeout=10)
        if rc != 0:
            errs.append('%s 未能生效：%s' % (k, (e or '')[:120]))
        else:
            applied.append(k)
    return applied, bbr_ok, errs


def _kern_snmp_conf(c):
    port = int(c.get('port') or 161)
    listen = (c.get('listen') or '').strip()
    addr = 'udp:%s:%d' % (listen, port) if listen else 'udp:%d' % port
    return '\n'.join([
        '# drouter 自动生成 —— 由「系统 → 内核转发与加速」维护',
        'agentAddress %s' % addr,
        'rocommunity %s default' % (c.get('community') or 'public'),
        'sysName %s' % (c.get('sysname') or 'drouter'),
        'sysLocation %s' % (c.get('location') or ''),
        'sysContact %s' % (c.get('contact') or ''),
    ]) + '\n'


def _kern_snmp_apply(c, errs):
    if not shutil.which('snmpd'):
        errs.append('未安装 snmpd，SNMP 未启动（请到「系统 → 依赖自检与安装」安装）')
        return
    try:
        os.makedirs(os.path.dirname(KERN_SNMP_CONF), exist_ok=True)
        with open(KERN_SNMP_CONF, 'w', encoding='utf-8') as f:
            f.write(_kern_snmp_conf(c))
    except Exception as e:
        errs.append('写入 %s 失败：%s' % (KERN_SNMP_CONF, e))
        return
    if c.get('enabled'):
        sh(['systemctl', 'enable', '--now', 'snmpd'], timeout=30)
    else:
        sh(['systemctl', 'disable', '--now', 'snmpd'], timeout=30)


def _kern_status():
    cfg = _kern_load()
    live = {
        'ip_forward': _sysctl_read('net.ipv4.ip_forward'),
        'ipv6_forwarding': _sysctl_read('net.ipv6.conf.all.forwarding'),
        'congestion': _sysctl_read('net.ipv4.tcp_congestion_control'),
        'qdisc': _sysctl_read('net.core.default_qdisc'),
        'available': _sysctl_read('net.ipv4.tcp_available_congestion_control'),
    }
    rc, act, _e = sh(['systemctl', 'is-active', 'snmpd'], timeout=8)
    v4 = _load_setting('nft_v4') or {}
    v6 = _load_setting('nft_v6') or {}
    fw = {
        'masquerade_v4': bool(v4.get('masquerade', True)),
        'mss_v4': bool(v4.get('mss_clamp', True)),
        'mss_v4_mode': str(v4.get('mss_mode') or 'clamp'),
        'mss_v4_value': int(v4.get('mss') or 1452),
        'masquerade_v6': bool(v6.get('masquerade', False)),
        'mss_v6': bool(v6.get('mss_clamp', False)),
        'mss_v6_mode': str(v6.get('mss_mode') or 'clamp'),
        'mss_v6_value': int(v6.get('mss') or 1432),
    }
    return ok({'config': cfg, 'live': live, 'fw': fw,
               'snmp_installed': bool(shutil.which('snmpd')),
               'snmp_active': (rc == 0 and act == 'active'),
               'build_mode': in_build_mode(),
               'items': KERN_ITEMS, 'fw_items': KERN_FW_ITEMS,
               'sysctl_file': KERN_SYSCTL_D}, '已读取内核与转发状态')


def _kern_save_op(p):
    p = p or {}
    cfg = _kern_load()
    for k in ('fwd_v4', 'fwd_v6', 'bbr'):
        if p.get(k) is not None:
            cfg[k] = bool(p[k])
    s = p.get('snmp')
    if isinstance(s, dict):
        if s.get('enabled') is not None:
            cfg['snmp']['enabled'] = bool(s['enabled'])
        if s.get('community') is not None:
            comm = str(s['community']).strip()
            if not comm or len(comm) > 64 or re.search(r'[\s#]', comm):
                return fail('SNMP 团体名不能为空、不能含空格或 # 号，且不超过 64 个字符')
            cfg['snmp']['community'] = comm
        if s.get('port') is not None:
            try:
                cfg['snmp']['port'] = max(1, min(int(s['port']), 65535))
            except Exception:
                return fail('SNMP 端口取值不合法')
        if s.get('listen') is not None:
            lis = str(s['listen']).strip()
            if lis and not re.match(r'^[0-9a-fA-F:.]{2,45}$', lis):
                return fail('SNMP 监听地址不合法（填 IP 或留空表示监听全部地址）')
            cfg['snmp']['listen'] = lis
        for k in ('contact', 'location', 'sysname'):
            if s.get(k) is not None:
                # 这三个字段被原样拼进 snmpd.conf 的 `sysName %s` 等指令行。
                # 原来只截长度、不过滤控制字符，于是 sysname 里塞一个换行就能
                # 凭空插出一条 rwcommunity —— 把只读团体名升级成可写，
                # 之后就能改路由/接口/重启设备。同段的 community 有 [\s#] 校验，
                # 这三个漏了。
                val = re.sub(r'[\r\n]', ' ', str(s[k]))
                val = val.replace('#', ' ').strip()[:128]
                cfg['snmp'][k] = val

    # ---- 防火墙的两组参数写回各自模块的配置库（render.py 从那里读）----
    fw = p.get('fw') or {}
    v4 = _load_setting('nft_v4') or {}
    v6 = _load_setting('nft_v6') or {}

    for dst, fam in ((v4, 'v4'), (v6, 'v6')):
        if fw.get('mss_%s' % fam) is not None:
            dst['mss_clamp'] = bool(fw['mss_%s' % fam])
        if fw.get('mss_%s_mode' % fam) in ('clamp', 'fixed'):
            dst['mss_mode'] = fw['mss_%s_mode' % fam]
        if fw.get('mss_%s_value' % fam) is not None:
            try:
                dst['mss'] = max(576, min(int(fw['mss_%s_value' % fam]), 9000))
            except Exception:
                return fail('IPv%s 的 MSS 数值不合法（576–9000）' % fam)
        if fw.get('masquerade_%s' % fam) is not None:
            dst['masquerade'] = bool(fw['masquerade_%s' % fam])
    _save_setting('nft_v4', v4)
    _save_setting('nft_v6', v6)

    errs = []
    applied = []
    bbr_ok = True
    # 写盘永远做；真正改内核参数和启停服务，只在非构建保护模式下进行
    if in_build_mode():
        errs.append('当前处于【构建保护模式】：配置已写入磁盘，但未修改内核参数、未启停服务')
        with open(KERN_SYSCTL_D, 'w', encoding='utf-8') as f:
            f.write('\n'.join(_kern_sysctl_lines(cfg, True)) + '\n')
    else:
        applied, bbr_ok, e2 = _kern_apply_sysctl(cfg)
        errs += e2
        _kern_snmp_apply(cfg['snmp'], errs)
    _kern_save(cfg)
    log('warn', 'system', 'KERN_SET',
        '内核与转发设置已更新（转发 v4=%s v6=%s，BBR=%s，SNMP=%s）'
        % (cfg.get('fwd_v4'), cfg.get('fwd_v6'), cfg.get('bbr'),
           cfg['snmp'].get('enabled')),
        {'applied': applied, 'errors': errs})
    msg = '已保存并生效' if not in_build_mode() else '已保存到配置（构建保护模式下未生效）'
    if errs:
        msg += '；' + '；'.join(errs)
    return ok({'config': cfg, 'applied': applied, 'errors': errs,
               'bbr_active': bbr_ok}, msg)


def act_kern(p):
    """内核转发与加速：status / save"""
    p = p or {}
    op = str(p.get('op') or 'status')
    if op == 'status':
        return _kern_status()
    if op == 'save':
        return _kern_save_op(p)
    return fail('未知的内核选项操作：%s' % op)


# ==================================================================== 新手向导
#
# ── 它是什么 ────────────────────────────────────────────────────────────────
# 中国大陆家庭宽带「第一次用这台机器上网」的最短路径：上行怎么拨号、内网怎么
# 发地址、DNS 用谁、要不要开 IPv6。四步走完，一个从没碰过路由器的人能上网。
#
# ── 它不是什么 ──────────────────────────────────────────────────────────────
# 它不重复实现任何已有功能，也不新造一份配置。每一「步」都是把值写进
# 既有模块的配置库（pppoe / dnsmasq / radvd / nft_v4），再调用既有的
# act_apply 走「渲染 → 语法预检 → 快照 → 原子写入 → 重启服务」同一条路。
# 向导只做三件别处不做的事：
#   1. 体检（probe）：把散在四五个页面上的状态汇总成一句话「你能不能上网」；
#   2. 范例（samples）：把「IP 段该填什么、DNS 填谁、IPv6 要不要开」写成
#      可直接抄的例子 —— 新手卡住的地方从来不是找不到输入框，而是不知道填啥；
#   3. 一次提交一组模块（apply_step）：手动逐个模块应用极易漏掉
#      「必须先在网卡页分配 WAN」这类前置条件。
#
# ── 安全 ────────────────────────────────────────────────────────────────────
# * 每一步都能单独提交，失败不影响别的步骤，也不会回滚已经成功的那几步；
# * 构建保护模式下一律只写盘、不重启服务（与全站一致）；
# * 「要不要真拨号」由用户单独确认：写凭据和应用配置不会把现有网络打断，
#   真正执行 connect 才可能断，所以那是独立的一个 op（dial）。

WIZ_STEPS = [
    {'k': 'wan', 'n': '第 1 步 · 接上外网', 'icon': '⇄',
     'd': '告诉这台机器「从哪里接宽带」，并且确认它真的连上了。'},
    {'k': 'lan', 'n': '第 2 步 · 给内网发地址', 'icon': '⌘',
     'd': '让手机、电脑插上网线或连上 Wi-Fi 就能自动拿到 IP，不用手工设置。'},
    {'k': 'dns', 'n': '第 3 步 · 能打开网址', 'icon': '⌖',
     'd': '域名要翻译成 IP 才能访问。这一步决定用运营商的 DNS 还是公共 DNS。'},
    {'k': 'v6', 'n': '第 4 步 · 要不要 IPv6', 'icon': '⁶',
     'd': 'IPv6 是新一代地址。国内宽带基本都支持了，多数情况建议开。'},
]

# 中国主流公共 DNS（第 3 步的下拉选项与「说明」都从这里取，前后端不各写一份）
WIZ_DNS_PRESETS = [
    {'k': 'ali', 'n': '阿里云 AliDNS', 'v4': '223.5.5.5,223.6.6.6',
     'd': '国内解析最快、覆盖最广，家庭宽带首选。'},
    {'k': 'dnspod', 'n': '腾讯 DNSPod', 'v4': '119.29.29.29,182.254.116.116',
     'd': '国内老牌，对恶意域名拦截做得比较积极。'},
    {'k': '114', 'n': '114DNS', 'v4': '114.114.114.114,114.114.115.115',
     'd': '运营商级公共 DNS，稳定但部分地区速度一般。'},
    {'k': 'cf', 'n': 'Cloudflare', 'v4': '1.1.1.1,1.0.0.1',
     'd': '国外 DNS。国内直连延迟偏高，访问国内网站可能变慢。'},
    {'k': 'gg', 'n': 'Google', 'v4': '8.8.8.8,8.8.4.4',
     'd': '国外 DNS。国内常被干扰，通常不作首选。'},
    {'k': 'isp', 'n': '跟随运营商下发', 'v4': '',
     'd': '用宽带拨号时运营商给的 DNS（PPPoE 会下发给本机）。最省心，但可能被劫持插广告。'},
]
WIZ_DNS_DEFAULT = '223.5.5.5,119.29.29.29'
WIZ_DNS_CACHE_DEFAULT = 1000
WIZ_LEASE_DEFAULT = 7200

WIZ_POOL_HINT = {
    'why': '内网设备的地址由这里开始分配。填错了轻则上不了网，重则和上游路由器打架。',
    'rules': [
        '前三段必须和本机 LAN 地址一致，最后一段落在 2–254 之间。',
        '起始–结束这段不要包含本机自己的地址（否则会和本机抢 IP）。',
        '常见做法是让池子从 .100 开始，把 .2–.99 留给打印机、NAS 这类固定设备。',
    ],
}

WIZ_WAN_MODES = [
    {'k': 'pppoe', 'n': 'PPPoE 拨号',
     'd': '需要宽带账号和密码，由本机发起拨号。中国电信 / 联通 / 移动 / 广电的家庭宽带最常见的就是这种。',
     'when': '光猫已设为桥接，或者你打算让本机取代运营商光猫拨号。'},
    {'k': 'dhcp', 'n': 'DHCP 自动获取（IPoE）',
     'd': '网线插上就有地址，不需要账号密码。',
     'when': '光猫已经在拨号，本机接在它后面；或者小区宽带 / 已开通 IPoE 的线路。'},
    {'k': 'static', 'n': '静态地址',
     'd': '运营商给了固定的 IP、网关和 DNS，手工填进去。',
     'when': '企业专线、固定 IP 宽带。家用极少见。'},
]


def _wiz_ip_in_pool(ip, start, end):
    """判断 ip 是否落在 [start, end] 这个 IPv4 区间内（按整数比，不能按字符串比）"""
    try:
        a = int(ipaddress.ip_address(ip))
        s = int(ipaddress.ip_address(start))
        e = int(ipaddress.ip_address(end))
    except Exception:
        return False
    lo, hi = (s, e) if s <= e else (e, s)
    return lo <= a <= hi


def _wiz_same_subnet(a, b, netmask='255.255.255.0'):
    """两个 IPv4 是否同网段（用于校验「地址池前三段 == 本机 LAN 前三段」）"""
    try:
        na = ipaddress.ip_network('%s/%s' % (a, netmask), strict=False)
        return ipaddress.ip_address(b) in na
    except Exception:
        return False


def _wiz_local_ipv4(iface):
    """取某张网卡的第一个 IPv4 地址（没有就返回空串）"""
    if not iface:
        return ''
    rc, raw, _e = sh(['ip', '-j', '-4', 'addr', 'show', 'dev', iface], timeout=8)
    if rc != 0:
        return ''
    try:
        for d in json.loads(raw or '[]'):
            for a in (d.get('addr_info') or []):
                if a.get('family') == 'inet' and a.get('local'):
                    return a['local']
    except Exception:
        pass
    return ''


def _wiz_ping(host, count=2, wait=2):
    """能不能 ping 通。用 ping -c N -W W；返回 (ok, 输出摘要)"""
    rc, out, _e = sh(['ping', '-c', str(count), '-W', str(wait), host], timeout=count * wait + 6)
    return rc == 0, (out or '').strip()


def _wiz_dns_probe(server, name='www.baidu.com', timeout=5):
    """用指定 DNS 解析一个域名。优先 dig，回退 nslookup。

    返回 (ok, 摘要)。这里的 ok 只代表「解析出了地址」，
    解析失败时要给出人话原因（超时 / 拒绝 / 没有该记录）。
    """
    tool = shutil.which('dig')
    if tool:
        rc, out, err = sh(['dig', '+time=%d' % timeout, '+tries=1', '+short',
                           '@' + server, name], timeout=timeout + 6)
        body = (out or '').strip()
        if rc == 0 and body and not body.startswith(';'):
            first = body.splitlines()[0] if body else ''
            return True, first
        blob = ((out or '') + (err or '')).lower()
        if 'timed out' in blob or 'timeout' in blob:
            return False, '查询超时（DNS 服务器没响应，可能被防火墙拦了）'
        if 'connection refused' in blob:
            return False, '连接被拒绝（对方 53 端口没开）'
        if 'nxdomain' in blob:
            return False, '域名不存在（DNS 服务器答复说查不到这个域名）'
        return False, (body.splitlines()[0] if body else '解析失败')
    nsl = shutil.which('nslookup')
    if nsl:
        rc, out, _e = sh([nsl, name, server], timeout=timeout + 6)
        if rc == 0 and 'Address' in (out or ''):
            return True, '解析成功（nslookup）'
        return False, '解析失败'
    return False, '本机没有 dig，也没有 nslookup，无法测试'


def _wiz_has_ipv6_global(iface):
    """该网卡是否有可用的公网 IPv6 地址（排除 fe80 链路本地）"""
    if not iface:
        return False
    rc, raw, _e = sh(['ip', '-j', '-6', 'addr', 'show', 'dev', iface], timeout=8)
    if rc != 0:
        return False
    try:
        for d in json.loads(raw or '[]'):
            for a in (d.get('addr_info') or []):
                if a.get('family') != 'inet6':
                    continue
                loc = a.get('local') or ''
                if loc.startswith('fe80'):
                    continue
                if a.get('scope') == 'global' or not loc.startswith(('fd', 'fc')):
                    return True
    except Exception:
        pass
    return False


def _wiz_svc_active(name):
    """单元是否 active（内部走批量实现，零额外 fork）。"""
    return _svc_states((name,))[name]['active'] == 'active'


def _wiz_service_exists(name):
    rc, _o, _e = sh(['systemctl', 'list-unit-files', name], timeout=10)
    return rc == 0


def _wiz_probe():
    """体检：把「能不能上网」拆成四个可以独立看懂的结论。

    刻意不一次性给一个总分 —— 总分对新手没有意义，他需要知道的是
    「哪一步还没做」，以及「这一步做了会怎样」。
    """
    cfg = _load_setting('dnsmasq') or {}
    ppp = _load_setting('pppoe') or {}
    sysc = _load_setting('system') or {}
    kern = _kern_load()

    # 性能：体检一共要问 4 次「dnsmasq/radvd 在不在、启没启」——原来每条都要
    # 单独 fork systemctl（dnsmasq 还被问了两遍，radvd 也是）。这里开头一次性
    # 批量取回，后面全查表。注意 _svc_states 对「单元不存在」也返回结构，
    # 但 _wiz_service_exists 是「单元文件存在与否」的语义，不能混用，单独保留。
    _svcs = _svc_states(('dnsmasq', 'radvd'))
    _wiz_dnsmasq_active = _svcs['dnsmasq']['active'] == 'active'
    _wiz_radvd_active = _svcs['radvd']['active'] == 'active'

    lan_iface = str(cfg.get('lan_iface') or sysc.get('lan_iface') or '')
    wan_iface = str(sysc.get('wan_iface') or ppp.get('iface') or '')
    lan_ip = _wiz_local_ipv4(lan_iface)

    # ---- 第 1 步：外网 ----
    mode = str(ppp.get('mode') or '')
    fwd4 = _sysctl_read('net.ipv4.ip_forward') == '1'
    default_rt = ''
    rc, out, _e = sh(['ip', '-4', 'route', 'show', 'default'], timeout=8)
    if rc == 0:
        default_rt = (out or '').strip()
    # 出网判据：本机有默认路由，且能 ping 通一个国内众所周知的地址。
    # 用 223.5.5.5 而不是 8.8.8.8：国内宽带 ping 不通 8.8.8.8 是常态，
    # 拿它做判据会把一大半「其实能上网」的用户判成失败。
    net_ok, ping_out = _wiz_ping('223.5.5.5', count=2, wait=2)
    wan = {
        'mode': mode,
        'mode_cn': next((x['n'] for x in WIZ_WAN_MODES if x['k'] == mode), '还没设置'),
        'iface': wan_iface,
        'has_wan_iface': bool(wan_iface),
        'has_account': bool((ppp.get('username') or '').strip()) if mode == 'pppoe' else True,
        'default_route': default_rt,
        'has_default_route': bool(default_rt),
        'online': bool(net_ok and default_rt),
        'ping_note': (ping_out.splitlines()[0] if ping_out else ''),
    }

    # ---- 第 2 步：内网发地址 ----
    dhcp_on = bool(cfg.get('dhcp_enabled'))
    start = str(cfg.get('pool_start') or '')
    end = str(cfg.get('pool_end') or '')
    pool_bad = []
    if not lan_ip:
        pool_bad.append('还没给 LAN 口配 IP 地址')
    if start and lan_ip and not _wiz_same_subnet(start, lan_ip, cfg.get('pool_netmask') or '255.255.255.0'):
        pool_bad.append('地址池起始地址（%s）和本机 LAN 地址（%s）不在同一个网段' % (start, lan_ip))
    if end and lan_ip and not _wiz_same_subnet(end, lan_ip, cfg.get('pool_netmask') or '255.255.255.0'):
        pool_bad.append('地址池结束地址（%s）和本机 LAN 地址（%s）不在同一个网段' % (end, lan_ip))
    if start and end and lan_ip and _wiz_ip_in_pool(lan_ip, start, end):
        pool_bad.append('地址池把本机自己的地址（%s）也包括进去了，会和本机抢 IP' % lan_ip)
    lan = {
        'iface': lan_iface,
        'lan_ip': lan_ip,
        'dhcp_enabled': dhcp_on,
        'pool_start': start,
        'pool_end': end,
        'pool_netmask': str(cfg.get('pool_netmask') or '255.255.255.0'),
        'lease_time': int(cfg.get('lease_time') or WIZ_LEASE_DEFAULT),
        'gateway': str(cfg.get('option_gateway') or ''),
        'dns_option': str(cfg.get('option_dns') or ''),
        'pool_problems': pool_bad,
        'dnsmasq_active': _wiz_dnsmasq_active,
        'ok': bool(dhcp_on and lan_ip and not pool_bad and _wiz_dnsmasq_active),
    }

    # ---- 第 3 步：DNS ----
    dmode = str(cfg.get('dns_mode') or 'custom')
    custom = str(cfg.get('dns_custom') or '')
    first_dns = (custom.split(',')[0].strip() if custom else '')
    if dmode == 'isp' or (dmode == 'both' and not first_dns):
        first_dns = ''
    dns_ok, dns_note = (False, '还没配 DNS')
    if first_dns:
        dns_ok, dns_note = _wiz_dns_probe(first_dns)
    elif dmode in ('isp', 'both'):
        # 运营商下发：去 /etc/resolv.conf 里挑第一个真实 nameserver 试
        cand = ''
        try:
            with open('/etc/resolv.conf', encoding='utf-8', errors='replace') as f:
                for ln in f:
                    ln = ln.strip()
                    if ln.startswith('nameserver'):
                        p = ln.split()
                        if len(p) > 1 and re.match(r'^\d+\.\d+\.\d+\.\d+$', p[1]):
                            cand = p[1]
                            break
        except Exception:
            pass
        if cand:
            dns_ok, dns_note = _wiz_dns_probe(cand)
            first_dns = cand
        else:
            dns_note = '选了「跟随运营商」，但当前还没拿到运营商下发的 DNS（多半是还没拨号成功）'
    dns = {
        'mode': dmode,
        'mode_cn': {'isp': '仅运营商下发', 'custom': '仅自定义', 'both': '两者合并'}.get(dmode, dmode),
        'custom': custom,
        'first': first_dns,
        'probe_ok': dns_ok,
        'probe_note': dns_note,
        'presets': WIZ_DNS_PRESETS,
        'default_custom': WIZ_DNS_DEFAULT,
        'ok': bool(dns_ok),
    }

    # ---- 第 4 步：IPv6 ----
    radvd_cfg = _load_setting('radvd') or {}
    v6_lan = _wiz_has_ipv6_global(lan_iface)
    v6_wan = _wiz_has_ipv6_global(wan_iface)
    fwd6 = _sysctl_read('net.ipv6.conf.all.forwarding') == '1'
    ra_on = _wiz_radvd_active
    v6 = {
        'lan_iface': lan_iface,
        'wan_iface': wan_iface,
        'lan_has_global': v6_lan,
        'wan_has_global': v6_wan,
        'forwarding': fwd6,
        'radvd_active': ra_on,
        'radvd_installed': _wiz_service_exists('radvd'),
        'prefix': str(radvd_cfg.get('prefix') or ''),
        'rdnss': str(radvd_cfg.get('rdnss') or ''),
        'advice': ('建议开启' if v6_wan or v6_lan else '可以先不开'),
        'ok': bool(v6_lan and ra_on),
    }

    # ---- 汇总 ----
    todo = []
    if not wan['online']:
        todo.append('第 1 步：外网还没通')
    if not lan['ok']:
        todo.append('第 2 步：内网还没在发地址')
    if not dns['ok']:
        todo.append('第 3 步：DNS 还解析不出结果')
    # IPv6 不通不算「没做完」—— 绝大多数功能不依赖它，只提示不拦。
    if not v6['ok']:
        todo.append('第 4 步：IPv6 还没启用（可选）')

    return ok({
        'steps': WIZ_STEPS,
        'wan': wan,
        'lan': lan,
        'dns': dns,
        'v6': v6,
        'wan_modes': WIZ_WAN_MODES,
        'pool_hint': WIZ_POOL_HINT,
        'dns_presets': WIZ_DNS_PRESETS,
        'internet_ok': bool(wan['online'] and lan['ok'] and dns['ok']),
        'todo': todo,
        'build_mode': in_build_mode(),
        'lan_ifaces': _wiz_lan_iface_options(),
        'wan_ifaces': _wiz_wan_iface_options(),
        'lease_default': WIZ_LEASE_DEFAULT,
        'dns_cache_default': WIZ_DNS_CACHE_DEFAULT,
    }, '体检完成')


def _wiz_iface_options():
    """给向导的两组网卡下拉：全部物理网卡（含当前角色）"""
    items = []
    rc, raw, _e = sh(['ip', '-j', 'link', 'show'], timeout=8)
    if rc == 0:
        try:
            for d in json.loads(raw or '[]'):
                n = d.get('ifname') or ''
                if not n or n == 'lo' or n.startswith(('veth', 'br-', 'virbr', 'ifb', 'docker', 'ppp')):
                    continue
                items.append({'name': n, 'mac': (d.get('address') or ''),
                              'state': (d.get('operstate') or '')})
        except Exception:
            pass
    items.sort(key=lambda x: x['name'])
    return items


def _wiz_wan_iface_options():
    out = _wiz_iface_options()
    sysc = _load_setting('system') or {}
    cur = str(sysc.get('wan_iface') or '')
    for x in out:
        x['role'] = 'wan' if x['name'] == cur else ''
    return out


def _wiz_lan_iface_options():
    out = _wiz_iface_options()
    sysc = _load_setting('system') or {}
    cur = str(sysc.get('lan_iface') or '')
    for x in out:
        x['role'] = 'lan' if x['name'] == cur else ''
    return out


def _wiz_apply(mod, cfg):
    """走既有 act_apply 同一条路（渲染 → 预检 → 快照 → 原子写 → 重启）

    返回 (ok, msg, data)。这里不吞异常 —— 出错了要让调用方知道是哪一步失败的。
    """
    r = act_apply({'module': mod, 'cfg': cfg, 'live': True})
    if isinstance(r, dict):
        return bool(r.get('ok')), str(r.get('msg_cn') or ''), r.get('data') or {}
    return False, '未知错误', {}


def _wiz_apply_step(p):
    """提交某一步。每个 step 只碰自己的模块，互不牵连。

    关键点：整步是「全成功才算成功」的。半途失败时，之前已经写成功的模块
    保持生效（那是用户要的结果），只把失败的模块报出来 —— 不做整体回滚，
    因为回滚会把用户原本就已正确的配置也一起抹掉。
    """
    step = str((p or {}).get('step') or '').strip()
    b = p or {}
    done, errs, notes = [], [], []

    # ---------- 第 1 步：上网方式 ----------
    if step == 'wan':
        mode = str(b.get('mode') or '').strip()
        if mode not in ('pppoe', 'dhcp', 'static'):
            return fail('请先选择上网方式（PPPoE / DHCP / 静态）', 'BAD_MODE')
        sysc = _load_setting('system') or {}
        ppp = _load_setting('pppoe') or {}
        iface = str(b.get('iface') or ppp.get('iface') or sysc.get('wan_iface') or '').strip()
        if iface:
            if not re.match(r'^[A-Za-z0-9_.:@-]{1,15}$', iface):
                return fail('WAN 网卡名不合法', 'BAD_IFACE')
            # 让 WAN 口的角色在系统设置里落下来 —— 后面渲染 nft / ppp 都要用它
            sysc['wan_iface'] = iface
        ppp['mode'] = mode
        if iface:
            ppp['iface'] = iface
        if mode == 'pppoe':
            user = str(b.get('username') or '').strip()
            pwd = str(b.get('password') or '')
            if not user:
                return fail('PPPoE 拨号必须填宽带账号', 'NO_USER')
            if not pwd:
                return fail('PPPoE 拨号必须填宽带密码', 'NO_PASS')
            if re.search(r'[\s,=#"\'\\]', user):
                return fail('宽带账号不能包含空格或 , = # " \' \\ 这些字符', 'BAD_USER')
            ppp['username'] = user
            ppp['password'] = pwd
            ppp['isp'] = str(b.get('isp') or ppp.get('isp') or 'auto')
            ppp['persist'] = True
            try:
                ppp['mtu'] = max(576, min(int(b.get('mtu') or ppp.get('mtu') or 1492), 1500))
                ppp['mru'] = max(576, min(int(b.get('mru') or ppp.get('mru') or 1492), 1500))
            except Exception:
                ppp['mtu'] = 1492
                ppp['mru'] = 1492
        elif mode == 'static':
            addr = str(b.get('static_address') or '').strip()
            gw = str(b.get('static_gateway') or '').strip()
            if not addr:
                return fail('静态地址模式要填 IP 地址 / 掩码', 'NO_ADDR')
            if not gw:
                return fail('静态地址模式要填网关', 'NO_GW')
            for f, v in (('IP 地址 / 掩码', addr), ('网关', gw)):
                try:
                    if f.startswith('IP'):
                        ipaddress.ip_interface(v)
                    else:
                        ipaddress.ip_address(v)
                except Exception:
                    return fail('%s 格式不正确' % f, 'BAD_ADDR')
            ppp['static_address'] = addr
            ppp['static_gateway'] = gw
            ppp['static_dns'] = str(b.get('static_dns') or '').strip()
        _save_setting('system', sysc)
        _save_setting('pppoe', ppp)
        # 写盘后要让 ppp 的渲染产物真正落下来（/etc/ppp/*），但不拨号。
        okk, msg, _d = _wiz_apply('pppoe', ppp)
        (done if okk else errs).append('上网方式（%s）%s' % (
            next((x['n'] for x in WIZ_WAN_MODES if x['k'] == mode), mode),
            '已写入配置' if okk else '写入失败：' + msg))
        # IPv4 转发 + masquerade 是「内网能上网」的另一半，第 1 步就一并保证。
        # 它们只影响转发方向，不影响本机自己出网，所以放在这里最安全。
        kcfg = _kern_load()
        kcfg['fwd_v4'] = True
        _kern_save(kcfg)
        try:
            _kern_apply_sysctl(kcfg)
            notes.append('已同时打开 IPv4 转发（转发是路由器的本职工，不开内网出不去）')
        except Exception as e:
            errs.append('打开 IPv4 转发失败：%s' % e)
        v4 = _load_setting('nft_v4') or {}
        if not v4.get('masquerade', True):
            v4['masquerade'] = True
            _save_setting('nft_v4', v4)
        if v4.get('mss_clamp') is None:
            v4['mss_clamp'] = True
            _save_setting('nft_v4', v4)
            notes.append('已默认打开 MSS 钳制 —— 它专治「部分网站打不开、图片加载一半」')

    # ---------- 第 2 步：内网发地址 ----------
    elif step == 'lan':
        cfg = _load_setting('dnsmasq') or {}
        sysc = _load_setting('system') or {}
        iface = str(b.get('iface') or cfg.get('lan_iface') or sysc.get('lan_iface') or '').strip()
        if iface:
            if not re.match(r'^[A-Za-z0-9_.:@-]{1,15}$', iface):
                return fail('LAN 网卡名不合法', 'BAD_IFACE')
            cfg['lan_iface'] = iface
        start = str(b.get('pool_start') or '').strip()
        end = str(b.get('pool_end') or '').strip()
        mask = str(b.get('pool_netmask') or '255.255.255.0').strip()
        for f, v in (('地址池起始', start), ('地址池结束', end), ('子网掩码', mask)):
            try:
                ipaddress.ip_address(v)
            except Exception:
                return fail('%s 不是合法的 IPv4 地址' % f, 'BAD_POOL')
        lan_ip = _wiz_local_ipv4(iface) if iface else ''
        if lan_ip:
            if not _wiz_same_subnet(start, lan_ip, mask) or not _wiz_same_subnet(end, lan_ip, mask):
                return fail('地址池（%s–%s）和本机 LAN 地址（%s）不在同一个网段。'
                            '地址池前三段必须和本机一致。' % (start, end, lan_ip), 'POOL_SUBNET')
            if _wiz_ip_in_pool(lan_ip, start, end):
                return fail('地址池（%s–%s）把本机自己的地址（%s）也包括进去了，'
                            '这样会自己和自己抢 IP。请把池子的起点往后挪，'
                            '比如从 %s.100 开始。' % (start, end, lan_ip,
                                                   lan_ip.rsplit('.', 1)[0]), 'POOL_SELF')
        cfg['dhcp_enabled'] = True
        cfg['pool_start'] = start
        cfg['pool_end'] = end
        cfg['pool_netmask'] = mask
        try:
            cfg['lease_time'] = max(120, min(int(b.get('lease_time') or cfg.get('lease_time') or WIZ_LEASE_DEFAULT), 604800))
        except Exception:
            cfg['lease_time'] = WIZ_LEASE_DEFAULT
        # Option 3（网关）与 Option 6（DNS）填成本机 —— 不填的话客户端拿不到网关，
        # 表现是「能连上 Wi-Fi 但上不了网」，新手最难自查的一种故障。
        if lan_ip:
            cfg['option_gateway'] = lan_ip
        dns_opt = str(b.get('option_dns') or cfg.get('option_dns') or '').strip()
        if dns_opt:
            for s in dns_opt.split(','):
                s = s.strip()
                if s:
                    try:
                        ipaddress.ip_address(s)
                    except Exception:
                        return fail('下发给客户端的 DNS（%s）不是合法地址' % s, 'BAD_DNS_OPT')
            cfg['option_dns'] = dns_opt
        elif lan_ip:
            cfg['option_dns'] = lan_ip
        _save_setting('dnsmasq', cfg)
        _save_setting('system', sysc)
        okk, msg, _d = _wiz_apply('dnsmasq', cfg)
        (done if okk else errs).append('DHCP 服务（%s–%s）%s' % (
            start, end, '已生效' if okk else '失败：' + msg))

    # ---------- 第 3 步：DNS ----------
    elif step == 'dns':
        cfg = _load_setting('dnsmasq') or {}
        mode = str(b.get('mode') or 'custom').strip()
        if mode not in ('isp', 'custom', 'both'):
            return fail('DNS 来源只能是「仅运营商」「仅自定义」「两者合并」之一', 'BAD_DNS_MODE')
        custom = str(b.get('custom') or '').strip()
        if mode in ('custom', 'both'):
            if not custom:
                return fail('选了自定义 DNS 就必须填至少一个地址', 'NO_DNS')
            for s in custom.split(','):
                s = s.strip()
                if not s:
                    continue
                try:
                    ipaddress.ip_address(s)
                except Exception:
                    return fail('DNS 地址「%s」不是合法的 IP（多个地址之间用英文逗号分隔）' % s, 'BAD_DNS')
        cfg['dns_mode'] = mode
        if custom:
            cfg['dns_custom'] = custom
        try:
            cfg['dns_cache_size'] = max(0, min(int(b.get('cache') or cfg.get('dns_cache_size') or WIZ_DNS_CACHE_DEFAULT), 20000))
        except Exception:
            cfg['dns_cache_size'] = WIZ_DNS_CACHE_DEFAULT
        _save_setting('dnsmasq', cfg)
        okk, msg, _d = _wiz_apply('dnsmasq', cfg)
        (done if okk else errs).append('DNS 来源（%s）%s' % (
            {'isp': '仅运营商下发', 'custom': '仅自定义', 'both': '两者合并'}[mode],
            '已生效' if okk else '失败：' + msg))

    # ---------- 第 4 步：IPv6 ----------
    elif step == 'v6':
        want = b.get('enable')
        want = True if want is None else bool(want)
        kcfg = _kern_load()
        radvd_cfg = _load_setting('radvd') or {}
        if not want:
            # 关掉时只停 radvd 与转发，不删用户已填的前缀 —— 「先不开」不等于
            # 「把以前配好的东西抹掉」，用户随时可以再打开。
            kcfg['fwd_v6'] = False
            _kern_save(kcfg)
            try:
                _kern_apply_sysctl(kcfg)
            except Exception as e:
                errs.append('关闭 IPv6 转发失败：%s' % e)
            sh(['systemctl', 'disable', '--now', 'radvd'], timeout=30)
            notes.append('已关闭 radvd 通告与 IPv6 转发。已填写的 IPv6 前缀保留未删除，'
                         '下次打开还能用。')
            done.append('IPv6 已关闭（不影响 IPv4 上网）')
        else:
            if not _wiz_service_exists('radvd'):
                errs.append('radvd 没装，IPv6 通告发不出去。请先到「系统 → 依赖自检与安装」装它，'
                            '或者先跳过这一步。')
            lan_iface = str(b.get('lan_iface') or radvd_cfg.get('iface')
                            or (_load_setting('dnsmasq') or {}).get('lan_iface') or '').strip()
            if lan_iface and not re.match(r'^[A-Za-z0-9_.:@-]{1,15}$', lan_iface):
                return fail('LAN 网卡名不合法', 'BAD_IFACE')
            prefix = str(b.get('prefix') or radvd_cfg.get('prefix') or '').strip()
            if prefix:
                # 只接受 /64 的规范写法；写得随意（少了位数）时自动补上
                try:
                    net = ipaddress.ip_network(prefix, strict=False)
                    if net.version != 6:
                        raise ValueError('not v6')
                    if net.prefixlen != 64:
                        # 内网 RA 通告必须 /64 —— 这不是我们的偏好，是 RFC 4862 的硬要求，
                        # 其它长度客户端直接忽略，用户会以为「开了没用」。
                        notes.append('前缀位数已改为 /64：内网 RA 通告只能用 /64，'
                                     '其它长度手机电脑会直接忽略。')
                        net = ipaddress.ip_network('%s/64' % net.network_address, strict=False)
                    prefix = str(net)
                except Exception:
                    return fail('IPv6 前缀格式不对。形如 2408:8207:1234:5678::/64。', 'BAD_PREFIX')
            if lan_iface:
                radvd_cfg['iface'] = lan_iface
            if prefix:
                radvd_cfg['prefix'] = prefix
            # RDNSS：把 IPv6 的 DNS 一起发给内网设备，否则它们有 v6 地址却解析不了域名
            rdnss = str(b.get('rdnss') or radvd_cfg.get('rdnss') or '').strip()
            if rdnss:
                for s in rdnss.split(','):
                    s = s.strip()
                    if s:
                        try:
                            ipaddress.ip_address(s)
                        except Exception:
                            return fail('RDNSS（%s）不是合法的 IPv6 地址' % s, 'BAD_RDNSS')
                radvd_cfg['rdnss'] = rdnss
            radvd_cfg['managed'] = False
            radvd_cfg['other_config'] = False
            _save_setting('radvd', radvd_cfg)
            kcfg['fwd_v6'] = True
            _kern_save(kcfg)
            try:
                applied, _bbr, kerr = _kern_apply_sysctl(kcfg)
                errs += kerr
            except Exception as e:
                errs.append('打开 IPv6 转发失败：%s' % e)
            okk, msg, _d = _wiz_apply('radvd', radvd_cfg)
            (done if okk else errs).append('IPv6 路由通告 %s' % ('已生效' if okk else '失败：' + msg))
            if not prefix and not _wiz_has_ipv6_global(str((_load_setting('system') or {}).get('wan_iface') or '')):
                notes.append('当前 WAN 口还没拿到运营商下发的 IPv6 前缀（PD），'
                             '所以内网暂时拿不到公网 v6 地址。等宽带那边下发后，'
                             '可以到「IPv6 / RA」页用「自动跟随 PD 前缀」。')

    else:
        return fail('未知的向导步骤：%s（只能是 wan / lan / dns / v6）' % step, 'BAD_STEP')

    if errs and not done:
        return fail('；'.join(errs), 'STEP_FAILED', {'step': step})
    msg = '；'.join(done + notes) if (done or notes) else '这一步无需改动'
    if errs:
        msg += '　【有未完成项】' + '；'.join(errs)
    return ok({'step': step, 'done': done, 'errors': errs, 'notes': notes,
               'build_mode': in_build_mode()}, msg)


def _wiz_dial(p):
    """真正拨号（可选动作，与「保存配置」分开）。

    刻意做成独立操作：写凭据不会断网，执行 connect 才可能断。
    把两者绑在一个按钮里，新手会在毫不知情的情况下把自家网掐掉。
    """
    p = p or {}
    if not p.get('confirm'):
        return fail('拨号会短暂中断现有网络，需要显式确认', 'NEED_CONFIRM')
    op = str(p.get('action') or 'connect').strip()
    if op not in ('connect', 'disconnect'):
        return fail('拨号动作只能是 connect 或 disconnect', 'BAD_ACTION')
    return act_ppp_control({'op': op, 'confirm': True})


def act_wizard(p):
    """新手向导：probe（体检）/ apply_step（提交某一步）/ dial（拨号）"""
    p = p or {}
    op = str(p.get('op') or 'probe').strip()
    if op == 'probe':
        return _wiz_probe()
    if op == 'apply_step':
        return _wiz_apply_step(p)
    if op == 'dial':
        return _wiz_dial(p)
    return fail('未知的新手向导操作：%s' % op)


ACTIONS = {
    'read:sysinfo': read_sysinfo,
    'read:metrics': read_metrics,
    'read:ifaces': read_ifaces,
    'read:routes': read_routes,
    'read:services': read_services,
    'read:nft': read_nft,
    'read:leases': read_leases,
    'read:upnpmap': read_upnpmap,
    'read:ipv6': read_ipv6,
    'read:iface_method': read_iface_method,
    'read:ppp_log': read_ppp_log,
    'read:wan_log': read_wan_log,
    'read:ddns': read_ddns,
    'ddns': act_ddns,
    'pubip': act_pubip,
    'cleanup': act_cleanup,
    'kern': act_kern,
    'wizard': act_wizard,
    'read:acl': read_acl,
    'acl': act_acl,
    'vpn': act_vpn,
    'quota': act_quota,
    'read:quotad': read_quotad,
    'read:share': read_share,
    'storage': act_storage,
    'share': act_share,
    'read:docker': read_docker,
    'docker': act_docker,
    'dcfg': act_dcfg,
    'print': act_print,
    'opensoho': act_opensoho,
    'ca': act_ca,
    'read:ulog': read_ulog,
    'read:ulog_flow': read_ulog_flow,
    'read:ulog_conf': read_ulog_conf,
    'ulog': act_ulog,
    'read:fw_log': read_fw_log,
    'lease_make_static': act_lease_make_static,
    'lease_release': act_lease_release,
    'web_port_set': act_web_port_set,
    'read:ntp': read_ntp,
    'read:users': read_users,
    'read:logs': read_logs,
    'read:journal': read_journal,
    'apply': act_apply,
    'render_text': read_render_text,
    'service': act_service,
    'power': act_power,
    'user': act_user,
    'diag': act_diag,
    'pkg': act_pkg,
    'snapshot': act_snapshot,
    'snapshot_list': act_snapshot_list,
    'snapshot_delete': act_snapshot_delete,
    'snapshot_prune': act_snapshot_prune,
    'snapshot_note': act_snapshot_note,
    'snapshot_protect': act_snapshot_protect,
    'snapshot_pack': act_snapshot_pack,
    'backup': act_backup,
    'read:backupd': read_backupd,
    'alert': act_alert,
    'read:alertd': read_alertd,
    'nat_check': act_nat_check,
    'read:nat_last': lambda _p: ok(_load_setting('nat_last') or {},
                                   '上次 NAT 检测结果'),
    'accel': act_accel,
    'pppoe_multi': act_pppoe_multi,
    'qos': act_qos,
    'dpi': act_dpi,
    'vlan': act_vlan,
    'wol': act_wol,
    'depcheck': act_depcheck,
    'fs': act_fs,
    'webshell': act_webshell,
    'auto_snapshot': act_auto_snapshot,
    'snapshotd_sync': act_snapshotd_sync,
    'logd_sync': act_logd_sync,
    'read:logd': read_logd,
    'net_totals': net_totals,
    'net_totals_update': lambda p: ok(_net_totals_update(),
                                      '累计流量已更新'),
    'read:theme': read_theme,
    'theme': act_theme,
    'rescue': act_rescue,
    'rollback': act_rollback,
    'migrate_networkd': act_migrate_networkd,
    'ppp_control': act_ppp_control,
    'remove_legacy': act_remove_legacy,
    'build_mode': act_build_mode,
}


def run_action(action, payload=None):
    """执行一次白名单动作，返回结果 dict（不打印、不 sys.exit）。

    抽成独立函数是为了让常驻守护 drouter-helpd 复用同一套分发、构建保护熔断
    与异常兜底逻辑。如果守护自己再写一份，两条路径迟早走偏：
    命令行版拦住的危险动作，守护版可能就漏了。
    """
    fn = ACTIONS.get(action)
    if not fn:
        return fail('未授权的操作：%s' % action)
    # 构建保护模式熔断
    guard = check_build_guard(action, payload)
    if guard is not None:
        return guard
    try:
        return fn(payload or {})
    except Exception as e:
        import traceback
        log('error', 'helper', 'EXCEPTION', '执行 %s 时发生异常：%s' % (action, e),
            traceback.format_exc()[-1500:])
        return fail('执行异常：%s' % e, 'EXCEPTION')


def main():
    if len(sys.argv) < 2:
        print(json.dumps(fail('缺少 action 参数'), ensure_ascii=False))
        return 2
    action = sys.argv[1]
    payload = {}
    if len(sys.argv) > 2 and sys.argv[2].strip():
        try:
            payload = json.loads(sys.argv[2])
        except Exception:
            print(json.dumps(fail('参数不是合法 JSON'), ensure_ascii=False))
            return 2
    res = run_action(action, payload)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())

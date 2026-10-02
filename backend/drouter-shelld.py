#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
drouter-shelld —— Web 终端的 PTY 会话守护（#4 重做版）

为什么必须有这个守护：
  Web 后端每次调 helper 都是「新起一个 sudo python3 进程，跑完就退出」，
  PTY 会话是内核里的文件描述符，进程一退出 fd 就没了。所以终端必须由一个
  常驻进程持有，helper 只当转发客户端。

之前的实现是「一条命令 + subprocess.run + 回传输出」，于是：
  * 输入不是实时的（要敲完回车再整条发）；
  * 没有回显（靠前端自己画 "$ cmd"，看着像有、其实不是终端给的）；
  * 交互式程序（top / vi / python / 密码提示）根本没法用。
改成真 PTY 后，回显、Ctrl+C、方向键历史、全屏程序都由内核终端行规程负责，
前端只负责把字节流显示出来。

安全边界：
  * Unix socket 位于 /run/drouter/shell.sock，root 拥有、权限 0600，
    只有 root（即被 sudoers 放行的 helper）能连；低权 drouter 用户连不上。
  * 会话空闲超过 IDLE_TIMEOUT 自动回收，避免忘记断开留下 root shell。
  * 每次连接 / 断开都写结构化日志，便于事后审计。
"""

import errno
import fcntl
import itertools
import json
import os
import pty
import select
import signal
import socketserver
import struct
import subprocess
import sys
import termios
import threading
import time
from datetime import datetime

_SBIN_DIRS = ('/usr/local/sbin', '/usr/sbin', '/sbin')


def _ensure_sbin_path():
    cur = os.environ.get('PATH') or ''
    parts = [p for p in cur.split(os.pathsep) if p]
    for d in _SBIN_DIRS:
        if d not in parts and os.path.isdir(d):
            parts.insert(0, d)
    os.environ['PATH'] = os.pathsep.join(parts)


_ensure_sbin_path()

SOCK_PATH = '/run/drouter/shell.sock'
LOG_DIR = '/var/log/drouter'
IDLE_TIMEOUT = 30 * 60        # 30 分钟无操作自动回收
# 单个会话的输出缓冲上限。
# 过去完全无上限：在终端里跑 `yes`、`journalctl -f`、或者任何持续打印的程序，
# 然后浏览器不读（或标签卡住），读线程会一直往 bytearray 里堆 ——
# 一个会话就能把 4 GB 内存吃干净，OOM 之后连面板一起挂。
MAX_BUF = 8 * 1024 * 1024
# 会话名额用信号量来**占**。过去的写法是「拿到锁看一眼没满 → 释放锁 → 慢慢创建
# PTY 和子进程 → 再拿锁登记」，中间的窗口里并发请求会各自通过检查，
# 结果实际会话数远超 MAX_SESSIONS（PTY、进程、fd 一起失控）。
_SESS_SEM = threading.BoundedSemaphore(MAX_SESSIONS)
READ_CHUNK = 65536
# 单次 read 最多回传多少字节：多了前端渲染会卡，少了要跑很多轮
MAX_READ = 262144
# 长轮询：前端带 wait 时，没有数据最多在这里挂起多久 / 每次探一下的间隔
READ_HOLD_MAX = 5.0
READ_HOLD_STEP = 0.02

# 必须是 RLock 而不是 Lock：connect 需要「检查是否满员 → 满了就回收 → 再检查」
# 这个序列，天然会在持锁时调用到内部也要拿锁的 _reap_idle()。
# 用普通 Lock 会把自己锁死（同线程二次 acquire 永久阻塞）—— 表现为
# 「连到第 9 个会话时永久卡住」，而不是干净地报「已达上限」。
_lock = threading.RLock()
_SESSIONS = {}
# 会话编号的自增序列。不能只用时间戳：同一秒内连两次会生成相同 sid，
# 后一个会话把前一个顶掉，表现为「刚开的终端突然不动了」。
_seq = itertools.count(1)


def ensure_ptmx():
    """确保 /dev/ptmx 可用；缺了就补回符号链接。

    为什么需要这个：
      pty.openpty() 走的是 /dev/ptmx。标准 Debian 上 /dev/ptmx 是指向
      /dev/pts/ptmx 的符号链接，由 devtmpfs + udev 在开机时建立。
      但在容器/虚拟化环境（LXC、某些 PVE 配置）里，/dev 可能被重新挂载，
      这个符号链接会**丢失**。此时 /dev/pts/ptmx 仍然是好的，只是没有
      那个入口 —— 表现就是 openpty() 抛 "out of pty devices"，
      而 sysctl kernel.pty.nr 显示 0（明明一个都没用），特别迷惑。

      修法就是补一条符号链接。这里做成幂等 + 自愈，避免用户要 SSH 进去
      手工处理（路由器出问题时恰恰进不去）。

    返回 (ok, detail)。
    """
    ptmx = '/dev/ptmx'
    if os.path.exists(ptmx):
        return True, 'ok'
    try:
        # /dev/pts/ptmx 存在就优先链它（devpts 实例里的那一份）
        if os.path.exists('/dev/pts/ptmx'):
            os.symlink('pts/ptmx', ptmx)
            return True, 'symlink created'
        # 退路：直接 mknod 一个 5,2（老式 devfs 行为，现代内核仍认）
        os.mknod(ptmx, 0o666 | 0o020000, os.makedev(5, 2))
        return True, 'device node created'
    except OSError as e:
        return False, str(e)


def _pty_diagnose():
    """PTY 建立失败时，给出一句人能看懂的原因，而不是原始 errno 天书。"""
    diag = []
    if not os.path.exists('/dev/ptmx'):
        diag.append('/dev/ptmx 缺失')
    try:
        mount = open('/proc/mounts', encoding='utf-8').read()
        if ' /dev/pts ' not in mount:
            diag.append('/dev/pts 未挂载 devpts')
    except Exception:
        pass
    try:
        nr = open('/proc/sys/kernel/pty/nr').read().strip()
        mx = open('/proc/sys/kernel/pty/max').read().strip()
        if nr == mx:
            diag.append('PTY 配额已满（%s/%s）' % (nr, mx))
    except Exception:
        pass
    live = len(_SESSIONS)
    if live >= MAX_SESSIONS:
        diag.append('本服务会话已达上限（%d）' % live)
    return '；'.join(diag) if diag else '未知原因'



def log(level, code, msg_cn, detail=None):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        rec = {'ts': datetime.now().isoformat(timespec='seconds'),
               'level': level, 'module': 'shelld', 'code': code,
               'msg_cn': msg_cn, 'detail': detail or ''}
        line = json.dumps(rec, ensure_ascii=False)
        for fn in ('all.jsonl', 'shelld.jsonl'):
            with open(os.path.join(LOG_DIR, fn), 'a', encoding='utf-8') as f:
                f.write(line + '\n')
    except Exception:
        pass


def _open_pty():
    """申请一对 PTY。首次失败时先尝试自愈 /dev/ptmx，再重试一次。

    pty.openpty() 抛的 OSError 文案就是一句 "out of pty devices"，
    对用户毫无指导意义 —— 明明一个 PTY 都没用，却说"用完了"。
    根因八成是 /dev/ptmx 这个入口没了。先补入口再试，还不行就把
    真实原因（见 _pty_diagnose）包进异常里往上抛。
    """
    try:
        return pty.openpty()
    except OSError as first:
        ok, detail = ensure_ptmx()
        if ok:
            try:
                m, s = pty.openpty()
                log('warn', 'PTY_HEAL', '/dev/ptmx 缺失，已自动补回后恢复',
                    {'detail': detail})
                return m, s
            except OSError:
                pass
        raise OSError('%s（诊断：%s）' % (first, _pty_diagnose()))


class Session(object):
    """一个 PTY 会话：master fd + 子进程 + 输出缓冲 + 读线程。"""

    def __init__(self, sid, user, shell, cols, rows, cwd):
        self.sid = sid
        self.user = user
        self.created = time.time()
        self._last_mono = time.monotonic()
        self.alive = True
        self.buf = bytearray()
        self.buf_lock = threading.Lock()
        self.dropped = 0      # 因缓冲上限被丢弃的字节数（宁丢oldest也不 OOM）

        # 先占名额再建会话：占不到就是真的满了
        if not _SESS_SEM.acquire(blocking=False):
            raise RuntimeError('终端会话已达上限（%d），请先断开不用的会话'
                               % MAX_SESSIONS)
        self._reserved = True

        self.master, slave = _open_pty()
        try:
            _set_winsize(self.master, rows, cols)

            env = dict(os.environ)
            env['TERM'] = 'xterm-256color'
            env['COLUMNS'] = str(cols)
            env['LINES'] = str(rows)
            # 关掉 bash 的“多行命令用临时文件编辑”提示，避免 readline 在
            # 非标准终端下把界面搞乱
            env['BASH_SILENCE_DEPRECATION_WARNING'] = '1'

            # start_new_session=True 让子进程成为会话首进程；
            # 打开 slave 端时内核会把这个 pty 设为它的控制终端，
            # 这样 Ctrl+C 才会真的向前台进程组发 SIGINT。
            self.proc = subprocess.Popen(
                [shell, '-i'], stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True, cwd=cwd or '/root', env=env,
                preexec_fn=None, close_fds=True)
        except BaseException:
            # 关键：Popen 失败（cwd 不存在、进程数超限、内存不足……）时，
            # master/slave **两个** fd 都得关。早先这里直接抛出去，
            # 每次失败泄漏两个 PTY，反复重试会把 /dev/pts 配额耗尽，
            # 症状是「后来连 ssh 都开不了终端」。
            try:
                os.close(slave)
            except Exception:
                pass
            try:
                os.close(self.master)
            except Exception:
                pass
            self._reserved = False
            _SESS_SEM.release()
            raise
        os.close(slave)

        self.th = threading.Thread(target=self._reader, daemon=True)
        self.th.start()

    @property
    def last(self):
        """最后一次活动的**单调**时刻（秒）。

        用单调时钟是因为 NTP 校时 / 机器休眠会把系统时间来回拨：
        回拨时所有会话看起来「刚刚用过」永远不回收，前跳时又会一次性全过期。
        """
        return self._last_mono

    @last.setter
    def last(self, _v):
        # 老的调用点写的是 `s.last = time.time()`，这里统一换成单调时钟
        self._last_mono = time.monotonic()

    def _append(self, data):
        """把 PTY 输出放进缓冲，超过 MAX_BUF 就丢最旧的那一截。"""
        with self.buf_lock:
            over = len(self.buf) + len(data) - MAX_BUF
            if over > 0:
                del self.buf[:over]
                self.dropped += over
            self.buf.extend(data)

    def _reader(self):
        """把 PTY 输出搬进缓冲区。子进程退出后 EIO 会跳出循环。"""
        while True:
            try:
                r, _, _ = select.select([self.master], [], [], 0.5)
                if not r:
                    if self.proc.poll() is not None:
                        break
                    continue
                data = os.read(self.master, READ_CHUNK)
            except OSError as e:
                # EIO = 子进程退出后 slave 全关；这是正常结束，不是错误
                if e.errno in (errno.EIO,):
                    break
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    continue
                break
            except Exception:
                break
            if not data:
                break
            self._append(data)
        self.alive = False

    def drain(self, limit=MAX_READ):
        with self.buf_lock:
            if not self.buf:
                return b''
            raw = bytes(self.buf[:limit])
            # 结尾可能正好切在半个汉字上（UTF-8 一个汉字 3 字节，分帧时会被拆开）。
            # 直接解码会得到 U+FFFD（黑菱形问号），中文输出里出现一次就很扎眼。
            # 所以把结尾不完整的那段留在缓冲区里，等下一帧凑齐再发。
            safe, _tail = _utf8_tail(raw)
            if not safe:
                return b''
            del self.buf[:len(safe)]
        return safe

    def write(self, data):
        if not self.alive and self.proc.poll() is not None:
            return False
        try:
            os.write(self.master, data)
            return True
        except OSError:
            return False

    def resize(self, cols, rows):
        if self.master is None:
            return False
        _set_winsize(self.master, rows, cols)
        try:
            os.kill(self.proc.pid, signal.SIGWINCH)
        except Exception:
            pass
        return True

    def close(self):
        try:
            if self.proc.poll() is None:
                # 先给子进程组发 SIGHUP，让它正常退出（bash 会写 history）
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGHUP)
                except Exception:
                    self.proc.terminate()
                for _ in range(30):
                    if self.proc.poll() is not None:
                        break
                    time.sleep(0.1)
                if self.proc.poll() is None:
                    self.proc.kill()
        except Exception:
            pass
        try:
            os.close(self.master)
        except Exception:
            pass
        self.alive = False
        # 读线程由 PTY 关闭触发退出（EIO）；join 一下是为了把 fd 复用窗口收窄，
        # 顺手把会话名额还回去（否则被回收的会话会永久占着名额）。
        try:
            if getattr(self, 'th', None) is not None:
                self.th.join(0.5)
        except Exception:
            pass
        if getattr(self, '_reserved', False):
            self._reserved = False
            try:
                _SESS_SEM.release()
            except Exception:
                pass


def _utf8_tail(data):
    """把 data 拆成（可安全解码的部分, 结尾处不完整的 UTF-8 序列）。

    从末尾往前找起始字节，判断它声明的长度够不够；不够就说明这一帧被切开了。
    """
    n = len(data)
    for k in range(1, min(4, n) + 1):
        i = n - k
        b = data[i]
        if b < 0x80:
            break                      # 结尾是 ASCII，整段可解
        if 0xC0 <= b <= 0xDF:
            need = 2
        elif 0xE0 <= b <= 0xEF:
            need = 3
        elif 0xF0 <= b <= 0xF7:
            need = 4
        else:
            continue                   # 续字节，继续往前找起始字节
        if k < need:
            return data[:i], data[i:]  # 起始字节在，后面不完整
        return data, b''               # 序列完整，整段可解
    return data, b''


def _set_winsize(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack('HHHH', rows or 24, cols or 80, 0, 0))
    except Exception:
        pass


def _reap_idle():
    """回收超时会话；顺带清掉已退出的僵尸子进程。返回回收的会话数。

    锁内只做「登记摘除」，真正的 close() 放到锁外做 ——
    close() 要等子进程退出（SIGHUP 后最多轮询 3 秒），持锁做会把
    所有并发请求堵死：表现为「9 个会话时新连接超时、老终端也卡住」。
    """
    # 闲置/僵尸会话的回收判据必须用单调时钟（理由见 Session.last 的注释）
    now = time.monotonic()
    victims = []
    with _lock:
        for sid, s in list(_SESSIONS.items()):
            if now - s.last > IDLE_TIMEOUT:
                log('info', 'SHELL_IDLE', '终端会话闲置超时，已自动回收：%s' % sid)
                del _SESSIONS[sid]
                victims.append(s)
            elif s.proc.poll() is not None and not s.alive:
                # 用户敲了 exit：保留一小段时间让前端把最后的输出读完
                if now - s.last > 60:
                    del _SESSIONS[sid]
                    victims.append(s)
    for s in victims:
        s.close()
    return len(victims)


def _pick_shell(user):
    """挑一个存在的登录 shell，缺 bash 时退回 sh。"""
    for s in ('/bin/bash', '/bin/sh'):
        if os.path.isfile(s):
            return s
    return '/bin/sh'


def handle(req):
    op = str(req.get('op') or '')
    sid = req.get('sid') or ''

    if op == 'connect':
        # 满员时先回收一轮闲置会话再决定是否拒绝。
        # 注意这里不能把 _reap_idle() 放在 with _lock 里面：它要等子进程
        # 退出（最长 3 秒），持锁调用会把所有并发 read/write 一起堵死。
        with _lock:
            full = len(_SESSIONS) >= MAX_SESSIONS
        if full:
            _reap_idle()
        with _lock:
            if len(_SESSIONS) >= MAX_SESSIONS:
                return {'ok': False, 'code': 'BUSY',
                        'msg_cn': '终端会话已达上限（%d），请先断开不用的会话' % MAX_SESSIONS}
        user = str(req.get('user') or 'root')
        shell = _pick_shell(user)
        cols = int(req.get('cols') or 100)
        rows = int(req.get('rows') or 30)
        cols = max(20, min(cols, 400))
        rows = max(5, min(rows, 200))
        sid = 'sh%d-%d' % (int(time.time()), next(_seq))
        try:
            s = Session(sid, user, shell, cols, rows, req.get('cwd') or '/root')
        except RuntimeError as e:
            # Session 构造里占不到名额会抛这个 —— 真正的并发上限在这里生效
            log('warn', 'SHELL_BUSY', '终端会话已满：%s' % e, {'user': user})
            return {'ok': False, 'code': 'BUSY', 'msg_cn': str(e)}
        except Exception as e:
            # 把日志也写一份 —— 用户看到界面报错时的同一个信息必须留在
            # 服务端日志里，否则事后排查只能靠猜。
            log('error', 'PTY_FAIL', '创建终端失败：%s' % e, {'user': user})
            return {'ok': False, 'code': 'PTY_FAIL',
                    'msg_cn': '创建终端失败：%s' % e}
        with _lock:
            _SESSIONS[sid] = s
        log('warn', 'SHELL_OPEN', 'Web 终端会话已建立（用户 %s）' % user,
            {'sid': sid, 'shell': shell, 'cols': cols, 'rows': rows})
        return {'ok': True, 'msg_cn': '终端已连接',
                'data': {'sid': sid, 'shell': shell, 'cols': cols, 'rows': rows}}

    if op == 'read':
        with _lock:
            s = _SESSIONS.get(sid)
        if not s:
            return {'ok': False, 'code': 'NOSESS', 'msg_cn': '会话不存在或已过期'}
        s.last = time.time()
        data = s.drain()
        # 长轮询：前端可以带 wait（秒），没数据时在这里等到有数据或超时再返回。
        # 以前前端是固定 60ms 空转轮询，一半的请求都空手而归，而且输出最快也要
        # 等下一个轮询点才看得到；长轮询让「命令一有输出」立刻就有响应。
        wait = 0.0
        try:
            wait = float(req.get('wait') or 0)
        except Exception:
            wait = 0.0
        wait = max(0.0, min(wait, READ_HOLD_MAX))
        if not data and wait > 0:
            deadline = time.time() + wait
            while time.time() < deadline:
                time.sleep(READ_HOLD_STEP)
                data = s.drain()
                if data:
                    break
                if s.proc.poll() is not None and not s.alive:
                    break
        exited = s.proc.poll()
        # latin-1 逐字节还原，保证 ANSI 控制序列不被 UTF-8 解码破坏；
        # 真正显示成文字由前端按 UTF-8 再解码（多字节字符会跨帧，前端用拼接处理）
        return {'ok': True, 'data': {
            'data': data.decode('utf-8', errors='replace'),
            'alive': s.alive or exited is None,
            'exit': exited}}

    if op == 'write':
        with _lock:
            s = _SESSIONS.get(sid)
        if not s:
            return {'ok': False, 'code': 'NOSESS', 'msg_cn': '会话不存在或已过期'}
        s.last = time.time()
        raw = req.get('data') or ''
        if isinstance(raw, str):
            raw = raw.encode('utf-8', errors='replace')
        okw = s.write(raw)
        return {'ok': okw, 'msg_cn': '' if okw else '写入失败（终端可能已退出）'}

    if op == 'resize':
        with _lock:
            s = _SESSIONS.get(sid)
        if not s:
            return {'ok': False, 'code': 'NOSESS', 'msg_cn': '会话不存在或已过期'}
        s.last = time.time()
        cols = max(20, min(int(req.get('cols') or 100), 400))
        rows = max(5, min(int(req.get('rows') or 30), 200))
        s.resize(cols, rows)
        return {'ok': True}

    if op == 'disconnect':
        with _lock:
            s = _SESSIONS.pop(sid, None)
        if s:
            s.close()
            log('info', 'SHELL_CLOSE', 'Web 终端会话已关闭', {'sid': sid})
        return {'ok': True, 'msg_cn': '已断开'}

    if op == 'sessions':
        with _lock:
            return {'ok': True, 'data': [
                {'sid': k, 'age': int(time.time() - v.created),
                 'idle': int(time.monotonic() - v.last),   # last 是单调秒
                 'alive': v.alive, 'dropped': v.dropped}
                for k, v in _SESSIONS.items()]}

    return {'ok': False, 'code': 'BAD_OP', 'msg_cn': '未知操作：%s' % op}


class Handler(socketserver.StreamRequestHandler):
    timeout = 30

    def handle(self):
        try:
            line = self.rfile.readline()
            if not line:
                return
            req = json.loads(line.decode('utf-8'))
        except Exception:
            return
        try:
            resp = handle(req)
        except Exception as e:
            resp = {'ok': False, 'code': 'ERR', 'msg_cn': '终端守护内部错误：%s' % e}
        try:
            self.wfile.write((json.dumps(resp, ensure_ascii=False) + '\n')
                             .encode('utf-8'))
            self.wfile.flush()
        except Exception:
            pass


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)
    d = os.path.dirname(SOCK_PATH)
    os.makedirs(d, exist_ok=True)
    # 目录必须 0755：/run/drouter 是 shelld 与 helpd 共用的，
    # drouter-web（以 drouter 用户运行）要能进这个目录去连 helper.sock。
    # 之前靠 systemd 的 RuntimeDirectory 建目录，结果 helpd 一重启 systemd 就把
    # 整个目录重建，把这里的 shell.sock 连带删掉 —— shelld 显示 active，
    # socket 却没了，终端直接报「守护未运行」。所以改成守护自己建。
    try:
        os.chmod(d, 0o755)
    except Exception:
        pass
    os.umask(0o177)          # 保证 socket 创建出来就是 0600

    # 启动即自愈 /dev/ptmx。这个守护的唯一职责就是开 PTY，如果入口都没有，
    # 后面每次连接都会失败 —— 与其让用户点一次踩一次坑，不如开机就修好。
    ok, detail = ensure_ptmx()
    if not ok:
        log('error', 'PTY_UNAVAILABLE',
            'PTY 入口不可用，Web 终端将无法使用',
            {'detail': detail, 'diag': _pty_diagnose()})
    else:
        if detail != 'ok':
            log('warn', 'PTY_HEAL', '/dev/ptmx 缺失，启动时已自动补回', {'detail': detail})

    srv = Server(SOCK_PATH, Handler)
    os.chmod(SOCK_PATH, 0o600)
    # 真正的周期回收线程。
    # 过去 _reap_idle() 只在「新建会话且发现已满」时才被调用 —— 也就是说
    # 只要没人连到第 9 个，那些突然断线（浏览器崩溃、手机切走）留下的
    # root shell 就会一直挂着，连同 master fd、Session 对象和若干 bytearray。
    def _reaper_loop():
        while True:
            time.sleep(60)
            try:
                n = _reap_idle()
                if n:
                    log('info', 'SHELL_REAP', '后台回收了 %d 个闲置终端会话' % n)
            except Exception:
                pass

    threading.Thread(target=_reaper_loop, daemon=True).start()

    log('info', 'SHELLD_UP', 'Web 终端守护已启动：%s' % SOCK_PATH)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        with _lock:
            for s in _SESSIONS.values():
                s.close()
            _SESSIONS.clear()
        try:
            os.unlink(SOCK_PATH)
        except OSError:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
drouter-helpd —— 特权动作的常驻执行守护（#7 性能优化）

为什么要有它：
  原来每来一个 Web 请求，drouter-web 都要
      sudo -n /usr/bin/python3 /opt/drouter/backend/drouter-helper.py <action> <json>
  也就是「起一个 Python 解释器 + 过一遍 sudo + 重新 import 一万行代码」，
  实测固定开销约 236ms，而动作本身的计算往往只有几十毫秒。
  一个面板页面点一下要调 1~3 个接口，光启动就花掉半秒多。

  改成常驻后：helper 的代码只 import 一次，请求通过 Unix socket 进来，
  直接调 run_action()，省掉解释器启动与 sudo 开销。

安全边界（与原来的 sudo 白名单等价，不放宽）：
  * 原来：drouter 用户被 sudoers 允许无密码执行
           /usr/bin/python3 /opt/drouter/backend/drouter-helper.py *
  * 现在：socket 权限 0660、属主 root:drouter，只有 drouter 用户/组能连上；
          其它本地用户连都连不上。能达到的权限完全一样。
  * 分发的仍然是同一张 ACTIONS 白名单，构建保护模式熔断也照样生效
    （复用 helper.run_action，不另写一套）。
  * 守护以 root 运行，但它不监听任何 TCP 端口，只暴露 Unix socket。

降级：
  drouter-web 优先连 socket；连不上（守护没起/崩了）就自动回退到原来的
  subprocess 方式。所以这个守护挂掉不会让面板不可用，只是变慢。
"""

import json
import os
import socket
import socketserver
import sys
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

# 让 import 能找到同目录的 drouter-helper
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SOCK_PATH = '/run/drouter/helper.sock'
SOCK_DIR = '/run/drouter'
SOCK_GROUP = 'drouter'
LOG_DIR = '/var/log/drouter'
# 单行请求的最大长度。配置类 payload 可能不小（防火墙规则、compose 文件），
# 给到 8MB；再大就拒绝而不是无限扩张内存。
MAX_LINE = 8 * 1024 * 1024

_started = time.time()
_stats_lock = threading.Lock()
STATS = {'requests': 0, 'errors': 0, 'by_action': {}}
# 同时执行的特权动作上限。这些动作里有格式化磁盘、apt 安装、重启服务这类
# 重活：客户端并发点几下就会同时跑好几个，2 vCPU 机器上直接把负载打满，
# 还可能互相抢系统资源（apt 锁、nft 表）。
MAX_ACTIONS = 6
_action_sem = threading.BoundedSemaphore(MAX_ACTIONS)


def log(level, code, msg_cn, detail=None):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        rec = {'ts': datetime.now().isoformat(timespec='seconds'),
               'level': level, 'module': 'helpd', 'code': code,
               'msg_cn': msg_cn, 'detail': detail or ''}
        line = json.dumps(rec, ensure_ascii=False)
        for fn in ('all.jsonl', 'helpd.jsonl'):
            with open(os.path.join(LOG_DIR, fn), 'a', encoding='utf-8') as f:
                f.write(line + '\n')
    except Exception:
        pass


class Handler(socketserver.StreamRequestHandler):
    """一连接可以多请求：每行一个 JSON 请求，回一行 JSON 响应。"""

    timeout = 900          # 格式化大硬盘这类动作本身就要跑很久

    def handle(self):
        peer = '-'
        try:
            # SO_PEERCRED 拿到对端 uid/gid，写进日志便于审计
            raw = self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _pid, uid, gid = __import__('struct').unpack('3i', raw)
            peer = 'uid=%d gid=%d' % (uid, gid)
        except Exception:
            pass
        try:
            while True:
                line = self.rfile.readline(MAX_LINE + 1)
                if not line:
                    return
                if len(line) > MAX_LINE:
                    self._write({'ok': False, 'code': 'TOO_LARGE',
                                 'msg_cn': '请求体过大'})
                    return
                try:
                    req = json.loads(line.decode('utf-8'))
                except Exception:
                    self._write({'ok': False, 'code': 'BAD_JSON',
                                 'msg_cn': '请求不是合法 JSON'})
                    return
                action = str(req.get('action') or '')
                payload = req.get('payload') or {}
                # 名额用完就明确告诉调用方「忙」，而不是把动作堆在线程池里
                if not _action_sem.acquire(blocking=False):
                    self._write({'ok': False, 'code': 'BUSY',
                                 'msg_cn': '已有 %d 个特权操作在执行，请稍后再试'
                                           % MAX_ACTIONS})
                    return
                t0 = time.time()
                try:
                    res = HELPER.run_action(action, payload)
                finally:
                    _action_sem.release()
                dt = (time.time() - t0) * 1000.0
                with _stats_lock:
                    STATS['requests'] += 1
                    if not res.get('ok'):
                        STATS['errors'] += 1
                    a = STATS['by_action']
                    # 键数上限：本地 drouter 用户可以发任意非法 action 字符串，
                    # 每个都会在这里留下一条永久记录。
                    if len(a) > 200 and action not in a:
                        a.clear()
                    rec = a.get(action) or {'n': 0, 'ms': 0.0, 'max': 0.0}
                    rec['n'] += 1
                    rec['ms'] += dt
                    rec['max'] = max(rec['max'], dt)
                    a[action] = rec
                if not self._write(res):
                    return
        except Exception as e:
            log('warn', 'CONN_ERR', '连接处理异常（%s）：%s' % (peer, e))

    def _write(self, obj):
        """写回一行 JSON。客户端已断开时安静返回 False，不能让线程崩掉。"""
        try:
            data = (json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8')
            self.wfile.write(data)
            self.wfile.flush()
            return True
        except Exception:
            return False


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True       # 主线程退出时不要被工作线程拖住
    request_queue_size = 128
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        log('error', 'HANDLER_ERR', '处理请求时抛出异常', str(sys.exc_info()[1])[:500])


def _bind():
    """建好 socket 并收紧权限。返回 server 对象。"""
    os.makedirs(SOCK_DIR, exist_ok=True)
    os.chmod(SOCK_DIR, 0o755)          # drouter 要能进目录
    if os.path.exists(SOCK_PATH):
        # 上次异常退出留下的陈旧 socket：连上去会直接被拒，必须先清掉
        try:
            os.unlink(SOCK_PATH)
        except Exception:
            pass
    srv = Server(SOCK_PATH, Handler)
    os.chmod(SOCK_PATH, 0o660)
    try:
        import grp
        gid = grp.getgrnam(SOCK_GROUP).gr_gid
        os.chown(SOCK_PATH, 0, gid)
    except Exception as e:
        # 属组设不成就退回 0600：那样只有 root 能连，drouter-web 会走
        # subprocess 回退路径，功能不受影响，只是没有加速。
        os.chmod(SOCK_PATH, 0o600)
        log('warn', 'CHOWN_FAIL', '无法把 socket 属组改为 drouter，已退回 0600：%s' % e)
    return srv


def _load_helper():
    """加载 drouter-helper 模块。

    文件名是 drouter-helper.py（带连字符），不能用普通 import，
    只能用 importlib 从文件路径加载。
    """
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'drouter-helper.py')
    spec = importlib.util.spec_from_file_location('drouter_helper', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['drouter_helper'] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    global HELPER
    HELPER = _load_helper()

    srv = _bind()
    log('info', 'STARTED', '常驻执行守护已启动（socket=%s，动作 %d 个）'
        % (SOCK_PATH, len(HELPER.ACTIONS)))
    print('helpd listening on %s' % SOCK_PATH, flush=True)

    def _on_stop(*_a):
        try:
            srv.shutdown()
        except Exception:
            pass

    try:
        import signal
        signal.signal(signal.SIGTERM, _on_stop)
        signal.signal(signal.SIGINT, _on_stop)
    except Exception:
        pass

    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            srv.server_close()
        except Exception:
            pass
        try:
            if os.path.exists(SOCK_PATH):
                os.unlink(SOCK_PATH)
        except Exception:
            pass
        log('info', 'STOPPED', '常驻执行守护已停止')
    return 0


HELPER = None

if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web 终端 PTY 可用性回归测试。

背景（真实故障）：
  用户点 Web 终端，任何账号都报「创建终端失败：out of pty devices」。
  sysctl kernel.pty.nr 显示 0 —— 一个 PTY 都没用，却说"用完了"。
  根因是 /dev/ptmx 这个入口符号链接丢失（容器/虚拟化环境里 /dev 被
  重新挂载时会丢），而 /dev/pts/ptmx 本身是好的。

这里钉住三件事：
  1. shelld 启动时会自愈 /dev/ptmx（ensure_ptmx 幂等）
  2. _open_pty 首次失败会先修入口再重试，不是直接抛错
  3. 失败时的诊断信息包含「/dev/ptmx」这种能定位问题的关键词，
     而不是只有一句 errno 天书

纯逻辑测试：把源码里的函数抽出来在隔离命名空间跑，用替身模拟
「ptmx 缺失 / 可用」两种情况，不碰真实 /dev。
"""
import ast
import os
import sys

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "backend", "drouter-shelld.py")

_ck = [0, 0]


def ck(name, cond, extra=''):
    _ck[1] += 1
    if cond:
        _ck[0] += 1
        print("  [OK] %s" % name)
    else:
        print("  [!!] %s  %s" % (name, extra))


def load_src():
    with open(SRC, encoding='utf-8') as f:
        return f.read()


def extract_funcs(src, names):
    """从模块源码里抽出指定函数，编译进一个干净命名空间。

    只抽 FunctionDef —— 不抽模块级赋值。模块级的东西（SOCK_PATH、
    _SESSIONS、_lock…）在替身环境里没有意义，而且抽进来会覆盖我们
    注入的测试替身，导致读到真实路径。
    """
    tree = ast.parse(src)
    ns = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            mod = ast.Module(body=[node], type_ignores=[])
            exec(compile(mod, SRC, 'exec'), ns)
    return ns


def main():
    src = load_src()

    print("一、源码必须定义了自愈与诊断函数")
    for fn in ('ensure_ptmx', '_pty_diagnose', '_open_pty'):
        ck("定义了 %s()" % fn, ('def %s(' % fn) in src)
    # Session 必须经由 _open_pty 申请 PTY，而不是直接 pty.openpty() ——
    # 直连的话就没有自愈和诊断，故障会原样抛给用户
    sess_src = src.split('class Session')[1].split('\ndef ')[0] if 'class Session' in src else ''
    ck("Session 不再直接调 pty.openpty()", 'pty.openpty()' not in sess_src)
    ck("Session 经由 _open_pty() 申请", '_open_pty()' in sess_src)

    print("\n二、ensure_ptmx 的三种分支")
    ns = extract_funcs(src, ['ensure_ptmx'])
    ensure_ptmx = ns['ensure_ptmx']

    # 分支 A：入口已存在 → 直接返回 ok，不做任何动作
    calls = []

    class FakeOs:
        path = type('p', (), {'exists': staticmethod(lambda p: True)})()

        @staticmethod
        def symlink(a, b):
            calls.append(('symlink', a, b))

        @staticmethod
        def mknod(*a):
            calls.append(('mknod', a))

    real_os = ns['ensure_ptmx'].__globals__.get('os')
    ns['ensure_ptmx'].__globals__['os'] = FakeOs
    ok, detail = ensure_ptmx()
    ck("入口存在时返回 (True,'ok')", (ok, detail) == (True, 'ok'), str((ok, detail)))
    ck("入口存在时不创建任何东西", calls == [], str(calls))

    # 分支 B：入口缺失但 /dev/pts/ptmx 在 → 补符号链接
    calls = []

    class FakeOs2:
        path = type('p', (), {'exists': staticmethod(
            lambda p: p == '/dev/pts/ptmx')})()

        @staticmethod
        def makedev(a, b):
            return 0

        @staticmethod
        def symlink(a, b):
            calls.append((a, b))
            return None

        @staticmethod
        def mknod(*a):
            calls.append(('mknod', a))

    ns['ensure_ptmx'].__globals__['os'] = FakeOs2
    ok, detail = ensure_ptmx()
    ck("缺失时返回 True", ok is True, str((ok, detail)))
    ck("缺失时建立 pts/ptmx 符号链接",
       calls == [('pts/ptmx', '/dev/ptmx')], str(calls))

    # 分支 C：什么都建不了 → 返回 False + 原因（不抛异常）
    class FakeOs3:
        path = type('p', (), {'exists': staticmethod(lambda p: False)})()

        @staticmethod
        def makedev(a, b):
            return 0

        @staticmethod
        def symlink(a, b):
            raise OSError(30, 'Read-only file system')

        @staticmethod
        def mknod(*a):
            raise OSError(30, 'Read-only file system')

    ns['ensure_ptmx'].__globals__['os'] = FakeOs3
    ok, detail = ensure_ptmx()
    ck("无力修复时返回 False", ok is False, str((ok, detail)))
    ck("返回的 detail 非空（便于日志）", bool(detail), repr(detail))

    print("\n三、_open_pty 失败时必须先自愈再重试")
    nsf = extract_funcs(src, ['_open_pty'])
    _open_pty = nsf['_open_pty']
    g = _open_pty.__globals__

    seq = {'n': 0}
    healed = {'v': False}

    class FakePty:
        @staticmethod
        def openpty():
            seq['n'] += 1
            if seq['n'] == 1:
                raise OSError(5, 'out of pty devices')
            return (7, 8)          # 第二次成功

    def fake_ensure():
        healed['v'] = True
        return True, 'symlink created'

    g['pty'] = FakePty
    g['ensure_ptmx'] = fake_ensure
    g['log'] = lambda *a, **k: None
    g['_pty_diagnose'] = lambda: '替身'

    r = _open_pty()
    ck("首败后自愈并重试成功", r == (7, 8), str(r))
    ck("重试前确实调用了 self-heal", healed['v'] is True)
    ck("openpty 被调用了两次", seq['n'] == 2, str(seq['n']))

    print("\n四、彻底失败时的诊断信息要能定位问题")
    nsf2 = extract_funcs(src, ['_open_pty'])
    _open_pty2 = nsf2['_open_pty']
    g2 = _open_pty2.__globals__

    class AlwaysFailPty:
        @staticmethod
        def openpty():
            raise OSError(5, 'out of pty devices')

    g2['pty'] = AlwaysFailPty
    g2['ensure_ptmx'] = lambda: (False, 'Read-only file system')
    g2['log'] = lambda *a, **k: None
    # 诊断函数：模拟 /dev/ptmx 缺失的情形
    g2['_pty_diagnose'] = lambda: '/dev/ptmx 缺失'

    try:
        _open_pty2()
        ck("无救时抛异常", False, '竟然没抛')
    except OSError as e:
        msg = str(e)
        ck("异常里保留了原始 errno 文案", 'out of pty devices' in msg, msg)
        ck("异常里带上了可定位的诊断", '/dev/ptmx' in msg, msg)

    print("\n五、诊断函数覆盖关键检查点")
    nsd = extract_funcs(src, ['_pty_diagnose'])
    dsrc = src.split('def _pty_diagnose')[1].split('\ndef ')[0]
    for kw in ('/dev/ptmx', '/dev/pts', 'pty/nr', 'pty/max'):
        ck("诊断检查了 %s" % kw, kw in dsrc)

    print("\n六、启动路径也会自愈（不是只在连接时）")
    main_src = src.split('def main()')[1] if 'def main()' in src else ''
    ck("main() 里调用了 ensure_ptmx", 'ensure_ptmx()' in main_src)
    ck("main() 里对失败情况写日志", 'PTY_UNAVAILABLE' in main_src)
    ck("自愈成功也写日志（便于追查）", 'PTY_HEAL' in main_src)

    print("\n七、前端给了可照抄的修复命令")
    app = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'web', 'app.js')
    ajs = open(app, encoding='utf-8').read()
    ck("前端识别 PTY_FAIL", "'PTY_FAIL'" in ajs or 'PTY_FAIL' in ajs)
    ck("前端提示 out of pty devices 的含义", 'out of pty devices' in ajs)
    ck("前端给出修复命令", 'ln -sf pts/ptmx /dev/ptmx' in ajs)
    ck("前端给出重启命令", 'systemctl restart drouter-shelld' in ajs)

    print("\n八、会话回收不得自锁（满员时整个终端服务会冻死）")
    # 背景：connect 需要「看是否满员 → 满了就回收 → 再看一次」这个序列，
    # 天然而必然地会在持锁时调到内部也要拿锁的 _reap_idle()。
    # 用普通 Lock 会把自己永久锁死（同线程二次 acquire），表现为
    # 「连到第 9 个会话时彻底卡住」——而且因为锁一直不放，
    # 其他会话的 read/write 也一起堵住，整个 Web 终端报废。
    ck("_lock 用 RLock（可重入）", '_lock = threading.RLock()' in src)
    # 只查模块级 _lock 的赋值。不能写成 `'_lock = threading.Lock()' not in src` ——
    # Session.__init__ 里的 `self.buf_lock = threading.Lock()` 尾部正好命中
    # 这个子串，会误报。那个是每会话独立的输出缓冲锁，非重入是对的。
    ck("模块级 _lock 不是普通 Lock",
       not __import__('re').search(r'(?m)^_lock\s*=\s*threading\.Lock\(\)', src))

    def blocked_span(lines, i):
        """返回从 lines[i]（一个 `xxx:` 块头）开始、该缩进块结束的下标。"""
        ind = len(lines[i]) - len(lines[i].lstrip())
        j = i + 1
        while j < len(lines):
            s = lines[j]
            if not s.strip():
                j += 1
                continue
            if (len(s) - len(s.lstrip())) <= ind:
                break
            j += 1
        return j

    def calls_inside_lock(text, needle):
        """text 里是否存在「在被 with _lock: 包住的缩进块内」调用 needle。"""
        lines = text.splitlines()
        for i, l in enumerate(lines):
            if l.strip() == 'with _lock:':
                end = blocked_span(lines, i)
                if needle in "\n".join(lines[i:end]):
                    return True
        return False

    reap_src = src.split('def _reap_idle')[1].split('\ndef ')[0]
    ck("_reap_idle 不同时持锁调 close()（close 最长等子进程 3 秒）",
       not calls_inside_lock(reap_src, '.close()'))

    conn_src = src.split("if op == 'connect':")[1].split("if op == 'read':")[0]
    ck("connect 不持锁调 _reap_idle()", not calls_inside_lock(conn_src, '_reap_idle()'))
    ck("connect 仍会在满员时尝试回收", '_reap_idle()' in conn_src,
       "回收被删掉就退化成「每次都直接拒绝」")

    # 行为验证：真的丢一个闲置会话进去，看它是否被摘除并 close
    print("\n九、_reap_idle() 行为正确（摘除 + 关闭 + 返回条数）")
    ns = {'threading': __import__('threading'), 'time': __import__('time'),
          'IDLE_TIMEOUT': 60, 'MAX_SESSIONS': 8, 'log': lambda *a, **k: None}
    live = {}
    ns['_SESSIONS'] = live
    ns['_lock'] = __import__('threading').RLock()
    # 注意 reap_src 是 "def _reap_idle" 之后的部分，拼回去才能编译
    exec(compile('def _reap_idle' + reap_src, '<reap>', 'exec'), ns)

    class _P(object):
        def poll(self):
            return None

    closed = []

    class _S(object):
        alive = True

        def __init__(self, last):
            self.last = last
            self.proc = _P()

        def close(self):
            closed.append(self)

    now = ns['time'].time()
    live['old'] = _S(now - 999)      # 闲置超时 → 该被回收
    live['new'] = _S(now)            # 刚活动过 → 该留下
    n = ns['_reap_idle']()
    ck("闲置会话已从 _SESSIONS 摘除", 'old' not in live, str(list(live)))
    ck("活跃会话被保留", 'new' in live, str(list(live)))
    ck("闲置会话被 close()", len(closed) == 1, str(len(closed)))
    ck("返回回收条数", n == 1, str(n))
    ck("空表调用返回 0 且不抛", ns['_reap_idle']() == 0)

    print("\n十、无 systemd 时的回退：直接拉起终端守护（容器形态）")
    # 背景：容器里没有 systemd，helper 的 `systemctl start drouter-shelld` 必然
    # 失败，socket 永远不出现 —— Web 终端在 Docker 形态下 100% 打不开，
    # 而界面给的提示还是「去执行 systemctl」。这里补一条不依赖 init 的回退。
    HS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      'backend', 'drouter-helper.py')
    hsrc = open(HS, encoding='utf-8').read()
    ck("helper 定义了 _shelld_spawn()", 'def _shelld_spawn(' in hsrc)

    spawn_src = hsrc.split('def _shelld_spawn')[1].split('\ndef ')[0]
    ck("子进程脱离本进程（start_new_session）",
       'start_new_session=True' in spawn_src,
       "不加的话 helper 一退出，守护会被连带收走")
    ck("三路标准流全部丢弃（不继承调用方管道）",
       spawn_src.count('subprocess.DEVNULL') >= 3,
       "继承管道会让 ssh / sudo 调用方一直挂着不返回")
    ck("启动前确认守护脚本存在", 'os.path.isfile(SHELL_DAEMON)' in spawn_src)

    start_src = hsrc.split('def _shelld_start')[1].split('\ndef ')[0]
    ck("_shelld_start 里有 _shelld_spawn() 回退", '_shelld_spawn()' in start_src)
    ck("socket 已存在时不再折腾 systemctl",
       'if os.path.exists(SHELL_SOCK):\n        return True' in start_src)

    # 行为：socket 已在 → 直接 True，一次 systemctl 都不调
    calls3 = []
    ns3 = {
        'os': type('O', (), {'path': type('p', (), {
            'exists': staticmethod(lambda p: True)})()})(),
        'time': __import__('time'),
        'sh': lambda cmd, **k: calls3.append(list(cmd)),
        'SHELL_SOCK': '/run/drouter/shell.sock',
        'SHELL_UNIT': 'drouter-shelld.service',
        '_shelld_spawn': lambda: (calls3.append('spawn'), True)[1],
    }
    exec(compile('def _shelld_start' + start_src, '<s>', 'exec'), ns3)
    ck("socket 已存在时 _shelld_start 直接返回 True",
       ns3['_shelld_start']() is True)
    ck("且没有调用 systemctl", calls3 == [], str(calls3))

    print("\n" + "=" * 56)
    print("结果：%d 项，失败 %d 项" % (_ck[1], _ck[1] - _ck[0]))
    return 1 if _ck[0] != _ck[1] else 0


if __name__ == '__main__':
    sys.exit(main())

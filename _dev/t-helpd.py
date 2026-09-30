#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""常驻执行守护（#7 性能优化）契约测试。

核心契约只有两条，任一条破了都会在生产环境出事：
  1. 有快路径：drouter-web 会先连 Unix socket；
  2. 有回退：连不上时必须退回原来的 subprocess 方式。
     —— 只有快路径没有回退，守护一崩整个面板就废了；
        只有回退没有快路径，性能优化等于没做。

另外几条是「上线后最难发现的坑」：
  * 守护把 helper 常驻内存，部署时不 restart 就跑的是旧代码；
  * 两个守护共用 /run/drouter，RuntimeDirectoryMode 不一致会让 socket
    权限随启动顺序时好时坏；
  * 守护必须复用 helper.run_action，不能另写一套分发（否则构建保护熔断
    就可能在守护路径上失效）。
"""
import ast
import io
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
fails = 0


def chk(label, cond, extra=''):
    global fails
    if not cond:
        fails += 1
    print('[%s] %s %s' % ('OK' if cond else 'FAIL', label, extra))


def read(rel):
    with io.open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


HELPD = read('backend/drouter-helpd.py')
WEB = read('backend/drouter-web.py')
HELPER = read('backend/drouter-helper.py')
DEPLOY = read('scripts/deploy.sh')

# ---------------------------------------------------------------- 1. 快路径
chk('web.py 定义了 socket 路径', "HELPD_SOCK = '/run/drouter/helper.sock'" in WEB)
chk('web.py 实现了 _helpd_call', 'def _helpd_call(' in WEB)
chk('web.py 的 helper() 先走守护', '_helpd_call(action, payload, timeout)' in WEB)
chk('协议是换行分隔的 JSON', "'\\n').encode('utf-8')" in WEB)
# ---------------------------------------------------------------- 2. 回退
chk('web.py 保留了 subprocess 回退',
    "['sudo', '-n', '/usr/bin/python3', HELPER, action" in WEB)
chk('回退前有「守护不可用」冷却期，避免每请求白等',
    '_helpd_down_until' in WEB and '_helpd_down_until[0] = time.time() + 30' in WEB)
chk('守护不可用时返回 None 触发回退', 'return None' in WEB.split('def _helpd_call')[1][:2000])
chk('回退分支有注释说明为什么不能只有快路径',
    '守护挂掉只会变慢' in WEB or '不会让面板不可用' in WEB)

# ---------------------------------------------------------------- 3. 守护本身
chk('守护复用 helper.run_action（不另写分发）', 'HELPER.run_action(action, payload)' in HELPD)
chk('守护 socket 权限 0660', 'os.chmod(SOCK_PATH, 0o660)' in HELPD)
chk('守护把 socket 属组设为 drouter（否则 drouter-web 连不上）',
    "SOCK_GROUP = 'drouter'" in HELPD and 'grp.getgrnam(SOCK_GROUP)' in HELPD)
chk('守护启动前清理陈旧 socket', 'os.unlink(SOCK_PATH)' in HELPD)
chk('守护用多线程（并发请求不排队）', 'ThreadingUnixStreamServer' in HELPD)
chk('客户端断开时工作线程不能崩', '_write(self, obj)' in HELPD)
chk('守护不监听任何 TCP 端口', 'AF_INET' not in HELPD)

# ---------------------------------------------------------------- 4. helper 改造
chk('helper 暴露 run_action', 'def run_action(' in HELPER)
h_tree = ast.parse(HELPER)
has_guard = False
for n in ast.walk(h_tree):
    if isinstance(n, ast.FunctionDef) and n.name == 'run_action':
        src = ast.get_source_segment(HELPER, n) or ''
        has_guard = 'check_build_guard' in src
chk('run_action 保留了构建保护模式熔断', has_guard)
chk('main() 改为调用 run_action（两条路径同一套逻辑）',
    'res = run_action(action, payload)' in HELPER)

# ---------------------------------------------------------------- 5. 部署
chk('部署脚本安装了 drouter-helpd.py', 'backend/drouter-helpd.py' in DEPLOY)
chk('部署脚本 restart 守护（否则旧代码常驻不更新）',
    'systemctl restart drouter-helpd' in DEPLOY)
# 共用 /run/drouter 时绝不能用 systemd 的 RuntimeDirectory：
# 一个守护重启，systemd 就重建目录，把另一个守护的 socket 删掉。
chk('两个守护都不用 RuntimeDirectory（改用各自 mkdir）',
    not re.search(r'^RuntimeDirectory=', DEPLOY, re.M))
chk('守护自己建目录并设 0755',
    "SOCK_DIR = '/run/drouter'" in HELPD and 'os.chmod(SOCK_DIR, 0o755)' in HELPD)
# helpd 单元段：从 'drouter-helpd.service' 到下一个 'cat > /etc/systemd/system'
_seg = DEPLOY.split('drouter-helpd.service')[1].split('cat > /etc/systemd/system')[0]
# 注意只匹配真正的配置行：单元里那条「故意不设 CPUQuota」的说明文字里
# 也含 CPUQuota 字样，直接 `not in` 会误报。
chk('守护单元刻意不限 CPU（格式化大盘等子进程不能被拖慢）',
    'MemoryMax=300M' in _seg
    and not re.search(r'^CPUQuota=', _seg, re.M))

# ---------------------------------------------------------------- 6. 语法
for rel, src in (('backend/drouter-helpd.py', HELPD),
                 ('backend/drouter-web.py', WEB),
                 ('backend/drouter-helper.py', HELPER)):
    try:
        ast.parse(src)
        chk('%s 语法正确' % rel, True)
    except SyntaxError as e:
        chk('%s 语法正确' % rel, False, str(e))

print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % fails))
sys.exit(1 if fails else 0)

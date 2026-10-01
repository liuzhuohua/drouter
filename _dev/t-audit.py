#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全项目静态审计（#6）：专抓「编译得过、跑起来才炸」的低级错误。

四类检查，全部基于源码静态分析，不需要目标机：
  1. web.py 里 self.helper('xxx') 的动作名必须都在 helper 的 ACTIONS 里
     —— 写错一个字母就是「接口返回 ok 但实际报未知操作」；
  2. app.js 里 api('/api/xxx') 的路径必须在 web.py 的路由里有分支
     —— 漏了就是 404，前端拿到 HTML 报错页，表现为「点了没反应」；
  3. 同一个顶层名字（函数 / 常量）不能重复定义
     —— 后定义的静默覆盖前面的，最难查的一类 bug；
  4. 前端引用的 CSS 类必须在 app.css 里有定义（只查我们自己新引入的类）。
"""
import ast
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')

fails = 0


def chk(label, cond, extra=''):
    global fails
    if not cond:
        fails += 1
    print('[%s] %s %s' % ('OK' if cond else 'FAIL', label, extra))


def read(rel):
    with io.open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


HELPER = read('backend/drouter-helper.py')
WEB = read('backend/drouter-web.py')
APP = read('web/app.js')
CSS = read('web/app.css')

# ---------------------------------------------------------------- 1. 动作名
h_tree = ast.parse(HELPER)
ACTIONS = set()
for node in ast.walk(h_tree):
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == 'ACTIONS' and isinstance(node.value, ast.Dict):
                for k in node.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        ACTIONS.add(k.value)
chk('helper 至少注册了 60 个动作', len(ACTIONS) >= 60, '实际 %d 个' % len(ACTIONS))

used = set(re.findall(r"self\.helper\(\s*'([^']+)'", WEB))
used |= set(re.findall(r'helper\(\s*"([^"]+)"', WEB))
bad = sorted(a for a in used if a not in ACTIONS)
chk('web.py 调用的动作全部已注册', not bad, '→ 未注册：%s' % bad)

# 反向：注册了但从来没人调（不算错，只是提示，避免误当失败）
unused = sorted(a for a in ACTIONS if a not in used)
print('     提示：已注册但 web.py 未直接调用的动作 %d 个（可能由其它入口使用）'
      % len(unused))

# ---------------------------------------------------------------- 2. 前端路径
# web.py 的路由写法有三种：p == '/api/x'、p in ('/api/x','/api/y')、
# p.startswith('/api/x')，还有一张 readings 字典把路径映射到 action。
# 只用正则会漏掉字典键（/api/ifaces 等 13 个路径就是这么被误报的），
# 所以这里改用 AST 精确收集。
routes, prefixes = set(), set()
w_tree = ast.parse(WEB)
for node in ast.walk(w_tree):
    # 任何字典里以 /api 开头的键（readings 路由表等）
    if isinstance(node, ast.Dict):
        for k in node.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str) \
                    and k.value.startswith('/api'):
                routes.add(k.value)
    # 元组/列表里的路径字面量（p in ('/api/a','/api/b')）
    if isinstance(node, (ast.Tuple, ast.List)):
        for e in node.elts:
            if isinstance(e, ast.Constant) and isinstance(e.value, str) \
                    and e.value.startswith('/api') and '*' not in e.value:
                routes.add(e.value)
    # p == '/api/x' / p in ... 的比较
    if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) \
            and node.left.id == 'p':
        for c in node.comparators:
            if isinstance(c, ast.Constant) and isinstance(c.value, str) \
                    and c.value.startswith('/api'):
                routes.add(c.value)
    # p.startswith('/api/x')
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == 'startswith':
        for a in node.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                    and a.value.startswith('/api'):
                prefixes.add(a.value)
# 前端实际请求
paths = set(re.findall(r"api\(\s*'([^']+)'", APP))
paths |= set(re.findall(r'api\(\s*`([^`]+)`', APP))
paths = set(p for p in paths if p.startswith('/api'))

missing = []
for p in sorted(paths):
    if p in routes:
        continue
    # 前缀匹配（如 /api/webshell/connect 由 startswith('/api/webshell') 处理）
    if any(p.startswith(pre) for pre in prefixes if pre.startswith('/api')):
        continue
    # 形如 '/api/xxx?op=yyy' 或带模板变量的，只看主干
    base = p.split('?')[0]
    if base in routes:
        continue
    if any(base.startswith(pre) for pre in prefixes if pre.startswith('/api')):
        continue
    missing.append(p)
chk('前端请求的接口路径都有后端路由', not missing, '→ 缺少路由：%s' % missing)

# ---------------------------------------------------------------- 3. 重复定义
def top_names(src, lang):
    out = {}
    if lang == 'py':
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.setdefault(node.name, []).append(node.lineno)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        out.setdefault(t.id, []).append(node.lineno)
    else:
        # JS：顶层 function 声明 + const/let/var
        for m in re.finditer(r'^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(',
                             src, re.M):
            out.setdefault(m.group(1), []).append(src[:m.start()].count('\n') + 1)
        for m in re.finditer(r'^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=',
                             src, re.M):
            out.setdefault(m.group(1), []).append(src[:m.start()].count('\n') + 1)
    return out


for rel, src, lang in (('backend/drouter-helper.py', HELPER, 'py'),
                       ('backend/drouter-web.py', WEB, 'py'),
                       ('web/app.js', APP, 'js')):
    names = top_names(src, lang)
    dup = {k: v for k, v in names.items() if len(v) > 1}
    # 白名单：JS 里允许用 let 再赋值覆盖；Python 里 __main__ 守卫下的同名不算
    chk('%s 无重复顶层定义' % rel, not dup,
        '→ %s' % dict(list(dup.items())[:5]))

# ---------------------------------------------------------------- 4. CSS 类
# 只查我们新引入的、带 t- 前缀的终端样式类
for cls in ('t-b', 't-dim', 't-i', 't-u', 'tcur', 'tl', 'ws-capture',
            'term-screen', 'ws-probe'):
    chk('app.css 定义了 .%s' % cls, ('.' + cls + '{') in CSS or ('.' + cls + ' ') in CSS)

# ---------------------------------------------------------------- 5. 外部命令 ↔ 依赖清单
# helper 里 sh(['xxx', ...]) 调的每个外部命令，都应该在 DEPS 里有对应条目。
# 漏了的表现最阴：依赖自检页一片绿，但某功能因为命令不存在静默失败。
BASE_CMDS = {
    # coreutils / util-linux / systemd / 账号管理：任何 Linux 都自带，不必登记
    'sh', 'bash', 'cat', 'ls', 'rm', 'cp', 'mv', 'date', 'hostname', 'uname',
    'mount', 'umount', 'df', 'du', 'tar', 'which', 'chown', 'chmod', 'mkdir',
    'sleep', 'echo', 'env', 'timeout', 'dpkg-query', 'apt', 'apt-get',
    'apt-cache', 'apt-mark', 'systemctl', 'systemd-detect-virt', 'modinfo',
    'modprobe', 'ss', 'pgrep', 'useradd', 'userdel', 'usermod', 'chpasswd',
    'kill', 'ip', 'getent', 'sed', 'grep', 'awk', 'head', 'tail', 'wc', 'sort',
    'id', 'stat', 'readlink', 'python3', 'find', 'touch', 'tr', 'cut', 'sync',
    'blockdev', 'partprobe', 'dmesg', 'free',
}
# 同一软件包提供的兄弟命令：DEPS 用其中一个登记即可
# 同包不同命令：DEPS 里登记的是该包的一个代表命令（如 procps 用 ps），
# 代码里却可能调用同包的另一个命令（pkill）。按命令名比对会误报，这里做映射。
PKG_ALIAS = {
    'chronyc': 'chronyd', 'docker-compose': 'docker', 'smbpasswd': 'smbd',
    'pkill': 'ps', 'pgrep': 'ps', 'free': 'ps', 'top': 'ps',
    'ss': 'ip', 'tc': 'ip', 'devlink': 'ip',
    'mount': 'lsblk', 'umount': 'lsblk', 'blkid': 'lsblk', 'findmnt': 'lsblk',
    'modinfo': 'modprobe', 'lsmod': 'modprobe', 'depmod': 'modprobe',
    'du': 'df', 'ls': 'df', 'stat': 'df',
    'fsck.exfat': 'mkfs.exfat', 'fsck.f2fs': 'mkfs.f2fs', 'fsck.vfat': 'mkfs.vfat',
    'xfs_repair': 'mkfs.xfs', 'btrfs': 'mkfs.btrfs', 'resize2fs': 'mkfs.ext4',
    'snmpwalk': 'snmpd', 'snmpget': 'snmpd',
    # 「内核转发与加速」用它即时生效 IP 转发 / BBR（同包不同命令）
    'sysctl': 'ps',
    # 打印服务：这几个命令全由 cups / cups-client 提供，DEPS 里以 cupsd 为代表登记
    'lpadmin': 'cupsd', 'lpstat': 'cupsd', 'lpinfo': 'cupsd',
    'lp': 'cupsd', 'cancel': 'cupsd', 'cupsctl': 'cupsd',
    'cupsaccept': 'cupsd', 'cupsreject': 'cupsd',
}
called = {}
for node in ast.walk(h_tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in ('sh', 'sh_out', 'run'):
        if node.args and isinstance(node.args[0], (ast.List, ast.Tuple)):
            elts = node.args[0].elts
            if elts and isinstance(elts[0], ast.Constant) \
                    and isinstance(elts[0].value, str):
                c = elts[0].value
                called[c] = called.get(c, 0) + 1
deps_node = None
for node in h_tree.body:
    if isinstance(node, ast.Assign) and node.value.__class__.__name__ == 'List':
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == 'DEPS':
                deps_node = node.value
dep_targets = set()
for row in deps_node.elts:
    if isinstance(row, (ast.Tuple, ast.List)) and len(row.elts) >= 4:
        kind = row.elts[2]
        tgt = row.elts[3]
        if isinstance(kind, ast.Constant) and kind.value == 'cmd' \
                and isinstance(tgt, ast.Constant) and isinstance(tgt.value, str):
            for c in tgt.value.split('|'):
                dep_targets.add(c.strip())
unknown = sorted(c for c in called
                 if c not in dep_targets and c not in BASE_CMDS
                 and PKG_ALIAS.get(c) not in dep_targets)
chk('helper 调用的外部命令都已登记到依赖清单', not unknown,
    '→ 未登记：%s' % [(c, called[c]) for c in unknown])

# ---------------------------------------------------------------- 6. 已知易错点
chk('helper 仍保留 PATH 归一化（/usr/sbin）', '_ensure_sbin_path' in HELPER)
DEPLOY = read('scripts/deploy.sh')
chk('Web 终端守护有独立 systemd 单元', 'drouter-shelld' in DEPLOY)
chk('常驻执行守护有独立 systemd 单元', 'drouter-helpd' in DEPLOY)
chk('部署脚本会校验 9 个后端模块',
    all(m in DEPLOY for m in ('render.py', 'drouter-helper.py', 'drouter-web.py',
                              'drouter-logd.py', 'drouter-snapshotd.py',
                              'drouter-rescue.py', 'drouter-shelld.py',
                              'drouter-helpd.py', 'theme.py')))
# 守护把 helper 常驻在内存：只 enable --now 的话跑的还是上一版代码
chk('部署时会 restart 常驻执行守护（否则旧代码不生效）',
    'systemctl restart drouter-helpd' in DEPLOY)
# 两个守护共用 /run/drouter。用 RuntimeDirectory 的话，systemd 会在最后一个
# 声明它的单元停止时把整个目录重建，顺手删掉另一个守护的 socket ——
# shelld 依旧 active 但 socket 没了，终端直接废。所以必须各自 mkdir。
# 只匹配真正的配置行：单元注释里也会提到 RuntimeDirectory 这个词
chk('两个守护都不再用 RuntimeDirectory（否则互相删 socket）',
    not re.search(r'^RuntimeDirectory=', DEPLOY, re.M))
chk('部署后会校验终端 socket 真的存在',
    '-S /run/drouter/shell.sock' in DEPLOY)
SHELLD = read('backend/drouter-shelld.py')
chk('shelld 自己创建 /run/drouter 并设 0755',
    'os.makedirs(d, exist_ok=True)' in SHELLD and 'os.chmod(d, 0o755)' in SHELLD)
chk('helper 暴露了 run_action（供守护复用同一套分发）',
    'def run_action(' in HELPER)
chk('前端入口破缓存版本号仍在', '?v=' in APP or 'cachebust' in APP.lower()
    or '?v=' in read('web/index.html'))

# ---------------------------------------------------------------- 部署副作用
# deploy.sh 是唯一会「替用户改机器」的脚本。它最容易犯、也最难被用户归因的错
# 是**静默回退用户设置** —— 用户只会觉得「部署完之后我的设置莫名其妙变回去了」。
chk('部署只在「还没有生效主题」时播种 default（不再每次覆写）',
    'if [ ! -f /etc/drouter/active-theme ]' in DEPLOY
    and DEPLOY.count('echo "default" > /etc/drouter/active-theme') == 1)
chk('部署只在 isp-dns.conf 缺失时才补占位（不再擦掉运营商 DNS）',
    'if [ ! -f /etc/drouter/generated/isp-dns.conf ]' in DEPLOY
    and DEPLOY.count('cat > /etc/drouter/generated/isp-dns.conf') == 1)
chk('生效主题指向已不存在的主题时会兜底回退',
    '_theme_read_active()' in DEPLOY and '_theme_write_active(' in DEPLOY)
# 清理是本项目唯一会真删文件的模块：无条件传 enabled=true 会把用户在界面上
# 明确关掉的自动清理重新打开 —— 静默恢复一个「自动删文件」的策略最不该发生。
chk('部署不再强制打开用户的自动清理开关',
    'if [ ! -f /etc/drouter/cleanup.conf ]' in DEPLOY)
chk('清理策略按「已有/新建」分支处理',
    DEPLOY.count('"op":"save","enabled":true') == 1
    and "cleanup '{\"op\":\"save\"}'" in DEPLOY)

# ---------------------------------------------------------------- 日志噪声
# socketserver 默认会把任何异常连整段 traceback 打进 journal（stderr）。
# 浏览器切页 / 关标签抛的 BrokenPipeError / ConnectionResetError 属于
# 「对面先挂了」，却足以把 journal 刷满、淹掉真正的报错。
chk('web 服务用自带 handle_error 的子类',
    'class _Server(ThreadingHTTPServer)' in WEB
    and 'super().handle_error(request, client_address)' in WEB)
chk('不再直接实例化 ThreadingHTTPServer（会把断连打成 traceback）',
    re.search(r'=\s*ThreadingHTTPServer\(', WEB) is None)
chk('HTTP 与 HTTPS 两个监听都换成 _Server',
    'httpd = _Server(' in WEB and 'httpsd = _Server(' in WEB)

print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % fails))
sys.exit(1 if fails else 0)

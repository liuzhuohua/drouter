#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计：扫出全项目真实用到的外部命令 / apt 包名，与 DEPS 清单对拍。

不是靠人肉记忆补清单 —— 直接从源码 AST 里抽：
  ① sh(['xxx', ...]) / sh(['sudo','xxx']) 的第一或第二个 token
  ② command -v xxx / which xxx 里的 xxx
  ③ apt-get install / apt install 后面的包名
  ④ FS_TYPES 里的 pkg
  ⑤ 各类 PKG / pkg = 'xxx' 常量
再和 backend/drouter-helper.py 的 DEPS 比对，输出「用到但 DEPS 没登记」的项。
"""
import ast
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, 'backend')

# 只看后端：前端 js 不直接执行系统命令
TARGETS = []
for fn in sorted(os.listdir(BACKEND)):
    if fn.endswith('.py'):
        TARGETS.append(os.path.join(BACKEND, fn))

# 已知的非「系统命令」：Python 内建 / shell 内建 / 我们自己的包装
# apt 系不登记：装 drouter 的机器必然有 apt，装它毫无意义
NOT_CMD = {
    'sudo', 'sh', 'bash', 'true', 'false', 'echo', 'cat', 'test', 'cd',
    'python3', 'python', 'sys', 'systemctl',  # systemctl 单独登记
    'apt', 'apt-get', 'apt-cache', 'apt-mark', 'dpkg', 'dpkg-query',
}
# shell 内建，不该登记成命令依赖
SHELL_BUILTIN = {
    'command', 'test', 'echo', 'cd', 'true', 'false', 'set', 'export',
    'if', 'then', 'fi', 'for', 'do', 'done', 'while', 'case', 'esac',
}


def _walk_str_list(node):
    """从 sh(['a','b']) 这种 list 里取常量字符串。"""
    if isinstance(node, ast.List):
        out = []
        for e in node.elts:
            if isinstance(e, ast.Constant) and isinstance(e.value, str):
                out.append(e.value)
        return out
    return None


def scan():
    cmds = {}     # cmd -> set(文件)
    pkgs = {}     # pkg -> set(文件)
    for path in TARGETS:
        base = os.path.basename(path)
        try:
            src = io.open(path, encoding='utf-8').read()
        except Exception:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            print('[SKIP] %s 语法错误: %s' % (base, e))
            continue

        # ① sh([...]) 调用
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                if n.func.id in ('sh', 'run', 'sh_out', 'sudo') and n.args:
                    lst = _walk_str_list(n.args[0])
                    if not lst:
                        continue
                    # 去掉 sudo 前缀
                    if lst and lst[0] == 'sudo':
                        lst = lst[1:]
                    if not lst:
                        continue
                    c = lst[0]
                    if c in ('sh', 'bash'):
                        # sh -c '...'：只取管道 / 分号 / && 之后的第一个词，
                        # 否则会把 docker images inspect 这种子命令词当成命令抓进来。
                        inner = ' '.join(lst[1:])
                        parts = re.split(r'\|\||&&|\||;|\n|\$\(', inner)
                        for seg in parts:
                            seg = seg.strip()
                            if seg.startswith('-'):   # sh -c 的 -c 本身
                                continue
                            w = seg.split()[0] if seg.split() else ''
                            w = w.strip('"\'')
                            if not w or w in SHELL_BUILTIN:
                                continue
                            if not re.match(r'^[a-z][a-z0-9_.-]*$', w):
                                continue
                            if w.endswith('/'):       # 路径片段
                                continue
                            cmds.setdefault(w, set()).add(base)
                        continue
                    if c in SHELL_BUILTIN:
                        continue
                    cmds.setdefault(c, set()).add(base)

        # ② 字符串里的 `command -v xxx`
        for m in re.finditer(r"command\s+-v\s+([a-zA-Z0-9_.-]+)", src):
            cmds.setdefault(m.group(1), set()).add(base + ':command-v')

        # ③ apt install
        for m in re.finditer(r"apt(?:-get)?\s+install[^'\"]*?([a-z0-9][a-z0-9.+-]*)", src):
            pass
        # apt install 多以 list 形式出现，用 AST 抽更准
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                if n.func.id in ('sh', 'run', 'sudo') and n.args:
                    lst = _walk_str_list(n.args[0]) or []
                    if 'install' in lst and ('apt' in lst or 'apt-get' in lst):
                        for t in lst:
                            if t in ('apt', 'apt-get', 'install', '-y', '-q',
                                     '--no-install-recommends', 'sudo', 'sh', '-c'):
                                continue
                            if re.match(r'^[a-z0-9][a-z0-9.+-]+$', t):
                                pkgs.setdefault(t, set()).add(base + ':apt')
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                v = n.value
                if re.match(r'^\s*apt(-get)?\s+install', v):
                    for t in v.split():
                        if re.match(r'^[a-z0-9][a-z0-9.+-]+$', t) and t not in (
                                'apt', 'apt-get', 'install', '-y', '-q',
                                '--no-install-recommends', 'DEBIAN_FRONTEND=noninteractive'):
                            pkgs.setdefault(t, set()).add(base + ':aptstr')

        # ④ 常量里的 'pkg': 'xxx'
        for m in re.finditer(r"""['"]pkg['"]\s*:\s*['"]([a-z0-9][a-z0-9.+-]*)['"]""", src):
            pkgs.setdefault(m.group(1), set()).add(base + ':pkgfield')

    return cmds, pkgs


def _is_pure(node):
    """判断表达式是否只由常量 / 名字 / 加减 / 容器组成（不含调用）。

    用来挑出「可以安全提前求值」的模块级常量 —— 带函数调用的（os.makedirs、
    open 之类）绝不能在本机 exec，否则测试自己就会去动真实路径。
    """
    for n in ast.walk(node):
        if isinstance(n, (ast.Call, ast.Attribute, ast.Subscript,
                          ast.Lambda, ast.Await, ast.Yield)):
            return False
    return True


def _exec_const(path, name):
    """从源码里 exec 出某个模块级常量，避免 import 整个后端模块。"""
    src = io.open(path, encoding='utf-8').read()
    tree = ast.parse(src)
    # 先把同层的纯常量按出现顺序求值进命名空间。
    # DEPS 里现在会引用别处定义的常量（例如 opensoho 那条引用 OH_BIN），
    # 只 exec 目标那一个节点会 NameError。逐个求值天然解决前后依赖，
    # 求值失败的（引用了未注入的名字）直接跳过，不影响结果。
    ns = {}
    for n in tree.body:
        if not isinstance(n, ast.Assign) or not _is_pure(n.value):
            continue
        try:
            mod = ast.Module(body=[n], type_ignores=[])
            ast.fix_missing_locations(mod)
            exec(compile(mod, '<const>', 'exec'), ns)
        except Exception:
            pass
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    mod = ast.Module(body=[ast.Assign(
                        targets=[ast.Name(name, ast.Store())], value=n.value)],
                        type_ignores=[])
                    ast.fix_missing_locations(mod)
                    exec(compile(mod, '<' + name + '>', 'exec'), ns)
                    return ns[name]
    return None


def load_deps():
    return _exec_const(os.path.join(BACKEND, 'drouter-helper.py'), 'DEPS') or []


def load_groups():
    g = _exec_const(os.path.join(BACKEND, 'drouter-helper.py'), 'DEP_GROUPS') or []
    return dict(g)


def main():
    cmds, pkgs = scan()
    deps = load_deps()
    global DEP_GROUP_NAME
    DEP_GROUP_NAME = load_groups()

    dep_keys = set(d[0] for d in deps)
    dep_targets = set()
    for d in deps:
        dep_targets.add(d[0])
        if d[2] in ('cmd', 'mod'):
            for c in str(d[3]).split('|'):
                dep_targets.add(c.strip())
    dep_pkgs = set(d[6] for d in deps if d[6])

    # 命令 → 提供它的常见 Debian 包（人工维护的映射表，用于提示）
    CMD2PKG = {
        'ip': 'iproute2', 'ss': 'iproute2', 'tc': 'iproute2', 'rdma': 'iproute2',
        'nft': 'nftables', 'iptables': 'iptables',
        'dnsmasq': 'dnsmasq', 'radvd': 'radvd', 'dhcpcd': 'dhcpcd-base',
        'pppd': 'ppp', 'pppoeconf': 'pppoeconf', 'pon': 'ppp', 'poff': 'ppp',
        'chronyd': 'chrony', 'chronyc': 'chrony',
        'miniupnpd': 'miniupnpd',
        'ethtool': 'ethtool', 'curl': 'curl', 'wget': 'wget',
        'conntrack': 'conntrack', 'sqlite3': 'sqlite3', 'openssl': 'openssl',
        'iperf3': 'iperf3', 'traceroute': 'traceroute', 'tracepath': 'iputils-tracepath',
        'dig': 'dnsutils', 'nslookup': 'dnsutils', 'host': 'bind9-host',
        'ping': 'iputils-ping', 'arping': 'iputils-arping', 'ping6': 'iputils-ping',
        'zip': 'zip', 'unzip': 'unzip', '7z': 'p7zip-full', '7za': 'p7zip-full',
        'vim': 'vim', 'vim.tiny': 'vim-tiny', 'nano': 'nano',
        'htop': 'htop', 'iftop': 'iftop', 'socat': 'socat', 'nc': 'netcat-openbsd',
        'logrotate': 'logrotate', 'sudo': 'sudo', 'systemctl': 'systemd',
        'journalctl': 'systemd', 'networkctl': 'systemd', 'nmcli': 'network-manager',
        'lsblk': 'util-linux', 'blkid': 'util-linux', 'findmnt': 'util-linux',
        'mount': 'util-linux', 'umount': 'util-linux', 'lsusb': 'usbutils',
        'mkfs.exfat': 'exfatprogs', 'fsck.exfat': 'exfatprogs',
        'mkfs.ext4': 'e2fsprogs', 'mkfs.ext3': 'e2fsprogs', 'mkfs.ext2': 'e2fsprogs',
        'resize2fs': 'e2fsprogs', 'tune2fs': 'e2fsprogs', 'e2fsck': 'e2fsprogs',
        'mkfs.ntfs': 'ntfs-3g', 'ntfsfix': 'ntfs-3g',
        'mkfs.vfat': 'dosfstools', 'fsck.vfat': 'dosfstools',
        'mkfs.xfs': 'xfsprogs', 'xfs_repair': 'xfsprogs', 'xfs_growfs': 'xfsprogs',
        'mkfs.btrfs': 'btrfs-progs', 'btrfs': 'btrfs-progs',
        'mkfs.f2fs': 'f2fs-tools', 'fsck.f2fs': 'f2fs-tools',
        'smbd': 'samba', 'nmbd': 'samba', 'smbpasswd': 'samba-common-bin',
        'exportfs': 'nfs-kernel-server', 'showmount': 'nfs-common',
        'tcpdump': 'tcpdump', 'mtr': 'mtr-tiny', 'etherwake': 'etherwake',
        'wakeonlan': 'wakeonlan', 'docker': 'docker.io',
        'modprobe': 'kmod', 'lsmod': 'kmod', 'modinfo': 'kmod',
        'smartctl': 'smartmontools', 'smartd': 'smartmontools',
        'sensors': 'lm-sensors', 'dmidecode': 'dmidecode', 'lscpu': 'util-linux',
        'lspci': 'pciutils', 'hwinfo': 'hwinfo', 'udevadm': 'udev',
        'hostapd': 'hostapd', 'wpa_supplicant': 'wpasupplicant', 'iw': 'iw',
        'vnstat': 'vnstat', 'nload': 'nload', 'bmon': 'bmon',
        'snmpd': 'snmpd', 'snmpwalk': 'snmp', 'snmpget': 'snmp',
        'lpstat': 'cups-client', 'lpadmin': 'cups-client', 'cupsd': 'cups',
        'lp': 'cups-client', 'lpinfo': 'cups-client', 'cupsctl': 'cups-client',
        'usbip': 'usbip', 'lsusb.py': 'usbutils',
        'jq': 'jq', 'rsync': 'rsync', 'tar': 'tar', 'gzip': 'gzip',
        'xz': 'xz-utils', 'zstd': 'zstd',
        'update-ca-certificates': 'ca-certificates',
        'c_rehash': 'openssl',
        'dpkg': 'dpkg', 'apt': 'apt', 'apt-get': 'apt',
        'kill': 'procps', 'pkill': 'procps', 'ps': 'procps', 'free': 'procps',
        'top': 'procps', 'uptime': 'procps', 'vmstat': 'procps',
        'getent': 'libc-bin', 'id': 'coreutils', 'stat': 'coreutils',
        'df': 'coreutils', 'du': 'coreutils', 'ls': 'coreutils',
        'hostname': 'hostname', 'hostnamectl': 'systemd',
        'timedatectl': 'systemd', 'locale-gen': 'locales',
        'sysctl': 'procps', 'iptables-save': 'iptables',
        'frr': 'frr', 'bird': 'bird2', 'vtysh': 'frr',
        'sha256sum': 'coreutils', 'md5sum': 'coreutils',
    }

    # 「必须被 DEPS 覆盖」的项：这些是本项目的关键功能，
    # 缺任何一个都会有页面能点但功能悄悄不工作。
    MUST_COVER = {
        # 文件系统工具：外置设备格式化页面会直接按 pkg 提示安装
        'e2fsprogs': 'mkfs.ext4 / mkfs.ext3（ext4 格式化）',
        'xfsprogs': 'mkfs.xfs（XFS 格式化）',
        'btrfs-progs': 'mkfs.btrfs（Btrfs 格式化）',
        'f2fs-tools': 'mkfs.f2fs（F2FS 格式化）',
        'exfatprogs': 'mkfs.exfat（exFAT 格式化）',
        'ntfs-3g': 'mkfs.ntfs（NTFS 格式化）',
        'dosfstools': 'mkfs.vfat（FAT32 格式化）',
        # 系统工具
        'util-linux': 'lsblk / mount（外置存储与挂载）',
        'iproute2': 'ip / ss / tc（网络与 QoS）',
        'kmod': 'modprobe / modinfo（QoS 内核模块加载）',
        'procps': 'ps / pkill（进程查询）',
        'tar': 'tar（主题 ZIP 导出等打包）',
        'logrotate': 'logrotate（日志轮转）',
        'curl': 'curl（公网 IP 探测 / DDNS 上报）',
        # 本轮新增功能
        'snmpd': 'snmpd（SNMP 监控服务）',
        'cups': 'cups（打印服务）',
        'cups-client': 'lpadmin / lpstat（打印机管理命令）',
        'usbutils': 'lsusb（USB 打印机识别）',
    }

    print('=' * 70)
    print('DEPS 清单：%d 项；扫描到命令 %d 个、包名 %d 个' % (len(deps), len(cmds), len(pkgs)))
    print('=' * 70)

    fails = []

    # ① 必须覆盖的包
    print('\n【1】关键依赖覆盖检查：')
    for pkg, why in sorted(MUST_COVER.items()):
        hit = pkg in dep_pkgs
        print('  %s %-16s %s' % ('[OK]' if hit else '[NG]', pkg, why))
        if not hit:
            fails.append('DEPS 缺少包：%s（%s）' % (pkg, why))

    # ② FS_TYPES 里每个 pkg 都要能在 DEPS 找到（否则格式化页面提示安装、
    #    但自检页的就绪列表里永远看不到它 —— 用户反馈的正是这个）
    print('\n【2】格式化工具与 DEPS 对拍：')
    fs = load_fs_types()
    for f in fs:
        hit = f['pkg'] in dep_pkgs
        print('  %s %-14s %-14s pkg=%s' % ('[OK]' if hit else '[NG]',
                                           f['n'], f['mkfs'], f['pkg']))
        if not hit:
            fails.append('FS_TYPES 的 %s 用包 %s，DEPS 未登记' % (f['mkfs'], f['pkg']))

    # ③ 代码里用到、DEPS 没登记的命令。
    #    按「提供它的包」比对而不是按命令名：du/ls 归 coreutils、modinfo 归 kmod，
    #    DEPS 里登记的是 df / modprobe 这个代表命令，按命令比对会全是误报。
    unreg = []
    for c in sorted(cmds):
        if c in NOT_CMD or c in SHELL_BUILTIN or c in dep_targets:
            continue
        p = CMD2PKG.get(c)
        if p and p not in dep_pkgs:
            unreg.append((c, p))
    print('\n【3】代码用到但 DEPS 未登记的命令（按提供包比对）：')
    for c, p in unreg:
        print('  [NG] %-20s -> %-20s %s' % (c, p, ','.join(sorted(cmds[c])[:2])))
    if not unreg:
        print('  [OK] 已登记命令全部有对应包')
    if unreg:
        fails.append('以下命令已在代码中使用但 DEPS 未登记其包：%s'
                     % ', '.join('%s(%s)' % (c, p) for c, p in unreg))

    # ④ 代码里引用的包名
    unreg_pkg = sorted(p for p in pkgs if p not in dep_pkgs and p not in dep_keys)
    print('\n【4】代码引用但 DEPS 未登记的包名：%s'
          % (', '.join(unreg_pkg) if unreg_pkg else '（无）'))

    # ⑤ DEPS 结构自检
    print('\n【5】DEPS 条目结构自检：')
    seen = set()
    for d in deps:
        if len(d) != 8:
            fails.append('DEPS 条目字段数不是 8（key,name,kind,target,required,note,pkg,group）：%r' % (d,))
            continue
        key, name, kind, target, req, note, pkg, group = d
        if key in seen:
            fails.append('DEPS 有重复 key：%s' % key)
        seen.add(key)
        if kind not in ('cmd', 'file', 'mod', 'svc'):
            fails.append('DEPS %s 的 kind 非法：%s' % (key, kind))
        if not name or not target:
            fails.append('DEPS %s 缺少 name 或 target' % key)
        if not note:
            fails.append('DEPS %s 缺少中文说明' % key)
        if group not in DEP_GROUP_NAME:
            fails.append('DEPS %s 的分组 %r 不在 DEP_GROUPS 里' % (key, group))
    print('  %s 共 %d 项，结构全部合法' % ('[OK]' if not fails else '[NG]', len(deps)))

    # ⑥ 打包声明与 DEPS 同步：离线脚本 / deb control 至少要覆盖必需项
    print('\n【6】打包依赖声明同步检查：')
    offline = read_offline_deps()
    control = read_control_deps()
    req_pkgs = sorted(set(d[6] for d in deps if d[4] and d[6]))
    for p in req_pkgs:
        in_o = p in offline
        in_c = p in control
        flag = '[OK]' if (in_o or in_c) else '[NG]'
        print('  %s %-18s 离线脚本=%s control=%s' % (flag, p,
                                                    '有' if in_o else '无',
                                                    '有' if in_c else '无'))
        if not (in_o or in_c):
            fails.append('必需依赖 %s 既不在 make-offline-deps.sh 也不在 control 里' % p)

    # ⑦ 解包处元数一致性：DEPS 元组改字段数时，helper 里每个
    #    `for ... in DEPS` 都要同步改。漏一处就是运行时 ValueError，
    #    而且只在用户点「开始检测」时才炸 —— 必须在这里挡住。
    print('\n【7】helper 解包处元数一致性：')
    helper_src = io.open(os.path.join(BACKEND, 'drouter-helper.py'),
                         encoding='utf-8').read()
    unpacks = re.findall(r'for\s+([A-Za-z_][\w,\s]*?)\s+in\s+DEPS\s*:', helper_src)
    if not unpacks:
        fails.append('helper 里找不到任何 `for ... in DEPS` 解包')
    for u in unpacks:
        n = len([x for x in u.split(',') if x.strip()])
        flag = '[OK]' if n == 8 else '[NG]'
        print('  %s 解包 %d 个字段：%s' % (flag, n, ' '.join(u.split())))
        if n != 8:
            fails.append('helper 有 DEPS 解包用了 %d 个字段（应为 8）：%s' % (n, u.strip()))

    # ⑧ 危险包绝不能标成「必需」：一键安装会装下所有必需项，
    #    把桌面环境 / 常驻服务塞进去对路由器是灾难
    print('\n【8】危险包不得标为必需：')
    DANGER = {
        'xfce4': '桌面环境', 'xserver-xorg': '图形服务',
        'samba': '装完自动拉起 smbd', 'nfs-kernel-server': '装完自动拉起 nfs-server',
        'docker.io': '装完自动拉起 docker 常驻', 'snmpd': '装完自动拉起 snmpd 常驻',
        'cups': '装完自动拉起 cups 常驻', 'smartmontools': '装完自动拉起 smartd 常驻',
    }
    for d in deps:
        key, name, kind, target, req, note, pkg, group = d
        if pkg in DANGER and req:
            print('  [NG] %-18s %s' % (key, DANGER[pkg]))
            fails.append('DEPS 的 %s（%s）被标为必需，一键安装会把它装上：%s'
                         % (key, pkg, DANGER[pkg]))
    if not any(d[6] in DANGER and d[4] for d in deps):
        print('  [OK] 会拉起常驻服务 / 桌面的包全部是可选')

    # ⑨ 前端契约：分组与搜索相关的函数和字段都在
    print('\n【9】前端契约：')
    app_js = io.open(os.path.join(ROOT, 'web', 'app.js'), encoding='utf-8').read()
    need = [
        ('renderDepList', '过滤/搜索只重渲染、不重打接口'),
        ('depMatch', '按名称/命令/包名/说明匹配'),
        ('DEP_LAST', '缓存上次检测结果'),
        ('DEP_FILTER', '分组过滤状态'),
        ("x.group === DEP_FILTER", '按后端返回的 group 过滤'),
        ("d.groups || []", '消费后端 groups 列表'),
        ("x.group_name", '渲染分组名标签'),
    ]
    for token, why in need:
        hit = token in app_js
        print('  %s %-32s %s' % ('[OK]' if hit else '[NG]', token, why))
        if not hit:
            fails.append('前端缺少 %s（%s）' % (token, why))
    css = io.open(os.path.join(ROOT, 'web', 'app.css'), encoding='utf-8').read()
    hit = '.dep-grp.on' in css
    print('  %s %-32s %s' % ('[OK]' if hit else '[NG]', '.dep-grp.on', '分组按钮选中态样式'))
    if not hit:
        fails.append('CSS 缺少 .dep-grp.on 选中态（点了看不出选中了哪个）')

    print('\n' + '=' * 70)
    if fails:
        print('失败 %d 项：' % len(fails))
        for f in fails:
            print('  - ' + f)
        _EXTRA_FAIL.append(len(fails))
    else:
        print('结果: 全部通过')


def read_offline_deps():
    p = os.path.join(ROOT, 'scripts', 'make-offline-deps.sh')
    try:
        src = io.open(p, encoding='utf-8').read()
    except OSError:
        return set()
    m = re.search(r'DEPS="([^"]*)"', src, re.S)
    return set(m.group(1).split()) if m else set()


def read_control_deps():
    p = os.path.join(ROOT, 'packaging', 'deb', 'control')
    try:
        src = io.open(p, encoding='utf-8').read()
    except OSError:
        return set()
    out = set()
    keep = False
    for line in src.splitlines():
        if re.match(r'^(Depends|Recommends|Suggests|Pre-Depends):', line):
            keep = True
            body = line.split(':', 1)[1]
        elif re.match(r'^\s', line) and keep:
            body = line
        else:
            keep = False
            continue
        for t in re.split(r'[,\s|()]+', body):
            t = t.strip()
            if re.match(r'^[a-z0-9][a-z0-9.+-]+$', t):
                out.add(t)
    return out


def load_fs_types():
    return _exec_const(os.path.join(BACKEND, 'drouter-helper.py'), 'FS_TYPES') or []


# main() 里收集到的失败数（main 不再自行 sys.exit，交给文件末尾统一决策）
_EXTRA_FAIL = []


def t(name, cond):
    """额外检查用的小断言：打印一行 [OK]/[NG] 并返回失败计数（0 或 1）。"""
    print('  %s %s' % ('[OK]' if cond else '[NG]', name))
    return 0 if cond else 1


def check_pkg_names():
    """包名必须是 Debian 13 真有的。

    踩过的坑：Docker Compose v2 在 Debian 13 叫 docker-compose，
    而 docker-compose-v2 是 Ubuntu 的叫法。写错包名的后果不是「装不上」
    而是「整条 apt-get 命令一起失败」—— apt 只要发现一个查不到的包名
    就整体中止，同命令里其它正常的包也一个都装不上（已实测）。
    """
    helper = io.open(os.path.join(BACKEND, 'drouter-helper.py'), encoding='utf-8').read()
    app = io.open(os.path.join(ROOT, 'web', 'app.js'), encoding='utf-8').read()
    ctl = io.open(os.path.join(ROOT, 'packaging', 'deb', 'control'), encoding='utf-8').read()

    def _local_t(name, cond):
        print('  %s %s' % ('[OK]' if cond else '[NG]', name))
        return 0 if cond else 1

    print('\n---- 包名合规（Debian 13） ----')
    n = 0
    # docker-compose-v2 允许作为「候选之一」出现在 DOCKER_PKGS 里（跨发行版兼容），
    # 但不能作为唯一写法出现在用户可见的安装命令 / control 声明里。
    n += _local_t('安装包声明里不再出现 Ubuntu 专有包名 docker-compose-v2',
           'docker-compose-v2' not in ctl)
    n += _local_t('前端提示里不再出现 Ubuntu 专有包名',
           'apt-get install -y docker.io docker-compose-v2' not in app)
    n += _local_t('DOCKER_PKGS 首选 Debian 名 docker-compose',
           re.search(r"DOCKER_PKGS\s*=\s*\['docker\.io',\s*'docker-compose'", helper) is not None)
    n += _local_t('DOCKER_PKGS 仍保留 Ubuntu 候选（跨发行版）',
           re.search(r"DOCKER_PKGS\s*=.*'docker-compose-v2'", helper) is not None)
    n += _local_t('有按可用性过滤包名的函数', 'def _pkgs_available(' in helper)
    n += _local_t('depcheck 安装前过滤不存在的包', '_pkgs_available(cand)' in helper)
    n += _local_t('docker 安装命令用过滤后的列表',
           "'install_cmd': 'apt-get install -y ' + ' '.join(have)" in helper)
    n += _local_t('过滤后为空时给出明确失败原因', '在当前 apt 源里都找不到' in helper)
    n += _local_t('compose 判断同时认两个包名',
           "inst('docker-compose') or inst('docker-compose-v2')" in helper)
    n += _local_t('DEPS 里 compose 的包名带双候选',
           "'docker-compose|docker-compose-v2'" in helper)
    return n


def check_atomic_write():
    """所有「会写生产配置文件」的路径都必须走 tmp + os.replace 原子替换。

    为什么单列一节：open(path,'w') 会先把原文件截断成 0 字节，再逐块写新内容。
    在这两步之间，如果守护进程恰好 reload、或者机器掉电，它读到的就是一个
    残缺文件 —— 轻则服务起不来，重则按半截规则放行（防火墙 / 共享目录尤其危险）。
    os.replace 是同文件系统内的原子重命名，读者只会看到完整的新旧两版之一。
    """
    helper = io.open(os.path.join(BACKEND, 'drouter-helper.py'), encoding='utf-8').read()
    lines = helper.split('\n')

    def seg_of(name):
        try:
            i = next(k for k, l in enumerate(lines) if l.startswith('def %s(' % name))
        except StopIteration:
            return ''
        j = next((k for k in range(i + 1, len(lines)) if lines[k].startswith('def ')), len(lines))
        return '\n'.join(lines[i:j])

    print('\n---- 原子写盘 ----')
    n = 0
    n += t('act_apply 用 path + .drouter.tmp 再 os.replace',
           "tmp = path + '.drouter.tmp'" in seg_of('act_apply')
           and 'os.replace(tmp, path)' in seg_of('act_apply'))
    share = seg_of('_share_apply_file')
    n += t('_share_apply_file 先写临时文件', "tmp = path + '.drouter.tmp'" in share)
    n += t('_share_apply_file 用 os.replace 原子覆盖', 'os.replace(tmp, path)' in share)
    n += t('_share_apply_file 不再直接 open(path,"w") 覆写',
           "with open(path, 'w'" not in share)
    n += t('写失败时清理残留临时文件', "os.remove(path + '.drouter.tmp')" in share)
    return n


if __name__ == '__main__':
    main()
    extra_fail = check_pkg_names() + check_atomic_write()
    total = sum(_EXTRA_FAIL) + extra_fail
    if total:
        print('\n总失败 %d 项' % total)
    sys.exit(1 if total else 0)

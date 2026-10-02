#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包产物静态自检：deb 元数据 / 维护脚本 / Dockerfile。

真机构建由 packaging/build-deb.sh 负责（需要 dpkg-deb），本脚本负责在任何机器上
都能跑的「结构正确性」检查，把「打出来的包不能用」挡在构建之前。
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))

fails = []


def ck(name, cond, extra=''):
    # extra 只在失败时打。通过的时候打出来是纯噪声（"OK → 只有 12 个" 这种
    # 自相矛盾的话最糟，会让人以为真有问题），久了就没人看这输出了。
    print('  %-46s %s %s' % (name, 'OK  ' if cond else 'FAIL',
                             '' if cond else extra))
    if not cond:
        fails.append(name)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


def no_crlf(rel):
    with open(os.path.join(ROOT, rel), 'rb') as f:
        return b'\r' not in f.read()


def bash_ok(rel):
    """能用 bash 就做真语法检查；Windows 上没有 bash 就跳过（不算失败）。"""
    exe = subprocess.run(['bash', '-n', os.path.join(ROOT, rel)],
                         capture_output=True)
    if exe.returncode == 127 or (b'not found' in exe.stderr.lower()):
        return None
    return exe.returncode == 0


print('一、deb 元数据（packaging/deb/control）')
ctl = read('packaging/deb/control')
ck('含 Package', 'Package: drouter' in ctl)
ck('Version 是占位符（构建时替换）', '__VERSION__' in ctl)
ck('Architecture: all', 'Architecture: all' in ctl)
ck('Depends 非空', 'Depends:' in ctl and len(ctl.split('Depends:')[1].split('\n\n')) > 0)
for pkg in ('python3', 'nftables', 'dnsmasq', 'radvd', 'ppp', 'systemd', 'sudo'):
    ck('Depends 含 %s' % pkg, pkg in ctl.split('Recommends:')[0])
# smartmontools 装完会自动起 smartd，不该出现在 apt 默认安装的 Recommends 里
rec = ctl.split('Recommends:')[1].split('\n')[0] if 'Recommends:' in ctl else ''
ck('Recommends 不含 smartmontools（会自启守护）', 'smartmontools' not in rec, rec.strip()[:50])
ck('smartmontools 放进了 Suggests',
   'smartmontools' in (ctl.split('Suggests:')[1] if 'Suggests:' in ctl else ''))
ck('control 无 CRLF', no_crlf('packaging/deb/control'))
# DEBIAN/control 是 deb822 格式，官方并不保证支持 "#" 注释 —— 别往里写注释
ck('control 不含 # 注释行（deb822 不保证支持）',
   not [l for l in ctl.split('\n') if l.lstrip().startswith('#')])
# 每一行必须是「空行」或「字段名: 值」或「以空格/制表符开头的续行」
bad_cont = []
for l in ctl.split('\n'):
    if not l.strip():
        continue
    if l[0] in (' ', '\t'):
        continue
    if ':' not in l:
        bad_cont.append(l)
ck('每行都是「字段: 值」或续行', not bad_cont, str(bad_cont[:2]))
# 依赖策略的理由写在 build-deb.sh 里（control 不能写注释）
ck('依赖策略说明在 build-deb.sh 里',
   'smartmontools' in read('packaging/build-deb.sh'))

print('\n二、维护脚本（postinst / prerm / postrm）')
for m in ('postinst', 'prerm', 'postrm'):
    rel = 'packaging/deb/' + m
    src = read(rel)
    ck('%s 存在' % m, bool(src))
    ck('%s 无 CRLF' % m, no_crlf(rel))
    ck('%s 有 shebang' % m, src.startswith('#!/bin/bash'))
    ck('%s 处理了所有 dpkg 动作' % m, 'case "$1" in' in src and '*)' in src)
    ck('%s 以 exit 0 收尾' % m, 'exit 0' in src)
    r = bash_ok(rel)
    if r is not None:
        ck('%s bash 语法' % m, r)

# postinst 的安全红线：绝不能 enable/start DHCP、DNS、UPnP
pi = read('packaging/deb/postinst')
for svc in ('dnsmasq', 'radvd', 'miniupnpd'):
    ck('postinst 不启动 %s' % svc,
       ('systemctl enable %s' % svc) not in pi and ('systemctl start %s' % svc) not in pi)
ck('postinst 用构建保护模式', 'BUILD_MODE' in pi)
ck('postinst 会装 drouter-ctl', 'drouter-ctl' in pi)
ck('postinst 会跑 deploy.sh', 'deploy.sh' in pi)
ck('postinst 用 DROUTER_SRC 指向 /opt/drouter', 'DROUTER_SRC=/opt/drouter' in pi)

prm = read('packaging/deb/postrm')
ck('postrm purge 清 /etc/drouter', 'rm -rf /etc/drouter' in prm)
ck('postrm purge 清 /var/lib/drouter（累计流量）', '/var/lib/drouter' in prm)
ck('postrm remove 保留配置', 'remove)' in prm)

print('\n三、Dockerfile')
df = read('packaging/docker/Dockerfile')
ck('无 CRLF', no_crlf('packaging/docker/Dockerfile'))
INSTR = ('FROM', 'LABEL', 'ENV', 'RUN', 'COPY', 'EXPOSE', 'VOLUME',
         'HEALTHCHECK', 'ENTRYPOINT', 'CMD', 'WORKDIR', 'ARG', 'USER', 'SHELL')
lines = df.split('\n')
bad = []
for i, l in enumerate(lines):
    s = l.strip()
    if not s or s.startswith('#'):
        continue
    if s.split()[0].startswith(INSTR):
        continue
    # 上一行以 \ 结尾则是续行，合法
    if i > 0 and lines[i - 1].rstrip().endswith('\\'):
        continue
    bad.append((i + 1, s))
ck('所有指令合法', not bad, str(bad[:2]))
ck('有 FROM', df.startswith('FROM ') or '\nFROM ' in df)
ck('有 ENTRYPOINT', 'ENTRYPOINT' in df)
ck('有 CMD', 'CMD' in df)
ck('有 HEALTHCHECK', 'HEALTHCHECK' in df)

# COPY 的源必须真实存在，否则 docker build 才失败就太晚了
missing = []
for l in lines:
    if l.strip().startswith('COPY'):
        parts = l.split()
        if len(parts) >= 2:
            src = parts[1]
            if '*' not in src and not os.path.exists(os.path.join(ROOT, src.rstrip('/'))):
                missing.append(src)
ck('COPY 源路径都存在', not missing, str(missing))

# HEALTHCHECK / 端口不应写死，否则用户改端口后永远 unhealthy
hc = [l for l in lines if 'HEALTHCHECK' in l or 'curl -sk' in l]
ck('健康检查读取实际端口（不写死 8443）',
   any('web-port' in x for x in hc), ''.join(hc)[:80])

ini = read('packaging/docker/docker-init.sh')
ck('docker-init 无 CRLF', no_crlf('packaging/docker/docker-init.sh'))
ck('docker-init 以 exec 结尾（信号可传递）', 'exec "$@"' in ini)
ck('docker-init 播种端口后 unset（避免压住界面改端口）',
   'unset DROUTER_WEB_PORT' in ini)
r = bash_ok('packaging/docker/docker-init.sh')
if r is not None:
    ck('docker-init bash 语法', r)

print('\n四、构建脚本')
for rel in ('packaging/build-deb.sh', 'packaging/build-docker.sh',
            'scripts/drouter-ctl.sh', 'scripts/install-base.sh'):
    ck('%s 存在' % rel, os.path.exists(os.path.join(ROOT, rel)))
    ck('%s 无 CRLF' % rel, no_crlf(rel))
    r = bash_ok(rel)
    if r is not None:
        ck('%s bash 语法' % rel, r)

bd = read('packaging/build-deb.sh')
ck('build-deb 先校验后端再打包', 'ast.parse' in bd)
ck('build-deb 生成 md5sums', 'md5sums' in bd)

# 逐个点名，别再用「数 .py 出现次数」这种会被注释里的文件名骗过的写法。
# drouter-shelld.py 是 Web 终端的 PTY 守护，漏了它终端就打不开。
#
# ⚠️ 清单必须**从 build-deb.sh 的 for 循环里抽**，不能在测试里再抄一份。
# 早先这里硬编码了 9 个，而 build-deb.sh 实际校验 12 个 —— 两份清单各自
# 独立维护，结果是 backupd / alertd / quotad 三个新守护「测试说齐了、
# 打包脚本说齐了」，但谁都没真去对过对方。测试抄实现的清单，等于没测。
# ⚠️ 必须 findall 再挑「.py 最多」的那个，不能用 search 取第一个 ——
# build-deb.sh 里有好几个 for 循环（复制文件、算权限…），第一个匹配到的
# 根本不是后端校验那个。
_cands = [re.findall(r'[\w.-]+\.py', g)
          for g in re.findall(r'for\s+\w+\s+in\s+(.*?);\s*do', bd, re.S)]
if not _cands:
    ck('build-deb.sh 能抽出后端模块 for 循环', False,
       '→ 找不到 `for f in ... ; do` 形式的校验循环')
    BACKEND_MODULES = []
else:
    BACKEND_MODULES = max(_cands, key=len)
    ck('build-deb.sh 抽出 %d 个后端模块' % len(BACKEND_MODULES),
       len(BACKEND_MODULES) >= 12,
       '→ 只有 %d 个，backupd/alertd/quotad 这类新守护容易漏' %
       len(BACKEND_MODULES))

miss_mod = [x for x in BACKEND_MODULES if x not in bd]
ck('build-deb 逐个校验 %d 个后端模块' % len(BACKEND_MODULES), not miss_mod,
   '→ 缺 %s' % miss_mod if miss_mod else '')

# 清单里的每个文件都必须真的存在于 backend/ 下。
# 上面那条只验「文件名出现在脚本里」，写错一个字（比如 drouter-backupd.py
# 拼成 backup.py）照样能过。这条才是「打包不会漏文件」的真正保证。
miss_real = [x for x in BACKEND_MODULES
             if not os.path.exists(os.path.join(ROOT, 'backend', x))]
ck('build-deb 清单里的每个模块在 backend/ 下真实存在', not miss_real,
   '→ 不存在 %s' % miss_real if miss_real else '')

# 反向：新加一个后端 .py 就必须进清单。漏了的话 dpkg 包里没有它，
# 线上表现是「某个页面 404 / 某个守护根本不存在」，极难定位。
have = {f for f in os.listdir(os.path.join(ROOT, 'backend'))
        if f.endswith('.py') and not f.startswith('__')}
notlisted = sorted(have - set(BACKEND_MODULES))
ck('backend/ 下没有游离于清单之外的模块', not notlisted,
   '→ 未进清单：%s' % notlisted if not notlisted else '')

print('\n结果：失败 %d 项' % len(fails))
for f in fails:
    print('  ✘ ' + f)
sys.exit(1 if fails else 0)

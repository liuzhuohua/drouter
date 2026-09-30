#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#10 Docker 面板 —— 纯逻辑与校验测试（不依赖 Docker / Linux）。

从 drouter-helper.py 抽取相关片段，用 NS 命名空间隔离执行。
覆盖：
  A. docker run → compose 转换（回归，与 t-docker-conv.py 互补的关键断言）
  B. _docker_validate_stack_name / stack_save 内容校验 / stack_delete 防穿越
  C. act_docker 参数白名单（非法 op / 非法容器 ID / 非法 prune 类型）
"""
import os
import re
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, 'backend', 'drouter-helper.py')
REG = os.path.join(ROOT, 'backend', 'render.py')

src = open(SRC, encoding='utf-8').read()

PASS = []
FAIL = []


def chk(name, got, want):
    if got == want:
        PASS.append(name)
        return True
    FAIL.append('%s：得到 %r，期望 %r' % (name, got, want))
    return False


def has(name, hay, needle, want=True):
    got = needle in (hay or '')
    if got == want:
        PASS.append(name)
        return True
    FAIL.append('%s：%r in 输出 = %s，期望 %s' % (name, needle, got, want))
    return False


def grab(pattern, name, flags=re.S):
    m = re.search(pattern, src, flags)
    if not m:
        raise SystemExit('抽取失败：%s' % name)
    return m.group(0)


# ---------- 构建 NS ----------
NS = {
    're': re, 'os': os, 'json': json, 'sys': sys,
    'ValidateError': type('ValidateError', (Exception,), {
        '__init__': lambda self, msg, field=None: (
            setattr(self, 'msg_cn', msg), setattr(self, 'field', field))[0] or None,
    }),
    'sh': lambda *a, **k: (1, '', 'skip'),
    'ok': lambda data=None, msg=None: {'ok': True, 'data': data, 'msg_cn': msg},
    'fail': lambda msg, code='ERR', data=None: {
        'ok': False, 'code': code, 'msg_cn': msg, 'data': data},
    'DOCKER_STACK_DIR': '/etc/drouter/docker/stacks',
    'datetime': __import__('datetime').datetime,
}

# 抽取 docker 相关纯逻辑与校验函数
for fn in ('_docker_shlex', '_docker_split_kv', '_yaml_scalar',
           '_to_yaml_compose', '_docker_validate_stack_name'):
    NS[fn] = None
seg = grab(r'DOCKER_BOOL_FLAGS = \{.*?(?=\ndef read_docker\()', 'DOCKER_* 常量与函数区')
exec(compile(seg, SRC, 'exec'), NS)

# 只抽取 _docker_run_to_compose 单函数（避免带入 read_docker/act_docker 的 IO）
seg2 = grab(r'def _docker_run_to_compose\(.*?(?=\ndef _docker_bin\()', '_docker_run_to_compose')
exec(compile(seg2, SRC, 'exec'), NS)
conv = NS['_docker_run_to_compose']

# ---------- A. 转换器关键回归 ----------
r = conv('docker run -d --name nginx -p 8080:80 -v /data:/usr/share/nginx/html nginx:alpine')
chk('A1 ok', r['ok'], True)
has('A1 image', r['yaml'], 'image: "nginx:alpine"')
has('A1 ports', r['yaml'], '"8080:80"')
has('A1 volume', r['yaml'], '"/data:/usr/share/nginx/html"')
has('A1 container_name', r['yaml'], 'container_name: nginx')

r = conv('docker run --rm -it alpine sh')
chk('A2 rm→no restart', 'restart: "no"' in r['yaml'], True)
has('A2 stdin_open', r['yaml'], 'stdin_open: true')
has('A2 tty', r['yaml'], 'tty: true')
has('A2 command', r['yaml'], 'command: sh')

r = conv('''docker run -d \\
  --name app \\
  -e "DB_HOST=host.docker.internal" \\
  -e TZ=Asia/Shanghai \\
  --health-cmd "curl -f http://localhost/ || exit 1" \\
  --health-interval 30s \\
  --ulimit nofile=1024:2048 \\
  app:latest''')
chk('A3 ok', r['ok'], True)
has('A3 env quoted', r['yaml'], '"DB_HOST=host.docker.internal"')
has('A3 env plain', r['yaml'], '"TZ=Asia/Shanghai"')
has('A3 healthcheck test', r['yaml'], 'curl -f http://localhost/ || exit 1')
has('A3 healthcheck CMD-SHELL', r['yaml'], 'CMD-SHELL')
has('A3 healthcheck interval', r['yaml'], 'interval: 30s')
has('A3 ulimit soft', r['yaml'], 'soft: "1024"')
has('A3 ulimit hard', r['yaml'], 'hard: "2048"')

# 未知参数启发式：`--flag value` 应识别为带值，不吞掉镜像名
r = conv('docker run -d --some-unknown-flag value nginx:latest')
has('A4 image kept', r['yaml'], 'image: "nginx:latest"')
chk('A4 unknown not image', r['image'], 'nginx:latest')
has('A4 warning', '\n'.join(r['warnings']), '--some-unknown-flag')

# 未知布尔开关 + 镜像（不带值）也不能误吞镜像名
r = conv('docker run -d --some-bool nginx:latest')
chk('A4b image', r['image'], 'nginx:latest')

# --mount 裸标志
r = conv('docker run -d --mount type=bind,src=/a,dst=/b,readonly nginx')
has('A4c mount read_only', r['yaml'], 'read_only: true')
has('A4c mount source', r['yaml'], 'source: /a')
has('A4c mount target', r['yaml'], 'target: /b')

# 具名网络 → 顶层 networks + external
r = conv('docker run -d --network mynet nginx')
has('A5 networks top', r['yaml'], '\nnetworks:')
has('A5 external', r['yaml'], 'external: true')
has('A5 mynet key', r['yaml'], 'mynet:')

# 版本：默认不加 version 字段（只剩注释里提到，正文行不以 'version:' 开头）
r = conv('docker run -d nginx')
chk('A6 default no version line',
    any(ln.startswith('version:') for ln in r['yaml'].splitlines()), False)
r = conv('docker run -d nginx', version='3.9')
has('A6 set version', r['yaml'], "version: \"3.9\"")

# 服务名自动推导
r = conv('docker run -d --name my-svc-1 nginx')
chk('A7 service name', r['service'], 'my-svc-1')
r = conv('docker run -d nginx:alpine')
chk('A7 service from image', r['service'], 'nginx')

# 非 docker run 命令
r = conv('echo hello')
chk('A8 reject non-run', r['ok'], False)

# ---------- B. stack 名称校验 ----------
V = NS['_docker_validate_stack_name']
for good in ('a', 'my-app', 'app_1', 'a' * 41):
    try:
        V(good)
        PASS.append('B good %s' % good[:12])
    except Exception as e:
        FAIL.append('B good %s 被拒：%s' % (good[:12], e))
for bad in ('', 'A', 'MyApp', '-x', '_x', 'a' * 42, 'a b', 'a/b', '../x', 'a.b'):
    try:
        V(bad)
        FAIL.append('B bad %r 未被拒' % bad)
    except Exception:
        PASS.append('B bad %r 已拒' % bad)

# ---------- C. act_docker 参数白名单（抽取判断体） ----------
# op 白名单
ops = set(re.findall(r"op (?:==|in) \(?'?([a-z_]+)'?\)?", src))
for need in ('convert', 'logs', 'inspect', 'prune', 'stack_save', 'stack_delete'):
    chk('C op %s 存在' % need, need in ops, True)

# 容器 ID 正则
m = re.search(r"re\.match\(r'\^\[a-zA-Z0-9\]\[a-zA-Z0-9_\.\-\]\{0,63\}\$'", src)
chk('C 容器 ID 正则存在', bool(m), True)
if m:
    rx = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$')
    chk('C id 合法', bool(rx.match('nginx')), True)
    chk('C id 合法2', bool(rx.match('a1b2c3d4e5f6')), True)
    chk('C id 非法(空)', bool(rx.match('')), False)
    chk('C id 非法(空格)', bool(rx.match('a b')), False)
    chk('C id 非法(斜杠)', bool(rx.match('a/b')), False)
    chk('C id 非法(点开头)', bool(rx.match('.hidden')), False)
    chk('C id 超长', bool(rx.match('a' * 65)), False)

# prune 白名单
pws = re.findall(r"'(\w+)': \['(?:container|image|volume|network|builder)'", src)
for need in ('containers', 'images', 'volumes', 'networks', 'builder'):
    chk('C prune %s' % need, need in pws, True)

# stack_delete 防穿越
has('C realpath 校验', src, 'os.path.realpath(os.path.join(DOCKER_STACK_DIR, name))')
has('C startswith root', src, "d2.startswith(root + os.sep)")
has('C 需确认项目名', src, "p.get('confirm') != name")

# stack_save 必须含 services 段
has('C services 校验', src, r"^\s*services\s*:")
has('C NUL 校验', src, "if '\\x00' in content")

# sh 支持 cwd（本轮修复）
has('C sh cwd 参数', src, "def sh(cmd, timeout=15, input_data=None, cwd=None, env=None):")
has('C sh 透传 cwd', src, "if cwd:\n        kw['cwd'] = cwd")
has('C compose 使用 cwd', src, 'sh(args, timeout=600, cwd=d2)')

# ACTIONS 注册
has('C ACTIONS read:docker', src, "'read:docker': read_docker")
has('C ACTIONS docker', src, "'docker': act_docker")

# DOCKER 常量
for cn in ('DOCKER_DIR', 'DOCKER_STACK_DIR', 'DOCKER_PKGS'):
    has('C 常量 %s' % cn, src, '%s = ' % cn)

print('=' * 62)
print('#10 Docker 面板 · 单测结果')
print('=' * 62)
print('通过：%d   失败：%d' % (len(PASS), len(FAIL)))
if FAIL:
    print('-' * 62)
    for f in FAIL:
        print('  ✗ ' + f)
    sys.exit(1)
print('全部通过 ✔')

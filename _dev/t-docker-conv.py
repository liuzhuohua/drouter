# -*- coding: utf-8 -*-
"""验证 docker run → docker-compose.yml 转换器（#10）。纯逻辑抽取测试。"""
import re, io, sys

SRC = r"C:/Users/lyrz-pve-win10/WorkBuddy/2026-09-27-19-57-00/router-build/backend/drouter-helper.py"
src = io.open(SRC, encoding='utf-8').read()


def grab(n):
    m = re.search(r'^def %s\(.*?(?=\n(?:def |[A-Z_]+ *=|# ---|class ))' % re.escape(n),
                  src, re.S | re.M)
    if not m:
        raise SystemExit('未找到函数：%s' % n)
    return m.group(0)


NS = {'re': re}
for c in ('DOCKER_BOOL_FLAGS', 'DOCKER_SHORT', 'DOCKER_LIST_FLAGS', 'DOCKER_RESTART',
          'DOCKER_RUN_MAP'):
    m = re.search(r'^%s = \{.*?\n\}' % c, src, re.S | re.M)
    if not m:
        raise SystemExit('未找到常量：%s' % c)
    exec(m.group(0), NS)
for f in ('_docker_shlex', '_docker_split_kv', '_docker_run_to_compose',
          '_yaml_scalar', '_to_yaml_compose'):
    exec(grab(f), NS)

CV = NS['_docker_run_to_compose']
SH = NS['_docker_shlex']

fails = 0


def chk(name, got, want):
    global fails
    ok = (got == want)
    if not ok:
        fails += 1
    print('[%s] %-46s got=%-26s want=%s' % ('OK' if ok else 'FAIL', name, repr(got), repr(want)))


def has(name, needle, hay, want=True):
    chk(name, needle in hay, want)


print('=== 词法分析 _docker_shlex ===')
chk('普通空格切分', SH('a b c'), ['a', 'b', 'c'])
chk('双引号保空格', SH('--name "my app"'), ['--name', 'my app'])
chk('单引号保空格', SH("-e 'A B=1'"), ['-e', 'A B=1'])
chk('转义空格', SH(r'a\ b c'), ['a b', 'c'])
chk('续行合并', SH('a \\\n b'), ['a', 'b'])
chk('引号内 $ 不展开', SH('-e "PATH=$PATH"'), ['-e', 'PATH=$PATH'])
chk('空串', SH(''), [])
chk('连续空格', SH('a   b'), ['a', 'b'])

print('\n=== 基础：名称 / 端口 / 卷 / 环境变量 ===')
r = CV('docker run -d --name web -p 8080:80 -p 8443:443 '
       '-v /srv/www:/usr/share/nginx/html:ro -e TZ=Asia/Shanghai nginx:alpine')
chk('解析成功', r['ok'], True)
chk('服务名用 --name', r['service'], 'web')
chk('镜像', r['image'], 'nginx:alpine')
has('含 container_name', 'container_name: web', r['yaml'])
has('端口 8080', '- "8080:80"', r['yaml'])
has('端口 8443', '- "8443:443"', r['yaml'])
has('卷 ro 后缀', '- "/srv/www:/usr/share/nginx/html:ro"', r['yaml'])
has('环境变量', '- "TZ=Asia/Shanghai"', r['yaml'])
has('无 version 行', 'version:', r['yaml'], False)

print('\n=== 短参数紧凑写法 -p8080:80 / -it / -m512m ===')
r2 = CV('docker run -it -p8080:80 -m512m ubuntu:24.04 bash')
chk('解析成功', r2['ok'], True)
has('紧凑端口识别', '- "8080:80"', r2['yaml'])
has('内存紧凑识别', 'mem_limit: 512m', r2['yaml'])
has('stdin_open', 'stdin_open: true', r2['yaml'])
has('tty', 'tty: true', r2['yaml'])
has('command 是 bash', 'command: bash', r2['yaml'])

print('\n=== 网络：host / none / container / 具名 ===')
r3 = CV('docker run -d --net=host nginx')
has('host → network_mode', 'network_mode: host', r3['yaml'])
r4 = CV('docker run -d --network none alpine')
has('none → network_mode', 'network_mode: none', r4['yaml'])
r5 = CV('docker run -d --network container:other alpine')
has('container: → network_mode', 'network_mode: "container:other"', r5['yaml'])
r6 = CV('docker run -d --network mynet alpine')
has('具名网络 → networks 列表', '- mynet', r6['yaml'])
has('顶层 networks 段', 'networks:', r6['yaml'])
has('external 声明', 'external: true', r6['yaml'])

print('\n=== 资源限制 / 权限 ===')
r7 = CV('docker run -d --cpus=1.5 -m 512m --memory-swap 1g --cpu-shares 512 '
        '--cap-add NET_ADMIN --cap-add SYS_TIME --cap-drop ALL '
        '--security-opt no-new-privileges --privileged alpine')
has('cpus', 'cpus: "1.5"', r7['yaml'])
has('mem_limit', 'mem_limit: 512m', r7['yaml'])
has('memswap_limit', 'memswap_limit: 1g', r7['yaml'])
has('cpu_shares', 'cpu_shares: "512"', r7['yaml'])
chk('cap_add 两个', r7['yaml'].count('- NET_ADMIN') + r7['yaml'].count('- SYS_TIME'), 2)
has('cap_drop', '- ALL', r7['yaml'])
has('security_opt', '- no-new-privileges', r7['yaml'])
has('privileged', 'privileged: true', r7['yaml'])

print('\n=== 只读 / init / 设备 / sysctl / ulimit ===')
r8 = CV('docker run -d --read-only --init --device /dev/dri:/dev/dri '
        '--sysctl net.ipv4.ip_forward=1 --ulimit nofile=1024:2048 alpine')
has('read_only', 'read_only: true', r8['yaml'])
has('init', 'init: true', r8['yaml'])
has('devices', '/dev/dri:/dev/dri', r8['yaml'])
has('sysctls', '- "net.ipv4.ip_forward=1"', r8['yaml'])
has('ulimits soft', 'soft: "1024"', r8['yaml'])
has('ulimits hard', 'hard: "2048"', r8['yaml'])

print('\n=== 日志 / 健康检查 ===')
r9 = CV('docker run -d --log-driver json-file --log-opt max-size=10m '
        '--health-cmd "curl -f http://localhost || exit 1" '
        '--health-interval 30s --health-retries 3 --health-timeout 5s '
        '--health-start-period 10s nginx')
has('logging.driver', 'driver: json-file', r9['yaml'])
has('log opt', 'max-size: 10m', r9['yaml'])
has('healthcheck CMD-SHELL', 'CMD-SHELL', r9['yaml'])
has('healthcheck interval', 'interval: 30s', r9['yaml'])
has('healthcheck retries', 'retries: "3"', r9['yaml'])
has('healthcheck timeout', 'timeout: 5s', r9['yaml'])
has('healthcheck start_period', 'start_period: 10s', r9['yaml'])

print('\n=== GPU ===')
rg = CV('docker run -d --gpus all nvidia/cuda:12.0-base')
has('deploy.resources', 'devices:', rg['yaml'])
has('nvidia driver', 'driver: nvidia', rg['yaml'])
has('count all', 'count: all', rg['yaml'])

print('\n=== --mount 长语法 ===')
rm = CV('docker run -d --mount type=bind,src=/srv/data,dst=/data,readonly alpine')
has('volumes 长语法生效', 'target: /data', rm['yaml'])
has('source 保留', 'source: /srv/data', rm['yaml'])
has('read_only', 'read_only: true', rm['yaml'])

print('\n=== --rm / -P 的提示 ===')
rr = CV('docker run --rm alpine echo hi')
chk('--rm 有 note', len(rr['notes']) >= 1, True)
has('restart no', 'restart: "no"', rr['yaml'])
rp = CV('docker run -d -P nginx')
chk('-P 有 note', any('publish-all' in n or '随机' in n for n in rp['notes']), True)

print('\n=== restart 值归一 ===')
chk('unless-stopped', CV('docker run -d --restart unless-stopped x')['fields'].get('restart'),
    'unless-stopped')
chk('on-failure', CV('docker run -d --restart on-failure x')['fields'].get('restart'),
    'on-failure')
chk('--restart=always', CV('docker run -d --restart=always x')['fields'].get('restart'),
    'always')

print('\n=== 服务名推断 ===')
chk('无 --name 用镜像名', CV('docker run -d nginx:alpine')['service'], 'nginx')
chk('带 registry 去路径', CV('docker run -d ghcr.io/linuxserver/sonarr:latest')['service'],
    'sonarr')
chk('无 tag 提取', CV('docker run -d traefik')['service'], 'traefik')
chk('指定服务名覆盖', CV('docker run -d nginx', service_name='my-svc')['service'], 'my-svc')

print('\n=== 错误处理 ===')
chk('空输入', CV('')['ok'], False)
chk('非 docker 命令', CV('ls -la')['ok'], False)
chk('compose 命令提示', 'docker compose' in CV('docker compose up -d')['msg_cn'], True)
chk('无镜像名', CV('docker run -d -p 80:80')['ok'], False)
chk('只有 docker run', CV('docker run')['ok'], False)

print('\n=== 未知参数 → 警告而非崩溃 ===')
ru = CV('docker run -d --some-future-flag=1 --another-future-flag nginx')
chk('仍然成功', ru['ok'], True)
chk('产生警告', len(ru['warnings']) >= 1, True)
has('镜像仍然正确', 'image: nginx', ru['yaml'])

print('\n=== 多行续行命令 ===')
rl = CV('''docker run -d \\
  --name app \\
  -p 3000:3000 \\
  -v /srv/app:/app \\
  node:20-alpine''')
chk('多行解析成功', rl['ok'], True)
has('多行 name', 'container_name: app', rl['yaml'])
has('多行 ports', '- "3000:3000"', rl['yaml'])
has('多行 volumes', '- "/srv/app:/app"', rl['yaml'])
has('多行 image', 'image: "node:20-alpine"', rl['yaml'])

print('\n=== YAML 引号安全 ===')
chk('含冒号加引号', NS['_yaml_scalar']('a:b'), '"a:b"')
chk('纯数字加引号', NS['_yaml_scalar']('8080'), '"8080"')
chk('布尔词加引号', NS['_yaml_scalar']('true'), '"true"')
chk('普通词不加引号', NS['_yaml_scalar']('alpine'), 'alpine')
chk('True 渲染 true', NS['_yaml_scalar'](True), 'true')
chk('空串加引号', NS['_yaml_scalar'](''), '""')

print('\n=== 老式 --link / volumes-from ===')
ro = CV('docker run -d --link db:db --volumes-from data alpine')
has('links', 'db:db', ro['yaml'])
has('volumes_from', '- data', ro['yaml'])

print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % fails))
sys.exit(1 if fails else 0)

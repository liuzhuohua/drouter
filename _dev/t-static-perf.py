#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态资源传输层契约：长缓存 + brotli 协商 + 版本号注入 + logo 回概述。

1.0.9 新增。这一批改动要挡住的具体回归：

1. **指纹资源没走长缓存** —— app.js 583KB（gzip 198KB / br 160KB）。
   早先一律 no-cache，指纹明明注入了却每次重传。局域网无感，
   走外网（cloudflared / 远程打开）就是每开一次面板多传 200KB。
2. **brotli 不可用时没回退 gzip** —— 目标机实测**没装 python3-brotli**
   （只有 C 库 libbrotli1）。如果「要 br 就只发 br，失败吐原文」，
   优化后在真机上退化成 583KB 裸传，比优化前更慢。
3. **两种编码共用一个缓存键** —— 后写进去的会把先写的覆盖掉，
   拿到 br 的客户端可能收到 gzip 版，浏览器解不开就是满屏乱码。
4. **`br;q=0` 被当支持** —— 子串匹配会正好发给「明确不要 br」的客户端。
5. **版本号没有单一真源** —— 早先 4 个构建脚本各写死一个默认值。
6. **index.html 的 ETag 与实际发出的内容不一致** —— 它发的是注入后的
   产物，ETag 却按原文件算；只改 VERSION 时 ETag 不变，客户端换到 304
   就继续用旧副本，页面版本号永远停在旧版。
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


WEB = io.open(os.path.join(ROOT, 'backend/drouter-web.py'),
              encoding='utf-8').read()
INDEX = io.open(os.path.join(ROOT, 'web/index.html'),
                encoding='utf-8').read()
APPJS = io.open(os.path.join(ROOT, 'web/app.js'),
                encoding='utf-8').read()
CSS = io.open(os.path.join(ROOT, 'web/app.css'), encoding='utf-8').read()

# ---- 1. 长缓存按「有没有指纹」区分 ----
chk('按有没有指纹区分缓存策略（不是按扩展名）',
    "fingerprinted = 'v=' in (self.path or '')" in WEB)
chk('带指纹的静态资源走 immutable 长缓存',
    'max-age=31536000, immutable' in WEB)
chk('没指纹的仍是 no-cache（否则部署后用户拿到旧文件）',
    "if fingerprinted else 'no-cache'" in WEB)
chk('HTML 入口页仍是 no-store（它每次都要重新注入）',
    "no-store, no-cache, must-revalidate" in WEB)

# ---- 2. brotli 逐级降级 ----
chk('有 _want_brotli 协商判断', 'def _want_brotli' in WEB)
chk('brotli 不可用时返回 (None, None) 而不是原文',
    re.search(r'def _brotli_cached.*?return None, None', WEB, re.S) is not None)
chk('brotli 失败后**继续试 gzip**（逐级降级，不能吐原文）',
    re.search(r'if _want_brotli.*?if enc:\s*\n\s*return body, enc.*?if _want_gzip',
              WEB, re.S) is not None)
chk('编码探测集中在一个函数里（避免两处实现漂移）',
    'def _accepts(headers, token)' in WEB)
chk('Accept-Encoding 识别 q=0（br;q=0 是「不要 br」）',
    "q.startswith('q=')" in WEB)
chk('brotli 质量档有定值（不是默认 q11，首屏等不起）',
    'BROTLI_QUALITY = ' in WEB)

# ---- 3. 两种编码不能共用缓存键 ----
chk('编码进缓存键', re.search(r'key = \(path, int\(st\.st_mtime\), st\.st_size, enc\)',
                              WEB) is not None)
chk('只有一个压缩缓存表', WEB.count('_comp_cache = {}') == 1
    and '_gz_cache' not in WEB and '_br_cache' not in WEB)

# ---- 4. 压缩协商的其余契约 ----
chk('压缩后必须发 Vary: Accept-Encoding',
    WEB.count("send_header('Vary', 'Accept-Encoding')") >= 2)
chk('有体积下限（小响应压了反而亏）', 'GZIP_MIN = ' in WEB)
chk('压缩比不划算时不压', 'if len(z) >= len(data)' in WEB)

# ---- 5. 版本号单一真源 ----
chk('有 packaging/VERSION 这个唯一真源',
    os.path.isfile(os.path.join(ROOT, 'packaging/VERSION')))
chk('后端从 /opt/drouter/VERSION 读版本',
    "os.path.join(BASE, 'VERSION')" in WEB)
chk('openapi 的 version 用 app_version() 而非硬编码',
    "'version': app_version()" in WEB)
chk('版本号读取只做一次（运行期不变，缓存住）', '_VERSION_READ' in WEB)
chk('版本号格式受校验（半截写入不会显示到页面上）',
    'VERSION_FALLBACK' in WEB and re.search(r"re\.match\(r'\^", WEB) is not None)
for p in ('packaging/build-deb.sh', 'packaging/build-docker.sh',
          'packaging/build-offline-bundle.sh', '_dev/build-image-remote.sh'):
    s = io.open(os.path.join(ROOT, p), encoding='utf-8').read()
    chk('%s 从 VERSION 文件取版本' % os.path.basename(p),
        'packaging/VERSION' in s and not re.search(r'\$\{1:-1\.\d', s))

# 每一份「装到运行目录」的清单都要带上 VERSION ——
# build-deb.sh(通配 backend/*.py) 不会自动带上它，漏了不报错只是版本号不更新。
chk('build-deb.sh 把 VERSION 装进包',
    '$STAGE/opt/drouter/VERSION' in
    io.open(os.path.join(ROOT, 'packaging/build-deb.sh'),
            encoding='utf-8').read())
chk('deploy.sh 把 VERSION 装到 /opt/drouter',
    '$OPT/VERSION' in io.open(os.path.join(ROOT, 'scripts/deploy.sh'),
                              encoding='utf-8').read())
chk('Dockerfile 把 VERSION 拷进镜像',
    'packaging/VERSION /opt/drouter/VERSION' in
    io.open(os.path.join(ROOT, 'packaging/docker/Dockerfile'),
            encoding='utf-8').read())
chk('sync.sh 打包时带上 packaging/VERSION',
    'packaging/VERSION' in io.open(os.path.join(ROOT, 'devtools/sync.sh'),
                                   encoding='utf-8').read())

# ---- 6. index.html 的 ETag 必须覆盖注入后的内容 ----
chk('index.html 的 ETag 并入版本号',
    re.search(r"endswith\('index\.html'\):\s*\n\s*etag = .*app_version\(\)",
              WEB) is not None)
chk('页脚有版本号占位符', '__APP_VERSION__' in INDEX and 'id="app-ver"' in INDEX)
chk('注入函数替换掉占位符', "'__APP_VERSION__', app_version()" in WEB)

# ---- 7. logo 回概述页 ----
chk('左上角 logo 是 <a>（键盘/读屏可用，不是 div onclick）',
    re.search(r'<a class="brand[^"]*"[^>]*id="brand-home"', INDEX) is not None)
chk('logo 有可访问名称', 'title="返回系统概览"' in INDEX)
chk('logo 点击已绑定', "brandHome.addEventListener('click'" in APPJS)
chk('点击时 preventDefault（否则 #dash 会跳到地址栏）',
    re.search(r"addEventListener\('click', e => \{\s*\n\s*e\.preventDefault\(\)",
              APPJS) is not None)
chk('已在概述页时不重复 go()（go 会 stopPageTimers 再重拉数据）',
    "if (S.page === 'dash') return;" in APPJS)
chk('logo 点击会收起手机端抽屉', re.search(
    r"addEventListener\('click', e => \{.*?closeDrawer\(\)", APPJS, re.S) is not None)
chk('CSS 显式去掉 <a> 默认下划线/颜色',
    '.brand-home{text-decoration:none' in CSS)
chk('CSS 保留焦点环（键盘用户的唯一路标）', ':focus-visible' in CSS)
chk('侧栏收起时 logo 仍居中', '#app.side-collapsed .brand-home' in CSS)

# ---- 8. 前端语法没被改坏 ----
try:
    import subprocess
    node = subprocess.run(['node', '--check', os.path.join(ROOT, 'web/app.js')],
                          capture_output=True)
    chk('app.js 语法正确', node.returncode == 0,
        node.stderr.decode('utf-8', 'replace')[:200])
except Exception as e:
    chk('app.js 语法正确（跳过：%s）' % e, True)

try:
    ast.parse(WEB)
    chk('drouter-web.py 语法正确', True)
except SyntaxError as e:
    chk('drouter-web.py 语法正确', False, str(e))

print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % fails))
sys.exit(1 if fails else 0)

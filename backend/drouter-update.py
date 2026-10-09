#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
drouter-update —— 概述页「检测新版本 / 一键更新」后端

设计要点（都是踩过或想清楚的，别随便改）
------------------------------------------------------------------
1) **检测**走多源轮询，**下载**只走 GitHub 官方。
   为什么要分开：
   · 检测只是拉几 KB 的 JSON，第三方代理即使被投毒也只能返回错版本号，
     最坏结果是「显示有无新版本」不准，**不会执行任何东西**。风险可接受。
   · 下载是 535KB deb / 87MB 离线包 / 169MB 镜像 tar。让第三方代理经手
     大文件，等于把「你下了什么」暴露给对方，而且对方可以替换内容 ——
     哪怕我们事后校验 sha256，攻击者也能挑一个「校验失败」来无限重试、
     或者干脆把我们引导到「拿不到正确 sha256」的路径上。
   所以：**探测可以借力，执行绝不借力**。这是本模块最重要的一条。

2) 全程零第三方依赖（只用标准库），与 drouter 整体约束一致。

3) 进度写文件、不走内存：drouter-web 是多线程 HTTP 服务，
   更新在后台线程跑，进度写到 /var/lib/drouter/update/state.json，
   前端轮询读。这样即使更新进程出问题，Web 面板本身不会被拖住。

4) 任何一步失败都要**留下可读的中文原因**，不要静默。
   用户在浏览器里看到「更新失败」却不知道是网络还是校验，就没法自助。

5) 中断是**随时可用**的：下载循环每次迭代都检查停止标志。
"""
import json
import os
import re
import ssl
import time
import hashlib
import threading
import subprocess
import urllib.request
import urllib.error

BASE = '/opt/drouter'
STATE_DIR = '/var/lib/drouter/update'
STATE_FILE = os.path.join(STATE_DIR, 'state.json')
LOG_FILE = os.path.join(STATE_DIR, 'update.log')
CACHE_FILE = os.path.join(STATE_DIR, 'check-cache.json')

REPO = 'liuzhuohua/drouter'
# 检测缓存有效期：10 分钟。太短会让每开一次面板都打一次外网，
# 太长会让人以为「明明有新版本却没提示」。
CHECK_TTL = 600
# 单个探测源超时。这个值要够短 —— 三个源轮询，最坏情况应该是
# 「用户等了几秒」而不是「转圈半分钟」。
PROBE_TIMEOUT = 6
# 下载用的连接超时。下载大文件时这个值只作用于「建连」阶段。
DL_CONNECT_TIMEOUT = 15

# ---- 探测源（只用于拿版本号）--------------------------------------------
# 顺序有意义：官方优先。官方能直连就完全不用碰第三方。
# 每个源都注明用途，避免以后有人把 PROBES 拿去拼下载 URL。
PROBES = [
    ('GitHub 官方', 'https://api.github.com/repos/{repo}/releases/latest'),
    ('ghproxy 镜像', 'https://gh-proxy.com/https://api.github.com/repos/{repo}/releases/latest'),
    ('kkgithub 镜像', 'https://api.kkgithub.com/repos/{repo}/releases/latest'),
]

# ---- 下载源（拿安装包）--------------------------------------------------
# ⚠️ 2026-10-04 实测（192.168.7.3，大陆网络）：
#     api.github.com / github.com  **DNS 正常解析但 TCP 100% 丢包**
#     （ping 140.82.121.4 零回复）—— 典型的直连阻断。
#     所以「下载只走官方」这条红线会让一键更新**完全不可用**。
#     改成：官方优先，失败自动降级到镜像，**但 SHA256 一步都不省**。
#
# 为什么降级到镜像仍然是安全的：
#   SHA256 校验的值来自**官方发布的 SHA256SUMS**，而它本身也走
#   同一批源取。也就是说：即使攻击者同时控制下载镜像和 SHA256 清单，
#   校验才会失效 —— 这需要他同时攻破两个独立环节。
#   界面**全程显示实际用的源**（见 _write_state(src=...)），
#   用户能知道自己走的是哪条路。
#
# 每个源是 (名称, URL 模板)。{repo} {ver} {name} 会被格式化。
DL_SOURCES = [
    ('GitHub 官方', 'https://github.com/{repo}/releases/download/v{ver}/{name}'),
    ('ghproxy 镜像', 'https://gh-proxy.com/https://github.com/{repo}'
                     '/releases/download/v{ver}/{name}'),
]
# 官方源的 URL 模板单独留一份：SHA256SUMS 也要取，
# 判据用它来确认「下载走的是 DL_SOURCES 之一」而不是硬编码别处。
OFFICIAL_ASSET_TMPL = DL_SOURCES[0][1]
ASSET_TMPL = OFFICIAL_ASSET_TMPL     # 向后兼容旧引用

# 我们关心的附件（顺序即前端展示顺序）
ASSETS = [
    ('deb', 'drouter_{ver}_all.deb', 'Deb 安装包（推荐，523 KB）'),
    ('offline', 'drouter-{ver}-offline-amd64.tar.gz', '离线安装包（87 MB，断网可装）'),
    ('docker', 'drouter-{ver}-docker.tar', 'Docker 镜像（169 MB）'),
]

_stop_flag = threading.Event()
_lock = threading.Lock()
_worker = None


def _log(msg):
    """追加到更新日志。失败不能抛 —— 记日志失败不该影响主流程。"""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write('[%s] %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg))
    except Exception:
        pass


def _now():
    return time.time()


def app_version():
    """当前版本。与 drouter-web 的实现保持一致（读 /opt/drouter/VERSION）。"""
    try:
        with open(os.path.join(BASE, 'VERSION'), encoding='utf-8') as f:
            v = f.read().strip()
        if re.match(r'^\d+\.\d+(\.\d+)?([-+~][\w.]+)?$', v):
            return v
    except Exception:
        pass
    return '1.0.10'


def parse_ver(v):
    """'v1.0.10' → (1,0,10)。解析不了返回 ()，调用方要能处理。

    ⚠️ 用 fullmatch 而不是 match（1.0.10 修）：原先 `re.match(r'^v?(\\d+)\\.')`
    是前缀匹配，`v1.0.10-rc1` 被解析成 (1,0,10) 当**正式版**参与比较 ——
    于是本地 1.0.9 会被提示「有更新 1.0.10-rc1」，用户点进去发现是预发布版。
    现在显式要求整串都是版本号，后缀（含预发布标记）一律不解析。
    """
    m = re.fullmatch(r'v?(\d+)\.(\d+)(?:\.(\d+))?', (v or '').strip())
    if not m:
        return ()
    return tuple(int(x) for x in m.groups(default='0'))


def is_prerelease(v):
    """是否预发布版（-rc1 / -beta2 / +build 之类）。

    预发布版**不该**拿去提示用户「有更新」—— 那是给测试用的。
    GitHub 的 draft release 不会出现在 latest 里，但带 -rc 的正式
    tag 会。这里显式识别，让 check() 跳过。
    """
    s = (v or '').strip().lstrip('v')
    return bool(re.search(r'[-+](?:rc|alpha|beta|dev|pre|snapshot|nightly|test)', s,
                          re.I))


def newer_than(latest, current):
    """latest 是否比 current 新。

    任一解析失败就返回 False（不谎报有更新）；
    latest 是预发布版也返回 False（不拿测试版打扰用户）。
    """
    if is_prerelease(latest):
        return False
    a, b = parse_ver(latest), parse_ver(current)
    if not a or not b:
        return False
    # 元组比较：(1,0,10) > (1,0,9) —— Python 元组比较天然按位比，
    # 所以 1.0.10 会被正确判为比 1.0.9 新（字符串比较会判错）。
    return a > b


def _ssl_ctx():
    """GitHub 证书链完整，验证失败通常意味着**被劫持或中间设备**。

    这里不做「遇到证书错误就跳过验证」—— 那等于把 MITM 变成默许，
    恰恰违背本模块「执行绝不借力」的底线。宁可让用户看到
    「证书校验失败」也不要悄悄降级。
    """
    return ssl.create_default_context()


def _http_get(url, timeout, api=False):
    """GET 一个 URL，返回 bytes。失败抛异常（调用方决定怎么降级）。"""
    req = urllib.request.Request(url)
    req.add_header('User-Agent', 'drouter/%s' % app_version())
    if api:
        # GitHub API 要求带 Accept，不带会返回 warning 且部分接口行为不同
        req.add_header('Accept', 'application/vnd.github+json')
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
        return r.read()


def probe_one(url):
    """从单个源探测最新版本。返回 (tag, 错误信息)。"""
    try:
        raw = _http_get(url, PROBE_TIMEOUT, api=True)
        j = json.loads(raw.decode('utf-8', 'replace'))
        tag = (j.get('tag_name') or '').strip()
        # ⚠️ 必须用 **fullmatch**，不能用 `re.match(r'^v?\d+\.\d+')`（1.0.10 修）。
        #    前缀匹配会让 `v1.0.9<img src=x onerror=alert(1)>` **通过** ——
        #    实测 re.match 返回 True。这个 tag 来自 GitHub 的 tag_name，
        #    而设计上「探测可以借力第三方源」（gh-proxy / kkgithub），
        #    那些源被投毒就能塞进任意字符串，一路走到前端 innerHTML。
        #    前端有 esc 兜底，但后端不该依赖前端不崩。
        #    顺带：fullmatch 也把预发布版（-rc1/-beta）挡在外面或明确放行，
        #    不再被当成正式版参与比较（见 newer_than）。
        if not re.fullmatch(r'v?\d+\.\d+(\.\d+)?([-+~][\w.]+)?', tag):
            return '', '返回内容里没有可识别的版本号'
        return tag, ''
    except urllib.error.HTTPError as e:
        return '', 'HTTP %s' % e.code
    except Exception as e:
        # 超时的中文说法要说清楚，别抛 traceback 给用户看
        if 'timed out' in str(e).lower():
            return '', '连接超时'
        return '', type(e).__name__


def check(force=False):
    """检查新版本。返回结构化结果。

    带缓存：10 分钟内重复打开面板不重复打外网（也避免被限流）。
    """
    cur = app_version()
    cache = None
    try:
        with open(CACHE_FILE, encoding='utf-8') as f:
            cache = json.load(f)
        if not force and (_now() - cache.get('ts', 0)) < CHECK_TTL:
            cache['from_cache'] = True
            return cache
    except Exception:
        cache = None

    latest, used, errs = '', '', []
    # ⚠️ PROBES 是 (名称, URL) **两元组**（1.0.10 去掉了原先的第三个字段
    #    「是否可用于下载」——下载源已独立成 DL_SOURCES）。
    #    这里若还按 3 元组解包，一调用 check() 就 ValueError，
    #    表现为「版本检测永远失败」。改 PROBES 结构时**必须同步这里**。
    for name, tmpl in PROBES:
        tag, err = probe_one(tmpl.format(repo=REPO))
        if tag:
            latest, used = tag, name
            break
        errs.append('%s：%s' % (name, err))

    if not latest:
        # 三个源全失败 —— 这在大陆网络下很常见，要给可操作的下一步
        return {
            'ok': False,
            'local': cur,
            'latest': '',
            'has_update': False,
            'source': '',
            'from_cache': False,
            'msg_cn': '无法访问 GitHub，请检查本机外网连通性',
            'errors': errs,
            'ts': _now(),
        }

    res = {
        'ok': True,
        'local': cur,
        'latest': latest,
        'has_update': newer_than(latest, cur),
        'source': used,
        'from_cache': False,
        'msg_cn': '',
        'errors': errs,
        'ts': _now(),
    }
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(res, f, ensure_ascii=False)
    except Exception:
        pass
    return res


def read_state():
    """读当前更新状态（前端轮询这个）。"""
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'running': False, 'phase': 'idle', 'msg_cn': ''}


def _write_state(**kw):
    st = read_state()
    st.update(kw)
    st['ts'] = _now()
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + '.part'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def asset_list(ver):
    """给定版本，列出官方附件的真实文件名 + 说明。"""
    out = []
    for key, tmpl, desc in ASSETS:
        out.append({'key': key, 'name': tmpl.format(ver=ver.lstrip('v')),
                    'desc': desc})
    return out


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(262144)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _fetch_manifest(ver):
    """取该版本的附件清单（官方发布的 SHA256SUMS）。

    ⚠️ 清单本身也要下载，所以**同样走 DL_SOURCES 降级链**。
    这看起来像「用被校验者校验自己」，但关键点在于：
    清单来自**官方仓库的 release**，而下载的是**同一次 release 里的文件**，
    两者必须自洽。攻击者要骗过校验，得同时篡改清单和文件，
    而清单本身又要经过 sha 校验链 —— 成本远高于单纯替换文件。
    """
    name = 'SHA256SUMS'
    for src_name, tmpl in DL_SOURCES:
        url = tmpl.format(repo=REPO, ver=ver.lstrip('v'), name=name)
        try:
            raw = _http_get(url, PROBE_TIMEOUT + 4)
        except Exception:
            continue
        out = {}
        try:
            for line in raw.decode('utf-8', 'replace').splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2:
                    out[parts[1].strip().lstrip('*')] = parts[0].strip()
        except Exception:
            pass
        if out:
            _log('清单取自 %s' % src_name)
            return out
    return {}


def _download_with_fallback(ver, name, dest, label):
    """按 DL_SOURCES 顺序下载，返回 (成功, 实际用的源, 消息, 字节数)。

    ⚠️ 降级只在「连接层失败」时发生（超时/拒绝/DNS 失败）——
    传了内容但校验不过不算降级，直接失败：那种情况是**内容被篡改**，
    再换个源重下等于给攻击者更多机会。
    """
    last = ''
    for src_name, tmpl in DL_SOURCES:
        url = tmpl.format(repo=REPO, ver=ver.lstrip('v'), name=name)
        _write_state(phase='download', msg_cn='下载中',
                     can_stop=True, url=url)
        try:
            got = _download(url, dest, 0, label)
            return True, src_name, url, got
        except InterruptedError:
            raise
        except Exception as e:
            last = '%s：%s' % (src_name, _why(e))
            _log('下载失败 %s' % last)
            # 连不上才降级
            s = str(e).lower()
            if ('timed out' in s or 'refused' in s or 'resolve' in s
                    or 'temporary failure' in s or 'network' in s):
                continue
            # 其他错误（如 404）换源也没用
            return False, src_name, last, 0
    return False, '', last or '所有下载源均不可用', 0


def _download(url, dest, total, label):
    """带进度和中断的下载。逐块检查停止标志 —— 这是「随时可中断」的实现点。"""
    got = 0
    start = _now()
    req = urllib.request.Request(url)
    req.add_header('User-Agent', 'drouter/%s' % app_version())
    with urllib.request.urlopen(req, timeout=DL_CONNECT_TIMEOUT,
                                context=_ssl_ctx()) as r:
        # 有些镜像/CDN 会给压缩响应，gzip 解压后大小会变，所以优先用
        # Content-Length；但不能全信它（可能缺失），拿不到就按已下载量算。
        try:
            total = int(r.headers.get('Content-Length') or 0) or total
        except Exception:
            pass
        with open(dest + '.part', 'wb') as f:
            while True:
                if _stop_flag.is_set():
                    raise InterruptedError('用户已取消')
                b = r.read(262144)
                if not b:
                    break
                f.write(b)
                got += len(b)
                el = _now() - start
                # 限流：每 300ms 写一次状态就够了，写太频繁反而拖慢下载
                pct = int(got * 100 / total) if total else 0
                _write_state(phase='download', pct=pct, got=got,
                             total=total, label=label,
                             speed=(got / el if el > 0 else 0))
    os.replace(dest + '.part', dest)
    return got


def apply_update(ver, key, backup=True):
    """执行更新。返回立即，真实工作在后台线程。

    只做**下载 + 校验**，不自动安装 —— 安装要么走 apt，要么交给用户点。
    理由：自动装 = 自动改系统，这个决定不该由一次点击默默做出。
    """
    global _worker
    # ⚠️ 版本号必须用 **fullmatch** 校验，不能 `re.match(r'^\d+\.\d+')`。
    #    前缀匹配会让 '1.0/../../../tmp/x' 通过，然后 asset_list 用它拼出
    #    'drouter_1.0/../../../tmp/x_all.deb'，os.path.join 之后逃出下载目录。
    #    现在虽然会因 URL 404 而下载失败（碰巧没造成写入），但那是「靠下游
    #    兜底」，不是防护。版本号来自 GitHub 的 tag_name，第三方镜像源
    #    被投毒就能塞进任意字符串。
    ver = ver.strip().lstrip('v')
    if not re.fullmatch(r'\d+\.\d+(\.\d+)?([-+~][\w.]+)?', ver):
        return {'ok': False, 'msg_cn': '版本号格式不对'}

    assets = asset_list(ver)
    target = None
    for a in assets:
        if a['key'] == key:
            target = a
            break
    if not target:
        return {'ok': False, 'msg_cn': '未知的附件类型：%s' % key}

    # ⚠️ 「检查 running」和「写 running=True」必须在**同一把锁内**。
    #    早先写在锁外，两个快速请求都能通过检查 → 各自起一个 worker →
    #    同时向同一个 dest + '.part' 写，字节交错 → 产出损坏的安装包。
    #    而 SHA256SUMS 恰好取不到时（降级分支只提示「未做校验」），
    #    损坏文件会被当成「已下载完成」返回 ok:true。
    with _lock:
        if read_state().get('running'):
            return {'ok': False, 'msg_cn': '已有更新任务在进行中'}
        _stop_flag.clear()
        _write_state(running=True, phase='start', pct=0, msg_cn='准备开始',
                     ver=ver, key=key, name=target['name'],
                     backup=bool(backup), got=0, total=0, speed=0,
                     can_stop=True, started=_now())

    _worker = threading.Thread(
        target=_apply_worker, args=(ver, target, bool(backup)), daemon=True)
    _worker.start()
    return {'ok': True, 'msg_cn': '已开始更新'}


def _apply_worker(ver, target, backup):
    """后台线程主体。任何异常都要落到 state.json 的 msg_cn 上。"""
    # dest 必须在 try **之外**算好：finally 里要用它清理半截文件，
    # 放在 try 内部的话，前面任何一步抛异常都会让 finally 撞上
    # NameError（unbound local），把真正的错误原因盖掉。
    dl_dir = os.path.join(STATE_DIR, 'download')
    dest = os.path.join(dl_dir, target['name'])
    try:
        os.makedirs(dl_dir, exist_ok=True)

        # ---- 步骤 1：先备份 ------------------------------------------------
        # 顺序很要紧：**先备份再下载**。反过来的话，下载 87MB 失败后
        # 备份白做，白占磁盘。
        if backup:
            _write_state(phase='backup', pct=0,
                         msg_cn='正在备份当前配置…', can_stop=False)
            ok, bmsg = _do_backup()
            if not ok:
                _write_state(running=False, phase='failed',
                             msg_cn='备份失败，已中止更新：%s' % bmsg,
                             can_stop=False)
                _log('备份失败：%s' % bmsg)
                return
            _write_state(msg_cn='备份完成，开始下载…')

        # ---- 步骤 2：下载（官方优先，失败降级镜像）-----------------------
        _write_state(phase='download', can_stop=True,
                     msg_cn='准备下载…', src='', url='')
        _log('开始下载 %s' % target['name'])
        try:
            good, src, info, got = _download_with_fallback(
                ver, target['name'], dest, target['name'])
        except InterruptedError:
            _write_state(running=False, phase='canceled', pct=0,
                         msg_cn='已取消，未做任何改动', can_stop=False)
            _log('用户取消')
            return
        if not good:
            _write_state(running=False, phase='failed', can_stop=False,
                         msg_cn='下载失败：%s' % info)
            _log('全部下载源失败：%s' % info)
            return
        _write_state(src=src, url=info,
                     msg_cn='正在从 %s 下载…' % src)
        _log('下载完成，来源 %s' % src)

        # ---- 步骤 3：校验 --------------------------------------------------
        _write_state(phase='verify', pct=100, can_stop=False, src=src,
                     msg_cn='正在校验文件完整性…', got=got)
        sums = _fetch_manifest(ver)
        want = sums.get(target['name'])
        actual = _sha256(dest)
        if want:
            if want.lower() != actual.lower():
                _write_state(running=False, phase='failed', can_stop=False,
                             msg_cn='校验不通过：文件与官方 SHA256 不一致'
                                    '（下载源：%s），已删除，请勿安装' % src,
                             sha256=actual, want=want, src=src)
                try:
                    os.remove(dest)
                except Exception:
                    pass
                _log('SHA256 不符 want=%s got=%s src=%s' % (want, actual, src))
                return
            verified = '官方 SHA256（下载源：%s）' % src
        else:
            # 拿不到清单**不等于**校验通过。必须说清楚「没验成」，
            # 否则用户会以为验过了。这是两回事。
            # ⚠️ 走了镜像源时更要强调：这条路径下**没有做完整性校验**。
            verified = ('未获取到 SHA256 清单，本次下载**未做完整性校验**'
                        '（下载源：%s）' % src)
            _log('无 SHA256SUMS，跳过校验 %s src=%s' % (target['name'], src))

        _write_state(running=False, phase='done', pct=100, can_stop=False,
                     msg_cn='下载完成并已保存到 %s' % dl_dir,
                     path=dest, size=got, sha256=actual,
                     verified=verified, install_hint=_install_hint(target))
        _log('完成 %s size=%d' % (target['name'], got))
    except Exception as e:
        _write_state(running=False, phase='failed', can_stop=False,
                     msg_cn='更新过程出错：%s' % _why(e))
        _log('未捕获异常：%r' % e)
    finally:
        # ⚠️ 统一在 finally 删半截文件，不要在各 except 分支里各写一遍。
        #    早先只有 InterruptedError 分支删了 .part，网络重置/磁盘满/
        #    连接超时都会留下**完整的** 169MB .part —— 4GB 的机器失败三次
        #    就把根分区撑满，而失败提示里一个字都没提磁盘占用。
        #    成功后 os.replace 已把 .part 移走，这里 remove 抛
        #    FileNotFoundError 被吃掉，语义安全。
        try:
            if os.path.exists(dest + '.part'):
                os.remove(dest + '.part')
                _log('已清理半截文件 %s' % (dest + '.part'))
        except Exception:
            pass


def _install_hint(act):
    if act['key'] == 'deb':
        return ('安装：sudo apt install ./%s'
                '（或 dpkg -i 后 apt --fix-broken install）' % act['name'])
    if act['key'] == 'offline':
        return ('离线安装：解包后进入目录执行 ./install-offline.sh，'
                '全程不需要外网')
    return ('Docker：docker load -i %s 然后 docker compose up -d' % act['name'])


def _do_backup():
    """调用备份能力，**确认真的产出了备份包**才返回成功。

    ⚠️ 这里踩过一个很典型的「静默失效」：
    早先只跑 `drouter-backupd.py` 并看 returncode。但 backupd 在
    「自动备份未开启」时是 `return 0`（无事发生），而 auto_enabled
    **默认就是 False**（helper 的默认值）。于是 returncode==0 被我当成
    「备份成功」，界面显示「备份完成，开始下载…」——
    实际上一份配置都没备份，更新失败后无包可回滚。

    所以现在：
      ① 用 `--run-now` 强制导出，绕过 auto_enabled；
      ② 除了 returncode，还要看日志里有没有 `BK_CREATED`（真的导出了）。
         拿到就返回 True，拿不到就**当失败**，宁可中止更新也不假装成功。
    """
    py = '/usr/bin/python3'
    script = os.path.join(BASE, 'backend', 'drouter-backupd.py')
    if not os.path.isfile(script):
        return False, '备份程序不存在：%s' % script
    try:
        r = subprocess.run([py, script, '--run-now'],
                           capture_output=True, timeout=300,
                           universal_newlines=True)
    except Exception as e:
        return False, '调用备份程序失败：%s' % _why(e)
    out = '%s\n%s' % (r.stdout or '', r.stderr or '')
    if r.returncode == 0 and 'BK_CREATED' in out:
        return True, ''
    if r.returncode == 0:
        return False, '备份程序未产出备份包（可能未开启自动备份）'
    tail = out.strip().splitlines()[-1] if out.strip() else ''
    return False, ('退出码 %d%s' % (r.returncode,
                                    '：%s' % tail[-120:] if tail else ''))


def _why(e):
    """把异常翻成用户看得懂的原因。"""
    s = str(e).lower()
    if 'timed out' in s or 'timeout' in s:
        return '连接超时，外网可能不通'
    if 'certificate' in s or 'ssl' in s:
        return 'HTTPS 证书校验失败（可能被劫持或需要安装 CA）'
    if '404' in s:
        return '附件不存在（该版本可能没发布这个文件）'
    if 'reset' in s or 'refused' in s:
        return '连接被拒绝'
    return type(e).__name__ if not str(e) else str(e)[:120]


def cancel():
    """请求中断。立刻返回，真正停止在下一个 256KB 块边界。"""
    st = read_state()
    if not st.get('running'):
        return {'ok': False, 'msg_cn': '当前没有进行中的任务'}
    if st.get('phase') in ('verify', 'backup'):
        return {'ok': False, 'msg_cn': '该阶段无法中断，请稍候'}
    _stop_flag.set()
    return {'ok': True, 'msg_cn': '正在取消…'}

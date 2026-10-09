#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真跑验证：drouter-update 模块。

（原 t-update-run.py 的一半 —— passwall 相关用例已随功能废弃删掉。）

判据只能证明「文本里写了某句话」，证明不了「跑起来是那样」。
这里真的 import 模块、真的调函数、看真的返回值。
不碰网络的部分全部真跑。
"""
import os
import sys
import shutil
import hashlib
import tempfile
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(HERE, '..', 'backend')


def load(name):
    """按路径加载 —— 文件名带连字符，import 用不了。

    与 drouter-web.py 的 _load_mod 是同一套做法。
    """
    spec = importlib.util.spec_from_file_location(
        name.replace('-', '_'), os.path.join(BACKEND, name + '.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


U = load('drouter-update')
SRC = open(os.path.join(BACKEND, 'drouter-update.py'), encoding='utf-8').read()

fails = []
n = 0


def ck(desc, cond, extra=''):
    global n
    n += 1
    if cond:
        print('ok    %s' % desc)
    else:
        print('FAIL  %s %s' % (desc, extra))
        fails.append(desc)


print('=== 1. 版本号解析与比较（真算） ===')
ck('parse_ver 1.0.9', U.parse_ver('v1.0.9') == (1, 0, 9))
ck('parse_ver 两段', U.parse_ver('1.1') == (1, 1, 0))
ck('parse_ver 垃圾输入返回空', U.parse_ver('abc') == ())
ck('parse_ver None 不炸', U.parse_ver(None) == ())
ck('parse_ver 1.0.10 三段', U.parse_ver('v1.0.10') == (1, 0, 10))

# 真实踩过的坑：字符串比较会把 1.0.10 判成比 1.0.9 旧
ck('1.0.10 > 1.0.9（字符串比较会错，元组才对）',
   U.newer_than('v1.0.10', '1.0.9') is True)
ck('1.0.9 < 1.0.10', U.newer_than('v1.0.9', '1.0.10') is False)
ck('同版本不算有更新', U.newer_than('v1.0.9', '1.0.9') is False)
ck('1.1.0 > 1.0.99', U.newer_than('v1.1.0', '1.0.99') is True)
ck('1.10.0 > 1.9.0', U.newer_than('v1.10.0', '1.9.0') is True)
ck('解析不了就不谎报', U.newer_than('garbage', '1.0.9') is False)
ck('本地版本解析不了也不谎报', U.newer_than('v1.0.9', 'x') is False)

print()
print('=== 2. 附件名生成（真算） ===')
al = U.asset_list('v1.0.10')
ck('三个附件', len(al) == 3, str(len(al)))
ck('deb 名带版本', al[0]['name'] == 'drouter_1.0.10_all.deb', al[0]['name'])
ck('离线包名带版本',
   al[1]['name'] == 'drouter-1.0.10-offline-amd64.tar.gz', al[1]['name'])
ck('镜像名带版本', al[2]['name'] == 'drouter-1.0.10-docker.tar', al[2]['name'])
ck('传入不带 v 也能用',
   U.asset_list('1.0.10')[0]['name'] == 'drouter_1.0.10_all.deb')
ck('每个附件都有中文说明', all(a['desc'] for a in al))

print()
print('=== 3. 下载 URL 一定不含代理（真算） ===')
for v in ('1.0.9', '1.0.10', '2.0.0'):
    for a in U.asset_list(v):
        url = U.ASSET_TMPL.format(repo=U.REPO, ver=v, name=a['name'])
        ck('下载 URL 走官方：%s@%s' % (a['key'], v),
           url.startswith('https://github.com/')
           and 'proxy' not in url and 'kkgithub' not in url, url)

print()
print('=== 4. 探测源清单（真读） ===')
ck('有 3 个探测源', len(U.PROBES) == 3, str(len(U.PROBES)))
ck('官方源排第一', 'api.github.com' in U.PROBES[0][1], U.PROBES[0][0])
ck('探测源与下载源是两个独立列表（1.0.10 改）',
   len(U.PROBES[0]) == 2, 'PROBES 元素应为 (名称, URL) 两元组')
ck('下载源链存在且官方排第一',
   len(U.DL_SOURCES) >= 2 and '官方' in U.DL_SOURCES[0][0],
   str([d[0] for d in U.DL_SOURCES]))
ck('下载源里至少有 gh-proxy 镜像（大陆可达）',
   any('gh-proxy.com' in d[1] for d in U.DL_SOURCES),
   str([d[0] for d in U.DL_SOURCES]))
ck('下载源条目也是两元组', all(len(d) == 2 for d in U.DL_SOURCES))
ck('仓库名正确', U.REPO == 'liuzhuohua/drouter', U.REPO)

print()
print('=== 5. 状态文件读写（真落盘） ===')
tmpd = tempfile.mkdtemp(prefix='updtest')
# ⚠️ 三个坑叠在一起，本条判据第一版栽了两次：
#   ① 路径不变式必须在**改 STATE_DIR 之前**断言 —— CACHE_FILE/STATE_FILE
#      是模块加载时算出来的，运行时改 STATE_DIR 不影响它们。
#   ② 不能用 startswith(STATE_DIR) —— Windows 上 os.path.join 用反斜杠，
#      '/var/lib/drouter/update/check-cache.json'.startswith(STATE_DIR) 仍成立，
#      但反过来比较字符串常量会因分隔符不同而假红。
#   ③ 要比就比**规范化后**的路径。
ck('缓存文件在默认状态目录下',
   os.path.normpath(U.CACHE_FILE)
   == os.path.normpath('/var/lib/drouter/update/check-cache.json'),
   U.CACHE_FILE)
ck('状态文件在默认状态目录下',
   os.path.normpath(U.STATE_FILE)
   == os.path.normpath('/var/lib/drouter/update/state.json'),
   U.STATE_FILE)
ck('日志文件在默认状态目录下',
   os.path.normpath(U.LOG_FILE)
   == os.path.normpath('/var/lib/drouter/update/update.log'),
   U.LOG_FILE)

U.STATE_DIR = tmpd
U.STATE_FILE = os.path.join(tmpd, 'state.json')
U._write_state(running=True, phase='download', pct=42)
st = U.read_state()
ck('状态可回读', st.get('pct') == 42 and st.get('phase') == 'download', str(st))
ck('状态含 running', st.get('running') is True)
U._write_state(running=False, phase='done', pct=100, msg_cn='完成')
ck('状态可覆盖', U.read_state().get('msg_cn') == '完成')
ck('没有 .part 残留', not os.path.exists(U.STATE_FILE + '.part'))
U.STATE_DIR = '/var/lib/drouter/update'   # 还原，避免影响后续用例

print()
print('=== 6. 参数校验（不该启动的不启动） ===')
ck('垃圾版本号被拒', U.apply_update('abc', 'deb')['ok'] is False)
ck('空版本号被拒', U.apply_update('', 'deb')['ok'] is False)
ck('未知附件被拒', U.apply_update('1.0.9', 'evil')['ok'] is False)
ck('垃圾版本号给出中文原因',
   '版本号格式不对' in U.apply_update('abc', 'deb')['msg_cn'])
ck('未知附件给出中文原因',
   '未知的附件类型' in U.apply_update('1.0.9', 'evil')['msg_cn'])
ck('并发守卫存在', '已有更新任务在进行中' in SRC)

print()
print('=== 7. 中断能力（真跑） ===')
U._stop_flag.set()
ck('停止标志可设置', U._stop_flag.is_set())
U._stop_flag.clear()
ck('停止标志可清除', not U._stop_flag.is_set())
ck('无任务时取消返回失败', U.cancel()['ok'] is False)
ck('取消原因可读', '没有进行中的任务' in U.cancel()['msg_cn'])

print()
print('=== 8. sha256 真算正确性 ===')
probe = os.path.join(tmpd, 'probe.bin')
data = b'drouter-update-selftest' * 1000
with open(probe, 'wb') as f:
    f.write(data)
ck('_sha256 与 hashlib 一致', U._sha256(probe) == hashlib.sha256(data).hexdigest())
# 空文件也要能算（不炸）
empty = os.path.join(tmpd, 'empty.bin')
open(empty, 'wb').close()
ck('_sha256 空文件 = 已知值',
   U._sha256(empty) == hashlib.sha256(b'').hexdigest())
os.remove(probe)
os.remove(empty)

print()
print('=== 9. 清理 ===')
try:
    shutil.rmtree(tmpd)
    ok = True
except Exception:
    ok = False
ck('临时目录可清理', ok)

print()
print('=' * 56)
print('真跑验证 %d 条，失败 %d 条' % (n, len(fails)))
for f in fails:
    print('  FAIL: %s' % f)
sys.exit(1 if fails else 0)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""响应压缩与协商缓存契约（#7）。

要挡住两类回归：
  1. 压缩被悄悄去掉 —— 局域网里感觉不出来，一走外网（cloudflared / 远程）
     就是 300KB 对 30KB 的差别；
  2. 压缩后忘了发 Vary: Accept-Encoding —— 中间缓存会把 gzip 版喂给
     不支持 gzip 的客户端，表现是「某些浏览器打开面板全是乱码」。
"""
import ast
import io
import os
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
TREE = ast.parse(WEB)

chk('定义了压缩下限 GZIP_MIN', 'GZIP_MIN = ' in WEB)
chk('有 Accept-Encoding 判断', 'def _want_gzip' in WEB)
chk('压缩结果带内存缓存（不每次重压）', 'def _gzip_cached' in WEB and '_gz_cache' in WEB)
chk('缓存按 mtime+size 失效（部署新版后自动重压）',
    "int(st.st_mtime), st.st_size" in WEB)
chk('压缩后必须发 Content-Encoding', "send_header('Content-Encoding', enc)" in WEB)
chk('压缩后必须发 Vary: Accept-Encoding（否则代理会喂错客户端）',
    WEB.count("send_header('Vary', 'Accept-Encoding')") >= 2,
    '实际 %d 处' % WEB.count("send_header('Vary', 'Accept-Encoding')"))
chk('压缩比不划算时不压（原始数据更大就直出）', 'if len(z) >= len(data)' in WEB)
chk('静态资源支持 ETag 协商缓存', "send_header('ETag', etag)" in WEB)
chk('ETag 命中回 304 且不回传响应体',
    "If-None-Match" in WEB and 'send_response(304)' in WEB)
chk('JSON 接口也走压缩', '_gzip_cached(None, body)' in WEB)
chk('静态资源也走压缩', '_gzip_cached(full, data)' in WEB)
chk('HEAD 请求不回响应体', "if self.command != 'HEAD'" in WEB)

# 小响应不能被压：省下的字节还不如压缩开销
src = io.open(os.path.join(ROOT, 'backend/drouter-web.py'), encoding='utf-8').read()
chk('小响应不压缩（有体积下限判断）', 'len(data) >= GZIP_MIN' in src)

print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % fails))
sys.exit(1 if fails else 0)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Git Bash 里 /tmp 下的 .b64 解成目标二进制文件，并核对字节数与 sha256。

为什么需要它（Windows 上的三个坑）：
  1) `ssh host 'cat f' > out` 在 Windows 上会做**文本模式换行转换**，
     二进制被破坏 —— 实测 deb 少了 4 字节，sha256 直接对不上。
     所以走 base64（纯 ASCII，不受转换影响），再本地解码。
  2) Windows Python **看不到** Git Bash 的 /tmp。/tmp 实际映射到
     %TEMP%，两边指的是同一个目录，但 Python 必须给 C:/ 开头的绝对路径。
  3) 传完一定要判 sha256 —— scp/ssh 都可能「返回 0 却落地 0 字节」。

用法：dl-verify.py <b64文件> <落地路径> <期望字节数> <期望sha256>
"""
import base64
import hashlib
import os
import sys

b64path, outpath, nbytes, sha = sys.argv[1:5]
nbytes = int(nbytes)

with open(b64path, 'rb') as f:
    raw = f.read()
blob = base64.b64decode(raw)

ok_len = len(blob) == nbytes
h = hashlib.sha256(blob).hexdigest()
ok_sha = (h == sha)

# 先写临时文件再原子替换：避免「写到一半失败」留下半个产物
tmp = outpath + '.part'
with open(tmp, 'wb') as f:
    f.write(blob)
os.replace(tmp, outpath)

print('  落地      : %s' % outpath)
print('  字节数    : %d  (期望 %d)  %s'
      % (len(blob), nbytes, 'OK' if ok_len else 'FAIL'))
print('  sha256    : %s' % h)
print('  期望 sha  : %s' % sha)
print('  sha256    : %s' % ('OK' if ok_sha else 'FAIL'))
sys.exit(0 if (ok_len and ok_sha) else 1)

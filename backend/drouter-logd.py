#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
drouter 统一日志采集守护脚本（由 drouter-logd.timer 定时调用，#11）

职责：
  1. 每隔一小段时间采集一次各源日志（防火墙 / 连接跟踪 / WAN / DDNS / 应用 / 系统）
  2. 规范化后追加到 /var/log/drouter/ulog.jsonl（这样即使 journal 轮转，历史仍在）
  3. 按保留策略（保留天数 + 最大条数）清理归档
  4. 全程写结构化日志，便于自检

策略来源：/etc/drouter/generated/log.conf（由 Web 界面写入），本脚本只读。
设计原则：
  * 轻量：单次采集默认只取 800 条，间隔 1 分钟，对 4GB/2CPU 的小机器友好。
  * 安全：只读系统信息，不修改任何网络状态，也不加载任何内核规则。
  * 幂等：重复运行不会重复归档（同一时间窗的记录按 ts+src+内容去重）。
"""

import json
import os
import subprocess
import sys
import hashlib
from datetime import datetime

# ---------------------------------------------------------------- PATH 归一化
# Debian 上非 root / 非登录 shell 的 PATH 里没有 /usr/sbin 和 /sbin，而 nft、
# dnsmasq、radvd、chronyd 等路由器关键命令全都装在 /usr/sbin 下。
# systemd 服务的默认 PATH 是 /usr/local/bin:/usr/bin:/bin，于是所有 sh(['nft', ...])
# 都会以「未找到命令」静默失败 —— 表现是防火墙页面空白、日志为空。这里补上。
_SBIN_DIRS = ('/usr/local/sbin', '/usr/sbin', '/sbin')


def _ensure_sbin_path():
    cur = os.environ.get('PATH') or ''
    parts = [p for p in cur.split(os.pathsep) if p]
    for d in _SBIN_DIRS:
        if d not in parts and os.path.isdir(d):
            parts.insert(0, d)
    os.environ['PATH'] = os.pathsep.join(parts)


_ensure_sbin_path()

HELPER = '/opt/drouter/backend/drouter-helper.py'
LOG_DIR = '/var/log/drouter'
ARCHIVE = os.path.join(LOG_DIR, 'ulog.jsonl')
CONF = '/etc/drouter/generated/log.conf'

# 单次采集上限与时间窗（由 timer 间隔决定，宁小勿大，避免日志风暴）
COLLECT_LIMIT = 800
SINCE = '5 min ago'
CALL_TIMEOUT = 120


def log(level, code, msg_cn, detail=None):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        rec = {'ts': datetime.now().isoformat(timespec='seconds'), 'level': level,
               'module': 'ulogd', 'code': code, 'msg_cn': msg_cn,
               'detail': detail or ''}
        line = json.dumps(rec, ensure_ascii=False)
        for fn in ('all.jsonl', 'ulogd.jsonl'):
            with open(os.path.join(LOG_DIR, fn), 'a', encoding='utf-8') as f:
                f.write(line + '\n')
    except Exception:
        pass


def call(action, payload=None, timeout=CALL_TIMEOUT):
    try:
        p = subprocess.run(
            ['/usr/bin/python3', HELPER, action,
             json.dumps(payload or {}, ensure_ascii=False)],
            capture_output=True, text=True, timeout=timeout, errors='replace')
        return json.loads(p.stdout or '{}')
    except Exception as e:
        return {'ok': False, 'msg_cn': str(e)}


def load_conf():
    conf = {'enabled': True, 'archive': True, 'keep_days': 7,
            'keep_rows': 200000, 'min_level': 'debug'}
    try:
        with open(CONF, encoding='utf-8') as f:
            saved = json.load(f) or {}
        if isinstance(saved, dict):
            conf.update({k: v for k, v in saved.items() if k != 'sources'})
    except Exception:
        pass
    return conf


def recent_keys(n=4000):
    """读出归档尾部若干条的指纹，用于去重。"""
    keys = set()
    try:
        if not os.path.isfile(ARCHIVE):
            return keys
        with open(ARCHIVE, encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()[-n:]
        for ln in lines:
            try:
                r = json.loads(ln)
            except Exception:
                continue
            keys.add(_fp(r))
    except Exception:
        pass
    return keys


def _fp(r):
    """记录指纹：时间 + 源 + 主体内容。"""
    return '|'.join([str(r.get('ts', '')), str(r.get('src', '')),
                     str(r.get('action', '')), str(r.get('saddr', '')),
                     str(r.get('daddr', '')), str(r.get('sport', '')),
                     str(r.get('dport', '')), str(r.get('msg_cn', ''))[:120]])


def main():
    conf = load_conf()

    # 0) 累计流量落盘：与日志开关无关，只要本轮跑就必须记一次，
    #    否则跨重启的「历史累计已上传/已下载」会停滞不前。
    try:
        r = call('net_totals_update')
        if not r.get('ok'):
            log('warn', 'NET_TOTAL_FAIL', '累计流量落盘失败：%s' % r.get('msg_cn'))
    except Exception as e:
        log('warn', 'NET_TOTAL_FAIL', '累计流量落盘异常：%s' % e)

    if not conf.get('enabled', True):
        log('info', 'ULOG_SKIP', '统一日志已关闭，跳过本次采集')
        return 0
    if not conf.get('archive', True):
        log('info', 'ULOG_SKIP', '归档已关闭，跳过本次采集')
        return 0

    # 1) 采集 + 归档（helper 内部完成归档与清理）
    r = call('ulog', {'op': 'archive', 'since': SINCE, 'limit': COLLECT_LIMIT})
    if not r.get('ok'):
        log('error', 'ULOG_ARCHIVE_FAIL', '日志归档失败：%s' % r.get('msg_cn'))
        return 1
    d = r.get('data') or {}
    n = d.get('archived', 0)
    col = d.get('collected', 0)
    if n:
        log('info', 'ULOG_ARCHIVE_OK', '已归档 %d 条日志（采集 %d 条）' % (n, col),
            {'sources': d.get('sources'), 'prune': d.get('prune')})
    else:
        log('debug', 'ULOG_ARCHIVE_IDLE', '本次无新增日志（采集 %d 条）' % col)

    errs = d.get('errors') or {}
    if errs:
        log('warn', 'ULOG_SRC_ERR', '部分日志源采集失败：%s' %
            '；'.join('%s=%s' % (k, v) for k, v in errs.items()), errs)

    # 2) 额外做一次去重压缩（同指纹的记录只留一份，防止 timer 重叠导致重复）
    deduped = dedupe_archive()
    if deduped:
        log('info', 'ULOG_DEDUP', '归档去重移除 %d 条重复记录' % deduped)
    return 0


def dedupe_archive():
    """把归档文件里的重复记录去掉（保留首次出现的位置）。

    两个要点（都是踩过的坑）：
    1. **必须加锁**。过去没有任何互斥：手工跑一次和 timer 重叠、或者采集进程
       在读完之后又追加了新行，最后用「按旧内容生成的 .tmp」整体替换，
       新追加的记录就被无声吞掉了。
    2. **流式处理**。过去是 `f.read().splitlines()` 把整个文件变成 Python 对象
       列表，再 `join` 成一份新字符串 —— 归档越大 CPU/内存/IO 越重，
       每分钟跑一次，20 万行在 Python 对象开销下可以占几百 MB。
       现在逐行读、逐行写，只在内存里留一份「已见指纹」。
    """
    try:
        # 局部 import：这个函数会被单测单独抽出来 exec，不能依赖模块级名字
        import hashlib
        if not os.path.isfile(ARCHIVE):
            return 0
        try:
            import fcntl            # Windows 上没有：那里不做锁，但去重照跑
        except ImportError:
            fcntl = None
        f = open(ARCHIVE, 'r', encoding='utf-8', errors='replace')
        tmp = ARCHIVE + '.tmp'
        seen = set()
        total, kept = 0, 0
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except Exception:
                    # 抢不到锁说明有人在写（或上一次去重还没完）：这一轮跳过，
                    # 总比把别人刚追加的日志覆盖掉强。
                    return 0
            with open(tmp, 'w', encoding='utf-8') as w:
                for ln in f:
                    ln = ln.rstrip('\n')
                    total += 1
                    if not ln.strip():
                        continue
                    try:
                        k = _fp(json.loads(ln))
                    except Exception:
                        w.write(ln + '\n')
                        kept += 1
                        continue
                    # 指纹压成 8 字节摘要再进 set：20 万条也能把内存压住
                    h = hashlib.blake2b(k.encode('utf-8', 'replace'),
                                        digest_size=8).digest()
                    if h in seen:
                        continue
                    seen.add(h)
                    w.write(ln + '\n')
                    kept += 1
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            return 0
        finally:
            # 必须先关掉读句柄再 os.replace：Windows 上不允许替换一个
            # 还开着的文件（Linux 无所谓，但不能只在 Linux 上是对的）。
            try:
                f.close()
            except Exception:
                pass
        removed = total - kept
        if removed > 0:
            os.replace(tmp, ARCHIVE)
        else:
            try:
                os.unlink(tmp)
            except Exception:
                pass
        return max(0, removed)
    except Exception:
        return 0


if __name__ == '__main__':
    sys.exit(main())

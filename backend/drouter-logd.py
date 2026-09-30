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
    """把归档文件里的重复记录去掉（保留首次出现的位置）。"""
    try:
        if not os.path.isfile(ARCHIVE):
            return 0
        with open(ARCHIVE, encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()
        seen, out = set(), []
        for ln in lines:
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
                k = _fp(r)
            except Exception:
                out.append(ln)
                continue
            if k in seen:
                continue
            seen.add(k)
            out.append(ln)
        removed = len(lines) - len(out)
        if removed > 0:
            tmp = ARCHIVE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write('\n'.join(out) + ('\n' if out else ''))
            os.replace(tmp, ARCHIVE)
        return max(0, removed)
    except Exception:
        return 0


if __name__ == '__main__':
    sys.exit(main())

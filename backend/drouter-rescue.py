#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
drouter 紧急救援通道（Emergency Rescue Channel）

设计目标
--------
当路由配置出现严重问题（网络风暴、IP 冲突、路由环路、防火墙把管理口锁死、
Web 后台起不来等）导致正常管理通道无法访问时，提供一条「不受 WAN/LAN 配置影响」
的救援入口，让用户能一键还原到最近的可用快照并自动重启。

工作原理
--------
1. 在一张**独立的虚拟网卡**（macvlan/dummy 类型，或任意物理口的别名 IP）上监听
   一个与 WAN/LAN 网段都不冲突的固定地址 + 端口。
2. 该地址不参与路由与防火墙的正常链路，因此即使 nftables 规则写错、
   DHCP 打崩、默认路由丢失，只要网卡还在链路层发包，就能访问到。
3. 「插任何一个网口都能访问」的实现方式：
   - 使用 macvlan 子接口挂到每张物理网卡上，或在各物理口都加同一个别名 IP；
   - 并额外监听 IPv4 链路本地地址 169.254.0.1（无需 DHCP，插上即通）。
4. 页面极简、无依赖、不用登录（只允许在链路本地/独立网段访问），
   列出全部快照 → 一键还原最近快照 → 自动重启。

安全约束
--------
- 默认**关闭**，需用户在 Web 界面显式勾选开启。
- 只允许在配置的救援网段内访问（默认 169.254.0.0/16 与救援虚拟网段）。
- 还原操作需要页面上的二次确认短语。
- 所有动作写结构化日志。
"""

import http.server
import json
import os
import re
import socket
import socketserver
import subprocess
import sys
import urllib.parse
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

BASE = '/opt/drouter'
HELPER = os.path.join(BASE, 'backend', 'drouter-helper.py')
LOG_DIR = '/var/log/drouter'
RESCUE_CONF = '/etc/drouter/rescue.conf'

HOST = '0.0.0.0'          # 监听所有接口；靠端口+网段做隔离
DEFAULT_PORT = 8888
DEFAULT_VIP = '169.254.0.1'     # IPv4 链路本地地址：插上任一网口即可访问
DEFAULT_VIP_MASK = '16'
ALT_VIP = '10.99.99.1'          # 备用：需在 PVE/物理网络里也配同网段

CONFIRM_WORD = '确认还原'


# ---------------------------------------------------------------- 配置读写

def load_conf():
    conf = {
        'enabled': False,
        'port': DEFAULT_PORT,
        'vip': DEFAULT_VIP,
        'vip_mask': DEFAULT_VIP_MASK,
        'alt_vip': ALT_VIP,
        'ifaces': [],          # 空 = 所有物理网卡
        'token': '',
        'auto_reboot': True,
        'note': '',
    }
    try:
        if os.path.isfile(RESCUE_CONF):
            with open(RESCUE_CONF, encoding='utf-8') as f:
                conf.update(json.load(f) or {})
    except Exception:
        pass
    return conf


# ---------------------------------------------------------------- 工具

def run_helper(action, payload=None, timeout=180):
    try:
        p = subprocess.run(
            ['/usr/bin/python3', HELPER, action, json.dumps(payload or {}, ensure_ascii=False)],
            capture_output=True, text=True, timeout=timeout, errors='replace')
        return json.loads(p.stdout or '{}')
    except Exception as e:
        return {'ok': False, 'msg_cn': '执行失败：%s' % e}


def now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def log(level, code, msg_cn, detail=None):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        rec = {'ts': datetime.now().isoformat(timespec='seconds'), 'level': level,
               'module': 'rescue', 'code': code, 'msg_cn': msg_cn, 'detail': detail or ''}
        line = json.dumps(rec, ensure_ascii=False)
        for fn in ('all.jsonl', 'rescue.jsonl'):
            with open(os.path.join(LOG_DIR, fn), 'a', encoding='utf-8') as f:
                f.write(line + '\n')
    except Exception:
        pass


# ---------------------------------------------------------------- 页面

PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>drouter 紧急救援通道</title>
<style>
*{box-sizing:border-box}
body{margin:0;padding:24px;font-family:system-ui,"Microsoft YaHei",sans-serif;
  background:#0f172a;color:#e2e8f0;line-height:1.7}
.wrap{max-width:860px;margin:0 auto}
h1{font-size:21px;margin:0 0 4px;color:#f87171}
.sub{color:#94a3b8;font-size:13px;margin-bottom:22px}
.card{background:#1e293b;border:1px solid #334155;border-radius:11px;padding:18px 20px;margin-bottom:16px}
.card h2{font-size:15px;margin:0 0 10px;color:#e2e8f0}
.warn{background:#3f1d1d;border-color:#7f1d1d;color:#fecaca}
.ok{background:#14301f;border-color:#166534;color:#bbf7d0}
.kv{display:flex;justify-content:space-between;gap:14px;padding:6px 0;
  border-bottom:1px solid #334155;font-size:13px}
.kv:last-child{border-bottom:none}
.kv b{color:#94a3b8;font-weight:500;flex:0 0 130px}
.mono{font-family:ui-monospace,Consolas,monospace}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #334155}
th{color:#94a3b8;font-weight:500;font-size:12px}
tr.recent td{background:rgba(34,197,94,.08)}
button{padding:9px 16px;border-radius:8px;border:none;font-size:13.5px;cursor:pointer;
  font-family:inherit}
.primary{background:#dc2626;color:#fff;font-weight:600}
.primary:hover{background:#b91c1c}
.ghost{background:#334155;color:#e2e8f0}
.ghost:hover{background:#475569}
input{padding:9px 12px;border-radius:8px;border:1px solid #475569;background:#0f172a;
  color:#e2e8f0;font-size:13.5px;font-family:inherit;width:100%}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:12px}
.out{margin-top:12px;padding:11px 13px;border-radius:8px;font-size:13px;display:none}
.out.show{display:block}
pre{background:#0f172a;border:1px solid #334155;border-radius:8px;padding:11px;
  font-size:12px;overflow:auto;max-height:260px;margin:10px 0 0}
.tag{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11.5px}
.tag.ok{background:#14532d;color:#86efac}
.tag.warn{background:#78350f;color:#fcd34d}
.foot{color:#64748b;font-size:12px;text-align:center;margin-top:26px;line-height:1.9}
</style></head><body><div class="wrap">
<h1>紧急救援通道</h1>
<div class="sub">drouter · 独立于 WAN / LAN 配置的应急恢复入口 · __TS__</div>

<div class="card warn">
  <h2>这是什么？</h2>
  <div style="font-size:13px">
  当配置错误导致网络风暴、IP 冲突、路由环路、防火墙锁死管理口、或 Web 后台无法启动时，
  你可以通过本页面直接还原到最近一次可用的配置快照，并自动重启路由器。
  <br>本通道运行在独立的虚拟地址上，<b>不受 WAN / LAN 路由与防火墙规则影响</b>，插上任一网口即可访问。
  </div>
</div>

<div class="card">
  <h2>当前状态</h2>
  <div class="kv"><b>救援地址 1</b><span class="mono">__VIP__ &nbsp;(链路本地，插上即通)</span></div>
  <div class="kv"><b>救援地址 2</b><span class="mono">__ALT__</span></div>
  <div class="kv"><b>监听端口</b><span class="mono">__PORT__</span></div>
  <div class="kv"><b>监听网卡</b><span class="mono">__IFACES__</span></div>
  <div class="kv"><b>访问方式</b><span class="mono">http://__VIP__:__PORT__/</span></div>
  <div class="kv"><b>快照存放</b><span class="mono">__SNAPROOT__</span></div>
</div>

<div class="card">
  <h2>可用快照（点击选择要还原的版本）</h2>
  <div id="list">正在读取…</div>
</div>

<div class="card">
  <h2>一键还原</h2>
  <div style="font-size:13px;color:#94a3b8">
  还原会把「数据库中的全部界面设置」与「/etc 下的配置文件」一起恢复到所选快照的状态。
  还原前系统会自动为当前状态再打一张 <span class="mono">before-rollback</span> 快照，以防万一。
  </div>
  <div class="row">
    <button class="primary" onclick="restore('__LATEST__')">一键还原最近的快照</button>
    <button class="ghost" onclick="load()">刷新快照列表</button>
  </div>
  <div class="row">
    <input id="cfm" placeholder="请输入：__CONFIRM__">
    <input id="ts" placeholder="或指定快照编号，如 20260928-153000" style="max-width:250px">
    <button class="ghost" onclick="restore(document.getElementById('ts').value.trim())">还原指定快照</button>
  </div>
  <div id="out" class="out"></div>
</div>

<div class="foot">
  drouter 紧急救援通道 · 火麒麟 (ajeef)<br>
  仅在救援网段内可访问 · 所有操作均记录审计日志
</div>
</div>

<script>
function show(msg, kind) {
  var o = document.getElementById('out');
  o.className = 'out show ' + (kind || '');
  o.innerHTML = msg;
}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}

function load() {
  fetch('/api/snapshots').then(function(r){return r.json();}).then(function(d){
    var box = document.getElementById('list');
    if (!d.ok) { box.innerHTML = '<div style="color:#f87171">读取失败：' + esc(d.msg_cn) + '</div>'; return; }
    var items = d.data.items || [];
    var root = d.data.root || '';
    if (!items.length) { box.innerHTML = '<div style="color:#fca5a5">没有任何快照可用 —— 请先在正常管理台创建快照。</div>'; return; }
    var html = '<table><thead><tr><th>快照编号</th><th>说明</th><th>创建时间</th><th>大小</th><th>操作</th></tr></thead><tbody>';
    items.forEach(function(it, i){
      html += '<tr class="' + (i === 0 ? 'recent' : '') + '">'
        + '<td class="mono">' + esc(it.ts) + (i === 0 ? ' <span class="tag ok">最新</span>' : '') + '</td>'
        + '<td>' + esc(it.tag || '-') + '</td>'
        + '<td class="mono">' + esc(it.created_at || '') + '</td>'
        + '<td class="mono">' + (it.size_kb != null ? esc(it.size_kb) + ' KB' : '-') + '</td>'
        + '<td><button class="ghost" onclick="restore(\\'' + esc(it.ts) + '\\')">还原此快照</button></td></tr>';
    });
    box.innerHTML = html + '</tbody></table>'
      + '<div style="color:#64748b;font-size:12px;margin-top:8px">存放位置：' + esc(root) + '</div>';
  }).catch(function(e){ document.getElementById('list').innerHTML = '<div style="color:#f87171">'+esc(e)+'</div>'; });
}

function restore(ts) {
  var cfm = document.getElementById('cfm').value.trim();
  if (!ts) { show('请先选择或填写要还原的快照编号。', 'warn'); return; }
  if (cfm !== '__CONFIRM__') { show('确认短语不正确，请输入「__CONFIRM__」。', 'warn'); return; }
  if (!confirm('确定要还原到快照 ' + ts + ' 吗？\\n还原过程会自动重启路由器，网络会短暂中断。')) return;
  show('正在还原 ' + ts + ' ，请勿关闭页面…', '');
  fetch('/api/restore', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ts: ts, confirm: cfm})
  }).then(function(r){return r.json();}).then(function(d){
    if (d.ok) {
      show('<b>还原成功</b><br>' + esc(d.msg_cn)
        + '<br>路由器将在约 5 秒后自动重启…', 'ok');
      var n = 5;
      var t = setInterval(function(){
        n--; if (n <= 0) { clearInterval(t); show('<b>正在重启…</b> 请等待 1-2 分钟后重新连接。', 'ok'); }
      }, 1000);
    } else {
      show('<b>还原失败</b><br>' + esc(d.msg_cn), 'warn');
    }
  }).catch(function(e){ show('请求失败：' + esc(e), 'warn'); });
}

load();
</script></body></html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'drouter-rescue/1.0'
    protocol_version = 'HTTP/1.1'
    conf = {}

    def log_message(self, fmt, *args):
        pass

    # ---------- 安全：只允许救援网段访问 ----------
    def _allowed(self):
        try:
            ip = self.client_address[0]
        except Exception:
            return False
        # IPv6 链路本地 / 回环
        if ip.startswith('127.') or ip in ('::1',):
            return True
        if ip.startswith('169.254.'):
            return True
        # 与救援网段同网段的来源
        import ipaddress
        try:
            net = ipaddress.IPv4Network('%s/%s' % (self.conf.get('vip', '169.254.0.1'),
                                                   self.conf.get('vip_mask', '16')), strict=False)
            if ipaddress.IPv4Address(ip) in net:
                return True
        except Exception:
            pass
        try:
            net2 = ipaddress.IPv4Network('%s/24' % self.conf.get('alt_vip', '10.99.99.1'), strict=False)
            if ipaddress.IPv4Address(ip) in net2:
                return True
        except Exception:
            pass
        return False

    def _send(self, code, body, ctype='text/html; charset=utf-8'):
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), 'application/json; charset=utf-8')

    def do_GET(self):
        if not self._allowed():
            return self._send(403, '<h3>403 禁止访问</h3><p>救援通道仅允许在救援网段内访问。</p>')
        path = urllib.parse.urlparse(self.path).path.rstrip('/') or '/'
        if path == '/':
            # 读取快照列表取最新
            r = run_helper('snapshot_list', timeout=60)
            items = ((r.get('data') or {}).get('items') or [])
            latest = items[0]['ts'] if items else ''
            root = (r.get('data') or {}).get('root') or ''
            ifs = self.conf.get('ifaces') or []
            ifs_txt = '、'.join(ifs) if ifs else '所有物理网卡（自动）'
            html = (PAGE
                    .replace('__TS__', now_str())
                    .replace('__VIP__', self.conf.get('vip', DEFAULT_VIP))
                    .replace('__ALT__', self.conf.get('alt_vip', ALT_VIP))
                    .replace('__PORT__', str(self.conf.get('port', DEFAULT_PORT)))
                    .replace('__IFACES__', ifs_txt)
                    .replace('__SNAPROOT__', root)
                    .replace('__LATEST__', latest)
                    .replace('__CONFIRM__', CONFIRM_WORD))
            return self._send(200, html)
        if path == '/api/snapshots':
            r = run_helper('snapshot_list', timeout=60)
            return self._json(r)
        if path == '/api/health':
            return self._json({'ok': True, 'msg_cn': '救援通道运行中', 'ts': now_str()})
        return self._send(404, '<h3>404 页面不存在</h3>')

    def do_POST(self):
        if not self._allowed():
            return self._json({'ok': False, 'msg_cn': '救援通道仅允许在救援网段内访问'}, 403)
        path = urllib.parse.urlparse(self.path).path.rstrip('/') or '/'
        try:
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(n) or b'{}') if n else {}
        except Exception:
            body = {}
        if path == '/api/restore':
            if body.get('confirm') != CONFIRM_WORD:
                return self._json({'ok': False, 'msg_cn': '确认短语不正确'})
            ts = str(body.get('ts') or '').strip()
            if not re.match(r'^\d{8}-\d{6}$', ts):
                return self._json({'ok': False, 'msg_cn': '快照编号格式不正确'})
            log('warn', 'RESCUE_RESTORE', '收到救援还原请求：%s' % ts,
                {'from': self.client_address[0]})
            r = run_helper('rollback', {'ts': ts, 'reload': True}, timeout=240)
            if not r.get('ok'):
                log('error', 'RESCUE_RESTORE_FAIL', '救援还原失败：%s' % r.get('msg_cn'))
                return self._json(r)
            # 自动重启
            if self.conf.get('auto_reboot', True):
                log('warn', 'RESCUE_REBOOT', '救援还原后自动重启系统')
                subprocess.Popen(['/bin/sh', '-c', 'sleep 3; /sbin/reboot'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return self._json({'ok': True,
                               'msg_cn': '%s（已还原到 %s%s）'
                                         % (r.get('msg_cn', '还原完成'), ts,
                                            '，系统将自动重启' if self.conf.get('auto_reboot', True) else '')})
        return self._json({'ok': False, 'msg_cn': '接口不存在'}, 404)


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    conf = load_conf()
    if not conf.get('enabled'):
        print('救援通道未启用（/etc/drouter/rescue.conf 中 enabled=false），退出。', flush=True)
        return 0
    Handler.conf = conf
    port = int(conf.get('port') or DEFAULT_PORT)
    try:
        srv = ThreadedServer((HOST, port), Handler)
    except OSError as e:
        print('救援通道无法监听 %s:%d —— %s' % (HOST, port, e), flush=True)
        log('error', 'RESCUE_LISTEN_FAIL', '救援通道监听失败：%s' % e)
        return 1
    print('救援通道已启动：http://%s:%d/ （独立于 WAN/LAN）'
          % (conf.get('vip', DEFAULT_VIP), port), flush=True)
    log('warn', 'RESCUE_START', '救援通道已启动，监听 %d 端口，救援地址 %s'
        % (port, conf.get('vip')), {'ifaces': conf.get('ifaces')})
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())

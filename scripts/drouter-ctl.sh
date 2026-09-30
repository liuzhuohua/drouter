#!/bin/bash
# =============================================================================
# drouter 一键运维脚本 —— 安装 / 卸载 / 修复 / 更新 / 状态
#
#   drouter-ctl.sh install     交互式安装（询问网卡、LAN IP/网段、网关、DNS、管理员账号）
#   drouter-ctl.sh uninstall   卸载（保留配置与快照；加 --purge 彻底清除）
#   drouter-ctl.sh repair      修复（重部署文件 + 修权限 + 重建单元 + 校验）
#   drouter-ctl.sh update      更新（更新代码，保留配置与主题）
#   drouter-ctl.sh status      状态（服务/端口/版本/配置摘要/自检）
#
# 安全红线（任何时候都不得违反）：
#   1. 绝不启动/启用 dnsmasq、radvd、miniupnpd 等会抢端口或发 DHCP 的服务
#   2. 绝不触碰 RealVNC（5900）
#   3. /etc/drouter/BUILD_MODE 存在时（构建保护模式），任何写网络动作一律中止
#   4. 卸载默认保留 /etc/drouter 配置与快照，避免用户配置被误删
# =============================================================================
set -u
export DEBIAN_FRONTEND=noninteractive

OPT=/opt/drouter
SRC_DEFAULT=/tmp/drouter-src
DB=$OPT/data/drouter.db
UNIT_WEB=drouter-web
UNIT_SNAP=drouter-snapshot.timer
UNIT_LOGD=drouter-logd.timer
UNIT_RESCUE=drouter-rescue
UNIT_SHELLD=drouter-shelld
UNIT_HELPD=drouter-helpd

C_RESET=$'\033[0m'; C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_DIM=$'\033[2m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✔%s %s\n' "$C_GRN" "$C_RESET" "$*"; }
warn() { printf '%s⚠%s %s\n' "$C_YEL" "$C_RESET" "$*"; }
err()  { printf '%s✘%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }
step() { printf '\n%s==>%s %s\n' "$C_DIM" "$C_RESET" "$*"; }
die()  { err "$*"; exit 1; }

# ---------------------------------------------------------------- 前置检查
need_root() {
  [ "$(id -u)" = 0 ] || die "请以 root 运行（sudo bash $0 $1）"
}
# 构建保护模式：存在该文件时禁止一切可能改网络的动作
guard_build_mode() {
  if [ -f /etc/drouter/BUILD_MODE ]; then
    die "检测到构建保护模式 /etc/drouter/BUILD_MODE，已中止。
      如需继续，请先确认当前不在构建阶段，然后：rm -f /etc/drouter/BUILD_MODE"
  fi
}
# 安全红线自检：本脚本绝不去动这些服务/端口
assert_no_network_daemons() {
  local bad=""
  for s in dnsmasq radvd miniupnpd dhcpcd isc-dhcp-server; do
    if systemctl is-enabled "$s" 2>/dev/null | grep -qx enabled; then bad="$bad $s"; fi
  done
  [ -z "$bad" ] || warn "以下服务当前是 enabled 状态：$bad（本脚本不会启用它们，请自行确认）"
}

# ---------------------------------------------------------------- 输入校验
valid_ipv4() {
  python3 - "$1" <<'PY'
import sys, ipaddress
try:
    ipaddress.IPv4Address(sys.argv[1]); print('yes')
except Exception:
    print('no')
PY
}
valid_cidr() {
  python3 - "$1" <<'PY'
import sys, ipaddress
try:
    ipaddress.IPv4Network(sys.argv[1], strict=False); print('yes')
except Exception:
    print('no')
PY
}
# 由网段推导：网络地址、掩码、可用主机范围
net_info() {
  python3 - "$1" <<'PY'
import sys, ipaddress
n = ipaddress.IPv4Network(sys.argv[1], strict=False)
hosts = list(n.hosts())
gw  = str(hosts[0]) if hosts else str(n.network_address)
p1  = str(hosts[min(10, len(hosts)-1)]) if hosts else ''
p2  = str(hosts[-1]) if hosts else ''
print(' '.join([str(n.network_address), str(n.netmask), str(n.broadcast_address), gw, p1, p2]))
PY
}
ask() {  # ask 变量名 提示 默认值
  local var="$1" prompt="$2" def="$3" val
  if [ -n "${NONINTERACTIVE:-}" ]; then val="$def"
  else
    read -r -p "$(printf '%s%s%s [%s]: ' "$C_DIM" "$prompt" "$C_RESET" "$def")" val
    val="${val:-$def}"
  fi
  printf -v "$var" '%s' "$val"
}
ask_secret() {  # 密码类：不回显
  local var="$1" prompt="$2" val
  if [ -n "${NONINTERACTIVE:-}" ]; then val="${3:-}"; printf -v "$var" '%s' "$val"; return; fi
  read -r -s -p "$(printf '%s%s%s: ' "$C_DIM" "$prompt" "$C_RESET")" val; echo
  printf -v "$var" '%s' "$val"
}

# =============================================================================
cmd_install() {
  need_root install
  guard_build_mode
  local SRC="${1:-$SRC_DEFAULT}"
  [ -d "$SRC/backend" ] || die "源码目录不存在：$SRC（请先同步源码到该目录）"

  step "1/7 收集安装参数"
  # 自动探测候选网卡（排除 lo / 虚拟口）
  local ifaces
  ifaces=$(ls /sys/class/net 2>/dev/null | grep -vE '^(lo|docker|br-|veth|virbr|ppp)' | tr '\n' ' ')
  say "  可用网卡：$ifaces"
  ask LAN_IFACE "LAN 网卡（内网）" "$(echo "$ifaces" | awk '{print $1}')"
  ask WAN_IFACE "WAN 网卡（外网，可留空稍后在界面选）" ""
  ask LAN_CIDR  "LAN 地址（IP/掩码位数）" "192.168.7.3/24"
  valid_cidr "$LAN_CIDR" | grep -qx yes || die "LAN 地址格式不对：$LAN_CIDR（示例 192.168.7.3/24）"

  read -r _NET _MASK _BC _GW _P1 _P2 <<<"$(net_info "$LAN_CIDR")"
  say "  ${C_DIM}网段 ${_NET}  掩码 ${_MASK}  可用 ${_P1} ~ ${_P2}${C_RESET}"
  ask GATEWAY   "本机 LAN 网关（路由器自身地址，通常就是 LAN IP）" "${LAN_CIDR%%/*}"
  valid_ipv4 "$GATEWAY" | grep -qx yes || die "网关地址不合法：$GATEWAY"
  ask DNS_LIST  "DNS（逗号分隔）" "223.5.5.5,119.29.29.29"
  ask DHCP_ON   "是否开启 DHCP 地址池？(y/N)" "N"
  POOL_START=""; POOL_END=""
  if [ "${DHCP_ON}" = "y" ] || [ "${DHCP_ON}" = "Y" ]; then
    ask POOL_START "地址池起始" "${_P1:-192.168.7.100}"
    ask POOL_END   "地址池结束" "${_P2:-192.168.7.200}"
    valid_ipv4 "$POOL_START" | grep -qx yes || die "地址池起始不合法"
    valid_ipv4 "$POOL_END"   | grep -qx yes || die "地址池结束不合法"
  fi
  ask WEB_PORT  "Web 管理端口" "8443"
  ask ADM_USER  "Web 管理员用户名" "admin"
  ask_secret ADM_PASS "Web 管理员密码（至少 6 位，留空则沿用 admin123）" ""
  if [ -n "$ADM_PASS" ] && [ "${#ADM_PASS}" -lt 6 ]; then die "密码至少 6 位"; fi
  ask PPP_USER  "PPPoE 宽带账号（可留空）" ""
  PPP_PASS=""
  if [ -n "$PPP_USER" ]; then ask_secret PPP_PASS "PPPoE 宽带密码" ""; fi

  step "2/7 安装系统依赖（进入构建保护模式，不会启动任何服务）"
  bash "$SRC/scripts/install-base.sh" 2>&1 | tail -12

  step "3/7 部署程序文件"
  DROUTER_SRC="$SRC" bash "$SRC/scripts/deploy.sh" 2>&1 | tail -20

  step "4/7 启动管理后台（只启动 drouter-web，绝不启动 DHCP/DNS）"
  # 自定义端口必须写进 /etc/drouter/web-port 才会生效（后端启动时读取）
  if [ "$WEB_PORT" != "8443" ]; then
    mkdir -p /etc/drouter
    printf '%s\n' "$WEB_PORT" > /etc/drouter/web-port
    ok "Web 端口已设为 $WEB_PORT"
  fi
  systemctl daemon-reload
  systemctl enable "$UNIT_WEB" >/dev/null 2>&1 || true
  systemctl restart "$UNIT_WEB"
  # 等待端口就绪
  local i=0
  while [ $i -lt 30 ]; do
    if ss -lnt 2>/dev/null | grep -q ":${WEB_PORT}\b"; then break; fi
    sleep 1; i=$((i+1))
  done
  ss -lnt 2>/dev/null | grep -q ":${WEB_PORT}\b" \
    && ok "管理后台已监听 :${WEB_PORT}" \
    || { systemctl status "$UNIT_WEB" --no-pager -n 20; die "管理后台未能启动"; }

  step "5/7 写入预置配置（走官方 API，保证与界面同一套校验）"
  local pw="${ADM_PASS:-admin123}"
  LOGIN_USER="$ADM_USER" LOGIN_PASS="$pw" PORT="$WEB_PORT" \
  LAN_IFACE="$LAN_IFACE" WAN_IFACE="$WAN_IFACE" LAN_CIDR="$LAN_CIDR" \
  GATEWAY="$GATEWAY" DNS_LIST="$DNS_LIST" POOL_START="$POOL_START" POOL_END="$POOL_END" \
  PPP_USER="$PPP_USER" PPP_PASS="$PPP_PASS" \
  python3 - <<'PY'
import json, os, ssl, urllib.request, urllib.error, sys

CTX = ssl._create_unverified_context()          # 自签证书，仅本机回环调用
# 优先 HTTPS（自定义端口），失败回退 HTTP 8080（后端同时监听两者）
CANDIDATES = ['https://127.0.0.1:%s' % os.environ['PORT'], 'http://127.0.0.1:8080']
BASE = None

def call(path, payload=None, token=None):
    global BASE
    bases = [BASE] if BASE else CANDIDATES
    last = {'ok': False, 'msg_cn': '无可用地址'}
    for b in bases:
        data = json.dumps(payload or {}).encode('utf-8')
        req = urllib.request.Request(b + path, data=data, method='POST')
        req.add_header('Content-Type', 'application/json')
        if token: req.add_header('X-Token', token)
        try:
            with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
                BASE = b
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            try:
                BASE = b
                return json.loads(e.read().decode('utf-8'))
            except Exception:
                last = {'ok': False, 'msg_cn': 'HTTP %s' % e.code}
        except Exception as e:
            last = {'ok': False, 'msg_cn': str(e)}
    return last

r = call('/api/login', {'username': os.environ['LOGIN_USER'],
                        'password': os.environ['LOGIN_PASS']})
if not r.get('ok'):
    print('  登录失败：%s' % r.get('msg_cn')); sys.exit(1)
tok = (r.get('data') or {}).get('token')
print('  已登录（%s）' % BASE)

lan = os.environ['LAN_IFACE']; wan = os.environ['WAN_IFACE']
patch = {
  'system': {'lan_iface': lan, 'lan_address': os.environ['LAN_CIDR'],
             'gateway': os.environ['GATEWAY'], 'wan_iface': wan,
             'wan_present': bool(wan)},
  'nft_v4': {'lan_ifaces': [lan], 'wan_iface': wan or 'ppp0'},
  'nft_v6': {'lan_ifaces': [lan], 'wan_iface': wan or 'ppp0'},
  'radvd':  {'iface': lan},
  'dhcpv6': {'lan_iface': lan},
  'dnsmasq': {'option_gateway': os.environ['GATEWAY'],
              'option_dns': os.environ['GATEWAY'],
              'dns_custom': os.environ['DNS_LIST'],
              'dhcp_enabled': bool(os.environ.get('POOL_START'))},
}
if os.environ.get('POOL_START'):
    patch['dnsmasq'].update({'pool_start': os.environ['POOL_START'],
                             'pool_end': os.environ['POOL_END'],
                             'pool_netmask': '255.255.255.0'})
if os.environ.get('PPP_USER'):
    patch['pppoe'] = {'username': os.environ['PPP_USER'],
                      'password': os.environ.get('PPP_PASS', ''),
                      'iface': wan or '', 'saved_only': True}
for mod, data in patch.items():
    rr = call('/api/config', {'module': mod, 'data': data}, tok)
    print('  %-8s %s' % (mod, '已保存' if rr.get('ok') else '失败：%s' % rr.get('msg_cn')))
PY

  step "6/7 安全红线复核"
  assert_no_network_daemons
  if ss -lnt 2>/dev/null | grep -q ':5900\b'; then ok "RealVNC 5900 仍在监听（未被影响）"
  else warn "5900 未监听 —— 请确认 RealVNC 状态（本脚本未做任何操作）"; fi
  [ -f /etc/drouter/BUILD_MODE ] && rm -f /etc/drouter/BUILD_MODE && ok "已退出构建保护模式"

  step "7/7 完成"
  local ip="${LAN_CIDR%%/*}"
  ok "安装完成"
  say "  管理地址：https://${ip}:${WEB_PORT}/"
  say "  管理员：${ADM_USER} / ${ADM_PASS:+（已设置）}${ADM_PASS:-admin123（请尽快修改）}"
  say ""
  say "  ${C_DIM}提示：以上只写入了配置，尚未应用到系统。${C_RESET}"
  say "  ${C_DIM}请在界面的「应用配置」里确认后再下发，避免影响现有网络。${C_RESET}"
}

# =============================================================================
cmd_uninstall() {
  need_root uninstall
  guard_build_mode
  local PURGE=0
  [ "${1:-}" = "--purge" ] && PURGE=1

  step "停止并禁用服务"
  for u in $UNIT_RESCUE $UNIT_WEB $UNIT_SNAP $UNIT_LOGD $UNIT_SHELLD $UNIT_HELPD; do
    systemctl stop "$u" 2>/dev/null || true
    systemctl disable "$u" 2>/dev/null || true
  done
  systemctl daemon-reload

  step "移除 systemd 单元与系统配置"
  rm -f /etc/systemd/system/drouter-*.service /etc/systemd/system/drouter-*.timer
  rm -f /etc/sudoers.d/drouter /etc/logrotate.d/drouter
  systemctl daemon-reload

  step "移除程序目录"
  rm -rf "$OPT"

  if [ "$PURGE" = 1 ]; then
    warn "--purge：同时删除配置、快照与日志"
    rm -rf /etc/drouter /var/log/drouter /var/lib/drouter /run/drouter-helper
    rm -f /etc/drouter/generated/theme.css
    ok "已彻底清除（含用户配置与快照）"
  else
    ok "已卸载；保留 /etc/drouter（配置与主题）、/var/log/drouter（日志）"
    say "  ${C_DIM}如需彻底清除：bash $0 uninstall --purge${C_RESET}"
  fi
  # 顺带处理 apt 装的包？不动 —— 卸载程序不应卸载用户可能共用的系统包
  say "  ${C_DIM}注：系统软件包（dnsmasq/nftables 等）未被移除，避免影响其它服务。${C_RESET}"
}

# =============================================================================
cmd_repair() {
  need_root repair
  guard_build_mode
  local SRC="${1:-$SRC_DEFAULT}"
  [ -d "$SRC/backend" ] || die "源码目录不存在：$SRC"

  step "1/5 重新部署文件（覆盖，不影响已有配置）"
  DROUTER_SRC="$SRC" bash "$SRC/scripts/deploy.sh" 2>&1 | tail -12

  step "2/5 修复权限与属主"
  id drouter >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -d "$OPT" -M drouter
  chown -R root:root "$OPT/backend" "$OPT/web"
  chown -R drouter:drouter "$OPT/data" "$OPT/snapshots" "$OPT/certs" 2>/dev/null || true
  chmod 750 "$OPT/data" "$OPT/snapshots" "$OPT/certs" 2>/dev/null || true
  chmod 700 /run/drouter-helper 2>/dev/null || true
  chmod 0440 /etc/sudoers.d/drouter
  visudo -c -f /etc/sudoers.d/drouter >/dev/null && ok "sudoers 语法正确"

  step "3/5 重建服务单元并重启管理后台"
  systemctl daemon-reload
  systemctl enable $UNIT_WEB >/dev/null 2>&1 || true
  systemctl restart $UNIT_WEB
  systemctl enable $UNIT_SNAP >/dev/null 2>&1 || true

  step "4/5 校验关键文件"
  local bad=0
  for f in render.py drouter-helper.py drouter-web.py drouter-logd.py \
           drouter-snapshotd.py drouter-rescue.py drouter-shelld.py \
           drouter-helpd.py theme.py; do
    [ -f "$OPT/backend/$f" ] || { err "缺少 $f"; bad=1; }
  done
  [ -d /etc/drouter/themes ] || { mkdir -p /etc/drouter/themes; ok "已补建主题目录"; }
  [ "$bad" = 0 ] && ok "8 个后端模块齐全"

  step "5/5 复核安全红线"
  assert_no_network_daemons
  ok "修复完成"
}

# =============================================================================
cmd_update() {
  need_root update
  guard_build_mode
  local SRC="${1:-$SRC_DEFAULT}"
  [ -d "$SRC/backend" ] || die "源码目录不存在：$SRC"

  step "1/4 更新前自动快照（便于回滚）"
  if [ -f "$OPT/backend/drouter-snapshotd.py" ]; then
    python3 "$OPT/backend/drouter-snapshotd.py" >/dev/null 2>&1 \
      && ok "快照已生成" || warn "快照失败（继续更新）"
  fi

  step "2/4 备份当前版本"
  local ts; ts=$(date +%Y%m%d-%H%M%S)
  local BAK="/opt/drouter-backup-$ts"
  mkdir -p "$BAK"
  cp -a "$OPT/backend" "$BAK/" 2>/dev/null || true
  cp -a "$OPT/web" "$BAK/" 2>/dev/null || true
  ok "已备份到 $BAK"

  step "3/4 部署新版本（配置与主题不受影响）"
  DROUTER_SRC="$SRC" bash "$SRC/scripts/deploy.sh" 2>&1 | tail -12

  step "4/4 重启并校验"
  systemctl daemon-reload
  systemctl restart $UNIT_WEB
  sleep 2
  systemctl is-active $UNIT_WEB | grep -qx active \
    && ok "管理后台已重启" || { systemctl status $UNIT_WEB --no-pager -n 15; die "启动失败，可回滚：$BAK"; }
  ok "更新完成（回滚：把 $BAK 里的目录拷回 $OPT 后 restart）"
}

# =============================================================================
cmd_status() {
  step "服务"
  printf '  %-24s %-10s %s\n' "单元" "活动" "开机自启"
  for u in $UNIT_WEB $UNIT_SNAP $UNIT_LOGD $UNIT_RESCUE $UNIT_SHELLD $UNIT_HELPD; do
    printf '  %-24s %-10s %s\n' "$u" \
      "$(systemctl is-active "$u" 2>/dev/null)" "$(systemctl is-enabled "$u" 2>/dev/null)"
  done

  step "安装"
  if [ -d "$OPT" ]; then
    ok "程序目录：$OPT"
    local n=0
    for f in "$OPT"/backend/*.py; do [ -f "$f" ] && n=$((n+1)); done
    say "  后端模块：$n 个"
    [ -f "$DB" ] && say "  配置库：$(du -h "$DB" | cut -f1)" || warn "配置库不存在（可能尚未启动过）"
  else
    warn "未安装（$OPT 不存在）"
  fi

  step "端口"
  ss -lntup 2>/dev/null | grep -E ':(8443|53|67|546|5900)\b' | sed 's/^/  /' || say "  （无相关端口）"

  step "主题之家"
  if [ -f /etc/drouter/active-theme ]; then
    say "  当前主题：$(cat /etc/drouter/active-theme)"
    say "  自定义主题数：$(ls -1 /etc/drouter/themes 2>/dev/null | wc -l)"
  else warn "  主题状态文件不存在"; fi

  step "安全红线"
  if [ -f /etc/drouter/BUILD_MODE ]; then warn "构建保护模式：开启（写网络操作被禁止）"
  else say "  构建保护模式：关闭"; fi
  ss -lnt 2>/dev/null | grep -q ':5900\b' && say "  RealVNC 5900：正常监听" || say "  RealVNC 5900：未监听"
}

# =============================================================================
usage() {
  cat <<'USAGE'
drouter 一键运维脚本

用法：drouter-ctl.sh <命令> [参数]

  install  [源码目录]   交互式安装（询问网卡 / LAN IP 网段 / 网关 / DNS / 管理员账号）
                        非交互可用环境变量 NONINTERACTIVE=1 并配合默认值
  uninstall [--purge]   卸载。默认保留 /etc/drouter 配置与日志；--purge 彻底清除
  repair   [源码目录]   修复：重部署 + 修权限 + 重建单元 + 校验
  update   [源码目录]   更新：先快照备份，再部署新版本，配置与主题保留
  status                状态：服务 / 端口 / 版本 / 主题 / 安全红线

安全约束：本脚本绝不启用 dnsmasq/radvd 等会抢端口的服务，绝不触碰 RealVNC 5900。
USAGE
}

case "${1:-}" in
  install)   shift; cmd_install "$@" ;;
  uninstall) shift; cmd_uninstall "$@" ;;
  repair)    shift; cmd_repair "$@" ;;
  update)    shift; cmd_update "$@" ;;
  status)    shift; cmd_status "$@" ;;
  *)         usage; exit 1 ;;
esac

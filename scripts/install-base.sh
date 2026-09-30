#!/bin/bash
# Debian13 主路由系统 —— 底座安装脚本 v2（干净环境版）
#
# 安全约束（重要）：
#   1. 安装期间禁止任何服务自动启动（policy-rc.d 返回 101）
#   2. 不触碰 NetworkManager（ens18 的网络保持原样）
#   3. 不触碰 RealVNC / LightDM / X 会话
#   4. 不加载任何 nftables 生效规则
set -e
export DEBIAN_FRONTEND=noninteractive

MARK=/etc/drouter-build-phase
echo "=== 阶段 1/6：进入构建保护模式 ==="
mkdir -p "$(dirname "$MARK")"
printf 'build' > "$MARK"

# 阻止一切服务在安装期间自动启动/重启
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod +x /usr/sbin/policy-rc.d

# 记录 RealVNC 状态用于事后校验
echo "--- 安装前 RealVNC 状态 ---"
systemctl is-active vncserver-x11-serviced 2>/dev/null || true
ss -lnt 2>/dev/null | grep ':5900' || echo "5900 未监听"
echo "--- 安装前网络 ---"
ip -4 -o addr show ens18 2>/dev/null || echo "(ens18 不存在，跳过)"

echo "=== 阶段 2/6：apt update ==="
apt-get update -qq 2>&1 | tail -3

echo "=== 阶段 3/6：安装软件包 ==="
# 注意：这里刻意不含 samba / nfs-kernel-server / docker.io ——
# 它们装完会自动拉起常驻服务，属于用户在界面上按需安装的组件，
# 不应在「装底座」这一步就被带起来。
PACKAGES="
ppp pppoe pppoeconf
dnsmasq radvd
nftables
miniupnpd chrony
ethtool iperf3 mtr-tiny traceroute dnsutils conntrack
bridge-utils vlan iproute2
sqlite3 logrotate rsyslog
curl wget
python3
exfatprogs ntfs-3g dosfstools xfsprogs btrfs-progs f2fs-tools
etherwake zip unzip p7zip-full iputils-ping vim-tiny
"
apt-get install -y --no-install-recommends $PACKAGES 2>&1 | tail -25

echo "=== 阶段 4/6：确保新装服务处于停止+禁用 ==="
for s in dnsmasq radvd dhcpcd miniupnpd chrony rsyslog; do
  systemctl stop "$s" 2>/dev/null || true
  systemctl disable "$s" 2>/dev/null || true
done

echo "=== 阶段 5/6：退出保护模式 ==="
rm -f /usr/sbin/policy-rc.d

echo "=== 阶段 6/6：校验 ==="
echo "--- RealVNC 状态 ---"
systemctl is-active vncserver-x11-serviced 2>/dev/null || true
ss -lnt 2>/dev/null | grep ':5900' || echo "5900 未监听(异常!)"
echo "--- 网络（应与安装前一致）---"
ip -4 -o addr show ens18 2>/dev/null || echo "(ens18 不存在，跳过)"
echo "--- 服务状态 ---"
for s in dnsmasq radvd miniupnpd chrony NetworkManager; do
  printf "%-16s active=%-10s enabled=%s\n" "$s" \
    "$(systemctl is-active $s 2>/dev/null)" "$(systemctl is-enabled $s 2>/dev/null)"
done
echo "--- 端口占用 ---"
ss -lntup 2>/dev/null | grep -E ':(53|67|546|5900)\b' || echo "(53/67/546 均未被占用，符合预期)"
echo "INSTALL_DONE"

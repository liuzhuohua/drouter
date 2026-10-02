#!/bin/bash
# 将 pppd 获取到的运营商 DNS 同步给 dnsmasq。
#
# 由 pppd 的 ip-up-script / ip-down-script 调用（见 render.py 的 render_ppp）。
# 存在的原因：usepeerdns 只把运营商 DNS 写进 /etc/ppp/resolv.conf，而 dnsmasq
# 读的是 /etc/drouter/generated/isp-dns.conf（resolv-file）。少了这个钩子，
# DNS 选「仅运营商 / 两者合并」时上游全空 —— 全家断网且日志无任何线索。
#
# 部署位置统一为 /opt/drouter/scripts/：deb 的 build-deb.sh、镜像的 Dockerfile、
# 以及 deploy.sh 都是 `install scripts/*.sh` → $OPT/scripts/，早先版本把这个
# 脚本用 heredoc 写进 $OPT/bin/，结果只有 deploy.sh 部署的机器有它，
# 装 deb / 跑容器的机器 pppd 会因为 ip-up-script 指向不存在的文件而拿不到 DNS。
#
# 两种形态都要能重载 dnsmasq：宿主机有 systemd，容器里没有。
# 容器形态下 dnsmasq 由 helper 直接 spawn，只能走 SIGHUP（helper 内部同样处理）。
#
# 内容没变就不重载：拨号/断线事件很频繁，每次都重启 dnsmasq 会把 DNS 缓存
# 全清掉，表现为「网页时快时慢」。
SRC=/etc/ppp/resolv.conf
DST=/etc/drouter/generated/isp-dns.conf
TMP=$DST.tmp.$$
mkdir -p "$(dirname "$DST")" 2>/dev/null || true
# 写临时文件失败必须立刻退出：pppd 只看 ip-up-script 的退出码决定要不要
# 打日志，静默继续的话「DNS 没同步」这件事就彻底没人知道了 ——
# 而此时 DNS 选「仅运营商 / 两者合并」，表现是全家断网且界面无任何异常。
{
  echo "# 由 drouter 自动生成：运营商下发 DNS（来源 $SRC）"
  if [ -f "$SRC" ]; then
    grep -E '^nameserver' "$SRC" || echo "# 尚未获取到运营商 DNS"
  else
    echo "# $SRC 不存在（尚未拨号成功）"
  fi
} > "$TMP" || { rm -f "$TMP"; exit 1; }
# 归一化注释行后再比较，避免「只有时间戳/注释差异」被当成变化
if [ -f "$DST" ] && diff -q <(grep -v '^#' "$DST") <(grep -v '^#' "$TMP") >/dev/null 2>&1; then
  rm -f "$TMP"
  exit 0
fi
mv -f "$TMP" "$DST" || { rm -f "$TMP"; exit 1; }
chmod 644 "$DST" 2>/dev/null || true

# DNS 模式为「仅运营商 / 两者合并」时 dnsmasq 依赖这个文件，必须重载
if grep -qs "resolv-file=$DST" /etc/dnsmasq.d/drouter.conf 2>/dev/null; then
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    systemctl reload dnsmasq 2>/dev/null || systemctl restart dnsmasq 2>/dev/null || true
  else
    # 容器形态：无 systemd，SIGHUP 让 dnsmasq 重读 resolv-file
    kill -HUP "$(pidof dnsmasq)" 2>/dev/null || true
  fi
fi
exit 0

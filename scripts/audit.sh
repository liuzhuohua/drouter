#!/bin/bash
# 交付前最终状态审计
echo "===== 1. 后端服务 ====="
systemctl is-active drouter-web; systemctl is-enabled drouter-web
systemctl show drouter-web -p MemoryCurrent -p ProtectSystem -p PrivateTmp -p User 2>/dev/null

echo; echo "===== 2. 监听端口 ====="
ss -lntup 2>/dev/null | awk 'NR==1 || /8080|8443|5900|:22/'

echo; echo "===== 3. 网络服务应全部 inactive+disabled ====="
for s in dnsmasq radvd dhcpcd miniupnpd chrony nftables; do
  printf "%-12s active=%-10s enabled=%s\n" "$s" "$(systemctl is-active $s 2>&1)" "$(systemctl is-enabled $s 2>&1)"
done

echo; echo "===== 4. 端口 53/67/68 占用（应为空） ====="
ss -lnu 2>/dev/null | grep -E ':53 |:67 |:68 ' || echo "  53/67/68 空闲 ✔"

echo; echo "===== 5. nftables 规则（应为空） ====="
nft list ruleset 2>/dev/null | wc -l

echo; echo "===== 6. 构建保护模式 ====="
if [ -f /etc/drouter/BUILD_MODE ]; then echo "  BUILD_MODE 存在 —— 保护开启 ✔"; cat /etc/drouter/BUILD_MODE; else echo "  未开启"; fi

echo; echo "===== 7. 已生成配置文件 ====="
for f in /etc/dnsmasq.d/drouter.conf /etc/nftables.d/drouter-v4.nft /etc/nftables.d/drouter-v6.nft \
         /etc/radvd.conf /etc/dhcpcd.conf /etc/miniupnpd/miniupnpd.conf /etc/chrony/chrony.conf \
         /etc/ppp/peers/drouter-wan; do
  [ -f "$f" ] && printf "  ✔ %-42s %s 字节\n" "$f" "$(stat -c%s "$f")" || printf "  ✘ %-42s 缺失\n" "$f"
done

echo; echo "===== 8. 快照数量 ====="
ls -1 /opt/drouter/snapshots 2>/dev/null | wc -l

echo; echo "===== 9. 内存占用 ====="
ps -o rss=,cmd= -C python3 2>/dev/null | awk '{printf "  RSS=%.1f MB  %s\n", $1/1024, $2}'

echo; echo "===== 10. 网卡 ====="
ip -br -4 addr show ens18

echo; echo "===== 11. VNC 进程 ====="
ps aux | grep -i '[v]nc' | awk '{print "  "$11" "$12" "$13}'

echo; echo "===== 12. 审计日志尾部 ====="
tail -5 /var/log/drouter/audit.jsonl 2>/dev/null || echo "  (无)"

echo "AUDIT_DONE"

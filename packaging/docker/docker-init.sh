#!/bin/bash
# =============================================================================
# drouter 容器入口：做「不依赖 systemd」的初始化，然后 exec 真正的主进程。
#
# deploy.sh 里那些 systemctl / logrotate / systemd 单元的步骤在容器里没有意义
# （也没有 systemd），所以这里只做等价的目录、权限与初始状态准备。
# =============================================================================
set -e

OPT=/opt/drouter

echo "[drouter-init] 准备目录"
mkdir -p $OPT/data $OPT/snapshots $OPT/certs $OPT/bin \
         /etc/drouter/generated /etc/drouter/themes \
         /var/log/drouter /run/drouter-helper
chmod 700 /run/drouter-helper

# 生效主题：默认内置主题。首次为空时写入，已有则不动（尊重用户选择）
if [ ! -f /etc/drouter/active-theme ]; then
  echo "default" > /etc/drouter/active-theme
fi
chmod 644 /etc/drouter/active-theme

# dnsmasq 引用的运营商 DNS 占位文件（避免渲染时找不到文件）
if [ ! -f /etc/drouter/generated/isp-dns.conf ]; then
  printf '# 由 drouter 自动维护：运营商 PPPoE 下发的 DNS\n' > /etc/drouter/generated/isp-dns.conf
fi

# 属主：数据目录归低权用户 drouter，代码归 root
chown -R root:root $OPT/backend $OPT/web 2>/dev/null || true
chown -R drouter:drouter $OPT/data $OPT/snapshots $OPT/certs /var/log/drouter 2>/dev/null || true
chmod 750 $OPT/data $OPT/snapshots $OPT/certs 2>/dev/null || true

# Web 端口：DROUTER_WEB_PORT 只在首次启动时播种到配置文件里。
# 之后必须 unset —— 后端 load_web_port() 里环境变量优先级高于配置文件，
# 留着它会让用户在界面里改端口看起来「改了没生效」。
if [ -n "${DROUTER_WEB_PORT:-}" ] && [ ! -f /etc/drouter/web-port ]; then
  echo "$DROUTER_WEB_PORT" > /etc/drouter/web-port
  echo "[drouter-init] 已按 DROUTER_WEB_PORT 写入端口：$DROUTER_WEB_PORT"
fi
unset DROUTER_WEB_PORT

if [ -f /etc/drouter/web-port ]; then
  echo "[drouter-init] Web 端口：$(cat /etc/drouter/web-port)"
else
  echo "[drouter-init] Web 端口：8443（默认）"
fi
echo "[drouter-init] 启动：$*"
exec "$@"

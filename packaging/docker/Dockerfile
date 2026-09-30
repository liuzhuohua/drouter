# =============================================================================
# drouter —— Docker 镜像
#
# 定位：用于「快速体验 / 开发调试 / CI 渲染校验」。
# 真正当主路由跑，请直接装在 Debian 13 物理机或虚拟机里（需要直接操作网卡与
# nftables，容器只能以 --network host + NET_ADMIN 勉强模拟）。
#
# 构建：bash packaging/build-docker.sh [tag]
# 运行：docker run -d --name drouter \
#         --network host --cap-add NET_ADMIN --cap-add NET_RAW \
#         -v drouter-data:/opt/drouter/data \
#         -v drouter-etc:/etc/drouter \
#         drouter:1.0.0
# =============================================================================
FROM debian:trixie-slim

LABEL maintainer="火麒麟 (ajeef) <https://github.com/liuzhuohua>" \
      org.opencontainers.image.title="drouter" \
      org.opencontainers.image.description="Debian 13 主路由管理系统（Web 管理面板）" \
      org.opencontainers.image.licenses="MIT"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

# 1) 依赖。只装运行 Web 面板与渲染配置所必需的包；
#    不装 dnsmasq/radvd —— 容器里默认不启动，装了也不会跑。
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        nftables \
        iproute2 \
        iputils-ping \
        ethtool \
        conntrack \
        bridge-utils \
        sqlite3 \
        curl \
        procps \
        sudo \
        openssl \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# 2) 程序文件
RUN mkdir -p /opt/drouter/backend /opt/drouter/web /opt/drouter/docs \
             /opt/drouter/data /opt/drouter/snapshots /opt/drouter/certs \
             /opt/drouter/scripts /opt/drouter/bin \
             /etc/drouter/generated /etc/drouter/themes \
             /var/log/drouter /run/drouter-helper
COPY backend/*.py    /opt/drouter/backend/
COPY web/            /opt/drouter/web/
COPY docs/           /opt/drouter/docs/
COPY scripts/*.sh    /opt/drouter/scripts/
COPY packaging/docker/docker-init.sh /opt/drouter/bin/docker-init.sh
RUN chmod +x /opt/drouter/bin/docker-init.sh /opt/drouter/scripts/*.sh

# 3) 运行用户（容器里以低权用户跑 Web，写 /etc 走 helper 白名单）
RUN useradd -r -s /usr/sbin/nologin -d /opt/drouter -M drouter \
    && printf 'drouter ALL=(root) NOPASSWD: /usr/bin/python3 /opt/drouter/backend/drouter-helper.py *\n' \
       > /etc/sudoers.d/drouter \
    && chmod 0440 /etc/sudoers.d/drouter \
    && chown -R drouter:drouter /opt/drouter/data /opt/drouter/snapshots /opt/drouter/certs /var/log/drouter \
    && chmod 700 /run/drouter-helper

EXPOSE 8080 8443
VOLUME ["/opt/drouter/data", "/opt/drouter/snapshots", "/etc/drouter", "/var/log/drouter"]

# 端口要跟着实际配置走，写死 8443 会在用户改端口后一直报 unhealthy
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -sk "https://127.0.0.1:$(cat /etc/drouter/web-port 2>/dev/null || echo 8443)/api/health" \
        || exit 1

ENTRYPOINT ["/opt/drouter/bin/docker-init.sh"]
CMD ["/usr/bin/python3", "/opt/drouter/backend/drouter-web.py"]

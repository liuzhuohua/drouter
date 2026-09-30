#!/bin/bash
# =============================================================================
# 构建 drouter 的 Docker 镜像
#
#   bash packaging/build-docker.sh [tag]
#
# 产物：drouter:<tag>
# 注意：Dockerfile 里 COPY 的是相对路径，所以构建上下文必须是项目根目录。
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
TAG="${1:-1.0.0}"

command -v docker >/dev/null 2>&1 || { echo "需要 docker"; exit 1; }

echo "== 1/3 构建前自测（避免把坏代码打进镜像）=="
python3 - <<'PY'
import ast, glob
bad = 0
for f in sorted(glob.glob('backend/*.py')):
    try:
        ast.parse(open(f, encoding='utf-8').read()); print('  OK:', f)
    except Exception as e:
        print('  FAIL:', f, e); bad += 1
raise SystemExit(1 if bad else 0)
PY
command -v node >/dev/null 2>&1 && node --check web/app.js && echo "  OK: web/app.js"

echo "== 2/3 构建镜像 drouter:$TAG =="
docker build -f "$HERE/packaging/docker/Dockerfile" -t "drouter:$TAG" "$HERE"

echo "== 3/3 产物 =="
docker images drouter:"$TAG"
echo ""
cat <<'RUN'
运行示例：
  docker run -d --name drouter --restart unless-stopped \
    --network host \
    --cap-add NET_ADMIN --cap-add NET_RAW \
    -e DROUTER_WEB_PORT=8443 \
    -v drouter-data:/opt/drouter/data \
    -v drouter-etc:/etc/drouter \
    drouter:1.0.0

说明：
  --network host  让容器直接看到宿主机网卡（路由类功能的前提）
  NET_ADMIN       允许操作 nftables / 网卡 / 路由表
  默认账号 admin / admin123，登录后请立即修改
RUN

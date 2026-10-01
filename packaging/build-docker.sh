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
TAG="${1:-1.0.4}"

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

# 同时打一个带用户名的别名，方便日后直推 Docker Hub / 私有仓库。
# 两个标签指向同一个 image id，不额外占空间。
docker tag "drouter:$TAG" "liuzhuohua/drouter:$TAG"

echo "== 3/3 产物 =="
docker images --filter "reference=drouter" --filter "reference=liuzhuohua/drouter"
echo ""
cat <<RUN
运行示例（详见 packaging/docker/packaging/docker/DOCKER-GUIDE.md）：

  # 方式 A：docker compose（Release 里附了 docker-compose.yml）
  docker compose up -d

  # 方式 B：docker run
  docker run -d --name drouter --restart unless-stopped \\
    --network host \\
    --cap-add NET_ADMIN --cap-add NET_RAW \\
    -e TZ=Asia/Shanghai \\
    -v drouter-etc:/etc/drouter \\
    -v drouter-data:/opt/drouter/data \\
    -v drouter-snapshots:/opt/drouter/snapshots \\
    -v drouter-log:/var/log/drouter \\
    drouter:$TAG

导出成 tar（用于分发）：
  docker save drouter:$TAG liuzhuohua/drouter:$TAG \\
    -o dist/drouter-$TAG-docker.tar

说明：
  --network host  让容器直接看到宿主机网卡（路由类功能的前提）
  NET_ADMIN       允许操作 nftables / 网卡 / 路由表
  不需要 --privileged：只要 NET_ADMIN + NET_RAW 就够
  默认账号 admin / admin123，登录后请立即修改
RUN

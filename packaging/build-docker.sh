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
# 版本号唯一真源是 packaging/VERSION（见 build-deb.sh 里的说明）
VF="$HERE/packaging/VERSION"
[ -f "$VF" ] || { echo "缺少 $VF（版本号唯一真源）"; exit 1; }
TAG="${1:-$(tr -d ' \t\r\n' < "$VF")}"

command -v docker >/dev/null 2>&1 || { echo "需要 docker"; exit 1; }

echo "== 1/4 构建前自测（避免把坏代码打进镜像）=="
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

echo "== 2/4 构建镜像 drouter:$TAG =="
docker build -f "$HERE/packaging/docker/Dockerfile" -t "drouter:$TAG" "$HERE"

# 同时打一个带用户名的别名，方便日后直推 Docker Hub / 私有仓库。
# 两个标签指向同一个 image id，不额外占空间。
docker tag "drouter:$TAG" "liuzhuohua/drouter:$TAG"

echo "== 3/4 渲染 Release 附件（docker-compose.yml / DOCKER-GUIDE.md）=="
# 这两个文件是**模板**，里面写的是 __VERSION__ 占位符，不是具体版本号。
# 早先它们直接硬编码版本号，于是每次发版都要手工 sed 一遍 —— 上一版就漏过，
# 附件里带着 drouter:1.0.8，用户拿 1.0.9 的 tar 加载后标签对不上，
# `docker compose up` 直接报「找不到镜像」。所以这里从 VERSION 渲染。
mkdir -p "$HERE/dist"
for f in docker-compose.yml DOCKER-GUIDE.md; do
  src="$HERE/packaging/docker/$f"
  [ -f "$src" ] || { echo "缺少模板 $src"; exit 1; }
  # 两步：① 整块删掉 TEMPLATE-NOTE 标记块（那是给维护者看的「这是模板」提示，
  #    留着会让用户看到「1.0.9 是占位符」这种自相矛盾的话）；
  #    ② 再把剩下的 __VERSION__ 换成真实版本号。
  #    顺序不能反 —— 先替换的话，标记块里的 __VERSION__ 也会被换掉，
  #    删的时候连同「这是模板」的语义一起留在了附件里。
  sed '/TEMPLATE-NOTE:BEGIN/,/TEMPLATE-NOTE:END/d' "$src" \
    | sed "s/__VERSION__/$TAG/g" > "$HERE/dist/$f"
  # 渲染完必须一个占位符都不剩 —— 剩下就说明模板里有漏网的写法
  if grep -q '__VERSION__' "$HERE/dist/$f"; then
    echo "✘ $f 渲染后仍含 __VERSION__，模板有问题"; exit 1
  fi
  # 标记块也不能残留（否则说明嵌套写错了，BEGIN/END 没配对）
  if grep -q 'TEMPLATE-NOTE' "$HERE/dist/$f"; then
    echo "✘ $f 渲染后仍含 TEMPLATE-NOTE 标记"; exit 1
  fi
  echo "  dist/$f  （$TAG）"
done

echo "== 4/4 产物 =="
docker images --filter "reference=drouter" --filter "reference=liuzhuohua/drouter"
echo ""
cat <<RUN
运行示例（详见 dist/DOCKER-GUIDE.md）：

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

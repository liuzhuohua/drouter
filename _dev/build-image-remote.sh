#!/bin/bash
# 在目标机上构建 drouter Docker 镜像并导出成 docker-archive tar。
#
#   bash _dev/build-image-remote.sh [版本号] [源码目录]
#
# 关键：基础镜像 debian:trixie-slim 里 /etc/apt/sources.list.d/debian.sources
# 指向 deb.debian.org，在国内拉 9.7MB 的 Packages 索引要几十秒甚至超时。
# 所以构建前先把容器内的源换成 163 镜像 —— 用 buildah 的 --build-arg 做不到
# （Dockerfile 里没有 ARG），改成「先 run 一个容器改源，再 commit 成临时基础镜像」。
#
# 必须以 root 跑（buildah 用 /var/lib/containers/storage，与普通用户的
# ~/.local/share/containers/storage 不互通）。
#
# 产物：/tmp/drouter-<版本>-docker.tar —— 还要再过一遍 devtools/retag-docker-image.py
#       把标签从 buildah 的 localhost/... 改成 docker 惯用的名字。
set -e

# 版本号唯一真源是 packaging/VERSION（见 packaging/build-deb.sh 里的说明）
_HERE="$(cd "$(dirname "$0")/.." && pwd)"
VF="$_HERE/packaging/VERSION"
[ -f "$VF" ] || { echo "缺少 $VF（版本号唯一真源）"; exit 1; }
VER="${1:-$(tr -d ' \t\r\n' < "$VF")}"
SRC="${2:-/tmp/drouter-build}"
cd "$SRC" || exit 1

echo "=== 0/4 环境检查 ==="
command -v buildah >/dev/null 2>&1 || { echo "✘ 需要 buildah"; exit 1; }
echo "  源码目录：$SRC"
echo "  目标标签：drouter:$VER"

echo "=== 1/4 准备国内源的基础镜像 ==="
CTR=$(buildah from docker.io/library/debian:trixie-slim)
buildah run "$CTR" -- bash -c '
cat > /etc/apt/sources.list <<EOF
deb http://mirrors.163.com/debian/ trixie main contrib non-free non-free-firmware
deb http://mirrors.163.com/debian/ trixie-updates main contrib non-free non-free-firmware
deb http://security.debian.org/debian-security trixie-security main contrib non-free non-free-firmware
EOF
rm -f /etc/apt/sources.list.d/debian.sources
apt-get update -qq 2>&1 | tail -2
echo "  容器内源已换成 163"
'
buildah commit "$CTR" drouter-base-163:latest
buildah rm "$CTR" >/dev/null

echo "=== 2/4 构建镜像 ==="
# 把 Dockerfile 的 FROM 换成刚做好的临时基础镜像
sed 's|^FROM debian:trixie-slim|FROM drouter-base-163:latest|' \
    packaging/docker/Dockerfile > /tmp/Dockerfile.drouter
buildah bud -f /tmp/Dockerfile.drouter -t "drouter:$VER" --isolation=chroot .
echo "BUILD_RC=$?"

echo "=== 3/4 导出 docker-archive ==="
# 用 buildah push 而不是 podman save：buildah 构建的镜像就在 root 的 storage 里，
# 不需要再用 podman 从另一个 storage root 去找。
OUT="/tmp/drouter-${VER}-docker.tar"
rm -f "$OUT"
buildah push "drouter:$VER" "docker-archive:$OUT:drouter:$VER"
ls -lh "$OUT"

echo "=== 4/4 结果 ==="
buildah images | head -8
echo ""
echo "下一步：scp 回本地 dist/，再跑 devtools/retag-docker-image.py 改标签"

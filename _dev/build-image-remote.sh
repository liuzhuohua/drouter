#!/bin/bash
# 在目标机构建 drouter Docker 镜像。
#
# 关键：基础镜像 debian:trixie-slim 里 /etc/apt/sources.list.d/debian.sources
# 指向 deb.debian.org，在国内拉 9.7MB 的 Packages 索引要几十秒甚至超时。
# 所以构建前先把容器内的源换成 163 镜像 —— 用 buildah 的 --build-arg 做不到
# （Dockerfile 里没有 ARG），改成「先 run 一个容器改源，再 commit 成临时基础镜像」。
set -e
cd /tmp/drouter-build || exit 1

echo "=== 1/3 准备国内源的基础镜像 ==="
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

echo "=== 2/3 构建镜像 ==="
# 把 Dockerfile 的 FROM 换成刚做好的临时基础镜像
sed 's|^FROM debian:trixie-slim|FROM drouter-base-163:latest|' \
    packaging/docker/Dockerfile > /tmp/Dockerfile.drouter
buildah bud -f /tmp/Dockerfile.drouter -t drouter:1.0.0 --isolation=chroot .
echo "BUILD_RC=$?"

echo "=== 3/3 结果 ==="
buildah images | head -6

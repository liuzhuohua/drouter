#!/bin/bash
# =============================================================================
# 构建「自包含离线安装包」——一个文件装完，不需要任何外网。
#
#   bash packaging/build-offline-bundle.sh [版本号] [输出目录]
#
# 产物：dist/drouter-<版本>-offline-amd64.tar.gz
#
# 为什么要有这个（而不是让用户自己拼 deb + 依赖）：
#   .deb 的 Depends 只声明依赖，真正把依赖装上去是 apt 的活。目标机没外网时
#   apt 无源可用，用户得自己想办法把 200+ 个 .deb 凑齐 —— 这几乎不可能做对
#   （传递依赖、Essential 包、版本匹配，任何一环错了装不上）。
#   所以这里把「本体 + 全部依赖 + 安装脚本」打成一个 tar.gz，
#   拷过去解包、跑一条命令就完事。
#
# 包内结构：
#   drouter-<版本>-offline-amd64/
#   ├── install.sh                  一条命令装完
#   ├── README-离线安装.md           给不熟悉的人看的步骤说明
#   ├── SHA256SUMS                  每个 deb 的校验和
#   ├── drouter_<版本>_all.deb       本体（含 systemd 单元）
#   └── debs/                       全部依赖（含 Essential 与传递依赖）
#       ├── *.deb
#       └── Packages + Packages.gz  本地仓库索引（**必须在 debs/ 里**，
#                                   否则 apt 找不到会静默退化成 dpkg -i）
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-1.0.5}"
OUTDIR="${2:-$HERE/dist}"
ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"

NAME="drouter-${VERSION}-offline-${ARCH}"
STAGE="$OUTDIR/$NAME"
BUNDLE="$OUTDIR/${NAME}.tar.gz"

command -v dpkg-deb >/dev/null 2>&1 || { echo "需要 dpkg-deb（请在 Debian/Ubuntu 上执行）"; exit 1; }

echo "======================================================================"
echo "构建自包含离线安装包：$NAME"
echo "======================================================================"

echo ""
echo "== 1/5 构建 drouter 本体 .deb =="
bash "$HERE/packaging/build-deb.sh" "$VERSION" >/dev/null
DEB="$HERE/dist/drouter_${VERSION}_all.deb"
[ -f "$DEB" ] || { echo "✘ 没生成 $DEB"; exit 1; }
echo "  $(basename "$DEB")  $(du -h "$DEB" | cut -f1)"

echo ""
echo "== 2/5 生成离线依赖库 =="
# 注意：make-offline-deps.sh 直接往 $STAGE 里写（debs/ + install.sh），
# 不需要中间目录。早期版本这里写过 `mv $OUTDIR/offline-deps/*` 是多余的，
# 那段在目录不存在时会静默跳过，容易让人以为「搬过了」。
rm -rf "$STAGE"
bash "$HERE/scripts/make-offline-deps.sh" "$STAGE" 2>&1 | sed 's/^/  /' | tail -8

NDEB=$(ls -1 "$STAGE"/debs/*.deb 2>/dev/null | wc -l)
[ "$NDEB" -gt 50 ] || { echo "✘ 依赖包只有 $NDEB 个，明显不对"; exit 1; }

# 索引必须在 debs/ 里，否则 apt 找不到会静默退化成 dpkg -i 乱装
if [ ! -f "$STAGE/debs/Packages" ] && [ ! -f "$STAGE/debs/Packages.gz" ]; then
  echo "✘ debs/ 里没有 Packages 索引 —— 离线安装会退化成 dpkg -i"; exit 1
fi

# 硬性依赖抽查：这几个缺任何一个，干净目标机一定装不上
for must in libssl3t64 hostname tar dash diffutils libc-bin liblz4-1 libapt-pkg7.0; do
  ls "$STAGE/debs/${must}"_*.deb >/dev/null 2>&1 || \
  ls "$STAGE/debs/${must}t64"_*.deb >/dev/null 2>&1 || \
  { echo "✘ 依赖里缺少关键包：$must"; exit 1; }
done
echo "  依赖包 $NDEB 个，索引与关键包校验通过"

echo ""
echo "== 3/5 放入本体 + 说明 =="
cp -f "$DEB" "$STAGE/"
# 把 make-offline-deps.sh 生成的 install.sh 换成 bundle 版（更完整、带自检）
cp -f "$HERE/packaging/offline-install.sh" "$STAGE/install.sh"
chmod +x "$STAGE/install.sh"

cat > "$STAGE/README-离线安装.md" <<EOF
# Drouter ${VERSION} 离线安装包

**适用场景**：目标机（Debian 13 amd64）**没有外网**，想一次装完。

## 三步装完

\`\`\`bash
# 1) 把这个目录整个拷到目标机（U 盘 / scp / 内网共享都行）
#    比如拷到 /tmp/drouter-offline

# 2) 进目录
cd /tmp/drouter-offline

# 3) 一条命令装完（会自动 sudo）
bash install.sh
\`\`\`

装完访问 \`https://<目标机IP>:8443/\`，默认账号 \`admin\` / \`admin123\`。

## 这个包里有什么

| 文件 | 说明 |
|---|---|
| \`drouter_${VERSION}_all.deb\` | Drouter 本体 |
| \`debs/\` | 全部依赖的 .deb，**含传递依赖与 Essential 包** |
| \`debs/Packages.gz\` | 本地仓库索引，让 apt 自己算依赖顺序 |
| \`install.sh\` | 安装脚本 |

依赖是完整的：从直接依赖（nftables / dnsmasq / radvd / ppp …）
一路到最底层（libc6 / zlib1g / dash / diffutils），全部在里面。
不需要目标机有任何网络。

## 检查完整性

\`\`\`bash
cd /tmp/drouter-offline
sha256sum -c SHA256SUMS        # 校验每个 deb 没损坏
ls debs/*.deb | wc -l          # 应该有两百多个
\`\`\`

## 如果装不上

1. **先看硬件够不够**：\`free -m\` / \`df -h /\`。内存 < 1G 或根分区 < 3G 会失败。
2. **确认系统版本**：\`cat /etc/os-release\`，必须是 Debian 13 (trixie) amd64。
   别的版本依赖版本对不上。
3. **看具体报错**：\`bash install.sh 2>&1 | tee /tmp/install.log\`，
   然后按 log 里的第一条 \`E:\` 定位。

## 卸载

\`\`\`bash
sudo apt remove drouter      # 删程序，保留配置
sudo apt purge  drouter      # 连配置一起删
\`\`\`
EOF

echo ""
echo "== 4/5 生成校验和 =="
# 先把构建期中间产物清掉再算校验和，免得它们被算进去又被打包
rm -rf "$STAGE/.solveroot" "$STAGE/.closure" "$STAGE/.allpkgs" \
       "$STAGE/.sim.txt" "$STAGE/.sim.err" 2>/dev/null || true
( cd "$STAGE" && find . -name '*.deb' -type f | sed 's|^\./||' \
    | sort | xargs -r sha256sum > SHA256SUMS )
echo "  SHA256SUMS: $(wc -l < "$STAGE/SHA256SUMS") 个文件"

# 打包前的最终结构校验：只允许出现下面这些顶层条目。
# 出现别的东西（隐藏目录、临时文件）说明前面的清理有漏，宁可在这里报错，
# 也不要让用户解包后看到一堆莫名其妙的东西。
BAD=$(cd "$STAGE" && ls -A | grep -vE '^(debs|drouter_.*_all\.deb|install\.sh|README-离线安装\.md|SHA256SUMS)$' || true)
if [ -n "$BAD" ]; then
  echo "✘ 离线包里有不该出现的顶层条目："
  echo "$BAD" | sed 's/^/    /'
  exit 1
fi
echo "  顶层结构校验通过"

echo ""
echo "== 5/5 打包 =="
# 用 tar.gz 而不是 zip：Linux 上天然可用，且保留可执行位
( cd "$OUTDIR" && tar czf "${NAME}.tar.gz" "$NAME" )
rm -rf "$STAGE"

echo ""
echo "======================================================================"
echo "✅ 自包含离线安装包已生成"
echo ""
echo "  文件：$BUNDLE"
echo "  大小：$(du -h "$BUNDLE" | cut -f1)"
echo ""
echo "  上传到 GitHub Release 时，只传这一个文件即可 ——"
echo "  目标机解包后跑 install.sh 就能在完全无外网的环境装好。"
echo "======================================================================"

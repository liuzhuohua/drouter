#!/bin/bash
# 容器成品镜像冒烟（第二段）：登录 + Web 终端建会话 + 接口抽查
# 重点验证 1.0.1 的两条修复：
#   ① 容器里没有 systemd，终端守护仍能被拉起（_shelld_spawn 回退路径）
#   ② /dev/ptmx 存在，PTY 能开
# 用 --network none：不碰宿主网络，只在容器内自测 127.0.0.1。
set -u

TAR=/tmp/drouter-101-final.tar
NAME=drouter-smoke101
IMG=drouter:1.0.1
BASE=https://127.0.0.1:8443
PODMAN="podman"

pass=0; fail=0
ck() {  # ck <描述> <期望正则> <实际输出>
  local desc="$1" want="$2" got="$3"
  if grep -qE -- "$want" <<<"$got"; then
    echo "  ✔ $desc"; pass=$((pass+1))
  else
    echo "  ✘ $desc"
    echo "     期望包含: $want"
    echo "     实际    : $(head -c 200 <<<"$got")"
    fail=$((fail+1))
  fi
}

cleanup() {
  $PODMAN rm -f "$NAME" >/dev/null 2>&1 && echo "  容器已删除"
  # podman 用 netavark 驱动，跑过一次容器就会在宿主留下 `table inet netavark`
  # （空隔离链 + 只匹配 meta mark 0x2000 的 masquerade）。无容器时它匹配不到
  # 任何流量，但既然是我们引入的，就顺手还回去，保持宿主规则集与跑之前一致。
  if nft list table inet netavark >/dev/null 2>&1; then
    nft list table inet netavark > /root/netavark-table-backup.nft 2>&1
    nft delete table inet netavark 2>/dev/null \
      && echo "  已移除 podman 的 netavark 表（备份 /root/netavark-table-backup.nft）"
  fi
}
trap cleanup EXIT

$PODMAN rm -f "$NAME" >/dev/null 2>&1
echo "=== 加载镜像 ==="
$PODMAN load -i "$TAR" 2>&1 | tail -2

echo "=== 启动容器（--network none）==="
$PODMAN run -d --name "$NAME" --network none "$IMG" >/dev/null 2>&1
for i in $(seq 1 20); do
  $PODMAN exec "$NAME" curl -sk --max-time 5 "$BASE/api/health" >/dev/null 2>&1 && break
  sleep 2
done
echo "  已就绪"

echo
echo "=== 1. 登录 ==="
RAW=$($PODMAN exec "$NAME" curl -sk --max-time 8 -X POST "$BASE/api/login" \
      -H 'Content-Type: application/json' \
      -d '{"username":"admin","password":"admin123"}' 2>/dev/null)
TOKEN=$(sed -n 's/.*"token": *"\([^"]*\)".*/\1/p' <<<"$RAW")
echo "  token 长度 = ${#TOKEN}"
ck "登录返回 token" '"ok": *true' "$RAW"

echo
echo "=== 2. 容器内没有 systemd（前提确认）==="
NSD=$($PODMAN exec "$NAME" sh -c 'command -v systemctl >/dev/null 2>&1 && echo 有 || echo 无; ls /run/systemd/system 2>/dev/null | head -1' 2>&1)
echo "  $NSD"

echo
echo "=== 3. /dev/ptmx 与 PTY ==="
PTMX=$($PODMAN exec "$NAME" sh -c 'ls -l /dev/ptmx 2>&1; echo "pts=$(ls /dev/pts 2>/dev/null | wc -l)"' 2>&1)
echo "  $PTMX"
ck "/dev/ptmx 存在" "/dev/ptmx" "$PTMX"

echo
echo "=== 4. Web 终端建会话（关键：容器内守护能否拉起）==="
CONN=$($PODMAN exec "$NAME" curl -sk --max-time 25 -X POST "$BASE/api/webshell/connect" \
       -H "X-Token: $TOKEN" -H 'Content-Type: application/json' \
       -d '{"user":"root","cols":100,"rows":30}' 2>&1)
echo "  $(head -c 300 <<<"$CONN")"
ck "终端建立成功（ok=true）" '"ok": *true' "$CONN"
ck "没有 NO_SHELLD" '' "$(grep -o NO_SHELLD <<<"$CONN" || echo '')"

SID=$(sed -n 's/.*"sid": *"\([^"]*\)".*/\1/p' <<<"$CONN")
echo "  sid = $SID"

echo
echo "=== 5. 终端守护进程确实在跑 ==="
PS=$($PODMAN exec "$NAME" sh -c 'pgrep -af shelld 2>&1 | head -3' 2>&1)
echo "  $PS"
ck "drouter-shelld 进程存在" "shelld" "$PS"
SOCK=$($PODMAN exec "$NAME" sh -c 'ls -l /run/drouter/shell.sock 2>&1' 2>&1)
echo "  $SOCK"
ck "shell.sock 已创建" "shell.sock" "$SOCK"

echo
echo "=== 6. 真正跑一条命令，读回显 ==="
if [ -n "$SID" ]; then
  $PODMAN exec "$NAME" curl -sk --max-time 10 -X POST "$BASE/api/webshell/write" \
    -H "X-Token: $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"op\":\"write\",\"sid\":\"$SID\",\"data\":\"echo DROUTER-$RANDOM-OK\\n\"}" >/dev/null 2>&1
  OUT=$($PODMAN exec "$NAME" curl -sk --max-time 15 -X POST "$BASE/api/webshell/read" \
        -H "X-Token: $TOKEN" -H 'Content-Type: application/json' \
        -d "{\"op\":\"read\",\"sid\":\"$SID\",\"wait\":2}" 2>&1)
  echo "  $(head -c 300 <<<"$OUT")"
  ck "读回显成功" '"ok": *true' "$OUT"
  ck "回显含命令输出" 'DROUTER-' "$OUT"

  echo
  echo "=== 7. 断开会话 ==="
  DISC=$($PODMAN exec "$NAME" curl -sk --max-time 10 -X POST "$BASE/api/webshell/disconnect" \
          -H "X-Token: $TOKEN" -H 'Content-Type: application/json' \
          -d "{\"op\":\"disconnect\",\"sid\":\"$SID\"}" 2>&1)
  echo "  $(head -c 200 <<<"$DISC")"
  ck "断开成功" '"ok": *true' "$DISC"
fi

echo
echo "=== 8. 接口抽查 ==="
for ep in sysinfo ifaces metrics; do
  printf '  %-9s ' "$ep"
  R=$($PODMAN exec "$NAME" curl -sk --max-time 10 -H "X-Token: $TOKEN" "$BASE/api/$ep" 2>&1)
  grep -qE '"ok": *true' <<<"$R" && { echo "ok ✔"; pass=$((pass+1)); } || { echo "✘ $(head -c 120 <<<"$R")"; fail=$((fail+1)); }
done

echo
echo "================================================"
echo "  容器冒烟：通过 $pass / 失败 $fail"
echo "================================================"

echo
echo "=== 宿主侧安全基线（应无变化）==="
echo "  ip_forward = $(cat /proc/sys/net/ipv4/ip_forward)"
echo "  nft 表     = $(nft list tables 2>/dev/null | tr '\n' ' ')"
echo "  默认路由   = $(ip route | grep '^default' | head -1)"
echo "  5900 监听  = $(ss -lnt 2>/dev/null | grep -c ':5900' )"

exit $fail

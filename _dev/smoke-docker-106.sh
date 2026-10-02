#!/bin/bash
# 容器成品镜像冒烟（第二段）：登录 + Web 终端建会话 + 接口抽查
# 1.0.6 本轮修的是「静默失效」类缺陷，容器形态同样要验：
#   ① 容器里没有 systemd，终端守护仍能被拉起（_shelld_spawn 回退路径）
#   ② /dev/ptmx 存在，PTY 能开（信号量上限后新建会话要回 BUSY 而不是挂死）
#   ③ 自签证书的 SAN 取自本机地址，不得再出现作者写死的内网 IP
#      （1.0.4 修复；过去直接硬编码 192.168.7.3，任何机器上签出来都带着它）
#   ④ 1.0.6 新增 sync-isp-dns.sh（PPPoE 上游 DNS 同步脚本）必须在镜像里
#   ⑤ 1.0.6 修的 rescue token / SNMP 净化都在 backend，要能被 import
# 用 --network none：不碰宿主网络，只在容器内自测 127.0.0.1。
set -u

TAR=/tmp/drouter-106-final.tar
NAME=drouter-smoke106
IMG=drouter:1.0.6
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
echo "=== 9. 自签证书 SAN（不得再写死作者内网 IP）==="
CERT=$($PODMAN exec "$NAME" sh -c 'C=$(ls /opt/drouter/certs/server.crt 2>/dev/null); [ -n "$C" ] || exit 1; openssl x509 -in "$C" -noout -text 2>/dev/null' 2>&1)
if [ -n "$CERT" ]; then
  echo "  SAN: $(grep -A1 -i 'Subject Alternative Name' <<<"$CERT" | tail -1 | head -c 200)"
  ck "证书 SAN 含 loopback 127.0.0.1" '127\.0\.0\.1' "$CERT"
  # 反向断言：写死的作者内网 IP 绝不能出现（ck 只能做「包含」判断，这里手写）
  if grep -q '192\.168\.7\.3' <<<"$CERT"; then
    echo "  ✘ 证书里仍写着硬编码的 192.168.7.3"; fail=$((fail+1))
  else
    echo "  ✔ 证书 SAN 不含写死的 192.168.7.3"; pass=$((pass+1))
  fi
else
  echo "  ✘ 读不到 /opt/drouter/certs/server.crt（$CERT）"; fail=$((fail+1))
fi

echo
echo "=== 10. sync-isp-dns.sh 在镜像里且可执行（1.0.6 新增）==="
# 1.0.6 修 PPPoE 断网：render_ppp 写 ip-up-script 指向这个脚本，
# 缺了它 dns_mode=isp/both 时 dnsmasq 上游全空 —— 而 PPPoE 页看不出异常。
# 路径必须是 scripts/：deb / 镜像 / deploy.sh 三条路都装到 $OPT/scripts/。
SYNC=/opt/drouter/scripts/sync-isp-dns.sh
if $PODMAN exec "$NAME" test -x "$SYNC"; then
  echo "  ✔ 脚本在且可执行"; pass=$((pass+1))
else
  echo "  ✘ 缺失或不可执行：$($PODMAN exec "$NAME" ls -l "$SYNC" 2>&1)"
  fail=$((fail+1))
fi
ck "脚本把运营商 DNS 写向 dnsmasq 的 resolv-file" \
   'generated/isp-dns\.conf' "$($PODMAN exec "$NAME" cat "$SYNC" 2>&1)"
# 容器里没有 systemd，只能 SIGHUP；早先版本只 systemctl reload，
# 在容器内等于什么都不做（DNS 文件更新了但 dnsmasq 仍用旧上游）。
ck "无 systemd 时回退 SIGHUP 重载 dnsmasq" \
   'kill -HUP .*pidof dnsmasq' "$($PODMAN exec "$NAME" cat "$SYNC" 2>&1)"
# render_ppp 写的路径必须与实际安装位置一致，否则 pppd 静默拿不到 DNS
ck "render_ppp 的 ip-up-script 路径与安装位置一致" \
   'ip-up-script /opt/drouter/scripts/sync-isp-dns\.sh' \
   "$($PODMAN exec "$NAME" grep -A8 'def render_ppp' /opt/drouter/backend/render.py 2>&1)"

echo
echo "=== 11. backend 关键修复在镜像里（1.0.6）==="
# 抽查三处最容易「打包漏掉」的地方：rescue token、SNMP 净化、静态租约字段名
ck "rescue 服务端有令牌校验" '_token_ok' \
   "$($PODMAN exec "$NAME" grep -c '_token_ok' /opt/drouter/backend/drouter-rescue.py 2>&1)"
ck "SNMP 三个字段过滤换行" "re\.sub\(r'\[\\\\r\\\\n\]', ' '" \
   "$($PODMAN exec "$NAME" grep -A6 "for k in ('contact', 'location', 'sysname')" /opt/drouter/backend/drouter-helper.py 2>&1)"
ck "静态租约写 enabled+name" "'enabled': True" \
   "$($PODMAN exec "$NAME" grep -c "'enabled': True, 'mac': mac" /opt/drouter/backend/drouter-helper.py 2>&1)"
ck "readings 不再遮蔽 POST 写分支" '_has_post_branch' \
   "$($PODMAN exec "$NAME" grep -c '_has_post_branch' /opt/drouter/backend/drouter-web.py 2>&1)"

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

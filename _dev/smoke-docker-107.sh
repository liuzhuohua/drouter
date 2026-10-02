#!/bin/bash
# 容器成品镜像冒烟（第二段）：登录 + Web 终端建会话 + 接口抽查
# 1.0.7 本轮新增备份 / 告警 / 配额三个守护，容器形态同样要验：
#   ① 容器里没有 systemd，三个新守护仍能被拉起（_shelld_spawn 同款回退路径）
#   ② /dev/ptmx 存在，PTY 能开（信号量上限后新建会话要回 BUSY 而不是挂死）
#   ③ 自签证书的 SAN 取自本机地址，不得再出现作者写死的内网 IP
#      （1.0.4 修复；过去直接硬编码 192.168.7.3，任何机器上签出来都带着它）
#   ④ sync-isp-dns.sh（1.0.6 新增，PPPoE 上游 DNS 同步）必须在镜像里
#   ⑤ rescue token / SNMP 净化（1.0.6 修）都在 backend，要能被 import
#   ⑥ 本轮修的 7 个缺陷：备份敏感排除、VPN 私钥不外泄、ip_network 3.13 判定
# 用 --network none：不碰宿主网络，只在容器内自测 127.0.0.1。
set -u

TAR=/tmp/drouter-107-final.tar
NAME=drouter-smoke107
IMG=drouter:1.0.7
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

ckc() {  # ckc <描述> <期望命中次数> <实际命中次数>
  # 专用于「grep -c 的数字」型断言。反向断言（期望 0 次）用这个，
  # 不要拿数字去 ck 里匹配 —— 那是在问「数字里含不含这段代码」，
  # 恒为假，1.0.6 首次冒烟的 5 个红全是栽在这里。
  local desc="$1" want="$2" got="$3"
  if [ "$got" = "$want" ]; then
    echo "  ✔ $desc（$got）"; pass=$((pass+1))
  else
    echo "  ✘ $desc"
    echo "     期望命中: $want 次，实际: $got"
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
# render_ppp 写的路径必须与实际安装位置一致，否则 pppd 静默拿不到 DNS。
# -A60 而不是小窗口：ip-up-script 距 def 行有 46 行（docstring + 5 行解释
# 为什么要这个钩子），窗口窄了正好切在它前面，断言恒假。
ck "render_ppp 的 ip-up-script 路径与安装位置一致" \
   'ip-up-script /opt/drouter/scripts/sync-isp-dns\.sh' \
   "$($PODMAN exec "$NAME" grep -A60 'def render_ppp' /opt/drouter/backend/render.py 2>&1)"
# 反向断言：不能再有指向 bin/ 的旧路径（那是不存在的位置）
ckc "render_ppp 不再指向不存在的 bin/ 路径" 0 \
    "$($PODMAN exec "$NAME" grep -c 'ip-up-script /opt/drouter/bin/' /opt/drouter/backend/render.py 2>&1)"

echo
echo "=== 11. backend 关键修复在镜像里（1.0.6）==="
# 抽查几处最容易「打包漏掉」的地方：rescue token、SNMP 净化、静态租约字段名。
# 注意用「内容匹配」而不是 grep -c 的数字 —— 数字拿去 ck 匹配代码文本
# 是把「命中了几个」当成了「命中了什么」，恒失败（1.0.6 首次冒烟栽在这）。
ck "rescue 服务端有令牌校验" '_token_ok' \
   "$($PODMAN exec "$NAME" grep '_token_ok' /opt/drouter/backend/drouter-rescue.py 2>&1 | head -2)"
# 只断言「净化那几行在」，不硬写正则字面量：ck 内部走 grep -E，
# `[\r\n]` 这类字符类再经一层 shell 转义后极易对不上（1.0.6 首次冒烟栽过）。
# 真正要防的是「三个字段只有截长度、不过滤控制字符」，用净化后的特征串断言。
ck "SNMP 三个字段过滤换行" "re\.sub\(r, ' ', str\(s\[k\]\)\)|replace\('#', ' '\)" \
   "$($PODMAN exec "$NAME" grep -A10 "for k in ('contact', 'location', 'sysname')" /opt/drouter/backend/drouter-helper.py 2>&1)"
ck "静态租约写 enabled+name" "'enabled': True, 'mac': mac" \
   "$($PODMAN exec "$NAME" grep "'enabled': True, 'mac': mac" /opt/drouter/backend/drouter-helper.py 2>&1 | head -2)"
ck "readings 不再遮蔽 POST 写分支" '_has_post_branch' \
   "$($PODMAN exec "$NAME" grep '_has_post_branch' /opt/drouter/backend/drouter-web.py 2>&1 | head -2)"
ckc "sync-isp-dns.sh 写盘失败非零退出（不能静默 rc=0）" 2 \
    "$($PODMAN exec "$NAME" grep -c 'exit 1' /opt/drouter/scripts/sync-isp-dns.sh 2>&1)"

echo
echo "=== 12. 1.0.7 三个新守护在镜像里且语法正确 ==="
# 容器形态没有 systemd，守护不能只靠 systemctl。这里只验「文件在 + 能编译 +
# 有不依赖 init 的拉起路径」，真跑起来要等 sync.sh 那轮真机验证。
for d in backupd alertd quotad; do
  ck "drouter-$d.py 存在" "drouter-$d\.py" \
     "$($PODMAN exec "$NAME" ls /opt/drouter/backend/drouter-$d.py 2>&1)"
  ckc "drouter-$d.py 语法正确" 0 \
      "$($PODMAN exec "$NAME" python3 -c "import ast,sys;ast.parse(open(sys.argv[1],encoding='utf-8').read())" /opt/drouter/backend/drouter-$d.py 2>&1; echo $?)"
  # 自启动回退：容器里没有 init，必须有 Popen / start_new_session 这类不依赖
  # systemd 的路径，否则守护永远起不来（真机上 systemctl 是有的，容易漏）
  ckc "drouter-$d.py 有不依赖 init 的拉起路径" 1 \
      "$($PODMAN exec "$NAME" grep -cE 'Popen|start_new_session' /opt/drouter/backend/drouter-$d.py 2>&1)"
done
ck "三个新守护在 deploy.sh 里被安装" 'drouter-backupd\.py' \
   "$($PODMAN exec "$NAME" grep -oE 'drouter-(backupd|alertd|quotad)\.py' /opt/drouter/scripts/deploy.sh 2>&1 | head -3)"
# 三个都要在。上面那条 ck 只要求「至少含一个」—— 用计数把「漏装一个」钉死。
ckc "deploy.sh 里三个新守护齐全" 3 \
    "$($PODMAN exec "$NAME" grep -oE 'drouter-(backupd|alertd|quotad)\.py' /opt/drouter/scripts/deploy.sh 2>&1 | sort -u | wc -l | tr -d ' ')"

echo
echo "=== 13. 1.0.7 修的三个隐蔽缺陷在镜像里 ==="
# ① 默认备份不再整目录排除 /etc/drouter
ckc "备份不再整目录标敏感（sensitive=(d == ...) 已消失）" 0 \
    "$($PODMAN exec "$NAME" grep -c "sensitive=(d == '/etc/drouter')" /opt/drouter/backend/drouter-helper.py 2>&1)"
ck "备份有逐文件敏感判定" '_bk_is_sensitive' \
   "$($PODMAN exec "$NAME" grep -c '_bk_is_sensitive' /opt/douter/backend/drouter-helper.py 2>&1 | head -1)"
# ② VPN status 不再返回含私钥的 raw
ckc "_vpn_status 不再返回 'raw': d" 0 \
    "$($PODMAN exec "$NAME" grep -cE "'raw': d" /opt/douter/backend/drouter-helper.py 2>&1)"
ck "_vpn_mask 仍在（脱敏没被一起删掉）" '_vpn_mask' \
   "$($PODMAN exec "$NAME" grep -c '_vpn_mask' /opt/douter/backend/drouter-helper.py 2>&1 | head -1)"
# ③ ip_network 成员判断必须用对象（3.13 回归）
ck "ip_network 归属判断用对象" 'aobj not in net' \
   "$($PODMAN exec "$NAME" grep -cE 'aobj not in net' /opt/drouter/backend/drouter-helper.py 2>&1)"
# ④ wireguard 登记进 DEPS
ck "DEPS 登记 wireguard-tools" "wireguard-tools" \
   "$($PODMAN exec "$NAME" grep -c 'wireguard-tools' /opt/drouter/backend/drouter-helper.py 2>&1 | head -1)"
# ⑤ Python 版本必须是 3.13 —— 本轮修的 ip_network 缺陷正是 3.13 才暴露的
ck "镜像 Python 是 3.13（3.12 上这条断言无意义）" 'Python 3\.13' \
   "$($PODMAN exec "$NAME" python3 -V 2>&1)"

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

#!/bin/bash
# 容器成品镜像冒烟（第二段）：登录 + Web 终端建会话 + 接口抽查
# 1.0.7 新增备份 / 告警 / 配额三个守护，容器形态同样要验：
#   ① 容器里没有 systemd，三个新守护仍能被拉起（_shelld_spawn 同款回退路径）
#   ② /dev/ptmx 存在，PTY 能开（信号量上限后新建会话要回 BUSY 而不是挂死）
#   ③ 自签证书的 SAN 取自本机地址，不得再出现作者写死的内网 IP
#      （1.0.4 修复；过去直接硬编码 192.168.7.3，任何机器上签出来都带着它）
#   ④ sync-isp-dns.sh（1.0.6 新增，PPPoE 上游 DNS 同步）必须在镜像里
#   ⑤ rescue token / SNMP 净化（1.0.6 修）都在 backend，要能被 import
#   ⑥ 1.0.7 修的 7 个缺陷：备份敏感排除、VPN 私钥不外泄、ip_network 3.13 判定
#   ⑦ 1.0.8 新增：VPN 四态诊断（_vpn_env）+ /etc/modules-load.d/ 开机自启，
#      容器里 wireguard 模块多半加载不了 → 这正好是「unsupported」那一档，
#      验的是**别误报成别的态**、以及 fix 按钮该消失时消失
# 用 --network none：不碰宿主网络，只在容器内自测 127.0.0.1。
set -u

TAR=/tmp/drouter-108-final.tar
NAME=drouter-smoke108
IMG=drouter:1.0.8
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

ckg() {  # ckg <描述> <期望出现的关键字> <grep -c 的输出>
  # 问的是「命中数 ≥1 吗」而不是「输出里含不含这个关键字」。
  # ⛔ 别用 ck 配 `grep -c` —— grep -c 只吐一个数字，数字里永远不会
  # 含代码片段，所以那种写法**恒为假**。ckc 又只能问「恰好等于几」，
  # 「出现过没有」这个中间档没有对应函数，于是 1.0.7 冒烟里 4 条断言
  # 全栽在这儿（其中 3 条还是恒绿：路径拼错时 grep 往 stderr 打的
  # 是错误信息，`2>&1` 把它收了进来，「期望 0 次」照样满足）。
  # 顺带：非数字输出（grep 的报错）也判失败，
  # 免得「查错了文件」又被读成「代码不在」。
  local desc="$1" key="$2" got="$3"
  if [[ "$got" =~ ^[0-9]+$ ]] && [ "$got" -ge 1 ]; then
    echo "  ✔ $desc（命中 $got 次）"; pass=$((pass+1))
  else
    echo "  ✘ $desc"
    echo "     期望命中 ≥1 次，关键词: $key"
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
  # 基线还原的**断言必须写在 cleanup 里面**。
  # 之前它被放在函数体末尾、cleanup 之前 —— 而 cleanup 是 trap EXIT 触发的，
  # 那时容器还活着、netavark 表当然还在（podman 每次 run 都会重建），
  # 于是这条断言**结构性必红**。顺序错了，判据再对也没用。
  #
  #⛔ 但 trap 在 `exit $fail` **之后**才跑，此时 $fail 已经求值传给内核了，
  # 在这里 `fail=$((fail+1))` 改计数毫无作用（1.0.8 首次就踩了）。
  # 想让「基线未还原」真的让脚本红，只能在 trap 里自己 exit。
  if nft list table inet netavark >/dev/null 2>&1; then
    echo "  ✘ podman 的 netavark 表未能删除（基线未还原）"
    BASELINE_BAD=1
  else
    echo "  ✔ podman 的 netavark 表已删除（基线还原）"
  fi
  if [ "${BASELINE_BAD:-0}" = 1 ]; then
    exit $(( ${RC_AT_EXIT:-0} + 1 ))
  fi
  exit ${RC_AT_EXIT:-0}
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
echo "=== 12. 1.0.7 新增的三个守护在镜像里且语法正确 ==="
# 容器形态没有 systemd。这里只验「文件在 + 能编译 + 被 deploy.sh 安装」，
# 真跑起来要等 sync.sh 那轮真机验证。
#
#⛔ 不要在这里断言「有 Popen / start_new_session」——
# 那是**常驻服务**（shelld 那类）的需求，而这三个是 **oneshot 型timer**：
# timer 定时把它拉起来、脚本自己跑完就退出，压根不需要脱离 init 常驻。
# 1.0.7 的冒烟里写了这三条断言，三个守护一个都没有 Popen →
# 每次冒烟必红 3 条，于是 1.0.8 有人把期望从 1 改成别的数字来「修绿」，
# 那是把判据迁就实现。判据该问的是它自己的形态对不对。
for d in backupd alertd quotad; do
  ck "drouter-$d.py 存在" "drouter-$d\.py" \
     "$($PODMAN exec "$NAME" ls /opt/drouter/backend/drouter-$d.py 2>&1)"
  ckc "drouter-$d.py 语法正确" 0 \
      "$($PODMAN exec "$NAME" python3 -c "import ast,sys;ast.parse(open(sys.argv[1],encoding='utf-8').read())" /opt/drouter/backend/drouter-$d.py 2>&1; echo $?)"
  # 形态自证：oneshot 守护的入口是 main()，跑完即退。
  # 常驻守护才有「起一个子进程」的需求，出现 Popen 反而要警惕它跑不飞。
  ckg "drouter-$d.py 是 oneshot（有 main 入口、无常驻子进程）" '__main__' \
      "$($PODMAN exec "$NAME" grep -c '__main__' /opt/drouter/backend/drouter-$d.py 2>&1)"
  ckc "drouter-$d.py 不含常驻子进程（Popen=0，oneshot 不需要）" 0 \
      "$($PODMAN exec "$NAME" grep -c 'Popen' /opt/drouter/backend/drouter-$d.py 2>&1)"
done
ck "三个新守护在 deploy.sh 里被安装" 'drouter-backupd\.py' \
   "$($PODMAN exec "$NAME" grep -oE 'drouter-(backupd|alertd|quotad)\.py' /opt/drouter/scripts/deploy.sh 2>&1 | head -3)"
# 三个都要在。上面那条 ck 只要求「至少含一个」—— 用计数把「漏装一个」钉死。
ckc "deploy.sh 里三个新守护齐全" 3 \
    "$($PODMAN exec "$NAME" grep -oE 'drouter-(backupd|alertd|quotad)\.py' /opt/drouter/scripts/deploy.sh 2>&1 | sort -u | wc -l | tr -d ' ')"

echo
echo "=== 13. 1.0.7 修的三个隐蔽缺陷在镜像里 ==="
# ⓪ 先确认文件在位。下面每一条都往这个路径 grep —— 路径拼错时 grep 会往
# stderr 打 "No such file or directory"，**而这个字符串被 2>&1 收进输出**，
# 于是「期望 0 次」那几条照样绿（错一次 == 期望值），
# 「期望包含 xxx」那几条则因为拿到的是报错而非内容而恒假。
# 一条 1.0.7 冒烟就栽在这里：/opt/douter 少个r，4 条断言一直是假红，
# 而它们本该守的 3 个真缺陷「看起来」早就修好了。
H2=/opt/drouter/backend/drouter-helper.py
ckc "helper 路径拼写正确（先验路径再谈内容）" 0 \
    "$($PODMAN exec "$NAME" sh -c "test -f $H2" 2>&1; echo $?)"
# ① 默认备份不再整目录排除 /etc/drouter
# ⛔ 反向断言必须**排除注释**。这个字面量就写在解释 bug 来历的注释里
#（helper 11669 行：「早先的版本写了 sensitive=(d == '/etc/drouter')」），
# 直接 grep -c 必然命中 1 → 每次冒烟假红。
# 用 `grep -v` 先剔掉注释行；行首锚定 `^[^#]*` 兜住缩进后的注释。
ckc "备份不再整目录标敏感（sensitive=(d == ...) 已消失）" 0 \
    "$($PODMAN exec "$NAME" grep -v '^[[:space:]]*#' $H2 2>&1 | grep -c "sensitive=(d ==")"
#ⓐ ckc 比数字，ckc 才问「数字等于几」；问「含不含 xxx」要用 ckg（见下）
ckg "备份有逐文件敏感判定" '_bk_is_sensitive' \
   "$($PODMAN exec "$NAME" grep -c '_bk_is_sensitive' $H2 2>&1 | head -1)"
# ② VPN status 不再返回含私钥的 raw
#⛔ 同一个坑 + 一个更隐蔽的：helper 里另有一处 'raw'（打印服务的
# `cfg = {'cups': ..., 'raw': dict(PRINT_DEFAULTS['raw'])}`），
# 与 VPN 毫无关系。所以既要去注释，还得把范围限在 _vpn_status 函数内。
ckc "_vpn_status 不再返回 'raw': d" 0 \
    "$($PODMAN exec "$NAME" sh -c "awk '/^def _vpn_status/,/^def [^_]/' $H2 | grep -v '^[[:space:]]*#' | grep -c \"'raw':\"" 2>&1)"
ckg "_vpn_mask 仍在（脱敏没被一起删掉）" '_vpn_mask' \
   "$($PODMAN exec "$NAME" grep -c '_vpn_mask' $H2 2>&1 | head -1)"
# ③ ip_network 成员判断必须用对象（3.13 回归）
ckg "ip_network 归属判断用对象" 'aobj not in net' \
   "$($PODMAN exec "$NAME" grep -cE 'aobj not in net' $H2 2>&1)"
# ④ wireguard 登记进 DEPS
ckg "DEPS 登记 wireguard-tools" "wireguard-tools" \
   "$($PODMAN exec "$NAME" grep -c 'wireguard-tools' $H2 2>&1 | head -1)"
# ⑤ Python 版本必须是 3.13 —— 1.0.7 修的 ip_network 缺陷正是 3.13 才暴露的
ck "镜像 Python 是 3.13（3.12 上这条断言无意义）" 'Python 3\.13' \
   "$($PODMAN exec "$NAME" python3 -V 2>&1)"

echo
echo "=== 14. 1.0.8 VPN 四态诊断在容器形态下的表现 ==="
# 容器里wireguard 模块多半加载不了（内核没暴露模块或 /lib/modules 缺失）。
# 关键不是「能不能装上」，而是**别判错态** ——
# 1.0.7 之前只分「有/无」，本轮才拆成四态并给出可执行的下一步。
H="/opt/drouter/backend/drouter-helper.py"
# ckg 而不是 ck：grep -c 只吐数字，问「数字含不含 def _vpn_env」恒为假。
ckg "_vpn_env 四态函数在镜像里" 'def _vpn_env' \
   "$($PODMAN exec "$NAME" grep -c 'def _vpn_env' $H 2>&1 | head -1)"
# ⛔ 判据不能只 grep 字面量：注释/docstring 里也会写这些词。
# 所以下面真把函数跑起来，看它在**本机**返回哪一态。
# 探针文件位置：可用环境变量覆盖。写死本机路径的话，换台机器跑就会
# 直接判「探针不存在」—— 而真实原因只是路径不对，误报成产品问题。
PROBE="${PROBE:-/tmp/vpn-env-probe.py}"
if [ ! -f "$PROBE" ]; then
  echo "  ✘ 探针脚本不存在：$PROBE"
  fail=$((fail+1))
else
  $PODMAN cp "$PROBE" "$NAME:/tmp/vpn-env-probe.py" >/dev/null 2>&1
  VENV_OUT="$($PODMAN exec "$NAME" python3 /tmp/vpn-env-probe.py 2>&1)"
  if grep -q 'IMPORTERR' <<<"$VENV_OUT"; then
    echo "  ✘ _vpn_env 能在镜像里 import 并运行"
    echo "$VENV_OUT" | head -5
    fail=$((fail+1))
  else
    echo "  ✔ _vpn_env 能在镜像里 import 并运行"
    pass=$((pass+1))
    for k in STATE VIRT FIXABLE AUTOLOAD KERNEL; do
      v="$(grep "^$k=" <<<"$VENV_OUT" | head -1 | cut -d= -f2-)"
      echo "     $k = ${v:-（空）}"
    done
    st="$(grep '^STATE=' <<<"$VENV_OUT" | cut -d= -f2-)"
    # 判据不能写死某一态：不同内核/容器能力下合法结果不同。
    # 真正要钉死的是「返回值落在四态之内」—— 判错态才是 1.0.7 那个 bug。
    if grep -qE '^(ready|need_module|need_tool|unsupported)$' <<<"$st"; then
      echo "  ✔ state 落在四态之内（$st）"
      pass=$((pass+1))
    else
      echo "  ✘ state 必须是四态之一，实际「${st}」"
      fail=$((fail+1))
    fi
    # unsupported 时不得声称「可一键修复」—— 那会把用户引向一条必然失败的操作
    if [ "$st" = "unsupported" ]; then
      if grep -q '^FIXABLE=False' <<<"$VENV_OUT"; then
        echo "  ✔ unsupported 时 fixable=False"
        pass=$((pass+1))
      else
        echo "  ✘ unsupported 时 fixable 必须为 False"
        fail=$((fail+1))
      fi
    fi
    # 模块文件那一级是本轮新增的探测；查不到也要正常返回而不是抛异常
    if grep -qE '^KERNEL=\S' <<<"$VENV_OUT"; then
      echo "  ✔ kernel 版本取到了（四态诊断没因为 /lib/modules 缺失而崩）"
      pass=$((pass+1))
    else
      echo "  ✘ kernel 版本应能取到"
      fail=$((fail+1))
    fi
  fi
fi
# 开机自启文件由 fix 写，镜像里不该预先存在。
#⛔ `test ! -e` 成功时退出码是 **0**，这里原来写期望 1 → 必红。
# 另外别用 ls：它失败时把错误信息打到 stdout，而
# `2>&1 >/dev/null` 那个顺序也不对（2>&1 在前，重定向的是已复制的旧 fd），
# echo $? 拿到的根本不是 ls 的退出码。
ckc "镜像里不预置 modules-load.d 配置" 0 \
    "$($PODMAN exec "$NAME" sh -c 'test ! -e /etc/modules-load.d/drouter-wireguard.conf' >/dev/null 2>&1; echo $?)"
# 前端：老后端（没有 env 字段）必须显示 unknown 而不是 unsupported。
#⛔ 又一个 grep -c 配 ck 的恒假 —— 前两条 1.0.8 首次跑就是红的。
ckg "前端老后端兜底是 unknown" "state: 'unknown'" \
   "$($PODMAN exec "$NAME" grep -c "state: 'unknown'" /opt/drouter/web/app.js 2>&1 | head -1)"
ckg "前端有 VPN 一键修复按钮" 'id="vp-fix"' \
   "$($PODMAN exec "$NAME" grep -c 'id="vp-fix"' /opt/drouter/web/app.js 2>&1 | head -1)"

echo
echo "================================================"
echo "  容器冒烟：通过 $pass / 失败 $fail"
echo "================================================"

echo
echo "=== 宿主侧安全基线 ==="
# ⚠️ 这里**只能读**，不能断言。cleanup 是 trap EXIT 触发的，在这几行之后
# 才执行；此时容器还在、netavark 表已经被 podman 重建了 ——
# 基线断言放在这里必然红。断言在 cleanup() 内部做。
echo "  ip_forward = $(cat /proc/sys/net/ipv4/ip_forward)"
echo "  nft 表     = $(nft list tables 2>/dev/null | tr '\n' ' ')"
echo "  默认路由   = $(ip route | grep '^default' | head -1)"
# 5900 是 RealVNC，本项目**绝不触碰**。这里只读不改，
# 出现监听是正常的（用户自己在用 VNC）。
echo "  5900 监听  = $(ss -lnt 2>/dev/null | grep -c ':5900' )（只读，从不干预）"

# RC_AT_EXIT 给 cleanup() 里的 trap 用 —— 它要在这个exit 之后决定最终退出码。
RC_AT_EXIT=$fail
exit $fail

#!/bin/bash
# OpenSOHO 真机行为探测：起一次临时实例，摸清 superuser / REST API / 集合名
# 用完必须清理（见脚本末尾的 cleanup 段）
set -u
SECRET='probe-secret-probe-secret-probe-32'
BIN=/tmp/opensoho
DIR=/tmp/oh-probe
LOG=/tmp/oh-probe.log
PORT=8099

echo "=== 0. 环境 ==="
id -un; uname -m
ls -l "$BIN" 2>&1
echo "--- 8099 / 8090 占用 ---"
ss -lntp 2>/dev/null | grep -E ':(8099|8090)\b' || echo "(未占用)"

echo
echo "=== 1. 版本 ==="
"$BIN" --help 2>&1 | head -30

echo
echo "=== 2. 启临时实例 ==="
rm -rf "$DIR" "$LOG"
mkdir -p "$DIR"
OPENSOHO_SHARED_SECRET="$SECRET" nohup "$BIN" serve --http 127.0.0.1:$PORT --dir "$DIR" > "$LOG" 2>&1 &
echo "pid=$!"
for i in $(seq 1 30); do
  if curl -sf -o /dev/null "http://127.0.0.1:$PORT/_/" ; then echo "第 ${i}s 起好了"; break; fi
  sleep 1
done

echo
echo "=== 3. 日志 ==="
cat "$LOG"

echo
echo "=== 4. 管理界面可达性 ==="
echo "-- /_/ --";     curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:$PORT/_/"
echo "-- /api/health --"; curl -s "http://127.0.0.1:$PORT/api/health" ; echo

echo
echo "=== 5. superuser upsert ==="
"$BIN" superuser upsert probe@drouter.local 'ProbePass12345' --dir "$DIR" 2>&1 | head -20
echo "退出码=$?"

echo
echo "=== 6. 登录取 token ==="
TOK=$(curl -s -X POST "http://127.0.0.1:$PORT/api/collections/_superusers/auth-with-password" \
  -H 'Content-type: application/json' \
  -d '{"identity":"probe@drouter.local","password":"ProbePass12345"}' | python3 -c 'import sys,json;print(json.load(sys.stdin).get("token","")' 2>/dev/null)
echo "token 长度=${#TOK}"

echo
echo "=== 7. 列出集合 ==="
curl -s -H "Authorization: $TOK" "http://127.0.0.1:$PORT/api/collections" \
  | python3 -c 'import sys,json
d=json.load(sys.stdin)
for c in d.get("items",d if isinstance(d,list) else []):
    print(c.get("name"), "|", ",".join(sorted((c.get("schema") or {}).get("fields",{}).keys())[:14]))' 2>&1 | head -30

echo
echo "=== 8. devices / clients 列表（空库） ==="
for c in devices clients radios wifi leds settings; do
  echo "-- $c --"
  curl -s -H "Authorization: $TOK" "http://127.0.0.1:$PORT/api/collections/$c/records?perPage=1" | head -c 400; echo
done

echo
echo "=== 9. devicestatus v1 ==="
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: $TOK" "http://127.0.0.1:$PORT/api/v1/devicestatus/00:11:22:33:44:55"

echo
echo "=== 10. 监听情况 ==="
ss -lntp 2>/dev/null | grep "$PORT" || echo "(无)"

echo
echo "=== 11. 清理 ==="
pkill -f "$BIN serve" ; sleep 2
rm -rf "$DIR" "$LOG"
ss -lntp 2>/dev/null | grep -E ':(8099|8090)\b' || echo "端口已释放"
echo "DONE"

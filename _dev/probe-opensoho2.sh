#!/bin/bash
# OpenSOHO 探测第 2 轮：带 secret 跑 superuser，拿 token，列真实集合名
set -u
SECRET='probe-secret-probe-secret-probe-32'
BIN=/tmp/opensoho
DIR=/tmp/oh-probe2
LOG=/tmp/oh-probe2.log
PORT=8099
export OPENSOHO_SHARED_SECRET="$SECRET"

echo "=== 0. 检查上轮是否污染了 home 目录 ==="
ls -ld /home/ajeef/pb_data 2>&1 || echo "(无 pb_data，--dir 生效了)"

echo
echo "=== 1. --help（带 secret） ==="
"$BIN" --help 2>&1 | head -40

echo
echo "=== 2. --dir 是否真生效（对比两处 pb_data） ==="
rm -rf "$DIR" "$LOG" /tmp/oh-nodir; mkdir -p "$DIR"
OPENSOHO_SHARED_SECRET="$SECRET" nohup "$BIN" serve --http 127.0.0.1:$PORT --dir "$DIR" > "$LOG" 2>&1 &
for i in $(seq 1 30); do curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/health" && break; sleep 1; done
echo "-- $DIR 下 --"; ls "$DIR" 2>&1 | head
echo "-- /home/ajeef/pb_data --"; ls /home/ajeef/pb_data 2>&1 | head -3

echo
echo "=== 3. superuser upsert（带 secret） ==="
"$BIN" superuser upsert probe@drouter.local 'ProbePass12345' --dir "$DIR" 2>&1 | head -20
echo "退出码=$?"
echo "-- 再跑一次（幂等？） --"
"$BIN" superuser upsert probe@drouter.local 'ProbePass12345' --dir "$DIR" 2>&1 | head -10
echo "退出码=$?"

echo
echo "=== 4. 登录取 token ==="
RESP=$(curl -s -X POST "http://127.0.0.1:$PORT/api/collections/_superusers/auth-with-password" \
  -H 'Content-type: application/json' \
  -d '{"identity":"probe@drouter.local","password":"ProbePass12345"}')
echo "响应前 200 字：${RESP:0:200}"
TOK=$(echo "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("token",""))' 2>/dev/null)
echo "token 长度=${#TOK}"

echo
echo "=== 5. 列出所有集合名 ==="
curl -s -H "Authorization: $TOK" "http://127.0.0.1:$PORT/api/collections?perPage=200" \
  | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
except Exception as e:
    print("解析失败", e); raise SystemExit
items=d.get("items",[])
print("共", d.get("totalItems"), "个集合")
for c in items:
    sch=c.get("schema") or {}
    print(" -", c.get("name"), "| type=", c.get("type"), "| fields=", ",".join((sch.get("schema") if isinstance(sch,dict) and "schema" in sch else sch).keys())[:160] if isinstance(sch,dict) else "")
' 2>&1 | head -40

echo
echo "=== 6. settings 集合（superuser 后） ==="
curl -s -H "Authorization: $TOK" "http://127.0.0.1:$PORT/api/collections/settings/records?perPage=50" | head -c 800; echo

echo
echo "=== 7. devicestatus v1（带 token） ==="
curl -s -w '\nHTTP=%{http_code}\n' -H "Authorization: $TOK" "http://127.0.0.1:$PORT/api/v1/devicestatus/00:11:22:33:44:55"

echo
echo "=== 8. 根目录有哪些 v1 路由（盲探几个） ==="
for p in /api/v1/devicestatus /api/v1/devices /api/v1/status /api/v1/device/00:11:22:33:44:55; do
  printf '%-40s ' "$p"
  curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: $TOK" "http://127.0.0.1:$PORT$p"
done

echo
echo "=== 9. 清理 ==="
pkill -f "$BIN serve"; sleep 2
rm -rf "$DIR" "$LOG" /home/ajeef/pb_data /tmp/oh-nodir
ss -lntp 2>/dev/null | grep -E ':(8099|8090)\b' || echo "端口已释放"
echo "DONE"

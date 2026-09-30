#!/bin/bash
# 前端契约回归：验证登录能拿到 token，且每个前端页面依赖的接口都返回 200
B=http://127.0.0.1:8080
echo "=== 1. 登录应返回真实 token ==="
RESP=$(curl -s $B/api/login -X POST -H 'Content-Type: application/json' \
      -d '{"username":"admin","password":"admin123"}')
echo "$RESP" | python3 -m json.tool --no-ensure-ascii
TOK=$(echo "$RESP" | python3 -c 'import sys,json;print((json.load(sys.stdin).get("data") or {}).get("token") or "")')
if [ -z "$TOK" ]; then echo "❌ token 为空 —— 登录仍坏"; exit 1; fi
echo "✅ token = ${TOK:0:16}..."

echo
echo "=== 2. 用该 token 请求各页面依赖接口 ==="
FAIL=0
check() {
  local ep="$1" m="${2:-GET}" body="${3:-}"
  local args=(-s -o /tmp/o.json -w '%{http_code}' "$B$ep" -H "X-Token: $TOK")
  [ "$m" = POST ] && args+=(-X POST -H 'Content-Type: application/json' -d "${body:-{\}}")
  local code=$(curl "${args[@]}")
  local ok=$(python3 -c 'import json;print(json.load(open("/tmp/o.json")).get("ok"))' 2>/dev/null)
  printf "  %-22s %-5s HTTP=%-4s ok=%-6s %s\n" "$ep" "$m" "$code" "$ok" \
    "$( [ "$code" = 200 ] && [ "$ok" = True ] && echo ✔ || { echo ✘; FAIL=1; } )"
}
# 概览页
check /api/sysinfo;  check /api/services; check /api/ifaces
check /api/ipv6;     check /api/routes
# 网卡/WAN/LAN
check /api/iface/meta; check /api/config
# 防火墙/日志/诊断/电源/用户/升级
check /api/nft; check /api/leases; check /api/users; check /api/ntp
check /api/logs; check /api/journal; check /api/audit
check /api/snapshot/list; check /api/upgrade/info
# 构建保护模式（前端用 GET）
check /api/buildmode GET
check /api/pppoe/saved
# 新增：Web 端口 / 网卡配置方式 / 拨号实时日志
check /api/webport
check /api/iface_method
check /api/ppp/log
# 新增：IPv6 连通性测试工具
check /api/diag POST '{"tool":"ipv6test","which":"cn"}'
# 新增：自动快照策略 / 紧急救援通道
check /api/snapshot/auto
check /api/rescue
# 新增批次（#17–#22）：通用 API / 依赖自检 / NAT / 软加速 / VLAN / WOL / 文件管理
check /api/openapi
check /api/examples
check /api/deps/check
check /api/vlan
check /api/wol
check /api/nat/last
check /api/accel POST '{"op":"get"}'
# 新增批次（#23–#25）：PPPoE 多拨 / QoS 智能限速 / DPI 应用识别
check /api/pppoe-multi
check /api/pppoe-multi POST '{"op":"get"}'
check /api/pppoe-multi/log POST '{"idx":1}'
check /api/qos
check /api/qos POST '{"op":"get"}'
check /api/dpi
check /api/dpi POST '{"op":"get"}'
check /api/dpi POST '{"op":"plan","which":"apt"}'
check /api/dpi/log
check '/api/files/list?path=/etc/drouter'
check '/api/files/read?path=/etc/drouter/snapshot.conf'
# 写操作：只保存（不应用）
check /api/config POST '{"module":"system","data":{"hostname":"debian-primaryrouter"}}'

echo
echo "=== 3. 关键字段核对 ==="
curl -s $B/api/buildmode -H "X-Token: $TOK" | python3 -c '
import sys,json;d=json.load(sys.stdin)
print("  buildmode.data.enabled =", d["data"]["enabled"], "(解除后应为 False)")'
curl -s $B/api/ifaces -H "X-Token: $TOK" | python3 -c '
import sys,json;d=json.load(sys.stdin)
for i in d["data"]:
    if i["name"]=="lo": continue
    n=i.get("name"); m=i.get("mac"); st=i.get("oper") or i.get("operstate") or ""
    print("  %-8s mac=%s state=%s" % (n, m, st))'
curl -s $B/api/routes -H "X-Token: $TOK" | python3 -c '
import sys,json;d=json.load(sys.stdin)
print("  默认路由:", [(r["gateway"], r["dev"]) for r in d["data"]["v4"] if r["dst"]=="default"])'

echo
[ $FAIL = 0 ] && echo "FRONTEND_CONTRACT_OK ✔ 全部接口正常" || echo "FRONTEND_CONTRACT_FAIL ❌ 存在异常接口"

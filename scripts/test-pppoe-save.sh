#!/bin/bash
# 模拟“用户在 WEB 里填写 PPPoE 账号密码后点击保存并应用”的完整链路
B=http://127.0.0.1:8080
TOK=$(curl -s $B/api/login -X POST -H 'Content-Type: application/json' \
      -d '{"username":"admin","password":"admin123"}' \
      | python3 -c 'import sys,json;print((json.load(sys.stdin).get("data") or {}).get("token",""))')
[ -z "$TOK" ] && { echo "登录失败"; exit 1; }

echo "=== A. 保存 pppoe 模块配置（仅保存）==="
curl -s $B/api/save -X POST -H "X-Token: $TOK" -H 'Content-Type: application/json' \
  -d '{"module":"pppoe","data":{"iface":"ens18","username":"user@test","password":"secret999","isp":"ct","mtu":1492}}' \
  | python3 -m json.tool --no-ensure-ascii 2>/dev/null || echo "(接口不存在，尝试 apply)"

echo
echo "=== B. 通过 apply 接口写盘（构建保护模式下 live=false）==="
curl -s $B/api/apply -X POST -H "X-Token: $TOK" -H 'Content-Type: application/json' \
  -d '{"module":"ppp","data":{"iface":"ens18","username":"user@test","password":"secret999","isp":"ct","mtu":1492},"live":false}' \
  | python3 -m json.tool --no-ensure-ascii

echo
echo "=== C. 落盘结果 ==="
echo "--- /etc/ppp/peers/drouter-wan ---"; cat /etc/ppp/peers/drouter-wan 2>/dev/null | head -12
echo "--- /etc/ppp/chap-secrets ---";  cat /etc/ppp/chap-secrets 2>/dev/null
echo "--- 权限 ---"; ls -l /etc/ppp/peers/drouter-wan /etc/ppp/chap-secrets 2>/dev/null

echo
echo "=== D. 服务是否仍未被启动（安全不变量）==="
for s in pppd dnsmasq; do printf "  %-8s %s\n" "$s" "$(systemctl is-active $s 2>&1)"; done
ss -lnu | grep -E ':53 |:67 ' && echo "  端口被占用(异常)" || echo "  53/67 仍空闲 ✔"
echo "PPPOE_SAVE_PATH_OK"

#!/bin/bash
# 认领新网卡 ens19 为 WAN 口
B=http://127.0.0.1:8080
TOK=$(curl -s $B/api/login -X POST -H 'Content-Type: application/json' \
      -d '{"username":"admin","password":"admin123"}' \
      | python3 -c 'import sys,json;print((json.load(sys.stdin).get("data") or {}).get("token",""))')

curl -s $B/api/iface/meta -H "X-Token: $TOK" | python3 -m json.tool --no-ensure-ascii
echo

echo "=== 认领 ens19 为 WAN ==="
curl -s $B/api/iface/save -X POST -H "X-Token: $TOK" -H 'Content-Type: application/json' \
  -d '{"mac":"bc:24:11:f2:5a:47","name":"ens19","remark":"WAN口（PPPoE拨号）","role":"wan"}' \
  | python3 -m json.tool --no-ensure-ascii

echo
echo "=== 确认 ens18 仍为 LAN ==="
curl -s $B/api/iface/save -X POST -H "X-Token: $TOK" -H 'Content-Type: application/json' \
  -d '{"mac":"bc:24:11:1a:8f:43","name":"ens18","remark":"LAN管理口","role":"lan"}' \
  | python3 -m json.tool --no-ensure-ascii

echo
echo "=== 结果 ==="
sqlite3 -header -column /opt/drouter/data/drouter.db 'select mac,name,remark,role from ifaces;'
echo
echo "--- system 配置 ---"
sqlite3 /opt/drouter/data/drouter.db "select value from settings where key='system';" | python3 -m json.tool --no-ensure-ascii

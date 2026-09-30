#!/bin/bash
# drouter 八模块回归自检（构建保护模式下应全部写成到磁盘，不启动服务）
B=http://127.0.0.1:8080
C="curl -s"
TOK=$($C $B/api/login -X POST -H 'Content-Type: application/json' \
      -d '{"username":"admin","password":"admin123"}' \
      | python3 -c 'import sys,json;d=json.load(sys.stdin);print((d.get("data") or {}).get("token",""))')
if [ -z "$TOK" ]; then echo "登录失败"; exit 1; fi
echo "token ok: ${TOK:0:12}..."

A(){ $C $B/api/apply -X POST -H "X-Token: $TOK" -H 'Content-Type: application/json' -d "$1" \
     | python3 -c 'import sys,json;d=json.load(sys.stdin);print("ok=",d.get("ok"),"|",d.get("msg_cn") or d.get("code") or "")'; }

echo "--- 1 dnsmasq ---"
A '{"module":"dnsmasq","data":{"lan_iface":"ens18","lan_ip":"192.168.7.3","dhcp_enabled":false,"pool_start":"192.168.7.100","pool_end":"192.168.7.200","lease_time":43200,"dns_mode":"custom","dns_custom":"223.5.5.5,119.29.29.29","options":[{"enabled":true,"code":"6","value":"192.168.7.3"},{"enabled":true,"code":"3","value":"192.168.7.3"}]}}'
echo "--- 2 nft_v4 ---"
A '{"module":"nft_v4","data":{"wan_iface":"","lan_iface":"ens18","allow_estab":true,"icmp":true,"input_policy":"drop"}}'
echo "--- 3 nft_v6 ---"
A '{"module":"nft_v6","data":{"wan_iface":"","lan_iface":"ens18","allow_estab":true,"icmpv6":true,"input_policy":"drop"}}'
echo "--- 4 radvd ---"
A '{"module":"radvd","data":{"lan_iface":"ens18","prefix":"fd00:7::/64","adv_link_mtu":1500}}'
echo "--- 5 dhcpv6 ---"
A '{"module":"dhcpv6","data":{"wan_iface":"","lan_iface":"ens18","pd_len":60}}'
echo "--- 6 upnp ---"
A '{"module":"upnp","data":{"enable":false,"lan_iface":"ens18","wan_iface":""}}'
echo "--- 7 ntp ---"
A '{"module":"ntp","data":{"servers":["ntp.aliyun.com","time.cloudflare.com"]}}'
echo "--- 8 ppp ---"
A '{"module":"ppp","data":{"iface":"ens18","username":"test12345","password":"pass67890","isp":"ct","mtu":1492}}'

echo "=== 生成文件落盘检查 ==="
ls -la /etc/dnsmasq.d/drouter.conf /etc/nftables.d/drouter-v4.nft /etc/nftables.d/drouter-v6.nft \
       /etc/radvd.conf /etc/dhcpcd.conf /etc/miniupnpd/miniupnpd.conf /etc/chrony/chrony.conf \
       /etc/ppp/peers/drouter-wan 2>&1

echo "=== 安全不变量 ==="
echo -n "dnsmasq: "; systemctl is-active dnsmasq 2>&1
echo -n "radvd:   "; systemctl is-active radvd 2>&1
echo -n "dhcpcd:  "; systemctl is-active dhcpcd 2>&1
echo -n "miniupnpd:"; systemctl is-active miniupnpd 2>&1
echo -n "chrony:  "; systemctl is-active chrony 2>&1
echo -n "53端口:  "; ss -lnu | grep -c ':53 ' || true
echo -n "67端口:  "; ss -lnu | grep -c ':67 ' || true
echo -n "VNC5900: "; ss -lnt | grep -c ':5900'
echo -n "BUILD_MODE: "; test -f /etc/drouter/BUILD_MODE && echo "存在(保护中)" || echo "不存在"
echo -n "ens18:   "; ip -4 -o addr show ens18 | awk '{print $4}'
echo "=== nft 规则 ==="
nft list ruleset 2>/dev/null | head -5 || echo "nftables 表为空"
echo "REGRESSION_DONE"

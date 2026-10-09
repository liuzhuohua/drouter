#!/bin/bash
# 部署后核验（记忆里的「部署后核验六项」）。
#
# ⛔ 为什么是 bash 而不是 node：rsh.sh 依赖 SSH_ASKPASS + SSH_ASKPASS_REQUIRE=force，
#    必须经 shell 启动。2026-10-05 先写 Node 版，用 execFileSync('bash', …) 在
#    Windows 上报 `spawnSync bash EBUSY` —— 11 项全红且看不出真机到底怎么了。
#    教训：**跨 Windows/SSH 的调用交给 shell，别在 Node 里 spawn bash**。
#
# 用法：bash devtools/verify-deploy.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
RSH="$HERE/rsh.sh"

fails=0; n=0
ck() {  # ck <名称> <条件结果0/1> [详情]
  n=$((n + 1))
  if [ "$2" = "0" ]; then
    printf '  ok    %s\n' "$1"
  else
    fails=$((fails + 1))
    printf '  FAIL  %s  %s\n' "$1" "${3:-}"
  fi
}

echo "=== 部署后核验 ==="
echo ""

# ---- 1) 本地与真机 sha256 一致（确认测的是新代码）----
echo "--- 1. 文件一致性（sha256 前 16 位）---"
for pair in "web/app.js:/opt/drouter/web/app.js" \
            "web/i18n.js:/opt/drouter/web/i18n.js" \
            "web/app.css:/opt/drouter/web/app.css" \
            "backend/drouter-helper.py:/opt/drouter/backend/drouter-helper.py" \
            "backend/drouter-web.py:/opt/drouter/backend/drouter-web.py"; do
  loc="${pair%%:*}"; rem="${pair##*:}"
  lh=$(sha256sum "$ROOT/$loc" 2>/dev/null | cut -c1-16)
  rh=$(bash "$RSH" "sha256sum $rem 2>/dev/null | cut -c1-16" 2>/dev/null | tr -d '\r' | head -1)
  if [ -n "$lh" ] && [ "$lh" = "$rh" ]; then ck "$loc" 0; else ck "$loc" 1 "本地=$lh 真机=$rh"; fi
done

# ---- 2) Web 服务可访问 + 版本注入 ----
echo "--- 2. Web 服务 ---"
# ⚠️ 别截断：版本号注入在 <footer> 的 id="app-ver" 里，位置靠后。
#    2026-10-05 用 head -c 5000 把它截掉了，判据报「未找到版本号」，
#    实际 v1.0.9 好好地在那儿 —— 判据假红。
home=$(bash "$RSH" "curl -sk https://127.0.0.1:8443/" 2>/dev/null | tr -d '\r')
if [ -z "$home" ]; then
  ck "首页可访问" 1 "无输出"
else
  ck "首页可访问" 0
  ver=$(printf '%s' "$home" | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | head -1)
  if [ -n "$ver" ]; then ck "首页带版本号（$ver）" 0; else ck "首页带版本号" 1 "未找到"; fi
  if printf '%s' "$home" | grep -q 'i18n.js'; then ck "首页引入 i18n.js" 0; else ck "首页引入 i18n.js" 1; fi
  # ⚠️ 顺序比「有没有引入」更关键（2026-10-05 整站登录失效的事故）：
  #    app.js 顶层就有 t() 调用，i18n.js 排在它之后 → 加载 app.js 时
  #    ReferenceError → 整个 app.js 挂掉 → 登录按钮没绑事件。
  #    静态检查器都给 app.js 备了 t 桩，**永远抓不到这个问题**。
  line_i18n=$(printf '%s' "$home" | grep -n 'src="/i18n.js' | head -1 | cut -d: -f1)
  line_app=$(printf '%s' "$home" | grep -n 'src="/app.js' | head -1 | cut -d: -f1)
  if [ -n "$line_i18n" ] && [ -n "$line_app" ] && [ "$line_i18n" -lt "$line_app" ]; then
    ck "i18n.js 排在 app.js 之前（顺序错会导致整站登录失效）" 0
  else
    ck "i18n.js 排在 app.js 之前" 1 "i18n.js@第${line_i18n:-?}行 app.js@第${line_app:-?}行"
  fi
fi

# ---- 3) i18n.js 可下载且含 raw 分组 ----
echo "--- 3. i18n 静态资源 ---"
sz=$(bash "$RSH" "curl -sk https://127.0.0.1:8443/i18n.js | wc -c" 2>/dev/null | tr -d '\r' | tr -d ' ')
if [ "${sz:-0}" -gt 200000 ] 2>/dev/null; then ck "i18n.js 可下载（$sz 字节）" 0; else ck "i18n.js 可下载" 1 "实测 $sz 字节"; fi
rawc=$(bash "$RSH" "curl -sk https://127.0.0.1:8443/i18n.js | grep -c 'raw: {'" 2>/dev/null | tr -d '\r' | tr -d ' ')
if [ "$rawc" = "1" ]; then ck "i18n.js 含 raw 分组" 0; else ck "i18n.js 含 raw 分组" 1 "grep -c=$rawc"; fi
rawN=$(bash "$RSH" "curl -sk https://127.0.0.1:8443/i18n.js | sed -n '/raw: {/,/^    },/p' | grep -c ': { zh: '" 2>/dev/null | tr -d '\r' | tr -d ' ')
if [ "${rawN:-0}" -gt 500 ] 2>/dev/null; then ck "raw 词条数 $rawN（>500）" 0; else ck "raw 词条数" 1 "实测 $rawN"; fi

# ---- 4) 后端服务 ----
echo "--- 4. 服务状态 ---"
for svc in drouter-web drouter-helpd drouter-snapshot.timer; do
  st=$(bash "$RSH" "systemctl is-active $svc" 2>/dev/null | tr -d '\r' | head -1)
  if [ "$st" = "active" ]; then ck "$svc active" 0; else ck "$svc" 1 "is-active=$st"; fi
done

# ---- 5) 网络与 VNC 基线零变化（记忆里的红线）----
echo "--- 5. 网络 / VNC 基线 ---"
ip=$(bash "$RSH" "ip -4 addr show ens18 | grep -oE 'inet [0-9.]+' | head -1" 2>/dev/null | tr -d '\r')
if printf '%s' "$ip" | grep -q '192.168.7.3'; then ck "LAN 地址未变（$ip）" 0; else ck "LAN 地址" 1 "$ip"; fi
vnc=$(bash "$RSH" "ss -ltn | grep -c ':5900'" 2>/dev/null | tr -d '\r' | tr -d ' ')
if [ "${vnc:-0}" -ge 1 ] 2>/dev/null; then ck "RealVNC 5900 仍在监听" 0; else ck "RealVNC 5900" 1 "grep -c=$vnc"; fi
nft=$(bash "$RSH" "/sbin/nft list ruleset 2>/dev/null | grep -c '^table'" 2>/dev/null | tr -d '\r' | tr -d ' ')
drouter_nft=$(bash "$RSH" "/sbin/nft list ruleset 2>/dev/null | grep -c 'table inet drouter'" 2>/dev/null | tr -d '\r' | tr -d ' ')
if [ "${drouter_nft:-0}" = "0" ]; then ck "未接管路由（drouter 自己的 nft 表 0 张，与部署前基线一致）" 0
else ck "nft 基线" 1 "table 总数=$nft drouter=$drouter_nft"; fi

# ---- 6) 终端 socket ----
echo "--- 6. 终端 socket ---"
sk=$(bash "$RSH" "test -S /run/drouter/shell.sock && echo yes || echo no" 2>/dev/null | tr -d '\r' | head -1)
if [ "$sk" = "yes" ]; then ck "终端 socket 就位" 0; else ck "终端 socket" 1 "$sk"; fi

echo ""
echo "==== 部署核验: $n 项, $fails 失败 ===="
[ "$fails" = "0" ] || exit 1
echo "DEPLOY_VERIFY_OK"

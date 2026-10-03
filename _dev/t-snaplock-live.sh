#!/bin/bash
# 快照保护 + 备注可编辑 的真机验收
#
# 走真实 HTTP 接口（登录取 X-Token），不直接调 helper ——
# 因为这一轮改的**三处**都在这条链路上：web 层两个新路由、
# helper 两个新 action、以及审计写入。只测 helper 会漏掉
# 「前端 POST /api/snapshot/note 但后端没这个路由」这种最可能的错。
#
# 跑法：scp 过去再执行（记忆：真机脚本必须 scp，本机直连会 WinError 10061）
#   ./devtools/rscp.sh _dev/t-snaplock-live.sh /tmp/ && \
#   ./devtools/rsudo.sh "bash /tmp/t-snaplock-live.sh"
set -u
BASE="http://127.0.0.1:8080"
H="/opt/drouter"
OK=0; NG=0
MARK="__T108_LIVE__"

say()  { printf '%s\n' "$*"; }

chk() { # chk <描述> <实际> <期望>
  if [ "$2" = "$3" ]; then say "[OK] $1"; OK=$((OK+1))
  else say "[NG] $1"; say "      期望: $3"; say "      实际: $2"; NG=$((NG+1)); fi
}

# 判据用 grep -c 时**必须写死期望值**。写成
#   chk "xxx" "$(printf '%s' "$JS" | grep -c foo)" "$(printf '%s' "$JS" | grep -c foo)"
# 是恒真的 —— 两边算的是同一个东西，永远相等，红不了。
# 那种判据比没有判据更糟：它给人「这一项验过了」的错觉。
#
# 而且 grep 是**全文**匹配，含注释 —— 而这批代码里注释密度很高
# （写明了「此前这里是 if (S.page === 'sys') ... else modal(...) 的分叉」、
# 「上一版用的是 shutil.copy2」这类）。第一版 grep -c 数出来 2 而不是 1，
# 差的都是注释。判据指向的东西被注释提一句就变红，这种判据只会被
# 人调成「>= 1」然后彻底失效。
# 所以：**剥掉注释再数**，只数真代码。
if [ ! -f /tmp/jsstrip.py ]; then echo '  （/tmp/jsstrip.py 缺失，注释剥离不可用）'; fi
strip_js() { python3 - "$1" <<'PY'
import io, sys
sys.path.insert(0, '/tmp')
try:
    from jsstrip import js_code_only
    print(js_code_only(io.open(sys.argv[1], encoding='utf-8').read()))
    sys.exit(0)
except Exception as e:
    sys.stderr.write('剥离失败：%s\n' % e)
    sys.exit(3)
PY
}

JS_RAW=$(cat "$H/web/app.js" 2>/dev/null)
JS=$(strip_js "$H/web/app.js")
if [ -z "$JS" ]; then
  # 剥不动就退回原文，但**必须把这一点说出来** ——
  # 静默降级会让后面所有 grepc 的期望值失去依据。
  JS="$JS_RAW"
  say "  ⚠️ 注释剥离失败，源码类判据改为原文匹配（可能因注释误报）"
else
  say "  （已剥注释后匹配：$(printf '%s' "$JS_RAW" | wc -l) → $(printf '%s' "$JS" | wc -l) 行）"
fi

grepc() { # grepc <needle> <描述> <期望个数>
  local n
  n=$(printf '%s' "$JS" | grep -c -- "$1" || true)
  if [ "$n" = "$3" ]; then say "[OK] $2（$3 处）"; OK=$((OK+1))
  else say "[NG] $2：期望 $3 处，实际 $n 处"; NG=$((NG+1)); fi
}

# 只在**某个函数体内**数。
# 判据得指向「哪一段代码」，不能扫全文 —— app.js 里
# S.page === 'xxx' 有几十处（菜单高亮、切页清理、路由守卫），
# 扫全文时「还剩 1 处」根本不能说明是哪儿，只能让人回去肉眼找。
# 判得越宽，越容易被无关改动带红，然后被人调成「>= 1」彻底失效。
#
# 边界用「起始行 → 下一个同缩进的顶层声明」来找，而不是「下一个
# function 关键字」—— 后者会在函数体里嵌套了函数时提前截断。
func_body() { # func_body <函数名> → 打印该函数体
  local start end
  start=$(printf '%s\n' "$JS" | grep -n "^ *\(async \)\?function $1\b" \
          | head -1 | cut -d: -f1)
  if [ -z "$start" ]; then return 1; fi
  end=$(printf '%s\n' "$JS" | awk -v s="$start" -v fn="$1" '
    NR > s && $0 ~ "^ *(async )?function " && $0 !~ ("function " fn "\\b") {
      print NR - 1; exit
    }')
  if [ -z "$end" ] || [ "$end" -le "$start" ]; then
    # 后面没有同级的下一个函数 → 取到文件尾
    printf '%s\n' "$JS" | tail -n "+$start"
  else
    printf '%s\n' "$JS" | sed -n "${start},${end}p"
  fi
}

grepc_in() { # grepc_in <函数名> <needle> <描述> <期望个数>
  local n
  n=$(func_body "$1" | grep -c -- "$2" || true)
  if [ "$n" = "$4" ]; then say "[OK] $3（$4 处）"; OK=$((OK+1))
  else say "[NG] $3：期望 $4 处，实际 $n 处"; NG=$((NG+1)); fi
}

# CSS 直接数选择器行，不数注释。判据写成「有几行规则命中」，
# 免得把选择器 + :focus 伪元素算成两次而误判。
cssc() { # cssc <needle> <描述> <期望命中行数>
  local n
  n=$(grep -c -- "$1" "$H/web/app.css" 2>/dev/null || echo 0)
  if [ "$n" = "$3" ]; then say "[OK] $2（$3 行）"; OK=$((OK+1))
  else say "[NG] $2：期望 $3 行，实际 $n 行"; NG=$((NG+1)); fi
}

contains() { # contains <描述> <haystack> <needle>
  case "$2" in *"$3"*) say "[OK] $1"; OK=$((OK+1));;
  *) say "[NG] $1"; say "      未找到: $3"; say "      实际: $(printf '%s' "$2" | head -c 240)"; NG=$((NG+1));; esac
}

call() { # call <path> <json-body> -> 响应 JSON
  curl -s --max-time 30 -X POST "$BASE$1" \
    -H "X-Token: $TOK" -H 'Content-Type: application/json' -d "$2"
}

jqv() { printf '%s' "$1" | python3 -c "import json,sys
try: d=json.load(sys.stdin)
except Exception: print('<PARSE-FAIL>'); sys.exit()
$2" 2>/dev/null
}

meta() { # meta <ts> <python 表达式，d 是 meta dict>
  python3 -c "import json,sys
try: d=json.load(open('$H/snapshots/$1/_meta.json'))
except Exception as e: print('<META-FAIL>'); sys.exit()
print($2)" 2>&1
}

say "=== 0. 登录 ==="
LOGIN=$(curl -s --max-time 20 -X POST "$BASE/api/login" \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}')
TOK=$(jqv "$LOGIN" "print(d.get('data',{}).get('token',''))")
if [ -z "$TOK" ] || [ "$TOK" = "<PARSE-FAIL>" ]; then
  say "[NG] 登录失败：$LOGIN"
  say ""
  say "通过 0 项，失败 1 项"
  exit 1
fi
say "[OK] 登录拿到 token"

say ""
say "=== 1. 基线：备份用户现有快照 ==="
# 验收前整体备份，跑完原样还原。
# 这一步不能省：prune 是**会真删文件**的动作，在用户真机的
# 真实快照目录上跑验收，删错了没法恢复。
BK=/tmp/drouter-snap-backup-$$
rm -rf "$BK"; mkdir -p "$BK"
cp -a "$H/snapshots/." "$BK/" 2>/dev/null
BASE_LIST=$(curl -s --max-time 20 "$BASE/api/snapshots" -H "X-Token: $TOK")
BASE_N=$(jqv "$BASE_LIST" "print(len(d.get('data',{}).get('items',[])))")
say "  现有 $BASE_N 份，已备份到 $BK"
say "  （本脚本要跑 prune，会真删文件；结束时整体还原）"

say ""
say "=== 2. 创建一份「创建时就上锁」的快照 ==="
TS=""
R=$(call /api/snapshot "{\"tag\":\"$MARK locked\",\"protected\":true}")
TS=$(jqv "$R" "print(d.get('data',{}).get('ts',''))")
if [ -z "$TS" ]; then
  say "[NG] 创建失败：$R"; NG=$((NG+1))
else
  say "[OK] 创建上锁快照 $TS"
  chk "创建响应带回 protected:true" "$(jqv "$R" "print(d.get('data',{}).get('protected'))")" "True"
  contains "提示语点明「已上锁，不参与自动清理」" "$R" "已上锁"
  chk "_meta.json 落盘存在" "$([ -f "$H/snapshots/$TS/_meta.json" ] && echo y || echo n)" "y"
  chk "meta 里 protected 为 true" "$(meta "$TS" "d['protected']")" "True"
fi

say ""
say "=== 3. 备注可编辑 ==="
R=$(call /api/snapshot/note "{\"ts\":\"$TS\",\"tag\":\"$MARK 改过的备注\"}")
contains "改备注成功" "$R" '"ok": true'
chk "备注真的落进 meta" "$(meta "$TS" "d['tag']")" "$MARK 改过的备注"

say ""
say "--- 3b. 备注消毒：控制字符 / 超长 / 纯空白 ---"
call /api/snapshot/note "{\"ts\":\"$TS\",\"tag\":\"a\nb\tc  \"}" >/dev/null
chk "换行/制表被压成空格" "$(meta "$TS" "repr(d['tag'])")" "'a b c'"
LONG=$(python3 -c "print('x'*200)")
call /api/snapshot/note "{\"ts\":\"$TS\",\"tag\":\"$LONG\"}" >/dev/null
LEN=$(meta "$TS" "len(d['tag'])")
chk "超长备注被截到 <= 61" "$([ "${LEN:-999}" -le 61 ] 2>/dev/null && echo y || echo n)" "y"
call /api/snapshot/note "{\"ts\":\"$TS\",\"tag\":\"   \"}" >/dev/null
chk "纯空白清空后是空串（不退回 manual 占位）" "$(meta "$TS" "repr(d['tag'])")" "''"

say ""
say "=== 4. 列表里看得出保护状态（老快照不报错） ==="
L=$(curl -s --max-time 20 "$BASE/api/snapshots" -H "X-Token: $TOK")
chk "新快照在列表里 protected=true" \
  "$(jqv "$L" "print([x.get('protected') for x in d['data']['items'] if x['ts']=='$TS'][0])")" "True"
chk "每一行都有 protected 字段（老快照为 False）" \
  "$(jqv "$L" "print(all('protected' in x for x in d['data']['items']))")" "True"

say ""
say "=== 5. 上锁的快照：prune 不删它 ==="
# keep_count=1 是最狠的压法：正常只留最新 1 份自动快照。
# 上锁的那份若被删，说明 protected 判据没生效。
R=$(call /api/snapshot/prune '{"keep_days":0,"keep_count":1,"keep_manual":true}')
contains "prune 正常返回" "$R" '"ok": true'
if [ -d "$H/snapshots/$TS" ]; then say "[OK] 上锁快照目录还在"; OK=$((OK+1))
else say "[NG] 上锁快照被 prune 删了"; NG=$((NG+1)); fi
contains "prune 回传 kept_locked" "$R" "kept_locked"
contains "kept_locked 里含本次快照" "$R" "$TS"

say ""
say "=== 6. 上锁 ≠ 删不掉：needs_force 门禁 ==="
R=$(call /api/snapshot/delete "{\"ts\":\"$TS\"}")
contains "普通删除被拦" "$R" "needs_force"
contains "拦下时给的是可读原因" "$R" "自动清理不会删除它"
chk "此时目录还在（真没删）" "$([ -d "$H/snapshots/$TS" ] && echo y || echo n)" "y"
R=$(call /api/snapshot/delete "{\"ts\":\"$TS\",\"force\":true}")
contains "带 force 后删掉" "$R" '"ok": true'
chk "目录真的没了" "$([ -d "$H/snapshots/$TS" ] && echo y || echo n)" "n"

say ""
say "=== 7. 强制删除要留审计痕迹 ==="
TS2=$(jqv "$(call /api/snapshot "{\"tag\":\"$MARK 审计用\"}")" "print(d.get('data',{}).get('ts',''))")
if [ -n "$TS2" ]; then
  call /api/snapshot/protect "{\"ts\":\"$TS2\",\"protected\":true}" >/dev/null
  call /api/snapshot/delete "{\"ts\":\"$TS2\",\"force\":true}" >/dev/null
  A=$(curl -s --max-time 20 "$BASE/api/audit?limit=20" -H "X-Token: $TOK")
  contains "审计记录了「受保护，已强制删除」" "$A" "受保护，已强制删除"
  contains "审计带上了快照 ts" "$A" "$TS2"
else
  say "[NG] 审计用例的前置快照没建成"; NG=$((NG+1))
fi

say ""
say "=== 8. 上锁/解锁来回切换 ==="
TS3=$(jqv "$(call /api/snapshot "{\"tag\":\"$MARK toggle\"}")" "print(d.get('data',{}).get('ts',''))")
if [ -n "$TS3" ]; then
  # 前端开关是显式传 protected 的，走的是下面第 3 条；
  # 这两条验的是 helper 的「不传就取反」语义在 HTTP 路上也能用。
  chk "不带 protected 时取反成 true（web 层不能无条件透传该键）" \
    "$(jqv "$(call /api/snapshot/protect "{\"ts\":\"$TS3\"}")" "print(d.get('data',{}).get('protected'))")" "True"
  chk "再取反回 false" \
    "$(jqv "$(call /api/snapshot/protect "{\"ts\":\"$TS3\"}")" "print(d.get('data',{}).get('protected'))")" "False"
  chk "显式带 protected:true 生效" \
    "$(jqv "$(call /api/snapshot/protect "{\"ts\":\"$TS3\",\"protected\":true}")" "print(d.get('data',{}).get('protected'))")" "True"
  chk "切换后 meta 与响应一致" "$(meta "$TS3" "d['protected']")" "True"
  call /api/snapshot/delete "{\"ts\":\"$TS3\",\"force\":true}" >/dev/null
else
  say "[NG] 切换用例的前置快照没建成"; NG=$((NG+1))
fi

say ""
say "=== 9. 非法输入不能打穿 ==="
contains "路径穿越被拒" "$(call /api/snapshot/note '{"ts":"../../etc","tag":"x"}')" '"ok": false'
contains "非时间戳格式被拒" "$(call /api/snapshot/note '{"ts":"not-a-ts"}')" '"ok": false'
contains "不存在的合法格式 ts 被拒" \
  "$(call /api/snapshot/protect '{"ts":"20240101-000000","protected":true}')" '"ok": false'
contains "空 ts 被拒" "$(call /api/snapshot/delete '{"ts":""}')" '"ok": false'

say ""
say "=== 10. 界面：列表已内嵌到紧急救援通道 ==="
# ⚠️ 这里**绝对不能**再写一遍 `JS=$(cat "$H/web/app.js")`。
# 第一版在下面又赋了一次值，把上面辛辛苦苦剥好注释的 $JS 覆盖回原文 ——
# 于是 grepc 数出来的其实是「代码 + 注释」，needs_force 因此从 1 变 2，
# S.page 分叉那条也被注释里的说明文字命中。查了半天才发现是变量被覆盖，
# 而不是代码有问题。**同一份数据只能有一个来源。**
if [ -z "$JS" ]; then say "[NG] 读不到 app.js"; NG=$((NG+1)); else
  # 期望值写死是有讲究的：这里数的是**真代码**里的出现次数，
  # 而 data-lock / data-save-tag 天然就是两处 ——
  # ①模板里渲染出这一列，②事件绑定时 $$('[data-lock]') 选中它们。
  # 只出现一次反而说明绑定代码被删了。所以别把它们当「1 处」来数。
  grepc 'id="sy-snapout"'  "容器 #sy-snapout 存在（救援通道页的列表容器）" 1
  grepc 'id="sy-protect"'  "带「创建后上锁」勾选" 1
  grepc 'needs_force'      "删受保护快照会追加二次确认（读后端标记）" 1
  grepc "仍要删除"          "二次确认按钮文案是「仍要删除」" 1
  grepc 'data-save-tag'    "「存备注」按钮：模板 1 处 + 事件绑定 1 处" 2
  grepc 'data-lock'        "上锁开关：模板 1 处 + 事件绑定 1 处" 2
  # ⚠️ 别写「整个 app.js 里不许出现 S.page === 'sys'」——
  # app.js 里 S.page === 'xxx' 有几十处（菜单高亮、切页清理），
  # 'sys' 只是其中一个。第一版就是这么写的，数出 1 处就红了，
  # 逼得人去核对「到底哪儿还有」。要判的是**快照列表那段代码里**
  # 还有没有按页分叉，所以只看 listSnapshots 附近。
  grepc_in listSnapshots "S.page === 'sys'" \
      "旧的按页分叉已删（只看 listSnapshots 附近）" 0
  chk "fw-rollback 改为跳转 sys 页" \
    "$(printf '%s' "$JS" | grep -A4 "fw-rollback" | grep -c "go('sys')")" "1"
  cssc '^\.snap-tag-input' "CSS 里有 .snap-tag-input 规则（选择器行）" 2
fi

say ""
say "=== 11. 还原：把用户原有快照整体放回 ==="
# prune 跑过了，目录状态已被改动。整体还原到验收前的状态，
# 而不是「尽量补回几份」——后者会让用户目录处于一个说不清的状态。
rm -rf "$H/snapshots"
mkdir -p "$H/snapshots"
cp -a "$BK/." "$H/snapshots/" 2>/dev/null
rm -rf "$BK"
AFTER_N=$(jqv "$(curl -s --max-time 20 "$BASE/api/snapshots" -H "X-Token: $TOK")" \
  "print(len(d.get('data',{}).get('items',[])))")
if [ "${AFTER_N:-0}" -ge "$BASE_N" ] 2>/dev/null; then
  say "[OK] 原有快照已还原（验收前 $BASE_N 份，现在 $AFTER_N 份）"
  OK=$((OK+1))
else
  say "[NG] 还原后数量异常：验收前 $BASE_N，现在 $AFTER_N"
  NG=$((NG+1))
fi

say ""
say "============================================================"
say "通过 $OK 项，失败 $NG 项"
[ "$NG" -gt 0 ] && say "  失败项需逐条查上面 [NG] 行"
say "============================================================"
if [ "$NG" -gt 0 ]; then exit 1; fi
exit 0
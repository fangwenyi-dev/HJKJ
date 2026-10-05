#!/usr/bin/env bash
# v1.7.63 快速自动发现代理——真栈 A/B/C/D/E E2E（v1.7.62 弹卡语义 + C-1 重放守卫）
# 前置：run_local.sh 跑完 driver（CI：run_e2e.sh 第 4 步自动衔接）。
# 本脚本会【删除慧尖条目】恢复零条目现场——跑完需重 run_local 复原。
#
# 语义（v1.7.62 起，见 discovery.py 第 3.5 步）：
#   首台网关：代理建「等待条目」装心跳耳 → 重放 → 出发现卡（**不静默填充**，
#     添加的确认权在卡片）→ REST 走完确认 → 新条目加载 + 等待条目被清理。
#   第二台起：无等待条目可填 → 走 config flow discovery 卡片（用户确认）。
#
# 相位：
#   A 代理缺席：零条目 HA 10 连发 → 无条目无卡片（现场缺口复现）。
#   B 代理在线：1 条上报 → 重放可观测（C-1 守卫）→ 等待条目出现 → 发现卡挂起
#     → 等待条目未被静默填充（反向半边）→ REST 确认 → 设备注册 → 等待条目清理。
#   C 风暴幂等：30 连发 → 条目恒 1、网关/子设备恒 1+1。
#   D 多网关：第二 SN 上报 → 出发现卡片（不自动建条目）。
#   E 最脏环境（无 MQTT 条目 + 标记引导）：等待条目 + MQTT 条目自动重建 →
#     弹卡 → REST 确认 → 整链配齐。注意 discovery 有 60s 冷却：E 的卡最迟在
#     B 出卡后 ~62s 出现——E 的轮询按 90s 放宽（不是竞态）。
#
# C-1 守卫（v1.7.63）：全程用 mosquitto_sub -v 录制 gateway/rpt_rsp 帧，扫尾
# 逐帧 json.loads——重放载荷若再被 "-v 主题前缀"污染（C-1 原缺陷形态），此处
# 必红；叠加"卡必须真出现"的功能面断言，契约面与功能面双半边闭环。
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
PROXY=$REPO/huijian_mqtt_broker/gateway_discovery_proxy.py
PY=${PY:-$(command -v python3)}
HA_PY=${HA_PY:-$HOME/local/havenv/bin/python3}
[ -x "$HA_PY" ] || HA_PY=$(command -v python3)
CFG=${CFG:-$HOME/local/ha-e2e-config}    # HA -c 目录（CI=bind 的宿主侧同路径）
HA=http://127.0.0.1:8123/api
TOKEN=$(cat /tmp/ha_e2e_token 2>/dev/null || true)
RPT_CAP=${RPT_CAP:-/tmp/rpt_capture_e2e.txt}
MOSQ_DIR=${MOSQ_DIR:-$HOME/local/mosq}
if ! command -v mosquitto_pub >/dev/null 2>&1 && [ -x "$MOSQ_DIR/usr/bin/mosquitto_pub" ]; then
    PATH="$MOSQ_DIR/usr/bin:$PATH"
    export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-$MOSQ_DIR/usr/lib/x86_64-linux-gnu}"
fi
[ -n "$TOKEN" ] || { echo "缺 /tmp/ha_e2e_token（先跑 run_local.sh）"; exit 2; }
command -v mosquitto_pub >/dev/null 2>&1 || { echo "缺 mosquitto_pub"; exit 2; }
command -v mosquitto_sub >/dev/null 2>&1 || { echo "缺 mosquitto_sub（代理重放/录制依赖）"; exit 2; }

GW1=100122501203            # 首台网关（相位 B/E）
SUB1=500700000001           # 其子设备
MSG1_TMPL='{"head":"$SH","id":__ID__,"ctype":"005","sn":"100122501203","data":{"rssi":65472,"sn":"500700000001","attrs":[{"attribute":"r_travel","value":"100"},{"attribute":"heartbeat_time","value":"10"}]}}'
MSG2='{"head":"$SH","id":7,"ctype":"002","sn":"100199999999","data":{}}'

probe() { HA_URL=http://127.0.0.1:8123 HA_TOKEN="$TOKEN" "$HA_PY" "$HERE/ws_flows_probe.py" "$1" 2>/dev/null | head -1; }
card_json() { HA_URL=http://127.0.0.1:8123 HA_TOKEN="$TOKEN" "$HA_PY" "$HERE/ws_flows_probe.py" "$1" 2>/dev/null | tail -n +2 | head -1; }
wc_domain() {
    curl -s -m 5 -H "Authorization: Bearer $TOKEN" "$HA/config/config_entries/entry" \
    | "$HA_PY" -c "import json,sys;print(len([e for e in json.load(sys.stdin) if e['domain']=='$1']))" 2>/dev/null
}
wc_entries() { wc_domain window_controller_gateway; }
dev_count() {  # device_registry 无 REST list（404 实测）→ WS 观测面
    local needle="${1:-}"
    if [ -n "$needle" ]; then
        HA_URL=http://127.0.0.1:8123 HA_TOKEN="$TOKEN" "$HA_PY" "$HERE/ws_device_probe.py" "$needle" 2>/dev/null | head -1
    else
        HA_URL=http://127.0.0.1:8123 HA_TOKEN="$TOKEN" "$HA_PY" "$HERE/ws_device_probe.py" 2>/dev/null | head -1
    fi
}
pub() { mosquitto_pub -h 127.0.0.1 -p 2022 -t gateway/rpt_rsp -m "$1" 2>/dev/null; }
pub_id() { mosquitto_pub -h 127.0.0.1 -p 2022 -t gateway/rpt_rsp -m "${MSG1_TMPL/__ID__/$1}" 2>/dev/null; }

PUMP_PID=""
pump_start() {  # 后台周期重发 005（每轮换 id——同 id 会被 5s 去重层吃掉）：贴合真网关心跳
    local i=${1:-200}
    ( while :; do pub_id "$i"; i=$((i+1)); sleep 2; done ) &
    PUMP_PID=$!
}
pump_stop() { [ -n "$PUMP_PID" ] && kill "$PUMP_PID" 2>/dev/null; PUMP_PID=""; }

wait_card() {  # $1=needle $2=秒数上限；出卡则回显 flow JSON 行并返回 0
    local i JSON=""
    for i in $(seq 1 "$2"); do
        JSON=$(card_json "$1")
        [ -n "$JSON" ] && { echo "$JSON"; return 0; }
        sleep 1
    done
    return 1
}

# 走完发现卡（＝用户在卡片上点确认）：user（提交 SN）→ 连接测试窗（pump 在喂
# 上报）过则 create_entry、不过则 confirm_add 步再确认 → 恒收敛。
drive_card() {  # $1=flow_id $2=SN $3=名称；成功打印 CREATE_ENTRY
    "$HA_PY" - "$1" "$2" "$3" <<'PYEOF'
import json, sys, urllib.error, urllib.request
fid, sn, name = sys.argv[1], sys.argv[2], sys.argv[3]
tok = open("/tmp/ha_e2e_token").read().strip()

def post(body):
    req = urllib.request.Request(
        f"http://127.0.0.1:8123/api/config/config_entries/flow/{fid}",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read() or b"null")
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
        sys.exit(1)

res = post({"gateway_sn": sn, "gateway_name": name})
for _ in range(8):
    if not isinstance(res, dict):
        print(f"BADRESP {res!r}")
        sys.exit(1)
    if res.get("type") == "create_entry":
        print("CREATE_ENTRY")
        sys.exit(0)
    if res.get("type") != "form":
        print(f"ABORT {res.get('reason') or res.get('errors')}")
        sys.exit(1)
    sid = res.get("step_id")
    if sid == "user":
        res = post({"gateway_sn": sn, "gateway_name": name})
    elif sid == "confirm_add":
        res = post({"confirm": True})
    else:
        print(f"UNKNOWN_STEP {sid}: {res}")
        sys.exit(1)
print("NO_TERMINAL")
sys.exit(1)
PYEOF
}

echo "== 前置：清场 + 残卡 fail-fast（只认本脚本自己的 SN） =="
pkill -f 'gateway_discovery_[p]roxy' 2>/dev/null || true
"$HA_PY" - <<'PYEOF' || true
import json, urllib.request
tok = open("/tmp/ha_e2e_token").read().strip()
def req(path, method="GET"):
    r = urllib.request.Request("http://127.0.0.1:8123/api" + path, method=method,
                               headers={"Authorization": f"Bearer {tok}"})
    return json.loads(urllib.request.urlopen(r, timeout=10).read() or b"null")
for e in req("/config/config_entries/entry"):
    if e.get("domain") == "window_controller_gateway":
        try:
            req(f"/config/config_entries/entry/{e['entry_id']}", "DELETE")
            print("已删条目", e["entry_id"])
        except Exception as ex:
            print("删条目失败:", ex)
PYEOF
sleep 3
STALE1=$(probe "122501203"); STALE2=$(probe "9999")
# 只拦本脚本 SN 的残卡：CI 里 driver 的 K 臂会留一张 E2EGW0000002 的在途卡
# （与本脚本相位无关），旧口径 probe "网关" 会把它误判成"上轮残卡"而拒跑。
if [ "${STALE1:-0}" != "0" ] || [ "${STALE2:-0}" != "0" ]; then
    echo "  ✗ 本脚本 SN 存在上轮残卡在途 flow（1203=${STALE1:-?} 9999=${STALE2:-?}）"
    echo "    集成内部 flow 无 REST 清理面，请重跑 run_local.sh 全新一键栈后再跑本 E2E"
    exit 3
fi

FAIL=0
ck() { if [ "$2" != "$3" ]; then echo "  ✗ $1: 期望[$3] 实得[$2]"; FAIL=1; else echo "  ✓ $1"; fi; }

echo "== 相位 A：代理缺席（缺口复现） =="
ck "起点：零条目" "$(wc_entries)" "0"
for i in $(seq 1 10); do pub_id $((50+i)); sleep 0.2; done
sleep 2
ck "10 连发后仍无条目（无耳朵，缺口存在）" "$(wc_entries)" "0"
ck "10 连发后无卡片" "$(probe "122501203")" "0"

echo "== 相位 B：代理在线（首报 → 等待条目 + 发现卡；不静默填充） =="
export HUIJIAN_HA_API="$HA" HUIJIAN_HA_TOKEN="$TOKEN"
: > "$RPT_CAP"
# 全程录制 gateway/rpt_rsp（-v 与代理同款）：C-1 守卫的契约面
mosquitto_sub -h 127.0.0.1 -p 2022 -t gateway/rpt_rsp -v >"$RPT_CAP" 2>/dev/null &
CAP_PID=$!
sleep 1
"$PY" -u "$PROXY" 2022 anonymous unusedpw >/tmp/proxy_e2e.log 2>&1 &
PROXY_PID=$!
sleep 1.5
kill -0 "$PROXY_PID" 2>/dev/null || { echo "  ✗ 代理未能存活"; cat /tmp/proxy_e2e.log; exit 1; }
B0=$(wc -l 2>/dev/null < "$RPT_CAP" || echo 0)
pub_id 101
# 重放可观测：1 条自发布 + 立即/3s 两次重放 = 3 帧（≤20s 到齐）
D=0
for i in $(seq 1 20); do sleep 1; D=$(( $(wc -l < "$RPT_CAP") - B0 )); [ "$D" -ge 3 ] && break; done
ck "重放自发布可观测（自发布+重放 ≥3 帧）" "$([ "$D" -ge 3 ] && echo 1 || echo 0)" "1"
pump_start 200
for i in $(seq 1 15); do sleep 1; [ "$(wc_entries)" != "0" ] && break; done
ck "等待条目出现（代理 REST 建耳）" "$(wc_entries)" "1"
CARD=$(wait_card "122501203" 60)
ck "首报出发现卡（卡真出现＝重放链功能面，C-1 守卫）" "$([ -n "$CARD" ] && echo 1 || echo 0)" "1"
[ -n "$CARD" ] || { echo "  ── 代理日志尾部 ──"; tail -20 /tmp/proxy_e2e.log; }
ck "等待条目未被静默填充（反向半边：无 1203 设备）" "$(dev_count 1203)" "0"
FID=$(echo "$CARD" | "$HA_PY" -c 'import json,sys; print(json.load(sys.stdin).get("flow_id",""))' 2>/dev/null)
ck "卡片 flow_id 已取到" "$([ -n "$FID" ] && echo 1 || echo 0)" "1"
if [ -n "$FID" ]; then
    OUT=$(drive_card "$FID" "$GW1" "慧尖网关 1203")
    ck "REST 走完卡片流（create_entry）" "$OUT" "CREATE_ENTRY"
fi
for i in $(seq 1 30); do sleep 1; [ "$(dev_count 1203)" = "1" ] && [ "$(dev_count "$SUB1")" = "1" ] && break; done
ck "网关设备注册（确认后真条目加载）" "$(dev_count 1203)" "1"
ck "子设备注册" "$(dev_count "$SUB1")" "1"
for i in $(seq 1 30); do sleep 1; [ "$(wc_entries)" = "1" ] && break; done
ck "等待条目已被清理（确认后只剩真条目）" "$(wc_entries)" "1"
pump_stop

echo "== 相位 C：风暴幂等 =="
for i in $(seq 1 30); do pub_id $((300+i)); sleep 0.05; done
sleep 2
ck "30 连发条目恒 1" "$(wc_entries)" "1"
ck "30 连发网关设备恒 1（去重幂等）" "$(dev_count 1203)" "1"
ck "30 连发子设备恒 1" "$(dev_count "$SUB1")" "1"

echo "== 相位 D：第二网关出卡片（不自动建条目） =="
pub "$MSG2"
DC=0
for i in $(seq 1 20); do sleep 1; DC=$(probe "9999"); [ "${DC:-0}" != "0" ] && break; done
ck "第二网关发现卡片出现（恰好 1 张）" "${DC:-0}" "1"
ck "第二网关不自动建条目（确认权在用户）" "$(wc_entries)" "1"

echo "== 相位 E：最脏环境（无 MQTT 条目 + 标记引导） =="
"$HA_PY" - <<'PYEOF' || true
import json, urllib.request
tok = open("/tmp/ha_e2e_token").read().strip()
def req(path, method="GET"):
    r = urllib.request.Request("http://127.0.0.1:8123/api" + path, method=method,
        headers={"Authorization": f"Bearer {tok}"})
    return json.loads(urllib.request.urlopen(r, timeout=10).read() or b"null")
for e in req("/config/config_entries/entry"):
    if e["domain"] in ("window_controller_gateway", "mqtt"):
        try:
            req(f"/config/config_entries/entry/{e['entry_id']}", "DELETE")
            print("已删", e["domain"], "条目")
        except Exception as ex:
            print("删除失败", e["domain"], ex)
PYEOF
for i in $(seq 1 15); do sleep 1; [ "$(wc_entries)" = "0" ] && [ "$(wc_domain mqtt)" = "0" ] && [ "$(dev_count 1203)" = "0" ] && break; done
ck "E 起点：慧尖 0 条目" "$(wc_entries)" "0"
ck "E 起点：MQTT 0 条目" "$(wc_domain mqtt)" "0"
ck "E 起点：1203 无设备残留（discovery 注册表闸）" "$(dev_count 1203)" "0"
cat > "$CFG/window_controller_gateway_mqtt_bootstrap.json" <<'JSON'
{"broker": "127.0.0.1", "port": 2022, "username": "anonymous", "password": "x"}
JSON
pkill -f 'gateway_discovery_[p]roxy' 2>/dev/null || true
sleep 1
"$PY" -u "$PROXY" 2022 anonymous unusedpw >>/tmp/proxy_e2e.log 2>&1 &
PROXY_PID=$!
sleep 1.5
kill -0 "$PROXY_PID" 2>/dev/null || { echo "  ✗ 代理未能存活（E）"; tail -20 /tmp/proxy_e2e.log; exit 1; }
pump_start 500
for i in $(seq 1 20); do sleep 1; [ "$(wc_entries)" = "1" ] && break; done
ck "等待条目重建（代理→awaiting setup）" "$(wc_entries)" "1"
for i in $(seq 1 30); do sleep 1; [ "$(wc_domain mqtt)" = "1" ] && break; done
ck "MQTT 条目被 awaiting bootstrap 自动重建" "$(wc_domain mqtt)" "1"
# discovery 60s 冷却自 B 出卡时刻起算 ⇒ 90s 上限是语义值，不是竞态放宽
E_CARD=$(wait_card "122501203" 90)
ck "E：再出发现卡（60s 冷却后，弹卡语义）" "$([ -n "$E_CARD" ] && echo 1 || echo 0)" "1"
ck "E：等待条目未被静默填充（反向半边）" "$(dev_count 1203)" "0"
FID=$(echo "$E_CARD" | "$HA_PY" -c 'import json,sys; print(json.load(sys.stdin).get("flow_id",""))' 2>/dev/null)
if [ -n "$FID" ]; then
    OUT=$(drive_card "$FID" "$GW1" "慧尖网关 1203")
    ck "E：REST 走完卡片流（create_entry）" "$OUT" "CREATE_ENTRY"
else
    ck "E：卡片 flow_id 已取到" "0" "1"
fi
for i in $(seq 1 30); do sleep 1; [ "$(dev_count 1203)" = "1" ] && [ "$(dev_count "$SUB1")" = "1" ] && break; done
ck "E：无 MQTT 条目起点下整链自动配齐（网关）" "$(dev_count 1203)" "1"
ck "E：子设备注册" "$(dev_count "$SUB1")" "1"
for i in $(seq 1 30); do sleep 1; [ "$(wc_entries)" = "1" ] && break; done
ck "E：等待条目已被清理" "$(wc_entries)" "1"
pump_stop

[ -n "${PROXY_PID:-}" ] && kill "$PROXY_PID" 2>/dev/null
[ -n "${CAP_PID:-}" ] && kill "$CAP_PID" 2>/dev/null
sleep 1
echo "── 代理日志尾部 ──"; tail -8 /tmp/proxy_e2e.log

echo "== 扫尾：gateway/rpt_rsp 全程帧载荷可解析（C-1 守卫：重放不得带 -v 主题前缀） =="
SWEEP=$(wc -l 2>/dev/null < "$RPT_CAP" || echo 0)
ck "录制帧数下限（≥25，防录制哑掉后空扫绿）" "$([ "${SWEEP:-0}" -ge 25 ] && echo 1 || echo 0)" "1"
BAD=$("$HA_PY" - "$RPT_CAP" <<'PYEOF'
import json, sys
bad = 0
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    line = line.strip()
    if not line:
        continue
    _, _, payload = line.partition(" ")
    try:
        json.loads(payload)
    except ValueError:
        bad += 1
        print(f"BAD: {line[:140]}", file=sys.stderr)
print(bad)
PYEOF
)
ck "录制 $SWEEP 帧、逐帧 json.loads 全过（C-1 守卫）" "${BAD:-1}" "0"
[ "${BAD:-1}" = "0" ] || echo "     ↑ 不可解析载荷＝重放被主题前缀污染（C-1 回归形态）或 broker 侧格式异常"

if [ $FAIL -eq 0 ]; then echo "== FAST_DISCOVERY_E2E PASS =="; else echo "== FAST_DISCOVERY_E2E FAIL =="; fi
exit $FAIL

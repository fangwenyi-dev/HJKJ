#!/bin/bash
# 慧尖 v1.6.24 共存自动桥——全链路机制实证（永久回归资产）
#
# 与 run_local.sh（集成/E2E）同域的**机制层** E2E：rootless mosquitto 2.0.22
# 双实例（内置口 2022 仿真 + "官方" peer 1883）+ run.sh 原文函数（经
# gen_bridge_harness.py 抽取）。改动 run.sh 桥机制后必须重跑本脚本。
#
# 用法（WSL，任意目录）：bash bridge_coexist_e2e.sh
# 依赖：~/local/mosq（run_local.sh 同款 rootless mosquitto）+ paho python
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
MOSQ_DIR=${MOSQ_DIR:-$HOME/local/mosq}
MOSQ_BIN=$MOSQ_DIR/usr/sbin/mosquitto
PY=${PY:-$HOME/local/havenv/bin/python3}
export LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-$MOSQ_DIR/usr/lib/x86_64-linux-gnu}
[ -x "$MOSQ_BIN" ] || { echo "缺 rootless mosquitto（先跑 ../run_local.sh 或按 README 解包到 $MOSQ_DIR）"; exit 2; }
[ -x "$PY" ] || PY=$(command -v python3)

L=$HOME/local/bridge-e2e-$$    # 轮次独占目录：旧轮孤儿即便垂死也只碰自己的路径
rm -rf "$L" 2>/dev/null; mkdir -p "$L" || exit 2
export MOSQ_LOCAL=$L
export MOSQ_BIN
PIDF=$L/broker.pid; export PIDFILE=$PIDF

# 被测函数（生产原文）
python3 "$HERE/gen_bridge_harness.py" || exit 3
# shellcheck disable=SC1091
source "$HERE/bridge_harness_lib.sh"
OFFICIAL_PORT_HEX=$(printf '%04X' 1884)   # e2e 沙盒对端口

CONF=$L/e2e.conf
fail() { echo "❌ $*"; echo "--- e2e.out ---"; tail -8 "$L/e2e.out" 2>/dev/null;
         echo "--- peer.conf.log ---"; tail -8 "$L/peer.conf.log" 2>/dev/null;
         echo "--- /proc/net/tcp 1884? ---"; grep -ci 075C /proc/net/tcp 2>/dev/null;
         echo "--- e2e.conf tail ---"; tail -14 "$CONF" 2>/dev/null;
         echo "[现场保留在 $L，排查后手动清理]"; KEEP_SCENE=1 exit 1; }
cleanup() {
    [ "${KEEP_SCENE:-0}" = 1 ] && return 0   # fail 现场留给开发者
    # 顺序：先看门狗（防复活）再按登记单杀全部后代；零 pattern 匹配
    kill "${SH_PID:-}" 2>/dev/null
    while read -r xpid; do kill "$xpid" 2>/dev/null; done 2>/dev/null < "$L/.pids"
    sleep 1
    kill -9 "${SH_PID:-}" 2>/dev/null
    while read -r xpid; do kill -9 "$xpid" 2>/dev/null; done 2>/dev/null < "$L/.pids"
    rm -rf "$L"
}
# 端口排他检查（不杀任何进程——pattern 杀在嵌套/孤儿场景互伤已 7 次实发，
# 万恶之源废除；确需清孤儿时手动跑 ~/local/kill_bridge_orphans.sh）
hex2022=$(printf '%04X' 2022); hex1884=$(printf '%04X' 1884)
if awk -v p1=":${hex2022}$" -v p2=":${hex1884}$" 'FNR>1 && ($2~p1||$2~p2) && $4=="0A"{f=1} END{exit !f}' /proc/net/tcp; then
    echo "❌ 2022/1884 已被占用（上轮孤儿或 run_local 栈在跑）——先执行：bash ~/local/kill_bridge_orphans.sh 再重试"
    exit 2
fi
trap cleanup EXIT

# 2022 基线 conf（无桥）+ 拉起（selfheal 后台看护）
cat > "$CONF" <<EOF
listener 2022 127.0.0.1
allow_anonymous true
persistence false
pid_file $PIDF
log_dest file $L/e2e.out
log_type all
EOF
selfheal & SH_PID=$!     # 首启+看护一体（与 run.sh 主循环同构）
echo "$SH_PID" >> "$L/.pids"
sleep 2
kill -0 "$(cat "$PIDF")" 2>/dev/null || { echo "broker 首启失败"; tail -5 "$L/e2e.out"; exit 2; }

# peer conf 工厂（S 段匿名版 / T 段认证版共用）
peer_conf() { # $1=port $2=confpath $3=anonymous|auth
    local anon=true
    [ "$3" = auth ] && anon=false
    cat > "$2" <<EOF
listener $1 127.0.0.1
allow_anonymous $anon
persistence false
# pid 文件独立命名：不能含 conf 路径字面，防 gen 的 $PIDFILE 替换波及（S2 事故）
pid_file ${2%.conf}.pidp
log_dest file $2.log
log_type all
EOF
    if [ "$3" = auth ]; then
        "$MOSQ_DIR/usr/bin/mosquitto_passwd" -c -b "$2.pw" z2m_user peerpass
        echo "password_file $2.pw" >> "$2"
    fi
}

echo "═══ S 段：匿名共存环境（老版官方/customize 关认证）状态机 S0-S6 ═══"

# S0 无 peer：探测假 + 不建桥
if _bridge_peer_up; then fail "S0 无 peer 却探测为真"; fi
_bridge_off || true
grep -q core_mosquitto "$CONF" && fail "S0 conf 不应有桥"
echo "✅ S0 探测正确：无 peer → 不建桥"

# S1 peer 出现 → 探测命中
peer_conf 1884 "$L/peer.conf" anonymous
"$MOSQ_BIN" -c "$L/peer.conf" & PEER_PID=$!
echo "$PEER_PID" >> "$L/.pids"
sleep 1
_bridge_peer_up || fail "S1 peer 已起但探测失败"
echo "✅ S1 官方 broker 出现 → 探测命中"

# S2 建桥 → 计划内 kill → selfheal 复活 → 桥激活
_bridge_on || fail "S2 _bridge_on 失败"
for i in $(seq 1 24); do grep -q "Connecting bridge .* core_mosquitto" "$L/e2e.out" && break; sleep 0.5; done
grep -q "Connecting bridge .* core_mosquitto" "$L/e2e.out" || fail "S2 重启后桥未激活"
echo "✅ S2 kill→自愈重启→桥自动激活"

# S3 终态语义（z2m 三向恰 1 + gateway 双向物理隔离负向钉桩）
TO=""; command -v timeout >/dev/null 2>&1 && TO="timeout 40"
$TO "$PY" - <<'ZZEOF' || fail "S3 端到端语义失败"
import time
import paho.mqtt.client as mqtt
got=[]
def mk(port, tag, topics, cid):
    cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
    cl.on_message = lambda c,u,m: got.append((tag,m.topic))
    cl.connect("127.0.0.1", port, 10); cl.loop_start()
    [cl.subscribe(t, qos=1) for t in topics]
mk(2022,"HA",["zigbee2mqtt/u8state","homeassistant/sensor/u8/config"],"ha-e2e")
mk(1884,"PEER",["zigbee2mqtt/u8set/#","gateway/u8gw/#"],"peer-e2e")
mk(2022,"FW",["gateway/u8gw/req"],"fw-e2e")
time.sleep(2)
z = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="z"); z.connect("127.0.0.1",1884,10)
z.loop_start(); time.sleep(1)
z.publish("zigbee2mqtt/u8state",'{"state":"ON"}',qos=1)
z.publish("homeassistant/sensor/u8/config",'{"name":"x"}',qos=1)
h = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="h"); h.connect("127.0.0.1",2022,10)
h.loop_start(); time.sleep(1)
h.publish("zigbee2mqtt/u8set/lamp",'{"state":"OFF"}',qos=1)
g = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="gw"); g.connect("127.0.0.1",2022,10)
g.loop_start(); time.sleep(1)
g.publish("gateway/u8gw/rsp",'{"ctype":"002"}',qos=1)
r = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="atk"); r.connect("127.0.0.1",1884,10)
r.loop_start(); time.sleep(1)
r.publish("gateway/u8gw/req",'{"ctype":"004","command":"open"}',qos=1)
time.sleep(8)
c = {}
for t,tp in got: c[(t,tp)] = c.get((t,tp),0)+1
q = lambda tag,tp: c.get((tag,tp),0)
print(f"in:state={q('HA','zigbee2mqtt/u8state')} in:disc={q('HA','homeassistant/sensor/u8/config')}"
      f" out:set={q('PEER','zigbee2mqtt/u8set/lamp')}"
      f" NEG:rsp@peer={q('PEER','gateway/u8gw/rsp')} NEG:req@fw={q('FW','gateway/u8gw/req')} total={len(got)}")
assert q('HA','zigbee2mqtt/u8state')==1, "z2m 状态未经 in 桥达 HA（0=断链,>1=风暴）"
assert q('HA','homeassistant/sensor/u8/config')==1, "discovery 未经 in 桥达 HA"
assert q('PEER','zigbee2mqtt/u8set/lamp')==1, "HA 控制命令未经 out 桥达 z2m"
assert q('PEER','gateway/u8gw/rsp')==0, "慧尖上报泄漏到官方 broker（gateway 腿未摘净！）"
assert q('FW','gateway/u8gw/req')==0, "1883 侧注入 req 穿桥达固件——攻击链复活！"
assert len(got)<=5, f"消息复制异常（风暴迹象）: {got}"
# ^ 5 = 3 正向 + 1 合法一跳自回环（in 注入命中同桥 out 订阅，实测终止）
#   + 1 冗余容差。风暴形态是数百条（both 时代实测），两条负向断言已堵
#   gateway 面，HA 面 n==1 精确断言已钉消费正确性。
print("✅ S3 终态：z2m 三向各恰 1 + gateway 双向物理隔离（攻击链封堵钉死）")
ZZEOF

# S4 peer 消失 → 拆桥 → broker 存活
kill -TERM "$PEER_PID" 2>/dev/null; wait "$PEER_PID" 2>/dev/null
sleep 1
_bridge_off || fail "S4 拆桥失败"
sleep 8
grep -q core_mosquitto "$CONF" && fail "S4 conf 残留桥块"
kill -0 "$(cat "$PIDF")" 2>/dev/null || fail "S4 内置 broker 不在"
echo "✅ S4 peer 消失 → 桥自动拆除 + broker 自愈存活"

# S5 拆桥后内置服务正常
"$MOSQ_DIR/usr/bin/mosquitto_pub" -p 2022 -i s5 -t gateway/s5/ping -m 1 -q 1 \
    && echo "✅ S5 拆桥后 2022 服务正常" || fail "S5 2022 publish 失败"

# S6 peer 重现 → 可逆重装
peer_conf 1884 "$L/peer.conf" anonymous
"$MOSQ_BIN" -c "$L/peer.conf" & PEER_PID=$!
echo "$PEER_PID" >> "$L/.pids"
sleep 1
_bridge_on || fail "S6 重装桥失败"
for i in $(seq 1 24); do grep -q "Connecting bridge .* core_mosquitto" "$L/e2e.out" && break; sleep 0.5; done
grep -q "Connecting bridge .* core_mosquitto" "$L/e2e.out" || fail "S6 桥未再激活"
echo "✅ S6 peer 重现 → 桥可逆重装（状态机闭环）"
kill -TERM "$PEER_PID" 2>/dev/null; wait "$PEER_PID" 2>/dev/null
_bridge_off || true; sleep 8

echo ""
echo "═══ T 段：认证共存环境（官方 7.x go-auth 同构 = allow_anonymous false） ═══"

# T1 匿名桥 → 被拒 + 慧尖自身无恙
peer_conf 1884 "$L/ap.conf" auth
"$MOSQ_BIN" -c "$L/ap.conf" & PEER_PID=$!
echo "$PEER_PID" >> "$L/.pids"
sleep 1
_bridge_on || fail "T1 写桥失败"
sleep 6
grep -qi "not authorised\|bad user name" "$L/e2e.out" \
    && echo "✅ T1 匿名桥被认证 peer 拒绝（官方 7.x 行为复现）" \
    || echo "⚠️ T1 未见拒绝日志（本地 peer 语义差异，不阻塞）"
"$MOSQ_DIR/usr/bin/mosquitto_pub" -p 2022 -i t1 -t gateway/t1 -m 1 \
    && echo "✅ T1b 桥不通但慧尖 2022 服务无恙（降级边界）" || fail "T1b 2022 挂死"

# T2 填凭据 → 带认证桥端到端穿透
# （harness 变量在 source 时求值——改环境后必须重新 source 才生效）
_bridge_off || true; sleep 6
export TEST_BRIDGE_USER=z2m_user TEST_BRIDGE_PASS=peerpass
# shellcheck disable=SC1091
source "$HERE/bridge_harness_lib.sh"
OFFICIAL_PORT_HEX=$(printf '%04X' 1884)   # e2e 沙盒对端口
_bridge_on || fail "T2 写带凭据桥失败"
grep -q "username z2m_user" "$CONF" || fail "T2 凭据行未展开进 conf"
# 桥日志实锤文案（2.0.22 实测）："Received CONNACK on connection
# local.<host>.core_mosquitto"——T1 匿名被拒后 mosquitto 对失败连接做
# **30s 退避重试**，T2 窗口必须跨过退避（authbridge 时代 sleep 30 同理，
# 移植时简化成 sleep 6 是本次误报根因）。上限 60s 双保险。
# 桥成败的权威判据在 peer 侧日志（本地 "Received CONNACK" 接受/拒绝都打，
# 无法区分）。(0, 0) = CONNACK accepted。失败场景有 30s 退避重试，等 45s。
for i in $(seq 1 45); do
    grep -qE "Sending CONNACK to .*core_mosquitto \(0, 0\)" "$L/ap.conf.log" && break
    sleep 1
done
grep -qE "Sending CONNACK to .*core_mosquitto \(0, 0\)" "$L/ap.conf.log" \
    || fail "T2 带凭据桥未与认证 peer 建立连接（看 e2e.out 拒绝原因）"
# 2.0.22 的 sub 无 -C/-W 计数退出选项（harness 参数形态坑）——timeout 包裹
timeout 12 "$MOSQ_DIR/usr/bin/mosquitto_sub" -p 1884 -u z2m_user -P peerpass \
    -t 'zigbee2mqtt/#' -i t2sub > "$L/t2.txt" 2>&1 &
TSUB=$!
echo "$TSUB" >> "$L/.pids"
sleep 1
"$MOSQ_DIR/usr/bin/mosquitto_pub" -p 2022 -i t2p -t zigbee2mqtt/deep/x -m hit -q 1
wait "$TSUB" 2>/dev/null
grep -q hit "$L/t2.txt" \
    && echo "✅ T2 条件凭据桥：${TEST_BRIDGE_USER} 认证 + 端到端数据穿透" \
    || { fail "T2 认证桥未穿透: $(cat "$L/t2.txt" 2>/dev/null)"; }

# T3 半填凭据（v1.6.26 D-2 真栈行为级回归）：只填用户名——旧模板展开出
# `password ` 空值行，mosquitto 2.x conf__parse_string 空值判错**拒载整份
# conf**（conf.c 源码实锤）→ 内置 broker 拒启。现降级匿名桥：conf 必须
# **零凭据行**，且 _bridge_on 的 kill→自愈重启后 2022 存活 = 真实
# eclipse-mosquitto:2 接受了该 conf（行为断言，不靠源码推断）。
_bridge_off || true; sleep 6
export TEST_BRIDGE_USER=half_only TEST_BRIDGE_PASS=""
# shellcheck disable=SC1091
source "$HERE/bridge_harness_lib.sh"
OFFICIAL_PORT_HEX=$(printf '%04X' 1884)
_bridge_on || fail "T3 半填凭据写桥失败"
grep -Eq '^[[:space:]]*(username|password)([[:space:]]|$)' "$CONF" \
    && fail "T3 半填形态泄漏凭据行（含毒行）: $(grep -nE '^[[:space:]]*(username|password)' "$CONF")"
OK3=false
for i in $(seq 1 12); do
    "$MOSQ_DIR/usr/bin/mosquitto_pub" -p 2022 -i t3 -t gateway/t3 -m 1 && { OK3=true; break; }
    sleep 1
done
"$OK3" && echo "✅ T3 半填凭据→匿名桥降级：零毒行 + conf 被 mosquitto 接受（2022 存活）" \
    || fail "T3 半填凭据后 broker 写桥即死（D-2 回潮！查 e2e.out 的 Empty bridge remote_password）"
_bridge_off || true; sleep 6

kill -TERM "$PEER_PID" 2>/dev/null; wait "$PEER_PID" 2>/dev/null
_bridge_off || true; sleep 6
kill "$SH_PID" 2>/dev/null
echo ""
echo "═══ 共存自动桥机制全链路实证通过（S0-S6 + T1-T3）═══"

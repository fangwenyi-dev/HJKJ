#!/usr/bin/env bash
# v1.6.21 真栈 E2E（CI 侧编排）：eclipse-mosquitto:2 + HA Core 真实容器，
# 全部业务断言在 tests/e2e/ha_e2e_driver.py（本地/CI 单一事实源——auth
# 契约经读 HA 2026.7.1 源码实证后重写，终止两轮 CI 盲打）。
#
# 本地复跑（WSL，无 docker）：bash tests/e2e/run_local.sh 一键完成
# （重置→拉起 HA Core→跑同一 driver→失败落日志），契约与 CI 同源。
#
# E2E 自 v1.6.22 起已是发布**硬门禁**（ci.yaml 无 continue-on-error——
# 本脚本失败即阻断 Build and Release）。失败 trap 落盘两侧容器日志尾部辅助排障。
set -Eeuo pipefail

CFG=/tmp/e2e-ha-config
DIR="$(cd "$(dirname "$0")" && pwd)"

diag() {
    echo "!! E2E 编排失败于: ${1:-unknown}"
    docker logs --tail 40 mosq-e2e 2>&1 || true
    docker logs --tail 150 ha-e2e 2>&1 | grep -v "^\s*$" | tail -100 || true
    # v1.7.34：HA 自带文件日志（bind 挂在 $CFG，不走 docker 的块缓冲）——
    # 第二轮 CI 实证 `docker logs` 在 05:45:20 后整段空白：python stdout
    # 非 TTY 时块缓冲，driver 失败那一刻缓冲区还没落盘，连"等待条目 setup"
    # 必打的 INFO 都看不到，等于没有取证。文件日志每条 flush，是唯一可靠面。
    echo "── HA 文件日志（$CFG/home-assistant.log 尾 200 行） ──"
    tail -200 "$CFG/home-assistant.log" 2>/dev/null \
        | grep -E "window_controller|发现|自动填充|等待模式|ERROR|Traceback" \
        | tail -80 || true
}
trap 'diag "line $LINENO"' ERR

echo "==== 1. mosquitto :2022 ===="
cat > /tmp/e2e-mosq.conf <<'EOF'
listener 2022
allow_anonymous true
message_size_limit 1048576
EOF
docker run -d --name mosq-e2e --network host \
    -v /tmp/e2e-mosq.conf:/mosquitto/config/mosquitto.conf \
    eclipse-mosquitto:2 >/dev/null

echo "==== 2. HA Core（bind 挂载本集成 + driver） ===="
rm -rf "$CFG"; mkdir -p "$CFG/custom_components"
cp -r "$DIR/../../custom_components/window_controller_gateway" \
    "$CFG/custom_components/"
cp "$DIR/ha_e2e_driver.py" "$CFG/"
# v1.7.34：本集成日志开到 INFO——发现链的早退分支全是 DEBUG，成功路径
# （"发现新网关"/"已使用标准发现流程发现网关"）是 INFO。默认 warning 下
# driver 失败时 diag 只能看到 WARNING+，K/L 两臂一旦红就得再盲跑一轮 CI
# 才能定位（首轮 405 假红即栽在这）。default 仍 warning，噪声不涨。
# 必须自带 default_config：镜像原本会自动生成 configuration.yaml（HA 源码
# config.py DEFAULT_CONFIG 实证首行就是 `default_config:`，api/auth/onboarding/
# frontend 全由它拉起）；预置文件后 HA 不再生成，漏掉它＝REST API 整个不存在。
# 模板里的 themes/automation/script/scene !include 不抄——对应文件不会随之
# 生成，缺文件的 !include 会让配置校验失败、HA 起不来（driver 也不用它们）。
cat > "$CFG/configuration.yaml" <<'EOF'
default_config:
logger:
  default: warning
  logs:
    custom_components.window_controller_gateway: info
EOF
chmod -R 777 "$CFG"
# PYTHONUNBUFFERED：python stdout 非 TTY 时块缓冲，HA 的日志会卡在缓冲区里
# 不进 docker logs（第二轮 CI 实证：driver 失败时刻之前的日志整段丢失）。
# HUIJIAN_HUB_BASE=黑洞：本 e2e 跑的是真实 async_setup_entry，会在
# async_ensure_hub_client 里拿内置生产默认去 /agent/register——不设此变量则
# **每次 CI 都往生产 hub 注册表塞一条 sn=E2EGW0000001 的孤儿实例**（v1.7.46/47
# 两次 CI 已实锤各留一条）。指到 127.0.0.1:1 让注册秒失败（拒连→WARNING→退避），
# 全程不触网。driver 不对 hub 做任何断言，黑洞不影响 e2e 判据。
docker run -d --name ha-e2e --network host \
    -e "TZ=Etc/UTC" -e "PYTHONUNBUFFERED=1" \
    -e "HUIJIAN_HUB_BASE=http://127.0.0.1:1" \
    -v "$CFG:/config" \
    ghcr.io/home-assistant/home-assistant:stable >/dev/null

echo "==== 3. 驱动器（等待/认证/entry/002/断言/soak 全在其内） ===="
# summary 经 $CFG（已 bind 进容器 /config）双向共享——直接指容器内路径
# （docker exec 无法临时挂载 runner 文件，第五轮自查断链修复）
RC=0
docker exec -e E2E_HA_URL=http://127.0.0.1:8123 -e E2E_MQTT_HOST=127.0.0.1 \
    -e GITHUB_STEP_SUMMARY=/config/summary.md \
    ha-e2e python3 /config/ha_e2e_driver.py || RC=$?
if [ -s "$CFG/summary.md" ] && [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    cat "$CFG/summary.md" >> "$GITHUB_STEP_SUMMARY"
fi
[ "$RC" -eq 0 ] || { diag "driver rc=$RC"; exit "$RC"; }

echo "==== 4. 发现代理真栈关口（fast_discovery_e2e.sh） ===="
# v1.7.63（C-11 接线）：该脚本此前从未被 CI 引用（"哑门"），且断言停在
# v1.7.11 的静默自动填充语义。它也是唯一真跑 gateway_discovery_proxy.py
# **进程**的关口——C-1 类"重放载荷被 -v 主题前缀污染"只有它能拦。
# 三条环境桥接必须成功（不做静默跳过，缺一即响亮失败）：
#  · token：driver 在 HA 容器内把 owner token 写 /tmp/ha_e2e_token → 取回宿主机
docker cp ha-e2e:/tmp/ha_e2e_token /tmp/ha_e2e_token
#  · mosquitto 客户端：代理以子进程方式调用 mosquitto_sub/pub（与生产同款）
sudo apt-get update -qq
sudo apt-get install -y -qq mosquitto-clients
#  · aiohttp：WS 观测探针（ws_flows_probe / ws_device_probe）在宿主机跑
pip install -q aiohttp
RC_F=0
CFG="$CFG" HA_PY=$(command -v python3) \
    bash "$DIR/fast_discovery_e2e.sh" || RC_F=$?
[ "$RC_F" -eq 0 ] || {
    echo "── 发现代理日志尾部 ──"
    tail -40 /tmp/proxy_e2e.log 2>/dev/null || true
    diag "fast_discovery rc=$RC_F"
    exit "$RC_F"
}
echo "==== E2E 编排完成 ✅ ===="

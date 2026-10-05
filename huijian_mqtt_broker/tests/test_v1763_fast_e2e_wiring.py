"""v1.7.63 对抗复核 C-11：fast_discovery_e2e.sh 的语义翻新与 CI 接线守卫。

背景：该脚本断言停在 v1.7.11 的"步骤 3.5 静默自动填充"，v1.7.62 取消该行为后
它对新语义失联（能拦 C-1 的真栈关口是哑的）；且 CI（ci.yaml → run_e2e.sh）
此前从未引用它。它同时是唯一真跑 gateway_discovery_proxy.py **进程**的关口——
C-1"重放载荷被 -v 主题前缀污染"这类缺陷只有它能拦（单测喂的是构造行）。

本文件钉两件事（防"接线丢了"与"语义回退"）：
1) run_e2e.sh 在 driver 之后真跑该脚本（非注释、不被 || true 吞、有 rc 闸），
   环境桥接三件套（token 回传 / mosquitto-clients / aiohttp）在场；
2) 脚本内部保留新语义与 C-1 守卫的关键锚（弹卡 → REST 确认；全程录制 +
   逐帧 json.loads 扫尾带帧数下限），且旧"静默自动填充"断言不得复活。
"""
import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parents[1]
RUN_E2E = (TESTS / "e2e" / "run_e2e.sh").read_text(encoding="utf-8")
FAST = (TESTS / "e2e" / "fast_discovery_e2e.sh").read_text(encoding="utf-8")
CI = (REPO / ".github" / "workflows" / "ci.yaml").read_text(encoding="utf-8")

CALL = 'bash "$DIR/fast_discovery_e2e.sh"'


def test_run_e2e_invokes_fast_script_after_driver():
    """接线：driver 之后真跑发现代理关口；调用行不得是注释、不得 || true 吞错。"""
    assert CALL in RUN_E2E, "run_e2e.sh 必须真调用 fast_discovery_e2e.sh（C-11 接线）"
    line = next(l for l in RUN_E2E.splitlines() if CALL in l)
    assert not line.lstrip().startswith("#"), "调用不得是注释"
    assert "|| true" not in line, "不得用 || true 吞掉失败"
    # 顺序：driver 先跑（复用同一真栈），代理关口在后
    assert RUN_E2E.index("python3 /config/ha_e2e_driver.py") < RUN_E2E.index(CALL)
    # rc 闸：失败必须走 diag + exit，不许静默带过
    tail = RUN_E2E[RUN_E2E.index(CALL):]
    assert "RC_F=$?" in tail and 'exit "$RC_F"' in tail, \
        "调用后必须有 rc 闸（RC_F=$? → 非零 diag+exit）"


def test_ci_bridge_pieces_present():
    """环境桥接三件套缺一即哑：token 回传 / mosquitto 客户端 / aiohttp。"""
    assert "docker cp ha-e2e:/tmp/ha_e2e_token /tmp/ha_e2e_token" in RUN_E2E
    assert "mosquitto-clients" in RUN_E2E
    assert "pip install -q aiohttp" in RUN_E2E


def test_ci_still_calls_run_e2e():
    """转递闭包：ci.yaml → run_e2e.sh → fast 脚本；缺一环即哑门。"""
    assert "bash huijian_mqtt_broker/tests/e2e/run_e2e.sh" in CI


def test_fast_script_card_semantics_and_c1_guard():
    """新语义锚（弹卡 → REST 确认）与 C-1 守卫锚（录制 + json.loads 扫尾）在场。"""
    assert "首报出发现卡" in FAST
    assert "CREATE_ENTRY" in FAST and "drive_card" in FAST, \
        "REST 走卡片流（create_entry）的驱动必须在"
    assert "不自动建条目" in FAST
    # C-1 契约面：全程 -v 录制 + 逐帧 json.loads；下限防"录制哑掉后空扫绿"
    assert "json.loads(payload)" in FAST and "C-1" in FAST
    assert "录制帧数下限" in FAST


def test_old_autofill_assertions_must_not_revive():
    """反钉（升级不弱化）：旧'静默自动填充'断言不得复活成断言面。"""
    assert "自动填充链启动" not in FAST
    assert "（device_registry 出现，全自动）" not in FAST


def test_device_counts_always_scoped_by_needle():
    """反向半边：无 needle 的裸设备计数会混入 driver 残遗（E2EGW*），断言失真。"""
    bad = re.findall(r'"?\$\(dev_count\)"?', FAST)
    assert not bad, f"发现裸 dev_count 计数（必须带 needle 限定 SN）：{bad}"

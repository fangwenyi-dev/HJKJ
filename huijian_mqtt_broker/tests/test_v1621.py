"""v1.6.21 批次钉桩：默认凭据提示 / Gitee Release 自动化 / 真栈 E2E 挂载。

背景（第七轮评分定案）：
- 默认 WS 令牌/默认 MQTT 密码与小程序、固件公开同串——Web UI 需提示改密
  （只提示绝不自动改：令牌双侧同步是既定契约，自动轮换=全客户 401）。
- Gitee Release 手工 POST 属人肉流程，CI 自动化消除。
- 279 单测全在 fake homeassistant mock 上跑——补真栈 E2E（CI 有 docker，
  本地没有），首阶段 continue-on-error 盲调试，连绿后升硬门禁。
"""
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).parent
PKG = HERE.parent / "custom_components" / "window_controller_gateway"


# ---------- security 视图行为 ----------

@pytest.fixture
def security_view(monkeypatch):
    from homeassistant.components import http as ha_http
    monkeypatch.setattr(
        ha_http.HomeAssistantView, "json",
        lambda self, data: data, raising=False)
    from custom_components.window_controller_gateway.api import (
        WindowGatewaySecurityView)
    return WindowGatewaySecurityView()


def _req(entries):
    hass = SimpleNamespace(config_entries=SimpleNamespace(
        async_entries=lambda domain: entries))
    return SimpleNamespace(app={"hass": hass})


def test_security_ws_token_default_detected(security_view):
    from custom_components.window_controller_gateway.const import (
        CONF_WS_GATEWAY_TOKEN, DEFAULT_WS_GATEWAY_TOKEN)

    def run(entries):
        import asyncio
        return asyncio.run(security_view.get(_req(entries)))

    # options 未设置 → 用默认值 → true
    r = run([SimpleNamespace(options={})])
    assert r["ws_token_is_default"] is True
    # 自定义令牌 → false
    r = run([SimpleNamespace(
        options={CONF_WS_GATEWAY_TOKEN: "a-custom-secret-token-9999"})])
    assert r["ws_token_is_default"] is False
    # 多条目任一未改 → true（安全口径取严）
    r = run([
        SimpleNamespace(options={CONF_WS_GATEWAY_TOKEN: "custom-x-123456789"}),
        SimpleNamespace(options={}),
    ])
    assert r["ws_token_is_default"] is True
    # 无条目 → None（无从判定，UI 降级不误报）
    r = run([])
    assert r["ws_token_is_default"] is None
    # 响应绝不含令牌明文
    assert DEFAULT_WS_GATEWAY_TOKEN not in json.dumps(r)


def test_security_view_registered():
    src = (PKG / "api.py").read_text(encoding="utf-8")
    assert "register_view(WindowGatewaySecurityView())" in src
    assert 'url = "/api/window_controller_gateway/security"' in src
    # 只读视图：无 POST/DELETE 面
    seg = src[src.index("class WindowGatewaySecurityView"):]
    seg = seg[:seg.index("class ") if "class " in seg[10:] else len(seg)]
    assert "async def post" not in seg and "async def delete" not in seg


# ---------- 默认凭据交叉锚（改默认值必须三处同动的防线） ----------

def test_default_password_cross_anchor():
    from custom_components.window_controller_gateway.const import (
        DEFAULT_MQTT_PASSWORD)
    assert DEFAULT_MQTT_PASSWORD == "huijian2022"
    cfg = (HERE.parent / "config.yaml").read_text(encoding="utf-8")
    # options 块默认值直写（schema 只声明 type: password）
    assert f"password: {DEFAULT_MQTT_PASSWORD}" in cfg
    run_sh = (HERE.parent / "run.sh").read_text(encoding="utf-8")
    assert f'= "{DEFAULT_MQTT_PASSWORD}"' in run_sh, \
        "run.sh 默认密码判定与 const 脱节"


def test_status_json_carries_default_flag():
    run_sh = (HERE.parent / "run.sh").read_text(encoding="utf-8")
    assert "mqtt_password_is_default:$dp" in run_sh
    assert "DP_IS_DEFAULT" in run_sh


# ---------- Web UI 提示面 ----------

def test_webui_credential_status_wired():
    """v1.6.22 定案反转：Web UI 不得展示凭据状态提示。

    MQTT 密码/令牌轮换必须与 LoRa 网关固件侧同步修改，终端用户无
    处置能力——展示"仍是默认值"只会造成困惑与误操作（用户 2026-09
    明确要求移除）。后端 security 视图与 status.json 字段保留为
    只读诊断面（零展示面），故此处的 UI 钉桩转为负向防复活。
    """
    # v1.6.25 三文件化：UI 内容外置到 www/css、www/js，负向防复活钉桩覆盖
    # 三文件拼接（= 拆分前单文件的检索面），断言语义不变。
    www = HERE.parent / "www"
    html = "".join((www / p).read_text(encoding="utf-8") for p in
                   ("index.html", "css/huijian.css", "js/huijian.js"))
    assert "credStatus" not in html
    assert "凭据状态" not in html
    assert "wsTokenIsDefault" not in html
    # 后端诊断面仍在（保留决策）：
    api = (HERE.parent / "custom_components" / "window_controller_gateway" /
           "api.py").read_text(encoding="utf-8")
    assert "WindowGatewaySecurityView" in api


def test_ci_e2e_hardgate_and_gitee_retired():
    ci = (HERE.parent.parent / ".github" / "workflows" / "ci.yaml").read_text(
        encoding="utf-8")
    assert "bash huijian_mqtt_broker/tests/e2e/run_e2e.sh" in ci
    # v1.6.23 定案反转：E2E 连绿转正为硬门禁——e2e job 段【不得】再挂
    # continue-on-error（防无凭据复活挂绳），manifest 必须以 e2e 为 needs
    # 前置（失败即不晋升 tag/latest）。
    def _job_seg(name):
        lines, hit = [], False
        for ln in ci.splitlines():
            if ln.startswith(f"  {name}:"):
                hit = True
            elif hit and ln.startswith("  ") and not ln.startswith("   "):
                break  # 下一个 2 缩进 job key
            elif hit:
                lines.append(ln)
        assert hit, f"ci.yaml 缺 {name} job"
        return "\n".join(lines)
    assert "continue-on-error" not in _job_seg("e2e"), \
        "E2E 已连绿转正，挂绳不得复活（如需复活先证明真栈回归全绿）"
    assert "needs: [prepare, init, build, e2e]" in _job_seg("manifest"), \
        "manifest 必须 gate 在 e2e 之后（硬门禁拓扑）"
    # 2026-09-16「只推 GitHub」裁定 2026-09-17 就被推翻（停推代价实证见 ci.yaml 注释），
    # 但 v1.7.24 下线的 gitee-release job 一直只留这条**负向防复活钉**——期间 v1.7.63/64/65
    # 三条 Gitee Release 全靠手工 POST，漏一条就是徽章偏旧且无人知。2026-10-06 D4 把该
    # job 自动化后，钉的方向随之翻转：不判"这个名字不许出现"，判"它必须存在且必须带着
    # 当年踩出来的那几件"。负向三句（Create Gitee Release / GITEE_TOKEN 不得出现）随裁定
    # 一并作废——留着就是把守卫钉在已死的行为上。
    seg = _job_seg("gitee-release")
    assert "needs: [prepare, release]" in seg, \
        "Gitee Release 必须 gate 在 GitHub Release 之后（否则两源正文会分叉）"
    assert "continue-on-error" not in seg, \
        "Gitee 侧不得挂绳：半截绿等于悄悄回到手工时代，比不建更难发现"
    for needle, why in [
        ("/releases/tags/", "必须先按 tag 直查再决定 POST/PATCH（别拿列表序赌）"),
        ('method="PATCH"', "同 tag 已存在时要 PATCH 同步正文（PUT 必 405 实锤）"),
        ('"tag_name": tag', "PATCH 必须同载 tag_name（只发 body 直接 400）"),
        ('"name": tag', "PATCH 必须同载 name（同上）"),
        ("target_commitish", "缺省行为不可靠，必须显式指提交"),
        ("isascii", "token 带 BOM/不可见字符要当场鉴别（.gitee_token 曾带 BOM 致 401 假象）"),
        ("commits/${COMMIT}", "必须先确认 Gitee 侧真有本次 sha，否则 target_commitish 会指到旧提交"),
    ]:
        assert needle in seg, "Gitee Release job 缺判据 %r：%s" % (needle, why)
    assert "只推 GitHub" in ci, "历史裁定要留痕（现由 D4 反转，见 ci.yaml 三段注释）"


def test_e2e_script_key_steps():
    sh = (HERE / "e2e" / "run_e2e.sh").read_text(encoding="utf-8")
    assert sh.startswith("#!/usr/bin/env bash")
    assert "set -Eeuo pipefail" in sh
    for anchor in ("ha_e2e_driver.py", "docker exec", "eclipse-mosquitto:2",
                   "ghcr.io/home-assistant/home-assistant", "diag"):
        assert anchor in sh, f"E2E 编排缺关键锚: {anchor}"
    d = (HERE / "e2e" / "ha_e2e_driver.py").read_text(encoding="utf-8")
    for anchor in ("api/onboarding/users", "auth_code", "/auth/token",
                   "gateway/rpt_rsp", "config_entries/flow",
                   "/api/window_controller_gateway/devices",
                   "window_controller_gateway", "paho", "GITHUB_STEP_SUMMARY",
                   "other_settings"):
        assert anchor in d, f"E2E driver 缺关键锚: {anchor}"
    # auth 契约注释必须留痕（client_id 需 IndieAuth URL 形态的实证结论）
    assert "verify_client_id" in d and "indieauth" in d.lower()
    # 本地一键迭代 harness（与 CI 同一 driver，契约同源）
    rl = HERE / "e2e" / "run_local.sh"
    assert rl.exists()
    r = rl.read_text(encoding="utf-8")
    for anchor in ("ha_e2e_driver.py", "python[0-9.]* -m home[a]ssistant"):
        assert anchor in r, f"run_local.sh 缺关键锚: {anchor}"
    # 防自杀（v1.6.21 定案）：pkill/pgrep 的 homeassistant 模式必须写成
    # `home[a]ssistant` 括号形态——裸写会连本 harness 自身一起杀。此处由
    # 死循环（for … : pass）转正为真断言 + 裸模式反钉，两向都判。
    assert r.count("home[a]ssistant") >= 2, (
        "括号技巧的两个使用点（pkill + wait 循环）应都在："
        f"实际 {r.count('home[a]ssistant')} 处"
    )
    assert not re.search(r'p(?:kill|grep)\s+-f\s+"[^"]*homeassistant[^"]*"', r), (
        "pkill/pgrep 的 homeassistant 模式必须保留括号技巧（裸模式=自杀）"
    )

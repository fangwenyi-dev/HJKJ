""""巡查是否还活着"必须是**可查询的面**，不是"取决于日志级别的一行字"。

现网实锤（2026-10-28，.91 已装 v1.8.10）：v1.8.10 给 healer 加的每圈 INFO 留痕，
在用户与我唯一能读的通道里**不可见**——
  * `GET /api/hassio/core/logs` 这 100 行的级别分布实测 `{WARNING: 37}`，INFO/ERROR 各 0；
  * 阳性对照：HA 每次启动必然打的 `Setting up …` / `Home Assistant 2026` / `Ready` 全 0 命中
    ⇒ 该端点只回 WARNING 以上，不是"这几分钟恰好没有 INFO"。
⇒ 留痕只能算人读面包屑；判"healer 活着/多久没跑/上一圈结论"要有正面数据面。本批改：
  healer 每圈把状态写进 `hass.data[DOMAIN]["_channel_verify"]`，经只读视图
  `/api/window_controller_gateway/hub` 回 `channelVerify`，面板渲染一行。

纪律：视图的**两条返回路径**都要带这个键（无长连时如实回 disabled 那条也带），
且"从未巡查"要显式 `ran=False`——缺键会让面板退回兜底、看起来像"功能没实现"。
"""
import asyncio
import json
import pathlib
import re
from datetime import datetime
from types import SimpleNamespace

import custom_components.window_controller_gateway.api as api
import custom_components.window_controller_gateway.mqtt_bootstrap as mb

DOMAIN = mb.DOMAIN
STATE_KEY = "_channel_verify"
HERE = pathlib.Path(__file__).resolve().parents[1]


class _Hass:
    def __init__(self):
        self.data = {DOMAIN: {}}
        self.is_stopping = False
        self.config = SimpleNamespace(config_dir=".")
        self.config_entries = SimpleNamespace(
            async_entries=lambda dom: [SimpleNamespace(entry_id="E1")]
            if dom == DOMAIN else [])

    def async_create_task(self, coro, name=None, eager_start=True):
        return asyncio.ensure_future(coro)

    def async_create_background_task(self, coro, name, eager_start=True):
        # 与真 HA 同签名（name 必需）；桶差异见 test_startup_blocking_resident_task
        return asyncio.ensure_future(coro)


def _drive(monkeypatch, *, marker, verdicts, circles):
    """驱 healer 跑指定圈数：marker=True 走"引导未落地"分支，False 走"常驻核验"分支。"""
    state = {"n": 0, "slept": 0}
    gate = asyncio.Event()

    async def fake_sleep(hass, delay):
        state["slept"] += 1
        if state["slept"] < circles:
            await gate.wait()
            return True
        return False                     # 最后一圈：收手退出

    async def fake_marker(hass):
        return marker

    async def fake_verify(hass):
        v = verdicts[min(state["n"], len(verdicts) - 1)]
        state["n"] += 1
        return v

    async def fake_ensure(hass):
        return None

    async def fake_mdns(hass):
        return None

    monkeypatch.setattr(mb, "has_bootstrap_marker", fake_marker)
    monkeypatch.setattr(mb, "verify_builtin_channel", fake_verify)
    monkeypatch.setattr(mb, "ensure_mqtt_connection", fake_ensure)
    monkeypatch.setattr(mb, "_mdns_guard", fake_mdns)
    monkeypatch.setattr(mb, "_interruptible_sleep", fake_sleep)

    hass = _Hass()

    async def go():
        mb.async_start_bootstrap_healer(hass)
        task = hass.data[DOMAIN]["_bootstrap_healer"]
        for _ in range(circles):
            await asyncio.sleep(0)
            gate.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(go())
    return hass


def test_resident_verify_circle_records_queryable_state(monkeypatch):
    """常驻核验分支：每圈必须写一次可查询状态（圈号递增、结论与卡片态在位）。"""
    hass = _drive(monkeypatch, marker=False, verdicts=["ok", "mismatch"], circles=2)
    st = hass.data[DOMAIN].get(STATE_KEY)
    assert st, "healer 跑了两圈却没留任何可查询状态＝'活着不可证'回到 v1.8.10 前"
    assert st["ran"] is True
    assert st["phase"] == "verify"
    assert st["circles"] == 2, f"圈号应随巡查递增，实得 {st['circles']}"
    assert st["verdict"] == "mismatch", "必须回最新一圈的判定（不是第一圈）"
    assert st["cardUp"] is True, "verdict 不在 (ok,no_endpoint) ⇒ 卡片应报'在挂'"
    assert st["intervalS"] == mb.CHANNEL_VERIFY_INTERVAL, "节拍必须取常量现值"
    datetime.fromisoformat(st["atIso"])          # 时间戳必须可解析（面板要算"多久之前"）


def test_bootstrap_branch_records_too(monkeypatch):
    """另一半分支也要记：引导未落地期（每 300s~1h 一轮）同样不能是哑的。"""
    hass = _drive(monkeypatch, marker=True, verdicts=["ok"], circles=1)
    st = hass.data[DOMAIN].get(STATE_KEY)
    assert st and st["ran"] is True, "走 bootstrap 分支时不记状态＝这段周期内不可判"
    assert st["phase"] == "bootstrap"
    assert st["circles"] == 1


def test_view_exposes_channel_verify_next_to_hub_state(monkeypatch):
    """视图承诺面（有长连）：channelVerify 必须与 hub 状态同屏回，不让用户翻日志。"""
    captured = {}

    class _Client:
        async def maybe_refresh_members(self):
            return None

        def status_view(self):
            return {"connected": True, "enabled": False}

    hass = _Hass()
    hass.data[DOMAIN][api.HUB_DATA_KEY] = _Client()
    monkeypatch.setattr(api, "_hub_client", lambda h: hass.data[DOMAIN][api.HUB_DATA_KEY])
    v = api.WindowGatewayHubView()
    v.json = lambda payload: (captured.update(payload), payload)[1]

    async def _get():
        await v.get(_Req(hass))

    asyncio.run(_get())
    assert captured["enabled"] is True
    assert captured.get("channelVerify", {}).get("ran") is False, \
        "没巡查过也要显式回 ran=False（缺键＝面板退回兜底，看着像功能没做）"
    hass.data[DOMAIN][STATE_KEY] = {"ran": True, "phase": "verify", "circles": 3,
                                    "verdict": "ok", "cardUp": False,
                                    "intervalS": 1800.0, "atIso": "2026-10-28T08:41:14+00:00"}
    captured.clear()
    asyncio.run(_get())
    assert captured["channelVerify"]["circles"] == 3
    assert captured["channelVerify"]["verdict"] == "ok"


def test_view_without_hub_client_still_exposes_channel_verify(monkeypatch):
    """视图的另一条返回路径（无长连⇒如实回 disabled）也必须带巡查面。

    扫全承诺方：healer 活在条目 setup 上，与云长连无关；只有一条路径带这个键
    ＝"broker 起了但 hub 没绑"的常见现场依旧不可判。
    """
    captured = {}
    hass = _Hass()
    hass.data[DOMAIN][STATE_KEY] = {"ran": True, "phase": "verify", "circles": 1,
                                    "verdict": "no_endpoint", "cardUp": False,
                                    "intervalS": 1800.0, "atIso": "2026-10-28T08:41:14+00:00"}
    monkeypatch.setattr(api, "_hub_client", lambda h: None)
    v = api.WindowGatewayHubView()
    v.json = lambda payload: (captured.update(payload), payload)[1]
    asyncio.run(v.get(_Req(hass)))
    assert captured.get("enabled") is False
    assert captured.get("channelVerify", {}).get("circles") == 1, \
        "无长连路径漏带巡查面＝面板在最常见的现场看不到它"
    json.dumps(captured)                       # 必须整体可序列化（视图回 JSON）


class _Req:
    def __init__(self, hass):
        self.app = {"hass": hass}


def test_panel_renders_the_row_with_both_branches_and_staleness():
    """接线判据按**盘上交付形状**写：承载元素、两条分支文案、超时判定取服务端节拍。"""
    js = (HERE / "www" / "js" / "huijian.js").read_text(encoding="utf-8")
    html = (HERE / "www" / "index.html").read_text(encoding="utf-8")
    assert 'id="hubVerifyTip"' in html, "面板没有承载元素＝这行渲染不出来"
    assert "channelVerify" in js, "面板没读视图新键（只读日志就回到 v1.8.10 的死路）"
    body = js[js.index("channelVerify") - 400:js.index("channelVerify") + 1600]
    assert "ran" in body, "必须分'巡查过/没巡查过'两态（合并成一态＝把不可判换个说法）"
    assert "intervalS" in body, "超时判定必须用服务端回的节拍现值（硬编秒数＝改了常量就骗人）"
    assert "未巡查" in js or "未见巡查" in js, \
        "ran=False 时要有明确文案，不能静默留空（留空与'功能没做'在用户眼里同形）"
    assert len(re.findall(r"getElementById\('hubVerifyTip'\)", js)) == 1, \
        "多出口渲染同一行＝总有一条路径漏刷（本仓单一出口纪律）"

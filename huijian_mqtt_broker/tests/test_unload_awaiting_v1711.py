"""v1.7.11 真栈双修回归（代理把 awaiting 链变主路径后暴露的潜伏 bug）：

1. async_unload_entry / _cleanup_partial_setup 必须只对真 forward 过平台的
   条目调 async_unload_platforms——HA≥2024 平台组件对 never-loaded 条目抛
   ValueError "Config entry was never loaded!"（entity_component.py:228，
   真栈实锤 12:30/12:37：awaiting 条目每次 reload 5 条 ERROR traceback）。
2. discovery 3.5 自动填充只经 update listener 单驱动 reload——显式
   async_reload 与 listener 的 reload 并发竞态（v1.6.19 已在
   _migrate_devices_async 定过同款案，discovery 3.5 是漏网之鱼）。
"""
import asyncio
from types import SimpleNamespace

import custom_components.window_controller_gateway as pkg
import custom_components.window_controller_gateway.discovery as disc_mod
import custom_components.window_controller_gateway.ws_gateway as wsg_mod
from custom_components.window_controller_gateway.const import (
    DOMAIN, CONF_GATEWAY_SN)

HA_NEVER_LOADED_MSG = "Config entry was never loaded!"


def _mk_hass(runtime):
    """记录型假 hass：async_unload_platforms 逐字复刻 HA 2026.1 行为——
    对未 forward 过的条目抛 ValueError。"""
    calls = []

    async def fake_unload_platforms(entry, platforms):
        calls.append(list(platforms))
        if not runtime.get("_platforms_forwarded"):
            raise ValueError(HA_NEVER_LOADED_MSG)
        return True

    hass = SimpleNamespace(
        data={DOMAIN: {"E1": runtime}},
        config_entries=SimpleNamespace(
            async_unload_platforms=fake_unload_platforms),
    )
    return hass, calls


def _patch_env(monkeypatch, *, ws_ok=True):
    async def noop_save(hass):
        return None

    async def noop_ws(hass):
        return None

    monkeypatch.setattr(pkg, "save_persistent_data", noop_save)
    monkeypatch.setattr(wsg_mod, "async_ensure_ws_gateway", noop_ws)


_INIT_SRC = (
    __import__("pathlib").Path(pkg.__file__).read_text(encoding="utf-8"))


class TestAwaitingBootstrapAnchor:
    def test_awaiting_branch_calls_ensure_mqtt(self):
        """v1.7.11 相位 E 实锤前件：干净客户机（无 MQTT 条目）时，代理建的
        awaiting 条目必须自己驱动 bootstrap——否则心跳武装 120s 超时，
        自动发现链静默断掉（config_flow 分支有 bootstrap，awaiting 曾漏）。"""
        start = _INIT_SRC.find("    if not gateway_sn:")
        end = _INIT_SRC.find("# ---- 有网关 SN：完整设置 ----")
        assert 0 <= start < end, "awaiting 分支锚丢失"
        seg = _INIT_SRC[start:end]
        assert "ensure_mqtt_connection" in seg, \
            "awaiting 分支必须尽力 bootstrap MQTT（标记在才动作，无标记 no-op）"
        assert "ConfigEntryNotReady" in seg, "引导失败要按'稍后再试'语义吞掉"


class TestUnloadGate:
    def test_awaiting_entry_skips_platform_unload(self, monkeypatch):
        _patch_env(monkeypatch)
        runtime = {"_awaiting_gateway": True}  # 从未 forward
        hass, calls = _mk_hass(runtime)
        entry = SimpleNamespace(entry_id="E1", title="慧尖网关", data={})
        ok = asyncio.run(pkg.async_unload_entry(hass, entry))
        assert ok is True, "awaiting 卸载必须成功（旧版被 ValueError 打成 ERROR 风暴）"
        assert calls == [], f"awaiting 条目不得调 async_unload_platforms，实得 {calls}"
        assert "E1" not in hass.data[DOMAIN], "成功卸载应清 runtime"

    def test_promoted_entry_still_unloads_platforms(self, monkeypatch):
        _patch_env(monkeypatch)
        runtime = {"_platforms_forwarded": True}
        hass, calls = _mk_hass(runtime)
        entry = SimpleNamespace(entry_id="E1", title="网关", data={})
        ok = asyncio.run(pkg.async_unload_entry(hass, entry))
        assert ok is True
        assert len(calls) == 1 and len(calls[0]) == 5, \
            f"forward 过的条目必须整批卸载 5 平台（A-1B 僵尸实体防线不松），实得 {calls}"

    def test_cleanup_partial_gated_too(self, monkeypatch):
        """forward 之前的 setup 失败路径：_cleanup_partial_setup 同样不得
        强卸从未加载的平台（A-1B 保留：forward 之后失败仍要清）。"""
        calls = []

        async def fake_unload_platforms(entry, platforms):
            calls.append(1)
            raise ValueError(HA_NEVER_LOADED_MSG)

        hass = SimpleNamespace(
            data={DOMAIN: {"E1": {}}},  # 无 flag：forward 之前
            config_entries=SimpleNamespace(
                async_unload_platforms=fake_unload_platforms))
        entry = SimpleNamespace(entry_id="E1")
        asyncio.run(pkg._cleanup_partial_setup(None, None, []))  # 无 hass/entry：no-op
        assert calls == []
        asyncio.run(pkg._cleanup_partial_setup(None, None, [], hass=hass, entry=entry))
        assert calls == [], "未 forward 的清理路径不得碰平台卸载"
        # forward 之后失败：仍必须清（A-1B 语义保留）
        calls.clear()
        hass.data[DOMAIN]["E1"]["_platforms_forwarded"] = True
        asyncio.run(pkg._cleanup_partial_setup(None, None, [], hass=hass, entry=entry))
        assert calls == [1], "forward 之后失败仍须卸载平台"


class TestAutoFillSingleReload:
    def _disc_hass(self):
        calls = {"update": [], "reload": []}

        async def fake_reload(entry_id):
            calls["reload"].append(entry_id)

        entry = SimpleNamespace(entry_id="E1", data={})
        hass = SimpleNamespace(
            data={DOMAIN: {"discovery": {"ignored_gateways": set(),
                                         "last_discovery_time": {},
                                         "announced_gateways": set()}}},
            config_entries=SimpleNamespace(
                async_entries=lambda domain: [entry],
                async_update_entry=lambda e, data=None, **kw:
                    calls["update"].append(dict(data or {})),
                async_reload=fake_reload,
                flow=SimpleNamespace(async_progress=lambda: []),
            ),
        )
        return hass, calls

    def test_awaiting_entry_not_silently_filled_card_instead(self):
        """v1.7.62（用户裁定）：空 SN 等待条目**不再被静默填充**——首台网关
        同样弹发现卡（旧 v1.7.11 行为＝不可见添加，现场两次被判为"配置有问题"）。
        （旧的"填充只经 update listener 单驱动 reload"议题随实现移除而终结。）"""
        hass, calls = self._disc_hass()
        flows = []

        async def fake_init(domain, context=None, data=None):
            flows.append({"context": context, "data": data})

        hass.config_entries.flow.async_init = fake_init
        disc_mod.dr = SimpleNamespace(async_get=lambda h: SimpleNamespace(
            async_get_device=lambda identifiers=None: None))
        asyncio.run(disc_mod.async_discover_gateway(
            hass, "100122501203", "慧尖网关 1203"))
        assert calls["update"] == [], "不许再静默填充（首台弹卡）"
        assert calls["reload"] == []
        assert len(flows) == 1, "必须走标准发现流（卡片）"
        assert flows[0]["data"]["gateway_sn"] == "100122501203"

    def test_second_gateway_after_fill_gets_flow(self):
        """填充转正后（条目带 SN），第二个网关走标准 discovery flow——
        确认权留给用户（D 相位语义的单元面）。"""
        hass, calls = self._disc_hass()
        flows = []

        async def fake_init(domain, context=None, data=None):
            flows.append({"context": context, "data": data})

        hass.config_entries.flow.async_init = fake_init
        filled = SimpleNamespace(entry_id="E1",
                                 data={CONF_GATEWAY_SN: "100122501203"})
        hass.config_entries.async_entries = lambda domain: [filled]
        # conftest 假 dr 模块无 async_get——整体替换为"无既有设备"桩
        disc_mod.dr = SimpleNamespace(
            async_get=lambda h: SimpleNamespace(
                async_get_device=lambda identifiers=None: None))
        asyncio.run(disc_mod.async_discover_gateway(
            hass, "100199999999", "慧尖网关 9999"))
        assert calls["update"] == [] and calls["reload"] == []
        assert len(flows) == 1
        assert flows[0]["context"]["source"] == "discovery"
        assert flows[0]["data"]["gateway_sn"] == "100199999999"

"""v1.7.62（用户裁定）：首台网关不再静默自动填充，同样走发现卡片确认。

背景：v1.7.11 起"零条目时首台全自动"——把代理建的空 SN 等待条目直接填上 SN
转正，无卡片、无通知。用户现场两次报障（"第一个网关上报了还是直接添加到集成
中的，没有出现弹出卡片"）⇒ 改为：发现链每次添加都留下可见确认动作（卡片 →
表单 → 提交）；等待条目此后只当耳朵，用户确认后由 `async_remove_awaiting_entries`
清理。
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import custom_components.window_controller_gateway.discovery as disc_mod
from custom_components.window_controller_gateway.const import (
    DOMAIN, CONF_GATEWAY_SN)

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "window_controller_gateway"


def _hass(entries):
    flows = []

    async def fake_init(domain, context=None, data=None):
        flows.append({"context": context, "data": data})

    hass = SimpleNamespace(
        data={DOMAIN: {"discovery": {"ignored_gateways": set(),
                                     "last_discovery_time": {},
                                     "announced_gateways": set()}}},
        config_entries=SimpleNamespace(
            async_entries=lambda domain: list(entries),
            async_update_entry=lambda e, data=None, **kw: e.__setitem__("data", data),
            async_init=None,
            flow=SimpleNamespace(async_progress=lambda: [], async_init=fake_init),
        ),
    )
    return hass, flows


def test_awaiting_entry_is_not_silently_filled(tmp_path):
    """空 SN 等待条目在位时：**不许**被静默填 SN（旧步骤 3.5），必须弹卡。"""
    empty = SimpleNamespace(entry_id="EA", data={}, title="慧尖网关")
    hass, flows = _hass([empty])
    disc_mod.dr = SimpleNamespace(async_get=lambda h: SimpleNamespace(
        async_get_device=lambda identifiers=None: None))

    asyncio.run(disc_mod.async_discover_gateway(hass, "1001215011a3", "慧尖网关 11a3"))
    assert empty.data.get(CONF_GATEWAY_SN) is None, "不许再静默填充（首台弹卡）"
    assert len(flows) == 1, "必须走标准发现流（卡片）"
    assert flows[0]["context"]["source"] == "discovery"
    assert flows[0]["data"]["gateway_sn"] == "1001215011a3"


def test_configured_gateway_still_skipped():
    """反向臂：已在条目里的网关照旧跳过发现（step 3 语义不变）。"""
    filled = SimpleNamespace(entry_id="E1",
                             data={CONF_GATEWAY_SN: "1001215011a3"}, title="x")
    hass, flows = _hass([filled])
    disc_mod.dr = SimpleNamespace(async_get=lambda h: SimpleNamespace(
        async_get_device=lambda identifiers=None: None))
    asyncio.run(disc_mod.async_discover_gateway(hass, "1001215011a3", "同名"))
    assert flows == []


def test_remove_awaiting_entries_cleans_only_empty():
    """清理助手：只删空 SN 条目，带 SN 的一律保留。"""
    empty = SimpleNamespace(entry_id="EMPTY", data={})
    filled = SimpleNamespace(entry_id="FILLED",
                             data={CONF_GATEWAY_SN: "10012250123f"})
    removed = []

    async def fake_remove(eid):
        removed.append(eid)

    hass = SimpleNamespace(config_entries=SimpleNamespace(
        async_entries=lambda dom: [empty, filled],
        async_remove=fake_remove))
    n = asyncio.run(disc_mod.async_remove_awaiting_entries(hass))
    assert n == 1 and removed == ["EMPTY"]


def test_remove_failure_does_not_raise():
    """清理失败只告警（添加结果不受影响）。"""
    empty = SimpleNamespace(entry_id="EMPTY", data={})

    async def boom(eid):
        raise RuntimeError("unload failed")

    hass = SimpleNamespace(config_entries=SimpleNamespace(
        async_entries=lambda dom: [empty], async_remove=boom))
    assert asyncio.run(disc_mod.async_remove_awaiting_entries(hass)) == 0


def test_create_paths_schedule_cleanup():
    """接线钉：两条带 SN 的创建路径（用户填 SN / 连接测试后仍添加）都必须
    调度清理；助手本体在 discovery.py（唯一实现）。"""
    src = (PKG / "config_flow.py").read_text(encoding="utf-8")
    assert src.count("self._schedule_awaiting_cleanup()") == 2, \
        "两条创建路径各一处（发现卡确认最终也走 async_step_user）"
    assert "def _schedule_awaiting_cleanup" in src
    dsrc = (PKG / "discovery.py").read_text(encoding="utf-8")
    assert "async def async_remove_awaiting_entries" in dsrc
    # 钉"机制"不钉字样（注释里写"取消静默填充"会满足字样钉——本仓既有教训）：
    # 填充的实现形态 = 对已有条目 async_update_entry(data=+SN) + unique_id 回填
    assert "async_update_entry(entry" not in dsrc, \
        "静默填充的实现必须彻底移除（注释提及不算）"
    assert 'update_kwargs["unique_id"] = gateway_key' not in dsrc


def test_flow_paths_still_set_unique_id():
    """E-4 不变量的新落点：静默填充没了，带 SN 的条目一律由各流入口的
    async_set_unique_id 建立唯一性（HA 原生查重的单一真源）。"""
    src = (PKG / "config_flow.py").read_text(encoding="utf-8")
    assert src.count("await self.async_set_unique_id(") >= 3, \
        "user/confirm_add/discovery 三个入口都要设 uid"
    assert "_abort_if_unique_id_configured()" in src

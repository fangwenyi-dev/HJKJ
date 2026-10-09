# -*- coding: utf-8 -*-
"""v1.8.6 审计收口判据：G-10（hub 端点/密钥改了就生效）+ G-9 写入点接线。

G-10 形状：`async_ensure_hub_client` 的 attach 支此前只挂集合、不比对端点，而
base/install_key 是**构造期绑死**的（hub_client.py:378-379，长连与重连全读
self.base）。≥2 条目时任一条目 reload 都只会走到 attach ⇒ 带外覆盖
（HUIJIAN_HUB_BASE 环境变量、手改 .storage 里的 options）永不生效，而
"端点被覆盖"那行日志只在创建支打 ⇒ 连"还在用旧端点"都看不见。同形态在
ws_gateway（:1096-1102）早已定案"比对 + 必要时热同步/重建"。

两条反向臂与正向臂同样重要：
  · 端点没变时**不得**重建长连——重建会重注册并作废用户手上所有绑定码
    （hub 侧一次性码语义），把"改了不生效"修成"没改也天天作废"是更坏的结果；
  · 停机窗内不得重建——C-2 那条实锤（新实例的 STOP 监听注册时事件已派发过
    ⇒ 任务与 aiohttp 会话无人回收）。
全部走真入口 `async_ensure_hub_client`，不手塞字典形状。
"""
import asyncio
import types

import custom_components.window_controller_gateway as pkg
from custom_components.window_controller_gateway.const import (
    DOMAIN, HUB_DATA_KEY)
from custom_components.window_controller_gateway.hub_client import (
    HUB_BASE_ENV, HUB_DEFAULT_INSTALL_KEY, resolve_hub_base)

DEFAULT_BASE = resolve_hub_base("")


class FakeManager:
    def __init__(self, gateway_sn="GW1"):
        self.gateway_sn = gateway_sn

    def add_status_listener(self, cb):
        pass

    def remove_status_listener(self, cb):
        pass


class FakeHub:
    """替身：记录构造次序，暴露真实现有的 base/install_key 两个属性面。"""

    made = []
    attach_calls = []

    def __init__(self, managers=None, **kw):
        self.managers = list(managers or [])
        self.base = kw.get("base")
        self.install_key = kw.get("install_key")
        self.stopped = False
        FakeHub.made.append(self)

    def attach_managers(self, managers):
        self.managers = list(managers)
        FakeHub.attach_calls.append(self)

    async def async_start(self):
        pass

    async def async_stop(self):
        self.stopped = True


def _hass(tmp_path, *, n_entries=2, option_base="", stopping=False):
    """n_entries 条已完成设置的条目（各带自己的 manager），hub 实例预置在 domain_data。"""
    entries = [types.SimpleNamespace(
        entry_id="e%d" % i,
        options=({"hub_base": option_base} if (option_base and i == 0) else {}),
        data={"gateway_sn": "GW%d" % (i + 1)},
    ) for i in range(n_entries)]
    domain_data = {}
    for ent in entries:
        # 真聚合口径：_hub_managers 只认 `_setup_complete` + device_manager 就位；
        # 期望数（G-2）只认"启用且 data 里有 SN"——两份判据都要能被本夹具喂到。
        domain_data[ent.entry_id] = {
            "_setup_complete": True,
            "device_manager": FakeManager(ent.data["gateway_sn"]),
        }
    hass = types.SimpleNamespace(
        data={DOMAIN: domain_data},
        config=types.SimpleNamespace(config_dir=str(tmp_path)),
        config_entries=types.SimpleNamespace(async_entries=lambda domain: entries),
        bus=types.SimpleNamespace(async_listen_once=lambda *a, **k: (lambda: None)),
    )
    if stopping:
        hass.is_stopping = True
    return hass, domain_data


async def _running_client(hass, domain_data, base=DEFAULT_BASE,
                          install_key=HUB_DEFAULT_INSTALL_KEY):
    """先跑一次 ensure 造出"在跑的实例"（走真创建支，不手塞）。"""
    FakeHub.made, FakeHub.attach_calls = [], []
    await pkg.async_ensure_hub_client(hass)
    current = domain_data[HUB_DATA_KEY]
    current.base, current.install_key = base, install_key
    return current


def _reset():
    FakeHub.made, FakeHub.attach_calls = [], []


def test_endpoint_change_rebuilds_the_long_connection(monkeypatch, tmp_path):
    """option 把 hub_base 指向别处 ⇒ 必须停旧实例、按新端点重建（G-10 正向臂）。"""
    monkeypatch.delenv(HUB_BASE_ENV, raising=False)
    monkeypatch.setattr(pkg, "HubClient", FakeHub)
    hass, domain_data = _hass(tmp_path, option_base="http://new-hub:9")
    old = asyncio.run(_running_client(hass, domain_data))
    _reset()

    asyncio.run(pkg.async_ensure_hub_client(hass))

    assert old.stopped is True, "旧实例没停 ⇒ 它还会继续打旧端点"
    assert not FakeHub.attach_calls, "不该在旧实例上 attach 了事"
    assert len(FakeHub.made) == 1, FakeHub.made
    new = FakeHub.made[0]
    assert new.base == "http://new-hub:9", new.base
    assert domain_data[HUB_DATA_KEY] is new, "新实例没接上键"


def test_unchanged_endpoint_does_not_rebuild(monkeypatch, tmp_path):
    """反向臂：端点/密钥一致时只许 attach。重建＝重注册＝作废用户手上全部绑定码。"""
    monkeypatch.delenv(HUB_BASE_ENV, raising=False)
    monkeypatch.setattr(pkg, "HubClient", FakeHub)
    hass, domain_data = _hass(tmp_path)
    old = asyncio.run(_running_client(hass, domain_data))
    _reset()

    asyncio.run(pkg.async_ensure_hub_client(hass))

    assert FakeHub.made == [], "端点没变也重建 ⇒ 每次 ensure 都在作废绑定码"
    assert old.stopped is False
    assert FakeHub.attach_calls == [old], "常规路径（条目 reload）必须仍走 attach"
    assert domain_data[HUB_DATA_KEY] is old


def test_key_change_also_rebuilds(monkeypatch, tmp_path):
    """install_key 同为构造期绑死，改它同样要生效（否则云侧鉴权永远对新钥失效）。"""
    monkeypatch.delenv(HUB_BASE_ENV, raising=False)
    monkeypatch.setattr(pkg, "HubClient", FakeHub)
    hass, domain_data = _hass(tmp_path)
    old = asyncio.run(_running_client(hass, domain_data))
    old.install_key = "old-key"
    _reset()

    asyncio.run(pkg.async_ensure_hub_client(hass))

    assert old.stopped is True and len(FakeHub.made) == 1
    assert FakeHub.made[0].install_key == HUB_DEFAULT_INSTALL_KEY


def test_no_rebuild_inside_ha_stop_window(monkeypatch, tmp_path):
    """停机窗内不重建（C-2）：新实例的 STOP 监听注册时事件已派发过 ⇒ 无人回收任务。"""
    monkeypatch.delenv(HUB_BASE_ENV, raising=False)
    monkeypatch.setattr(pkg, "HubClient", FakeHub)
    hass, domain_data = _hass(tmp_path, option_base="http://new-hub:9", stopping=True)
    old = asyncio.run(_running_client(hass, domain_data))
    _reset()

    asyncio.run(pkg.async_ensure_hub_client(hass))

    assert FakeHub.made == [], "停机窗内重建＝留下没人回收的 aiohttp 会话"
    assert old.stopped is False
    assert FakeHub.attach_calls == [old], "停机窗内退回旧行为（只挂集合）"


# ── G-9 写入点接线（域判的行为侧已在 test_v1747_winact_echo.py 跑过）────
def test_wind_lock_mode_write_point_is_normalized_before_storing():
    """005 attrs 的写入点必须先过域判再入库，不许原样存 value。

    只钉视图会漏掉真问题：垃圾值一旦进 `attributes["wind_lock_mode"]` 就随
    persist 落盘，此后即使读侧钳住，坏数据也永久留在设备字典里。
    块内有界匹配（不跨函数），防止 `.*?` 把别的分支也算进来。
    """
    import pathlib
    import re

    src = (pathlib.Path(__file__).resolve().parents[1] / "custom_components" /
           "window_controller_gateway" / "mqtt_handler" / "_ctypes.py"
           ).read_text(encoding="utf-8")
    m = re.search(r'elif attribute == "rwp_wind_lock_mode":(.*?)'
                  r'elif attribute == "rwp_winact_speed":', src, re.S)
    assert m, "005 风锁模式分支锚丢失（结构变了要同步改这条钉）"
    block = m.group(1)
    assert "wind_lock_mode_or_unknown" in block, "写入点没接域判：垃圾值会直接落盘"
    assert not re.search(r'attributes\["wind_lock_mode"\]\s*=\s*value\b', block), \
        "回潮：原样存 value（越界值随 persist 进盘）"

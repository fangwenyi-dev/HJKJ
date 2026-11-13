# -*- coding: utf-8 -*-
"""v1.9.7：小程序子设备名称跟 HA 同步（用户 2026-11-13 定案）。

用户原话：「小程序 开窗器子设备的名称 还是跟ha同步吧 现在一个一个改好麻烦」。

契约（跨仓，本仓是**发送端**，小程序仓是消费端）：
  · 加载项只下发"用户**在 HA 里手动改过**的名字"（device registry 的 name_by_user）。
    没改过的一律不带 `name` 键 —— 否则会把默认长名「开窗器 GW尾4-DEV尾4」搬进小程序，
    比它自己的「开窗器 后四位」更长且没多任何信息。
  · 三条通道同源：LAN `device_list`、LAN `device_update`、云状态快照都经
    `ha_custom_device_name(manager, dev_sn)` 取名（v1.4.28 的教训：两处各算一遍迟早会漂）。
  · 云快照**始终写 name 键**（没改过＝空串）：hub 的 setStates 是按 sn 合并
    （store.js `Object.assign`），缺键会保留上一轮旧名字 —— 用户在 HA 里把名字改回去后
    云端会永远停在旧名上。LAN 的列表/推送是全量/增量语义不同，不需要这个空键。
  · `rename_device()` 已经走 `_notify_status_listeners` 漏斗（v1.7.33），本条把
    device_update 里缺的名字字段补上，改名才会**立刻**推到小程序（此前要等下次 get_devices）。

为什么读注册表而不是 `self.devices[sn]["name"]`：HA 设备页重命名只写注册表；内存里的名字
不落盘（persist.py 只存映射/手动删除表），HA 重启回填会重置成默认名。
"""
from types import SimpleNamespace

from custom_components.window_controller_gateway import const as c
from custom_components.window_controller_gateway import device_manager as dm_mod
from custom_components.window_controller_gateway import hub_client as hc
from custom_components.window_controller_gateway.ws_gateway import (
    WsGatewayServer, ha_custom_device_name)

GW_SN = "100122501207"
DEV_SN = "500500000001"
OTHER_SN = "500500000002"


# ==================== 替身 ====================

class _Registry:
    """两代设备注册表的最小替身（只实现 get_device_by_sn 会走到的那个出口）。"""

    def __init__(self, found):
        self._found = found
        self.calls = []

    def async_get_device(self, identifiers=None, connections=None):
        self.calls.append(frozenset(identifiers or ()))
        return self._found


class _Entry:
    def __init__(self, name_by_user=None, name=None):
        self.name_by_user = name_by_user
        self.name = name


def _make_dm(monkeypatch, registry):
    hass = SimpleNamespace(
        data={c.DOMAIN: {}},
        config=SimpleNamespace(config_dir="."),
        config_entries=SimpleNamespace(
            async_get_entry=lambda eid: None, async_entries=lambda d: []),
    )
    entry = SimpleNamespace(entry_id="e1", data={c.CONF_GATEWAY_SN: GW_SN}, options={})
    monkeypatch.setattr(dm_mod, "async_get", lambda _hass: registry)
    return dm_mod.WindowControllerDeviceManager(hass, entry)


class _Manager:
    """设备管理器替身：ws/hub 两侧只用到 devices / gateway_sn / 名字取值。"""

    gateway_sn = GW_SN

    def __init__(self, names=None):
        self.devices = {
            DEV_SN: {"attributes": {"r_travel": 30, "voltage": 12.3}},
            OTHER_SN: {"attributes": {"r_travel": 0}},
        }
        self._names = names or {}

    def get_ha_custom_name(self, device_sn):
        return self._names.get(device_sn, "")

    def add_status_listener(self, cb):
        return None


# ==================== ① DeviceManager.get_ha_custom_name ====================

class TestGetHaCustomName:
    def test_returns_user_renamed_name_only(self, monkeypatch):
        dm = _make_dm(monkeypatch, _Registry(_Entry(name_by_user="  客厅窗  ")))
        assert dm.get_ha_custom_name(DEV_SN) == "客厅窗"

    def test_default_name_without_user_rename_is_not_sent(self, monkeypatch):
        """没手动改过 ⇒ 回空串（调用方据此不带 name 键，小程序用本地命名）。"""
        dm = _make_dm(monkeypatch, _Registry(_Entry(name="开窗器 1207-0001")))
        assert dm.get_ha_custom_name(DEV_SN) == "", \
            "把注册表默认名当成了用户改名（小程序会显示更长的默认名）"

    def test_device_not_found_and_registry_absent(self, monkeypatch):
        dm = _make_dm(monkeypatch, _Registry(None))
        assert dm.get_ha_custom_name(DEV_SN) == ""
        dm2 = _make_dm(monkeypatch, None)
        assert dm2.get_ha_custom_name(DEV_SN) == ""

    def test_registry_exception_never_escapes(self, monkeypatch):
        """名称是可选装饰：注册表炸了也不许把状态上行/列表拖垮。"""
        class _Boom:
            def async_get_device(self, identifiers=None, connections=None):
                raise RuntimeError("registry boom")

        dm = _make_dm(monkeypatch, _Boom())
        assert dm.get_ha_custom_name(DEV_SN) == ""

    def test_name_by_user_non_string_is_ignored(self, monkeypatch):
        dm = _make_dm(monkeypatch, _Registry(_Entry(name_by_user=123)))
        assert dm.get_ha_custom_name(DEV_SN) == ""


# ==================== ② 三条下发通道同源 ====================

class TestChannelsCarryTheName:
    def test_helper_is_defensive(self):
        """替身/夹具没有该能力 ⇒ 空串；取值抛异常 ⇒ 空串；非字符串 ⇒ 空串。"""
        assert ha_custom_device_name(_Manager({"x": "客厅窗"}), DEV_SN) == ""
        assert ha_custom_device_name(object(), DEV_SN) == ""

        class _Boom:
            def get_ha_custom_name(self, _sn):
                raise RuntimeError("boom")

        assert ha_custom_device_name(_Boom(), DEV_SN) == ""

        class _Weird:
            def get_ha_custom_name(self, _sn):
                return 123

        assert ha_custom_device_name(_Weird(), DEV_SN) == ""

    def test_device_list_carries_only_renamed_devices(self, monkeypatch):
        manager = _Manager({DEV_SN: "客厅窗"})
        server = WsGatewayServer(SimpleNamespace(), port=9001, token="t")
        monkeypatch.setattr(server, "_entries_data",
                            lambda: [("e1", {"device_manager": manager})])
        payload = server._device_list_payload()
        by_sn = {it["sn"]: it for it in payload["devices"]}
        assert by_sn[DEV_SN]["name"] == "客厅窗"
        assert "name" not in by_sn[OTHER_SN], \
            "没改名的设备也带上了 name（会把默认长名搬进小程序）"
        # 视图原有契约键一个都不能少（name 是可选装饰，不是替换）
        assert {"sn", "gwSn", "position", "battery", "state", "windLockMode",
                "winactSpeed", "winactStrength"} <= set(by_sn[DEV_SN])

    def test_device_update_carries_name_for_rename_push(self, monkeypatch):
        """改名的即时推送必须带名字：v1.7.33 把 rename 接进了通知漏斗，但推送里
        此前根本没有 name 字段 ⇒ 小程序要等下次 get_devices 才见新名。"""
        manager = _Manager({DEV_SN: "客厅窗"})
        server = WsGatewayServer(SimpleNamespace(), port=9001, token="t")
        monkeypatch.setattr(server, "_find_entry",
                            lambda _gw: {"device_manager": manager})
        payload = server._device_update_payload(GW_SN, DEV_SN)
        assert payload["name"] == "客厅窗"
        assert payload["type"] == "device_update"
        # 没改名的设备不带键（与列表同口径）
        payload2 = server._device_update_payload(GW_SN, OTHER_SN)
        assert "name" not in payload2

    def test_cloud_snapshot_always_writes_name_key(self, tmp_path):
        """云快照必须**始终**带 name（空串也算）：hub 按 sn 合并，缺键会保留旧名。"""
        client = hc.HubClient([_Manager({DEV_SN: "客厅窗"})],
                              config_dir=str(tmp_path), session=object())
        items, _auth = client.build_state_snapshot()
        by_sn = {it["sn"]: it for it in items}
        assert by_sn[DEV_SN]["name"] == "客厅窗"
        assert OTHER_SN in by_sn and by_sn[OTHER_SN]["name"] == "", \
            "没改名的设备在云快照里缺 name 键 ⇒ hub 合并会保留上一轮旧名"

    def test_snapshot_helper_failure_does_not_break_upload(self, tmp_path):
        """名字取不到（替身没能力/注册表异常）时快照照常上行，name 为空串。"""
        class _NoNameManager(_Manager):
            def __init__(self):
                super().__init__()

            def get_ha_custom_name(self, _sn):
                raise RuntimeError("boom")

        client = hc.HubClient([_NoNameManager()],
                              config_dir=str(tmp_path), session=object())
        items, _auth = client.build_state_snapshot()
        assert items, "名字取值异常把整条状态上行拖垮了"
        assert all(it["name"] == "" for it in items)


# ==================== ③ 结构钉：三条通道同源，不许各写一份 ====================

class TestSingleSourcePins:
    def test_ws_channels_use_the_shared_helper(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "custom_components"
               / "window_controller_gateway" / "ws_gateway.py").read_text(encoding="utf-8")
        list_body = src.split("def _device_list_payload", 1)[1].split("\n    # ----------", 1)[0]
        update_body = src.split("def _device_update_payload", 1)[1].split("\n    async def ", 1)[0]
        assert "ha_custom_device_name(manager, dev_sn)" in list_body, \
            "device_list 没走共享取名函数（两处各算一遍迟早会漂）"
        assert "ha_custom_device_name(data[\"device_manager\"], device_sn)" in update_body, \
            "device_update 没走共享取名函数"

    def test_hub_snapshot_uses_the_shared_helper(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "custom_components"
               / "window_controller_gateway" / "hub_client.py").read_text(encoding="utf-8")
        assert "from .ws_gateway import ha_custom_device_name" in src, \
            "云快照自己算名字＝第三条通道开始漂"
        body = src.split("def build_state_snapshot", 1)[1]
        assert 'view["name"] = _ha_custom_name(manager, dev_sn)' in body

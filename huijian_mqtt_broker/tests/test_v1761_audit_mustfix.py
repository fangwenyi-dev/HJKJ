"""v1.7.61 必修 5 条行为钉（源自 docs/bug-audit-2026-10-05.md，逐条已复核为真）。

每条都先在这里钉"真实后果"，再改产品代码——修完这些钉必须全绿，且反向臂
（合法输入照旧、网关过滤照旧）必须保留，防"修一条拆一条"。
"""
import asyncio
import json
from types import SimpleNamespace

import homeassistant.components.mqtt as fake_mqtt
import custom_components.window_controller_gateway.mqtt_bootstrap  # noqa: F401  (conftest 树装载)
import custom_components.window_controller_gateway.persist as persist_mod
import custom_components.window_controller_gateway.ws_gateway as ws_mod
import custom_components.window_controller_gateway.hub_client as hc
import custom_components.window_controller_gateway.config_flow as cf_mod
from custom_components.window_controller_gateway.const import (
    DOMAIN, DEVICE_TO_GATEWAY_MAPPING, DEVICE_SETPOINTS, CONF_GATEWAY_SN,
    CONF_WS_GATEWAY_ENABLED, CONF_WS_GATEWAY_PORT, CONF_WS_GATEWAY_TOKEN,
    DEFAULT_WS_GATEWAY_PORT, DEFAULT_WS_GATEWAY_TOKEN,
)
from custom_components.window_controller_gateway.mqtt_handler import (
    WindowControllerMQTTHandler)

import pytest


# ============ 1. persist：根类型 / schema_version 类型不再炸穿 setup ============

def _ph(tmp_path):
    async def _exec(fn, *a):
        return fn(*a)
    return SimpleNamespace(
        data={DOMAIN: {}},   # 生产由 async_setup 先 setdefault(DOMAIN, {}) 建好
        config=SimpleNamespace(config_dir=str(tmp_path)),
        async_add_executor_job=_exec,
    )


def test_persist_non_dict_root_degrades_or_rescues(tmp_path):
    """根是合法 JSON 非对象（[1,2,3]）时旧实现 :71 `data.get` AttributeError
    逃逸到 async_setup ⇒ **整个集成 setup 失败**。修后：按"损坏"处理——
    先试 .bak 救援，救不到就带告警退化（绝不抛）。"""
    (tmp_path / persist_mod.PERSISTENT_DATA_FILE).write_text("[1,2,3]", encoding="utf-8")
    hass = _ph(tmp_path)
    asyncio.run(persist_mod.load_persistent_data(hass))     # 不得抛
    assert DEVICE_SETPOINTS in hass.data[DOMAIN], "损坏根必须走退化路径（不是静默死）"
    assert DEVICE_TO_GATEWAY_MAPPING not in hass.data[DOMAIN]


def test_persist_non_dict_root_rescued_by_bak(tmp_path):
    """根损坏 + 合法 .bak ⇒ 备份救援路径仍要生效（不是直接放弃）。"""
    (tmp_path / persist_mod.PERSISTENT_DATA_FILE).write_text('"x"', encoding="utf-8")
    (tmp_path / (persist_mod.PERSISTENT_DATA_FILE + ".bak")).write_text(
        json.dumps({"device_to_gateway_mapping": {"500534380259": "10012250123f"}}),
        encoding="utf-8")
    hass = _ph(tmp_path)
    asyncio.run(persist_mod.load_persistent_data(hass))
    assert hass.data[DOMAIN][DEVICE_TO_GATEWAY_MAPPING] == {"500534380259": "10012250123f"}


@pytest.mark.parametrize("bad_version", ["2", None, 1.5, True])
def test_persist_bad_schema_version_does_not_crash(tmp_path, bad_version):
    """schema_version 为字符串/null/浮点/bool 时旧实现 `version > 1` TypeError
    逃逸。修后：按 0 处理并告警，其余字段照常加载。"""
    (tmp_path / persist_mod.PERSISTENT_DATA_FILE).write_text(
        json.dumps({"schema_version": bad_version,
                    "device_to_gateway_mapping": {"aaa": "bbb"}}),
        encoding="utf-8")
    hass = _ph(tmp_path)
    asyncio.run(persist_mod.load_persistent_data(hass))     # 不得抛
    assert hass.data[DOMAIN][DEVICE_TO_GATEWAY_MAPPING] == {"aaa": "bbb"}, \
        "只有版本位非法，其余字段必须照常加载"


def test_persist_valid_file_still_loads(tmp_path):
    """反向臂：合法文件行为不变（防"一律退化"式假修）。"""
    (tmp_path / persist_mod.PERSISTENT_DATA_FILE).write_text(
        json.dumps({"schema_version": 1,
                    "device_to_gateway_mapping": {"s1": "gw1"},
                    "manually_removed_devices": ["s2"]}),
        encoding="utf-8")
    hass = _ph(tmp_path)
    asyncio.run(persist_mod.load_persistent_data(hass))
    assert hass.data[DOMAIN][DEVICE_TO_GATEWAY_MAPPING] == {"s1": "gw1"}


# ============ 2. ws_gateway_wanted：端口非有限浮点不再炸 ============

def _ws_hass(options, disabled_by=None):
    entry = SimpleNamespace(options=options, disabled_by=disabled_by)
    return SimpleNamespace(config_entries=SimpleNamespace(
        async_entries=lambda dom: [entry]))


@pytest.mark.parametrize("bad_port", [float("inf"), float("-inf"), float("nan")])
def test_ws_wanted_nonfinite_port_falls_back(bad_port):
    """int(float('inf')) 抛 OverflowError，不在 (ValueError, TypeError) 捕获面
    ⇒ 异常逃出 ws_gateway_wanted，四个调用点只记 error ⇒ **9001 永不监听、
    改端口/令牌也不重聚合**。修后：回退默认端口。"""
    hass = _ws_hass({CONF_WS_GATEWAY_ENABLED: True, CONF_WS_GATEWAY_PORT: bad_port})
    assert ws_mod.ws_gateway_wanted(hass) == (
        DEFAULT_WS_GATEWAY_PORT, DEFAULT_WS_GATEWAY_TOKEN)


def test_ws_wanted_normal_and_disabled_paths_unchanged():
    """反向臂：正常端口照旧采用；显式关闭 ⇒ None（既有语义保留）。"""
    hass = _ws_hass({CONF_WS_GATEWAY_ENABLED: True, CONF_WS_GATEWAY_PORT: 9005})
    assert ws_mod.ws_gateway_wanted(hass)[0] == 9005
    off = _ws_hass({CONF_WS_GATEWAY_ENABLED: False, CONF_WS_GATEWAY_PORT: 9005})
    assert ws_mod.ws_gateway_wanted(off) is None


# ============ 3. hub_client：身份落盘成功必须清 OP_IDENTITY ============

def test_identity_persist_success_clears_op_error(tmp_path):
    """:727 的清除点只在注册路径可达，而 :686 内存有身份即早返回 ⇒ 首轮落盘
    失败后**任何后续成功落盘都不再清**（refresh_bind_code 换码成功落盘也只清
    OP_BINDCODE）⇒ 面板永久显示"没保存云端身份"。修后：_save_identity 成功即清。"""
    c = hc.HubClient([], config_dir=str(tmp_path))
    c.instance_id, c._secret, c.bind_code = "i1", "s1", "BIND"
    c._bind_code_at = 1.0
    c._set_op_error(hc.OP_IDENTITY, "identity_persist_failed")
    asyncio.run(c._save_identity())
    assert c.last_op_error is None, "落盘成功 ⇒ 告警条件已消失，必须清"


def test_identity_persist_failure_keeps_op_error(tmp_path, monkeypatch):
    """反向臂：落盘仍失败时错误必须留着（不许"无条件清"式假修）。"""
    c = hc.HubClient([], config_dir=str(tmp_path))
    c.instance_id, c._secret, c.bind_code = "i1", "s1", "BIND"
    c._bind_code_at = 1.0
    c._set_op_error(hc.OP_IDENTITY, "identity_persist_failed")

    def _boom(config_dir, data):
        raise OSError("disk full")

    monkeypatch.setattr(hc, "save_identity", _boom)
    try:
        asyncio.run(c._save_identity())
    except OSError:
        pass
    assert c.last_op_error == "identity_persist_failed", \
        "落盘失败时错误必须保留（由调用方设置；此处验证不被误清）"


# ============ 4. 002：model/vesion 为 null 不再整条吞掉设备 ============

class _DM:
    def __init__(self):
        self.devices = {}
        self.entry = SimpleNamespace(options={})
        self._manually_removed_devices = set()
        self.added = []
        self.updated = []

    async def update_gateway_status(self, s):
        pass

    def get_device(self, sn):
        return self.devices.get(sn)

    def allocate_device_number(self):
        return 1

    async def add_device(self, sn, name, dtype, is_manual_pairing=False):
        self.added.append(sn)
        self.devices[sn] = {"sn": sn}

    async def update_device_status(self, sn, status, attrs=None):
        self.updated.append(sn)

    def is_device_manually_removed(self, sn):
        return False

    def _notify_status_listeners(self, sn):
        pass


def _hass():
    async def _pub(hass, topic, payload, qos=0, retain=False):
        pass
    fake_mqtt.async_publish = _pub
    hass = SimpleNamespace(
        data={DOMAIN: {}},
        config=SimpleNamespace(config_dir="/config"),
        loop=None,
        async_create_task=lambda coro, name=None, **kw: asyncio.ensure_future(coro),
    )
    return hass


def _run_002(device_info):
    hass = _hass()
    dm = _DM()

    async def go():
        hass.loop = asyncio.get_running_loop()
        h = WindowControllerMQTTHandler(hass, "10012250123f", dm)
        data = {"devices": [device_info]}
        await h._handle_ctype_002(
            {"head": "$SH", "ctype": "002", "id": 172, "sn": "10012250123f",
             "data": data}, "002", data)
        await asyncio.sleep(0.05)

    asyncio.run(go())
    return dm


def test_002_null_model_does_not_swallow_device():
    """`device_info.get("model","")` 在键存在值为 null 时返回 None ⇒
    None.lower() AttributeError 被逐条 except 吞掉 ⇒ 该设备本帧既不更新也不
    入库（已存在的设备 r_travel/电量随之冻结）。修后：与 device_sn 同型守卫。"""
    dm = _run_002({"sn": "50063420020A", "r_travel": 0, "battery": 108,
                   "model": None, "vesion": None})
    assert dm.added == ["50063420020A"], "model/vesion 为 null 不得吞掉设备"


def test_002_gateway_filter_still_works():
    """反向臂：model/vesion 含 gateway 的网关类条目照旧跳过（过滤不能被拆）。"""
    dm = _run_002({"sn": "50063420020B", "r_travel": 0, "model": "YGZN_GATEWAY"})
    assert dm.added == []
    dm2 = _run_002({"sn": "50063420020C", "r_travel": 0, "vesion": "x-gateway-y"})
    assert dm2.added == []


def test_002_normal_device_unchanged():
    """反向臂：正常设备路径不变。"""
    dm = _run_002({"sn": "50013214041E", "r_travel": 99, "battery": 97,
                   "model": "YGZN", "vesion": "V1"})
    assert dm.added == ["50013214041E"]


# ============ 5. OptionsFlow：保存不再清空带外配置 ============

def _capture_flow(entry):
    captured = {}

    class _Flow(cf_mod.OptionsFlow):
        def __init__(self):
            super().__init__(entry)
            self.hass = SimpleNamespace()

        def async_create_entry(self, title="", data=None):
            captured.update(data or {})
            return {"type": "create_entry"}

        def async_show_form(self, **kw):
            return {"type": "form", **kw}

    return _Flow(), captured


def test_options_save_preserves_out_of_band_keys():
    """OptionsFlow 整表覆盖 options，而表单不含 hub_base/hub_install_key
    （_hub_option 会读）⇒ 用户进选项页保存一次，自建 hub 端点/安装密钥被
    **静默清空**。修后：未知键原样保留。"""
    entry = SimpleNamespace(
        data={CONF_GATEWAY_SN: "10012250123f"},
        options={"hub_base": "https://hub.example.com",
                 "hub_install_key": "IK-123",
                 "debug_logging": False},
    )
    flow, captured = _capture_flow(entry)
    asyncio.run(flow.async_step_options(user_input={
        "discovery_interval": 300,
        "auto_discovery": True,
        "debug_logging": True,
        CONF_WS_GATEWAY_ENABLED: True,
        CONF_WS_GATEWAY_PORT: 9001,
        CONF_WS_GATEWAY_TOKEN: "abcdefgh12345678",
    }))
    assert captured.get("debug_logging") is True, "表单字段必须生效"
    assert captured.get("hub_base") == "https://hub.example.com", \
        "带外键 hub_base 不得被保存动作清空"
    assert captured.get("hub_install_key") == "IK-123"


def test_options_form_values_win_over_stale_options():
    """反向臂：表单字段的值必须覆盖同名旧值（不是简单 union 反了）。"""
    entry = SimpleNamespace(
        data={CONF_GATEWAY_SN: "10012250123f"},
        options={"hub_base": "https://x", "debug_logging": True},
    )
    flow, captured = _capture_flow(entry)
    asyncio.run(flow.async_step_options(user_input={
        "discovery_interval": 120,
        "auto_discovery": False,
        "debug_logging": False,
        CONF_WS_GATEWAY_ENABLED: False,
        CONF_WS_GATEWAY_PORT: 9002,
        CONF_WS_GATEWAY_TOKEN: "abcdefgh12345678",
    }))
    assert captured.get("debug_logging") is False, "表单新值必须覆盖旧值"
    assert captured.get("auto_discovery") is False
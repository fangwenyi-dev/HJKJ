# -*- coding: utf-8 -*-
"""v1.7.47 钉桩：速度/力度/锁定模式必须**回传**给小程序（此前只能发不能收）。

真机症状来源：小程序「调速度」「调力度」滑块的初值只来自本地存储（默认 60 / 50），
与设备实际值无关——因为加载项的两条状态通道都只回 `position/battery/state`：
  · LAN `device_list` 用 `device_ws_view`（5 字段）
  · LAN `device_update` 推送（7 键，含 windLockMode）
  · 云通道 `build_state_snapshot` = `device_ws_view` + 单独补一个 windLockMode
而设备其实**上报了** `rwp_winact_speed` / `rwp_winact_strength`，`_ctypes.py:501-513`
也把它们解析进了 `attributes["winact_speed"/"winact_strength"]`，HA 侧 number 实体
正是靠 `_state_key` 读它显示真值。所以换手机、清缓存、或在 HA 里调过速度之后，
小程序显示的就与设备实际值对不上——用户看到的就是"调了没反应/数值不对"。

修法是**加载项补回传**（真相源在这边），小程序按可选字段读（它也可能直连 ESP32
固件的 9001，固件不发这两个键 ⇒ 缺失必须回退本地值，不能崩也不能显示 NaN）。
入界纪律与 position 同款：越界/不可解析一律 -1（未知），绝不把垃圾值当合法数字发出去。
"""
import re
from pathlib import Path

from custom_components.window_controller_gateway import hub_client as hc
from custom_components.window_controller_gateway.ws_gateway import device_ws_view

ROOT = Path(__file__).resolve().parents[1]
WS_SRC = (ROOT / "custom_components" / "window_controller_gateway" / "ws_gateway.py").read_text(encoding="utf-8")


def _view(attrs):
    return device_ws_view("DEV1", "GW1", {"attributes": attrs})


# ── 1. 视图字段与入界纪律 ────────────────────────────────────────────
def test_view_carries_speed_strength_and_lock_mode():
    v = _view({"r_travel": 30, "voltage": 10.5, "wind_lock_mode": "1",
               "winact_speed": 42, "winact_strength": 77})
    assert v["winactSpeed"] == 42, v
    assert v["winactStrength"] == 77, v
    assert v["windLockMode"] == 1, "LAN device_list 此前不带锁定模式（云端与推送都带）"


def test_view_boundaries_are_inclusive():
    assert _view({"winact_speed": 0, "winact_strength": 0})["winactSpeed"] == 0, "0 是合法值（最弱档），不得当未知"
    assert _view({"winact_speed": 100, "winact_strength": 100})["winactStrength"] == 100
    assert _view({"winact_speed": "55"})["winactSpeed"] == 55, "字符串数字要能解析（上报值常是字符串）"


def test_view_out_of_range_and_garbage_become_minus_one():
    for bad in (101, -5, 255, "abc", None, float("inf"), float("nan")):
        v = _view({"winact_speed": bad, "winact_strength": bad})
        assert v["winactSpeed"] == -1, "速度 %r 应判未知，实得 %s" % (bad, v["winactSpeed"])
        assert v["winactStrength"] == -1, "力度 %r 应判未知，实得 %s" % (bad, v["winactStrength"])


def test_view_missing_attributes_are_minus_one_not_zero():
    """缺字段必须是 -1（未知），不能是 0——0 会被小程序当"最弱档"显示成真值。"""
    v = _view({})
    assert v["winactSpeed"] == -1 and v["winactStrength"] == -1 and v["windLockMode"] == -1, v


# ── 2. 两条通道同源（列表 / 推送 / 云）──────────────────────────────
def test_device_update_payload_reads_from_the_view_not_its_own_math():
    """结构钉：推送必须取 `view[...]`，不得自己再算一遍——两处算法迟早会漂
    （windLockMode 就是这么漂的：推送里单独算了一次，列表里干脆没有）。"""
    body = WS_SRC.split("def _device_update_payload", 1)[1].split("\n    async def ", 1)[0]
    for key in ("winactSpeed", "winactStrength", "windLockMode"):
        assert 'view["%s"]' % key in body, "device_update 的 %s 没有走 view（两处算法会漂）" % key
    assert "_as_int(" not in body, "推送里不该再自己算属性（一律走 device_ws_view）"


def test_cloud_state_items_carry_speed_and_strength(tmp_path):
    """云通道用的是同一个 device_ws_view（_resolve_builder 的缺省），
    所以补在视图里就两条通道一起有了——这条钉住"云端也真的带上了"。"""

    class _Manager:
        gateway_sn = "GW1"
        devices = {"DEV1": {"attributes": {"r_travel": 10, "voltage": 12.0,
                                           "winact_speed": 33, "winact_strength": 66,
                                           "wind_lock_mode": 1}}}

        def add_status_listener(self, _cb):
            return None

    client = hc.HubClient([_Manager()], config_dir=str(tmp_path), session=object())
    items, _auth = client.build_state_snapshot()
    assert len(items) == 1, items
    it = items[0]
    assert it["winactSpeed"] == 33 and it["winactStrength"] == 66, it
    assert it["windLockMode"] == 1, it
    assert it["gwSn"] == "GW1" and it["sn"] == "DEV1", it


def test_lock_mode_out_of_domain_becomes_unknown_on_both_channels(tmp_path):
    """v1.8.6（审计 G-9）：windLockMode 是这一族里**唯一没做入界闸**的字段。

    `ws_gateway.py:296` 的 docstring 立了"入界纪律与 position 同款"，speed/strength
    有 `0<=x<=100`、position 钳 [0,100]、battery 判 [80,140]，而 windLockMode 走
    `_as_int()` 裸透传（写入点 `_ctypes` 也原样存 value）⇒ 异常上报 2/999 会以
    "合法数字"形态经 device_list / device_update / 云快照三条通道一起分发出去。
    域表外一律 -1（本仓既有"未知"哨兵，小程序 getModeText 对非 0/1 显示 '--'）。
    """
    for junk in (2, 999, "abc", {"a": 1}, True, None):
        assert _view({"wind_lock_mode": junk})["windLockMode"] == -1, junk

    class _Manager:
        gateway_sn = "GW1"
        devices = {"DEV1": {"attributes": {"r_travel": 10, "voltage": 12.0,
                                           "wind_lock_mode": 999}}}

        def add_status_listener(self, _cb):
            return None

    client = hc.HubClient([_Manager()], config_dir=str(tmp_path), session=object())
    items, _auth = client.build_state_snapshot()
    assert items[0]["windLockMode"] == -1, \
        "云通道没走同一份域判：%r" % (items[0],)


def test_lock_mode_domain_is_single_sourced():
    """同源臂：判据必须读 CONTROL_ATTR_DOMAINS，不许在两处各写一份 0/1。

    把域表临时改成 (0,2) 后 2 必须变成合法值——若哪一侧是硬编码 0/1，它不会跟着变。
    全局表改完必须还原（本仓夹具纪律：fixture 改全局态要复原，否则污染后序用例）。
    """
    from custom_components.window_controller_gateway import const

    key = const.ATTRIBUTE_WIND_LOCK_MODE
    original = const.CONTROL_ATTR_DOMAINS[key]
    try:
        const.CONTROL_ATTR_DOMAINS[key] = (0, 2, ())
        assert _view({"wind_lock_mode": 2})["windLockMode"] == 2, "视图侧域判写死了"
        assert hc._wind_lock_mode({"attributes": {"wind_lock_mode": 2}}) == 2, "云侧域判写死了"
    finally:
        const.CONTROL_ATTR_DOMAINS[key] = original
    assert _view({"wind_lock_mode": 2})["windLockMode"] == -1, "还原后必须回到未知"


def test_field_names_are_camel_case_like_the_rest_of_the_contract():
    """命名口径钉：视图字段一律 camelCase（gwSn/windLockMode 同款）。
    写成 winact_speed 小程序读不到，而"读不到"是静默的——只会显示成默认值。"""
    v = _view({"winact_speed": 1, "winact_strength": 2, "wind_lock_mode": 0})
    for key in ("winactSpeed", "winactStrength", "windLockMode", "gwSn"):
        assert key in v, "缺 camelCase 字段 %s" % key
    for snake in ("winact_speed", "winact_strength", "wind_lock_mode"):
        assert snake not in v, "视图里不该出现下划线键 %s（小程序按 camelCase 取）" % snake


def test_regex_nail_view_returns_exactly_the_contract_keys():
    """反钉：视图字段集是**跨仓契约**，多一个少一个都要有人发现。
    新增字段时必须同步改小程序（normalizeStates / _handleDeviceList / _handleDeviceUpdate）
    与 tests/e2e/cross_repo_contract.sh 的对账行。"""
    m = re.search(r"def device_ws_view.*?\n    return \{(.*?)\n    \}", WS_SRC, re.S)
    assert m, "解析锚点失效：没抽到 device_ws_view 的 return"
    keys = set(re.findall(r'"(\w+)":', m.group(1)))
    assert keys == {"sn", "gwSn", "position", "battery", "state",
                    "windLockMode", "winactSpeed", "winactStrength"}, \
        "视图字段集变了（跨仓契约）：%s" % sorted(keys)

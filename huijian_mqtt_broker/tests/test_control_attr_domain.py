"""v1.7.65 控制属性域表钉桩：004 的 attribute 全集 + 各属性值域，两条通道同一实现。

缺陷本体（三仓联审查出，判据逐层来自本四仓源码不是推测）：
  · 云侧 `hub_client.validate_control_params` 与 LAN 侧 `ws_gateway._cmd_control`
    对 attribute 的**全部**判据只有一句"非空字符串"（v1.7.52 只补了 value 侧格式）；
  · 而 `mqtt_handler/_commands.py` 的 `send_ws_raw_004` docstring 自陈"不做语义解释…
    本方法同样不校验"，control_ack 的 ok 又是**发布级**语义（发布到 broker 就算成功）；
  ⇒ 属性名打错一个字母（rwp_winact_sped）照样发布并回 **ok:true 假成功**，
    界面显示"速度已设置 80%"而设备从未收到过这条命令；
  · 值域此前**只有小程序一侧在夹**（pctParam 0..100），直连 WS 的调用方送 speed=150
    会一路发布出去。

本文件的判据纪律：
  ①合法集**由域表自己生成**回灌断言（不手写小清单——手写清单只证明我自己）；
  ②两条通道的等价性用**行为矩阵**验，不再比注释/模式串（旧钉"两个 _VALUE_RE 逐字
    同串"正是漏检的那一类：串同了，判据面却各自少东西）；
  ③失败文案必须逐字沿用既有的 'invalid value' / invalid_params——文案表在正要上线的
    小程序里，换码等于让它退回裸兜底。
"""
import math

import pytest

from custom_components.window_controller_gateway import ws_gateway as wg
from custom_components.window_controller_gateway.const import (
    ATTRIBUTE_W_TRAVEL,
    ATTRIBUTE_WIND_LOCK_MODE,
    ATTRIBUTE_WINACT_SPEED,
    ATTRIBUTE_WINACT_STRENGTH,
    COMMAND_VALUE_CLOSE,
    COMMAND_VALUE_OPEN,
    COMMAND_VALUE_STOP,
    COMMAND_VALUE_TOGGLE,
    COMMAND_VALUE_WIND_LOCK_FLAT,
    COMMAND_VALUE_WIND_LOCK_TILT,
    CONTROL_ATTR_DOMAINS,
    SPEED_MAX,
    SPEED_MIN,
)
from custom_components.window_controller_gateway.hub_client import (
    validate_control_params,
)

ALL_ATTRS = (ATTRIBUTE_W_TRAVEL, ATTRIBUTE_WIND_LOCK_MODE,
             ATTRIBUTE_WINACT_SPEED, ATTRIBUTE_WINACT_STRENGTH)


def _srv(published=None):
    """最小可用的 WS 服务器实例（照 test_v1718_audit 同款 __new__ 绕开 aiohttp 装配）。"""
    pub = published if published is not None else []

    class _H:
        async def send_ws_raw_004(self, dev, attr, val):
            pub.append((dev, attr, val))
            return True

    s = wg.WsGatewayServer.__new__(wg.WsGatewayServer)
    s._device_gateway = lambda sn: {"mqtt_handler": _H()}
    s._entries_data = lambda: [("G", {"mqtt_handler": _H()})]
    return s, pub


# ── ① 域表自身的完整性 ──────────────────────────────────────────────
def test_domain_table_keys_are_exactly_the_four_wire_attributes():
    """域表键 == const 里的 ATTRIBUTE_* 全集。

    元钉方向：加载项将来新增 ATTRIBUTE_* 而忘记进域表时，本条**必须红**——
    否则那条新命令会在两条通道被静默拒发（新属性发不出去，两侧都不报错）。
    """
    import custom_components.window_controller_gateway.const as const_mod
    wire_attrs = {v for k, v in vars(const_mod).items()
                  if k.startswith("ATTRIBUTE_") and isinstance(v, str)}
    assert wire_attrs, "const 里没解析到任何 ATTRIBUTE_*＝扫描面异常（本条会假绿）"
    assert set(CONTROL_ATTR_DOMAINS) == wire_attrs, (
        "域表与属性全集不一致：只在const=%s 只在域表=%s"
        % (sorted(wire_attrs - set(CONTROL_ATTR_DOMAINS)),
           sorted(set(CONTROL_ATTR_DOMAINS) - wire_attrs)))


def test_domain_shape_is_lo_hi_extra_and_numeric():
    for attr, spec in CONTROL_ATTR_DOMAINS.items():
        lo, hi, extra = spec
        assert isinstance(lo, int) and isinstance(hi, int) and lo <= hi, attr
        assert isinstance(extra, tuple) and all(isinstance(x, int) for x in extra), attr


# ── ② 合法集全量放行（域表自己生成，不手写清单）──────────────────────
@pytest.mark.parametrize("attr", ALL_ATTRS)
def test_every_in_domain_value_is_accepted_verbatim(attr):
    lo, hi, extra = CONTROL_ATTR_DOMAINS[attr]
    good = list(range(lo, hi + 1)) + list(extra)
    accepted = [v for v in good if wg.validate_control_command(attr, v) is not None]
    assert accepted == good, "%s 域内值被误拒，漏: %s" % (attr, sorted(set(good) - set(accepted)))
    # 小程序实际下发的是 String(int) 形态 ⇒ 同值必须以**同一字符串**放行（不许归一化）
    for v in (0, 1, 50, hi, *extra):
        if lo <= v <= hi or v in extra:
            assert wg.validate_control_command(attr, str(v)) == str(v), (attr, v)


def test_the_exact_wire_values_the_mini_program_sends_all_pass():
    """小程序真会发的每一条线值必须放行（w_travel 的 0/100/101/200、模式 0/1、速度力度 0..100）。

    值取自 const 的 COMMAND_VALUE_*，不是测试里手抄的字面量。
    """
    for val in (COMMAND_VALUE_OPEN, COMMAND_VALUE_CLOSE, COMMAND_VALUE_STOP, COMMAND_VALUE_TOGGLE):
        assert wg.validate_control_command(ATTRIBUTE_W_TRAVEL, val) == val
    for val in (COMMAND_VALUE_WIND_LOCK_TILT, COMMAND_VALUE_WIND_LOCK_FLAT):
        assert wg.validate_control_command(ATTRIBUTE_WIND_LOCK_MODE, val) == val
    for val in (SPEED_MIN, SPEED_MAX, 60, 50):
        assert wg.validate_control_command(ATTRIBUTE_WINACT_SPEED, val) == str(val)
        assert wg.validate_control_command(ATTRIBUTE_WINACT_STRENGTH, val) == str(val)


# ── ③ 缺陷本体：未知属性不得再拿 ok:true ─────────────────────────────
UNKNOWN_ATTRS = ["rwp_winact_sped", "position", "state", "r_travel", "voltage",
                 "heartbeat_time", "w_travel_", "", "  ", None, 7, {"a": 1}]


@pytest.mark.parametrize("attr", UNKNOWN_ATTRS)
def test_unknown_attribute_rejected_by_cloud_validator(attr):
    assert validate_control_params(attr, "50") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("attr", UNKNOWN_ATTRS)
async def test_unknown_attribute_rejected_by_lan_and_publishes_nothing(attr):
    published = []
    s, pub = _srv(published)
    out = await s._cmd_control({"gwSn": "G", "devSn": "D", "attribute": attr, "value": "50"})
    # 文案分两种，且必须按 LAN 既有那道闸分：属性**不是非空字符串**（缺失/空/非 str）
    # 走既有的 'missing fields'（不改，小程序文案表里有它）；是非空字符串但不在域表内
    # 才走 'invalid value'。"  " 属后者——它是个非空字符串，不是缺失。
    is_missing = not (isinstance(attr, str) and attr)
    want = "missing fields" if is_missing else "invalid value"
    assert out["ok"] is False and out["msg"] == want, (attr, out)
    assert pub == [], "未知属性不得发布 004（发了就拿不到回执也回 ok:true）"


def test_lan_ack_vocabulary_unchanged_for_the_shipping_mini_program():
    """失败码必须逐字沿用既有两个字面值：换码会让正要上线的小程序退回裸兜底文案。"""
    import asyncio
    s, _ = _srv()
    out = asyncio.run(s._cmd_control({"gwSn": "G", "devSn": "D",
                                      "attribute": "position", "value": "50"}))
    assert out["msg"] == "invalid value", out
    assert validate_control_params("position", "50") is None
    # 关联字段照旧回带（页面靠它配对在途命令）
    out = asyncio.run(s._cmd_control({"gwSn": "G", "devSn": "D", "attribute": "position",
                                      "value": "50", "cmdsn": "c1"}))
    assert out.get("attribute") == "position" and out.get("cmdsn") == "c1", out


# ── ④ 越界与脏值 ─────────────────────────────────────────────────────
OUT_OF_RANGE = [
    (ATTRIBUTE_W_TRAVEL, -1), (ATTRIBUTE_W_TRAVEL, 102), (ATTRIBUTE_W_TRAVEL, 150),
    (ATTRIBUTE_W_TRAVEL, 201), (ATTRIBUTE_W_TRAVEL, 999),
    (ATTRIBUTE_WIND_LOCK_MODE, 2), (ATTRIBUTE_WIND_LOCK_MODE, -1),
    (ATTRIBUTE_WINACT_SPEED, 101), (ATTRIBUTE_WINACT_SPEED, -1),
    (ATTRIBUTE_WINACT_STRENGTH, 150), (ATTRIBUTE_WINACT_STRENGTH, -5),
]


@pytest.mark.parametrize("attr,value", OUT_OF_RANGE)
def test_out_of_range_rejected_both_channels(attr, value):
    assert wg.validate_control_command(attr, value) is None, (attr, value)
    assert validate_control_params(attr, value) is None, (attr, value)


@pytest.mark.parametrize("attr,value", OUT_OF_RANGE)
@pytest.mark.asyncio
async def test_out_of_range_rejected_by_lan_and_publishes_nothing(attr, value):
    s, pub = _srv()
    out = await s._cmd_control({"gwSn": "G", "devSn": "D", "attribute": attr, "value": value})
    assert out["ok"] is False and out["msg"] == "invalid value", (attr, value, out)
    assert pub == []


@pytest.mark.parametrize("bad", ["50.5", 50.5, "1e2", math.inf, math.nan, "NaN",
                                 "", None, True, False, [1], {"a": 1}, "abc"])
def test_non_integer_and_dirty_values_rejected(bad):
    """小数也拒：四个出站属性值域全是整数（小数只出现在上报侧，如 r_travel 行程分数）。"""
    assert wg.validate_control_command(ATTRIBUTE_WINACT_SPEED, bad) is None


# ── ⑤ 两条通道行为等价（矩阵；旧"模式串逐字同串"钉的替代）─────────────
_MATRIX = (
    [(a, v) for a in ALL_ATTRS for v in
     (-1, 0, 1, 50, 100, 101, 150, 200, 201, "50", "0100", "50.5", "NaN", math.inf, True)]
    + [(u, 50) for u in UNKNOWN_ATTRS if isinstance(u, str)]
)


@pytest.mark.parametrize("attr,value", _MATRIX)
@pytest.mark.asyncio
async def test_two_entry_points_return_identical_verdict(attr, value):
    """同一个 (attribute, value)：**两个真实入口**必须同结论——LAN `_cmd_control`
    与云侧 `validate_control_params`（hub_client._handle_cmd 就是拿它当闸，:1247）。

    故意不写成"委托函数 == 被委托函数"：那种比法恒真（同一实现跟自己比），
    只能证明 Python 没坏。这里比的是两条通道各自的**入口链**，将来任何一侧
    重新加回自己的格式闸或绕过共享判据，本条就会红。
    只判"接受/拒绝"这一件事——失败文案两侧本就不同族（LAN 'missing fields' vs 云
    invalid_params），强求同文案等于要求小程序两张码表合并成一张。
    """
    s, pub = _srv()
    out = await s._cmd_control({"gwSn": "G", "devSn": "D", "attribute": attr, "value": value})
    cloud = validate_control_params(attr, value)
    assert bool(out["ok"]) is (cloud is not None), (attr, value, out, cloud)
    if cloud is not None:
        assert pub == [("D", attr, cloud)], (attr, value, pub)


# ── ⑥ 本批两条**故意**改判，写清楚免得下版当成回归 ─────────────────────
def test_minus_one_is_a_report_sentinel_not_a_command_value():
    """-1 在 v1.7.65 之前是"格式合法"因而被放行；现在按值域拒。

    依据：`device_ws_view`/`_ctypes` 用 -1 表示"这项从没收到过上报"，它是**视图层哨兵**，
    不是任何下行命令的取值（w_travel 0..100/101/200、mode 0/1、speed 与 strength 0..100）。
    把它原样透传给设备只会得到"窗不动、回执 ok:true"。旧钉（GOOD_WIRE、v1718 的合法串清单）
    里删掉它就是这条判据，不是漏改。
    """
    for attr in ALL_ATTRS:
        assert wg.validate_control_command(attr, -1) is None, attr
        assert wg.validate_control_command(attr, "-1") is None, attr
        assert validate_control_params(attr, "-1") is None, attr


def test_fractional_command_values_are_now_rejected():
    """小数（"50.5"）此前过格式闸、现在按"四个属性值域都是整数"拒。

    上报侧才有小数（r_travel 行程分数、battery ÷10），下行四属性没有半档语义；
    放行只会让设备收到一个它自己还要再解释一次的字符串。
    """
    for attr in ALL_ATTRS:
        assert wg.validate_control_command(attr, "50.5") is None, attr
        assert validate_control_params(attr, 50.5) is None, attr
    # 但整数值的 float 形态仍按 str() 后判定（100.0 → "100.0" 仍不是整数形态 ⇒ 拒），
    # 与"不改写、不猜"的既有口径一致：宁可拒一个奇怪的入参，不静默改它的线值。
    assert wg.validate_control_command(ATTRIBUTE_WINACT_SPEED, 100.0) is None

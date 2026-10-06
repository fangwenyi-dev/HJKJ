# -*- coding: utf-8 -*-
"""v1.7.52 两条新跨仓契约：control_ack 的**关联字段** + 两条通道的**线值格式闸同源**。

为什么单开一个文件：这两件事都是"三端各写一份、编译期与既有单测都发现不了漂移"的那类。
改之前的状态是——LAN 回执**根本没有**关联字段（页面只能退化成"认最早那条在途"的弱
FIFO，「打开」的回执会被当成速度命令的回执）；而两条通道对同一个 value 的合法性判据
各写一份**且都豁免 str**，`'NaN'` 正好从豁免缝里穿到物理执行器并拿到 ok:true 假成功。

本文件全部走**真行为**（直接驱动 `_cmd_control` / `validate_control_params`），
不用 grep 字符串——grep 型钉会被注释满足（v1.7.51 刚为这个栽过一次）。
"""
import re
from pathlib import Path

import pytest

from custom_components.window_controller_gateway import const
from custom_components.window_controller_gateway import hub_client as hc
from custom_components.window_controller_gateway import ws_gateway as wg

GW_ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "window_controller_gateway"
_CTYPES = GW_ROOT / "mqtt_handler" / "_ctypes.py"

# 004 协议已知的全部属性名（入站由 _ctypes 解析、出站由小程序/云下发）
KNOWN_INBOUND_ATTRS = {"voltage", "r_travel", "rwp_wind_lock_mode",
                       "rwp_winact_speed", "rwp_winact_strength"}

# 取消 str 豁免的依据：本协议**不存在**字符串线值。合法值取自 const 与
# send_ws_raw_004 的 docstring（"w_travel 的 100/0/101/200/0-100、rwp_wind_lock_mode 0/1"）
# v1.7.65 起这批值还要过 CONTROL_ATTR_DOMAINS 的值域闸；"-1" 已从本清单移出——
# 它是视图层的"从未收到上报"哨兵，不是任何下行命令的取值（改判理由见
# tests/test_control_attr_domain.py::test_minus_one_is_a_report_sentinel_not_a_command_value）。
GOOD_WIRE = ["0", "1", "50", "100", "101", "200", "3", "99"]
BAD_WIRE = ["NaN", "nan", "inf", "Infinity", "1e999", "0x10", "open", " 12", "12 ",
            "100;reboot", "1.2.3", "+5", "5f", ""]


class _Pub:
    """假 mqtt handler：记录每次 004 发布，可配置发布成功/失败。"""

    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    async def send_ws_raw_004(self, dev_sn, attribute, value):
        self.calls.append((dev_sn, attribute, value))
        return self.ok


def _srv(pub=None):
    """最小可用的 WsGatewayServer：_device_gateway 返回 None ⇒ 走广播分支，
    回执形状与定向分支同形（两个分支都用同一个 ack() 出口）。"""
    s = wg.WsGatewayServer.__new__(wg.WsGatewayServer)
    s._device_gateway = lambda sn: None
    p = pub if pub is not None else _Pub()
    s._entries_data = lambda: [("GW1", {"mqtt_handler": p})]
    return s, p


# ── 一、关联字段回带 ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_cmdsn_and_attribute_echoed_verbatim_in_ack():
    s, _ = _srv()
    out = await s._cmd_control({"gwSn": "GW1", "devSn": "5005A", "attribute": "w_travel",
                                "value": "100", "cmdsn": "lmc123-456"})
    assert out["ok"] is True, out
    assert out["cmdsn"] == "lmc123-456", "cmdsn 必须**原样**回带（不改写、不补零）: %r" % (out,)
    assert out["attribute"] == "w_travel", out


@pytest.mark.asyncio
async def test_ack_shape_for_client_that_sends_no_cmdsn():
    """向后兼容：老小程序不发 cmdsn ⇒ 回执里不该凭空多出一个键。

    这条钉的是"加字段不能顺手造默认值"——凭空造的 cmdsn 会让页面把一个**不属于任何
    在途命令**的回执当成某条命令的确认（比不带的危害更大）。
    """
    s, _ = _srv()
    out = await s._cmd_control({"gwSn": "GW1", "devSn": "5005A", "attribute": "w_travel",
                                "value": "100"})
    assert "cmdsn" not in out, out
    assert out == {"type": "control_ack", "ok": True, "msg": "ok", "attribute": "w_travel"}


@pytest.mark.asyncio
async def test_every_ack_return_path_carries_correlation_fields():
    """六个出口逐条走一遍：关联字段在唯一的 ack() 里统一，漏一条就说明有人又开始
    在每个 return 点各抄一份（那正是这批要消灭的形态）。"""
    s, _ = _srv()
    cases = [
        ({"devSn": ""}, "missing fields"),
        ({"value": ""}, "missing fields"),
        ({"value": True}, "missing fields"),
        ({"value": {"a": 1}}, "invalid value"),
        ({"value": "NaN"}, "invalid value"),
        ({"value": "100"}, "ok"),
    ]
    for over, want_msg in cases:
        msg = {"gwSn": "GW1", "devSn": "D1", "attribute": "w_travel",
               "value": "100", "cmdsn": "cmd-X"}
        msg.update(over)
        out = await s._cmd_control(msg)
        assert out["msg"] == want_msg, (over, out)
        assert out.get("cmdsn") == "cmd-X", "cmdsn 在这条回执路径上丢了: %r" % (out,)
        assert out.get("attribute") == "w_travel", "attribute 丢了: %r" % (out,)


@pytest.mark.asyncio
async def test_rejected_value_is_never_published_to_the_device():
    """格式闸必须在**发布之前**——拒了还发出去，就等于只是给了个失败回执而已。"""
    s, pub = _srv(_Pub(ok=True))
    for bad in ("NaN", "inf", "open"):
        out = await s._cmd_control({"gwSn": "GW1", "devSn": "D1", "attribute": "w_travel",
                                    "value": bad})
        assert out["ok"] is False, (bad, out)
    assert pub.calls == [], "被拒的值仍然发到了设备: %r" % (pub.calls,)


# ── 二、两条通道的格式闸同源 ────────────────────────────────────────
def test_both_channels_share_one_implementation_not_two_copies():
    """两条通道的判据必须是**同一实现**（v1.7.65 起云侧委托 ws_gateway）。

    旧钉比的是 `wg._VALUE_RE.pattern == hc._VALUE_RE.pattern`。那种比法只能证明两段
    正则抄得一样，而两侧真正漏掉的东西各不相同（云侧从不校验属性名，也不校验值域），
    于是它一路绿着放过了"attribute 打错字照样回 ok:true"这条真缺陷。
    本批把判据收敛成一份 ⇒ 这里只钉"不许再出现第二份拷贝"；两条通道**入口链**的
    行为等价性由 tests/test_control_attr_domain.py::test_two_entry_points_return_identical_verdict
    逐条矩阵钉（比函数比自己恒真，那条不算验证）。
    """
    assert not hasattr(hc, "_VALUE_RE"), \
        "hub_client 又自带了一份线值格式闸＝两条通道重新分叉"
    assert hc.validate_control_params("w_travel", "50") == "50"
    assert hc.validate_control_params("position", "50") is None


@pytest.mark.parametrize("bad", BAD_WIRE)
def test_cloud_channel_rejects_non_wire_values(bad):
    assert hc.validate_control_params("w_travel", bad) is None, "云通道放行了 %r" % (bad,)


@pytest.mark.parametrize("good", GOOD_WIRE)
def test_cloud_channel_passes_legal_decimal_values(good):
    assert hc.validate_control_params("w_travel", good) == good, "云通道误拒 %r" % (good,)


# ── 三、"合法值全是十进制"这个依据要能响，不能只活在注释里 ──────────
def test_no_unknown_attribute_appeared_in_the_004_parser():
    """取消 str 豁免的全部依据是"本协议不存在字符串线值"，而这条依据写在两个地方：
    const 的属性常量 + _ctypes 的属性 elif 链。将来固件加一个字符串值的属性时，
    现象会是"小程序静默收到 invalid value"——最难查的那种。所以这里把两个清单钉住：
    出现未知属性名必须先让这条红，由人显式决定格式闸怎么改。
    """
    src = _CTYPES.read_text(encoding="utf-8")
    found = set(re.findall(r'attribute == "([A-Za-z_0-9]+)"', src))
    assert found, "_ctypes 的属性判据锚点漂移（这条钉会退化成空判）"
    unknown = found - KNOWN_INBOUND_ATTRS
    assert not unknown, "004 出现未知属性名 %r：先确认它的值是不是十进制线值" % (sorted(unknown),)
    # const 里声明的出站属性也必须在已知集合内
    outbound = {const.ATTRIBUTE_W_TRAVEL, const.ATTRIBUTE_WIND_LOCK_MODE,
                const.ATTRIBUTE_WINACT_SPEED, const.ATTRIBUTE_WINACT_STRENGTH}
    assert outbound <= KNOWN_INBOUND_ATTRS | {"w_travel"}, \
        "const 的出站属性不在已知集合内: %r" % (outbound - KNOWN_INBOUND_ATTRS,)


@pytest.mark.asyncio
@pytest.mark.parametrize("good", GOOD_WIRE)
async def test_every_legal_wire_value_survives_both_gates(good):
    """合法值全集必须**两条通道都**放行——格式闸收紧后最坏的失败模式是挡掉真命令，
    所以不是只测拒绝面，而是把已知合法值逐个送过两道闸。"""
    assert hc.validate_control_params("w_travel", good) == good
    s, pub = _srv(_Pub(ok=True))
    out = await s._cmd_control({"gwSn": "GW1", "devSn": "D1",
                                "attribute": "w_travel", "value": good})
    assert out["ok"] is True, (good, out)
    assert pub.calls == [("D1", "w_travel", good)], (good, pub.calls)

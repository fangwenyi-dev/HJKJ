# -*- coding: utf-8 -*-
"""v1.5.2（N2）钉：百分比族越界绝对值分发前如实拦下（1.5.1 现网实锤）。

现网证据（21:47:20 / 21:48:10，加载项日志原行）：
    [执行] AdjustDeviceAttribute {'attribute':'brightness','delta':'200',...}
    [级联] '把亮度调到200' → [t0] '好的，射灯的亮度已设为200%'
「200%」是不可能状态却播成成功（集成按量程顶钳位执行了）——fast_path :130 立着
`0-100 之外不属百分比语义`，v1.1.27 量程闸只拦 4 位裸数，3 位的 150/200 从表规则
裸数支漏网。修位＝执行器分发闸（与开关族/能力/可用性三闸同位同形）：越界绝对值
零下发、如实报「只有 0 到 100」；相对档（±）、max/min、"3档"、非百分比族
（temperature/色温/number 域）一概放行——判不准就不判（v1.1.15），绝不拦死合法句。
"""
import asyncio

from conftest import FakeHAClient
from core.executor import Executor
from core.nlu.fast_path import Plan


def _ent(eid, state, name, **attrs):
    a = {"friendly_name": name}
    a.update(attrs)
    return {"entity_id": eid, "state": state, "attributes": a}


LAMP = _ent("light.ban_gong_shi_she_deng", "on", "射灯", supported_color_mode="brightness")
AREA = {"light.ban_gong_shi_she_deng": "办公室"}


def _adj(delta, attr="brightness", extra=None):
    steps = [( "AdjustDeviceAttribute",
              {"attribute": attr, "delta": delta,
               "target": [{"area": "办公室",
                           "devices": [{"name": "射灯", "domains": ["light"]}]}]},
              "t0")]
    es = [{"name": n, "args": a, "source": s} for n, a, s in (extra or [])]
    intent, args, src = steps[0]
    return Plan(intent=intent, args=args, source=src, utterance="量程测试",
                extra_steps=es)


def _run(plan):
    ha = FakeHAClient(results={"AdjustDeviceAttribute": {"success": True},
                               "HassTurnOn": {"success": True}},
                      states={"light.ban_gong_shi_she_deng": LAMP},
                      entity_area=AREA)
    ok, reply = asyncio.run(Executor(ha).run(plan))
    return ha, ok, reply


def test_200_percent_rejected_zero_dispatch():
    """主案（现网 21:47 形）：delta=200 ⇒ 零下发 + 如实量程话术，不得播"已设为200%"。"""
    ha, ok, reply = _run(_adj("200"))
    assert ha.calls == [], ha.calls
    assert ok is False and "0 到 100" in reply and "200" in reply, reply
    assert "已设为" not in reply, reply


def test_150_and_101_rejected_100_ok():
    """边界：101 拒、100 放（0-100 闭区间，v1.1.21 钉同律）。"""
    _, ok150, r150 = _run(_adj("150"))
    assert ok150 is False and "0 到 100" in r150
    _, ok101, _ = _run(_adj("101"))
    assert ok101 is False
    _, ok100, r100 = _run(_adj("100"))
    assert ok100 is True, r100


def test_relative_and_special_and_gear_still_pass():
    """不误拦（判据只吃裸绝对值）：+20/-20 相对档、max、"3档" 全部照常下发。

    v1.6.5（刀5-c）：旧形 `ok is not None` 对 bool **恒真**＝放行侧根本没断言
    （闸整个摘掉也绿）。改真判据：放行＝**必须真下发**（ha.calls 非空）且回执
    成功（ok is True）——量程闸被摘/被改成一律放行时，本钉与主案仍各自咬得住。"""
    for delta in ("+20", "-20", "max", "min", "3档", "50"):
        ha, ok, reply = _run(_adj(delta))
        assert ha.calls, (delta, "应下发却零调用")
        assert ok is True, (delta, ok, reply)
        assert "0 到 100" not in reply, (delta, reply)


def test_non_percent_attrs_untouched():
    """temperature 26（℃）、色温 4000（K）、number 域 value——不在百分比表＝不判。"""
    for attr, delta in (("temperature", "26"), ("color_temperature", "4000"),
                        ("value", "200")):
        ha, _, reply = _run(_adj(delta, attr=attr))
        assert "0 到 100" not in reply, (attr, reply)


def test_chain_second_leg_range_reject_keeps_first_leg_note():
    """链式形「亮度调到50，再调到200」：首腿合法执行、次腿越界拦下——话术必须
    走 _step_say 步序（第 1 步已完成），不得整句谎报也不得吞首腿。"""
    p = _adj("50")                       # 首腿：合法亮度 50，必下发
    p.extra_steps = [{"name": "AdjustDeviceAttribute",
                      "args": {"attribute": "brightness", "delta": "200",
                               "target": [{"area": "办公室",
                                           "devices": [{"name": "射灯",
                                                        "domains": ["light"]}]}]},
                      "source": "t0"}]
    ha, ok, reply = _run(p)
    assert len(ha.calls) == 1, ha.calls          # 首腿照发、次腿越界零下发
    assert ok is False and "步已完成" in reply and "0 到 100" in reply, reply

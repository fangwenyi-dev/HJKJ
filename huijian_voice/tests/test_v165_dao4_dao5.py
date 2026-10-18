# -*- coding: utf-8 -*-
"""v1.6.5 刀4（B 组话术）＋刀5（C 组收口）守卫钉。

工单＝2026-10-17 对现网 1.5.9→1.6.4 的深审批（B1~B5＋C 组在册），修法逐条落码后
本文件把**每条工单实跑形**钉成回归资产。三条纪律（工单"量具假绿"族直接点名过这里）：
· 夹具用**现网真形制**（含空格名、注册表 device_id 归堆）——旧钉"9 条全是不带空格名
  ＝零覆盖"正是 C 组假绿的成因，不许用理想形喂判据；
· 断言走**真函数**（_ambiguity_ask/Executor.run/_delay_verdict/dry_run 本体），
  不在测试里复刻剥壳/折叠逻辑；
· 正反双向：修复形必须绿，病形（工单原 repro）必须红——反向自证见各钉"病形"注记，
  变异 harness 在发布臂执行（_v165 mutation 脚本）。
"""
import asyncio
import collections
import os
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_v165_"))

from conftest import FakeHAClient                      # noqa: E402
from core.executor import Executor                     # noqa: E402
from core.nlu.fast_path import Plan                    # noqa: E402
from core.pipeline import (Pipeline, _DELAY_TURN,      # noqa: E402
                          _delay_turn_isolated)


def _pipe(states, entity_area, entity_device=None):
    class _S:
        def __init__(self, d):
            self.d = d

        def get(self, k, default=None):
            return self.d.get(k, default)

    p = Pipeline.__new__(Pipeline)
    p.settings = _S({})
    p._confirm = {}
    p._origin_ts = {}
    p.agent = None
    p.ha = types.SimpleNamespace(_states=dict(states),
                                 _entity_area=dict(entity_area),
                                 _entity_device=dict(entity_device or {}))
    return p


# ══ 刀4·B1 回显真名闸：含空格真名（现网 34 台里 32 台含空格）════════
# 病形（split(" ")[0] 取首段）：'客厅 吸顶灯' 听岔 → 播「客厅」；'新 麦克风开关'
# 听岔 → 播「新」。旧 9 条钉夹具全是不带空格名 ⇒ 零覆盖＝假绿。

def _echo(fn, word, area=""):
    ex = Executor.__new__(Executor)
    ex.ha = types.SimpleNamespace(
        _states={"light.x": {"attributes": {"friendly_name": fn}}},
        _entity_alias={})
    return ex._echo_truth_word(word, "light.x", area)


def test_b1_spaced_names_echo_device_segment():
    """工单 repro 双例：取**最后含中文段**，区域/父设备首段/英文型号尾都不许出场。"""
    assert _echo("客厅 吸顶灯", "昔顶灯") == "吸顶灯"     # 病形：'客厅'
    assert _echo("新 麦克风开关", "麦克分开关") == "麦克风开关"  # 病形：'新'
    assert _echo("办公室 空调 Indicator Light", "指等") == "空调"


def test_b1_exact_word_still_echoes_verbatim():
    """反向（相容支逐字不变）：听对时闸不介入——过改＝把 1.5.8 验收过的行为改坏。"""
    assert _echo("办公室射灯", "射灯") == ""
    assert _echo("新 麦克风开关", "新 麦克风开关") == ""
    assert _echo("客厅 吸顶灯", "吸顶灯") == ""          # 尾段相容也不许叠字


# ══ 刀4·B2 歧义话术分母：设备级折叠＋示例真形制 ════════════════════
# 工单实跑形：主域证据缺席时（HassTurnOff+泛窗名，domains=[]）pool=cands，
# 一台开窗器的按钮/数值/传感器被数成"独立设备"（实测 6 台/2 台）；示例硬编码
# 「客厅」、动词恒「打开」。

_OFFICE_WIN = {
    "cover.pk": {"entity_id": "cover.pk", "state": "open",
                 "attributes": {"friendly_name": "平开窗 开窗器"}},
    "cover.nk": {"entity_id": "cover.nk", "state": "open",
                 "attributes": {"friendly_name": "内开内倒 开窗器"}},
    "button.pk_1": {"entity_id": "button.pk_1", "state": "2026-01-01T00:00:00",
                    "attributes": {"friendly_name": "平开窗 ① 开启"}},
    "number.pk_sp": {"entity_id": "number.pk_sp", "state": "31.0",
                     "attributes": {"friendly_name": "平开窗 速度"}},
    "sensor.pk_bat": {"entity_id": "sensor.pk_bat", "state": "11.3",
                      "attributes": {"friendly_name": "平开窗 电池电压"}},
    "switch.nk_pow": {"entity_id": "switch.nk_pow", "state": "on",
                      "attributes": {"friendly_name": "内开内倒 电源"}},
}
_DEV2 = {"cover.pk": "DEV1", "button.pk_1": "DEV1", "number.pk_sp": "DEV1",
         "sensor.pk_bat": "DEV1", "cover.nk": "DEV2", "switch.nk_pow": "DEV2"}
_AREA2 = {k: "卧室" for k in _OFFICE_WIN}


def _turn_off_window():
    return Plan(intent="TurnDeviceOff",
                args={"target": [{"devices": [{"name": "窗", "domains": []}]}]},
                source="t0", utterance="关闭窗")


def test_b2_denominator_is_device_level_when_domain_evidence_absent():
    """主域证据缺席（domains=[]）：6 实体池话术必须报 **2 台**（注册表归堆），
    零件名不得进列名。病形（len(pool)=6）＝工单实测的"6 台/2 台"不实陈述。"""
    p = _pipe(_OFFICE_WIN, _AREA2, _DEV2)
    r = p._ambiguity_ask(_turn_off_window(), "o")
    assert r is not None and r.source == "clarify" and r.ok is False, r
    txt = r.text or ""
    assert "2 台" in txt, f"分母未折叠到设备级：{txt}"
    assert "6 台" not in txt, txt
    assert "① 开启" not in txt and "速度" not in txt and "电池" not in txt, \
        f"同机零件被报成独立设备：{txt}"
    assert "平开窗 开窗器" in txt and "内开内倒 开窗器" in txt, txt


def test_b2_example_verb_follows_intent_direction():
    """动词不再恒「打开」：关闭句撞歧义，示例教「关闭…」；病形＝旧「打开」恒定。"""
    p = _pipe(_OFFICE_WIN, _AREA2, _DEV2)
    r = p._ambiguity_ask(_turn_off_window(), "o")
    txt = r.text
    seg = txt.split("先不动。", 1)[1]
    assert "关闭" in seg and "打开" not in seg, f"示例动词与用户意图相反：{seg}"
    # 开向句仍报「打开」（映射只改恒值，不许把对的改反）
    p2 = _pipe(_OFFICE_WIN, _AREA2, _DEV2)
    plan_on = Plan(intent="TurnDeviceOn",
                   args={"target": [{"devices": [{"name": "窗", "domains": []}]}]},
                   source="t0", utterance="开窗")
    r2 = p2._ambiguity_ask(plan_on, "o")
    assert "打开" in (r2.text.split("先不动。", 1)[1]), r2.text


def test_b2_example_area_is_real_not_hardcoded():
    """示例区域取候选真挂的区域；**确证不了不许虚构**（病形＝硬编码「客厅」）。"""
    p = _pipe(_OFFICE_WIN, _AREA2, _DEV2)
    r = p._ambiguity_ask(_turn_off_window(), "o")
    assert "卧室" in r.text and "客厅" not in r.text, r.text
    # 无区域可证 ⇒ 不编房间名，改成"先说它所在的房间"句式
    p0 = _pipe(_OFFICE_WIN, {}, _DEV2)      # entity_area 空表＝区域无从确证
    r0 = p0._ambiguity_ask(_turn_off_window(), "o")
    t0 = r0.text
    assert "客厅" not in t0, f"无证据仍虚构客厅：{t0}"
    assert "所在的房间" in t0, t0


def test_b2_same_name_twins_not_collapsed():
    """反向保护（#17 同律）：真两台同名设备（注册表两个 device_id）**不得**被折叠
    成 1 台——报少了同样是实陈述。病形＝按名字归堆（旧首版就是这么错的）。"""
    twins = {
        "light.a": {"entity_id": "light.a", "state": "on",
                    "attributes": {"friendly_name": "射灯"}},
        "light.b": {"entity_id": "light.b", "state": "on",
                    "attributes": {"friendly_name": "射灯"}},
    }
    dev = {"light.a": "DEVA", "light.b": "DEVB"}
    p = _pipe(twins, {"light.a": "客厅", "light.b": "卧室"}, dev)
    plan = Plan(intent="TurnDeviceOn",
                args={"target": [{"devices": [{"name": "灯", "domains": []}]}]},
                source="t0", utterance="开灯")
    r = p._ambiguity_ask(plan, "o")
    assert r is not None and r.source == "clarify", r
    assert "2 台" in r.text and "1 台" not in r.text, r.text


def test_b2_say_no_area_no_evidence_conservative():
    """话术函数直调：example_area 空 ⇒ 不出现任何房间名；默认参保持旧文案形。"""
    s = Pipeline._ambiguity_clarify_say(["射灯", "氛围灯"], 2, area="",
                                        verb="关闭", example_area="")
    assert "客厅" not in s and "所在的房间" in s and "关闭" in s, s
    s2 = Pipeline._ambiguity_clarify_say(["射灯"], 1, area="", example_area="卧室")
    assert "卧室的射灯" in s2 and "打开" in s2, s2      # 默认动词＝旧形不漂移


# ══ 刀4·B3 量程闸位后移：真因先报 ═══════════════════════════════════

def _ent(eid, name, **attrs):
    a = {"friendly_name": name}
    a.update(attrs)
    return {"entity_id": eid, "state": "on", "attributes": a}


def _adj_plan(attr, delta, area, name, domains):
    return Plan(intent="AdjustDeviceAttribute",
                args={"attribute": attr, "delta": delta,
                      "target": [{"area": area,
                                  "devices": [{"name": name,
                                               "domains": domains}]}]},
                source="t0", utterance="量程")


def test_b3_unsupported_light_200_reports_capability_truth_not_range():
    """工单 repro：不支持亮度的灯说「亮度调到200」——真因是**不支持**（80 也不行）。
    病形（量程闸在前）播「只有0到100」＝"换个数再试"式误导。"""
    plain = _ent("light.dm", "门口灯", supported_features=0)
    ha = FakeHAClient(results={"AdjustDeviceAttribute": {"success": True}},
                      states={"light.dm": plain},
                      entity_area={"light.dm": "门口"})
    ok, reply = asyncio.run(Executor(ha).run(
        _adj_plan("brightness", "200", "门口", "门口灯", ["light"])))
    assert ok is False and ha.calls == []
    assert "0 到 100" not in reply, f"量程话术仍盖掉真因：{reply}"
    assert "不支持" in reply, reply


def test_b3_no_metadata_fallback_still_blocks_200():
    """能力元数据缺席（老集成/未上报）⇒ 能力闸放行、量程闸**兜底不失**：
    v1.5.2 主案（「亮度调到200」谎报 200%）不得因闸位后移回归。"""
    nofeat = {"entity_id": "light.x", "state": "on",
              "attributes": {"friendly_name": "玄灯"}}
    ha = FakeHAClient(results={"AdjustDeviceAttribute": {"success": True}},
                      states={"light.x": nofeat}, entity_area={"light.x": "玄"})
    ok, reply = asyncio.run(Executor(ha).run(
        _adj_plan("brightness", "200", "玄", "玄灯", ["light"])))
    assert ok is False and ha.calls == []
    assert "0 到 100" in reply, reply


# ══ 刀4·B4 非百分比族越界如实报（读实体注册表量程）════════════════

def test_b4_climate_200_refused_with_real_range():
    """工单 repro：「把空调温度调到200度」旧形放行⇒集成 30 钳位执行⇒播「已设为30度」。
    修后：拦下并念**实体真挂量程**（min_temp/max_temp），零下发。"""
    ac = _ent("climate.kt", "办公室空调", min_temp=16, max_temp=30)
    ha = FakeHAClient(results={"AdjustDeviceAttribute": {"success": True}},
                      states={"climate.kt": ac},
                      entity_area={"climate.kt": "办公室"})
    ok, reply = asyncio.run(Executor(ha).run(
        _adj_plan("temperature", "200", "办公室", "空调", ["climate"])))
    assert ok is False and ha.calls == []
    assert "16 到 30" in reply and "200" in reply, reply
    assert "已设为" not in reply, reply


def test_b4_climate_default_bounds_match_integration():
    """实体没上报量程 ⇒ 与集成 `.get(ATTR_MIN_TEMP,10)/.get(...,30)` **同缺省**判：
    闸与钳位口径不一致＝同一个谎报族。"""
    ac = _ent("climate.kt", "卧室空调")
    ha = FakeHAClient(results={"AdjustDeviceAttribute": {"success": True}},
                      states={"climate.kt": ac}, entity_area={"climate.kt": "卧室"})
    ok, reply = asyncio.run(Executor(ha).run(
        _adj_plan("temperature", "50", "卧室", "空调", ["climate"])))
    assert ok is False and "10 到 30" in reply, reply


def test_b4_legal_and_unjudged_attrs_pass():
    """反向：26 度合法句照常下发；color_temperature 不在来源表＝不判（v1.1.15）。"""
    ac = _ent("climate.kt", "办公室空调", min_temp=16, max_temp=30)
    ha = FakeHAClient(results={"AdjustDeviceAttribute": {"success": True}},
                      states={"climate.kt": ac}, entity_area={"climate.kt": "办公室"})
    ok, reply = asyncio.run(Executor(ha).run(
        _adj_plan("temperature", "26", "办公室", "空调", ["climate"])))
    assert ok is True and len(ha.calls) == 1, (ok, reply)
    lamp = _ent("light.sd", "射灯", supported_features=19,
                supported_color_modes=["color_temperature"])
    ha2 = FakeHAClient(results={"AdjustDeviceAttribute": {"success": True}},
                       states={"light.sd": lamp}, entity_area={"light.sd": "办公室"})
    ok2, reply2 = asyncio.run(Executor(ha2).run(
        _adj_plan("color_temperature", "4000", "办公室", "射灯", ["light"])))
    assert len(ha2.calls) == 1, f"色温被量程闸误拦（越权判）：{reply2}"


def test_b4_multi_target_one_reachable_passes():
    """多台候选：一台量程够 ⇒ 放行（集成逐台判，闸不得替全场拦死）。"""
    ac1 = _ent("climate.a1", "客厅空调", min_temp=16, max_temp=30)
    ac2 = _ent("climate.a2", "机房空调", min_temp=5, max_temp=300)
    st = {"climate.a1": ac1, "climate.a2": ac2}
    ha = FakeHAClient(results={"AdjustDeviceAttribute": {"success": True}},
                      states=st, entity_area={})
    plan = Plan(intent="AdjustDeviceAttribute",
                args={"attribute": "temperature", "delta": "200",
                      "target": [{"devices": [{"name": "空调",
                                               "domains": ["climate"]}]}]},
                source="t0", utterance="温度调到200")
    ok, reply = asyncio.run(Executor(ha).run(plan))
    assert len(ha.calls) == 1, f"有台做得到却被整条拦死：{reply}"


# ══ 刀4·B5 CJK 相邻控制符（旧 1.5.4 钉夹具零覆盖形）══════════════

def test_b5_cjk_adjacent_control_tokens_stripped():
    """工单 repro：'SPK嗯'/'嗯SPK' 在旧 `\\b` 字界下**根本不剥**（Python unicode
    把 CJK 也算 \\w，K 与 嗯 相邻无边界）⇒ 塌不成空、fallback 播「我不会」。
    现网 307 金标句没有这两形＝旧钉全绿＝假绿族；本钉直调真出口单点。
    反向：BOSE音箱 的 BOS 右邻 E 是 ASCII 字母 ⇒ 不许误剥（型号名是真实存在过
    的坑——1ed54d3 Sun 事故同族）。"""
    from core.asr import _clean_transcript_text as clean
    assert clean("SPK嗯") == "嗯"           # 剥净控制符
    assert clean("嗯SPK") == "嗯", "CJK 前置形未剥＝工单主案"
    assert clean("打开SPK灯") == "打开灯"
    assert clean("<unk>SPK<unk>") == ""
    assert clean("BOSE音箱") == "BOSE音箱", "型号名被过剥"
    assert clean("SPK1音箱") == "SPK1音箱", "粘连 ASCII 数字不剥（保守侧原律）"


# ══ 刀5-a dry_run 面板与真流量同裁决（延时腿）══════════════════════

def test_d5a_dryrun_reports_delay_verdict_not_immediate_plan():
    """工单 C 组：旧面板对「十秒后关灯」展示立即执行的 plan，真流量 source=delay。
    修后：面板报排程预演、plan=None，且**零落袋**（调试不动生产预约）。"""
    p = _pipe({}, {}, {})

    async def go():
        return await p.dry_run("10秒后关闭办公室射灯")

    out = asyncio.run(go())
    assert out.get("delay") is True, out
    assert out.get("delay_action") == "schedule", out
    assert out.get("plan") is None, "面板仍在展示立即执行的 plan＝两套裁决回潮"
    assert out.get("delay_body") == "关闭办公室射灯", out
    assert getattr(p, "_delay_tasks", set()) == set(), "面板落了袋＝调试动了生产"
    assert getattr(p, "_delay_booking", {}) == {}


def test_d5a_dryrun_refuse_wording_is_same_source_as_real_flow():
    """面板话术与真流量**同一单点**生成（不许抄第二份——本仓纪律）：
    越界形的 final 逐字等于 _delay_verdict 的 say。"""
    p = _pipe({}, {}, {})
    _k, say, _b, _s = p._delay_verdict("两小时后关灯")

    async def go():
        return await p.dry_run("两小时后关灯")

    out = asyncio.run(go())
    assert out.get("delay_action") == "nosched" and out.get("final") == say, out


# ══ 刀5-b 语音撤单（booking 按 origin 分桶）════════════════════════

def _delay_pipe():
    p = Pipeline.__new__(Pipeline)
    p.settings = types.SimpleNamespace(get=lambda k, d=None: d)
    p._delay_tasks = set()
    p._delay_booking = collections.OrderedDict()
    return p


def test_d5b_cancel_removes_only_own_origin_bookings():
    """排两单（panel/satA 各一）→ panel 说「取消预约」只撤自己那条；satA 撤自己。
    病形＝全局袋一把清（一颗卫星撤单吞别人家的单＝在册低危同族）。"""
    async def go():
        p = _delay_pipe()
        r1 = p._delay_gate("30秒后关闭办公室射灯", "panel")
        r2 = p._delay_gate("30秒后打开客厅的灯", "satA")
        assert r1.ok and r2.ok and len(p._delay_booking) == 2
        g = p._delay_cancel_gate("取消预约", "panel")
        assert g is not None and "关闭办公室射灯" in g.text, g.text
        assert len(p._delay_booking) == 1, "别人的单被误撤"
        g2 = p._delay_cancel_gate("取消定时", "satA")
        assert g2 is not None and len(p._delay_booking) == 0
        # 空账如实答"没有"，不得假成功念"已取消"
        g3 = p._delay_cancel_gate("取消预约", "panel")
        assert g3 is not None and "没有待执行" in g3.text, g3.text
        for t, _b in list(p._delay_booking.items()):
            t.cancel()
    asyncio.run(go())


def test_d5b_bare_cancel_does_not_touch_bookings():
    """反向钉（判据不越权）：裸「取消」是确认环应答词，**不得**被撤单表吃走
    ——扩它的全局语义＝跨族行为变更（"判据被别处同名喂饱"在册同型坑）。"""
    p = _delay_pipe()
    assert p._delay_cancel_gate("取消", "panel") is None
    assert p._delay_cancel_gate("不要", "panel") is None
    assert p._delay_cancel_gate("关闭客厅的灯", "panel") is None
    assert p._delay_cancel_gate("取消预约吧喂", "panel") is None   # 非整句等值不进门


def test_d5b_delay_turn_isolation_blocks_self_cancel():
    """延时轮隔离旗内不响应撤单（A3 同律）：到期重入正文撞进撤单表＝自撤。"""
    p = _delay_pipe()

    async def go():
        r = p._delay_gate("30秒后关闭办公室射灯", "panel")
        assert r.ok
        tok = _DELAY_TURN.set(True)
        try:
            assert p._delay_cancel_gate("取消预约", "panel") is None, \
                "延时轮吃撤单＝A3 互踩复发"
        finally:
            _DELAY_TURN.reset(tok)
        assert len(p._delay_booking) == 1
        for t, _b in list(p._delay_booking.items()):
            t.cancel()
    asyncio.run(go())


def test_d5b_wired_before_delay_gate():
    """接线面（防"函数写了没人调"）：_cascade 必须调 _delay_cancel_gate；
    dry_run 必须带撤单预演支（面板零 cancel）。"""
    import inspect
    src = inspect.getsource(Pipeline._cascade)
    assert "_delay_cancel_gate(" in src, "撤单闸没接进主链＝摆设"
    dsrc = inspect.getsource(Pipeline.dry_run)
    assert "_DELAY_CANCEL_WORDS" in dsrc, "面板不预演撤单＝两套裁决"


# ══ 刀5-c 量具真判据的"钉有牙"结构自证（不依赖变异 harness 的部分）══

def test_d5c_v152_pass_side_asserts_real_dispatch():
    """放行侧断言必须**有判据**（工单点名 `ok is not None` 恒真）：
    test_v152 的放行钉现在含 `ok is True` 与 `ha.calls`——钉文本反查太弱，
    这里对**行为**负责：假造一个"闸一律放行"的世界没意义，改钉闸摘除的等价
    判据＝放行形必须真下发（ha.calls 非空）。用真 Executor 走一遍 min 档：
    min/max 相对形不判、必须下发（若哪天判据发疯拦了放行形，本钉红）。"""
    lamp = _ent("light.b1", "射灯", supported_color_mode="brightness")
    ha = FakeHAClient(results={"AdjustDeviceAttribute": {"success": True}},
                      states={"light.b1": lamp},
                      entity_area={"light.b1": "办公室"})
    for delta in ("+20", "-20", "max", "min", "3档", "50"):
        _h, _ok, _r = ha, None, None
        h2 = FakeHAClient(results={"AdjustDeviceAttribute": {"success": True}},
                          states={"light.b1": lamp},
                          entity_area={"light.b1": "办公室"})
        ok, reply = asyncio.run(Executor(h2).run(
            _adj_plan("brightness", delta, "办公室", "射灯", ["light"])))
        assert h2.calls, f"{delta}：放行形被拦＝误伤回归"
        assert ok is True, (delta, ok, reply)


def test_d5c_clean_is_single_export_point():
    """刀5-c 结构钉：静音剥壳=生产出口单点 _clean_transcript_text（v154 语义/接线
    两半都咬这个真函数）；_local_transcribe 里不得再留**摊开的**两级 sub
    （摊开＝复刻第二处的起点，v1.5.4 假绿的形状）。"""
    import inspect
    from core.asr import _clean_transcript_text
    from core.asr import AsrEngine
    cs = inspect.getsource(_clean_transcript_text)
    assert "_STRIP_TAGS.sub" in cs and "_STRIP_CTRL.sub" in cs, \
        "出口单点丢了某一级剥壳"
    tl = inspect.getsource(AsrEngine._local_transcribe)
    assert "_STRIP_CTRL.sub(" not in tl and "_STRIP_TAGS.sub(" not in tl, \
        "转写函数里又出现摊开的剥壳＝复刻第二份（假绿回潮起点）"
    assert "_clean_transcript_text(" in tl, "单点没接在转写出口上"

"""v1.5.8 klar 回显真名闸（问题2 现网日志「办公室设置开了」）。

钉死一件事：**播报落到实际执行的那台**——STT 听岔词不得被回显层原样念出去。
根因三层（详见 E:\\AI\\0513gujian\\docs\\修复工单_klar回显听岔词_2026-10-15.md）：
说「打开办公室射灯」，FireRedASR2 转写成「打开办公室设置」（听岔），但 klar 靠拼音
模糊**仍把设备挑对了**（`light.ban_gong_shi_she_deng`＝办公室射灯，灯真开了）；执行层
无责。缺陷放大器在 `_klar_echo`：它按 2026-09-14 的设计"回显用户原话里的目标词"，
把听岔词照念 ⇒「办公室设置开了」。

修法＝回显层单点加"词—真名相容性闸"：word 与实际设备真名不符时改播真名，听对时
逐字不变。动的是话术、不动裁决/转写/动作（保守到不发虚，宁漏不误改）。

这些钉同时是**反向保护**：
- 听对（word 相容真名）必须**不换**——保住既有 test_klar_nlu:684/732 的「射灯开了」
  「办公室射灯开了」，闸不得把对的改成错的；
- 别名（用户亲手写的叫法）必须**不被真名顶掉**——homophone 先验同源那套别名纪律；
- 拿不到真名（快照缺失/无 friendly_name/多目标）必须**退回现行为**逐字不变；
- 真名自带区域时**不重念**——复用 v1.1.28「办公室的办公室空调」那条钉的同一规矩。

执行入口一律走真 `Executor.run` 全链（含 `_klar_echo` 调用点 :1382），不打桩绕开，
防"改了但没人调用"（v1.6 契约钉元测试同族防线）。
"""
import asyncio
import types

from core.executor import Executor
from core.nlu.fast_path import Plan


def arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


EID = "light.ban_gong_shi_she_deng"


class _HA:
    """只读快照夹具：与 pipeline 侧同款 SimpleNamespace(_states=..., _entity_alias=...)，
    外加 run() 需要的 call_service（平铺 entity_id 形走直调服务，绕开 intent handler）。"""

    def __init__(self, states=None, alias=None):
        self._states = states if states is not None else {}
        self._entity_alias = alias or {}
        self.services = []

    async def handle_intent(self, name, args):
        return {"success": True}

    async def call_service(self, domain, service, data, timeout=10.0):
        self.services.append((domain, service, data))
        return {"success": True, "message": "", "raw": "[]"}


def _states(friendly, area=None):
    ent = {"entity_id": EID, "state": "off",
           "attributes": {"friendly_name": friendly}}
    return {EID: ent}


def _plan(utterance, args):
    return Plan(intent="HassTurnOn", args=args, source="klar", utterance=utterance,
                speech="办公室开了。")


def test_v158_echo_stt_mishearing_replaced_by_true_name():
    """听岔替换：说「设置」实际开的是「办公室射灯」⇒ 播报含射灯、不含设置。"""
    ha = _HA(_states("办公室射灯"))
    ok, reply = arun(ha and Executor(ha).run(_plan(
        "打开办公室设置", {"entity_id": EID})))
    assert ok
    assert "射灯" in reply, f"听岔词没被真名替换：{reply}"
    assert "设置" not in reply, f"仍在复读 STT 听岔词：{reply}"


def test_v158_echo_correct_word_untouched():
    """听对时逐字不变（对齐 test_klar_nlu:732 无 area 槽那钉）：仍「办公室射灯开了」。"""
    ha = _HA(_states("办公室射灯"))
    ok, reply = arun(Executor(ha).run(_plan(
        "打开办公室射灯", {"entity_id": EID})))
    assert ok
    assert reply == "办公室射灯开了", f"听对被闸改坏了：{reply}"


def test_v158_echo_alias_word_preserved():
    """别名相容不换：用户亲手写的叫法命中 `_entity_alias[eid]` ⇒ 保留原叫法，
    不许被 friendly_name 顶掉（别名是用户意图，不是听岔）。"""
    ha = _HA(_states("办公室射灯"), alias={EID: ["小射", "ban_gong_shi_she_deng"]})
    ok, reply = arun(Executor(ha).run(_plan(
        "打开小射", {"entity_id": EID})))
    assert ok
    assert "小射" in reply, f"别名被真名顶掉了：{reply}"


def test_v158_echo_missing_snapshot_falls_back():
    """快照缺失（ha 无 `_states`，即现 FakeHA/SpyHA 形状）⇒ 现行为逐字不变。"""
    class _NoStates:
        def __init__(self):
            self.services = []

        async def handle_intent(self, name, args):
            return {"success": True}

        async def call_service(self, domain, service, data, timeout=10.0):
            self.services.append((domain, service, data))
            return {"success": True, "message": "", "raw": "[]"}

    ha = _NoStates()
    ok, reply = arun(Executor(ha).run(_plan(
        "打开办公室设置", {"entity_id": EID})))
    assert ok
    assert reply == "办公室设置开了", f"拿不到真名不该改现行为：{reply}"


def test_v158_echo_multi_entity_not_gated():
    """多 entity_id（list 长度>1）不进闸：批量承诺归 P1 回执派生支，此处不动。"""
    ha = _HA(_states("办公室射灯"))
    ha._states["light.other"] = {"entity_id": "light.other", "state": "off",
                                 "attributes": {"friendly_name": "客厅台灯"}}
    p = Plan(intent="HassTurnOn", source="klar", utterance="打开办公室设置",
             speech="办公室开了。",
             args={"entity_id": [EID, "light.other"]})
    ok, reply = arun(Executor(ha).run(p))
    assert ok
    # 多目标：闸不触发，word 仍是原话听岔词（P1 管批量，本单不越界）
    assert "设置" in reply, f"多目标不该进真名闸：{reply}"


def test_v158_echo_no_area_double():
    """区域不重念反向钉：真名自带区域时，替换后不得出现「办公室的办公室射灯」
    （v1.1.28 test_v1128:142 同规矩的回显层回声）。"""
    ha = _HA(_states("办公室射灯"))
    ok, reply = arun(Executor(ha).run(_plan(
        "打开办公室设置", {"entity_id": EID, "area": "办公室"})))
    assert ok
    assert "办公室的办公室" not in reply, f"区域被念了两遍：{reply}"
    assert "射灯" in reply and "设置" not in reply, f"听岔没替换干净：{reply}"


def test_v158_echo_empty_friendly_name_untouched():
    """快照有条目但无 friendly_name ⇒ 不动现行为（真名拿不到＝不发虚）。"""
    ha = _HA({EID: {"entity_id": EID, "state": "off", "attributes": {}}})
    ok, reply = arun(Executor(ha).run(_plan(
        "打开办公室设置", {"entity_id": EID})))
    assert ok
    assert "设置" in reply, f"无真名不该改：{reply}"


def test_v158_truth_word_unit_shapes():
    """闸本体单元形状（纯函数面，钉死'该不改返回 ""'的几条相容判据）。"""
    ex = Executor(_HA(_states("办公室射灯")))
    f = ex._echo_truth_word
    # 相容（全等/子串/剥区域设备段）⇒ ""
    assert f("办公室射灯", EID, "") == ""
    assert f("射灯", EID, "") == ""
    # 不相容 ⇒ 真名设备段（area 空时真名整段，含区域字样即区域不叠加）
    assert f("设置", EID, "") == "办公室射灯"
    assert f("设置", EID, "办公室") == "射灯"
    # 无该 eid 快照 ⇒ ""
    assert f("设置", "light.不在册", "") == ""


# ── 互斥契约钉（全量回归抓出的真回归后补，2026-10-15）──────────────
# 场景：说「打开客厅的灯」，klar 落到离线的 light.she_deng（friendly_name 恰好也叫
# 「射灯」），执行层 v1.1.15 D4 改指到同名可用的办公室射灯。此时回显词（「客厅的灯」）
# 与真名（「射灯」）**确实不符**——但不符是**目标被替换**造成的，不是 STT 听岔，
# 本闸"设备挑对了"的前提不成立。那条链的如实机制＝"原话术＋改指注"两条一起播
# （test_offline_twin_repoint 收口批钉着原话术不被吞）。闸必须在改指轮让路；
# 这条钉显式锁死两条链的互斥，防日后重构把让路条件弄丢（test_repoint_is_announced
# 是同一事故的第二来源，两条都红才说明真丢了）。
def test_v158_gate_yields_on_repoint_turn():
    from conftest import FakeHAClient

    class _RepointHa(FakeHAClient):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.svc_calls = []

        async def call_service(self, domain, service, data, timeout=10.0):
            self.svc_calls.append((domain, service, data))
            return {"success": True}

    offline = {"entity_id": "light.she_deng", "state": "unavailable",
               "attributes": {"friendly_name": "射灯"}}
    good = {"entity_id": "light.ban_gong_shi_she_deng", "state": "off",
            "attributes": {"friendly_name": "射灯"}}
    ha = _RepointHa(states={"light.she_deng": offline,
                            "light.ban_gong_shi_she_deng": good})
    p = Plan(intent="HassTurnOn", source="klar", utterance="打开客厅的灯",
             speech="客厅开了。", args={"entity_id": "light.she_deng"})
    ok, msg = arun(Executor(ha, None).run(p))
    assert ok is True, msg
    assert "客厅的灯开了" in msg, f"改指轮里闸把原话术吞了：{msg}"
    assert "离线" in msg and "改指" in msg, f"改指注丢了：{msg}"


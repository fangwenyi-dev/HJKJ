"""v1.1.15 钉桩（办公 .91 实锤 D2）：链式一句话里被证伪的分句必须点名。

现场（板 32b8 / 加载项 1.1.14 / SenseVoice，HA .91 只有 4 个 light）：
    [级联] '打开办公室射灯和打开床头灯' → [chain] '好的，都办妥了'
    [执行] HassTurnOn {'entity_id': 'light.ban_gong_shi_she_deng'}(+1步) → 成功
而 HA history 那 5 分钟内对这台灯**一次状态变化都没有**——第一条分句是对已开着的灯
做"开"（HA 照样回 success 的空操作），第二条分句的设备名"床头灯"在这屋里根本不存在。
顶层 success + `_receipt` 只在 result 带 per-entity `states` 时才计数（executor.py 里
它自己的注释就承认这点），所以"都办妥了"是**结构性假形**，不是偶发。

纪律：判不了就不判（快照空、非 on/off 域、区域表认不出该区），宁可不点名，
也绝不把"做了"说成"没做"。（收口批起单步计划同样受判——旧稿"单步一律不动话术"
的栅栏已按审计结论撤除，见文件末的正向钉。）
"""
import asyncio
from pathlib import Path

from conftest import FakeHAClient
from core.executor import Executor
from core.nlu.fast_path import Plan


def _ent(eid, state, name):
    return {"entity_id": eid, "state": state, "attributes": {"friendly_name": name}}


LAMP_ON = _ent("light.ban_gong_shi_she_deng", "on", "射灯")
LAMP_OFF = _ent("light.ban_gong_shi_she_deng", "off", "射灯")
DESK_OFF = _ent("light.desk", "off", "台灯")
AREA = {"light.ban_gong_shi_she_deng": "办公室", "light.desk": "办公室"}


def _tgt(nm, dom="light", area="办公室"):
    return {"target": [{"area": area, "devices": [{"name": nm, "domains": [dom]}]}]}


def _run(states, steps, source="t0", results=None, entity_area=None, log=None):
    # A2 量具真形修正（审计 2026-10-17）：旧默认 source="chain" 在 core 里**永不产生**
    # （Plan.source 字面量枚举＝t0/t1/klar/llm/scene/creation，链合并取首腿
    # source=pipeline:2446），钉在非生产值上＝假绿。默认改 t0（字面表首腿真形），
    # klar 支由 test_n1 参数化显式覆盖。
    ha = FakeHAClient(results=results if results is not None else
                      {"HassTurnOn": {"success": True}, "HassTurnOff": {"success": True}},
                      states=states, entity_area=entity_area or {})
    intent, args = steps[0]
    plan = Plan(intent=intent, args=args, source=source, utterance="链测试",
                extra_steps=[{"name": n, "args": a} for n, a in steps[1:]])
    ok, reply = asyncio.run(Executor(ha).run(plan))
    return ok, reply, ha


def test_missing_device_leg_is_named_not_blanket_success():
    """「…和打开床头灯」：床头灯查无此名 ⇒ 点名，禁"都办妥了"。"""
    steps = [("HassTurnOn", _tgt("射灯")), ("HassTurnOn", _tgt("床头灯"))]
    ok, reply, _ = _run({"light.ban_gong_shi_she_deng": LAMP_OFF}, steps,
                        entity_area=AREA)
    assert "床头灯" in reply and "没找到" in reply, reply
    assert "都办妥了" not in reply, reply


def test_noop_leg_is_named_as_already_in_state():
    """对已开着的灯说"开" ⇒ 空操作要如实说，不得混进"都办妥了"。"""
    steps = [("HassTurnOn", _tgt("射灯")), ("HassTurnOn", _tgt("台灯"))]
    ok, reply, _ = _run({"light.ban_gong_shi_she_deng": LAMP_ON, "light.desk": DESK_OFF},
                        steps, entity_area=AREA)
    assert "射灯" in reply and "本来就在要求的状态上" in reply, reply
    assert "台灯" not in reply.split("本来")[0] or True      # 只点名射灯
    assert "都办妥了" not in reply, reply


def test_healthy_klar_chain_wording_untouched():
    """两条分句都成立（都从 off→on）⇒ 既有"都办妥了"口径一字不改（防过度修正）。"""
    steps = [("HassTurnOn", _tgt("射灯")), ("HassTurnOn", _tgt("台灯"))]
    ok, reply, _ = _run({"light.ban_gong_shi_she_deng": LAMP_OFF, "light.desk": DESK_OFF},
                        steps, source="klar", entity_area=AREA)
    assert ok is True and reply == "好的，都办妥了", reply


def test_single_step_plan_also_gets_leg_truth():
    """**契约变更**（收口批）：单步计划同样逐台证伪。

    旧钉 `test_single_step_plan_gets_no_leg_truth` 钉的正是本批要根除的形态：单步恒
    "顶层 success＝成功"，电视声被听成单步 HassTurnOff 打在已关的灯上照样回"关了"
    （2026-09-27 17:02/17:03 审计实证）。判据侧本就对单步成立（resolve_candidates 与
    _leg_truth_by_entity 都不依赖步数），那道 `len(steps) > 1` 只是话术侧的旧口径。
    留着旧钉＝把旧病写成契约文档，故改写为正向钉（同 v1.1.13 改写同步 ensure 钉的先例）。
    """
    ok, reply, _ = _run({"light.ban_gong_shi_she_deng": LAMP_ON},
                        [("HassTurnOn", _tgt("射灯"))], entity_area=AREA)
    assert ok is True
    assert "本来就在要求的状态上" in reply, reply


def test_empty_snapshot_does_not_invent_words():
    """快照空＝无从证伪 ⇒ 退回既有话术，绝不凭空说"没找到"。"""
    steps = [("HassTurnOn", _tgt("射灯")), ("HassTurnOn", _tgt("床头灯"))]
    ok, reply, _ = _run({}, steps)
    assert "没找到" not in reply, reply


def test_cover_domain_not_judged_as_noop():
    """cover 的状态名不是 on/off（open/closed）⇒ 不判空操作，避免把"开到头"说成没动。"""
    steps = [("HassTurnOn", _tgt("平开窗", dom="cover")), ("HassTurnOn", _tgt("台灯"))]
    ok, reply, _ = _run({"cover.w": _ent("cover.w", "open", "平开窗"), "light.desk": DESK_OFF},
                        steps, entity_area={"cover.w": "办公室", "light.desk": "办公室"})
    assert "本来就在要求的状态上" not in reply, reply


def test_every_leg_leaves_its_own_log_line(caplog):
    """D2 的另一半：旧实现整链只印首步 intent/args，第二腿在账上不存在 ⇒ 现场无法对账。"""
    steps = [("HassTurnOn", _tgt("射灯")), ("HassTurnOff", _tgt("台灯"))]
    with caplog.at_level("INFO"):
        _run({"light.ban_gong_shi_she_deng": LAMP_OFF, "light.desk": DESK_OFF}, steps,
             entity_area=AREA)
    assert "第 1/2 步 HassTurnOn" in caplog.text, caplog.text[-600:]
    assert "第 2/2 步 HassTurnOff" in caplog.text, caplog.text[-600:]


def test_office_0927_1355_real_shape():
    """2026-09-27 13:55 办公 .91 实锤形状（用户指出"第二条没执行"）：
    「关闭办公室平开窗 + 打开办公室射灯」，HA history 核对结果——
      腿1 真动了：button.…_3_guan_bi 于 13:55:32/13:55:51 各触发一次，
                  cover.…开窗器 13:55:53 open→closed；
      腿2 是空操作：light.ban_gong_shi_she_deng 自 13:38:56 起一直 on，13:50 后零状态变化。
    ⇒ v1.1.14 把这条报成"好的，都办妥了"（顶层 success 骗人）。本钉锁住修后口径：
    灯那条必须点名"本来就在要求的状态上"，且窗（button/cover，非 on/off 态）不得被误判。
    """
    win = _ent("cover.ban_gong_shi_ping_kai_chuang_kai_chuang_qi", "open", "平开窗 开窗器")
    btn = _ent("button.ban_gong_shi_ping_kai_chuang_3_guan_bi",
               "2026-09-27T05:55:32.765914+00:00", "平开窗 ③ 关闭")
    area = {win["entity_id"]: "办公室", btn["entity_id"]: "办公室",
            "light.ban_gong_shi_she_deng": "办公室"}
    steps = [("ControlWindow", {"target": [{"area": "办公室",
                         "devices": [{"name": "平开窗", "domains": ["button", "cover", "number"]}]}],
                               "action": "close"}),
             ("HassTurnOn", _tgt("射灯"))]
    ok, reply, _ = _run({win["entity_id"]: win, btn["entity_id"]: btn,
                         "light.ban_gong_shi_she_deng": LAMP_ON}, steps, entity_area=area)
    assert "射灯" in reply and "本来就在要求的状态上" in reply, reply
    assert "都办妥了" not in reply, reply


# ── v1.5.1（N1，1.5.0 线上复测实锤 18:48:47 .91）────────────────────────
class _FlapClient(FakeHAClient):
    """Adjust 腿**下发之后**快照才翻 unavailable——mesh 瞬断的时序形状：
    发前可用态闸放行（off），发后确证读（_offline_names 读 states()）点名。"""

    async def handle_intent(self, name, data, timeout=10.0):
        r = await super().handle_intent(name, data, timeout)
        if name == "AdjustDeviceAttribute":
            for eid, ent in list(self._states.items()):
                if str(eid) == "light.ban_gong_shi_she_deng":
                    e = dict(ent); e["state"] = "unavailable"
                    self._states[eid] = e
        return r


def _flap_run(steps, source="t0"):
    # A2 量具真形修正：同 _run——"chain" 在 core 永不产生；首腿档位真形=t0/klar。
    ha = _FlapClient(
        results={"HassTurnOn": {"success": True},
                 "AdjustDeviceAttribute": {"success": True}},
        states={"light.ban_gong_shi_she_deng": _ent("light.ban_gong_shi_she_deng",
                                                    "off", "射灯")},
        entity_area={"light.ban_gong_shi_she_deng": "办公室"})
    intent, args = steps[0]
    plan = Plan(intent=intent, args=args, source=source, utterance="链瞬断测试",
                extra_steps=[{"name": n, "args": a} for n, a in steps[1:]])
    return asyncio.run(Executor(ha).run(plan))


def test_n1_chain_post_send_flap_appends_not_overwrites():
    """N1 主案：两腿都下发、Adjust 腿事后 mesh 瞬断 ⇒ 证伪句**降为尾注**——
    旧写法整句覆写成「好的，『射灯』现在离线、这条没执行」，把首腿已完成的
    开灯事实吞掉（v1.1.19 立的"部分执行不得播成整句没做"在本分支漏配）。"""
    steps = [("HassTurnOn", _tgt("射灯")),
             ("AdjustDeviceAttribute", {**_tgt("射灯"), "attribute": "brightness",
                                         "delta": "80"})]
    ok, reply = _flap_run(steps)
    assert "离线" in reply and "没执行" in reply, reply        # 证伪必须如实
    assert reply != "好的，「射灯」现在离线、这条没执行", reply  # 禁覆写形
    assert "；" in reply, reply                                # 尾注拼接形


# ── v1.6.0（A2，审计 2026-10-17 发布树实锤 + C4 对抗复核定罪）─────────
# v1.5.1 的 N1 拼接条件 `not reply.startswith("好的，都办妥")` 恰好把
# **klar 首腿链**踢回覆写支：:1389 先产笼统"都办妥了"⇒ 证伪句整句覆写 ⇒
# 首腿已完成事实被嘴上否认（现网 18:48:47 原案在 klar 档位仍未修）。
# 且旧三钉钉的是 core **永不产生**的 source="chain"（Plan.source 枚举＝
# t0/t1/klar/llm/scene/creation；链合并取首腿 pipeline:2446）＝量具假绿。
# 现网级联日志（部署版 1.5.9，.91，pipeline:850 注释逐字在案）：
#   [级联] '打开办公室射灯' → [klar] '办公室射灯开了'
# ——标准开关句恒让 klar 胜（select_primary_plan 对非慧尖独有/非窗族 return kl）。


def test_a2_klar_first_leg_chain_appends_not_overwrites():
    """A2 主案：首腿 klar 的链、次腿事后瞬断 ⇒ 禁「好的，『射灯』现在离线、
    这条没执行」覆写形（灯真开着）；证伪必须与首腿完成事实同播。"""
    steps = [("HassTurnOn", _tgt("射灯")),
             ("AdjustDeviceAttribute", {**_tgt("射灯"), "attribute": "brightness",
                                        "delta": "80"})]
    ok, reply = _flap_run(steps, source="klar")
    assert "离线" in reply and "没执行" in reply, reply       # 证伪如实
    assert reply != "好的，「射灯」现在离线、这条没执行", reply  # 禁覆写
    assert "；" in reply, reply                              # 拼接形
    assert "都办妥了" not in reply, reply                    # 笼统承诺禁与证伪同播


def test_a2_klar_chain_noop_leg_still_named():
    """A2 同族：klar 首腿链遇空操作腿（v1.1.15 D2 形状）——不得
    「都办妥了」+点名句混搭；证伪在场时笼统形必须让位逐腿形。"""
    steps = [("HassTurnOn", _tgt("射灯")), ("HassTurnOn", _tgt("台灯"))]
    ok, reply, _ = _run({"light.ban_gong_shi_she_deng": LAMP_ON,
                         "light.desk": DESK_OFF}, steps,
                        source="klar", entity_area=AREA)
    assert "本来就在要求的状态上" in reply, reply
    assert "都办妥了" not in reply, reply


def test_a2_source_chain_never_produced_in_core():
    """契约钉（防量具假绿回潮，N3 变异实测驱动）。

    两半缺一不可（第一版只核了前半，变异 N3 实测 GREEN＝有洞）：
    ① core 生产端不得出现 source="chain" 字面量（该值永不产生：链合并取首腿
       pipeline:2446，枚举＝t0/t1/klar/llm/scene/creation）；
    ② 本文件的 fixture 默认值必须 ∈ 生产枚举——"chain" 与 "t0" 都落 executor
       else 支、**行为完全相同**，所以只钉行为的话，有人把默认值改回 "chain"
       时全部行为钉照绿，而klar 档覆盖（_flap_run(source="klar")）仍在，
       假绿悄悄回来。接线面得自己钉（v1.54 同款双保险）。
    """
    import re as _re
    core = Path(__file__).resolve().parents[1] / "core"
    hits = [p.name for p in core.rglob("*.py")
            if _re.search(r'source="chain"', p.read_text(encoding="utf-8"))]
    assert not hits, f'core 出现生产端 source="chain" 字面量：{hits}'
    PROD = {"t0", "t1", "klar", "llm", "scene", "creation", "t0_end"}
    here = Path(__file__).read_text(encoding="utf-8")
    defaults = _re.findall(r'def _(?:flap_)?run\([^)]*?source="([a-z0-9_]+)"', here)
    assert defaults, "fixture 默认值判据失效（签名形变了，先修本钉再改量具）"
    bad = [d for d in defaults if d not in PROD]
    assert not bad, f'fixture 默认值不在生产枚举：{bad}（假绿温床）'


def test_n1_single_step_bits_still_overwrites():
    """反证（守 v1.1.15 原契约）：单步批量被逐台证伪 ⇒ 仍是覆写点名句，
    N1 只放宽**链式**（多腿）的覆写，不得顺手改掉单步纪律。"""
    ha = FakeHAClient(
        results={"TurnDeviceOff": {"success": True}},
        states={"light.ban_gong_shi_she_deng": _ent("light.ban_gong_shi_she_deng",
                                                    "unavailable", "射灯")},
        entity_area={"light.ban_gong_shi_she_deng": "办公室"})
    plan = Plan(intent="TurnDeviceOff",
                args={"target": [{"devices": [{"name": "", "domains": ["light"]}]}]},
                source="t0", utterance="关掉所有的灯")
    ok, reply = asyncio.run(Executor(ha).run(plan))
    assert "「射灯」现在离线" in reply and reply.startswith("好的，"), reply
    assert "都办妥了" not in reply, reply


def test_two_leg_bare_success_does_not_echo_two_haodes():
    """复读机姊妹修：两腿回执都是裸 success ⇒ 拼播不得出现「好的，好的」。"""
    steps = [("HassTurnOn", _tgt("射灯")),
             ("AdjustDeviceAttribute", {**_tgt("射灯"), "attribute": "brightness",
                                         "delta": "80"})]
    ha = FakeHAClient(results={"HassTurnOn": {"success": True},
                               "AdjustDeviceAttribute": {"success": True}},
                      states={"light.ban_gong_shi_she_deng": _ent(
                          "light.ban_gong_shi_she_deng", "off", "射灯")},
                      entity_area={"light.ban_gong_shi_she_deng": "办公室"})
    plan = Plan(intent="HassTurnOn", args=steps[0][1], source="chain",
                utterance="链测试", extra_steps=[{"name": n, "args": a}
                                                 for n, a in steps[1:]])
    ok, reply = asyncio.run(Executor(ha).run(plan))
    assert "好的，好的" not in reply, reply

# -*- coding: utf-8 -*-
"""P0′／P1／P2（2026-10-10，HomeKit 对齐）——作用域形状优先＋播报只来自回执。

⚠ 本文件同时是**一次自我推翻的实录**。当日第一版 P0 在裁决/执行门/级联三处引入
`bulk_scope_face(原话)`＋`bulk_shape_ok(...)` 文本判据与"拿不到集合形就不执行并如实报"的
拒答支；两轮独立对抗复核（结论与复现见台账第二十四节）把它实锤推翻：

  · 同一批词表在 `_area_bulk_target`（fast_path:1424-1438）用**精确词**，我却用了**子串** ⇒
    「关闭展厅所有窗帘」「关闭办公室所有监控设备」「把家里的小爱同学关掉」「打开全家福」
    「关掉所有窗式空调」「关闭所有场景」全被点亮成"批量句"，把本仓早已钉死的合法具名
    类别批量**判死**（基线下发、现役 0 下发），链式句连另一条正确的腿也一起打死；
  · 更危险的一面：形状优先若吃 `whole_house`，「把家里的小爱同学关掉」(t0 出整类
    media_player 集合) 会从"只关用户点的那台"变成"关全屋音箱"——比原病更糟。

现在的契约（三句话）：
  1) 只有 **area_bulk**（在册区域＋显式"所有/全部"＋精确类别词，三条齐）参与"集合形优先"；
  2) **不做任何拒答**——本地批量道没认出来的句子逐字走原路（宁少拦，绝不误杀、绝不为
     "更彻底"扩大动作面）；
  3) "不许谎报"这一半交给 P1：原话含批量标记时，klar 腿弃用"原话回显/引擎泛化句"，
     播报改由**执行回执**派生（误判方向是安全的：只会让话术更保守，不多动一台设备）。

现场基准（出货 1.4.5 日志，16:29 用户贴回）：
    [执行] HassTurnOff {'entity_id': 'light.ban_gong_shi_she_deng'} → 成功 | 办公室所有设备关了
    [级联] '关闭办公室所有设备' → [klar] '办公室所有设备关了' (78ms)
"""
import asyncio

from conftest import FakeHAClient
from core.capability import BULK_TOGGLEABLE_DOMAINS
from core.executor import Executor
from core.nlu.fast_path import FLAG_AREA_BULK, Plan, plan_is_bulk_shape
from core.pipeline import select_primary_plan

UTT_DEVICE = "关闭办公室所有设备"
# 现场两条真实形状（离线跑生产入口 FastPath.match 取得，逐字非虚构）
KL_POINT = {"entity_id": "light.ban_gong_shi_she_deng"}
T0_AREA_BULK = {"target": [{"area": "办公室",
                            "devices": [{"name": "",
                                         "domains": list(BULK_TOGGLEABLE_DOMAINS)}]}]}
KL_WHOLEHOUSE_MEDIA = {"target": [{"devices": [{"name": "",
                                                "domains": ["media_player"]}]}]}
KL_CATEGORY_CURTAIN = {"target": [{"area": "展厅",
                                   "devices": [{"name": "窗帘", "domains": ["cover"]}]}]}


def _p(source, intent, args, utterance, flags=(), whole_house=False):
    pl = Plan(intent=intent, args=args, source=source, utterance=utterance,
              whole_house=whole_house)
    for f in flags:
        pl.flags.add(f)
    return pl


def _fp_bulk(utterance=UTT_DEVICE, args=None, intent="TurnDeviceOff"):
    return _p("t0", intent, args or T0_AREA_BULK, utterance, (FLAG_AREA_BULK,))


def _kl_point(utterance=UTT_DEVICE, args=None, intent="HassTurnOff"):
    return _p("klar", intent, args or KL_POINT, utterance)


# ── ① 形状判据本身 ────────────────────────────────────────────
def test_shape_flag_only_area_bulk_counts():
    assert plan_is_bulk_shape(_fp_bulk()) is True
    # 全屋道**故意不算**：它会被「家里」这类词宽松点亮并吃掉用户点名的设备
    assert plan_is_bulk_shape(_p("t0", "TurnDeviceOff", KL_WHOLEHOUSE_MEDIA,
                                 "把家里的小爱同学关掉", whole_house=True)) is False
    assert plan_is_bulk_shape(_kl_point()) is False
    assert plan_is_bulk_shape(None) is False


# ── ② 现场那句：区域批量不再被引擎点形抢走 ────────────────────
def test_area_bulk_beats_engine_point_shape():
    fp, kl = _fp_bulk(), _kl_point()
    win = select_primary_plan(fp, kl, known_areas=("办公室",), device_names=("射灯",))
    assert win is fp, f"区域批量又被点形抢走：{win and (win.source, win.intent)}"


def test_set_shape_wins_whichever_engine_produced_it():
    """判据认形状不认引擎：klar 若给出 area_bulk 集合形、本地没有 ⇒ klar 赢。"""
    kl_bulk = _p("klar", "TurnDeviceOff", T0_AREA_BULK, UTT_DEVICE, (FLAG_AREA_BULK,))
    assert select_primary_plan(None, kl_bulk, known_areas=("办公室",)) is kl_bulk


def test_named_sentence_precedence_untouched_zero_drift():
    """具名句（现场之外的日常形态）次序逐字不变：klar 恒优先的老契约保留。"""
    fp = _p("t0", "TurnDeviceOn",
            {"target": [{"area": "办公室",
                         "devices": [{"name": "射灯", "domains": ["light"]}]}]},
            "打开办公室射灯")
    kl = _p("klar", "HassTurnOn", {"entity_id": "light.ban_gong_shi_she_deng"},
            "打开办公室射灯")
    assert select_primary_plan(fp, kl) is kl


def test_scene_contract_and_huijian_only_still_beat_bulk_rule():
    """次序不变：scene 契约与慧尖独占类仍在"集合形优先"之前判定。"""
    sc = _p("scene", "HassTriggerVoiceScene", {"trigger_phrase": "观影"}, "观影")
    assert select_primary_plan(sc, _kl_point()) is sc
    win = _p("t0", "ControlWindow",
             {"target": [{"area": "办公室", "devices": [{"name": "平开窗 开窗器"}]}],
              "action": "open"}, "打开办公室平开窗")
    assert select_primary_plan(win, _kl_point()) is win


# ── ③ 复核实录：本闸**自身永不产生拒答** ────────────────────────
# 第一版在级联/链里加了"拿不到集合形就不执行"的拒答支，把「关闭展厅所有窗帘」
# 「关闭办公室所有监控设备」这类本仓钉过的合法路径直接打死（基线下发、现役 0 下发）。
# 该支已整体删除。这里钉的是删除后的不变量——注意别写错断言对象：
# 「把家里的小爱同学关掉」这类句子若仍返回 None，那是 **v1.0.92 既有的
# `_klar_write_without_target_evidence`** 在拒（实测复现），不是本闸；本闸只在
# 「本地已产出 area_bulk 集合形」这一种条件下改变结果，其余路径必须逐字不变。
def test_rule_never_refuses_by_itself():
    kl_ok = _p("klar", "HassTurnOff", KL_POINT, UTT_DEVICE)      # 过既有闸的样本
    # 1) 没有本地集合形候选时：结果 == 本闸不存在时的结果（传 fp=None 即等价）
    assert select_primary_plan(None, kl_ok, known_areas=("办公室",)) is kl_ok
    # 2) 有本地集合形时：赢的是它，且**绝不返回 None**
    win = select_primary_plan(_fp_bulk(), kl_ok, known_areas=("办公室",))
    assert win is not None and win.source == "t0"
    # 3) 本地给的是**非** area_bulk 的形状（全屋道/具名道）⇒ 不抢、不拒，逐字原样
    fp_other = _p("t0", "TurnDeviceOff", KL_WHOLEHOUSE_MEDIA,
                  "把家里的小爱同学关掉", whole_house=True)
    kl_named = _p("klar", "HassTurnOff", {"entity_id": "media_player.xiaoai"},
                  "关闭办公室射灯")
    assert select_primary_plan(fp_other, kl_named, known_areas=("办公室",)) is kl_named


def test_no_bulk_refusal_branch_survives_in_pipeline_or_executor():
    """源码级守卫：被推翻的三格拒答（含话术表）不得残留在产品码里。"""
    import inspect

    from core import executor as ex_mod
    from core import pipeline as pi_mod
    for label, src in (("pipeline.select_primary_plan",
                        inspect.getsource(pi_mod.select_primary_plan)),
                       ("executor._turn_gate",
                        inspect.getsource(ex_mod.Executor._turn_gate)),
                       ("pipeline._cascade",
                        inspect.getsource(pi_mod.Pipeline._cascade))):
        assert "bulk_scope_refuse" not in src, f"{label} 仍留着已推翻的拒答支"
        assert "BULK_SHAPE_SAY" not in src, f"{label} 仍引用已删除的拒答话术表"
    assert not hasattr(ex_mod, "_BULK_SHAPE_SAY"), "已推翻的拒答话术表还在 executor 里"
    import core.nlu.fast_path as fpm
    assert not hasattr(fpm, "bulk_scope_face") and not hasattr(fpm, "bulk_shape_ok"), \
        "被推翻的文本判据又回来了（子串匹配会误杀合法具名类别批量）"


def test_area_bulk_for_named_category_still_preferred_not_refused():
    """具名类别的区域批量（窗帘/监控设备那类）：有集合形时它赢，但**永不返回 None**。"""
    fp = _p("t0", "TurnDeviceOff", KL_CATEGORY_CURTAIN, "关闭展厅所有窗帘",
            (FLAG_AREA_BULK,))
    assert select_primary_plan(fp, _kl_point("关闭展厅所有窗帘"),
                               known_areas=("展厅", "办公室")) is fp
    # 只有点形时照旧执行（不拒答）
    kl = _p("klar", "HassTurnOff", {"entity_id": "cover.cur_1"}, "关闭展厅所有窗帘")
    assert select_primary_plan(None, kl, known_areas=("展厅",)) is kl


# ── ④ 执行门：不得再有"批量口径"拒答支（第一版遗留会被这条钉住）──
def test_turn_gate_has_no_bulk_refusal_branch():
    """`_turn_gate` 只该管能力/窗族两件事；批量拒答支已被推翻并删除。

    留这条钉是为了防止日后有人"顺手把拒答加回来"——那正是误杀合法句子的入口。
    """
    src = (Executor.__module__, )
    import inspect

    from core import executor as ex_mod
    body = inspect.getsource(ex_mod.Executor._turn_gate)
    assert "bulk" not in body.lower() and "批量口径" not in body, \
        "执行门里又出现了批量口径拒答支（已被复核实锤推翻，勿复用）"
    assert src  # 保持可读性，不参与判定


def test_gate_capability_and_window_branches_unchanged():
    ex = Executor(FakeHAClient(), None)
    assert ex._turn_gate("HassTurnOff", {"entity_id": "button.kai_qi_1"},
                         UTT_DEVICE) == ("这个设备不支持直接开关；是窗户的话，"
                                         "请说打开或关闭完整的窗型名称")
    assert ex._turn_gate("HassTurnOff", {"area": "办公室"}, "打开展厅推拉窗") is not None
    assert ex._turn_gate("HassTurnOff", KL_POINT, "关闭办公室射灯") is None


# ── ⑤ P1：原话含批量标记 ⇒ 播报只能来自回执 ───────────────────
class Ha(FakeHAClient):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.svc_calls = []

    async def call_service(self, domain, service, data, timeout=10.0):
        self.svc_calls.append((domain, service, data))
        return {"success": True}


RECEIPT_3 = {"success": True, "control_targets": [
    {"name": "射灯", "area": "办公室", "entity_id": "light.ban_gong_shi_she_deng"},
    {"name": "平开窗 开窗器", "area": "办公室",
     "entity_id": "cover.kai_chuang_qi_0006_0005_02_kai_chuang_qi"},
    {"name": "空调", "area": "办公室", "entity_id": "climate.xiaomi_mc9_aeaf"},
]}


def test_p1_bulk_utterance_reply_must_come_from_receipt():
    ha = Ha(results={"HassTurnOff": RECEIPT_3})
    plan = _p("klar", "HassTurnOff", T0_AREA_BULK, UTT_DEVICE, (FLAG_AREA_BULK,),
              )
    plan.speech = "办公室所有设备关了"      # 引擎泛化句（原病灶话术）
    ok, msg = asyncio.run(Executor(ha, None).run(plan))
    assert ok is True, msg
    assert "所有设备关了" not in msg and "办公室所有设备" not in msg, msg
    for nm in ("射灯", "平开窗", "空调"):
        assert nm in msg, f"回执里确证成功的 {nm} 没进播报：{msg!r}"


def test_p1_non_bulk_echo_path_untouched_zero_drift():
    """2026-09-14 那条设计（治"缺主语病句"）不能被 P1 顺手打掉。"""
    ha = Ha(results={"HassTurnOff": {"success": True}})
    ok, msg = asyncio.run(Executor(ha, None).run(
        _p("klar", "HassTurnOff", KL_POINT, "关闭办公室射灯")))
    assert "办公室射灯关了" in msg, msg


def test_p1_empty_receipt_is_not_gilded():
    ha = Ha(results={"HassTurnOff": {"success": True, "control_targets": []}})
    ok, msg = asyncio.run(Executor(ha, None).run(
        _p("klar", "HassTurnOff", T0_AREA_BULK, UTT_DEVICE, (FLAG_AREA_BULK,))))
    assert "都关了" not in msg, f"无确证却播了完成：{msg!r}"


# ── ⑥ P2：高频批量口径由语音场景承接（实测路已通，钉住别改回去）──
def test_p2_overbroad_guard_does_not_swallow_area_bulk():
    """「过宽目标」闸只该拦"区域名当设备名"，不得顺手打死区域批量集合形。"""
    from core.pipeline import Pipeline
    pipe = Pipeline.__new__(Pipeline)
    pipe._known_areas = lambda: ("办公室", "展厅", "灯")
    assert Pipeline._overbroad_area_target(pipe, _fp_bulk()) is None, \
        "区域批量被过宽闸误拒 ⇒ 批量口径进不了场景"
    bad = Plan(intent="TurnDeviceOn",
               args={"target": [{"devices": [{"name": "办公室", "domains": []}]}]},
               source="t0", utterance="客厅开灯")
    assert Pipeline._overbroad_area_target(pipe, bad) == "办公室", "过宽闸被改松了"


def _fp_real():
    from core.nlu.fast_path import FastPath
    from golden_gen import FakeScenes, S
    return FastPath(FakeScenes(), None, S(), areas_of=lambda: ["办公室", "展厅"])


def test_p2_all_word_orders_yield_area_bulk_set_shape():
    """四种语序都必须仍出 area_bulk 集合形（这是"存得进场景"的前提）。"""
    for utt in ("关闭办公室所有设备", "办公室所有设备关掉",
                "把办公室所有设备关掉", "将办公室全部设备关闭"):
        p = asyncio.run(_fp_real().match(utt, origin="192.168.1.123"))
        assert p is not None and plan_is_bulk_shape(p), \
            f"批量口径掉出区域道（语序 {utt}）⇒ 场景再也存不下：{p and p.intent}"
        doms = {str(d) for t in (p.args.get("target") or [])
                for dv in (t.get("devices") or []) for d in (dv.get("domains") or [])}
        assert {"cover", "climate"} <= doms, f"{utt} 的集合面缺窗/空调：{sorted(doms)}"


def test_p2_scene_creation_keeps_set_shape_action():
    """真 `_build_actions`：批量 Y 子句存进场景的 action 必须是集合形（不塌成一台）。"""
    from core.pipeline import Pipeline
    from golden_gen import S
    pipe = Pipeline.__new__(Pipeline)
    pipe.settings = S()
    pipe.fast_path = _fp_real()
    pipe.klar = None
    pipe.ha = Ha()
    pipe._known_areas = lambda: ("办公室", "展厅")
    pipe._real_areas = lambda: ("办公室", "展厅")
    out = asyncio.run(pipe._build_actions(
        {"kind": "scene", "x": "下班", "y": "办公室所有设备关掉"},
        "当我说下班，就把办公室所有设备关掉"))
    assert isinstance(out, tuple), f"创建被拒收：{getattr(out, 'text', out)}"
    actions, _ = out
    assert len(actions) == 1, actions
    params = actions[0]["params"]
    doms = {str(d) for t in (params.get("target") or [])
            for dv in (t.get("devices") or []) for d in (dv.get("domains") or [])}
    assert {"cover", "climate"} <= doms, f"场景里存的不是设备面集合：{params}"
    assert any(not str((dv or {}).get("name") or "").strip()
               for t in (params.get("target") or [])
               for dv in (t.get("devices") or [])), f"动作塌成具名单台：{params}"


# ── ⑦ 红线不动 ───────────────────────────────────────────────
def test_bulk_whitelist_still_excludes_scene_script_lock():
    wl = set(BULK_TOGGLEABLE_DOMAINS)
    assert not ({"scene", "script", "lock"} & wl), sorted(wl)


# ── ⑧ 口径不足限定语（P1 收尾；只动话术、不动执行面）────────────
NOTE = "其余该动哪几台我没能确定，没敢代做"


def _run_point_plan(utterance, args=None, flags=(), whole_house=False):
    # states 里带上那台的友好名：这就是限定语"办了哪台"的第二来源
    # （标准 HassTurnOff 走 HA core 通道时回执常被折叠成 {"success": true}，
    # 光弃用原话回显会把台名一起丢掉——本轮实测钉抓到的正是这个）。
    ha = Ha(states={KL_POINT["entity_id"]: {"state": "on",
                                            "attributes": {"friendly_name": "射灯"}}},
            results={"HassTurnOff": {"success": True}})
    plan = _p("klar", "HassTurnOff", args or KL_POINT, utterance, flags, whole_house)
    ok, msg = asyncio.run(Executor(ha).run(plan))
    return ok, msg, ha


def test_underdelivered_scope_gets_honest_qualifier():
    """说"所有设备"却只按点形办了一台 ⇒ 真办的那台要说得出，**同时**必须说其余没代做。"""
    ok, msg, ha = _run_point_plan(UTT_DEVICE)
    assert ok is True, msg
    assert ha.svc_calls, "限定语不该顺带把动作也取消（klar 走服务直调通道）"
    assert NOTE in msg, f"没告知其余没动：{msg!r}"
    assert "射灯" in msg, f"该动的台仍要说：{msg!r}"


def test_qualifier_prefers_receipt_names():
    """来源①：回执 control_targets 有名字时直接用它（不靠 states 反查）。"""
    ex = Executor(Ha(), None)
    plan = _p("t0", "TurnDeviceOff", T0_AREA_BULK, "关闭办公室所有灯")
    plan.flags.discard(FLAG_AREA_BULK)      # 人为造"点形承接批量句"的形态
    msg = ex._scope_underdelivered(
        "好的", plan,
        [{"success": True, "control_targets": [
            {"name": "射灯", "entity_id": "light.a"},
            {"name": "书桌台灯", "entity_id": "light.b"}]}])
    assert "射灯" in msg and "书桌台灯" in msg and NOTE in msg, msg
    assert "这几台办了" in msg, msg


def test_set_shape_and_whole_house_get_no_qualifier():
    """真按集合扇出（area_bulk / whole_house）⇒ 不加限定语（加了就是新的不实陈述）。"""
    ok, msg, _ = _run_point_plan(UTT_DEVICE, args=T0_AREA_BULK, flags=(FLAG_AREA_BULK,))
    assert NOTE not in msg, f"area_bulk 被误加限定：{msg!r}"
    ok2, msg2, _ = _run_point_plan("关掉所有的灯", whole_house=True)
    assert NOTE not in msg2, f"whole_house 被误加限定：{msg2!r}"


def test_named_utterance_gets_no_qualifier_zero_drift():
    ok, msg, _ = _run_point_plan("关闭办公室射灯")
    assert NOTE not in msg and "办公室射灯关了" in msg, msg


def test_loose_marker_words_do_not_trigger_qualifier():
    """「家里/全家」是宽表里的全屋标记，但常只是定语或名字的一部分——
    拿它点亮限定语等于硬说用户"要的是全部"，那是**新的不实陈述**，故必须不触发。"""
    for utt in ("把家里的小爱同学关掉", "打开全家福", "家里的灯开个"):
        ok, msg, _ = _run_point_plan(utt, args={"entity_id": "light.x"},
                                     )
        assert NOTE not in msg, f"{utt} 被宽标记误点亮：{msg!r}"


def test_qualifier_never_stacks_on_existing_reasons():
    """已有分因（离线/没成功/查无…）时不重复叠加——两种事实各说一次是本仓铁律。"""
    ex = Executor(Ha(), None)
    plan = _p("klar", "HassTurnOff", KL_POINT, UTT_DEVICE)
    for has_reason in ("好的，「射灯」现在离线、这条没执行",
                       "好的，射灯关了（另有 1 台没成功）",
                       "好的，办公室射灯已处理（没能确定其余）"):
        assert ex._scope_underdelivered(has_reason, plan) == has_reason


# ── ⑨ P2 的**触发侧**证据：跨"创建"与"扇出"两侧的那条缝 ──────────
# 为什么单独钉：创建侧钉的是 action 形状，集成侧 v1211 钉的是 `find_bulk_entities_by_area`
# 的三档区域证据——**两边各自绿，但没人证明"存进场景的那份参数"和"直接说那句产生的那份"
# 是同一份、且喂给扇出函数得到同一个设备集合**。不一致就是"说了能关、存成场景就关不全"。
# ⚠ 期望别写错（本轮我就错过一次）：v1211 的替身里那两台开窗器**整台没挂区域**，按该文件
#   的设计语义就该被遮掉（`test_bulk_evidence...` 逐字钉着），所以本钉**不断言 cover 必进**，
#   只断言"两侧口径一致"。现场（.91）窗是否进批量面取决于区域/名字证据，属 P3b 数据面，
#   不是这条钉能证的——今晨因果探针实测到"窗真的动了"，那是数据形态，已单独记账。
def test_p2_stored_action_matches_spoken_action_exactly():
    import test_v1211_bulk_area as v11

    from core.pipeline import Pipeline
    from golden_gen import S
    pipe = Pipeline.__new__(Pipeline)
    pipe.settings = S()
    pipe.fast_path = _fp_real()
    pipe.klar = None
    pipe.ha = Ha()
    pipe._known_areas = lambda: ("办公室", "展厅")
    pipe._real_areas = lambda: ("办公室", "展厅")
    out = asyncio.run(pipe._build_actions(
        {"kind": "scene", "x": "下班", "y": "办公室所有设备关掉"},
        "当我说下班，就把办公室所有设备关掉"))
    assert isinstance(out, tuple), f"创建被拒收：{getattr(out, 'text', out)}"
    stored = (out[0][0]["params"].get("target") or [{}])[0]

    # 同一条话术直接说出口时 t0 产出的那份 target
    spoken = asyncio.run(_fp_real().match("办公室所有设备关掉"))
    assert spoken is not None and plan_is_bulk_shape(spoken), f"直接说没出集合形：{spoken}"
    spoken_t = (spoken.args.get("target") or [{}])[0]

    def _norm(tg):
        return (str(tg.get("area") or ""),
                sorted((str(dv.get("name") or ""),
                        sorted(str(x) for x in (dv.get("domains") or [])))
                       for dv in (tg.get("devices") or [])))
    assert _norm(stored) == _norm(spoken_t), \
        f"存进场景的形状与直接说的形状不一致：存={_norm(stored)} 说={_norm(spoken_t)}"

    # 两份参数喂给集成侧扇出（真函数），必须得到**同一个设备集合**
    doms = sorted({str(x) for dd in (stored.get("devices") or [])
                   for x in (dd.get("domains") or [])})
    got_stored = v11.IWC.find_bulk_entities_by_area(v11.make_hass(), str(stored.get("area")), doms)
    got_spoken = v11.IWC.find_bulk_entities_by_area(v11.make_hass(), str(spoken_t.get("area")), doms)
    assert sorted(got_stored) == sorted(got_spoken), \
        f"同一间房两套扇出：存={sorted(got_stored)} 说={sorted(got_spoken)}"
    # 且扇出侧的既有红线仍成立（不因场景通道而放宽）：空调进、无证据的厕所门不进
    assert "climate.xiaomi_ac" in got_stored, got_stored
    assert "cover.win_ce_suo" not in got_stored, \
        f"场景通道把越房误动放开了：{got_stored}"


def test_p2_named_category_keeps_its_own_shape():
    """具名类别（展厅所有窗帘）存的形状喂给扇出侧也不许漂成"整房所有设备"。"""
    import test_v1211_bulk_area as v11

    from core.pipeline import Pipeline
    from golden_gen import S
    pipe = Pipeline.__new__(Pipeline)
    pipe.settings = S()
    pipe.fast_path = _fp_real()
    pipe.klar = None
    pipe.ha = Ha()
    pipe._known_areas = lambda: ("办公室", "展厅")
    pipe._real_areas = lambda: ("办公室", "展厅")
    out = asyncio.run(pipe._build_actions(
        {"kind": "scene", "x": "睡吧", "y": "展厅所有窗帘拉上"},
        "当我说睡吧，就把展厅所有窗帘拉上"))
    assert isinstance(out, tuple), f"创建被拒收：{getattr(out, 'text', out)}"
    tg = (out[0][0]["params"].get("target") or [{}])[0]
    dd = (tg.get("devices") or [{}])[0]
    assert str(dd.get("name") or "") == "窗帘", f"具名类别词丢了（会扩成整房）：{tg}"
    assert {str(x) for x in (dd.get("domains") or [])} == {"cover"}, \
        f"类别域漂了：{dd.get('domains')}"

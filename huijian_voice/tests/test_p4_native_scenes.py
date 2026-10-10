# -*- coding: utf-8 -*-
"""P4′（2026-10-10）HA 原生场景可语音执行——**复核后重做版**。

症状（.91 现网日志，用户贴回）：
```
[级联] '观影场景'     → [fallback] '这句话我还不会' (42ms)
[级联] '执行观影场景'  → [fallback] '这句话我还不会' (25ms)
```
根因确实是数据源：`SceneCache` 只拉 `HassListVoiceScenes`（加载项自有语音库），
HA 原生 `scene.guan_ying`（友好名「观影」）从不在表里。

但第一版（当日 17:5x）被独立对抗复核**复现推翻**，四类危害（本文件逐条钉成永久回归）：
  C1 语音库前缀契约被抢：触发词「观影」+ 原生场景「观影」时，「观影场景」走原生 ⇒
     用户被告知的"说 X 就 Y"静默改指另一个场景。
  C2 抢设备口令：带标记形**不看 blocked**，且动词＋裸名与设备口令同形 ⇒
     装有 vacuum「扫地机」时「启动扫地机」变成触发场景，机器没动还播「好的，扫地机已执行」。
  C3 二次按名字猜：marked 形无声盖掉另一条场景的**整名**（{观影:a, 观影场景:b} 说「观影场景」）。
  C5 blocked 有结构性盲区：`targets._dyn_vocab._name_tokens` 只留 **2~8 个纯汉字**
     （`_name_tokens('Fan')=[]`），九字以上的场景名（如「关闭办公室所有设备」）进不了 guard ⇒
     批量口令被同名场景劫走。当日新加的 hostile-name 钉之所以绿，是因为它挑的名字都 ≤8 字。

P4′ 的三条硬边界：① **句子必须含字面「场景」**（裸名通道整体取消，动词也只与"X场景"组合）；
② **语音库整条 `check()` 先判**（等值与最长前缀都算认领）；③ **在装设备整名来自同一份
states 快照**（`Scene._occupied`，无字符/长度盲区），命中即让路；歧义一律不接管。
执行面沿用第一版（它是对的那半）：`call_service("scene","turn_on",{entity_id})`，
绝不回集成 `HassTriggerVoiceScene` 通道。
"""
import asyncio

from core.capability import BULK_TOGGLEABLE_DOMAINS
from core.executor import Executor
from core.nlu.scenes import SceneCache

SCENE_EID = "scene.guan_ying"


class Ha:
    """只替 HAClient 三个口。P4′ 起原生索引读的是 **`_states` 快照**（不 await），
    所以 `states()` 调用次数就是"有没有多发 HTTP"的直接判据。"""

    def __init__(self, entities=None, voice=None, load_ok=True):
        self._states = dict(entities or {})
        self._voice = voice or []
        self.load_ok = load_ok
        self.intent_calls = []
        self.svc_calls = []
        self.states_calls = 0

    async def handle_intent(self, name, data, timeout=5.0):
        self.intent_calls.append((name, data))
        if name == "HassListVoiceScenes":
            if not self.load_ok:
                return {"success": False, "error": "integration offline"}
            return {"success": True, "scenes": self._voice}
        return {"success": True}

    async def call_service(self, domain, service, data, timeout=10.0):
        self.svc_calls.append((domain, service, data))
        return {"success": True}

    async def states(self):                     # 真客户端会打 GET；测试里只用来计数
        self.states_calls += 1
        return dict(self._states)


def ent(eid, fn, state="on"):
    return eid, {"state": state, "attributes": {"friendly_name": fn}}


def loaded(entities, voice=None, load_ok=True):
    ha = Ha(entities=entities, voice=voice, load_ok=load_ok)
    sc = SceneCache(ha)
    asyncio.run(sc.refresh(force=True))
    return sc, ha


class Settings:
    def get(self, k, default=None):
        return {"nlu.textcnn_enabled": False, "nlu.enabled": True,
                "spatial.satellite_areas": {}, "nlu.corrections_extra": []}.get(k, default)


def _fp(sc, areas=("办公室",)):
    from core.nlu.fast_path import FastPath
    return FastPath(sc, None, Settings(), areas_of=lambda: list(areas))


# ── ① 数据源：只读快照，零额外 HTTP ────────────────────────────
def test_native_index_from_states_snapshot_without_any_request():
    sc, ha = loaded([ent(SCENE_EID, "观影"), ent("light.a", "射灯"),
                     ent("switch.b", "Fan")])
    assert sc.native_scenes() == {"观影": [SCENE_EID]}, sc.native_scenes()
    assert ha.states_calls == 0, "P4′ 声称零额外 HTTP，却调了会打 GET 的 states()"
    # 在装设备整名同快照产出（英文名也必须在——这是 C5 盲区的根）
    assert sc.occupied_device_names() == {"射灯", "Fan"}, sc.occupied_device_names()


def test_client_without_states_api_keeps_old_table_and_does_not_crash():
    """旧客户端/替身**根本没有 states()** ⇒ 原生道安静不启用，不抛、不清旧表。

    ⚠ 本钉原名 `…sends_nothing`，钉的是"快照空时绝不发请求"。10-11 真机双臂（慢链路）
    证明那个前提错：不兜这次请求，原生场景就静默不可用（#20/#21 落 fallback），
    属于本仓定义的"回执成功而什么都没发生"变体。新契约＝**快照优先，空则兜一次带上限的
    states()**（见本文件 ⑧ 节两条钉），所以这里只留"客户端没有 states() 这个口"的退化面。
    """
    class NoStatesHa:
        def __init__(self):
            self._states = {}
        async def handle_intent(self, name, data, timeout=5.0):
            return {"success": True, "scenes": []}
        # 故意不提供 states()

    sc = SceneCache(NoStatesHa())
    sc._native = {"旧": ["scene.old"]}
    assert asyncio.run(sc.refresh(force=True)) is True      # 语音库语义不因原生而变
    assert sc.native_scenes() == {"旧": ["scene.old"]}, "缺口时把旧表清了"



def test_voice_library_failure_does_not_break_native():
    sc, ha = loaded([ent(SCENE_EID, "观影")], load_ok=False)
    assert sc.native_scenes() == {"观影": [SCENE_EID]}
    kind, payload = sc.check_native("观影场景")
    assert kind == "hit" and payload["entity_id"] == SCENE_EID, (kind, payload)


def test_unavailable_scene_is_not_indexed():
    sc, _ = loaded([ent(SCENE_EID, "观影", state="unavailable")])
    assert sc.native_scenes() == {}, sc.native_scenes()


# ── ② C1：语音库整条认领（等值**与最长前缀**）都优先 ──────────
def test_voice_prefix_contract_wins_over_same_named_native_scene():
    """触发词「观影」时，「观影场景」仍必须走 `HassTriggerVoiceScene`（v1.1.27 前缀契约）。"""
    sc, _ = loaded([ent(SCENE_EID, "观影")],
                   voice=[{"trigger_phrase": "观影", "name": "观影"}])
    assert sc.check("观影场景") == "观影", "语音库自己都没认领，测试前提不成立"
    p = asyncio.run(_fp(sc).match("观影场景"))
    assert p is not None and p.intent == "HassTriggerVoiceScene", \
        f"原生场景抢了用户被告知的契约：{p and (p.intent, p.args)}"
    p2 = asyncio.run(_fp(sc).match("观影"))
    assert p2 is not None and p2.intent == "HassTriggerVoiceScene", p2 and p2.intent


# ── ③ C2：没有字面「场景」就与本道无关（裸名/动词形整体封死）───
def test_device_commands_are_never_hijacked_without_scene_marker():
    """装有逐字叫「扫地机」的 vacuum + 同名场景：三条设备口令都不得触发场景。"""
    sc, _ = loaded([ent("vacuum.sao_di_ji", "扫地机"), ent("scene.vac", "扫地机")])
    fp = _fp(sc)
    for utt in ("启动扫地机", "开始扫地机", "切换扫地机", "扫地机", "打开扫地机"):
        p = asyncio.run(fp.match(utt))
        assert p is None or p.intent != "TriggerHaScene", \
            f"设备口令被场景抢走：{utt} → {p and (p.intent, p.args)}"
    # 用户真说了"场景"才接管
    p = asyncio.run(fp.match("扫地机场景"))
    assert p is None, "「扫地机场景」与在装设备「扫地机」撞名，仍必须让路"


def test_bare_name_channel_is_gone_even_without_conflict():
    """裸名通道已取消：家里没有叫「观影」的设备，也只认带标记的形。"""
    sc, _ = loaded([ent(SCENE_EID, "观影")])
    fp = _fp(sc)
    assert asyncio.run(fp.match("观影")) is None, "裸名通道没关干净"
    for utt in ("观影场景", "观影的场景", "执行观影场景", "运行观影场景", "切换观影场景"):
        p = asyncio.run(fp.match(utt))
        assert p is not None and p.intent == "TriggerHaScene", (utt, p and p.intent)
        assert p.args["entity_id"] == SCENE_EID and p.source == "scene", p.args


def test_no_prefix_swallowing_of_longer_sentences():
    sc, _ = loaded([ent(SCENE_EID, "观影")])
    fp = _fp(sc)
    for utt in ("观影场景里加一盏灯", "观影模式调亮一点", "把观影灯打开"):
        p = asyncio.run(fp.match(utt))
        assert p is None or p.intent != "TriggerHaScene", (utt, p and p.intent)


# ── ④ C5：在装设备整名让路（含 9 字以上的口令形场景名）────────
def test_long_scene_name_cannot_hijack_bulk_command():
    """场景名取成「关闭办公室所有设备」（9 字，旧 blocked 词表看不见它）⇒ 批量口令照旧执行。"""
    sc, _ = loaded([ent("light.ban_gong_shi_she_deng", "射灯"),
                    ent("cover.kai_chuang_qi", "平开窗 开窗器"),
                    ent("climate.mc9", "空调"),
                    ent("scene.hijack", "关闭办公室所有设备")])
    p = asyncio.run(_fp(sc).match("关闭办公室所有设备"))
    assert p is not None and p.intent == "TurnDeviceOff" and p.source == "t0", \
        f"批量口令被同名场景劫走：{p and (p.intent, p.args)}"


def test_english_named_device_still_shields_scene():
    """旧 guard 的 `2~8 纯汉字` 盲区：设备叫 'Fan'、场景也叫 'Fan' 时不得抢。"""
    sc, _ = loaded([ent("fan.desk", "Fan"), ent("scene.fan", "Fan")])
    p = asyncio.run(_fp(sc).match("Fan场景"))
    assert p is None or p.intent != "TriggerHaScene", \
        f"英文名设备仍被抢：{p and (p.intent, p.args)}"


# ── ⑤ C3：marked 形与另一条场景整名相撞 ⇒ 不猜 ────────────────
def test_marked_form_vs_other_scene_exact_name_is_ambiguous():
    sc, _ = loaded([ent("scene.a", "观影"), ent("scene.b", "观影场景")])
    kind, payload = sc.check_native("观影场景")
    assert kind == "ambiguous", f"该不猜却挑了一条：{kind} {payload}"
    p = asyncio.run(_fp(sc).match("观影场景"))
    assert p is None or p.intent != "TriggerHaScene", p and p.args


def test_duplicate_scene_names_are_never_guessed():
    sc, _ = loaded([ent("scene.a", "观影"), ent("scene.b", "观影")])
    assert sc.check_native("观影场景")[0] == "ambiguous"


# ── ⑥ 执行面：只有服务直调，且缺据不播成功 ────────────────────
from core.nlu.fast_path import Plan                      # noqa: E402


def test_execution_is_scene_turn_on_with_entity_id_only():
    ha = Ha(entities=[ent(SCENE_EID, "观影")])
    ok, speech = asyncio.run(Executor(ha).run(
        Plan(intent="TriggerHaScene", args={"entity_id": SCENE_EID, "name": "观影"},
             source="scene", utterance="观影场景")))
    assert ok is True, speech
    assert ha.svc_calls == [("scene", "turn_on", {"entity_id": SCENE_EID})], ha.svc_calls
    assert ha.intent_calls == [], f"原生场景仍被塞给集成意图通道：{ha.intent_calls}"
    assert "观影" in speech and "已执行" in speech, speech


def test_missing_entity_id_fails_without_claiming_success():
    ha = Ha()
    ok, speech = asyncio.run(Executor(ha).run(
        Plan(intent="TriggerHaScene", args={"name": "观影"}, source="scene",
             utterance="观影场景")))
    assert ok is False, speech
    assert ha.svc_calls == []
    assert "已执行" not in speech, f"没 entity_id 却播成功：{speech}"


# ── ⑦ 红线与热路径 ───────────────────────────────────────────
def test_scene_domain_stays_out_of_bulk_whitelist():
    assert "scene" not in BULK_TOGGLEABLE_DOMAINS and "script" not in BULK_TOGGLEABLE_DOMAINS


def test_steady_state_native_match_is_pure_memory():
    sc, ha = loaded([ent(SCENE_EID, "观影"), ent("scene.b", "回家")])
    base_states, base_intents = ha.states_calls, len(ha.intent_calls)
    assert sc.native_scenes(), "索引没建起来，后面断言就是空转"
    for _ in range(80):
        sc.check_native("观影场景")
        sc.check_native("执行回家场景")
    assert ha.states_calls == base_states, "匹配里偷偷打 GET"
    assert len(ha.intent_calls) == base_intents, "匹配里偷偷拉语音场景库"


# ── ⑦ 目标 P4④：同名多个 ⇒ **clarify 列候选**（不是"默默不接管"）──────────
# 这格此前只做到"不接管＋留痕"，用户听到的是泛化兜底「这句话我还不会」——既不知道
# 家里有重名场景，也不知道该怎么修。现在发射 `ClarifyNativeScene` 哨兵，由级联当场收口。
def test_ambiguous_payload_lists_candidates():
    sc, _ = loaded([ent("scene.a", "观影"), ent("scene.b", "观影")])
    kind, payload = sc.check_native("观影场景")
    assert kind == "ambiguous", (kind, payload)
    assert payload["reason"] == "duplicate_name", payload
    assert payload["names"] == ["观影"] and sorted(payload["entity_ids"]) == ["scene.a", "scene.b"], payload
    sc2, _ = loaded([ent("scene.a", "观影"), ent("scene.b", "观影场景")])
    kind2, p2 = sc2.check_native("观影场景")
    assert kind2 == "ambiguous" and p2["reason"] == "overlap", (kind2, p2)
    assert p2["names"] == ["观影", "观影场景"], p2


def test_last_scene_lane_emits_clarify_plan():
    from core.nlu.fast_path import SCENE_CLARIFY_INTENT
    sc, _ = loaded([ent("scene.a", "观影"), ent("scene.b", "观影")])
    fp = _fp(sc)
    p = asyncio.run(fp._native_scene_plan("观影场景", ["t"], allow_clarify=True))
    assert p is not None and p.intent == SCENE_CLARIFY_INTENT and p.source == "scene", p
    assert p.args["names"] == ["观影"] and sorted(p.args["entity_ids"]) == ["scene.a", "scene.b"]
    # 前两位（契约最高优先）**不出** clarify：那句还可能是设备口令，不许抢对话回合
    assert asyncio.run(fp._native_scene_plan("观影场景", ["t"])) is None


def test_clarify_wording_names_scenes_and_tells_fix():
    """话术三条：说清是**场景**重名、这次没动、以及唯一有效的下一步（去 HA 改名字）。"""
    from core.nlu.fast_path import SCENE_CLARIFY_INTENT
    from core.pipeline import Pipeline
    plan = Plan(intent=SCENE_CLARIFY_INTENT,
                args={"reason": "duplicate_name", "names": ["观影"],
                      "entity_ids": ["scene.a", "scene.b"]},
                source="scene", utterance="观影场景")
    say = Pipeline._scene_clarify_say(plan)
    assert "场景都叫「观影」" in say and "这次没动" in say, say
    assert "独一无二的名字" in say, f"没给可照着做的修法：{say}"
    assert "设备名字相近" not in say, f"沿用设备模板＝不实陈述：{say}"
    say2 = Pipeline._scene_clarify_say(Plan(
        intent=SCENE_CLARIFY_INTENT,
        args={"reason": "overlap", "names": ["观影", "观影场景"], "entity_ids": ["a", "b"]},
        source="scene", utterance="观影场景"))
    assert "「观影」" in say2 and "「观影场景」" in say2 and "没替你挑" in say2, say2


def test_occupied_device_name_beats_scene_ambiguity():
    """两台场景重名、但家里**真有**一台设备就叫这个名 ⇒ 让路给设备，不占对话回合澄清。"""
    sc, _ = loaded([ent("switch.view", "观影"), ent("scene.a", "观影"), ent("scene.b", "观影")])
    assert sc.check_native("观影场景")[0] == "none", \
        f"设备在位的名字被场景歧义抢走：{sc.check_native('观影场景')}"
# ── ⑧ 快照未填时的兜底（10-11 真机双臂抓出的回归：#20/#21 落 fallback）──────
# 只读 `ha._states` 等于赌"别人已经把快照填好了"。慢链路（Tailscale）下首轮刷新时
# 快照确实还是空的 ⇒ 原生场景静默不可用，播「这句话我还不会」——这类"功能看运气"
# 就是本仓定义的假成功变体。修法：快照优先，取不到兜一次 `states()`，但套 1s 上限。
class _LazyHa:
    """`_states` 一直空，只有 await states() 才给数据（＝冷启动真实形态）。"""

    def __init__(self, entities, delay=0.0, raise_err=False):
        self._states = {}
        self._data = dict(entities)
        self.delay = delay
        self.raise_err = raise_err
        self.calls = 0

    async def handle_intent(self, name, data, timeout=5.0):
        return {"success": True, "scenes": []}

    async def states(self):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raise_err:
            raise RuntimeError("boom")
        return dict(self._data)


def test_main_refresh_never_waits_but_self_heal_builds_table():
    """新契约两半：**主刷新不兜底**（不往语音路径塞网络等待）＋ **后台自愈会建表**。

    必须在一个**运行中的事件循环**里验：`native_soon()` 用 `get_running_loop()` 调度，
    同步上下文里调它只会走"无循环静默跳过"分支（生产永远在循环内，测试照该形态写）。
    第一版这条钉断言 `refresh()` 自己建表，等于允许语音首句去等 `/api/states`；
    真机慢链路下正是它把首句拖了 1.2s —— 我自己补的性质钉抓到的。
    """
    ha = _LazyHa({SCENE_EID: {"state": "on",
                              "attributes": {"friendly_name": "观影"}},
                  "light.a": {"state": "on", "attributes": {"friendly_name": "射灯"}}})
    sc = SceneCache(ha)

    async def scenario():
        ok = await sc.refresh(force=True)                     # 主刷新：只读快照
        assert ok is True, "语音库成败语义被原生改动"
        assert sc.native_scenes() == {}, "主刷新不该兜底网络，却建了表"
        assert ha.calls == 0, f"主刷新打了 {ha.calls} 次 states()（会拖住语音路径）"
        sc.native_soon()                                      # 自愈：后台单飞兜一次
        assert sc._native_bg is not None, "native_soon 没调度后台任务 ⇒ 表永远空着"
        await sc._native_bg
        return sc.native_scenes(), sc.occupied_device_names(), ha.calls

    native, occupied, calls = asyncio.run(scenario())
    assert calls == 1, f"自愈兜底次数不对：{calls}"
    assert native == {"观影": [SCENE_EID]}, native
    assert occupied == {"射灯"}, occupied
    before = calls
    assert sc.check_native("观影场景")[0] == "hit"
    assert ha.calls == before, "匹配阶段又去打 states()"


def test_slow_states_is_given_up_within_its_bound():
    """/states 慢 ⇒ 兜底**有上限**（现 2.0s）、本轮放弃、旧表保留；不挂死调用方。

    断言钉的是性质而不是秒数：`spent < 2.6` 证明 wait_for 真在截（不是让它跑完 2s+RTT）；
    上限若被人改动，改这里要连同 scenes.py 的注释一起改，别只把测试放宽。"""
    ha = _LazyHa({SCENE_EID: {"state": "on",
                              "attributes": {"friendly_name": "观影"}}}, delay=2.0)
    sc = SceneCache(ha)
    import time
    t0 = time.monotonic()
    asyncio.run(sc._refresh_native())
    spent = time.monotonic() - t0
    assert spent < 2.6, f"兜底没被上限截住，实测 {spent:.2f}s（会挂死调用方）"
    assert sc.native_scenes() == {}, sc.native_scenes()
    # 异常也一样：留旧表、不抛
    ha2 = _LazyHa({}, raise_err=True)
    sc2 = SceneCache(ha2)
    sc2._native = {"旧": ["scene.old"]}
    assert asyncio.run(sc2._refresh_native()) is False
    assert sc2.native_scenes() == {"旧": ["scene.old"]}, "兜底失败把旧表清了"
def test_voice_path_never_waits_for_slow_states():
    """性质钉：慢 /states 绝不能把语音首句挂住——原生表空时只**调度后台自愈**。"""
    import time

    class SlowHa(_LazyHa):
        pass

    ha = SlowHa({SCENE_EID: {"state": "on",
                             "attributes": {"friendly_name": "观影"}}}, delay=1.2)
    sc = SceneCache(ha)
    fp = _fp(sc)
    t0 = time.monotonic()
    p = asyncio.run(fp.match("观影场景"))
    spent = time.monotonic() - t0
    assert spent < 0.5, f"语音路径被慢 states 挂住了：{spent:.2f}s"
    assert p is None or p.intent != "TriggerHaScene",         f"表还没建好却声称触发（可能拿空表猜）：{p and p.args}"
    assert sc._native_bg is not None, "没调度后台自愈 ⇒ 原生场景会永久静默不可用"

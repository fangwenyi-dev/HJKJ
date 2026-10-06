# -*- coding: utf-8 -*-
"""外部审计 6 条 P0/P1 的复现钉（v1.1.38 工作树批）。

流程纪律：每条指控先由我本机用**现役真函数**复现（`_goldtest/repro_audit_1138_p0p1.py`），
复现成立的才修；6 条全部成立，逐条落钉。每条都配**反向臂**（防把修复做成新误杀）。

| # | 级别 | 复现到的现场 | 收口位置 |
|---|---|---|---|
| 1 | P0 | `_cover_wordorder("别把窗帘关上")→"关闭别把窗帘"`，否定闸吃改写字 ⇒ `TurnDeviceOff(窗帘)` 真下发 | fast_path：否定句不做帘窗语序改写 |
| 2 | P1 | 合链只摊平 `plans[1:]`，首腿自带 extra_steps 被丢 ⇒ 少一台却播「都办妥了」 | pipeline._chain_decide：按腿序摊平「腿本体+该腿自带步」 |
| 3 | P1 | `refresh_states` 从不抛（失败只置 `_states_ok=False` 并留旧快照）⇒ `except` 死码、拿陈旧真值判 noop | executor：两处确证读改查具名 `states_stale()` |
| 4 | P1 | turn 转发窗控只取 control_targets，message/partial_error 丢 ⇒ 三条消费链全静默 | window 侧补 `partial_error`＋turn 侧带上该字段 |
| 5 | P1 | 「把空调调高到26度」被相对档吃成 `+26` ⇒ clamp 到量程顶 | 温度绝对档补「调高到/调低到/…」且排在相对档前 |
| 6 | P1 | 「退下。」不匹配裸正则 ⇒ 停麦意图丢失（注释承诺由 is_end_dialogue 兜，只在 nlu-off 支兑现） | match() 改判 `is_end_dialogue` |
"""
import asyncio
import sys
import time
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.nlu import targets as T                                   # noqa: E402
from core.nlu.fast_path import (FastPath, Plan, _cover_wordorder,    # noqa: E402
                                is_negation_imperative, _scan_delta,
                                is_end_dialogue)
from test_experience_batch import (HA, NullQuery, PSettings,          # noqa: E402
                                   RecExecutor)

# ── 工装 ────────────────────────────────────────────────────────────
T.sync_vocab({
    "cover.ke_ting_chuang_lian": {"attributes": {"friendly_name": "客厅窗帘"}},
    "cover.ban_gong_shi_ping_kai_chuang": {"attributes": {"friendly_name": "平开窗"}},
    "light.ke_ting_tai_deng": {"attributes": {"friendly_name": "客厅台灯"}},
    "climate.ban_gong_shi_kong_tiao": {"attributes": {"friendly_name": "办公室空调"}},
}, {})
T.sync_areas(["客厅", "办公室", "卧室"])


class _Scenes:
    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    async def refresh(self, force=False):
        pass

    def check(self, text):
        return None

    async def verify_or_refresh(self, phrase):
        return False


class _Set:
    def get(self, dotted, default=None):
        return default


def _fp():
    return FastPath(_Scenes(), None, _Set())


def _match(text):
    return asyncio.run(_fp().match(text))


# ══ ① P0 帘窗语序归一 × 否定祈使 ═══════════════════════════════════
def test_negation_over_cover_wordorder_never_executes():
    for utt in ("别把窗帘关上", "别把窗户打开", "不要把客厅窗帘拉上", "别拉窗帘"):
        assert is_negation_imperative(utt), f"{utt} 判据本身要成立"
        got = _match(utt)
        assert got is None, f"{utt} 仍被字面表接走并下发：{got.intent} {got.args}"


def test_cover_wordorder_still_works_for_commands():
    """反向不变量：非否定句的 SOV/SVO 归一不许被这次护栏连带废掉。"""
    assert _cover_wordorder("把窗帘关上") == "关闭把窗帘"
    got = _match("把窗帘关上")
    assert got is not None and got.intent == "TurnDeviceOff", f"正常关窗帘句被误杀：{got}"
    got2 = _match("客厅窗帘拉上")
    assert got2 is not None, "SOV 形「客厅窗帘拉上」归一失效（正常命令掉回兜底）"


def test_wordorder_guard_is_ahead_of_gate():
    """接线：护栏挂在改写那一支上，而不是把否定闸提到场景等值之前（会误吞触发词）。"""
    import ast
    src = (ROOT / "core/nlu/fast_path.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "match")
    body_src = ast.get_source_segment(src, fn)
    i_guard = body_src.index("is_negation_imperative(text)")
    i_gate = body_src.index('self._miss(trace, "否定句→不接管(拒执行)")')
    assert i_guard < i_gate, "否定句改写护栏必须位于改写处（闸吃的是改写后的 text）"


# ══ ② P1 合链丢腿内 extra_steps ═════════════════════════════════════
def test_chain_flattens_each_legs_own_steps():
    from core.nlu.fast_path import Plan
    from core.pipeline import Pipeline
    import time as _time
    from collections import OrderedDict
    from test_experience_batch import HA, PSettings, RecExecutor, NullQuery

    L1 = "light.ban_gong_shi_shede"
    L2 = "light.wo_shi_deng"
    L3 = "light.deng"
    first = Plan("HassTurnOn", {"target": [{"devices": [{"name": "客厅灯"}]}]},
                 source="t0", utterance="打开客厅的灯和卧室的灯",
                 extra_steps=[{"name": "HassTurnOn",
                               "args": {"target": [{"devices": [{"name": "卧室灯"}]}]},
                               "utterance": "打开客厅的灯和卧室的灯"}])
    second = Plan("HassTurnOff", {"target": [{"devices": [{"name": "灯"}]}]},
                  source="klar", utterance="关灯")

    class Lane2:
        def __init__(self, table):
            self.table = table

        async def match(self, text):
            return self.table.get(text)

    p = Pipeline.__new__(Pipeline)
    p.settings = PSettings()
    ha = HA({})
    ha._areas = {"o": "客厅"}
    p.ha, p.executor, p.agent = ha, RecExecutor(), None
    p.query, p.fast_path, p.klar, p.scenes = (NullQuery(), Lane2({
        "打开客厅的灯和卧室的灯": first, "关灯": second}), Lane2({}), None)
    p._last, p._turns, p._last_target = OrderedDict(), {}, {}
    p._origin_ts, p._confirm, p._pending = {}, {}, set()
    p._vocab_ts = _time.time()

    _reply, merged, plans, _flag = asyncio.run(
        p._chain_decide("打开客厅的灯和卧室的灯然后关灯", "o"))
    assert merged is not None, "本例 split_compound 必须切成两腿（形态前提）"
    names = [s.get("name") for s in (merged.extra_steps or [])]
    # 期望执行面共 3 步：首腿本体(merged.intent/args) + 首腿自带的卧室灯 + 次腿关灯
    assert len(names) == 2, f"首腿并列宾语那一步仍被丢掉：{names}"
    assert "HassTurnOn" in names, f"首腿自带步没进链：{merged.extra_steps}"
    assert names[-1] == "HassTurnOff", f"次腿顺序错位：{names}"
    targets = [str(s.get("args")) for s in merged.extra_steps]
    assert any("卧室灯" in t for t in targets), f"卧室灯蒸发：{targets}"
    assert len(plans) == 2


def test_chain_single_step_legs_unchanged():
    """反向不变量：腿都不带 extra_steps 时，合链步数与旧形逐值相等（不许多塞）。"""
    from core.nlu.fast_path import Plan
    from core.pipeline import Pipeline
    import time as _time
    from collections import OrderedDict
    from test_experience_batch import HA, PSettings, RecExecutor, NullQuery

    a = Plan("HassTurnOn", {"target": [{"devices": [{"name": "射灯"}]}]},
             source="t0", utterance="打开射灯")
    b = Plan("HassTurnOff", {"target": [{"devices": [{"name": "台灯"}]}]},
             source="klar", utterance="关掉台灯")

    class Lane2:
        def __init__(self, table):
            self.table = table

        async def match(self, text):
            return self.table.get(text)

    p = Pipeline.__new__(Pipeline)
    p.settings = PSettings()
    ha = HA({})
    ha._areas = {"o": "客厅"}
    p.ha, p.executor, p.agent = ha, RecExecutor(), None
    p.query, p.fast_path, p.klar, p.scenes = (NullQuery(),
                                              Lane2({"打开射灯": a, "关掉台灯": b}),
                                              Lane2({}), None)
    p._last, p._turns, p._last_target = OrderedDict(), {}, {}
    p._origin_ts, p._confirm, p._pending = {}, {}, set()
    p._vocab_ts = _time.time()
    _r, merged, _plans, _f = asyncio.run(p._chain_decide("打开射灯然后关掉台灯", "o"))
    assert [s["name"] for s in merged.extra_steps] == ["HassTurnOff"], \
        f"单步腿链被改动撑胖：{merged.extra_steps}"


# ══ ③ P1 确证读：刷新失败不抛 ⇒ 必须查具名陈旧 ═══════════════════════
class _HaStale:
    """复刻现役 ha_client 的失败形态：refresh 永不抛，只把 _states_ok 置否、留旧快照。"""

    def __init__(self, stale_reason=""):
        self._states = {"light.tai_deng": {"entity_id": "light.tai_deng", "state": "on",
                                           "attributes": {"friendly_name": "台灯"}}}
        self._reason = stale_reason
        self.refreshed = 0

    async def refresh_states(self, force=False):
        self.refreshed += 1
        return None                       # 真实实现：失败也正常返回

    async def states(self):
        return dict(self._states)

    def states_stale(self):
        return self._reason


def _executor():
    from core.executor import Executor
    return Executor.__new__(Executor)


def test_noop_claim_is_dropped_when_read_is_stale():
    from core.executor import Executor
    ex = Executor.__new__(Executor)
    ex.settings = {}
    ex.ha = _HaStale("上次状态读取失败（快照约 12 秒前）")
    args = {"target": [{"devices": [{"name": "台灯", "domains": ["light"]}]}]}
    kind, label = asyncio.run(ex._leg_truth_confirmed("TurnDeviceOn", args))
    assert ex.ha.refreshed == 1, "没做确证读"
    assert kind == "" and label == "", \
        f"读取失败却拿陈旧快照指控空操作：{kind} {label}"


def test_noop_claim_survives_when_read_is_fresh():
    """反向不变量：确证读成功时**照旧要点名**——不许把这次修复做成整条弃权。"""
    from core.executor import Executor
    ex = Executor.__new__(Executor)
    ex.settings = {}
    ex.ha = _HaStale("")                  # 刷成功、快照现行
    args = {"target": [{"devices": [{"name": "台灯", "domains": ["light"]}]}]}
    kind, label = asyncio.run(ex._leg_truth_confirmed("TurnDeviceOn", args))
    assert kind == "noop", f"灯已开着还说'本来就在要求的状态上'这条被做哑了：{kind}"


def test_lock_confirm_skips_when_stale():
    """同形态第二处（`_lock_unconfirmed` 的 docstring 自称"强制刷新失败 → 不判"）。

    判别力靠两臂：**新鲜态必须照样指控**——否则这条钉就只是"整条函数哑掉"的假绿。
    """
    from core.executor import Executor

    class _LockHa(_HaStale):
        def __init__(self, stale_reason=""):
            super().__init__(stale_reason)
            self._states = {"lock.front": {
                "entity_id": "lock.front", "state": "unlocked",
                "attributes": {"friendly_name": "大门锁"}}}

    ex = Executor.__new__(Executor)
    ex.settings = {}
    ex.ha = _LockHa("上次状态读取失败（快照约 2 秒前）")
    out = asyncio.run(ex._lock_unconfirmed("HassLock", {"entity_id": "lock.front"}))
    assert out == [], f"确证读失败仍拿旧快照指控锁具：{out}"

    ex2 = Executor.__new__(Executor)
    ex2.settings = {}
    ex2.ha = _LockHa("")                  # 刷成功、快照现行
    out2 = asyncio.run(ex2._lock_unconfirmed("HassLock", {"entity_id": "lock.front"}))
    assert out2, "新鲜态下『上锁没确认到』这条指控被整条做哑（假绿）"


# ══ ④ P1 窗侧部分失败必须带具名 partial_error ═══════════════════════
def _wc():
    import test_v1097_intent_500_guard as v1097
    import test_window_speed_behavior as bench
    bench._install_ha_stubs()
    return v1097._load("intent_window_control")


def _ir():
    import test_v1097_intent_500_guard as v1097
    import test_window_speed_behavior as bench
    bench._install_ha_stubs()
    return v1097._load("intent_result")


def test_window_partial_failure_carries_named_partial_error():
    wc = _wc()
    out = wc._all_window_result("客厅", "open", ["button.a"], ["卧室平开窗 按压超时"])
    assert out["success"] is True
    assert out.get("partial_error") == "1扇未成功：卧室平开窗 按压超时", \
        f"部分失败没带具名字段：{out}"
    assert "但1扇未成功" in out["message"]


def test_window_full_success_has_no_partial_error():
    """反向不变量：全成不许被塞 partial_error（否则成功被说成失败）。"""
    wc = _wc()
    out = wc._all_window_result("客厅", "open", ["button.a", "button.b"], [])
    assert not out.get("partial_error"), out
    ir = _ir()
    ok, err = ir.fold_action_ok({"success": True, "control_targets": [{"name": "窗"}]})
    assert ok and err == "", f"全成功被折出原因：{ok} {err}"


def test_fold_reads_turn_forwarded_partial_error():
    """消费侧闭环：turn 转发带上 partial_error 后，场景/自动化回放不再回全绿。"""
    ir = _ir()
    ok, err = ir.fold_action_ok({"success": True,
                                 "control_targets": [{"name": "平开窗"}],
                                 "partial_error": "1扇未成功：卧室平开窗 按压超时"})
    assert err, "部分失败被折成零问题（回放会播「已执行场景」）"
    assert "1扇未成功" in err


def test_turn_forwarder_propagates_partial_error():
    """中段转发（intent_turn._async_handle）形状钉：success 支必须读并带上该字段。

    端到端要真 HA 意图夹具（test_audit4_fixes 的 _turn_harness 会替换
    homeassistant.helpers.* 全局，同会话污染既有钉），本批改用源钉守形状，
    真机口径列入未验清单。"""
    src = (ROOT / "custom_components/huijian_ai/intent_turn.py").read_text(encoding="utf-8")
    assert "window_partials" in src, "turn 转发不再收集 partial_error"
    assert 'out["partial_error"]' in src, "转发返回值没带 partial_error（字段被吞回原样）"
    assert src.count('result.get("partial_error")') >= 1


# ══ ⑤ P1 温度绝对档不得被相对档截胡 ═════════════════════════════════
def test_temperature_absolute_with_direction_verb():
    for utt, want in (("把空调调高到26度", "26"), ("把空调调低到18度", "18"),
                      ("空调提高到24度", "24"), ("把空调调到26度", "26")):
        got = _scan_delta(utt, "temperature")
        assert got and got[0] == want, f"{utt} 折成 {got}（期望绝对 {want}）"


def test_temperature_relative_without_dao_still_relative():
    """反向不变量：没有「到」就是相对档，一条都不许改成绝对。"""
    for utt, want in (("把空调调高2度", "+2"), ("温度调低3度", "-3"),
                      ("空调温度调高5度", "+5")):
        got = _scan_delta(utt, "temperature")
        assert got and got[0] == want, f"{utt} 折成 {got}（期望相对 {want}）"


def test_brightness_absolute_forms_untouched():
    assert _scan_delta("调高亮度到80%", "brightness")[0] == "80"
    assert _scan_delta("亮度调到50", "brightness")[0] == "50"


# ══ ⑥ P1 退下字面表吃尾标点 ═════════════════════════════════════════
def test_end_dialogue_with_trailing_punctuation():
    for utt in ("退下", "退下。", "再见。", "结束对话。", "拜拜！", "不用了。"):
        assert is_end_dialogue(utt), f"{utt} 谓词本身要成立"
        got = _match(utt)
        assert got is not None and got.intent == "HuijianEndConversation", \
            f"{utt} 停麦意图丢失：{got}"
        assert got.source == "t0_end"


def test_end_dialogue_table_still_exact():
    """反向负例钉：等值表不许被改成子串/前缀匹配（「安静一点」是调亮度）。

    已知**既存**假阳性、本批不动并留账：「不用了谢谢」经礼貌剥离折成在表的
    「不用了」⇒ 停麦。判据来自 `_goldtest/repro_audit_1138_p0p1.py` 的 trace
    `['礼貌→不用了','退下字面表']`，1.1.37 同形——要改的是"礼貌折字后不许再吃等值表"
    这条一般规则，属另一类，需用户定性后再收。
    """
    for utt in ("安静一点", "再见面再说", "退下了吗", "告别的话", "再见一面"):
        got = _match(utt)
        assert got is None or got.intent != "HuijianEndConversation", \
            f"{utt} 被误判成退下（会掐麦克风）"

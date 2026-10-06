# -*- coding: utf-8 -*-
"""外部审计 P2 六条（#7/#8/#10/#11/#12/#13）的复现钉（v1.1.38 工作树批）。

#9（窗控部分失败 fold 不读）已由 #4 那批的 `partial_error` 具名字段一并闭掉，
钉在 `test_v1138_audit_p0p1_batch.py` 里，本文件不重复。

复现工装：`_goldtest/repro_audit_1138_p2.py`（修前/修后各存一份 log）。
每条都配反向臂——本批最容易出的事故是"把脏数据守卫做成把正常数据也吞掉"。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pytest                                                      # noqa: E402
from core.nlu import targets as T                                   # noqa: E402
from core.nlu import creation                                       # noqa: E402
from core.nlu.fast_path import Plan                                 # noqa: E402
from core.executor import Executor, zh_error                        # noqa: E402
from core import tts                                                # noqa: E402
from test_v1064_batch456 import _exec_extract, CC                   # noqa: E402

T.sync_vocab({
    "light.ke_ting_tai_deng": {"attributes": {"friendly_name": "客厅台灯"}},
    "cover.ke_ting_chuang_lian": {"attributes": {"friendly_name": "客厅窗帘"}},
}, {})
T.sync_areas(["客厅"])

_va = _exec_extract(CC / "api.py", "_validate_automation_actions")


# ══ #7 speech() 对脏形态永不抛（契约：callers 永不炸）═══════════════
def test_speech_never_raises_on_dirty_rows():
    ex = Executor.__new__(Executor)
    ex.settings = {}
    dirty = [
        ("HassListVoiceScenes", {"scenes": ["客厅模式"]}),
        ("TurnDeviceOn", {"success": True, "message": {"plain": {"output": "好的"}}}),
        ("HassLock", {"success": True, "states": [None]}),
        ("HassLock", {"success": True, "states": ["x", {"name": "大门锁", "success": True}]}),
    ]
    for intent, payload in dirty:
        out = ex.speech(Plan(intent, {}, source="t0", utterance=""), payload)
        assert isinstance(out, str), f"{intent} 返回非字符串：{out!r}"


def test_speech_does_not_read_a_dict_aloud():
    """message 是 dict 时**不许**把 `{'plain': …}` 念出去，也不许因此抛。"""
    ex = Executor.__new__(Executor)
    ex.settings = {}
    out = ex.speech(Plan("TurnDeviceOn", {}, source="t0", utterance=""),
                    {"success": True, "message": {"plain": {"output": "好的"}}})
    assert "{" not in out and "plain" not in out, f"把 dict 结构播进了话术：{out!r}"


def test_speech_clean_shapes_unchanged():
    """反向不变量：正常形态的话术一字不许改。"""
    ex = Executor.__new__(Executor)
    ex.settings = {}
    out = ex.speech(Plan("HassListVoiceScenes", {}, source="t0", utterance=""),
                    {"scenes": [{"name": "观影"}, {"trigger_phrase": "睡觉"}]})
    assert "观影" in out and "睡觉" in out, out
    out2 = ex.speech(Plan("TurnDeviceOn", {"target": []}, source="t0", utterance=""),
                     {"success": True, "message": "办公室开了"})
    assert out2 == "办公室开了", out2


# ══ #8 coord_refuse 覆盖任一分片（原来只看首片）═════════════════════
def test_coord_refuse_covers_every_segment():
    for utt in ("打开冰箱和台灯", "关闭冰箱和窗帘", "打开排气扇和灯"):
        assert T.coord_clauses(utt) == [], f"{utt} 前提变了（本家设备已扩链）"
        assert T.coord_refuse(utt) is True, f"{utt} 仍放行单发=半执行谎报"


def test_coord_clauses_still_expands_all_known():
    """反向不变量：两片都是本家设备时扩链照旧，不许被这次放宽打死。"""
    assert T.coord_clauses("打开台灯和窗帘") == ["打开台灯", "打开窗帘"]


def test_coord_refuse_ignores_all_unknown_pairs():
    """反向：两片都不认识时保持单发行为（不去吞与设备无关的并列句）。"""
    assert T.coord_refuse("打开苹果和香蕉") is False


# ══ #10 自动化 desc 带上真实分钟 / 「两分」进字符类 ═════════════════
def test_automation_desc_matches_actual_trigger():
    cases = {"每天早上七点零五分打开窗帘": ("07:05", "5分"),
             "每天晚上八点半打开空调": ("20:30", "30分"),
             "每天上午十点一刻打开窗帘": ("10:15", "15分")}
    for utt, (at, frag) in cases.items():
        r = creation.parse(utt)
        assert r and (r["trigger"].get("at") == at), f"{utt} at 变了：{r}"
        assert frag in r["desc"], f"{utt} desc 仍丢分钟：{r['desc']!r}"


def test_liang_minute_is_parsed_not_dropped():
    r = creation.parse("每天七点两分打开灯")
    assert r and r["trigger"]["at"] == "07:02", f"「两分」没进分钟表：{r}"
    assert r["y"] == "打开灯", f"分钟残片污染动作子句：{r['y']!r}"


def test_hour_only_desc_does_not_gain_fake_minutes():
    """反向不变量：整点句不许被加出「0分」。"""
    r = creation.parse("每天早上七点打开窗帘")
    assert r and r["trigger"]["at"] == "07:00" and r["desc"] == "每天早上七点", r


# ══ #11 逗号残片不独占一次合成 ══════════════════════════════════════
def test_short_comma_residue_is_merged_back():
    src = "甲" * 19 + "，乙"
    out = tts.split_sentences(src)
    assert out == [src], f"1 字残片仍独占一段（多一次合成起步开销＋空洞）：{out}"


def test_split_still_preserves_every_character():
    """反向不变量：并块不许丢字/改序，长句仍守 ≤_CHUNK_CHARS+2 的出帧上限。"""
    for src in ("甲" * 19 + "，乙", "乙" * 45 + "，丙丙丙丙丙",
                "把办公室的灯全部打开，然后把窗帘关上，嗯"):
        out = tts.split_sentences(src)
        assert "".join(out) == src, f"拼接不守恒：{out}"
        assert all(len(x) <= tts._CHUNK_CHARS + 2 for x in out), f"出帧单元超限：{[len(x) for x in out]}"


# ══ #12 全失败支不再双「抱歉，」═════════════════════════════════════
def test_total_failure_apology_prefix_once():
    steps = [{"name": "TurnDeviceOn", "args": {}}, {"name": "TurnDeviceOff", "args": {}}]
    inner = zh_error("这台设备不支持该操作")
    stripped = inner[3:] if inner.startswith("抱歉，") else inner
    say = Executor._step_say(1, steps, stripped)
    assert say.count("抱歉，") == 1, f"「抱歉，」出现两遍：{say}"
    assert say.startswith("抱歉，前面 1 步已完成"), say


def test_total_failure_branch_uses_the_strip():
    """接线：全失败支调 `_step_say` 前必须剥字头（与中腿支同口径），不是只测模板。"""
    src = (ROOT / "core/executor.py").read_text(encoding="utf-8")
    i = src.index("_step_say(len(results) - 1, steps,")
    window = src[i - 60:i + 220]
    assert 'reply[3:] if reply.startswith("抱歉，")' in window, \
        f"全失败支的剥离被摘掉：{window}"


# ══ #13 自动化 PUT 的 actions 形状闸 + 执行侧存量守卫 ═══════════════
@pytest.mark.parametrize("actions,expect_bad", [
    (["不是dict"], "actions"),
    ("xx", "actions"),
    ([{"intent": "x"}] * 60, "actions"),
    ([{"intent": "TurnDeviceOn", "params": [1]}], "actions.params"),
    ([{"intent": "TurnDeviceOn", "params": {}}], ""),
    (None, ""),                                                    # 部分更新
])
def test_automation_actions_gate(actions, expect_bad):
    assert _va(actions) == expect_bad, actions


def test_automation_put_wired_and_executor_guarded():
    api = (CC / "api.py").read_text(encoding="utf-8")
    assert "bad = _validate_automation_actions(actions)" in api, "PUT 侧的 actions 闸被摘掉"
    ia = (CC / "intent_automation.py").read_text(encoding="utf-8")
    j = ia.index("def _execute_actions")
    body = ia[j:j + 1800]
    assert "isinstance(action, dict)" in body, \
        "执行侧对存量脏 actions 无守卫（一条 str 就让整串静默失效）"

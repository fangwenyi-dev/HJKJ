"""v1.5.8 相对延时（时间语音批）行为钉。

现场（2026-10-15 18:00 现网日志，用户报"语音里有时间的概念现在不能执行"）：
    [级联] '十秒后打开办公室射灯'                     → [fallback] 如实拒（安全但不支持）
    [级联] '打开办公室射灯然后十秒钟后关闭办公室射灯' → [chain]  **立即开又立即关＋播「都办妥了」**

第二行是真缺陷（延时被 split_compound 切腿后剥词 ⇒ 立即执行＋谎报）。本批处置：
· 句首前导延时（单条正文）＝**真排程**，到点重入正常 handle()（全闸、不建特权通道、
  不主动播报）；回执用预约语气（将来时），绝不"已做完"。
· 其它延时形状（链中延时/越界）＝**如实拒并教分开说**，绝不再回到"剥词→立即跑→谎报"。
· 非延时句 has_delay_shape=False 直接放行 ⇒ **既有链族逐字不变**（下面第②组是这条的
  反向保护：任何既有链句被误判成延时＝撞承重钉）。

设计边界（如实，非缺陷）：内存调度，加载项重启丢预约；链式逐腿排程本批不做。
"""
import asyncio
import sys
import time
from collections import OrderedDict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.nlu import delay as D                    # noqa: E402
from core.nlu.fast_path import Plan                 # noqa: E402
from core.pipeline import Pipeline                  # noqa: E402
from test_experience_batch import (HA, Lane, NullQuery, PSettings,  # noqa: E402
                                   RecExecutor)


def arun(c):
    return asyncio.run(c)


# ── ① 解析单元 ────────────────────────────────────────────────────
def test_parse_leading_shapes():
    assert D.parse_leading("十秒后打开办公室射灯") == (10, "打开办公室射灯", "十秒后")
    assert D.parse_leading("十秒钟后打开办公室射灯")[:2] == (10, "打开办公室射灯")
    assert D.parse_leading("10秒后关闭台灯")[0] == 10
    assert D.parse_leading("两分钟后开空调") == (120, "开空调", "两分钟后")
    assert D.parse_leading("一个小时后把窗帘拉上")[0] == 3600
    assert D.parse_leading("30秒后关掉客厅的灯") == (30, "关掉客厅的灯", "30秒后")


def test_parse_leading_rejects():
    # 非前导（延时在句尾/句中）→ 不排
    assert D.parse_leading("打开办公室射灯然后十秒钟后关闭办公室射灯") is None
    # 越界（>1h）→ 不排
    assert D.parse_leading("两小时后打开办公室射灯") is None
    # 正文里还藏第二个延时 → 不排（多个待排动作，本批不拆）
    assert D.parse_leading("十秒后打开办公室射灯再五分钟后台灯") is None
    # 「稍后/待会儿」无具体时长 → 不算延时形状
    assert D.has_delay_shape("稍后打开办公室射灯") is False
    assert D.has_delay_shape("待会儿关灯") is False
    # 空/异常输入不抛
    assert D.parse_leading("") is None and D.has_delay_shape(None) is False


def test_shape_in_range_vs_out_of_range():
    # 越界仍是"延时形状"（要接管、不能漏回老链），但 delay_seconds 给越界值供分档话术
    assert D.has_delay_shape("两小时后打开办公室射灯") is True
    assert D.delay_seconds("两小时后打开办公室射灯") == 7200
    assert D.delay_seconds("十秒后打开办公室射灯") == 10


# ── ② 反向保护：既有链/普通句**绝不被误判成延时**（撞承重钉的防线）────────
def test_no_false_positive_on_existing_chain_sentences():
    """这些是本仓既有链族测试里的句子（split_compound/serial/coord 族）。
    任一被 has_delay_shape 判真 ⇒ 我的闸会抢在链切分前接管 ⇒ 既有链行为漂移＝事故。"""
    for s in [
        "打开灯然后关闭窗帘",
        "打开客厅的灯和卧室的灯",
        "关闭办公室射灯关闭办公室平开窗",
        "打开办公室射灯、空调、平开窗",
        "打开办公室射灯和打开床头灯",
        "打开射灯然后关掉台灯",
        "打开客厅的灯，然后再关闭书房窗帘",
        "把没关紧的窗关上",
        "打开办公室射灯",
        "亮度调到百分之五十",
        "把办公室空调温度调到26度",
    ]:
        assert D.has_delay_shape(s) is False, f"既有句被误判成延时（会抢链切分）：{s!r}"


# ── ③ 端到端：真级联处置（复用 test_experience_batch 手工装配夹具）─────────
def _mk(fp_plan=None):
    p = Pipeline.__new__(Pipeline)
    p.settings = PSettings()
    p.ha = HA({})
    p.executor = RecExecutor()
    p.agent = None
    p.query = NullQuery()
    p.fast_path = Lane(single=fp_plan or Plan(
        "TurnDeviceOn", {"target": [{"area": "办公室", "devices": [{"name": "射灯"}]}]},
        source="t0", utterance="打开办公室射灯"))
    p.klar = Lane()
    p.scenes = None
    p._last = OrderedDict()
    p._turns, p._last_target, p._origin_ts, p._confirm = {}, {}, {}, {}
    p._pending = set()
    p._delay_tasks = set()
    p._vocab_ts = time.time()
    return p


def test_plain_sentence_untouched():
    """无延时句走原路（source=t0），不碰延时闸。"""
    p = _mk()
    r = arun(p.handle("打开办公室射灯"))
    assert r.source == "t0" and r.ok
    assert len(p.executor.plans) == 1          # 立即执行（原行为）
    assert len(p._delay_tasks) == 0


def test_leading_delay_schedules_does_not_execute_now():
    """`十秒后X`：排程、回执预约语气、**此刻不执行**（不谎报）。

    同一 loop 内断言：asyncio.run 收尾会 cancel 在途 task（测试装置形态，生产是
    常驻 loop），跨 run() 看 _delay_tasks 必然是空——所以袋内计数与"没立即跑"
    都必须在场景内查（与 test_fire_* 同形）。
    判据力修正（变异 M2 抓出的量具洞）：create_task 后协程未让出前 _fire_delay
    根本没跑，"立即断言 plans==0"对 0 秒变体也恒绿 ⇒ 必须 `await sleep(0.05)`
    给 loop 充分机会后，10 秒排程**仍然**不许执行——这才真钉住"排程≠立即跑"。
    """
    async def scenario():
        p = _mk()
        r = await p.handle("十秒后打开办公室射灯")
        assert r.source == "delay" and r.ok
        assert "十秒后" in r.text or "10秒" in r.text
        assert "办好了" not in r.text and "打开了" not in r.text   # 绝不是完成时
        assert len(p._delay_tasks) == 1              # 真排上了（同 loop 内可见）
        await asyncio.sleep(0.05)                    # 给在途 task 充分开跑机会
        assert len(p.executor.plans) == 0            # 10 秒没到，一秒都不该动
    asyncio.run(scenario())


def test_combo_delay_refuses_no_lie():
    """`X然后十秒后Y`：如实拒（ok=False），绝不再"立即开关＋都办妥了"。"""
    p = _mk()
    r = arun(p.handle("打开办公室射灯然后十秒钟后关闭办公室射灯"))
    assert r.source == "delay" and not r.ok
    assert "分开说" in r.text
    assert "办妥" not in r.text and "办好了" not in r.text   # 谎报形态绝迹
    assert len(p.executor.plans) == 0             # 一腿都不立即跑


def test_out_of_range_delay_refuses():
    """`两小时后X`（>1h）：接管后如实拒"超出支持范围"，不放回老链。"""
    p = _mk()
    r = arun(p.handle("两小时后打开办公室射灯"))
    assert r.source == "delay" and not r.ok
    assert "超出" in r.text or "1小时" in r.text
    assert len(p.executor.plans) == 0


def test_reply_has_no_markdown_stars():
    """回执是语音播报：话术里不许出现 markdown `**`（会被 TTS 念出来）。"""
    p = _mk()
    for t in ["打开办公室射灯然后十秒钟后关闭办公室射灯", "两小时后开灯"]:
        r = arun(p.handle(t))
        assert "**" not in r.text, f"回执混进 markdown：{r.text!r}"


def test_delay_caps_at_max_pending():
    """并发上限：排满 DELAY_MAX_PENDING 后拒新单，不无界堆 task。"""
    async def scenario():
        p = _mk()
        n = p.DELAY_MAX_PENDING
        holders = [asyncio.create_task(asyncio.sleep(3600)) for _ in range(n)]
        p._delay_tasks.update(holders)              # 占位（不真跑）
        r = await p.handle("十秒后打开办公室射灯")
        assert r.source == "delay" and not r.ok
        assert "排满" in r.text
        for t in holders:
            t.cancel()
    asyncio.run(scenario())


def test_fire_actually_executes_body_at_due_time():
    """真到期：1 秒排程后，executor 多跑一次（走正常 handle 全闸，非特权通道）。"""
    async def scenario():
        p = _mk()
        before = len(p.executor.plans)
        await p.handle("1秒后打开办公室射灯")
        assert len(p.executor.plans) == before      # 排程时不执行
        await asyncio.sleep(1.3)
        assert len(p.executor.plans) == before + 1  # 到点真执行
    asyncio.run(scenario())


def test_fire_body_flows_through_full_cascade():
    """到期执行的是**正文**（去掉"十秒后"），走 handle() ⇒ 与直接说正文同源。"""
    async def scenario():
        p = _mk()
        await p.handle("1秒后打开办公室射灯")
        await asyncio.sleep(1.3)
        ran = p.executor.plans[-1]
        assert ran.intent == "TurnDeviceOn"          # 正文被正常裁决成执行计划
    asyncio.run(scenario())


def test_cancel_delays_on_shutdown():
    """停机撤单：在途预约被取消，返回条数；防 ha 关闭后孤儿 task 触设备。"""
    async def scenario():
        p = _mk()
        await p.handle("60秒后打开办公室射灯")
        assert len(p._delay_tasks) == 1
        n = p.cancel_delays()
        assert n == 1
        await asyncio.sleep(0.05)
        assert len(p._delay_tasks) == 0             # 已摘袋
        assert len(p.executor.plans) == 0           # 撤单后不会到点执行
    asyncio.run(scenario())


# ── ④ 结构钉：接线位置与单点（防重构把闸弄丢/弄到链后）────────────────
def test_delay_gate_runs_before_compound_split():
    """延时闸必须在 `_try_compound` **之前**——否则链切分已先剥词、谎报照旧。"""
    import inspect
    src = inspect.getsource(Pipeline._cascade)
    i_dly = src.index("self._delay_gate(")
    i_chain = src.index("await self._try_compound(")
    assert i_dly < i_chain, "延时闸必须排在复合切分之前（链切分会先剥掉延时词）"
    assert src.count("self._delay_gate(") == 1, "延时闸全链单点，多处调用各自漂移"


def test_shutdown_cancels_delays():
    """main.shutdown 必须撤延时预约（孤儿 task 携旧引用会在关闭后触设备）。"""
    main = (Path(__file__).resolve().parents[1] / "core" / "main.py").read_text(
        encoding="utf-8")
    assert "self.pipeline.cancel_delays()" in main, "停机未撤在途延时预约"

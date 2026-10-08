# -*- coding: utf-8 -*-
"""P0-1 整轮时长拒收闸（30s）钉桩。

活缺陷（v1.2.6 起，非本刀引入）：默认档换成 FireRedASR2-AED 后整句解码代价曲线变了
两个数量级，而兜底闸门还是围着旧引擎定的——开发机实测（_bench/probe_aed_long_utterance.py，
num_threads=2）：

    音频   解码墙钟   RTF     RSS 增量
    10s     2.28s    0.227    +1448MB
    30s    10.99s    0.364    +1883MB
    60s    36.13s    0.599    +3006MB
    120s   76.52s    0.634    +5913MB
    181s  109.05s    0.603    +8013MB
    211s/241s  RuntimeError（ORT /encoder/.../Add_4 广播失败，该轮无结果）

而 `const.STT_RESULT_BUDGET_S=52` 那道 `wait_for` **只截回执不截线程**：解码走
`asr.py:321 run_in_executor(None, …)`，asyncio 取消 awaitable 取消不掉已在飞的阻塞
线程——于是一轮 181s 语音＝会话 52s 时放客户端走人、后台继续跑满 109s 并全程占 8GB。
4核8G 靶机上内存先到顶（根本到不了 211s 那道崩点），最坏形状是 OOM→Supervisor 重启循环。

触发面：`session.py:180` 的单会话 PCM 累积顶是 300s，它防的正是"设备狂发音频但不发
stop"（v1.0.41 S16 注释在案）——所以这不是日常语句，而是在案故障形态。现网 403 句
命令集中位 2.75s、最长 5.88s；取 30s 留 5 倍冗余（那批数据是合成音，人念更慢更长）。

**拒收不是截尾**：把超过 30s 的音频截掉前面一段再喂引擎，引擎会只听到半句话——
"把空调打开**不要**…"这类否定尾巴被留下时，风险方向是把"拒收"变成"误执行"。
故整轮不进引擎，只回具名分因。
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.session import SttSession   # noqa: E402

# 16k mono s16le ⇒ 32000 B/s（const.SAMPLE_RATE×2×CHANNELS，上下行都钉死 16k mono）
BPS = 32000


class _WS:
    def __init__(self):
        self.sent = []
        self.closed = False       # session._send 读它（缺了＝发送分支静默 False，回执永不落）

    async def send_str(self, s):
        self.sent.append(json.loads(s))

    async def send_bytes(self, b):
        self.sent.append(b)


def _mk_ctx(seconds_per_frame=1.0, reply="开灯"):
    """替身：数引擎被真调了几次；decoder 每帧吐 N 秒 PCM。"""
    calls = []

    async def transcribe(pcm):
        calls.append(len(pcm))
        return reply

    ctx = SimpleNamespace()
    ctx.asr = SimpleNamespace(transcribe_pcm=transcribe)
    ctx.decoder_factory = lambda: SimpleNamespace(
        decode=lambda p: b"\x01" * int(BPS * seconds_per_frame))
    return ctx, calls


async def _drive_turn(ctx, sec, rid=7):
    """真入口：listen start → 若干二进制帧 → listen stop。返回 (ws, 引擎调用列表)。"""
    ws = _WS()
    sess = SttSession(ws, ctx)
    frames = max(1, int(round(sec)))
    await sess.on_text(json.dumps({"type": "listen", "state": "start", "rid": rid}))
    for _ in range(frames):
        await sess.on_binary(b"\xfe\xfe" * 40)      # 帧内容不参与，替身固定吐 1s
    await sess.on_text(json.dumps({"type": "listen", "state": "stop", "rid": rid}))
    for _ in range(200):
        if any(isinstance(x, dict) and x.get("type") == "stt" for x in ws.sent):
            break
        await asyncio.sleep(0.01)
    return ws, getattr(sess, "_pcm", None)


def _first_stt(ws):
    return next(x for x in ws.sent if isinstance(x, dict) and x.get("type") == "stt")


def test_31s_turn_is_rejected_before_the_engine_runs():
    """31s 整轮：引擎必须一次都没被调（闸在引擎调用之前，不是事后追认）。"""
    async def go():
        ctx, calls = _mk_ctx()
        ws, _ = await _drive_turn(ctx, 31)
        assert calls == [], f"超长轮仍进了引擎（pcm={calls}）——闸形同虚设"
        stt = _first_stt(ws)
        assert stt.get("text", "") == ""
        reason = stt.get("reason", "")
        assert "过长" in reason, f"回执没点名拒收原因：{reason!r}"
        assert "31" in reason, f"分因未带本轮实测时长（现场无从判断差多少）：{reason!r}"
    asyncio.run(go())


def test_30s_turn_at_the_boundary_still_goes_to_the_engine():
    """边界：恰好 30s 属合法轮，必须照常进引擎。

    这条与上一条配对，钉的是**阈值数字本身**——若有人把闸抬到 _MAX_PCM_BYTES(300s)
    那一档"顺手复用"，31s 那条转红；若有人把它收紧过头，本条转红。
    """
    async def go():
        ctx, calls = _mk_ctx()
        ws, _ = await _drive_turn(ctx, 30)
        assert calls == [30 * BPS], f"30s 合法轮没进引擎或字节数不对：{calls}"
        assert _first_stt(ws).get("text") == "开灯"
    asyncio.run(go())


def test_rejected_turn_still_gets_exactly_one_stt_reply_with_rid():
    """契约：拒收轮不得静默——仍回**恰好一条** stt 帧，且 rid 回显本轮身份。

    设备侧等的是 stt 帧；"超长所以不回话"与"网络僵死"在客户端同形，会撞
    v1.0.92 那套 rid 配对（旧回执落进新轮＝张冠李戴）。
    """
    async def go():
        ctx, _ = _mk_ctx()
        ws, _ = await _drive_turn(ctx, 45, rid=7)
        stts = [x for x in ws.sent if isinstance(x, dict) and x.get("type") == "stt"]
        assert len(stts) == 1, f"拒收轮回执条数={len(stts)}（须恰好 1 条）"
        assert stts[0].get("rid") == 7, f"rid 未回显：{stts[0]}"
    asyncio.run(go())


def test_next_normal_turn_works_after_a_rejection():
    """拒收不留 residue：同一会话下一轮正常语句仍要能被识别（缓冲已随轮清空）。"""
    async def go():
        ctx, calls = _mk_ctx()
        ws = _WS()
        sess = SttSession(ws, ctx)
        for sec, rid in ((60, 1), (3, 2)):
            await sess.on_text(json.dumps({"type": "listen", "state": "start", "rid": rid}))
            for _ in range(max(1, int(round(sec)))):
                await sess.on_binary(b"\xfe\xfe" * 40)
            await sess.on_text(json.dumps({"type": "listen", "state": "stop", "rid": rid}))
            for _ in range(200):
                if sum(1 for x in ws.sent
                       if isinstance(x, dict) and x.get("type") == "stt") >= rid:
                    break
                await asyncio.sleep(0.01)
        assert calls == [3 * BPS], f"两轮引擎调用数不对：{calls}"
        stts = [x for x in ws.sent if isinstance(x, dict) and x.get("type") == "stt"]
        assert stts[0].get("reason") and stts[1].get("text") == "开灯", \
            f"拒收轮/后续轮回执形制不对：{stts}"
    asyncio.run(go())


def test_bytes_per_second_is_derived_from_const_not_literal():
    """秒↔字节换算必须来自 const。

    写死 32000 的后果：采样率/声道一旦变，闸的"秒数"与本轮真实时长分家——
    分因报给用户的是 31s 而引擎实际只听到 15s，这类"显示与实际不同形"是本仓记过的同族坑。
    """
    from core import const
    assert SttSession._PCM_BYTES_PER_SEC == const.SAMPLE_RATE * 2 * const.CHANNELS == 32000
    assert SttSession._MAX_UTTER_BYTES == \
        SttSession._PCM_BYTES_PER_SEC * SttSession._MAX_UTTER_SEC


def test_gate_value_is_30s_and_far_below_the_pcm_backstop():
    """反向钉：30s 拒收闸 ≠ 300s 累积顶，两者不许合并成一个常量。

    `_MAX_PCM_BYTES`（300s）是"无界内存"兜底（丢最旧保顶），它触发时音频**照样进
    引擎**——用它当识别闸＝把 181s→109s 解码 + 8GB 那条路放行。两条闸职责不同，
    数值必须显著分离。
    """
    gate = getattr(SttSession, "_MAX_UTTER_SEC", None)
    assert gate == 30.0, f"拒收闸应钉在 30.0s（现值 {gate!r}）"
    assert SttSession._MAX_PCM_BYTES // BPS == 300
    assert gate < (SttSession._MAX_PCM_BYTES // BPS) / 5, \
        "拒收闸与累积顶贴得太近＝两条闸实际上合成一条，长句仍会进引擎"

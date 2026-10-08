"""v1.2.9 D 刀：三个本地 TTS 档的**原生采样率**必须是仓内单一事实源，且不许有人
把源率写死成 const.SAMPLE_RATE。

动因（本轮实测，非推断）：melo=44100 / kokoro=24000 / matcha=16000（包内声码器
即 vocos-16khz-univ.onnx）。三档率不同 ⇒ "切引擎档要跟着切率"是本地 TTS 的真实不
变量，而它今天只存在于运行时 `audio_obj.sample_rate` 一处——静态侧（lock、面板、
诊断）没有任何可核对的口径，一旦有人按常量重采样就静默出错（44.1k 当 16k 用＝
2.756× 拖长＋基频掉到 87Hz，正是"女声听成男声"的机理）。
"""
import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "models.lock.json").read_text(encoding="utf-8"))

# 本机 sherpa 实测：OfflineTts(melo).num_speakers=1 / sample_rate=44100；
# OfflineTts(kokoro v1.1 fp32).num_speakers=103 / sample_rate=24000。
EXPECTED = {"tts_melo_zh_en": 44100, "tts_kokoro_multilang": 24000,
            "tts_matcha_zh_en": 16000}


def test_lock_declares_native_rate_for_every_local_tts():
    for key, want in EXPECTED.items():
        assert key in LOCK, f"lock 缺档 {key}"
        got = LOCK[key].get("sample_rate")
        assert got == want, f"{key}.sample_rate={got!r}，应为 {want}（原生输出率）"


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_declared_rate_is_in_sane_domain(key):
    v = LOCK[key].get("sample_rate")
    assert isinstance(v, int), f"{key}.sample_rate 必须是整数，拿到 {v!r}"
    assert 8000 <= v <= 192000, f"{key}.sample_rate={v} 超出合理域"


def _synth_body(tree: ast.Module) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_synth":
            return node
    raise AssertionError("core/tts.py 里找不到 _synth（本地合成的唯一落点）")


def test_local_synth_resamples_from_engine_reported_rate():
    """反向钉：本地合成必须"引擎实际率 → 16k"，源率与目标率两头都不许写死。

    写成 `const.SAMPLE_RATE` 当源率＝"拿 44.1k 的内容当 16k 用"——音高时长同时错
    且不抛异常（女声基频掉到 87Hz＝本次报障症状）。而**只盯源率不够**：对抗复核给
    出 `resample_pcm16(pcm, rate, rate)`——src==dst 走直通支，产出正是这个男声，
    旧钉看 args[1] 全绿。故目标率（args[2]）也必须是 16k 常量，且 traced 名字在
    resample 之后不得被字面量重赋值（"读完引擎率再手动改数"这种合法-looking 的写法）。
    """
    tree = ast.parse((ROOT / "core" / "tts.py").read_text(encoding="utf-8"))
    fn = _synth_body(tree)
    from_attr = lambda n: any(  # noqa: E731
        isinstance(x, ast.Attribute) and x.attr == "sample_rate"
        for x in ast.walk(n))
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "resample_pcm16"]
    assert calls, "_synth 里不再有 resample_pcm16 调用——本地率处理被改走了别处，须重新对账"
    traced = {}          # 名 → 该赋值在函数体源码里的位置（用于"后续重赋值"判据）
    body_src = ast.unparse(fn)
    for stmt in ast.walk(fn):
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, (ast.Call, ast.Attribute)):
            for t in stmt.targets:
                if isinstance(t, ast.Name) and from_attr(stmt.value):
                    traced[t.id] = body_src.index(f"{t.id} =")
    assert traced, "_synth 内没有任何变量取自 *.sample_rate"
    for c in calls:
        assert len(c.args) > 2, f"resample_pcm16 必须显式给源率与目标率，拿到 {ast.unparse(c)}"
        src_txt, dst_txt = ast.unparse(c.args[1]), ast.unparse(c.args[2])
        assert "SAMPLE_RATE" not in src_txt, f"源率被写死成常量：{src_txt}"
        assert "SAMPLE_RATE" in dst_txt, f"目标率不是 16k 常量：{dst_txt}（直通支会静默产男声）"
        ok = (isinstance(c.args[1], ast.Name) and c.args[1].id in traced) or from_attr(c.args[1])
        assert ok, f"源率实参 {src_txt!r} 追不到 *.sample_rate 赋值（可追踪名：{sorted(traced)}）"
        if isinstance(c.args[1], ast.Name):
            name = c.args[1].id
            tail = body_src[traced[name] + len(f"{name} ="):]
            for m in re.finditer(rf"^[ \t]*{name} = (.+)$", tail, re.M):
                rhs = m.group(1)
                assert from_attr(ast.parse(rhs, mode="eval")), \
                    f"{name} 在取自引擎后被重赋值为 {rhs}——目标率/源率口径已脱离引擎"

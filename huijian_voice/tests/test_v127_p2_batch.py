# -*- coding: utf-8 -*-
"""2026-10-08 第五轮复查 P2 批的专项钉。

分两类，如实标注（本仓规矩：完成度要分级，别让语法钉冒充行为钉）：
  【行为】= 真函数/真入口跑出来的判据
  【语法】= 集成侧文件本机装不了 homeassistant，按 `test_ota_firmware.py`/
            `test_pipeline_wiring.py` 先例判 AST/函数体片段——它只保"形状没回退"，
            不保"跑起来对"。需要台架背书的那几处已写进交付说明。
"""
import ast
import asyncio
import os
import re
import sys
import time
from collections import OrderedDict

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from core import tts as tts_mod                            # noqa: E402
from core.executor import zh_error                         # noqa: E402
from core.nlu import homophone as H                        # noqa: E402
from core.nlu import targets as T                          # noqa: E402

CC = os.path.join(ROOT, "custom_components", "huijian_ai")


def _fn_seg(path, fname):
    """取某函数源码片段（AST 定位，不用裸字符串搜——注释里提那个 bug 就能骗过文本钉）。"""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fname:
            return src, n, (ast.get_source_segment(src, n) or "")
    raise AssertionError(f"{path} 里没有 {fname}")


# ── TTS 写缓存：重算不得留下 Python 级迭代点 ─────────────────────────────
def _cache_engine():
    eng = tts_mod.TtsEngine.__new__(tts_mod.TtsEngine)
    eng._cache = OrderedDict()
    eng._cache_bytes = 0
    eng.settings = {"tts.cache_enabled": True}
    return eng


def test_cache_put_recompute_has_no_python_level_iteration():
    """v1.1.36 把记账改成"从表全量重算"，但 `sum(... for ... in self._cache.values())`
    是**生成器逐 next**：每次 next 都回到解释器 eval 循环，另一个线程（换代 `clear()`
    :1242、换绑 :1103，都跑在 executor 线程）就能在两次 next 之间插队 ⇒
    `RuntimeError: OrderedDict mutated during iteration`，而调用点 :1480 在**发声协程**
    里没兜底 ⇒ 穿到 session.py:532 ⇒ 本轮播报以 truncated 收束＝缺尾。
    `list(self._cache.values())` 是一次 C 级拷贝、中间不回 eval 循环 ⇒ 插不进去。

    判据读那行的**实际形状**（AST），不读注释；行为臂另测"clear 插在赋值与重算之间
    仍不得抛、账必须与表一致"。替身不能用"values() 返回会中途 clear 的生成器"——
    那模拟的是 CPython 对 C 级拷贝根本不允许的交错，会把正确实现判成红。
    """
    path = os.path.join(ROOT, "core", "tts.py")
    src_lines = open(path, encoding="utf-8").read().splitlines()
    body = _fn_seg(path, "_cache_put")[2]
    tree = ast.parse(body)

    def live_view(node):
        """直接吃 OrderedDict 活视图（values/items/keys）＝迭代可被别的线程插队。"""
        if isinstance(node, ast.Call):        # list(...) 已一次性拷贝，插不进来
            return False
        return isinstance(node, ast.Attribute) and node.attr in ("values", "items", "keys")

    bad = []
    for n in ast.walk(tree):
        if isinstance(n, ast.comprehension) and live_view(n.iter):
            bad.append(ast.unparse(n))
        elif isinstance(n, ast.For) and live_view(n.iter):
            bad.append(ast.unparse(n.iter))
    assert not bad, (
        "_cache_put 里还有直接吃活视图的迭代：" + repr(bad) +
        "（Python 级逐 next 会回 eval 循环，worker 线程的 clear() 能插在两次 next 之间）")
    assert any("list(self._cache.values())" in ln for ln in src_lines), "重算没走一次性拷贝"


class _ClearOnSetItem(OrderedDict):
    """clear() 插在 `pop(旧值) → 赋值` 之间（生产里由 worker 线程执行）。"""
    armed = False

    def __setitem__(self, k, v):
        if self.armed:
            self.armed = False
            OrderedDict.clear(self)
        super().__setitem__(k, v)


def test_cache_put_accounting_converges_after_concurrent_clear():
    eng = _cache_engine()
    eng._cache = _ClearOnSetItem()
    eng._cache_put(("p", "warm", 0, 1.0), [b"z" * 5000])
    eng._cache.armed = True
    eng._cache_put(("p", "s2", 0, 1.0), [b"y" * 100])
    assert eng._cache_bytes >= 0
    assert eng._cache_bytes == sum(sz for _p, sz in eng._cache.values()), eng._cache_bytes


# ── 【行为】同音层缓存按内容判（旧形按对象身份判＝恒 miss 的死码）──────────
def test_homophone_index_cache_is_content_keyed():
    a = H.homophone_index(("悬窗", "开窗器"))
    b = H.homophone_index(("悬窗", "开窗器"))          # 另一个对象、同样内容
    assert a is b, "同内容仍重建倒排：调用方每句现造新 tuple，按 is 判必恒 miss"
    c = H.homophone_index(("悬窗", "开窗器", "推拉窗"))
    assert c is not a and len(c) == 3, "内容变了却没重建（缓存过头）"
    v1 = H.near_index(("悬窗", "开窗器"))
    v2 = H.near_index(("悬窗", "开窗器"))
    assert v1 is v2, "近音档向量表同样按内容判"


# ── 【行为】听不懂的固定兜底不得记成成功 ──────────────────────────────────
def test_fallback_reply_is_not_ok():
    from test_experience_batch import HA, Lane, NullQuery, PSettings, RecExecutor
    from core.pipeline import Pipeline
    p = Pipeline.__new__(Pipeline)
    p.settings = PSettings()
    p.ha = HA({"light.x": {"attributes": {"friendly_name": "客厅灯"}}})
    p.executor, p.agent, p.query = RecExecutor(), None, NullQuery()
    p.fast_path, p.klar, p.scenes = Lane({}, single=None), Lane({}, single=None), None
    p._last = OrderedDict()
    p._turns, p._last_target, p._origin_ts, p._confirm = {}, {}, {}, {}
    p._pending, p._vocab_ts = set(), time.time()
    reply = asyncio.run(p.handle("唔啦呀滴", origin="o"))
    assert reply.source == "fallback", reply.source
    assert reply.ok is False, f"兜底被记成成功（漏斗 ok_pct 会虚高）：{reply}"


# ── 【行为】origin 键源表自己有界 ─────────────────────────────────────────
def test_origin_ts_table_is_bounded():
    from core.pipeline import Pipeline
    p = Pipeline.__new__(Pipeline)
    p._turns = {f"d{i}": [1] for i in range(70)}
    p._last_target, p._confirm, p._last_list = {}, {}, {}
    p._origin_ts = {f"d{i}": float(i) for i in range(500)}
    p._gc_origins()
    assert len(p._origin_ts) <= 64, f"origin 键表无界（docstring 自称 64 台封顶）：{len(p._origin_ts)}"


# ── 【行为】话术映射（P1-3 裸窗句 + P2 颜色形态闸）────────────────────────
@pytest.mark.parametrize("raw,must", [
    ("No target specified", "窗"),
    ("Colour value 'max' is not a hex colour", "颜色"),
])
def test_error_phrases_map_to_guidance(raw, must):
    say = zh_error(raw)
    assert must in say and "抱歉" in say, say
    assert raw.split()[0].lower() not in say.lower(), f"英文串被念出去：{say}"


# ── 【行为】「两号窗」两侧折名一致（跨仓同一入口比对）──────────────────────
def _integration_normalizer():
    """集成侧文件 import 不了（要 homeassistant），按源码把三件套 exec 出来跑。"""
    src = open(os.path.join(CC, "intent_window_const.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {"re": re}
    picked = []
    for n in tree.body:
        name = (getattr(n, "targets", [None])[0].id if isinstance(n, ast.Assign)
                else getattr(n, "name", ""))
        if name in ("_CN_DIGITS", "_CN_UNITS", "_CN_INDEX_COUNTERS", "_CN_PURE_NUM_RE",
                    "_CN_NUM_PATTERN", "_parse_chinese_number",
                    "normalize_chinese_numbers"):
            exec(ast.get_source_segment(src, n), ns)
            picked.append(name)
    assert "normalize_chinese_numbers" in picked and "_CN_DIGITS" in picked
    return ns["normalize_chinese_numbers"]


def test_two_hao_window_normalizes_same_on_both_sides():
    nc = _integration_normalizer()
    for spoken, want in (("两号测试窗", "2号测试窗"), ("两百三十号", "230号"),
                         ("二号窗户", "2号窗户"), ("百叶窗", "百叶窗")):
        assert nc(spoken) == want, f"集成侧 {spoken} → {nc(spoken)!r}，期望 {want!r}"
        assert T.normalize_name(spoken) == want, f"加载项侧 {spoken} 与集成侧不同名"


# ── 【语法】播报异常路径必须吊销推流 ──────────────────────────────────────
def test_announce_revoke_is_in_finally():
    _, fn, seg = _fn_seg(os.path.join(CC, "assist_satellite.py"), "_do_announce")
    tree = ast.parse(ast.unparse(fn))
    ok = False
    for n in ast.walk(tree):
        if isinstance(n, ast.Try) and n.finalbody:
            fin = ast.dump(ast.Module(body=n.finalbody, type_ignores=[]))
            bod = ast.dump(ast.Module(body=n.body, type_ignores=[]))
            if "req_task" in bod and "_revoke_announce_stream" in fin:
                ok = True
    assert ok, "吊销点回到 await 之后（异常路径跳过它＝孤儿推流复发形状）"


# ── 【语法】取不到推流时不得把抢跑事件发出去 ──────────────────────────────
def test_missing_stream_still_suppresses_url_event():
    """旧形 `… and (stream := async_get_stream(...))` 把"取不到流"与"不是推流设备"
    混成同一侧 ⇒ 令牌过期/Core 刚重启的窗口里，TTS_END{url} 原样发给无自取能力的
    API 音频板，正是 :727 那条 v1.0.27 注释里"抢跑会把固件刚起的会话拆掉"的形态。"""
    src = open(os.path.join(CC, "assist_satellite.py"), encoding="utf-8").read()
    assert "and (stream := tts.async_get_stream" not in src, "walrus 短路回魂"
    assert "not stream" in src and "suppress_event = True" in src


# ── 【语法】窗控部分失败必须置 partial_error ──────────────────────────────
def test_partial_failures_set_partial_error_key():
    src = open(os.path.join(CC, "intent_window_control.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
        vals = {k.value: v for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant)}
        if "success" not in keys or "message" not in keys:
            continue
        msg = ast.unparse(vals.get("message") or ast.Constant(""))
        if "未成功" not in msg:
            continue
        checked += 1
        assert "partial_error" in keys, (
            f"部分失败只写进 message（场景回放 intent_voice_scene.py:462 只读 "
            f"success/partial_error，会把半失败折成全绿）：行 {node.lineno}")
    assert checked >= 2, f"只查到 {checked} 处部分失败支（开度+速度/力度两条），覆盖面漂移"


# ── 【语法】云 STT 应答与云 TTS 英文句的两处判据形状 ───────────────────────
def test_cloud_stt_response_is_capped():
    _, fn, seg = _fn_seg(os.path.join(ROOT, "core", "asr.py"), "_cloud_transcribe")
    assert "await r.json(" not in seg, "成功支整包进内存回魂（错误支已截 8192，同函数只修一半）"
    assert "(1 << 20)" in seg and "json.loads" in seg, seg[:200]


def test_cloud_truncation_gate_is_cjk_scoped():
    _, fn, seg = _fn_seg(os.path.join(ROOT, "core", "tts.py"), "_stream_opus_round")
    assert re.search(r"_cjk \* 2 >= len\(text\)", seg), (
        "4.5 字/s 的比值账又对英文句开放：≥60 字正常英文整句会被误判缺尾并钉扎换嗓")

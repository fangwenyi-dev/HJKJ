# -*- coding: utf-8 -*-
"""v1.6.10 刀P1-5：点名查无闸不吃「方位+的」修饰段（级联层判据钉）。

基线（v1.6.9，本树 `_myaudit/gate_after.txt` 留痕）——客户最自然的中文方位形被
拦成"家里没这台"：
    打开客厅里的灯   → 没有找到对应的设备「里的灯」
    关闭客厅里的窗帘 → 没有找到对应的设备「里的窗帘」
    关掉书桌上的台灯 → 没有找到对应的设备「书桌上的台灯」（同句「关掉书桌台灯」能执行）
姊妹层 targets.py:783 早就剥 里（被 test_v166_generic_light_domain 钉着），本闸没剥
=本仓自己反复点名的"同类洞只修一边"。

反向臂同样重要：本闸的本职是"没点名的设备任何方向都不动"（v1.1.35 红线），所以
描述性修饰段（会飞的灯）必须**照旧拦**；同时新增单字元不得把 A8 那条 DP/回溯
教训改回指数级（时间臂钉着）。
"""
import asyncio
import copy
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_v1610lg_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

from core.nlu import targets as T                     # noqa: E402
from core.nlu.textcnn import TextCNN                  # noqa: E402
from core.pipeline import (                            # noqa: E402
    Pipeline, _NAME_HEAD_STRIP, _strip_tail_tokens)
from core.executor import Executor                     # noqa: E402
from core.settings import DEFAULTS                     # noqa: E402
from conftest import FakeHAClient                      # noqa: E402

STATES = {
    "light.ke_ting_deng": {"state": "off", "attributes": {"friendly_name": "吸顶灯"}},
    "light.ke_ting_tai_deng": {"state": "off", "attributes": {"friendly_name": "客厅台灯"}},
    "light.shu_zhuo_tai_deng": {"state": "off", "attributes": {"friendly_name": "书桌台灯"}},
    "cover.ke_ting_chuang_lian": {"state": "closed",
                                  "attributes": {"friendly_name": "客厅窗帘"}},
}
AREAS = {"a1": "客厅", "a2": "书房"}
ENTITY_AREA = {"light.ke_ting_deng": "客厅", "light.ke_ting_tai_deng": "客厅",
               "light.shu_zhuo_tai_deng": "书房", "cover.ke_ting_chuang_lian": "客厅"}


class _S:
    def get(self, dotted, default=None):
        cur = copy.deepcopy(DEFAULTS)
        for k in dotted.split("."):
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur


class _Scenes:
    def check(self, t):
        return None

    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    async def refresh(self, force=False):
        pass

    async def verify_or_refresh(self, phrase):
        return True


class _NoKlar:
    async def match(self, t, origin=""):
        return None


@pytest.fixture(scope="module")
def tc():
    t = TextCNN(os.environ["HUIJIAN_NLU_DATA"])
    t._ensure()
    return t


def _pipe(tc):
    T.sync_areas(list(AREAS.values()))
    ha = FakeHAClient(states=STATES, areas=AREAS, entity_area=ENTITY_AREA)
    st = _S()
    return Pipeline(st, ha, _Scenes(), tc, Executor(ha, st), agent=None, klar=_NoKlar()), ha


async def _run(tc, text):
    p, ha = _pipe(tc)
    return await p.handle(text, origin="panel"), ha


@pytest.mark.parametrize("text", [
    "打开客厅里的灯", "关闭客厅里的窗帘", "关掉书桌上的台灯", "把客厅里的灯打开",
])
def test_locative_modifier_is_not_a_named_device(tc, text):
    """方位+的修饰段不得被当成"点名的设备"⇒ 不许再回 no_such_device。"""
    r, _ha = asyncio.run(_run(tc, text))
    assert r.source != "no_such_device", f"{text!r} 仍被拦成查无:{r.text!r}"
    assert "没有找到对应的设备" not in r.text, f"{text!r} 回显坏名:{r.text!r}"


def test_locative_sentence_actually_closes_the_curtain(tc):
    """正向执行臂：区域+里+的+类别词唯一命中时必须真下发（不是弃权）。"""
    r, ha = asyncio.run(_run(tc, "关闭客厅里的窗帘"))
    assert r.ok is True, r.text
    names = [c[0] for c in ha.calls]
    assert names, f"零下发:{r.text!r}"


@pytest.mark.parametrize("text", ["打开会飞的灯", "关掉没修好的台灯"])
def test_descriptive_modifier_still_refuses(tc, text):
    """反向臂（红线）：描述性修饰段照旧拦——没点名的设备任何方向都不许动。"""
    r, _ha = asyncio.run(_run(tc, text))
    assert r.source == "no_such_device", f"{text!r} 本闸失效:{r.source}/{r.text!r}"


def test_head_strip_eats_locative_particles():
    """机制钉：两张剥词表都吃方位单字元（只修一边＝下次还红）。"""
    import core.pipeline as P
    assert _NAME_HEAD_STRIP.sub("", "里的灯") == "灯"
    assert _strip_tail_tokens("书桌上的") in ("书桌", "书桌的")
    for tok in ("里", "上", "下", "内", "外", "旁", "边", "侧"):
        assert tok in P._LOC_PARTICLES, f"{tok} 未进方位单源表"
        assert tok in P._TAIL_TOKENS, f"{tok} 未进尾切表（同类洞只修一边的形状）"
    # 反向臂：中控屏/智能面板这类真名自带的字**不许**进表（进了会把点名剥坏）
    for keep_out in ("中", "面"):
        assert keep_out not in P._LOC_PARTICLES, f"{keep_out} 混进方位表（会剥坏真名）"
    assert "(?:" not in P._TAIL_TOKENS, "尾切串里不许有分组——它同时被 split 成 DP 词元"


def test_locative_strip_is_linear_time():
    """A8 教训臂：加单字元后，长串剥词仍是线性/多项式时间，不得回到指数回溯。"""
    import core.pipeline as P
    worst = ("里上中下面旁" * 12) + "不行"       # 72 字可两切 + 收尾不在表内
    t0 = time.perf_counter()
    got = P._NAME_TAIL_STRIP.sub("", worst)
    dt = time.perf_counter() - t0
    assert dt < 0.5, f"尾切正则退化:{dt:.3f}s（A8 同型）"
    assert _strip_tail_tokens(worst) is not None
    t1 = time.perf_counter()
    _NAME_HEAD_STRIP.sub("", "里" * 60 + "行")
    assert time.perf_counter() - t1 < 0.5

# -*- coding: utf-8 -*-
"""v1.5.7 窗型裸名路由批（用户问题2「开窗器识别准确度」的收口钉）。

现场（10-13 深夜，.91 真清单回灌 + FastPath 端到端测量 _goldtest/_window_e2e_measure_1013.py）：
办公室**最新入网**设备『内开内倒 开窗器』——先验段/设备名段都是裸『内开内倒』，但六张
窗型表里只有带窗尾的『内开内倒窗』 ⇒ 「打开内开内倒」错道 TurnDeviceOn（违反本仓铁律
「看到窗字→ControlWindow，窗族含内开内倒」，见 test_multi_device_and_opener 头注引
《意图数据集/意图集要求.md》），「内开内倒关上」整句落空＝用户可感的"听不懂"。
修复＝裸形入六表（fast_path._WINDOW_TYPES / targets 尾表+前缀表 / 集成
WINDOW_NAME_MAPPING + WINDOW_KEYWORDS），**键序一律排在长词之后**（先命中先返回截胡纪律，
悬窗族/外装平开窗同型教训）。

同批如实记录（刻意不做，非欠账）：动词错形『官闭/公式/光闭→关闭』『展停→暂停』不入
corrector——corrector.py:53-57 有第七轮审计 A7 的**事故明令**（控制动词键会把
「把X打开」折成反极性命令、凭空造命令）；这类句由查无/fallback 如实收口。
"""
import asyncio
import copy
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

from core.settings import DEFAULTS                       # noqa: E402
from core.nlu.fast_path import FastPath, _WINDOW_TYPES   # noqa: E402
from core.nlu import targets as T                        # noqa: E402
from core.nlu import homophone as H                      # noqa: E402
from core.nlu.textcnn import TextCNN                     # noqa: E402


class _S:
    def get(self, dotted, default=None):
        cur = copy.deepcopy(DEFAULTS)
        for k in dotted.split("."):
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur


class FakeScenes:
    def check(self, t):
        return None

    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    async def refresh(self, force=False):
        pass


@pytest.fixture(scope="module")
def tc():
    t = TextCNN(os.environ["HUIJIAN_NLU_DATA"])
    t._ensure()
    return t


@pytest.fixture()
def fp(tc):
    return FastPath(FakeScenes(), tc, _S())


# 真清单派生的先验（含裸『内开内倒』；与 pipeline.prior_universe 同一构造函数）
PRIOR = H.prior_names(["悬窗 开窗器", "推拉窗 开窗器", "内开窗 开窗器",
                       "平开窗 开窗器", "内开内倒 开窗器", "办公室射灯"])


def _route(fp, text):
    """复刻生产单点顺序：先验改写 → fast_path.match（pipeline.handle 里就是这个序）。"""
    out, _ = H.rewrite(text, PRIOR)
    return asyncio.run(fp.match(out))


# ── ① 行为钉：裸『内开内倒』全语序进窗道 ──────────────────────────
BARE_CASES = [
    ("打开内开内倒",    "open",  "内开内倒"),
    ("关闭内开内倒",    "close", "内开内倒"),
    ("内开内倒关上",    "close", "内开内倒"),   # SOV：修复前整句落空（真实"听不懂"）
    ("把内开内倒打开",  "open",  "内开内倒"),
    # n/l 方言形（v1.5.6 档）端到端必须落同一车道
    ("打开内开累倒",    "open",  "内开内倒"),
    ("打开推那窗",      "open",  "推拉窗"),
]


@pytest.mark.parametrize("text,action,name", BARE_CASES)
def test_bare_window_type_routes_to_control_window(fp, text, action, name):
    p = _route(fp, text)
    assert p is not None, f"{text!r} 落空（修复前『内开内倒关上』即此形态）"
    assert p.intent == "ControlWindow", f"{text!r} → {p.intent}（错意图：窗族必须窗道）"
    assert p.args.get("action") == action, f"{text!r} action={p.args.get('action')}"
    tgt = (p.args.get("target") or [{}])[0]
    got = (tgt.get("devices") or [{}])[0].get("name")
    assert got == name, f"{text!r} name={got!r}≠{name!r}"


def test_tilt_action_not_stolen_by_bare_type(fp):
    """反向钉：裸形入表后，『内倒』作为**动作**的既有语义不许被截胡——
    「平开窗内倒」仍须 action=a（v1.1.1 #5 内倒语序洞的同族防线）。"""
    p = _route(fp, "平开窗内倒")
    assert p is not None and p.intent == "ControlWindow"
    assert p.args.get("action") == "a", f"内倒动作被裸窗型截胡：{p.args}"


# ── ② 结构钉：六表同步 + 长词在前（键序＝正确性） ─────────────────
def test_bare_form_lands_in_all_synced_tables():
    assert "内开内倒" in _WINDOW_TYPES
    w = _WINDOW_TYPES
    assert w.index("内开内倒窗") < w.index("内开内倒"), "长词必须在前（截胡纪律）"
    assert "内开内倒" in T.KNOWN_DEVICES_PREFIX, "前缀剥离表漏裸形（守卫只查超集，防不住反向漏）"
    assert "内开内倒" in T._KNOWN_DEVICES_TAIL, "尾表漏裸形"


def test_integration_tables_have_bare_form_long_first():
    """集成侧无 HA 环境＝按本仓惯例钉源文本（test_multi_device_and_opener:376 同风格）。"""
    const = (HERE / "custom_components" / "huijian_ai" /
             "intent_window_const.py").read_text(encoding="utf-8")
    mapping = const[const.index("WINDOW_NAME_MAPPING = {"):const.index("WINDOW_ALL_NAMES")]
    assert '"内开内倒"' in mapping, "WINDOW_NAME_MAPPING 缺裸形（设备名段认不出）"
    assert mapping.index('"内开内倒窗"') < mapping.index('"内开内倒"'), "长词键必须在前"
    shared = (HERE / "custom_components" / "huijian_ai" /
              "intent_device_shared.py").read_text(encoding="utf-8")
    assert '"内开内倒"' in shared, "WINDOW_KEYWORDS 缺裸形"

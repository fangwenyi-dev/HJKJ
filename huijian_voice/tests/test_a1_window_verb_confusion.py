# -*- coding: utf-8 -*-
"""A1 修复批：STT 听岔「关闭→官闭/公式/光闭」不得把窗反向打开（审计 2026-10-17 入账）。

现场（发布树 5be116e 复跑，真 TextCNN+真 FastPath，本会话与外部审计两路同实锤）：
「官闭/公式/光闭平开窗」TextCNN 判 ControlWindow（0.86/0.89/0.84 过 0.7 阈）后，
t1 支 `_scan_window_action(text)` **裸扫整句**：「平开窗」名内的「开」配中 open 档
（听岔形配不上 `关|close`，corrector A7 判例明令动词极性形**永远不入纠错表**）
⇒ 用户要关、action=open 真按「平开窗 ① 开启」（现网 .91 实测该设备在位）。
同族「公式内开内倒」v1.5.6 对照树 MISS 查无、v1.5.7 裸形入表后经 t1 判出 a＝
内倒真下发（加重实锤，A/B 差分已入账）。

修法＝t1 主支与 T1-retry 支同闸 `_t1_window_action`：剥净句内全部窗名语素
（复用 _window_type/_opener_word 单点，不建第二张表）再扫；剥完只剩名＝
没说要做什么 → None → 既有「ControlWindow 无动作词」_miss 如实收口。
**宁可不接，不许接反**。

本文件两类钉：
① 行为面（真级联入口序：先验改写→fast_path.match，与 test_v157 同量具）；
② 接线面（v1.54 教训：只加函数不接线＝没修——扫描调用点必须走新闸、
  两处裸扫不得回潮）。
"""
import asyncio
import copy
import os
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

from core.settings import DEFAULTS                       # noqa: E402
from core.nlu import fast_path as FP                     # noqa: E402
from core.nlu.fast_path import FastPath, _t1_window_action  # noqa: E402
from core.nlu.textcnn import TextCNN                     # noqa: E402
from core.nlu import homophone as H                      # noqa: E402


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


# 与 test_v157 同口径的先验宇宙（办公室真清单派生）
PRIOR = H.prior_names(["悬窗 开窗器", "推拉窗 开窗器", "内开窗 开窗器",
                       "平开窗 开窗器", "内开内倒 开窗器", "办公室射灯"])


def _route(fp, text):
    """复刻生产单点顺序：先验改写 → fast_path.match。"""
    out, _ = H.rewrite(text, PRIOR)
    return asyncio.run(fp.match(out))


# ── ① 行为钉·A1 主案：听岔关闭形绝不反向下发 ────────────────────────
# 判据强度来源：真 TextCNN 对这些形给 ControlWindow 0.83~0.95 过阈，
# 病灶只可能出在动作档；本批修复后它们必须整句查无（None），
# 绝不产出 ControlWindow（哪怕带 action）。
A1_CASES = [
    "官闭平开窗",      # 审计复现 0.862 → 旧形 action=open（要关却开）
    "公式平开窗",      # 0.895 → 同上
    "光闭平开窗",      # 0.835 → 同上
    "官闭内开内倒",
    "公式内开内倒",    # v1.5.7 加重形：旧形 action=a 内倒真下发（1.5.6 树此形 MISS）
    "光闭内开内倒",
    # v1.6.6 刀1（2026-10-19 复核当场用发布函数复现）：复合窗型名「平开内倒窗/
    # 外开内倒窗」六表全缺，剥名剥到子串 内倒窗 就停 ⇒ 残余「官闭平开」里的名内
    # 「开」字被当动作，旧码 `_t1_window_action('官闭平开内倒窗')=='open'`＝要关却开。
    "官闭平开内倒窗",
    "公式平开内倒窗",
    "光闭外开内倒窗",
    "官闭平开内倒",
]


@pytest.mark.parametrize("text", A1_CASES)
def test_a1_misheard_close_never_opens_window(fp, text):
    p = _route(fp, text)
    assert p is None, (
        f"{text!r} 仍被接管 → {p.intent} action={p.args.get('action')!r}"
        f"（审计 A1 原形：要关却下发 {p.args.get('action')}＝反极性真执行）")


# ── ① 行为钉·反向保护：合法既有形逐字不动（宁可不接≠少接正主） ─────
KEEP_CASES = [
    ("打开平开窗", "open"),
    ("关闭平开窗", "close"),
    ("平开窗打开", "open"),
    ("打开内开内倒", "open"),
    ("关闭内开内倒", "close"),
    ("内开内倒关上", "close"),      # v1.5.7 SOV 战果
    ("把内开内倒打开", "open"),
    ("打开内开累倒", "open"),        # v1.5.6 n/l 方言形
    ("打开推那窗", "open"),
    ("打开客厅窗户内倒", "a"),       # v1.1.1 #5 尾位内倒战果
    ("关闭开窗器", "close"),
    ("打开平开内倒窗", "open"),      # v1.6.6 刀1 新增名（反向保护：新名不得吃掉正动词）
    ("关闭外开内倒窗", "close"),
    ("把平开内倒窗关上", "close"),   # SOV 形在新名上同样成立
]


@pytest.mark.parametrize("text,action", KEEP_CASES)
def test_reverse_protection_legit_shapes_unchanged(fp, text, action):
    p = _route(fp, text)
    assert p is not None, f"{text!r} 本应接管，修复后落空＝少做也是回归"
    assert p.intent == "ControlWindow", f"{text!r} → {p.intent}（错道）"
    assert p.args.get("action") == action, (
        f"{text!r} action={p.args.get('action')!r} 期望 {action!r}")


# ── ① 单元面：闸本体语义（剥名→扫残余；只剩名→None） ───────────────
@pytest.mark.parametrize("text,expect", [
    ("官闭平开窗", None),           # 剥名后「官闭」不在任何档
    ("平开窗", None),               # 只剩名＝没说要做什么
    ("开窗器", None),
    ("关闭平开窗", "close"),
    ("打开客厅窗户内倒", "a"),       # 表序 内倒>…>开 保持
    ("关闭开窗器", "close"),
    ("公式内开内倒", None),          # 剥「内开内倒」剩「公式」
])
def test_t1_window_action_unit(text, expect):
    assert _t1_window_action(text) == expect, text


# ── ② 接线钉：两处扫描调用点必须走 _t1_window_action（v1.54 教训：
#    加了函数不接线＝没修；旧裸扫回潮＝白修） ─────────────────────────
_SRC = (HERE / "core" / "nlu" / "fast_path.py").read_text(encoding="utf-8")


def test_wiring_t1_branch_uses_tail_scan():
    # t1 主支与 retry 支各一次调用，且**不许**残留对整句的裸扫
    calls = re.findall(r"act2? = _t1_window_action\(text\)", _SRC)
    assert len(calls) == 2, f"接线面：_t1_window_action 应恰 2 处调用，实 {len(calls)}"
    bare = re.findall(r"act2? = _scan_window_action\(text\)", _SRC)
    assert not bare, f"接线面：裸扫整句回潮 {bare}（A1 病灶本体）"


def test_retry_branch_refuses_actionless_window():
    # retry 支扫不出动作必须置 t1_intent=None 跳过接管（防无 action 窗计划
    # 落到执行器被念成「已调节」/集成 action_map 撞空键）。
    # 逐行结构判据（不靠缩进正则——那是量具自身脆皮，v1.5.3 路径钉同族教训）：
    lines = _SRC.splitlines()
    # 1) act2 取不到时的 else 分支里存在 t1_intent = None
    try:
        i = next(n for n, l in enumerate(lines)
                 if "act2 = _t1_window_action(text)" in l)
    except StopIteration:
        pytest.fail("retry 支未接 _t1_window_action")
    seg = lines[i:i + 12]
    assert any("t1_intent = None" in l for l in seg), "retry 支缺『扫不出→不接管』闸"
    # 2) 接管（trace T1-retry / _build_plan）必须包在 if t1_intent: 里
    j = next(n for n, l in enumerate(lines) if 'trace.append(f"T1-retry' in l)
    assert any(re.match(r"^\s*if t1_intent:\s*$", l) for l in lines[i:j + 1]), \
        "retry 接管未被 if t1_intent: 守卫（None 会穿透建计划）"


# ── v1.6.6 刀1：类守卫（判据来自表本身，不靠手写例子） ─────────────────
_MISHEARD_CLOSE = ("官闭", "公式", "光闭")   # corrector A7 明令禁入纠错表的听岔形


def test_class_guard_no_window_name_yields_open_under_misheard_close():
    """任何窗名（含**表外超集形**）前挂听岔关闭词，一律不得判成 open。

    名字宇宙是派生的、不是手写的：母表全词 ∪ 机型词 ∪ {名内词根 × 常见尾}。
    1.5.7『裸形』与 1.6.0『官闭平开窗』两批都只靠手写例子守，第三形
    （平开内倒窗）就漏过去并当场可复现——本钉把"下一形"一起守住。
    宇宙为空＝扫描面失效，先响亮红。
    """
    from core.nlu import fast_path as fp

    names = set(fp._WINDOW_TYPES) | set(fp._WINDOW_OPENER_WORDS)
    for root in fp._WINDOW_NAME_POLARITY_ROOTS:
        for tail in ("内倒窗", "窗", "开窗器", "内倒"):
            names.add(root + tail)
    assert len(names) >= 20, f"窗名宇宙塌缩（扫描面失效）：{sorted(names)}"

    bad = [f"{p}{n}" for p in _MISHEARD_CLOSE for n in sorted(names)
           if fp._t1_window_action(p + n) == "open"]
    assert not bad, f"听岔关闭句被判成 open＝反极性真下发：{bad}"
    # 同一宇宙：剥名后只剩名字的句必须整句查无（绝不凭空造动作）
    only_names = [n for n in sorted(names) if n in fp._WINDOW_TYPES]
    silent = [n for n in only_names if fp._t1_window_action(n) not in (None,)]
    assert not silent, f"只报了名没说要做什么，却凭空造出动作：{silent}"

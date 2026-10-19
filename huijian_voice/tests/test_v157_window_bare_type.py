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


def _literal(name, src_text):
    """ast 取源文件**顶层**字面量（子串文本钉会被注释与泛称值满足＝v1.6.5 在册坑）。

    1.6.5 时它是本文件某条测试的闭包局部件；被第二条测试引用即 NameError，
    故提到模块级共用一份判据（复刻第二处＝两条测试可以各绿各的）。
    """
    import ast
    tree = ast.parse(src_text)
    for node in tree.body:
        # v1.6.6 刀12：旧形写 `getattr(node, "targets", [None])[0] is not None`，
        # 对 AnnAssign（`X: Final = [...]` 没有 targets 属性）取到 [None]→[0] is None
        # ⇒ 整条 AnnAssign 支**从没执行过**（死支），本钉当时只靠 Assign 支蒙对。
        # 集成侧一旦改用带类型标注的写法就假红。按类型分派取目标名，不再猜属性。
        if isinstance(node, ast.Assign):
            if not node.targets or not isinstance(node.targets[0], ast.Name):
                continue
            tgt, val = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            if not isinstance(node.target, ast.Name) or node.value is None:
                continue
            tgt, val = node.target, node.value
        else:
            continue
        if tgt.id == name:
            return ast.literal_eval(val)
    raise AssertionError(f"源里找不到字面量 {name}（整表被搬走也红）")


def test_integration_tables_have_bare_form_long_first():
    """集成侧无 HA 环境＝按本仓惯例钉源文本（test_multi_device_and_opener:376 同风格）。

    v1.6.5（刀5-c）：旧形是**子串文本钉**——`'"内开内倒"' in mapping` 只证明这串字
    在文件里出现过。两种变异都躲得过去（工单在册）：①把裸形键挪进别的 EXCLUDE
    表、mapping 里留个注释性字符串；②值改成泛称（"内开内倒": "窗"），键子串仍在、
    语义已坏，133/242 全不红。改 **ast 判据**：解析真字面量——mapping 收为 dict、
    键序取插入序、键→值**全等**核；WINDOW_KEYWORDS 收为 list 的**元素级成员**核。
    注释与别的表里的同名串从此不算数。"""
    from pathlib import Path
    const_src = (HERE / "custom_components" / "huijian_ai" /
                 "intent_window_const.py").read_text(encoding="utf-8")

    mapping = _literal("WINDOW_NAME_MAPPING", const_src)
    assert isinstance(mapping, dict) and mapping.get("内开内倒") == "内开内倒", \
        f"裸形键缺失或值被改成泛称（假绿在册变异②）：{mapping.get('内开内倒')!r}"
    keys = list(mapping)
    assert keys.index("内开内倒窗") < keys.index("内开内倒"), "长词键必须在前（截胡纪律）"

    shared_src = (HERE / "custom_components" / "huijian_ai" /
                  "intent_device_shared.py").read_text(encoding="utf-8")
    kws = _literal("WINDOW_KEYWORDS", shared_src)
    assert isinstance(kws, list) and "内开内倒" in kws, \
        "WINDOW_KEYWORDS 缺裸形元素（成员级判据，注释串不作数）"


def _prompt_window_names():
    """LLM 槽位引导表（intent_window_control 的 "Valid window names:"）按 ast 取名单。

    子串/文本钉挡不住这一面：`"内开内倒" in 源文件` 在漏改时**本来就绿**
    （它是「内开内倒窗」的子串）＝典型假绿。故按成员级取。
    """
    import ast as _ast
    ctl_src = (HERE / "custom_components" / "huijian_ai" /
               "intent_window_control.py").read_text(encoding="utf-8")
    for node in _ast.walk(_ast.parse(ctl_src)):
        if (isinstance(node, _ast.Constant) and isinstance(node.value, str)
                and "Valid window names:" in node.value):
            seg = node.value[node.value.index("Valid window names:"):]
            return [x.strip() for x in
                    seg[len("Valid window names:"):].split(". ")[0].split(",")
                    if x.strip()]
    return None


def test_window_type_master_list_synced_across_faces():
    """六表纪律的**一般式**：母表 `_WINDOW_TYPES` 每个词必须在其余各面都出现。

    1.5.7「六表齐」真齐的是五张（LLM 面漏裸形），刀1 又发现复合窗型名（平开内倒窗）
    **六表全缺**——两次同一成因：手改一遍、漏一面没人知道。本钉按母表遍历，
    下次新增窗型漏任何一面当场红，不再依赖"作者记得六处都要改"。
    """
    cc = HERE / "custom_components" / "huijian_ai"
    mapping = _literal("WINDOW_NAME_MAPPING",
                       (cc / "intent_window_const.py").read_text(encoding="utf-8"))
    kws = _literal("WINDOW_KEYWORDS",
                   (cc / "intent_device_shared.py").read_text(encoding="utf-8"))
    prompt = _prompt_window_names()
    assert prompt and kws and mapping, "某一面整表消失（判据宇宙塌缩＝假绿起点）"
    prefix = set(T.KNOWN_DEVICES_PREFIX)
    tail = set(T._KNOWN_DEVICES_TAIL)
    missing = {}
    for w in _WINDOW_TYPES:
        for face, universe in (("targets 前缀表", prefix), ("targets 尾表", tail),
                               ("集成 WINDOW_NAME_MAPPING", set(mapping)),
                               ("集成 WINDOW_KEYWORDS", set(kws)),
                               ("LLM valid names", set(prompt))):
            if w not in universe:
                missing.setdefault(w, []).append(face)
    assert not missing, f"母表词漏面（六表纪律一般式）：{missing}"


def test_llm_prompt_window_names_complete_and_long_first():
    """第六面＝LLM 槽位引导表（intent_window_control 里的 "Valid window names:"）。

    1.5.7 号称"六表齐"，真齐的是五张——这面漏了，而本仓的六表定义早把它算进去
    （test_en_targets_and_window_rescue 对 电动窗 就钉 "intent 描述 valid names 未同步"）。
    子串钉挡不住这种漏法：`"内开内倒" in ctl` 在漏改的文件里**本来就绿**（它是
    「内开内倒窗」的子串）＝典型假绿。判据因此取两件实事实：
      ① 名单用 ast 从常量串里取（注释与别处散文不作数）；
      ② mapping 宇宙里每个中文具名都必须在表中——少一个响亮红（下次新增窗型漏这面当场红）。
    **不**钉这张清单的键序：它是喂 LLM 的话术清单，没有任何代码按序取它（截胡纪律
    属解析表那一侧，本文件另两条测试已在那侧钉住）。写这条判据时曾把存量形制
    （单内倒窗 排在 内倒窗 之后）误判成缺陷——按"盘上实际交付形状"写钉，别按计划写。
    豁免只有两条且写明理由：泛称「窗」不进槽位引导（LLM 要抽具名），'pingtui' 是
    实体英文别称、不属中文窗型话术。新增豁免必须在此解释，不许默默加。
    """
    cc = HERE / "custom_components" / "huijian_ai"
    prompt = _prompt_window_names()
    assert prompt, "摘不到 LLM 的 Valid window names 常量串（引导面形制变了）"

    mapping = _literal("WINDOW_NAME_MAPPING",
                       (cc / "intent_window_const.py").read_text(encoding="utf-8"))
    assert isinstance(mapping, dict) and mapping, "WINDOW_NAME_MAPPING 取不到真字面量"
    universe = {n for n in set(mapping) | set(mapping.values()) if not n.isascii()}
    EXCLUDED = {"窗"}
    missing = sorted((universe - EXCLUDED) - set(prompt))
    assert not missing, f"LLM 槽位引导表漏名（用户说了不认）：{missing}"
    # 本批两形点名（1.5.7 漏改的正是它们）
    assert "内开内倒" in prompt and "内倒窗" in prompt, prompt


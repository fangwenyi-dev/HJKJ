# -*- coding: utf-8 -*-
"""v1.6.7 刀12：时刻句尾缀「后」不得漏进播报（现网 1.6.6 首测实锤）。

现网留痕（192.168.1.91，1.6.6，2026-10-19 17:27:34）：
    [执行] HassCreateAutomation {'trigger': {'at': '18:30'}, ...} → 成功
    [级联] '每天下午6点30分后打开客厅灯' → [creation]
           '好的，语音自动化已创建：每天晚上6点半的时候，就后打开客厅灯。…'

根因两格，缺一不可：
① `creation._AUTO_TIME_RE` 只到 `(?:时候|时)?`，**不吃**时刻后的 `后/以后/之后`
   ⇒ `y` 带着尾缀出去；
② `pipeline.py:1848` 的模板 `f"…，就{y_say}"` 原样拼接 ⇒ 「就后打开客厅灯」。
这条洞 1.6.6 之前看不见——那类句子会被延时闸整句拒排，根本走不到创建链；刀4 把
路放开后它才第一次被说出口（＝既有缺陷被本批放开路径显形，不是本批写坏）。

修法两处：正则吃尾缀（与 delay.py `_MID` 同族）＋ 时间残片闸点名收「只剩尾缀」
的无动作形。**光靠吃尾缀收不严**：整句无动作时正则回溯会把 `之后` 让给 y
（本机实测 y='之后'），所以残片闸是必需的第三格，不是可选项。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CREATION = _ROOT / "core" / "nlu" / "creation.py"
_PIPELINE = _ROOT / "core" / "pipeline.py"

import sys  # noqa: E402
sys.path.insert(0, str(_ROOT.parent))

from core.nlu import creation  # noqa: E402

# 现网实测会串字的四形（1.6.6 上 y 一律以 后/之后/以后 开头）
_LEAKY = [
    ("每天下午6点30分后打开客厅灯", "18:30", "打开客厅灯"),
    ("每天6点后打开办公室的灯", "06:00", "打开办公室的灯"),
    ("每天晚上8点之后打开射灯", "20:00", "打开射灯"),
    ("每天下午6点半以后把客厅灯关掉", "18:30", "客厅灯关掉"),
]

# 既有战果形：本刀一个字符都不许改（含 v1.1.38 的「零五分」复述修复形）
_KEEP = [
    ("每天下午6点30分打开客厅灯", "18:30", "打开客厅灯"),
    ("每天7点打开办公室的灯", "07:00", "打开办公室的灯"),
    ("每天早上七点零五分打开窗帘", "07:05", "打开窗帘"),
    ("每天六点半的时候就把会议室的灯打开", "06:30", "把会议室的灯打开"),
]

_SUFFIX_RE = re.compile(r"^(?:之?以?后|后)")


@pytest.mark.parametrize("text,at,y", _LEAKY)
def test_leaky_forms_now_clean(text: str, at: str, y: str) -> None:
    """串字四形：y 干净、时刻不变。"""
    c = creation.parse(text)
    assert c is not None and c["kind"] == "automation", c
    assert c["trigger"]["at"] == at, f"时刻被本刀改坏：{c['trigger']}"
    assert c["y"] == y, c["y"]
    assert not _SUFFIX_RE.match(c["y"]), "尾缀仍漏进动作正文"


@pytest.mark.parametrize("text,at,y", _KEEP)
def test_keep_forms_untouched(text: str, at: str, y: str) -> None:
    """反向保护：无尾缀的既有形逐字不变（本刀只多认一个可选尾缀）。"""
    c = creation.parse(text)
    assert c is not None and c["trigger"]["at"] == at and c["y"] == y, c


def test_say_composition_has_no_jiu_hou() -> None:
    """话术端到端一格：模板确实是 `…，就{y_say}`，而干净 y 拼出来不再说破。

    为什么两件都要钉：只钉 parse 挡不住模板那侧改形（如改成 `就{y}吧` 或把尾缀
    重新塞回去）；只钉模板挡不住 parse 漏尾缀。判据用**拼接结果**，不对源码求值。
    """
    src = _PIPELINE.read_text(encoding="utf-8")
    m = re.search(r'say = f"([^"]*)\{y_say\}([^"]*)"', src)
    assert m, "找不到 `…{y_say}…` 模板——回执改形了，本钉需同步重指"
    prefix, suffix = m.group(1), m.group(2)
    assert prefix.endswith("就"), f"模板前缀不再是「就{{y}}」拼接：{prefix!r}"
    for text, _at, y in _LEAKY + _KEEP:
        say = prefix + y + suffix
        assert not re.search(r"就(?:之?以?后)", say), say
        assert "就" + y in say, say


def test_bare_suffix_is_refused_not_built() -> None:
    """无动作形必须**如实拒建**，而不是建一条"到点执行「之后」"的废自动化。

    变异自证：只补正则、不扩残片闸 ⇒ 本条转红（回溯把 `之后` 让给 y，实测 y='之后'）。
    """
    for t in ("每天6点之后", "每天6点后", "每天7点以后", "每天7点十分"):
        assert creation.parse(t) is None, f"{t} 不该建成自动化：{creation.parse(t)}"


def test_regex_and_guard_actually_changed() -> None:
    """机制钉：尾缀在正则里、尾缀在残片闸里——两处缺一处本钉红（防"只改注释"）。"""
    src = _CREATION.read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            tgts = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in tgts:
                if isinstance(t, ast.Name):
                    names.add(t.id)
                    if isinstance(node.value, ast.Call) and node.value.args:
                        lit = node.value.args[0]
                        if isinstance(lit, ast.Constant) and isinstance(lit.value, str):
                            names.add(f"{t.id}={lit.value}")
    rx = next((v.split("=", 1)[1] for v in names if v.startswith("_AUTO_TIME_RE=")), "")
    rd = next((v.split("=", 1)[1] for v in names if v.startswith("_TIME_RESIDUE_RE=")), "")
    assert "之?以?后" in rx, "_AUTO_TIME_RE 未吃时刻尾缀"
    assert "之?以?后" in rd, "_TIME_RESIDUE_RE 未收『只剩尾缀』的无动作形"

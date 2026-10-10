# -*- coding: utf-8 -*-
"""三端意图契约钉（v1.0.20 实锤教训固化）。

背景：v1.0.20 给 fast_path 开了 HassUnlock/HassLock 车道，但 HA core **没有**
这两个内置意图（intent_builtin 文档 + core 全树 intent.py 清点实查），集成端
也没注册——真机上解锁令必 Unknown intent；而单测/E2E 用的 intent 替身恒成功，
整条验收线全绿放行了坏功能。本文件把「发射名 ⊆ 可执行名」变成可执行契约：
以后任何人给 NLU 加新车道，若执行面无人接，这里当场红。
"""
import ast
import re
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
CC = HERE / "custom_components" / "huijian_ai"

# HA core 确实内置的意图（2026-09-12 按 developers.home-assistant intent_builtin
# + core components/*/intent.py 全树清点核定；只列本仓会用到的）
HA_CORE_INTENTS = {
    "HassTurnOn", "HassTurnOff", "HassToggle",
    "HassClimateSetTemperature", "HassClimateGetTemperature",
    "HassOpenCover", "HassCloseCover",
    "HassHumidifierMode", "HassHumidifierSetpoint",
    "HassGetCurrentTime", "HassGetCurrentDate", "HassGetCurrentWeather",
    "HassShoppingListAddItem", "HassShoppingListLastItems",
}

# 词表命中但**永不派发到执行面**的名字（fast_path 显式丢弃落上层）——
# PlayMusic：模式 B 无小智播放面，:815 `return self._miss(...)`，设计豁免。
# HuijianEndConversation（v1.0.93 退下词表）：纯会话控制意图，裁决命中即在
# pipeline._cascade **当场收口**（固定话术 + Reply.end_dialogue 旗），链式句
# 的分句也在 _try_compound 滤除——设计上永不到 /api/intent/handle。豁免有据：
# 行为钉 tests/test_v1093_end_dialogue.py 以 executor 桩「一碰即红」+链中
# extra_steps 不含本名双证，不是靠注释自证。
LOCAL_ONLY_INTENT_NAMES = {"PlayMusic", "HuijianEndConversation", "TriggerHaScene",
                           "ClarifyNativeScene"}
LOCAL_ONLY_INTENTS = set(LOCAL_ONLY_INTENT_NAMES)
# ClarifyNativeScene（P4④，2026-10-10）：原生场景**同名多条/名字相撞**时发射的澄清哨兵。
# 它**没有任何执行面**，也不该有：`pipeline._cascade` 与 `_chain_decide` 见名即转
# `Reply(…, "clarify", ok=False)` 列出候选并零下发（同 HuijianEndConversation 的先例）。
# 依据不是注释：tests/test_p4_native_scenes.py 用 `RecExecutor` 断言"一进执行面即红"，
# 另有源码级钉断言两个接入点都在（见 test_local_only_exemptions_are_justified）。
# TriggerHaScene（P4，2026-10-10 HA 原生场景）：执行面在 core 侧——`Executor.run` 见名
# 即走 `call_service("scene","turn_on",{entity_id})`，**永不**进 /api/intent/handle
# （集成的场景库只认自己登记的 trigger_phrase，把原生场景发过去＝"回执成功而什么都没
# 发生"那类假成功）。豁免依据同前两名一样是行为钉，不是注释：
# tests/test_p4_native_scenes.py 断言 `ha.intent_calls == []`（一发到集成即红）＋
# `ha.svc_calls == [("scene","turn_on",{"entity_id": …})]` 双证，另有一条反向钉：
# 缺 entity_id 时**不得**播成功。见 test_local_only_exemptions_are_justified。


def _registered_intents() -> set[str]:
    """集成端注册表：所有 handler 文件的 intent_type 字面量。"""
    names: set[str] = set()
    for py in CC.glob("*.py"):
        names |= set(re.findall(r'intent_type\s*=\s*"([^"]+)"',
                                py.read_text(encoding="utf-8")))
    return names


def real_executable_intents() -> set[str]:
    """真实可执行意图全集（集成注册 ∪ HA core 内置）。
    conftest 的 FakeHAClient 以此为默认裁决——替身不得比真机更宽容
    （v1.0.20 教训：恒成功替身放行坏功能）。
    """
    return _registered_intents() | set(HA_CORE_INTENTS)


def _emitted_intents() -> set[str]:
    """加载项可能发射的意图名：fast_path 动作车道 + intent 改道字面量。

    v1.0.52：改 AST 结构提取。旧 regex 版有实锤漏网——
    ① `re\\.compile\\([^)]*\\)` 在 pattern 含 `)`（如 `(?!帘)`）时整行断扫；
    ② `intent\\s*=\\s*"X"` 匹配不到元组赋值 `intent, tag = "PauseDevice", "家电暂停"`
    （fast_path.py:561，实测 PauseDevice 曾被静默漏掉——现无实害仅因集成恰好
    注册了它，属侥幸非防线）。AST 提取不依赖文本形态，堵这两个洞。
    """
    src = (HERE / "core" / "nlu" / "fast_path.py").read_text(encoding="utf-8")
    names: set[str] = set()

    def _is_intentish(v):
        return isinstance(v, str) and re.fullmatch(r"[A-Z][A-Za-z0-9]+", v)

    tree = ast.parse(src)
    for node in ast.walk(tree):
        # 目标名含 intent 的赋值：intent = "X" / intent, tag = "X", "…" / self.intent… = "X"
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            hit = any(
                (isinstance(t, ast.Name) and "intent" in t.id.lower())
                or (isinstance(t, ast.Attribute) and "intent" in t.attr.lower())
                or (isinstance(t, (ast.Tuple, ast.List)) and any(
                    isinstance(e, ast.Name) and "intent" in e.id.lower()
                    for e in t.elts))
                for t in targets
            )
            if not hit or node.value is None:
                continue
            vals = (node.value.elts
                    if isinstance(node.value, (ast.Tuple, ast.List))
                    else [node.value])
            for e in vals:
                if isinstance(e, ast.Constant) and _is_intentish(e.value):
                    names.add(e.value)
        # _ACTION_PATTERNS 条目：(re.compile(...), "IntentName")——第二列过
        # intentish 滤网：_ADJUST_REWRITES 等形似结构（re.compile, "close"/"$1"）
        # 的第二列是动作/替换串，不是意图名。
        if isinstance(node, ast.Tuple) and len(node.elts) >= 2:
            first = node.elts[0]
            if (isinstance(first, ast.Call)
                    and isinstance(first.func, ast.Attribute)
                    and first.func.attr == "compile"):
                second = node.elts[1]
                if isinstance(second, ast.Constant) and _is_intentish(second.value):
                    names.add(second.value)
        # keyword 形态：Plan(intent="X") / some_call(intent="X")
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg and "intent" in kw.arg.lower() \
                        and isinstance(kw.value, ast.Constant) \
                        and _is_intentish(kw.value.value):
                    names.add(kw.value.value)
    return names


def test_scanner_catches_tuple_form_intent():
    """防再漏钉：元组赋值形态的 PauseDevice 必须在 emitted 集合里
    （旧 regex 实锤漏网的那个）。这条红了 = 扫描器又瞎了，不是业务坏了。"""
    emitted = _emitted_intents()
    assert "PauseDevice" in emitted, (
        f"扫描器漏发 PauseDevice——AST 提取退化，发射名集合不可信：{sorted(emitted)}")


def test_every_emitted_intent_has_a_handler():
    registered = _registered_intents()
    emitted = _emitted_intents()
    assert emitted, "扫描失效：fast_path 未提取到任何意图名"
    unhandled = {n for n in emitted if n not in registered
                 and n not in HA_CORE_INTENTS
                 and n not in LOCAL_ONLY_INTENTS}
    assert not unhandled, (
        f"新车道发射名无执行面：{sorted(unhandled)}——"
        "须在 custom_components/huijian_ai/intent*.py 注册 handler"
        "（或确认 HA core 内置后加入 HA_CORE_INTENTS）")


def test_local_only_exemptions_are_justified():
    """豁免表不得烂成"随手加名字"：每个 LOCAL_ONLY 名字都要有**真实的本地落点**。

    三条判据（任一不满足即红）：
      · 它确实**没有**注册在集成端——否则它根本不需要豁免，写在这张表里只会掩盖真相；
      · core 侧源码（executor / pipeline / fast_path）里存在对该名字的**处理点**
        （执行、当场收口或显式丢弃），不是凭注释自称本地；
      · TriggerHaScene 这类"改名不改道"的坑单独加一条：必须由 `call_service` 承接，
        源码里出现"发往 handle_intent 的分支"覆盖它即红（那是假成功通道）。
    """
    reg = _registered_intents()
    core_src = {
        "executor.py": (HERE / "core" / "executor.py").read_text(encoding="utf-8"),
        "pipeline.py": (HERE / "core" / "pipeline.py").read_text(encoding="utf-8"),
        "fast_path.py": (HERE / "core" / "nlu" / "fast_path.py").read_text(encoding="utf-8"),
    }
    for n in sorted(LOCAL_ONLY_INTENTS):
        assert n not in reg, f"{n} 已在集成注册，却仍列在 LOCAL_ONLY——豁免掩盖事实"
        where = [f for f, src in core_src.items() if f'"{n}"' in src]
        assert where, (f"{n} 在 core 侧没有任何处理点：豁免无据，"
                       "要么实现本地执行、要么从 LOCAL_ONLY_INTENTS 移除")
    # TriggerHaScene 专项：本地执行必须是服务直调，且不得被路由到集成意图通道
    ex = core_src["executor.py"]
    assert 'name == "TriggerHaScene"' in ex, "P4 本地执行分支被摘（原生场景会走假成功通道）"
    assert '"scene", "turn_on"' in ex, "P4 未用 scene.turn_on 服务直调"
    # ClarifyNativeScene 专项：它必须**没有**执行面，只在级联两处当场收口
    from core.pipeline import Pipeline
    import inspect as _ins
    cas, chain = _ins.getsource(Pipeline._cascade), _ins.getsource(Pipeline._chain_decide)
    assert "SCENE_CLARIFY_INTENT" in cas, "单发链路的场景澄清收口被摘（会退化成泛化兜底）"
    assert "SCENE_CLARIFY_INTENT" in chain, "链内分句的场景澄清收口被摘（会静默丢腿）"
    assert "SCENE_CLARIFY_INTENT" not in ex, "澄清哨兵竟有执行面：它可能真被下发成服务调用"


def test_lock_family_registered_in_integration():
    """v1.0.20 缺口回归钉：解锁/上锁执行面必须在集成端。"""
    reg = _registered_intents()
    assert {"HassUnlock", "HassLock"} <= reg, reg
    lock_src = (CC / "intent_lock.py").read_text(encoding="utf-8")
    assert "except Exception" in lock_src, "锁 handler 失守永不抛纪律"
    intent_py = (CC / "intent.py").read_text(encoding="utf-8")
    assert "HassUnlockIntent()" in intent_py and "HassLockIntent()" in intent_py


def test_executor_unlock_speech():
    """话术名实相符：解锁播报走专支（不许裸"好的"糊弄）。"""
    ex = (HERE / "core" / "executor.py").read_text(encoding="utf-8")
    assert 'if intent in ("HassUnlock", "HassLock")' in ex
    assert "已解锁" in ex and "已上锁" in ex

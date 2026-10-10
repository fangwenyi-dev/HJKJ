# -*- coding: utf-8 -*-
"""P4′ **真接线钉**（2026-10-11 双臂事故直接补洞）。

上一轮 16 条 P4 钉全绿的同时，真机双臂 #20/#21 落 fallback——因为既有钉在 FastPath
层直调，而 `SceneCache.check_native` 里的 `except Exception → ("none", None)` 把一次
**代码级 NameError**（10-11 `_fix_selfshadow.py` 改名 `claimed`→`claimed_raw` 漏改
L327）折叠成了"未命中"。判据崩溃与判据不认领在替身面前长得一模一样，套件全绿也自欺。

本文件补三条真接线的性质钉：
  ① 穿 `Pipeline._cascade → FastPath → 真实 SceneCache（假 ha 只假网络）→ 真实
     Executor` 全链：`观影场景`/`执行观影场景` 必须真产出 `scene.turn_on` 服务调用；
  ② **check_native 不得吞代码级异常**：把 `_occupied` 打成坏类型（None 不可迭代），
     旧写法 except 折叠 ⇒ 返回 none、套件毫无动静；新语义＝**异常照抛到日志 WARNING
     级**且调用方不被带崩——本钉用 caplog 钉住"异常被记录"而不是静默；
  ③ 裸名/撞名设备/同名多条三条安全边界在**全链**下同样成立（零服务调用）。
"""
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.executor import Executor                    # noqa: E402
from core.nlu.scenes import SceneCache                # noqa: E402
from core.pipeline import Pipeline                    # noqa: E402

SCENE_EID = "scene.guan_ying"


class Ha:
    """只假网络口的"真客户端"：`_states`/`_entity_area`/`_entity_alias` 与真
    HAClient 同名同形，Pipeline 的区域预检/能力解析吃的就是这三样。"""

    def __init__(self, entities=None, voice=None):
        self._states = dict(entities or {})
        self._entity_area = {}
        self._entity_alias = {}
        self._voice = voice or []
        self.intent_calls = []
        self.svc_calls = []

    async def handle_intent(self, name, data, timeout=5.0):
        self.intent_calls.append((name, data))
        if name == "HassListVoiceScenes":
            return {"success": True, "scenes": self._voice}
        return {"success": True}

    async def call_service(self, domain, service, data, timeout=10.0):
        self.svc_calls.append((domain, service, data))
        return {"success": True}

    async def states(self):
        return dict(self._states)

    async def refresh_states(self, force=False):
        return None


def ent(eid, fn, state="on"):
    return eid, {"state": state, "attributes": {"friendly_name": fn}}


class S:
    def __init__(self, **kw):
        self.d = {"nlu.textcnn_enabled": False, "llm.enabled": False, **kw}

    def get(self, k, default=None):
        return self.d.get(k, default)


class StubKlar:
    async def match(self, text, origin=""):
        return None


def _pipe(ha):
    sc = SceneCache(ha)
    asyncio.run(sc.refresh(force=True))          # 建表走真 refresh（读真 _states）
    ex = Executor(ha, S())
    return Pipeline(S(), ha=ha, scenes=sc, textcnn=None, executor=ex,
                    agent=None, klar=StubKlar())


def _casc(pipe, text):
    return asyncio.run(pipe._cascade(text, "pin"))


# ── ① 全链：说了「场景」就必须真下发 ───────────────────────────────
def test_pipeline_full_wiring_native_scene_dispatches_turn_on():
    ha = Ha([ent(SCENE_EID, "观影"), ent("light.a", "射灯")])
    for utt in ("观影场景", "执行观影场景", "观影的场景"):
        ha.svc_calls.clear()
        rep = _casc(_pipe(ha), utt)
        assert rep.ok, (utt, rep.text, rep.source)
        assert ha.svc_calls == [("scene", "turn_on", {"entity_id": SCENE_EID})], \
            f"{utt} 穿全链没有真下发 scene.turn_on：svc={ha.svc_calls} source={rep.source}"
        # 绝不许再借集成意图通道（库里没有这条场景）
        assert all(n != "HassTriggerVoiceScene" for n, _ in ha.intent_calls), ha.intent_calls


# ── ② 判据崩溃不得折叠成"未命中"（NameError 事故的直接教训）───────
def test_check_native_programming_error_is_logged_not_swallowed(caplog):
    ha = Ha([ent(SCENE_EID, "观影")])
    sc = SceneCache(ha)
    asyncio.run(sc.refresh(force=True))
    sc._occupied = None                       # 人为打坏内部状态：迭代必 TypeError
    with caplog.at_level(logging.WARNING, logger="huijian.scenes"):
        kind, _ = sc.check_native("观影场景")
    assert kind == "none", "坏状态不得产出命中（这条边界保留）"
    recs = [r for r in caplog.records if "原生场景匹配异常" in r.getMessage()]
    assert recs and recs[0].levelno == logging.WARNING, \
        "判据崩溃必须到 WARNING（生产可见）；折叠成 debug/静默＝双臂全绿自欺的重演"


# ── ③ 三条安全边界在全链同样成立（零服务调用）────────────────────
def test_full_wiring_safety_edges_zero_dispatch():
    # 裸名不接管
    ha = Ha([ent(SCENE_EID, "观影")])
    rep = _casc(_pipe(ha), "观影")
    assert ha.svc_calls == [], ("裸名抢发场景", ha.svc_calls)
    # 在装设备整名撞场景名 ⇒ 让路
    ha2 = Ha([ent("switch.view", "观影"), ent(SCENE_EID, "观影")])
    rep2 = _casc(_pipe(ha2), "观影场景")
    assert ha2.svc_calls == [], ("设备在位仍被场景抢名", ha2.svc_calls)
    # 同名多条 ⇒ 不猜
    ha3 = Ha([ent("scene.a", "观影"), ent("scene.b", "观影")])
    rep3 = _casc(_pipe(ha3), "观影场景")
    assert ha3.svc_calls == [], ("同名多条竟猜发了一条", ha3.svc_calls)

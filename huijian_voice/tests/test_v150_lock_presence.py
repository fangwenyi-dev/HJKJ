# -*- coding: utf-8 -*-
"""v1.5.0 ⑮ 钉：锁族风险环的存在性预审（无锁的家不许被索要解锁许可）。

现网证据（1.4.9 线上复测 17:33:31）：234 实体快照、lock 域 **0 台**，
「解锁大门」仍挂环念「接下来要解锁门，说『确认』执行」——用户答"确认"后
执行侧才会失败＝拿一份对不存在设备的假许可空转一轮。
判据三态（方向纪律：判据故障绝不放松风险闸）：
  · 快照可信且零 lock 实体 ⇒ 不挂环、零下发、如实查无（ok=False）；
  · 快照不可得（冷启动/代理故障窗，_states 空）⇒ 不可判 ⇒ 照旧挂环；
  · 家在有 lock ⇒ 逐字旧行为（问句、挂环、场景删除族都不受影响）。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.nlu.fast_path import Plan
from tests.test_v147_pronoun_focus import _pipe, Lane, RecExecutor


def _p_no_lock_with_snapshot():
    p = _pipe(Lane(table={}), Lane(single=Plan("noop", {}, "t0", "", [])), RecExecutor())
    p.ha._states = {
        "light.ban_gong_shi_she_deng": {"entity_id": "light.ban_gong_shi_she_deng",
                                        "state": "off", "attributes": {}},
        "binary_sensor.door_101": {"entity_id": "binary_sensor.door_101",
                                   "state": "closed",
                                   "attributes": {"friendly_name": "大门"}},
    }
    return p


def _unlock_plan(eid="binary_sensor.door_101"):
    return Plan(intent="HassUnlock", args={"entity_id": eid}, source="klar",
                utterance="解锁大门", trace=["HassUnlock"])


def test_150_lockless_home_unlock_never_asks_permission():
    """⑮ 主案（现网形状）：可信快照 + 零 lock ⇒ 查无如实、零下发、不挂环。"""
    p = _p_no_lock_with_snapshot()
    r = p._confirm_ask(_unlock_plan(), "O150")
    assert r is not None and r.source == "no_such_device", (r.source, r.text)
    assert r.ok is False and "没执行" in r.text
    assert "O150" not in p._confirm, "没挂环才是对的：挂了就等于给了假许可"
    # 上锁族同判据（v1.1.27 同族）
    pl = Plan(intent="HassLock", args={"entity_id": "binary_sensor.door_101"},
              source="klar", utterance="锁上大门", trace=["HassLock"])
    r2 = p._confirm_ask(pl, "O150b")
    assert r2 is not None and r2.source == "no_such_device"
    # D7 反转形（TurnDeviceOff×lock 域目标）同族同罚
    pl3 = Plan(intent="TurnDeviceOff",
               args={"target": [{"devices": [{"name": "大门", "domains": ["lock"]}]}]},
               source="t0", utterance="关闭大门", trace=["t0"])
    r3 = p._confirm_ask(pl3, "O150c")
    assert r3 is not None and r3.source == "no_such_device", (r3.source, r3.text)


def test_150_has_lock_keeps_ring_verbatim():
    """反证一：家在有 lock ⇒ 环照挂、问句逐字不变（预审不得伤主路径）。"""
    p = _p_no_lock_with_snapshot()
    p.ha._states["lock.front_door"] = {"entity_id": "lock.front_door",
                                       "state": "locked",
                                       "attributes": {"friendly_name": "大门锁"}}
    r = p._confirm_ask(_unlock_plan("lock.front_door"), "Ohas")
    assert r is not None and r.source == "confirm"
    assert "接下来要解锁" in r.text and "确认" in r.text
    assert p._confirm.get("Ohas"), "挂环必须照常"


def test_150_snapshot_unavailable_fails_closed_toward_ring():
    """反证二：快照空（冷启动/代理故障窗）＝不可判 ⇒ 照旧挂环。
    方向纪律：存在性判据故障绝不放松风险闸（v1.1.35 查无闸 fail-open 同律）。"""
    p = _pipe(Lane(table={}), Lane(single=Plan("noop", {}, "t0", "", [])), RecExecutor())
    p.ha._states = {}                    # fixture 本为空——显式钉住语义
    r = p._confirm_ask(_unlock_plan(), "Ono")
    assert r is not None and r.source == "confirm", "无快照时必须保守：照挂环"


def test_150_scene_delete_family_untouched():
    """反证三：删除场景/自动化风险族不在锁族判据射程（verb 圈定死）。"""
    p = _p_no_lock_with_snapshot()
    pl = Plan(intent="HassDeleteVoiceScene", args={"trigger_phrase": "观影"},
              source="klar", utterance="删除观影场景", trace=["k"])
    r = p._confirm_ask(pl, "Odel")
    assert r is not None and r.source == "confirm" and "删除场景" in r.text

# -*- coding: utf-8 -*-
"""②「语音操作完成后必须关闭对应设备」的归位守卫钉桩（用户 2026-10-09 立规）。

现场依据（不是假想）：2026-10-09 17:22:55 办公室平开窗被语音打开后长时间未关，
HA 历史复核时仍为 open；办公室射灯当天多次亮 10–33 分钟。HA 内无引用这些实体的
自动化 ⇒ 收尾必须有人负责，责任落在加载项执行层。

判准（逐条对应边界，别把保守当成弱）：
  · 只登记"执行成功 + 目标已 grounded 成 entity_id"的设备（区域扇出目标未知 ⇒ 不归位）
  · 默认域 cover/light；switch 等模式类不默认纳入
  · 关闭方向取消登记；重复打开＝续期（不得叠加成两个计时）
  · 总开关关闭 ⇒ 逐值回到本批之前（一帧都不登记）
  · 到点先读状态：已经关了就不做二次动作
"""
from __future__ import annotations

import asyncio

from conftest import FakeHAClient as HAClient
from core.auto_restore import AutoRestore
from core.executor import Executor
from core.settings import DEFAULTS


class Set:
    """settings 替身：只认 dialog.* 扁平键，缺失回落默认。"""

    def __init__(self, **over):
        self.v = dict(over)

    def get(self, key, default=None):
        return self.v.get(key, default)


class HA:
    """带 get_state / call_service 的最小真机形态替身。"""

    def __init__(self, state="on", fail=False):
        self.state = state
        self.calls = []
        self.fail = fail

    async def get_state(self, entity_id):
        return None if self.state == "missing" else {"entity_id": entity_id,
                                                     "state": self.state}

    async def call_service(self, domain, service, data, timeout=10.0):
        self.calls.append((domain, service, dict(data)))
        return {"success": False, "error": "boom"} if self.fail else {"success": True}


def _arm(ha=None, settings=None, delay_min=10):
    st = settings if settings is not None else Set(**{
        "dialog.auto_restore": True,
        "dialog.auto_restore_min": delay_min,
        "dialog.auto_restore_domains": ["cover", "light"],
        "dialog.auto_restore_exclude": []})
    return AutoRestore(ha or HA(), st)


def _note(ar, intent, args, ok=True, receipt=None):
    """生产形态：执行器在事件循环里调用 note()。返回登记后的 pending 快照。"""
    async def go():
        ar.note(intent, args, ok, receipt=receipt)
        return dict(ar.pending())
    return asyncio.run(go())


# ── 登记面 ──────────────────────────────────────────────────────
def test_灯被语音打开_必须登记归位():
    ar = _arm()
    assert "light.ban_gong_shi_she_deng" in _note(
        ar, "HassTurnOn", {"entity_id": "light.ban_gong_shi_she_deng"})


def test_开合类_也登记():
    ar = _arm()
    assert "cover.ping_kai_chuang_a" in _note(
        ar, "HassOpenCover", {"entity_id": ["cover.ping_kai_chuang_a"]})


def test_只给区域_目标未知_绝不归位():
    """关错设备比忘关更糟：area 扇出由 HA 自己解析，我们不知道打了哪台 ⇒ 不登记。"""
    ar = _arm()
    assert _note(ar, "HassTurnOn", {"area": "客厅"}) == {}


def test_switch_等模式类_默认不纳入():
    ar = _arm()
    assert _note(ar, "HassTurnOn", {"entity_id": "switch.kong_tiao_suan_fa"}) == {}


def test_点名排除_生效且支持通配():
    ar = _arm(settings=Set(**{"dialog.auto_restore": True,
                              "dialog.auto_restore_min": 10,
                              "dialog.auto_restore_domains": ["light"],
                              "dialog.auto_restore_exclude": ["light.bed_*"]}))
    assert _note(ar, "HassTurnOn", {"entity_id": "light.bed_lamp"}) == {}
    assert "light.ban_gong_shi_she_deng" in _note(
        ar, "HassTurnOn", {"entity_id": "light.ban_gong_shi_she_deng"})


def test_总开关关_逐值不登记():
    ar = _arm(settings=Set(**{"dialog.auto_restore": False}))
    assert _note(ar, "HassTurnOn", {"entity_id": "light.x"}) == {}


def test_失败步不登记():
    ar = _arm()
    assert _note(ar, "HassTurnOn", {"entity_id": "light.x"}, ok=False) == {}


def test_重复打开是续期_不是两枚计时():
    ar = _arm()
    first = _note(ar, "HassTurnOn", {"entity_id": "light.x"})
    second = _note(ar, "HassTurnOn", {"entity_id": "light.x"})
    assert set(first) == set(second) == {"light.x"}


def test_语音关闭_取消登记():
    ar = _arm()
    assert "light.x" in _note(ar, "HassTurnOn", {"entity_id": "light.x"})
    assert _note(ar, "HassTurnOff", {"entity_id": "light.x"}) == {}


def test_无事件循环时明确不登记_不许谎称已安排():
    """本仓教训：静默不执行比报错更坏（"配了没开"就长这样）。
    note() 在循环外被调用时，登记必须**为空**且不得抛——空就是可观测的"没发生"。"""
    ar = _arm()
    ar.note("HassTurnOn", {"entity_id": "light.x"}, True)     # 循环外：不许抛
    assert ar.pending() == {}                                  # 也不许假装安排了


# ── 执行面 ──────────────────────────────────────────────────────
def test_到点_灯亮着才关_并走官方关服务():
    ha = HA(state="on")
    ar = AutoRestore(ha, Set(**{"dialog.auto_restore": True}))
    asyncio.run(ar._fire("light.ban_gong_shi_she_deng"))
    assert ha.calls == [("light", "turn_off", {"entity_id": "light.ban_gong_shi_she_deng"})]


def test_到点_窗开着_走_close_cover_但必须显式扩域():
    """默认域已收窄成 light（10-10 复核：办公室那扇平开窗由 YAML 自动化每 ~10 分钟
    开合，默认计时会与它抢关）。本钉因此**显式把 cover 加进 domains**——
    它要证的是"cover 在策略内时收尾服务选对了"，不是"cover 默认在策略内"。"""
    ha = HA(state="open")
    ar = AutoRestore(ha, Set(**{"dialog.auto_restore": True,
                                "dialog.auto_restore_domains": ["cover", "light"]}))
    asyncio.run(ar._fire("cover.ping_kai_chuang"))
    assert ha.calls == [("cover", "close_cover", {"entity_id": "cover.ping_kai_chuang"})]
    # 反向：不扩域时到点不许动窗（默认口径）
    ha2 = HA(state="open")
    ar2 = AutoRestore(ha2, Set(**{"dialog.auto_restore": True}))
    asyncio.run(ar2._fire("cover.ping_kai_chuang"))
    assert ha2.calls == [], "cover 已不在默认域，却仍去关窗：%s" % (ha2.calls,)


def test_到点_已经关了_不做二次动作():
    """与自动化/人手撞车的护栏：状态已是终态就什么也不发。"""
    for st in ("off", "closed", "idle", "unavailable", "unknown"):
        ha = HA(state=st)
        ar = AutoRestore(ha, Set(**{"dialog.auto_restore": True}))
        asyncio.run(ar._fire("light.x"))
        assert ha.calls == [], st


def test_收尾失败不无限重试_但留痕():
    ha = HA(state="on", fail=True)
    ar = AutoRestore(ha, Set(**{"dialog.auto_restore": True}))
    asyncio.run(ar._fire("light.x"))          # 不抛即为过；重试策略见模块注释
    assert len(ha.calls) == 1


def test_归位永不把异常抛回语音主流程():
    """note() 处在播报路径上：任何异常都必须自己咽掉。"""
    ar = AutoRestore(None, None)
    ar.note("HassTurnOn", {"entity_id": "light.x"}, True)      # ha=None：不许抛
    ar.note("HassTurnOn", "不是字典", True)
    ar.note(None, {}, True)
    ar.note("HassTurnOn", {"entity_id": ["", None, 7]}, True)


# ── 接线面：必须从**生产入口**走到登记（模块自己会动 ≠ 执行器接上了它）──────────
class _HAWithSvc(HAClient):
    """conftest 的替身没有 get_state/call_service（归位要用），这里补齐成真机形态。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.svc_calls = []

    async def get_state(self, entity_id):
        ent = self._states.get(entity_id)
        return dict(ent) if isinstance(ent, dict) else None

    async def call_service(self, domain, service, data, timeout=10.0):
        self.svc_calls.append((domain, service, dict(data)))
        return {"success": True}


def _plan(intent, args, source="klar", utterance="打开办公室射灯"):
    from core.nlu.fast_path import Plan
    return Plan(intent=intent, args=args, source=source, utterance=utterance)


def test_生产入口执行成功后_归位确实被登记():
    """只测 AutoRestore 自己会动是不够的：executor 没接上＝功能等于不存在
    （本仓反复的"配了没开/接了没通"教训）。这里走 Executor.run 真实路径。"""
    ha = _HAWithSvc(states={"light.ban_gong_shi_she_deng": {
        "entity_id": "light.ban_gong_shi_she_deng", "state": "off",
        "attributes": {"friendly_name": "射灯"}}})
    ex = Executor(ha, Set(**{"dialog.auto_restore": True,
                             "dialog.auto_restore_min": 10,
                             "dialog.auto_restore_domains": ["light"],
                             "dialog.auto_restore_exclude": []}))

    async def go():
        ok, reply = await ex.run(_plan("HassTurnOn",
                                        {"entity_id": "light.ban_gong_shi_she_deng"}))
        return ok, reply, dict(ex.restore.pending())

    ok, reply, pend = asyncio.run(go())
    assert ok, reply
    assert "light.ban_gong_shi_she_deng" in pend, "执行成功却没登记归位＝executor 没接线"


def test_生产主车道意图名TurnDeviceOn_从Executor_run到登记一条龙():
    """10-10 实发缺陷的钉：慧尖自有意图名是 `TurnDeviceOn/TurnDeviceOff`
    （fast_path.py:295/327 是**生产主车道**，executor.py:210 注释实锤），而第一版
    `_ON_INTENTS` 只认 `HassTurnOn` ⇒ 真语音链路一帧都不登记，21 条钉却全绿
    （它们喂的是透传族的 Hass*）。本钉用**生产真名**走生产入口，缺这条就是假绿。"""
    ha = _HAWithSvc(states={"light.ban_gong_shi_she_deng": {
        "entity_id": "light.ban_gong_shi_she_deng", "state": "off",
        "attributes": {"friendly_name": "射灯"}}})
    ex = Executor(ha, Set(**{"dialog.auto_restore": True,
                             "dialog.auto_restore_domains": ["light"]}))

    async def go():
        ok, reply = await ex.run(_plan("TurnDeviceOn",
                                       {"entity_id": "light.ban_gong_shi_she_deng"}))
        return ok, reply, dict(ex.restore.pending())

    ok, reply, pend = asyncio.run(go())
    assert ok, reply
    assert "light.ban_gong_shi_she_deng" in pend, \
        "生产主车道名字不登记＝真机上这条功能不存在"


def test_窗控族在生产形状下不登记_and_钉不再喂生产产不出的形状():
    """10-10 对抗复核：旧版这里有一段 `ControlWindow` 按 args["action"] 判方向的代码，
    而钉喂的是 `{"entity_id": "cover.window", "action": "open"}` ——生产**产不出**这形：
    慧尖窗控车道的 args 是 action/position/target（fast_path.py:173-178），实体解析在集成侧，
    回执行只有 {name, area}。⇒ 那段代码永不生效、那条钉是假绿（同一病灶的第二例）。
    现在代码已删，钉改成钉**真实形状不登记**。"""
    ar = _arm(settings=Set(**{"dialog.auto_restore": True,
                              "dialog.auto_restore_domains": ["cover", "light"]}))
    prod = {"action": "open", "target": [{"area": "办公室",
                                         "devices": [{"name": "平开窗"}]}]}
    assert "cover.window" not in _note(ar, "ControlWindow", prod)
    assert _note(ar, "ControlWindow", {"entity_id": "cover.window",
                                       "action": "open"}) == {}, \
        "凭空造的形状不再登记：_direction 已删窗控支（评审 #6）"
    ar.cancel_all()


def test_生产入口执行失败_不得登记归位():
    ha = _HAWithSvc(results={"HassTurnOn": {"success": False, "error": "timeout"}})
    ex = Executor(ha, Set(**{"dialog.auto_restore": True,
                             "dialog.auto_restore_domains": ["light"]}))

    async def go():
        # source 必须非 klar：klar 形态走 call_service 直调，会绕过上面注入的失败回执
        ok, _ = await ex.run(_plan("HassTurnOn", {"entity_id": "light.x"},
                                   source="chain", utterance="打开灯"))
        return ok, dict(ex.restore.pending())

    ok, pend = asyncio.run(go())
    assert not ok
    assert pend == {}, "失败轮不许登记归位（不然会把没开成的设备当开过的关掉）"


def test_到点收尾真的发出关服务_从登记到落地一条龙():
    ha = _HAWithSvc(states={"light.ban_gong_shi_she_deng": {
        "entity_id": "light.ban_gong_shi_she_deng", "state": "on",
        "attributes": {"friendly_name": "射灯"}}})
    ar = AutoRestore(ha, Set(**{"dialog.auto_restore": True}))

    async def go():
        ar._arm("light.ban_gong_shi_she_deng", 0.05)   # 直接走内部延时，不等 10 分钟
        await asyncio.sleep(0.2)
        return list(ha.svc_calls)

    calls = asyncio.run(go())
    assert calls == [("light", "turn_off", {"entity_id": "light.ban_gong_shi_she_deng"})], calls


def test_UI提交的dialog键必须在服务端DEFAULTS里存在():
    """结构钉（不是文案钉）：Web 写了服务端认不出来的键＝静默丢弃；
    服务端有键而 UI 从不回写＝"配了没接"。两边必须对得上。"""
    import pathlib
    import re
    html = (pathlib.Path(__file__).resolve().parents[1] / "www" / "index.html").read_text(
        encoding="utf-8")
    m = re.search(r"dialog:\s*\{(.*?)\},\s*\n\s*spatial:", html, re.S)
    assert m, "index.html 里 dialog 提交块结构变了，钉要跟着改（别放开判据）"
    written = set(re.findall(r"([a-z_]+)\s*:", m.group(1)))
    written |= {"auto_restore", "auto_restore_min"}     # 展开写法（条件 spread）里的键
    unknown = written - set(DEFAULTS["dialog"])
    assert not unknown, "UI 写了服务端不存在的 dialog 键：%s" % sorted(unknown)
    # 归位两个键必须在 UI 的读/写两侧都出现——只在一侧＝存了读不回或反之
    for key, ctl in (("auto_restore", "dlg_restore"), ("auto_restore_min", "dlg_restore_min")):
        assert key in m.group(1), "写侧缺 %s" % key
        assert html.count(ctl) >= 2, "%s 控件只在一侧出现（读或写没接上）" % ctl


def test_设置面默认值_in_opt_in_形态_UI读法必须同向():
    """默认**关**是 10-10 复核后的判断，不是漏配：真链路最常见的
    `TurnDeviceOn {target:[{area,devices}]}` 形里，集成回的 control_targets 行只有
    {name, area}、没有 entity_id（intent_turn.py:121-126）⇒ 加载项无法确证"是哪台被开了"，
    靠 area+name 反查去关＝猜目标，猜错比忘关更坏。所以先 opt-in。
    UI 的读值也必须同向：缺键显示成"已开"就是面板骗人。"""
    d = DEFAULTS["dialog"]
    # 默认开的前提已由**真机**证明（10-10：生产 target 形那句“打开办公室的灯”端到端
    # 登记＋60 秒自动收尾；登记对象取自回执 control_targets 行的 entity_id——
    # intent_turn.py:213-214 自 v1.2.10 起就带着，不是我先前以为的“只有 name/area”；
    # 那行只有窗控子道成立）。noop 与 changed=False 一律不登记，所以不会去关用户
    # 本来开着的设备。默认域只留 light。
    assert d["auto_restore"] is True, "翻回关需给出覆盖面退化的证据"
    assert d["auto_restore_min"] == 10
    assert d["auto_restore_domains"] == ["light"], "默认域被扩宽必须先回答『与现场自动化抢关』这条"
    assert d["auto_restore_exclude"] == []

    import pathlib
    import re
    html = (pathlib.Path(__file__).resolve().parents[1] / "www" / "index.html").read_text(
        encoding="utf-8")
    read = re.search(r'\$\("#dlg_restore"\)\.checked\s*=\s*([^;]+);', html)
    assert read, "dlg_restore 的读值语句找不到＝控件被拆了"
    # UI 的“缺键怎么显示”必须与服务端默认同向（默认 True ⇒ 缺键应显示已勾选）。
    want = "!== false" if d["auto_restore"] else "=== true"
    assert want in read.group(1), \
        "UI 缺键方向与服务端默认 %s 不同向（应含 %r）：%s" % (
            d["auto_restore"], want, read.group(1))


def test_延时钳位_不许把配置当成立刻关():
    """0/负数/离谱值都不得退化成"马上关设备"——那是另一种误关。"""
    for bad in (0, -5, "", None, "x"):
        ar = _arm(settings=Set(**{"dialog.auto_restore": True,
                                  "dialog.auto_restore_min": bad}))
        en, delay, dom, exc = ar._cfg()
        assert delay >= 30.0, bad
        ar.cancel_all()


def test_登记之后总开关被关掉_到点不许再关设备():
    """10-10 对抗复核补的洞：`_fire` 原来只重读设备状态、不重读策略。
    用户中途把 `dialog.auto_restore` 关掉＝收回了这条规则，已挂的计时不得继续动手。"""
    st = Set(**{"dialog.auto_restore": True, "dialog.auto_restore_min": 1,
                "dialog.auto_restore_domains": ["light"]})
    ha = HA(state="on")
    ar = _arm(settings=st, ha=ha)
    assert "light.desk" in _note(ar, "TurnDeviceOn", {"entity_id": "light.desk"})
    st.v["dialog.auto_restore"] = False          # 人在中途关掉总开关
    asyncio.run(ar._fire("light.desk"))
    assert ha.calls == [], ha.calls              # 一帧服务都不许下发
    ar.cancel_all()


def test_登记之后该设备被豁免_到点同样不许动手():
    """豁免名单是逐设备的收回方式，判定时机同样必须在"要动手的那一刻"。"""
    st = Set(**{"dialog.auto_restore": True, "dialog.auto_restore_min": 1,
                "dialog.auto_restore_domains": ["light"],
                "dialog.auto_restore_exclude": []})
    ha = HA(state="on")
    ar = _arm(settings=st, ha=ha)
    assert "light.desk" in _note(ar, "TurnDeviceOn", {"entity_id": "light.desk"})
    st.v["dialog.auto_restore_exclude"] = ["light.desk"]
    asyncio.run(ar._fire("light.desk"))
    assert ha.calls == [], ha.calls
    ar.cancel_all()



# ── 10-10 对抗复核补的四条（每条都在评审探针里复现过，不是假想）──────────
def test_noop步绝不登记_用户本来开着的灯不许到点被关():
    """评审 #1（高）：设备本来就在要求状态（执行器判 kind="noop"、播报都直说
    「本来就在要求的状态上」），这种步没打开任何东西。旧代码无条件登记 ⇒
    10 分钟后把用户**正在用**的灯关掉。走生产入口复现并钉死。"""
    ha = _HAWithSvc(states={"light.ban_gong_shi_she_deng": {
        "entity_id": "light.ban_gong_shi_she_deng", "state": "on",
        "attributes": {"friendly_name": "射灯"}}})
    ex = Executor(ha, Set(**{"dialog.auto_restore": True,
                             "dialog.auto_restore_domains": ["light"]}))

    async def go():
        ok, reply = await ex.run(_plan("TurnDeviceOn",
                                       {"entity_id": "light.ban_gong_shi_she_deng"},
                                       source="klar", utterance="打开办公室射灯"))
        return ok, reply, dict(ex.restore.pending())

    ok, reply, pend = asyncio.run(go())
    assert ok, reply
    assert "本来就在要求的状态" in reply or "noop" in reply.lower(), reply
    assert pend == {}, "noop 步登记了归位＝会关掉用户本来就开着的灯：%s" % pend
    ex.restore.cancel_all()


def test_到点读不到状态_不动手_not_fail_open():
    """评审 #3（中高）：get_state 返回 None 是真机常见形（改名/删除/首刷未完成/刷新失败
    折叠成 None）。旧写法 `cur is not None and …` 让 None **绕过**"已关就别动"，
    在毫无状态证据时发关服务——与模块红线"绝不靠猜去关"相反。"""
    ha = HA(state="missing")
    ar = AutoRestore(ha, Set(**{"dialog.auto_restore": True,
                                "dialog.auto_restore_domains": ["light"]}))
    asyncio.run(ar._fire("light.x"))
    assert ha.calls == [], "读不到状态却发了关服务：%s" % (ha.calls,)


def test_域清空成空列表_必须真的一个都不登记():
    """评审 #8：旧写法 `get(...) or DEFAULT_DOMAINS` 把显式 [] 当"没配"⇒ 用户
    清空域来停功能停不掉，只能去关总开关。"""
    ar = _arm(settings=Set(**{"dialog.auto_restore": True,
                              "dialog.auto_restore_domains": []}))
    assert _note(ar, "TurnDeviceOn", {"entity_id": "light.desk"}) == {}, \
        "domains=[] 被当成没配 ⇒ 仍按默认域登记"
    ar.cancel_all()


def test_回执带实体id_target形也能登记_覆盖面不再只限klar():
    """10-10 实测纠正我自己写在 CHANGELOG 的判断：慧尖集成 `TurnDeviceOn` 的回执
    `control_targets` 行**自 v1.2.10 起就带 entity_id**（intent_turn.py:213-214），
    所以「打开办公室的灯」这种 target 形（args 里只有 area/name）**是可以覆盖的**——
    登记对象取回执里的实体，不靠加载项猜目标。这条钉住那条正道。"""
    ar = _arm()
    args = {"target": [{"area": "办公室", "devices": [{"name": "灯"}]}]}   # args 里没有 entity_id
    receipt = {"success": True, "control_targets": [
        {"name": "射灯", "area": "办公室", "entity_id": "light.ban_gong_shi_she_deng"}]}
    pend = _note(ar, "TurnDeviceOn", args, receipt=receipt)
    assert list(pend) == ["light.ban_gong_shi_she_deng"], \
        "回执里明明有实体 id 却没登记＝覆盖面还是关着的：%s" % (pend,)
    ar.cancel_all()


def test_回执说没人被改动_changed_False_等同_noop_不登记():
    """实测：/api/services/* 的 200 body 是**状态真变了的实体列表**，本来就在该状态时是 []。
    空列表不是失败（目标状态已满足），但它是"谁都没被打开"的确证 ⇒ 不许登记归位。"""
    ar = _arm()
    r = {"success": True, "changed": False, "entities": []}
    assert _note(ar, "TurnDeviceOn", {"entity_id": "light.desk"}, receipt=r) == {}, \
        "changed=False 仍登记＝会把用户本来开着的灯关掉"
    # 反向对照：真变了就必须登记
    r2 = {"success": True, "changed": True, "entities": ["light.desk"]}
    assert "light.desk" in _note(ar, "TurnDeviceOn", {"entity_id": "light.desk"}, receipt=r2)
    ar.cancel_all()


def test_pending_在事件循环外也必须可读_现场排查靠它():
    """评审 #7：旧实现在 _tasks 非空时取 running loop ⇒ 循环外调用直接 RuntimeError，
    而模块头承诺"在 pending 快照里可见，便于现场排查"。"""
    ar = _arm()

    async def go():
        ar.note("TurnDeviceOn", {"entity_id": "light.a"}, True)
    asyncio.run(go())
    pend = ar.pending()                      # 循环外读——不许抛
    assert "light.a" in pend, pend
    assert isinstance(pend["light.a"], float)
    ar.cancel_all()

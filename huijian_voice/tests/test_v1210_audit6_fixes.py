# -*- coding: utf-8 -*-
"""第六轮全仓审计修复批（靶版 v1.2.10 / da0d4f5）的回归钉。

覆盖四条已落地修复；每条都记了**修复前的实测形态**，变异验证时可直接对照：

B9  诊断包漏脱敏设备 MAC。修复前 `REDACT_KEYS` 只有 `mac_address`/
    `bluetooth_mac_address`，而 config_flow 往 entry.data 写的键名是 **`"mac"`**
    （:442/894/977/1010），`diag["config"] = config_entry.as_dict()` 原样导出 ⇒
    `async_redact_data`（按精确键名掩码、不做子串）放裸 MAC 直通"下载诊断信息"。

A1  「…开关关掉」被解析成 TurnDeviceOn（**说关→去开**）。修复前实测：
    「把客厅灯开关关掉」「客厅灯开关关闭」「客厅灯开关关了」→
    `TurnDeviceOn(name=客厅灯, domains=[light])` ＋回「好的，办好了」；同句说成
    「关闭客厅灯开关」才是对的 Off（同义两句方向相反）。根因两处：动作表裸 `开`
    的护栏只挡 窗器/合器 不挡「开关」；且 ②③ 的剥离候选 `_strip_heads()` 原注释
    写着"每次现读：动态词表会扩表"、函数体却只读静态表 ⇒ 只挡裸开会让整句失能
    掉 fallback，**两半必须一起修**。

A6  在册整名被尾剥成子名。修复前 `parse_target('客厅灯开关') = (None,'客厅灯',6)`：
    `_NAME_TAIL_VERBS` 收了裸 开|关（为「灯打开→灯」立的），把用户亲手起的
    「客厅灯开关」连剥两轮 ⇒ 点名的 switch 实体被换成 light 那台。同病灶的
    **回显侧**早立过反向钉（test_klar_nlu.py 原文「设备名自身含动作字：不得被剥残
    （实发风险：灯开关 → 灯）」），目标提取侧缺同源防护＝半道闸。

B14 去重键与级联不同源。修复前 `_dkey` 调 `canonical(text)`（不带 settings），级联入口
    带 settings ⇒ 用户手工纠错表（nlu.corrections_extra，正是为听岔写法准备的那张）
    在键侧不参与归一：「关掉蒸汽灯」(→射灯) 与「关掉射灯」级联认同一句、键认两个桶
    ⇒ dedup 窗内重说即重复下发（相对量 ±10% 叠加；锁/卷帘/扫地机非幂等做两遍）。
    第四轮审计 P2 的修法在纠错表这一维没生效。

A9  确认环答「继续吧」= 挂起的高风险计划被静默丢弃。修复前实测：`canonical('继续吧')`
    折成「继续」，而 `_CONFIRM_YES` 收的是「继续吧」⇒ 既不落 YES 也不落 NO ⇒ 走
    "改口"支 `pop` 掉挂起的 HassUnlock，用户只听到兜底句、连「已取消」都没有。
    对照同表 好吧→好✓ 执行吧→执行✓ 取消吧→取消✓，只有这条折出表外＝词条从上线起
    永不命中。修法取档案立的纪律「礼貌/纠错折字后不许再吃整句等值表」，与 v1.1.38
    对「退下」表"同时判原话"同源（比对**折字前原话**，不往表里塞折字产物）。
    **口径留档（勿"顺手修"）**：答裸「继续」仍按改口处理——它从未在表内，且挂起时
    的提示语教的正是"说「确认」执行，或说「取消」放弃"；把裸「继续」补进 YES 等于
    允许折字后的字符串去吃整句等值表，正是上面那条纪律禁止的方向。此非缺陷。
"""
import ast
import asyncio
import time
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import pytest

CC = Path(__file__).resolve().parents[1] / "custom_components" / "huijian_ai"
DIAG = CC / "diagnostics.py"


def _redact_key_literals() -> set:
    """抠 diagnostics.REDACT_KEYS 里的**字符串字面量**成员（常量名不算）。"""
    tree = ast.parse(DIAG.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "REDACT_KEYS":
                    return {e.value for e in node.value.elts
                            if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    raise AssertionError("diagnostics.py 里找不到 REDACT_KEYS 赋值")


# ── B9 ────────────────────────────────────────────────────────────
def test_diagnostics_masks_the_mac_key():
    keys = _redact_key_literals()
    assert "mac" in keys, (
        f"REDACT_KEYS 缺裸 `mac`（当前={sorted(keys)}）——config_flow 往 entry.data 写的"
        "就是这个名字，诊断下载会明文带出设备 MAC")


def test_redact_keys_cover_entry_data_mac_keys():
    """同源钉（收窄版）：钉**已确证落进 entry.data 的键名**，不是全文扫 `mac` 字样。

    第一版用 `"([A-Za-z_]*mac[A-Za-z_]*)":` 全文正则，实测误收了 config_flow 里的
    **局部变量**（`existing_mac`/`expected_mac`/`unexpected_mac`——出现在 `context[...]`
    赋值与判断里，从不落 entry.data）⇒ 把钉写成一条必红的假缺陷。教训：同源判据要
    锚在"数据落点"，不能锚在字面形态。
    更强的形态仍欠（本钉只守已知键，不会自动发现新增落盘键）：按 AST 定位
    `async_create_entry(data=...)` / `async_update_entry(data=...)` 的 dict 字面量键集，
    再断言其中凭据/MAC 形态键 ⊆ REDACT_KEYS。
    """
    entry_data_mac_keys = {"mac"}          # config_flow.py:442/894/977/1010 实测写入点
    assert not (entry_data_mac_keys - _redact_key_literals())


# ── 动态词表隔离（C8 先例：本批用例自己会 sync_vocab）───────────────
@pytest.fixture(autouse=True)
def _isolate_dynamic_vocab():
    from core.nlu import targets as _T

    _T.clear_vocab()
    yield
    _T.clear_vocab()


HOME = {
    "light.ke_ting_deng": {"entity_id": "light.ke_ting_deng", "state": "off",
                           "attributes": {"friendly_name": "客厅灯"}},
    "switch.ke_ting_deng_kai_guan": {"entity_id": "switch.ke_ting_deng_kai_guan", "state": "on",
                                     "attributes": {"friendly_name": "客厅灯开关"}},
    "switch.zong_kai_guan": {"entity_id": "switch.zong_kai_guan", "state": "on",
                             "attributes": {"friendly_name": "总开关"}},
}


class _Scenes:
    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    async def refresh(self, force=False):
        pass

    def check(self, text):
        return None


class _Settings:
    def get(self, dotted, default=None):
        return default


def _match(text: str):
    """真 FastPath（不带 TextCNN 资产＝按字面表/剥离道裁决，正是本批病灶所在层）。"""
    from core.nlu import targets as T
    from core.nlu.fast_path import FastPath

    T.sync_vocab(HOME, {})
    T.sync_areas(["客厅"])
    return asyncio.run(FastPath(_Scenes(), None, _Settings()).match(text))


def _dev_of(plan):
    slot = ((plan.args or {}).get("target") or [{}])[0]
    return (slot.get("devices") or [{}])[0]


# ── A1 + A6：「开关」住在设备名里时，方向与目标都必须对 ──────────────
@pytest.mark.parametrize("utt", [
    "把客厅灯开关关掉",   # SOV＋把字头（修复前=TurnDeviceOn）
    "客厅灯开关关闭",     # 纯 SOV（修复前=TurnDeviceOn）
    "客厅灯开关关了",     # 修复前=TurnDeviceOn
    "关一下客厅灯开关",   # SVO＋一下
    "关闭客厅灯开关",     # 修复前就是对的形态——留下当"别把对的改坏"的对照
])
def test_switch_in_device_name_is_turned_off(utt):
    p = _match(utt)
    assert p is not None, f"{utt} 整句失能（挡了裸开，②③ 却没接住在装整名）"
    assert p.intent == "TurnDeviceOff", f"{utt} 方向错成 {p.intent}：用户要关，设备去开"
    dev = _dev_of(p)
    assert dev.get("name") == "客厅灯开关", f"{utt} 目标名被剥残：{dev.get('name')!r}"
    assert "switch" in (dev.get("domains") or []), f"{utt} 域错到 {dev.get('domains')}"


@pytest.mark.parametrize("utt", ["把客厅灯开关打开", "客厅灯开关打开"])
def test_switch_in_device_name_is_turned_on(utt):
    p = _match(utt)
    assert p is not None and p.intent == "TurnDeviceOn", utt
    dev = _dev_of(p)
    assert dev.get("name") == "客厅灯开关" and "switch" in (dev.get("domains") or [])


@pytest.mark.parametrize("utt,want", [
    ("把灯打开", "TurnDeviceOn"), ("打开灯", "TurnDeviceOn"),
    ("关灯", "TurnDeviceOff"), ("把灯关掉", "TurnDeviceOff"),
    ("客厅灯打开", "TurnDeviceOn"), ("打开客厅灯开关", "TurnDeviceOn"),
])
def test_positive_orders_are_not_hurt(utt, want):
    """护栏不得把正常句挡掉：裸 开/关 仍是动词，动词语尾残留仍被剥。"""
    p = _match(utt)
    assert p is not None, f"正例失能：{utt}"
    assert p.intent == want, f"{utt} 方向漂成 {p.intent}（期望 {want}）"
    if utt == "客厅灯打开":        # 「…灯打开」的动词语尾必须继续剥得掉
        assert _dev_of(p).get("name") == "客厅灯", f"动词语尾剥离被改坏：{_dev_of(p)}"


def test_installed_full_name_is_not_stripped():
    """A6 单点：在册整名原样带回，而 `clean_name` 本身行为一字不改。"""
    from core.nlu import targets as T

    T.sync_vocab(HOME, {})
    T.sync_areas(["客厅"])
    assert T.parse_target("客厅灯开关")[1] == "客厅灯开关"
    assert T.parse_target("总开关")[1] == "总开关"
    assert T.clean_name("灯打开") == "灯"              # 原用途不回归
    assert T.clean_name("客厅灯开关") == "客厅灯"       # 豁免只在"整名在册"判定处，不在清洗处


def test_strip_heads_sees_installed_device_names():
    """A1 的第二半：②③ 剥离候选必须并上**动态在装整名**（原注释说了却没做）。"""
    from core.nlu import targets as T
    from core.nlu.fast_path import _strip_heads

    T.sync_vocab(HOME, {})
    heads = set(_strip_heads())
    assert {"客厅灯开关", "总开关"} <= heads
    assert "灯" in heads                               # _BARE_DEV_HEADS 点名的裸词仍在


# ── A9：确认环必须还能看见折字前的原话 ─────────────────────────────
def _unlock_plan():
    from core.nlu.fast_path import Plan

    return Plan(intent="HassUnlock", args={"entity_id": "lock.da_men"},
                source="klar", utterance="解锁大门")


def _pending(ex, origin="sat-A"):
    p = _mk_pipe(ex)
    p._confirm[origin] = {"plan": _unlock_plan(), "ts": time.time(), "ttl": 60.0}
    return p


def _mk_pipe(ex):
    from test_experience_batch import _pipe

    return _pipe(ex=ex)


def test_continue_ba_confirms_the_pending_risky_action():
    from test_experience_batch import RecExecutor

    ex = RecExecutor()
    p = _pending(ex)
    reply = asyncio.run(p.handle("继续吧", origin="sat-A"))
    assert reply.source == "confirm_exec", (
        f"「继续吧」被当成改口丢弃：source={reply.source} 回复={reply.text!r}")
    assert len(ex.plans) == 1, "挂起的高风险计划没被执行"
    assert "sat-A" not in p._confirm, "确认后仍留着挂起计划（下一句会重复执行）"


def test_cancel_still_cancels_without_executing():
    from test_experience_batch import RecExecutor

    ex = RecExecutor()
    p = _pending(ex)
    reply = asyncio.run(p.handle("取消", origin="sat-A"))
    assert reply.source == "confirm_cancel" and ex.plans == []
    assert "sat-A" not in p._confirm


def test_confirm_answer_does_not_leak_across_origins():
    """反向闸：A 挂起时 B 的应答既不得替 A 执行，也不得清掉 A 的挂起。"""
    from test_experience_batch import RecExecutor

    ex = RecExecutor()
    p = _pending(ex, origin="sat-A")
    asyncio.run(p.handle("继续吧", origin="sat-B"))
    assert ex.plans == [], "别的卫星的应答替 sat-A 执行了解锁"
    assert "sat-A" in p._confirm, "别的卫星的应答清掉了 sat-A 的挂起计划"


def test_confirm_answer_raw_text_is_keyword_only():
    """既有钉把 `_confirm_answer` 当**裸函数**挂实例（只收 2 个形参）。新形参必须是
    仅关键字＋有默认值，否则这类替身集体 TypeError（本批改时实发 2 红）。"""
    from core.pipeline import Pipeline

    p = Pipeline.__new__(Pipeline)
    p.settings = _Settings()
    p._confirm = {}
    p._origin_ts = {}
    p._last = OrderedDict()
    p._turns = {}
    p._last_target = {}
    p._pending = set()
    assert asyncio.run(Pipeline._confirm_answer(p, "随便一句不是应答", "sat-X")) is None


# ── B14：去重键与级联同源 ─────────────────────────────────────────
class _Extra:
    def __init__(self, extra):
        self._extra = extra

    def get(self, dotted, default=None):
        return self._extra if dotted == "nlu.corrections_extra" else default


def test_dedup_key_honours_the_user_correction_table():
    from core.nlu.canonical import canonical
    from core.pipeline import Pipeline

    st = _Extra({"蒸汽灯": "射灯"})
    assert canonical("关掉蒸汽灯", st) == "关掉射灯", "前提不成立：纠错表没生效，本钉无意义"
    a = Pipeline._dkey(SimpleNamespace(settings=st), "关掉蒸汽灯", "devA")
    b = Pipeline._dkey(SimpleNamespace(settings=st), "关掉射灯", "devA")
    assert a == b, "键侧不吃纠错表 ⇒ 同一句两个桶 ⇒ dedup 窗内重说即重复下发"


def test_dedup_key_folds_politeness_without_settings_and_never_raises():
    """裸对象（无 settings 属性）退回不带纠错表的归一，**不抛**——既有钉就这么用。"""
    from core.pipeline import Pipeline

    bare = SimpleNamespace()
    assert not hasattr(bare, "settings")
    assert Pipeline._dkey(bare, "请调亮一点", "devA") == Pipeline._dkey(bare, "调亮一点", "devA")


# ── A4：人感触发的极性不得被懒量词反转 ────────────────────────────
@pytest.mark.parametrize("utt,want", [
    ("没人就把灯关掉", "off"),        # 修复前 to='on'（反义自动化）
    ("无人的时候关闭空调", "off"),    # 修复前 to='on'
    ("如果没人就关灯", "off"),        # 修复前 to='on'
    ("当客厅没人时把灯关掉", "off"),   # 修复前就对——带区域的对照形
    ("有人就把灯打开", "on"),         # 反向守卫：肯定形不得被折成 off
    ("当客厅有人时把灯打开", "on"),
])
def test_motion_trigger_polarity(utt, want):
    from core.nlu import creation

    r = creation.parse(utt)
    assert r and r.get("kind") == "automation", f"{utt} 没建成自动化：{r}"
    trig = r["trigger"]
    assert trig.get("to") == want, f"{utt} 极性判成 {trig.get('to')!r}（期望 {want!r}）"
    # 否定字被当区域名 ⇒ desc/entity_id 长成垃圾「没人体」，集成侧按「人体」关键词
    # 兜底还能解析成功并回「已将传感器修正为…」⇒ 反义自动化坐实。垃圾形不得出现。
    assert "没" not in str(trig.get("entity_id")) and "无" not in str(trig.get("entity_id")), \
        f"{utt} entity_id 里混进否定字（垃圾描述）：{trig}"


# ── A3：人称主语不得被当设备回指而跳过卫星区域 ──────────────────────
def _spatial_target(text: str):
    from core.nlu import targets as T
    from core.nlu.fast_path import FastPath
    from test_experience_batch import RecExecutor, _pipe

    sync_vocab_for(T)
    ex = RecExecutor()
    p = _pipe(fp=FastPath(_Scenes(), None, _SatSettings()), kl=_Lane(), ex=ex,
              settings=_SatSettings())
    p.ha._states = _A3_HOME
    p.ha._areas = {"a1": "客厅", "a2": "卧室"}
    asyncio.run(p.handle(text, origin="10.0.0.5"))
    return [pl.args for pl in ex.plans]


_A3_HOME = {
    "light.ke_ting_deng": {"entity_id": "light.ke_ting_deng", "state": "off",
                           "attributes": {"friendly_name": "客厅灯"}},
    "light.wu_shi_deng": {"entity_id": "light.wu_shi_deng", "state": "off",
                          "attributes": {"friendly_name": "卧室灯"}},
}


def sync_vocab_for(T):
    T.sync_vocab(_A3_HOME, {})
    T.sync_areas(["客厅", "卧室"])


class _SatSettings:
    def get(self, dotted, default=None):
        if dotted == "spatial.satellite_areas":
            return {"10.0.0.5": "客厅"}
        return default


class _Lane:
    async def match(self, text, origin=""):
        return None


@pytest.mark.parametrize("utt", ["把灯打开", "我们把灯打开", "帮我们把灯打开", "大家把灯打开"])
def test_personal_pronoun_subject_keeps_the_satellite_area(utt):
    """「我们/帮我们/大家」是主语，不是设备回指 ⇒ 泛类词仍须限定在本房间。

    修复前 `"们" in text` 无主语判别 ⇒「我们把灯打开」丢掉 area ⇒ 全屋按名子串匹配，
    名字含「灯」的实体一起动（同句少一个「我们」就只开本房间——差一个字行为翻转）。
    """
    targets = _spatial_target(utt)
    assert targets, f"{utt} 整句没执行（本不该失能）"
    slot = targets[0]["target"][0]
    assert slot.get("area") == "客厅", (
        f"{utt} 丢了卫星区域 ⇒ 泛称退化成全屋匹配：{slot}")


def test_true_anaphora_still_suppresses_the_area():
    """反向守卫：回指代词＋们 仍算回指，不得被本批收窄放出去叠区域。"""
    from core.nlu.fast_path import Plan
    from core.pipeline import Pipeline

    # `_is_anaphoric` 是 staticmethod：第一参数是 plan，别按实例方法调。
    no_flag = Plan(intent="TurnDeviceOn", args={}, source="t0", utterance="")
    assert Pipeline._is_anaphoric(no_flag, "它们打开") is True, "「它们」不再算回指=收窄过头"
    # 指示词「这些/那些」不带「们」字，本判据从来不管（收窄前后一致，非本批改动面）；
    # 真正把回指挡住的是 FLAG_ANAPHORA_STRIPPED / is_pronoun 那几路，此处不扩权。
    assert Pipeline._is_anaphoric(no_flag, "这些灯打开") is False
    assert Pipeline._is_anaphoric(no_flag, "我们把灯打开") is False, "人称主语仍被当回指"
    assert Pipeline._is_anaphoric(no_flag, "帮我们把空调关掉") is False, "同上（帮字头形）"


# ── A8：尾剥正则不得把 HA 事件循环冻住 ────────────────────────────
def test_tail_strip_abstains_in_linear_time():
    """计时钉——**防回溯的修复必须配计时钉**，否则下次谁把 `+` 加回去无人察觉。

    修复前实测：`_NAME_TAIL_STRIP` 原形 `(?:TOK)+$`，词元表含「一些|一|些」
    「全部|全|部」这类**可两切**成员，遇"收尾非词表字"的长串 ⇒ 指数回溯：
      「一些」×24（49 字）单次 sub = 11.5~18.2s；「全部」×28（57 字）= 267s；
      真入口 `_unknown_spoken_device_name` 52 字 = 15.4s。
    全程同步无 await，而本加载项就跑在 HA 事件循环里 ⇒ 一句 ASR 幻听能把整机拖住
    十几秒到分钟级；且弃权判据 `2<=len(pre)<=4` 排在剥尾**之后**——花完时间什么也没拦。
    修复后终态与 `+` 版等价（同样剥不动⇒弃权），代价降到微秒级。
    """
    import time as _time

    from core.nlu import targets as T
    from core.pipeline import _category_nouns, _unknown_spoken_device_name

    T.sync_vocab({"light.a": {"entity_id": "light.a", "state": "off",
                              "attributes": {"friendly_name": "台灯"}}}, {})
    words = _category_nouns("light")
    for bomb in ("一些" * 24 + "阳", "全部" * 28 + "阳", "一些" * 60 + "阳"):
        text = "把" + bomb + "的灯"
        t0 = _time.perf_counter()
        _unknown_spoken_device_name(text, words, ("台灯",),
                                    known_areas=(), real_areas=None)
        cost = _time.perf_counter() - t0
        assert cost < 0.5, f"{len(text)} 字输入耗时 {cost:.3f}s（阈值 0.5s）⇒ 回溯回归"
    # 本钉只守**成本**。判据的等价性另有两条证据：①`_strip_tail_tokens` 与
    # `(?:TOK)+$` 的 3 万串随机等价扫描零差异；②test_v1136_area_modifier /
    # test_v1136_second_batch 那批孤字下限钉全绿（把实现改成"逐词元剥"时它们当场红）。
    # 注：60 字「全部」串残段是「阳的」→ 照判「阳的灯」——`全部` 在**头部**剥离表里，
    # 与尾剥无关；那是既有行为，不由本钉管辖（我第一版把它当成"应弃权"写错了断言）。


def test_name_gate_judgement_unchanged_after_strip_rewrite():
    """A8 只准改成本、不准改判据：描述性修饰段照旧拦，泛称/数量形照旧放。"""
    from core.nlu import targets as T
    from core.pipeline import _category_nouns, _unknown_spoken_device_name

    T.sync_vocab({"light.a": {"entity_id": "light.a", "state": "off",
                              "attributes": {"friendly_name": "台灯"}}}, {})
    words = _category_nouns("light")
    for utt, want in (("打开会飞的灯", "会飞的灯"), ("打开书桌的灯", "书桌的灯"),
                      ("打开天棚灯", "天棚灯"), ("打开三个灯", ""), ("打开灯", "")):
        got = _unknown_spoken_device_name(utt, words, ("台灯",),
                                          known_areas=(), real_areas=None)
        assert got == want, f"{utt} 判据漂移：{got!r}（期望 {want!r}）"


def test_strip_tail_tokens_is_equivalent_to_the_regex_property():
    """A8 的**真正护栏**（属性钉）：线性实现必须与 `(?:TOK)+$` 逐串等价。

    计时钉只防"退回指数"，防不了"顺手改判据"——而本仓在同一个尾剥循环上已经
    踩过两次粒度陷阱（v1.1.36 复核⑥ 的「关掉阳台的」一次剥到「阳」；本批改第一版
    把 `+$` 换成逐词元，孤字下限的刹车落点变了，三条 v1136 钉当场红）。
    故把等价性直接钉住：随机串（词元表 ∪ 非词元噪声字）上两种实现必须同结果。
    串长压在 14 字内——不是迁就正则，而是让"正则那边"别在扫描途中先炸。
    """
    import random

    from core import pipeline as PL

    rnd = random.Random(7)
    alpha = list(PL._TAIL_TOKEN_TUPLE) + list("阳飞桌窗户人开我们灯")
    diff = []
    for _ in range(20000):
        s = "".join(rnd.choice(alpha) for _ in range(rnd.randint(0, 14)))
        a = PL._NAME_TAIL_STRIP.sub("", s).strip()
        b = PL._strip_tail_tokens(s).strip()
        if a != b:
            diff.append((s, a, b))
    assert not diff, f"线性实现与正则不等价，前 3 例：{diff[:3]}"


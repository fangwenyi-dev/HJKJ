# -*- coding: utf-8 -*-
"""v1.1.38：拒绝腿（否定祈使）不得被"点名设备查无"闸当成设备名。

现网留痕（办公 .91，升级到 v1.1.37 后第一手复验 `_goldtest/live_check_1137.log`）：

    [N5] FAIL 「打开办公室的射灯，别开台灯」
        回执：没有找到对应的设备「别开台灯」，换个叫法或带上房间名再试试
        状态变化：无（零下发）

v1.1.36 同一句是 PASS（灯开了）。差别不是链侧改过，而是 v1.1.37 把被 `Sun` 短路的
「点名查无」闸修活了之后，闸**开始吃否定腿**：`serial_clauses` 切出的第二腿
「别开台灯」在裁决面已被 `is_bare_negation_imperative` 弃用（修②，pipeline:650/:679），
但同一条腿走到查无闸时，`_unknown_spoken_device_name` 把否定前缀「别开」当成
**设备修饰语**拼出"用户点名的设备"，于是"这句是拒绝"被读成"这台家里没有"⇒ 整链判死、
该动的灯没动。这正是修②当年立的那条红线（半句否定不许打死整句）换了个入口复活。

同一形状的病灶还有两处（本文件逐条钉住）：
- 链侧：拒绝腿天然不该有计划，也就不能按"分句不中→回退单发"或"点名查无→整链判死"；
- 单发侧：「别开灯，也不要关窗」整句既非"裸否定"（句里有两个动作），否定词又被拼进
  设备名 ⇒ 回话播成"家里没这台"，把用户的拒绝答成"没听懂"。

纪律同源：`is_negation_imperative`/`is_bare_negation_imperative` 仍是 fast_path 单点定义
（pipeline 只消费，不另抄一份正则），与疑问闸「两档都不得执行」同一条纪律。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.nlu.fast_path import Plan                                  # noqa: E402
from core.nlu import creation                                        # noqa: E402
from core.pipeline import (_klar_named_absent_target,                 # noqa: E402
                           _unknown_spoken_device_name, _category_nouns)
from test_experience_batch import (Lane, RecExecutor, HA, _pipe)      # noqa: E401

LAMP = "light.ban_gong_shi_she_deng"
TAI = "light.ke_ting_tai_deng"
CEID = "cover.ban_gong_shi_ping_kai_chuang"
# 与 v1.1.37 钉同形：在装清单里**带拉丁短名**（现网 234 条里真有 `Sun`/`TV`），
# 免得判据又被"手写小清单"绕过去（本仓记过的清单型判据假绿坑）。
LIVE = ("射灯", "射灯", "客厅台灯", "Sun", "TV", "平开窗 ① 开启",
        "办公室空调 Indicator Light")
AREAS = ("办公室",)


def _kl(utt, eid, intent="HassTurnOn"):
    return Plan(intent=intent, args={"entity_id": eid}, source="klar",
                utterance=utt)


def _states():
    return {eid: {"entity_id": eid, "state": "off",
                  "attributes": {"friendly_name": name}}
            for eid, name in ((LAMP, "射灯"), (TAI, "客厅台灯"), (CEID, "平开窗"))}


def _legs(utt):
    """按链侧真正用的切分器造 klar 查表（一腿一条计划，形态同真机引擎）。"""
    table = {}
    for c in creation.serial_clauses(utt) or [utt]:
        if "台灯" in c:
            table[c] = _kl(c, TAI)
        elif "会飞" in c:
            table[c] = _kl(c, LAMP, "HassTurnOff")
        elif "射灯" in c or "灯" in c:
            table[c] = _kl(c, LAMP)
        elif "窗" in c:
            table[c] = _kl(c, CEID)
    return table


def _say(entity_ids):
    return list(entity_ids)


# ── ① 承重：拒绝腿必须被当成"零动作腿"，其余腿照常链发 ──────────────
def test_refusal_leg_does_not_kill_the_chain():
    """现网 N5 那一句：该动的灯必须动，且回话不许是"家里没这台"。"""
    utt = "打开办公室的射灯，别开台灯"
    ex = RecExecutor()
    ha = HA(_states())
    ha._areas = {"o": "办公室"}
    r = asyncio.run(_pipe(kl=Lane(_legs(utt)), ex=ex, ha=ha).handle(utt, origin="o"))
    assert _say([p.args.get("entity_id") for p in ex.plans]) == [LAMP], \
        f"拒绝腿打死整句或误动台灯：{[p.args for p in ex.plans]}"
    assert "没有找到对应的设备" not in r.text, f"把拒绝答成了查无：{r.text}"


def test_refusal_leg_leading_is_also_skipped():
    utt = "别开台灯，打开办公室的射灯"
    ex = RecExecutor()
    ha = HA(_states())
    ha._areas = {"o": "办公室"}
    asyncio.run(_pipe(kl=Lane(_legs(utt)), ex=ex, ha=ha).handle(utt, origin="o"))
    assert [p.args.get("entity_id") for p in ex.plans] == [LAMP], \
        f"拒绝腿在前时同样不许打死整句：{[p.args for p in ex.plans]}"


def test_all_refusal_legs_execute_nothing_and_do_not_crash():
    """整句只剩拒绝腿 ⇒ 零下发，且不能踩到 `plans[0]`（空链守卫）。

    选「然后」形而不是「，」形：`split_compound` 只看字面连接词，而逗号形要靠
    `creation.serial_clauses`，后者会因全局词表是否同步而给出不同结果（夹具
    `_pipe` 刻意抑制 sync_vocab）——判据的输入形态必须在夹具里也生产可达。
    """
    utt = "别开台灯然后别关窗"
    ex = RecExecutor()
    ha = HA(_states())
    ha._areas = {"o": "办公室"}
    pipe = _pipe(kl=Lane(_legs(utt)), ex=ex, ha=ha)
    # 直接钉链裁决：这里必须干净地"整链不成立"，而不是 IndexError 被外层吞掉
    from core.nlu.fast_path import split_compound
    assert split_compound(utt) == ["别开台灯", "别关窗"], "切分形态变了，此钉不再承重"
    reply, merged, plans, end_flag = asyncio.run(pipe._chain_decide(utt, "o"))
    assert (reply, merged, plans, end_flag) == (None, None, [], False), \
        f"全拒绝链没有干净回退：{(reply, merged, plans, end_flag)}"
    r = asyncio.run(pipe.handle(utt, origin="o"))
    assert ex.plans == [], f"全拒绝句真下发了：{[p.args for p in ex.plans]}"
    assert "没有找到对应的设备" not in r.text, f"把拒绝答成了查无：{r.text}"


# ── ② 反向不变量：闸修活之后"点了家里没有的那台"照旧判死 ────────────
def test_absent_named_leg_still_kills_the_chain():
    """v1.1.35/1.1.37 的收口不许被这次放宽带回去（链内查无腿 ⇒ 整链不执行）。"""
    utt = "打开办公室射灯然后关掉会飞的灯"
    ex = RecExecutor()
    ha = HA(_states())
    ha._areas = {"o": "办公室"}
    r = asyncio.run(_pipe(kl=Lane(_legs(utt)), ex=ex, ha=ha).handle(utt, origin="o"))
    assert ex.plans == [], f"链内查无腿被放宽成半执行：{[p.args for p in ex.plans]}"
    assert "会飞的灯" in r.text, f"没点名说没找到：{r.text}"


def test_single_shot_absent_name_still_refused():
    assert _klar_named_absent_target(_kl("关掉会飞的灯", LAMP, "HassTurnOff"),
                                     LIVE, AREAS, AREAS) == "会飞的灯"
    assert _klar_named_absent_target(_kl("关掉阳台的灯", LAMP, "HassTurnOff"),
                                     LIVE, AREAS, AREAS) == "阳台的灯"


# ── ③ 根因点：否定前缀不得拼进"点名的设备名" ────────────────────────
def test_negation_prefix_is_not_a_device_name():
    """单元级：拒绝前缀在拼名那一层就该放弃裁决。"""
    for utt in ("别开台灯", "不要开台灯", "别开灯，也不要关窗", "关灯，不要拉窗帘"):
        assert _unknown_spoken_device_name(utt, _category_nouns("light"), LIVE,
                                           known_areas=AREAS,
                                           real_areas=AREAS) == "", \
            f"{utt} 的否定前缀被拼成了设备名"


def test_absent_gate_is_blind_to_refusal_legs():
    """闸层：整条腿就是拒绝（含「别把…打开」这种把字形——它的否定词被目标段隔开，
    拼名层的短片段判不出来，靠 utterance 级裸否定兜住）。"""
    for utt in ("别开台灯", "别把台灯打开", "不要关窗"):
        eid, intent = ((CEID, "HassTurnOff") if utt.endswith("窗") else (TAI, "HassTurnOn"))
        assert _klar_named_absent_target(_kl(utt, eid, intent), LIVE,
                                         AREAS, AREAS) == "", f"{utt} 仍被当成点名查无"


def test_descriptive_modifier_still_has_adjudication_power():
    """反向不变量：带否定字的**描述性定语**不是拒绝，裁决权不许顺手豁免掉。

    「不亮的/不会亮的/没名字的」里没有"否定词+动作动词"的贴合形态（`不` 后面接的是
    形容词或能愿动词+形容词），与本闸要豁免的「别开/不要关」形状正相反。
    """
    for utt in ("关掉不亮的灯", "关掉不会亮的灯", "关掉没名字的灯"):
        assert _unknown_spoken_device_name(utt, _category_nouns("light"), LIVE,
                                           known_areas=AREAS,
                                           real_areas=AREAS) != "", \
            f"{utt} 被否定豁免顺手放掉了（该拦没拦）"


# ── ④ 接线：判据单源，链侧与拼名侧都真的消费了它 ────────────────────
def test_negation_predicate_wired_on_chain_and_naming_sides():
    import ast
    src = (ROOT / "core/pipeline.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef)

    def body(name):
        return next(n for n in ast.walk(tree)
                    if isinstance(n, kinds) and n.name == name)

    def calls(node):
        return [n.func.id for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]

    assert "is_bare_negation_imperative" in calls(body("_chain_decide")), \
        "链侧的否定腿豁免被摘掉（回退成'分句不中→回退单发'）"
    assert "is_negation_imperative" in calls(body("_unknown_spoken_device_name")), \
        "拼名侧不再识别否定前缀（N5 现网案复发）"
    assert "is_bare_negation_imperative" in calls(body("_klar_named_absent_target")), \
        "闸层的整腿裸否定豁免被摘掉（把字形句复发）"
    fp_src = (ROOT / "core/nlu/fast_path.py").read_text(encoding="utf-8")
    assert fp_src.count("_NEGATION_CMD = re.compile") == 1, "否定正则被另抄第二份"

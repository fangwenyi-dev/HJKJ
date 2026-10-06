# -*- coding: utf-8 -*-
"""v1.1.38 同族扫描：「点名设备查无」闸只许吃**指认形态**，两类非指认形态一律弃权。

来路：修完 v1.1.37 的 N5（拒绝腿）之后，按同一类把闸可能吃错的形态扫了一遍——
工装 `_goldtest/sweep_false_reject_1138.py`（清单喂现网 `GET /api/states` 真全量 227 名）
+ 端到端 `_goldtest/probe_gate_sweep_e2e_1138.py`（真入口 `Pipeline.handle`、体表 234 实体），
再用 worktree（1.1.36 / 1.1.37）三棵树比列分"新引入 vs 既存"。两类成立：

① 疑问代词（**v1.1.36 上是真下发**，v1.1.37 只是被闸撞成错回话——洞没补）：
    [1.1.36 臂] 「哪个灯开着」 source=klar 执行=['light.ban_gong_shi_she_deng'] 回执=好的，办好了
    [1.1.37 臂] 「哪个灯开着」 source=no_such_device 执行=无 回执=没有找到对应的设备「哪个灯」
   `_QUERY_TRIGGER_RE` 里有「哪些」却没有同族的「哪个/哪盏/哪台…]与「谁」——
   正是 v1.1.26 审计记过的"疑问闸枚举漏"那一族。问一句动一次设备，撞 v1.1.2 红线。

② 时段状语+轻动词（既存，自 v1.1.35 起三棵树都拦）：
    「睡觉前把灯关掉」→ 拼出『睡觉前把灯』；「出门前把灯关了」→ 『出门前把灯』；
    「等一下开灯」→ 『等一下开灯』该做的没做。
   收口在 `_phrase_is_not_a_name` 的形状判据里，**尾巴只认轻动词（把将给让开关）**：
   「阳台**上**的灯」「书桌**上**的灯」的位置短语尾巴必须有裁决权——第一版把
   `_NAMELESS_HEAD_VERBS`（含 上/下/到/过/起）并进尾巴判据，当场被
   `test_v1136_area_modifier` / `test_v1136_state_descriptor` 两条反向钉打死，改窄后绿。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.nlu.fast_path import Plan                                  # noqa: E402
from core.nlu.query import is_query_like, is_status_query             # noqa: E402
from core.pipeline import (_klar_named_absent_target,                 # noqa: E402
                           _unknown_spoken_device_name, _category_nouns,
                           select_primary_plan)
from test_experience_batch import (Lane, RecExecutor, HA, _pipe)      # noqa: E401

LAMP = "light.ban_gong_shi_she_deng"
TAI = "light.ke_ting_tai_deng"
LIVE = ("射灯", "射灯", "客厅台灯", "Sun", "TV", "平开窗 ① 开启")
AREA = ("办公室",)
LIGHT_NOUNS = _category_nouns("light")


def _kl(utt, eid=LAMP, intent="HassTurnOn"):
    return Plan(intent=intent, args={"entity_id": eid}, source="klar", utterance=utt)


# ── ① 疑问代词进单源疑问表：问句既不执行，也不答成"家里没这台" ──────
def test_interrogative_determiners_are_queries():
    for utt in ("哪个灯开着", "哪盏灯亮着", "哪些窗开着", "谁开了灯", "哪台空调在跑"):
        assert is_query_like(utt), f"{utt} 仍不被当疑问句（v1.1.36 实测真下发开灯）"


def test_interrogative_leg_has_no_adjudication_and_never_executes():
    for utt in ("哪个灯开着", "哪盏灯亮着"):
        assert _klar_named_absent_target(_kl(utt, TAI), LIVE, AREA, AREA) == "", \
            f"{utt} 被回成'家里没这台'（提问答成查无设备）"
        assert select_primary_plan(None, _kl(utt), known_areas=AREA,
                                   device_names=LIVE, real_areas=AREA) is None, \
            f"{utt} 仍可被下发"


def test_interrogative_trigger_is_single_source_table():
    """疑问表只有一份，且 `is_query_like`/`is_status_query` 共用同一对象。

    （v1.1.19 复审立的纪律：字面表守卫与本表逐字同源，另抄一份必然漂移。）"""
    src = (ROOT / "core/nlu/query.py").read_text(encoding="utf-8")
    assert src.count("_QUERY_TRIGGER_RE = re.compile") == 1, "疑问触发表被抄成两份"
    assert is_query_like("哪个灯开着") and is_status_query("哪个灯开着"), \
        "新加的疑问代词只进了一侧=半道闸"


def test_imperative_with_demonstrative_is_not_a_query():
    """反向：指示代词（那/这）不是疑问代词（哪），命令档不许掉。"""
    for utt in ("关掉那个灯", "打开这盏灯", "把客厅台灯关上"):
        assert not is_query_like(utt), f"{utt} 被误判成疑问句（命令档掉光）"
        assert select_primary_plan(None, _kl(utt, TAI), known_areas=AREA,
                                   device_names=LIVE, real_areas=AREA) is not None, \
            f"{utt} 被疑问闸误杀"


# ── ② 时段状语+轻动词：连动前段不是设备名 ─────────────────────────
def test_temporal_adverbial_is_not_a_device_name():
    for utt in ("睡觉前把灯关掉", "出门前把灯关了", "等一下开灯"):
        assert _unknown_spoken_device_name(utt, LIGHT_NOUNS, LIVE,
                                          known_areas=AREA,
                                          real_areas=AREA) == "", \
            f"{utt} 的状语被拼成了点名设备名"


def test_temporal_adverbial_sentences_execute():
    """端到端：这三句都是"要动灯"，闸不许再把它们判死（真入口走 handle）。"""
    for utt in ("睡觉前把灯关掉", "等一下开灯", "出门前把灯关了"):
        ex = RecExecutor()
        ha = HA({LAMP: {"entity_id": LAMP, "state": "on",
                        "attributes": {"friendly_name": "射灯"}}})
        ha._areas = {"o": "办公室"}
        r = asyncio.run(_pipe(kl=Lane({utt: _kl(utt)}), ex=ex, ha=ha).handle(utt, origin="o"))
        assert [p.args.get("entity_id") for p in ex.plans] == [LAMP], \
            f"{utt} 仍被闸打死：source={r.source} 回执={r.text}"


def test_positional_tail_still_has_adjudication_power():
    """反向不变量（本批第一次收宽就是被这两条钉打死的，永久保留）：
    位置短语尾巴「上」**不属于**轻动词，家里没这间房时照旧必须有裁决权。"""
    for utt in ("关掉阳台上的灯", "关掉书桌上的灯", "关掉会飞的灯"):
        assert _unknown_spoken_device_name(utt, LIGHT_NOUNS, LIVE,
                                           known_areas=AREA,
                                           real_areas=AREA) != "", \
            f"{utt} 被时段状语豁免顺手放掉了（该拦没拦）"

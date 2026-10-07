# -*- coding: utf-8 -*-
"""v1.2.2 刀1：Tier A 的先验宇宙必须只收「可当目标的本家名」，不收 HA 的能力段。

现网实证（办公 .91 真清单快照 10-05，234 实体；本地工装
`_audit_tiera_real_inventory.py`，生产清单**不入库**）：HA 的 friendly_name 一律是
`<设备名> <能力后缀>` 形状——`悬窗 ① 开启`、`推拉窗 ③ 关闭`、`开窗器 123f-020A 速度`、
`射灯 确认`、`V3 音量`。旧形把 `targets.tokens_of()` 的**整份**拆词结果当先验，实测产出
`('悬窗','开启','暂停','力度','推拉窗','关闭','射灯','展厅','空调','下次日出','音量')`
——**动作词与属性名被当成"本家名字"**。本层的产物是改写后的文本，它还要继续走确认环、
复合切分、直调与集成展开；归一目标一旦是 `确认/关闭/开启` 这类控制词，改的就不是"哪台设备"
而是"这句话是什么行为"。v1.1.17「摄像机→洗碗机」的规矩（`_admissible`：不得凭空造本家
没有的目标）在这里同族，只是换了入口。

方向性判据（本刀的全部正当性）：**收窄只会漏改，不会多改**。窗型名照旧进得来
（`悬窗 ① 开启` → 设备名段 `悬窗`），能力段一律进不来。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.nlu import homophone  # noqa: E402

# 逐字取自现网清单的形状（含裸名与带后缀名、拉丁首段名）
HA_NAMES = (
    "悬窗 ① 开启", "悬窗 ② 暂停", "悬窗 力度", "悬窗 电池电压",
    "推拉窗 ③ 关闭", "内开窗 ④ 内倒", "平开窗 开窗器",
    "射灯", "射灯 确认", "射灯 Effect",
    "会议室空调 温度", "V3 音量", "Sun 下次日出", "HUIJIAN-EB60 媒体播放器",
)


# ── 承重正向：设备名段必须进得来，且归一真的发生 ─────────────────────
def test_device_segment_of_suffixed_names_is_the_normalization_target():
    """"旋窗/宣窗/玄窗" 必须归一到 `悬窗`——靶名来自 `悬窗 ① 开启` 的**首段**。
    这条同时钉两件事：①先验没有因为"名字带后缀"而丢掉窗族；②归一产物是设备名。"""
    prior = homophone.prior_names(HA_NAMES)
    assert "悬窗" in prior and "推拉窗" in prior and "内开窗" in prior, prior
    for spoken, want in (("关闭展厅旋窗", "关闭展厅悬窗"),
                         ("打开半天宣窗", "打开半天悬窗"),
                         ("关掉玄窗", "关掉悬窗"),
                         ("关掉催拉窗不行推拉窗来", "关掉催拉窗不行推拉窗来")):
        out, pairs = homophone.rewrite(spoken, prior)
        assert out == want, "%s -> %s (%s)" % (spoken, out, pairs)


def test_bare_names_still_participate_unchanged():
    """无空格的名字原样进先验（收窄不得把 `射灯` 这类正主挡掉）。"""
    prior = homophone.prior_names(HA_NAMES)
    assert "射灯" in prior and "会议室空调" in prior, prior
    out, pairs = homophone.rewrite("关掉社灯", prior)
    assert out == "关掉射灯" and pairs == [("社灯", "射灯")], (out, pairs)


# ── 承重反向：能力段与控制词**不得**成为归一目标 ─────────────────────
def test_capability_and_control_tokens_are_never_targets():
    """`确认/开启/关闭/暂停/力度/音量/状态/电池电压` 一个都不许当本家名参与归一。

    反向臂：用户说一句与它们同音、且字面不在清单里的片段（`确人`→确认、`开起`→开启、
    `立度`→力度），今天会被改写成**行为词/属性词**——改的是这句话是什么动作，不是哪台设备。
    """
    prior = homophone.prior_names(HA_NAMES)
    banned = {"确认", "开启", "关闭", "暂停", "力度", "音量", "状态", "电池电压"}
    assert not (banned & set(prior)), "能力段混进先验：%s" % sorted(banned & set(prior))
    for spoken in ("把确人打开", "把灯开起", "调立度到五十", "关毕窗户"):
        out, pairs = homophone.rewrite(spoken, prior)
        assert pairs == [], "控制词/属性词被当目标改写：%s -> %s %s" % (spoken, out, pairs)


def test_non_han_first_segment_drops_the_whole_name():
    """首段非汉字 ⇒ 整条不参与（`V3 音量`/`Sun 下次日出`/`HUIJIAN-EB60 媒体播放器`）。
    代价是 `媒体播放器` 不再是被救目标——**宁可漏改**，与 `_admissible` 同规。"""
    prior = homophone.prior_names(HA_NAMES)
    assert "音量" not in prior and "下次日出" not in prior and "媒体播放器" not in prior, prior


# ── 接线与同源不变量 ──────────────────────────────────────────────
def test_pipeline_prior_source_is_the_device_face_and_stays_single_point():
    """接线形状钉：级联改写入口只经 `prior_universe()` 取先验，而该口体内必须是
    `prior_names(_device_names(), _alias_names()) + _real_areas()`；**全链只有一个改写点**
    （多处调用会各自漂移）；不读进程级全局词表。
    `_device_names()` 本身不许被换掉——它与「点名设备查无」子闸同源。
    v1.2.3 升级理由：这一份词表现在**两个**消费者共用（级联改写、吃热词档的解码期偏置），
    "改写点单点"不够承重，必须升级为"先验构造单口"——两套词表必然各自漂移。"""
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "..", "core", "pipeline.py"),
               encoding="utf-8").read()
    i = src.index("homophone.rewrite(")
    assert src.count("homophone.rewrite(") == 1
    call = src[i:i + 120]
    assert "self.prior_universe()" in call, call
    assert "ALL_DEVICES" not in call, "改写层不得读进程级全局词表"
    j = src.index("def prior_universe(")
    body = src[j:j + 1200]
    assert "prior_names(self._device_names()," in body, body
    assert "self._alias_names()" in body              # v1.2.3：别名同口进先验
    assert "self._real_areas()" in body
    assert "ALL_DEVICES" not in body, "先验单口不得读进程级全局词表"
    # 反向：旧形（整份拆词当先验）不许复活
    assert "T.tokens_of(self._device_names())" not in src, \
        "tokens_of 整份当先验的旧形回来了——能力段会重新混进先验"


# ── 刀A（v1.2.3）：HA 语音别名进先验，但本家能力词不许借道回来 ──────────
# 现场：`ha_client._parse_registry` 已经在读实体注册表的
# `aliases`（v1.1.4 注释写得很清楚——"用户自己在 HA 设备与服务→实体→别名里写的叫法，
# 过去我们完全没读"），路由词表 `targets.sync_vocab` 也吃了它；
# 但同音改写层的先验只取 `friendly_name` ⇒ **用户自造叫法的同音错形这一层完全救不到**
# （说「小兰」而别名是「小蓝」时，字面查无、拼音键里又没有「小蓝」＝原样放行给 klar）。
# 反向风险也要一起钉死：注册表里的 `name/original_name` 常常就是能力名（`电池电压`），
# 别名通道不得把刀1 刚刚请出去的能力词再请回来——"本家能力词"从**清单自身**推出来
# （每条 friendly_name 除首段外的段），不靠手写禁词表。
def test_alias_names_join_the_prior():
    """别名「小蓝」在清单里以 aliases 形式存在时，同音错形「小兰」必须归一回「小蓝」。

    地形注意（我自己先踩过）：第二个别名不能与第一个同音——`小篮` 与 `小蓝` 拼音串相同，
    歧义护栏会把整条键作废，那测的是护栏不是别名通道。这里用不同音的第二别名做陪衬。
    """
    names = ("灯 亮度", "灯 色温", "客厅主灯")
    aliases = ("小蓝", "夜灯")
    prior = homophone.prior_names(names, aliases)
    out, pairs = homophone.rewrite("把小兰打开", prior)
    assert out == "把小蓝打开" and pairs == [("小兰", "小蓝")], (out, pairs)


def test_alias_channel_must_not_smuggle_capability_words_back():
    """反向臂：与**本家能力词**同形的注册表名（`亮度`/`色温`）不许借别名通道进先验。
    否则刀1 刚从 `tokens_of` 里请出去的动作/属性词，又从别名这条路回来了。"""
    names = ("灯 亮度", "灯 色温", "客厅主灯")
    aliases = ("亮度", "色温", "小蓝")
    prior = homophone.prior_names(names, aliases)
    assert "亮度" not in prior and "色温" not in prior, prior
    assert "小蓝" in prior, prior
    out, pairs = homophone.rewrite("把应度调高", prior)
    assert pairs == [], f"能力词借别名通道回来了：{out} {pairs}"


def test_alias_names_source_is_the_registry_alias_map_not_global_vocab():
    """接线形状钉：别名必须与 `_device_names()` 一道过 `prior_names`，且这一句只写在
    单口 `prior_universe()` 体内——不得改成读进程级全局词表。
    v1.2.3 起锚点从"改写调用那 260 字符"改为"单口函数体"：先验构造收进单口后，
    按旧锚点切片会抓到 `_alias_names()` 的 docstring（里面正好有同名字面），假绿。"""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                             "core", "pipeline.py"), encoding="utf-8").read()
    assert "self._alias_names()" in src, "别名没接进级联先验"
    i = src.index("def prior_universe(")
    call = src[i:i + 1200]
    assert "homophone.prior_names(" in call, call
    assert "self._device_names()" in call and "self._alias_names()" in call, call
    assert "ALL_DEVICES" not in call
    assert "homophone.rewrite(text, self.prior_universe())" in src, \
        "改写层没走单口——两套本家名必然各自漂移"
    # `_alias_names()` 的取数面：注册表 + 用 states 的 friendly_name 去掉重复
    j = src.index("def _alias_names")
    body = src[j:j + 900]
    assert "_entity_alias" in body, body[:200]


def test_alias_names_method_reads_registry_and_dedups_friendly_name():
    """真方法行为钉（不是复制逻辑）：直接调 `VoicePipeline._alias_names()`，
    喂一个带 `_states`/`_entity_alias` 的假 ha ——别名要进来、与该实体 friendly_name
    重名的那条要剔掉、脏值不得炸。`_entity_alias` 缺失 ⇒ 空表（行为退化成 v1.2.2）。"""
    from core.pipeline import Pipeline as VoicePipeline

    class _Ha:
        _states = {"light.x": {"attributes": {"friendly_name": "客厅主灯"}},
                   "light.y": {"attributes": {}}}
        _entity_alias = {"light.x": ["客厅主灯", "小蓝"],
                         "light.y": ["夜灯", None, "  "]}

    p = VoicePipeline.__new__(VoicePipeline)
    p.ha = _Ha()
    got = p._alias_names()
    assert "小蓝" in got and "夜灯" in got, got
    assert "客厅主灯" not in got, f"与 friendly_name 重复的条目没剔掉：{got}"
    assert None not in got and "" not in got, got

    class _NoReg:
        _states = {}
    p.ha = _NoReg()
    assert p._alias_names() == (), "注册表缺失时必须退化而不是炸"

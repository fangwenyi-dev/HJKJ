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
    """接线形状钉：级联入口喂的是 `prior_names(_device_names()) ∪ _real_areas()`，
    且**全链只有一个改写点**（多处调用会各自漂移）；不读进程级全局词表。
    `_device_names()` 本身不许被换掉——它与「点名设备查无」子闸同源。"""
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "..", "core", "pipeline.py"),
               encoding="utf-8").read()
    i = src.index("homophone.rewrite(")
    assert src.count("homophone.rewrite(") == 1
    call = src[i:i + 240]
    assert "prior_names(self._device_names())" in call, call
    assert "self._real_areas()" in call, call
    assert "ALL_DEVICES" not in call, "改写层不得读进程级全局词表"
    # 反向：旧形（整份拆词当先验）不许复活
    assert "T.tokens_of(self._device_names())" not in src, \
        "tokens_of 整份当先验的旧形回来了——能力段会重新混进先验"

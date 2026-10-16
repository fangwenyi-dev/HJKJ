# -*- coding: utf-8 -*-
"""相对延时语句识别（v1.5.8 时间语音批，纯函数、零依赖、永不抛）。

现场（2026-10-15 18:00 现网日志）：
    [级联] '十秒后打开办公室射灯'                     → [fallback] 如实拒（安全但不支持）
    [级联] '打开办公室射灯然后十秒钟后关闭办公室射灯' → [chain]  **立即开又立即关＋播「都办妥了」**

第二行是真缺陷：`split_compound` 在"然后"处切腿后，"十秒钟后"被下游剥成普通开关句
⇒ 延时被悄悄丢弃、动作立即执行，还宣称办妥（用户红线：半执行/谎报比不执行更坏）。

本模块只做**文字→秒数**的识别（调度在 pipeline）。两把尺子分开：
· `has_delay_shape`：句子是否**长得像**相对延时（不管范围）。级联用它决定"要不要接管"
  ——凡延时形状的句一律接管处置，**绝不**放它回到"剥词→立即执行→谎报"的老链。
  范围外（如"2小时后"）也在这里被拦下、如实拒，而不是漏给老链。
· `parse_leading`：整句＝**句首**一个 `N(个/钟)?单位(之?以?后)` + 干净正文（正文里不再
  含延时）⇒ 返回 (秒, 正文, 延时短语)＝唯一"可排程"入口，到点执行正文。宁可漏排不错排。

数字档：阿拉伯（10）与中文（十/二十三/一百二十，复用 targets.cn2num 同一张表）。
单位 秒/分钟/分/小时（"个""钟"可选：十秒钟后 / 一个小时后）；尾缀 后/以后/之后。
范围闸：1s..3600s（一小时）内才可排；形状对但越界 → has_shape 真、parse_leading None
（＝接管但拒排，给"超出支持范围"如实话术）。
「稍后/待会儿/等一下」**不算**（无具体时长，算＝凭空猜时刻，不接管，交原路径）。
"""
from __future__ import annotations

import re

from .targets import cn2num

_NUM = r"\d{1,4}|[零一二三四五六七八九十百两]{1,6}"
_UNIT = r"(?:秒|分钟|分|小时)"
# 单位与"后"之间允许口语量名"钟"与"之/以"：十秒钟后 / 一分钟之后 / 半小时后(不匹配半小时)
_MID = r"\s*(?:钟)?\s*(?:之?以?后|后)"
# 任意位置的相对延时形状（不要求吃正文）：链腿里"十秒后关Y"也算。
_ANY = re.compile(rf"(?P<n>{_NUM})\s*(?:个)?\s*(?P<unit>{_UNIT}){_MID}")
# 句首前导延时（可排程唯一入口）：整句以延时开头，正文≥2字。
_LEADING = re.compile(
    rf"^(?P<n>{_NUM})\s*(?:个)?\s*(?P<unit>{_UNIT}){_MID}\s*(?P<body>.{{2,}})$")

_UNIT_SECONDS = {"秒": 1, "分": 60, "分钟": 60, "小时": 3600}
DELAY_MIN_S = 1
DELAY_MAX_S = 3600


def _seconds_of(num_s: str, unit: str):
    """N+单位 → 秒；不可解析 → None。**不判范围**（范围交给调用方区分"形状对但越界"）。"""
    try:
        if num_s.isascii() and num_s.isdigit():
            n = int(num_s)
        else:
            n = int(cn2num(num_s))
    except Exception:  # noqa: BLE001
        return None
    sec = n * _UNIT_SECONDS.get(unit, 0)
    return sec or None


def has_delay_shape(text: str) -> bool:
    """是否长得像相对延时（任意位置、任意范围）。级联接管判据，永不抛。"""
    try:
        for m in _ANY.finditer(text or ""):
            if _seconds_of(m.group("n"), m.group("unit")) is not None:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def delay_seconds(text: str):
    """句中首个可解析的相对延时秒数（**不判范围**——范围由调用方区分"排不了"的
    两种话术：形状对在范围内=组合句不排；越界=超出支持范围）。没有则 None。永不抛。"""
    try:
        for m in _ANY.finditer(text or ""):
            sec = _seconds_of(m.group("n"), m.group("unit"))
            if sec is not None:
                return sec
    except Exception:  # noqa: BLE001
        return None
    return None


def parse_leading(text: str):
    """整句＝句首延时 + 干净正文 → (秒, 正文, 延时短语)；否则 None。永不抛。

    仅认在范围内的延时；正文里不得再含第二个延时形状（含则不排＝多个待排动作，
    本批不拆，交上层按 shape 如实拒）。
    """
    try:
        t = (text or "").strip()
        m = _LEADING.match(t)
        if not m:
            return None
        sec = _seconds_of(m.group("n"), m.group("unit"))
        if sec is None or sec < DELAY_MIN_S or sec > DELAY_MAX_S:
            return None
        body = m.group("body").strip()
        if not body or has_delay_shape(body):
            return None
        phrase = t[:m.start("body")].strip()
        return sec, body, phrase
    except Exception:  # noqa: BLE001
        return None

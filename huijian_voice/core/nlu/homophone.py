# -*- coding: utf-8 -*-
"""清单驱动的**同音改写**（远场优化 L1 Tier A，2026-10-06）。

为什么要有这一层：远场低 SNR 下 SenseVoice 的失效形态是"流利但选错字"——
`悬窗` 被写成 `旋窗/玄窗/宣窗/选窗`。这些形态**拼音串与原话完全相同**，声学判得
没错，错的只是选字，所以抬增益、开降噪、往手工纠错表里追加条目都治不到（2026-10-06
现网 11 个错形对手工表 11 不命中即为证据）。本层把"本家清单"当词表先验用：片段字面
在本家查无、而它的拼音串**唯一**对应一个本家名时，改写为该规范写法。

四条红线（与 v1.1.35「关掉会飞的灯」真关射灯事故同族，逐条有钉）：
  · **歧义不改**：两个及以上本家名同音 ⇒ 这条拼音键整条作废，一个字都不动
    （`平开窗 ① 开启` 与 `平开窗 ② 开启` 拆词后同音塌缩，正是此档）；
  · **字面已在本家 ⇒ 不改**：用户真说的就是它，不得被同音邻居顶掉；
  · **非汉字名不参与**：拉丁/数字短名（`Sun` 3 字符 1 音节）两侧都不进比对，否则音节
    差会被假读成 0（2026-10-05 现网清单实证过这个短路）。承重闸门是 `_HAN`（索引侧
    与片段侧各一道）；`_syl()` 里的"一字一音节"长度等式与它**互为冗余带**（变异验证
    显示单摘其一不转红），保留的理由是它同时是"改写两侧等长"这条不变量的来源——
    该不变量的后果由钉 `test_replacement_never_shifts_positions` 直接守，不靠实现细节。
  · **不吞正说对的名字**：话里字面正命中的本家长名，其真子串不得被改写
    （防「悬窗 开窗器」里的「开窗」被顶成别的同音名）。

pypinyin 缺失或异常 ⇒ 原样返回，行为逐值回落到没有本层之前（与 `_generic_rescue`
的 fail-open 同口径：绝不因缺依赖改现网行为）。
"""
import logging
import re

logger = logging.getLogger(__name__)

MIN_LEN, MAX_LEN = 2, 8                       # 与 targets._name_tokens 同口径
_HAN = re.compile(r"^[\u4e00-\u9fff]+$")

_lazy_pinyin = None                           # 首次调用绑定；导入失败置 False，不再重试
_INDEX_CACHE: tuple = (None, None)            # (names 对象, 音节倒排)——ALL_DEVICES 整体替换赋值


def _syl(word: str):
    """词 → 音节串 tuple；非"一字一音节"或拼音库不可用 → None（不参与比对）。"""
    global _lazy_pinyin
    if _lazy_pinyin is False:
        return None
    if _lazy_pinyin is None:
        try:
            from pypinyin import lazy_pinyin
        except ImportError:
            _lazy_pinyin = False
            return None
        _lazy_pinyin = lazy_pinyin
    try:
        s = _lazy_pinyin(word)
    except Exception:  # noqa: BLE001 —— 救援层不得冒泡到级联
        return None
    if not isinstance(s, list) or len(s) != len(word):
        return None
    return tuple(s)


def homophone_index(names):
    """音节串 → {本家写法}。只收纯汉字、2~8 字、一字一音节的名。

    歧义档（同音两个以上写法）**留在返回值里**由调用方丢弃——钉要钉在"这里确实存在
    两个同音写法"上，而不是钉在"改写函数没动文本"这种间接表征上。
    ALL_DEVICES 每次换绑都是整体赋值（`targets.py:365`），故按对象身份缓存倒排。
    """
    global _INDEX_CACHE
    if _INDEX_CACHE[0] is names:
        return _INDEX_CACHE[1]
    idx: dict[tuple, set] = {}
    for w in names or ():
        w = str(w or "").strip()
        if not (MIN_LEN <= len(w) <= MAX_LEN) or not _HAN.match(w):
            continue
        s = _syl(w)
        if s is None:
            continue
        idx.setdefault(s, set()).add(w)
    _INDEX_CACHE = (names, idx)
    return idx


def rewrite(text: str, names) -> tuple[str, list[tuple[str, str]]]:
    """同音唯一命中 → 改写。返回 (文本, [(原片段, 规范写法), ...])；不改 → (text, [])。

    幂等：改写后的片段字面即在本家清单里，再跑一次是空操作。
    改写两侧同音节数 ⇒ 长度不变 ⇒ 位移与覆盖标记恒成立。
    """
    if not text:
        return text, []
    idx = {k: v for k, v in homophone_index(names).items() if len(v) == 1}
    if not idx:
        return text, []
    literal = frozenset(str(w).strip() for w in names or () if str(w or "").strip())
    guarding = [str(w).strip() for w in names or ()
                if w and len(str(w).strip()) >= 2 and str(w).strip() in text]
    changed: list[tuple[str, str]] = []
    out = text
    covered = [False] * len(out)
    for width in range(MAX_LEN, MIN_LEN - 1, -1):          # 长窗口优先，防「悬窗」被切碎
        start = 0
        while start + width <= len(out):
            if any(covered[start:start + width]):
                start += 1
                continue
            span = out[start:start + width]
            if not _HAN.match(span) or span in literal:
                start += 1
                continue
            if any(span in g and span != g for g in guarding):
                start += 1                                  # 正命中长名的真子串 ⇒ 不动
                continue
            key = _syl(span)
            cand = idx.get(key) if key is not None else None
            if not cand:
                start += 1
                continue
            to = next(iter(cand))
            out = out[:start] + to + out[start + width:]
            for i in range(start, start + width):
                covered[i] = True
            changed.append((span, to))
            start += width
    return out, changed

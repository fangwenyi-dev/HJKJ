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
_VEC_CACHE: tuple = (None, None)              # (names 对象, 逐词音节向量)——近音档用，同上按身份缓存

# 声母表：最长优先，zh/ch/sh 必须排在 z/c/s 前，否则 `窗 chuang` 会被切成 c+huang
_INITIALS = ("zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l",
             "g", "k", "h", "j", "q", "x", "r", "z", "c", "s")
# 同发音部位的送气↔不送气对。**不含** h/x、f/h 这类"部位不同"的组合——那是另一个词。
_ASP = frozenset(frozenset(p) for p in
                 (("b", "p"), ("d", "t"), ("g", "k"), ("z", "c"), ("zh", "ch"), ("j", "q")))


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


def prior_names(raw_names, extra=()) -> tuple:
    """本层先验宇宙的唯一构造口：HA `friendly_name` 清单（＋注册表别名）→ **可当目标的本家名**。

    为什么不能把 `targets.tokens_of()` 的整份拆词结果直接当先验：现网清单的形状一律是
    `<设备名> <能力后缀>`（2026-10-05 办公 .91 真快照实证：`悬窗 ① 开启`、`推拉窗 ③ 关闭`、
    `开窗器 123f-020A 速度`、`射灯 确认`、`V3 音量`）。整片拆词会把**动作词与属性名**
    （开启/暂停/关闭/力度/状态/电池电压/音量/确认）一起收进"本家名字"里。本层的产物是
    **改写后的文本**，它还要继续走确认环、复合切分、直调与集成展开——归一目标一旦是
    `确认/关闭/开启` 这类控制词，改的就不是"哪台设备"而是"这句话是什么行为"。
    v1.1.17「摄像机→洗碗机」立的规矩（`_admissible`：不得凭空造本家没有的目标）在这里
    同族，只是入口换到了词表层。

    规则＝只取**首个空格前那段**；段本身非汉字或长度越界 ⇒ 整条不参与（由
    `homophone_index` 的 `_HAN`/长度闸门执行）。方向性＝**收窄只会漏改，不会多改**，
    所以丢掉 `媒体播放器` 这类"名字本身带空格"的救正是我们要的偏向。
    去重保序：`返回 tuple` 供 `_INDEX_CACHE` 按对象身份缓存。

    `extra`＝本台 HA 注册表里的**别名**（用户亲手写的叫法，`ha_client._entity_alias`）。
    v1.2.3 补的口：路由词表 `sync_vocab` 从 v1.1.4 起就吃别名，本层却只看 friendly_name
    ⇒ 说「小兰」而别名是「小蓝」时字面查无、键里又没「小蓝」，整条只能原样放行给 klar。
    别名通道**不许把刀1 请出去的能力词再请回来**：`cap` 由清单自身推出（每条 friendly_name
    除首段外的所有段），命中即丢弃——不靠手写禁词表，也不靠"哪个域像能力名"的猜测。
    """
    raw = [str(w or "").strip() for w in (raw_names or ())]
    raw = [w for w in raw if w]
    cap = set()
    for w in raw:
        segs = w.split()
        cap.update(segs[1:])
    out, seen = [], set()

    def keep(seg):
        if seg and seg not in seen and seg not in cap:
            seen.add(seg)
            out.append(seg)

    for w in raw:
        keep(w.split(maxsplit=1)[0])
    for a in (extra or ()):
        a = str(a or "").strip()
        if a:
            keep(a.split(maxsplit=1)[0])
    return tuple(out)


def homophone_index(names):
    """音节串 → {本家写法}。只收纯汉字、2~8 字、一字一音节的名。

    歧义档（同音两个以上写法）**留在返回值里**由调用方丢弃——钉要钉在"这里确实存在
    两个同音写法"上，而不是钉在"改写函数没动文本"这种间接表征上。
    缓存按**内容**判、不按对象身份：本层从不读 `ALL_DEVICES`，调用方每句现造新 tuple
    （`pipeline.prior_universe()` = `prior_names(...) + _real_areas()`），按 `is` 判必
    恒 miss＝死码 + 每句重建音节表（旧注释引 `targets.py:365` 的整体赋值规矩，
    那个前提在这里不成立）。内容比较 O(n)，仍远省于每句重跑 `lazy_pinyin`。
    """
    global _INDEX_CACHE
    key = tuple(names or ())
    if _INDEX_CACHE[0] == key:
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
    _INDEX_CACHE = (key, idx)
    return idx


def _split(syl: str) -> tuple[str, str]:
    """无声母整音节 → (声母, 韵母)。声母表按"最长优先"排（zh/ch/sh 必须先于 z/c/s）。"""
    for i in _INITIALS:
        if syl.startswith(i):
            return i, syl[len(i):]
    return "", syl


def _confusable(a: str, b: str) -> bool:
    """两个音节是否只差**一个远场可解释特征**。相同也算通过（调用方自己数差异位）。

    远场低 SNR 先丢的是低能量线索，实测就集中在三类（本仓量过 4–8 kHz 擦音段在
    SNR≈0 档丢 1.4 dB，见 docs/internal 远场批记录）：
      ① 同发音部位的送气↔不送气：b/p、d/t、g/k、z/c、zh/ch、j/q（`带灯`↔`台灯`）；
      ② 鼻音尾↔无尾：-n↔-ng（`频开窗`↔`平开窗`）；
      ③ 介音丢/多：i/u/ü 的有无（`干连器`↔`关联器`）。
    **跨发音部位不算**（`平台窗` 的 tai↔kai 是部位差＝另一个词），韵母主体不同也不算
    （`旋称`cheng vs `悬窗`chuang 差的不止一个特征）。方向＝宁漏改不多改。
    """
    if a == b:
        return True
    ia, fa = _split(a)
    ib, fb = _split(b)
    if ia == ib:
        if fa + "g" == fb or fb + "g" == fa:        # -n ↔ -ng
            return True
        for m in ("u", "i", "v"):                    # 介音有无（pypinyin 的 ü 写作 v）
            if fa == m + fb or fb == m + fa:
                return True
        return False
    if frozenset((ia, ib)) in _ASP and fa == fb:     # 同部位送气对，韵母不动
        return True
    return False


def near_index(names):
    """可参与近音档比对的**本家音节向量**表：纯汉字、2~8 字、一字一音节。

    与第一档同源同闸（`_HAN`＋`_syl`），只是这里留的是逐词向量而非拼音串键——
    第二档要逐位置比，键相等那条判据用不上。缓存同 `_INDEX_CACHE`：按**内容**判。
    """
    global _VEC_CACHE
    key = tuple(names or ())
    if _VEC_CACHE[0] == key:
        return _VEC_CACHE[1]
    vecs = []
    for w in names or ():
        w = str(w or "").strip()
        if not (MIN_LEN <= len(w) <= MAX_LEN) or not _HAN.match(w):
            continue
        s = _syl(w)
        if s is not None:
            vecs.append((w, s))
    _VEC_CACHE = (key, vecs)
    return vecs


def _near_one(names_vecs, key):
    """与 key 同长度、**恰好一个音节位**不同、且那一个不同属可容混淆的本家名。
    返回列表（0/1/多）——多即歧义，由调用方按"一个都不改"处理。

    这里**不**按"名字是否字面在清单里"筛候选：本表里每一条都在清单里，那样筛等于筛空
    （第一版我就栽在这上面——`w in literal` 恒真，三条正向钉全红才发现）。片段自己
    是否字面已在话里，由调用点的 `span in literal` 与 `guarding` 两道管。
    """
    hit = []
    for w, wkey in names_vecs:
        if len(wkey) != len(key):
            continue
        diff = [i for i in range(len(key)) if key[i] != wkey[i]]
        if len(diff) == 1 and _confusable(key[diff[0]], wkey[diff[0]]):
            hit.append(w)
    return hit


def rewrite(text: str, names) -> tuple[str, list[tuple[str, str]]]:
    """同音唯一命中 → 改写。返回 (文本, [(原片段, 规范写法), ...])；不改 → (text, [])。

    幂等：改写后的片段字面即在本家清单里，再跑一次是空操作。
    改写两侧同音节数 ⇒ 长度不变 ⇒ 位移与覆盖标记恒成立。
    """
    if not text:
        return text, []
    idx = {k: v for k, v in homophone_index(names).items() if len(v) == 1}
    vecs = near_index(names)
    if not idx and not vecs:
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
            if key is None:
                start += 1
                continue
            cand = idx.get(key)
            if cand:
                to = next(iter(cand))                        # 第一档：拼音串完全相同
            else:
                near = _near_one(vecs, key)                  # 第二档：恰一个可容特征之差
                if len(near) != 1:
                    start += 1            # 0＝不在射程；≥2＝歧义。两种都宁可不改
                    continue
                to = near[0]
            out = out[:start] + to + out[start + width:]
            for i in range(start, start + width):
                covered[i] = True
            changed.append((span, to))
            start += width
    return out, changed

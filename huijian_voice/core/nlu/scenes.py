"""语音场景触发缓存（HassListVoiceScenes，60s TTL + 未命中强刷一次）。

契约（盘点 §3.1/§3.2）：huijian_ai 的列表意图返回裸 dict {success, scenes:[{trigger_phrase,...}]}；
触发走 HassTriggerVoiceScene {trigger_phrase}。fast_path v1.5 的 _refresh_scenes/_check_scene
逐语义移植：MCP 调用 → HA REST（ha_client），文本缓存判定（相等或前缀）不变。

体验批（2026-09）：TTL 到期后的刷新转后台（refresh_soon），不再压在语音关键路径上
——每 60s 有一句要同步等 HassListVoiceScenes 最坏 5s 的尖刺就此消掉。冷缓存例外：
从未成功加载过时仍同步拉一次（needs_blocking），只发生在启动后的头几句。
"""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger("huijian.scenes")

TTL_S = 60.0

# 归一化只去空白与句读（**不**借 fast_path 的 normalize_polite：scenes 被 fast_path
# 导入，反向 import 会成环；礼貌尾在原生形状里由「的场景/场景」式形态显式覆盖）。
_NORM_DROP = " \t\r\n。，,、；;：:！!？?～~「」『』“”\"'（）()【】"


def _norm(s) -> str:
    try:
        return "".join(ch for ch in str(s or "") if ch not in _NORM_DROP)
    except Exception:  # noqa: BLE001
        return ""


def _build_native_sync(sc: "SceneCache", states) -> bool:
    """纯同步建表（与 `_refresh_native` 同一套判据，不复制第二份规则：这里只做
    "读 dict → 分组"这一步，语义与协程版逐字一致）。永不抛。"""
    try:
        native: dict[str, list] = {}
        occupied: set[str] = set()
        for eid, v in states.items():
            eid = str(eid or "")
            fn = str(((v or {}).get("attributes") or {})
                     .get("friendly_name") or "").strip()
            if not eid.startswith("scene."):
                if fn:
                    occupied.add(fn)
                continue
            if str((v or {}).get("state") or "") == "unavailable":
                continue
            native.setdefault(fn or eid, []).append(eid)
        sc._native = native
        sc._occupied = occupied
        sc._native_ts = time.time()
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[场景] 同步建表异常（转后台自愈）", exc_info=True)
        return False


class SceneCache:
    # P4（2026-10-10）原生场景的**允许形状**（保守到不发虚）：
    #   ·「观影」「观影场景」「观影的场景」——名字本体或带场景标记；
    #   ·「执行观影场景」「运行观影」「切换观影」——动词＋上面三种之一。
    # 刻意**不做前缀吞句**：原生场景名短又常见（「回家」「晚安」「观影」），沿用语音
    # 库那条"最长前缀命中"会让「观影模式调亮一点」被吞成触发场景。语音库是用户被明确
    # 告知过的契约（说 X 就 Y），原生场景不是，判据必须更紧。
    NATIVE_HEAD_VERBS = ("执行", "运行", "启动", "切换", "启用", "开始", "来一个", "来段")
    NATIVE_NAME_TAILS = ("场景", "的场景")

    def __init__(self, ha):
        self.ha = ha
        self._triggers: list[str] = []
        self._scenes: list[dict] = []
        # P4：HA **原生场景**（用户在界面/YAML 建的 `scene.*`）。现网实锤：
        # `scene.guan_ying`（友好名「观影」）说「观影场景」「执行观影场景」全落
        # `fallback`——本缓存此前只有 `HassListVoiceScenes`（加载项自有语音场景库）
        # 一个数据源，原生场景压根不在表里。形态 {名称: [entity_id,...]}：
        # 同名多条是真实可能（HA 允许重名场景），那时一律不接管——绝不猜。
        self._native: dict[str, list] = {}
        # P4′：同一份 states 里**所有非 scene 实体**的 friendly_name——"场景别抢设备名"的
        # 唯一可靠清单（`targets._dyn_vocab` 只留 2~8 个纯汉字，英文名/长名是结构性盲区）。
        self._occupied: set[str] = set()
        # P4′（10-11）：原生表**自己管自己的新鲜度**——它此前借用语音库的
        # `_last_refresh/_loaded`，而那是另一条链的闸：语音库只要"尝试过"就不再
        # 同步刷新 ⇒ 原生表一旦没建成就永久空着（真机慢链路实测：#20/#21 落
        # fallback）。分开计数才能自愈。
        self._native_ts = 0.0
        self._native_attempt = 0.0
        self._native_bg: asyncio.Task | None = None
        self._last_refresh = 0.0
        self._last_attempt = 0.0
        self._loaded = False          # 是否成功加载过一次（冷启动判据）
        self._lock = asyncio.Lock()
        self._bg: asyncio.Task | None = None   # 后台刷新单飞（强引用，session F7b 纪律）

    def needs_blocking(self) -> bool:
        """仅「从未加载成功 且 距上次尝试超 TTL」才允许同步刷新（冷启动兜底）；
        稳态一律走后台，语音路径零等待。"""
        return (not self._loaded) and (time.time() - self._last_attempt >= TTL_S)

    async def refresh(self, force: bool = False) -> bool:
        """v1.0.41（F5）：返回显式成败。handle_intent 恒折叠不抛，上层 try/except
        是死代码——「集成掉线如实说明」必须看返回值：True=拉取成功（或缓存尚在
        TTL 内被跳过）；False=本轮拉取失败（旧缓存照常保留，HA 重启窗口不误清）。"""
        async with self._lock:
            now = time.time()
            if not force and now - self._last_refresh < TTL_S:
                return True
            self._last_attempt = now
            # P4：原生场景先取。`ha.states()` 是客户端自己的状态缓存（60s TTL），
            # 只读快照、零额外请求；兜底请求只发生在后台任务里）；
            # 成败判定（那个值专指语音场景库，admin/创建链都按它决策，语义不动）。
            await self._refresh_native()
            result = await self.ha.handle_intent("HassListVoiceScenes", {}, timeout=5.0) or {}
            if result.get("success") and isinstance(result.get("scenes"), list):
                self._scenes = [s for s in result["scenes"] if isinstance(s, dict)]
                self._triggers = [s.get("trigger_phrase", "") for s in self._scenes if s.get("trigger_phrase")]
                self._last_refresh = now
                self._loaded = True
                logger.debug("[场景] 缓存 %d 个触发词、%d 个原生场景",
                             len(self._triggers), len(self._native))
                return True
            # 失败保留旧缓存（HA 重启窗口期不误清空）
            return False

    async def _refresh_native(self, allow_fetch: bool = False) -> bool:
        """建原生场景索引与"在装设备整名"表。**快照优先，取不到就兜一次带上限的请求**。

        成本与正确性的取舍（10-10 两轮纠正，别再摆回任何一端）：
          · 只读 `ha._states`（`HAClient.refresh_states()` 产物，`_CACHE_TTL=5.0s`）是
            **0 时延**那条路，稳态绝大多数句子走它；
          · 但**不能把它当唯一来源**——那等于赌"别人已经把快照填好了"。真实现场：慢链路下
            金标双臂 `#20/#21 观影场景` 直接落 fallback（局域网那次还是 PASS），因为首轮
            刷新时快照未填而本道静默不可用。"功能看运气"正是本仓定义的假成功变体，
            必须由判据本身堵住，不靠调用顺序碰巧对；
          · 兜底那次 `ha.states()` 套 **1.0s 上限**（`asyncio.wait_for`）：`states()` 内部
            无逐请求超时、受 session `ClientTimeout(total=15)` 兜底，不设上限就是把最坏
            15s 串进语音路径。超时 ⇒ 本轮放弃、保留旧表，下一句再试：
            **宁可晚几秒可用，不可拖死首句**。

        `_occupied`（所有非 scene 实体的 friendly_name）同表产出：它是"场景别抢设备名"的
        唯一可靠清单（`targets._dyn_vocab._name_tokens` 只留 2~8 个纯汉字，英文名与九字
        以上口令名全漏 —— 复核实锤的结构性盲区）。
        """
        try:
            states = getattr(self.ha, "_states", None)
            if not isinstance(states, dict) or not states:
                # 快照未填：只有**后台任务**才允许发这次带上限的请求；
                # `refresh()` 可能被语音首句同步 await（冷启动闸），那条路上绝不等待网络。
                if not allow_fetch:
                    return False
                getter = getattr(self.ha, "states", None)
                if getter is None:
                    return False
                try:
                    states = await asyncio.wait_for(getter(), timeout=2.0)
                except asyncio.TimeoutError:
                    logger.info("[场景] 快照未填且 /states 2s 内未回 ⇒ 本轮不建原生场景表"
                                "（后台任务下句再试，语音路径从不等它）")
                    return False
                except Exception:      # noqa: BLE001 客户端已折叠异常，这里只是双保险
                    logger.debug("[场景] 兜底 states() 异常（保留旧表）", exc_info=True)
                    return False
            if not isinstance(states, dict) or not states:
                return False
            native: dict[str, list] = {}
            occupied: set[str] = set()
            for eid, v in states.items():
                eid = str(eid or "")
                fn = str(((v or {}).get("attributes") or {})
                         .get("friendly_name") or "").strip()
                if not eid.startswith("scene."):
                    if fn:
                        occupied.add(fn)
                    continue
                if str((v or {}).get("state") or "") == "unavailable":
                    continue          # 离线的场景不接管（接管了也只是又一次"成功但没执行"）
                native.setdefault(fn or eid, []).append(eid)
            self._native = native
            self._occupied = occupied
            self._native_ts = time.time()
            return True
        except Exception:      # noqa: BLE001 观测面不得伤语音链
            logger.debug("[场景] 原生场景索引刷新失败（保留旧表）", exc_info=True)
            return False

    def native_from_cache(self) -> bool:
        """**快照已热时同步建表**：零网络、零额外时延，当句就能用。

        与 `native_soon()` 分职——本方法一次请求都不发，所以能安全放在语音路径上；
        需要发请求的冷启动场景才交给后台自愈。上一版我把建表整个搬进后台，结果是
        "快照明明热着、当句却说我不会"：用首句可用性换时延等于换了个方向的缺陷。
        """
        try:
            states = getattr(self.ha, "_states", None)
            if not isinstance(states, dict) or not states:
                return False
            return _build_native_sync(self, states)
        except Exception:      # noqa: BLE001 观测面不得伤语音链
            logger.debug("[场景] native_from_cache 异常（转后台自愈）", exc_info=True)
            return False

    def native_soon(self) -> None:
        """原生场景表**自愈**：空或超过 2×TTL 就后台单飞重建，**绝不在语音路径上 await**。

        为什么必须存在：语音库那条链有自己的 60s 节流与"成功过就不再同步刷"的语义，
        原生表借用它会得到"`_loaded` 已 True ⇒ 永不重试"的死局（10-11 真机慢链路实锤：
        首轮 states 快照未填 + 兜底超时 ⇒ 表空，之后每一句都静默走 fallback，用户听到
        「这句话我还不会」而永远等不到自愈）。这里只补原生这一路，不碰语音库语义。
        """
        now = time.time()
        if self._native and now - self._native_ts < TTL_S * 2:
            return                       # 有表且不算旧 ⇒ 不打扰
        if self._native_bg is not None and not self._native_bg.done():
            return                       # 单飞：已有一条在跑就等它
        if now - self._native_attempt < 5.0:
            return                       # 退避：慢链路下也不至于每句都发一次 states()
        self._native_attempt = now

        async def _job():
            try:
                await self._refresh_native(allow_fetch=True)
            except Exception:            # noqa: BLE001 后台自愈失败静默，下句再试
                logger.debug("[场景] 原生表后台重建异常", exc_info=True)

        try:
            self._native_bg = asyncio.get_running_loop().create_task(_job())
        except RuntimeError:             # 无运行循环（单测直调）：静默跳过
            pass

    def refresh_soon(self) -> None:
        """TTL 到期 → 后台单飞刷新（不 await，不抛，主路径立即用陈旧缓存）。"""
        if self._bg is not None and not self._bg.done():
            return
        if time.time() - self._last_refresh < TTL_S:
            return

        async def _job():
            try:
                await self.refresh(force=True)
            except Exception:      # noqa: BLE001 —— 后台刷新失败静默，下句再触发
                logger.debug("[场景] 后台刷新异常", exc_info=True)

        try:
            self._bg = asyncio.get_running_loop().create_task(_job())
        except RuntimeError:       # 无运行循环（单测直调 check()）：静默跳过
            pass

    def check(self, text: str) -> str | None:
        """等值优先 → **最长**前缀命中（v1.1.27）。

        旧实现按缓存序"先命中即返回"：先建「关灯」后建「关灯睡觉」时，
        「关灯睡觉」被短触发词「关灯」吃掉——fast_path 的等值闸（phrase == text）
        落空、后续前缀兜底拿到的也是短词，用户被明确告知过"说 X 就 Y"的句子
        触发了**另一个**场景。全等仍最高优先（「开灯亮度50」不得被「开灯」前缀
        吞成场景）；前缀并列同长时按缓存序取先出现者（确定性不依赖 set 序）。
        """
        if not text:
            return None
        if text in self._triggers:                 # 全等优先（最精确语义）
            return text
        best: str | None = None
        for s in self._triggers:
            if s and text.startswith(s) and (best is None or len(s) > len(best)):
                best = s
        return best

    async def verify_or_refresh(self, phrase: str) -> bool:
        """触发词最终核验：缓存命中，或强刷一次后命中（原双查语义）。"""
        if self.check(phrase) == phrase or phrase in self._triggers:
            return True
        await self.refresh(force=True)
        return phrase in self._triggers

    def check_native(self, text: str, extra_occupied=()):
        """P4′ 原生场景匹配 → `("none", None)` / `("hit", {"name","entity_id"})` /
        `("ambiguous", {"reason","names","entity_ids"})`。永不抛（判据故障=不接管，绝不误触发）。
        歧义载荷是给上层**列候选**用的（`reason` ∈ duplicate_name 同名多条 / overlap 两条名字相撞），
        调用方拿它生成 clarify，绝不自己挑一条。

        三条硬边界，每条都对应 2026-10-10 对抗复核实锤的一条危害（别退回旧口径）：
          1. **句子里必须有字面「场景」标记**。「观影场景」「观影的场景」「执行观影场景」算；
             「观影」「启动扫地机」「切换扫地机」一律不算。复现：家里有一台逐字叫「扫地机」
             的 vacuum 时，旧口径把「启动扫地机」判成触发场景——机器没动却播「好的，扫地机
             已执行」。"动词＋裸名"与设备口令天然同形，只有用户自己说了"场景"才没有歧义。
             ⇒ **裸名通道整体取消**（连带取消 `allow_bare`），不做"名字不撞设备就接管"那种弱保险。
          2. **在装设备整名占位即让路**：`_occupied` 取自同一份 states 快照（**无字符/长度
             盲区**——复核实锤 `targets._dyn_vocab` 的 `_name_tokens` 只留 2~8 个纯汉字，
             `_name_tokens('Fan')=[]`，英文名与九字以上口令名永远进不了旧 guard）。
          3. **两种命中相撞 ⇒ 不猜**：某条场景的 marked 形与另一条场景的**整名**同时命中
             （native={观影:scene.a, 观影场景:scene.b} 说「观影场景」）或同名多条 ⇒
             `ambiguous`。旧口径在这里静默挑了 marked 那条，正是"二次按名字猜"。
        """
        try:
            t = _norm(text)
            if not t or not self._native:
                return ("none", None)
            if "场景" not in t:                 # 边界①：没点"场景"就与本道无关
                return ("none", None)
            # ⚠ 自我遮蔽防线（10-11 真机双臂实锤）——**正确形状只有一处**：
            # `_scene_extra_names()` 不再并 `targets._dyn_vocab`（那张表含 scene 实体名，
            # 会让每个原生场景被"自己"挡死）。`_occupied` 构造上**只收非 scene 实体**
            # （`_build_native_sync` 的 `if not eid.startswith("scene.")` 分支），
            # 所以设备与场景**同名**时这里必须在清单里＝让路（边界②，三条离线钉）。
            # 上一版"顺手"把 `own`（场景自己）从 claimed 里减掉，等于把让路判据废了——
            # `test_english_named_device_still_shields_scene` 等三条钉当场抓回，已回退。
            claimed_raw = self._occupied | {str(x).strip() for x in (extra_occupied or ())
                                             if str(x).strip()}
            claimed_norm = {_norm(x) for x in claimed_raw if str(x).strip()}
            marked, exact = [], []
            for name, ids in self._native.items():
                nm = _norm(name)
                if not nm:
                    continue
                forms = {nm + "场景", nm + "的场景"}
                forms |= {v + x for v in self.NATIVE_HEAD_VERBS
                          for x in (nm + "场景", nm + "的场景")}
                if t in forms:
                    marked.append((name, ids))
                elif t == nm:
                    exact.append((name, ids))
            picked = marked + exact
            if not picked:
                return ("none", None)
            names = {n for n, _ in picked}
            # 先让路、再谈歧义（2026-10-10 自查发现的顺序缺陷）：旧写法先判 ambiguous，
            # 于是「家里有一台真设备就叫这个名」时也会被场景歧义抢走一次对话回合——
            # 用户听到"两个场景重名"，而那台设备本来就该由这条口令控制。
            if any(n in claimed_raw or _norm(n) in claimed_norm for n in names):
                return ("none", None)          # 边界②：抢设备名字 ⇒ 让路
            if len(names) > 1:                 # 边界③：一条的 marked 形撞另一条的整名
                return ("ambiguous", {"reason": "overlap", "names": sorted(names),
                                      "entity_ids": [i for _, ids in picked
                                                     for i in ids]})
            name, ids = picked[0]
            if len(ids) > 1:                   # 同名多条 ⇒ 让用户去 HA 改名
                return ("ambiguous", {"reason": "duplicate_name", "name": name,
                                      "names": [name], "entity_ids": list(ids)})
            return ("hit", {"name": name, "entity_id": ids[0]})
        except Exception:      # noqa: BLE001
            # 10-11 双臂事故：判据本体抛错（NameError）被 debug 级吞了三天……不，
            # 吞到双臂才发现。判据崩溃＝功能整体哑火，必须到生产可见级别。
            logger.warning("[场景] 原生场景匹配异常（不接管）", exc_info=True)
            return ("none", None)

    def native_scenes(self) -> dict:
        """只读视图（诊断/单测用；不参与回写）。"""
        return {k: list(v) for k, v in (self._native or {}).items()}

    def occupied_device_names(self) -> set:
        """只读视图：states 快照里所有**非 scene 实体**的 friendly_name。
        这是"场景别抢设备名"的主清单（`check_native` 自用），单测据此断言英文名/长名
        也在表内——旧实现借 `targets._dyn_vocab` 只有 2~8 个纯汉字，那类名字全漏。"""
        return set(self._occupied or set())

    @property
    def triggers(self) -> list[str]:
        return list(self._triggers)

    def all(self) -> list[dict]:
        """场景全量原始 dict（Web「语音场景与自动化」页展示用；只读不回写）。"""
        return list(self._scenes)

    def name_for(self, phrase: str) -> str:
        for s in self._scenes:
            if s.get("trigger_phrase") == phrase:
                return s.get("name") or phrase
        return phrase

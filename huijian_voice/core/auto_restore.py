"""语音操作后的"归位"守卫（用户 2026-10-09 立规：语音操作 HA 设备，操作完成一定要关闭对应设备，
点名例子＝办公室射灯）。

为什么需要它（现场证据，不是假想）
    2026-10-09 现场证据（**其中一条当晚已被我自己的复查推翻，改在此留痕**）：
      ✔ 成立：办公室射灯 `light.ban_gong_shi_she_deng` 会被"打开办公室的灯"这类口令**真点亮**
         且没人关——本仓台架激励 `_live_cmdB` 整晚就在制造这个形态，23:50 我自己发现灯开着、
         手工关回。当天历史记录里它也有多次"亮 10–33 分钟无人关"。
      ✘ 撤回：傍晚我拿"平开窗 17:22 被语音打开后一直 open"当第二条证据，并按
         `/api/config` 的 automations 为空断言"HA 里没有自动化引用这些实体"。
         后来用 `/api/history/period` 查明：该窗整夜每 ~10 分钟自动 open、7~11 分钟后
         自动 closed、`context.user_id` 为空 ⇒ 有 **YAML 自动化**在驱动它，
         而 `/api/config` **只列 UI 建的自动化**、看不到 YAML 那套。
         ⇒ 窗这条不是"操作后没人关"的证据；本模块的立论只建立在射灯那一类上。
    ⇒ 但结论不变：**"操作完成"设备侧不会自己发生**，UI 自动化覆盖不到的设备（灯、插座、
      非循环执行器）仍没人收尾，所以归位放在加载项执行层，且**默认只管明确点名的域**。

★ 覆盖面实况（10-10 我连着判错两次，最终结论以读码＋真机各一遍为准）
  ✘ 第一版结论：“target 形拿不到 entity_id ⇒ 不覆盖，所以默认关”——**错**。
    设备道的回执 `control_targets` 行自 v1.2.10 起就带 `item.state.entity_id`
    （intent_turn.py:213-214，当时为的是播报按设备数数，不是为归位）。
    只有**窗控子道**那几行（:121-126/716/756）仍只有 {name, area}。
  ✔ 现在归位**吃回执**（_receipt_eids）：target 形（“打开办公室的灯”，args 里只有
    area+name）⇒ 登记回执里那台；klar 直调 /api/services/* ⇒ 200 body 就是
    “状态真变了的实体列表”，空列表＝谁都没变 ⇒ changed=False 等价 noop，不登记
    （10-10 实测四次，off→on / on→on / on→off / off→off 全部对称成立）。
  ⇒ 默认**翻回开**（用户立规要的就是这条真实生效）。真机端到端证据：生产 target 形
    “打开办公室的灯”→播“办公室射灯开了”、HA 实测 on、60 秒后自己 off、终态复原 off。
  ⚠ 仍未覆盖：窗控子道回执不带 id ⇒ “打开办公室的平开窗”这类不登记；要覆盖得先给
    窗控行补 entity_id（那边同样有 item.state.entity_id 可用）。cover 也已不在默认域。

★ 意图词表（10-10 第二版改掉自己的一处假绿）
  慧尖自有意图名是 `TurnDeviceOn/TurnDeviceOff`（fast_path.py:295/327 是生产主车道），
  HA 内置名 `HassTurnOn/Off` 只是透传族。第一版只认 Hass* ⇒ 生产一帧都不登记，
  而当时 21 条钉全绿——因为它们喂的正是 Hass*。现在两族都认（见 _direction），
  且钉**必须喂生产真名**并走 Executor.run。

边界（故意保守，写清楚免得后人当成万能）
  1. **只登记在"这一条真的执行成功且目标已被 grounded 成 entity_id"的设备上**。
     只给区域（"把客厅灯打开"→ area 扇出由 HA 自己解析）时我们不知道打了哪台 ⇒ 不归位，
     绝不靠"猜目标"去关设备（关错设备比忘关更糟）。
  2. **默认域＝light 一族**（10-10 复核后从 cover+light 收窄）：灯是“忘关”语义最干净的一类；窗/帘**不**默认纳入——现场证据是办公室那扇平开窗由一条 YAML 自动化每 ~10 分钟开合，默认计时必然与它抢关；而“打开窗帘”的终态本就是人要留下的状态（遮光/采光），自动合帘不是“补忘关”。switch/fan/media_player 同理（摆风、提示音、播放属模式语义）。要管哪几族，由用户在设置里显式扩域。
  3. 再次语音打开同一台＝**续期**；语音关闭同一台＝**取消**（人已经关了，别再去关一次）。
  4. 延时不落盘：加载项重启后未到期的归位任务丢失（不假装持久化）。这是已知限制，
     在 pending 快照里可见，便于现场排查。
  5. 归位动作本身**不再登记**（否则关一次又开一次计时，永远循环）。
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from typing import Any, Iterable, Optional

logger = logging.getLogger("huijian.auto_restore")

# 意图 → 方向。**必须按生产词表来**：慧尖自有意图名是 `TurnDeviceOn/TurnDeviceOff`
# （fast_path.py:295/327 主车道、executor.py:210 注释实锤），HA 内置名 `HassTurnOn/Off`
# 是透传族。10-10 第一版只认 Hass*，结果真语音链路（产 TurnDeviceOn）**一帧都不登记**
# ——测试全绿而生产不生效，正是本仓"钉没喂生产形状"那类假绿的翻版。
_ON_INTENTS = {"TurnDeviceOn", "HassTurnOn", "HassOpenCover"}
_OFF_INTENTS = {"TurnDeviceOff", "HassTurnOff", "HassCloseCover"}
# 注：这里**没有** ControlWindow/WindowControl 一支。10-10 复核判定它在生产上永不成立
# （那族的 args 不带 entity_id，见 _direction 的说明），保留只会让读代码的人以为"窗也管"。


def _direction(intent: str, args: Any) -> Optional[str]:
    """'on' / 'off' / None（None＝这条不参与归位判断）。

    10-10 对抗复核：原先这里还有一段"`ControlWindow` 按 args['action'] 判开/关"，**已删**。
    理由不是判据写错，而是它**在生产上永不生效**：慧尖窗控车道的 args 是
    action/position/target（fast_path.py:173-178），实体解析在集成侧，**没有 entity_id**
    ⇒ `_eids()` 恒空，那段分支拿不到任何可登记的主体；而当时钉它的用例喂的
    `{"entity_id": …, "action": "open"}` 是生产产不出的形状＝第二例"钉没喂生产形状"的假绿。
    留着读起来像"窗也管"，实际不管——这种"看起来有覆盖面"比明写不管更坏。
    窗/帘要纳入的前提是集成回执带 entity_id（见模块头"下一步"）+ 用户显式扩域。
    """
    if intent in _ON_INTENTS:
        return "on"
    if intent in _OFF_INTENTS:
        return "off"
    return None

# 域 → 收尾服务（官方域服务优先，跨域兜底用 homeassistant.turn_off）
_CLOSE_SERVICE = {
    "cover": ("cover", "close_cover"),
    "light": ("light", "turn_off"),
    "switch": ("switch", "turn_off"),
    "fan": ("fan", "turn_off"),
    "media_player": ("media_player", "media_off"),
    "humidifier": ("humidifier", "turn_off"),
    "vacuum": ("vacuum", "return_home"),
}

DEFAULT_MIN = 10
# 10-10 对抗复核：默认域**只留 light**。原先带上 cover 的理由是"开合类语义上操作完就该闭合"，
# 但这条被我自己的现场证据推翻：`light.ban_gong_shi_she_deng` 所在办公室的那扇平开窗，
# 整夜由一条 YAML 自动化每 ~10 分钟开→7~11 分钟关（见模块头撤回记录）⇒ 10 分钟归位计时
# 与它必然抢关；而"打开客厅的窗帘"的终态语义根本不是"忘关"（遮光/采光是要人留下的状态）。
# ⇒ 窗/帘要纳入请用户在设置里显式扩域，不默认替他决定。
DEFAULT_DOMAINS = ("light",)


def _eids(args: Any) -> list[str]:
    """从步骤 args 里取**已 grounded** 的实体 id 列表；拿不到就返回空（绝不猜）。"""
    if not isinstance(args, dict):
        return []
    raw = args.get("entity_id")
    if isinstance(raw, str):
        return [raw] if "." in raw else []
    if isinstance(raw, Iterable):
        return [e for e in raw if isinstance(e, str) and "." in e]
    return []


def _receipt_eids(receipt: Any) -> list[str]:
    """从**执行回执**里取"HA 说它真动过/真选中"的实体 id。两个来源，都不猜：
      · 直调道 `/api/services/*`：200 body 就是状态真变了的实体列表（10-10 实测）⇒ `entities`
      · 意图道（慧尖 TurnDevice*）：`control_targets` 行自 v1.2.10 起带 `entity_id`
        （intent_turn.py:213-214），`states`/`results` 行同理
    拿不到就返回空。**回执比 args 硬**：args 是"用户点了什么名"，回执才是"动了哪台"。"""
    if not isinstance(receipt, dict):
        return []
    ids: list[str] = []
    for rows in (receipt.get("entities"), receipt.get("control_targets"),
                 receipt.get("states"), receipt.get("results")):
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, str):
                eid = row
            elif isinstance(row, dict):
                eid = str(row.get("entity_id") or "")
            else:
                continue
            if "." in eid and eid not in ids:
                ids.append(eid)
    return ids


class AutoRestore:
    """按实体的归位计时器。永不抛：任何异常都只记日志，不影响语音链主流程。"""

    def __init__(self, ha, settings=None):
        self.ha = ha
        self.settings = settings
        self._tasks: dict[str, asyncio.TimerHandle] = {}
        self._loops: dict[str, asyncio.Task] = {}
        # 登记那一刻算出的绝对终点（monotonic），让 pending() 不依赖事件循环
        self._deadline: dict[str, float] = {}

    # ── 配置面（settings 缺失时走保守默认）───────────────────────────
    def _cfg(self) -> tuple[bool, float, tuple[str, ...], list[str]]:
        get = (lambda k, d: d) if self.settings is None else \
            (lambda k, d: self.settings.get(k, d))
        # 缺键（老存档）＝**未启用**：这条能力是 opt-in，不能因为存档里没写过就默认开。
        enabled = bool(get("dialog.auto_restore", False))
        try:
            minutes = float(get("dialog.auto_restore_min", DEFAULT_MIN) or DEFAULT_MIN)
        except (TypeError, ValueError):
            minutes = float(DEFAULT_MIN)
        minutes = max(0.5, min(minutes, 24 * 60.0))          # 钳位：0＝“不等待”不是本意
        # 显式空列表＝"一个域都不归位"，必须与"没配"区分开（旧写法 `or DEFAULT` 把
        # [] 当没配 ⇒ 用户清空域来停功能停不掉，只能去关总开关）。
        raw_domains = get("dialog.auto_restore_domains", None)
        domains = list(DEFAULT_DOMAINS) if raw_domains is None else raw_domains
        if isinstance(domains, str):
            domains = [d.strip() for d in domains.split(",") if d.strip()]
        exclude = get("dialog.auto_restore_exclude", None) or []
        if isinstance(exclude, str):
            exclude = [d.strip() for d in exclude.split(",") if d.strip()]
        return enabled, minutes * 60.0, tuple(str(d) for d in domains), [str(x) for x in exclude]

    def _wanted(self, eid: str, domains: tuple[str, ...], exclude: list[str]) -> bool:
        dom = eid.split(".", 1)[0]
        if dom not in domains:
            return False
        for pat in exclude:
            if pat and (fnmatch.fnmatch(eid, pat) or pat == eid):
                return False
        return True

    # ── 主入口：执行器在每步结束后调用 ──────────────────────────────
    def note(self, intent: Optional[str], args: Any, ok: bool,
             noop: bool = False, receipt: Any = None) -> None:
        """成功开了一台策略内设备 → 登记归位；成功关闭 → 取消归位。永不抛。

        `noop`（10-10 对抗复核）：执行器**执行前**的证据 `kind == "noop"` ⇒ 目标本来就在
        要求的状态上，这一步**没打开任何东西**。原先这种步也登记（播报都会说「本来就在
        要求的状态上」了，10 分钟后却把用户正在用的灯关掉）——这是"关掉用户没开过的设备"，
        比忘关更坏，所以：noop ⇒ 绝不登记（关闭方向仍照取消处理）。

        `receipt`（10-10 实测后加）：执行回执。**登记对象以回执里的实体为准**，args 只作兜底
        （klar 直调改旧集成时回执可能没带 id）。回执里 `changed=False`（直调道 200 body 是
        `[]`）等价于 noop：谁都没被动过 ⇒ 不登记。这条同时把覆盖面打开到
        `target` 形（"打开办公室的灯"）——集成侧解析实体，回执 `control_targets` 行带
        `entity_id`（v1.2.10 起），加载项不再需要自己猜目标。"""
        try:
            if not ok or not isinstance(intent, str):
                return
            rec_ids = _receipt_eids(receipt)
            eids = list(dict.fromkeys(rec_ids + _eids(args))) if rec_ids else _eids(args)
            if isinstance(receipt, dict) and receipt.get("changed") is False:
                noop = True
            if not eids:
                return
            enabled, delay, domains, exclude = self._cfg()
            direction = _direction(intent, args)          # 生产词表实锤，见 _direction
            if direction == "off":
                for eid in eids:
                    self._cancel(eid)
                return
            if direction != "on" or not enabled:
                return
            if noop:
                logger.info("[归位] %s 本步是 noop（目标已在要求状态/回执无人被改动）"
                            "⇒ 不登记，不动用户的东西", eids)
                return
            for eid in eids:
                if self._wanted(eid, domains, exclude):
                    self._arm(eid, delay)
        except Exception:                                   # noqa: BLE001
            logger.exception("[归位] 登记异常（不影响本轮播报）")

    def _arm(self, eid: str, delay_s: float) -> bool:
        """登记一枚归位计时。返回是否真的安排了——**没有事件循环时绝不谎称安排了**
        （本仓教训：静默不执行比报错更坏，"配了没开"和"开了没接上"都长这样）。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("[归位] 无运行事件循环 ⇒ %s **未登记**（不假装已安排收尾）", eid)
            return False
        old = self._tasks.pop(eid, None)
        self._deadline.pop(eid, None)
        if old is not None:
            old.cancel()
        # 续期语义：重复打开同一台 = 重置计时，不是叠加成两个任务
        self._deadline[eid] = time.monotonic() + delay_s
        self._tasks[eid] = loop.call_later(delay_s, self._spawn, eid, delay_s)
        logger.info("[归位] %s 将在 %.0f 秒后自动关闭（语音打开）", eid, delay_s)
        return True

    def _cancel(self, eid: str) -> None:
        h = self._tasks.pop(eid, None)
        self._deadline.pop(eid, None)
        if h is not None:
            h.cancel()
            logger.info("[归位] %s 已被语音关闭 → 取消自动收尾", eid)
        # 任务已起飞（正在等服务端）时无从撤回，交由 _fire 自查最新状态兜底

    def _spawn(self, eid: str, delay_s: float) -> None:
        self._tasks.pop(eid, None)
        self._deadline.pop(eid, None)
        t = asyncio.get_running_loop().create_task(self._fire(eid))
        self._loops[eid] = t
        t.add_done_callback(lambda _f, k=eid: self._loops.pop(k, None))

    async def _fire(self, eid: str) -> None:
        """到点收尾：**先看当前状态**，已经是关的就不做（防与自动化/人手撞车），
        失败只记日志——归位是"补忘关"，不是"必须成功"的硬闸。"""
        try:
            # 10-10 对抗复核补：登记之后用户把总开关关掉（或把这台豁免掉）时，
            # 已到点的计时**不许**再去关设备——那条策略已经被他收回了。
            # 只在真正要动手的这一刻重读策略，不靠登记时的旧快照。
            enabled, _delay, domains, exclude = self._cfg()
            if not enabled or not self._wanted(eid, domains, exclude):
                logger.info("[归位] %s 到点，但策略已关/该设备已豁免 ⇒ 不动手", eid)
                return
            st = await self.ha.get_state(eid) if hasattr(self.ha, "get_state") else None
            cur = None
            if isinstance(st, dict):
                cur = st.get("state")
            elif st is not None:
                cur = getattr(st, "state", None) or (st if isinstance(st, str) else None)
            if cur is None:
                # 10-10 对抗复核：读不到状态**不 fail-open**。`get_state` 的 None 是真机常见形
                # （实体改名/删除、states 首刷未完成、刷新失败折叠成 None）。原先 `cur is None`
                # 不进"已关"集合、直接发关服务 ⇒ 在**毫无状态证据**的情况下去关设备，
                # 与模块自己写的红线"绝不靠猜去关（关错设备比忘关更糟）"相反。
                logger.warning("[归位] %s 到点但读不到状态（None）⇒ 不动手，宁可不管", eid)
                return
            if str(cur) in ("off", "closed", "idle", "unavailable", "unknown"):
                logger.info("[归位] %s 当前=%s，无需收尾", eid, cur)
                return
            dom = eid.split(".", 1)[0]
            domain, svc = _CLOSE_SERVICE.get(dom, ("homeassistant", "turn_off"))
            res = await self.ha.call_service(domain, svc, {"entity_id": eid})
            ok = bool(res.get("success")) if isinstance(res, dict) else True
            logger.info("[归位] %s 自动收尾 %s.%s → %s", eid, domain, svc,
                        "成功" if ok else "失败:%s" % (res,))
            if not ok:
                # 失败不静默：再等一个周期重试一次，仍失败就放弃并留痕（不无限重试）
                logger.warning("[归位] %s 收尾未成功，不再自动重试（避免与故障设备死循环）", eid)
        except asyncio.CancelledError:
            raise
        except Exception:                                   # noqa: BLE001
            logger.exception("[归位] %s 收尾异常", eid)

    # ── 观测/测试面 ────────────────────────────────────────────────
    def pending(self) -> dict[str, Any]:
        """当前登记待归位的实体与剩余秒数（观测/测试用；不含持久化承诺）。

        10-10 对抗复核：旧写法在 `_tasks` 非空时取 `get_running_loop()` ⇒ **在循环外调用直接抛**
        RuntimeError，而这条函数正是"现场排查"要看的那一个（模块头承诺了"在 pending 快照里可见"）。
        现在剩余时间从登记那一刻存的 `time.monotonic()` 终点算，**任何上下文都能读**。"""
        now = time.monotonic()
        return {eid: round(float(self._deadline.get(eid, float("nan"))) - now, 1)
                for eid in self._tasks}

    def cancel_all(self) -> None:
        for h in self._tasks.values():
            h.cancel()
        self._tasks.clear()
        self._deadline.clear()
        for t in self._loops.values():
            t.cancel()
        self._loops.clear()

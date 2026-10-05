"""工具模块 - 存放通用辅助函数"""
import asyncio
import functools
import inspect
import json
import logging
import os
import time
import uuid
from typing import Dict, Any, Optional, Tuple
from homeassistant.core import HomeAssistant

from .const import (DOMAIN, PROTOCOL_HEAD, TOPIC_GATEWAY_REQ_FORMAT,
                    CONF_GATEWAY_SN, GLOBAL_IGNORED_GATEWAYS)

_LOGGER = logging.getLogger(__name__)


def iter_devices(device_registry) -> list:
    """设备注册表全量条目遍历（v1.7.28，双 HA 形态兼容）。

    新 HA：`devices` 是可直接迭代出 DeviceEntry 的集合（frame 告警原话
    "iterate it to get the device entries"），映射查找法 .values()/.items()/
    .get() 已弃用（2027.9 停摆）；旧 HA：`devices` 仍是 Mapping，直接迭代
    拿到的是 key 字符串。探测首元素类型自动回退，两种形态都拿到条目列表。
    注意：DeviceRegistry 与 EntityRegistry 均无 async_entries()——CI E2E
    两轮实锤，臆造 API 由 tests/test_v1728_registry_api.py 反钉。
    """
    col = device_registry.devices
    items = list(col)
    if items and isinstance(items[0], str):
        # 旧 Mapping 形态：迭代得键，回退 .values()（此路径仅在旧 HA 触发，
        # 旧版 .values() 合法无告警；新 HA 永不走到）
        items = list(col.values())
    return items


def gateway_instance_uuid(hass: HomeAssistant) -> str:
    """服务端实例指纹：uuid5(NAMESPACE_DNS, config_dir)。

    与 mqtt_handler._lifecycle 同式（该处已改为调用本函数）——确定性、跨
    重启/跨 handler 稳定，保证 v1.7.27 起耳朵 001 代答的 uuid 与转正后
    正式 handler 的应答逐字一致，固件无需处理两个指纹。
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, hass.config.config_dir))


def should_ear_ack_001(ctype: Any, data: Any) -> bool:
    """耳朵级代答门（v1.7.26 用户裁定 A）：仅"未配置网关的 001 绑定请求"可代答。

    与 2026-09-02 五条 ack 方向契约同门：data 带 errcode 的 001 是网关对我方
    报文的回复，绝不再答；002/005 等其余上报在耳朵层不代答（绑定请求语义仅 001）。
    """
    return (ctype == "001" and isinstance(data, dict) and "errcode" not in data)


#: 条目「此刻没人应答」的状态集（v1.7.34）：正式 handler 未订阅 ⇒ 网关 001
#: 无人应答，固件每 5s 重发永不停血。not_loaded=从未加载/已卸载；
#: setup_error=setup 抛异常终态失败（等人工重试）；setup_retry=ConfigEntryNotReady
#: 退避重试中。三者都不持有 MQTT 订阅。
#: 反之 loaded / setup_in_progress / unload_in_progress / migration_in_progress
#: 都是「在飞或将答」的瞬态——按 configured 静默，避免与正式 handler 双答
#: （v1.7.30 仲裁只收编耳朵之间的重复，管不住耳朵 vs 正式 handler）。
ENTRY_STATES_UNANSWERED = frozenset({"not_loaded", "setup_error", "setup_retry"})


def entry_state_name(entry: Any) -> str:
    """条目状态归一为小写裸名（"loaded"/"not_loaded"/…）。

    三种形态同源：StrEnum（HA 2024.4+，``str()`` 即值）、``str, Enum`` 混血
    （旧 core，``str()`` 得 "ConfigEntryState.LOADED" 但 ``.value`` 是裸名）、
    REST 字典里的裸字符串（发现代理形态）。取 ``.value`` 再兜底字符串化，
    与 config_flow._entry_state 同式（该处为本仓既有先例）。
    状态缺失/不可识别 ⇒ 返回 ""，调用方按「未知＝不打断既有语义」处理。
    """
    st = getattr(entry, "state", None)
    if st is None:
        return ""
    raw = getattr(st, "value", st)
    if not isinstance(raw, str):
        return ""
    return raw.rsplit(".", 1)[-1].strip().lower()


def entry_state_for_sn(hass: HomeAssistant, gateway_sn: Any) -> str:
    """「已配置」判定四态门（v1.7.31 A-3 三态 → v1.7.34 补加载态）。

    真源核验（HA 2026.1.3 源码 inspect）：``async_entries()`` 默认
    ``include_disabled=True``——禁用条目照样返回。旧三处"已配置"门
    （心跳监听器/_protocol 他网关分支/discovery）默认调用 ⇒ 用户**禁用**的
    网关上电后 001 既不代答也不被发现、零留痕，固件每 5s 重发永不停血。
    BUG-5（mqtt_bootstrap.py:204）已对 MQTT 条目定过同款语义："禁用条目不算
    有效配置"——本函数把该口径收为单一真源（自过滤 disabled_by，不依赖
    跨版本参数旗标，manifest 2024.12 下限安全）。

    v1.7.34：三态门的第二个盲区——命中条目**未加载**（setup_error/setup_retry/
    not_loaded）时旧实现照样返回 "configured"，两只耳朵一起静默让位一个根本
    不存在的 handler：网关 001 无人应答、日志零留痕，比禁用态更难归因（用户
    眼里条目就在列表里）。加载态一并纳入判定。

    返回：
    - "configured"：存在未禁用且**在飞/已加载**的条目命中该 SN → 耳朵静默让位
                    正式 handler（状态未知同样按此处理，不打断既有语义）
    - "disabled" ：仅禁用条目命中 → 代答止血但**不弹发现卡**（尊重禁用决策），
                    由调用方打节流 WARNING 留痕
    - "not_loaded"：命中条目全部处于 ``ENTRY_STATES_UNANSWERED`` → 代答止血+
                    节流 WARNING 指向 setup 失败根因；发现卡不弹（条目已在列表
                    里，弹卡是打扰且 async_discover_gateway 第 3 步本就早退）
    - "none"     ：未配置 → 正常代答+发现链
    """
    disabled_hit = False
    unloaded_hit = False
    try:
        entries = hass.config_entries.async_entries(DOMAIN)
    except Exception as e:  # noqa: BLE001 — 判定面炸穿按 none（不阻断止血主语义）
        # v1.7.33（全量审计）：兜底不得无声——误判 none 会对已配置/已禁用网关
        # 重复弹发现卡，静默断链无从归因。节流留痕（不阻断止血主语义）。
        log_throttled(hass, "_entry_state_err_logged", "entries", 600.0,
                      _LOGGER.warning, "条目状态判定读取失败，按 none 处理: %s", e)
        return "none"
    target = str(gateway_sn or "").lower()
    for e in entries:
        sn = (getattr(e, "data", None) or {}).get(CONF_GATEWAY_SN) or ""
        if str(sn).lower() != target:
            continue
        if getattr(e, "disabled_by", None):
            disabled_hit = True
        elif entry_state_name(e) in ENTRY_STATES_UNANSWERED:
            unloaded_hit = True
        else:
            return "configured"
    if disabled_hit:
        return "disabled"
    return "not_loaded" if unloaded_hit else "none"


def log_throttled(hass: HomeAssistant, bucket: str, key: str, ttl: float,
                  log_fn, msg: str, *args, **kwargs) -> None:
    """按 key 节流的日志闸（v1.7.31 A-3/A-4）。

    TTL 窗内同 key 只放行一条——5s 上报风暴下"必响的留痕"不得刷屏（0917
    取证铁律要求 WARNING+ 可见与不刷屏的平衡点，同 _ear_promotion_watched
    去重窗哲学）。dict 过期即清，容量随活跃 key 数有界。

    v1.7.33：宿主 data 面不可用（测试替身/极端形态）时退化为直接打日志，
    绝不因为"留痕工具本身"把主流程炸掉——留痕是旁路，不是前置条件。
    """
    try:
        runtime = hass.data.setdefault(DOMAIN, {})
        seen = runtime.setdefault(bucket, {})
    except Exception:  # noqa: BLE001
        log_fn(msg, *args, **kwargs)
        return
    now = time.monotonic()
    for stale in [k for k, ts in seen.items() if now - ts > ttl]:
        seen.pop(stale, None)
    if key in seen:
        return
    seen[key] = now
    log_fn(msg, *args, **kwargs)


async def async_ack_gateway_001(hass: HomeAssistant, gateway_sn: str,
                                 msg_id: Any) -> bool:
    """向未绑定网关的 001 绑定请求代答同型 ack（发 gateway/{sn}/req）。

    背景（用户 2026-09-17 裁定 A）：固件首配期每 5s 重发 001，直到收到应答；
    旧链条里应答只在"条目转正→reload→正式 handler 订阅"之后才发出，转正链
    任一环节断裂即成无限重试风暴。耳朵（心跳监听器/_protocol 他网关分支）
    听到未配置网关的 001 时当场代答，风暴即停。

    格式（v1.7.27 用户现场定稿，与正式 handler 应答完全同形）：
    {"head":"$SH","ctype":"001","id":<回带>,"sn":<网关SN>,
    "data":{"errcode":0,"uuid":<实例指纹>}}——uuid 由 gateway_instance_uuid
    确定性计算，与转正后正式 handler 发的逐字一致（固件要求应答必带 uuid，
    2026-09-17 用户实锤格式）。
    返回发布是否成功（失败由调用方留痕，不抛出——代答永不反噬发现主流程）。
    """
    from homeassistant.components import mqtt
    payload = {
        "head": PROTOCOL_HEAD,
        "ctype": "001",
        "id": msg_id,
        "sn": gateway_sn,
        "data": {"errcode": 0, "uuid": gateway_instance_uuid(hass)},
    }
    try:
        await mqtt.async_publish(
            hass,
            TOPIC_GATEWAY_REQ_FORMAT.format(gateway_sn=gateway_sn),
            json.dumps(payload),
            1,
            False,
        )
        return True
    except Exception as e:  # noqa: BLE001
        _LOGGER.warning("耳朵代答 001 发布失败（不阻塞发现）: %s", e)
        return False


# ==================== v1.7.30 ②③：代答单点仲裁 + 转正留痕看守 ====================
#
# 台架实锤（2026-09-17 真栈 A/B 双臂）：两处耳朵对同一条 001 各自
# 独立代答——每个已配置条目的 _protocol 他网关分支都是应答者，外加每条等待
# 条目的心跳监听器——实测 1 请求 → 2~3 条同 uuid 应答（倍数=应答者数）。
# 报文无害但属噪声，且"固件对重复 ack 幂等"只是文档主张、未真机实证。
# 正解：以 (sn, id) 为键的进程内认领仲裁——第一个耳朵发布，其余抑制；
# 固件重试换新 id 仍可得一次应答（止血语义不变），重发同 id 超 TTL 后补答
#（丢包保险）。TTL 30s 远大于并发应答者间 ~40ms 的实测散布，收紧到
# 重复风暴消失即可。

EAR_ACK_CLAIM_TTL = 30.0
EAR_ACK_CLAIM_MAX = 256
# 入站报文尺寸上限（协议合法帧远小于此：最大 002 全量设备列表也仅数 KB）
EAR_INBOUND_MAX_BYTES = 64 * 1024
EAR_PROMOTION_WATCH_SECONDS = 30.0
EAR_PROMOTION_WATCH_LOG_TTL = 600.0


def ear_ack_release(hass: HomeAssistant, gateway_sn: str, msg_id: Any) -> None:
    """撤销一枚代答认领（v1.7.33 全量审计）。

    发布失败时必须撤销：否则"该答而没答成"的 (sn,id) 会在 TTL 内对所有耳朵
    静默抑制——固件 5s 重发的同 id 请求最长 30s 拿不到代答（6 次），且顺带
    跳过转正看守，现场只见"网关不出现"。撤销后下一次重发即可重新认领发布。
    """
    runtime = hass.data.get(DOMAIN) or {}
    claims = runtime.get("_ear_ack_claims") or {}
    claims.pop((str(gateway_sn).lower(), str(msg_id)), None)


def inbound_payload_ok(msg) -> bool:
    """入站 MQTT 报文尺寸闸（v1.6.19 A-LOW7 定案，v1.7.33 上收双耳共用）。

    旧实现只在 `_protocol` 耳的闭包里做，等待态条目的心跳耳（干净主机首配
    形态下**唯一**的应答者）直接 json.loads 全量 payload——mosquitto 默认
    不限 message_size_limit，LAN 上任一可连 2022 的客户端 publish 一条
    50-100MB 的 rpt_rsp 就在事件循环线程卡死整个 HA。
    payload 非常规类型时放行（交给下游既有归一/兜底流程，不在闸上抛）。
    """
    try:
        return len(msg.payload) <= EAR_INBOUND_MAX_BYTES
    except Exception:  # noqa: BLE001
        return True


def ear_ack_claim(hass: HomeAssistant, gateway_sn: str, msg_id: Any) -> bool:
    """代答认领（v1.7.30 ②）：同一 (SN, id) 在 TTL 内只放行一个应答者。

    返回 True=本调用获得发布权；False=另一耳朵已答/在答，必须抑制。
    HA 单事件循环内全部调用点在循环线程执行（同步回调/已派发协程），
    无需加锁；容量闸防畸形流量下 dict 无界增长。
    """
    runtime = hass.data.setdefault(DOMAIN, {})
    claims = runtime.setdefault("_ear_ack_claims", {})
    now = time.monotonic()
    for stale in [k for k, ts in claims.items() if now - ts > EAR_ACK_CLAIM_TTL]:
        claims.pop(stale, None)
    key = (str(gateway_sn).lower(), str(msg_id))
    if key in claims:
        return False
    claims[key] = now
    if len(claims) > EAR_ACK_CLAIM_MAX:
        claims.pop(min(claims, key=claims.get), None)
    return True


def _watch_ear_promotion(hass: HomeAssistant, gateway_sn: str) -> None:
    """代答成功后的转正看守（v1.7.30 ③）：30s 未转正且无待确认卡片 → WARNING。

    旧症状"固件停发 001 但设备列表永不出现"只有 INFO 留痕——现场 WARNING+
    级日志采集根本看不见（0917 取证铁律：归因行必须 WARNING），故障被代答
    的"成功表象"抹掉。看守区分两种停发：
    - discovery 卡片已挂起等用户确认（第二台网关的正常形态）→ 不打扰；
    - 代答后既无条目也无卡片（发现/转正链静默断）→ loud 告警指排障方向。
    每 SN 10 分钟窗口只起一个看守、最多留一条痕（防每 5s 重试刷屏）。
    """
    runtime = hass.data.setdefault(DOMAIN, {})
    watched = runtime.setdefault("_ear_promotion_watched", {})
    now = time.monotonic()
    for stale in [k for k, ts in watched.items() if now - ts > EAR_PROMOTION_WATCH_LOG_TTL]:
        watched.pop(stale, None)
    key = str(gateway_sn).lower()
    if key in watched:
        return
    watched[key] = now

    async def _check():
        from .const import CONF_GATEWAY_SN
        try:
            await asyncio.sleep(EAR_PROMOTION_WATCH_SECONDS)
            if getattr(hass, "is_stopping", False):
                return
            for entry in hass.config_entries.async_entries(DOMAIN):
                if str((getattr(entry, "data", None) or {}).get(
                        CONF_GATEWAY_SN, "")).lower() == key:
                    return  # 已转正——正常，静默退场
            try:
                for flow in hass.config_entries.flow.async_progress():
                    ctx = flow.get("context") or {}
                    if (flow.get("handler") == DOMAIN
                            and str(ctx.get("unique_id") or "").lower() == key):
                        return  # 发现卡片挂起待确认——正常形态，不误报
            except Exception:  # noqa: BLE001 — flow 查询失败按"无卡片"从严处理
                pass
            # v1.7.31（A-5 复写实锤后修）：用户已忽略＝第三种正常终态——
            # ignore 会 abort flow（卡片不再可见），被忽略网关每个上电周期
            # 照常发 001、照常获代答，30s 后必然打出"请检查发现卡"的误导
            # 告警。与"卡片挂起"同权静默退场（GLOBAL_IGNORED_GATEWAYS 与
            # discovery dict 同一 set 对象，persist 承载）。
            ignored = runtime.get(GLOBAL_IGNORED_GATEWAYS) or set()
            if key in {str(g).lower() for g in ignored}:
                return  # 用户已忽略——正常终态，不打扰
            _LOGGER.warning(
                "网关 %s 获耳朵代答 001 后 %ds 未转为配置条目、且无待确认发现卡片"
                "——绑定停在发现/转正链（检查 设置→设备与服务 的发现卡与「HA MQTT "
                "通道」就绪态），本留痕每网关 10 分钟去重",
                gateway_sn, int(EAR_PROMOTION_WATCH_SECONDS),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 看守绝不反噬代答主流程
            _LOGGER.debug("网关 %s 转正看守异常（忽略）", gateway_sn, exc_info=True)

    tasks = runtime.setdefault("_ear_watch_tasks", set())
    task = hass.async_create_task(_check(), name=f"{DOMAIN}_ear_promotion_watch_{key}")
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def async_ear_ack_001_arbitrated(hass: HomeAssistant, gateway_sn: str,
                                       msg_id: Any) -> bool:
    """两处耳朵的统一代答入口（v1.7.30）：认领 → 发布 → 转正看守。

    返回 True 仅当本调用实际发布了代答；被仲裁抑制与发布失败都返回 False。
    调用方不得再直接 await async_ack_gateway_001（守卫反钉），否则 N+1
    重复应答面复活。
    """
    if not ear_ack_claim(hass, gateway_sn, msg_id):
        _LOGGER.debug("001 代答被仲裁抑制（另一耳朵已答）: sn=%s id=%s",
                      gateway_sn, msg_id)
        return False
    ok = await async_ack_gateway_001(hass, gateway_sn, msg_id)
    if ok:
        _watch_ear_promotion(hass, gateway_sn)
    else:
        # v1.7.33（全量审计）：发布失败必须撤销认领——否则固件 5s 重发的同
        # id 请求在 TTL 内被全体耳朵抑制，止血目标反而被放大成 6 次不应答。
        ear_ack_release(hass, gateway_sn, msg_id)
    return ok


def is_mqtt_loaded(hass: HomeAssistant) -> bool:
    """MQTT 集成是否已加载。

    兼容新旧 HA：mqtt 集成 setup 完成会写入 ``hass.data["mqtt"]``，
    这是长期稳定的契约（官方推荐的可用性判断方式之一）。
    集中于此判断，未来 HA 若调整存储方式只需改此处。
    """
    return hass.data.get("mqtt") is not None


def is_mqtt_connected(hass: HomeAssistant) -> bool:
    """MQTT broker 是否已连接（官方 API，带兼容回退）。

    ``homeassistant.components.mqtt.async_connected(hass)`` 是 2023.5+ 官方
    辅助函数；旧版不存在时回退为"集成已加载即视为可用"。
    """
    try:
        from homeassistant.components.mqtt import async_connected
        return bool(async_connected(hass))
    except (ImportError, AttributeError):
        return is_mqtt_loaded(hass)


#: 实例指纹落盘文件名（写着跨容器共享的 HA 配置目录里）。
#: 消费者是加载项容器内的 gateway_discovery_proxy（HA MQTT 失聪时的兜底应答），
#: 它必须用与正式 handler/耳朵**逐字一致**的 uuid——文件是唯一跨容器通道。
INSTANCE_UUID_FILENAME = "window_controller_gateway_instance.json"


def _write_instance_uuid_file_sync(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


async def async_write_instance_uuid_file(hass: HomeAssistant) -> None:
    """把实例指纹写入 HA 配置目录（容器侧兜底应答读取，v1.7.60）。

    背景：HA 的 MQTT 客户端一旦没连在内置 broker 上（条目被别的 broker 抢走/
    禁用/凭据失配），全部"耳朵"都收不到网关上报——容器内的发现代理是唯一还能
    看到 broker 真值的组件，但它的 001 兜底应答必须带 HA 的实例指纹（固件按
    uuid 认服务端；两个指纹会让固件把兜底应答当另一台服务器）。指纹公式仍在
    utils.gateway_instance_uuid 单一真源，本函数只负责把它落盘。

    失败只降级（容器侧不代答），绝不反噬 setup——性能与安全都不依赖本文件
    （uuid 会随 001 应答发给网关，非机密）。
    """
    try:
        path = hass.config.path(INSTANCE_UUID_FILENAME)
    except Exception:  # noqa: BLE001 — 无 config 面（测试替身）时静默降级
        return
    payload = {"uuid": gateway_instance_uuid(hass)}
    executor = getattr(hass, "async_add_executor_job", None)
    try:
        if callable(executor):
            await executor(_write_instance_uuid_file_sync, path, payload)
        else:
            _write_instance_uuid_file_sync(path, payload)
    except Exception as e:  # noqa: BLE001 — 落盘失败不反噬 setup
        _LOGGER.debug("实例指纹落盘失败（容器侧兜底应答将降级为不代答）: %s", e)


async def async_wait_mqtt_loaded(
    hass: HomeAssistant, timeout: float = 10.0, interval: float = 0.5
) -> bool:
    """等待 MQTT 集成 setup 完成（hass.data["mqtt"] 出现），返回是否就绪。

    v1.6.13（客户现场 mqtt_not_available 误诊根治）：ensure_mqtt_connection 的
    "创建/更新 MQTT 条目"路径以提交动作为终点，而 MQTT 集成真正 setup 完成
    （``hass.data["mqtt"]`` 写入）是异步的——config flow 在 ensure 返回后立即
    同步检查 is_mqtt_loaded，会把"刚创建正在连接"的正常时序误判成失败。

    为何不用官方 async_wait_for_mqtt_client：它等待的是"客户端实际连上
    broker"（内部 30 秒超时）。本门禁的唯一判据是"下游
    mqtt.async_subscribe 是否会因 wrapper 缺失而炸"，即 hass.data 条目
    存在性——broker 永久不可达时应快速失败给出可读错误，而不是让
    表单卡 30 秒。轮询目标与 is_mqtt_loaded 保持同一谓词，
    上游改存储结构时仍只需改一处。
    """
    if is_mqtt_loaded(hass):
        return True
    waited = 0.0
    while waited < timeout:
        await asyncio.sleep(interval)
        waited += interval
        if is_mqtt_loaded(hass):
            return True
    return False


def get_via_device_id(device) -> Optional[str]:
    """读取设备的父设备 id（v1.6.12 第五轮审计，跨版本兼容）。

    DeviceEntry 上**从未存在** ``via_device`` 属性——``via_device=(DOMAIN, sn)``
    只是 ``async_get_or_create`` 的入参形式；读取端属性名是 ``via_device_id``，
    其值分两代：
    - 新版 HA：str（父设备 id），上游已列入移除遗留别名计划
    - 旧版 HA：tuple ``(config_entry_id, device_id)`` → 取 device_id
    本库此前多处 ``getattr(device, "via_device", ...)`` 恒落 None，
    网关子设备清单/迁移/删除清理整段死分支（教训与 v1.6.0 "entity"
    字面量同族：假 mock 带真机没有的属性骗过全部测试）。
    """
    via = getattr(device, "via_device_id", None)
    if isinstance(via, tuple):
        return via[1] if len(via) > 1 else None
    return via


def resolve_via_device_id(device_registry, gateway_sn) -> Optional[str]:
    """写入端解析：网关 (DOMAIN, sn) → 其 DeviceEntry.id（v1.7.31 现场实锤 F-B）。

    ``async_get_or_create`` 的 ``via_device=(DOMAIN, sn)`` 入参已被现网 HA
    2026.9 实锤弃用（每次启动一条 WARNING，明示 2027.8.0 停摆——v1.7.28
    迁移批清了 devices/entities 直读面，漏了这个**写参数面**，守卫扫描
    pattern 未覆盖所致）。新写法 ``via_device_id=<父设备注册表 id>``。

    网关设备尚未注册时返回 None 并省略该参数——HA 旧实现对 via_device 父
    标识不命中时同样不设置 service 归属，语义逐字一致，子设备注册主链
    不受影响。与 ``get_via_device_id``（读取端）配对构成本库宿主归属的
    单一真源。调用点不直接用本函数出参，须经 ``via_device_kwargs``。
    """
    try:
        gateway_device = device_registry.async_get_device(
            identifiers={(DOMAIN, gateway_sn)})
    except Exception:  # noqa: BLE001 — 只读面兜底，宁缺归属不断注册
        return None
    return gateway_device.id if gateway_device else None


@functools.lru_cache(maxsize=None)
def _supports_via_device_id(async_get_or_create) -> bool:
    """探测注册表写方法是否已有 via_device_id 形参（源码签名核验，非猜测）。

    台架真 HA 2026.1.3 实证该参数**不存在**（签名仅 via_device）；现场真机
    2026.9.2 实证存在且旧参弃用告警。manifest 声明支持 2024.12.0 起全区间，
    故形态选择必须按运行时签名判定，禁止无条件用新参（旧 HA 会
    TypeError 直接打死设备注册，比弃用告警严重一级）。bound method 的
    hash/eq 按 (self, func)，同 registry 实例探测结果天然缓存。
    """
    try:
        return "via_device_id" in inspect.signature(
            async_get_or_create).parameters
    except (TypeError, ValueError):
        return False


def via_device_kwargs(device_registry, gateway_sn) -> Dict[str, Any]:
    """宿主归属入参双形态出口（v1.7.31，与 call_registry_method 同族兼容哲学）。

    - 新 HA（≥2026.x 引入 via_device_id 者）：{"via_device_id": <父设备 id>}
    - 旧 HA（签名仅 via_device）：{"via_device": (DOMAIN, sn)} 原形态保留
    全库 async_get_or_create 调用点一律 ``**via_device_kwargs(...)`` 展开，
    禁止直接写 ``via_device=`` 关键字实参（AST 守卫反钉）。
    """
    if _supports_via_device_id(device_registry.async_get_or_create):
        # v1.7.33（全量审计）：父网关未注册时**省略**该参数而非显式传 None。
        # registry 契约里 None=清空归属、缺省(Undefined)=不改——旧实现传 None，
        # 首启顺序竞态（网关条目尚未落地）下会把已存在子设备改挂关系抹掉
        # （设备详情页层级丢失，async_remove_entry 的 get_via_device_id 反查失效）。
        resolved = resolve_via_device_id(device_registry, gateway_sn)
        return {"via_device_id": resolved} if resolved else {}
    return {"via_device": (DOMAIN, gateway_sn)}


def get_device_config_entry_ids(device) -> set:
    """读取设备关联的配置条目 id 集合（跨版本兼容，同 api.py 的双读法）。

    新版 HA 是 ``config_entries``（set），旧版是 ``config_entry_id``（str）。
    ``config_entry_ids`` 这个属性名不存在——v1.6.12 修正 __init__.py 的
    恒空读取（共享保护死分支）。
    """
    ids = set()
    ce = getattr(device, "config_entries", None)
    if ce:
        ids.update(ce)
    ce_id = getattr(device, "config_entry_id", None)
    if ce_id:
        ids.add(ce_id)
    return ids


def get_entity_registry(hass: HomeAssistant):
    """获取实体注册表

    Args:
        hass: Home Assistant实例

    Returns:
        EntityRegistry: 实体注册表
    """
    from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
    return async_get_entity_registry(hass)


async def async_get_entity_id(
    hass: HomeAssistant, domain: str, unique_id: str
) -> Optional[str]:
    """按 unique_id 查找实体的 entity_id（兼容新旧 HA）。

    HA 真实签名：``EntityRegistry.async_get_entity_id(domain, platform, unique_id)``
    - domain:   实体域（button/cover/number/sensor…，即 entity_id 前缀）
    - platform: 集成域名（本集成 DOMAIN = window_controller_gateway）

    背景（2026-08-28 实测）：HA registry 异步化重构期间，不同版本中
    该方法可能是 async（返回 coroutine，await 后为 RegistryEntry）或
    sync（直接返回 str）。本函数统一处理，返回 entity_id 字符串；不存在返回 None。

    v1.6.3 修复：v1.6.0 重构兼容层时曾把第一个实参误写为字面量 "entity"
    并丢弃调用方传入的实体域，导致索引键 ("entity", DOMAIN, uid) 永不命中、
    全集成 unique_id 反查恒返回 None（重命名别名/按钮清理/删除按钮自删等
    13 处调用点静默失效）。参数亦由 platform 更名为 domain 防再犯。
    """
    entity_registry = get_entity_registry(hass)
    try:
        result = await call_registry_method(
            entity_registry.async_get_entity_id, domain, DOMAIN, unique_id
        )
    except TypeError as e:
        # 签名不兼容（极老版本），放弃查找（v1.5.9 原有兜底，v1.6.3 恢复）。
        # v1.6.4：兜底不得无声——registry 内部真 TypeError 也会被吞成
        # "实体不存在"，与 v1.6.0 "entity" 字面量回归同构的静默失效面，
        # 必须留可观测痕迹（manifest 已钉 2024.12 下限，触发即异常事件）
        _LOGGER.warning(
            "async_get_entity_id(%s, %s) 抛出 TypeError，降级为未找到: %s",
            domain, unique_id, e,
        )
        return None
    if result is None:
        return None
    # 新版返回 RegistryEntry，旧版返回 str
    if hasattr(result, "entity_id"):
        return result.entity_id
    return str(result)


async def call_registry_method(method, *args, **kwargs):
    """调用 registry 方法并兼容同步/异步两种实现（HA registry 异步化过渡期）。

    背景（2026-08-28 实测）：Home Assistant 对 EntityRegistry/DeviceRegistry
    的异步化重构尚未完成，同一方法在不同版本中可能是：
    - 同步方法（``@callback def ...``）：直接执行并返回结果（新版 master 如此，
      如 async_remove 返回 None、async_update_entity 返回 RegistryEntry）
    - 异步方法（``async def ...``）：返回 coroutine（部分过渡版本如此）

    本函数统一处理：调用后若返回值是 coroutine 则 await，否则原样返回。
    避免 ``await`` 同步方法（返回 None/RegistryEntry）导致
    "'NoneType' object can't be awaited" / "'RegistryEntry' object can't be awaited"。

    收口约定（v1.6.3）：所有 registry **写操作**（async_get_or_create / async_remove /
    async_remove_device / async_update_device / async_update_entity /
    async_get_entity_id 等）一律经本函数调用，不允许直调；纯**只读查询**
    （device_registry.async_get、async_get_device、entity_registry.async_get 等）
    在所有已知版本中均为同步 @callback，可直调，无需经过本函数。
    """
    result = method(*args, **kwargs)
    if hasattr(result, "__await__"):
        return await result
    return result


def clear_entity_registry_cache(hass=None):
    """清理实体注册表缓存（兼容接口，实际不再需要缓存管理）"""
    pass


def _resolve_domain_identifier(hass: Any, device_id: str) -> Optional[str]:
    """将 HA 设备注册表 ID（UUID）解析为集成标识符值（网关SN/设备SN）

    服务的 device_id 参数可能来自设备详情页复制的 HA 设备注册表 UUID，
    此函数通过注册表按设备ID直接查找，返回匹配设备的 (DOMAIN, sn) 标识符值。
    找不到或解析失败时返回 None。
    """
    try:
        from homeassistant.helpers.device_registry import async_get as async_get_device_registry
        device_registry = async_get_device_registry(hass)
        entry = device_registry.async_get(device_id)
        if entry:
            for identifier in entry.identifiers:
                if identifier[0] == DOMAIN:
                    return identifier[1]
    except Exception as e:
        _LOGGER.debug("解析设备注册表ID失败（可忽略）: %s", e)
    return None


def find_gateway_by_device_id(hass: Any, device_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """根据设备ID查找对应的网关
    
    Args:
        hass: Home Assistant实例
        device_id: 设备ID，包含网关SN、设备SN，或 HA 设备注册表ID（UUID）
        
    Returns:
        Tuple[Optional[Dict[str, Any]], Optional[str]]: (网关数据, 网关SN) 如果找到，否则 (None, None)
    """
    if DOMAIN not in hass.data or not hass.data[DOMAIN]:
        _LOGGER.error("服务调用失败：集成尚未完成初始化或没有已配置的网关。")
        return None, None

    for _entry_id, data in hass.data[DOMAIN].items():
        if isinstance(data, dict):
            gateway_sn = data.get("gateway_sn", "")
            if gateway_sn and gateway_sn in device_id.split("_"):
                return data, gateway_sn
            
            # 检查是否包含设备SN
            device_manager = data.get("device_manager")
            if device_manager:
                devices = device_manager.get_all_devices()
                id_parts = device_id.split("_")
                for device in devices:
                    device_sn = device.get("sn", "")
                    # v1.7.33：空串防护——device_id="" 时 id_parts=[""]，
                    # 缺 sn 的设备条目会因 "" in [""] 命中而把命令派发到错误网关
                    if device_sn and device_sn in id_parts:
                        return data, gateway_sn
    
    # 兜底：device_id 可能是 HA 设备注册表ID（UUID）
    gateway_sn = _resolve_domain_identifier(hass, device_id)
    if gateway_sn:
        for _entry_id, data in hass.data[DOMAIN].items():
            if isinstance(data, dict) and data.get("gateway_sn", "").lower() == gateway_sn.lower():
                return data, gateway_sn
        # v1.7.12（第 6 轮审计 E-10）：用户从**子设备**详情页复制"设备 ID"
        # 调服务时，_resolve 解出的是子设备 SN——旧版按"等于某网关 SN"匹配
        # 永不命中，误报"未找到对应网关"。经设备→网关映射反查补最后一跳。
        from .const import DEVICE_TO_GATEWAY_MAPPING
        mapping = hass.data[DOMAIN].get(DEVICE_TO_GATEWAY_MAPPING) or {}
        mapped = mapping.get(gateway_sn)
        if mapped is None:
            for k, v in mapping.items():
                if str(k).lower() == str(gateway_sn).lower():
                    mapped = v
                    break
        if mapped:
            for _entry_id, data in hass.data[DOMAIN].items():
                if (isinstance(data, dict)
                        and str(data.get("gateway_sn", "")).lower()
                        == str(mapped).lower()):
                    return data, data.get("gateway_sn")

    return None, None


def find_device_by_device_id(hass: Any, device_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[str]]:
    """根据设备ID查找对应的设备和网关
    
    Args:
        hass: Home Assistant实例
        device_id: 设备ID，包含设备SN，或 HA 设备注册表ID（UUID）
        
    Returns:
        Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[str]]: (设备数据, 网关数据, 网关SN) 如果找到，否则 (None, None, None)
    """
    if DOMAIN not in hass.data or not hass.data[DOMAIN]:
        _LOGGER.error("服务调用失败：集成尚未完成初始化或没有已配置的网关。")
        return None, None, None

    for _entry_id, data in hass.data[DOMAIN].items():
        if isinstance(data, dict):
            device_manager = data.get("device_manager")
            if device_manager:
                devices = device_manager.get_all_devices()
                id_parts = device_id.split("_")
                for device in devices:
                    device_sn = device.get("sn", "")
                    if device_sn and device_sn in id_parts:   # v1.7.33：空串防护
                        return device, data, data.get("gateway_sn", "")

    # 兜底：device_id 可能是 HA 设备注册表ID（UUID）
    device_sn = _resolve_domain_identifier(hass, device_id)
    if device_sn:
        for _entry_id, data in hass.data[DOMAIN].items():
            if isinstance(data, dict):
                device_manager = data.get("device_manager")
                if device_manager:
                    device = device_manager.get_device(device_sn)
                    if device:
                        return device, data, data.get("gateway_sn", "")

    return None, None, None


def get_device_gateway_mapping(hass: HomeAssistant, device_sn: str) -> Optional[str]:
    """获取设备关联的网关SN
    
    Args:
        hass: Home Assistant实例
        device_sn: 设备SN
    
    Returns:
        Optional[str]: 网关SN，如果未找到返回None
    """
    try:
        from .const import DEVICE_TO_GATEWAY_MAPPING
        if DOMAIN in hass.data and DEVICE_TO_GATEWAY_MAPPING in hass.data[DOMAIN]:
            device_to_gateway_mapping = hass.data[DOMAIN][DEVICE_TO_GATEWAY_MAPPING]
            if device_sn in device_to_gateway_mapping:
                return device_to_gateway_mapping[device_sn]
    except Exception as e:
        _LOGGER.error("获取设备网关映射失败: %s", e)
    return None

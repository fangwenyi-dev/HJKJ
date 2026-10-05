"""慧尖一体化插件 — HA 与内置 Mosquitto 的 MQTT 连接自动引导。

背景：插件的 run.sh 启动时会把内置 Mosquitto 的连接信息写入 HA 配置目录下的
``window_controller_gateway_mqtt_bootstrap.json``。本模块在集成侧消费该标记：

1. HA 中已存在（启用态的）MQTT 配置条目：
   - 数据与内置 Broker 完全一致 → 视为引导完成，删除标记；
   - 不一致 → **强制接管**（v1.6.x 定案）：改写条目 data 指向内置 Broker 并
     reload（source=hassio 条目则删除重建，接管前发持久化通知留痕）。一体化
     产品必须保证网关可被 HA 听到；外接 Broker 共存走"官方 Broker 加载项 +
     慧尖内置账户直连/桥接"路径。
   v1.7.18 口径：禁用条目不算有效配置——不代劳启用/删除，告警并保留标记。
2. 不存在（启用态）MQTT 条目且标记存在 → 通过程序化 config flow 自动创建指向
   内置 Mosquitto 的配置条目，成功后删除标记。

独立安装（HACS）场景下不存在标记文件，所有函数立即返回，行为与旧版完全一致。

为什么不用 REST API（历史方案的失败原因）：HA Core 的 REST API 从未提供"创建
配置条目"的端点——``/api/config/config_entries/entry`` 仅支持 GET 列表，
``/api/config/config_entries/entry/{entry_id}`` 仅支持 DELETE。插件容器无论用
何种有效 token 调 POST 都不可能成功。而本模块运行在 HA Core 进程内部，直接调用
稳定的 Python API（config flow），无认证、无代理、无版本兼容问题。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import InvalidData
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

BOOTSTRAP_FILENAME = "window_controller_gateway_mqtt_bootstrap.json"

#: 常驻端点文件（**无凭据**）：run.sh 每次启动写入。引导标记是一次性的
#: （落地即删），本文件给集成提供"常驻核验"的依据——只要它存在，就说明
#: 这台机器应该存在一条指向内置 broker 的 MQTT 连接，可据此巡检不变量。
ENDPOINT_FILENAME = "window_controller_gateway_mqtt_endpoint.json"

#: 内置 broker 定值（mosquitto.conf listener 2022 / run.sh 写标记同源）。
BUILTIN_BROKER = "127.0.0.1"
BUILTIN_PORT = 2022


def _usable_mqtt_entries(entries) -> list:
    """「有效 MQTT 配置」= 未禁用 **且** 非 source=ignore（v1.7.61，10-05 现场）。

    HA 里 `source=ignore` 的条目是用户在发现卡上点过"忽略"的记录，**永不加载**。
    旧实现只滤 `disabled_by` ⇒ 把这种条目当已配置：走"匹配/接管"分支后删掉引导
    标记自称成功，而真正的 MQTT 条目永不创建 ⇒ `is_mqtt_loaded` 恒假 ⇒ 心跳耳
    无限干等、首台网关的自动添加不成链（现场 .184 实锤：mqtt not_loaded +
    source=ignore + devices 空 + "MQTT 集成仍未就绪"）。与 v1.7.18 BUG-5
    "禁用条目不算有效配置" 同口径扩大。
    """
    out = []
    for e in entries:
        if getattr(e, "disabled_by", None):
            continue
        if str(getattr(e, "source", "") or "") == "ignore":
            continue
        out.append(e)
    return out


def _ignored_mqtt_entries(entries) -> list:
    return [e for e in entries
            if not getattr(e, "disabled_by", None)
            and str(getattr(e, "source", "") or "") == "ignore"]

#: mDNS 广播状态文件（容器侧 mdns_publisher.py 写，无凭据）：同局域网存在
#: 第二台慧尖加载项时名字（huijian.local）被先注册者独占，本机广播永久失败
#: ——集成侧据此出提示卡，别让"网关永远只连另一台 HA"成为无迹之谜。
MDNS_STATUS_FILENAME = "window_controller_gateway_mdns_status.json"
MDNS_ISSUE_ID = "mdns_name_conflict"

# 模块级锁：多个 entry / 配置流并发触发时只创建一次 MQTT 条目。
# 与 persist.py 相同的模式：HA 单事件循环内安全；asyncio.Lock 延迟绑定事件循环。
_create_lock: Optional[asyncio.Lock] = None


def _get_lock() -> asyncio.Lock:
    """惰性创建模块级锁（避免在导入期绑定事件循环）。"""
    global _create_lock
    if _create_lock is None:
        _create_lock = asyncio.Lock()
    return _create_lock


def _read_marker(path: str) -> Optional[Dict[str, Any]]:
    """读取引导标记文件；不存在或不可解析时返回 None。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as err:
        _LOGGER.warning("MQTT 引导标记文件无法读取（忽略）: %s", err)
        return None
    return data if isinstance(data, dict) else None


def _remove_marker(path: str) -> None:
    """删除引导标记文件（内含凭据，用完即删）；失败仅记录不抛出。"""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as err:
        _LOGGER.warning("删除 MQTT 引导标记文件失败: %s", err)


def _marker_exists(path: str) -> bool:
    return os.path.isfile(path)


async def has_bootstrap_marker(hass: HomeAssistant) -> bool:
    """引导标记是否仍存在（True = 自动配置意图尚未确认完成）。

    v1.6.13：config flow 错误码分流使用——标记存在说明加载项已声明
    "要用内置 broker 自动配置 MQTT"，此时 MQTT 未就绪的根因通常是
    内置 broker 未启动/凭据被拒，应给 broker_not_ready 而非误导性的
    "请先启用 MQTT 集成"。检查失败按 False 处理（保守：宁可少一类等待，
    不可让门禁异常打断添加流程）。
    """
    try:
        return await hass.async_add_executor_job(
            _marker_exists, hass.config.path(BOOTSTRAP_FILENAME)
        )
    except Exception:  # noqa: BLE001 — 探针失败不改变主判定
        _LOGGER.debug("检查 MQTT 引导标记失败（按不存在处理）", exc_info=True)
        return False


def _read_json_sync(path: str) -> Optional[Dict[str, Any]]:
    """通用 JSON 文件读取；缺失/损坏 → None。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_endpoint_sync(path: str) -> Optional[Dict[str, Any]]:
    """读常驻端点文件；缺失/损坏/无 broker 字段 → None（无判定依据）。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("broker"):
        return None
    try:
        port = int(data.get("port") or BUILTIN_PORT)
    except (TypeError, ValueError, OverflowError):
        # v1.7.61 对抗复核 S3：非有限数（1e999→inf）的 int() 抛 OverflowError，
        # 旧实现让整份端点文件被判"读不到"（外层兜底吞成 None）⇒ 通道核验静默
        # 关闭（no_endpoint）。按非法端口回落，保留核验能力。
        port = BUILTIN_PORT
    return {"broker": str(data["broker"]), "port": port}


async def _probe_endpoint(hass: HomeAssistant) -> tuple:
    """端点文件三态探针（v1.7.63 C-7）：("no_endpoint", None) 缺失 /
    ("endpoint_broken", None) 存在但读不出或无 broker 字段 / ("ok", data) 可用。

    旧实现把"缺失/损坏"都并成 None ⇒ 卡片误清 + 核验静默关闭（文件被截断时
    通道其实可能仍是坏的）。状态与数据一次读出，避免双读竞态。
    """
    try:
        path = hass.config.path(ENDPOINT_FILENAME)
    except Exception:  # noqa: BLE001 — 无 config 面（测试替身）
        return "no_endpoint", None
    executor = getattr(hass, "async_add_executor_job", None)

    def _sync():
        if not os.path.isfile(path):
            return "no_endpoint", None
        data = _read_endpoint_sync(path)
        if data is None:
            return "endpoint_broken", None
        return "ok", data

    try:
        if callable(executor):
            return await executor(_sync)
        return _sync()
    except Exception:  # noqa: BLE001 — v1.7.63（对抗复核 F5）：探针本体逃逸＝
        # 无结论（probe_error ⇒ 走 guard 不清卡）；折成 no_endpoint 会被 healer
        # 当"通过"清卡并永久关核验——与 C-7"判定面坏不误清"自相矛盾。
        return "probe_error", None


async def async_read_endpoint(hass: HomeAssistant) -> Optional[Dict[str, Any]]:
    try:
        path = hass.config.path(ENDPOINT_FILENAME)
    except Exception:  # noqa: BLE001 — 无 config 面（测试替身）：按无判定依据
        return None
    executor = getattr(hass, "async_add_executor_job", None)
    try:
        if callable(executor):
            return await executor(_read_endpoint_sync, path)
        return _read_endpoint_sync(path)
    except Exception:  # noqa: BLE001 — 只读探针失败不误报
        return None


async def async_read_mdns_status(hass: HomeAssistant) -> Optional[Dict[str, Any]]:
    """读容器侧 mDNS 状态文件（缺失/损坏 → None，不误报）。"""
    try:
        path = hass.config.path(MDNS_STATUS_FILENAME)
    except Exception:  # noqa: BLE001
        return None
    executor = getattr(hass, "async_add_executor_job", None)
    try:
        if callable(executor):
            data = await executor(_read_json_sync, path)
        else:
            data = _read_json_sync(path)
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _clear_mdns_issue(hass: HomeAssistant) -> None:
    try:
        from homeassistant.helpers import issue_registry as ir
        ir.async_delete_issue(hass, DOMAIN, MDNS_ISSUE_ID)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("清除 mDNS 撞名提示卡失败（卡片可能滞留）: %s", err)


async def _mdns_guard(hass: HomeAssistant) -> None:
    """mDNS 撞名提示卡（v1.7.60，用户裁定的 B 方案：不改名、只响亮）。

    同局域网两台慧尖加载项抢 `huijian.local`，先注册者独占——**走 mDNS 自动
    发现的网关只会连上那一台**，本机永远收不到网关（现场 .184/.91 双实例实测：
    huijian.local → .192.168.1.91，服务实例 server 也是 .91）。撞名是永久失败，
    容器侧已退避；这里把"为什么本机没有网关"升为 HA 里可见的提示卡。
    """
    status = await async_read_mdns_status(hass)
    try:
        from homeassistant.helpers import issue_registry as ir
        if status and status.get("state") == "name_conflict":
            ir.async_create_issue(
                hass, DOMAIN, MDNS_ISSUE_ID,
                is_fixable=False,
                severity="warning",
                translation_key=MDNS_ISSUE_ID,
                translation_placeholders={
                    "owner": str(status.get("owner") or "未知"),
                    "local_ip": str(status.get("local_ip") or "未知"),
                },
            )
        else:
            _clear_mdns_issue(hass)
    except Exception as err:  # noqa: BLE001 — 可见性面失败不影响自愈主流程
        _LOGGER.warning("mDNS 撞名提示卡更新失败（丢可见性）: %s", err)


async def verify_builtin_channel(hass: HomeAssistant) -> str:
    """只读核验「内置通道」不变量：HA 的 MQTT 客户端此刻真连在内置 broker 上吗。

    v1.7.60 根因修复件。旧实现的自愈**完全依赖一次性引导标记**：标记首次
    落地即被删除（_remove_marker），此后 healer 见不到标记直接退出——
    MQTT 条目被官方 Mosquitto/EMQX 抢走、被用户改向、被 Supervisor 覆盖、
    或密码失配时，慧尖永不复查、永不告警、永不自愈。现场形态：网关仍在
    上报（broker 侧可见），HA 侧零订阅者 ⇒ 001 无人应答（固件每 5s 重发
    不止血）+ 发现卡片永不出现，且日志全绿。本函数给"通道健康"一个
    可周期核验的真源。

    返回（全部为只读判定）：
    - ``no_endpoint``    ：无端点文件（HACS 独立安装/关掉自动配置）→ 无判定依据，
      按"不打断既有语义"处理（不报警）
    - ``endpoint_broken``：端点文件**存在但读不出/无 broker 字段**（v1.7.63 C-7：
      旧实现与"缺失"混为一谈 ⇒ 卡被误清且核验关闭）。按"判定面坏了"处理：不清卡
    - ``probe_error``    ：条目表读取抛错（v1.7.63 C-7：旧实现归 no_endpoint 会
      把"探针坏了"当"通过"）。保守：不清卡
    - ``ok``             ：存在启用条目、地址匹配、客户端已连接
    - ``no_entry``       ：端点文件在，但没有任何启用中的 MQTT 条目
    - ``ignored_only``   ：只剩 source=ignore（永不加载）的条目
    - ``mismatch``       ：条目在，但指向的 broker:port 不是内置端点
    - ``disconnected``   ：条目地址正确，但客户端尚未连上（broker 未起/凭据被拒）
    """
    state, endpoint = await _probe_endpoint(hass)
    if state != "ok":
        return state   # missing → no_endpoint；broken → endpoint_broken
    exp_broker, exp_port = endpoint["broker"], int(endpoint["port"])
    try:
        raw_entries = hass.config_entries.async_entries("mqtt")
    except Exception as e:  # noqa: BLE001 — 判定面不可读：不是"通过"，是"没探到"
        from .utils import log_throttled
        log_throttled(hass, "_channel_probe_err", "entries", 600.0, _LOGGER.warning,
                      "MQTT 条目表读取失败，通道核验本轮无结论（不误清卡）: %s", e)
        return "probe_error"
    entries = _usable_mqtt_entries(raw_entries)
    if not entries:
        # v1.7.61：只有 source=ignore 条目——永不加载，给专门判词（卡片归因准确）
        return "ignored_only" if _ignored_mqtt_entries(raw_entries) else "no_entry"
    first = entries[0]
    try:
        cur_broker = str((first.data or {}).get("broker") or "")
        cur_port = int((first.data or {}).get("port") or 0)
    except (TypeError, ValueError, OverflowError):
        # v1.7.61 对抗复核 S2：条目 data 的 port 为 1e999→inf 时 int() 抛
        # OverflowError，而 healer 无 try 调用本函数 ⇒ 巡检任务静默死亡。
        cur_broker, cur_port = "", 0
    if cur_broker != exp_broker or cur_port != exp_port:
        return "mismatch"
    from .utils import is_mqtt_connected
    if not is_mqtt_connected(hass):
        return "disconnected"
    return "ok"


async def _wait_for_mqtt_client(hass: HomeAssistant) -> bool:
    """等待 MQTT 客户端连接就绪，最多 30 秒。

    注意：async_wait_for_mqtt_client（2023.5+）不抛超时异常而是返回 False，
    必须检查布尔返回值；任何异常一律视为未就绪。
    """
    from homeassistant.components import mqtt

    wait_fn = getattr(mqtt, "async_wait_for_mqtt_client", None)
    if wait_fn is not None:
        try:
            return bool(await asyncio.wait_for(wait_fn(hass), timeout=30))
        except asyncio.TimeoutError:
            return False
        except Exception:  # noqa: BLE001 — mqtt 集成尚未就绪时辅助函数可能抛错
            _LOGGER.debug("async_wait_for_mqtt_client 异常，视为未就绪", exc_info=True)
            return False
    # 兜底：极老版本无该辅助函数时轮询 hass.data（mqtt 集成 setup 完成即写入）
    from .utils import is_mqtt_loaded
    for _ in range(30):
        if is_mqtt_loaded(hass):
            return True
        await asyncio.sleep(1)
    return is_mqtt_loaded(hass)


async def _quietly_abort_flow(hass: HomeAssistant, flow_id: str) -> None:
    """中止残留的进行中流程，避免每次重试都堆积一个悬挂 flow。"""
    try:
        await hass.config_entries.flow.async_abort(flow_id)
    except Exception:  # noqa: BLE001 — 流程可能已自行结束
        _LOGGER.debug("中止 MQTT 配置流程 %s 失败（可能已结束）", flow_id)


async def _update_mqtt_entry(
    hass: HomeAssistant, entry: Any, broker: str, port: int,
    username: Optional[str], password: Optional[str],
) -> None:
    """更新已有 MQTT 配置条目的 broker 连接信息。

    一体化插件场景：用户安装插件后，可能已有其他 broker 的 MQTT 条目
    （如官方 Mosquitto 插件、EMQX 等）。本函数将该条目更新为指向
    插件内置 broker，确保 HA 能接收到 LoRa 网关的数据。

    v1.7.33（全量审计）：除覆写连接四键外，显式清除**与明文内置 broker
    不兼容**的键。旧实现只覆写 broker/port/username/password，用户自建
    条目里的 `certificate`/`tls_insecure`/`transport=websockets`/`ws_*`
    被原样带进明文 2022——HA MQTT 客户端按 TLS/WebSocket 去连明文 TCP，
    永久连不上；而此处标记已删、healer 下轮走"地址匹配分支"直接 return，
    没有任何出口再纠偏（现场表现：全部门窗离线且不随重启自愈）。
    """
    _INCOMPATIBLE_KEYS = ("certificate", "tls_insecure", "transport",
                          "ws_path", "ws_headers")
    new_data = dict(entry.data)
    dropped = [k for k in _INCOMPATIBLE_KEYS if k in new_data]
    for k in dropped:
        new_data.pop(k, None)
    new_data["broker"] = broker
    new_data["port"] = port
    if username is not None:
        new_data["username"] = username
    if password is not None:
        new_data["password"] = password
    hass.config_entries.async_update_entry(entry, data=new_data)
    if dropped:
        _LOGGER.warning(
            "接管 MQTT 条目时清除了与内置明文 Broker 不兼容的键 %s"
            "（若原有 TLS/WebSocket 配置仍被其他集成依赖，请另行保留独立条目）",
            sorted(dropped),
        )
    _LOGGER.info(
        "已将 MQTT 配置条目 %s 更新为内置 Broker %s:%s",
        entry.entry_id, broker, port,
    )


async def ensure_mqtt_connection(hass: HomeAssistant) -> Optional[bool]:
    """确保 HA 的 MQTT 集成已连接到内置 Broker（需要时自动创建/更新配置条目）。

    - 标记不存在（HACS 独立安装等场景）→ 立即返回，不做任何事。
    - 已有 MQTT 条目但 broker 地址不一致 → 自动更新为内置 Broker 地址。
    - 已有 MQTT 条目且地址一致 → 保持现状，删除标记。
    - 无条目且有标记 → 程序化运行 mqtt config flow 创建条目并等待连接。

    返回值契约（v1.6.13 审计#3：消除调用方重复等待）：
    - ``True``  已消耗过最长 30s 的连接等待且客户端就绪；
    - ``False`` 已消耗过最长 30s 的连接等待仍未就绪——调用方**不应**再
      自行宽限轮询（同一时段内不可能凭空就绪，只会白等）；
    - ``None``  本次未做连接等待（无标记 / 条目已匹配 / 无需动作），
      就绪与否由调用方自行判断。

    抛出 :class:`ConfigEntryNotReady` 表示暂时无法完成（典型为内置 Broker 尚未
    就绪），调用方应让 setup 流程稍后自动重试。
    """
    marker_path = hass.config.path(BOOTSTRAP_FILENAME)
    data = await hass.async_add_executor_job(_read_marker, marker_path)

    # 无标记：非一体化安装或插件未启用 auto_setup_ha_mqtt —— 保持旧行为
    if data is None:
        return

    broker = str(data.get("broker") or "").strip()
    try:
        # v1.6.3：内置 Broker 固定监听 2022（见 mosquitto.conf/run.sh），
        # 旧回退值 1883 指向根本不监听的端口，属死配置
        port = int(data.get("port") or 2022)
    except (TypeError, ValueError, OverflowError):
        # v1.7.61 对抗复核 S4：标记被手改出 1e999 同型缺口——溢出逃出 ensure
        # 会被上层吞成"连不上"（假归因）。按默认口回落。
        port = 2022
    username = data.get("username") or None
    password = data.get("password") or None

    # v1.7.12（第 6 轮审计 B-14）：空 broker 标记属损坏标记——原先只有 create
    # 路径有守卫，update/匹配分支可把 broker="" 写进用户已有 MQTT 条目毁掉其
    # 连接。在统一入口处熔断：不删标记（保留现场）、不动条目、大声告警。
    if not broker:
        _LOGGER.error(
            "bootstrap 标记缺少 broker 字段（%s），已跳过 MQTT 自愈——"
            "请重启慧尖加载项重写标记", marker_path,
        )
        return

    # v1.7.18（第 7 轮审计 BUG-5）：async_entries 会返回**禁用条目**——
    # 旧实现取 [0] 命中禁用死条目时：改写+reload 对 disabled 不生效（setup
    # 被跳过）却照删标记 → 自愈凭据蒸发、真正启用的条目永不纠偏；"仅有
    # 禁用条目"还会被 create 分支双检误判成已配置 → 永久 broker_not_ready
    # 且根因不可见。统一口径：仅启用条目算有效 MQTT 配置；全禁用则 loud
    # 告警并保留标记（禁用是用户决策，插件不代劳启用/删除）。
    all_entries = hass.config_entries.async_entries("mqtt")
    existing_entries = _usable_mqtt_entries(all_entries)
    if all_entries and not existing_entries:
        disabled = [e for e in all_entries if getattr(e, "disabled_by", None)]
        ignored = _ignored_mqtt_entries(all_entries)
        if disabled:
            _LOGGER.warning(
                "MQTT 配置条目全部处于禁用状态（%d 个）——慧尖不代为启用/删除，"
                "请在 设置→设备与服务→MQTT 重新启用或删除该条目后重启慧尖加载项；"
                "引导标记已保留待自动重试",
                len(disabled),
            )
            return False
        if ignored:
            # v1.7.61 复核修订（HA 2024.12 源码 config_entries.py:1285-1289 实证）：
            # 单实例闸对 **SOURCE_USER 流不统计 ignore 条目**（闸只看
            # include_ignore=False 那一支，第二支带 `source != SOURCE_USER`）⇒
            # "只有被忽略条目"时建条**本可成功**。早退（初版修法）等于把唯一的
            # 自愈出口关掉——此处只 loud 点名根因，然后**继续走创建路径**。
            _LOGGER.warning(
                "HA 里只有被忽略的 MQTT 条目（source=ignore，永不加载，%d 条）——"
                "它不挡 user 流，慧尖继续创建新条目；若创建被单实例闸拦下（旧版 "
                "HA 或并存禁用条目），请先删除那条被忽略的 MQTT 条目再重启加载项",
                len(ignored),
            )

    if existing_entries:
        first = existing_entries[0]
        cur_broker = first.data.get("broker")
        cur_port = first.data.get("port")
        cur_username = first.data.get("username")

        # Bug5 修复：仅当 broker/port/username 全部一致才视为已配置完成。
        # 旧版插件把 ${USERNAME}（huijian）写入 bootstrap 标记，HA 集成用它连接；
        # 新版分离出 ha_mqtt 用户（ACL 全权限）。升级后旧条目 username 与标记
        # 不一致，必须走更新分支把用户名/密码刷成 ha_mqtt，否则 HA 集成继续用
        # 被收紧 ACL 的 huijian 连接，MQTT discovery 收不到消息。
        # v1.7.12（第 6 轮审计 B-2）：补 password 比对——旧版只比前三项，加载项
        # 密码变更后匹配分支照样删标记，HA MQTT 条目永持旧密码 → 30s not
        # authorised 风暴且无自愈路径（HA2 现场"条目旧凭据"事故的机器成因）。
        # 条目侧为 !secret/!env_var 模板值时豁免比较：比对明文必然失配，且覆写
        # 会破坏用户模板/强制 reload 成环——视为用户自管，不抢方向盘。
        entry_password = str(first.data.get("password") or "")
        password_user_managed = entry_password.startswith("!")
        if (
            cur_broker == broker
            and str(cur_port) == str(port)
            and cur_username == username
            and (password_user_managed or entry_password == (password or ""))
        ):
            _LOGGER.debug(
                "MQTT 配置条目已指向内置 Broker %s:%s 且用户一致，无需更新",
                broker, port,
            )
            await hass.async_add_executor_job(_remove_marker, marker_path)
            return

        # broker 地址不一致，需要更新
        _LOGGER.warning(
            "MQTT 集成已连接到 %s:%s，但本插件内置 Broker 为 %s:%s；"
            "正在自动更新 MQTT 配置条目以使用内置 Broker。",
            cur_broker, cur_port, broker, port,
        )

        # source=hassio 条目由 Supervisor 管理，async_update_entry 的数据
        # 会在 reload 时被 Supervisor 覆盖回原 broker 地址。
        # 禁用（disabled_by）也不行——MQTT config flow 的 _async_current_entries()
        # 仍会返回 disabled 条目，触发 single_instance_allowed 中止。
        # 唯一方案：删除 hassio 条目 → 走下方 create_new_entry 路径。
        if getattr(first, "source", None) == "hassio":
            _LOGGER.warning(
                "MQTT 条目 %s 由 Supervisor 管理 (source=hassio)，"
                "删除后创建新的 USER 源条目指向内置 Broker。",
                first.entry_id,
            )
            # 破坏性操作提示：删除用户已有的 MQTT 配置条目前，
            # 通过持久化通知明确告知用户，避免静默切断其现有 MQTT 连接
            # （如官方 Mosquitto 插件 / EMQX / 其他依赖该条目的集成）。
            try:
                await hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "慧尖插件正在接管 MQTT 配置",
                        "message": (
                            "检测到已有 MQTT 配置条目（由 Supervisor 管理，"
                            f"broker: {cur_broker}:{cur_port}）。\n\n"
                            "慧尖一体化插件将删除该条目并创建指向内置 Broker "
                            f"({broker}:{port}) 的新条目，以确保 LoRa 网关数据可达。\n\n"
                            "若您有其他设备/集成依赖原 MQTT broker，请知悉："
                            "它们的连接将被切换到慧尖内置 broker。"
                        ),
                        "notification_id": "huijian_mqtt_takeover",
                    },
                    blocking=False,
                )
            except Exception as err:  # noqa: BLE001 — 通知失败不影响主流程
                _LOGGER.debug("发送 MQTT 接管通知失败（可忽略）: %s", err)
            try:
                await hass.config_entries.async_remove(first.entry_id)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "删除 hassio MQTT 条目失败: %s，降级为直接更新条目数据", err,
                )
                # 降级方案：虽然 Supervisor 可能会在 reload 时覆盖回原 broker，
                # 但至少在当前会话中让 MQTT 客户端连上内置 broker。
                # v1.6.13（审计#1a）：条目数据此刻已被改写为内置 broker——
                # "引导配置已落地"这一标记职责完成，无条件删标记；
                # 连不上属 MQTT 集成自身的重连职责（HA 内置退避重试），
                # 保留标记反而会与"匹配分支即删"的真实行为自相矛盾，
                # 并在 Supervisor 持续覆盖的极端场景下形成 reload 循环。
                # 返回值如实上报"等过一轮没成就"，调用方不必重复等待。
                await _update_mqtt_entry(hass, first, broker, port, username, password)
                await hass.config_entries.async_reload(first.entry_id)
                _LOGGER.info("等待 MQTT 客户端重连到内置 Broker（降级模式）...")
                await hass.async_add_executor_job(_remove_marker, marker_path)
                if not await _wait_for_mqtt_client(hass):
                    _LOGGER.warning("MQTT 客户端重连超时（降级模式），交由 MQTT 集成自动重试")
                    return False
                return True
            # 不删标记——让后续 create_new_entry 路径接管
        else:
            # 非 hassio 条目（用户手动创建等）：直接更新数据
            # v1.6.13（审计#1a）：同上——更新落地即删标记，连接归 MQTT 集成重试。
            await _update_mqtt_entry(hass, first, broker, port, username, password)
            await hass.config_entries.async_reload(first.entry_id)
            await hass.async_add_executor_job(_remove_marker, marker_path)
            _LOGGER.info("等待 MQTT 客户端重连到内置 Broker...")
            if not await _wait_for_mqtt_client(hass):
                _LOGGER.warning("MQTT 客户端重连超时，交由 MQTT 集成自动重试")
                return False
            return True

    async with _get_lock():
        # 双重检查：等待锁期间可能已被并发触发的流程创建。
        # hassio 条目已在上方被删除（async_remove），此处只检查是否还有其他活跃条目。
        # v1.7.18（BUG-5）：等锁期间条目可能被禁用——双检同样只认启用条目；
        # 出现"全禁用"竞态时保留标记并告警（与入口熔断同口径）。
        locked_all = hass.config_entries.async_entries("mqtt")
        locked_enabled = _usable_mqtt_entries(locked_all)
        locked_disabled = [e for e in locked_all if getattr(e, "disabled_by", None)]
        if locked_all and not locked_enabled and locked_disabled:
            # 只有**禁用**条目才算"真的挡住建条"（HA 单实例闸计禁用、不计 ignore）
            _LOGGER.warning(
                "等待 MQTT 引导锁期间条目被禁用，保留引导标记；"
                "请在 设置→设备与服务→MQTT 重新启用或删除条目后重启慧尖加载项"
            )
            return False
        if locked_enabled:
            await hass.async_add_executor_job(_remove_marker, marker_path)
            return

        from homeassistant.config_entries import SOURCE_USER
        from homeassistant.data_entry_flow import FlowResultType

        _LOGGER.info(
            "检测到插件引导标记，正在自动创建 MQTT 配置条目 (%s:%s)", broker, port
        )
        result = await hass.config_entries.flow.async_init(
            "mqtt", context={"source": SOURCE_USER}
        )
        flow_id = result["flow_id"]

        # 2024.9+ 在 Supervisor 环境（HAOS/Supervised，即本插件的运行环境）下，
        # mqtt 的 user 步骤返回菜单（addon/broker）而非表单；必须显式导航到
        # "broker" 子步骤。Core 容器安装则直接返回 broker 表单。
        if result.get("type") == FlowResultType.MENU:
            result = await hass.config_entries.flow.async_configure(
                flow_id, user_input={"next_step_id": "broker"}
            )

        result_type = result.get("type")
        if result_type != FlowResultType.FORM:
            await _quietly_abort_flow(hass, flow_id)
            raise ConfigEntryNotReady(
                f"MQTT 配置流程未能进入表单步骤（type={result_type}）"
            )

        user_input: Dict[str, Any] = {"broker": broker, "port": port}
        if username:
            user_input["username"] = username
        if password:
            user_input["password"] = password

        # 自适应兼容新旧 HA 的 mqtt broker 表单 schema：
        # - 旧版（<2026.8）：不认识 other_settings 键，按不含该键提交即可；
        # - 2026.8.0-dev：broker 校验器直接索引 user_input[OTHER_SETTINGS]，
        #   缺失抛 KeyError；
        # - 2026.8 正式版：schema 改为 vol.Required(OTHER_SETTINGS)，缺失由
        #   data_entry_flow 包装成 InvalidData（v1.6.14 真机 E2E 实锤：客户
        #   HA≥2026.8 首添在健康 broker 下也必然 InvalidData→旧版误报
        #   mqtt_not_available）。两种异常都触发同一"补字段重试"。
        OTHER_SETTINGS = {
            "set_ca_cert": "off",
            "set_client_cert": False,
            "transport": "tcp",
        }
        try:
            try:
                result = await hass.config_entries.flow.async_configure(
                    flow_id, user_input=user_input
                )
            except (KeyError, InvalidData):
                # 新版 HA 要求 other_settings 段：补字段后重试一次
                if "other_settings" not in user_input:
                    user_input["other_settings"] = OTHER_SETTINGS
                    result = await hass.config_entries.flow.async_configure(
                        flow_id, user_input=user_input
                    )
                else:
                    raise
        except Exception as err:  # noqa: BLE001 — 校验器内部异常不能炸掉 setup
            await _quietly_abort_flow(hass, flow_id)
            _LOGGER.warning("提交 MQTT 配置流程失败: %s", err)
            raise ConfigEntryNotReady("MQTT 配置流程提交失败，稍后重试") from err

        result_type = result.get("type")

        if result_type == FlowResultType.CREATE_ENTRY:
            _LOGGER.info("已自动创建 MQTT 配置条目，等待客户端连接…")
            if not await _wait_for_mqtt_client(hass):
                # v1.6.13（客户现场"装完立刻添加网关必报 mqtt_not_available"
                # 根修）：条目已创建但客户端 30s 内没连上（内置 Broker 仍在
                # 启动）→ **保留标记**：条目若因 HA 版本差异未真正落地，
                # 下次 ensure 仍能重建；就绪语义 = 客户端真连上才算引导完成。
                # （审计#1 复核：本路径保留有独立价值，区别于更新/降级路径
                # ——后两者条目数据已落地且必然匹配，保留标记无消费出口。）
                _LOGGER.warning("MQTT 客户端 30 秒内未完成连接，保留引导标记待下次重试")
                return False
            await hass.async_add_executor_job(_remove_marker, marker_path)
            return True

        if result_type == FlowResultType.ABORT:
            reason = result.get("reason")
            if reason == "single_instance_allowed":
                # 已有 MQTT 条目（或 HA 核心层单实例拦截）：按已存在处理。
                # v1.7.18（BUG-5 同口径）：拦截也可能全来自**禁用条目**——
                # v1.7.61：或全来自**被忽略条目**（source=ignore 永不加载）。
                # 死条目永不 setup，"已存在"是假象，不得删标记掩盖根因。
                _cur = hass.config_entries.async_entries("mqtt")
                if _cur and not _usable_mqtt_entries(_cur):
                    _LOGGER.warning(
                        "MQTT 单实例拦截来自禁用/被忽略（source=ignore）条目，"
                        "保留引导标记；请在 设置→设备与服务→MQTT 重新启用或删除"
                        "该条目（含被忽略的那条）后重启慧尖加载项"
                    )
                    return False
                _LOGGER.info("MQTT 配置流程中止（%s），视为已有配置", reason)
                await hass.async_add_executor_job(_remove_marker, marker_path)
                return
            # 其他中止原因：保守处理——保留标记待下次重试
            _LOGGER.warning(
                "MQTT 配置流程意外中止（reason=%s），保留引导标记待下次重试",
                reason,
            )
            raise ConfigEntryNotReady(f"MQTT 配置流程中止（{reason}），稍后重试")

        # 表单校验失败（典型为 cannot_connect：内置 Broker 尚未就绪）
        errors = result.get("errors") or {}
        _LOGGER.warning(
            "自动创建 MQTT 条目未成功（errors=%s），保留引导标记待下次重试",
            errors,
        )
        await _quietly_abort_flow(hass, flow_id)
        raise ConfigEntryNotReady(
            f"自动连接 MQTT 失败（{errors.get('base') or '未知原因'}），稍后自动重试"
        )


# ==================== v1.7.29 A+B：持久自愈 + 可见修复入口 ====================

BOOTSTRAP_RETRY_INTERVAL = 300.0
# v1.7.30 ④：指数退避上限。台架实锤（双臂对照）v1.7.29 的 healer 每 300s
# 恒频重跑 ensure——该形态只到"警告+保留标记"，但同一节拍在"条目与标记不
# 匹配 / source=hassio"形态会驱动删建 Supervisor 托管 MQTT 条目并发接管
# 通知。恒频把这一破坏面的重试节奏写死在 5 分钟；改为 300s×2^(n-1) 指数
# 退避、封顶 1h——引导未落地仍永久低频巡查（自愈目的不弃），但不再对
# 用户条目高频施加 takeover 压力，首次触顶一次性 WARNING 收口症状。
BOOTSTRAP_RETRY_MAX_INTERVAL = 3600.0
TAKEOVER_ISSUE_ID = "mqtt_bootstrap_pending"
#: 常驻通道核验（v1.7.60）：引导落地后 MQTT 条目仍可能被改走/失效——
#: 独立修复条目 id 与巡检节拍（低频巡查，不再对用户条目高频施压）。
CHANNEL_ISSUE_ID = "mqtt_channel_broken"
CHANNEL_VERIFY_INTERVAL = 1800.0


def _retry_delay(rounds: int) -> float:
    """第 rounds 轮（1 起）失败后到下一轮的间隔：指数退避、封顶。"""
    return min(BOOTSTRAP_RETRY_INTERVAL * (2 ** (rounds - 1)),
               BOOTSTRAP_RETRY_MAX_INTERVAL)


HEALER_SLEEP_CHUNK = 30.0


async def _interruptible_sleep(hass: HomeAssistant, delay: float) -> bool:
    """切片长睡（v1.7.31 A-1）：每片结束复检停机/条目出口。

    返回 False=应即刻退出 healer。真源实证：HA async_stop 各阶段以
    async_timeout 包裹 block_till_done——单发 3600s 睡不会挂死关机，但每停
    一次必烧光一条全局超时预算（同阶段其他在途任务被连带提前取消 + 一条
    超时 WARNING）。切片后最大退出延迟=CHUNK，退避节奏总量分毫不差。
    """
    remaining = delay
    while remaining > 0:
        step = min(HEALER_SLEEP_CHUNK, remaining)
        await asyncio.sleep(step)
        remaining -= step
        if getattr(hass, "is_stopping", False) or \
                _enabled_huijian_entry_count(hass) == 0:
            return False
    return True


def _report_takeover_issue(hass: HomeAssistant) -> None:
    """B：把"引导未完成"升为 HA 修复条目（设置→系统→问题），带一键重试。

    静默失败 → 可见可修；fix flow 由 **repairs.py**（async_create_fix_flow →
    GatewayRepairFlow）承接——v1.7.63 订正：HA 的修复流不启动集成自己的
    config flow，旧入口 config_flow.async_step_repair 从未被调用过。
    issue registry 不可用（异常/老版本）只丢可见性，不影响自愈主循环。
    """
    try:
        from homeassistant.helpers import issue_registry as ir
        ir.async_create_issue(
            hass, DOMAIN, TAKEOVER_ISSUE_ID,
            # v1.7.31（C-1 真签名实锤）：真实形参是 is_fixable——2026.1.3
            # inspect 双臂验证 unexpected keyword 'is_fix_flow' + 官方
            # repairs 文档同口径。旧臆造名使本函数**每次都 TypeError 被下面
            # except 吞成 DEBUG**：修复条目自 v1.7.29 起从未出过卡，
            # 修复入口整面不可达（v1.7.63 另证实：即使卡片出了，HA 也只
            # 走 ConfirmRepairFlow——入口须在 repairs.py，见该文件头注释），
            # healer 告警文案把用户指向一个不存在的入口。签名复制品守卫见
            # tests/conftest.py:issue_registry + test_v1731。
            is_fixable=True,
            severity="warning",
            translation_key=TAKEOVER_ISSUE_ID,
        )
    except Exception as err:  # noqa: BLE001
        # v1.7.31（C-1）：吞异常降为 DEBUG 是"永不出卡"躲过全部测试的帮凶
        # ——registry 可用性问题必须可见。
        _LOGGER.warning("创建 MQTT 引导修复条目失败（丢可见性，不影响自愈）: %s", err)


def _clear_takeover_issue(hass: HomeAssistant) -> None:
    try:
        from homeassistant.helpers import issue_registry as ir
        ir.async_delete_issue(hass, DOMAIN, TAKEOVER_ISSUE_ID)
    except Exception as err:  # noqa: BLE001
        # v1.7.31（C-1 同族）：清除失败=修复卡片滞留不消失，必须可见
        _LOGGER.warning("清除 MQTT 引导修复条目失败（卡片可能滞留）: %s", err)


def _report_channel_issue(hass: HomeAssistant, verdict: str) -> None:
    """通道不变量被破坏 → HA 修复条目（可一键重试），不自动改写用户条目。

    v1.7.60 定线：**只报障 + 一键修，不主动接管**。条目可能承载用户/其他
    加载项的连接（官方 Mosquitto/EMQX），静默改写是破坏性动作（v1.7.30 已
    就"接管破坏面"定过案）；本修复只负责让故障可见可修——修复流走
    repairs.py：提交即重跑一轮 ensure + 通道核验（真动作；v1.7.63 前
    卡上的"点提交重试"是空操作——HA 走 ConfirmRepairFlow 只删卡）。
    """
    try:
        from homeassistant.helpers import issue_registry as ir
        ir.async_create_issue(
            hass, DOMAIN, CHANNEL_ISSUE_ID,
            is_fixable=True,
            severity="warning",
            translation_key=CHANNEL_ISSUE_ID,
            translation_placeholders={"verdict": verdict},
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("创建 MQTT 通道修复条目失败（丢可见性，不影响自愈）: %s", err)


def _clear_channel_issue(hass: HomeAssistant) -> None:
    try:
        from homeassistant.helpers import issue_registry as ir
        ir.async_delete_issue(hass, DOMAIN, CHANNEL_ISSUE_ID)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("清除 MQTT 通道修复条目失败（卡片可能滞留）: %s", err)


async def _channel_guard(hass: HomeAssistant, verdict: str) -> None:
    """通道不变量违反时的留痕 + 修复条目（v1.7.60）。

    文案给足可执行修复：应然端点、默认凭据、以及"重启加载项后重试"的路径
    （重启重写引导标记 → 接管分支把条目改回内置端点）。绝不反噬主流程。
    """
    detail = {
        "endpoint_broken": ("端点文件存在但读不出（被截断/改坏）——无法核验 HA 是否"
                            "仍连在内置 Broker 上；请检查 HA 配置目录下 "
                            "window_controller_gateway_mqtt_endpoint.json（或重装/"
                            "重启慧尖加载项重建它）"),
        "probe_error": ("MQTT 条目表本轮读不到（HA 侧瞬时异常）——核验无结论；"
                        "若持续出现请查 HA 日志"),
        "no_entry": "HA 里没有启用中的 MQTT 配置条目",
        "ignored_only": ("HA 里只有被忽略（source=ignore，永不加载）的 MQTT 条目"
                         "——请到 设置→设备与服务 删除它，或直接添加一次 MQTT 集成"),
        "mismatch": "HA 的 MQTT 条目指向了别的 Broker",
        "disconnected": "MQTT 条目地址正确但客户端未连上内置 Broker（未起/凭据被拒）",
    }.get(verdict, verdict)
    _LOGGER.warning(
        "MQTT 通道核验未通过：%s——网关上报将无人接收（001 无人应答、"
        "设备/网关卡片不出）。应然端点 %s:%s；修复后无需重启 HA。若需慧尖"
        "把条目改回内置端点：重启慧尖加载项（会重写引导标记）后在「修复」"
        "卡点重试。本留痕每 %d 分钟巡查一次",
        detail, BUILTIN_BROKER, BUILTIN_PORT,
        max(1, int(CHANNEL_VERIFY_INTERVAL // 60)),
    )
    _report_channel_issue(hass, verdict)


def _enabled_huijian_entry_count(hass: HomeAssistant) -> int:
    """未禁用的慧尖条目数（v1.7.31 A-3，BUG-5 统一口径）。

    旧 healer"条目全卸即退出"用默认 async_entries()——禁用条目把它骗住：
    用户禁用慧尖条目后 healer 永续巡查，与"不悬挂"设计意图相悖。
    取数面炸穿按 1（宁多活一轮，不误杀自愈）。
    """
    try:
        return sum(1 for e in hass.config_entries.async_entries(DOMAIN)
                   if not getattr(e, "disabled_by", None))
    except Exception:  # noqa: BLE001
        return 1


def async_start_bootstrap_healer(hass: HomeAssistant) -> None:
    """A（用户 2026-09 拍板）：bootstrap 持久自愈。

    旧行为：ensure_mqtt_connection 只在慧尖条目 setup 瞬间跑一次，失败或
    错过窗口（官方 Mosquitto 后删、broker 晚起、表单不兼容）即静默等待
    下次 reload/HA 重启——现场"MQTT not ready"长期滞留的根因。

    新行为：条目 setup 时拉起本任务（每 hass 单实例、幂等，多条目并发调用
    安全）：只要引导标记还在（自动配置未落地）就重试 ensure——v1.7.29 恒频
    300s，v1.7.30 ④ 改指数退避（300s 起、×2、封顶 1h，首次触顶一条
    WARNING），未落地不放弃、破坏面不再高频施压；**v1.7.60：标记删除后不再
    直接退出**——转为每 30 分钟只读核验「MQTT 客户端是否仍连在内置 broker」
    （verify_builtin_channel），不变量被破坏时留 WARNING + 修复条目常驻可见，
    直到再次成立或慧尖条目全卸/hass 停机才收尾。ensure 内部模块级锁保证
    并发创建只发生一次；ConfigEntryNotReady（内置 broker 未起）按"稍后再试"
    语义吞掉，交给下一轮。
    """
    runtime = hass.data.setdefault(DOMAIN, {})
    existing = runtime.get("_bootstrap_healer")
    if existing is not None and not existing.done():
        return

    async def _healer():
        rounds = 0          # 连续未落地轮数（v1.7.30 ④ 退避基准；任务级局部，
                            # healer 重拉起即复位——重启/重 setup 算新周期）
        capped_warned = False
        try:
            while True:
                if getattr(hass, "is_stopping", False) or \
                        _enabled_huijian_entry_count(hass) == 0:
                    # v1.7.63（C-10）：收尾前清卡——否则"通道坏 → 出卡 → 用户
                    # 禁用慧尖条目"后 healer 不再运行，僵尸卡永留（即便 MQTT 被
                    # 改回内置端点也无人清）。停机路径不清（避免关机刷屏）。
                    if not getattr(hass, "is_stopping", False):
                        _clear_channel_issue(hass)
                        _clear_mdns_issue(hass)
                    return  # 宿主停机 / 启用条目已清空（v1.7.31 A-3：
                            # 禁用不再把 healer 骗成永续巡查——BUG-5 同口径）
                if not await has_bootstrap_marker(hass):
                    # v1.7.60 根因修复：引导落地 ≠ 通道健康。标记是一次性的
                    # （首落即删），旧实现此处直接 return——此后 MQTT 条目被
                    # 官方 Mosquitto/EMQX 抢走、被改向、被 Supervisor 覆盖或
                    # 凭据失配时，慧尖永不复查/告警/自愈。现场形态：网关仍在
                    # 上报（broker 侧可见），HA 侧零订阅者 ⇒ 001 无人应答
                    # （固件 5s 重发不止血）+ 发现卡永不出，日志全绿。
                    # v1.7.63（C-6）：**真常驻**——健康也睡 30 分钟再复查，
                    # 只有停机/启用条目清零才收尾（旧实现"健康即 return"是
                    # 一次性核验，docstring/CHANGELOG 宣称的"此后被抢走也复查"
                    # 并不成立；mDNS 卡也因此只在那一次被查）。
                    # v1.7.63（对抗复核 F2）：核验本体任何逃逸都不得杀死常驻
                    # healer（v1.7.61 S2"巡检任务静默死亡"同族）——按"无结论"
                    # 处理：不清卡、走 guard 留痕、继续循环。
                    try:
                        verdict = await verify_builtin_channel(hass)
                    except Exception as err:  # noqa: BLE001 — 按无结论处理
                        verdict = "probe_error"
                        from .utils import log_throttled
                        log_throttled(hass, "_channel_verify_err", "verify",
                                      600.0, _LOGGER.warning,
                                      "MQTT 通道核验异常（按无结论处理，不清卡）: %s",
                                      err)
                    # 标记已删 = 引导已落地，旧"引导未落地"卡片语义终结
                    _clear_takeover_issue(hass)
                    # mDNS 撞名提示（独立于通道核验：即便通道全绿，第二台 HA
                    # 也永远收不到走 mDNS 自动发现的网关）
                    await _mdns_guard(hass)
                    if verdict in ("ok", "no_endpoint"):
                        _clear_channel_issue(hass)
                    else:
                        await _channel_guard(hass, verdict)
                    if not await _interruptible_sleep(hass, CHANNEL_VERIFY_INTERVAL):
                        return
                    continue
                rounds += 1
                delay = _retry_delay(rounds)
                if delay >= BOOTSTRAP_RETRY_MAX_INTERVAL and not capped_warned:
                    capped_warned = True
                    _LOGGER.warning(
                        "MQTT 引导自愈连续 %d 轮未落地，重试间隔已指数退避封顶 %ds——"
                        "此后低频巡查不再逐轮刷日志；若内置 Broker 明明可达却始终不"
                        "落地，请到 设置→系统→修复入口（mqtt_bootstrap_pending）或"
                        "查慧尖加载项日志定根因，勿静默等待",
                        rounds, int(BOOTSTRAP_RETRY_MAX_INTERVAL),
                    )
                try:
                    await ensure_mqtt_connection(hass)
                except ConfigEntryNotReady:
                    pass  # broker 未起等"稍后再试"，下一轮再来
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning(
                        "MQTT 引导自愈轮次异常（%ds 后继续）: %s",
                        int(delay), err)
                if not await has_bootstrap_marker(hass):
                    _LOGGER.info("MQTT 引导自愈落地（标记已删除）")
                    # v1.7.60：收尾（clear + 退出）统一由循环顶部的常驻通道
                    # 核验分支决定——落地 ≠ 通道健康，不得在此直接退出。
                    continue
                _report_takeover_issue(hass)
                # v1.7.31（A-1）：单发 sleep(封顶 3600s) 改切片——one-shot
                # 长睡对停机信号无感，会把 async_stop 各阶段的 block_till_done
                # 预算顶到超时（真源实证 async_stop 确有 async_timeout 保护，
                # 不会真挂 1h，但每停一次烧一条阶段超时 WARNING、healer 最终
                # 被强杀——违背本任务"停机也退出，不悬挂"的 docstring 自述）。
                # 30s 片：停机最多迟 30s 自然退出，退避节奏总量不变。
                if not await _interruptible_sleep(hass, delay):
                    return
        finally:
            if hass.data.get(DOMAIN) is not None:
                hass.data[DOMAIN]["_bootstrap_healer"] = None

    runtime["_bootstrap_healer"] = hass.async_create_task(
        _healer(), name=f"{DOMAIN}_bootstrap_healer")

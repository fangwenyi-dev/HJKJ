"""慧尖云 hub 出站客户端（P0）——加载项主动长连到慧尖云，供小程序远程控制。

方案背景（2026-09-22 定线）：小程序正式版只能连"已备案域名 + wss"，而客户 HA 都在
家里 NAT 后面；故方向反过来——**加载项出站长连**到慧尖在微信云托管的 hub，小程序经
`wx.cloud.callContainer`（免配服务器域名，已真栈验证）发命令、拉状态。

设计要点（逐条对齐本仓既有纪律与历史事故）：
· **出站 wss（aiohttp）+ 指数退避重连**，睡眠一律 30s 切片——照 mqtt_bootstrap 的
  "长睡烧 HA 停机预算"教训（:500-516）与"恒频重试给对端施压"教训（:481-494）。
· **实例身份本地持久化**（instanceId/secret）：重启复用、不重复注册；连不上再重新
  注册（此时换新绑定码，需用户重新扫一次）。
· **状态上行复用 device_manager 状态监听**，条目形状与 LAN WS 网关同源（由调用方注入
  ws_gateway.device_ws_view 构造），保证"小程序看到的字段"两条通道完全一致。
· **命令下行**：action='control' + params{attribute,value}，值校验与 LAN `_cmd_control`
  同口径（空串/bool/非 str-int-float/非法数值一律拒），执行走调用方注入的 control_fn
  （与 LAN 同一条 send_ws_raw_004 路径）。
· **凭据不回显**：日志只记 cred_brief（长度+首字节），绝不落 secret/bindCode 明文。

本模块**不 import HA**（只 aiohttp+标准库）——便于在 tests/conftest 的假 HA 树下直接测。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import re
import tempfile
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import aiohttp

_LOGGER = logging.getLogger(__name__)


class HubHttpError(RuntimeError):
    """hub 回了非 200：带 `status` 与 hub 自己的 `err`，调用方按状态码分支。

    继承 RuntimeError 是刻意的（既有调用点与测试都按 RuntimeError 兜），但判据必须走
    `status`/`err` 属性——从 `str(e)` 里 grep "404" 一旦消息措辞变了就静默判错。
    `err` 是 hub 响应体里的应用级错误码（如 unknown_instance/bad_secret），
    非 JSON 响应体（反代错误页）时为 None。
    """

    def __init__(self, path: str, status: int, err: Optional[str] = None) -> None:
        super().__init__("hub %s -> %d" % (path, status))
        self.path = path
        self.status = int(status)
        self.err = err if isinstance(err, str) and err else None

# 慧尖云 hub 默认端点（P0）。客户零配置：加载项内置默认值，慧尖换域名只改默认值或
# entry.options 覆盖（hub_base / hub_install_key）。
HUB_DEFAULT_BASE = "https://huijian-hub-318191-7-1412991472.sh.run.tcloudbase.com"
HUB_DEFAULT_INSTALL_KEY = "c019cee1ef3c68ceea68cfdbcc121b6f"

# hub base 端点解析的环境变量档（专供真栈 e2e/CI 把注册指向黑洞，见 resolve_hub_base）。
HUB_BASE_ENV = "HUIJIAN_HUB_BASE"


def resolve_hub_base(option_value: str = "", env: Optional[Dict[str, str]] = None) -> str:
    """按优先级解析 hub base：entry.options > HUIJIAN_HUB_BASE 环境变量 > 内置默认。

    环境变量这一档存在的唯一理由：真栈 e2e（docker run_e2e.sh / WSL run_local.sh）跑的
    是**真实** async_setup_entry，会在 async_ensure_hub_client 里拿内置生产默认去
    /agent/register——每次 CI 都在生产 hub 注册表里留一条 sn=E2EGW0000001 的孤儿实例
    （v1.7.46/v1.7.47 两次 CI 已实锤各留一条）。e2e 把本变量设成 http://127.0.0.1:1
    即让注册秒失败（连接被拒→WARNING→退避），绝不触网到生产。

    默认（不设环境变量）＝逐层回退到内置生产地址，与既往行为逐字节相同（defaults-off）。
    env 参数仅供测试注入；用户显式配置的 option 永远压过环境变量（环境是基础设施杠杆，
    不该静默盖掉用户的主动选择）。
    """
    option_value = (option_value or "").strip()
    if option_value:
        return option_value
    mapping = os.environ if env is None else env
    return (mapping.get(HUB_BASE_ENV) or "").strip() or HUB_DEFAULT_BASE

HUB_RECONNECT_BASE_S = 5.0
HUB_RECONNECT_MAX_S = 300.0
HUB_RECONNECT_FLOOR_S = 1.0         # "连上过又被干净关掉"的最小间隔（防零间隔风暴，见 _run_forever）
HUB_RECONNECT_JITTER = 0.2          # 退避抖动 ±20%：hub pod 重启后多台 agent 同步 5/10/20… 就是惊群
HUB_SLEEP_SLICE_S = 30.0            # 长睡切片（停机时最多 30s 内让出）
HUB_STATE_DEBOUNCE_S = 0.3          # 状态上行合并窗
HUB_KEEPALIVE_S = 300.0             # 保活重推：让 hub 侧 updatedAt 反映 agent 存活
# 绑定码有效期的**兜底值**：权威值在云端（hub 回 expiresInSec / 注册回 bindExpire），
# 只有云端没回时才用这里（老 hub、身份文件是旧版本写的）。改这里不会改 hub 的 TTL。
BIND_CODE_TTL_S = 600
BIND_CODE_RENEW_BEFORE_S = 120      # 剩余不足这么久就自动换新码（用户不必自己发现过期）
BIND_CODE_RENEW_MIN_INTERVAL_S = 120.0   # 两次换码尝试的最小间隔（失败后不连击，见 _renew_bind_code_if_stale）
# 面板 GET /hub 顺带刷成员列表的服务端节流窗：面板从不调用只读的 /hub/members，
# 不刷就永远显示"只有你一人"；不节流就会把云端调用打成每 30s 一次（面板开着即刷）。
MEMBERS_REFRESH_MIN_INTERVAL_S = 120.0
HUB_HTTP_TIMEOUT_S = 15.0
# 面板顺带刷成员列表用的更短超时：GET /hub 此前纯读内存即回，加了顺带刷新之后最坏会被
# 一次云端 HTTP 挂住 15s（面板表现为刷新转圈）。/agent/members 在 hub 侧是**纯内存读**
# （listMembers 不落盘不推镜像），正常远快于 1s ⇒ 5s 已很宽松，超了就如实显示"读取失败"
# 并在下个节流窗重试，别把 HA 的 REST 请求吊在那里。
HUB_PANEL_HTTP_TIMEOUT_S = 5.0
HUB_IDENTITY_FILE = "huijian_hub_identity.json"
# 成员称呼表（v1.7.54）：hub 的成员记录只有 {openid, at}，**云端没有名称字段**，
# 所以"爸爸/妈妈"这类称呼只能存在本机面板侧（键＝hub 给的 mid）。
HUB_MEMBER_ALIAS_FILE = "huijian_member_aliases.json"
# 称呼长度上限。面板 huijian.js 里的 MEMBER_ALIAS_MAX 是同一值的第二份，
# 由 tests/test_v1754_member_alias.py 对账（改一边必须改另一边，否则
# 面板允许输入 13 个字而加载项静默截成 12 个＝对用户说谎）。
MEMBER_ALIAS_MAX_LEN = 12
# 长连凭据走请求头，不进 URL query：任何记 request line 的中间层（云托管访问日志、
# 反代、错误上报）都会把明文 secret 留档。hub 侧先读头、缺失回落 query（发版必须 hub 先）。
WS_HEADER_INSTANCE_ID = "x-hub-instance-id"
WS_HEADER_SECRET = "x-hub-secret"
# 长连握手被这些状态明确拒了＝"云端不认识本机身份"（hub 重新部署抹了注册表的主形态），
# 值得重注册；其余失败一律按网络问题处理，继续抱身份退避重试。
IDENTITY_REJECTED_HTTP = (401, 403)
# 连续多少次【被拒 - 重注册 - 再被拒】就熔断（不再自动重注册）。病态形态＝云托管多副本
# 且注册表不共享：register 落到 A、握手落到 B，每轮退避都会白造一个新实例。
HUB_REREGISTER_FUSE = 3
# 家庭成员上限的**兜底值**——权威值在云端（/agent/members 回 membersMax）。
# 与 hub 的 HUB_MEMBERS_MAX 同值（跨仓契约：两侧各写一份，
# 由 tests/test_v1747_cross_repo_contract 对账，改一边必须改另一边）
HUB_MEMBERS_MAX = 8

# 操作类错误的族别：成功只清**同族**的错误——换码走 /agent/bindcode、成员走
# /agent/members，一个通证明不了另一个通（跨族清零＝把还没恢复的故障藏起来）。
OP_BINDCODE = "bindcode"
OP_MEMBERS = "members"
OP_MEMBER_REMOVE = "member_remove"
OP_MEMBER_RENAME = "member_rename"
# 审计 2026-10-05 D-1 补口：身份类操作错误（云端已签发、本机落盘失败）。
# 它既不是"连接断了"（长连照活），也不属于换码/成员任何一族，混进连接槽会被
# 每轮尝试清掉且面板不映射 ⇒ 单独一家。
OP_IDENTITY = "identity"

_ALIAS_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")

_VALUE_RE = re.compile(r"-?\d+(\.\d+)?")


def hub_reconnect_delay(attempt: int) -> float:
    """指数退避：5s×2^(n-1)，封顶 5min，再叠 ±HUB_RECONNECT_JITTER 抖动。

    抖动防惊群：hub pod 重启后全部 agent 在同一秒开始按 5/10/20… 同步重试。
    地板（HUB_RECONNECT_FLOOR_S）是"干净断开"那条路专用的，不走这里，语义不受抖动影响。
    """
    if attempt < 1:
        attempt = 1
    base = min(HUB_RECONNECT_BASE_S * (2 ** (attempt - 1)), HUB_RECONNECT_MAX_S)
    delay = base * (1.0 + random.uniform(-HUB_RECONNECT_JITTER, HUB_RECONNECT_JITTER))
    # 抖动只许让重试错开，不许把它压到地板以下或顶穿封顶
    return min(max(delay, HUB_RECONNECT_FLOOR_S), HUB_RECONNECT_MAX_S)


async def interruptible_sleep(seconds: float, is_stopping: Callable[[], bool]) -> None:
    """30s 切片睡眠：停机时最多 30s 内让出（对齐 mqtt_bootstrap 同款纪律）。"""
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        if is_stopping():
            return
        await asyncio.sleep(min(HUB_SLEEP_SLICE_S, remaining))
        remaining -= HUB_SLEEP_SLICE_S


def _attr_int(dev: Any, key: str) -> int:
    """设备属性安全取整（-1=未知约定；bool/None/不可解析/非有限数一律 -1）。

    与 ws_gateway._as_int 同口径：`int(float('inf'))` 抛 OverflowError、
    JSON 里 `1e999` 就能造出 inf，故必须把 OverflowError 一起接住。
    """
    raw = (dev or {}).get("attributes") if isinstance(dev, dict) else None
    value = (raw or {}).get(key) if isinstance(raw, dict) else None
    if value is None or isinstance(value, bool):
        return -1
    try:
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return -1


def cred_brief(value: Any) -> str:
    """凭据摘要：只回长度+首字节（日志/视图都不回显明文）。"""
    if not value:
        return "(空)"
    s = str(value)
    return "len=%d head=%s" % (len(s), s[:2])


def _positive_int(value: Any) -> int:
    """云端回的数值一律过这道闸：bool/不可解析/非正数 ⇒ 0（＝未知，调用方回落兜底）。

    v1.7.64（对抗复核 A-1）：一并拒 float 不可表示的整数。本函数的产物最终都要做
    浮点算术（`ms / 1000.0`、`ttl - (time.time() - at)`），而 JSON 允许任意精度整数
    ——`10**400` 原样放行的话那两处算术抛 OverflowError：`_ttl_from_expire_ms` 在
    `_ensure_registered`（无局部兜底）里炸 ⇒ 长连主循环每 5s 重启且永远修不好；
    成员码倒计时在 `status_view` 里炸 ⇒ 面板 500（两处本机实测）。
    """
    if value is None or isinstance(value, bool):
        return 0
    try:
        n = int(value)
        float(n)   # 超出 float 可表示范围（≈1.8e308）＝后续浮点算术必抛，按不可用处理
    except (TypeError, ValueError, OverflowError):
        return 0
    return n if n > 0 else 0


def _ttl_from_expire_ms(bind_expire: Any) -> int:
    """注册回的是绝对到期时刻（epoch ms）⇒ 换算成剩余 TTL 秒；不可用回 0（回落常量）。"""
    ms = _positive_int(bind_expire)
    if not ms:
        return 0
    return max(0, int(ms / 1000.0 - time.time()))


def identity_path(config_dir: str) -> str:
    return os.path.join(config_dir, HUB_IDENTITY_FILE)


def load_identity(config_dir: str) -> Dict[str, Any]:
    """读实例身份；无/损坏一律回空 dict（损坏时改名留证，不静默丢状态）。"""
    path = identity_path(config_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001 - 任何读取异常都不该拖垮集成
        _LOGGER.warning("hub 身份文件读取失败（%s），按未注册处理", type(e).__name__)
        try:
            os.replace(path, path + ".bad")
        except Exception:  # noqa: BLE001
            pass
    return {}


def save_identity(config_dir: str, data: Dict[str, Any]) -> None:
    """原子写实例身份（唯一 tmp 名 + replace），避免断电留半截文件。

    tmp 名必须唯一：写入跑在 asyncio.to_thread 里（v1.7.49 起），固定的 `path + ".tmp"`
    会被两个协程交错截断 ⇒ os.replace 落地半截/混合 JSON ⇒ load_identity 判损坏改名
    .bad ⇒ 重新注册换 instanceId，作废所有人手上的绑定码。锁在 _save_identity 侧，
    这里是第二道（模块级函数也可能被别处直接调）。
    """
    path = identity_path(config_dir)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=config_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def member_alias_path(config_dir: str) -> str:
    return os.path.join(config_dir, HUB_MEMBER_ALIAS_FILE)


def clean_member_alias(raw: Any) -> str:
    """规范化成员称呼：控制符换空格、去首尾空白、截到 MEMBER_ALIAS_MAX_LEN。

    截断而不是拒绝：面板 JS 用同一个上限先截过一遍，两侧同口径 ⇒ 用户看到的就是存下来的。
    """
    if not isinstance(raw, str):
        return ""
    return _ALIAS_CTRL_RE.sub(" ", raw).strip()[:MEMBER_ALIAS_MAX_LEN]


def load_member_aliases(config_dir: str) -> Dict[str, str]:
    """读成员称呼表；缺失/损坏/内容畸形一律回空表。

    称呼只是本机面板上"怎么看这个人"，丢了不影响成员关系（关系真相在 hub），
    所以这里不像身份文件那样改名留证——一句告警足够，别把排障预算花在小装饰上。
    """
    path = member_alias_path(config_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001 - 读不出来就当没起过名字，不能拖垮面板
        _LOGGER.warning("成员称呼文件读取失败（%s），按无称呼处理", type(e).__name__)
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, str] = {}
    for mid, raw in data.items():
        if not isinstance(mid, str) or not mid:
            continue
        name = clean_member_alias(raw)
        if name:
            out[mid] = name
    return out


def save_member_aliases(config_dir: str, mapping: Dict[str, str]) -> None:
    """原子写成员称呼表（唯一 tmp 名 + replace，同 save_identity 的纪律）。"""
    path = member_alias_path(config_dir)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=config_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(mapping, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def validate_control_params(attribute: Any, value: Any) -> Optional[str]:
    """命令参数校验——与 LAN `_cmd_control` 同口径。

    返回规范化后的 value 字符串；不合法返回 None：
    空串/bool 拒（固件把空串按缺失字段拒、bool 经 str() 是 'True'/'False' 不可解析）；
    仅 str/int/float 可转线值（dict/list 经 str() 出 Python repr，固件不可解析，
    而调用方已收到假成功——v1.7.12 F6）；**任何形态**都须过十进制格式校验
    （inf/nan/1e999 等 str() 出设备不可解析字面量——v1.7.18 BUG-16；字符串入参
    自 v1.7.52 起**不再豁免**，'NaN' 就是从那道豁免缝里穿过去的）。
    """
    if not isinstance(attribute, str) or not attribute:
        return None
    if value is None or value == "" or isinstance(value, bool):
        return None
    if not isinstance(value, (str, int, float)):
        return None
    value_s = str(value)
    # 十进制格式闸对**所有**类型一律生效——此前 `str` 被豁免，而豁免正是漏洞本身：
    # 小程序云通道送上来的是 `params: {attribute, value: String(value)}`，
    # `NaN` 经 String() 得到字符串 'NaN' 恰好从豁免缝里穿过去，一路透传 004 到固件，
    # 且调用方已经拿到 ok:true 的假成功。合法值域逐条核过全部是十进制串
    # （w_travel ∈ 0/100/101/200 与位置 0-100、rwp_wind_lock_mode ∈ 0/1、
    # rwp_winact_speed/strength ∈ 0-100）⇒ 取消豁免不会挡掉任何合法命令。
    # 模式串与 ws_gateway._VALUE_RE 必须同串（两条通道判据不一致＝分裂行为）。
    if not _VALUE_RE.fullmatch(value_s):
        return None
    return value_s


class HubClient:
    """出站长连客户端（**一个 HA 安装一个实例**，聚合该安装下全部网关条目）。

    为什么不是"每条目一个实例"：身份文件 `huijian_hub_identity.json` 落在全局
    config_dir，N 个条目会互相覆盖同一份身份；HA 重启后它们又各自读回**同一个**
    instanceId 去连，而 hub 的 onAgent 会 `close(4000,'replaced')` 顶掉前一条 ⇒
    N 台网关抢一条长连（重连战争），且小程序只看到其中一台。
    """

    def __init__(
        self,
        managers: Optional[List[Any]] = None,
        *,
        config_dir: str,
        base: str = HUB_DEFAULT_BASE,
        install_key: str = HUB_DEFAULT_INSTALL_KEY,
        control_fn: Optional[Callable[[str, str, str], Awaitable[bool]]] = None,
        view_builder: Optional[Callable[[str, str, Dict[str, Any]], Dict[str, Any]]] = None,
        session: Optional[aiohttp.ClientSession] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._managers: List[Any] = list(managers or [])
        # 上一轮成功构造的设备视图，键 (gwSn, sn)。hub 侧要靠"这一批是全量"淘汰已删
        # sn，所以单台设备视图构造失败不能让它从快照里消失＝从"状态不更新"升级成
        # "小程序里设备没了"。这里留一份回退位。
        self._last_views: Dict[Any, Dict[str, Any]] = {}
        self.config_dir = config_dir
        self.base = (base or HUB_DEFAULT_BASE).rstrip("/")
        self.install_key = install_key or HUB_DEFAULT_INSTALL_KEY
        self.control_fn = control_fn
        self.view_builder = view_builder
        self._session = session
        self._own_session = session is None
        self._logger = logger or _LOGGER

        self.instance_id: Optional[str] = None
        self._secret: Optional[str] = None
        self.bind_code: Optional[str] = None
        self._bind_code_at: float = 0.0      # 当前码的签发时刻（epoch 秒；0=未知）
        # 码的有效期以云端回的为准（expiresInSec / 注册回的 bindExpire），0＝云端没给，回落常量
        self._bind_code_ttl_s: int = 0
        self._member_code_ttl_s: int = 0
        # 家庭成员（v1.7.47）：成员码短命且属显式操作 ⇒ **不落身份文件**（落了会与 hub 真相分叉）
        self.member_code: Optional[str] = None
        self._member_code_at: float = 0.0
        self.members: List[Dict[str, Any]] = []   # hub 回的掩码成员列表（原样透传给面板）
        self.owner_masked: Optional[str] = None
        self.members_supported: bool = True       # 老 hub 无成员端点时置 False，面板据此禁用成员区
        self.members_max: Optional[int] = None    # hub 回的成员上限（None＝没拿到，回落常量）
        self._members_refresh_at: float = 0.0     # 上一次"GET /hub 顺带刷成员"时刻（节流基准）
        # 成员称呼（v1.7.54）：本机面板侧的标签，键＝hub 的 mid，落 HUB_MEMBER_ALIAS_FILE。
        # 与上面那条"成员关系本地不留副本"不冲突——**关系**仍以 hub 为准，这里存的不是关系。
        self.member_aliases: Dict[str, str] = {}
        self._aliases_loaded = False
        self._bind_renew_at: float = 0.0     # 上一次"换码尝试"时刻（成功失败都记，用于节流）
        self._rereg_streak: int = 0        # 连续【被拒 - 重注册】计数（见 HUB_REREGISTER_FUSE）
        # 审计 2026-09-30 D-2：本机刚被云端拒过身份 ⇒ 磁盘凭据不可信，
        # _ensure_registered 必须跳过"读回旧凭据即复用"的短路（见该方法注释）。
        self._identity_rejected: bool = False
        self.connected = False
        # 两个错误槽必须分开：连接类（长连/身份）与操作类（换码/成员/踢人）混在一个槽里，
        # 一次瞬时操作失败就会长期盖住 identity_rejected_loop 这条最有诊断价值的信息，
        # 且所有成功路径都不清它 ⇒ 面板会在刚签发的有效码旁边一直喊"码可能已过期"。
        self.last_error: Optional[str] = None
        self.last_op_error: Optional[str] = None
        self._op_error_family: Optional[str] = None

        self._task: Optional[asyncio.Task] = None
        self._stopping = False           # 停机闩锁（照 _lifecycle._closing 纪律）
        self._state_dirty = asyncio.Event()
        self._snapshot_degraded = False   # 残缺快照只告警一次（恢复前不再刷屏）
        # 两把锁都按事件循环懒建（见 _loop_bound_lock）
        self._send_lock: Optional[asyncio.Lock] = None
        self._send_lock_loop: Optional[asyncio.AbstractEventLoop] = None
        self._identity_lock: Optional[asyncio.Lock] = None
        self._identity_lock_loop: Optional[asyncio.AbstractEventLoop] = None
        self._alias_lock: Optional[asyncio.Lock] = None
        self._alias_lock_loop: Optional[asyncio.AbstractEventLoop] = None

    # ── 错误槽（连接类 / 操作类）───────────────────────────────────
    def _set_op_error(self, family: str, value: Optional[str]) -> None:
        """记一次操作失败：值优先用 hub 回的真实 err（no_owner/members_full/…），
        本地降级值（bindcode_failed/members_unavailable）只在拿不到 err 时用。"""
        self._op_error_family = family
        self.last_op_error = value or "op_failed"

    def _clear_op_error(self, family: str) -> None:
        if self._op_error_family == family:
            self._op_error_family = None
            self.last_op_error = None

    def _write_lock(self) -> asyncio.Lock:
        """出帧锁（按事件循环懒建）。

        为什么懒建：asyncio.Lock 首次 await 就绑定当前循环，而条目 reload 与测试里的
        多次 asyncio.run 都会换循环——在 __init__ 里建死就是 RuntimeError。
        """
        loop = asyncio.get_running_loop()
        if self._send_lock is None or self._send_lock_loop is not loop:
            self._send_lock = asyncio.Lock()
            self._send_lock_loop = loop
        return self._send_lock

    def _identity_write_lock(self) -> asyncio.Lock:
        """身份写入锁（同款按循环懒建，理由见 _write_lock）。"""
        loop = asyncio.get_running_loop()
        if self._identity_lock is None or self._identity_lock_loop is not loop:
            self._identity_lock = asyncio.Lock()
            self._identity_lock_loop = loop
        return self._identity_lock

    def _alias_write_lock(self) -> asyncio.Lock:
        """成员称呼写入锁（同款按循环懒建）。面板连点两行「改名」＝两次并发写同一文件。"""
        loop = asyncio.get_running_loop()
        if self._alias_lock is None or self._alias_lock_loop is not loop:
            self._alias_lock = asyncio.Lock()
            self._alias_lock_loop = loop
        return self._alias_lock

    # ── 网关集合（条目增删/重载时由 __init__.py 重新聚合）───────────
    @property
    def managers(self) -> List[Any]:
        return list(self._managers)

    def attach_managers(self, managers: List[Any]) -> None:
        """把状态监听挂到**当前全部** device_manager 上，并摘掉已不存在的。

        幂等：重复 ensure（条目 reload、多处调用点）只会得到"每个 manager 一个回调"。
        照抄 ws_gateway._attach_listeners 的语义——漏摘会让回调继续持有已卸载条目的
        manager（死对象），漏挂则第二台网关的状态变化永远不上行。
        """
        incoming = list(managers or [])
        for gone in [m for m in self._managers if m not in incoming]:
            try:
                gone.remove_status_listener(self._on_device_status)
            except Exception:  # noqa: BLE001
                pass
        self._managers = incoming
        self.mark_state_dirty()
        for m in self._managers:
            try:
                m.add_status_listener(self._on_device_status)
            except Exception as e:  # noqa: BLE001
                self._logger.warning("hub 状态监听注册失败：%s", type(e).__name__)

    # ── 生命周期 ──────────────────────────────────────────────────
    async def async_start(self) -> None:
        """在运行中的事件循环里拉起（HA setup 路径；测试用 asyncio.run 驱动）。"""
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        await self._load_identity()
        await self._load_aliases()
        self.attach_managers(self._managers)
        self._task = asyncio.ensure_future(self._run_forever())

    async def async_stop(self) -> None:
        self._stopping = True
        for m in self._managers:
            try:
                m.remove_status_listener(self._on_device_status)
            except Exception:  # noqa: BLE001
                pass
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            # 用 gather(return_exceptions=True) 而不是裸 await：主动取消的子任务必然回
            # CancelledError，裸 await 会把它当"本协程被取消"传上去（收尾全跳过），
            # 而旧写法 `except (CancelledError, Exception)` 反过来把**我们自己**被取消
            # 的信号也吞了（HA 停机路径）。gather 两者都对：子任务的取消不转抛，
            # 本协程真被取消时照样上传。
            await asyncio.gather(task, return_exceptions=True)
        if self._own_session and self._session is not None:
            try:
                await self._session.close()
            except Exception:  # noqa: BLE001
                pass
        self._session = None
        self.connected = False

    # ── 连接主循环 ────────────────────────────────────────────────
    async def _run_forever(self) -> None:
        attempt = 0
        while not self._stopping:
            try:
                connected = False
                # 审计 2026-09-30 B-2 配套：每次尝试开头清空连接类错误槽。
                # 只在本轮没人写过语义值时才用异常类名兜底（见下方 except），
                # 否则上一轮残留的 identity_rejected 会把这一轮真实的异常藏起来。
                self.last_error = None
                try:
                    connected = await self._session_once()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - 任何异常都只降级重连
                    # 审计 2026-09-30 B-2：旧写法在这里**无差别覆写** last_error。
                    # 而 :927/:954 两处语义值（identity_rejected_loop /
                    # identity_rejected）写完必然 raise，异常一到本行就被改成异常
                    # 类名——面板 hubErrorText（huijian.js:133-141）只认那两个值，
                    # 其余一律回 ''，于是唯一能指路的文案（/healthz mirror.enabled、
                    # 实例数必须为 1）永久丢失，用户只看到"未连接"。
                    # 本行上方的 :388-390 注释早就写明"不得让瞬时失败盖住这条最有
                    # 诊断价值的信息"，实装却正好盖住了——按该口径改为只在空槽时兜底。
                    # 同时 :519 只打类名把 HubHttpError.status/err（:39-51 专为此设计）
                    # 与消息一起丢掉；:723-731 已为同一课定下 `why = str(e) or 类名`
                    # 的口径并做凭据降级，这里对齐（WS URL 已不带凭据，:880-893）。
                    why = str(e) or type(e).__name__
                    if self._secret and self._secret in why:
                        why = type(e).__name__    # 万一消息里带上密钥，降级为类名
                    if not self.last_error:
                        self.last_error = why
                    self._logger.warning("hub 连接异常（%s），准备重连", why)
                finally:
                    self.connected = False
                if self._stopping:
                    return
                # `_session_once` 只在**真的连上过**之后正常返回才为 True——所以"连上后被
                # 对端关掉"证明端点是通的，退避阶梯必须归零（否则 hub 反复重启时阶梯会一路
                # 涨到 300s，把恢复拖成几分钟不可用；跨仓 e2e 的 C 臂就是这么红的）。
                # 但仍留 HUB_RECONNECT_FLOOR_S 地板：旧实现此时零间隔立即重连，遇到
                # "接受后立刻关"（被 replaced / 网关抽风 / 云端发布中）就是无间隔风暴。
                if connected:
                    attempt = 0
                    delay = HUB_RECONNECT_FLOOR_S
                else:
                    attempt += 1
                    delay = hub_reconnect_delay(attempt)
                self._logger.info("hub 重连退避 %.0fs（第 %d 次）", delay, attempt)
                await interruptible_sleep(delay, lambda: self._stopping)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 看门狗：循环体自身异常不得让任务静默死
                # 真机形态（v1.7.48）：hub pod 换版后主循环体（退避计算/切片睡眠等）抛一次
                # 异常 ⇒ 本任务退出且无人重启 ⇒ agentsOnline 永久 0、必须人工重启集成才恢复。
                # 内层 try 只护住 _session_once，护不住循环体其余行；这层兜底让循环"不死"，
                # 并记 ERROR 指路（此前是静默退出，日志里什么都看不到）。
                self.last_error = "loop_%s" % type(e).__name__
                self._logger.error(
                    "hub 长连主循环自身异常（%s），5s 后重启循环——若反复出现请把本行上报",
                    type(e).__name__)
                await interruptible_sleep(5.0, lambda: self._stopping)

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=HUB_HTTP_TIMEOUT_S)
            self._session = aiohttp.ClientSession(timeout=timeout)
            self._own_session = True
        return self._session

    async def _http(self, path: str, payload: Dict[str, Any],
                    timeout_s: Optional[float] = None) -> Dict[str, Any]:
        session = await self._ensure_session()
        # 只在显式给了更短超时时才覆盖：aiohttp 里 `timeout=None` 的语义是**不限时**而不是
        # "用会话默认"，无脑透传会把 HUB_HTTP_TIMEOUT_S 那道闸整个拆掉。
        kwargs: Dict[str, Any] = {}
        if timeout_s:
            kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout_s)
        async with session.post(self.base + path, json=payload, **kwargs) as resp:
            if resp.status != 200:
                raise HubHttpError(path, resp.status, await self._err_of(resp))
            # v1.7.64（第三轮复核 #6）：200 也要验形状。云函数返回空时云托管照样回
            # 200 + `null`（或 `[]`/字符串），旧实现原样 return ⇒ 调用方在 **try 之外**
            # 做 `data.get("ok")`（见 list_members 的 if not data.get(...)）⇒
            # AttributeError 逃出本方法：面板路由 500、maybe_refresh_members 打死保活
            # task（本仓"巡检任务静默死亡"同族）。按边界形状拒，错误码可辨识。
            body = await resp.json()
            if not isinstance(body, dict):
                raise HubHttpError(path, resp.status, "bad_body")
            return body

    @staticmethod
    async def _err_of(resp: Any) -> Optional[str]:
        """从非 200 响应体里取 hub 的应用级 err（取不到就 None，绝不猜）。

        为什么值得多读一次体：hub 的 /agent/members 在"实例不认识"时也回 404，
        与"老 hub 没有这个端点"同状态码——只有体里的 err 能分开这两件事。
        """
        try:
            body = await resp.json()
        except Exception:  # noqa: BLE001 - 反代错误页/空体都不是 hub 的 err
            return None
        if isinstance(body, dict) and isinstance(body.get("err"), str):
            return body["err"]
        return None

    async def _load_identity(self) -> None:
        """读本地身份（重启复用；文件缺失/损坏按未注册处理）。

        文件 I/O 放线程池：HA 会检测事件循环内的阻塞 open（v1.7.48 真机被点名）。
        """
        ident = await asyncio.to_thread(load_identity, self.config_dir)
        self.instance_id = ident.get("instanceId") or None
        self._secret = ident.get("secret") or None
        self.bind_code = self.bind_code or ident.get("bindCode") or None
        if not self._bind_code_at:
            try:
                self._bind_code_at = float(ident.get("bindCodeAt") or 0)
                # v1.7.61 对抗复核 S1：`1e999`（JSON 合法）→ float('inf') 不抛，
                # 却会让 bind_code_expires_in 的 int() 抛 OverflowError ⇒
                # status_view（面板 API）500、_keepalive_loop 任务静默死亡。
                if not math.isfinite(self._bind_code_at):
                    self._bind_code_at = 0.0
            except (TypeError, ValueError, OverflowError):
                # v1.7.64（#3 同类漏网）：身份文件里的大整数进 float() 抛的是
                # OverflowError，旧元组接不住 ⇒ 从 _load_identity 逃出：面板侧
                # （refresh_bind_code/list_members）500，长连侧每 5s 重启主循环
                # 却永远修不好那份文件（本仓"巡检反复异常不落地"同族）
                self._bind_code_at = 0.0
        # 老身份文件没有 bindCodeTtl ⇒ 0 ⇒ 有效期回落 BIND_CODE_TTL_S（向后兼容读取）
        if not self._bind_code_ttl_s:
            self._bind_code_ttl_s = _positive_int(ident.get("bindCodeTtl"))

    async def _load_aliases(self) -> None:
        """读本机成员称呼表（幂等：面板路由不经过 async_start，改名前自己补一次）。

        v1.7.64（对抗复核 A-2）：读盘与赋 `_aliases_loaded` 必须在**同一把写锁内**并
        做二次判。旧实现两个并发改名各自发起一次读盘，慢的那次回来把
        `self.member_aliases` 整个覆盖成"第一次落盘之前"的磁盘快照——即便读-改-写
        已经入锁，先完成的那次改名仍会在内存与磁盘上被抹掉（冷启动窗：
        `_aliases_loaded` 还是 False 时面板就能连着点两行改名）。
        """
        if self._aliases_loaded:
            return
        async with self._alias_write_lock():
            if self._aliases_loaded:
                return
            self.member_aliases = await asyncio.to_thread(load_member_aliases, self.config_dir)
            self._aliases_loaded = True

    async def set_member_alias(self, mid: str, name: Any) -> bool:
        """给一位家人起本机称呼（面板「改名」）。留空＝恢复显示云端掩码。

        为什么存本地而不是云端：hub 的成员记录只有 {openid, at} 两个字段，没有任何
        名称/头像概念，小程序侧也从来没有成员列表可显示——为一句称呼去加一条云端
        写端点，代价是发版顺序 + 注册表镜像文档体积，收益是零。
        键取 hub 的 mid（= sha256(openid) 前 12 位，只随微信账号走）：人被踢掉再扫
        回来，称呼仍然对得上同一个人。
        只认当前列表里的 mid：面板只会给看得见的行挂「改名」，凭一个不存在的 mid
        往文件里写名字没有任何正当调用方，而 HTTP 边界要自己把关。
        """
        await self._load_aliases()
        mid = str(mid or "")
        if not mid or not any(
            isinstance(m, dict) and str(m.get("mid") or "") == mid for m in self.members
        ):
            self._set_op_error(OP_MEMBER_RENAME, "member_rename_rejected")
            return False
        cleaned = clean_member_alias(name)
        # v1.7.64（第三轮复核 #5）：读-改-写必须**整体在锁内**。旧实现把
        # `dict(self.member_aliases)` 快照做在锁外，两行「改名」并发时两边从同一
        # 份快照出发、各自整份覆盖落盘 ⇒ 先完成的那次改名静默丢失（锁只串行了
        # 写盘，没串行 read-modify-write）。内存提交一并挪进锁内：后一人必须看得见
        # 前一人的结果，否则同样的丢更新会在内存里再复现一次。
        async with self._alias_write_lock():
            table = dict(self.member_aliases)
            if cleaned:
                table[mid] = cleaned
            else:
                table.pop(mid, None)
            try:
                await asyncio.to_thread(save_member_aliases, self.config_dir, table)
            except Exception as e:  # noqa: BLE001 - 写不进去就别在内存里假装记住了
                self._set_op_error(OP_MEMBER_RENAME, "member_rename_failed")
                self._logger.warning("成员称呼落盘失败（%s），本次改名未保存", type(e).__name__)
                return False
            self.member_aliases = table
        self._clear_op_error(OP_MEMBER_RENAME)
        return True

    async def _ensure_registered(self) -> None:
        """有身份则直接连；无身份（或上次被拒）则注册一次拿 instanceId/secret/绑定码。"""
        # 审计 2026-09-30 D-2：_invalidate_identity 的落盘可能失败（盘满/只读），
        # 那时磁盘仍是刚被 401 拒掉的那张凭据。不加这道闸的话 _load_identity 把
        # 死身份读回来、下方判"有身份"直接 return 跳过注册 ⇒ 再 401 → 三次熔断，
        # 而且 HA 重启也读回同一份死身份，永不自愈。被拒标记只在**本机刚被拒过**
        # 时为真，正常重启复用凭据的行为不变。
        if self.instance_id is None and not self._identity_rejected:
            await self._load_identity()
        if self.instance_id and self._secret and not self._identity_rejected:
            return
        # 注册载荷的 sn 只是 hub 侧展示字段（协议不变）：取首个网关，
        # 完整网关列表靠状态上行的 gwSn 体现——不让 hub 变成网关拓扑的权威。
        sn = self._first_gateway_sn()
        data = await self._http("/agent/register", {
            "installKey": self.install_key,
            "sn": sn,
            "fw": "huijian-gateway-plugin/hub-p0",
        })
        if not data.get("ok"):
            raise RuntimeError("register rejected: %s" % data.get("err"))
        self.instance_id = data.get("instanceId")
        self._secret = data.get("secret")
        if not (self.instance_id and self._secret):
            # ok:true 却缺凭据＝hub 侧半截响应：裸 KeyError 看着像本地 bug，得说清是谁的问题
            raise RuntimeError("register incomplete")
        self.bind_code = data.get("bindCode")
        self._bind_code_at = time.time()
        # 注册回的是**绝对**到期时刻（ms），换算成 TTL；缺失＝老 hub，回落本地常量
        self._bind_code_ttl_s = _ttl_from_expire_ms(data.get("bindExpire"))
        self._identity_rejected = False   # 拿到新身份即解除强制重注册闸
        # 审计 2026-09-30 D-1 第三处：云端这一步**已经成功**（instanceId/secret/绑定码
        # 都已签发），只有本地落盘失败（盘满/只读/tmp 竞态）。裸 await 会把异常抛给
        # _run_forever ⇒ 本轮作废，下一轮 _ensure_registered 见内存里有身份直接 return
        # ⇒ 一切"看起来正常"，直到 HA 重启才暴露：磁盘没凭据 ⇒ 重新注册 ⇒ 换
        # instanceId ⇒ 作废所有人手上的绑定码 + hub 侧留一条孤儿实例。身份只在内存里
        # 活着是可继续工作的状态，但必须让人看得见——看得见的具体落点见下方操作槽。
        try:
            await self._save_identity()
        except Exception as e:  # noqa: BLE001 - 落盘失败不许掐断刚签发的身份
            # 审计 2026-10-05 D-1 补口（v1.7.56 只修了一半）：这里原先写 `last_error`
            # （连接类槽），但那个槽 ① 每轮尝试开头就被清成 None、② 面板 hubErrorText
            # 只认 identity_rejected/identity_rejected_loop 两个值 ⇒ 注释承诺的"必须让人
            # 看得见"根本不成立。改走**操作类槽**：不被每轮清、面板按 hubOpErrorText
            # 渲染（与同批换码站点 bindcode_persist_failed 同口径）。
            self._set_op_error(OP_IDENTITY, "identity_persist_failed")
            self._logger.warning(
                "hub 身份落盘失败（%s），本次凭据仅存内存：HA 重启后会重新注册并作废绑定码",
                type(e).__name__)
        else:
            # 这一轮写盘成功＝上一条落盘告警已过时，成功即清（同族口径）
            self._clear_op_error(OP_IDENTITY)
        # 绑定码要让用户看得到，但日志只记摘要（凭据不回显纪律）
        self._logger.info("hub 注册成功 instance=%s 绑定码=%s（请在插件页查看完整码）",
                          self.instance_id, cred_brief(self.bind_code))

    async def _save_identity(self) -> None:
        """落盘实例身份（含签发时刻与云端给的 TTL——否则重启后无从判断码是否已过期）。

        锁 + 唯一 tmp 名两道防线：写在 asyncio.to_thread 里，两个协程可真并发
        （面板连点两次二维码＝显式换码刻意不受节流；用户点刷新与保活 tick 重叠），
        交错截断同一个 .tmp 会让 os.replace 落地混合 JSON ⇒ 身份读回时被判损坏改名
        .bad ⇒ 重注册换 instanceId ⇒ 作废所有人手上的绑定码 + 云端多一条孤儿实例。
        """
        async with self._identity_write_lock():
            await asyncio.to_thread(save_identity, self.config_dir, {
                "instanceId": self.instance_id,
                "secret": self._secret,
                "bindCode": self.bind_code,
                "bindCodeAt": round(self._bind_code_at, 3),
                "bindCodeTtl": self._bind_code_ttl_s,
            })
        # v1.7.61（10-05 审计 #4）：落盘成功 ⇒ "身份只在内存"的告警条件已消失，
        # 必须在这里清。这是**所有落盘路径的汇合点**（注册路径与
        # refresh_bind_code 都经此）——旧实现只在 _ensure_registered 的 else
        # 分支清，而该方法在"内存已有身份"时早返回 ⇒ 首轮落盘失败后任何后续
        # 成功落盘都不再清，面板永久显示"没保存云端身份/重启会作废绑定码"。
        self._clear_op_error(OP_IDENTITY)

    def bind_code_ttl_s(self) -> int:
        """当前绑定码的有效期秒数：云端回的优先，没有才回落 BIND_CODE_TTL_S。"""
        return self._bind_code_ttl_s or BIND_CODE_TTL_S

    def member_code_ttl_s(self) -> int:
        return self._member_code_ttl_s or BIND_CODE_TTL_S

    def bind_code_expires_in(self) -> int:
        """当前绑定码剩余秒数（负数=已过期；0 是签发时刻未知＝按过期处理）。"""
        if not self.bind_code or not self._bind_code_at:
            return -1
        try:
            return int(self.bind_code_ttl_s() - (time.time() - self._bind_code_at))
        except (OverflowError, ValueError):
            # v1.7.61 对抗复核 S1 出口守卫：_bind_code_at/ttl 任一为非有限数时
            # int() 抛 OverflowError（旧实现直接炸穿面板 status_view 与保活循环）。
            return -1

    async def refresh_bind_code(self, kind: str = "owner") -> bool:
        """向 hub 换一个新绑定码（旧码当场作废）。kind='member' 换的是**成员码**。

        面板"点二维码/添加家人"与 owner 码的自动补发都走这里。失败只记日志回 False——
        绑定码拿不到不影响本地控制与云通道本身。
        """
        if not (self.instance_id and self._secret):
            await self._load_identity()
        if not (self.instance_id and self._secret):
            return False
        kind = "member" if kind == "member" else "owner"
        self._bind_renew_at = time.time()     # 成功失败都算一次尝试（自动补发侧的节流基准）
        try:
            data = await self._http("/agent/bindcode", {
                "instanceId": self.instance_id,
                "secret": self._secret,
                "kind": kind,
            })
        except Exception as e:  # noqa: BLE001 - 网络/旧 hub 无此端点都只降级
            # 原因必须进日志：这里曾只打 type(e).__name__，而 RuntimeError 的消息带的
            # 正是 "hub /agent/bindcode -> 502"——真机 32s 内 8 条同样的告警，却没人说得出
            # 是云端发布中（该重试）、凭据不认（该重注册）还是被墙（该看网络）。
            why = str(e) or type(e).__name__
            if self._secret and self._secret in why:
                why = type(e).__name__        # 万一消息里带上 URL，绝不回显凭据
            self._set_op_error(OP_BINDCODE, "bindcode_failed")
            self._logger.warning("hub 换绑定码失败（kind=%s）：%s", kind, why)
            return False
        if not data.get("ok") or not data.get("bindCode"):
            # 保留 hub 的真实 err（no_owner/members_full/rate_limited/registry_full/…）：
            # 压成一个笼统的 bindcode_rejected，面板就只能说"稍后再试"，而 no_owner 的
            # 正解是"先自己扫码成为主人"——重试永远不会成功。
            self._set_op_error(OP_BINDCODE, data.get("err") or "bindcode_rejected")
            self._logger.warning("hub 换绑定码被拒（kind=%s）：%s", kind, data.get("err"))
            return False
        if kind == "member" and data.get("kind") != "member":
            # 老 hub 忽略 kind ⇒ 它轮换的其实是 owner 码（用户手上那张已作废，覆水难收）。
            # 判据只能是响应里的 kind 回显；此处必须**丢弃返回值**，否则面板会把 owner 码
            # 当成员码显示 —— 家人扫到的就是"成为主人"的码。
            self.members_supported = False
            self._set_op_error(OP_BINDCODE, "hub_too_old_for_member_code")
            self._logger.warning("云端 hub 版本过旧（/agent/bindcode 不回 kind），已忽略成员码请求")
            return False
        ttl = _positive_int(data.get("expiresInSec"))   # 权威 TTL 在云端，缺了才回落常量
        if kind == "member":
            self.member_code = data["bindCode"]
            self._member_code_at = time.time()
            self._member_code_ttl_s = ttl
            self._clear_op_error(OP_BINDCODE)
            self._logger.info("hub 成员码已签发（%s）", cred_brief(self.member_code))
            return True
        self.bind_code = data["bindCode"]
        self._bind_code_at = time.time()
        self._bind_code_ttl_s = ttl
        self._clear_op_error(OP_BINDCODE)
        # 审计 2026-09-30 D-1：`_save_identity` 此前裸奔。抛 OSError（盘满 /
        # config_dir 不可写）时三件后果一起发生：①api.py 的换码视图未包异常
        # ⇒ 面板收 500；②本方法由 _keepalive_loop:1015/1020 调用时保活 task
        # 静默死亡，_session_once 的 gather(return_exceptions=True) 把异常吞掉
        # ⇒ 无日志、last_error 不变，本会话期内自动换码与"标脏重推"全部停摆
        # （正是 :1008-1013 说要避免的"updatedAt 越来越旧"）；③hub 已当场作废
        # 旧码，磁盘仍留旧码 ⇒ 重启后 status_view 回显一张死码，用户扫它必失败。
        # 与同文件 set_member_alias 的落盘闸同口径：捕获、如实标错、不假装没发生。
        # 不回滚内存值——新码才是云端当前认的那张，回滚成旧码等于确定性地发一张死码。
        try:
            await self._save_identity()
        except Exception as e:  # noqa: BLE001 - 落盘失败不得拖垮换码与保活
            self._set_op_error(OP_BINDCODE, "bindcode_persist_failed")
            self._logger.warning(
                "hub 绑定码已换发但本机落盘失败（%s）：本会话内这张码有效，"
                "但 HA 重启后会回显旧码（已被云端作废）——请重新点二维码换码并扫码",
                type(e).__name__)
            return True
        self._logger.info("hub 绑定码已更新（%s）", cred_brief(self.bind_code))
        return True

    def member_code_expires_in(self) -> int:
        """成员码剩余秒数（-1＝无码/签发时刻未知，与 owner 码同口径：不当"刚过期"渲染）。"""
        if not self.member_code or not self._member_code_at:
            return -1
        try:
            return int(self.member_code_ttl_s() - (time.time() - self._member_code_at))
        except (OverflowError, ValueError):
            # v1.7.64（对抗复核 A-1）：与上方 bind_code_expires_in 的 v1.7.61 S1 出口
            # 守卫同形——这条是同族漏网，非有限/过大的 ttl 或签发时刻会让 int() 抛
            # OverflowError 直接炸穿 status_view（面板 500）
            return -1

    async def list_members(self) -> bool:
        """拉家庭成员（掩码 openid + mid 句柄）。

        走**实例凭据**而不是 openid：openid 只由云托管注入到小程序请求，加载项根本没有。
        只有 hub 明确"没有这个端点"（404 且体里没给应用级 err）才降级成
        members_supported=False；超时/5xx/网络一律是**读取失败**——两者在面板上是完全不同
        的话（"云端版本过旧，暂不支持" vs "读取失败，稍后重试"），混为一谈会让人以为
        云通道坏了、或以为升级云端就能修好一个网络问题。
        """
        if not (self.instance_id and self._secret):
            await self._load_identity()
        if not (self.instance_id and self._secret):
            return False
        try:
            data = await self._http("/agent/members", {
                "instanceId": self.instance_id,
                "secret": self._secret,
            }, timeout_s=HUB_PANEL_HTTP_TIMEOUT_S)
        except HubHttpError as e:
            if e.status == 404 and e.err in (None, "not_found"):
                self.members_supported = False
                self._set_op_error(OP_MEMBERS, "members_unsupported")
                self._logger.warning("云端 hub 无 /agent/members（404），成员区降级为不支持")
            else:
                # hub 的 /agent/members 在"实例不认识"时也回 404（体里带 err）——那不是老 hub
                self._set_op_error(OP_MEMBERS, e.err or "members_unavailable")
                self._logger.warning("hub 取家庭成员失败：HTTP %s err=%s", e.status, e.err)
            return False
        except Exception as e:  # noqa: BLE001
            why = str(e) or type(e).__name__
            if self._secret and self._secret in why:
                why = type(e).__name__
            self._set_op_error(OP_MEMBERS, "members_unavailable")
            self._logger.warning("hub 取家庭成员失败：%s", why)
            return False
        if not data.get("ok"):
            self._set_op_error(OP_MEMBERS, data.get("err") or "members_rejected")
            self._logger.warning("hub 取家庭成员被拒：%s", data.get("err"))
            return False
        self.members_supported = True
        self.members = list(data.get("members") or [])
        self.owner_masked = data.get("ownerMasked")
        self.members_max = _positive_int(data.get("membersMax")) or None
        self._clear_op_error(OP_MEMBERS)
        return True

    async def maybe_refresh_members(self) -> None:
        """GET /hub 顺带刷一次成员列表（服务端节流）。

        为什么必须在这里刷：面板只调 GET /hub 与两条 POST，从不调只读的 /hub/members，
        而 status_view 回的是内存里的 self.members（只在 list_members 里被写）⇒ HA 每次
        重启后打开面板一律"只有你一人"、count=0，看不到也踢不了任何已有家人，
        直到用户点一次「添加家人」才顺带把列表刷准。
        节流窗内不重复打云端；面板不开就完全不产生调用。老 hub（members_supported=False）
        不触发——那只会白拿一个 404。
        """
        if not (self.connected and self.members_supported):
            return
        if time.time() - self._members_refresh_at < MEMBERS_REFRESH_MIN_INTERVAL_S:
            return
        self._members_refresh_at = time.time()   # 成败都算一次（并发 GET 不得同时打云端）
        await self.list_members()

    async def remove_member(self, mid: str) -> bool:
        """按 mid 踢一个成员（面板「移除」）。mid 由 list_members 给出：稳定、不可逆推。"""
        if not (self.instance_id and self._secret) or not mid:
            return False
        try:
            data = await self._http("/agent/unbind", {
                "instanceId": self.instance_id,
                "secret": self._secret,
                "mid": str(mid),
            })
        except HubHttpError as e:
            self._set_op_error(OP_MEMBER_REMOVE, e.err or "member_remove_failed")
            self._logger.warning("hub 移除成员失败：HTTP %s err=%s", e.status, e.err)
            return False
        except Exception as e:  # noqa: BLE001
            why = str(e) or type(e).__name__
            if self._secret and self._secret in why:
                why = type(e).__name__
            self._set_op_error(OP_MEMBER_REMOVE, "member_remove_failed")
            self._logger.warning("hub 移除成员失败：%s", why)
            return False
        if not data.get("ok"):
            self._set_op_error(OP_MEMBER_REMOVE, data.get("err") or "member_remove_rejected")
            self._logger.warning("hub 移除成员被拒：%s", data.get("err"))
            return False
        self._logger.info("家庭成员已移除（剩余 %s）", data.get("remaining"))
        self._clear_op_error(OP_MEMBER_REMOVE)
        await self.list_members()
        return True

    async def _renew_bind_code_if_stale(self) -> None:
        """快到期/已过期就自动换新码——面板上显示的码因此始终可用。

        失败后按 BIND_CODE_RENEW_MIN_INTERVAL_S 节流：这条挂在"上线自检 + 每个保活
        tick"上，云端发布中的几十秒里会话会重连很多次，不节流就是同一告警刷屏
        （真机实发 32s 内 8 条）+ 对付费端点的无意义连击。面板上用户点二维码那条
        （api.py → refresh_bind_code）**不受此节流**——那是显式意图。
        """
        if self.bind_code_expires_in() >= BIND_CODE_RENEW_BEFORE_S:
            return
        if time.time() - self._bind_renew_at < BIND_CODE_RENEW_MIN_INTERVAL_S:
            return
        if await self.refresh_bind_code():
            self._logger.info("绑定码自动补发完成（原码已作废，请以面板显示为准）")
        else:
            self._logger.warning("绑定码已过期且自动补发失败——面板点一下二维码可重试")

    def _ws_url(self) -> str:
        """长连端点——**不带凭据 query**。

        secret 进 URL 就会被任何记 request line 的中间层留档（云托管访问日志、反代、
        错误上报），与"凭据不回显"纪律相悖；HTTP 的 /agent/* 一直走 body，唯独 WS 例外。
        另一个隐患：aiohttp 的 ClientResponseError.__str__ 带完整 URL，谁写一句 str(e)
        就把 secret 送进 HA 日志。凭据改走请求头（见 _ws_headers）。
        """
        base = self.base
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return "%s/agent/ws" % base

    def _ws_headers(self) -> Dict[str, str]:
        return {
            WS_HEADER_INSTANCE_ID: str(self.instance_id or ""),
            WS_HEADER_SECRET: str(self._secret or ""),
        }

    async def _open_ws(self, session: aiohttp.ClientSession):
        """建长连；云端明确"不认识这个身份"时当场重注册再连一次（只补一次）。

        为什么必须显式判 HTTP 状态：hub 抹掉注册表（云托管重新部署 ⇒ /data 是容器本地盘）
        后，本机身份对它而言就是陌生凭据。旧版 hub 是裸 destroy，客户端只能看到
        ServerDisconnectedError＝与"网络抖一下"同形 ⇒ 抱着死身份无限退避重连，
        面板显示的码是当前 hub 从未签发过的，小程序侧必然 code_invalid 且永不自愈。
        现在 hub 被拒时回 401（见 huijian-cloud-hub src/server.js），据此才敢重注册：
        **网络层失败一律不清身份**，否则每次抖动都多发一次 register、把用户手上的活码换掉。
        """
        try:
            return await session.ws_connect(self._ws_url(), headers=self._ws_headers(),
                                            heartbeat=25.0)
        except aiohttp.WSServerHandshakeError as e:
            if e.status not in IDENTITY_REJECTED_HTTP:
                raise
            if self._rereg_streak >= HUB_REREGISTER_FUSE:
                # 连续被拒不是"hub 忘了我"这一件事，而是每次 register 落到另一个容器：
                # 云托管多副本、或注册表没跨重建活下来时正是这个形态。继续重注册只会
                # 无限造孤儿实例 + 反复作废用户手上的绑定码 ⇒ 停在熔断上，把根因写给运维。
                #
                # 指路必须与 hub 侧口径一致：v0.2.6 起注册表跨重建存活靠**云开发数据库镜像**，
                # 不靠「存储挂载」——「存储挂载 → 对象存储」在云开发云存储桶类型上实测必失败
                # （平台生成的 cosfs endpoint 缺 http:// scheme ⇒ 挂载钩子 exit 1 ⇒ Pod 起不来）。
                # 这里原先叫人去配存储挂载：照着做会把 hub 搞成根本起不来，比原故障更糟。
                # 由 tests/e2e/cross_repo_contract.sh 与 hub README 对账，别再漂回去。
                self.last_error = "identity_rejected_loop"
                self._logger.error(
                    "hub 连续 %d 次拒本机身份，已停止自动重注册。根因几乎总是云端注册表没跨重建活下来："
                    "①看 hub 的 /healthz：mirror.enabled 必须为 true 且 mirror.adopted.instances 非 0；"
                    "reason=missing_cred ＝云托管「API Key 设置」没注入 TENCENTCLOUD_SECRETID /"
                    " TENCENTCLOUD_SECRETKEY / TCB_ENV。**不要**改去配「存储挂载」——云开发桶上实测必失败"
                    "且会让 Pod 起不来（详见 hub 仓 README 部署章节）；"
                    "②确认服务实例数固定为 1（/cmd 按容器内存里的长连表转发，多副本必然随机 offline）",
                    self._rereg_streak)
                raise
            await self._invalidate_identity("云端拒绝身份（HTTP %s）" % e.status)
            await self._ensure_registered()
            return await session.ws_connect(self._ws_url(), headers=self._ws_headers(),
                                            heartbeat=25.0)

    async def _invalidate_identity(self, reason: str) -> None:
        """清空本地身份并落盘：下次连接必然重新注册（换新 instanceId + 新绑定码）。

        代价要说清：注册表没了意味着**绑定关系也没了**，各微信号都要重新扫一次码——
        这一步不能替用户偷偷完成，所以只记 WARNING 指路，不假装什么都没发生。
        """
        self.instance_id = None
        self._secret = None
        self.bind_code = None
        self._bind_code_at = 0.0
        # 审计 2026-09-30 D-2：member 侧字段同样属于**当前这个 instance**。
        # status_view 无条件回显 memberCode/members/ownerMasked，不清就会在换发
        # 新 instanceId 之后继续把旧实例的成员码推给面板——家人扫的是一张新 hub
        # 从未签发过的码，hub 只能回 code_invalid，而它永不自愈。
        # 与 :373"不落盘以免与 hub 真相分叉"是同一口径的另一半。
        self.member_code = None
        self._member_code_at = 0.0
        self.members = []
        self.owner_masked = None
        # 落盘失败不得让"内存已清空"半途而废：旧写法 _save_identity 抛出 ⇒ 后面的
        # _rereg_streak/last_error 全不执行，且磁盘仍是刚被 401 拒掉的那张凭据，
        # 下一轮 _ensure_registered 的 _load_identity 把它读回来并直接 return 跳过
        # 注册 ⇒ 再 401 → 三次熔断 → **连 HA 重启都读回同一份死身份**，与本函数
        # docstring 承诺的"下次连接必然重新注册"相反。
        self._identity_rejected = True    # 强制下一轮一定重注册（不看磁盘身份）
        try:
            await self._save_identity()
        except Exception as e:  # noqa: BLE001 - 清档失败只降级，不吞掉熔断计数
            self._logger.warning(
                "身份失效后本地清档失败（%s）：本机内存已清，下次连接将强制重新注册"
                "（忽略磁盘上的旧身份）", type(e).__name__)
        self._rereg_streak += 1
        self.last_error = "identity_rejected"
        self._logger.warning(
            "hub 身份失效（%s）：已清空本地身份并将重新注册，绑定码随之换发——"
            "云端注册表重置会同时丢掉绑定关系，请在小程序重新绑定一次", reason)

    async def _session_once(self) -> bool:
        """建一次长连，收消息直到断开；返回 True 表示"干净断开可立即重连"。"""
        await self._ensure_registered()
        session = await self._ensure_session()
        self._logger.info("hub 连接中：%s（凭据 %s）", self.base, cred_brief(self.instance_id))
        async with await self._open_ws(session) as ws:
            self.connected = True
            self.last_error = None
            self._rereg_streak = 0        # 连上过＝云端确实认识当前身份，熔断计数归零
            self._logger.info("hub 已连接（instance=%s）", self.instance_id)
            self._state_dirty.set()          # 上线先全量推一次
            # 上线自检的换码挪进保活 task：那是 HTTP，最坏等 HUB_HTTP_TIMEOUT_S=15s，
            # 挡在接收循环前＝上线首 15s 内下行命令一律不被处理。
            flush = asyncio.ensure_future(self._flush_loop(ws))
            keepalive = asyncio.ensure_future(self._keepalive_loop())
            try:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        data = json.loads(msg.data)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(data, dict) or data.get("t") != "cmd":
                        continue
                    result = await self._handle_cmd(data)
                    await self._send_json(ws, {
                        "t": "cmd_result",
                        "cmdsn": data.get("cmdsn"),
                        "ok": bool(result.get("ok")),
                        "data": result.get("data"),
                        "err": result.get("err"),
                    })
            finally:
                for task in (flush, keepalive):
                    task.cancel()
                await asyncio.gather(flush, keepalive, return_exceptions=True)
        return True

    async def _send_json(self, ws: aiohttp.ClientWebSocketResponse, payload: Dict[str, Any]) -> None:
        """所有出帧的唯一出口：状态上行与命令回执是两个并发 task 写同一条 ws。

        今天靠 aiohttp 非压缩帧的同步 write 侥幸不交错；协商上 permessage-deflate
        或实现变化后就不是了，所以显式串行化。
        """
        async with self._write_lock():
            await ws.send_json(payload)

    async def _keepalive_loop(self) -> None:
        """上线自检 + 周期标脏重推：状态长时间不变时也要刷新 hub 侧 updatedAt。

        否则"设备一直没人动"与"agent 已死"在云端是同一种形态（updatedAt 越来越旧），
        小程序侧无法区分。睡眠按 30s 切片（停机时最多 30s 内让出）。
        换码自检放这里（而不是 `_session_once` 的接收循环之前）：它是 HTTP，
        最坏等 HUB_HTTP_TIMEOUT_S，挡在接收循环前＝上线首 15s 下行命令不被处理。
        """
        await self._renew_bind_code_if_stale()   # 上线自检：过期码当场换新
        while not self._stopping:
            await interruptible_sleep(HUB_KEEPALIVE_S, lambda: self._stopping)
            if self._stopping:
                return
            await self._renew_bind_code_if_stale()
            self._state_dirty.set()

    # ── 状态上行 ──────────────────────────────────────────────────
    def mark_state_dirty(self) -> None:
        """标脏并唤醒上行协程（任一网关的状态变化都要重推**全量**）。"""
        if self._stopping:
            return
        if self._state_dirty is not None:
            self._state_dirty.set()

    def _on_device_status(self, gateway_sn: str, device_sn: str) -> None:  # noqa: ARG002
        """device_manager 状态监听（同步回调）→ 只标脏 + 唤醒上行协程。"""
        self.mark_state_dirty()

    def _resolve_builder(self):
        if self.view_builder is not None:
            return self.view_builder
        try:  # 懒 import：模块本体不依赖 HA，注入缺省时才碰 ws_gateway
            from .ws_gateway import device_ws_view
            return device_ws_view
        except Exception:  # noqa: BLE001
            return None

    def build_state_snapshot(self) -> Tuple[List[Dict[str, Any]], bool]:
        """按 LAN 同源视图构造**全量**状态快照，回 (items, authoritative)。

        小程序云模式按 `gwSn` 分桶渲染网关列表，所以每台网关的设备都必须带自己的
        gwSn；只推第一条＝用户报障的"只添加了一个网关给小程序"。

        `authoritative` 是对 hub 的承诺："这批就是当前全部设备，没在这批里的 sn 已经
        不存在了"——hub 要靠它淘汰已删设备（删了不消失＝用户报的幽灵设备）。承诺一旦
        兑现，漏一台就少一台，所以三种"漏法"分开处理：
        · 设备视图构造失败/回不了 sn：设备确实还在 `manager.devices` 里 ⇒ 回退上一轮的
          成功视图，快照仍然全（首轮没有回退位才判不权威）；
        · 读某个 manager 的 devices/gateway_sn 失败、或 builder 取不到：这批根本看不见
          那台网关 ⇒ 不权威；
        · 一个 manager 都没挂上（条目全在卸载中／启动没完成）：此时的"空"不代表"没有
          设备" ⇒ 不权威。零设备但 manager 在（真把最后一台删了）⇒ 权威空快照，必须上行。
        """
        builder = self._resolve_builder()
        if builder is None:
            return [], False
        items: List[Dict[str, Any]] = []
        fresh: Dict[Any, Dict[str, Any]] = {}
        authoritative = True
        for manager in self._managers:
            try:
                devices = getattr(manager, "devices", {}) or {}
                gateway_sn = getattr(manager, "gateway_sn", "") or ""
            except Exception:  # noqa: BLE001 - 单条目异常不拖垮整批上行
                authoritative = False
                continue
            for dev_sn, dev in list(devices.items()):
                view: Optional[Dict[str, Any]] = None
                try:
                    built = builder(dev_sn, gateway_sn, dev or {})
                    if isinstance(built, dict) and built.get("sn"):
                        view = built
                        # 云通道没有 LAN 那路 device_update 实时推送，锁定模式只能靠
                        # 状态上行带过去——device_ws_view 是 device_list 项视图（不含它），
                        # 这里补上与 LAN `_device_update_payload` 同源的字段。
                        view["windLockMode"] = _attr_int(dev, "wind_lock_mode")
                except Exception as e:  # noqa: BLE001
                    self._logger.debug(
                        "hub 设备视图构造失败（%s），按上一轮视图上报：%s",
                        type(e).__name__, dev_sn)
                if view is None:
                    view = self._last_views.get((gateway_sn, dev_sn))
                if view is None:
                    authoritative = False
                    continue
                items.append(view)
                fresh[(gateway_sn, dev_sn)] = view
        if not self._managers:
            authoritative = False
        self._last_views = fresh
        return items, authoritative

    async def _flush_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while not self._stopping:
            await self._state_dirty.wait()
            self._state_dirty.clear()
            await asyncio.sleep(HUB_STATE_DEBOUNCE_S)
            items, authoritative = self.build_state_snapshot()
            if not authoritative:
                # 宁可不推（下一次状态变化或 5 分钟保活还会再试），也不能把残缺快照
                # 当全量交出去——那会让 hub 把没看见的设备判成已删除。
                if not self._snapshot_degraded:
                    self._snapshot_degraded = True
                    self._logger.warning(
                        "hub 状态快照不完整（网关条目未挂上或视图构造失败），"
                        "本轮不上行；恢复前云端设备列表停在上一批")
                continue
            self._snapshot_degraded = False
            try:
                await self._send_json(ws, {"t": "state", "items": items})
            except Exception as e:  # noqa: BLE001 - 发送失败交给外层重连
                self._logger.debug("hub 状态上行失败：%s", type(e).__name__)
                return

    # ── 命令下行 ──────────────────────────────────────────────────
    async def _handle_cmd(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        if msg.get("action") != "control":
            return {"ok": False, "err": "unknown_action"}
        dev_sn = msg.get("sn")
        # 审计 2026-09-30 A-2：`or {}` 只挡假值——hub/小程序发来 `"params":[]`
        # /`"params":"x"`/`"params":123` 这类**真值非 dict** 时原样透传，
        # params.get 抛 AttributeError 冒泡出 _session_once（接收循环 :984 无 try）
        # ⇒ 被外层当"连接异常"，整条云长连断开重连（会话状态与 _rereg_streak 随
        # 重连重置），且 cmd_result 永不回，小程序干等。LAN 同款入口有闸
        # （ws_gateway.handle_json_message 的 isinstance(msg, dict)），云通道此前
        # 只校验了 data 是 dict（:982）——两条通道判据必须一致（本仓纪律）。
        params = msg.get("params")
        if not isinstance(params, dict):
            return {"ok": False, "err": "invalid_params"}
        value_s = validate_control_params(params.get("attribute"), params.get("value"))
        if not isinstance(dev_sn, str) or not dev_sn or value_s is None:
            return {"ok": False, "err": "invalid_params"}
        if self.control_fn is None:
            return {"ok": False, "err": "control_unavailable"}
        try:
            ok = await self.control_fn(dev_sn, params.get("attribute"), value_s)
        except Exception as e:  # noqa: BLE001
            self._logger.warning("hub 命令执行异常：%s", type(e).__name__)
            return {"ok": False, "err": "control_failed"}
        # 语义与 LAN 一致：ok = 004 已发布到 broker（设备是否执行靠状态上报）
        return {"ok": bool(ok), "data": {"published": bool(ok)}}

    # ── 视图（供插件页展示）──────────────────────────────────────
    def gateway_summary(self) -> List[Dict[str, Any]]:
        """当前聚合到的网关清单（面板要显示"N 个网关 · M 个子设备"）。"""
        out: List[Dict[str, Any]] = []
        for m in self._managers:
            try:
                sn = getattr(m, "gateway_sn", "") or ""
                count = len(getattr(m, "devices", {}) or {})
            except Exception:  # noqa: BLE001 - 视图绝不因取字段抛错
                continue
            if sn:
                out.append({"sn": sn, "deviceCount": count})
        return out

    def _first_gateway_sn(self) -> str:
        for m in self._managers:
            try:
                sn = getattr(m, "gateway_sn", "") or ""
            except Exception:  # noqa: BLE001
                continue
            if sn:
                return sn
        return ""

    def _members_with_alias(self) -> List[Dict[str, Any]]:
        """把本机称呼并进成员行（**不改 self.members**——那份是 hub 原样透传的关系真相）。"""
        out: List[Dict[str, Any]] = []
        for m in self.members:
            if not isinstance(m, dict):
                continue
            row = dict(m)
            alias = self.member_aliases.get(str(m.get("mid") or ""))
            if alias:
                row["alias"] = alias
            out.append(row)
        return out

    def status_view(self) -> Dict[str, Any]:
        """给插件页/排障用：**不回显 secret**；绑定码本就是给用户看的，可回显。"""
        gateways = self.gateway_summary()
        gateway_sn = gateways[0]["sn"] if gateways else ""
        expires_in = self.bind_code_expires_in()
        members = self._members_with_alias()
        return {
            "connected": bool(self.connected),
            "instanceId": self.instance_id,
            "bindCode": self.bind_code,
            "bindCodeExpiresIn": expires_in,
            "bindCodeExpired": bool(self.bind_code) and expires_in <= 0,
            # TTL 也让面板拿权威值：hub 改了有效期后，"10 分钟内有效"这类硬编文案就是假话
            "bindCodeTtlS": self.bind_code_ttl_s(),
            "gatewaySn": gateway_sn,
            "gateways": gateways,
            "hub": self.base,
            "lastError": self.last_error,
            "lastOpError": self.last_op_error,
            # 家庭成员（v1.7.47）：members 里只有掩码与 mid（hub 从不回完整 openid）；
            # alias 是 v1.7.54 本机面板侧起的称呼，云端没有这个字段
            "memberCode": self.member_code,
            "memberCodeExpiresIn": self.member_code_expires_in(),
            "memberCodeExpired": bool(self.member_code) and self.member_code_expires_in() <= 0,
            "memberCodeTtlS": self.member_code_ttl_s(),
            "members": members,
            "membersCount": len(members),
            "membersMax": self.members_max or HUB_MEMBERS_MAX,
            "membersSupported": bool(self.members_supported),
            "ownerMasked": self.owner_masked,
        }

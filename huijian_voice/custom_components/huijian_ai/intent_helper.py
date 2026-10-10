import logging
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

import voluptuous as vol
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent

_LOGGER = logging.getLogger(__name__)

DOMAIN_ALIASES: dict[str, str | list[str]] = {
    "window": ["cover", "button"],
    "windows": ["cover", "button"],
    "curtain": "cover",
    "curtains": "cover",
    "blind": "cover",
    "blinds": "cover",
    "shutter": "cover",
    "shutters": "cover",
    "plug": "switch",
    "plugs": "switch",
    "outlet": "switch",
    "outlets": "switch",
    "fan": "fan",
    "fans": "fan",
    "ac": "climate",
    "air_conditioner": "climate",
    "heater": "climate",
    "lamp": "light",
    "lamps": "light",
    "door": ["lock", "cover", "button"],
    "doors": ["lock", "cover", "button"],
    "tv": "media_player",
    "speaker": "media_player",
}


def validate_slots_safely(
    handler: "intent.IntentHandler",
    intent_obj: "intent.Intent",
    feature: str,
) -> "tuple[dict | None, dict | None]":
    """HA core `async_validate_slots` 的唯一安全入口（v1.0.97，VM HAOS 2026.9.2 实锤）。

    core 在该函数里有两种裸抛形态，经 REST /api/intent/handle 直接炸成 HTTP 500
    纯文本（2026-09-19 全量枚举 11 面复现）：
    ① slot_schema=None → 对 None 迭代抛 AttributeError（HuijianGetLiveContext）；
    ② Required 键缺失/值不合型 → 抛 vol.Invalid（"required key not provided"，
       TurnDeviceOn/Off、PauseDevice、SetDeviceMode、AdjustDeviceAttribute、
       场景 create/trigger/delete、自动化 create/delete/update）。
    500 被 ha_client 洗成「HA 内部错误(500)」→ zh_error 指路"重启/确认安装"——
    裸「关闭」误诊"集成没生效"三日悬案即此链（本函数收口）。同 intent_turn
    「永不抛」铁律：失败折叠为可复述的结构化 dict。
    IntentHandleError 是 HA 正规失败通道，透传不二次折叠（M5 窗控同口径）。

    Returns:
        (slots, None) 校验通过；(None, error_dict) 校验炸出未捕获异常。
    """
    try:
        return handler.async_validate_slots(intent_obj.slots), None
    except intent.IntentHandleError:
        raise
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("%s 槽位校验未捕获异常，如实失败: %s", feature, err)
        return None, {"success": False, "error": f"{feature} 参数校验未通过: {err}"}


def normalize_targets_device_names(targets: list[dict]) -> list[dict]:
    """归一化 targets 中所有设备名称里的中文数字。

    Converts Chinese numerals (一二三...十) in device names to Arabic numerals.
    Improves entity matching when ASR recognizes "五号" but HA entity is "5号".

    Args:
        targets: List of target dicts with 'devices' containing 'name' fields.

    Returns:
        New list of targets with normalized device names.
    """
    from .intent_window_const import normalize_chinese_numbers

    result = []
    for target in targets:
        target = dict(target)
        devices = target.get("devices", [])
        if devices:
            new_devices = []
            for d in devices:
                d = dict(d)
                if d.get("name"):
                    d["name"] = normalize_chinese_numbers(d["name"])
                new_devices.append(d)
            target["devices"] = new_devices
        result.append(target)
    return result


def _expand_domains(domains: list[str]) -> list[str]:
    expanded = list(domains)
    for d in domains:
        alias = DOMAIN_ALIASES.get(d)
        if alias:
            aliases = alias if isinstance(alias, list) else [alias]
            for a in aliases:
                if a not in expanded:
                    expanded.append(a)
    return expanded


def target_parameter_type():
    return vol.All(
        cv.ensure_list,
        [
            vol.Schema(
                {
                    vol.Optional("devices"): vol.All(
                        cv.ensure_list,
                        [
                            vol.Schema(
                                {
                                    vol.Required("domains"): vol.All(
                                        cv.ensure_list, [cv.string]
                                    ),
                                    vol.Optional("name"): cv.string,
                                }
                            )
                        ],
                    ),
                    vol.Optional("area"): cv.string,
                }
            )
        ],
    )


def get_entity_name(entity_entry: er.RegistryEntry, state: State) -> str:
    if len(entity_entry.aliases) > 0:
        alias = list(entity_entry.aliases)[0]
        name = str(alias) if alias is not None else state.name
        if "ComputedNameType" not in name:
            return name

    if isinstance(entity_entry.name, str):
        name = entity_entry.name if entity_entry.name else state.name
        if "ComputedNameType" not in name:
            return name

    friendly = state.attributes.get("friendly_name", "")
    if friendly and "ComputedNameType" not in friendly:
        return friendly
    if "ComputedNameType" not in state.name:
        return state.name
    return state.entity_id


@dataclass
class AreaInfo:
    name: str
    id: str
    # v1.1.27：别名**另存集合**。旧版把 aliases 展平进同一个 list 再取 [0]，
    # 而 HA 的 aliases 是 set（无序）⇒ name 可能拿到别名，本模块所有
    # `entity_area.name != 口述区域名` 的比较随之随机丢候选（同一句话时对时错）。
    aliases: frozenset[str] = frozenset()

    def matches(self, area_name: str) -> bool:
        """区域名命中判定：注册名或任一别名都算（用户口述两种都要认）。"""
        want = _norm_area_name(area_name)
        if not want:
            return False
        if want == _norm_area_name(self.name):
            return True
        return any(want == _norm_area_name(a) for a in self.aliases)


def _norm_area_name(value: str | None) -> str:
    """区域名比对归一：去空白与「的」、小写（注册名常带尾空格、口述常带「的」）。"""
    return (str(value or "").replace(" ", "").replace("\u3000", "")
            .replace("的", "").lower())


def _area_info(area, area_id: str) -> AreaInfo:
    """AreaInfo 唯一构造入口：name 恒取注册名，别名进集合（v1.1.27）。"""
    aliases = getattr(area, "aliases", None) or ()
    return AreaInfo(
        name=str(getattr(area, "name", "") or ""),
        id=area_id,
        aliases=frozenset(str(a) for a in aliases),
    )


def get_entity_area(
    hass: HomeAssistant, entity_entry: er.RegistryEntry
) -> AreaInfo | None:
    area_registry = ar.async_get(hass)
    device_registry = dr.async_get(hass)
    if entity_entry.area_id and (
        area := area_registry.async_get_area(entity_entry.area_id)
    ):
        return _area_info(area, entity_entry.area_id)
    elif entity_entry.device_id and (
        device := device_registry.async_get(entity_entry.device_id)
    ):
        if device.area_id and (area := area_registry.async_get_area(device.area_id)):
            return _area_info(area, device.area_id)
    return None


@dataclass
class EntityInfo:
    name: str
    area: AreaInfo | None
    state: State
    entity: er.RegistryEntry
    on_off: Literal["on", "off"]

    @property
    def area_name(self) -> str:
        if self.area:
            return self.area.name
        return ""

    @property
    def area_id(self) -> str:
        if self.area:
            return self.area.id
        return ""


class HaDeviceItem(TypedDict):
    domains: list[str]
    name: str | None


class HaTargetItem(TypedDict):
    area: str | None
    devices: list[HaDeviceItem]


@dataclass
class StateWithAreaConstraint:
    states: list[State]
    unset_area_constraint: bool
    # v1.1.27：本命中组来自哪个 device.name（逐目标名称过滤用；旧版只认第一个）
    requested_name: str | None = None


def _entity_area_id(hass: HomeAssistant, entity_id: str) -> str | None:
    """实体所属区域：实体自身 area_id → 回落到设备 area_id（与 HA 同口径）。"""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return None
    if entry.area_id:
        return entry.area_id
    if entry.device_id:
        dev = dr.async_get(hass).async_get(entry.device_id)
        if dev is not None:
            return dev.area_id
    return None


def _contains_name_states(hass: HomeAssistant, name: str, area_name: str | None,
                          domains) -> list[State]:
    """名称「包含」回捞（v1.1.25 办公 .91 英文句实锤根修）。

    病灶：HA 的 target 名称匹配是**词级**——实体叫「射灯」、用户说「灯」
    （英文 'turn on the office light' 经双语桥也落到「灯」）时严格匹配必 miss ⇒
    「没找到符合条件的设备」；中文同形只是常被 klar 接走，英文没有兜底。
    保守三约束：**同区域（若给了）+ 同域 + 名称包含**；区域名注册表里不认识 ⇒
    一律返回空（宁如实 miss，绝不跨区抓设备）。仅供严格匹配为空后的回捞使用。"""
    if not name:
        return []
    area_id = None
    if area_name:
        area = ar.async_get(hass).async_get_area_by_name(area_name)
        if area is None:
            return []
        area_id = area.id
    low = name.lower()
    out: list[State] = []
    for state in hass.states.async_all(domains or None):
        if area_id is not None and _entity_area_id(hass, state.entity_id) != area_id:
            continue
        fname = str(state.attributes.get("friendly_name") or state.name or "")
        if low in fname.lower():
            out.append(state)
    return out


async def _match_with_constraints(
    hass: HomeAssistant,
    targets: list[HaTargetItem],
    assistant: str | None,
) -> tuple[list[StateWithAreaConstraint], set[str]]:
    """Match targets with given assistant constraint. Returns states + expanded domains."""
    found_states: list[StateWithAreaConstraint] = []
    all_expanded_domains: set[str] = set()

    for target in targets:
        # 2026-09-12 三端深挖实锤两坑（core 2025.1.0 源码核定）：
        # ①area-only 目标（加载项空间化曾发 {"area": x} 无 devices 键）→ 旧写法
        #   target["devices"] 必 KeyError；改 .get() 兜底并补一个"任意域"设备。
        # ②domains=[] 会把空列表直接传给 HA async_match_targets，而
        #   `hass.states.async_all([])` 返回空表 → 立刻 MatchFailedReason.DOMAIN
        #   （实测：通道/插座/开关/大门/音箱 等 14 个常见设备名 domain_hint 为空，
        #   真机"没找到设备"）。空列表语义必须是"不按域过滤"=None。
        devices = target.get("devices") or [{"domains": []}]
        for device in devices:
            # v1.1.27：area 空串（LLM/REST 会传，加载项按 targets.py 注释不再写）
            # ＝「未点名区域」，绝不能当"有区域约束"用——旧版把它折成
            # unset_area_constraint=True，在建候选时把挂区域下的实体整批剔掉。
            raw_area = target.get("area")
            area_name = raw_area or None
            requested_name = str(device.get("name") or "").strip() or None
            expanded_domains = _expand_domains(device.get("domains") or [])
            all_expanded_domains.update(expanded_domains)
            # v1.2.11「(区域)所有设备」= 本区域**全部**可开关设备，判据换成区域证据。
            # 病灶（办公 .91 现场）：`async_match_targets(area_name=…)` 对"区域"只认
            # HA 注册表归属，而网关那两台开窗器整台设备没挂任何区域 ⇒ 一条都不返回，
            # 「关闭办公室所有设备」只剩空调/射灯，播「已关闭」而窗纹丝不动；同一间房
            # 说「所有窗户」却能成——窗控那条从来走的是 `intent_window_const` 的三档
            # 区域证据。两条车道对"区域"必须同一个口径，否则用户在同一间房里说
            # 「所有窗」和「所有设备」会拿到两个答案（本仓"判据面与执行面同源"旧账）。
            # 触发面**只此一种**：空名 + 点名区域 + 域集覆盖可开关白名单
            # （＝加载项「(区域)所有设备」批量道的形状，与 core
            # `executor._bulk_all_devices_slot` 的兼容判据同源）。
            # 为什么不能只看"空名+区域"（第七轮审计自查出来的越界）：LLM/klar 也会给
            # `{area:客厅, devices:[{name:"", domains:["light"]}]}` 这种**单域空名**目标，
            # 走进证据展开就会把整台没登记区域的灯（现场真有：走廊感应灯）算成
            # "客厅的灯"一起开——那是拿批量判据去接具名句，比原病更危险。单域/具名句
            # 一律留在 HA 严格匹配那侧，逐字节零扰动。
            # 展开为空（这真没这间房／全被确凿别区剔除／注册表形态拿不到）⇒ 落回原支，
            # 与旧行为一致地如实查无——绝不因为换了判据就冒按。
            if _bulk_evidence_eligible(requested_name, raw_area, expanded_domains):
                from .intent_window_const import find_bulk_entities_by_area
                bulk_eids = find_bulk_entities_by_area(
                    hass, area_name, expanded_domains)
                if bulk_eids:
                    _LOGGER.info("Bulk area-evidence matched %d entities for area %r",
                                 len(bulk_eids), area_name)
                    found_states.append(
                        StateWithAreaConstraint(
                            states=[s for s in (hass.states.get(e) for e in bulk_eids)
                                    if s is not None],
                            unset_area_constraint=False,
                            requested_name=None,
                        )
                    )
                    continue
            match_constraints = intent.MatchTargetsConstraints(
                name=device.get("name"),
                area_name=area_name,
                domains=expanded_domains or None,
                assistant=assistant,
                single_target=False,
                allow_duplicate_names=True,
            )
            _LOGGER.info("Match constraints (assistant=%s): %s", assistant, match_constraints)
            match_result = intent.async_match_targets(hass, match_constraints)
            if not match_result.is_match:
                # v1.1.25：「包含」回捞（泛称/英文双语桥必 miss 形态；办公 .91 实锤
                # 'turn on the office light' → 桥成「灯」→ 实体叫「射灯」⇒ 严格匹配空）。
                fallback = _contains_name_states(
                    hass, str(device.get("name") or ""), area_name,
                    expanded_domains or None)
                if fallback:
                    _LOGGER.info(
                        "Contains-name fallback matched %d entities for %r",
                        len(fallback), device.get("name"))
                    found_states.append(
                        StateWithAreaConstraint(
                            states=fallback,
                            unset_area_constraint=(raw_area == ""),
                            requested_name=requested_name,
                        )
                    )
                continue
            found_states.append(
                StateWithAreaConstraint(
                    states=match_result.states,
                    unset_area_constraint=(raw_area == ""),
                    requested_name=requested_name,
                )
            )

    return found_states, all_expanded_domains


OWN_INTEGRATION_DOMAIN = "huijian_ai"

# v1.2.10「(区域)所有设备」＝一台设备只动它的电源位（2026-10-08 用户拍板："空调只需要
# 关闭空调电源"）。本机注册表实测：办公室空调名下 16 条实体，7 条是功能位 switch
# （ECO/上下摆风/左右摆风/辅热/干燥/睡眠/提示音/空调开关），且它们的 `entity_category`
# 与 `capabilities` **都是 None** ⇒ 拿"类别/主实体"位判不了，只有**域**这一条判据在这批
# 生态上可信。域优先级＝家电本体在前、`switch` 垫后（智能插座这类"本来就是一路开关"
# 的设备仍会被留）。覆盖面必须与 core 的 `BULK_TOGGLEABLE_DOMAINS` 逐字相等（有钉）。
_BULK_DOMAIN_PRIORITY = ("climate", "water_heater", "humidifier", "vacuum",
                         "cover", "fan", "light", "media_player", "switch")
# 配置/诊断实体（指示灯、信息按钮、配对键、升级位）不是"一台设备"，批量面一律不碰。
_BULK_SKIP_CATEGORIES = frozenset({"config", "diagnostic"})


def _bulk_evidence_eligible(requested_name, raw_area, expanded_domains) -> bool:
    """这条目标是不是「(区域)所有设备」的批量面（只有它才走区域证据展开）。

    三件缺一不可：**空名**（用户没点名某台/某类）＋ **点名区域**（批量必须有房间硬
    约束，无区域的全屋句归 HA 严格匹配那侧）＋ **域集覆盖可开关白名单**（这就是
    「所有设备」的形状本身）。少了第三件，LLM/klar 的单域空名目标
    （`{area:客厅, name:"", domains:["light"]}`）会被拖进展开，把整台没登记区域的灯
    当成"客厅的灯"一起开——拿批量判据接具名句，比原病更危险（第七轮审计自查）。
    `_BULK_DOMAIN_PRIORITY` 与 core 的 `BULK_TOGGLEABLE_DOMAINS` 等值由
    tests/test_v1210_area_bulk.py 的字面量钉守着，所以这里不需要再引 core。
    纯函数、永不抛：判据故障 ⇒ False（走原路，保守）。
    """
    try:
        return (requested_name is None and bool(raw_area)
                and set(str(d) for d in (expanded_domains or ()))
                >= {str(d) for d in _BULK_DOMAIN_PRIORITY})
    except Exception:                                        # noqa: BLE001
        return False


def _is_bulk_auxiliary(entity_entry) -> bool:
    """实体是否只是某台设备的"零件位"（指示灯、信息按钮、配对键、升级位）。

    HA 的 `entity_category` 是 `str` 枚举，序列化后可能是裸串 ⇒ 先取 `.value`
    再比。**取不到就算不是零件位**：这条只用来在批量展开里少按几个开关，
    宁可多动一个指示灯，也不能凭空剔掉客户能点名的实体。永不抛。
    """
    try:
        cat = getattr(entity_entry, "entity_category", None)
        if cat is None:
            return False
        return str(getattr(cat, "value", cat)) in _BULK_SKIP_CATEGORIES
    except Exception:                                        # noqa: BLE001
        return False


def _bulk_one_per_device(entities: list[EntityInfo]) -> list[EntityInfo]:
    """按 device 折叠：一台设备只留优先级最高的那条实体，其余都是它的"零件"。

    同优先级按 entity_id 定序——结果必须与注册表遍历序无关（否则同一句话今天关这台、
    明天关那台）。无 device_id 的实体各自成组（不参与折叠，宁多不少）。保持入参原序。
    """
    best: dict[str, tuple] = {}
    for e in entities:
        dom = str(e.state.entity_id).split(".", 1)[0]
        rank = (_BULK_DOMAIN_PRIORITY.index(dom)
                if dom in _BULK_DOMAIN_PRIORITY else len(_BULK_DOMAIN_PRIORITY))
        dev = getattr(e.entity, "device_id", None) or f"@{e.state.entity_id}"
        key = (rank, e.state.entity_id)
        cur = best.get(dev)
        if cur is None or key < cur[0]:
            best[dev] = (key, e)
    keep = {id(v[1]) for v in best.values()}
    return [e for e in entities if id(e) in keep]


def _is_own_integration(entity_entry, hass: HomeAssistant | None = None) -> bool:
    """实体是否属于**本集成自己**（语音卫星：麦克风开关、媒体播放器、连续对话…）。

    两条独立证据任一命中即真：注册表 `platform`、或 `config_entry_id` 落在本集成的
    config entry 里（老 HA 无 platform 字段时仍判得准）。**判不了就算"不是自己人"**
    ——这条只用来在批量展开里少带几台，宁可多带一台也不凭空剔客户的设备。永不抛。
    """
    try:
        if entity_entry is None:
            return False
        if getattr(entity_entry, "platform", None) == OWN_INTEGRATION_DOMAIN:
            return True
        cid = getattr(entity_entry, "config_entry_id", None)
        if not cid or hass is None or not hasattr(hass, "config_entries"):
            return False
        return cid in {e.entry_id
                       for e in hass.config_entries.async_entries(OWN_INTEGRATION_DOMAIN)}
    except Exception:                                        # noqa: BLE001
        return False


def _build_entities_for_item(
    hass: HomeAssistant,
    item: StateWithAreaConstraint,
    # er.Registry 在 HA 2026.x 已移除（仅剩 EntityRegistry）——注解是运行期求值的，
    # 挂错名字 = import 即炸、全集成瘫（2026-09 E2E 实证）。真栈 E2E 兼做冒烟。
    entity_registry: er.EntityRegistry,
) -> list[EntityInfo]:
    """单个 target×device 命中组的候选实体（v1.1.27：逐组构建，供逐目标过滤）。"""
    candidate_entities: list[EntityInfo] = []
    # P3 前置（10-10）：卫星自己绝不进批量。下面那道 `_is_own_integration` 只认归属本集成
    # （platform/config_entry==huijian_ai），而现网的麦克风开关/连续对话/回声消除是
    # **ESPHome 侧实体** ⇒ 够不到。补一条与归属无关的**设备级**判据，判据单源在
    # `intent_window_const.satellite_self_device_ids`（设备面批量道用的是同一个）。
    # 只作用于空名批量目标：点名「关掉小智音箱」这类具名句照旧能动它。
    self_devs: set = set()
    if not item.requested_name:
        from .intent_window_const import satellite_self_device_ids
        self_devs = satellite_self_device_ids(hass) or set()

    for state in item.states:
        if state.state == "unavailable":
            continue
        entity_entry = entity_registry.async_get(state.entity_id)
        if not entity_entry:
            continue
        # v1.2.10「(区域)所有设备」绝不关语音卫星自己（用户明令：不要关 esp 相关）。
        # 卫星的麦克风开关/媒体播放器天然落在可开关域里，客户把它登记在哪个房间，
        # 那句「关闭客厅所有设备」就会把它自己关掉——下一句再没人听得见（自杀式静音）。
        # 只挡**批量展开**（空名目标）；用户点名「关闭小智音箱」这类具名句照旧能动它。
        if (not item.requested_name
                and (_is_own_integration(entity_entry, hass)
                     or (self_devs and str(getattr(entity_entry, "device_id", "") or "")
                         in self_devs))):
            _LOGGER.info("Bulk target skips own-assistant entity: %s", state.entity_id)
            continue
        # v1.2.10「(区域)所有设备」不碰设备零件位（用户拍板"空调只需要关闭空调电源"）：
        # 指示灯/信息按钮/配对键/升级位都是 config·diagnostic 类，它们不是一台设备。
        # 只挡批量展开（空名目标）——点名「打开射灯指示灯」这类具名句照旧能动。
        if not item.requested_name and _is_bulk_auxiliary(entity_entry):
            _LOGGER.info("Bulk target skips auxiliary entity: %s", state.entity_id)
            continue
        entity_area = get_entity_area(hass, entity_entry)
        # v1.1.27：旧版此处 `if item.unset_area_constraint and entity_area: continue`
        # 把"空 area"当成"有区域约束"用——LLM/REST 传 area:""（=未点名区域）时，
        # 挂在区域下的实体被整批剔除（"打开灯"直接把有区域的灯全丢）。空串语义
        # 是"未点名区域"，没有任何过滤依据：HA 匹配层已按区域过滤，加载项亦不再
        # 写 ""（core/nlu/targets.py 空 area 注释）。
        entity_name = get_entity_name(entity_entry, state)
        candidate_entities.append(
            EntityInfo(
                name=entity_name,
                area=entity_area,
                state=state,
                entity=entity_entry,
                on_off="off" if state.state == "off" else "on",
            )
        )

    # v1.2.10 批量展开收到"一台设备一条"：办公室空调名下 8 条可开关实体（本体 +
    # ECO/摆风/辅热/干燥/睡眠/提示音/电源开关），逐条按下去等于替客户把遥控器摸了一遍
    # ——他只要"关闭空调"。具名目标（「灯」「射灯」）不折叠：那是用户自己圈的范围。
    # **同域那一摊也不折叠**：整组只有一个域时（klar/LLM 给「开灯」传 name="" +
    # domains=[light]），一台双路灯的两条 light 通道就是客户要的两盏，谁都不该被顶掉；
    # 只有跨域混装（空调本体+功能位、插座+指示灯）才存在"这条是那台的零件"的判据。
    if not item.requested_name and len({str(e.state.entity_id).split(".", 1)[0]
                                        for e in candidate_entities}) > 1:
        return _bulk_one_per_device(candidate_entities)
    return candidate_entities


def _build_candidate_entities(
    hass: HomeAssistant,
    found_states: list[StateWithAreaConstraint],
    entity_registry: er.EntityRegistry,
) -> list[EntityInfo]:
    """Build candidate EntityInfo list from matched states."""
    return [
        entity
        for item in found_states
        for entity in _build_entities_for_item(hass, item, entity_registry)
    ]


def _build_candidate_groups(
    hass: HomeAssistant,
    found_states: list[StateWithAreaConstraint],
    entity_registry: er.EntityRegistry,
) -> list[list[EntityInfo]]:
    """逐命中组的候选（与 found_states 同序同长）——v1.1.27 逐目标名称过滤用。"""
    return [_build_entities_for_item(hass, item, entity_registry)
            for item in found_states]


def _narrow_by_name(
    hass: HomeAssistant,
    group: list[EntityInfo],
    requested_name: str | None,
) -> list[EntityInfo]:
    """按**本组** device.name 收窄候选（档序=旧版全局过滤：精确名→前缀→设备名→
    entity_id 子串；全档不中时原样返回，与旧版"宁宽不误剔"同口径）。

    v1.1.27：旧版只取第一个 device.name 做**全局**过滤——多目标句「关灯和空调」
    里"灯"会把"空调"整组静默剔掉却照回 success。改为逐组过滤后合并，每组只认
    自己的名字。
    """
    if not group or not requested_name:
        return group
    name_lower = requested_name.lower().strip()

    exact_matches = [e for e in group if (e.name or "").lower() == name_lower]
    if exact_matches:
        _LOGGER.info(
            "Exact name match found: %d entities (filtered from %d)",
            len(exact_matches), len(group),
        )
        return exact_matches

    # 无精确匹配时，尝试前缀匹配（如 name='窗户' 匹配 '2号测试窗户' 等）
    prefix_matches = [
        e for e in group if (e.name or "").lower().startswith(name_lower)
    ]
    if prefix_matches:
        _LOGGER.info(
            "Prefix name match found: %d entities (filtered from %d)",
            len(prefix_matches), len(group),
        )
        return prefix_matches

    # ── 设备名匹配 ──
    # 当实体名不匹配请求名时（如 has_entity_name=True 的网关按钮，
    # 实体名 "开启" vs 设备名 "开窗器 01"），通过设备注册表查找
    dev_reg = dr.async_get(hass)
    device_matches = []
    for e in group:
        entity_entry = e.entity
        if not entity_entry.device_id:
            continue
        device = dev_reg.async_get(entity_entry.device_id)
        if not device:
            continue
        device_name = device.name_by_user or device.name or ""
        if (
            name_lower in device_name.lower()
            or device_name.lower() in name_lower
        ):
            device_matches.append(e)
    if device_matches:
        _LOGGER.info(
            "Device name match found: %d entities (filtered from %d via device name)",
            len(device_matches), len(group),
        )
        return device_matches

    # ── Entity ID 子串匹配 ──（最终兜底）
    # 当设备名也匹配不上时（如实体无 device_id），
    # 尝试用请求名匹配 entity_id（如 entity_id 含设备标识）
    entity_id_matches = [
        e for e in group
        if getattr(getattr(e, "entity", None), "entity_id", None)
        and name_lower in e.entity.entity_id.lower()
    ]
    if entity_id_matches:
        _LOGGER.info(
            "Entity ID match found: %d entities (filtered from %d via entity_id)",
            len(entity_id_matches), len(group),
        )
        return entity_id_matches
    return group


async def match_intent_entities(
    intent_obj: intent.Intent, targets: list[HaTargetItem]
) -> tuple[dict | None, list[EntityInfo] | None]:
    """Match entities by request parameters."""
    hass = intent_obj.hass
    entity_registry = er.async_get(hass)

    # 第一轮：严格匹配（带 assistant）
    found_states, domains1 = await _match_with_constraints(
        hass, targets, intent_obj.assistant
    )
    all_expanded_domains = domains1.copy()
    groups = _build_candidate_groups(hass, found_states, entity_registry)
    candidate_entities = [e for group in groups for e in group]

    # 第二轮：无 assistant 回退
    if len(candidate_entities) == 0:
        _LOGGER.warning(
            "Strict match failed (assistant=%s), trying fallback without assistant filter",
            intent_obj.assistant,
        )
        found_states2, domains2 = await _match_with_constraints(
            hass, targets, None
        )
        all_expanded_domains.update(domains2)
        found_states = found_states2
        groups = _build_candidate_groups(hass, found_states2, entity_registry)
        candidate_entities = [e for group in groups for e in group]

    # ── 精确名称优先过滤（v1.1.27：逐目标/逐 device 各自过滤后再合并）──
    # 旧版只取**第一个** device.name 做全局过滤：多目标句「关掉灯和空调」里"灯"
    # 会把"空调"整组静默剔掉却照回 success（点名两台只落地一台，加载项侧看不出来）。
    # 现按命中组各用自己 device.name 收窄，再按序合并去重。
    requested_name = None
    for target in targets:
        for device in target.get("devices") or []:   # area-only 目标无 devices 键
            name = device.get("name")
            if name:
                requested_name = name
                break
        if requested_name:
            break

    if groups:
        narrowed: list[EntityInfo] = []
        seen_entity_ids: set[str] = set()
        for item, group in zip(found_states, groups):
            for entity_info in _narrow_by_name(hass, group, item.requested_name):
                entity_id = entity_info.state.entity_id
                if entity_id in seen_entity_ids:
                    continue
                seen_entity_ids.add(entity_id)
                narrowed.append(entity_info)
        if narrowed:
            candidate_entities = narrowed

    # ── 设备注册表级兜底（第5级） ──
    # 前4级匹配全部失败（如按钮实体名"开窗器 开启"完全不包含
    # 请求的设备名"2号测试窗"），HA 的 async_match_targets 按
    # 实体名匹配完全找不到结果。此时通过设备注册表按设备名查找。
    if len(candidate_entities) == 0 and requested_name:
        _LOGGER.info(
            "Device registry fallback: name='%s', expanded_domains=%s",
            requested_name,
            all_expanded_domains,
        )
        dev_reg = dr.async_get(hass)
        ent_reg = er.async_get(hass)

        name_lower = requested_name.lower().strip()
        matched_device_ids: set[str] = set()
        # v1.0.34: devices 以映射用已弃用（2027.9 硬失效，HA 现网警告点名本
        # 集成）；DeferredMapping 直接迭代即得 entries。
        for device_entry in dev_reg.devices:
            device_display = (
                device_entry.name_by_user or device_entry.name or ""
            ).lower()
            if device_display and (
                name_lower in device_display or device_display in name_lower
            ):
                matched_device_ids.add(device_entry.id)

        if matched_device_ids:
            # 移植商店仓 369f8ea（2026-06-17）：回退匹配同样遵守 area 约束，
            # 否则"把客厅的灯打开"在设备名命中的实体上会跨区域全开。
            requested_area = None
            for t in targets:
                a = t.get("area")
                if a:
                    requested_area = a
                    break
            for entity_entry in list(ent_reg.entities.values()):
                if entity_entry.device_id not in matched_device_ids:
                    continue
                if (
                    all_expanded_domains
                    and entity_entry.domain not in all_expanded_domains
                ):
                    continue
                # 回退匹配时同样遵守 area 约束（v1.1.27：名或**别名**都算命中——
                # 旧版严格 != 本名比较，区域名一旦被别名顶替就把候选全丢）
                if requested_area:
                    entity_area = get_entity_area(hass, entity_entry)
                    if entity_area and not entity_area.matches(requested_area):
                        continue
                state = hass.states.get(entity_entry.entity_id)
                if not state or state.state == "unavailable":
                    continue
                entity_area = get_entity_area(hass, entity_entry)
                entity_name = get_entity_name(entity_entry, state)
                on_off = "off" if state.state == "off" else "on"
                entity_info = EntityInfo(
                    name=entity_name,
                    area=entity_area,
                    state=state,
                    entity=entity_entry,
                    on_off=on_off,
                )
                _LOGGER.info("Device registry fallback entity: %s", entity_info)
                candidate_entities.append(entity_info)

    # ── 实体显示名子串兜底（第6级，2026-09 真栈 E2E 实锤）──
    # MQTT/z2m 实体的注册表 name 常为 None，用户起的中文名只活在 friendly_name
    # 组合串里（如设备 "TSL2011" + 实体 "射灯" → state.name "TSL2011 射灯"）。
    # HA 核心层是 strip+casefold 等值匹配（2026.x 无子串档），第5级又只看设备名，
    # 两头漏空 → "射灯" 报 No available devices found。此级按 friendly_name 子串
    # 兜底；带区域请求时必须同区域（防跨房间过匹配），无区域时即全屋该域内命中。
    if len(candidate_entities) == 0 and requested_name:
        name_lower6 = requested_name.lower().strip()
        area_req = next((t.get("area") for t in targets if t.get("area")), None)
        for state in hass.states.async_all(list(all_expanded_domains) or None):
            if state.state == "unavailable":
                continue
            if name_lower6 not in (state.name or "").lower():
                continue
            entity_entry = entity_registry.async_get(state.entity_id)
            if entity_entry is None or entity_entry.hidden_by or entity_entry.disabled_by:
                continue
            entity_area = get_entity_area(hass, entity_entry)
            # v1.1.27：与上面第 5 级同一判据——区域**名或别名**都算命中
            if area_req and (
                entity_area is None or not entity_area.matches(area_req)
            ):
                continue
            candidate_entities.append(
                EntityInfo(
                    name=get_entity_name(entity_entry, state),
                    area=entity_area,
                    state=state,
                    entity=entity_entry,
                    on_off="off" if state.state == "off" else "on",
                )
            )
            _LOGGER.info("Friendly-name fallback entity: %s", state.entity_id)

    if len(candidate_entities) == 0:
        return {"success": False, "error": "No available devices found"}, None

    return None, candidate_entities

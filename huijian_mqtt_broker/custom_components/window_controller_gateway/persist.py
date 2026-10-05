"""持久化数据管理 - 避免 __init__.py 与 device_manager.py 之间的循环导入"""
import copy
import logging
import os
import json
import asyncio
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    DEVICE_TO_GATEWAY_MAPPING,
    GLOBAL_MANUALLY_REMOVED_DEVICES,
    GLOBAL_IGNORED_GATEWAYS,
    DEVICE_SETPOINTS,
)

_LOGGER = logging.getLogger(__name__)

PERSISTENT_DATA_FILE = "window_controller_gateway_data.json"
SCHEMA_VERSION = 1

# 串行化写入锁 + 防抖标志。
# 注意：这里必须保持"模块级全局锁"——所有 entry 的持久化数据写入的是同一个
# JSON 文件（PERSISTENT_DATA_FILE），全局锁保证跨 entry 的写入串行化。
# 不要按 entry 拆分锁：那会允许两个 entry 并发写同一文件，造成数据竞争/文件损坏。
_save_lock = asyncio.Lock()
_save_pending = False


async def load_persistent_data(hass: HomeAssistant) -> None:
    """加载持久化的设备映射和手动删除列表"""
    config_dir = hass.config.config_dir
    data_file = os.path.join(config_dir, PERSISTENT_DATA_FILE)
    bak_file = data_file + ".bak"

    data = None
    if os.path.exists(data_file):
        # 尝试读取主文件
        try:
            def _read_file():
                with open(data_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            data = await hass.async_add_executor_job(_read_file)
        except Exception as e:
            _LOGGER.error("加载持久化数据失败（主文件损坏）: %s", e)
        else:
            # v1.7.61（10-05 审计 #1）：根类型闸。json 解析成功但根不是对象
            # （[1,2,3]/"x"/123/true）时下方 data.get() 抛 AttributeError，
            # 而唯一调用点是 async_setup 里的裸 await ⇒ **整个集成 setup 失败**。
            # 按"损坏"处理：置 None 让既有的 .bak 救援/退化路径接管。
            if data is not None and not isinstance(data, dict):
                _LOGGER.error("持久化数据根类型非法（%s），按损坏处理并尝试备份恢复",
                              type(data).__name__)
                data = None
    else:
        # v1.6.12（第五轮审计 #10）：主文件缺失此前直接 return，.bak 永不救援
        # ——误删主文件后重启即全量丢失（映射/手动删除列表），而备份明明在。
        # "缺失"与"损坏"应同样触发备份恢复
        _LOGGER.warning("持久化主文件缺失: %s", data_file)

    # 主文件缺失或读取失败，尝试 .bak 恢复
    if data is None and os.path.exists(bak_file):
        _LOGGER.warning("主文件缺失或损坏，尝试从 .bak 恢复")
        try:
            def _read_bak():
                with open(bak_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            bak = await hass.async_add_executor_job(_read_bak)
            # v1.7.61：备份同样过根类型闸（.bak 也可能是非对象根）
            if isinstance(bak, dict):
                data = bak
                _LOGGER.info("从 .bak 恢复成功")
            else:
                _LOGGER.error(".bak 根类型非法（%s），放弃恢复", type(bak).__name__)
        except Exception as e:
            _LOGGER.error("从 .bak 恢复也失败: %s", e)

    if data is None:
        _LOGGER.error("持久化数据完全不可用，使用空数据继续运行")
        # v1.7.33：走 setdefault(DOMAIN, {}) 出口——旧实现直取 hass.data[DOMAIN]，
        # 隐式依赖 async_setup 先建好该键（调用顺序一变即 KeyError 直穿 setup）
        hass.data.setdefault(DOMAIN, {}).setdefault(DEVICE_SETPOINTS, {})
        return

    version = data.get("schema_version", 0)
    # v1.7.61（10-05 审计 #1）：版本位类型闸——"2"/null/浮点/bool 直接参与
    # `>` 比较会抛 TypeError（bool 还会被当 int 混过），同属"逃逸到 async_setup"
    # 的形态。非法一律按 0 处理并告警，其余字段照常加载。
    if isinstance(version, bool) or not isinstance(version, int):
        _LOGGER.warning("schema_version 类型非法（%r），按 0 处理", version)
        version = 0
    if version > SCHEMA_VERSION:
        _LOGGER.warning(
            "持久化数据版本(%d)高于当前支持版本(%d)，可能不兼容",
            version, SCHEMA_VERSION
        )

    # v1.6.12（第五轮审计 #10）：字段必须类型校验——此前 JSON 解析成功但字段
    # 畸形（如 "manually_removed_devices": null → set(None) TypeError）时异常
    # 逃逸至 async_setup，整个集成 setup 失败；备份恢复只覆盖"解析失败"，
    # 覆盖不了"内容畸形"。非法字段丢弃+告警，合法字段照常加载
    if 'device_to_gateway_mapping' in data:
        mapping = data['device_to_gateway_mapping']
        if isinstance(mapping, dict):
            # v1.6.19（第六轮审计 B-MED2）：校验下探一层——手工编辑/半写
            # 损坏可以让 {"<devSn>": 50} 这类"值非网关SN字符串"的条目混进
            # 映射；下游 base_entity（.lower()）与 device_manager
            # transfer_device 全部 AttributeError，波及该设备 cover/按钮的
            # 控制路径。与 v1.6.12 #10 同法：脏条目丢弃+告警。
            clean = {k: v for k, v in mapping.items()
                     if isinstance(k, str) and isinstance(v, str)}
            if len(clean) != len(mapping):
                _LOGGER.warning("映射表含 %d 条键/值非字符串脏条目，已丢弃",
                                len(mapping) - len(clean))
            hass.data[DOMAIN][DEVICE_TO_GATEWAY_MAPPING] = clean
            _LOGGER.info("已加载设备到网关映射表，共 %d 个设备", len(clean))
        else:
            _LOGGER.error(
                "device_to_gateway_mapping 字段类型非法（%s），已丢弃",
                type(mapping).__name__,
            )

    if 'manually_removed_devices' in data:
        removed = data['manually_removed_devices']
        if isinstance(removed, (list, tuple, set)):
            removed_set = {x for x in removed if isinstance(x, str)}
            hass.data[DOMAIN][GLOBAL_MANUALLY_REMOVED_DEVICES] = removed_set
            _LOGGER.info("已加载手动删除设备列表，共 %d 个设备", len(removed_set))
            if len(removed_set) != len(removed):
                _LOGGER.warning("手动删除列表含 %d 个非字符串条目，已丢弃",
                                len(removed) - len(removed_set))
        else:
            _LOGGER.error(
                "manually_removed_devices 字段类型非法（%s），已丢弃",
                type(removed).__name__,
            )

    # v1.7.12（审计 E-1/CF-F2）：被忽略网关跨重启持久——discovery 的
    # ignored_gateways 内存集合与此 key 共享同一 set 对象（见 discovery.py）
    if 'ignored_gateways' in data:
        ignored = data['ignored_gateways']
        if isinstance(ignored, (list, tuple, set)):
            ignored_set = {x for x in ignored if isinstance(x, str)}
            hass.data[DOMAIN][GLOBAL_IGNORED_GATEWAYS] = ignored_set
            if ignored_set:
                _LOGGER.info("已加载被忽略网关列表，共 %d 台", len(ignored_set))
        else:
            _LOGGER.error(
                "ignored_gateways 字段类型非法（%s），已丢弃",
                type(ignored).__name__,
            )

    # 设备参数设定值（速度/力度等），旧版文件无此字段时保持空表
    hass.data[DOMAIN].setdefault(DEVICE_SETPOINTS, {})
    if 'device_setpoints' in data and isinstance(data['device_setpoints'], dict):
        # v1.6.19（第六轮审计 B-MED2）：内层必须是"设备SN→参数dict"。
        # 混入标量值（{"500534...": 50}）时 number._get_setpoint 的
        # setpoints.get(sn, {}).get(param) 在实体 __init__ 路径抛
        # AttributeError，number 启动循环无 try → 整个 number 平台 setup
        # 失败（一坏俱坏）。逐键过滤内层。
        sp_raw = data['device_setpoints']
        sp = {k: v for k, v in sp_raw.items()
              if isinstance(k, str) and isinstance(v, dict)}
        if len(sp) != len(sp_raw):
            _LOGGER.warning("setpoints 含 %d 条内层非对象脏条目，已丢弃",
                            len(sp_raw) - len(sp))
        hass.data[DOMAIN][DEVICE_SETPOINTS] = sp
        _LOGGER.info("已加载设备参数设定值，共 %d 个设备", len(sp))


async def save_persistent_data(hass: HomeAssistant) -> None:
    """保存设备映射和手动删除列表到持久化存储

    使用 asyncio.Lock 串行化写入，确保不会有两个协程同时写同一个 .tmp 文件。
    通过 _save_pending 标志实现防抖：当写入期间有新的保存请求到来时，
    当前写入完成后会再执行一次写入（读取最新数据），确保数据不会丢失。
    后续的保存请求只需设置标志即可返回，无需重复写入。
    """
    global _save_pending

    # 如果已有保存任务在执行或等待，只需标记还需要再保存一次
    # 正在执行的任务会在完成后检查此标志并自动补写最新数据
    if _save_pending:
        _save_pending = True
        return

    _save_pending = True
    async with _save_lock:
        while _save_pending:
            _save_pending = False
            await _do_save(hass)


async def _do_save(hass: HomeAssistant) -> None:
    """执行实际的文件写入操作"""
    try:
        config_dir = hass.config.config_dir
        data_file = os.path.join(config_dir, PERSISTENT_DATA_FILE)

        # 在事件循环内先做快照，避免 executor 线程 json.dump 期间事件循环
        # 并发增删设备时抛 "dictionary changed size during iteration" 或写入不一致数据。
        # DEVICE_SETPOINTS 是嵌套 dict（设备 → 参数表），滑动条回调会并发修改其内层
        # dict，浅拷贝只保护外层；必须深拷贝生成不可变快照，否则 executor 序列化
        # 期间内层 dict 被并发增删键仍会触发 RuntimeError，导致持久化静默丢失。
        mapping_snapshot = dict(hass.data[DOMAIN].get(DEVICE_TO_GATEWAY_MAPPING, {}))
        removed_snapshot = list(hass.data[DOMAIN].get(GLOBAL_MANUALLY_REMOVED_DEVICES, set()))
        ignored_snapshot = sorted(hass.data[DOMAIN].get(GLOBAL_IGNORED_GATEWAYS, set()))
        setpoints_snapshot = copy.deepcopy(hass.data[DOMAIN].get(DEVICE_SETPOINTS, {}))

        data = {
            'schema_version': SCHEMA_VERSION,
            'device_to_gateway_mapping': mapping_snapshot,
            'manually_removed_devices': removed_snapshot,
            'ignored_gateways': ignored_snapshot,
            'device_setpoints': setpoints_snapshot
        }

        def _write_file():
            tmp_file = data_file + ".tmp"
            bak_file = data_file + ".bak"
            try:
                # 写入前备份旧文件为 .bak（用于损坏恢复）
                if os.path.exists(data_file):
                    try:
                        import shutil
                        shutil.copy2(data_file, bak_file)
                    except OSError as be:
                        # v1.7.12（第 6 轮审计 E-8）：备份失败必须留痕——
                        # .bak 停留在旧代次意味着"主文件损坏"救援可能回滚
                        # 掉最近变更，静默会让排障者对备份时效产生错误信任
                        _LOGGER.warning(".bak 备份轮转失败（救援将使用旧备份）: %s", be)
                with open(tmp_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                os.replace(tmp_file, data_file)
            except OSError as we:
                # v1.7.12（审计 E-8）：降级为非原子直写要大声——断电可致主文件
                # 截断，此时只能靠（可能过期一代的）.bak 救援
                _LOGGER.warning("原子写失败（%s），降级直写主文件——断电有截断风险", we)
                with open(data_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)

        await hass.async_add_executor_job(_write_file)

        _LOGGER.debug("已保存持久化数据")

    except Exception as e:
        _LOGGER.error("保存持久化数据失败: %s", e)

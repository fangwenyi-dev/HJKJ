"""pytest 共享配置：注入完整的假 homeassistant 包树，使集成模块可在无 HA 环境 import。

覆盖 window_controller_gateway/__init__.py 及其依赖模块的 import 链。
"""
import asyncio
import enum
import os
import sys
import types


def _pkg(name):
    mod = types.ModuleType(name)
    mod.__path__ = []  # 标记为包（允许子模块导入）
    sys.modules[name] = mod
    return mod


# ---- homeassistant 包树 ----
ha = _pkg("homeassistant")
ha_core = _pkg("homeassistant.core")
ha_const = _pkg("homeassistant.const")
ha_exceptions = _pkg("homeassistant.exceptions")
ha_config_entries = _pkg("homeassistant.config_entries")
ha_data_entry_flow = _pkg("homeassistant.data_entry_flow")
ha_helpers = _pkg("homeassistant.helpers")
ha_helpers_event = _pkg("homeassistant.helpers.event")
ha_helpers_entity = _pkg("homeassistant.helpers.entity")
ha_helpers_entity_platform = _pkg("homeassistant.helpers.entity_platform")
ha_helpers_device_registry = _pkg("homeassistant.helpers.device_registry")
ha_helpers_entity_registry = _pkg("homeassistant.helpers.entity_registry")
ha_helpers_config_validation = _pkg("homeassistant.helpers.config_validation")
ha_helpers_restore_state = _pkg("homeassistant.helpers.restore_state")
ha_components = _pkg("homeassistant.components")
ha_components_http = _pkg("homeassistant.components.http")
ha_components_mqtt = _pkg("homeassistant.components.mqtt")
ha_components_button = _pkg("homeassistant.components.button")
ha_components_binary_sensor = _pkg("homeassistant.components.binary_sensor")
ha_components_sensor = _pkg("homeassistant.components.sensor")
ha_components_cover = _pkg("homeassistant.components.cover")
ha_components_number = _pkg("homeassistant.components.number")
ha_components_repairs = _pkg("homeassistant.components.repairs")


# ---- core ----
class _DoneTask:
    """async_create_task 的返回面替身：既能被 await，也有 cancel()/done()。

    只返回裸 coro 的旧写法让两类判据都失去意义：存句柄后 .cancel() 的收尾路径
    在假件下永远"成功"，而 await 它的测试会拿到 coro 而不是结果。
    """

    def __init__(self, coro=None):
        self._coro = coro
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        return True

    def cancelled(self):
        return self._cancelled

    def done(self):
        # 真 async_create_task 返回的是**尚未完成**的 Task。此前这里恒 True，
        # 而生产侧通篇是 `if task and not task.done(): task.cancel()`
        # ——假件下那条分支永远不进，"卸载/停机会不会真取消后台任务"整类判据
        # 被静默作废（对抗复核实锤，且与本类 docstring 的自述正好相反）。
        return self._cancelled

    def __await__(self):
        if False:
            yield
        return None


class FakeServices:
    """`hass.services` 替身，签名与真实现等宽。

    审计 2026-09-30（C-1 修复的配套桩）：域级服务的**重注册**落点补进了
    async_setup_entry（旧实现只在 DOMAIN 级 async_setup 调过一次，删完全部条目
    再加条目 ⇒ 7 个服务永久缺席）。多处 hass 替身根本没有 `services` 属性，
    而真 HA 永远有——桩比真实现窄，就会把"生产能跑、测试打红"和调用形态漂移
    一起放过（本仓已四次实锤这条纪律）。async_call 一并给出，服务处理器
    可以被真调用而不只是被登记。
    """

    def __init__(self):
        self.registered = {}      # (domain, service) -> handler
        self.schemas = {}
        self.removed = []
        self.calls = []

    def async_register(self, domain, service, service_func, schema=None,
                       supports_response=None):
        self.registered[(domain, service)] = service_func
        self.schemas[(domain, service)] = schema

    def async_remove(self, domain, service):
        self.removed.append((domain, service))
        self.registered.pop((domain, service), None)
        self.schemas.pop((domain, service), None)

    def has_service(self, domain, service):
        return (domain, service) in self.registered

    async def async_call(self, domain, service, service_data=None, blocking=True,
                         context=None):
        """真调已登记的处理器（不复制其内部逻辑，只补调度面）。"""
        func = self.registered.get((domain, service))
        if func is None:
            raise KeyError("Service %s.%s not found" % (domain, service))
        call = types.SimpleNamespace(domain=domain, service=service,
                                     data=dict(service_data or {}))
        self.calls.append(call)
        result = func(call)
        if hasattr(result, "__await__"):
            result = await result
        return result


class HomeAssistant:
    """最小可用的假 HomeAssistant 实例"""

    def __init__(self):
        self.data = {}
        self.config = types.SimpleNamespace(config_dir=".")
        self.loop = None
        # 审计 2026-09-30：真 HA 的 hass.services 一定存在（域级服务注册面），
        # 替身缺这一面会让 async_setup_entry 的新补注册路径直接判红。
        self.services = FakeServices()

    def async_create_task(self, coro, name=None, **kw):
        """审计 2026-09-30 H-6：真签名带 name=，且返回的是 **Task**。

        旧写法 `return coro` 两头都不像：既没调度（测试里"任务真的跑了"的断言无从
        成立），也不是 Task（凡"存下句柄后 .cancel()/.done()"的路径直接失真，而
        unload 收尾正是这条路）。返回一个 done()/cancel() 齐备的替身，让"句柄
        被存起来后被取消"这类判据在假件下仍然可判。
        """
        if asyncio.iscoroutine(coro):
            return _DoneTask(coro)
        return _DoneTask()

    def add_job(self, job, *args):
        if callable(job):
            return job(*args)
        return None


ha_core.HomeAssistant = HomeAssistant
# 各测试文件自建的 hass 替身也要能拿到同一个等宽桩（本仓 fake homeassistant
# 包树的做法：替身符号从假模块导出，不让测试互相 import）。
ha_core.FakeServices = FakeServices


def full_status_view(**overrides):
    """从**真** HubClient.status_view() 派生假件视图，只覆盖测试关心的那几个字段。

    为什么要有这个东西（审计 2026-09-30 H-6 #5）：三处测试各写一份手写 dict 当
    status_view，真实现加/减字段时假件静默不跟——于是
    `test_member_op_views_return_the_full_status_view` 这类**名字**判的其实是假件
    自己的键：真 status_view 停发 bindCodeTtlS，测试照样绿，而面板倒计时会退回
    硬编兜底（huijian.js:427 自己写着"硬编 10 分钟在 hub 改了 TTL 后就是假话"）。
    键集由真实现单向决定，假件不可能再"窄"。
    """
    from custom_components.window_controller_gateway.hub_client import HubClient

    view = HubClient([], config_dir=".").status_view()
    view.update(overrides)
    return view


ha_core.full_status_view = full_status_view
ha_core.ServiceCall = type("ServiceCall", (), {})
# v1.6.11：config_flow 首次被测试导入（审计 #6 钉桩）——homeassistant.core.callback
# 是恒等标记装饰器，真实实现即返回原函数
ha_core.callback = lambda func: func


# ---- const ----
class Platform(enum.Enum):
    BINARY_SENSOR = "binary_sensor"
    BUTTON = "button"
    NUMBER = "number"
    SENSOR = "sensor"
    COVER = "cover"


ha_const.Platform = Platform
ha_const.EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"
ha_const.__version__ = "2026.8.0"


# ---- exceptions ----
class HomeAssistantError(Exception):
    pass


class ConfigEntryNotReady(Exception):
    pass


ha_exceptions.ConfigEntryNotReady = ConfigEntryNotReady
ha_exceptions.HomeAssistantError = HomeAssistantError
# v1.6.9：cover.py/button.py/gateway.py 硬 import HomeAssistantError（控制命令
# 假成功根治），假环境必须提供，否则 import 期 AttributeError


# ---- config_entries ----
class ConfigEntry:
    def __init__(self, data=None, options=None, entry_id="test", title="test"):
        self.data = data or {}
        self.options = options or {}
        self.entry_id = entry_id
        self.title = title


ha_config_entries.ConfigEntry = ConfigEntry
ha_config_entries.SOURCE_DISCOVERY = "discovery"
ha_config_entries.SOURCE_USER = "user"


# v1.6.11（审计 #6 钉桩）：config_flow.py 首次被测试导入——类定义
# `class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN)` 需要能消化
# domain= 关键字基类（真实 HA 靠 __init_subclass__），OptionsFlow 为普通基类
class _ConfigFlowBase:
    def __init_subclass__(cls, **kwargs):
        pass


ha_config_entries.ConfigFlow = _ConfigFlowBase
ha_config_entries.OptionsFlow = type("OptionsFlow", (), {})


# ---- data_entry_flow ----
class FlowResultType(enum.Enum):
    FORM = "form"
    CREATE_ENTRY = "create_entry"
    ABORT = "abort"
    MENU = "menu"


ha_data_entry_flow.FlowResult = dict
ha_data_entry_flow.FlowResultType = FlowResultType


class InvalidData(Exception):
    """fake data_entry_flow.InvalidData（schema 校验失败的包装异常，2026.8 实态）"""


ha_data_entry_flow.InvalidData = InvalidData


# ---- helpers ----
def _noop(*args, **kwargs):
    return None


ha_helpers_event.async_track_time_interval = lambda *a, **k: (lambda: None)
ha_helpers_device_registry.async_get = lambda hass: None
ha_helpers_entity_registry.async_get = lambda hass: None


class EntityCategory(enum.Enum):
    CONFIG = "config"
    DIAGNOSTIC = "diagnostic"


ha_helpers_entity.DeviceInfo = dict
ha_helpers_entity.EntityCategory = EntityCategory
ha_helpers_entity_platform.AddEntitiesCallback = type("AddEntitiesCallback", (), {})
ha_helpers_config_validation.string = lambda v: v
ha_helpers_config_validation.positive_int = lambda v: v
ha_helpers_config_validation.boolean = lambda v: v


# ---- helpers.issue_registry（v1.7.31 C-1 教训件）----
# 参数名集合 = 真 HA 2026.1.3 `inspect.signature(async_create_issue)` 实测
# 逐字复制（现场 TypeError 消息 "Did you mean 'is_fixable'?" + 官方 repairs
# 文档双源核验）。**禁止**退化为 **kwargs 吞参假件——C-1 臆造 is_fix_flow
# 骗过全测试的根因正是假 HA 树里根本没有本模块。
ha_helpers_issue = _pkg("homeassistant.helpers.issue_registry")
ha_helpers.issue_registry = ha_helpers_issue


def _ir_create_issue(hass, domain, issue_id, *, breaks_in_ha_version=None,
                     data=None, discovery_key=None, issue_domain=None,
                     is_fixable, is_persistent=False, learn_more_url=None,
                     severity=None, translation_key=None,
                     translation_placeholders=None):
    """真签名复制品；记录实参供守卫断言（is_fixable 为必填 keyword-only）。"""
    ISSUES_CREATED.append({
        "domain": domain, "issue_id": issue_id, "is_fixable": is_fixable,
        "severity": severity, "translation_key": translation_key,
    })


ISSUES_CREATED = []
ISSUES_DELETED = []
ha_helpers_issue.async_create_issue = _ir_create_issue
ha_helpers_issue.async_delete_issue = (
    lambda hass, domain, issue_id: ISSUES_DELETED.append((domain, issue_id)))
# 记录簿必须挂在假模块自身（测试经 ir.ISSUES_CREATED 访问同一 list 对象）
ha_helpers_issue.ISSUES_CREATED = ISSUES_CREATED
ha_helpers_issue.ISSUES_DELETED = ISSUES_DELETED


# ---- components ----
ha_components_http.HomeAssistantView = type("HomeAssistantView", (), {})
# v1.7.63 对抗复核 C-4：真 HA（2024.12 源码 :553）只有 is_connected，
# 没有 async_connected——假件不许带真机没有的属性（本仓已记过的反模式）。
ha_components_mqtt.is_connected = lambda hass: True
ha_components_mqtt.async_publish = _noop
ha_components_mqtt.async_subscribe = lambda *a, **k: (lambda: None)
ha_components_button.ButtonEntity = type("ButtonEntity", (), {})
ha_components_binary_sensor.BinarySensorEntity = type("BinarySensorEntity", (), {})
ha_components_binary_sensor.BinarySensorDeviceClass = type(
    "BinarySensorDeviceClass", (), {"CONNECTIVITY": "connectivity"}
)
ha_components_sensor.SensorEntity = type("SensorEntity", (), {})
ha_components_sensor.SensorDeviceClass = type(
    "SensorDeviceClass", (), {"VOLTAGE": "voltage", "ENUM": "enum"}
)
ha_components_sensor.SensorStateClass = type(
    "SensorStateClass", (), {"MEASUREMENT": "measurement"}
)
ha_components_cover.CoverEntity = type("CoverEntity", (), {})
# v1.7.20 对齐上游 homeassistant/components/cover/const.py::CoverEntityFeature
# 真实位值（OPEN=1 CLOSE=2 SET_POSITION=4 STOP=8）——旧替身 STOP=4 恰为
# 真实 SET_POSITION 位，位掩码断言在替身语义下会失真；ATTR_POSITION 为
# 生产导入符号（真值 "position"，上游 cover/__init__ 自 homeassistant.const
# 重导出）
ha_components_cover.CoverEntityFeature = type(
    "CoverEntityFeature", (), {"OPEN": 1, "CLOSE": 2, "SET_POSITION": 4, "STOP": 8}
)
ha_components_cover.ATTR_POSITION = "position"
ha_components_cover.CoverDeviceClass = type("CoverDeviceClass", (), {"WINDOW": "window", "CURTAIN": "curtain"})
ha_components_number.NumberEntity = type("NumberEntity", (), {})
ha_components_number.NumberMode = type("NumberMode", (), {"SLIDER": "slider"})

# ---- restore_state（v1.6.8：RestoreEntity 假基类，模拟真实异步接口契约）----
class FakeRestoreEntity:
    async def async_added_to_hass(self):
        pass

    async def async_get_last_state(self):
        return None


ha_helpers_restore_state.RestoreEntity = FakeRestoreEntity

# ---- components.repairs（v1.7.63 C-5）：真契约 = data_entry_flow.FlowHandler
# 子类，带 issue_id/data 两个属性（HA components/repairs/models.py 逐字核验）；
# 修复流只能经 async_create_fix_flow 产出（HA 会 hasattr 校验平台）。
class RepairsFlow:
    """假基类：只保真契约面（issue_id/data + FlowHandler 的 show/create/abort）。"""

    issue_id = None
    data = None


ha_components_repairs.RepairsFlow = RepairsFlow


# ---- v1.6.9：base_entity 生命周期方法链补 super() 后，真实 HA 里
# Entity 基类（homeassistant/helpers/entity.py）对两个钩子都有空实现，
# 假实体类必须同样提供，否则测试 MRO 里 super() 落到 object 抛 AttributeError
async def _noop_async(self):
    return None


for _fake in (
    ha_components_button.ButtonEntity,
    ha_components_binary_sensor.BinarySensorEntity,
    ha_components_sensor.SensorEntity,
    ha_components_cover.CoverEntity,
    ha_components_number.NumberEntity,
    FakeRestoreEntity,
):
    _fake.async_added_to_hass = _noop_async
    _fake.async_will_remove_from_hass = _noop_async


# ---- 加入 custom_components 路径（测试文件在 huijian_mqtt_broker/tests/）----
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

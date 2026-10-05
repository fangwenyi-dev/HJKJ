"""MQTT处理器 - 使用HA内置MQTT，符合新的主题规程

v1.6.25 拆包（行为零变化）：本包是 1.6.24 前单文件 mqtt_handler.py（1774 行）的
物理拆分——方法按消息生命周期内聚搬入 5 个 mixin（见各文件头注释），由下方
组合类还原为**同一个类**：实例方法解析、对外 import 面、ack 方向契约、
dedup 语义、weakref 回调设计均与拆分前逐字一致。
"""
import logging

# 供外部/tests 经包面 patch（mh_mod.mqtt 与子模块引用同一模块对象，monkeypatch
# 天然传播）；本行直接引用面为零，故 noqa。
from homeassistant.components import mqtt  # noqa: F401

from ._lifecycle import _LifecycleMixin
from ._protocol import _ProtocolMixin
from ._ctypes import _CtypeHandlersMixin
from ._commands import _CommandsMixin
from ._callbacks import _CallbacksMixin

# logger 名钉死为拆分前模块 __name__ 值（包 __init__ 的 __name__ 恰为同串，
# 显式写字面量防未来重构手滑改用 __name__ 导致日志面漂移）
_LOGGER = logging.getLogger("custom_components.window_controller_gateway.mqtt_handler")


class WindowControllerMQTTHandler(_LifecycleMixin, _ProtocolMixin,
                                  _CtypeHandlersMixin, _CommandsMixin,
                                  _CallbacksMixin):
    """MQTT处理器类 - 使用HA内置MQTT

    状态语义（Bug4 澄清，避免混用）：
    - ``self.connected``：**网关是否在线**。收到网关上报（001/002/005）置 True，
      网关超时未上报（GATEWAY_TIMEOUT_SECONDS）置 False；重连（重新订阅）成功后
      置 True 代表 MQTT 层就绪，网关真实在线状态由后续上报刷新。
    - **MQTT broker 是否就绪**：用 ``homeassistant.components.mqtt.is_connected(hass)``
      检查（见 check_connection），与 ``self.connected`` 是两回事。
    - ``pairing_active``：网关是否处于配对模式（与 connected 无关）。
    """


__all__ = ["WindowControllerMQTTHandler"]

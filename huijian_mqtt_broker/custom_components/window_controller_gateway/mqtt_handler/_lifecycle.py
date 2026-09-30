"""_LifecycleMixin —— 连接生命周期与实例状态：__init__/setup/cleanup、网关超时巡检、重连退避、async 任务调度、bind-op 登记、配对门控开关

v1.6.25 拆包：代码自 mqtt_handler.py 单文件**逐字原样搬移**，禁止在此顺手优化；方法经
组合类 WindowControllerMQTTHandler 解析（单类形态与拆分前一致）。
"""
import logging
import asyncio
import random
import time
import weakref
from typing import Optional
from homeassistant.core import HomeAssistant
from ..utils import is_mqtt_loaded, gateway_instance_uuid
from ..const import (
    DOMAIN,
    CONF_AUTO_DISCOVERY,
    GATEWAY_CHECK_INTERVAL,
    GATEWAY_TIMEOUT_SECONDS,
    INITIAL_RETRY_DELAY,
    MQTT_MAX_RETRIES,
    MQTT_MIN_JITTER,
    MQTT_MAX_JITTER,
    MQTT_RETRY_DELAY_MAX,
    DEFAULT_COMMAND_ID,
    TOPIC_GATEWAY_REQ_FORMAT,
    TOPIC_GATEWAY_RSP,
    MAX_BIND_OPS,
)

# logger 名钉死为拆分前模块 __name__ 值——日志输出零差异（回归要求）
_LOGGER = logging.getLogger("custom_components.window_controller_gateway.mqtt_handler")


class _LifecycleMixin:
    def __init__(self, hass: HomeAssistant, gateway_sn: str, device_manager):
        """初始化MQTT处理器"""
        self.hass = hass
        self.gateway_sn = gateway_sn
        self.device_manager = device_manager
        self.connected = False  # 网关在线状态（见类 docstring）
        self.pairing_active = False
        self.last_gateway_report_time = None  # 最后收到网关上报的时间（time.monotonic() 单调时钟）
        self.command_id = DEFAULT_COMMAND_ID  # 命令ID初始值
        self._check_task = None  # 后台任务引用
        self._reconnect_task = None  # MQTT 重连任务引用（去重 + cleanup 可取消）
        # v1.6.19（第六轮审计 A-MED1）：cleanup 与 _reconnect_mqtt 竞态守卫——
        # cleanup 在 await 让出点期间，重连成功路径会"吞掉旧任务取消→重建
        # 检查任务"，cleanup 恢复后把引用覆没，泄漏一个永不取消的
        # _check_gateway_timeout 循环（每次 MQTT 抖动期 reload 泄漏一个实例）。
        # cleanup 首行置 True，重连路径创建任何后台任务前必须检查。
        self._closing = False
        self._unsub_rsp = None  # MQTT 订阅取消函数
        # v1.7.12（审计 B-1）：订阅所绑定的 MQTT client 实例身份，巡检发现
        # 变化即重建订阅（见 _ensure_mqtt_subscription）。
        # v1.7.33：同时持弱引用——裸 id() 在 reload 后可能被新对象复用地址
        # （"client 未变"假阴性），弱引用按对象身份判换代。
        self._mqtt_client_id = None
        self._mqtt_client_ref = None
        # v1.7.33：订阅重建互斥（重连任务 × 30s 巡检并发进入会让两个句柄
        # 互相覆盖、前一个回调永久泄漏）
        self._sub_lock = asyncio.Lock()
        self._msg_lock = asyncio.Lock()  # 异步消息去重锁
        # v1.7.27：公式上收 utils.gateway_instance_uuid 单一真源——耳朵 001
        # 代答与正式 handler 应答共用同一确定性 uuid（值逐字不变，回归安全）
        self.instance_uuid = gateway_instance_uuid(hass)
        # P1 修复：将配对超时句柄统一存储在 mqtt_handler 上，
        # 使服务调用和按钮按下共享同一个超时管理，避免重复超时回调。
        self.pairing_timeout_handle = None
        
        # MQTT主题定义 - 根据协议要求简化为两个主题
        self.TOPIC_GATEWAY_REQ = TOPIC_GATEWAY_REQ_FORMAT.format(gateway_sn=gateway_sn)  # 发送命令到网关
        self.TOPIC_GATEWAY_RSP = TOPIC_GATEWAY_RSP  # 接收网关数据和响应，同时用于发送响应
        
        # 状态更新回调 - 使用字典按设备SN组织回调
        self._status_callbacks = {}
        
        # 消息去重 - 记录最近处理的消息ID，避免重复处理
        self._processed_messages = {}  # {message_id: timestamp}
        self._message_dedup_duration = 5  # 5秒内相同ID的消息认为是重复
        # 记录最近发出的 003 绑定/解绑命令方向（按命令 id 匹配回复）。
        # 固件解绑回复不带 data.bind，仅凭"设备是否已存在"推断存在竞态窗口
        # （回复晚于本地删除时误判为绑定、设备复活），id 匹配可完全消除。
        # {command_id: "bind" / "unbind"}，收到回复即清理，不会累积。
        self._bind_ops = {}
        # v1.7.31（A-6）：_schedule_async_task 派发任务的统一句柄登记。
        # 旧实现丢弃 create_task 返回值——cleanup 后在途消息处理协程继续
        # 对已清空的 device_manager（devices.clear）做 add/ update，违反
        # device_manager P1 定案"cleanup 后不得访问已清理状态"。done 即
        # 自摘除，集合规模=在途任务数有界。
        self._dispatch_tasks: set = set()

    def _record_bind_op(self, command_id: int, direction: str, device_sn: Optional[str] = None) -> None:
        """记录 003 绑定/解绑命令方向（按命令 id 匹配回复）

        网关离线/不回复时记录不会被消费，因此设置上限，超出后按
        插入顺序淘汰最旧记录（dict 保持插入顺序），防止无界增长。
        device_sn 在可确定时一并记录（解绑命令），供设备删除时清理
        该设备的待处理记录（_clear_bind_ops_for_device）。
        """
        self._bind_ops[command_id] = (direction, device_sn)
        if len(self._bind_ops) > MAX_BIND_OPS:
            oldest = next(iter(self._bind_ops))
            self._bind_ops.pop(oldest)
            _LOGGER.debug("_bind_ops 超过上限 %d，淘汰最旧记录: %s", MAX_BIND_OPS, oldest)

    def _auto_discovery_enabled(self) -> bool:
        """读取本条目 options 的 auto_discovery（v1.6.12 第五轮审计 #4）。

        此前该选项在选项表单里存在但全工程零消费——取消勾选静默无效。
        entry/设备管理器不可得或字段缺失时保持 True（历史默认行为），
        避免取不到配置反而误停自动添加。
        """
        try:
            options = getattr(self.device_manager.entry, "options", None) or {}
            return bool(options.get(CONF_AUTO_DISCOVERY, True))
        except Exception:
            return True

    def _remember_mqtt_client(self) -> None:
        """记下订阅所绑定的 MQTT client 身份（v1.7.33 双份形态）。

        弱引用用于身份比较（地址复用不再造成假阴性）；身份整数保留给不可
        弱引用的替身/极端形态做退化路径，二者由 _ensure_mqtt_subscription
        按可用性选用。
        """
        client = self.hass.data.get("mqtt")
        self._mqtt_client_id = id(client)
        try:
            self._mqtt_client_ref = weakref.ref(client) if client is not None else None
        except TypeError:
            self._mqtt_client_ref = None

    def _clear_bind_ops_for_device(self, device_sn: str) -> None:
        """清除指定设备的待处理 003 绑定/解绑方向记录

        设备删除后，晚到的绑定确认不得再利用 id 匹配复现"绑定"语义；
        清除后此类回复的 bind_op 为空，将落入 _handle_ctype_003 的
        手动删除列表拒绝分支，防止设备复活。
        """
        for command_id, (direction, op_device_sn) in list(self._bind_ops.items()):
            if op_device_sn == device_sn:
                del self._bind_ops[command_id]
                _LOGGER.debug("已清除设备 %s 的待处理绑定记录 (id=%s, %s)", device_sn, command_id, direction)

    def _schedule_async_task(self, coro):
        """安全地将异步任务调度到主事件循环

        MQTT 回调可能在 paho-mqtt 网络线程中被调用，而非事件循环线程。
        - 在事件循环内：使用 hass.async_create_task（线程安全，HA 自动记录异常）
        - 在线程中：使用 asyncio.run_coroutine_threadsafe（线程安全），
          并通过 done_callback 记录未捕获异常，避免静默吞没。

        v1.7.33（全量审计）：首行判 `_closing`——cleanup 置闩锁后仍有一串
        await 让出点（退订/取消任务/await 收口），此窗口内到达的上报会再拉起
        新任务、逃过 A-6 的在途快照，在 device_manager.cleanup() 清空后继续
        写缓存。闩锁后一律丢弃。
        """
        if self._closing:
            coro.close()
            return
        try:
            loop = self.hass.loop
            if not loop.is_running():
                _LOGGER.warning("事件循环未运行，跳过任务调度")
                coro.close()
                return

            # 检测当前是否在事件循环线程中
            try:
                current_loop = asyncio.get_running_loop()
                in_event_loop = current_loop is loop
            except RuntimeError:
                in_event_loop = False

            if in_event_loop:
                # v1.7.31（A-6）：返回值不再丢弃——cleanup 需可收口
                self._register_dispatch(self.hass.async_create_task(coro))
            else:
                future = asyncio.run_coroutine_threadsafe(coro, loop)

                def _log_exception(fut):
                    """记录任务中的未捕获异常"""
                    try:
                        fut.result()
                    except Exception as e:
                        _LOGGER.error("异步任务执行失败: %s", e, exc_info=True)

                future.add_done_callback(_log_exception)
                self._register_dispatch(future)
        except RuntimeError as e:
            _LOGGER.error("调度异步任务失败: %s", e)
            coro.close()

    def _register_dispatch(self, fut) -> None:
        """登记派发句柄并挂自摘除回调（v1.7.31 A-6）。"""
        tasks = getattr(self, "_dispatch_tasks", None)
        if tasks is None:  # 测试代用对象未走 __init__ 时降级为不登记
            return
        tasks.add(fut)
        fut.add_done_callback(tasks.discard)

    async def setup(self):
        """设置MQTT处理器"""
        _LOGGER.info("MQTT处理器初始化: %s", self.gateway_sn)
        
        # 检查MQTT集成是否可用
        if not is_mqtt_loaded(self.hass):
            _LOGGER.error("MQTT集成未启用，请先在Home Assistant中启用MQTT集成")
            return False
            
        # 订阅主题
        # v1.7.18（第 7 轮审计 BUG-1）：订阅失败不再静默当成功——旧版丢弃
        # 返回值，条目照常 loaded 而入站永久丢单，唯一补救的重连循环 5 次
        # 封顶（~2min）烧完后无人再管。返回 False 由调用方抛
        # ConfigEntryNotReady，HA 条目级重试成为原生自愈通道。
        if not await self._subscribe_topics():
            _LOGGER.error("MQTT 订阅失败，稍后由 HA 自动重试条目加载")
            return False
        
        # 启动定时检查任务，每30秒检查一次是否超时
        self._check_task = asyncio.create_task(
            self._check_gateway_timeout(),
            name=f"{DOMAIN}_check_timeout_{self.gateway_sn}"
        )
        
        return True

    async def _check_gateway_timeout(self):
        """检查网关是否超时未上报"""
        try:
            while True:
                await asyncio.sleep(GATEWAY_CHECK_INTERVAL)  # 每30秒检查一次
                try:
                    # v1.7.12（审计 B-1）：先做订阅代际检查——MQTT 条目 reload
                    # 后 client 已换、旧订阅随之作废，发布正常而入站永久失联，
                    # 必须在本心跳里重建（cleanup 已取消旧订阅句柄防误触）
                    await self._ensure_mqtt_subscription()

                    should_go_offline = False
                    reason = ""

                    if self.last_gateway_report_time:
                        # 有上报记录：检查是否超时。
                        # 使用 time.monotonic()（单调时钟）：墙钟 datetime.now() 在
                        # NTP 校时/用户改时间/时区切换时会发生跳变，导致误判离线或
                        # 无限延长超时窗口；单调时钟不受系统时间调整影响。
                        time_diff = time.monotonic() - self.last_gateway_report_time
                        if time_diff > GATEWAY_TIMEOUT_SECONDS:
                            should_go_offline = True
                            reason = f"超过{GATEWAY_TIMEOUT_SECONDS}秒未上报"
                    else:
                        # 从未收到网关上报：如果当前标记为在线，属于误判
                        if self.connected:
                            should_go_offline = True
                            reason = "从未收到网关上报消息"

                    if should_go_offline and self.connected:
                        self.connected = False
                        self._notify_status_change()
                        _LOGGER.warning("网关 %s %s，标记为离线", self.gateway_sn, reason)
                        self._schedule_async_task(
                            self.device_manager.update_gateway_status("offline")
                        )
                except Exception as e:
                    _LOGGER.error("检查网关超时出错: %s", e)
        except asyncio.CancelledError:
            _LOGGER.info("网关超时检查任务已取消")
            return
        except Exception as e:
            _LOGGER.error("网关超时检查任务异常: %s", e)

    def _schedule_reconnect(self):
        """调度 MQTT 重连（去重：已有重连任务在运行则跳过，避免任务无限累积）"""
        if self._closing:  # v1.6.19 A-MED1：unload 后不得再拉起任何后台任务
            _LOGGER.debug("条目清理中，跳过重连调度")
            return
        if self._reconnect_task and not self._reconnect_task.done():
            _LOGGER.debug("MQTT重连任务已在运行，跳过重复调度")
            return
        self._reconnect_task = asyncio.create_task(
            self._reconnect_mqtt(),
            name=f"{DOMAIN}_reconnect_{self.gateway_sn}"
        )

    async def _reconnect_mqtt(self):
        """MQTT重连逻辑 - 自适应重试策略，结合抖动和随机化"""
        retry_count = 0
        max_retries = MQTT_MAX_RETRIES
        base_delay = INITIAL_RETRY_DELAY
        min_jitter = MQTT_MIN_JITTER
        max_jitter = MQTT_MAX_JITTER
        
        while retry_count < max_retries:
            if self._closing:  # v1.6.19 A-MED1：cleanup 已开始，立即退出重试循环
                _LOGGER.debug("条目清理中，中止 MQTT 重连循环")
                return
            try:
                _LOGGER.debug("尝试重新连接MQTT... (重试 %d/%d)", retry_count + 1, max_retries)
                
                # 重新订阅主题
                # v1.7.12（审计 B-3）：_subscribe_topics 现返回 bool 且不再让
                # 异常外抛——旧版重连循环拿不到失败信号，第一圈必然"成功"
                # 返回，指数退避整段成死代码。False 时显式 raise 走下方退避。
                sub_ok = await self._subscribe_topics()
                if not sub_ok:
                    raise RuntimeError("MQTT 重新订阅失败")

                # 重新启动网关超时检查任务
                if self._closing:
                    # v1.6.19（第六轮审计 A-MED1）：上面 await self._check_task
                    # 是让出点——cleanup 恰好在此挂起期间恢复并继续把
                    # _check_task 覆没为 None；若本处再 create，新任务无人持
                    # 引用、永不取消（泄漏 + 周期触碰已销毁的 manager）。
                    _LOGGER.debug("条目清理中，放弃重建超时检查任务")
                    return
                if self._check_task:
                    if not self._check_task.done():
                        self._check_task.cancel()
                        try:
                            await self._check_task
                        except asyncio.CancelledError:
                            # Bug3 修复：主动 cancel 后 await，CancelledError 是预期
                            # 结果（任务被取消），吞掉继续；与旧 (CancelledError, Exception)
                            # 合并写法行为等价，但语义更清晰，不再把 CancelledError
                            # 与普通异常混为一谈。
                            pass
                        except Exception:
                            pass
                # v1.7.12（审计 B-4，v1.6.19 A-MED1 同族补漏）：上面 cancel/await
                # 旧任务又是一次让出点，:244 的检查结果可能已过期——create 前
                # 就地复检，否则 cleanup 恢复后会把新任务覆没成孤儿
                if self._closing:
                    _LOGGER.debug("条目清理中，放弃重建超时检查任务（create 前复检）")
                    return
                self._check_task = asyncio.create_task(
                    self._check_gateway_timeout(),
                    name=f"{DOMAIN}_check_timeout_{self.gateway_sn}"
                )
                
                _LOGGER.debug("MQTT重新连接成功")
                # Bug1 修复：重连（重新订阅）成功后立即标记连接就绪。
                # self.connected 语义 = 网关在线（收到上报为 True），但订阅成功
                # 代表 MQTT broker 层已就绪；此处置 True 避免 send_command 的
                # 重连等待分支误判失败。网关真实在线状态随后由上报刷新，
                # 若网关离线，_check_gateway_timeout 会在超时窗口内纠正为 False。
                if not self.connected:
                    self.connected = True
                    self._notify_status_change()
                return
            except Exception as e:
                retry_count += 1
                _LOGGER.debug("MQTT重连失败: %s", e)
                
                if retry_count < max_retries:
                    # 实现自适应重试策略
                    # 1. 基础指数退避
                    delay = base_delay * (2 ** (retry_count - 1))
                    # 2. 添加抖动（随机化）
                    jitter = random.uniform(min_jitter, max_jitter)
                    jittered_delay = delay * jitter
                    # 3. 确保延迟在合理范围内
                    jittered_delay = max(1, min(jittered_delay, MQTT_RETRY_DELAY_MAX))
                    
                    _LOGGER.debug("%.1f秒后重试... (基础延迟: %.1f秒, 抖动系数: %.2f)", jittered_delay, delay, jitter)
                    await asyncio.sleep(jittered_delay)
                else:
                    _LOGGER.debug("MQTT重连失败，已达到最大重试次数")
                    # 标记为离线
                    if self.connected:
                        self.connected = False
                        self._notify_status_change()
                        self._schedule_async_task(
                            self.device_manager.update_gateway_status("offline")
                        )
                    return

    async def cleanup(self):
        """清理MQTT资源"""
        _LOGGER.info("清理MQTT资源")
        # v1.6.19（第六轮审计 A-MED1）：必须最先置位——本函数后续每个
        # await 都是让出点，重连任务可能在任意让出点恢复并重建后台任务；
        # 置位后 _schedule_reconnect/重连循环/检查任务重建全部拒绝。
        self._closing = True
        # v1.7.33（全量审计）：退订提到**第一个 await 之前**（旧序在若干 await
        # 之后才退订，窗口内到达的上报仍会经 handle_gateway_response 派发；
        # 虽有 _closing 门兜底，但订阅面早退一秒就少一秒在途面——两者同批）。
        if self._unsub_rsp:
            try:
                self._unsub_rsp()
            except Exception as e:
                _LOGGER.debug("取消MQTT订阅异常: %s", e)
            self._unsub_rsp = None
        # v1.7.31（A-6）：先收口在途派发任务——消息处理协程（002 批处理/
        # 003 绑定/代答派发）不得活到 cleanup 之后去写已清空的 device_manager
        # /注册表（同族 P1 定案；_closing 门旧版只罩住重连路径）。
        # 当前任务自身若在被派发集中（罕见自引用）跳过自我等待防死锁。
        _cur = asyncio.current_task()
        _pending = [f for f in list(getattr(self, "_dispatch_tasks", ()))
                    if f is not _cur]
        for _f in _pending:
            _f.cancel()
        if _pending:
            await asyncio.gather(*_pending, return_exceptions=True)
        # 配对超时句柄
        if self.pairing_timeout_handle:
            try:
                self.pairing_timeout_handle.cancel()
            except Exception as e:
                _LOGGER.debug("取消配对超时句柄异常: %s", e)
        self.pairing_timeout_handle = None
        # 清理 003 绑定/解绑方向记录
        self._bind_ops.clear()

        # 取消后台任务
        # 审计 2026-09-30 C-6：三处"取消子任务再 await 收尾"统一改
        # gather(return_exceptions=True)（照 hub_client.async_stop:493-497 的定案，
        # 那里的注释就是本处的判据）。裸 await 的两个毛病：①子任务必然回
        # CancelledError，裸 await 把它当"本协程被取消"往上传（要靠 except 分支
        # 吞掉，于是真被取消时也一起被吞）；②写成 `except (CancelledError, Exception)`
        # 更是明着吞掉 HA 停机路径的取消信号。gather 两者都对：子任务的取消不转抛，
        # 本协程真被取消时照样上传。
        if self._check_task:
            self._check_task.cancel()
            await asyncio.gather(self._check_task, return_exceptions=True)
            _LOGGER.debug("MQTT检查任务已收尾")
            self._check_task = None

        # 取消 MQTT 重连任务（若正在运行）
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            await asyncio.gather(self._reconnect_task, return_exceptions=True)
            _LOGGER.debug("MQTT重连任务已收尾")
            self._reconnect_task = None

        # v1.7.12（审计 B-4 尾检兜底）：重连任务可能吞掉本次取消并完成
        # create（A-MED1 残余窗口）——复检一次，凡新出现的超时检查任务都
        # 在 unload 收口前取消，不留泄漏
        if self._check_task and not self._check_task.done():
            self._check_task.cancel()
            await asyncio.gather(self._check_task, return_exceptions=True)
            _LOGGER.debug("cleanup 尾检取消残留超时检查任务")
            self._check_task = None
        
        # v1.7.33：订阅已在 cleanup 首部（首个 await 之前）退订，此处不再重复。
        
        # 清理所有回调引用，避免内存泄漏
        self._status_callbacks.clear()
        _LOGGER.debug("所有状态更新回调已清理")

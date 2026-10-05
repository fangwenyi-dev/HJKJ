"""修复流平台（v1.7.63，对抗复核 C-5）。

没有本文件时 HA 走 `ConfirmRepairFlow`：点「修复」只弹通用确认框，确认后
**只删卡、不执行任何动作**（HA 2024.12 `components/repairs/issue_handler.py:73/90`
逐字核验）——v1.7.29 起卡面上承诺的"点提交重试一轮"在此前都是假话：旧实现把
重试写在 `config_flow.async_step_repair`，而 HA 的修复流根本不启动集成自己的
config flow（它只查 repairs 平台）。本文件把两处可修 issue 接上真动作。

契约（`components/repairs/models.py` + esphome/repairs.py 先例）：平台须提供
`async_create_fix_flow(hass, issue_id, data) -> RepairsFlow`；流成功走
`async_create_entry`（HA 的 `async_finish_flow` 据此删卡），失败走
`async_show_form(errors=...)` 让"还没好"可见。
"""
from __future__ import annotations

import logging

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .mqtt_bootstrap import (CHANNEL_ISSUE_ID, TAKEOVER_ISSUE_ID,
                             ensure_mqtt_connection, has_bootstrap_marker,
                             verify_builtin_channel)

_LOGGER = logging.getLogger(__name__)

#: 可修 issue 全集：未知 id 一律响亮抛（照 esphome repairs.py 先例——静默返回
#: 空流会让 HA 以为"有修复动作"而实际什么都不做，等于换一种假宣称）。
_FIXABLE_ISSUES = {
    TAKEOVER_ISSUE_ID: "MQTT 自动配置未落地（引导标记仍在）",
    CHANNEL_ISSUE_ID: "HA 的 MQTT 连接未指向内置 Broker（127.0.0.1:2022）",
}


class GatewayRepairFlow(RepairsFlow):
    """确认步显示诊断 → 提交后跑一轮重试/核验 → 成功结束（HA 删卡）/ 失败回显。"""

    async def async_step_init(self, user_input=None) -> FlowResult:
        """修复流首步：只渲染确认（诊断与处置写在翻译的 description 里）。

        v1.7.63（对抗复核 F3）：**不得转发 user_input**——HA 把流的 init data
        （`{"issue_id": …}`，`repairs/websocket_api.py:130-132`）当 user_input
        传进首步（`data_entry_flow.py:342`）；转发会让"点修复"在建流请求里
        直接执行、确认步被绕过（HA 自家 ConfirmRepairFlow 即为此
        `async_step_confirm()` 不传参，`issue_handler.py:24-28`）。
        """
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input=None) -> FlowResult:
        if user_input is not None:
            try:
                await ensure_mqtt_connection(self.hass)
            except Exception as e:  # noqa: BLE001 — broker 未起等统一按"仍坏"处理
                _LOGGER.debug("修复流内引导重试失败（按仍坏处理）: %s", e)
            try:
                if self.issue_id == TAKEOVER_ISSUE_ID:
                    # 接管卡的成功判据 = 引导标记被消费（落地）
                    ok = not await has_bootstrap_marker(self.hass)
                else:
                    # 通道卡：标记早已被删，必须用常驻核验判（标记判据会误报已修好）
                    ok = (await verify_builtin_channel(self.hass)) in ("ok", "no_endpoint")
            except Exception as e:  # noqa: BLE001 — 判定面异常不得炸穿修复流
                _LOGGER.warning("修复流核验异常（按仍坏处理）: %s", e)
                ok = False
            if ok:
                return self.async_create_entry(data={})
            return self.async_show_form(
                step_id="confirm", errors={"base": "still_broken"})
        return self.async_show_form(step_id="confirm")


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str,
                                data: dict | None) -> RepairsFlow:
    """HA 修复流入口（平台被 hasattr 校验，签名不符即 HomeAssistantError）。"""
    if issue_id not in _FIXABLE_ISSUES:
        raise ValueError(f"unknown repair issue: {issue_id}")
    return GatewayRepairFlow()

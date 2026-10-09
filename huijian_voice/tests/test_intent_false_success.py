# -*- coding: utf-8 -*-
"""HA 意图回证的"假成功"闸（10-10 真机实锤，payload 是从 .91 上原样抄回来的）。

现场：`_dbg_intent_path.py` 对办公室射灯发 HassTurnOn(entity_id=…)——
HTTP **200**，但 body 明写 `response_type=error / data.code=failed_to_handle`
（"Service handler cannot target all devices"），HA 侧状态**没变**。
旧 `HAClient._normalize_result` 的兜底是 `success = status < 300` ⇒ 归一化成成功，
执行器播「好的」，用户听到"办好了"而灯没亮——v1.0.21 / v1.0.34 / v1.0.39 同族再来一次。

判据方向：**HTTP 码只证明"有没有回"，response_type/data.code 才证明"有没有办"**。
同时留正向对照（真执行成功的 200 回执不得被误杀）。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from core.ha_client import HAClient        # noqa: E402

# ── 真机原样回执（.91 / light.ban_gong_shi_she_deng / HassTurnOn）────────────
REAL_ERROR_BODY = (
    '{"speech": {"plain": {"speech": "Service handler cannot target all devices", '
    '"extra_data": null}}, "card": {}, "language": "zh-Hans", '
    '"response_type": "error", "data": {"code": "failed_to_handle"}}')

# 真执行成功的形状（HA action_done + 逐实体 states）：必须仍然判成功
REAL_OK_BODY = (
    '{"speech": {"plain": {"speech": "Call completed"}},'
    ' "response_type": "action_done", "data": {"success_count": 1, "failed": 0,'
    ' "states": [{"entity_id": "light.ban_gong_shi_she_deng", "state": "on"}]}}')


def test_200_但_response_type_error_必须判失败():
    r = HAClient._normalize_result(200, REAL_ERROR_BODY, "HassTurnOn")
    assert r["success"] is False, r
    assert "cannot target all devices" in r["message"], r["message"]
    assert r["raw"].get("response_type") == "error"


def test_data_code_失败也要判失败_即使没有_response_type():
    body = ('{"speech": {"plain": {"speech": "nope"}}, "data": {"code": "no_intent_match"}}')
    r = HAClient._normalize_result(200, body, "HassTurnOn")
    assert r["success"] is False, r


def test_真成功的200不得被误杀():
    r = HAClient._normalize_result(200, REAL_OK_BODY, "HassTurnOn")
    assert r["success"] is not False, r          # action_done 且带 success_count=1 ⇒ 不降级


def test_裸_handler_回执_带_success_键_仍按_success_走():
    r = HAClient._normalize_result(200, '{"success": false, "message": "设备离线"}',
                                   "TurnDeviceOn")
    assert r["success"] is False
    r2 = HAClient._normalize_result(200, '{"success": true, "message": ""}', "TurnDeviceOn")
    assert r2["success"] is True


def test_顶层success与error同现时不得绕过这道闸():
    """漏杀面自查（10-10）：这道闸若挂在 `if "success" in obj` 之后就是白挂——
    HA 版本/包装差异哪天同时带顶层 success 与 response_type=error，仍会被洗成成功。"""
    body = ('{"success": true, "response_type": "error",'
            ' "data": {"code": "failed_to_handle"},'
            ' "speech": {"plain": {"speech": "cannot target all devices"}}}')
    r = HAClient._normalize_result(200, body, "HassTurnOn")
    assert r["success"] is False, r
    assert "cannot target all devices" in r["message"], r["message"]


def test_未知data_code不得被误判为失败():
    """方向取"宁可漏杀不误杀"：误杀会把已动的设备说成没动，用户去重按更坏。"""
    body = ('{"success": true, "data": {"code": "some_future_code"},'
            ' "control_targets": ["light.x"]}')
    r = HAClient._normalize_result(200, body, "TurnDeviceOn")
    assert r["success"] is True, r


def test_纯speech回执没有错误标记_仍按状态码走_不改旧语义():
    r = HAClient._normalize_result(200, '{"speech_result": "好的"}', "HassTurnOn")
    assert r["success"] is True and r["message"] == "好的", r

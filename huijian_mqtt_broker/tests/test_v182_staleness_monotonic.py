"""v1.8.2 A2：子设备 15 分钟时效闸改用单调钟（含同族计数钉与反向钉）。

改前：写侧 `device_manager` 用 `time.time()`，读侧 cover/sensor 用
`time.time() - last_update > 15min` —— 而同库 `_lifecycle.py:244-247` 早已判定
超时判据不许用墙钟（NTP 校时/改时间/时区切换会跳变 ⇒ 误判离线或无限延长超时窗）。
修法是加内存态孪生 `last_update_mono`（单调钟只活在内存，与
`_lifecycle.last_gateway_report_time` 同纪律），读侧有孪生值用它、没有回落墙钟。
"""

import pathlib
import re
import time

from custom_components.window_controller_gateway.const import SENSOR_TIMEOUT_MINUTES
from custom_components.window_controller_gateway.utils import device_is_stale

LIMIT = SENSOR_TIMEOUT_MINUTES * 60
PKG = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "window_controller_gateway"


# ---------- 行为：单调钟在场就不受墙钟跳变影响 ----------

def test_forward_wall_clock_jump_does_not_make_device_stale():
    """墙钟前跳 10 小时（NTP 校正形态）不得把刚上报的设备判成陈旧。"""
    d = {"last_update": time.time() - 10 * 3600, "last_update_mono": time.monotonic() - 5}
    assert device_is_stale(d) is False, "墙钟跳一下就全设备 unknown＝自动化误触发"


def test_backward_wall_clock_jump_does_not_hide_staleness():
    """墙钟后跳不得让失联设备永远显示陈旧值（改前的"无限延长超时窗"形态）。"""
    d = {"last_update": time.time() + 10 * 3600, "last_update_mono": time.monotonic() - LIMIT - 1}
    assert device_is_stale(d) is True


def test_mono_elapsed_beyond_limit_is_stale():
    d = {"last_update": time.time(), "last_update_mono": time.monotonic() - LIMIT - 1}
    assert device_is_stale(d) is True


# ---------- 行为：回落路径必须与改前逐值一致（防"重启即全员 unknown"）----------

def test_restart_backfill_without_mono_falls_back_to_wall_clock():
    """重启回填的设备只有墙钟戳：回落判定必须与改前同结果。"""
    assert device_is_stale({"last_update": time.time()}) is False
    assert device_is_stale({"last_update": time.time() - LIMIT - 1}) is True


def test_missing_timestamp_is_fresh():
    """无时间戳=新鲜（历史形态/测试夹具语义，改前 cover 的 `if _lu and …` 同款）。"""
    assert device_is_stale({}) is False


def test_zero_sentinel_is_fresh_and_is_the_repo_convention():
    """`last_update: 0` 是本仓夹具表示"无有意义时间戳"的哨兵值，必须=新鲜。

    这条是被 test_audit_round6::test_no_timestamp_treated_fresh 打红之后补的：
    helper 初版写 `wall is not None`，于是 0 被算成"1970 年 ⇒ 陈旧"。生产写点恒为
    `time.time()` 取不到 0，差别只在夹具形态，但约定就是约定——sensor 原先
    `is not None` 的读法与 cover 的 `if _lu and` 本就不一致，这里统一到 cover 侧。
    """
    assert device_is_stale({"last_update": 0}) is False
    assert device_is_stale({"last_update": 0, "last_update_mono": 0}) is False  # mono 的 0 也算缺席


# ---------- 反向钉：实体侧不许再出现墙钟时效比较 ----------

def test_no_wall_clock_staleness_gate_left_in_entities():
    for name in ("cover.py", "sensor.py"):
        src = (PKG / name).read_text(encoding="utf-8")
        assert "time.time() - _lu" not in src, f"{name} 回潮：仍用墙钟判时效"
        assert re.search(r"time\.time\(\)\s*-\s*last_update", src) is None, \
            f"{name} 回潮：仍用墙钟判时效"
        assert "device_is_stale(" in src, f"{name} 没走统一时效判据"


# ---------- 同族计数钉：每个墙钟写点必须成对写单调孪生 ----------

def test_every_wall_clock_write_site_has_monotonic_twin():
    src = (PKG / "device_manager.py").read_text(encoding="utf-8")
    lines = src.split("\n")
    dict_sites = [i for i, ln in enumerate(lines)
                  if re.search(r'"last_update":\s*time\.time\(\)', ln)]
    asg_sites = [i for i, ln in enumerate(lines)
                 if re.match(r'^\s*\S+\["last_update"\] = time\.time\(\)\s*$', ln)]
    assert len(dict_sites) == 3 and len(asg_sites) == 3, \
        f"写点数变了（dict={len(dict_sites)} 赋值={len(asg_sites)}）——同族新增点需同步补孪生并更新本判据"
    for i in dict_sites:
        assert '"last_update_mono": time.monotonic()' in lines[i], \
            f"device_manager.py:{i + 1} dict 写点缺单调孪生"
    for i in asg_sites:
        assert '["last_update_mono"] = time.monotonic()' in lines[i + 1], \
            f"device_manager.py:{i + 1} 赋值写点下一行缺孪生（下一行={lines[i + 1].strip()[:50]}）"


def test_monotonic_stamp_is_never_persisted():
    """单调钟跨重启相减是垃圾值，绝不能进持久化文件。

    本仓 `devices` 字典目前整体只在内存（persist.py 落的是 mapping/删除列表），
    这条钉的是"以后谁把 devices 加进 persist 就会红"，不是描述现状。
    """
    payload = (PKG / "persist.py").read_text(encoding="utf-8")
    assert "last_update_mono" not in payload, "持久化侧出现了单调钟字段＝重启后时效判定会全错"

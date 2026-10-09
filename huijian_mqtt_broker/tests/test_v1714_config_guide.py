# -*- coding: utf-8 -*-
"""v1.7.14 钉桩：Web UI「插件配置项中文说明」卡与 Supervisor schema 同步。

背景（用户 2026-09-08 要求）：Supervisor 配置表单只显示英文键名、平台无
字段说明机制（schema 仅类型声明）——中文说明落在我方 Web UI 的
#configGuideCard。本测试防两向漂移：
  1) schema 加了新配置项而说明卡漏讲 → 红（逐 key 查卡在位 + 含中文）；
  2) 说明卡讲了 schema 里不存在的键（改名/删除遗留）→ 红；
  3) 共存桥默认关定案（v1.7.13）的叙述在卡内保住（勿回潮"默认开"）。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG = (ROOT / "config.yaml").read_text(encoding="utf-8")
HTML = (ROOT / "www" / "index.html").read_text(encoding="utf-8")


def _schema_keys():
    """schema: 块=直到下一个顶格键；块内混有注释行，键行须以缩进+词起。"""
    m = re.search(r"^schema:\n(.*?)(?=^\S|\Z)", CFG, re.M | re.S)
    assert m, "config.yaml schema 块锚丢失"
    return [km.group(1) for line in m.group(1).splitlines()
            if (km := re.match(r"[ \t]+(\w+):", line))]


def _guide_block():
    m = re.search(r'id="configGuideCard".*?</details>', HTML, re.S)
    assert m, "Web 配置说明卡（#configGuideCard）整体丢失"
    return m.group(0)


def test_guide_covers_every_schema_key():
    keys = _schema_keys()
    assert keys, "schema 键清单为空？"
    guide = _guide_block()
    missing = [k for k in keys if f"<code>{k}</code>" not in guide]
    assert not missing, f"配置项未进中文说明卡: {missing}"


def test_guide_no_ghost_keys():
    keys = set(_schema_keys())
    guide = _guide_block()
    ghosts = [k for k in re.findall(r"<code>(\w+)</code>", guide)
              if k not in keys]
    assert not ghosts, f"说明卡引用了 schema 不存在的键: {ghosts}"


def test_guide_rows_are_chinese():
    guide = _guide_block()
    for k in _schema_keys():
        row = re.search(rf"<code>{k}</code></td><td[^>]*>(.*?)</td>",
                        guide, re.S)
        assert row, f"{k} 说明行结构损坏"
        assert re.search(r"[\u4e00-\u9fff]{2,}", row.group(1)), \
            f"{k} 的说明缺中文正文"


def test_bridge_default_off_narrative_kept():
    """v1.7.13 定案叙述防回潮（说明卡与 config 默认值同向）。"""
    assert "coexist_bridge_enabled: false" in CFG, "共存桥默认关定案漂移"
    guide = _guide_block()
    row = re.search(r"<code>coexist_bridge_enabled</code></td>"
                    r"<td[^>]*>(.*?)</td>", guide, re.S).group(1)
    assert "默认关" in row and "1.7.13" in row, "桥开关叙述未随定案更新"


def test_discovery_row_narrative_matches_v1762_ruling():
    """v1.7.62 裁定：首台也要在卡片上点确认。说明卡是终端用户唯一的中文解释。

    实发教训（10-09 审计 G-11）：本行自 v1.7.14 写死"零条目时首台全自动、第二台起
    弹卡片确认"，而实现与 README 早已改成"每一台都要确认"——面板对外说的与做的相反，
    用户会等一台"本该自动出现"的网关然后报障。反向臂（不得再出现"全自动"）是判据的
    另一半：只肯定"含首台"而不禁旧口径，改一半也能过。
    """
    guide = _guide_block()
    row = re.search(r"<code>fast_auto_discovery</code></td>"
                    r"<td[^>]*>(.*?)</td>", guide, re.S).group(1)
    assert "含首台" in row, "发现项叙述未随 v1.7.62 裁定更新（首台也要确认）"
    assert "1.7.62" in row, "叙述要写明裁定出处，免得下轮被当成笔误改回去"
    assert "全自动" not in row, "回潮：说明卡又对用户承诺首台不用确认"

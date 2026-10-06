"""商店仓实名一致性钉：加载项元数据与 Web UI 运行时取数必须指同一个仓。

为什么要有这条：迁仓时 `config.yaml`/`repository.yaml` 这类"声明位"很容易改全，
但 `www/js/huijian.js` 里「检查更新」是**运行时**直接打 GitHub/Gitee 的 Releases API，
它不 import 任何配置，改漏了不会报错——面板会安静地在旧仓的 Release 列表里找一个
永远不会出现的版本，失败文案还把用户往旧地址指回去。这是"最后一层"型漏网，
只有拿声明位当判据反查它才咬得住。

期望值从 repository.yaml 现读，不在本文件里硬编码仓名（免得扫描型守卫扫到自己）。
"""

import re
from pathlib import Path

import pytest

ADDON = Path(__file__).resolve().parents[1]
REPO_YAML = ADDON.parent / "repository.yaml"
JS = ADDON / "www" / "js" / "huijian.js"

# 运行时打的两个源：GitHub 与 Gitee 容灾
FETCH_RE = re.compile(
    r"""api/(github|gitee)/repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/releases""")


@pytest.fixture(scope="module")
def store_repo() -> str:
    text = REPO_YAML.read_text(encoding="utf-8")
    m = re.search(r"^url:\s*'?https://(?:www\.)?github\.com/([^'\n/]+/[^'\n/]+)",
                  text, re.M)
    assert m, "repository.yaml 的 url 不是 github.com/<owner>/<repo> 形态"
    return m.group(1).strip().rstrip("/")


def test_web_ui_release_fetch_targets_the_store_repo(store_repo: str) -> None:
    hits = FETCH_RE.findall(JS.read_text(encoding="utf-8"))
    assert hits, "huijian.js 里找不到 Releases API 取数点，判据已失效（上游改名？）"
    wrong = [(host, repo) for host, repo in hits if repo != store_repo]
    assert not wrong, (
        f"Web UI 运行时取数指向 {wrong}，而商店仓是 {store_repo}："
        "迁仓漏了最后一层，面板的「检查更新」会在旧仓里永远找不到新版本")


def test_no_stale_repo_url_in_user_facing_help_text(store_repo: str) -> None:
    """失败提示文案里让用户添加的地址，也必须就是商店仓自己。"""
    js = JS.read_text(encoding="utf-8")
    shown = re.findall(r"https://(?:www\.)?github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", js)
    stale = sorted({r for r in shown if r.lower() != store_repo.lower()})
    assert not stale, f"帮助文案把用户往 {stale} 指，当前商店仓是 {store_repo}"

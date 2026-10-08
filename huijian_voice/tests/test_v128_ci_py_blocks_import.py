# -*- coding: utf-8 -*-
"""CI 工作流里内嵌 python 块的 import 自检（vo-1.2.8 发版实炸换来的）。

病灶：`.github/workflows/ci-voice.yaml` 的 Gitee Release 步，`ed641cf` 把正文提取器
改成正则行首锚定（用了 `re.M`）却没在 heredoc 的 import 行里加 `re` ⇒ 语音线 Gitee
腿自那笔起每次 `NameError: name 're' is not defined` 必炸（Release 作业 success、
Gitee 作业 failure ⇒ 四件套缺一件，客户从 Gitee 容灾源看不到本版）。CI 的 YAML 门
只判"能不能 safe_load"，判不出 heredoc 里那段 python 的语义。

本钉的做法：把工作流里每个 `python3 - <<'PY'` 块抽出来，先 `ast.parse`（语法不过就红），
再比对"块里以属性方式用到的模块名"与"块里 import 进来的名字"。少一个 import 就红。
"""
import ast
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
WORKFLOWS = os.path.join(REPO, ".github", "workflows")

# 只查"块里当模块用"的名字；不在这个集合里的名字不管（业务变量、内建等）
MODULEISH = {
    "ast", "asyncio", "base64", "collections", "csv", "datetime", "glob",
    "hashlib", "io", "json", "math", "os", "pathlib", "re", "secrets",
    "shutil", "string", "subprocess", "sys", "tempfile", "textwrap", "time",
    "traceback", "urllib", "yaml",
}

_BLOCK = re.compile(r"<<-?\s*'(?P<tag>PY[A-Z0-9_]*)'\n(?P<body>.*?)"
                    r"\n[ \t]*(?P=tag)[ \t]*(?:\n|$)", re.S)


def _blocks(path):
    txt = open(path, encoding="utf-8").read()
    out = []
    for m in _BLOCK.finditer(txt):
        body = m.group("body")
        # 去掉 YAML 残留的公共缩进，否则 dedent 不了
        lines = body.split("\n")
        ind = min((len(l) - len(l.lstrip()) for l in lines if l.strip()), default=0)
        body = "\n".join(l[ind:] if len(l) >= ind else l for l in lines)
        out.append((m.start(), body))
    return out


def _imported(tree):
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                names.add(a.asname or a.name)
    return names


def _used_modules(tree):
    used = set()
    for n in ast.walk(tree):
        if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id in MODULEISH):
            used.add(n.value.id)
    return used


def test_every_heredoc_python_block_imports_what_it_uses():
    checked = 0
    bad = []
    for fn in sorted(os.listdir(WORKFLOWS)):
        if not fn.endswith((".yaml", ".yml")):
            continue
        path = os.path.join(WORKFLOWS, fn)
        for off, body in _blocks(path):
            if "import" not in body:
                continue                     # 不是 python 块（shell 片段）
            try:
                tree = ast.parse(body)
            except SyntaxError as e:
                bad.append(f"{fn}@{off}: 内嵌 python 语法不过 {e}")
                continue
            missing = _used_modules(tree) - _imported(tree)
            if missing:
                line = body.split("\n")[0][:40]
                bad.append(f"{fn}（块首行 {line!r}）用了却未 import：{sorted(missing)}")
            checked += 1
    assert checked >= 4, f"只抽到 {checked} 个内嵌 python 块——提取器本身失效了（别让它假绿）"
    assert not bad, "；".join(bad)

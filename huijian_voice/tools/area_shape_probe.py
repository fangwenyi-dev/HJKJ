#!/usr/bin/env python3
"""现网区域地形探针（仓库工具，不进镜像，只读零副作用）。

用途：任何"按区域挑台"的改动，改前改后都要拿**现网真清单**量一次附带影响——
`config/entity_registry/list` ＋设备级区域继承，且区域值取**区域名**
（与 `ha_client._entity_area` 同口径，注册表里存的是 area_id，直接比会全判"不一致"）。

    python huijian_voice/tools/area_shape_probe.py                  # 默认办公 .91
    python huijian_voice/tools/area_shape_probe.py http://192.168.1.18   # 换宿主
    HUIJIAN_HA_TOKEN_FILE=~/gates/.hatok_vm18 python ...            # 换令牌

只走 WS 只读命令（get_states / 三张注册表 list），不订阅、不写、不调服务。
REST 的 `/api/config/entity_registry/list` 在现版 HA Core 已 404 ⇒ 必须走 WS。
凭据只读文件、永不打印。
"""
import asyncio
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))          # huijian_voice/（core.* 与 conftest 同口径）

import aiohttp                                        # noqa: E402
from core import capability                           # noqa: E402
from core.nlu import targets                          # noqa: E402

HA = sys.argv[1] if len(sys.argv) > 1 else "http://192.168.1.91"
TOKEN_FILE = os.environ.get("HUIJIAN_HA_TOKEN_FILE", str(Path.home() / "gates/.hatok"))
_seq = 0


async def rpc(ws, cmd):
    global _seq
    _seq += 1
    cid = _seq
    await ws.send_str(json.dumps(dict(cmd, id=cid)))
    while True:
        msg = await ws.receive(timeout=30)      # 逐帧限时：整轮挂死过一次的教训
        if msg.type == aiohttp.WSMsgType.TEXT:
            d = json.loads(msg.data)
            if d.get("id") == cid:
                if d.get("type") == "result":
                    if not d.get("success"):
                        raise RuntimeError(f"{cmd.get('type')} 失败: {d.get('error')}")
                    return d["result"]
        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
            raise RuntimeError("ws closed")


async def main():
    token = Path(TOKEN_FILE).read_text(encoding="utf-8").strip()
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=2)) as s:
        async with s.ws_connect(HA.replace("http", "ws") + "/api/websocket") as ws:
            d = json.loads((await ws.receive(timeout=20)).data)
            assert d["type"] == "auth_required", d["type"]
            await ws.send_str(json.dumps({"type": "auth", "access_token": token}))
            ok = False
            for _ in range(10):
                m = json.loads((await ws.receive(timeout=20)).data)
                if m["type"] == "auth_ok":
                    ok = True
                    break
                if m["type"] == "auth_failed":
                    raise SystemExit("auth_failed（令牌与实例不配对？）")
            assert ok, "never got auth_ok"
            states = await rpc(ws, {"type": "get_states"})
            eregs = await rpc(ws, {"type": "config/entity_registry/list"})
            dregs = await rpc(ws, {"type": "config/device_registry/list"})
            areas = await rpc(ws, {"type": "config/area_registry/list"})

    area_names = sorted({str(a.get("name") or "").strip() for a in areas} - {""})
    targets.sync_areas(area_names)                  # 与加项同一张动态区表
    id2name = {a["area_id"]: str(a.get("name") or "") for a in areas}
    dev_area = {d["id"]: (d.get("area_id") or "") for d in dregs}
    ent_area = {}
    for e in eregs:
        a = (e.get("area_id") or "").strip() or dev_area.get(e.get("device_id") or "", "")
        if a:
            ent_area[e["entity_id"]] = id2name.get(a, a)

    def fn(x):
        return str(x.get("attributes", {}).get("friendly_name") or "")

    universe = [x for x in states if x["entity_id"].split(".", 1)[0]
                not in capability.UNTOGGLEABLE_DOMAINS]
    words = set(targets.BASE_AREAS) | set(targets._dyn_areas)
    named = [x for x in universe if any(w in fn(x) for w in words)]
    clash = [(x["entity_id"], ent_area[x["entity_id"]], fn(x))
             for x in named
             if x["entity_id"] in ent_area
             and any(w != ent_area[x["entity_id"]] for w in words if w in fn(x))]

    print(json.dumps({
        "ha": HA,
        "states": len(states),
        "registry_entries": len(eregs),
        "with_area": len(ent_area),
        "area_names": area_names,
        "controllable_universe": len(universe),
        "name_head_is_area": len([x for x in universe
                                  if targets.split_area_head(fn(x))[0]]),
        "name_contains_area_word": len(named),
        "name_has_space": len([x for x in universe if " " in fn(x)]),
        "named_and_NO_registered_area": len([x for x in named
                                             if x["entity_id"] not in ent_area]),
        "named_area_word_conflicts_own_area": len(clash),
        "controllable_with_area": len([x for x in universe if x["entity_id"] in ent_area]),
    }, ensure_ascii=False, indent=2))
    print("\n可控域逐台：")
    for x in sorted(universe, key=lambda y: y["entity_id"]):
        print(f"  {x['entity_id']:46s} | area={ent_area.get(x['entity_id']) or '(无)':12s} | fn={fn(x)}")
    if clash:
        print("\n名字房间词与自身注册区域不一致的台（收紧区域闸会动到它们）：")
        for c in clash:
            print("  ", c)


asyncio.run(main())

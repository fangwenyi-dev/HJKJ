#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hub 生命周期真栈回归（本机跑，跨 huijian-cloud-hub 仓）。

为什么要有这个文件：v1.7.41 把"绑定码会过期/可换新"修完了、三仓单测全绿、CI 含
HA 真栈 e2e 全绿，线上仍然绑不上——因为**没有任何一条测试跑过"hub 换了注册表"**。
云托管若未配「存储挂载」，HUB_STORE 就落在容器本地盘上（v0.2.0 线上实测：healthz 回
instances:0 且 uptime:104s），每次重新部署/重启即抹盘，hub 不再认识任何 instanceId；
插件却抱着本地身份无限重连（`_ensure_registered` 只在 instance_id 为空时才注册），
面板显示的就是一个当前 hub 从未签发过的码 ⇒ 小程序侧必然 code_invalid。
这条链只有真进程 + 真 ws + 真 /bind 能证。

（以上是 v0.2.0 的**历史现场**，别把它读成"该去配存储挂载"：hub v0.2.6 起注册表跨重建
存活靠云开发数据库镜像，而「存储挂载 → 对象存储」在云开发桶上实测必失败、会让 Pod 起不来。
下面第 0 臂钉的 storeFile 落在 /mnt/ 只是容器内路径约定，与挂不挂载无关。）

三臂（按线上事实顺序；store 语义已读 hub 源码核对：bindByCode 成功即清空该码，
owns() 按 ownerOpenid 判，所以"绑定关系是否活着"用 /state 探，不用重绑探）：
  A 注册即绑：起干净 hub → HubClient 真长连 → 注册时下发的 6 位码可被 /bind 接受。
  B 保盘重启：同 storeFile 重启 → 插件重连上、instanceId 不变（不重复注册）、
    原绑定仍活着（/state 200 而不是 403 forbidden）。
  C 抹盘重启：删掉 storeFile 再重启（＝线上"重新部署"的等价形态）→ 插件必须自愈：
    换发新身份、换新码，且**新码真能被 /bind 接受**、/cmd 不再 offline。
C 是 v1.7.41 的盲区，也是用户报障的那一条。
"""
import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # huijian_mqtt_broker/
HUB_REPO = os.environ.get("HUB_REPO") or ""
if not HUB_REPO or not (Path(HUB_REPO) / "src" / "server.js").exists():
    print("SKIP 需要 HUB_REPO 指向 huijian-cloud-hub 仓（私有仓，CI 侧不可见）")
    sys.exit(3)

sys.path.insert(0, str(ROOT / "tests"))
import conftest  # noqa: F401  假 HA 包树 + custom_components 路径（与 pytest 同源）
import aiohttp  # noqa: E402
from custom_components.window_controller_gateway import hub_client as hc  # noqa: E402

PORT = int(os.environ.get("HUB_E2E_PORT", "18311"))
BASE = "http://127.0.0.1:%d" % PORT
OPENID = "o_e2e_wechat_user"
INSTALL_KEY = "e2e-install-key"
TMP = Path(tempfile.mkdtemp(prefix="hub-e2e-"))
STORE = TMP / "store.json"
CFG = TMP / "config"

results = []
_hub = None


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (" :: " + detail if detail and not ok else ""))


class FakeDeviceManager:
    """最小 device_manager 替身：HubClient 只用 gateway_sn / devices / 监听注册。"""

    def __init__(self, gateway_sn="GW-E2E-001", devices=None):
        self.gateway_sn = gateway_sn
        self.devices = devices if devices is not None else {}

    def add_status_listener(self, _cb):
        return None

    def remove_status_listener(self, _cb):
        return None


def start_hub():
    global _hub
    env = dict(os.environ, HUB_REPO=str(Path(HUB_REPO).resolve()), HUB_INSTALL_KEY=INSTALL_KEY)
    _hub = subprocess.Popen(
        ["node", str(ROOT / "tests" / "e2e" / "hub_lifecycle_harness.js"), str(PORT), str(STORE)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return _hub


async def http_json(method, path, payload=None, openid_in_body=False):
    """按**生产口径**发请求：云托管是把调用者 openid 注入 `x-wx-openid` 头，
    而 hub 默认不认 body.openid（那是 HUB_TRUST_BODY_OPENID 联调开关，生产不开）。
    e2e 若继续用 body 传身份，测的就是一条线上根本不存在的路径——门控一关就全红。
    openid_in_body=True 只给"验证生产门控确实关着"那一条臂用。"""
    body = dict(payload or {})
    headers = {}
    if not openid_in_body and "openid" in body:
        headers["x-wx-openid"] = str(body.pop("openid"))
    async with aiohttp.ClientSession() as s:
        async with s.request(method, BASE + path, json=body, headers=headers) as r:
            try:
                return r.status, await r.json(content_type=None)
            except Exception:  # noqa: BLE001
                return r.status, {"_raw": (await r.text())[:120]}


async def wait_hub_up(timeout=20.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            st, body = await http_json("GET", "/healthz")
            if st == 200 and body.get("ok"):
                return True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.2)
    return False


async def wait_until(fn, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if fn():
                return True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.25)
    return False


def _kill_hub():
    p = _hub
    if p is None or p.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(p.pid)], capture_output=True)
        else:
            p.send_signal(signal.SIGTERM)
            p.wait(timeout=8)
    except Exception:  # noqa: BLE001
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            pass


async def restart_hub(wipe=False, label=""):
    """重启 hub 进程；wipe=True 时先删 storeFile（＝线上重新部署后的容器本地盘）。"""
    _kill_hub()
    if wipe and STORE.exists():
        STORE.unlink()
    await asyncio.sleep(0.4)
    start_hub()
    if not await wait_hub_up():
        check("%s hub 重启就绪" % label, False, "healthz 超时")
        return False
    return True


async def main():
    # ── 0 跨仓常量对账（这类"两边各写一份"的契约，不一致时只在真机第一次注册才暴露）──
    dk = (Path(HUB_REPO) / "Dockerfile").read_text(encoding="utf-8")
    mk = re.search(r"^ENV HUB_INSTALL_KEY=(\S+)", dk, re.M)
    check("0 加载项内置 installKey 与 hub Dockerfile 逐字相同", bool(mk) and
          mk.group(1) == hc.HUB_DEFAULT_INSTALL_KEY,
          "不一致则加载项注册必然 403（A 侧 %s / C 侧 %s）" % (hc.HUB_DEFAULT_INSTALL_KEY[:4] + "…",
                                                              (mk.group(1)[:4] + "…") if mk else "缺失"))
    srv = (Path(HUB_REPO) / "src" / "server.js").read_text(encoding="utf-8")
    mstat = re.search(r"socket\.write\('HTTP/1\.1 (\d{3})", srv)
    check("0 hub 身份被拒时回的状态码在加载项的可判集合内",
          bool(mstat) and int(mstat.group(1)) in hc.IDENTITY_REJECTED_HTTP,
          "hub 回 %s，而插件只认 %s＝一边改了信号一边没跟上，自愈静默失效"
          % (mstat.group(1) if mstat else "裸断(无状态码)", list(hc.IDENTITY_REJECTED_HTTP)))
    mstore = re.search(r"^ENV HUB_STORE=(\S+)", dk, re.M)
    check("0 hub 注册表落在云托管存储挂载目录（/mnt…）", bool(mstore) and mstore.group(1).startswith("/mnt/"),
          "HUB_STORE=%s ＝容器本地盘，每次部署全体掉绑" % (mstore.group(1) if mstore else "未设"))

    if not await restart_hub(label="A"):
        return
    client = hc.HubClient([FakeDeviceManager()], config_dir=str(CFG), base=BASE,
                         install_key=INSTALL_KEY)
    await client.async_start()
    try:
        # ── A 注册即绑 ────────────────────────────────────────────
        got = await wait_until(lambda: client.connected and client.bind_code, 20)
        inst_a, code_a = client.instance_id, client.bind_code
        check("A 长连建立并拿到 6 位绑定码", got and len(code_a or "") == 6,
              "connected=%s code_len=%s err=%s" % (client.connected, len(code_a or ""), client.last_error))
        st, body = await http_json("POST", "/bind", {"bindCode": code_a, "openid": OPENID})
        check("A /bind 接受注册时下发的码", st == 200 and body.get("ok"), "%s %s" % (st, body))
        # 生产门控：hub 只认云托管注入的 x-wx-openid 头；body.openid 不算身份。
        # 少了这条，"任何人可在公网自选身份"的回归不会被真栈发现（hub 侧 401 先于查码，
        # 所以用废码即可，不消耗任何真实码）。
        st_f, body_f = await http_json("POST", "/bind", {"bindCode": "000000", "openid": OPENID},
                                       openid_in_body=True)
        check("A2 body.openid 不被当作身份（生产门控默认关，公网伪造身份打不进）",
              st_f == 401 and body_f.get("err") == "no_openid", "%s %s" % (st_f, body_f))
        st, body = await http_json("POST", "/state", {"instanceId": inst_a, "openid": OPENID})
        check("A 绑定后 /state 可取（owns 判定通过）", st == 200 and body.get("ok"), "%s %s" % (st, body))

        # ── B 保盘重启：不失联、不重注册、绑定关系仍在 ────────────
        if await restart_hub(label="B"):
            back = await wait_until(lambda: client.connected, 45)
            check("B 保盘重启后插件重连成功", back, "45s 内未重连（退避阶梯未复位也会栽这里）")
            check("B 保盘重启后 instanceId 未变（不重复注册）", client.instance_id == inst_a,
                  "%s -> %s" % (inst_a, client.instance_id))
            st, body = await http_json("POST", "/state", {"instanceId": inst_a, "openid": OPENID})
            check("B 保盘重启后原绑定仍活着（/state 200 而非 403）", st == 200 and body.get("ok"),
                  "%s %s" % (st, body))

        # ── C 抹盘重启：必须自愈成新身份 + 新码可绑 ────────────────
        if await restart_hub(wipe=True, label="C"):
            healed = await wait_until(lambda: client.connected and client.bind_code, 75)
            check("C 抹盘重启后插件自愈并重连", healed,
                  "75s 内未恢复＝身份被拒后没有重注册路径（v1.7.41 盲区，线上表现就是永远绑不上）")
            check("C 已换发新身份（instanceId 变了）", bool(client.instance_id) and client.instance_id != inst_a,
                  "instanceId 仍是 %s＝插件没察觉 hub 已不认识它" % client.instance_id)
            check("C 换发了不同的绑定码", bool(client.bind_code) and client.bind_code != code_a,
                  "码没变＝面板还在显示当前 hub 从未签发的死码")
            st, body = await http_json("POST", "/bind", {"bindCode": client.bind_code, "openid": OPENID})
            check("C 新码真能被 /bind 接受", st == 200 and body.get("ok"), "%s %s" % (st, body))
            st, body = await http_json("POST", "/state", {"instanceId": client.instance_id, "openid": OPENID})
            check("C 自愈后 /state 通（旧绑定失效可见地变成'要重绑'，而不是静默死码）",
                  st == 200 and body.get("ok"), "%s %s" % (st, body))
            st, body = await http_json("POST", "/cmd", {
                "instanceId": client.instance_id, "openid": OPENID, "sn": "A1B2",
                "action": "control", "params": {"attribute": "w_travel", "value": "100"}})
            # 断言必须是"这条命令真走到了 agent"：只判 err!=offline 会被 forbidden 蒙过
            # （身份没自愈时 /cmd 先撞 owns()＝403 forbidden，同样"不是 offline"——首轮实发假绿）
            check("C 自愈后 /cmd 真下发到本机（回执来自长连那侧）",
                  body.get("err") in (None, "control_unavailable") and st == 200,
                  "%s %s" % (st, json.dumps(body, ensure_ascii=False)))
            # ── D 多网关：一个实例必须扛下全部网关（用户报障的那条）────────
            # 只证"端到端带得过去"：命令按条目路由是集成侧的事，由
            # tests/test_v1743_hub_singleton.py 的归属钉负责。
            d1 = FakeDeviceManager("GW-D1", {"D1A": {"state": 1}})
            d2 = FakeDeviceManager("GW-D2", {"D2B": {"state": 2}})
            client.view_builder = lambda sn, gw, dev: {
                "sn": sn, "gwSn": gw, "state": (dev or {}).get("state", 0)}
            client.attach_managers([d1, d2])
            client.mark_state_dirty()
            await asyncio.sleep(1.2)
            st, body = await http_json("POST", "/state", {
                "instanceId": client.instance_id, "openid": OPENID})
            states = (body or {}).get("states") or {}
            got = {k: v.get("gwSn") for k, v in states.items()}
            check("D 一次长连把两台网关的子设备都上行到 hub",
                  body.get("ok") and set(got) == {"D1A", "D2B"},
                  "%s %s" % (st, json.dumps(body, ensure_ascii=False)[:200]))
            check("D 每条设备带自己的 gwSn（小程序按 gwSn 分桶，串了就合成一台）",
                  got.get("D1A") == "GW-D1" and got.get("D2B") == "GW-D2", str(got))
            st, body = await http_json("POST", "/cmd", {
                "instanceId": client.instance_id, "openid": OPENID, "sn": "D2B",
                "action": "control", "params": {"attribute": "w_travel", "value": "100"}})
            check("D 第二台网关的设备也能被远程控制（不再只有第一条）",
                  body.get("err") not in ("offline", "forbidden"),
                  "%s %s" % (st, json.dumps(body, ensure_ascii=False)[:160]))

            # ── E 家庭多人绑定（v1.7.47 / hub v0.2.5）─────────────────────
            # 用**独立注册的新实例**跑（不碰 A/C/D 臂那条长连的身份），全程只用 HTTP：
            # 这条臂要证的是 hub 侧的归属语义在真进程里成立，不需要长连；
            # "member 能控制"用"回 offline 而不是 403"来判——403＝鉴权没过，
            # offline＝鉴权过了但没 agent，正是本臂要的区分。
            print("\n==== E 臂：家庭多人绑定 ====")
            E_OWNER = "o_e2e_fam_owner"
            E_MOM = "o_e2e_fam_mom"
            E_STRANGER = "o_e2e_fam_stranger"
            st, reg = await http_json("POST", "/agent/register", {
                "installKey": INSTALL_KEY, "sn": "GW-E2E-FAM", "fw": "e2e"})
            fam_id = (reg or {}).get("instanceId")
            fam_secret = (reg or {}).get("secret")
            check("E 注册成功（多人绑定臂前置）", st == 200 and bool(fam_id),
                  "%s %s" % (st, json.dumps(reg, ensure_ascii=False)[:160]))

            st, b = await http_json("POST", "/bind", {
                "bindCode": (reg or {}).get("bindCode"), "openid": E_OWNER})
            check("E1 owner 码绑定回 role=owner",
                  st == 200 and (b or {}).get("role") == "owner",
                  "%s %s" % (st, json.dumps(b, ensure_ascii=False)[:160]))

            st, oc = await http_json("POST", "/agent/bindcode", {
                "instanceId": fam_id, "secret": fam_secret, "kind": "owner"})
            owner_code = (oc or {}).get("bindCode")
            st, mc = await http_json("POST", "/agent/bindcode", {
                "instanceId": fam_id, "secret": fam_secret, "kind": "member"})
            member_code = (mc or {}).get("bindCode")
            check("E2 签发成员码不作废 owner 码（分字段的正题）",
                  st == 200 and (mc or {}).get("kind") == "member"
                  and bool(owner_code) and owner_code != member_code,
                  "%s owner=%s member=%s" % (st, oc, mc))
            st, reb = await http_json("POST", "/bind", {"bindCode": owner_code, "openid": E_OWNER})
            check("E2b 签发成员码后 owner 码仍可用（同一 owner 幂等）",
                  st == 200 and (reb or {}).get("role") == "owner",
                  "%s %s" % (st, json.dumps(reb, ensure_ascii=False)[:160]))

            st, mb = await http_json("POST", "/bind", {"bindCode": member_code, "openid": E_MOM})
            check("E3 第二个微信号用成员码绑定 → role=member",
                  st == 200 and (mb or {}).get("role") == "member",
                  "%s %s" % (st, json.dumps(mb, ensure_ascii=False)[:160]))
            st, mst = await http_json("POST", "/state", {"instanceId": fam_id, "openid": E_MOM})
            check("E3b member 能读状态（不再 403）",
                  st == 200 and (mst or {}).get("ok") is True,
                  "%s %s" % (st, json.dumps(mst, ensure_ascii=False)[:160]))
            st, again = await http_json("POST", "/bind", {"bindCode": member_code, "openid": E_STRANGER})
            check("E3c 成员码一次性（同码再绑 → code_invalid）",
                  st == 404 and (again or {}).get("err") == "code_invalid",
                  "%s %s" % (st, json.dumps(again, ensure_ascii=False)[:160]))

            st, cmd = await http_json("POST", "/cmd", {
                "instanceId": fam_id, "openid": E_MOM, "sn": "DEV1", "action": "control",
                "params": {"attribute": "w_travel", "value": "100"}})
            check("E4 member 发控制命令鉴权通过（无长连时回 offline，不是 403）",
                  st == 200 and (cmd or {}).get("err") == "offline",
                  "%s %s" % (st, json.dumps(cmd, ensure_ascii=False)[:160]))
            st, denied = await http_json("POST", "/cmd", {
                "instanceId": fam_id, "openid": E_STRANGER, "sn": "DEV1", "action": "control",
                "params": {"attribute": "w_travel", "value": "100"}})
            check("E4b 陌生人仍被拒（放宽只放宽到 member）",
                  st == 403 and (denied or {}).get("err") == "forbidden",
                  "%s %s" % (st, json.dumps(denied, ensure_ascii=False)[:160]))

            st, mem = await http_json("POST", "/agent/members", {
                "instanceId": fam_id, "secret": fam_secret})
            members = (mem or {}).get("members") or []
            check("E5 /agent/members 用实例凭据可列成员且回 mid+掩码",
                  st == 200 and len(members) == 1
                  and all(m.get("mid") and m.get("openidMasked") for m in members),
                  "%s %s" % (st, json.dumps(mem, ensure_ascii=False)[:200]))
            check("E5b 成员列表不回完整 openid（面板/日志一律掩码）",
                  E_MOM not in json.dumps(mem, ensure_ascii=False), json.dumps(mem, ensure_ascii=False)[:200])
            st, badsec = await http_json("POST", "/agent/members", {
                "instanceId": fam_id, "secret": "wrong-secret"})
            check("E5c /agent/members 错 secret → 403 bad_secret",
                  st == 403 and (badsec or {}).get("err") == "bad_secret",
                  "%s %s" % (st, json.dumps(badsec, ensure_ascii=False)[:160]))

            mid = (members[0].get("mid") if members else "")
            st, kick = await http_json("POST", "/agent/unbind", {
                "instanceId": fam_id, "secret": fam_secret, "mid": mid})
            check("E6 按 mid 踢人成功", st == 200 and (kick or {}).get("ok") is True,
                  "%s %s" % (st, json.dumps(kick, ensure_ascii=False)[:160]))
            st, after = await http_json("POST", "/state", {"instanceId": fam_id, "openid": E_MOM})
            check("E6b 被踢者立刻失去访问权（403）",
                  st == 403 and (after or {}).get("err") == "forbidden",
                  "%s %s" % (st, json.dumps(after, ensure_ascii=False)[:160]))

            full_ok = True
            for i in range(9):
                st, c = await http_json("POST", "/agent/bindcode", {
                    "instanceId": fam_id, "secret": fam_secret, "kind": "member"})
                st2, r2 = await http_json("POST", "/bind", {
                    "bindCode": (c or {}).get("bindCode"), "openid": "o_e2e_fam_m%d" % i})
                if i < 8:
                    full_ok = full_ok and st2 == 200 and (r2 or {}).get("role") == "member"
                else:
                    full_ok = full_ok and st2 == 409 and (r2 or {}).get("err") == "members_full"
            check("E7 前 8 人加入成功、第 9 人 409 members_full", full_ok, "上限校验失败")

            st, leave = await http_json("POST", "/unbind", {"instanceId": fam_id, "openid": E_OWNER})
            check("E8 owner 不能退自己（否则实例无主、没人能管成员）",
                  st == 409 and (leave or {}).get("err") == "owner_cannot_leave",
                  "%s %s" % (st, json.dumps(leave, ensure_ascii=False)[:160]))

    finally:
        await client.async_stop()
        _kill_hub()
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
    bad = [n for n, ok, _ in results if not ok]
    print("\nhub 生命周期真栈: %d passed, %d failed" % (len(results) - len(bad), len(bad)))
    if bad:
        print("红臂: " + " | ".join(bad))
    sys.exit(1 if bad else 0)

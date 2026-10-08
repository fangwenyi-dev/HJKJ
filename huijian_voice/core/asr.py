"""STT 引擎（现役：默认本地 FireRedASR2-AED，回落档 FireRedASR2-CTC；云可配、失败自动回落本地）。

2026-09-08 实证裁定（晚到研究定案，两条都推翻过早期假设，代码以此为准）：
  1. `sherpa-onnx-paraformer-zh-int8-2025-10-07` 包实为 **WSChuan 四川话**模型
     （tar 内 README 实证）→ 剔除；主模型曾定 = **bilingual 流式 int8**（中英，标准普通话）。
  2. API 实测：from_paraformer(tokens, encoder, decoder, sample_rate=16000,
     **feature_dim=80**)——feature_dim=560 会**静默返回空串**（无异常，最恶坑）；
     int8 实测 RTF 0.042 / peak RSS ~400MB（x86，aarch64 预估 2-4× 仍宽裕）。

2026-09-13 A/B 台架裁定（_bench/bench_result.txt，用户批准换引擎）：
  - 默认本地引擎 = **SenseVoice-Small int8**（OfflineRecognizer，中英粤日韩）：
    真实录音不再丢尾（paraformer 实测「下午五」缺「点」）、粤语整句正确、中英
    code-switch/8k 电话带宽显著强；命令音频推理 56-94ms（paraformer 125-175ms）；
    单引擎 peak RSS 370MB（≤ 旧档 413MB）。必须用 **model.int8.onnx**（同包
    fp32 model.onnx 937MB→RSS 1.6GB+，4G 主机不可用，代码不引用）。
  - SenseVoice 输出带 <|zh|><|NEUTRAL|>… 标签 → 必须剥离（_STRIP_TAGS）。
  - 生产链路是 stop 后整句识别（session 契约「仅回一条 stt」），流式模型的增量
    优势本就未使用，换离线引擎零协议损失。
  - Paraformer 当年保留为兼容/回落档（fail-open 不断链）——**该档已于 2026-10-07
    用户点名删除**，见下文最新一节；本条为历史记录。
2026-09-28 追加（用户点名实测的**对比档**，非默认、不参与 fail-open）：
  - `firered_ctc` = FireRedASR2-CTC int8（OfflineRecognizer，中英 + 20 多种方言）。
    它是 FireRedASR2-AED 同一份权重里**只取 encoder + CTC 分支**的导出，attention
    decoder 被排除（导出方官方文档明写）⇒ **官方没有此档的 CER/WER 数字**，
    3.05% 那张表是 AED/LLM 口径，不得张冠李戴。解包 776MB（现役 239MB）。
  - 该档在 v1.2.3 及以前是**显式对比档、不参与 fail-open**（对比档被顶替＝用户把回落档
    的成绩记成 FireRed 的）；v1.2.4 起它升为默认档，回落语义随之改变，见下。
2026-10-07 同日的接入与撤除（v1.2.3 接、v1.2.4 撤，两版间隔数小时）：
  - v1.2.3 曾接入两档"解码期吃热词"的 LLM 档 `qwen3_asr` / `funasr_nano`：1.13.7 实测
    签名里构造期吃 `hotwords` 的就这唯二（sense_voice/paraformer/whisper/各 *_ctc **没有**
    该形参，传了是 TypeError）；上游把热词拼进 prompt（qwen3 system 段、funasr user 段），
    故**挤占 `max_total_len`**。两包系第三方导出件（ModelScope `zengshuishui/*`，导出者
    Wasser1462），官方榜单数字不适用。开发机实测：加载 2.60–3.89s、RTF 0.190–0.199、
    峰值 RSS 1425–1645MB；两档 `result.ys_log_probs` 与 SenseVoice 一样是 `[]`。
  - .91 真机首跑给出的决定性一格：62 条热词＝**209 token**，`before_audio=218`，而该导出件
    的 `max_total_len` 与模型上限都是 **512** ⇒ 音频＋生成只剩 294，且随即出现
    `Result is truncated. max_new_tokens 128 is too small`。也就是说"把清单当热词"在这条
    路上是**按条计费**的，而现网清单里还混着 `Tailscale/Samba/Matter/go2rtc` 这类永远不会
    被说出口的系统实体名。
  - 用户真机对比后判：FireRed-CTC 识别更好、且比这两档快（非自回归 CTC 天然无逐 token
    生成开销），故 v1.2.4 删掉两档、把默认档换成 FireRed-CTC。**代价要写清**：删掉后
    本加载项**再无任何档消费解码期热词**，"清单偏置"只剩解码后的同音/近音改写层
    （`core/nlu/homophone.py`，全档共用、零 token 成本、救不到少字与声学丢音）。
  - 换默认带来的回落链（`ensure_loaded`，判据用 `_DEFAULT_KIND`/`_FALLBACK_KIND`
    常量而非字面）：FireRed 起不来 → SenseVoice（旧默认、239MB、台架基线最全）。
    E2E 就绪门按 lock `default_provider` 派生 need 集，因此**只留主档一项 true**：
    回落档留在必检集＝门等一个没人下的模型直到超时（v1.2.4 的 E2E 实伤）。
    生产侧下载仍只由 `asr.model_key` 驱动。
2026-10-07 当日追加（v1.2.5 提交之后，用户点名删除 `asr_paraformer_bilingual`）：
  - 被删档自 v4.2 起是兼容/回落档、v1.2.4 起是回落链第三级。删除后：回落链**只剩
    一级**（默认 FireRed 坏 → SenseVoice），显式档（含显式 SenseVoice）起不来一律
    **不回落**（对比档被顶替＝用户的实测作废）；存量 settings.json 里
    `local_model=paraformer` 经 `_primary_kind()` 未知值回落自动迁移到默认档。
  - 连带清理：库内最后一条**流式**引擎消失 ⇒ `_local_transcribe` 的流式收流分支
    （1s 尾补静音 / is_ready / get_result_all）与 `_OFFLINE_KINDS`/`_CHUNK` 一并移除，
    全部引擎统一走离线整句解码。原第三级是"FireRed 还在下、SenseVoice 也被清过"
    窗口里的兜底——该可用性取舍随本次删除一并取消（用户拍板，别再擅自加回）。
2026-10-07 当晚再换默认（用户点名）：主档=FireRedASR2-AED、回落档=FireRedASR2-CTC
  （上一代默认；换默认时把回落目标挪到"旧默认"，与 v1.2.4 换默认同规格；判据仍只认
  `_DEFAULT_KIND`/`_FALLBACK_KIND` 常量）。代价（发版说明要写）：新装首启下载面
  776MB→1.23GB 解包（tar 520MB→838,589,068B）、单轮更慢（开发机 RTF 0.31-0.38），
  换得的是用户真机对比的"中文同音段更稳"；SenseVoice 位置=小体积可选档。
云档 = OpenAI 兼容 /audio/transcriptions（whisper 形态），任何异常回落本地（v4.1-②）。
"""
from __future__ import annotations

import asyncio
import functools
import gc
import logging
import re
import threading
import time

from . import audio, const

logger = logging.getLogger("huijian.asr")

# 引擎键位（models.lock.json 的 key）
KEY_SV = "asr_sensevoice_small"
KEY_FR = "asr_firered_ctc"
KEY_FA = "asr_firered_aed"
_KIND_KEY = {"sensevoice": KEY_SV, "firered_ctc": KEY_FR,
             "firered_aed": KEY_FA}
_KIND_LABEL = {"sensevoice": "SenseVoice-Small",
               "firered_ctc": "FireRedASR2-CTC", "firered_aed": "FireRedASR2-AED"}
# 换默认档（用户 2026-10-07 两次点名）：现主档＝FireRedASR2-AED，回落目标＝FireRedASR2-CTC
# （上一代默认；回落只作可用性兜底，主档补就绪即换回）。这两个常量是**唯一真源**：
# `_primary_kind()` 的非法值回落、`ensure_loaded()` 的 fail-open 判据、settings.DEFAULTS
# 三处都指它，不许再写死字面（写死＝换默认时只改一处，出现"默认档自己不落盘、回落档当主档跑"的裂口）。
_DEFAULT_KIND = "firered_aed"
_FALLBACK_KIND = "firered_ctc"
# ── 进程级解码准入（2026-10-07，AED 换默认后新增）───────────────────
# 根因：AED 的代价是**全局量**（并发 × 每轮），而本仓此前每一道闸都是**每会话量**
# （session 的 30s 拒收、52s 预算都只管自己那一轮）。STT 走
# `run_in_executor(None, …)`＝asyncio 默认池（4 核机 min(32, cpu+4)=8 worker），
# 8 轮同 30s ⇒ 排队 8×10.99s 且瞬时 8×1.88GB≈15GB，靶机 8G 直接 OOM。
# 开发机 403 句实测（num_threads=2）：AED RTF 0.320 / 峰值 RSS 1415MB，
# CTC 0.167 / 864MB，SenseVoice 0.022 / 358MB。
# 取 1：`num_threads=2` 是**每轮**的，N 轮并发＝2N 个 ORT 线程压 4 核 ⇒ RTF 随并发
# 劣化（0.32 那个基准只在单轮成立）。串行后正常句（中位 2.75s）单轮约 0.9s，
# 8 轮排队约 7s，仍在预算内；最坏 30s 轮串行排队由 _DECODE_QUEUE_WAIT_S 兜住。
_DECODE_CONCURRENCY = 1
# 排队上限：单轮上界 30s×RTF≈11s，等 30s + 解码 11s = 41s < const.STT_RESULT_BUDGET_S
# 的 52s ⇒ 超上限那轮带具名分因失败，而不是让会话在客户端先判死后还占着准入位。
_DECODE_QUEUE_WAIT_S = 30.0
# （2026-10-07：全库引擎均为离线整句解码——最后一条流式档 Paraformer 删除后，
#  收流分支与归错集合一并移除；若将来再接流式档，尾补静音必须一起做，
#  否则 2026-09-08 台架实锤的丢尾事故会回来。）

# SenseVoice 输出的语言/情感/事件标签：zh/yue/EN/NEUTRAL/Speech/woitn 等
_STRIP_TAGS = re.compile(r"<\|[^<>|]*\|>")


class AsrEngine:
    def __init__(self, settings, model_store):
        self.settings = settings
        self.store = model_store
        self._rec = None
        self._lock = threading.Lock()
        self._busy = 0          # 在飞推理数（卸载避让；审查 F1）——含排队中的轮
        # 进程级解码准入位（见上方 _DECODE_CONCURRENCY 的代价账）。BoundedSemaphore：
        # 多一次 release 当场炸，而不是悄悄把上限放大（上限本身就是这条钉的对象）。
        self._decode_gate = threading.BoundedSemaphore(_DECODE_CONCURRENCY)
        self._loading = False   # 冷启动双载闩（下载/构建期并发诉求直接 False，走礼貌话术）
        self.last_used = time.time()
        # 本轮拿不到结果的具名分因。空串与"用户没说话"在设备侧逐字节同形，
        # 没有这个字段就只能靠人猜（与 v1.0.96「每条路带回具名分因」同口径）。
        self.last_reason = ""

    # ── 引擎选择 ────────────────────────────────────────────────
    def _set_reason(self, msg: str, sink: dict | None = None) -> None:
        """分因双写：进程级 last_reason（面板/状态页读）+ **本轮 sink**。

        v1.1.27 项1：AsrEngine 全进程唯一（main.py 单例），last_reason 是单槽——
        两台卫星/两轮并发时谁最后写谁赢，会话若在**自己没调引擎的轮次**（静音/
        解码器缺失）去读它，就会把上一轮/另一台卫星的分因说成本轮的话。
        故分因必须能按轮取回：调用方传 sink dict，本轮各取各的。"""
        self.last_reason = msg
        if sink is not None:
            sink["reason"] = msg

    def _primary_kind(self) -> str:
        k = str(self.settings.get("stt.local_model", _DEFAULT_KIND) or _DEFAULT_KIND)
        return k if k in _KIND_KEY else _DEFAULT_KIND

    @property
    def model_key(self) -> str:
        """主档 key（_loop_models 按此下载/预热；回落在载不改变主档诉求）。"""
        return _KIND_KEY[self._primary_kind()]

    def loaded_kind(self) -> str:
        with self._lock:
            return getattr(self._rec, "_hj_kind", "") if self._rec is not None else ""

    def stale_kind(self) -> bool:
        """在载引擎 ≠ 配置主档（切档后，或首载走了回落档）→ 主档就绪后换绑。"""
        return self.ready() and self.loaded_kind() != self._primary_kind()

    # ── 模型生命周期 ────────────────────────────────────────────
    def ready(self) -> bool:
        return self._rec is not None

    def _build_recognizer(self, kind: str, d):
        import sherpa_onnx
        if kind == "sensevoice":
            rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(d / "model.int8.onnx"),
                tokens=str(d / "tokens.txt"),
                num_threads=2,
                sample_rate=const.SAMPLE_RATE,
                language="",                      # 自动语种判定（含粤语）
                use_itn=False,                    # 与历史档对齐：不挂 ITN（旧档无 rule_fsts）
                provider="cpu",
            )
        elif kind == "firered_ctc":
            # v1.13.4 实测签名只有 model/tokens/num_threads/decoding_method/debug/
            # provider，**没有 sample_rate**——照 SenseVoice 那套传参是 TypeError，
            # 现场表现为"模型加载失败"而非可读分因。
            rec = sherpa_onnx.OfflineRecognizer.from_fire_red_asr_ctc(
                model=str(d / "model.int8.onnx"),
                tokens=str(d / "tokens.txt"),
                num_threads=2,
                provider="cpu",
            )
        elif kind == "firered_aed":
            # v1.2.5：FireRedASR2 的 AED 分支（encoder + attention decoder 都在，与 CTC
            # 同源权重）。**没有 sample_rate 形参**——照 SenseVoice 那套传参是 TypeError，
            # 现场表现为"模型加载失败"而非可读分因（CTC 档同坑，实测过）。
            # encoder.int8/decoder.int8/tokens.txt 这套文件名与历史 Paraformer 档同名过，
            # 分派错了不会报错、只会拿错模型 ⇒ 靠 lock 的 required_files 与取数目录钉住。
            rec = sherpa_onnx.OfflineRecognizer.from_fire_red_asr(
                encoder=str(d / "encoder.int8.onnx"),
                decoder=str(d / "decoder.int8.onnx"),
                tokens=str(d / "tokens.txt"),
                num_threads=2,
                provider="cpu",
            )
        else:
            raise ValueError(f"未知引擎档：{kind!r}（_KIND_KEY 未登记，不得进入构建）")
        rec._hj_kind = kind    # 路径随 recognizer 走：快照语义 + 分派校验用
        return rec

    def _load_one(self, kind: str, sink: dict | None = None) -> bool:
        """单引擎加载：**只查在盘**，缺失则丢给后台补下载并立即失败（快败）。
        2026-09-26 改：这里原本是同步 `store.ensure(key)`——在请求热路径（executor
        线程）里跑三级跨境下载，一挂就是分钟级；而 `main.py::_loop_models` 本来就在
        后台带退避补下同一批模型，同步那次既冗余又与它抢线程。后果实测：家网 SenseVoice
        未落盘时，每轮语音都被挂死在该线程，设备侧 `T_AWAITING=20s` 先超时 ⇒ **无应答
        也无报错**，面板与语音侧完全看不出是模型缺失。改快败后同一故障变成带因的可读失败。"""
        key = _KIND_KEY[kind]
        d = self.store.model_dir_for(key)
        if d is None:
            try:
                self.store.ensure_async(key)      # 交给后台循环去下，不占死会话线程
            except Exception as e:  # noqa: BLE001
                logger.warning("[STT] 触发后台补下载失败(%s): %s", key, e)
            logger.warning("[STT] 模型未就绪(%s)，已交后台补取——本轮快败", key)
            self._set_reason(
                f"模型资产缺失({key})，已转后台补下载（本轮不等待）", sink)
            return False
        # N2 先卸后载：换档时若不先把旧档从 self._rec 摘掉，构造新档的这几秒里两档
        # 共驻——AED 1415MB + CTC 864MB ≈ 2.28GB 纯 STT（旧 SenseVoice↔CTC 才 1.2GB，
        # 所以这条也是换默认后才变成问题的），靶机 4核8G 叠上在飞解码就是 OOM 入口。
        # 只在"在载的是**另一个**档"时摘（首载/同档重载无对象可摘）；摘了就必须负责：
        # 新档构建失败要把旧档装回去，不能把"切档失败"升级成"这台机没引擎"。
        prev_kind = ""
        with self._lock:
            cur = getattr(self._rec, "_hj_kind", "") if self._rec is not None else ""
            if cur and cur != kind:
                prev_kind = cur
                self._rec = None
        if prev_kind:
            logger.warning("[STT] 换绑 %s → %s：先卸旧档（避免两档共驻 ~2.3GB）",
                           _KIND_LABEL.get(prev_kind, prev_kind), _KIND_LABEL.get(kind, kind))
            # 让上一档的 ORT 权重真还给 OS，而不是等下一次分代回收；只在确实摘掉过
            # 旧档时做——首载/同档重载没有对象要释放，没必要为它停一次全堆 GC。
            gc.collect()
        try:
            rec = self._build_recognizer(kind, d)
        except Exception as e:  # noqa: BLE001
            logger.error("[STT] 模型加载失败(%s): %s", kind, e)
            reason = f"模型加载失败({kind}): {type(e).__name__}: {e}"
            if prev_kind and self._restore_prev(prev_kind):
                reason += f"（已回滚到 {_KIND_LABEL.get(prev_kind, prev_kind)}，功能不中断）"
            self._set_reason(reason, sink)
            return False
        with self._lock:
            self._rec = rec
        self.last_used = time.time()
        logger.warning("[STT] %s 已加载 @ %s", _KIND_LABEL.get(kind, kind), d)
        return True

    def _restore_prev(self, prev_kind: str) -> bool:
        """换绑失败兜底：把刚才为省内存卸掉的旧档装回来（只回滚一层，不递归）。"""
        d = self.store.model_dir_for(_KIND_KEY[prev_kind])
        if d is None:
            logger.error("[STT] 回滚旧档失败：资产已不在盘(%s)", prev_kind)
            return False
        try:
            rec = self._build_recognizer(prev_kind, d)
        except Exception:  # noqa: BLE001
            logger.exception("[STT] 回滚旧档失败(%s)", prev_kind)
            return False
        with self._lock:
            if self._rec is None:
                self._rec = rec
        logger.warning("[STT] 已回滚到旧档 %s", _KIND_LABEL.get(prev_kind, prev_kind))
        return True

    def ensure_loaded(self, sink: dict | None = None) -> bool:
        """同步加载（调用方放 to_thread）。已加载直接 True。
        主档优先；主档缺失/损坏自动回落 FireRed-CTC（fail-open）；显式档不回落。
        sink：本轮分因出参（见 _set_reason），冷却/失败分因按轮取回。"""
        with self._lock:
            if self._rec is not None:
                return True
            if self._loading:
                self._set_reason("模型正在加载（冷启动双载闩拦下本轮）", sink)
                return False
            self._loading = True
        try:
            primary = self._primary_kind()
            p1: dict = {}
            if self._load_one(primary, p1):
                return True
            primary_reason = str(p1.get("reason") or self.last_reason)
            # 回落链（2026-10-07 晚换默认后）。只认"当下主档坐在哪个位置"：
            #   默认 AED 起不来 → FireRed-CTC（上一代默认、非自回归快、盘上最可能在）
            #   显式档（含显式 CTC/SenseVoice）起不来 → 不回落（对比档被顶替＝实测作废）
            # 判据取 _DEFAULT_KIND/_FALLBACK_KIND 常量，不写死引擎名，见其注释。
            # 原第三级（Paraformer）是"FireRed 还在下、SenseVoice 也被清过"窗口里的
            # 兜底，该档已删 ⇒ 窗口内本轮无识别，属用户拍板的取舍，别再擅自加回。
            chain: list = []
            if primary == _DEFAULT_KIND:
                chain = [_FALLBACK_KIND]
            for nxt in chain:
                p2: dict = {}
                if self._load_one(nxt, p2):
                    return True
                primary_reason = primary_reason or str(p2.get("reason") or "")
            # 整条链都起不来时说的是**所选**那档：现场要听到"你配的那档没下来"，
            # 不是回落尝试链的最后一条。
            self._set_reason(primary_reason or self.last_reason, sink)
            return False
        finally:
            with self._lock:
                self._loading = False

    def rebind_primary(self) -> bool:
        """回落档在载/切档后原地换绑主档（推理在飞跳过，_loop_models 下一轮再试）。"""
        kind = self._primary_kind()
        if self.store.model_dir_for(_KIND_KEY[kind]) is None:
            return False
        with self._lock:
            if self._busy or self._loading:
                return False
            if self._rec is not None and getattr(self._rec, "_hj_kind", "") == kind:
                return False
            self._loading = True
        try:
            ok = self._load_one(kind)
        finally:
            with self._lock:
                self._loading = False
        if ok:
            logger.warning("[STT] 已换绑主档 %s", kind)
        return ok

    def unload(self) -> bool:
        """True=已卸载；False=推理/加载在飞本轮跳过（调用方下一轮再试）。"""
        with self._lock:
            if self._busy:
                logger.info("[STT] 推理进行中，本轮跳过卸载")
                return False
            if self._loading:
                # v1.1.27 项2：与 ensure_loaded(:166) / rebind_primary(:191) 同护栏。
                # 旧式只查 _busy——冷载/换绑在飞时本函数清掉的正是 _load_one 马上
                # 要写回的空位：卸载空转 + 面板据返回值谎报"STT 已卸载"（admin_api
                # _reload_models），下一句又冒出新引擎，现场对不上账。
                logger.info("[STT] 模型加载/换绑在飞，本轮跳过卸载")
                return False
            self._rec = None
            logger.warning("[STT] 模型已卸载（省电档）")
            return True

    # ── 识别 ────────────────────────────────────────────────────
    async def transcribe_pcm(self, pcm_s16: bytes, reason_out: dict | None = None) -> str:
        """整句 s16le@16k → 文本。云档优先（若配置），失败回落本地。

        reason_out：本轮具名分因出参（v1.1.27 项1）。会话侧一律走
        transcribe_pcm_with_reason 取本轮分因；本参数同时服务多连接各取各的。"""
        self.last_used = time.time()
        self.last_reason = ""
        if reason_out is not None:
            reason_out["reason"] = ""
        prov = str(self.settings.get("stt.provider", "local_paraformer"))
        if prov.startswith("cloud"):
            cloud = self.settings.get("stt.cloud") or {}
            try:
                return await self._cloud_transcribe(pcm_s16, cloud)
            except Exception as e:
                logger.warning("[STT] 云识别失败(%s) → 回落本地（v4.1-②）", e)
        if not self.ready():
            loop = asyncio.get_running_loop()
            ok = await loop.run_in_executor(
                None, functools.partial(self.ensure_loaded, reason_out))
            if not ok:
                # 轮次级分因：设备侧只看到 stt.text=""，与真静音同形。没有这一行，
                # "说了没反应"只能靠翻加载日志猜时间戳对齐。
                logger.warning("[STT] 本轮空结果：本地引擎未就绪（%s / %s）",
                               self.model_key, self.last_reason or "加载日志见上")
                return ""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, functools.partial(self._local_transcribe, pcm_s16, reason_out))

    async def transcribe_pcm_with_reason(self, pcm_s16: bytes) -> tuple[str, str]:
        """本轮识别 + **本轮**具名分因（v1.1.27 项1）。

        与 transcribe_pcm 的差别只在分因取法：本方法不读进程级 last_reason（会被
        他轮/他卫星覆盖），分因全部经本轮 sink 快照带回。会话侧据此把"静音 /
        引擎没就绪 / 音频解码失败"三类同形空结果拆成三句话。"""
        sink: dict = {}
        text = await self.transcribe_pcm(pcm_s16, reason_out=sink)
        return text, str(sink.get("reason") or "")

    def _local_transcribe(self, pcm_s16: bytes, reason_out: dict | None = None) -> str:
        # F1 双保险：锁内快照当代 rec + busy 计数。此后即便 reaper/reload 把
        # self._rec 置 None，本地快照仍持引用（旧对象析构推迟到本调用返回），
        # 绝不出现「旧 stream 喂新 recognizer」的跨代 UB。分派按 rec 自带 _hj_kind。
        if not pcm_s16:
            return ""
        with self._lock:
            rec = self._rec
            if rec is None:
                # 在载 recognizer 被 reaper/reload 摘走：与静音同形，补分因
                self._set_reason("推理在飞时引擎被卸载（省电档/reload）", reason_out)
                return ""
            self._busy += 1
        try:
            if not self._decode_gate.acquire(timeout=_DECODE_QUEUE_WAIT_S):
                # 准入位被占满：这条必须带具名分因——旧形是排队无上限，8 轮一起挤进
                # ORT，客户端 20s 先判死而服务端还在解码（"说了没反应"的新造法）。
                self._set_reason(
                    f"引擎繁忙：解码排队超 {_DECODE_QUEUE_WAIT_S:.0f}s"
                    f"（在飞解码上限 {_DECODE_CONCURRENCY}），本轮未进解码", reason_out)
                logger.warning("[STT] 解码准入排队超 %ss，本轮放弃", _DECODE_QUEUE_WAIT_S)
                return ""
            try:
                samples = audio.pcm16_to_f32(pcm_s16)
                # 2026-10-07：全部引擎均为离线整句解码（最后一条流式档已删，收流分支
                # 与 1s 尾补静音一并移除）。若将来再接流式档，2026-09-08 那份丢尾事故
                # （「打开办公室射灯」→「打开办公室射」）会回来，尾补静音必须一起做。
                stream = rec.create_stream()
                stream.accept_waveform(const.SAMPLE_RATE, samples)
                rec.decode_stream(stream)
                text = _STRIP_TAGS.sub("", stream.result.text or "")
                # 复位？1.13.7 的 OfflineRecognizer 根本没有那个方法（实测 dir()），
                # 旧形那一行每轮抛 AttributeError 被吞——它从未做任何复位，而且离线
                # 形态本来就每轮新建 stream。
                # 并发安全不靠它：真依据是 DecodeStream 全程 const＋decoder 无实例态
                # ＋ORT Session::Run 线程安全，代价上限由上面的准入位管。
                self.last_used = time.time()
                return text.replace("　", "").strip()
            finally:
                self._decode_gate.release()
        except Exception:
            # 第四轮审计 P2：异常必须落**本轮分因**——旧形只写日志、返回 ""，
            # 回执与"真静音"逐字节同形（设备侧无从区分，只能靠人猜）。
            logger.exception("[STT] 本地识别异常")
            self._set_reason("本地识别异常（详见加载项日志）", reason_out)
            return ""
        finally:
            with self._lock:
                self._busy -= 1

    async def _cloud_transcribe(self, pcm_s16: bytes, cloud: dict) -> str:
        import aiohttp
        base = str(cloud.get("base_url", "")).rstrip("/")
        if not base:
            raise RuntimeError("云 STT 未配置 base_url")
        wav = audio.pcm_to_wav(pcm_s16)
        form = aiohttp.FormData()
        form.add_field("file", wav, filename="utterance.wav", content_type="audio/wav")
        form.add_field("model", str(cloud.get("model") or "whisper-1"))
        if lang := str(cloud.get("language") or self.settings.get("stt.language") or ""):
            form.add_field("language", "zh" if lang.startswith("zh") else lang)
        headers = {}
        if key := str(cloud.get("api_key") or ""):
            headers["Authorization"] = f"Bearer {key}"
        timeout = aiohttp.ClientTimeout(total=float(cloud.get("timeout", 12)))
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(f"{base}/audio/transcriptions", data=form, headers=headers) as r:
                if r.status != 200:
                    # 第四轮审计 P2：错误体截读——r.text() 会把整个响应灌进内存（base_url 误指
                    # 大文件/滴流端点时每轮 12s 内的字节全落内存）；与 tts.py F12 同口径。
                    _err_body = (await r.content.read(8192))[:160].decode("utf-8", "replace")
                    raise RuntimeError(f"HTTP {r.status}: {_err_body}")
                obj = await r.json(content_type=None)
                return str(obj.get("text", "")).strip()

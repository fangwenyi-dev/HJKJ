# 变更日志

所有版本变更记录在此文件中。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)。

## [1.7.56] - 2026-09-30

全仓逐行审计修复批：30 条产品缺陷（A~G）+ 测试假绿面（H 类）一次收口。配套 hub / 小程序 **零改动**。**本批无发版顺序约束**——线协议零字段变更（无新端点、无字段改名、无值形制变化）；唯一新增的是云通道对畸形 `params` 回 `err:"invalid_params"`，小程序未收录该码 ⇒ 走既有兜底文案「控制失败（原码）」，不吞排障线索、也不需要对面改。面板改了 JS/HTML，静态引用 5 处 `?v=` 与 `CURRENT_VERSION` 同步 bump 强制穿透客户端缓存。

**一、崩溃与错误响应（A-1~A-6，外部输入可达）**。WS 握手：`hmac.compare_digest` 对含非 ASCII 的 `str` **直接抛 TypeError**，而握手调用点在 try 之外 ⇒ 未认证方一发 `Sec-WebSocket-Protocol: <非ASCII>,<正确令牌>` 就把契约规定的 401 变成 500 + HA ERROR 栈，且"正确令牌排在非 ASCII 候选之后"的合法客户端一起被拒（本机 aiohttp 3.13.5 双解析器臂实测）；改：候选与 `oldToken` 都先过 `isascii` 闸（令牌字符集本就是 `[A-Za-z0-9_-]`，非 ASCII 永远配不上，筛掉不引入时序面）。云通道一条 `params:[]` 的畸形命令会 `AttributeError` 冒泡出 `_session_once` ⇒ 整条长连断开重连、会话状态与熔断计数被重置、`cmd_result` 永不回；改：类型闸 + 接收循环包 try。其余四条同族：成员视图 body 未判 dict→500；容器型命令 id 让绑定/解绑确认整帧丢弃（`dict.pop([1])` unhashable，与该函数 docstring 自陈的契约正相反）；legacy `device_discovery` 一条脏 name 打断整批入库（`.get(k,默认)` 在"键存在值为 null"时返回的是 **None** 而不是默认值）；持久化 JSON 里一个 `inf` 让 `int()` 抛 `OverflowError` 而兜底只捕 `(ValueError,TypeError)` ⇒ 整个 number 平台建不起来、每次 reload 复发。每处都对齐**同仓已有的**正确写法（`_as_int`、`_ctypes` 的 OverflowError、LAN 通道同款入口），不引入新设计。

**二、静默失效与状态永久错值（B-1~B-7）**。force 迁移重建设备字典丢 `last_update`（本仓为同一缺陷打过三次补丁），15 分钟时效闸对该设备**永久豁免**；且 `attributes` 被清空后，电压 sensor 的"取不到值就早退"排在时效判定**之前** ⇒ 电池电压与状态传感器永久冻结在迁移前的值、网关彻底失联也不转 unknown——两处都修，只补字典那半边等于没补。云通道排障信息被兜底 `type(e).__name__` 无差别覆写 ⇒ 面板只剩"未连接"三个字，`/healthz` mirror.enabled、实例数须为 1 这些真指路信息只活在一次性日志里（同文件另两个 catch 早就定了 `why = str(e) or 类名` 并对凭据降级，同一课只在一个 catch 里学了）。迁移"仅支持开窗器"的类型校验跑在新 manager 上查旧 manager 缓存 ⇒ 恒 None 的死分支。`config_flow:292` 的 `async_set_unique_id` 被上一行注释块的过度缩进带进已 return 的守卫体内 ⇒ 死码。"取消忽略"在 `discovery` 键缺失时整体 no-op 却照记"已取消"并回 200（误点忽略后的**唯一自救入口**失效）。家人码过期的指路文案写在被 `hidden` 的容器里 ⇒ 整块永不显示（绑定码同族逻辑位置是对的，此处漏抄）。被禁用的条目仍被第二个循环喂进设备渲染 ⇒ 同一张卡灰徽标"已被用户禁用"配一排恒 4xx 的按钮（三个入口一起补）。

**三、生命周期 / 泄漏 / 停机（C-1~C-7）**。删掉最后一个条目后域级服务被注销，而 `register_services` 只在 DOMAIN 级 `async_setup` 调过一次、组件一旦进了 `hass.config.components` 就不再跑 ⇒ "删完再加一台网关"面板三条按钮**永久 Service not found**，直到重启 HA；原注释"每次 setup 全覆盖注册"把 DOMAIN setup 当成了条目 setup（注销有落点、重注册无落点＝单向不变量缺反向）。hub 通道缺 STOP 闩锁 ⇒ 关机过程中把刚停掉的长连**重新拉起**，其 STOP 监听注册时事件已派发过 ⇒ 任务与 aiohttp 会话无人回收（同形状的 WS 网关早已定案必须加锁）；本轮再补"已停机就不再注册 `listen_once`"，免得留一只永不触发、句柄无人摘的监听。WS 停机竞态四处一起收：`async_stop` 幂等短路直接 return 不等首次收尾（客户端 close 最坏 ~10s + runner.cleanup）、并发 ensure 越过闩锁后 `current` 仍是 None ⇒ 整个停机过程 9001 继续监听、登记后无复检自关、两处 `pop` 一处判等一处不判等。hub 的 STOP 一次性监听在派发后被二次 `unsub()` ⇒ 每次停机每条目一条 `Unable to remove unknown listener`（v1.7.31 F-A 修过摘除侧，本处照抄了注册侧、漏抄摘除侧）。心跳订阅在卸载期被补装后再无人退订（读句柄早于取消后台任务，而条目数据要到 unload 结尾才 pop ⇒ 它自带的 `data_now is None` 双检恒不成立），reload 一轮多一只挂在已卸载条目上的耳朵继续代答 001。`cleanup` 尾检写 `except (CancelledError, Exception)` ⇒ 把**自己**被取消的信号也吞掉（本仓 hub_client 的注释点名过这条反例，同文件另两段逻辑刻意拆成两个 except）。run.sh 用了镜像里根本不存在的 `pkill`（alpine base 无 procps，busybox 只有 `killall`），而 `2>/dev/null || true` 把 command-not-found 一起吃掉 ⇒ mDNS 清理从未达成：broker 退出后 python 仍广播一个已死/已降权的网关，网关连上即被拒（同族第三次事故，前两例 netstat、getent）。

**四、落盘与内存分叉（D-1/D-2）**。换码/清档/注册三处 `_save_identity` 此前全部裸奔，盘满或 config_dir 不可写时三件后果同时发生：①面板 POST `/hub/bindcode` 直接 500；②由保活循环调用时 keepalive task 抛错退出、被 `gather(return_exceptions=True)` 吞 ⇒ **无日志、`last_error` 不变、本会话期自动换码与保活标脏全部停摆**（正是代码里说要避免的"updatedAt 越来越旧"）；③hub 侧已作废旧码而磁盘仍是旧码 ⇒ 重启后回显一张死码，用户扫它必失败。`_invalidate_identity` 只清 instance 侧字段、不清成员码/成员列表/owner 掩码 ⇒ 面板继续显示**上一个实例**从未签发过的码；且落盘失败即永不自愈——下一轮 `_load_identity` 把刚被 401 拒掉的凭据读回来直接 return 跳过注册 ⇒ 再 401 ⇒ 三次熔断，连 HA 重启都读回同一份死身份，与该函数 docstring"下次连接必然重新注册"的承诺正相反。

**五、配置口径与部署面（E/F）**。awaiting 条目（无 SN）永不进 `async_step_options` ⇒ WS 端口/令牌/开关全无 UI 入口，而 `__init__` 的 awaiting 分支已按"半开口径"起 9001 ⇒ 以 `const.py` 里小程序内置的**公开默认令牌**对外监听、用户改不掉（除非先配一台网关）。WS 聚合、`/security` 视图、令牌持久化、hub 地址/密钥取值四处不滤 `disabled_by`，破本仓自立的"有效配置"单一真源；同时把 22 个 `async_entries(DOMAIN)` 站点逐个归类成"是不是有效配置（必须滤）"与"条目还在不在／它的 entry_id（**不许**滤：滤了把禁用读成已删除，v1.6.17 那类假成功正从这条路出来）"，新增站点不进清单即红、清单里的站点消失也红——把"这处到底该不该滤"变成每次提交都要回答的问题。github/gitee 两条 https 反代开 `proxy_ssl_verify on` + 受信 CA 束 + `verify_depth 2`（nginx 默认不验上游证书 ⇒ 同网段 MITM/DNS 劫持可伪造 release JSON、支配 Web UI 的"最新发布版"徽章；镜像本就带 ca-certificates，开校验零成本；depth 2 是因 gitee 实测两层中间 CA，回默认 1 会判超限 502；缺文件则是 `[emerg]` 整个 UI 起不来，故 Dockerfile 显式列装并被钉住）。nginx 全生命周期此前只在首启试一次、Supervisor watchdog 又只探 2022 ⇒ 探活判据取"10998 有 LISTEN **且** 本容器 nginx 进程在"（`host_network: true` 下 `/proc/net/tcp` 是**宿主**的表，只看端口会被"宿主抢占 10998"骗成健康），探活拆成两个可测函数并逐文件容错（两文件一起喂 awk 时缺失那个是致命错误，会把另一文件已数到的命中一起吞成 0 ⇒ 误拉起；而 `set -e` 下 `X=$(awk …)` 的非零退出会把整个看门狗子 shell 静默杀死）。未引号 heredoc 里的 `$SYS` 被展开成空 ⇒ 安全红线说明文被吞字节（v1.7.10 只钉了反引号形态，`$` 形态漏网）。lint job 补 `timeout-minutes`（此前任一用例挂死要等 Actions 默认 6 小时，而同文件 e2e job 有）。

**六、面板交互（G-1/G-2）**。二次配对被前一次的 60s 定时器拆掉守卫（句柄一丢 + `startPairing` 无重入闸 ⇒ 30s 后再点一次，t=60s 时**上一次的**回调照样执行、把新窗口 delete 掉，配对服务端仍生效的 30 多秒里徽标显示"在线"，用户误判失败反复点击——正是 `PAIRING_UNTIL` 注释自己声明要防的场景）；改：两个 `setTimeout` 句柄记账、重入先清、失败路径保留仍活着的窗口与它的定时器（否则一次失败调用会删掉上一次的生效窗口）。`statusEl` 是 await 之前的旧引用、且守卫开窗晚于两次 await ⇒ 往返期间容器被整体重建时写回孤儿节点，而守卫只护 `badge-warn`（此时是 `badge-ok`）⇒ 配对成功却恒显"在线"；改：徽标一律按 id 重取后再写（与 `loadGatewayDevices`/`updateGatewayDevices` 同口径）。

**七、测试假绿面（H-1~H-7）与两轮对抗复核**。`test_service_catalog_consistency` 的文件头自称三方一致、**从不读 `services.py`**（把注册名改成 `check_gw_status` 并同步改它的自登记清单以绕开自对账，守卫照绿而 `services.yaml` 仍广告旧名 ⇒ 用户调用必 ServiceNotFound）——现补第四条腿真读实际注册集，并给面板 4 条服务调用名接上钉。WS 网关 STOP 路径在测试里恒不可观测（`except Exception # 无 bus 环境（测试桩）` 是为测试挖的逃逸口，除三处外全部 ws 测试的 hass 假件没有 bus ⇒ `_on_ha_stop` 在 tests/ 零引用）——停机不停服这条路径**没有任何测试**，本轮由 C-3 的四条真跑钉用真 bus 兑现。CI 的 110 条跨仓契约此前"本地全跑、CI 一条不跑"，且 `hub_lifecycle_e2e.sh` 无任何 CI step（v1.7.41 抹盘自愈事故链的 35 条真栈断言在 CI 里一条不执行）：新增 `ci_hub_lifecycle.sh` 入口把两条分支彻底分开（不可见 ⇒ `::warning` + exit 0 且**绝不**打印汇总行；可见 ⇒ 真跑 + 自己复核 PASS 计数 ≥ 下限），peer-sync 前移、主 pytest step 注入两个对端仓路径，判据按 YAML 字段路径与 step 顺序取。约 200 处文本/计数钉的存量按"本轮改到哪就补哪条"推进；本轮改到的 8 处（F-1/F-2/F-4/C-7/A-5/A-1/E-2/C-6 外加 B-1 的 sensor 半边）全部从"子串在场"升级为"生效行匹配 / AST 结构位 / 真跑行为"——第一轮对抗复核实测这些钉可被**行首加 `#` 的注释**、**留一个字符串常量而删掉整段行为**（`__code__.co_consts`）、**改名 + 在死代码里保留一次调用**（AST"存在即过"）、**把键挪到 YAML 的 step 缩进**这四种形态满足而全量 1290 照绿，现已全部封死并各配一条同型变异验红。顺带修掉上一批遗留的一处 ruff F841（CI lint 会红）。

**八、门禁与变异核验**：pytest **1306**（收集数＝通过数，零 skip）；变异矩阵**两批 50/50 + 16/16 全 RED**（每条变异都做成语法合法的等价回退，凡 `1 error` 一律不算红——第一批 `D-1c` 就是这样被勘误重做的：它摘掉 `try/except` 留下孤儿缩进行，"红"只证明文件解析不了）；ruff(CI 同参 `--select F,E9,B --ignore B008,B905`)、compileall、`py_compile`、`bash -n`(run.sh + 10 个 e2e)、`node --check`(3 个 JS)、YAML/JSON 解析全绿；版本四源 + 5 处 cache-buster = 1.7.56；跨仓契约钉 **110 passed**（入口复核下限）；hub 生命周期真栈 **35 passed**（含新入口的"可见⇒真跑"分支）；真栈 HA e2e（CI 发布硬门禁）本地 WSL 全链路绿：真 `async_setup_entry`、002→handler→注册表→REST、HomeKit 双机型真发 004、001 首报恰一条代答 + 发现卡、零 SN 等待条目零点击自动添加、WS 9001 常听、500 条 soak；z2m 直连认证臂 Z1–Z5、共存自动桥臂 S0–S6 + T1–T3 全绿。**仍未上真容器的三处**：F-1（未在 alpine 里 `nginx -t`／实拉一次 release JSON）、F-2（看门狗双判据只在抽出的探针函数与真判据行上跑过）、C-3 停机链（真 bus 假件级，未走真 HA 关机）。工作树全程未提交前完成两轮独立复核；审计与逐条处置见本地 `docs/bug-audit-2026-09-30.md`。

## [1.7.55] - 2026-09-29

面板「无感刷新」被一个 `const` 永久冻死。配套 hub / 小程序 **零改动**。**本批无发版顺序约束**：只改 `www/js/huijian.js` 一处声明关键字，是纯浏览器侧修复，线协议零字段变更（无新端点、无新字段、无字段改名）⇒ 任一端先上都不会把另一端断开。

**一、无感刷新跑不完：`deviceListEl`/`statusEl` 声明成 `const`，`await` 之后又重取，赋值即抛 `TypeError` 被 `catch` 静默吞掉（P0）**。`updateGatewayDevices`（无感刷新，:869 起）在第二个 `await haApi('/states')` **之后**按 id **重新取值**（防容器被整体重建后写回孤儿节点），但这两行是 `const` ⇒ `TypeError: Assignment to constant variable.`，被同函数内层 `catch` 吞 ⇒ 网关徽标 `updateGatewayStatus` 与逐设备 `loadDeviceState` **永久执行不到**。用户可见后果：首屏正常，之后在 HA 里开/关、拖滑块、改状态，面板一律停在初始值，直到**手动 F5**，且无报错、无提示。**三条调用路径全中**：每 30s `silentRefresh`、控制命令后 2s、点「状态」按钮检查后 2s——用户以为"刷新在跑"，实际一次都没跑成。

**二、修法**：把这两行改回 `let`，与同段逻辑 `loadGatewayDevices:780-781` 对齐（那里一直是 `let`，本处是漏改），并加三行 v1.7.55 说明注释把因果钉在原地，防将来又被"顺手 const 化"。

**三、新增行为钉 `test_v1755_silent_refresh_behavior.py`（3 用例，node 真跑假 DOM）**：把**真实函数体**按大括号配平从源码里抽出来（不 `eval` 整个文件），配一个最小假 DOM，用 node **真跑**它，探针记录 `loadDeviceState` 调用次数——无感刷新真的刷新了，这个数就必须 ≥ 1。① 元钉 `test_extracted_function_is_real`：抽取锚点一漂移就红（防守卫抽到 0 个函数后静默假绿）；② 主行为钉 `test_silent_refresh_really_updates_device_states`：新旧两条 API 分支各自断言状态真的刷上去；③ 自变异钉 `test_probe_catches_the_const_regression`：把抽出的 `let` 改回 `const` 再跑必须红——钉子自证不是假绿钉（变异只在临时目录，仓库不动）。**四向核验**：基线 `let` 绿 / 目标变异 `const` 红 / 反向变异 `var` 绿（证判据有区分度，不是见改就红）/ `git show HEAD:` 真实缺陷版 `const` 红。

**四、这条没覆盖什么（如实记，别当成已闭环）**：新测试只钉 `updateGatewayDevices` 一个函数，**不覆盖** `loadGatewayDevices`——后者本来就是对的 `let`，但同段没有行为钉。另外假 DOM 有两个会致假的坑：`getElementById('dev-<id>')` 必须返回真实元素（否则 `if (devEl)` 本就跳过）、服务端 `subDevices` 的 id 集合必须与 `querySelectorAll('.device-item')` 逐字相等，两条都写进文件头维护提醒并用 `probe.rebuild === 0` 兜住。`node --check` 只查语法、`test_mobile_v175.py` 对该函数只做源码文本扫描（结构钉），**都抓不到这条运行时缺陷**——这正是要补一条行为钉的理由。

门禁：本地回归子集 **8 个文件 44 passed**（`test_v1755` + `test_mobile_v175` + `test_v1746_panel_render` + `test_v1746_panel_unique_funcs` + `test_v1737_hub_ui` + `test_v1738_hub_qr_ui` + `test_const` + `test_v1716_store_schema`）；版本四源结构钉 `test_audit_round6` / `test_audit_round8` / `test_webui_split_v1625` **83 passed**；`node --check` ×3、`bash -n run.sh`、`compileall` 全绿；版本四源＝**1.7.55**、5 处 cache-buster＝**1.7.55** 全绿。

## [1.7.54] - 2026-09-27

面板「家庭成员」那一行终于可读：每行加「改名」，称呼存在本机。配套 hub / 小程序 **零改动**。**本批无发版顺序约束**：改名是**纯本机操作**，新路由一次都不打云端（`set_member_alias` 只写 `config_dir` 下的一个 JSON，`_http` 不在调用路径上），线协议零字段变更 ⇒ 任一端先上都不会把另一端断开。

**一、成员行只有掩码 openid（`oeh…zS`），对家人等于不可读**。用户一次加了三位家人，面板上就是三行机器掩码，认不出谁是谁、也不敢踢。修法是给每行一个可改的称呼。**为什么存本地而不是云端，是三仓核出来的而不是图省事**：hub 的成员记录字面上就是 `store.js` 里 `concat([{openid, at}])` 两个字段，全仓 `nickname/avatar/昵称` **0 命中**（阳性对照：同一次 grep `openid` 命中 152 处）；小程序侧 `cloud-gw.js` 的 binding 只有 `{sn, role, at}`，而且它**从来没有成员列表**——`/agent/members` 要实例凭据，小程序根本没有。所以"名字存云端"今天买不到任何显示位，代价却是 hub 新写端点 + 发版顺序 + 注册表镜像单文档体积（`HUB_MAX_INSTANCES=1000` 本就是为 512KB 上限定的）。**代价如实记**：称呼只在这台 HA 的面板有效，换 HA 安装或删配置文件要重起名字——面板里那行说明就是为此而写，不让人自己踩。键取 hub 的 `mid`（= sha256(openid) 前 12 位，只随微信账号走）⇒ 人被踢掉再扫回来，称呼仍然对得上同一个人。

**二、顺手修掉一处邻近缺陷**（可以回退，与改名本身不耦合）：成员写操作（「移除」）原来回的是**窄包**（只回 members 那几个键），而面板唯一渲染出口 `applyHubStatus` 吃的是完整视图 ⇒ 用户点完「移除」，面板就在一张刚还在倒计时的有效码旁边显示"未连接 / 绑定码 ------"，直到下次手动刷新。现在「移除」与「改名」共用一个 `_member_op_view` 出口回完整 status_view（**单一出口纪律**：两处各写一份就总有一处漏字段）。

**三、留了哪些缝（别当成已闭环）**：① 改名输入用的是 `window.prompt`，与既有「移除」的 `confirm` 同机制——Ingress 面板与 HA 前端同源，Chrome 那个"跨源 iframe 禁模态框"的干预不该命中，但**这一条没在真浏览器里点过**（要有 HA 实例跑本加载项），面板渲染是用 node + 最小假 DOM 真跑验的；若现场点了不弹框，正解是换行内输入框而不是放弃改名。② 只认**当前成员列表里**的 mid：凭一个不存在的 mid 往文件里写名字没有正当调用方，HTTP 边界自己把关，被拒时面板有中文文案而不是"点了没反应"。③ 落盘失败**不改内存**——否则当场显示新名字、重启后打回掩码，那是比报错更坏的形态。④ 上限 12 字在 Python 与 JS 各写一份（面板要先截一次），两侧同口径由逐条用例对账钉住，不是"看着一样"。

门禁：pytest **1156**（1125 + 31 条 `test_v1754_member_alias.py`）、跨仓契约钉**真跑 110/110**（本地新增的 `alias` 键不打破三仓对账）、ruff / compileall / `node --check` / 版本四源与 5 处 cache-buster=1.7.54 全绿。变异自证 **3 组全红**：删掉 `alias ||` 渲染 → 挂；`MEMBER_ALIAS_MAX` 12→13 → 挂 2 条（上限对账 + 跨语言规范化对账）；成员写操作回包改窄 → 挂 2 条。

## [1.7.53] - 2026-09-27

四角同场模拟（真 hub + 真加载项 + 真小程序模块 + 真 WS 网关，110 条臂）跑出来的三条修复批。配套小程序 **v1.4.32** / hub **v0.2.8**。**本批无发版顺序约束**：A1 是纯客户端时序、B2 是 `/healthz` 加一个字段、B3 只动 CI，三处都不碰线协议（判据取自源码而非推定：`/state`/`/cmd` 的响应体形状不变，老端不读 `version` 键，未知键一律不拒）。

**一、云通道首帧空窗最长 5.3s（小程序侧，本批唯一用户可见缺陷）**。四角同场实测：同一个"注册→绑定→进页面"流程，界面齐备的时长是**二值**的——0.00/0.30s 或 **5.30s**，不是随机的慢。根因用一条对照钉死：加载项的状态上行有 0.3s 合并窗（`hub_client.HUB_STATE_DEBOUNCE_S`），而 `connect()` 里那次立即轮询恰好落在这 0.3s 内；诊断探针显示 owner 绑定后 hub 侧 `/state` **首次非空于 0.00–0.22s**，即 hub 与加载项都没问题，问题是撞了空窗之后**下一班要等整个 5s**（`_pollCloud` 对"成功但 0 台设备"没有任何处理）。用户看到的就是"刚扫完码打开页面是空的"。修：进云通道后若拿到"成功但 0 台设备"，把下一班上膛到 **800ms 补拉**，额度 2 次用满即回 5s，一有设备立即复位；`_cloudPollDelay()` 明确**失败退避优先于补拉**（通道都在失败就不该快拉）；顺带把"复位节奏"和"刚判定这帧是空的"合并成一条方向无关的重新上膛判据 `armed > 该用的`。**成本上限是刻意的**：`callContainer` 按次计费，所以额度只给 2 次——代价见文末"这条没覆盖什么"。新增 4 条钉（首帧补拉 / 额度封顶 / 有设备不补拉 / 退避优先），并做 2 次变异自证：额度改 0 → 3 条红；把退避优先级翻转 → 5 条红（含 2 条既有退避钉，说明合并没削弱旧判据）。**同时收紧了既有台架判据**：模拟里那条"≤6s"在修复后成了松闸（回归也不会红），改成 ≤1.5s。

**二、`/healthz` 不带版本，"线上到底跑哪版"只能靠推理（hub）**。核查生产是 v0.2.6 还是 v0.2.7 时发现：`git diff v0.2.6..v0.2.7` 对 healthz 处理**零改动**，两版响应逐字段同形 ⇒ 远程不可判别，只能退回云托管部署记录做**时间相关性**佐证（deploy 020、`BuildId 2607400383≠0`、FlowRatio 100、tag 后 38 分钟）。修：`/healthz` 增 `version` 字段，值取自 `require('../package.json').version`——**不写死字面量**（写死＝版本一涨就谎报），这条由新增的反向钉管住：把 `version` 改成字面量 `'0.2.7'` 时正向断言挑不出来（它恰好等于当前版本），只有反向那半抓到。代价说清：版本对外可见，但比"运维只能猜"值。

**三、110 条跨仓契约钉在 CI 里从没跑过（本仓）**。CI 拿不到私有的 hub / 小程序仓 ⇒ `cross_repo_contract.sh` 按设计 `exit 3`、pytest 记 skipped、job 照绿，于是"CI 9/9 success"把**没跑**说成了**跑过并通过**。跳过本身没错，错在两种形态在同一个绿灯下不可区分。修：新增 `tests/e2e/ci_contract_pins.sh` 作唯一入口——对端仓可见就真跑**并自己复核 PASS 计数 ≥ 110**（子脚本被改瘪到 3 条也要判失败，因为锚点漂移会抽到 0 条然后全绿），不可见就退出 0（跳过不该阻断发版）但打 `::warning::` + 写进 step summary，且**绝不**打印对账汇总行（那在日志里就是"跑过了"的形态）；`ci.yaml` 增一步按两个**只读** deploy key（`HUB_REPO_DEPLOY_KEY` / `MINIPROGRAM_REPO_DEPLOY_KEY`）clone 对端仓。新增 7 条钉真跑两条分支，含一条内置变异（把子脚本换成"永远只报 3 条"的空壳 ⇒ 入口必须红）。**⚠️ 完全闭环需要运维动作**：给本仓配那两个只读 key，没配时 CI 仍是跳过、但会在 job 页如实写明跳过了多少条。

**过程自证（两条我自己的错被当场抓出，值得记）**：① 给新 step 顺手写了显式 `continue-on-error: false`（本就是默认值），被**既有**硬门钉 `test_hard_gate_still_hard`（判"全文件只允许出现一处 continue-on-error"）打红——冗余声明在计数型不变量里不是中性的；② 为防"CI 缺 pyyaml ⇒ 我新写的 workflow 结构钉走 skip＝又一个假绿"加的判据，第一版写成"任一 step 的 pip 行含 pyyaml"，实测**跨 job 命中也算过**（7 passed 是假绿），收紧成"定位到跑 pytest 的那个 job 且在 pytest 之前"后，删掉那行才真的红。

**这条没覆盖什么（如实记，别当成已根治）**：抹盘重建后界面恢复本轮实测 **11.4s**（修复前有一次采样 0.8s，那是运气顺序）。机制查证过——`/state` 从不回 `offline`，只回 `ok:true` 加当前存的状态，所以 agent 重连那几秒表现为"连续空帧"，超出 A1 有意封顶的 2 次额度，仍要等 5s 那班。放宽到 4 次可压到约 4s，代价是最坏多 2 次计费调用/次进页面——额度是成本决定，未擅自放宽。

门禁：pytest **1125**（1118 + 7 条 B3 新钉）、跨仓契约钉 **110/110**、真栈 e2e **35/35**、四角同场模拟 **107/110**（3 红全是"工作树须等于 tag"那三条自我校验，本轮是 WIP 属预期红，功能臂零红）、hub `npm test` **97**（24+10+25+38）、小程序 `npm test` **338** 行 0 失败、ruff / compileall / node --check / bash -n / 版本四源与 5 处 cache-buster=1.7.53 全绿。

## [1.7.52] - 2026-09-25
复审批的收尾批：把上一份复审报告里"尚未解决清单"中能确证为**真实缺陷**的三条修掉。配套小程序 **v1.4.31**；hub 本轮**零改动**（v0.2.7 已发未部署，仍是 v0.2.6 在线上跑，四条纯加固不急着上）。**本批无发版顺序约束**：加载项与小程序的改动是**向后兼容的加字段**，任一端先上都不会把另一端断开（判据见下）。

**一、`'NaN'` 能一路发到物理开窗器并拿到 ok:true 假成功**。两条通道（LAN `_cmd_control` / 云 `validate_control_params`）的线值格式闸都**豁免 str**，而小程序云通道送上来的正是 `String(value)` ⇒ `NaN` 变成字符串 `'NaN'` 恰好从豁免缝里穿过去，透传 004 到固件。取消豁免的依据不是推断而是逐条核出来的：`const.py` 的属性常量、`_ctypes.py` 的 elif 链（voltage / r_travel / rwp_wind_lock_mode / rwp_winact_speed / rwp_winact_strength）、以及 `_commands.py` 里 `send_ws_raw_004` docstring 原文（"w_travel 的 100/0/101/200/0-100、rwp_wind_lock_mode 0/1 等"）——**合法线值全部是十进制串**，所以不再豁免 str 不挡任何合法命令。闸在**发布之前**（拒绝路径核实过：`control_fn` 不被调用，且如实回 `invalid_params` / `invalid value`，两侧都已有中文文案）。**⚠️ 这条反转了 v1.7.12 F6 的"str 维持透传语义"决定**，代价说清：固件自己的 WS 桥仍不校验，所以加载项这侧比固件严——判据是本协议不存在字符串线值，且该依据现在被钉住（见四），将来真加字符串属性会**测试红**而不是静默挡命令。

**二、LAN 命令回执没有关联字段**。加载项 `control_ack` 不回带任何东西，页面只能退化成"认最早那条在途"的弱 FIFO ⇒ 点「打开」后立刻拖速度滑块，「打开」的回执会被当成速度命令的确认：把未确认的速度值写进本地记忆，或因"打开失败"把速度滑块回退掉。修：加载项所有回执走**单一 `ack()` 出口**回带 `attribute` + `cmdsn`（一个出口统一，免得在七个 return 点各抄一份而漏掉某条路径）；小程序 `sendControl` 发 `cmdsn` 并把两个字段透传给页面，页面 `_takePending` **零改动**即升级成精确配对（它本来就优先按 attribute 匹配）。**向后兼容是从源码确认的，不是假设**：`handle_json_message` 只判 `isinstance(msg, dict)` 再按 `cmd` 分发、**不拒绝未知键** ⇒ 老加载项遇新小程序照常工作；老加载项不回带 ⇒ 字段是 `undefined`，页面自动退回弱 FIFO，与既往逐字节同形。

**三、云模式 LAN 回探失败会让页面显示「未连接」**。上一批加的钉刻意断言"云模式下 disconnect 必须透传"，但云模式下的连接态只该由云通道决定：LAN 回探失败那条 disconnect 让页面立刻报"未连接"，而控制完全正常，要等下一轮 `device_update`（≤5s）才复原——那不是"一闪"，是一次对用户说谎。修：云模式下 `error` 与 `disconnect` 一起压掉，更新类事件照旧透传。**变异核验时顺带查出一件旧事实**：LAN→云自动兜底过程中 `_startCloud` 自己调 `_lan.disconnect()`，那一次回执也被透传 ⇒ 一次断开实际 emit **两条** disconnect，本改动一并修掉。旧钉按新契约改写并补了反向钉（真掉线仍须透传并触发兜底），不是把断言反过来就完事。

**四、跨仓契约钉 100 → 110**。新增 ㉑㉒（LAN 回执关联字段两侧对账 + "加载项回带了但没人读"的反向钉）、㉓（004 四个属性名在加载项 const 与小程序常量两侧逐字对账——这组字面量此前无人钉，抄错就是"命令发了设备不动"且两侧单测各自自洽都不红）、㉔（两条通道的格式模式**必须逐字同串**，否则同一 value 出现"云拒 LAN 放行"的分裂行为；以及"小程序下发的 VALUE_* 全是十进制"这条**取消豁免的前提** + VALUE_* 抽取量元钉防空扫假绿）。防稀释下限抬到 `>=110`。

门禁：pytest **1118**（1080 + 38 条新契约测试，含参数化覆盖 9 个非法线值与 9 个合法值逐个过两道闸）、跨仓契约钉 **110/110**、真栈 e2e **35/35**、WSL 真 HA Core + mosquitto 的 `run_local.sh` 全绿（含 500 条 002 soak）、小程序 `npm test` **328**（327+1：旧钉按新契约拆成两条）、ruff / compileall / py_compile / node --check / bash -n / 版本四源与 5 处 cache-buster=1.7.52 全绿；hub 零改动故 v0.2.7 的 96 条沿用。变异核验 **13/13 精准红 + 1 条反向对照**（新钉 8 条含两条元钉；行为级 5 条；反向＝两侧一起改成同一个串时同串钉必须仍绿，证明它有区分度不是"改什么都红"）。过程中两次把**自己的测量失败**误读成"没咬住"（pytest cwd 算错致 rc=4 无统计行、字符串切片削掉右括号致 import 崩），都重跑修正后才下结论——没有把测量失败当结果交出去。

## [1.7.51] - 2026-09-25

对上一批（加载项 v1.7.50 / hub v0.2.6 / 小程序 v1.4.29）做**第三方独立复审**后查出的缺陷修复批。复审不采信任何声称（提交信息、CHANGELOG、测试名一律不算证据），只认盘上代码与真跑输出：全部门禁逐条重跑并与基线对齐（pytest 1078 / 跨仓契约 55 / 真栈 e2e 35 / hub 91 / 小程序 327 全部相符），P0 用新旧两版 hub 各跑一遍做正反对照（v0.2.5 进程确实死于 `Buffer.from(undefined) ← verifySecret`，v0.2.6 四种 `__proto__`/`constructor`/带换行形态一律 401 且服务继续），并对契约钉做了 12 条变异核验。**结论是上一批修得对、三仓配合正确、无需回滚**；本批改的是复审新查出的 4 条 P2 + 6 条 P3。配套 hub **v0.2.7** / 小程序 **v1.4.30**。**本批无发版顺序约束**——三仓改动都不碰线协议（无新端点、无新字段、无字段改名），任一端先上都不会把另一端断开；上一批那条"hub 必须先上"的硬闸门不适用于本版。

**一、加载项的运维指路与 hub README 正好相反（P2，本仓）**。熔断（连续 3 次被 hub 拒身份）时那条 ERROR 写着"①确认云托管「存储挂载」已挂到 /mnt（否则每次部署即抹）"，而 hub v0.2.6 已把这条结论**整个推翻**：README 明写"**不要配「存储挂载」**"，因为「存储挂载 → 对象存储」在云开发云存储桶类型上实测必失败（平台生成的 cosfs endpoint 缺 `http://` scheme ⇒ 挂载钩子 exit 1 ⇒ **Pod 起不来**），跨重建存活改由云开发数据库镜像负责。也就是说运维照着加载项的日志去做，会把一个"部分用户掉绑"的故障升级成"hub 完全起不来"。修：话术改为指向 `/healthz` 的 `mirror.enabled` 与 `mirror.adopted.instances`、点名 `missing_cred` 就是「API Key 设置」没注入那三个变量，并**显式否定**存储挂载；同批把 e2e driver 里那段 v0.2.0 历史现场也加了按语，免得下一个人把它读成"该去配挂载"。另加**跨仓运维口径钉**（hub README 的否定句 + 加载项的否定句 + 加载项必须提 mirror.enabled），因为这类"两仓各写一份的运维结论"没有任何编译期或单测能发现漂移。既有那条 `assert "存储挂载" in joined` 对**旧的有害话术同样为真**＝假绿，已改成必须匹配"不要…存储挂载"这个否定式。

**二、面板成员区"读取失败"与陈旧名单同屏自相矛盾（P3，本仓）**。一次 `list_members` 失败会把 `lastOpError` 置成 `members_unavailable`，而 `self.members` 仍留着上次成功的 N 条；`renderMembers` 里 `empty.hidden = supported && !readFailed && list.length > 0` 会**强制显示**占位行"成员列表读取失败（云端暂不可达），稍后自动重试"，同时下面的 `ul` 照常渲染出 N 行陈旧成员（还带可用的「移除」按钮），人数也被"读取失败"顶掉。用户同屏看到"失败"和一份名单，还不知道家里有几个人。修：占位行只在**真的一条都没有**时出现（`empty.hidden = supported && list.length > 0`），陈旧这件事改挂在人数上——`membersText` 在读取失败且手上有列表时回「N / 8 人（列表可能已过期）」，一条都没有时才回「读取失败，稍后重试」。成员行本身继续渲染（比一片空白有用）。新增面板场景 7b 在假 DOM 里 node 真跑，两条断言（人数不被顶掉、占位行必须隐藏）各自变异核验精准红。

**三、GET /hub 会被顺带刷新吊住最长 15s（P3，本仓）**。v1.7.50 给这条路由加了"顺带刷成员"，于是它从纯读内存即回变成最坏要等一次云端 HTTP 走完 `HUB_HTTP_TIMEOUT_S=15s`，面板表现为刷新转圈。修：新增 `HUB_PANEL_HTTP_TIMEOUT_S=5.0`，`_http` 支持按调用点覆盖超时，成员读取走短超时（`/agent/members` 在 hub 侧是纯内存读，不落盘不推镜像，正常远快于 1s，5s 已很宽松），超了就如实显示"读取失败"并在下个 120s 节流窗重试。**关键 footgun 一并钉住**：aiohttp 里 `timeout=None` 的语义是**不限时**而不是"用会话默认"，所以 `_http` 只在显式给了 `timeout_s` 时才传 `timeout=`；无脑透传 None 会把会话那道 15s 闸整个拆掉（注册/换码/踢人在云端挂起时永久不返回）。新增一条用假 session 记 kwargs 的行为钉，断言"没给超时时 kwargs 里根本没有 timeout 这个键"。

**四、跨仓契约钉 55 → 100，并把"治假绿的那条钉本身是假绿"修掉（P2，本仓）**。第 ⑯ 条钉的名字就叫"回显字段必须走到云缓存层 gw-router（只钉 normalize 层＝假绿）"，判据却是 `grep -qF 'winactSpeed' gw-router.js`——而**解释这个 bug 的注释里就有 winactSpeed**。复审核验：把首建分支那两行真赋值整行删掉（等于把 v1.4.28 的回归原样放回去），55 条**全绿**；再删更新分支那两行覆盖，跨仓钉与小程序 `winact-echo` **两套测试同时全绿**（后者只断言了"垃圾值不覆盖已知值"，从没断言过"合法新值必须覆盖"⇒ 云模式下速度/力度会冻结在首次轮询的值，用户在 HA 里调过之后小程序永远显示旧的，正是 v1.4.28/v1.4.29 两批要修的那个报障的另一形态）。修：⑯ 改成钉**赋值语法**（首建与更新两支各一条，注释满足不了），并给 position 补上同一条；小程序侧补正向覆盖断言。另补 8 处盲区：LAN 回执文案 14 条英文字面量与加载项 `ws_gateway.py` **连引号一起**逐字对账（裸串会被注释满足——实测把 `"send failed"` 改成 `"send faild"` 裸串钉照样全绿）、两个抽取量元钉（防空扫假绿）、面板 TTL 真消费 `bindCodeTtlS` 与 `#hubCodeTtl`/`#hubMemberTip` 两个落点元素两侧都在、控制/身份/加载项侧错误码从单侧改**两侧**对账且判据用 `err: '<code>'` 返回点字面量、hub `/cmd` 响应体带 `cmdsn` 且 router 真读、`_bindErrText` **不许有死条目**（可达清单逐个返回点核出，hub 加新码时必须显式过一遍"这条真的可达吗"）、以及修掉 `grep -qF X 文件A 文件B` 的 OR 语义（grep 多文件任一命中即 exit 0，与注释宣称的"hub 会回"不是一回事）。全部 12 条新钉逐条变异核验精准红。

**五、测试面同步**。`_http` 新增 `timeout_s` 形参后，21 处 `_http` 桩签名全部等宽化（`project-test-stub-signatures` 那条铁律：桩签名窄于真实现＝测试报 TypeError 而不是验行为，本次 10 条失败全是这个原因，修在桩侧不是修回实现）；`_bindErrText` 的 `registry_full` 是死条目（hub 只从 `/agent/register` 回它，那是加载项的注册通道，小程序从不调）已在小程序侧删除，本仓新增的反向钉会在它被加回来时变红；契约钉的防稀释下限从 `>=15` 抬到 `>=100`（精确下限，条数掉下来必须有人来解释）。

门禁：pytest **1080**（1078 + 2 条面板超时钉；面板场景 7b 是 node 脚本内断言，不占 pytest 条数）、ruff（CI 同款 F,E9,B --ignore B008,B905）、compileall、py_compile、node --check、bash -n、YAML/JSON 解析、版本四源与 5 处 cache-buster=1.7.51 全绿；跨仓契约钉 **100/100**（原 55 + 45）；真栈 e2e **35/35**；WSL 真 HA Core + mosquitto 的 `run_local.sh` 全绿（含 500 条 002 soak）；hub `npm test` **96**（91 + 5 条新钉）；小程序 `npm test` **327**（条数不变：删 1 条死条目断言、加 1 条正向覆盖断言，都在既有用例内）。变异核验合计 **20/20 精准红**（新增钉 12 + hub 自修 6 + 面板自修 2），影子树逐条拆、跑完即删、活树全程未动、每条带阳性对照；其中两条反过来咬出我自己新写的弱钉（LAN 回执裸串被注释满足、`grep` 多文件 OR 语义），已当场改成强判据。

## [1.7.50] - 2026-09-25

三仓联审（加载项 / hub / 小程序）查出的确证缺陷批，本仓 10 条 + 跨仓契约钉 28 → 55 条。配套 hub **v0.2.6**（已部署上线并核验）/ 小程序 **v1.4.29**。**发版顺序：hub 必须先上**——本版长连凭据只发请求头，靠 hub v0.2.6 保留的 query 兜底才不会把已装机用户断开；顺序反了新加载项连不上。

**一、面板「家庭成员」列表恒为空（功能半坏）**。面板只调 GET /hub 与两条 POST，**从不调**那条专用的只读路由 /hub/members，而 status_view 回的是内存里的 self.members（只在 list_members 里被写）⇒ HA 每次重启后打开面板一律显示"只有你一人"、count=0，**看不到也踢不了任何已有家人**，直到点一次「添加家人」才顺带刷准。修：GET /hub 在 connected 且 members_supported 时带**服务端节流**（MEMBERS_REFRESH_MIN_INTERVAL_S=120，成败都算一次，防并发 GET 同时打云端）触发一次 list_members；面板不开就完全不产生调用，老 hub 不触发（那只会白拿一个 404）。另把 loadRemoteControl 纳入 30s silentRefresh（此前只刷设备 ⇒ 连接状态点、二维码、码倒计时全会陈旧），后台标签的跳过语义仍在 setInterval 那侧，不新增后台空转。

**二、身份文件并发写竞态（v1.7.49 移线程池时引入的回归）**。save_identity 用**固定** tmp 名，而 _save_identity 改成 asyncio.to_thread 后两个协程可真并发写同一个 .tmp（改动前是事件循环里的同步调用，单线程天然串行、不可能竞态）⇒ 交错截断 ⇒ 落地半截 JSON ⇒ load_identity 判损坏改名 .bad ⇒ 重新注册 ⇒ **换 instanceId、作废所有人手上的绑定码、云端多一条孤儿**。最易触发是面板连点两次二维码（显式换码刻意不受 120s 节流）。修两道：实例级 asyncio.Lock（按 loop 重建，防绑到已死循环）+ tmp 名唯一（tempfile.mkstemp）。仓内早有先例：persist.py 的 _save_lock 注释原话就是"确保不会有两个协程同时写同一个 .tmp 文件"。

**三、面板错误呈现三处**。① hub 对无主人实例回 no_owner，加载项把它压成 bindcode_rejected（真实 err 只进日志不进视图）⇒ 面板显示"稍后再试或点二维码重试"，而新装用户先点「添加家人」再扫码就会撞上，**重试永远不会成功**（正解是"先自己扫码成为主人"）。② last_error 有 8 个失败写入点但**只有 WS 连上会清**，所有成功路径都不清 ⇒ 一次瞬时失败后面板长期在一张**刚签发的有效二维码旁边**显示"换绑定码失败，页面上的码可能已过期"；同一个槽还让面板操作失败掩盖掉 identity_rejected_loop 这条最有诊断价值的信息。③ 成员类错误码全不在 hubErrorText 的 4 条映射里 ⇒ 点「移除」失败时用户看到"确认框关了、什么都没发生"。修：错误槽**拆 conn / op 两个**（lastError 只留连接类；新增 lastOpError 操作类、**对应操作成功即清零**、并保留 hub 的真实 err 不再压成笼统值），面板拆 hubErrorText / hubOpErrorText 两个纯函数、操作类优先、仍走唯一出口 applyHubStatus；覆盖 no_owner / members_full / rate_limited / registry_full / superseded / bad_secret / unknown_instance / unknown_member / owner_cannot_leave 与全部本地降级值。

**四、list_members 把瞬时网络失败判成"云端版本过旧"**⇒ 面板显示"云端版本过旧，暂不支持"并禁用按钮，而真因是网络。修：给 _http 加带 status 属性的 HubHttpError（**继承 RuntimeError**，既有 pytest.raises 不受影响），只有 hub 明确 **404** 才置 members_supported=False，其余走独立网络错误态、文案区分"读取失败，稍后重试"与"版本过旧"。**不靠字符串解析错误消息**。

**五、加载项无视 hub 回的权威值**。hub 回 expiresInSec 与 membersMax，加载项两个都丢掉、改用自己的 BIND_CODE_TTL_S=600 与 HUB_MEMBERS_MAX；面板还把"10 分钟""8 人"写死 ⇒ hub 一改 TTL，面板就对着一张云端已作废的码继续倒计时，用户扫到 code_invalid 且无人解释。修：一律**优先采用云端值、本地常量只兜底**；index.html 的"10 分钟"包进 #hubCodeTtl 由 JS 按 bindCodeTtlS 覆写（字面量留作兜底）。

**六、长连凭据不再进 URL query**。secret 走 query 就会被任何记 request line 的中间层留档（云托管访问日志、反代、错误上报），与"凭据不回显"纪律相悖（HTTP 的 /agent/* 一直走 body，唯独 WS 例外）；另一隐患是 aiohttp 的 ClientResponseError.__str__ 带完整 URL，将来谁写一句 str(e) 就把 secret 送进 HA 日志。修：改走请求头 x-hub-instance-id / x-hub-secret。**线上已实证自定义头能穿过云托管网关**（带 x-wx-openid → code_invalid，不带 → no_openid），且老加载项的 query 写法在 hub v0.2.6 上照常连上。

**七、小修一批**。_send_lock 此前创建后**从不使用**（死码），而状态上行与命令回执是两个并发 task 写同一条 ws ⇒ 收敛到唯一出口 _send_json 并真正加锁（今天靠 aiohttp 非压缩帧的同步 write 侥幸不交错，协商上 permessage-deflate 就不是了）；async_stop 不再吞 CancelledError；退避加 ±20% 抖动防惊群（hub pod 重启后多台 agent 同步 5/10/20… 重试），地板语义不受抖动影响；上线自检的换码从"挡在接收循环前"挪进保活 task（此前最坏 15s 内下行命令不被处理）；_ensure_registered 改 .get + 明确的 register incomplete；删掉 number.py 里从不被读取的死属性 _state_key，并在 docstring 写明"HA 侧显示 setpoint、小程序侧显示设备回传值"是刻意分叉。

**八、跨仓契约钉 28 → 55 条（本批最重要的一项）**。原 28 条全过，却**放走了一个真缺陷**：小程序云缓存层 gw-router 丢掉了 winactSpeed/winactStrength，而钉只 grep 了 cloud-gw.js——字符串在、值不在，**字符串存在性证明不了数据流贯通**。补：/agent/register 端点（4 个 agent 端点此前只钉 3 个）、**WS 消息词汇**（t:cmd/cmd_result/state、cmdsn、items、params.attribute）、**两条通道故意不同的判别键**（LAN cmd:'control' ↔ 云 action:'control'，谁"顺手统一"就静默断一边）、绑定码 TTL 三处对账（hub 600000ms / 加载项 600s 兜底 / 面板文案）、加载项产的 4 个控制错误码在小程序必须有中文文案、hub 三个新码在面板必须有文案、WS 凭据头名两侧逐字一致 + **hub 的 query 兜底不许被清理**（发版顺序安全网）、加载项必须优先采用 hub 的 expiresInSec/membersMax、winact* 必须走到 gw-router 云缓存层。并把 `grep -qF 'mid'` 这类会被注释与 middle/amid 满足的弱匹配改成精确形态。**6 条变异核验全咬**（影子树全量替换 + 按字节解码，跑完即删）。

**九、真栈 e2e harness 改到生产口径（34 → 35 臂）**。hub 那条 openid 生产门控一加，真栈 e2e 当场红 **20/34**（满屏 401 no_openid / 403 forbidden）——根因是 harness 一直用 body.openid 传身份，**在测一条线上根本不存在的路径**。修法不是给 e2e 开联调开关（那会让它继续测假路径），而是让 http_json 按生产口径把 openid 注入 x-wx-openid 头：20 处调用点一行未改，并新增 A2 臂专钉"只带 body.openid 必须 401"，把门控本身钉进真栈；防稀释计数下限同步抬到精确值。

门禁：pytest **1078**（1006+72）、ruff（CI 同款 F,E9,B --ignore B008,B905，四目标）、compileall、py_compile、node --check、bash -n（9 个脚本）、YAML 6 / JSON 63 解析、版本四源与 5 处 cache-buster=1.7.50 全绿；**CI 的 E2E real stack 硬门禁本地补跑通过**（本机无 Docker，改走 WSL 里的 HA Core venv + mosquitto 跑 run_local.sh，与 CI 同一份 ha_e2e_driver.py：真 async_setup_entry、MQTT→handler→registry→REST 全链路、cover 真发 004、WS 端口监听、零点击自动添加、500 条 002 soak ~199/s 后 HA 仍正常）；跨仓契约钉 **55/55**；跨仓真栈 e2e **35/35**；hub npm test **91**；小程序 npm test **327**。另抓到一条钉自己假绿：面板"除唯一出口外不许有人渲染操作错误"扫到了**自己的函数签名**（_fn 返回的源码含 `function hubOpErrorText(`）⇒ 恒红；改成扫函数体，并变异核验它仍会咬（把调用塞进 renderMembers → 只那一条红）。

## [1.7.49] - 2026-09-24

两处** hub 长连可靠性**修复，源自真机一次"换 pod 后远程控制整条断、必须人工重启集成"的事故复盘。

### 一、身份文件 I/O 移出事件循环（修 HA 阻塞 IO 告警）

`save_identity`/`load_identity` 原先用**同步 `open()`** 读写 `huijian_hub_identity.json`，而调用链
（`refresh_bind_code`/`_ensure_registered` 等）跑在事件循环里，被 HA 的阻塞 IO 检测器点名
（`homeassistant.util.loop`：blocking call to open … hub_client.py）。虽是 WARNING 不致功能坏，
但会拖慢整个事件循环。修：`_load_identity`/`_save_identity`/`_invalidate_identity` 改 async，
文件 I/O 经 `asyncio.to_thread` 放线程池；全部 8 处调用点改 `await`（已逐一核对 enclosing 函数
全为 async，唯一同步调用者 `_invalidate_identity` 的唯一调用者 `_open_ws` 亦为 async，整链安全）。
模块级 `load_identity`/`save_identity` 保留为同步 worker 供线程池调用。

### 二、hub 长连主循环加看门狗（修"换 pod 后不自愈、须人工重启"）

真机形态：hub 云托管换 pod 后，`_run_forever` 主循环**体自身**（退避计算/切片睡眠等行）抛一次
异常 ⇒ 任务退出且无人重启 ⇒ `agentsOnline` 永久 0、小程序无设备/扫码 code_invalid，只能人工重启
集成才恢复（本次事故 17:16→17:38 卡了 22 分钟）。旧代码内层 try 只护住 `_session_once`，护不住
循环体其余行。修：外层加兜底 try/except，循环体自身异常时记 **ERROR** 并 5s 后重启循环（"不死"），
不再静默退出；ERROR 行同时指路便于真机排查。

### 门禁

pytest **1006**（1004+2 新钉：身份 I/O 离线循环钉 + 主循环看门狗钉，均经变异核验精准红）、
ruff(CI 同款 F,E9,B --ignore B008,B905)、compileall、node --check、bash -n 全绿；
跨仓契约 28/28、真栈 hub 生命周期 e2e 34/34。协议与 hub/小程序零改动。

## [1.7.48] - 2026-09-24

两处**测试/CI 卫生**修复，生产运行路径零改动（新环境变量默认不设＝行为与既往逐字节相同）。

### 一、真栈 e2e 不再往生产 hub 注册孤儿实例

`run_e2e.sh`(docker) 与 `run_local.sh`(WSL) 跑的是**真实** `async_setup_entry`，会在
`async_ensure_hub_client` 里拿内置生产默认 `HUB_DEFAULT_BASE` 去 `/agent/register`——两个 harness
都没设 `hub_base`，于是**每次 CI 都在生产 hub 注册表留一条 `sn=E2EGW0000001` 的孤儿实例**
（生产 `/healthz` 一度 `instances=4` 而 `agentsOnline=1`，多出的两条时间戳精确对上 v1.7.46/v1.7.47
两次 CI）。`ha_e2e_driver.py` 对 hub **零断言** ⇒ 纯静默污染，从不让 e2e 变红。

- 修：`hub_client.py` 加纯函数 `resolve_hub_base()`（优先级 `entry.options > HUIJIAN_HUB_BASE 环境变量
  > 内置默认`），`__init__.py` 接线并在覆盖生效时打一行 INFO（覆盖永不静默）；两 harness 都设
  `HUIJIAN_HUB_BASE=http://127.0.0.1:1` 黑洞 → 注册秒失败（拒连→WARNING→退避），全程不触网。
- 新增 `test_hub_base_env.py` **10** 条：纯函数三档优先级 + 走真 `async_ensure_hub_client` 的接线行为
  （env 真能掰成黑洞 / 默认路径仍是生产 / option 压过 env）+ 两 harness 黑洞值双侧钉（漏设或指向生产即红）。
  **5 条变异全咬**（接线回退 / env 档删除 / harness 漏设 / harness 指向生产 / 优先级翻转，各精准红）。

### 二、修一个潜伏的测试顺序污染

`test_v1744` 的 `_quiet` autouse fixture 把整个集成 logger 永久 `setLevel(CRITICAL)` **却不 teardown 还原**
⇒ 任何排在其后、靠 `caplog` 抓 ERROR 的用例（`test_hub_client` 重注册熔断钉）被静默过滤。全量按字母序时
v1744 排在 hub_client **之后**侥幸不炸；任何"先 v1744 再 hub_client"的子集/CI 分片**必红**。已 `git stash`
到纯净树复跑同一子集确认是既有 bug、非本批引入；修＝fixture 里 save/restore level（子集 1 red → 64 passed）。

### 门禁

pytest **1004**（994+10）、ruff(CI 同款 F,E9,B --ignore B008,B905)、compileall、node --check×3、bash -n×9、
JSON×63/YAML 解析、版本四源 + 5 处 cache-buster=1.7.48 全绿；跨仓契约 **28/28**、真栈 hub 生命周期 e2e
**34/34**（含 E 臂多人绑定 16 条）。协议未变，hub/小程序本批零改动。

## [1.7.47] - 2026-09-24

两件事：**家庭多人绑定**（三端同批：hub v0.2.5 / 加载项 v1.7.47 / 小程序 v1.4.28）与
**速度/力度回显**（用户报"调速度调力度还有问题"）。设计与实施计划见 `docs/superpowers/`。

### 一、家庭多人绑定

此前一个 HA 安装只能绑一个微信号（hub 归属是 `ownerOpenid` **单值**，第二人扫码一律
`already_bound` 409）。现在：一个 owner + 最多 **8** 个 member。第一个扫码的人是主人，家人由
**主人在本面板签发的一次性成员码**加入（10 分钟、扫完即清）；主人可踢人、家人只能退自己、
**主人不能退自己**（否则实例无主、没人能再管成员）。

- 面板新增「家庭成员」区：`添加家人` → 成员码二维码（载荷前缀与 owner 码**完全相同**，角色由
  hub 按码查表决定 ⇒ 改二维码字符串也提不了权）；成员列表只显示**掩码 openid** + `移除`；
  满 8 人或云端过旧时按钮禁用并写清原因（能点但必然失败，比不能点更让人困惑）。
- 成员码与 owner 码**分字段**（`memberCode`/`memberCodeExpire`）：否则主人点一次「添加家人」就把
  自己正在扫的 owner 码作废（v1.7.41 踩过的形态）。**无主人时拒发成员码**（`no_owner`）：否则第一个
  扫码的人成了 member，既不能踢人也不能升 owner ⇒ 实例**永久锁死**，唯一出口是重注册＝全员重绑。
- 加载项走**实例凭据**而非 openid（openid 只由云托管注入到小程序请求，加载项根本没有）：
  `/agent/members`、`/agent/unbind`；成员句柄 `mid = sha256(openid)[:12]`（稳定、不可逆推；掩码会撞，
  不能当句柄）。小程序 `/unbind` **只能退自己**——`body.openid` 是 hub 的身份兜底来源，一名两用会变成
  "我声称我是你、然后把你踢了"的提权面。
- **老 hub 降级**：响应缺 `kind` 回显 ⇒ 丢弃返回值 + `membersSupported=False`（面板禁用「添加家人」，
  绝不把 owner 码当成员码显示）；`/agent/members` 404 同理（不显示成"读取失败"）。成员码**不进自动
  补发**（会让已截图发出去的码失效），面板显式请求**不受 120s 节流**。
- 兼容：老加载项不传 `kind` ⇒ 走 owner 码、行为不变（真栈 e2e 原 18 条照过）；注册表**只加字段**，
  老 hub 读新库忽略 `members` ⇒ 回滚不用清库。

### 二、速度/力度回显（此前"只能发不能收"）

设备一直**在上报** `rwp_winact_speed`/`rwp_winact_strength`，插件也解析进了 attributes
（`_ctypes.py:501-513`），HA 滑块靠 `_state_key` 读的就是它——但**两条状态通道都不回传给小程序**：
LAN `device_list` 用 `device_ws_view`（5 字段）、LAN 推送 7 键、云端＝视图 + 单补 windLockMode。
于是小程序滑块只能显示本地记忆值（默认 60/50），换手机、清缓存、或在 HA 里调过就对不上。

- `device_ws_view` 补 `windLockMode`/`winactSpeed`/`winactStrength`（一处补，LAN 列表与云端同时有）；
  LAN 推送改为**取 view 的值**，不再自己算一遍（windLockMode 就是这么漂的：推送里算、列表里没有）。
  入界纪律与 position 同款：越界/不可解析一律 **-1 未知**，绝不把垃圾值当合法数字发出去。
- 小程序：LAN 列表/推送与云 `normalizeStates` 都带上这两个字段；滑块初值改为**设备真值 > 本地记忆 >
  默认**，且 -1/缺失**不覆盖已知值**（老加载项与固件直连不发这些键）；**成功才保存、失败回退**
  （对齐 `number.py` 的 `_revert_to_saved`；此前"下发即保存"，失败也把假值记住）；入参与加载项同口径
  ——四舍五入取整、越界裁剪、**不可解析拒绝下发**（回退 0＝"最弱档"，是静默的反向动作）。

门禁：pytest **994**、ruff/compileall/`node --check`/`bash -n` 全绿；hub `npm test` **59**；小程序
`npm test` **287**；跨仓真栈 e2e **34/34**（原 18 + E 臂 16：owner/member 分流、成员码不作废 owner 码、
鉴权放宽只到 member、踢人后立即 403、上限 8、owner 不能退）；**跨仓契约钉 28 条**（载荷版本位 / 上限 8 /
`kind`+`mid`+`role`+`ownerMasked` 字段名 / 7 个端点 / 10 个错误码文案 / `winactSpeed`+`winactStrength`+
`windLockMode` 回显字段 / 百分比入参口径；缺对端仓以 rc=3 响亮跳过并由元钉真跑验证）。
变异核验 **16/16** 精准红（多人绑定 9 + 速度力度 7），影子树逐条拆、跑完即删、活树全程未动。
## [1.7.46] - 2026-09-24

三端契约对比审查（加载项 ⇄ 小程序 ⇄ 云端 hub）抓出的确证缺陷，本批只修
"不改协议、不改行为语义"的部分；协议级的**家庭多人绑定**另批（设计文档与实施计划
见 `docs/superpowers/`）。

**① 面板 `loadRemoteControl` 被定义了两次 ⇒ v1.7.41 的渲染改进一直是死码。**
`www/js/huijian.js` 里同名函数出现在两处（新版走统一出口 `applyHubStatus`、旧版是
v1.7.37 残留），JS 函数声明**后者覆盖前者** ⇒ 页面加载与 30s 无感刷新跑的都是旧版：
绑定码过期文案（`hubCodeExp`）永不显示、「纳管网关 N 台 · M 个子设备」退回只显示单个
SN，新版只在"点二维码"那条 POST 路径可达。940 条测试全绿的原因更值得记：所有结构钉用
`index("function loadRemoteControl(")` **只取第一处**，钉到的是那个永不执行的版本，
连"除统一出口外不得直调 renderBindQr"的反钉也一起失效。修：删旧版；两个抽取器改成
"出现多次即报错"；新增**重名钉**（扫全部函数名，每个恰好一次）+ 元钉（扫到 0 个即红）。

**② 面板显示云通道故障原因。** `hub_client.status_view()` 早就回了 `lastError`、
`api.py` 也透传了，但 `www/` 全目录 grep `lastError` **0 命中** ⇒ 用户只能看到"未连接"，
排障得去翻 HA 日志。新增纯函数 `hubErrorText()`：只映射 4 个已知码（身份被拒 / 反复被拒 /
换码失败 / 换码被拒），**未知码一律不显示**——面板是给终端用户看的，把 `RuntimeError`
这类异常名摆上去只是噪声。

**③ 小程序云模式控制失败不再甩英文错误码。** `gw-router` 把 hub 回的 `res.err` 原文塞进
`control_ack.msg`，而页面直接 toast 它 ⇒ 用户在云模式下看到裸 `offline`/`timeout`/
`forbidden`。改为 12 个码的中文映射；未收录的码回「控制失败（原码）」——既不吞排障线索，
也不甩裸英文。改前已核过全仓没有任何页面按 `msg` 字面值分支（不会破逻辑）。

**④ 删死码 `GwRouter.prototype.reconnect`**：全仓零调用点（页面的同名方法走 `connect()`），
且它调 `this._lan.reconnect`——`WsGatewayClient` 从来没这个方法 ⇒ 恒 false 的陷阱。
配套加**方法面钉**：`gw-router` 里所有 `this._lan.X` 必须真存在于 LAN 客户端原型上。
另修一条**与实现相反**的注释：小程序侧写"set_token 后固件会重启 WS 网关、需用新 token
重连"，而加载项实现是内存即时生效 + 异步持久化 + **当前连接保持**。

**⑤ `.gitignore` 加密钥安全网**（`*.csv` / `*SecretKey*` / `*.key` / `*.pem`）：本仓推
GitHub + Gitee 两个公开远端，本地下载的密钥文件不该有任何被 `git add -A` 带进去的机会。

门禁：pytest **949**（940 + 9：7 条结构钉 + 2 条**假 DOM 里 node 真跑** `applyHubStatus`
五场景的行为钉）、ruff（CI 同款 `F,E9,B --ignore B008,B905`）、compileall、`node --check`、
`bash -n` 全绿；跨仓真栈 e2e **18/18 rc=0**；小程序 `npm test` **247**（240 + 6，
`all-tests-wired` 22→23 证明新文件已挂链）。变异核验 **8/8** 各精准红自己那条（影子树
逐条拆、跑完即删、活树全程未动）；其中一条反过来咬出我自己的弱钉——删掉一个错误码的
中文文案居然还绿，因为兜底文案也是中文、也不等于原码 ⇒ 已补"不许落兜底"断言。

## [1.7.45] - 2026-09-23

真机报障：`custom_components.window_controller_gateway.hub_client` 打
「hub 换绑定码失败（RuntimeError）」**32 秒内 8 条**（13:02:12–13:02:44）。
先说清这条告警说明什么：**长连客户端这次真的起来了**（1.7.44 修的接线落点生效），
失败发生在"换绑定码"这一步——而 hub 侧同时刻的取证支持"云端正在发布"：
`/healthz` 的 pod 启动时刻落在报错窗口内，之后 62s 连续 6 次采样
`agentsOnline:1`、`instances:1`、`storeFile:/mnt/store.json`、`storeFailures:0` 全程稳定
（存储挂载与单实例两个运维前提已就位，掉绑根因解除）。

真正的缺陷在我们自己这三条（都与"云端抖一下"有关，抖完就该看不见的形态却留下了）：

- **告警把原因丢了**：只打 `type(e).__name__`，而 `RuntimeError` 的消息里带的正是
  `hub /agent/bindcode -> 502`——8 条同样的告警没人说得出是发布中（该重试）、
  凭据不认（该重注册）还是被墙（该看网络）。现在把状态写进日志与 `last_error`；
  万一消息里含凭据（URL 带 secret 那类）则退回类型名，不回显。
- **换码失败不节流**：自动补发挂在"上线自检 + 每个保活 tick"两条路径上，云端发布中的
  几十秒里会话会重连很多次 ⇒ 同一告警刷屏 + 对付费端点无意义连击。加
  `BIND_CODE_RENEW_MIN_INTERVAL_S=120`（成功失败都算一次尝试）；**面板点二维码那条不受节流**
  ——那是用户显式意图，旧码当场作废是刻意的。
- **干净断开＝零间隔立即重连（风暴本身）**：`_run_forever` 见 `_session_once()` 返回 True
  就 `attempt=0; continue`，对端"接受后立刻关"（被 replaced / 网关抽风 / 云端发布中）时
  是**无间隔风暴**。现在阶梯照旧归零（连上过＝端点是通的），但重连间隔留
  `HUB_RECONNECT_FLOOR_S=1s` 地板。
  ⚠️ 第一版这里做错了：改成"会话活不过 10s 就按失败升级退避"，跨仓真栈 e2e 当场红
  **5 条**（C 臂自愈后 `/cmd` 仍 offline、D 臂三条 `states:{}`）——hub 反复重启时阶梯一路
  涨到 300s，把恢复拖成几分钟不可用。e2e 抓的正是这种"单测全绿、真栈才露"的取舍错误。

测试 `tests/test_hub_client.py` 34→41：原因进日志（判据含 `502` 字样）、失败不回显凭据、
失败后节流且过最小间隔仍可再试、面板点击不被节流、干净断开按地板间隔且不升级阶梯、
连不上仍走 5/10/20 指数阶梯、地板值必须 >0（防"风暴保护"被一句 `FLOOR_S = 0` 静默取消）。
既有用例 `renew_only_when_needed` 补了节流放行——否则第三条子断言会被上一轮的节流
"顺带"满足＝假绿。

变异 M18/M19/M20（影子树逐条还原旧行为）各自精准红一条，基线与还原后全绿；
影子树与脚本放系统临时目录，跑完即删。全量 **940 passed** + ruff(CI 同款) + compileall +
bash -n + node --check + 跨仓真栈 e2e **18/18**（含按第一版设计复红、改回后复绿的完整往返），
版本四源与 cache-buster=1.7.45。

## [1.7.44] - 2026-09-23

热修：1.7.43 的多网关改造把 hub 单例的**接线落点**放错了分支 ⇒ 远程控制整条不启动。
真机证据：插件页「远程控制（慧尖云）」显示 `云端连接=未启用`、`纳管网关=—`、绑定码为空。

根因：`async_ensure_hub_client` 只挂在 `async_setup_entry` 的 **awaiting（无网关 SN）分支**
（还重复两次），生产真实路径的**完整设置分支一次都没调**——1.7.42 里那段按条目建实例的
代码被删掉后没在新位置补回。⇒ HA 重启后 `hass.data[DOMAIN]` 里没有单例键，
`/api/window_controller_gateway/hub` 回 `enabled:false`，hub 侧 `agentsOnline:0`。
awaiting 分支那两处同样有害：该支 `_hub_managers()` 恒空，调用的净效果只剩"把长连停掉"。

- 完整设置分支在 `async_ensure_ws_gateway` 之后补一处 ensure（与 WS 单例同批落点）；
- awaiting 分支两处全删；
- 「hub 长连在此启动」那段失效注释挪到真调用处（注释不是证据——1.7.42 同型教训）。

测试（新增 `tests/test_v1744_hub_wiring.py` 8 条）。本批根因也是**测试形态**：46 条 hub
单测全部直接调 `async_ensure_hub_client(hass)`、真栈 e2e 直接 `HubClient(...)`，接线层零
覆盖，而"反钉 setup 里不得按条目建 client"只判**不存在**（单侧钉，挡不住"该在的没了"）。
两条腿一起补：

- 行为：真跑 `async_setup_entry` 完整分支（外部依赖打桩、hub 接线不打桩），断言 setup 完
  单例已建且 `started==1`；第二条网关注册后仍是**一个**实例挂两条 manager；起不来不留半个注册。
- 结构：按 `if not gateway_sn:` 把 setup 切两支分别判计数（完整支 ==1、awaiting 支 ==0），
  配「两支 WS 计数各 ==1」元钉作锚点存活证据，另加全仓调用点总数 ==3 的计数钉。

变异 M17（影子树删掉完整支的 ensure＝还原 1.7.43 形态）：新钉 5 条红，老 46 条 hub 测试
**全绿**——盲区就此复现，也正是它能发布出去的原因。

## [1.7.43] - 2026-09-23

用户报"只把一台网关给了小程序"。根因是**归属粒度错**：`HubClient` 按 config entry 各建一个
（`__init__.py` 原 :571），N 台网关就注册 N 个实例、各拿一个绑定码，而身份文件
`huijian_hub_identity.json` 本来就落在全局 config_dir ⇒ 多条目**互相覆盖同一份身份**；
HA 重启后它们又各自读回同一个 instanceId 去连，hub 的 `onAgent` 用
`close(4000,'replaced')` 顶掉前一条 ⇒ N 台网关抢一条长连（重连战争），小程序只看到其中一台。

- **归属改成"一个 HA 安装一个实例"**：新增 DOMAIN 级 `async_ensure_hub_client()` /
  `async_stop_hub_client()`，挂在 `async_ensure_ws_gateway` 的同一批调用点
  （条目 setup 尾部两处、unload、删除条目后），照抄 WS 单例三条纪律：幂等、
  **判等再 pop**（`async_stop` 有真实让出点，无条件 pop 会删掉别人刚登记的实例）、
  STOP 监听只注册一次（v1.7.33 那条"句柄不存不摘"教训）。
- **状态上行跨条目聚合**：`collect_state_items()` 遍历全部 manager，每条设备带自己的
  `gwSn`——小程序云模式本来就按 `gwSn` 分桶（`gw-router._cloudCache`），所以
  **小程序与 hub 协议零改动**就能看到全部网关。
- **命令下行**：`_make_hub_control(hass)` 早就是安装级（遍历条目、按设备 SN 命中所属
  条目才发 004），本批只把它钉住：非所属条目的 handler 必须一次都不被调用
  （广播＝同一台子设备收到两条重复控制）。
- **面板不再说谎**：标签「本机网关」→「纳管网关」，值改为 `N 台 · M 个子设备（SN…）`；
  纯函数 `hubGatewayText()` 用 node **真跑**验证（含老集成只回 `gatewaySn` 的兼容路径、
  `deviceCount` 缺失不得出 NaN）。
- 测试：新增 `test_v1743_hub_singleton.py` 12 条（单例幂等/换挂不重建/最后条目撤走须停并摘键/
  单例键不被当成条目/命令归属/面板文案真跑/反钉"setup 里不得再按条目建 client"、
  "api 不得遍历条目取第一个"）；`test_hub_client.py` 29→34（多网关 items、监听挂满并可摘、
  `gateways[]` 视图、注册 sn 取首个、空 manager 不得抛）；真栈 e2e 加 **D 臂** 3 条
  （一次长连带两台网关的设备、gwSn 不串、第二台也能被控制）→ 18/18。
- 运维可见性：多条目老用户升级后会**合并成一个实例 ⇒ 绑定码换发，需重扫一次**；
  hub 侧会留下 N-1 个孤儿实例（不影响功能，`/healthz` 的 `instances` 会虚高）。

## [1.7.42] - 2026-09-22

v1.7.41 发完后真机仍绑不上（用户第二条日志：`[bind] 载荷解析: 命中` → `绑定返回: code_invalid`）。
线上取证 `GET /healthz` 回 `{"instances":0,"uptime":104}` —— **hub 刚重启过、注册表是空的**：
`HUB_STORE=/data/store.json` 落在云托管容器本地盘上，每次部署即抹。于是当前 hub 根本不认识
本机 instanceId/secret，而面板显示的正是它从未签发过的码。

- **自愈路径补上**：`_open_ws()` 统一握手，握手返回 401/403 ＝ "云端不认识本机身份" ⇒
  `_invalidate_identity()` 清空并落盘身份（重启进程也不会复活死身份）→ 立即重注册 → 换发新码。
  只补这一次，再失败交给外层退避。
- **判据刻意收窄**：网络层失败（ServerDisconnectedError / ClientOSError / 连不上）**一律不清身份**。
  旧 hub 被拒时是裸 `socket.destroy()`，客户端只能看到 ServerDisconnectedError＝与"网络抖一下"
  同形 ⇒ 必须配套 hub v0.2.1（被拒时先回 `HTTP/1.1 401` 再断）。若在这里也清身份，每次断网都会
  多发一次 register、把用户手上正在扫的活码作废。
- **重注册熔断**：连续 3 次【被拒→重注册→再被拒】后停止自动重注册并打 ERROR。病态形态是云托管
  多副本且注册表不共享（register 落到 A、握手落到 B）——不熔断就是每轮退避白造一个孤儿实例、
  反复作废用户手上的绑定码。连上任何一次即清零计数（否则一次故障永久废掉自愈能力）。
- **新增真栈回归 `tests/e2e/hub_lifecycle_e2e.sh`**（真 node hub 进程 + 真 HubClient + 真 /bind，
  跨 huijian-cloud-hub 仓）三臂 12 条：A 注册即绑 / B 保盘重启（instanceId 不变、原绑定仍活着）/
  C **抹盘重启**（换新身份、换新码、新码真能被 /bind 接受、/cmd 真下发到本机）。
  修复前 C 臂 5 条全红，复现的就是线上那条链。另有 3 条**跨仓常量对账**（installKey 逐字相等、
  hub 被拒状态码 ∈ 插件可判集合、HUB_STORE 落在 /mnt）——这类"两边各写一份"的契约不一致时
  只在真机第一次注册才暴露。hub 仓不可见时 loud skip（exit 3），不静默放行。
- 测试：`test_hub_client.py` 21→29（401/403 清身份并重注册、429/500/502/503 与裸断**不**清身份、
  清过身份必须落盘、握手只能经 `_open_ws` 且只补连一次、熔断触发与计数复位）
  + `test_v1742_hub_identity.py` 7 条（三臂防删防稀释、skip 必须非 0 且真跑一次、
  `/cmd` 臂不得被 forbidden 蒙过）；`ws_gateway.py` 文档串里的「Matter 网关」→「LoRa 网关」（命名口径）。
  桩修正：真 `session.ws_connect()` 既 awaitable 又可 async-CM，`FakeCM` 只实现了后者＝桩比真实现窄。
- **部署侧配套（huijian-cloud-hub v0.2.1）**：`HUB_STORE` 默认改 `/mnt/store.json`（云托管的持久化
  形态是「存储挂载→对象存储」，不是块盘），`_save()` 不再让 IO 异常串进请求路径，
  `/healthz` 自证 `storeFile`/`storeSaved`/`storeFailures`。**且服务实例数必须固定为 1**——
  `/cmd` 靠容器内存里的长连表转发，多副本必然随机 `offline`（共享存储救不了这件事）。

## [1.7.41] - 2026-09-22

两条现场问题一批收：**绑定码 10 分钟后永久失效、且面板没有换新路径**（用户真机"扫码绑定失败"的直接原因），以及卡内文案补上"去哪儿找小程序"。

### Fixed

- **绑定码只发一次、过期即死，而面板一直显示它**：hub 侧码只在注册时生成（TTL 10 分钟）、绑定成功即清空，**没有任何轮换路径**；插件侧 `status_view()` 又无条件回显身份文件里那个码——于是 10 分钟后面板上摆的是个死码，而"刷新本页（或点一下右边的二维码）即可看到当前码"这句提示在系统里**根本没有实现**，用户只会一路拿到 `code_invalid`（真机实报）。
  - **hub（v0.2.0）**：新增 `POST /agent/bindcode`（instanceId + secret 鉴权）轮换绑定码——旧码当场作废、TTL 重新计时；已绑定实例也允许轮换（同 openid 重绑幂等，他人仍 `already_bound`）。
  - **插件**：身份文件记 `bindCodeAt`（签发时刻，重启后仍能判过期）；`status_view()` 出 `bindCodeExpiresIn` / `bindCodeExpired`；新增 `refresh_bind_code()`；**上线自检**与**保活 tick** 发现码快到期/已过期即自动补发；面板显示「剩余 X 分钟」或「已过期，点右边的二维码换新码」，**点击二维码改走 POST 换新码**（老版本集成无此路由时回退只读刷新，不显示成故障）。
- **卡内文案漏了小程序名**：微信里要搜「小慧语音」才找得到，只写"打开小程序"客户搜不到；改为 `手机打开小程序 → 搜索「小慧语音」→「LoRa 网关」页 →「绑定微信远程控制」→ 扫一扫右边的二维码 / 或输入 6 位数字。`

### Tests

- hub：`tests/run.js` 15 → **19**（轮换鉴权 403 不探存在性 / 旧码当场作废 / 新码可绑 / 已绑定实例可换码且他人仍 already_bound / TTL=0 立死）。
- 插件：`test_hub_client.py` 17 → **21**（过期判定与视图、轮换成功/被拒/网络异常三条路、只在"快到期"时才补发、**自动补发真接在 `_session_once` 与 `_keepalive_loop` 两条活路径上**——AST 钉，防死码凑数）；`test_v1737/1738_hub_*` 渲染钉改到统一出口 `applyHubStatus`，新增「点击＝换码（POST）」「过期态与指路」「换码路由 = api.py 注册路由」「读取失败与未启用必须分开」。
- 全量 **893** 用例通过（基线 887）；`bash -n` 全 shell、`compileall` 递归、ruff（F,E9,B）、`node --check` 全部前端 JS、四源版本一致。

### 配套（另两仓）

- `xiaochengxu-mqtt` v1.4.22：绑定流程补日志（只记长度/命中与否与 hub 错误码，**不回显码值**）；过期文案改为「绑定码无效或已过期：在 HA 加载项页点一下二维码换出新码再试」；离线选路改进——有云绑定时 **LAN 最多等 3 秒即切云**（不再干等 10 次重连阶梯）、切云时停掉 LAN 阶梯、云模式下每约 60 秒回探局域网（回到家自动切回本地）。
- `huijian-cloud-hub` v0.2.0：**需重新部署**（新增 `/agent/bindcode` 路由；不部署则该端点 404 → 面板点击只做只读刷新，过期码仍换不出来）。

## [1.7.39] - 2026-09-22

二维码位置订正（用户 2026-09-22 反馈页头那块白底码"太突兀"）：从页头 logo 旁**挪进「远程控制（慧尖云）」卡的右侧**，自动出现/点击刷新/复制这些行为不变。

### Changed

- **卡片改为两栏**（`www/index.html` + `css/huijian.css`）：左栏＝云端连接/本机网关两行状态 + 绑定码与说明，右栏＝绑定码二维码（132px，窄屏 160px 居中；≤720px 自动上下堆叠）。页头不再有任何二维码块。
- **文案跟着位置改**：卡内说明从"扫一扫上面的二维码 / 点一下 logo 旁的二维码"改为"扫一扫右边的二维码 / 点一下右边的二维码"——位置口径写错会让用户照着找不到（小程序那侧同步改为"该卡右侧的二维码"，见配套）。

### Tests

- `test_v1738_hub_qr_ui.py` 重写位置类断言：二维码必须在 `#remoteCard` 内且在 `.hub-main` **之后**（＝右侧）、页头不得残留、`brand-qr` 旧类必须清干净、卡内文案不得再提"logo 旁"；新增 `.hub-row{display:flex}` 布局钉与"卡片两栏容器存在"钉。
- `test_v1737_hub_ui.py` / `test_v1738_hub_qr_ui.py` 的卡片抽块从锚点式正则改为 **`<div>` 配平抽取器**——本次加了两层嵌套就把旧正则的"三个连续收尾 `</div>`"顶到别处，属本仓踩过的"定宽窗/锚点被新嵌套顶掉"同族问题。
- 全量 885 用例通过；`bash -n`、`compileall`、ruff（F,E9,B）、`node --check`、四源版本一致全清。
- 真浏览器复核（本地面板）：二维码在卡内、页头无残留、有码时自动显示（hidden=false、码值标签同步）、`.hub-row` 在宽屏为 row（二维码在内容右侧）、窄屏堆叠居中。

### 配套（另一仓）

- `xiaochengxu-mqtt` v1.4.21：绑定卡提示文案同步为"该卡右侧的二维码"，并在测试里钉住这个位置口径（两端措辞必须同向）。

## [1.7.38] - 2026-09-22

P0 绑定入口体验（用户 2026-09-22 令）：绑定码**二维码放在页头 logo 旁，自动出现、点击刷新**，并给绑定码加「复制」。v1.7.37 的绑定码只能手抄 6 位数字——用户明确要求补二维码。

### Added

- **页头二维码**（`www/index.html` + `js/huijian.js` + `css/huijian.css`）：拉到绑定码即自动渲染（未启用/读取失败时不摆空盒子），**点一下＝重新拉码并重绘**（码 10 分钟过期，不必去下面卡片里找刷新按钮），键盘 Enter/空格同样可触发；带宽屏/窄屏两档尺寸（≥56px，保证手机能扫动）。
- **绑定码「复制」按钮**：走 `navigator.clipboard`；非安全上下文/权限策略拒绝时退回"选中文本"，按钮文案给「已复制 / 按 Ctrl+C」反馈。
- **`www/js/qr.js`——自带二维码编码器**（零依赖）：ingress iframe 有 CSP、本仓前端从不连外网，引不了 CDN 库，所以自己实现（版本 1–6 × L/M/Q/H、字节模式、RS 纠错与分块交织、格式信息 BCH(15,5)、八种掩码 + 罚分选优、SVG 渲染）。文件内不出现任何网络 API 与远程 URL（除 SVG 命名空间），由守卫钉死；渲染走 DOM API 而非字符串拼标记（注入面为零）。
- **载荷格式契约**：二维码内容 = `HUJIAN-BIND:<载荷版本>:<6 位码>`（当前版本位 `1`）。小程序侧解析同一格式，**不认识的版本位只提示不绑定**——以后换协议时老版本不会误解。

### Tests

- 新增 `test_v1738_qr.py`（21 条）：**与 Python `qrcode`（独立实现）逐格对账**——8 种掩码、4 个纠错级别、以及版本 4-H（4 块）与版本 5-Q（两组块）的分块交织形态；自动选掩码时校验"输出等于参考实现在同一掩码下的矩阵"（符号合法）；同一载荷两次生成必须一致（避免二维码看着在变）；超长载荷必须抛错而不是截断；有 `zxing-cpp` 时**真解码回读**（矩阵 → 位图 → 文本，24 组全过）。本机实测：24/24 由 ZXing 解码为原载荷。
- 新增 `test_v1738_hub_qr_ui.py`（7 条）：二维码块在页头 brand 内且默认 hidden（含 `.brand-qr[hidden]{display:none}` 复位——自带 display:flex 会吃掉 hidden 属性）、点击=重新拉码（不是重画旧码）、`renderBindQr` 在三条出口都被调用（漏一条会残留过期码）、复制按钮含退路与反馈、`qr.js` 必须先于 `huijian.js` 加载、面板真载荷必须编得出来（前缀变长到超容量时当场红）。
- 全量 885 用例通过（基线 857）；`bash -n` 全部 shell、`compileall` 递归、ruff（F,E9,B）生产 + 测试全清、`node --check` 全部前端 JS（含新 `qr.js`）、四源版本一致。

### 配套（另一仓）

- `xiaochengxu-mqtt` v1.4.20：「LoRa 网关」页绑定卡新增 **📷 扫一扫**（扫到自家载荷即填入并自动绑定；非自家二维码给可见提示；用户取消不打扰）与 **📋 粘贴**（只填框不自动绑，留用户确认）；`parseBindPayload` 单一解析入口（认 `HUJIAN-BIND:<版本>:<6 位码>` 与裸 6 位数字，版本位不认即拒）。

## [1.7.37] - 2026-09-22

P0 绑定入口（加载项侧）：插件页新增「远程控制（慧尖云）」卡，把 6 位绑定码摆到用户眼前。v1.7.35/36 把通道打通了，但绑定码此前只存在于 REST 视图的 JSON 里——用户没有"在哪看码"的地方，通道等于验不了（真机联调的第一道门就是它）。

### Added

- **插件页绑定卡**（`www/index.html` + `js/huijian.js` + `css/huijian.css`）：云端连接状态、本机网关 SN、6 位绑定码（等宽 + 字距，照着往手机里敲不抄错），并写明小程序侧入口名与位置；刷新并入既有「⟳ 刷新」主流程。小程序侧的卡名与这里指路的卡名成对，两端各有守卫钉住（改一侧忘改另一侧即红）。
- **`status_view()` 补 `gatewaySn`**：多网关 / 多 HA 实例时用来确认"这一台是哪一台"；取 SN 抛错也不带崩视图（回空串）。
- **老集成优雅降级**：视图缺失或未启用时显示「未启用」而不是停在"检测中"——`<1.7.35` 的集成没有这条路由，页面不能看起来像坏了。

### Tests

- 新增 `test_v1737_hub_ui.py`（5 条）：卡结构与 id 契约齐全；面板拼的路由字符串必须等于 `api.py` 注册的 url（真值从面板源码里抽出来比对，不是再抄一遍常量）；`loadRemoteControl` 必须真被 `refreshAll` 调用（防"函数在但没人调"的死码假绿）；未启用 / 读取失败 / 已连接三分支都在；页面与 HTML 都不得出现 secret 字样；绑定码样式必须等宽 + 字距。
- `test_hub_client.py` 16 → 17 条：`status_view` 带 `gatewaySn`；`gateway_sn` 取用抛错时视图仍可用。
- 变异核验（影子树）：删掉 `refreshAll` 里的调用 → "不是死码"钉红；面板路径漂移 → 路由等式钉红；去掉「未启用」降级 → 状态分支钉红。

### 配套（另一仓）

- `xiaochengxu-mqtt` v1.4.19：「LoRa 网关」页新增「绑定微信远程控制」卡——输入 6 位码绑定 / 解除绑定（需确认）、显示当前通道（本地直连 / 微信云远程）；hub 的 err 码全中文映射且带兜底；新增 `tests/remote-bind-entry.test.js` 12 条（结构 + 行为：输入只留 6 位数字、非 6 位可见拒绝且不发请求、绑定成功即清空并重连、失败不重连且忙标志复位、解绑需确认）。

## [1.7.36] - 2026-09-22

v1.7.35 发布后、真机联调前的**跨仓契约复核批**：把「小程序页面 ↔ 选路层」与「云通道 ↔ LAN 通道」两处接口面/字段逐条对账，修掉 1.7.35 一进真机就会暴露的两处（小程序侧同批的 v1.4.18 修另外两处）。

### Fixed

- **云通道「锁定模式」恒为未知**：锁定模式在 LAN 通道是由 `device_update` 实时推送补齐的（`_device_update_payload` 七键之一），而 `device_ws_view` 只是 device_list 项视图（sn/gwSn/position/battery/state）——云通道只有状态上行这一路，插件不带上它，小程序页面永远显示 "--"。状态条目补 `windLockMode`，取值与 LAN 同源（`attributes.wind_lock_mode`；缺失 / bool / 不可解析 / `1e999` 这种非有限数一律 -1，沿用 `_as_int` 连 OverflowError 一起接住的口径）。
- **状态上行不保活 ⇒ 云端分不清「一直没人动」与「agent 已死」**：hub 侧每条状态都带 `updatedAt`，但只有状态变化才刷新——网关静置一段时间后，小程序侧的「在线/最后上报」就无从判断（活着与死了同形）。新增 5 分钟保活重推（`_keepalive_loop`，睡眠仍按 30s 切片，不占 HA 停机预算）：agent 活着则 `updatedAt` 恒新鲜，掉线后自然转旧。

### Tests

- `test_hub_client.py` 14 → 16 条：新增「状态条目必带 windLockMode（缺失 / bool / 字符串 / inf 全落 -1，合法 0/1 原样带过）」「保活循环按时标脏、置停机闩锁后自行退出」；原状态条目用例同步改断言（不再只是五键视图）。
- 全量 851 用例通过（基线 849）；`bash -n` 全部 shell、`compileall` 递归、ruff（F,E9,B）生产 + 测试全清、`node --check` 全部前端 JS、四源版本一致（config.yaml / manifest.json / version.json / index.html）。

### 配套（另一仓）

- `xiaochengxu-mqtt` v1.4.18：选路层 `GwRouter` 补齐页面在调但漏实现的 `getDevice` / `setDeviceName` / `removeDeviceLocal` / `refreshDevices`（并改为按 LAN 客户端原型动态兜底 + 测试钉住两个 API 面相等），云模式 `getGateways()` 补齐 `devices[]` / `lastSeen` / `deviceCount` 形状（页面 `gw.devices.map(...)` 直接崩）、设备名走本地同一套命名、按 `updatedAt` 判在线并剪枝。

## [1.7.35] - 2026-09-22

P0 远程控制通道（插件侧）：加载项到「慧尖云 hub」的**出站长连**——客户零配置（装加载项即自动注册实例、取 6 位一次性绑定码），小程序扫码绑定后即可在**局域网之外**控制子设备。配套 hub 服务在独立仓 `huijian-cloud-hub`（微信云托管部署，公网 HTTPS + wss 四跳已在真栈验证），本轮只发插件侧。

### Added

- **`hub_client.py`（新增；模块不 import HA，可独立测）**：
  - 注册/身份：`POST /agent/register` → instanceId / secret 落盘 `huijian_hub_identity.json`（原子替换、损坏改名 `.bad` 留证）；secret 全程不进日志——`cred_brief` 只留长度 + 首字节（沿用三端凭据不回显口径）；绑定码只在 `status_view` 里给用户看。
  - 长连：`wss://…/agent/ws`（`http→ws` / `https→wss` 自动换 scheme），心跳 25s，断线指数退避 5s×2^(n-1) 封顶 300s，睡眠按 30s 切片（不占 HA 停机预算）。
  - 上行：设备状态变更（`device_manager.add_status_listener`）debounce 0.3s 后推与 `GET /state` 同构的快照（懒 import `device_ws_view`，与侧边栏/HTTP 视图同源，避免出现第二份状态真相）；命令执行结果回 `cmd_result`。
  - 下行：`{action:'control', sn, attribute, value}` 先过**与局域网 `_cmd_control` 同口径**的值校验（拒空属性 / 空串 / bool / 容器 / nan / inf / 科学计数）→ 只向「设备所属条目」发布 `send_ws_raw_004`，命中即止（不广播，防多条目重复控制）。
  - 控制语义三端一致：ok = **004 已发布到 broker**（不代表设备已执行）；命令**不重发**。
- **装载与卸载**（`__init__.py`）：`async_setup_entry` 创建并 `async_create_task(hub.async_start())` 入 `_bg_tasks`；**启动失败只降级重连**、不影响本地功能（对齐 WS 网关「启动失败只记 error」的既定语义）。`async_unload_entry` 新增「1.4 hub 客户端先行停」——先摘状态监听、关长连，避免卸载后仍收命令。端点/密钥可用 `entry.options` 的 `hub_base` / `hub_install_key` 覆盖（P1 再进 config_flow 表单）。
- **只读 REST 视图** `GET /api/window_controller_gateway/hub`（`api.py`）：回 instanceId / 6 位绑定码 / 连接状态 / `enabled`，**绝不回显 secret**；插件页「扫码绑定」用它。

### Known limitations（P0，P1 收）

- 共享 install key（写在加载项默认值与 hub 仓 Dockerfile 里）：够用但不够严，P1 换每实例一次性密钥。
- hub 默认域名 `*.sh.run.tcloudbase.com` **仅限开发测试**：生产需给云托管绑自定义备案域名（只影响「加载项→hub」这一跳；小程序侧走 `callContainer` 免白名单）。
- 绑定关系现落 hub 本地 JSON（`data/store.json`）：P1 迁云开发数据库并上 `watch()` 实时状态推送（协议不变）。

### Tests

- 新增 `test_hub_client.py`（14 条）：退避序列 / 切片睡眠 / 凭据摘要形态 / 值校验与 LAN 同口径 / 身份往返与损坏留证（断言日志中不出现绑定码与 secret）/ 已有身份不重复注册 / ws 地址换 scheme / 坏状态条目跳过 / 命令三类错误与无 `control_fn` / `_session_once` 回执 + 上线即全量推状态（FakeWS 保持窗口）/ `status_view` 不含 secret / 启停幂等。
- 全量 849 用例通过（基线 835）；`bash -n` 全部 shell、`compileall` 递归、ruff（F,E9,B）生产 + 测试全清、`node --check` 全部前端 JS、四源版本一致（config.yaml / manifest.json / version.json / index.html）。

## [1.7.34] - 2026-09-22

v1.7.33 发布后的两条现场链路复核批（用户问「新装加载项后第一台网关直接进集成」与「新网关首报 001」是否仍有问题）：
复核确认两条主链在真栈上已通，同时抓出 v1.7.33 自己引入的一处生命周期回归、三态门的第二个盲区，并把两条链路补成 CI 真栈门禁。

### Fixed

- **域级服务注销放错生命周期钩子（v1.7.33 引入的回归）**：注销写在 `async_unload_entry`，而 reload 同样走 unload，且 reload 时条目仍留在 `config_entries` 里——"剩余条目为空"恒真 ⇒ **每次重载都摘掉全部 7 个域级服务**（start_pairing / refresh_devices / set_position / check_gateway_status / rename_device / transfer_device / unignore_gateway）；若重载后的 setup 失败（ConfigEntryNotReady 或异常），服务就长期空着，再调用得裸 KeyError/500 而非可读错误。落点改到 `async_remove_entry`（条目确已从列表移除后才回调），并补双向反钉（unload 体内不得出现 `services.async_remove`、remove 体内必须在）。
- **条目「已配置但未加载」时两只耳朵一起装聋（v1.7.31 三态门的第二个盲区）**：`entry_state_for_sn` 只看 SN 命中与 `disabled_by`，从不看 `entry.state`——命中条目处于 `setup_error` / `setup_retry` / `not_loaded` 时照样返回 "configured"，耳朵静默"让位"一个根本没挂订阅的 handler：网关 001 无人应答、固件每 5s 重发永不停血、日志**零留痕**。用户视角比禁用态更难归因（条目明明就在 设置→设备与服务 列表里）。四态门补 `not_loaded` 态（`ENTRY_STATES_UNANSWERED` 单一真源；状态名归一兼容 StrEnum / 旧 `str,Enum` 混血 / 裸字符串三形态，与 config_flow 既有 `_entry_state` 同式）：两耳照答止血 + 每 SN 10 分钟节流 WARNING 指向 setup 失败根因 + 不弹发现卡（条目已在列表里，`async_discover_gateway` 第 3 步命中同 SN 本就早退）。`loaded` / `setup_in_progress` / `unload_in_progress` / `migration_in_progress` 与状态未知一律仍按 configured 静默——否则与正式 handler 双答，把 v1.7.30 仲裁刚收口的"1 请求 2~3 答"噪声面放回来。

### Added

- **真栈 E2E 补两臂（`ha_e2e_driver.py` K/L 段，随 e2e job 作发布硬门禁）**：
  - K 臂：未配置网关首报 001 → `gateway/{sn}/req` 上**恰好一条**同型代答（head `$SH` / ctype / id / sn 回带 + `data.errcode:0` + `uuid`），且发现卡挂起可确认。计数在凑够后再静置 2s——只等到第一条就返回会漏掉晚几十毫秒的第二个应答者（假绿）。
  - L 臂：空 SN 等待条目（发现代理同款 REST 路径）+ 首报 → SN 自动填进**同一条**等待条目、条目 loaded、子设备真注册进设备注册表、发现卡 **0 张**（"直接添加到集成"零点击契约），且两耳并存（handler 耳 + 心跳耳）时仍 1 请求 1 答（v1.7.30 仲裁在真栈上的第二应答者形态）。
  - 配套的真栈取证修正（两轮 CI 实锤后修）：①在途发现流改走 WS `config_entries/flow/progress`——`GET /api/config/config_entries/flow` 在 HA 2026.9.3 是 **405**（源码 `ConfigManagerFlowIndexView.get` 显式 `raise HTTPMethodNotAllowed`），恒空列表既让 K 臂假红、也会让 L 臂的"0 张卡"变成假绿，故查询失败一律显式 `die`；②L 臂"填充成功"判定改走 **devices 视图**——`GET /api/config/config_entries/entry` 返回 `entry.as_json_fragment`，**不含 data/unique_id**（源码实证），拿它读 `data.gateway_sn` 恒为假（第二轮"填充=None"即此假象），现以等待条目按 entry_id 甄别 + devices 三真（网关 SN identifier／子设备注册／gateway_online）+ 条目数不变共同判定；③取证能力：`run_e2e.sh` 预置 `configuration.yaml` 把本集成日志开到 INFO 并保留 `default_config:`（镜像原本自动生成那份首行即此，api/auth/onboarding 全靠它；漏掉＝REST 整个不存在），diag 追加 **HA 文件日志**（`$CFG/home-assistant.log`，每条 flush）并给容器加 `PYTHONUNBUFFERED=1`——第二轮实证 `docker logs` 在关键时段整段空白（python stdout 非 TTY 时块缓冲），等于没有取证。

### Tests

- 新增 `test_v1734_gate_states.py`（38 条）：四态门单元判定（含三种状态形态归一、禁用优先于未加载、同 SN 多条目只要一个在飞即 configured）；两只耳朵的**行为级**证据——心跳耳经真 `async_setup_entry` 的 awaiting 分支真抓 `_heartbeat_listener`、`_protocol` 耳经真 `WindowControllerMQTTHandler._do_subscribe_topics` 真抓 `handle_gateway_response`，判定逻辑不打桩；风暴下"每请求必答（6 请求 6 答）+ 留痕只放一条"；setup 重试成功后同一条耳朵立刻转静默（状态实时读、无缓存）。反钉：健康条目零代答零留痕、未配置仍走发现链、禁用态文案优先、状态未知不倒退、加载态判定不得在耳朵文件里各写一份。
- 新增 `test_v1734_e2e_arms.py`（29 条）：两臂不被悄悄删掉或断言被稀释（含"两臂必须跑在 J 段 500 条 soak 之前，否则 req 主题被洪水污染"的顺序钉、settle 窗不得调 0、不得回退到 405 的 REST 流索引）；把 driver 的 `_check_single_ack` / `_wait_acks` 从源文本切片抽出真跑——多答、错 id、漏 uuid、errcode≠0 必须真 `die`，晚到的第二答必须被计入；并起一个**最小 HA WS 台架**（真 aiohttp 服务端、真 TCP，按 2026.9.3 源码的 auth_required→auth→auth_ok 契约应答）真跑 `_ws_call`/`_hj_flows`/`_cards_for`——认证被拒、命令失败、端口不通一律显式失败，"发现卡 0 张"只能来自查通了的空结果（否则 CI 绿是假的）。
- 变异核验（影子树）：把 `ENTRY_STATES_UNANSWERED` 清空＝退回 v1.7.33 盲区 → 新守卫 13 条红，其中双耳行为面直接呈现"零代答 + 零留痕"，而全部反向钉仍绿（证明红的正是被修的那一面）。
- 全量 834 用例通过（基线 767）；`bash -n` 全部 shell、`compileall` 递归、`py_compile` driver、`node --check` 两份 JS、ruff（F,E9,B）生产+测试全清。

## [1.7.33] - 2026-09-22

五路并行只读审计 + 一手复核后的全量收口批（用户指令「全部采用最佳方案优化」）。
核心是两枚此前"看起来有守卫、实际是假绿"的洞，以及三条物理控制面的假成功链。

### Fixed

- **决定开合方向的线值此前零行为覆盖**：open/close/stop/a 的 100/0/101/200 是唯一决定"窗往哪边走"的常量，而全测试树只在断言 CLAUDE.md 里的 markdown 表格字符串（`test_audit_round6::test_claude_wire_values_fixed`）、`send_command` 真实调用点只有 start_pairing/set_position/set_speed 三种命令——把常量对调或退回废弃旧表，655 条全绿而现网每台窗反向动作。新增出站行为守卫：逐命令驱动并断言发布报文的值字面量 + `改常量必须改报文` 的反钉 + 文档表格从常量派生比对。
- **Ingress 端口五处真值源，`config.yaml ingress_port` 零守卫**：v1.7.32 的"五处收口"只被机械对账到四处（config.yaml 那处在 tests/ 与 .github/ 零引用），改它即全绿 + 侧边栏 502。新增五源等式守卫（config.yaml / ingress.conf / run.sh heredoc / `WS_RESERVED_PORTS` / `:2AF6` 取证门），并顺带把 `MQTT_PORT`/`INGRESS_PORT` 升为 const 一等真值源、保留口集合由其派生；options 步撞口文案补漏报的 8123。
- **三条假成功链**：① WS `control`/`pair` 在零条目（awaiting-only 或 reload 让出窗）时循环空转仍回 `ok:true`——小程序显示"已下发"而一条报文未发；② `set_position` 服务无机型能力闸，Web 滑块/REST/自动化绕过实体侧校验，向无百分比硬件打固件未定义的 w_travel 指令并回 200（前端同步按 `position_capable` 渲染，并带出服务端失败原因）；③ 「移除」按钮在 `await sleep()` 让出点之后仍用构造期捕获的旧 manager，reload 窗内本地删除整体 no-op → 手动删除名单未登记 → 幽灵设备复活（照 WS 通道既有写法按 entry_id 重解析）。
- **陈旧值冒充新鲜值三破口**：`sensor` 用 `r_travel == 0` 严格比较（字符串 `"0"`/`"0.0"` 与 255 未校准标记全落 "open"，而 v1.7.31 起 Web 圆点主判据就是它）改数值归一 + 0..100 值域；重启回填设备补 `last_update`（不推翻 v1.6.8/1.6.19「无时间戳=新鲜」定案——真缺口是回填路径借用了该语义）；`cover.extra_state_attributes` 补同款 15 分钟时效闸（失联设备此前「灰点 + 状态:打开 + 位置 65%」并存且滑块可拖）。
- **MQTT 链加固**：心跳耳补入站 64KB 闸（旧实现只有 `_protocol` 耳有，而干净主机首配期只有心跳耳，一条大报文即在事件循环线程卡死 HA）；代答认领在发布失败时撤销（30s 抑制窗把一次失败放大成 6 次不应答）；002 先应答再批处理（保留"必 ack"契约）；订阅重建加互斥且句柄"先落新的再退旧的"；订阅代际判据由裸 `id()` 改弱引用（地址复用会让 B-1 形态复活且零日志）；`_schedule_async_task` 守 `_closing`、`cleanup` 退订提到首个 await 前；接管 MQTT 条目时清除 TLS/WebSocket 等不兼容键（旧实现只覆写四键，会把用户条目的证书/传输方式带进明文 2022 且此后无纠偏出口）。
- **WS 网关安全与资源**：令牌比较改 `hmac.compare_digest`（含 oldToken）；运行态令牌闸补 `WS_TOKEN_MIN_LEN`（storage 手改短令牌可绕过表单下限）；子协议切分收窄到 `[ ,]+`（与固件及 aiohttp 对齐）；空闲计时**实现**改为只认业务 TEXT 帧（旧代码每轮重起表，BINARY 帧可永久占槽，与自述口径相反）；停机窗口拒绝新握手；广播失败显式 close；令牌写入全部 enabled 条目；STOP 监听单例；401 日志按来源节流；ERROR 帧留归因。
- **生命周期与表单**：setup 先清 `_platforms_forwarded`/`_bg_tasks`/`unsub_listeners` 再合并（卸载失败残留会被下次 setup 原样继承）；`discovery_interval` 抽出可测的归一+钳制（该值是网关离线回收唯一节拍源，旧实现可被调成 3600 或字符串导致集成起不来）；心跳武装订阅失败改 60s 退避无限重试；令牌字段 default 取存储值；5 处 `async_set_unique_id` 显式 `raise_on_progress=False`；`via_device_kwargs` 未解析时省略参数（传 None=清空归属）；服务注册集登记 + 最后条目卸载时注销；`integration_type` 改 `hub`。
- **容器与前端**：集成目录改临时名+`mv` 原子换防（旧 rm -rf + cp -r 失败会把集成目录留半截且 broker 未起）；`set -o pipefail`；mosquitto 队列 100→1000 且 `autosave_on_changes`；发现代理 stderr 留痕；Web UI 修未定义 CSS 变量、Release 链接协议白名单 + `rel=noopener`、禁用条目单独渲染、await 后重取容器节点、清死分支与死函数、logo cache-buster。

### Added

- CI 补机械化 lint 门（`ruff --select F,E9,B`，生产+测试，存量已清零）与 JS 语法门（`node --check`，79KB 手写前端此前无任何机械门）。
- Web UI 停机/禁用可见性：被禁用或未 loaded 的条目独立渲染（灰徽 + 原因），不再当正常网关配控制按钮。

### Tests

- 新增 8 个守卫文件（共 111 条用例）：线值出站行为、Ingress 五源等式、服务目录三方一致、假成功链（WS 空集如实 ack / 能力闸 / 让出点重解析）、陈旧值契约（r_travel 归一 / 回填时间戳 / 属性时效闸）、协议加固（双耳闸 / 认领撤销 / 弱引用代际 / 先应答后处理 / 接管清洗 / 闩锁派发）、WS 加固（时序安全 / 下限制 / TEXT-only 续期 / 停机准入 / 广播 close / 令牌全条目）、F/G/H 组（间隔钳制 / 桩签名 / 原子换防 / 队列设置 / 前端结构）。
- 修三处假绿守卫：`test_v1624` 的 `pytest.skip` 从未 import pytest（无 bash 机器上 skip 变 NameError，已实测复现）；`test_v1621` 的"墓碑断言"（循环体是 pass，三项锚只断言两项）；`test_services_failfast` 的零参 `async_entries` 桩（只因断言在遍历前抛出才恰好绿）。
- 全量 766 用例通过（基线 655）；`bash -n` 全部 shell、`compileall` 递归、`node --check` 两份 JS、ruff 生产+测试全清。

## [1.7.32] - 2026-09-21

Ingress Web UI 端口迁移 8099 → 10998（用户指令），全链路单一口径收口。

### Changed

- **Web UI（Ingress/nginx）端口 8099 → 10998**：五处功能面同步——`config.yaml` ingress_port（Supervisor 注册）、`ingress.conf` 模板、`run.sh` heredoc 真值源、nginx 启动失败取证的 `/proc/net/tcp` 十六进制门（`:1F9B$` → `:2AF6$`，漏改则端口占用取证恒空）、`WS_RESERVED_PORTS` 保留口集合（8099 出列、10998 入列，WS 网关端口选项撞口拒绝与报错文案 strings/zh-CN 四处同步）。侧边栏入口经 Supervisor 代理 URL 不变；LAN 直连旧口本就 403（来源白名单），无外部调用方依赖。历史事故叙述（v1.6.18 系列注释与 CHANGELOG 旧条目）保留 8099 原样。

## [1.7.31] - 2026-09-18

真机 1.7.30 测试 + 五路并行审计收口批：现场抓出 2 枚静态审计漏网缺陷（停机 ERROR、`via_device` 弃用面），审计判死项一次全修，全部经真栈 A/B 实证。

### Fixed

- **每次 HA 停机刷 ERROR（真机 0918 实锤，台架 A/B 双臂复现→归零）**：`EVENT_HOMEASSISTANT_STOP` 的一次性监听器在总线派发时已被消费摘除，停机回调又自调条目卸载、对已消失的监听器再次退订 → HA core 每次停机每条目打一条 "Unable to remove unknown job listener" ERROR。回调自卸载前先摘句柄；reload（非停机）路径的正常退订保留反钉。
- **`via_device` 弃用参数迁移（2027.8 停摆面，现网 2026.9 每次启动一条 WARNING）**：v1.7.28 清注册表弃用面时只清了 `devices`/`entities` 直读，漏了 `async_get_or_create(via_device=(DOMAIN,sn))` **写参数**6 处。manifest 声明支持 HA 2024.12 起全区间，而台架真签名核验 2026.1.3 **尚无** `via_device_id` 形参——无条件改新参会在旧版 TypeError 打死设备注册。新增 `utils.via_device_kwargs` 双形态出口（inspect 签名探测+缓存：新 HA 传 `via_device_id=<父设备id>`，旧 HA 保留原形态）；AST 守卫禁一切 `via_device=` 调用实参、迁移点计数正钉。
- **MQTT 引导修复条目从未真正出现过（真签名实锤）**：创建修复卡片误用不存在的参数名 `is_fix_flow`（真实形参 `is_fixable`），每次调用 TypeError 被吞成一行 DEBUG——v1.7.29 的「设置→系统→问题」修复入口与一键重试整面不可用，healer 告警文案还把用户指向不存在的入口。改正参数并让吞异常路径升 WARNING；conftest 新增按真 HA 2026.1.3 `inspect.signature` 逐字复制的 `issue_registry` 假件（禁止 **kwargs 吞参形态），守卫当场抓获同类签名漂移。
- **禁用条目不再算「已配置」（BUG-5 统一口径，真源核验 `async_entries` 默认含 disabled）**：用户禁用的慧尖网关条目上电后，001 绑定请求既不被耳朵代答也不被发现、零日志——固件每 5s 重发永不停血。新增三态门 `utils.entry_state_for_sn`：禁用条目命中→继续代答止血、**不弹发现卡**（尊重禁用决策）、节流 WARNING 指路「启用条目」；bootstrap 自愈的退出判定同步修正（条目全禁用即退出，不再被骗成永续巡查）。
- **心跳监听器兜底异常从 DEBUG 升 WARNING（按异常形态 10 分钟节流，带堆栈）**：代答/发现/注册表任一环节异常此前默认级别零可见，发现链静默断裂无迹可寻——与 v1.7.30「归因行必须 WARNING」取证纪律对齐。
- **「已忽略」网关不再触发误导告警**：转正看守只豁免「条目转正」「发现卡挂起」两态，忽略会中止发现流——被忽略网关每个上电周期必得一条"请检查发现卡"WARNING。补第三态豁免（静默退场），复现测试转正。
- **bootstrap 自愈长睡切片**：指数退避封顶 1 小时的单发 sleep 对停机信号无感（真源实证各停机阶段有全局超时保护，不会真挂 1 小时，但每停一次烧光一条阶段超时预算）；改 30s 片+片界复检出口，最大退出延迟 30s，退避节奏总量不变。
- **WS 网关对非升级请求显式 400（真栈栈实锤）**：带合法子协议令牌但无 `Upgrade: websocket` 的请求（健康检查/端口探活常形）在握手失败分支返回未 prepared 的响应对象，aiohttp 完成层二次 prepare 抛 `HTTPBadRequest` 逃逸成 "Unhandled exception" ERROR、连接无状态行即断。改显式 `400 bad websocket request`；E2E driver 以裸 socket 断言响应状态行（真栈门禁）。
- **WS 解绑区分「reload 途中」与「条目已删除」**：解绑命令下发后 1s 等待窗内条目可能正被 reload，旧实现把「解析不到设备管理器」一律当「已随条目删除」回 `ok:true`——reload 完成后新管理器按残留映射回填设备，幽灵复活+小程序收到假成功。现仅条目确已从列表消失才回真；reload 形态如实回 `ok:false, entry reloading`。
- **WS 令牌运行时防线（与端口 BUG-7 同威胁模型补全）**：Storage 手改/跨版本残留可写入含空白/逗号或超长的令牌——子协议头按 `,`/空白拆分使该令牌成为不可满足握手（全部客户端永久 401 自锁，机制实锤），且会写进 101 响应头成非法值。表单层与 set_token 层原有校验之外，运行时同样回退默认+告警；空串=不认证是合法形态不受影响。
- **退役 manager 的迟到上报整体拒绝**：`add_device` 条目存活门前置到一切副作用之前——旧检查在内存/映射写入与持久化落盘之后，cleanup 后的在途协程仍会为已删除条目重写设备映射并落盘（幽灵复活燃料）。
- **MQTT 派发任务统一收口**：消息处理派发面（002 批处理/003 绑定/耳朵代答协程）句柄登记，`cleanup()` 逐个取消并 await——条目卸载后在途任务不得再触碰已清空的设备管理器/注册表。
- **更新检查徽章的 Gitee 源排序盲区**：Gitee releases API 默认按创建**升序**返回，徽章只取第一页 100 条——release 总数增长后最新若干版全部落在窗外（0918 实测首页最大 v1.7.15），GitHub 被限流时徽章静默不亮或指向旧版。Gitee URL 显式 `direction=desc`（实测首页即见最新）。
- **Web UI 设备卡在线圆点改业务真值源**：子设备本就没有 online 实体（全集成唯一在线传感器挂在网关上），旧降级判据「cover unavailable」因 cover 恒 available 永不触发——设备断电失联仍显绿点+陈旧状态。改读 `{gw}_{sn}_status` 状态传感器（上报驱动+超时效转 unknown）：无新鲜上报显**灰色未知**点（新增 `.dot-unknown` 样式），宁显未知不造假绿灯。
- Web UI 杂项：`/states` 拉取失败的降级重建后立即逐设备异步补拉（旧版瓷砖冻结"加载中"最长 30s）；「HA MQTT 通道」查询加 `?domain=mqtt` 过滤；状态板第 2 项陈旧注释订正。

### Added

- E2E driver：WS 非升级 GET 的 4xx 真栈断言（CI 硬门禁内）。
- `run_local.sh`（本地台架）configuration 补 `logger: default: info`——INFO 归因行现场可见（此前仅 WARNING+，排查耳朵/看守行为需改日志配置）。

### Tests

- 新增 `tests/test_v1731_field_fixes.py` 34 用例：F-A 行为双臂（STOP 臂 0 退订/reload 臂必 1 退订）、`via_device_kwargs` 新旧 HA 三形态+AST 守卫+6 点计数、issue_registry 真签名守卫、三态门全形态、healer 切片睡眠（停机打断/全长睡反钉）、节流助手、ignored 豁免复现、unbind reload/删除双甄别、令牌运行态闸、add_device 前置门、派发任务 cleanup 收口、C-3 常量单一真源、前端 5 钉（desc/domain 过滤/dot-unknown/降级回填/假判据反钉）。
- `tests/test_v1726_ear_ack.py` 接线钉升级三态门形态（代答位置/仲裁入口/disabled 留痕与短路全断言）；`test_audit_round5` 死属性扫描豁免合法 dict 键形态；`test_audit_round8` 假件补 `async_get_entry`。
- 全量 655 用例通过；台架真栈：ArmA(1.7.30) 复现停机 ERROR=1 → ArmB(1.7.31)=0，旧 HA 2026.1.3 兼容臂 driver A–J 全绿。

### Known Limitations（明确非目标）

- 「耳朵随 MQTT client 换代失聪」的静态审计推断经真栈实验**未复现**（reload 后耳朵仍在响应），本轮不改该面，留观察项。
- WS 空闲超时以业务 TEXT 帧复位（纯 RFC6455-PING 保活不重置计时，300s 被踢——台架双侧实证）；与固件 socket 层任意帧复位存在已知语义偏差，小程序 60s 业务心跳是唯一联审保活通道，已在代码注释钉死口径。
- 004 控制报文 `data` 中 `position/speed/strength` 原始键残留属协议洁净度问题，固件按 attribute/value 解释实测无害；清理涉及通用透传机制全调用面回归，本批不动。
- OptionsFlow 构造传参的上游弃用（2025.9 口径）在现网 2026.8 尚未产生告警，登记为前瞻项。

## [1.7.30] - 2026-09-17

001 首配 A/B 真栈收口批（真 HA + 真 broker 台架双臂实测，止血/重复应答/转正/自愈节奏四问定论）：三项实证缺陷一次关闭。

### Fixed

- **②耳朵代答 N+1 重复应答收口（单点仲裁）**：台架实锤同一条 001 被 2~3 个耳朵各自代答（每个已配置条目的 `_protocol` 他网关分支+每条等待条目的心跳监听器皆为应答者；ws_peek2 复证"无等待条目+2 handler → 恰好 2 答"）——同 uuid 报文虽无害，但"固件对重复 ack 幂等"仅是文档主张未真机验证。新增 `utils.ear_ack_claim`：以 `(sn,id)` 为键的进程内认领（TTL 30s、容量闸 256），两处耳朵统一改走新入口 `async_ear_ack_001_arbitrated`（认领→发布→转正看守），第一个耳朵发布、其余抑制；固件重试换新 id 仍得一次应答（止血语义不变），同 id 超 TTL 补答（丢包保险）。接线反钉：两处耳朵源码直调 `async_ack_gateway_001` 即测试红。
- **②顺手修两耳 data 归一不对称**（独立回归审计三条实测缺陷之二收编）：001 带 `data:null` 时 `_protocol` 耳归一后照答、心跳耳谓词见非 dict 拒答——干净主机只有心跳耳，该形态风暴不止血；心跳侧补同款归一。
- **③代答未转正必留痕（WARNING 级）**：旧症状"固件停发 001 但设备永不出现"只有 INFO——现场 WARNING+ 日志采集不可见，症状被代答成功表象抹掉。代答成功后新起 30s 转正看守：SN 既无配置条目也无待确认发现卡片 → 一条指排障方向的 WARNING（区分"卡片挂起等确认＝正常形态不误报"），每 SN 10 分钟去重防每 5s 重试刷屏。
- **④bootstrap 自愈退避+封顶**：真栈双臂实锤 v1.7.29 的 healer 每 300s 恒频重跑 ensure（B_s4c：t+300s 恰一条重跑；A_s4c 对照组 v1.7.25 九分钟零重跑）——恒频把"条目与标记不匹配/source=hassio 时删建 Supervisor 托管 MQTT 条目"的破坏面节奏钉死在 5 分钟。改为 300s×2^(n-1) 指数退避、封顶 1 小时：引导未落地仍永久低频巡查（自愈目的不弃），首次触顶发一条"连续 N 轮未落地、不再逐轮刷日志"的收口 WARNING。修复条目文案与 Web UI 悬停提示同步改口径（"每 5 分钟重试"→"5 分钟起指数递增封顶 1 小时"）。

### Tests

- 新增 `tests/test_v1730_arbitration_backoff.py` 16 用例：认领先/抑后/新 id 放行/大小写不敏感/TTL 过期复放/容量闸；仲裁入口端到端"1 请求恰 1 发布"、发布失败不起看守；转正看守三态（无条目无卡片=恰 1 条 WARNING/已转正静默/卡片挂起静默）+ per-SN 去重；退避公式（1,2,4,8,8）+ 生产常数钉 + healer 实跑 sleep 序列与触顶 WARNING 恰好一条 + 恒频 sleep 反钉。`test_v1726_ear_ack.py` 接线钉升级为仲裁入口版（两耳反钉直调即红、正式 handler 零触碰钉保留）。全量 621 用例通过。

## [1.7.29] - 2026-09-17

MQTT 引导"持久自愈 + 可见修复 + Web UI 可见性"批（用户拍板 A+B+C，根治现场"mqtt not ready"长期滞留）。

### Added

- **A：bootstrap 持久自愈**（`mqtt_bootstrap.async_start_bootstrap_healer`）——旧行为只在慧尖条目 setup 瞬间跑一次 `ensure_mqtt_connection`，失败/错过窗口（官方 Mosquitto 后删、broker 晚起、表单不兼容）即静默等下次 reload/HA 重启。新行为：两分支 setup 均拉起后台任务（每 hass 单实例、幂等），引导标记存活期间每 300s 重试一轮；标记删除（落地）即清修复条目退出；慧尖条目全卸/宿主停机自动退出，不悬挂。`ConfigEntryNotReady`（内置 broker 未起）按"稍后再试"吞掉交下一轮；ensure 内部模块锁保证并发创建只发生一次。
- **B：可见修复入口**——自愈轮次未落地时在「设置→系统→问题」挂 `mqtt_bootstrap_pending` 修复条目（warning、is_fix_flow），config_flow 新增 `async_step_repair` 一键重试（重跑引导，标记消失即 abort `mqtt_bootstrap_fixed` 并清条目，仍在则回显 `mqtt_bootstrap_still_pending`）；strings.json/zh-CN.json 双侧同步（issues 节 + repair 步骤 + 两个新文案键）。
- **C：Web UI 第四状态项「HA MQTT 通道」**——服务状态板新增行：经 `/api/ha/` 代理查 HA Core 的 MQTT 集成条目（无条目=红/未就绪=黄/loaded=绿），悬停释义给出现场排障第一判据（前三项——broker 运行、集成安装、客户端计数——都看不见"HA 侧通道没打通"这个断点）；`.status-grid` 改 auto-fit 防第四项裂行。

### Tests

- 新增 `tests/test_v1729_bootstrap_selfheal.py`：healer 功能实测（标记两轮落地/异常轮续跑/无条目即退/单实例去重/ConfigEntryNotReady 吞）、两分支接线正钉、repair 流与文案键双侧对称、Web 第四项三件套（HTML id+label、JS 检测、CSS auto-fit）。全量 604 用例通过。

## [1.7.28] - 2026-09-17

注册表弃用面拆弹 + 心跳武装无限化批（现场两条日志 + CI E2E 实锤）。

### Fixed

- **设备注册表弃用面拆弹**（helpers/frame 告警：`device_registry.devices` 的**映射查找法** .values()/.items()/.get() 弃用，HA 2027.9.0 停摆；告警正解"iterate it to get the device entries"）：新增 `utils.iter_devices()` 统一出口——探测首元素类型，兼容新版集合（直接迭代条目）与旧版 Mapping（回退 .values()）双形态；api.py（告警点名处）、device_manager.py（计数+遍历共用一次快照）、`__init__.py`（卸载清理）三处接入。**两轮 CI E2E 实锤 `async_entries()` 在 DeviceRegistry 与 EntityRegistry 上都不存在**（首版两处臆造 API 均被打回）——实体侧保持 `entities` 映射（未弃用），实体查找用 `async_get()`。
- **心跳监听器"120s 即弃"改无限期耐心武装**（现场实锤：升级首启窗口 `等待 MQTT 集成 120s 仍未就绪，心跳监听器未武装` 后耳朵永久失聪，直到手动 reload 条目/重启 HA）：等待循环每 120s 一轮、每轮一条节流 WARNING（提示检查 MQTT 集成能否连上 broker），MQTT 一旦就绪立即补装订阅；条目卸载/reload 时自检退出（_bg_tasks 统一取消 + entry_id 双检），无悬挂任务。

### Tests

- 新增 `tests/test_v1728_registry_api.py` 三守卫：全源扫描禁 devices 映射查找法三型 + 双向反钉臆造 API `*.async_entries`；`iter_devices` 双形态功能实测（Mapping/集合/空表）；武装 while 循环/旧放弃文案反钉、无限等待保留卸载自检。全量 591 用例通过。

## [1.7.27] - 2026-09-17

耳朵代答格式定稿批（用户现场实锤：固件要求 001 应答必带 uuid，格式 `{"head":"$SH","ctype":"001","id":<回带>,"sn":<网关SN>,"data":{"errcode":0,"uuid":"<实例指纹>"}}`）。

### Changed

- **v1.7.26 耳朵代答补齐 uuid**：代应答与正式 handler 应答**完全同形**——`data` 含 `{errcode:0, uuid}`；uuid 由新函数 `utils.gateway_instance_uuid(hass)` 确定性计算（`uuid5(NAMESPACE_DNS, config_dir)`，与转正后正式 handler 的应答逐字一致，固件全程只见一个服务端指纹）。
- **指纹公式收敛单一真源**：`_lifecycle.__init__` 的 `instance_uuid` 改为调用 `gateway_instance_uuid(hass)`（值零变化，回归安全），删除本地 uuid5 字面量与死导入。

### Tests

- `test_v1726_ear_ack.py` 升级 v1.7.27 定稿：代答报文逐字含 uuid（用户实锤格式）、耳朵 uuid == `gateway_instance_uuid` == uuid5(config_dir) 确定性断言、`_lifecycle` 不再自带 uuid5 字面量反钉；全量 585 用例通过。

## [1.7.26] - 2026-09-17

耳朵级 001 代答批（用户裁定 A）。客户现场实锤：新网关首配期每 5s 重发 001 绑定请求，而旧链条中 HA 的应答只在「条目转正 → reload → 正式 handler 订阅」之后才发出——转正链任一环节断裂（如 HA MQTT 与网关不同侧）即成无限重试风暴，首配永不完成。

### Added

- **未配置网关首报 001 由耳朵当场代答**（两处接线：无 SN 等待条目的心跳监听器 + `_protocol` 他网关发现分支）：同型 echo `{head:"$SH", ctype:"001", id:<回带>, sn:<网关SN>, data:{errcode:0}}` 发到 `gateway/{sn}/req`（QoS 1）。**不带 uuid**——uuid 应答仍由转正后的正式 handler 按 2026-09-02 ack 契约规则 1 补发，固件对两次应答幂等（与在网 002 应答同型 echo 口径一致）。
- 门谓词 `should_ear_ack_001` 与契约同门：仅"001 且 data 无 errcode"代答——data 带 errcode 的 001 是网关对我方报文的回复，再答即成回环（规则 1 禁）；002/005 不在耳朵层代答。已配置网关的 SN 在代答前先行 return（防与正式 handler 双答）。代答发布失败仅留痕不抛出——装饰路径永不反噬发现主流程。

### Tests

- 新增 `tests/test_v1726_ear_ack.py`（14 用例）：门谓词 9 形态全覆盖（含 errcode 回复不代答/非 dict 归一防御）、代答报文逐字（id/sn 回带、无 uuid、QoS1/非 retain）、发布失败返回 False 不抛出、两处接线位置断言（已配置 return 之后/`not already_configured` 分支内）、`_handle_ctype_001` 零触碰反钉。既有 ack 方向契约（规则 1-6）全绿，全量 584 用例通过。

## [1.7.25] - 2026-09-16

流星下半屏覆盖批（用户点报："流星现在只有界面上半部分有，下部分没有，应该从中下部分也有流星进入"）。

### Fixed

- **v1.7.24 斜坠化的覆盖回归根治**：母本 1100px 斜轨迹纵向分量 ≈0.57×1100≈631px 系按手机竖屏（~667px）编排，桌面视口只扫过上半 ~58%——下半屏自此无流星。现起点改 `top: var(--my, -12%)` 逐颗编排：4 道保持顶部（-12%），3 道下放到中高度（meteor-2 26% / meteor-4 40% / meteor-6 12%），斜轨迹贯穿下半屏；角度 145°/35°、行程 1100px、10~16s 全周期、负相位进场、三色配比（白4/蓝2/琥珀1）与"只准向下"纪律全部不变。

### Tests

- 流星钉桩新增：基类必须读 `var(--my)`、逐颗 `--my` 显式在位、≥3 道中高度起点（下半屏覆盖防回潮）。全量回归 570 用例通过。

## [1.7.24] - 2026-09-16

流星方向订正批（用户点报"流星现在的运行方向不对"）：Web 移植版此前是 120px 竖直光条 `translateY(1100px) rotate(18deg)` 近乎垂直下坠、7 道同向——母本"斜坠"语义在移植时丢失。本批恢复小程序/官网母本同款几何。

### Fixed

- **流星几何=母本斜坠**：头改 2px 光点（`box-shadow` 双晕）+ `::before` 133px 渐隐尾线（透明→头侧增亮），关键帧 `rotate(var(--mrot)) translateX(1100px)`；默认 `--mrot:145deg` 斜左下（起点右半屏 4 道），`.meteor.mr` 置 `35deg` 斜右下（起点左半屏 3 道：meteor-1/2/7，`--mx` 8%/26%/35%）；只准向下——负角度/负位移禁回潮（-35° 爬升版废弃）。蓝/琥珀流星尾线随母本点头色（`m-blue #38bdf8`、`m-amber #fbbf24`，drop-shadow 辉光口径不变）；负相位进场、10~16s 全周期、reduce 慢速变体（850px，峰 .9 尾 .5）全部保留，`--mrot` 元素级变量令 reduce 下 `.mr` 方向同样不丢。

### Tests

- 流星钉桩翻转至母本几何：`rotate(var(--mrot)) translateX(1100px)` 逐字、双档角度 145°/35°、`.mr` 恰 3 道且为左半屏起点、禁爬升三断言（`translateY(-`/`rotate(-`/`translateX(-`）、reduce slow 帧仍斜向且垂直旧几何反钉；三色配比断言保留。全量回归 570 用例通过。

## [1.7.23] - 2026-09-16

「星辰大海」UI 底色跨仓统一批：Web UI 按小程序仓 v1.4.15 定案「方案A·深蓝夜空」（母本 98ebc5b）同步重做。用户点报旧底"过黑+过紫"（#030712 近纯黑 + 大面积氛围染紫在黑底叠脏、氛围压主体），夜空感改由底色自带蓝相承担。

### Changed

- **底色令牌**：`--bg-main #030712 → #071426`、`--surface #0a0f1e → #0a1a30`（v1.0.42 曾试过深靛紫 #1d1750 提亮路线，用户后续否决为"过紫"，本条为终定）；`theme-color` 随基底。
- **星空氛围底 `.star-bg`**：平涂黑底改 180° 深蓝渐变 `#071426→#0a1a30(55%)→#0c2138`；左上蓝角云降档（.22→.14）；**删除**整屏横贯的靛蓝大云与薰衣草/弱青大面氛围染紫（手机端"脏紫"最大来源，不回加）；紫全站仅两通道：右下小角 `rgba(139,92,246,.06)` + 星云尾锥 `α.035`。
- **星云三团"氛围退、主体进"**：主星云（蓝团）核提亮 `rgba(56,189,248,.1)` 并带蓝→靛→紫罗兰尾锥；青/金两团随"氛围退"退回官网克制原值 `.08/.07`（v1.0.42 的 .12/.10 提浓作废，禁回潮）。
- 青蓝主系令牌、星点/行星/流星各层节奏、玻璃卡体系与留空原则一律不变。

### Tests

- `tests/test_starsky_v1627.py` 钉桩同步刷新：令牌 #071426/#0a1a30、theme-color、方案A 渐变底与右下紫小角逐值钉死、大面染紫死值反钉（`.22 靛紫云 / 薰衣草 / .24 底靛 / 弱青 .12` 出现即红）、星云团 α 上限收紧回 ≤.10。全量回归 570 用例通过。

## [1.7.22] - 2026-09-12

手机端滑块防误触批（用户报：位置/速度/力度三滑块距离过近，手动调节容易误触发）。

### Fixed

- **三滑块触控误触根治（纯 CSS，桌面外观零变化）**：原布局行距仅 7px、滑杆命中高度=可见轨道 6px、拇指珠 19px——三行热区在指尖下几乎重叠，手指蹭过相邻滑杆即改值，松手 `change` 就把 `set_position/set_speed/set_strength` 发上空口。新增 `@media (hover: none) and (pointer: coarse)` 触摸能力块（不按宽度断点判定，手机横屏/小平板同样生效）：①行距 7→18px 形成相邻滑杆安全带；②透明 padding 把命中区撑到 30px 高（`background-clip: content-box` 保持 6px 细轨外观与拇指珠居中），30px 命中 + 18px 行距 = 48px 轨距 ≥ Apple HIG 44px 最小触控目标；③拇指珠 19→26px（Firefox 15→22px）更好捏住起拖。"轻碰轨道跳值"的行为面已由 v1.7.21 位置命令合并与三滑块 change 提交（松手才发令）兜底，本批不动 JS。

### Tests

- 新增 `tests/test_mobile_slider_touch_v1722.py`：触摸块存在性与插入位序（640px 块后、reduce 块前，保住 test_starsky_v1627 的 reduce 段切片断言）；行距 ≥16px；height−2×padding 恰等于基类 6px（可见轨道不变粗的前提）；命中 ≥28px；双引擎拇指珠下限；桌面基线钉死（gap 7px / input 6px / thumb 19px 不得被污染）；JS 无新增 touch/pointer 事件劫持（本批纯 CSS 口径）。

## [1.7.21] - 2026-09-10

HomeKit 真机回归批（用户真机暴露，先分析后动手）：机型百分比能力分流 + 位置命令合并。

### Fixed

- **机型能力分流——"假滑块"与"暂停丢失"同源根治**：v1.7.20 把 `SET_POSITION` 当**全局**能力声明，但百分比是逐机型能力（用户 2026-09-10 权威矩阵：5001 推拉窗 / 5003 低功耗窗帘 / 5005 内开内倒执手电机 / 5006 平推主机 / 5007 后装开窗电机 **支持**；**5002 平开窗暂不支持**，用户注明"以后有可能支持"）。两层后果：①5002 在 Apple Home 出现"拖了没反应"的假滑块（004 `w_travel` 打到无百分比硬件）；②形态由 `WindowCoveringBasic`（上游三段式：滑块 >70 开 / <30 关 / **中间停=暂停**）跳到 `Window`（纯位置透传）→ **暂停消失**。现按 `POSITION_CAPABLE_SN_PREFIXES`（SN 前四位 = 机型码）逐机型声明：支持机型 → `Window` + 真位置；5002 / 未知前缀 → 不声明该位 → 自动落回三态形态（**暂停回来**，且不再产生无效空口报文）；`current_cover_position` 对无百分比机型恒 None（不谎报位置）。**5002 未来支持百分比时：把 "5002" 加进该集合即可**，无其他改动。
- **位置命令合并（机制二：首发立即 + 窗口内只发最终值）**：Apple 窗子磁贴拖动期**持续写** TargetPosition（用户 broker 实测 34→46→47，间隔约 250ms），旧实现逐条直发 004 → 一次拖动十几条报文全压 LoRa 空口（还需等网关逐条 ack）。现行为：距上次下发 ≥ `POSITION_COALESCE_SECONDS`(0.5s) 的首次调用**立即下发**（保住 v1.6.9 failfast：未送达仍同步抛 `HomeAssistantError`）；窗口内后续调用只记 pending 并重置定时器；静默 0.5s 后**补发最后一条**（该路径已无调用方可抛错，失败落 warning——机制二的契约边界，用户已拍板）。定时器生命周期照抄 number 实体 v1.6.3/v1.6.4/v1.6.10 三教训：实体移除即取消、hass 失联丢弃 pending、补发路径 TOCTOU 守卫；越界/非法值仍在**合并之前**即拒（v1.6.19 B-LOW11 口径）。

### Tests

- 新增 `tests/test_v1721_position_capability.py`（23 用例）：五个支持机型声明 `SET_POSITION` 且掩码=15；5002 与未知前缀（含空 SN、短 SN）掩码=11 且无该位；`current_cover_position` 能力分流（含 255 端点兜底只对支持机型生效）；5002 位置服务被实体层拒绝且**零下发**；合并语义全覆盖（首发立即 / 拖动序列只发首末 / 窗口外仍立即 / 失败不占合并窗口 / 补发失败只告警 / 移除取消 / hass 失联丢弃 pending / 非法值合并前即拒）。
- 真栈 E2E H2 段扩为**双机型对照**（CI 硬门禁）：5007 → `SET_POSITION` + `device_class=window` + `current_position=50` + `position_capable=true`，并调 `cover.set_cover_position` 于真 broker 的 req 主题捕获 `w_travel=37` 的 004；5002 → 无位置位 + `position_capable=false` + 位置服务被 HA 拒绝（HTTP 4xx）+ **无 `w_travel` 报文泄漏到空口**。驱动改用真实机型码 SN（5007/5002）。

### Docs

- README「Apple Home（HomeKit Bridge）」补机型能力矩阵、5002 三态形态说明与命令合并行为。

## [1.7.20] - 2026-09-10

Apple Home（HomeKit Bridge）"窗子"正确映射批：开窗器实体补齐位置能力面。旧实体在 HomeKit 桥落入 `WindowCoveringBasic`（开/关/停三态、无百分比），本版起进入真正的 `Window` accessory（位置滑块 + 开度回显）。

### Added

- **cover 实体暴露位置能力（HomeKit Window 刚需）**：上游 `homeassistant/components/homekit/type_covers.py` 实证 `Window`/`OpeningDevice` 准入判据为 `supported_features & SET_POSITION`——缺位时 window 类设备退化为 `WindowCoveringBasic`（仅 open/close/stop，`current_cover_position` 恒 None 时 Apple 端也无开度可显）。用户以 template cover 实证"加上位置参数即正确映射成窗子"，本版本原生对齐：
  - `supported_features` 增加 `SET_POSITION`（完整位掩码 15 = open|close|set_position|stop）；
  - `current_cover_position` 返回真实 `r_travel`（0-100 如实回；未校准 255/缺失按开/关状态端点兜底 100/0，校准后自动恢复精确；与 `is_closed` 同款 `SENSOR_TIMEOUT_MINUTES` 时效闸，失联不谎报）；
  - `async_set_cover_position` 复用现成 004 `set_position` 命令链（与 Web 面板位置滑块同构），越界/非法在实体层即拒（v1.6.19 B-LOW11 口径：绝不静默兜 0 执行反向动作），失败与 open/close/stop 同族如实上抛。
- **v1.6.16"三键恒可点"定案不受影响**：防置灰的正解是 `assumed_state=True` 短路前端 `canOpen/canClose` 判据（`home-assistant/frontend` cover.ts 实证），"position 恒 None"只是当年的双保险——本版让位给 HomeKit 刚需，钉桩测试同步演进并保住 assumed_state 断言。

### Tests

- `test_cover_state.py`：`current_cover_position` 契约测试改判新语义（真实位置/255 端点兜底/时效闸/双盲 None 多面）+ SET_POSITION 位与位掩码断言；`test_command_failfast.py` 增 set_cover_position 成功参数/未送达抛错/异常抛错/非法值拒发四族。
- `tests/conftest.py` cover 替身对齐上游真实位值（`OPEN=1 CLOSE=2 SET_POSITION=4 STOP=8`——旧替身 `STOP=4` 恰占真实 SET_POSITION 位，位掩码断言在替身语义下会失真），补 `ATTR_POSITION`。
- 真栈 E2E（CI 硬门禁）新增 H2 段：真实 HA 核对 cover 实体 `device_class=window` + `SET_POSITION` 位 + `current_position=50`（002 上报驱动），并调 `cover.set_cover_position` 服务、在真 broker 的 req 主题捕获 `value=37/w_travel` 的 004 报文——Apple Home 滑块的完整链路（服务→集成→broker）真栈实证。

### Docs

- README 新增 Apple Home（HomeKit Bridge）映射说明：升级后需重启 HA（集成代码换载），桥按 Window 重建 accessory；未校准电机位置以开/关端点近似，校准后恢复精确百分比。

## [1.7.19] - 2026-09-08

共存根治批（方案一：桥主题白名单可配置化）：安装慧尖后，官方 Mosquitto 上的**任意** MQTT 加载项（ESPHome/Tasmota/Shelly/自研前缀…）可与慧尖互不干扰地全功能共存，不再只有 zigbee2mqtt 有活路。

### Added

- **`coexist_bridge_topics` 共存桥追加主题**（配置页「共存桥 · 追加桥接主题」）：逗号分隔 `主题[:方向]`（in=官方→慧尖、out=慧尖→官方、缺省 both），在默认双腿（`zigbee2mqtt/#` 双向 + `homeassistant/#` 进）之上追加桥腿。每条桥腿在 ha_mqtt 账号 ACL 逐条对齐派生（in→read / out→write / both→readwrite），维持 v1.6.24"爆炸半径不超桥白名单"安全不变量。改后重启慧尖生效；留空=行为与旧版逐字节一致。
- 安全红线**代码级固化，配置不可解除**：gateway/test/$SYS 保留树、`#`/`+` 通配首层（全匹配会圈进慧尖树）、`#` 非末层与 `+` 混层等 mosquitto 拒载形态、字符白名单外的一切注入形态（反引号/分号/换行/`$()`）全部在写入门上拒绝并打 `[共存桥] 拒绝 …` 日志跳过；homeassistant 树 out 腿永久钳制（防心跳回灌）；上限 16 树。

### Tests

- 新增 `tests/test_v1719_bridge_topics.py` 36 用例：净化器**逐字抽出生产 bash 段真实执行**（raw 值 base64 内嵌 + RAWLEN 自检防传输变形——开发期实锤旁路文件路径换算失败会令拒绝类断言集体假阴）；gateway/保留树/注入/通配形态逐类钉死；桥腿与 ACL 同源咬合静态锚。
- `test_v1624` 桥块红线条目纳入 `${BRIDGE_TOPICS_EXTRA}` 占位、ha_mqtt 段钉上 `${BRIDGE_ACL_EXTRA}` 注入点；e2e harness 生成器接 `TEST_BRIDGE_TOPICS_EXTRA`（默认空=既有实证不变）。
- 文档：README 共存章节改写为普适共存模型（含 ESPHome/Tasmota/Shelly 填法矩阵）；配置页说明卡 + Supervisor 中文/英文翻译同步。

## [1.7.18] - 2026-09-08

第 7 轮全量审计（5 路并行审计 + 父级逐条独立核验 + 上游 HA 源码实证）修复批：23 条真实缺陷清零，审计原始 32 条中 3 条误报驳回、约 7 条撞既往定案红线不列。钉桩 tests/test_v1718_audit.py（16 用例）+ 更新 test_v1712/test_mqtt_gate/test_discovery_proxy 至新契约。

### Fixed

- **订阅失败永久失聪链根治（B1）**：`setup()` 不再丢弃订阅结果——失败返回 False 走 ConfigEntryNotReady 由 HA 条目级重试自愈；巡检重建失败不再把 client 身份置 None（None=入口早退，"下轮再试"自锁死）——保留旧身份令重试自然可达。触发面：MQTT 条目禁用期 reload、启动拥塞期订阅失败。
- **003 绑定/解绑确认数值 SN 归一（B2）**：与 002/005 的 B-5 同型补齐——固件 JSON 数字形态回包不再触发 get_device 恒 miss（绑定误判）与 add_device TypeError（配对确认静默丢失、解绑幽灵设备面）。
- **新增 `window_controller_gateway.unignore_gateway` 服务（B3）**：误点发现卡片"忽略"后的唯一自救出口（v1.7.12 起忽略持久化跨重启，此前无任何解除路径）；`async_unignore_gateway` 自 v1.6 引入以来首次拥有生产调用方。
- **快速发现代理"耳朵确认"永久缓存改实时+冷却（B4）**：用户删除/禁用自动建的等待条目后 ≤30s 自动补种（旧版秒级发现静默死亡直到容器重启）；domain 匹配补 state 过滤——禁用/not_loaded 条目不再算耳朵。
- **mqtt_bootstrap 不再把禁用条目当有效 MQTT 配置（B5）**：全禁用形态保留引导标记+loud 告警（旧版改写死条目后删标记=自愈凭据蒸发；单检误判=永久 broker_not_ready 且根因不可见）；create 单实例拦截同源细分。
- WS 网关 wanted-None 分支删注册补 `is current` 判等（F2 对称补齐）；启动撞口复检活对端+等 1s 重试一次，不再"一次撞口=冻结永不监听"（B6）
- WS 端口聚合层补保留端口（2022/8099/8123/1883）运行时回退防线，非表单路径写入撞口端口不再静默失败（B7）
- `add_device` 容量闸改只拦新设备：满载网关上既有设备重配对/注册表自愈不再被整体拒绝（B8）
- `async_remove_entry` 直清全局持久忽略集：发现平台初始化失败时旧实现 discard 落在一次性临时 dict 上，删条目后网关永不再被发现（B9）
- 发现/手动添加表单被拒后回填改"用户输入优先"（旧版 context 恒优先=重显发现原值，输入像被吞）（B10）
- MQTT 就绪门禁 already_waited 快败仅限条目终态（setup_error/setup_retry/禁用）；setup 仍排队的 31s 落地窗口不再白报一次 broker_not_ready（B11，审计#3 精化）
- mqtt_bootstrap 头注释改为描述真实"强制接管"行为；入口统一熔断后的不可达死块删除（B12）
- 003/004 失败日志 `%d`→`%s`（字符串 errcode 不再产生 Logging error 噪音+日志丢失）（B13）
- `_norm_cmd_id(True)` 归一为 None（旧"原样返回"实际命中 `_bind_ops` 键 1 造成绑定方向误判）（B14）
- legacy（无 head/ctype）分支补齐在线记账与 auto_discovery 门禁（旧格式消息不再绕过三连守卫）（B15）
- WS control 拒绝 inf/nan/1e+308 等非十进制线值（旧版透传设备不可解析值还回 ok=true 假成功）（B16）
- Web：配对窗口内全量重建保留"配对中"黄徽（旧版重建即复位"检测中"，L-3 守卫失效被覆写"离线"误导重复点击）（B17）
- Web：silentRefresh/控制后刷新不再回写用户正在拖动的滑块（activeElement 判定；松手 change 照常发送、下轮自然同步）（B18）
- Web：网关列表加载失败文案细分——12s 超时/断连/401 各自表述（旧版全归因"未走 HA 侧边栏"误导排障方向）（B19）
- run.sh：桥对账时间戳 `$((10#…))` 显式十进制解析——"0 开头八进制坑一律归 0"的注释承诺首次真正落地（B20）
- run.sh：用户名白名单校验上移至凭据恢复块之前（旧顺序校验不可达）；报错不再回显未校验原值（日志注入面）（B21）
- config.yaml：fast_auto_discovery 注释"代理绝不自动建条目"改为准确描述（会建零功能等待条目，v1.7.11 定案）（B22）
- CHANGELOG：v1.7.11~15 日期修正为 git 真实提交日期（09-05/09-06，旧标 09-07/09-08 与 v1.7.16/17 时序倒挂）（B23）
- manifest 的 documentation/issue_tracker 指向不存在的旧仓库名，修正为 ha-gateway-plugin
- 初启桥接日志"重启 broker 生效"措辞纠偏（首启本无重启可言）

## [1.7.17] - 2026-09-06

### Fixed
- **现网安装失败（镜像拉取 connection reset）——镜像主源切换 ghcr.io →
  ghcr.1ms.run（毫秒国内透传）**：2026-09-06 客户实锤 Supervisor 直连
  ghcr.io 拉 1.7.16 时 blob 传输中途 `read tcp [2606:50c0:8000::154]:443:
  connection reset by peer`（IPv6 走 GitHub 海外 CDN 被干扰）——v1.6.20
  "源站稳定"定案的网络环境已不复存在（对比 v1.7.10：image 链路与镜像
  内容均无回归，纯网络侧恶化）。1ms 同日实测 manifest+blob 匿名可拉
  （热缓存 ~1.1MB/s+），且 CI warm-mirrors 发版即预热。已知残余风险
  （发版后 1 小时新 tag 边缘 404 闪断窗口）写入定案注释与 README 自救
  指引；nju 降为次选手动备源；主源演变史完整留档 config.yaml。
- 镜像钉桩升级：`test_config_primary_is_ghcr_io_source_1620`（把某一版
  定案写死、历次迁主源都被迫改测试）重构为
  `test_config_image_domain_in_proven_candidate_set`——只钉"域 ∈ 实测
  候选集 {1ms, nju, ghcr.io} + {arch} 路径模板不漂"，具体主源以
  config.yaml 定案注释为唯一权威。

### Changed
- **README 整体重写**：①「与 zigbee2mqtt 共存」从 FAQ 提升为正式章节
  （路径 A 直连 4 步 / 路径 B 共存桥开关+官方凭据，含误桥警示与主题
  隔离说明）；②取消「常见问题」章节——仍有效的条目就地化（安装镜像
  失败自救 → 安装章、升级/数据安全 → 对应小节），已修复的历史缺陷
  条目（v1.6.17 :80 占用等）删除（CHANGELOG 留档）；③大幅瘦身：
  45 行 ASCII 架构图删除（链接 ARCHITECTURE.md）、配置项明细不再双处
  维护（v1.7.15 起配置页原生中文说明），178 行 → ~100 行。

## [1.7.16] - 2026-09-06

### Fixed
- **加载项从商店静默消失（v1.7.12 引入的回归，P0 阻断）**：v1.7.12 把
  config.yaml schema 写成 `username: str=huijian` / `password:
  password=huijian2022`——**`=默认值` 是臆造语法**，Supervisor 的 schema
  元素校验正则 `RE_SCHEMA_ELEMENT`（`supervisor/apps/options.py`，
  2026.05.1 前在 `supervisor/addons/options.py`）只接受 `类型`、
  `类型(min,max)`、尾缀 `?` 三种形态，上游全历史（0.62→2026.09 各 tag）
  实证从无 `=`。商店每次刷新时整份 config.yaml 校验失败，
  `store/data.py` 记一行 WARNING `Can't read .../config.yaml` 后即
  `continue`——**加载项从商店整体消失**（仓库添加仍显示成功、其它内容
  不受影响），新装用户按 README 添加链接后找不到"慧尖 LoRa 网关"，
  与 2026-09-06 客户现网报障完全吻合。
  修复：schema 回退为 v1.7.10 之前的合法形态 `str` / `password`。
  **行为零损失**——新装默认值本就由 `options:` 块供给（Supervisor
  唯一正规途径，huijian/huijian2022 一直在位），运行期另有 run.sh
  启动自动恢复兜底。
- 订正 `tests/test_v1712_audit.py` 凭据默认值钉桩：原版把 `=` 假语法
  钉成了断言（错误语法骗过全部 463 个测试的又一实锤——schema 从未按
  Supervisor 真实语法校验过，CLAUDE.md"静默失效面须补断言实参的测试"
  教训第三次重演）。现钉 `str`/`password` 类型声明 + options 默认值
  双契约。

### Added
- 防复发钉桩 `tests/test_v1716_store_schema.py`（4 例）：逐字抄录上游
  `RE_SCHEMA_ELEMENT` 与 `watchdog` 商店校验正则——每个 schema 值必须
  匹配、`=值` 假语法点名禁止回潮、非 `?` 必填键必须有 options 默认值、
  watchdog 形态合规。上游语法若演进须同步本钉复核，而非改回 `=`。

### Notes
- 修复推送后验证路径：Supervisor「设置→加载项→加载项商店→⋮→检查更新」
  刷新（商店元数据有缓存），「慧尖 LoRa 网关」即重新出现在商店；
  Supervisor 日志中对应的 `Can't read ...huijian_mqtt_broker/config.yaml`
  WARNING 应随之消失。已装用户不受本回归影响（安装走的是本地副本），
  但升级检查依赖商店条目，故本修复同样解除其"查不到更新"的断联。
- v1.7.15 的 translations/ 本地化文件与本回归无关（上游实证：翻译文件
  畸形仅 WARNING 跳过该文件，从不影响加载项商店可见性）。

## [1.7.15] - 2026-09-06

### Added
- **Supervisor 配置表单官方本地化（用户要求"配置界面不要全部英文"）**：
  新增 `translations/zh-Hans.yaml` + `zh-CN.yaml`（同文兜底）+
  `en.yaml`——采用 Supervisor 官方翻译机制（一手源码实证：
  `store/data.py::_read_addon_translations` 读加载项目录
  `translations/<语言代码>.yaml`，`configuration.<键>` 提供
  name/description）。此前"表单无字段说明机制"的答复只对了一半：
  英文键名标签确实不可改 schema 键，但**官方 translations 层可以把
  每行渲染成中文标题+中文说明**。8 个配置项全部配中文（凭据勿改/
  共存桥默认关定案/官方凭据成对填写等口径与 Web 说明卡一致）。
  翻译文件随商店仓库分发（Supervisor 直接读仓库目录，非容器内文件），
  用户刷新商店更新加载项元数据即见中文配置页。
- 防漂移钉桩 `tests/test_v1715_addon_translations.py`（4 例）：三份
  翻译与 schema 键双向对齐、每条目必填 name 齐备（缺一条 Supervisor
  拒收整份文件、全字段一起失语）、zh 双文件逐字同文。

### Notes
- v1.7.14 的 Web UI 说明卡保留（双通道：配置页原生中文 + 页面速查表）。
- 本次发布随附未单独发布的 **v1.7.13 / v1.7.14** 变更，要点：
  - **共存桥默认停用**（v1.7.13）：`coexist_bridge_enabled` 默认 false，
    慧尖不再自动向官方 Mosquitto :1883 搭桥——消除官方 7.x 强制认证下
    匿名桥 30 秒一次的 not authorised 日志风暴；升级重启后已写入的桥段
    自动拆除。要用 z2m 共存时在配置页打开开关并成对填官方凭据即可恢复。
  - **Web UI 新增「插件配置项中文说明」卡**（v1.7.14）：连接信息卡下方
    可折叠速查表，逐项中文讲清 8 个配置项。

## [1.7.14] - 2026-09-06

### Added
- **Web UI 新增「插件配置项中文说明」卡**：Supervisor 配置表单只显示英文
  键名（平台无字段说明机制，schema 仅支持类型声明），现于慧尖 Web UI
  连接信息卡下方逐项中文讲清 8 个配置项：username/password（固件内置、
  改会自动恢复，勿动）、auto_setup_ha_mqtt、install_integration、
  coexist_bridge_enabled（v1.7.13 默认关的由来与开启方法）、
  coexist_official_user/password（须填官方自己的账号、成对填写）、
  fast_auto_discovery。可折叠（details/summary 原生零 JS），改完配置
  同屏即查，不用翻 README。
- 防漂移钉桩 `tests/test_v1714_config_guide.py`：schema 新增配置项漏进
  说明卡、说明卡引用不存在键、共存桥"默认关"定案叙述回潮——任一情形
  CI 直接红。

### Changed
- huijian.css 补 `code` 等宽可读样式与 `.cfg-table` 行分隔、折叠卡
  caret（v1.6.7 卡片体系建成时无 code 场景，本卡首次密集使用）。

## [1.7.13] - 2026-09-06

### Changed
- **共存桥默认停用（用户定案："先把搭桥注释掉，以后用再说"）**：
  `coexist_bridge_enabled` 默认值 true → **false**。官方 Mosquitto 7.x
  强制认证下，未配桥凭据的自动搭桥退化为对端每 30 秒一条
  `not authorised` 日志风暴（慧尖自身无恙、纯噪音）；且判据"本机 :1883
  在听即搭桥"会把宿主上任何第三方 1883 服务误当官方 broker。默认关后：
  不建新桥，**已写入的桥段由对账循环自动拆除**（升级重启慧尖即清净）。
- 能力零损失：将来需要 zigbee2mqtt 与慧尖共存时，在加载项配置页重新
  勾选打开本开关，并成对填写 `coexist_official_user` /
  `coexist_official_password`（官方 broker 自己的用户）即可恢复桥；
  z2m 亦可直接连慧尖内置 :2022（预置账号 huijian_z2m，默认关桥后
  该直连路径不受影响）。
- 注释同步订正：run.sh 桥段头部"默认开=零配置共存"叙述改为反映新
  默认语义；tests/test_v1624.py 默认值钉桩随定案更新。

### Tests
- 全量回归通过（pytest / bash -n run.sh / compileall）

## [1.7.12] - 2026-09-05

### Added
- **MQTT 凭据启动自动恢复默认（用户定案）**：LoRa 网关固件内置
  `huijian/huijian2022`（端口 2022 本就固定），插件配置若被改动，run.sh
  启动时自动恢复为固件内置值并在日志留痕（密码不回显）；config.yaml
  schema 默认值同步为固件内置值。用户从此无需也无法错配凭据——历史上
  "改了用户名/密码导致网关集体失联"的排障类别从根上消除。
- **"忽略网关"跨重启持久**：发现卡片上的"忽略"此前纯内存，HA 重启后
  被忽略的网关复活。现经 persist.py 统一落盘（与手动删除列表同通道），
  删除网关条目时同步清除其忽略记录与设备速度/力度设定值。

### Fixed
（第 6 轮全谱审计：5 路并行只读审计 + 逐条独立核验，以下均为核实修复）
- mDNS 与发现代理两个看门狗在 `set -e` 子壳下恒死（`cmd; RC=$?` 失败即
  终止子壳，重启分支永不执行）→ 改 `RC=0; cmd || RC=$?`（v1.6.3 C3 同族）
- HA MQTT 条目 reload/重建后本集成入站订阅永久失聪（发布照常、
  gateway/rpt_rsp 永不再达，网关 30 分钟被误判离线）→ 30s 巡检比对
  client 身份，换代自动重建订阅；订阅入口返回成败、失败补重连调度
- mqtt_bootstrap：标记缺 broker 字段时的空值更新面熔断；配置条目
  密码比较改为值相等判断并豁免 `!secret`/`!env_var` 托管凭据（旧版
  对托管值恒判不等→无谓 reload MQTT 条目）
- 005 设备上报补 auto_discovery 门禁（关闭自动发现后未知设备不再经
  首条上报入库；ack 契约不变）；去重记账处理失败回滚（消除网关重发
  被 5s 去重窗吞掉的更新黑洞）；字符串 `"0"` id 恢复直发旁路
- device_manager：新设备首建未播 last_update、上报未刷新时间戳（30s
  窗口期误判离线）；注册表写失败映射簿记缺失与自愈；添加回调异常
  gather 兜底留痕；"手动删除竞态"窗口内自动发现复活设备即时回滚
- 配置流向导：空 SN 引导条目幽灵 unique_id 清除；replace 步骤对空 SN
  条目 KeyError 拆雷；options 表单缺 `ws_port_reserved` 错误文案补齐
- 发现步骤 3.5 自动填充时回填 unique_id（带占用判重），使 HA 原生
  查重覆盖到全自动配置的条目
- WS 网关：启动失败分支误删并发成功方注册（孤儿监听）→ 身份守卫 +
  迟到让位复检 + 停止中实例不再热同步；踢客户端改并发（STOP 路径
  最坏 40s 卡死消除）；control 命令 value 类型白名单（dict/list 假成功
  透传面关闭）
- Web：设备为空时假红"离线"改中性"未知"；"HA MQTT"状态项更名
  "MQTT 客户端"并加悬停释义（该数含全部 :2022 客户端，旧文案在 HA
  条目断连而网关在线时显示假绿灯）；toast 叠字/连点刷屏修复；改名
  客户端校验；position=null 渲染出 "null%" 修复；配对中黄徽不再被
  静默刷新覆写；SN 映射重建前清理；jsQuote 行终止符转义
- 发现代理：重放发布结果不再丢弃——mosquitto_pub 被拒不记账，随下条
  上报重试（旧版失败仍打"已重放×2"假日志且该 SN 永不重试）
- 持久化 .bak 轮转失败/非原子降级直写不再静默（数据时效可追责）
- 服务调用"设备 ID"支持子设备 UUID 经映射反查所属网关（旧版误报未
  找到网关）；devices API 缺参不再返回全设备注册表（收紧为本集成
  设备）；manifest 显式声明 http 依赖
- CI：config.yaml 版本提取失败即时报错（空版本号曾可假绿穿透一致性
  门并污染 tag/Release）；tests/e2e/*.sh 纳入 bash -n 语法门；prepare
  拉取 20 条历史供 changelog 兜底；Gitee Release 正文缺段时复用
  prepare 产物不再崩 job
- ingress.conf 与 run.sh heredoc 的"机械 diff 门禁"由虚言落实：
  tests/test_v1712_audit.py 归一化逐行比对，漂移即 CI 失败

### Tests
- 新增 `tests/test_v1712_audit.py`（42 例：功能实跑 + 修复形态钉桩）

## [1.7.11] - 2026-09-05

### Added
- **快速自动发现（`fast_auto_discovery`，默认开）**：修复结构性缺口——全新
  HA（从未添加过慧尖集成、零 config entry）没有任何 `gateway/rpt_rsp` 订阅者
  （心跳监听器挂「无 SN 等待条目」、其它网关发现挂「已配置条目」，两者都要求
  条目先存在），网关上报再多也没有耳朵听，发现卡片永不出现，只能手动填 SN。
  现由加载项容器内新增 `gateway_discovery_proxy.py` 补位：长驻订阅上报，捕获
  001/002/005 后若集成尚无任何条目，经 Supervisor API 创建一个 gateway_sn 留空
  的「等待模式」条目（集成既有设计，零功能耳朵），并重放原报文一次——此后
  **首台网关全自动配置完成**（心跳监听器 → 发现链 3.5「自动填充空条目」，
  网关+子设备直接注册，无需任何点击）；**第二台起弹标准发现卡片**由用户确认
  （无空条目可填时走 config flow，unique_id 幂等）。代理仅做装耳朵+重放各一
  次/SN，网关配对确认权、多网关去重、忽略语义全部复用集成既有代码。真栈
  A–E E2E 全绿（`tests/e2e/fast_discovery_e2e.sh`，12 项断言：A 无代理 10 连发
  0 条目 → B 1 条上报全自动配齐（网关+子设备入注册表）→ C 风暴 30 连发恒 1
  条目 2 设备 → D 第二 SN 出卡片且不自动建条目 → E 最脏环境：慧尖+MQTT 条目
  全删后 1 条上报，MQTT 条目被自动重建、整链仍自动配齐）。
- 集成新增 `_platforms_forwarded` 运行时标记（配套修复见 Fixed）。

### Fixed
- **等待模式条目 reload 平台卸载 ERROR 风暴**（真栈实锤，代理使其变主路径后
  暴露）：`async_unload_entry`/`_cleanup_partial_setup` 硬编码对 5 个平台调
  `async_unload_platforms`，而 awaiting 条目从未 forward 过任何平台——HA≥2024
  平台组件对 never-loaded 条目抛 `ValueError: Config entry was never loaded!`，
  每次 reload 刷 5 条 ERROR traceback（历史版本等待模式条目现场几乎不存在、
  自动填充链从未走完过，故未暴露）。现以 `_platforms_forwarded` 为门禁。
- **发现链 3.5 自动填充双 reload 竞态**：`async_update_entry` 本就经 update
  listener 触发 reload，显式 `async_reload` 再叠一次——交错竞态。移除显式
  reload，与 v1.6.19 `_migrate_devices_async` 定案口径统一。
- **等待模式条目缺 MQTT bootstrap**（相位 E 真栈实锤修复）：干净客户机
  （从未装过官方 Mosquitto、HA 无 MQTT 条目）上，此前只有 config_flow
  （手动添加/卡片确认）会驱动内置 broker 引导——代理建的零交互等待条目
  setup 后心跳武装等 120s 超时，自动发现链静默断掉。现 awaiting 分支
  setup 尽力调用 `ensure_mqtt_connection`（无标记文件时本就 no-op，不劫持
  非一体化安装），E2E 实证「慧尖+MQTT 条目全删」起点也能自动重建 MQTT
  条目并整链配齐。
- 新增回归：`tests/test_unload_awaiting_v1711.py`（5 项，含逐字复刻 HA
  never-loaded 行为的假平台组件）+ `tests/test_discovery_proxy.py`（21 项）。

## [1.7.10] - 2026-09-07

### Fixed
- **run.sh 桥接 heredoc 反引号 bug**（HA2 现场实锤：`/run.sh: line 664:
  password: command not found`）：v1.6.26 在共存桥 heredoc 体注释里写的
  「反引号 password 反引号」被未引号 `<<EOF` 当作命令替换执行——函数无害
  （rc 被忽略、mosquitto.conf 正常写入、broker 正常启动），但每次启动污染
  日志且严重误导排障。改为普通引号，并新增回归钉桩
  `tests/test_runsh_heredoc_v1710.py`（静态扫描全部未引号 heredoc 体禁
  反引号 + bash -c 新旧写法对照）；桥 harness 同步再生成

## [1.7.9] - 2026-09-07

### Changed
- **Web 端（桌面）改回 v1.7.7 样式**（用户令「web 端改回去」）：`.page`
  max-width 1400→**1140px** 复原；`.device-list` 恢复 **auto-fill +
  minmax(280px,340px)** 轨道封顶（v1.7.8 的 auto-fit 等分撑满作废）；
  `.device-item` 移除 680px 封顶兜底
- **手机端全对齐（v1.7.8）保留不变**：≤640px 全部 .card 统一 360px 居中；
  闪屏修复（v1.7.5）保留不变

## [1.7.8] - 2026-09-07

### Fixed
- **手机端卡片左缘不对齐**（用户附实拍图指认）：设备卡 360px 居中而连接
  信息卡全宽，两卡左缘错位——改为 ≤640px 下**全部 .card 统一 360px 居中**
  （横向内边距收 2px 对齐 card-flush，卡内网关卡/设备瓦片/连接瓦片/蓝色
  说明框回归自然撑满内容区）；v1.7.5~1.7.7 的 gateway-item/conn-item/
  info-box 单独封顶与 calc 抵消方案全部作废

### Changed
- **Web 端两边太空**（用户附桌面截图）：`.page` max-width 1140→**1400px**；
  设备瓦片轨道 auto-fill+340px 封顶 → **auto-fit minmax(300px,1fr)** 等分
  撑满整行（v1.6.29「轨道封顶保留」口径按用户新令作废），单设备由
  `.device-item` max-width:680px 居中兜底防拉成一条
- 闪屏修复（v1.7.5 静默更新）保持不变

## [1.7.7] - 2026-09-07

### Changed
- **手机端网关卡（含子设备瓦片）320px→360px 居中**（用户改令与底部蓝色
  玻璃统一宽度）：≤640px 下 `.gateway-item` max-width 360px 居中，
  card-flush 无内边距 max-width 直接生效；`.conn-item`/`.info-box` 维持
  v1.7.6 的 360px 不变；手机端闪屏修复（v1.7.5 静默更新）保持不变

## [1.7.6] - 2026-09-07

### Changed
- **手机端底部蓝色玻璃统一 360px 居中**（用户令「底部蓝色玻璃全部统一
  宽度，说明框 360px 居中」）：`.conn-item` 连接瓦片与 `.info-box` 说明
  框统一 360px 居中收窄；网关卡（含子设备瓦片）保持 v1.7.5 的 320px
- v1.7.5 的手机端闪屏修复（控制后走静默更新不重建）保持不变

## [1.7.5] - 2026-09-07

### Fixed
- **手机端控制后"闪一下"**（电脑端无感）：开/关/停/内倒等控制与「检查状态」
  后 2s 的刷新由 `loadGatewayDevices`（innerHTML 全量重建）改为
  `updateGatewayDevices`（静默只更新状态）——重建会重放 `.device-item`
  的 fadeUp 入场动画 + backdrop-filter 重排，手机慢渲染即肉眼可见的
  整列闪烁；静默版自带设备增删检测（有变化自动升级重建，不漏更新）。
  重命名保留重建（名称文本只在重建时刷新，低频可接受）

### Changed
- **手机端卡片收窄**（用户令"太宽了不好看"）：≤640px 下网关卡（含内部
  子设备瓦片）与底部蓝色玻璃说明框（.info-box）统一 **320px 居中**，
  两侧留空让星空透出

## [1.7.4] - 2026-09-07

### Changed
- **流星三色（用户令对照官网）**：7 道流星按官网 createMeteor 配比
  60% 白 / 25% 蓝 / 15% 琥珀（白 4 + `m-blue`×2 + `m-amber`×1），蓝尾
  #38bdf8、琥珀尾 #fbbf24，各带同色 drop-shadow 辉光
- **新增星团层（用户令「多颗星星聚一团、明暗变化」附官网截图）**：
  starsky.js `makeClusters()` 固定种子生成 4 团（屏中上带 14/40/67/86%），
  每团 6~8 颗聚簇（±4.5%×6% 散布），每团 1~2 颗核心大星（2.6~3.4px +
  8px 白晕，官网 canvas 大星 glow size×4 的 CSS 对应物）；正弦明暗
  .1↔.9、三色配比同星点、reduce 豁免同星点层

## [1.7.3] - 2026-09-07

### Changed
- **星点按官网母本放大提亮（用户令"星星尺寸过小，对照官网调整"）**：
  far 0.6~1.4px→**1.8~2.5px**、near 1.2~2.2px→**2.5~3.2px**（官网
  2/2.5/3 三档）；明灭由"单峰触零"改官网**正弦 .1↔.9 永不黑**
  （峰亮度 far .55~.75、near .8~.95，下限 `--floor`=峰×.13）；周期统一
  官网 3~7s；新增官网三色（白 60% / 蓝 #38bdf8 20% / 琥珀 #fbbf24 20%）
  与大星 4px 白晕（near ≥2.9px）。行星群 16 颗口径不动（其"单峰触零 +
  双层独立时钟"是小程序标准，与官网星点是两套母本各管各层）
- 静态资源 cache-bust query 升至 `?v=1.7.3`

## [1.7.2] - 2026-09-07

### Changed
- **取消顶部页头玻璃（用户令，留空原则扩到头栏）**：`.header` 容器
  background/border/backdrop-filter/box-shadow 全清、右上装饰高光
  `::after` 移除——只留布局与 logo/标题/按钮自身（玻璃仍自持在
  网关头 `.gateway-header` 与设备瓷砖 `.device-item` 上，不受影响）
- 静态资源 cache-bust query 升至 `?v=1.7.2`

## [1.7.1] - 2026-09-07

### Changed
- **页头 logo 外框正圆 + 发光强化（用户令）**：`border-radius: 14px→50%`
  圆盘（img 同步圆形裁切），新增内层径向发光盘面 `::before`
  （rgba(125,211,252,.5)→transparent），双环光晕+本体 drop-shadow 保留，
  「最大一颗星」观感整体成形
- **减弱动画下星云漂移纳入豁免（用户令「没有星云」）**：三团 α.07~.10
  静止态在深底上不可辨、运动才可见——`.nebula::before/::after/.nebula3`
  恢复 45/50/60s 超慢漂移（比星点更温和，符合"宁慢勿快"；用户授权对
  官网母本的增强偏差）

### Changed
- **静态资源 cache-bust（根因修复）**：`huijian.css / starsky.js / huijian.js`
  引用统一挂 `?v=版本号`。现场实证：Supervisor update 实体
  `installed_version=1.7.0`（容器确为新版），但用户浏览器呈现的仍是
  ≤1.6.29 旧界面（星星停闪/星云不见/服务状态长玻璃/logo 无光晕）——
  nginx `no-store` 无法穿透 Service Worker/国产浏览器壳等一切客户端缓存
  形态，URL 恒定则旧缓存永生。此后每次发布 query 随版本变化强制穿透；
  钉桩禁止任何裸 css/js 引用

## [1.7.0] - 2026-09-07

### Fixed
- **减弱动画下星云星星"消失/不闪"（用户令对照小程序标准）**：官网星星为
  canvas rAF 驱动（CSS 守卫拦不住）、小程序双层独立时钟常动——插件星点/
  行星群此前只靠 CSS 动画，reduce 下被守卫冻停。现星点明灭 `var(--dur)`、
  行星漂移 `var(--drift)`（55~75s）、行星辉光闪烁 `var(--twk)`（3.5~7s）
  三层同流星一样在 reduce 下按各自周期复流（仅星空装饰层豁免，内容层
  转场仍静止合规）；星云三团漂移不豁免（官网 CSS 层同停）

### Changed
- **背景按小程序标准加一点紫（用户定案"紫仅限氛围底色层"）**：底靛云
  α.12→.16 且中心回收进视口，右上角弱青上叠 #8b5cf6 靛紫云 .09；
  星点/行星/星云/按钮等前景一律不加紫
- **子设备控制按键与数值框透明化（用户令）**：开/关/停/内倒四键由实心
  渐变块改透明 ghost（currentColor 描边 + 同色文字承载语义，hover 轻提亮），
  `.slider-value` 数值小框去底去边框——玻璃层级只留瓷砖本体，与留空原则
  同系；作用域限 `.control-row`，网关区配对/状态键与模式键不受影响
- **页头慧尖 logo 发光（用户令，对齐小程序标准③「logo＝最大一颗星」）**：
  玻璃盘外晕升级为双环光晕（近环 22px/α.55 提形 + 远环 56px/α.22 铺氛围），
  logo 图本体加 `drop-shadow(6px)` 沿 alpha 轮廓发光，随 hero-breathe 呼吸
  脉动、reduce 下静止但仍亮；PIL 像素实测近环带 (63,146,158) vs 基准
  (4,29,48)、远环带 (45,117,144) 光晕成形
- **页头下方"服务状态"长玻璃板取消**（用户令，留空原则扩面）：状态卡加
  `card-flush`——面板背景/边框/阴影/模糊全部透明透星空，仅保留紧凑
  padding；三个状态胶囊（`.status-item` 自带浅玻璃+状态色描边）不受影响
  照常浮于星空上，与网关/设备区容器同款口径

## [1.6.29] - 2026-09-07

### Fixed
- **减弱动画下"没动图没流星"（用户同浏览器对照官网实证）**：官网尾注注入
  `.meteor { animation-duration: 12s !important }` 级联穿透其自身 reduce
  守卫——官网在系统关动画时流星照流。母本行为即标准：本插件 reduce 守卫
  同开流星例外（`meteor-fall-slow` 温和关键帧：峰 .9 尾 .5 程 850px，
  保留各自 10~16s 周期与负延迟相位、infinite），星点/行星/星云在 reduce
  下仍静止合规。CDP 实测 reduce 态流星 opacity 0.88/0.72/0.50 在途、
  整屏动帧 diff 0.85%（旧两版口径——全隐身、静态冻结——均作废）

### Changed
- **子设备玻璃统一按 5005 高度**（用户改令，作废 v1.6.27"各自 hug"口径）：
  行内 stretch + `.device-item` min-height 262px 双保险，无 5005 的行也齐高
- **页头采用慧尖真 logo**：`www/img/logo.png`（563×563，取自插件
  `logo.png` 素材）替换 📡 emoji，玻璃盘呼吸/悬浮动画保留；素材随
  Dockerfile `COPY www/` 整目录进镜像

## [1.6.28] - 2026-09-07

### Fixed
- **空态提示被 grid 轨道挤偏**：`empty-hint`（"暂无子设备，点击「配对」
  按钮添加"）是 `.device-list` 的子 `<p>`，v1.6.27 把该容器改成 grid 后
  提示被塞进首根 ≤340px 轨道左偏显示——补
  `.device-list .empty-hint { grid-column: 1 / -1; }` 横跨整行居中
  （教训泛化：容器布局体系切换时必须清点容器内**非主内容子元素**的
  全行语义）。CDP 真实 DOM 实测三视口（1280/760/390）：提示占满行、
  所有瓷砖零横向溢出、按钮行零溢出、瓷砖高 262/132/96 各自 hug 内容

### Changed
- CHANGELOG 1.6.27「活的宇宙」条星云口径措辞订正（单团旧述→官网三团），
  消除与同节「星云＝官网母本口径」条的自相矛盾

## [1.6.27] - 2026-09-07

### Changed（Web UI「星辰大海」重设计——对齐小程序 UI 设计标准）
- **视觉体系整体切换**：旧版浅色靛紫主题（#4f46e5 主系）作废，按小程序
  「星辰大海」唯一权威标准（记忆条 0d2200c5）移植：深空 `#030712` 底 +
  青蓝令牌系（--primary #0ea5e9 / accent #06b6d4 / 提亮 #7dd3fc），紫仅
  保留在氛围底色层（星点/图标/按钮一律无紫）；卡片/头部/徽章/弹层全部
  改透明玻璃悬浮（--bg-card .07 + --card-border .14 + backdrop-blur）
- **活的宇宙背景层**（新增 `www/js/starsky.js`，mulberry32 固定种子＝
  全站同一片天）：三档氛围角云底 → 官网口径三团星云（蓝左上/金中上/青右下）
  → 双层视差星点（明灭单峰触零）→ 行星群 16 颗（3/4/6px 三档、辉光宁淡
  α.35/4~7px、双层独立时钟：漂移 55~75s 与明灭 3.5~7s 解耦）→ 流星 7 颗
  （10~16s linear 全周期坠落、行程 1100px、只准向下）；`prefers-reduced-
  motion` 全停
- **logo＝最大一颗星**：玻璃盘提亮（32% 盘 + 38% 亮环 + #7dd3fc 光环，
  深空必须）+ 呼吸 0.7→1 与悬浮双动画并行（品牌永不触零）
- 语义色按暗底重排（badge/toast/slider/状态点低 α 化），布局与全部
  DOM 钩子不变（huijian.js 零改动）；移动端断点与无障碍焦点样式保留
- **留空原则（用户校准）**：网关与子设备区容器不再整块铺玻璃底
  （`.card-flush`），未添加的位置直接透出星空；玻璃只落在真实存在的
  悬浮件上——网关头部条与设备瓷砖各自自持 `--bg-card + backdrop-blur`
  （对齐小程序标准⑤"白垫底透光/磨砂磨糊星星均被否"同源教训）
- **设备玻璃框只包自身内容**：`.device-list` 加 `align-items: start`——
  原默认 `stretch` 会把矮内容瓷砖纵向拉高去齐平同行最高框，多出一段
  无内容的空玻璃；现每张开窗器玻璃只比其单独设备内容大一圈（用户校准），
  各框互不等高、间隙透出星空
- **星云＝官网母本口径（用户校准）**：作废单团适配版，逐字移植
  v0.0.1 L105-117 三团结构——蓝团 `.nebula::before`（左上 .10/45s）+
  青团 `::after`（右下 .08/50s）+ 金团 `.nebula3`（中上 .07/60s，新增
  DOM 元素）与 aurora-move1/2/3 关键帧；三团皆 radial 柔边、无 blur、
  无内核白雾（官网原文）
- **行星偏置区随官网三团重排**：starsky.js 的 60% 星云偏置不再指旧单团
  左下（x2-52/y52-96），改按官网蓝团（左上 2-42×2-45）/金团（中上
  55-88×8-48）/青团（右下 60-97×42-85）三区轮分；钉桩锁旧坐标禁回潮
- **单设备行右侧留空**：`.device-list` 轨道 1fr→封顶 340px
  （`minmax(280px, 340px)`）——一行只有一个开窗器时玻璃框只占一轨，
  右侧整段透出星空，不再拉成超宽玻璃板
- **流星负相位进场**：7 颗 `--mdelay` 全改负值（-1s~-12s）——原正延迟
  使打开页面头 1~11s 一颗流星都看不到（用户实测"没看到流星"根因）；
  10~16s 全周期节奏与只准向下、1100px 行程不变
- 版本徽章链路不变：`theme-color` 随主色改深空，页脚/页头版本号仍由
  /api/version 事实回填（W-6/E 系列修复不回退）

## [1.6.26] - 2026-09-07

### Fixed（第八轮全量审计批：5 路独立只读审计 + 母节点逐条实证复核）
- **多网关发现 / 替换网关整链失效**（阻断级）：v1.6.25 mqtt_handler 拆包时
  函数体内惰性导入未随物理下沉升层（`from .discovery` → `mqtt_handler`
  包内不存在该模块，ModuleNotFoundError 被外层 except 吞成一行 error 日志）。
  改 `..discovery` + 补该分支实参断言测试（此前零覆盖——314 测试全绿与
  真栈 E2E 均拦不住，教训已录 CLAUDE.md 同族规范）
- **WS 令牌清空后握手 500**："空串=不认证"是 config_flow 明文支持的形态，
  但 aiohttp≥3.9 的 `WebSocketResponse(protocols=None)` 收到带子协议头的
  请求（微信 connectSocket 恒带）在 _handshake 抛 TypeError。改空元组，
  免认证直连态恢复（aiohttp 3.13.5 活体 A/B 复现取证）
- **半填桥凭据会打死内置 broker**：只填用户名时旧模板展开 `password `
  空值行——mosquitto 2.x 解析器对空值判错**拒载整份 conf**。现凭据双非空
  才成对输出，半填形态降级匿名桥并打警告；e2e harness 由 run.sh 现文重
  生成，新增生成物漂移防护测试
- **awaiting 条目"添加网关"后配置静默不生效**：该路径从不注册 update
  listener、v1.6.19 删掉的显式 reload 兜底前提为假 → 用户见"已保存"但
  无 handler/无实体，须重启 HA。现 setup 完成即注册，条目变更自动重载
- **未校准设备重启后被写成"全开 100%"**：r_travel=255（未校准标记）经
  钳制持久化再恢复被洗成 100。现持久化原始值，恢复仅 0-100 界内回填
  位置，界外只恢复开关态、位置保持未知（固件"255 丢弃"口径维持）
- **幽灵设备复活**：remove_device 在设备不在内存缓存时全部清理（映射/
  注册表/setpoints/回调/bind_ops）静默整体跳过。现缓存无关清理无条件
  幂等执行，缓存缺失打 warning，返回 bool
- setup 在平台转发后失败不卸载平台 → 条目进错误态而实体残留（僵尸实体）
- MQTT 尚未加载时心跳监听器永不武装（自动发现静默失效、无重试）→
  后台等待 MQTT 就绪再武装（120s 上限，订阅前后条目存活双检）
- awaiting-only 安装不启动小程序 WS 网关，与"条目存在即监听"定案口径
  不符 → awaiting setup 同样 ensure
- 含字母 SN 录错大小写呈现"在线但指令全无反应"（入站匹配不敏感、下发
  主题敏感）→ 以网关实际上报形态内存自纠 gateway_sn 并打 warning
- 选项添加网关 unique_id 撞车预检：HA 2026.x `async_update_entry` 对重复
  uid **不抛异常**（仅 error 日志），旧 except 兜底永不触发（源码实证）
- 配置流向导 MockDeviceManager 补齐 003 分支所需属性面（连接测试窗口内
  到达的 003 曾抛 AttributeError 被任务面吞没）

### 安全 / 门禁加固
- nginx ingress.conf（含明文 SUPERVISOR_TOKEN）权限收紧 600——与
  passwd/acl 600/700 同口径，堵容器内低权进程读 token 经代理打 HA API
- 桥段写入后 mosquitto.conf 收紧 600（conf 含对端凭据时不再全局可读）
- 崩溃诊断打印 mosquitto.conf 时 username/password 行脱敏（防密码入
  Supervisor 日志，与 v1.6.3 "passwd 只 cut 用户名"定案同口径）
- CI 语法门 `py_compile *.py` → `compileall` 递归（mqtt_handler 子包不再
  漏出语法门——正是 A-1 那类拆包事故的第二道闸）；Release 正文 awk 补回
  `## [版本]` 标题行；warm-mirrors 镜像仓名改由 image 字段派生（自定义
  镜像改名后预热不再静默失效）

### Added
- **共存桥总开关 `coexist_bridge_enabled`**（默认 true 零感知）：桥判据是
  "本机 :1883 有进程在听"，宿主第三方进程占口存在误桥面（out 腿外送控制
  命令/in 腿注入 discovery，桥消息不受本地 ACL 约束）；置 false 熔断：
  不建新桥、已建桥对账循环自动拆除（README 已加误桥警示）
- Web 页脚版本改 JS 回填占位：消灭遗留硬编码 v1.5.1 的错版闪现
- nginx 启动失败现场取证改用 /proc/net/tcp{,6} 扫描（base 镜像无
  netstat，旧取证行恒空输出——恰在端口被占最需要时无声）

### Changed（文档口径订正）
- CLAUDE.md：更新检查订正为"Gitee+GitHub 双源并集取版本号最大者"（与
  huijian.js v1.6.7 定案一致，旧"默认源→回退"记载与代码不符）；本地
  回归命令同步 compileall；Web UI 架构描述三文件化
- 根 README 与加载项 README 的版本硬字符串改动态引用（曾滞后 8 个版本）；
  共存 FAQ 增补误桥警示/总开关/凭据成对填写要求；run_e2e.sh 过时的
  "首阶段 continue-on-error"注释订正为 v1.6.22 起硬门禁
- config.yaml 共存凭据注释归属订正（v1.6.24 引入、随 v1.6.25 首发——
  历史版本号曾被 bump 全局 sed 漂错）

### 特性公开归并（v1.6.24 未单独发布，本版本起对外可见——详情见 [1.6.24] 段）
- **第三方共存自动桥**：官方 Mosquitto 在跑时自动搭方向分离桥
  （仅 `zigbee2mqtt/#` 双向 + `homeassistant/#` 单向入），z2m 零配置共存；
  对端消失自动拆桥，120s 冷却防抖；`gateway/#` 永久禁跨桥（安全评审定案）
- 可选配置 `coexist_official_user/password`：官方加载项 7.x 强制认证时
  填其 logins 任一账号即带认证建桥；**v1.6.26 起两字段必须成对填写**
- z2m 直连账号 `huijian_z2m`（推荐路径，不装官方 Mosquitto 即可共存）
- `status.json` 诊断位 `coexist_bridge` / `official_peer_up`（无 UI 展示面）

## [1.6.25] - 2026-09-06

### Changed（纯重构批，行为零变化）
- **mqtt_handler 拆包**：1774 行单文件 → `mqtt_handler/` 包（组合类 + 5 个按消息
  生命周期内聚的 mixin：lifecycle/protocol/ctypes/commands/callbacks，全部 <600 行）。
  机械逐字拆分（1514 个非空正文行多重集比对逐字节一致）；对外 import 面、ack 方向
  契约、dedup 语义、weakref 回调设计零触碰；logger 名钉死拆分前值
- **Web UI 三文件化**：index.html 1468 → 90 行；内联样式/脚本逐字节外置为
  `www/css/huijian.css`(356) + `www/js/huijian.js`(1022)（唯一形态变化：
  CURRENT_VERSION 语句逐字移至 index.html 内联单行，全仓唯一声明面有钉桩守护）；
  外置脚本保持 body 末尾同步执行（与原内联时序严格一致）；css/js 自动继承
  `location /` 的 no-store 缓存定案（新增双份配置防回退钉桩，nginx 零改动）
- **run.sh 维持单文件的定案**：加载项规范要求唯一入口，且 30+ 测试锚/桥 e2e/
  凭据生成资产逐字锚定该文件——拆 shell 会连锁重写全部实证资产，风险收益倒挂

### Added
- `tests/e2e/z2m_direct_e2e.sh` + `gen_z2m_authenv.py`：zigbee2mqtt 直连慧尖全链路
  实证（生产认证形态 Z1-Z4 用 run.sh 原文生成区逐字产出 + 真 HA 消费 Z5），
  实测全绿；实证 mosquitto 2.x 对 ACL 拒绝 PUBLISH 静默丢弃不断连（零投递硬断言）
- `ARCHITECTURE.md`：mermaid 全拓扑 + ASCII 简版（Gitee 兜底）+ 三条连接主线
  （分发/数据/接口）+ 安全边界索引；README 挂链
- `tests/test_webui_split_v1625.py`：三文件化 8 项钉桩（字节搬运/相对路径/
  no-store 覆盖/脚本时序/onclick 可得性）

### Notes
- z2m 直连交付结论定案：只装慧尖即可——z2m 配 `mqtt://<host>:2022` +
  `huijian_z2m` 账号；Supervisor 源码证实 `mqtt:need` 无启动闸，官方
  Mosquitto 非 z2m 启动前提
- 全量回归 314 绿（含本批新增 8 项）+ 真 HA 栈 E2E 全断言（HA 实载拆包代码）
  + Z 矩阵重跑；bash -n / py_compile 全过

## [1.6.24] - 2026-09-06

### Added
- **第三方共存自动桥**：慧尖内置 broker 每 30s 探测本机 `1883`——检测到官方
  「Mosquitto broker」加载项（zigbee2mqtt 默认依赖）时自动向其建立**方向分离
  桥**（仅 `zigbee2mqtt/#` 双向 + `homeassistant/#` 单向入），z2m 用户无需改
  慧尖任何配置即可与慧尖共存；官方 broker 停止/卸载后桥自动拆除。桥状态
  变更走"计划内重启"（kill → 主自愈循环 5s 复活），120s 冷却防抖，仅在真实
  状态迁移时记冷却戳（noop 续期缺陷已修）。纯慧尖客户桥**完全不存在**，零感知
- 官方加载项 **7.x 起 go-auth 强制认证**（源码实锤），匿名桥会被拒——新增可选
  配置 `coexist_official_user/password`（慧尖配置页），填官方 logins 任一账号
  即带认证建桥（实测端到端穿透）；留空=匿名桥兼容老版官方。桥不通仅影响共存，
  慧尖自身服务无恙（实测降级边界）
- **z2m 直连账号 `huijian_z2m`**（推荐路径）：broker 启动时自动创建，ACL 仅
  `zigbee2mqtt/# + homeassistant/#`，z2m 的 `mqtt.server` 填
  `mqtt://<主机>:2022` 即可不装官方 Mosquitto 直接共存
- `status.json` 诊断位 `coexist_bridge` / `official_peer_up`（无 UI 展示面，
  与凭据诊断位同边界）

### Changed
- `ha_mqtt`（HA 集成账号）ACL 段新增 `zigbee2mqtt/#`（消费桥入向消息所必需），
  保持白名单式（评审否决 `readwrite #` 通配方案——同密码换用户名的提权链
  爆炸半径不超桥主题白名单）；LoRa 网关账号 `huijian` 权限保持最小不变

### Fixed / 安全定案（三路独立审计 + 真栈取证，1.6.24 未发布故记于本段）
- **摘除 gateway/# 跨桥双腿**：匿名/弱认证官方 broker 场景下，`in` 腿等于把
  对端信任域直连慧尖执行器——真栈实锤"匿名@1883 publish req → 桥 → 固件"
  未认证物理开窗攻击链，已封堵并加负向 e2e 钉桩（S3：注入 req/rsp 零穿透）
- mosquitto 桥块红线（两轮 crash-loop 实证教训）：禁 `topic # both`
  （2.0.22 无 origin 防环，retained 乒乓风暴实测）、禁未实证选项
  （`try_initialize`/`notification_interval` 等 unknown 变量 = broker 整进程
  拒启）；`notifications false` 为实测可用形态（防桥在官方侧残留
  `mosquitto/online` retained 痕迹）
- 巡检子 shell `set -e` 隐患清除（`x && y` 短路行尾返回 false 会静默杀死
  巡检循环——v1.6.3/1.6.4 同族教训）；`/run/bridge_last_ts` 垃圾内容净化
  （防算术展开炸循环）；初启对账 `|| true` 包裹（写失败不得杀死 broker 启动）
- `test_acl.py` 夹具与 run.sh 生成逻辑加**逐行耦合测试**（本次 v1.6.24 期间
  夹具静默漂移被审计抓获的根因整改）；mqtt_match 测试模型修正 MQTT 规范
  语义（通配符不匹配 `$` 前缀系统主题）

### 验证
- 真栈机制实证（rootless mosquitto 2.0.22 双实例 + run.sh 原文函数抽取执行，
  已固化为 `tests/e2e/bridge_coexist_e2e.sh` 永久资产）：S0-S6 状态机（无 peer
  不建桥→探测→建桥→计划内重启激活→z2m 三向语义逐条恰 1→gateway 双向零穿透
  →peer 消失拆桥→服务无恙→可逆重装）+ T1-T2 认证环境（匿名桥被拒但慧尖自身
  正常→填凭据端到端穿透）全通过
- 官方加载项行为源码实锤：home-assistant/addons `mosquitto` 7.1.0 go-auth
  模板 + init 脚本逐行核对；全量回归 306 绿（含 v1.6.24 新增 14 项：test_v1624 9 + test_acl 5）

。

## [1.6.23] - 2026-09-05

### Added
- 集成「配置 → 选项」新增**「以窗帘身份暴露开窗器」**开关（默认关闭）：
  vivo 官方 ha_vivohomebridge 桥的 cover 枚举仅放行 `device_class == curtain`
  （其 vbridge.py 源码实证），开窗器默认的 `window` 类目会被过滤导致在
  vivo 智慧生活中选不到设备。勾选后（条目自动重载、秒级生效、双向可逆）
  cover 以窗帘身份暴露，vivo 端可正常添加并开/关/停控制。
- 默认关闭保证存量用户零影响（HA 原生语义仍为"窗"）；控制路径与
  device_class 无关（开/关/停命令构建逐字节一致，真栈实证）。
- tests/test_v1623.py 六条钉桩：默认值、双向 device_class、options 接线、
  两处 setup 读取、双语描述完整性。

### Fixed
- 测试基建：conftest fake `CoverDeviceClass` 补 CURTAIN 成员；
  round5 lifecycle fixture 如实模拟 `entry.options`（本功能首次真实消费
  该属性暴露的代理对象缺口）。

## [1.6.22] - 2026-09-05

### Removed
- **Web UI 移除「凭据状态」提示项**（用户定案）：MQTT 密码/小程序令牌
  轮换必须与 LoRa 网关固件侧同步修改，终端用户无处置能力，展示
  "仍是默认值"只会造成困惑。后端只读诊断面保留：集成
  `/api/window_controller_gateway/security` 视图与 status.json 的
  `mqtt_password_is_default` 字段继续存在（零展示面，供远程支持排查），
  UI 钉桩转负向防复活（tests/test_v1621.py）。

### Infrastructure（同批推送，不改变已发布行为）
- 真栈 E2E 驱动器定稿（tests/e2e/ha_e2e_driver.py + run_local.sh）：
  本地 WSL 真 HA Core + 真 mosquitto 六轮迭代全绿——onboarding
  IndieAuth URL client_id 契约、confirm_add 二步流、500 条上报
  ~199/s soak、ack 现场实证；CI 编排 summary 断链修复。

## [1.6.21] - 2026-09-04

### 新增（第七轮评分扣分项优化批——不动现有功能，纯增量）

- **默认凭据提示（Web UI 概览新增"凭据状态"项）**：默认 MQTT 密码与默认
  小程序 WS 令牌是公开同串（知道 SN + 内网即可连），此前无任何提醒。
  现 run.sh status.json 输出 `mqtt_password_is_default`，集成新增只读视图
  `/api/window_controller_gateway/security` 输出 `ws_token_is_default`
  （仅布尔，零明文回显；无网关条目时 null 不误导）。概览页合并判定为
  warn 提示。**只提示绝不自动改**——令牌双侧同步是既定契约，自动轮换
  等于全客户永久 401。const.py 增 DEFAULT_MQTT_PASSWORD 交叉锚，测试钉
  三处字面量同步。
- **Gitee Release CI 自动化**（gitee-release job）：消除"发版后手动补
  最新一条"人肉步骤；body 自动取 CHANGELOG 版本段、target_commitish 必
  带、同 tag 幂等跳过、非 ASCII token（BOM）前置拦截报错。需仓库
  Secrets 配置 GITEE_TOKEN（本次已配）。
- **真栈 E2E job**（tests/e2e/run_e2e.sh）：eclipse-mosquitto:2 + HA Core
  真实容器、REST onboarding、config flow 建 MQTT/慧尖 entry、真 MQTT 002
  报文驱动、断言 entry loaded + 集成 devices 视图 gateway_online/子设备 +
  WS 9001 常听 + 500 条 soak 吞吐。补"279 单测全在 mock 上"的真伪验证
  债；盲调试期 continue-on-error，连绿后升硬门禁（脚本内注明）。

### 明确不做（本批）

- `mqtt_handler.py` 物理拆分：纯重构收益仅开发体验，风险波及 279 项内部
  结构钉桩与六轮审计建立的行级熟悉度，与"不影响现有功能"约束冲突——
  记为技术债非缺陷。
- `huijian.local` A 记录冲突裁决：需真机现场（插件与固件同广播），无法
  我方实证，维持文档"待真机验证"口径。

## [1.6.20] - 2026-09-04

### 变更（镜像主源回退 ghcr.io 源站——1.6.19 升级现场实测决策）

1.6.19 把主源切到 ghcr.nju.edu.cn 后，用户升级实测卡在低百分比：nju 对
aarch64 新 tag 的 21MB 大层回源同步近乎冻结（我方两次实测 4.3KB/s →
311B/s 递减），"慢而稳"的预估不成立——**假活慢滴比明确失败更糟**；
ghcr.1ms.run 同期认证端点仍故障（"专属域名获取失败"）。源站 ghcr.io
实测 216KB/s 稳定无闪断，42MB 全量约 3-4 分钟，可接受——主源回退源站。

- 镜像站降级为**手动加速可选项**：追求首包极致（热缓存 1ms ~5MB/s）的
  用户仍可在加载项「配置 → 镜像(Image)」覆盖，README FAQ 同步改口径
  （可用性随时间波动，不作任何默认保证）。
- CI warm-mirrors 保留（对换源用户尽力预热，continue-on-error 不阻塞）。
- 本次无代码逻辑变更；`config.yaml`/`www/version.json`/`www/index.html`/
  `manifest.json` 四处版本同步 1.6.20；测试钉桩同步
  （`test_config_primary_is_ghcr_io_source_1620`）。

## [1.6.19] - 2026-09-04

### 变更（镜像主源回退 ghcr.nju.edu.cn —— v1.6.18 方案实测翻车纠偏）

v1.6.18 把主源切到 ghcr.1ms.run（热缓存 5MB/s），发版当日实测打脸：1ms 是
多边缘 LB，对**新 tag 冷缓存**的各边缘同步完成前返回 404，200/404 按边缘
闪断、分钟~小时级自愈——"发版后首装"恰是它最不可用的窗口，warm-mirrors
预热也只能打到部分边缘。nju 为同步 pull-through：慢（42MB 数分钟）但回源
确定性 200。首装体验"慢而稳"优于"快而随机失败"，主源回退 nju；1ms 降级为
手动快通道（老 tag 已缓存后求快可在加载项配置「镜像(Image)」覆盖）。
README FAQ 同步把 v1.6.18"预热消掉冷缓存概率"的过度承诺改为如实描述。

### 修复（第六轮四路并行审计：MQTT 核心 / 实体与配置 / 基础设施 / WS 契约）

**崩溃与毒报文（HIGH）**
- `mqtt_handler`：显式 `"data": null` 报文会让 001 处理在 ack 前抛
  AttributeError → 网关无限重传毒报文；dispatch 入口统一归一化
  （非 dict → `{}`+告警），一处收口保护全部 ctype 分支。
- 电压解析 `1e999` → `float("inf")` 合法解析后 `int(inf)` 抛
  OverflowError——三处电压点 + `_as_int` + WS 视图 battery 全部加
  `math.isfinite` 判式与 OverflowError 捕获；r_travel/speed/strength
  四个 int 解析点同步加捕。
- `handle_gateway_response` 增加 64KB 入站载荷上限（防畸形超大 JSON 打满
  CPU/内存的 DoS 面）。

**生命周期与状态一致性（MED）**
- `_closing` 闩锁：cleanup 让出点期间重连成功路径不再复活
  `_check_gateway_timeout` 循环（泄漏 task）。
- WS `_cmd_unbind`：sleep 让出后**重解析**条目（reload 竞态下旧 manager
  已清空 devices，remove_device 整体 no-op → 幽灵设备复活）；本地删除
  失败如实回 `ok:false`（原唯一谎报点）。
- WS `_persist_token`：写成功后回灌内存令牌（热同步覆写窗口）；一个
  enabled 条目都没命中时也回滚内存（原"空转正常结束"路径=静默不持久化，
  小程序已存新令牌/重启回退旧令牌的 401 漂移从此路漏出）。
- 配置流"忽略"按钮整体空转实锤：HA ignore_flow **另起新流**只带原流
  unique_id，旧实现读 `self.context`（新流里恒无 gateway_sn）→ 忽略永不
  生效、重启卡片复活。发现流补 `async_set_unique_id`，ignore 步按
  user_input→context 顺序取 SN 并执行 async_ignore_gateway。
- `add_gateway`（选项流）三连修：create_entry(data={}) 会清空条目全部
  options（用户配过的 WS 端口/令牌被抹）→ 原样保留；显式 reload 与
  update-listener 双路径重载 → 删显式；顺手写 unique_id（撞车按已配置
  回显）。
- `persist.py` 内层类型过滤：mapping 值非字符串（base_entity .lower()
  炸）、setpoints 值非 dict（number 实体 __init__ 炸→整平台 setup 失败）
  两类手工编辑/半损坏脏数据逐键丢弃+告警。
- cover `is_closed` 接入 sensor 同款 15 分钟时效判据（SENSOR_TIMEOUT_
  MINUTES）：网关长期失联不再永久冻结最后已知值与 sensor 矛盾显示；重启
  恢复快照获 15 分钟信任窗（与 v1.6.8 恢复设计自洽）。

**健壮性与口径（LOW）**
- 握手 `ws.prepare()` 的计数递减移入 finally：CancelledError（STOP 级联/
  runner 强拆）不再泄漏预约槽，攒满 4 个即本实例永久 503 的路径封死。
- 设备状态广播 task 登记 `_bg_tasks`、stop 时统一取消（"Task was
  destroyed" 尾噪；帧序由 aiohttp _send_lock FIFO 保证，无需额外排队）。
- `set_position/speed/strength` 非法参数从"静默按 0 下发"改为拒绝
  （0="关窗"是反向动作，误执行比失败更糟）。
- 003 绑定回执 id 匹配前 `_norm_cmd_id` 归一（JSON 浮点 id `12.0` 与登记
  键 `12` 失配丢回执）。
- cover/number/sensor 移除路径补 unique_id 优先定位（button v1.6.3 定案
  同款）：配对后秒级解绑时实体未获派 entity_id 不再悬挂注册表条目、
  重配对永久缺实体的竞态封死。
- 无 SN 安装分支查重（连点"下一步"不再造多个空条目）+ ensure 任意异常
  不再打穿"不阻塞安装"承诺；WS 端口选项拒绝本栈保留口 2022/8099/8123/
  1883（撞口 bind 失败属静默失联）；strings/zh-CN 补 invalid_ws_token、
  ws_port_reserved、required、already_configured 四条缺失文案。
- `start_pairing` duration schema 收 10-300s（服务调用与 UI 选择器同界；
  003 报文不携带时长，纯本地兜底）；`refresh_devices`/
  `check_gateway_status` 描述纠偏为如实语义（空操作/仅日志，行为未变）。
- CI warm-mirrors 四修：OCI 平台过滤 `arm64`≠`aarch64`（旧过滤永不命中
  落 ms[0] 可能取到 attestation 摘要）、MAN/BLOBS 判空防"ok=0 fail=0"
  假绿、计数按 arch 归零、1ms 改单轮+300s 一次性复查（密集短重试对
  分钟级闪断无效且反触发限流）。

**文档**
- `CLAUDE.md` 命令表线值勘误：open/close/stop 实为 "100"/"0"/"101"
  （字符串；旧表 0/1/2 是废弃固件时代记载，v1.6.17 已对照固件实证）。
- ws_gateway 文档字符串纠偏（set_token 不触发 reload，热同步语义）。

## [1.6.18] - 2026-09-03

### 修复（Web UI 侧边栏启动失败：nginx 抢绑宿主 80——v1.6.4 半截工程实锤）

现场日志（v1.6.17，2026-09-02）：`bind() to 0.0.0.0:80 failed (98: Address
in use)` ×8 + `still could not bind()` → nginx master 直接退出，8099 连坐，
侧边栏全挂；mosquitto/mDNS/集成一切正常。根因：alpine nginx 包自带
`/etc/nginx/http.d/default.conf`（`listen 80 default_server; listen [::]:80;`），
被重写版 nginx.conf 的 `include /etc/nginx/http.d/*.conf` 拉入，而
host_network: true 使这个默认站一直绑的是**宿主** 80——宿主 80 空闲时表现为
"插件白占 80"（NAS 部署副作用），被占时（DSM 反代等常态）bind 失败打死整个
nginx。v1.6.4 的 Dockerfile 注释"移除默认 server 块"只删了 nginx.conf 内嵌
默认块，从未删过 http.d 文件，属静默失效面（此前所有版本都带病）。

- `Dockerfile`：构建期 `RUN rm -f /etc/nginx/http.d/default.conf` + 定案注释。
- `run.sh`：启动期兜底清扫 http.d 中一切 `listen 80/[::]:80` 杂散 conf
  （防基础镜像或 apk 升级带回）；nginx 失败改"5 秒重试一次"（宿主服务重启
  竞态窗口）后再打印 `netstat` 占用取证，不再只留 syntax-ok 假象。
- 回归钉桩 `tests/test_ingress_port80.py`：Dockerfile rm 行 / nginx.conf 重写
  段零 listen / 清扫先于 nginx 启动 / 模板与 heredoc 仅监听 8099 / 失败路径
  带重试与取证，共 7 项断言防复发。
- README 新增「侧边栏打不开/bind 80」FAQ。

### 改进（安装提速 v2：镜像主源切国内加速 + CI 镜像站自动预热）

用户实报"用 Gitee 仓库在 HA 里装插件仍特别慢"。拨测定案：慢不在 Gitee——
商店元数据 git 包 <1MB 秒级；真正大头是 Supervisor 拉 **42MB 运行镜像**走
容器 Registry，而 **Gitee 无镜像仓库服务**，此步与商店源地址无关（v1.6.17
及之前 image 指 ghcr.io 境外直连，家宽实测 ~84KB/s≈8 分钟且常超时）。

- **家宽同链路实测对比**：ghcr.io ~84KB/s；v1.6.16 主源 ghcr.nju.edu.cn
  0.02~0.1MB/s 波动大、新 tag 冷缓存回源偶发 404；**ghcr.1ms.run（毫秒
  镜像）4.6~5.2MB/s，全镜像≈10s**，匿名 token 流程，双架构/版本/latest/
  历史 tag 全链路验证 200；同 IP 短时间 ~150MB 测试级流量会触发其数十
  分钟 404 惩罚窗（疑单 IP 限流）后自愈，正常单次安装 42MB 不触发——
  主源仍定 1ms.run，nju 列第一备源，README FAQ 给出换源步骤。
- `config.yaml`：`image:` 主源切 `ghcr.1ms.run/fangwenyi-dev/{arch}-
  huijian-mqtt-broker`；注释定案 Supervisor **image 单 URL、无原生多源
  故障转移**，手动换源三级（1ms.run → nju.edu.cn → ghcr.io）与商店
  "检查更新"刷新缓存前置步骤全部写入注释与 README FAQ。
- **CI 新增 `warm-mirrors` 作业**（needs manifest、continue-on-error 不
  阻塞发布）：每次发版自动把新版本双架构全部 blobs 经 1ms.run 与 nju
  完整拉一遍预热边缘缓存——把"多路径"落到发布环节：客户无论走主源还是
  手动换到备用源，首装即热缓存，消掉冷缓存 404/慢回源概率。
- 加载项 README FAQ 重写：讲清"Gitee 商店源 ≠ 镜像下载源"的两段式下载，
  给出检查更新→手动换源→等仓库切换主源三步自助恢复路径。
- 版本同步 bump：config.yaml / version.json / index.html / manifest.json
  → 1.6.18（镜像本体不变，Supervisor 按新版本号才会重新拉取换源后的镜像）。

## [1.6.17] - 2026-09-03

### 修复（小程序 ↔ 插件联审：四路独立审计 × 一手复核定案的 WS 联动缺陷批）

流程：按 dsh-review-loop 四路并行只读审计（消息契约层/业务语义联动层/
发现与网络配置层/握手会话层），所有 HIGH/MED 结论均由父代理对照固件
`app_ws_gateway.c`/`app_protocol_bridge.cpp` 一手复核后才动手。
**结论：协议骨架（cmd/type 键、令牌子协议握手、错误文案、-1 约定、
帧限/槽位/心跳）三方逐字对齐**，真实缺口集中在视图层与解绑闭环。

#### 插件侧（本仓库）

- **WS 解绑幽灵设备（HIGH）**：`_cmd_unbind` 此前只发 003 bind=0 即
  ack ok——本地删除在插件里由「设备→删除」按钮流程负责，003 解绑
  确认分支明确注释"本地删除已由删除按钮流程完成"，而 **WS 通道不
  经过按钮**：小程序解绑后设备永远留在缓存/注册表/映射，下次
  get_devices 原样复活。修复：镜像按钮流程（发布 003 → 等
  GATEWAY_READY_DELAY → `remove_device` 本地删除并登记手动删除
  列表）；发布失败如实 ack `send failed`，不谎报 ok
- **设备视图无入界校验（HIGH）**：`device_ws_view` 把 r_travel=255
  （固件"未校准/离线"标记）原样显示为 255% 且推导 state=1（"已开"），
  battery 垃圾值（voltage=0 → 0、过期缓存 → 240）照发。修复：与固件
  同口径——position 仅接受 0..100 否则 -1、state 从钳制后值推导、
  电池 raw 仅接受 [80,140]（固件 BATTERY_RAW_MIN/MAX，12V 锂电
  9.5-12.6V 放宽 8-14V）否则 -1；HA 侧"未校准"sensor 语义不动
- **control 脏值透传（MED）**：`value:""`（空串）与 bool（str() 出
  "True"）此前放行发布 004；按固件语义拒为 missing fields；数字 0
  仍合法（falsy 误杀回归护栏测试钉住）
- **control 广播分歧（MED）**：映射缺失广播分支跳过 connected=False
  网关——connected 是"1800s 无上报"业务口径，与 MQTT 发布成败无关；
  固件 P2 定式无条件向全部网关发布。已对齐
- **gateway_list online 比固件乐观（MED）**：固件 900s 无上报即显示
  离线（GATEWAY_OFFLINE_TIMEOUT_SEC），插件 connected 位 1800s 才灭。
  WS 视图层新增 `WS_GATEWAY_ONLINE_STALE_SECONDS=900` 双条件判定
  （connected ∧ 900s 内有真实上报），HA 内部超时不动
- **手动配对即时推送缺失（MED）**：003 绑定确认走 add_device 直达，
  不经 update_device_status 的 device_update 推送漏斗——新设备要等
  下次上报才出现在小程序。绑定分支补一次监听器通知（全 -1 视图，
  与固件 pair 后推送语义等价）
- **set_token 持久化失败漂移（LOW）**：固件 NVS 写失败回滚运行时
  令牌；插件此前只告警——会形成"小程序已存新令牌、HA 重启回退旧
  令牌"的永久 401。补同口径回滚
- **槽位检查非原子（LOW）**：`len(_clients)>=4` 与 prepare 后入册之间
  有 await 挂起点，并发握手可瞬时超 4；补在途握手预约计数
- **重连风暴日志刷屏（LOW）**：aiohttp AppRunner access_log 默认每
  连接一条 INFO，改 access_log=None（本模块已有中文连接/断开日志）
- **文档**：options 端口文案加"改端口=直连失联"警示（微信 mDNS 不透传
  TXT，小程序恒拨 9001）；加载项 README FAQ 补四条——连上但列表为空
  （半开口径）、端口耦合、与固件共存实例区分、huijian.local A 记录
  冲突提示

#### 小程序侧（E:\AI\ha-yy\weichat-huijian-hz，同批修复）

- **发现服务名恒 undefined**：读 `res.name`，当前微信 API 字段是
  `serviceName`（真机日志"发现服务: undefined"根因）；补读正确字段
  + 剥 mDNS 后缀 + 按 IP+实例名去重（固件/插件共存时第二台不再被藏）
- **配对失败无感知**：`pair_ack ok:false` 此前只 console，页面照旧
  轮询 60 秒黑洞才报"未发现新设备"；新增 `EVENT_PAIR_ACK` 事件并由
  网关页消费——被拒立即退出配对态并 toast 原因；`type:"error"` 同样
  透传 UI
- **重连阶梯被无限续命**：真机日志"第1→4次→回到第1次"根因实锤——
  页面 onShow/前台恢复走 `connect()` 默认手动语义清零计数且不清在途
  定时器；`connect()` 入口统一清 `_reconnectTimer`，三处自动语义调用
  点（index/app.js/broker-gateways.checkConnection）改传
  `connect(false)`，仅「重连」按钮保留手动语义
- **改令牌泄漏僵尸连接**：broker-setup 改令牌/换网关流程调
  `_cleanup()` 只丢引用不 close——服务端旧连接占槽最长 300s（满 4 槽
  即 503 拒新），且继续吃 device_update；`_cleanup` 补幂等 close
- **fail 回调断自动链**：`wx.connectSocket` 的 fail 在部分平台是唯一
  失败信号，此前不调 `_scheduleReconnect`——自动重连静默死亡；补上

### 测试

- 新增 9 项回归：position 255/越界/字符串形态、battery 固件域内外、
  control 空串/bool 拒绝与 0 值护栏、广播含离线网关、gateway_list
  900s 新鲜度、unbind 本地闭环与发布失败如实 ack、set_token 回滚、
  配对通知记账；全量 **231 通过**，py_compile/JSON 校验绿

## [1.6.16] - 2026-09-02

### 修复（小程序局域网直连 9001 永不监听——默认开定案）

**实证**（2026-09-02 小程序日志 + 我方端口探测）：mDNS 已发现
`_mqtt._tcp → 192.168.1.91:2022`（探活 2022 OPEN），但
`ws://192.168.1.91:9001/ws` 恒 `Connection refused(111)`。根因非小程序/
非网络：v1.6.15 集成侧 `DEFAULT_WS_GATEWAY_ENABLED=False`——WS 服务器
根本不启动，9001 无监听；而固件（matter-broker main.cpp:231/735）是
**配网完成即 `app_ws_gateway_start` 常听、无任何用户开关**。小程序按
"固件同款设备"预期直撞 9001，插件的显式 opt-in 设计打破了该预期，
客户更无从知晓存在隐藏开关。

- `DEFAULT_WS_GATEWAY_ENABLED` False → **True**：任一网关 entry 存在即默认
  监听（options 仍可显式关闭/改端口/令牌）；安全门禁不变——握手子协议
  令牌校验（默认令牌=小程序内置同值）401 拒连、认证成功才占槽（≤4）、
  空闲 300s 断开、帧长上限，与固件同构
- config_flow / ws_gateway 三处 docstring 同步定案措辞；测试改造：
  `test_none_only_when_explicitly_disabled`（仅显式 False 不启动）+
  `test_empty_options_starts_with_defaults`（老 entry 空 options 也默认
  拉起——正是本次事故形状），全量 224 通过
- **升级生效条件**：HA 重启（集成代码随加载项落盘、HA 启动时加载；
  1.6.15 在线实例默认关定死在代码里，勾选 options 可先行启用）

### 修复（cover 原生卡片开/停/关三键任何状态恒可点——用户定案）

**根因实锤**（home-assistant/frontend `src/data/cover.ts`）：
`canOpen = assumed_state || (!isFullyOpen && !isOpening)`、canClose 同理、
`canStop` 仅排除 unavailable；`isFullyOpen/Closed` 无 `current_position`
属性时回退 `state === 'open'/'closed'`。v1.0.1 把 `current_cover_position`
写死 None（注释本意即"保证所有按钮始终可用"）只堵住了位置分支——v1.6.8
恢复真实 state 后，同方向按钮被前端按 state 禁用，即用户所见
"开态灰开键/关态灰关健"。

- `cover._attr_assumed_state = True`：官方合法开关，短路 canOpen/canClose
  两式 → 三键任何状态恒可下发；HA 状态机（state 由 is_closed 计算）不受该
  属性影响，历史曲线/自动化/LLM 语义全保留；位置仍只走 extra_state_attributes。
  语义诚实性成立：协议规定网关只能被动上报（002/005），HA 无法回查实际窗位
- 新增 TestAlwaysControllableButtons 5 项钉桩（assumed 恒真 / available 不回退 /
  is_closed 保真 / current_cover_position 恒 None / STOP feature 声明），全量 223 通过
- 残余置灰仅剩条目重载/设置重试窗口（HA core 对未加载条目的统一行为，
  秒级~分钟级自愈，集成侧不可覆盖）

### 优化（升级引导补「商店断联」排障指引）

**背景**：客户点击 Web UI「去加载项页面更新」跳转
`/hassio/addon/huijian_mqtt_broker` 后报
`Error fetching addon info: App huijian_mqtt_broker does not exist in the store`。
经 Supervisor 源码定案（`supervisor/exceptions.py: StoreAppNotFoundError`、
`supervisor/apps/app.py: app_store = store.get(slug)`）：新版 Supervisor 的 App
架构中，已安装加载项的详情页与「更新」接口均依赖商店条目；若安装后仓库被删除
或商店刷新失败（国内访问 GitHub 不通常见），即出现该报错——插件本身运行正常，
属商店侧断联，非本插件 bug。

- `www/index.html` 升级卡提示：补全恢复路径文案（重新添加仓库 URL 并更新商店）
- `doUpgrade()` confirm 文案同步补一行排障提示；注释块记录 App 架构的 store 依赖定案
- 协议 ack 方向五条规则钉桩：新增 test_protocol_ack_contract.py 17 项
  （003/004/006/007 网关回复零再下发；001/002/005 恰好一次响应）
- 版本号四点同步 1.6.15 → 1.6.16（config.yaml / version.json / index.html / manifest.json）

### 发布后修正（仓库元数据，不占版本号，2026-09-02）

- **安装提速**：`config.yaml` 的 `image` 由 `ghcr.io` 改指南京大学 ghcr 透传
  镜像 `ghcr.nju.edu.cn/fangwenyi-dev/{arch}-huijian-mqtt-broker`——用户实报
  "仓库用的 Gitee 但安装显示 GitHub 且很慢"：仓库元数据走 Gitee 没问题，
  慢在镜像固定拉 ghcr.io（国内龟速/超时）。nju 全链路实测匿名 200
  （index/config/blob；amd64+aarch64、latest 与历史 tag 全通；镜像压缩
  ≈42MB）；镜像本体仍单份发布 CI 推 ghcr.io，零 CI 改动；nju 偶发不可用
  可在加载项配置页用镜像覆盖字段改回。README 同步新增两条 FAQ（安装慢、
  9001 直连排障）。**生效方式**：Supervisor 从仓库直接读取，更新商店后
  新安装即提速，无需新版本发布
- **README 收敛**：addon/根 README 仅保留 1.6.16 与「工作原理/安装启动/
  重启 HA/常见问题」板块（删配置 LoRa 网关/插件配置/设备类型/逐版本清单，
  版本历史单一真源=CHANGELOG）；根 README 修正"直连默认关闭"过期表述

## [1.6.15] - 2026-09-02

### 新增（小程序局域网直连 · 路线 A：集成内置 WS 网关，与 Matter 固件 1:1 协议对等）

**背景**：慧尖小程序经 mDNS `_mqtt._tcp`（本加载项已广播）能"看到"HA，但
随后固定拨 `ws://<IP>:9001/ws`（小程序零名称过滤、丢弃广播端口），插件
此前 9001 无监听 → "可见不可连"。matter-broker 固件的
`app_ws_gateway.c` 证明该端口/路径/子协议令牌握手即小程序的 LAN 控制协议，
本版本在集成内实现同契约服务器，小程序与固件网关在用户侧同构共存。

- **`ws_gateway.py`（新）**：aiohttp 单例服务器（`0.0.0.0:<port>/ws`），
  命令 `get_gateways/get_devices/control/pair/unbind/ping/set_token`、
  推送 `device_update` 七键；握手令牌按 `Sec-WebSocket-Protocol` 逗号/空格
  拆分精确匹配（401 拒连）、认证成功才占槽（≤4）、入站帧 >1024B 报
  `command too long` 断开、空闲 300s 断连——全部与固件定式逐字对齐；
  消息文案（`missing cmd`/`unknown command: x`/`send failed`/set_token
  五级校验链含 B16 bootstrap）逐字符钉死。`set_token` 成功即热改运行时
  令牌并异步持久化到主控 entry options
- **`mqtt_handler.send_ws_raw_004`（新）**：control 透传出口，$SH 004
  线格式与 send_command 同 id 计数器；发布失败回 `False` → `control_ack
  ok:false "send failed"`（不假成功，v1.6.9 家族契约）并同步网关离线态
- **`device_manager`**：新增 `add/remove_status_listener`（update_device_status
  成功漏斗单点挂钩，002/005 上报即推 `device_update`；cleanup 兜底清空）
- **`config_flow` OptionsFlow + strings/zh-CN**：新增 3 个选项
  `ws_gateway_enabled`（**默认关**——9001 是新增局域网监听面）、
  `ws_gateway_port`（默认 9001，1024-65535）、`ws_gateway_token`
  （默认=固件出厂令牌；留空=不认证；表单侧预校验固件字符集/长度，
  防 B4 式含空格令牌自锁）
- **生命周期**：entry setup/unload/remove 三点 `async_ensure_ws_gateway`
  幂等聚合；端口/令牌变更热切换；HA STOP 闩锁防关机过程重拉；启动失败
  （端口占用）只记日志不影响集成其余功能
- 测试：新增 33 项（握手拆分/令牌校验链/device_ws_view -1 约定/dispatch
  全命令实参/004 线格式 topic+QoS+payload 逐键/发布失败离线联动/真
  aiohttp 握手 E2E 含 101 子协议回显+device_update 推送实收）；
  全套 201 passed；`bash -n run.sh` / py_compile 全绿

## [1.6.14] - 2026-09-01

### 修复（真机 E2E 揪出的第二生产根因：HA 2026.8 MQTT 表单 schema 演化击穿自适应）

**问题**：本地真栈（WSL HA 2026.8.3 进程内 + mosquitto 2.0.21 真 broker +
假网关 MQTT 上报）A/B 实锤：客户 HA≥2026.8 首添网关时，MQTT 引导的
`async_configure` 提交**在 broker 完全健康的情况下也必然失败**——2026.8
正式版将 broker 表单的 `other_settings` 改为 `vol.Required`，缺失时由
data_entry_flow 抛 **InvalidData**，而 v1.6.5 引入的"补字段重试"自适应
只捕获 **KeyError**（那是 2026.8.0-dev 校验器直接索引的形态）。落空后
进入兜底 except → ConfigEntryNotReady：v1.6.12 误报 `mqtt_not_available`、
v1.6.13 报 `broker_not_ready`——文案更准但自动建条目依然不可用。
该分支自引入以来零测试覆盖，历轮代码审计（纯静态）均未能发现。

- `ensure_mqtt_connection` 提交重试同时捕获 `(KeyError, InvalidData)`，
  两代 2026.8 形态共用同一"补 other_settings 重试"；重试仍失败则照旧
  收敛为 CENR + abort + 保留标记（不死循环、不穿透异常）
- 旧 HA（无 other_settings 直通）不受影响：单次提交不多试
- 测试：新增 G 组 4 项（InvalidData 重试契约 / KeyError 形态不回退 /
  双失败止损 CENR / 旧版直通单次），变异验证（还原 KeyError-only）精确 2 红
- 全套 168 passed；真机矩阵复跑：健康 broker 首添一次成功（含 cover
  实体生成、网关在线判定、标记消费）

## [1.6.13] - 2026-09-01

### 修复（客户现场 mqtt_not_available 误报根治 · dsh-review-loop 双审计 + 变异测试验证）

**问题**：客户安装加载项后在集成中添加网关报 "MQTT 集成未启用"，但实际根因是
config flow 在 `ensure_mqtt_connection` 之后**立即同步**检查 `hass.data["mqtt"]`
——MQTT 条目刚创建/重载时集成 setup 尚未异步完成，正常启动时序被误判为失败；
且两种完全不同的故障（从未配置 MQTT / 内置 broker 未就绪）复用同一误导文案，
用户无从下手。

**config_flow.py（就绪门禁）**
- 新增 `_async_gate_mqtt_ready` 统一门禁：未就绪时先宽限轮询（10s）再判定；
  错误码按失败形态分流——无条目且无引导标记 → `mqtt_not_available`（快速失败，
  不空等）；有条目或有标记但未就绪 → 新增 `broker_not_ready`（如实提示内置
  broker 未起/凭据被拒，文案中性兼容 HACS 自建 broker 用户）
- `ensure_mqtt_connection` 抛 `ConfigEntryNotReady` 不再直接定错，转交门禁统一
  分流（旧行为=本 bug 本体，E 组端到端接线测试钉死防回归）

**mqtt_bootstrap.py（引导返回值契约）**
- `ensure_mqtt_connection` 返回 `True/False/None`：False=已消耗满一轮 30s 连接
  等待仍未就绪，调用方不得再叠加宽限（消除 30s+10s 串行白等）；None=本次未做
  连接等待，就绪判定交由调用方
- CREATE_ENTRY 超时**保留**标记（条目未落地时下次可重建，独立价值）；更新/
  降级路径**无条件删除**标记（条目数据已落地即引导职责完成，连接由 MQTT 集成
  自身重试负责；此前"保留"语义经审计证实无消费出口且可在 Supervisor 覆盖场景
  形成周期性 reload 环）；修复 hassio 降级分支注释与行为相反的历史漂移
- 新增 `has_bootstrap_marker` 探针（异常安全回退 False，不打断门禁）

**utils.py**
- 新增 `async_wait_mqtt_loaded(hass, timeout)`：轮询与 `is_mqtt_loaded` 同一
  谓词（下游 async_subscribe 的真实前置条件），先查后睡无忙等

**测试**（161 项全绿，新增 22 项）
- `test_mqtt_gate.py`：宽限三态 / 门禁分流 / 短路守护 / 标记生命周期（含
  HAOS MENU 导航形态、hassio 降级分支）/ async_step_user 调用点接线（E 组）
- 变异测试验证：还原旧硬编码 → E 组 4 红；更新分支改回保留标记 → 契约红；
  CREATE 分支改回无条件删 → D1 红——排除假绿

## [1.6.12] - 2026-08-30

### 修复（第五轮全量审计 16 项：4 路并行审计 + 父代理逐条实证后全部落地）

**MQTT 核心（mqtt_handler.py）**
- **005 毒消息 ack 必达（#1）**：attrs 非列表/元素 null → TypeError/AttributeError
  炸穿处理协程、尾部 `_send_ack` 不可达 → 网关对同一报文无限重传。005 处理体
  重构为 inner+wrapper：异常记录并吞掉，ack 进 finally（畸形帧仍恰 ack 一次）
- **002 属性转换吞噬（#2）**：`float(battery)`/`int(r_travel)` 对 null 抛 TypeError
  被 `except ValueError` 漏掉 → 整个属性更新协程中断且被 gather 静默。补
  (ValueError, TypeError)；`_batch_process_tasks` 的 gather 结果逐个查异常落日志
- **陈旧 bind 记账死账（#3）**：start_pairing 记录为 ("bind", None)，
  `_clear_bind_ops_for_device` 按 device_sn 匹配永不命中、超时也不清理 →
  旧会话迟到确认仍命中旧记账，bind_op=="bind" 门控下掐掉当前配对会话的定时器
  （v1.6.11 #2 的残留窗口）。新配对启动时清光全部旧 "bind" 记账（会话不变式：
  同一时刻只留最新一条）
- **auto_discovery 真实接线（#4）**：该选项在表单存在但全工程零消费，取消勾选
  静默无效。新增 `_auto_discovery_enabled()`（读 entry options，取不到默认 True
  保持历史行为），门控 002 未知设备自动添加（已有设备更新/配对路径不受影响）

**实体平台**
- **cover 状态回调缺失（#5）**：cover 是四个平台里唯一没注册
  `add_status_callback` 的——005 上报后滑块/传感器即时刷新而 cover 卡片只能等
  HA 轮询，v1.6.8「cover 驱动历史/自动化」定案的实时性半边从未兑现。启动
  循环与 on_device_added 两路径补注册，移除路径对称摘除
- **sensor 时效契约复活（#7）**：`_update_state` 每次从缓存读到值就把本地
  时效戳重置为 now，紧随其后的 SENSOR_TIMEOUT_MINUTES 判定恒为假——网关离线
  数小时电压/状态仍显示离线前值。改读设备缓存 `last_update`（真实上报墙钟），
  陈旧值如实转 unknown；永不生效的 `last_update_time` 字段删除
- **button 清理总闸（#8）**：基础按钮（open/stop/close/a/内倒两模式）的注册表
  清理整段嵌在「本会话创建过删除按钮」的 if 里——删除按钮被查重跳过时本会话
  新建的基础按钮永久滞留注册表（number/sensor v1.6.3 已修同类，button 最后
  残留）。抽模块级 `_remove_device_buttons` 无条件幂等清理

**注册表死属性簇（本轮最重，#6/#9/#10/#11 关联）**
- **`via_device` 读取簇根治**：DeviceEntry 上从未存在 `via_device` 属性
  （读取端是 `via_device_id`，值=父设备 id：新版 str/旧版 tuple），但
  device_manager 验证日志/转移短路/**`_get_gateway_devices_from_registry`**
  与 `__init__` 子设备清理全部读它且恒落 None——网关子设备清单在生产中**恒空**，
  迁移快照/实体转移/跨网关冲突通知整段静默 no-op；删除网关时子设备孤儿条目
  清理从未执行。新增 utils `get_via_device_id`/`get_device_config_entry_ids`
  双形态兼容层统一改造 5 处读取点；`config_entry_ids` 同为不存在属性名
  （正确 config_entries/旧 config_entry_id），共享保护死分支一并修复。
  附 tokenize 静态扫描测试：集成源码再现 `.via_device`/`config_entry_ids`
  读取即红（v1.6.0 "entity" 字面量同族教训的机制化防复发）
- **options 死控件（#9）**：gateway_sn 字段写入 options 后零消费（setup 只读
  entry.data），删除；真实消费三字段与翻译文案对齐
- **翻译契约（#11）**：strings/zh-CN 的 options.step 只有从不渲染的 "init"，
  补 options/add_gateway 真实步骤与 add_gateway 三个 error 键（此前 UI 裸显
  英文 key）

**持久化与配置**
- **persist .bak 救援（#10）**：主文件缺失直接 return——误删主文件后重启全量
  丢失而备份明明在；缺失与损坏同走 .bak。字段级类型校验：mapping 非 dict/
  removed 非容器此前以 len(None)/set(42) 逃逸到 setup 整挂，现丢弃+告警
- **test_acl 2.x 语义**：未知用户判定从 1.x"默认允许"翻正为 2.x"默认拒绝"
  （运行时即 2.x，测试模型与真机分叉）
- **config.yaml ssl 映射移除**：全仓对 /ssl 零引用、broker 无 TLS——与 v1.6.3
  hassio_api 移除同源的权限最小化清理

**Web UI**
- **abort reason 必须是 Error（#12）**：WHATWG fetch 以 reason 原值 reject，
  `abort('请求超时')` 字符串 reason 让全部 `e.message` toast 显示 "undefined"
  （v1.6.10 目标实际未达成）。改 `abort(new Error(...))`
- **fetchT 超时覆盖 body 读取（#13）**：`.finally(clearTimeout)` 在响应头到达
  即清 timer——代理"回头不 Body"时 resp.json() 无限悬挂，silentRefresh 防重入
  标志永不自愈。timer 延后到 json()/text() 结算；静态钉桩防回潮

**文档**
- README 端口出处纠偏（2022 由 mosquitto.conf+run.sh 定义，config.yaml 不含）；
  ingress.conf "兜底"死路径叙事更正（run.sh 必先重写它，真实角色是 heredoc
  第二拷贝）

### 测试
- 新增 `tests/test_audit_round5.py` 27 项：005 毒消息三形态 ack 恰一次、002 转换
  吞噬、bind 清账+迟到确认不掐会话、auto_discovery 门控开/关、真实 DeviceEntry
  形态（str/tuple via_device_id，刻意不带 via_device）的子设备清单命中与排除、
  静态死属性扫描、sensor 新/旧值四态、cover 注册/摘除生命周期、button 无条件
  清理、options schema 键断言、persist 救援/畸形三例、Web 契约正则、infra 断言
- `test_persist` 主文件缺失用例更新为"完全不可用"分支契约；全套 139 绿

## [1.6.11] - 2026-08-30

### 修复（第三轮外部审计 7 项：5 项落地，2 项裁决误报/维持不改）
- **迟到 003 掐掉当前配对会话（#2）**：_handle_ctype_003 成功分支无条件
  取消配对定时器并退出会话——id 无记账（_bind_ops.pop 一次性消费后）且
  设备恰好不在列表时（如刚删设备的迟到绑定确认撞上新一轮配对），会误关
  当前配对窗口。会话退出/状态恢复现限定 `bind_op == "bind"`（只允许我们
  记账发起的确认结束会话）；设备添加保留（errcode=0 即事实）
- **cleanup 遍历中列表收缩跳项（#3）**：任务 done 回调从 _background_tasks
  remove（:166），第二个循环 await 让出控制权时列表原地变异，索引迭代跳项
  → 被跳过的任务从未被 await（终态异常无人消费、"cleanup 后无任务触碰已清
  状态"保证被破）。两循环改遍历 list() 快照
- **publish 失败状态源分叉（#4）**：send_command 发布异常置 connected=False
  +notify 却漏了 update_gateway_status("offline")——其余全部 connected=False
  路径（check_connection×2/重连放弃）都同步，唯此分叉。对齐补齐
  （注：审计对症状的描述"binary sensor 离线/Web 在线"不准确——
  gateway_status 无显示面消费方，实际是内部状态源一致性问题）
- **去重时间轴 monotonic 化（#5）**：time.time() 换 time.monotonic()（唯一
  喂入点，整体同时基）。注：审计"回跳致字典无限增长"不成立——键空间
  （ctype,id,sn）有限天然有界，本项按 better-practice 顺手修
- **config_flow 连接测试 mock 缺方法（#6）**：MockDeviceManager（生产文件
  内，非测试）缺 allocate_device_number——测试窗口内到达的 005 走
  _quick_add_device 必抛 AttributeError 被消息循环兜底吞（丢一帧+噪音）。
  补齐 + conftest 增补 config_flow 导入面桩（callback/ConfigFlow 基类/
  OptionsFlow）
- **cover.is_closed 浮点截断（外部审计第四轮 #2）**：防御兜底分支
  int(r_travel) 把 0.5 截成 0 → 微开误判"关"，违反">0=打开"定案语义；
  改 float 直比（协议规定整数 0-100，本分支正常不触发，纯防御加固），
  非数值仍落 None

### 裁决为误报（不设修复）
- **#1 "MQTT 订阅永久失效"（🔴 指控最重者）**：不成立。订阅走 HA MQTT 集成
  的 async_subscribe（:403），HA core 自持 broker 重连并在恢复后自动重订阅
  全部注册项；_unsub_rsp 在放弃路径从不注销；_reconnect_mqtt 重试的只是
  冗余的再注册（本地操作，几乎首试即成）；send_command 断连时还会
  _schedule_reconnect 再拉保险。"5 次后永久失聪"的前提机制错误
- **#7 005/002 添加竞争（#NN 号抖动）**：真实存在但纯 cosmetic——
  add_device 按 sn 幂等，无数据损坏；统一命名需中等重构，不值本批动

### 测试
- 新增 5 用例（test_audit_round3.py）：迟到 003 会话保持×1、我方确认
  仍退出会话×1（N1 语义护栏）、cleanup 快照红绿双验×1（旧代码实测
  消费 5/6 确定性失败）、publish 失败 offline 对齐×1、mock 契约×1、
  is_closed 浮点/负值/非数值×1；全量 112 passed；py_compile /
  node --check（JS 无逻辑改动）全绿


## [1.6.10] - 2026-08-30

### 修复（v1.6.9 回归检查 + 第二轮外部审计 12 项 + 第三轮审计 4 项确认修复）
- **绑定成功后状态卡「配对中」（N1，P2）**：_handle_ctype_003 成功分支清了
  pairing_active、取消了超时定时器（唯一自动恢复者），却没复位
  device_manager 的 gateway_status（start_pairing 置的 "pairing"）——
  「配对中」要等下次 002 心跳才消失。成功路径就地 `update_gateway_status
  ("online")`
- **Web 全部请求零超时（N6，P2 健壮性）**：新增 `fetchT()` 统一
  AbortController 封装（HA API 12s / 本地 nginx 8s / github+gitee 更新源
  20s，abort 带「请求超时」reason），并给 silentRefresh 加防重入锁——
  HA Core 挂起不再无限卡住刷新周期或造成并发周期交错写 DOM
- **transfer_device 后置失败不可见（N8）**：映射更新后的注册表重挂/实体
  重链/旧实体清理/双端 reload 四个失败点补 exc_info 堆栈 + post_failures
  计数，尾部明确告警「映射已转移但 N 个注册表步骤失败，重载条目可修复」
  （映射=事实来源，return True 语义不变）
- **number 簿记失败错误回弹（N9）**：命令已送达后 setpoints 写入/持久化
  create_task 抛错（如 hass 关闭期 RuntimeError）此前落入外层 except
  触发 _revert_to_saved，把已生效滑块回弹。簿记独立 try，仅告警不回退；
  未送达回退语义保持（新用例钉桩）
- **二次配对卡死（B2，P1）**：start_pairing/配对按钮先 cancel 旧超时定时器
  再发送，若发送失败抛错，上次成功残留的 pairing_active 再无定时器可清 →
  网关卡片永久显示「配对中」。新增幂等助手
  `WindowControllerMQTTHandler.abort_pairing_if_active()`，services 四条失败
  分支与 gateway 按钮两条失败分支全部先清理再上抛
- **transfer_device 假成功（B1，P1）**：执行块 `transfer_device` 返回 False
  仅日志 → REST 200（校验/查找路径 v1.6.9 已收口，执行块漏网；服务已注册，
  dev tools/自动化可达）。失败/异常均抛 ServiceValidationError
- **check_gateway_status 吞异常（B3）**：执行异常仅日志 → 200「已发送」。
  同族收口抛错（is_connected=False 是合法检查结果，不抛）
- **migrate_devices 契约收口（B4）**：执行失败仅发事件照常返回——服务当前
  未注册（dead code），但按契约补 raise，防止将来重新注册复活假成功
- **silentRefresh 阻断回归（B5，v1.6.9 引入）**：config_entries 瞬断时
  catch 直接 return → 跳过本轮全部设备状态刷新（窗口状态少刷 30s 周期）。
  改为 needRebuild 标志：检测失败仅跳过增删判定，设备更新照常
- **ingress.conf 兜底模板漂移（B9）**：v1.6.9 只给 run.sh 动态生成版的
  `/api/ha/` 加 no-store，Dockerfile COPY 的兜底模板漏同步（调试模式下
  仍供出可缓存响应）。两处已一致

### 澄清（非 bug，语义边界写明）
- **send_command True 的边界（B6）**：True 仅表示 QoS1 publish 被 broker
  接收，不代表设备执行——执行实据靠 005 上报。docstring 已明确，failfast
  契约的适用范围据此界定

### 已知取舍（记录在案，本批不改）
- 004 响应 errcode≠0 无命令级 ack 回传 UI（B8）：需要请求-响应关联机制，
  属架构增强，非缺陷修复
- cover 恢复注入可能回填过期开/关状态（B7）：自愈依赖下次 005，锁不值得
- 默认凭据 huijian/huijian2022（B10）：改动会毁掉存量安装，文档已警告
- base 镜像 :latest 浮动标签（B11）：hassio base 跟踪 Alpine 源，pin 旧版
  反有 apk 仓库轮换导致构建失败的风险（本项目已实证镜像轮换之痛）
- /api/ha/ 对 172.30.32.0/24 内其他加载项可达（B12）：token 仅
  homeassistant_api 范围，run.sh 已留 TODO 待实机取证收紧

### 测试
- 新增 13 用例（stuck-recovery×2、abort 助手单元×3、transfer×3、
  check_status×2、N1 绑定状态恢复×1、N9 簿记不回退×2），全量 106 passed；
  JS node --check、bash -n、py_compile 全绿；无头 Edge 渲染断言 8/8


## [1.6.9] - 2026-08-29

### 修复（外部深度审计终审确认的 5 项真实 bug + 1 项横向扫描同族）
- **start_pairing 假成功（高）**：services.py 内 try 块的 `raise
  ServiceValidationError`（命令未送达）被末尾 `except Exception` 吞掉 →
  REST 200、Web 弹「配对模式已启动」但 pairing_active/超时定时器从未设置。
  补 `except ServiceValidationError: raise`（rename 已有同款保护，v1.6.4
  根治漏掉此路径）；连接/超时/配置类异常分支同族收口如实抛错
- **set_position 假成功（中）**：原 fire-and-forget `async_create_task` +
  内部把全部异常吞成日志 → broker 掉线时永远 200「已提交」。改为同步
  await send_command，未送达（返回 False，QoS1 发布语义无 ack 误判）或
  异常均抛 ServiceValidationError
- **控制实体假成功同族 5 处（中）**：cover 开/关/停、button 按压（普通+
  风锁）、gateway 配对按钮——send_command 返回值不检查且异常仅日志，
  HA 原生卡片/Web 控制全部假成功。统一改为查返回值+抛 HomeAssistantError
  （number 滑块已有回退可见反馈，不改）
- **Web 网关级增删检测（中低）**：silentRefresh 此前仅在卡片为 0 时重建，
  HA 中新增第二台网关页面永不出现（与 v1.6.6 设备级自动增删不对称）。
  每轮比对 config_entries 集合，不一致才整建；API 失败退回旧行为
- **run.sh `/api/ha/` 补 no-store（低）**：此前唯一无缓存头的代理块，
  HA Core REST 的 JSON 响应可被浏览器启发式缓存 → UI 显示陈旧
- **CI 包可见性检查升级为阻断（中）**：ci.yaml 的匿名拉取探测失败时仅
  ::warning，绿 CI 掩盖「用户装不上」。改为 15 秒复测一次仍失败 ⇒
  ::error + exit 1 阻断发布

### 加固（审计低危项）
- `base_entity` 两个生命周期方法补 `await super()` 链（断链曾静默跳过
  mixin 钩子；当前 HA 走 async_internal_* 功能无损，防御性修复）；
  conftest 假实体同步补空实现镜像真实契约
- Web `silentUpdateCheck` 三重限流：localStorage 跨标签页去重（5 分钟）+
  document.hidden 跳过 + 间隔 10→30 分钟（v1.6.7 双源合并后多标签轮询
  有触发 GitHub 匿名限流 60/h/IP 的现实风险）
- `cover.is_closed` 位置兜底分支注释纠偏（002/005 现行链路 r_travel 总与
  推导 status 同写，该分支为防御性冗余）
- README 收敛：两份 README 重复且过期的更新日志改为最新摘要 + 指向
  CHANGELOG.md 单一真源；徽章/版本表同步

### 已知取舍（评估后不改）
- restore 注入与实时上报的微竞态（一个事件循环 tick 窗口，下次上报自愈）
- restore 回填显示重启前状态，期间手拨窗户会短暂失真（协议不能主动查询，
  HA RestoreEntity 通用行为）

### 测试
- 新增 tests/test_command_failfast.py：16 例钉死「未送达/异常 ⇒ 必须抛错、
  成功 ⇒ 不抛且副作用正确」契约（覆盖上述全部 7 处修复路径）

## [1.6.8] - 2026-08-29

### 修复（子设备状态恒显 unknown——v1.0.1 起的历史设计缺陷）
- `cover.is_closed` 由写死 `return None` 改为按网关上报缓存推导真实开/闭：
  HA 标准 state 计算在 is_closed=None 时输出 None→`unknown`（官方源码实证），
  导致 cover.state 恒为 unknown——Web 状态行、历史曲线、自动化条件、
  LLM 语义控制全部只能拿到 unknown；真实状态此前仅存在于
  attributes.device_status。现 state 输出真实 open/closed（位置属性仍
  不出 state，保留原生卡片按钮不因位置 0/100 置灰的原始意图）
- Cover 实体接入 `RestoreEntity`：协议规定网关只主动推送（002/005），
  HA 无法查询、device_manager 缓存不跨重启——修复前每次 HA 重启后
  所有子设备状态直到下次上报都是 unknown。现启动即恢复上次开/关与
  位置（仅当无实时数据时回填，真实上报到达优先覆盖）
- Web 状态行三级兜底：cover.state → attributes.device_status →
  「待上报/离线」，不再向用户暴露英文 unknown/unavailable 裸值
- 状态与位置同步（用户定案：r_travel 0=关、>0=开）：当 status 仍为
  unknown/connected 但已有 position 上报时，is_closed 与 Web 状态行均按
  位置推导，消除「状态: 待上报 + 位置: 65%」的自相矛盾显示
- 新增 tests/test_cover_state.py：is_closed 推导×5 + 重启回填×5
  （静默失效面断言，参考 CLAUDE.md v1.6.0 教训）

## [1.6.7] - 2026-08-29

### 修复（更新检查版本源）
- `fetchLatestRelease` 由「Gitee 有数据就只用 Gitee」改为 **Gitee+GitHub
  双源并集取最大版本号**：gitee Release 不由 CI 自动创建、最大版本会陈旧
  （2026-08-29 实测停在 v1.3.0），旧逻辑会把真新版误判「已是最新版本」、
  升级徽章永不点亮；现单源失败/陈旧均不再影响判定（GitHub 条目先入列，
  同版本优先其 html_url 详情链接）

### 优化（Web 界面视觉全面翻新，纯展示层零逻辑改动）
- 设计系统升级：新色板与分层阴影、统一 16px 圆角、背景柔光径向渐变、
  数字等宽（tabular-nums）显示；字体栈补 PingFang/雅黑中文回退
- 头部：品牌区（📡 磨砂图标块 + 标题 + 版本药丸徽章）、双径向高光渐变；
  favicon/theme-color；刷新按钮加 ⟳ 图标
- 服务状态：改**单行横排**（● 状态灯 + 名称 + 右对齐状态值）——解决堆叠
  瓦片卡片过高问题，整卡高度约减半；灯保留呼吸涟漪动画、按 ok/err/warn
  整卡染色（CSS :has，不动 setStatusDot 的 DOM 契约）；手机端自动改纵向
  单列堆叠保证可读；该卡内边距收紧（.card-dense）
- 网关卡片：左侧渐变强调条 + 📡 头像；SN 改芯片样式；徽章前置状态圆点；
  「状态/改名」按钮内联色改 .btn-slate 类，「配对/内倒/内倒模式」等
  全部改渐变按钮类
- 子设备卡片：入场 fadeUp 动画（v1.6.6 新增设备自动出现时正好淡入）、
  hover 抬升；状态行改独立小面板；网格改 auto-fill minmax(280px) 自适应
- 滑块：完全自定义外观（轨道圆角、白底彩环滑块、按压缩放），位置/速度/
  力度分别用靛/紫/青强调色；数值改芯片式回显
- 更新卡片与 Toast：update-ok/update-err 类化（去内联色）；Toast 加
  ✅/⚠️/⏳ 图标、上滑动画、毛玻璃
- 新增深色模式：@media (prefers-color-scheme: dark) 全变量覆盖，
  跟随浏览器/HA 主题自动切换；内联硬编码色全部清为类，深色无漏网
- 无障碍与偏好：focus-visible 焦点环、prefers-reduced-motion 降级、
  ::selection 配色
- 约束保持：所有 JS DOM 契约不变（id/class 选择器、slider.nextElementSibling、
  badge className 覆写、.device-item 增删检测、setStatusDot 结构）

## [1.6.6] - 2026-08-29

### 修复（Web 界面设备列表不自动更新）
- **新配对子设备最迟 30 秒自动出现**：无感刷新（updateGatewayDevices）此前
  只更新页面上已存在的 `dev-*` 元素状态，新子设备没有对应 DOM、循环直接
  跳过——集成里早已注册的新设备在 Web 界面上永远等不到，只能手动点
  「刷新」或重载页面。现在每轮比对服务端设备 id 集合与已渲染集合，
  有新增/移除即升级为 loadGatewayDevices 完整重建（平时依然无闪烁）
- 同理修复反向场景：设备被整体移除后，页面残留行此前会永久滞留

### 修复（Web 界面版本陈旧：插件 1.6.5、页面显示 1.6.4）
- 根因：ingress 会话 token 路径在插件更新前后不变，而 nginx 对
  `index.html`/`/api/version`/`/api/integration` 从不发送 Cache-Control，
  浏览器启发式缓存持续供应旧版页面与旧 version.json（CI/ghcr 已核实
  1.6.5 镜像内文件均为新）
- nginx（run.sh 动态生成版与 ingress.conf 兜底版同步）：静态页与本地 json
  端点全部 `Cache-Control: no-store`；GitHub/Gitee 代理端 hide 上游自带
  Cache-Control（GitHub API 默认 public max-age=60 会透传进 iframe）后
  统一 no-store，「有可用升级」发现不再被上游缓存拖慢
- 前端 fetch 双保险：haApi、/api/version、/api/status、/api/integration、
  /api/broker、更新检查请求全部显式 `cache: 'no-store'`
- 注意：本次修复要生效一次的前提是更新到 1.6.6 后硬刷新一次（Ctrl+F5）
  ——缓存里躺着的旧页面自身没有这些修复，它救不了自己

## [1.6.5] - 2026-08-29

### 新增（Web UI 升级提醒自动化）
- **「有可用升级」徽章**：检查更新此前只能手动点按钮，页面不会主动告知
  新版本——现 init 完成后自动静默检查一次、之后每 10 分钟复查（GitHub
  匿名限流 60/h/IP，此频率安全）；发现新版头部按钮变为绿色
  「⬆️ 有可用升级 vX.Y.Z」，点击展开完整升级引导卡片（手动检查同款）
- 顺带修复三处小毛病：页头静态版本徽章硬编码 "v1.5.1"（JS 失效时陈旧）
  改为 CURRENT_VERSION 先落底；"已是最新"卡片误称数据源为 Gitee（实测
  仓库 Releases 只在 GitHub，Gitee 恒空数组走回退）改称"发布源"；
  release tag 解析加 ^\d+(\.\d+)+$ 白名单，防 -beta 类后缀把
  compareVersions 拖进 NaN 误判


### 修复（安装故障热修，用户报告"点安装一直不成功"）
- **Dockerfile 分层重构 + pip 国内镜像三级回退**：Supervisor 源码安装在
  NAS 上现场跑 `apk add + pip install zeroconf`，v1.6.4 加 openssl 使
  apk/pip 同层缓存全废、pip 直连 pypi.org 在弱网下无限卡→安装永不完成。
  现拆为 apk/pip 独立两层（后续版本改动不再连带重建依赖层），pip 依次回退
  pypi 直连(30s×2 快速失败)→清华→阿里；requirements COPY 紧随 apk 层，
  代码文件层全部后置以获得最优缓存顺序
- **ghcr 预构建镜像路线打通**（定案更新）：GitHub 无 visibility 变更 API 实锤
  （REST 4 组合 + GraphQL schema 全路径取证），但 Actions GITHUB_TOKEN 首推
  包【继承仓库可见性】——历史包 private 系 8-25 本地手工推送创建时被定死。
  修复手段：gh api DELETE 删包 → CI 重建自动 public（amd64 实测匿名拉 200，
  期间踩坑：token 探测必须带 service=ghcr.io + Accept 索引头，否则假 404）。
  config.yaml 已启用 image: ghcr.io/fangwenyi-dev/{arch}-huijian-mqtt-broker，
  安装/更新从"NAS 现场编译"变为"拉预构建镜像几十秒"；CI 探测步骤同步修正
  并改为输出"删包重推"指引

## [1.6.4] - 2026-08-29

### 修复
- **Web UI 误报「MQTT Broker 已停止」（v1.6.3 引入）**：v1.6.3 的 status.json
  探活循环用 `netstat` 判 2022 端口 LISTEN，但 HA alpine base 镜像根本不含
  netstat（apk 仅 bash/bind-tools/ca-certificates/curl/jq/libstdc++/tzdata/xz），
  `2>/dev/null` 吞掉 command-not-found 后 grep 恒失败 → status.json 永远写
  stopped。v1.6.3 之前该假状态被 nginx 硬编码 "running" 掩盖，之后则反向
  恒假。改用 base 必有的 `/proc/net/tcp{,6}` + busybox awk 解析
  （2022=0x07EA，state 0A=LISTEN/01=ESTABLISHED），一次扫描同时产出：
  status.json（running/pid/listeners）与 broker_status.json（clients/connected）
- **连接数恒 0 的连带旧 bug 一并根治**：v1.5 时代的 `netstat -tn | ... -c
  ESTABLISHED` 连接数采集同样依赖不存在的 netstat（grep -c 空输入返回 0
  伪装成功），Web「HA MQTT 连接」指示灯一直靠 0 兜底走 warn 分支

### 修复（v1.6.4 增量审查批次：两路独立审查 + 主线对抗复核）
- **run.sh `getent hosts supervisor` 恒假（netstat 同构）**：alpine/musl 体系
  不存在 getent（busybox 未编 applet、aports 无包），错误被吞后每次启动都
  走"无法解析"误导日志、主机名优先设计永久落空——改用 base 实装的
  bind-tools `host` 命令
- **停机路径根修**：`trap cleanup EXIT INT TERM` 的 handler 只清理不退出，
  SIGTERM 后从被中断的 wait 返回（rc=143）被主循环当作崩溃重启 mosquitto，
  直到 docker 宽限期 SIGKILL——broker 从未收到优雅 TERM（persistence 最坏
  丢 30 分钟 retained 状态）、每次 stop/restart 拖满超时。现 INT/TERM 显式
  转发 TERM 给 broker + 1s 落盘窗口 + exit 143（bash 行为已实证）
- **transfer_device 实体重挂从未生效**：`EntityRegistry.async_get_or_create`
  关键字白名单（2024.12→dev 全版本取证）无 name/aliases，旧代码传之
  → 该分支 100% TypeError 被外层 except 吞。已删非法 kwargs（命中既有
  实体时 registry 本就不覆盖自定义名，无需"保留"传参）
- **start_pairing/rename_device 假成功残留**：8 个 error-log-then-return
  分支（未指定参数/未找到网关设备/MQTT 发送失败/rename 返回 False）
  与 check_gateway_status 同类——REST 200 令 Web 弹「配对模式已启动」
  「重命名成功」假 toast。全部改抛 ServiceValidationError
- **check_gateway_status REST 语义纠偏（上游源码取证）**：ServiceValidationError
  实测返回 500 非注释宣称的 400（服务视图仅映射 vol.Invalid/ServiceNotFound），
  对前端 !resp.ok 判据等价；注释改实，前端失败分支补徽标回"未知"重判
- **status/broker json 探活写失败告警**：tmp+mv 链尾 `|| true` 会在 /usr
  只读/磁盘满时让 status.json 冻结在最后成功值（v1.6.2 硬编码 200 的隐蔽
  复刻），改为一行告警入容器日志
- **7b 循环端口动态化**：0x07EA 硬编码改 `printf '%04X' ${MQTT_PORT}`，
  status.json 的 port 字段跟随配置（消灭"改端口后状态说谎"面）
- **Dockerfile 显式安装 openssl CLI**：base 仅含 libssl 库，run.sh 密码哈希
  的 mosquitto_passwd 兜底路径此前是死路（command not found 被吞）
- **诊断与静默吞错清理**：nginx 启动不再 `2>/dev/null`（真实 bind 错误可见，
  此前只剩 nginx -t"语法正常"假象）；`cut|tr||echo 文件不存在` 死兜底改
  显式存在性判断；`cat|jq` 管道掩蔽（cat 失败 jq 空输入输出空串、|| echo 0
  永不触发）改 jq 直读；删除 s6-overlay v3 不存在的 /run/s6/container-env
  死回退分支；utils.async_get_entity_id 的 TypeError→None 兜底补 warning
  日志（registry 内部真 TypeError 不再无声吞成"实体不存在"）
- **number._send_value TOCTOU**：await send_command 后补二次 hass-None 守卫
  （await 窗口内实体被删时不再把"命令已成功"误记为"设置失败"并空跑回退）
- **index.html**：速度/力度 oninput 的 unit 转义序统一为 jsAttr（同类
  残留，当前不可利用但防属性来源变化）
- **services.yaml**：check_gateway_status 补 gateway_sn 字段、device_id 改
  非 required（与 Python schema 与 Web 实际调用形态对齐）
- **nginx 静态 JSON 响应头**：status/version/integration/broker 四处删除
  `add_header Content-Type application/json`（与 mime.types 默认值重复
  产生双 Content-Type 头，违反 HTTP 语义）
- **运维卫生**：run.sh 状态文件写入改 tmp+mv 原子替换（防 nginx 读半截）、
  /api/status 与 /api/broker 补 Cache-Control no-store（防浏览器启发缓存
  显示滞后状态）、头部"v1.4.2"假版本号清除、探活注释周期修正
- 审查中排除的假定性问题（勿再整改）：sensor 移除回调不删注册表条目安全
  （device_registry.async_remove_device 上游级联删除全部实体条目，删除按钮
  实体恰有 button.py 显式删除闭环）；number 启动循环与回调注册间无 await，
  无覆盖竞态窗口；`async_get_device(identifiers=set)` 在 manifest 下限
  2024.12→dev 全版本合法（HA 已标 2027.8 废弃，届时再迁）

## [1.6.3] - 2026-08-29

### 修复（Critical）
- **注册表查找实参回归（utils.py）**：v1.6.0 重构把 `async_get_entity_id()`
  第一实参误写为字面量 `"entity"` 并丢弃调用方传入的实体域，HA 真实签名
  `(domain, platform, unique_id)` 的索引键永不命中——重命名别名同步、删除按钮
  精确定位、`_fix_entity_categories`/`_cleanup_unsupported_buttons`、
  on_device_added 查重等 13 处调用全部静默失效。已还原正确转发（并恢复
  TypeError 兜底），新增 RecordingEntityRegistry 实参断言单测防再犯
- **Web UI 事件属性 XSS（index.html）**：onclick/onchange 参数转义顺序颠倒
  （`jsQuote(escapeHtml(x))` 使 `'` 先变 `&#39;`，浏览器解码后 JS 单引号字符串
  可被含引号的设备昵称闭合注入）。新增 `jsAttr()=escapeHtml(jsQuote(x))` 用于
  全部 13 处事件属性；滑块 state/min/max/单位等拼接一并补转义
- **Broker 崩溃自愈失效（run.sh）**：主循环区 `set -e` 生效下
  `wait $PID; EXIT_CODE=$?`——wait 非零返回直接杀死脚本，重启逻辑成为死代码。
  改为 `|| EXIT_CODE=$?`；重启计数按"连续崩溃"语义在稳定运行 60 秒后清零
  （旧实现生命周期内累计 5 次即永久放弃）

### 修复（High）
- **number 移除竞态崩溃**：`_send_value`/`_revert_to_saved`/防抖链路补
  `hass is None` 守卫（拖滑块后立即删设备不再复现 v1.6.1 类 traceback）
- **实体重复创建**：on_device_added 增加会话内 created_* 字典幂等短路，
  设备重同步不再叠加 add_status_callback（旧实例回调无人摘除的泄漏路径）
- **凭据与攻击面收敛**：删除 nginx `/api/supervisor/` 死代理（带完整
  Supervisor token、前端零调用）及 config.yaml `hassio_api` 权限；
  mosquitto `log_type all` 降为 warning+error（不再把主题/SN 全量入日志）；
  删除启动日志打印密码哈希片段；printf 密码文件写法改 `%s` 格式串并校验
  用户名白名单（含 `%`/`\` 不再损坏哈希）
- **镜像发布链路**：CI 构建后自动把 ghcr 包设为 public（成功后可在
  config.yaml 启用 image: 字段）；Dockerfile 构建排除 `__pycache__`/`tests`
  （新增 .dockerignore）

### 修复（Medium/Low）
- `/api/status` 由 nginx 硬编码 "running" 改为后台探活循环写 status.json
  （2022 端口 LISTEN 判活，broker 挂时页面如实显示已停止）
- mDNS：IP 探测失败不再广播 127.0.0.1（改退出交由看门狗 10 秒重试）；
  run.sh 增加监督循环（进程异常退出自动重启）；每 30 秒检测 IP 变化自动重注册
- Web UI：「未知」占位符不再污染网关 SN 映射与状态/配对请求（改实时读
  GATEWAY_SN_BY_ENTRY）；identifiers 锚点改为按集成 DOMAIN 匹配；
  删除 controlDevice/controlDevicePosition 中拉而未用的全量 /states 请求
  （其失败会拦死控制按钮）；set_position 钳制 0-100 并拒绝 NaN；
  Gitee 无 Release 返回 200+[] 时正确回退 GitHub；页头/页脚版本号不再
  硬编码过期值；隐藏页暂停 30 秒轮询；删除 refreshDevices/transferDevice
  死代码（字段有误，注释留修复指引）
- mqtt_bootstrap 端口回退 1883→2022（1883 根本不监听，死配置）
- check_gateway_status 服务找不到网关时抛 ServiceValidationError
  （REST 400），前端不再收到 200 弹「已发送」假成功
- device_manager：8 处 registry 写操作统一收口 call_registry_method
  （约定已写入 utils.py docstring）；迁移兜底循环补 list() 快照
  （循环内有 await，防注册表并发变更）；button 删除按钮双路径落空补 warning 日志

### 工具链
- CI lint job 新增 pytest 步骤（38→47 项测试；曾整体漏掉 C1 的兼容层
  现有单测首次进入 CI）；版本一致性检查覆盖 manifest.json 与
  version.json 双字段；.gitattributes 补 Dockerfile/*.txt/LICENSE LF 规则；
  .gitignore 排除 会话纪要/ 与 .pytest_cache/
- CLAUDE.md 纠偏：删除「/addons/self/update 免认证」「一键升级依赖
  hassio_api」等与实测定案矛盾的记载，补 MQTT 端口 2022 事实与回归测试纪律

## [1.6.2] - 2026-08-28

### 修复
- **Web 移除按钮"未找到删除按钮实体"**：根因是删除按钮实体
  （GatewayDeviceRemoveButton.device_info 用网关 SN，gateway.py:240）
  归属于网关设备，不在子设备实体列表里。v1.5.5 起前端 remove 分支只在
  子设备实体列表中按 unique_id 查找（v1.5.9 双锚点仍未跳出子设备实体
  列表），导致恒报"未找到删除按钮实体"。修复：新增 findRemoveButtonEntity，
  在 API 返回的整个设备列表（网关 parent + 子设备）中按 _remove_{sn}
  锚点精确定位删除按钮实体，触发 button/press 删除
- 版本号统一为 1.6.2（插件 + 集成）

## [1.6.1] - 2026-08-28

### 修复
- **删除设备后实体残留崩溃（'NoneType' object has no attribute 'data'）**：
  实体从注册表删除后 HA 仍周期性调用 async_update，此时 self.hass 已为 None。
  number/sensor/cover 的 async_update 与 base_entity 的
  get_current_gateway_sn/_get_mqtt_handler 均加 hass 守卫
- **移除按钮实体未删除**：删除按钮改用 unique_id 精确定位删除
  （_aget_eid），不再依赖动态添加实体的 entity_id 赋值时机，带兜底
- **日志级别优化**：自动发现跳过被删设备、手动配对重新添加被删设备日志降为 debug
- 版本号统一为 1.6.1（插件 + 集成）
## [1.6.0] - 2026-08-28

### 修复
- **删除设备批量报错 'NoneType' object can't be awaited**：新版 HA 中
  EntityRegistry/DeviceRegistry 的 async_remove/async_remove_device/async_update_entity
  等均为同步方法（@callback def，直接返回结果），代码中 await 同步方法导致
  'NoneType'/'RegistryEntry' object can't be awaited。新增 utils.call_registry_method
  兼容层（自动探测返回类型，coroutine 则 await，否则直接用），全项目 22 处
  registry 调用统一修复，兼容新旧 HA
- 修复范围：删除设备/子设备、重命名、设备转移、网关迁移、禁用实体恢复、发现忽略等
- 版本号统一为 1.6.0（插件 + 集成）
## [1.5.9] - 2026-08-28

### 修复
- **Web 界面"删除"按钮找不到实体**：删除按钮 unique_id 格式为 {gw}_remove_{sn}
  （gateway.py:228，remove 在设备 SN 前），与常规实体 {gw}_{sn}_{suffix} 布局不同，
  导致 findEntityByUniqueId 按 _{sn}_remove 锚点匹配失败。改为双锚点匹配
  （_{sn}_{suffix} 与 _{suffix}_{sn} 两种布局）
- 版本号统一为 1.5.9（插件 + 集成）
## [1.5.8] - 2026-08-28

### 修复
- **重命名设备报错 'RegistryEntry' object can't be awaited**：HA 新版将
  EntityRegistry.async_get_entity_id 改为 async 方法（返回 coroutine，await 后为
  RegistryEntry），项目 12 处调用均未 await。新增 utils.async_get_entity_id 兼容
  辅助函数（自动探测同步/异步 API，统一返回 entity_id），全部调用点已修复
- **Web 界面"内倒"按钮不发送命令**：controlDevice 缺少 'a' 命令分支，点内倒
  落入"未知命令"；新增分支按 button 实体 unique_id 后缀 _a 精确查找并调用
  button/press 触发内倒（004 命令 value=200）
- 版本号统一为 1.5.8（插件 + 集成）
## [1.5.7] - 2026-08-28

### 优化
- **Web 界面子设备速度/力度滑块始终可用**：移除无初始上报数据时的 disabled 逻辑，
  与 HA 集成 number 实体行为一致（无需先在集成中调整，Web 界面直接可拖动设置；
  有上报数据时回显当前值）
- 版本号统一为 1.5.7（插件 + 集成）
## [1.5.6] - 2026-08-28

### 修复
- **Web 界面网关状态"未知"**：网关在线状态改为直接读取 mqtt_handler.connected
  （收到网关上报即在线，超时置离线），通过设备 API 的 gateway_online 字段返回，
  不再依赖 binary_sensor 在线实体（实体未创建/匹配失败时不再显示"未知"）
- **Web 界面网关 SN"未知"**：从设备注册表 identifiers 提取真实 SN 更新显示，
  解决无 SN 等待模式下 entry.data.gateway_sn 为空导致的"未知"
- 版本号统一为 1.5.6（插件 + 集成）
## [1.5.5] - 2026-08-28

### 修复
- **Web 界面无法控制子设备（"未找到设备 cover 实体"）**：Web UI 用设备 SN 后 6 位
  模糊匹配 entity_id，但设备显示名只含 SN 后 4 位（get_device_display_name 用
  device_sn[-4:]，HA 生成的实体名不含后 6 位）→ 匹配永远失败。
  修复：设备列表 API（/window_controller_gateway/devices）为每个设备返回精确实体列表
  （entity_id/domain/unique_id），Web UI 按 unique_id 锚点（_{device_sn}_{suffix}）
  精确查找，替代字符串模糊匹配。修复范围：开/关/停、位置滑块、速度/力度滑块、
  内倒/风锁模式按钮、删除按钮、在线状态、电池电压显示
- 版本号统一为 1.5.5（插件 + 集成）
## [1.5.4] - 2026-08-28

### 修复
- **手动配对无法重新添加被删子设备**：手动删除过的设备进入全局手动删除列表后，
  `_handle_ctype_003` 的"设备复活守卫"无条件拦截，导致手动配对（003 绑定确认）
  也无法重新添加（2026-08-27 实测）。修复：手动配对确认（bind_op=bind）
  允许重新添加并从删除列表移除；自动发现（002）仍拦截，保持防复活语义
- 新增 003 绑定确认诊断日志（id/errcode/sn/bind_op/手动删除列表）
- 新增 5 个回归测试（test_mqtt_bind.py）
- 版本号统一为 1.5.4（插件 + 集成）
## [1.5.3] - 2026-08-27

### 修复
- **一键升级 400/403 根因修复**：Supervisor 安全设计（2025 年引入）禁止插件通过 API 自我更新——
  `/addons/self/update` 与 `/store/addons/{slug}/update` 检查 REQUEST_FROM 返回 403，
  `hassio.addon_update`（HA Core 服务）返回 400（add-on token 调服务 API 权限不足）。
  将 Web UI「一键升级」改为跳转 Supervisor 加载项页面，以管理员身份点击「更新」（唯一可靠路径）
- 版本号统一为 1.5.3（插件 + 集成）
## [1.5.2] - 2026-08-27

### 优化
- **Web UI 视觉体验优化**：精简布局、优化控件样式与交互细节
- **README 精简优化**：文档结构整理，更清晰易读
- 版本号统一为 1.5.2（插件 + 集成）
## [1.5.1] - 2026-08-27

### 修复
- **一键升级失败诊断增强（Bug A）**：区分 400（hassio 集成未加载/权限不足）与 403（Supervisor 自我更新限制），新增打开加载项页面引导
- **run.sh 密码兜底格式无效（Bug B）**：`openssl dgst -sha256` 拼 `$6$` 前缀为无效格式，改用 `openssl passwd -6`（SHA-512 crypt）
- **静默接管 MQTT 配置（Bug C）**：删除 hassio 源 MQTT 条目前发送持久化通知告知用户
- **设备编号竞态（Bug D）**：新增原子自增计数器 `allocate_device_number()`，批量添加编号不再重复
- **无 SN 模式平台注册（Bug E）**：不再 forward 空平台，消除平台 setup 错误日志
- **墙钟超时误判（Bug F）**：网关/传感器超时改用 `time.monotonic()` 单调时钟
- 版本号统一为 1.5.1（插件 + 集成）
## [1.2.9] - 2026-08-26

### 修复
- **Web UI "HA MQTT 未连接" 根因修复**：addon 的 `config.yaml` 缺少 `homeassistant_api: true` 权限声明，导致 Supervisor 的 `/core/api/` 代理返回 401。所有 haApi() 调用（网关列表、设备、状态、服务）全部失败，前端显示"未连接"。添加该权限后，SUPERVISOR_TOKEN 可通过代理访问 HA Core REST API

## [1.2.8] - 2026-08-26

### 修复
- **一键升级 403 根因修复**：nginx 将 `/api/supervisor/` 代理到 `http://supervisor/supervisor/`，但 Supervisor API 路由是 `/addons/{slug}/...`（无 `/supervisor/` 前缀），导致路径不匹配返回 403。修正为 `proxy_pass http://supervisor/`

## [1.2.7] - 2026-08-26

### 改进
- **自动发现心跳监听器**：无 SN 模式下订阅 `gateway/rpt_rsp` 主题，网关上电后自动触发发现流程，实现"先装集成、后上电网关"的零配置体验

## [1.2.6] - 2026-08-26

### 改进
- **安装流程简化**：`async_step_user` 网关 SN 改为可选项，用户可先安装集成（点"下一步"），之后通过选项页添加网关或等待自动发现
- **选项页支持添加网关**：OptionsFlow 新增 `add_gateway` 步骤，无网关 SN 时自动进入添加表单
- **自动发现填充空条目**：网关被发现时，若已有空 SN 的集成条目，自动填充该条目而非创建新流程
- **无 SN 优雅降级**：`async_setup_entry` 在无网关 SN 时注册空平台并返回，不崩溃

## [1.2.5] - 2026-08-26

### 修复
- **集成版本强制升级 1.4.6→1.4.7**：设备上旧的/损坏的集成代码因版本号恰好已是 1.4.6 导致 run.sh 跳过更新，集成无法加载。强制版本升级确保 run.sh 重新拷贝全部集成文件

## [1.2.4] - 2026-08-26

### 修复
- **一键升级 403 修复**：升级函数调用 `/addons/{slug}/update`（需 admin 权限），addon 的 SUPERVISOR_TOKEN 无权限被 Supervisor 拒绝。改用 `/addons/self/update`（免 admin 路径，Supervisor 自动识别调用者身份）

## [1.2.3] - 2026-08-26

### 修复
- **服务处理器 `hass` 变量修复**：v1.2.0 将 7 个服务处理器从 `__init__.py` 拆分到 `services.py` 时，处理器由闭包函数变为模块级函数，丢失了对 `hass` 变量的闭包访问，导致所有服务调用（配对/重命名/设位置/检查状态/转移设备）触发 `NameError`。修复方案：7 个处理器签名增加显式 `hass: HomeAssistant` 形参，注册时通过 lambda 绑定，兼容所有 HA 版本
- **面板网关列表 401 降级提示**：`loadGateways()` 依赖 HA Core REST API 读取配置条目（插件 token 无权访问），catch 块已改为区分 401 与连接失败，显示对应引导提示而非原始错误

## [1.2.2] - 2026-08-26

### 修复
- **Web UI 状态检查改用插件本地事实**：HA Core 拒绝插件 SUPERVISOR_TOKEN 访问 Core REST API（401），导致面板「网关集成/HA MQTT」永远显示"认证失败"。现改为：网关集成状态读取 run.sh 安装集成时写入的 `integration.json`（`/api/integration`）；MQTT 状态读取 broker 实际 ESTABLISHED 连接数（后台循环每 10 秒写入 `broker_status.json`，`/api/broker`）——面板不再依赖任何 HA API 认证
- **修复 `/api/ha/` 双斜杠**：`haApi` 拼接路径时剥离前导斜杠，消除 `/api/ha//config/...` 形式的请求

## [1.2.1] - 2026-08-26

### 修复
- **移除 `image:` 强制镜像拉取,改回设备本地构建**：GHCR 包可见性反复被重置为 private（匿名拉取报 401/denied），导致 1.2.0 在部分环境无法安装。移除 image 键后，Supervisor 直接在设备上从源码构建镜像，不再依赖任何镜像仓库的可用性与可见性；GHCR 镜像仍由 CI 持续发布，供网络环境良好的用户选用

## [1.2.0] - 2026-08-26

### 修复（核心）
- **MQTT 自动配置根本重写**：旧方案通过 HA Core REST API 自动创建 MQTT 配置条目，但 HA Core REST API 从未提供"创建配置条目"端点（`/api/config/config_entries/entry` 仅支持 GET 列表，`/api/config/config_entries/entry/{entry_id}` 仅支持 DELETE），导致所有版本的自动配置静默失败。新方案改用标记文件机制：插件 `run.sh` 启动时将 broker 连接信息写入 HA 配置目录下的 `window_controller_gateway_mqtt_bootstrap.json`，集成侧新增 `mqtt_bootstrap.py` 模块在 `async_setup_entry` 时读取标记并通过程序化 config flow 自动创建 MQTT 配置条目
- **依赖声明修正**：`manifest.json` 中 `dependencies` 改为 `after_dependencies`，避免鸡生蛋问题（集成需要 MQTT 但 bootstrap 在集成 setup 时创建 MQTT 条目）
- **密码和 token 不再打印到容器日志**：`run.sh` 移除密码明文输出和 SUPERVISOR_TOKEN 前缀输出，符合安全最佳实践
- **新增集成版本下限**：`manifest.json` 添加 `"homeassistant": "2023.8.0"` 最低版本要求

### 改进
- **插件镜像声明**：`config.yaml` 添加 `image:` 键，支持从 GHCR 拉取预构建镜像而非本地构建
- **CI lint 列表同步**：移除已删除的 `auto_setup_mqtt.sh` 引用
- **版本号同步**：插件版本 1.1.9 → 1.2.0，集成版本 1.4.4 → 1.4.5

### 修复（回归审查轮）
- **Supervisor 环境菜单流程适配**：HA 2024.9+ 在 HAOS/Supervised 安装上，MQTT config flow 首步返回 menu（addon/broker 选择）而非表单；bootstrap 现在自动导航到 broker 子步骤，否则自动配置会无限重试永不完成
- **新版 broker schema 兼容**：2026.8+ 校验器要求 `other_settings` 段，缺失直接 KeyError 导致 SETUP_ERROR 无重试；按 `__version__` 探测决定是否附带
- **`async_wait_for_mqtt_client` 用法修正**：该辅助函数超时返回 `False` 而非抛异常，原实现忽略返回值误报连接成功；所有失败路径现在会中止残留流程避免堆积
- **陈旧标记清理**：关闭 `auto_setup_ha_mqtt` 选项后启动时删除历史引导标记文件，避免集成读到过期凭据无限重试
- **前端 Ingress 全量修复**：API 基路径改为从 `location.pathname` 推导（此前在 HA 侧边栏 iframe 中所有请求打到 HA 根路径全部 404）；位置滑块改调集成真实服务 `set_position`（cover 实体无 SET_POSITION 特性）；在线徽章空 SN 误匹配守卫；一键升级动态发现插件 slug（git 仓库安装的 slug 带仓库前缀）并正确区分"已是最新 / 任务进行中 / 已中止"
- **集成运行时缺陷**：畸形 sn 类型帧不再因 AttributeError 导致整帧丢弃（含心跳，避免网关被误判离线）；persist 深拷贝消除并发持久化静默丢失；已手动删除的设备不再被晚到的绑定确认复活；后台任务随配置条目卸载取消；补齐 `invalid_input` 中止翻译键

### 安全加固
- nginx ingress 仅允许 HA Core（172.30.32.2）来源访问
- Docker 基础镜像固定为 `ghcr.io/home-assistant/{arch}-base:3.21`（amd64/aarch64 双架构经 GHCR registry 验证）
- 移除 services.yaml 中未注册的幽灵服务声明 `migrate_devices`
- `full_access` 经 Supervisor app schema 核验后保留（当前 schema 无 `host_name` 选项，且 `host_network` 会与 HAOS 系统 avahi 冲突抢占 UDP 5353）

## [1.1.9] - 2026-08-26

### 新功能
- **Web UI 一键升级**：检查更新发现新版本时，点击「一键升级」按钮直接调用 Supervisor API 更新插件，无需手动操作 HA 插件商店
- **nginx 新增 Supervisor API 代理**：`/api/supervisor/` 代理到 `http://supervisor/supervisor/`，支持前端直接调用插件更新/重启等 Supervisor 端点
- **GitHub API 代理修复**：检查更新改为通过 nginx `/api/github/` 代理，避免 Ingress iframe 中 CSP 拦截外部 `api.github.com` 请求

### 优化（Web UI 全面重构）
- **CSS 变量化**：使用 `:root` CSS 变量统一管理颜色，全站一致
- **配色现代化**：主色改为 #5b6ee1，状态色用绿/黄/红+对应浅色背景，视觉层次更清晰
- **卡片/按钮/徽章重设计**：圆角、阴影、hover 过渡效果统一
- **状态指示器优化**：带 `box-shadow` 光环的圆点，更醒目
- **响应式适配**：窄屏状态网格自动折叠为单列
- **代码精简**：`checkServiceStatus` 和 `loadDeviceState` 去重，逻辑更紧凑

### 修复（关键 - Web UI 状态检测三项全失败）
- **MQTT Broker 状态"无法连接"**：`/api/status` 由 nginx 直接返回，但 nginx 启动失败时不可达。添加 `nginx -t` 诊断输出，同时 `add_header` 添加 `always` 确保错误响应也带 Content-Type
- **网关集成/HA MQTT 检测"检测失败"**：nginx 代理到 Supervisor API 时 `supervisor` 主机名在 `full_access: true` 模式下可能无法解析。添加 `getent hosts` DNS 解析检测，失败时兜底为 Supervisor 固定 IP `172.30.32.2`
- **SUPERVISOR_TOKEN 为空导致 401**：`run.sh` 中 `HA_SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN:-}"` 可能为空（`with-contenv` 未正确加载时）。添加从 `/run/s6/container-env` 手动 source 的兜底逻辑，并输出 token 前缀确认

### 修复（关键 - 根本原因）
- **auto_setup_mqtt.sh HTTP 401 Unauthorized（最终修复）**：`run.sh` 通过 `SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN:-}" /auto_setup_mqtt.sh &` 显式传递环境变量，但这个旧 token 会覆盖 `with-contenv` 从 `/run/s6/container-env` 加载的最新有效 token。改为不传递 `SUPERVISOR_TOKEN`，让 `auto_setup_mqtt.sh` 的 `with-contenv` shebang 自动加载正确 token
- **auto_setup_mqtt.sh Supervisor 主机解析兜底**：与 `run.sh` 一致，添加 `getent hosts` 检测，失败时使用 `172.30.32.2`

### 改进（前端容错）
- **`haApi` 函数不再 throw**：改为始终返回 `resp` 对象，由调用方检查 `resp.ok` 和 `resp.status`，可区分 401（认证失败）、502（代理连接失败）等不同错误
- **`checkServiceStatus` 错误分类**：401 显示"认证失败"（红色），代理连接异常显示"代理失败"（红色），其他 HTTP 错误显示状态码（黄色）
- **所有 `haApi` 调用方添加 `resp.ok` 检查**：`loadGateways`、`loadGatewayDevices`、`startPairing`、`controlDevice`、`controlDevicePosition` 均添加

## [1.1.8] - 2026-08-26

### 修复（关键 - 根本原因）
- **auto_setup_mqtt.sh HTTP 401 Unauthorized（根本原因）**：`#!/bin/bash` 不通过 `with-contenv`，无法从 `/run/s6/container-env` 加载最新的 `SUPERVISOR_TOKEN`，导致 token 虽有值但被 Supervisor 拒绝。恢复 `#!/usr/bin/with-contenv bashio` shebang，同时所有配置变量用 `${var:-default}` 避免 bashio `set -u` 报错
- **avahi-daemon 启动逻辑矛盾**：失败时仍输出"已启动"。修复为 `if/else` 逻辑，失败时提示改用 IP 地址
- **dbus 启动失败**：容器中缺少 `/run/dbus` 目录，Dockerfile 和 run.sh 均添加 `mkdir -p /run/dbus`

### 变更
- `auto_setup_mqtt.sh` shebang 从 `#!/bin/bash` 恢复为 `#!/usr/bin/with-contenv bashio`
- `run.sh` avahi-daemon 启动逻辑改为 `if/else`，添加 `mkdir -p /run/dbus` 和 `sleep 1` 等待 dbus
- Dockerfile 添加 `mkdir -p /run/dbus`
- **config.yaml 添加 `full_access: true`**：avahi-daemon + dbus 需要系统总线权限才能运行 mDNS 广播
- **前端 XSS 修复**：`renderGateway` / `renderDevice` 中所有用户可控字段（网关名称、设备名称、SN、ID）添加 `escapeHtml` 转义
- **Dockerfile 注释修复**：`dbbus` 拼写错误 → `dbus`
- **auto_setup_mqtt.sh 注释修复**：过时的 `127.0.0.1:1883` → `127.0.0.1:2022`
- **顶层 README.md 同步**：架构图和安装说明中的 1883 端口 → 2022，mDNS 描述对齐
- **前端连接信息更新**：MQTT 地址显示 `huijian.local:2022`，配置提示支持 mDNS
- **CI 质量门禁**：添加 lint job，检查 shell 语法、YAML/JSON 语法、版本号一致性
- **安全文档**：README 添加修改默认密码和备份提醒
- 版本号 1.1.7 → 1.1.8

## [1.1.7] - 2026-08-26

### 修复
- **auto_setup_mqtt.sh HTTP 401**：bashio shebang (`#!/usr/bin/with-contenv bashio`) 可能重新加载环境覆盖了 SUPERVISOR_TOKEN，改用 `#!/bin/bash` 普通解释器
- 新增调试日志：输出 SUPERVISOR_TOKEN 前缀确认是否有效传递

### 新增
- **mDNS 支持**：Dockerfile 添加 avahi + dbus 依赖，run.sh 添加 avahi-daemon 配置和启动，LoRa 网关可通过 `huijian.local` 发现 HA 主机

### 变更
- `auto_setup_mqtt.sh` shebang 从 bashio 改为普通 bash
- Dockerfile 添加 avahi、avahi-compat-libdns_sd、dbus 包
- README 更新 LoRa 网关地址说明，支持 `huijian.local`
- 版本号 1.1.6 → 1.1.7

## [1.1.6] - 2026-08-26

### 修复（关键 - 根本原因）
- **端口冲突彻底解决**：主机端口和容器端口统一改为 `2022`，完全不使用 1883/1885，与 HA 官方 Mosquitto broker 零冲突
- `config.yaml` `ports` 改为 `2022/tcp: 2022`
- `mosquitto.conf` 监听端口改为 `2022`
- `run.sh` 中 `MQTT_PORT` 和 `INTERNAL_PORT` 均改为 `2022`

### 变更
- 所有端口引用从 1885/1883 统一改为 2022
- 版本号 1.1.5 → 1.1.6

## [1.1.5] - 2026-08-26

### 修复（关键 - 根本原因）
- **端口 1883 冲突（根本原因）**：`config.yaml` 中 `services: - mqtt:provide` 导致 HA Supervisor 自动将 1883 端口分配给插件，与官方 Mosquitto broker 的 1883 冲突。移除 `services: mqtt:provide`，插件不再向 HA 声明为 MQTT 服务提供者，HA MQTT 集成通过 auto_setup_mqtt.sh 手动配置连接到 172.30.32.1:1885

### 变更
- `config.yaml` 移除 `services: - mqtt:provide`
- 版本号 1.1.4 → 1.1.5

## [1.1.4] - 2026-08-26

### 修复（关键）
- **端口 1883 冲突**：移除 `mqtt_port` 可配置项，Docker 端口映射固定为 `1885:1883`（主机 1885 → 容器 1883），避免用户误配 `mqtt_port` 为 1883 与 HA 官方 Mosquitto 冲突
- **`mqtt_port` 与 `ports` 脱节**：之前 `mqtt_port` 配置项只影响显示和 auto_setup，不改变 Docker 实际端口映射，容易造成混淆

### 变更
- `config.yaml` 移除 `mqtt_port` 配置项和 schema
- `run.sh` 中 `MQTT_PORT` 固定为 1885，不再从 bashio::config 读取
- 版本号 1.1.3 → 1.1.4

## [1.1.3] - 2026-08-26

### 修复（关键）
- **auto_setup_mqtt.sh HTTP 401 Unauthorized**：`SUPERVISOR_TOKEN` 未显式传递给子进程，导致调用 HA Supervisor API 认证失败
- **README 端口信息过时**：文档中 LoRa 网关端口仍写 1883，实际应为 1885

### 变更
- `run.sh` 显式传递 `SUPERVISOR_TOKEN` 给 `auto_setup_mqtt.sh`
- `auto_setup_mqtt.sh` 中 `HA_TOKEN` 添加默认值防止 `set -u` 报错
- README 全部端口信息更新为 1885
- 版本号 1.1.2 → 1.1.3

## [1.1.2] - 2026-08-26

### 修复（关键）
- **Web UI「MQTT Broker无法连接」**：nginx ingress 配置了 `allow/deny` IP 限制，Ingress 模式下来源 IP 不固定导致 403，移除 IP 限制
- **Web UI「网关集成检测失败」「HA MQTT检测失败」**：默认 `ingress.conf` 缺少 `/api/ha/` 代理配置，添加 HA API 代理
- **Web UI「检查更新」无反应**：直接请求 `https://api.github.com` 被 Ingress iframe CSP 拦截，改为通过 nginx `/api/github/` 代理
- **auto_setup_mqtt.sh 崩溃**：`USERNAME: unbound variable` — bashio 的 `set -u` 导致子 shell 中未定义变量报错，为所有变量添加默认值

### 变更
- `run.sh` 中显式传递环境变量给 `auto_setup_mqtt.sh` 子进程
- `ingress.conf` 默认配置同步添加 `/api/ha/` 和 `/api/github/` 代理
- Web UI 新增 `updateConnectionInfo()` 从 `/api/status` 动态读取端口显示
- 版本号 1.1.1 → 1.1.2

## [1.1.1] - 2026-08-26

### 修复（关键）
- **端口映射架构修正**：`config.yaml` 端口映射改为 `1885:1883`（主机 1885 → 容器 1883），mosquitto 容器内固定监听 1883
- **auto_setup 检测修正**：broker 可达性检测用 `127.0.0.1:1883`（容器内），HA MQTT 集成连接用 `172.30.32.1:1885`（主机映射端口）
- **auto_setup 更新也输出 HTTP 状态码**：之前更新分支没有输出 HTTP 状态码，无法诊断失败原因

## [1.1.0] - 2026-08-26

### 修复（关键）
- **端口冲突彻底解决**：MQTT 端口改为可配置（`mqtt_port`），默认 1885，避免与 HA 官方 MQTT 集成的 1883 端口冲突
- **自动配置 MQTT 集成失败**：`curl` 改用 `-s`（去掉 `-f`），不再在 HTTP 错误时返回空字符串；输出 HTTP 状态码和完整响应体便于诊断
- **mosquitto.conf 动态端口**：配置文件中用 `__MQTT_PORT__` 占位符，`run.sh` 启动时用 `sed` 替换为实际端口

### 变更
- 新增 `mqtt_port` 配置项（默认 1885）
- `watchdog` 改为 `tcp://[HOST]:1885`
- Docker 端口映射改为 `1885:1885`
- 插件版本号 1.0.9 → 1.1.0
- 集成版本保持 1.4.4

## [1.0.9] - 2026-08-26

### 修复
- 移除 `host_network: true`，使用 Docker 端口映射
- mosquitto 文件权限 0700
- 移除 mDNS/avahi 依赖

## [1.0.8] - 2026-08-26

### 修复
- 端口冲突检测
- avahi-daemon 修复

## [1.0.7] - 2026-08-26

### 修复
- Mosquitto 启动流程重写
- Web UI 动态化
- GitHub Actions 自动 Release

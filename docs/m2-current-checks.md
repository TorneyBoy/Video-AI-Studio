# M2 当前实现检查（2026-10-18）

后续更新：本日新增C06/C07服务端版本基础后，专项32项与最终全量 **381 passed、1 skipped，ALL: PASS**；原生共享DTO类型检查通过。最新证据在`data/testing/m2-versions/`，详细实现及尚未完成的双端页面/帧精度见[编辑版本](edit-revisions.md)。下表353/21项保留为前一轮记录，不与新计数相加；完整M2仍未验收。

用户在微信开发者工具被占用时明确要求先做其他工作并自动测试。本报告记录本轮自动检查和本机网页交互，不是整个M2验收报告，也不把类型检查代替微信编译/真机。

| 检查 | 实际结果 | 范围与证据 |
| --- | --- | --- |
| 当前自动质量门 | 353 passed、1 skipped | compile、Ruff、所有拆分网页脚本语法、网页恢复、供应商离线契约和完整pytest；`data/testing/m2-current/quality-gate.log`。真实PostgreSQL使用专用临时schema，包含刷新竞争和两个标签编辑者竞争 |
| 新增计划/上传专项 | 21 passed | `test_auto_edit.py`、`test_planning_analysis_reuse.py`、`test_storage_gateway.py`；`data/testing/m2-current/planner-upload-regression.log`。与完整质量门有重叠，不累加为374项 |
| 原生小程序 | TypeScript类型检查通过 | 最新`npm run typecheck`成功，`data/testing/m2-current/typecheck.log`；不启动或占用微信开发者工具，不配置虚假AppID/API域名 |
| 最后差异的快速质量门 | 90 passed、1 skipped，FAST: PASS | `data/testing/m2-current/fast-gate.log`；Ruff、全部脚本语法、原有恢复与新增上传恢复脚本均通过，与其他测试有重叠 |
| 新网页上传恢复脚本 | 通过 | `tests/frontend_upload.cjs`：2MiB以上分块指纹对照独立SHA；模拟云端已收字节但客户端丢失确认；错文件在续签/传输前拒绝；无原文件核验恢复；同内容改本机文件名仍保留原意图；凭据仅在云调用内存，平台认证不转发。内存传输桩不是新一次真实OSS验收 |
| 本机网页交互 | 通过所列流程 | 127.0.0.1:8090独立SQLite、测试账号和项目；实际注册/会话进入工作台、页内建项目、单项名称/标签/备注保存、筛选保留选择、批量追加标签、双页面旧版本保存409 |

完整质量门完成后，补充了11条计划/上传测试及上传恢复脚本，并对影响范围继续回归；不能把此前353项改写成未经重跑的全量364项。最后的快速门/类型检查结果以`data/testing/m2-current/fast-gate.log`和`typecheck.log`为准。后续继续开发时应按新增差异更新证据。

浏览器测试数据只在`data/testing/m2-current/browser.db`。两条素材明确使用`upload://pending/...`、storage.status=created和`QA 待上传`名称，没有真实媒体、完成证明、成片或供应商任务。通过这些记录测试标签权限与交互，不能据此称为新的真实媒体闭环。第二页面先改名后，第一页面保存显示“0个已保存”、版本冲突并禁用保存，列表保留新名称。界面证据：

![本机QA素材冲突检查](../data/testing/m2-current/labels-conflict.png)

本轮未调用新的收费云分析/渲染，不重置原3/6分析与6/6渲染计数；累计USD10测试约束、CNY20体验包支出及待出账事项均保留。未修改DNS，未将M2账号/标签迁移运行到ECS，生产仍为M1 control v8/gateway v11。

以上为账号/素材基础批次记录，后续计划版本进展如下。测试通过的是各批次实际范围，完整Goal继续保持active。

## C06/C07 页面及帧精度批次

- 整批：403 passed、1 skipped，ALL: PASS，包含真实PostgreSQL并发，`data/testing/m2-studio/quality-gate.log`。编译、Ruff、全部网页脚本语法及三种网页恢复检查通过。跳过项仍是缺本机FFmpeg/ffprobe的旧本地适配烟测。
- 最后提交同步围栏调整：`tests/frontend_editor.cjs`通过，`data/testing/m2-studio/editor-final.log`；请求已经确认但新版尚未同步时仍禁用后续写入，避免旧head二次保存。
- 原生：类型检查和`tests/miniprogram_studio.cjs`通过，`typecheck.log`、`native-studio.log`；编译到临时目录后以模拟wx存储/请求执行真实状态与页面代码，覆盖原键重试、409草稿、刷新保留、上下文变化、页面离开与缓存失败。CI已加入这些检查，未运行微信工具/真机。
- 帧精度：不足一帧的源不隐式延长，目标过短明确拒绝，3镜头在0.1秒/30fps时各保留一帧；不同帧率、整数分配、源区间防越界通过。共享模板预设重复初始化、已有模板不覆写及Job精确键/类型/项目/租户过滤通过。
- 本机网页：127.0.0.1:8091使用同一独立QA数据库的新项目`M2 版本编辑自动检查`。三条素材名称含“非真实媒体”，只有测试镜头元数据，无真实视频文件。页面创建计划后仅运行自动剪辑元数据Worker：v1计划→v2锁定→v3替换→v4恢复→v5六秒方形配置；另一页保存v6锁定后，原v5页三秒配置被409拒绝。原输入保留，载入v6后仍为3，重新应用按钮由禁用变可用；放弃草稿没有改变服务端v6。截图`data/testing/m2-studio/editor-conflict.png`；不据此声称实际六秒方形云视频已通过。

首轮整批为402通过、1失败、1跳过：新增Job列表用例误把自动创建的分析Job算为计划，修正用例的job_type过滤后重跑403通过。首轮日志保留，不隐藏失败，也不与381或其他专项相加。最后前端围栏变更仅补跑受影响的网页/原生恢复用例，不重复无关整批。

C06/C07的后端和双端页面源码已衔接，完整M2仍缺微信登录/工具/iOS/Android/真实直传、实际新增输出规格、C08/C09精细编辑和字幕、审核/比较/预算/批次。没有新收费云请求，不重置3/6分析和6/6渲染；生产、DNS和供应商权限未变。详见[编辑版本说明](edit-revisions.md)。

## C08 片段基础批次

整批质量门416 passed、1 skipped，ALL: PASS，`data/testing/m2-clips/quality-gate.log`，含真实PostgreSQL；跳过项仍为旧本地FFmpeg/ffprobe适配烟测。片段/版本专项26 passed有重叠，不累加；最后字体地址过滤新增专项另见`clip-final.log`，不改写整批数量。原生类型/离线运行和网页精确参数草稿恢复通过，同目录`native-studio.log`、`web-editor.log`。

新增13条事务检查覆盖裁切向内对齐、源范围/帧数拒绝、分割唯一ID与原键收据、锁定邻居的位置保护、精细时长不恢复已剪内容、音量和固定渲染快照、必需槽位缺失不分配Job、多操作原子失败、其他轨道不被隐式裁切。随后补充只读视图去除字体位置和媒体签名的专项。网页与原生参数草稿新增动作组隔离、原始精确输入及409保留检查。

本机网页沿同一QA元数据项目继续v7解锁→v8裁切→v9分割→v10设置音量0.25→v11后移→v12删除→v13撤销；实际保留源区间、4.933秒总长、帧对齐及音量，浏览器无控制台错误。截图`data/testing/m2-clips/editor-precise.png`。没有真实媒体文件、供应商渲染或听感证据，不声称C08/完整M2完成。微信工具/真机继续延后；下一批继续字幕及剩余多轨、叠加/转场等范围。

## C09 字幕基础批次

整批 **432 passed、1 skipped，ALL: PASS**，`data/testing/m2-subtitles/quality-gate.log`，包括真实PostgreSQL及全部网页脚本语法/恢复；唯一跳过项仍是旧本地FFmpeg/ffprobe适配烟测。字幕/片段/版本专项42 passed有重叠，不累加；最后原文真正分页和subtitle/1元数据标记调整的字幕专项 **15 passed**，`subtitle-final.log`。最后历史恢复按钮和跨表单草稿保护修正通过`web-editor-final.log`；原生类型及离线运行通过`typecheck.log`、`native-studio.log`。

服务端检查覆盖：建立计划时冻结原文，重新分析不改旧原文；明确导入旧来源、重复导入不覆盖修订/不恢复删除；连续分割片段合为一句、后续分割仍跟随、裁切缩短/移除随源句；不连续关联拒绝；固定时间切换、帧/画布/重叠/空文本拒绝；版本稳定的UTF-8 SRT/VTT、转义/空行、哈希、租户隔离；多操作原子失败。两端恢复检查另覆盖409文字草稿、他页删句后明确转为新增、错误版本/内容哈希阻止文件导出。模拟文件写入不等于微信设备验收。

网页实测v14恢复初始12秒→v15导入三句测试识别→v16中文人工修订、字号56、颜色#FFE080、位置72/1600→实际下载SRT/VTT→v17视频分割→v18视频裁切。分割后仍为三句，裁切后字幕0–3.5、3.5–7.5、7.5–11.5秒，修订与原文保留，部分音频给出审阅提示。历史面板打开时保存新版后，非当前版本恢复按钮已确认恢复可用。

实际下载SRT241字节、VTT249字节，与v16快照逐字节和SHA-256一致，证据`browser-export-evidence.json`，文件[`browser-v16.srt`](../data/testing/m2-subtitles/browser-v16.srt)、[`browser-v16.vtt`](../data/testing/m2-subtitles/browser-v16.vtt)。浏览器下载观察事件超时，但实际文件已落到正常下载目录并独立核对；没有因观察超时重新创建版本/收费任务。截图`subtitle-editor.png`。全部是非真实媒体与测试ASR元数据，没有新的云识别、烧录或媒体输出；不得据此宣称真实中文字幕可读性或音画同步通过。独立音轨/淡入淡出、剩余高级轨道、审核/批量和完整M2继续进行。


## C08/C09 多轨与准入批次

整批451 passed、1 skipped，ALL: PASS（含真实PostgreSQL），`data/testing/m2-tracks/quality-gate.log`。最终差异专项55 passed，含轨道/准入20项，与整批重叠，不累加；`final-delta.log`。原生TS、模拟wx、网页真实脚本恢复与Ruff通过，见同目录对应日志。唯一跳过项仍为旧本机FFmpeg/ffprobe适配器烟测。

自动检查覆盖音轨/叠加素材捕获、按帧区间、淡入淡出、同类轨移动、重叠拒绝、画布边界、静音/删除/撤销、转场真实重叠时长和分割、锁定、三段重叠拒绝，以及渲染未开放时不建任务/账务预占、同服务完整覆盖、既有收据和远程核对。网页/原生实际脚本模拟409和他页删除后，原始参数保留，明确转新增后才可再次保存；不同表单不能覆盖草稿。

本机网页QA从v19到v24保存音轨淡入淡出、图片位置/尺寸/透明度、轨道顺序与隐藏/显示；数据库独立核对，只有原计划Job 1条、费用账0条。截图`layer-editor.jpg`，结构记录`browser-evidence.json`。素材为无真实媒体文件的显式QA夹具，不能作为真实合成/混音/中文烧录/听感证据；转场本批是自动元数据契约检查，没有冒称网页已渲染。

M2整体、C10–C12及基础批量继续。没有占用微信工具，没有新的云媒体请求、配额调整、权限扩张、DNS变更或生产部署。


## C10 审核批次

整批471 passed、1 skipped，ALL: PASS，含真实PostgreSQL，`data/testing/m2-reviews/quality-gate.log`。最终版号/UTC差异专项17 passed，`final-review.log`；与整批重叠，不累加。真实网页脚本和原生源码的离线回执/草稿/生命周期检查、TS和Ruff通过，同目录对应日志。

检查覆盖真实个人会话的所有者自审/编辑者拒绝、跨租户隐藏、审核CAS与同键重放、分页历史、归档/质检/manifest/版本/撤销后领取拒绝、签名期间审核改变不返回新地址、新版本和另一份高清输出不继承批准，以及未知网络结果沿用原请求、存储失败前不提交、账号变化和迟到相册调用拦截。网页加载不同成片时的迟到响应不能覆盖当前视频和审核对象。

本机QA已观察v24退回（字幕1–2秒问题）→批准→编辑v25→新成片待审核且领取禁用，原历史保留，实际页面显示清楚版号。记录`browser-evidence.json`和两张JPEG。此处输出、QC和归档是显式元数据夹具，未调用媒体服务，不能证明真实机器质检、播放定位、实际下载或音画质量。QA项目3个Job中2个是直接种入的占位记录，费用账0，不把模拟Job计作真实渲染。

M2仍缺C10高级机器质检/实际媒体及微信验收、C11比较与受控分享/清单、C12预算恢复和D批量。没有部署到ECS、没有变更DNS或扩展供应商额度/权限，微信工具未占用。

## C11作品与交付批次

整批`quality_gate.py all` **487 passed、1 skipped，ALL: PASS**，真实PostgreSQL开启，日志`data/testing/m2-delivery/quality-gate.log`。随后新增真实会话分享范围检查1项通过，`session-scope.log`；该项单列。`native-delivery.log`、`native-review.log`、`native-studio.log`、`web-delivery.log`与`typecheck.log`均通过。唯一跳过仍为缺本地FFmpeg/ffprobe的旧适配器测试。

覆盖：分享同键回执/不同意图、摘要存储、空间/外链受众、默认播放不含下载/字幕、撤销/到期/密钥轮换/后续审核/归档变化失效、签名及文件导出期间撤销、分享期限约束、清单脱敏和内容指纹、成片固定版本字幕、跨项目/租户拒绝、200条无命中后继续游标、并发创建及单调撤销；离线双端原提交恢复、错哈希/路径拒绝、隐藏页面与账号切换、版本播放迟到结果排序。新增真实会话检查所有者可创建，编辑者不能管理分享、同空间成员可读取指定空间分享、退出后401。

本机真实网页操作只使用显式无媒体夹具：批准列表v24；v24/v25音量1→0.75；内部清单和中文SRT；一小时外链的公开清单/字幕导出；撤销后接收页禁用控件。`browser-evidence.json`及`comparison.png`、`comparison-final.png`、`share-active.png`、`share-revoked.png`，并保存4份下载副本。独立核验两份JSON的manifest_hash、敏感字段省略、两份字幕字节一致。浏览器下载事件观察超时后，用实际下载文件证明结果，没有继续依赖事件或占用微信工具。

QA仍3条Job（2条无媒体占位），费用账0；没有真实媒体下载/播放或新供应商请求。不能据此宣告C11/M2完成；批次分组随D01实现，真实媒体、微信相册/文件交付及C10高级质检待验。下一推进C12两端费用/恢复，再D01–D05；生产仍M1，0008–0012未部署，DNS/配额/权限不变。

最终轨道摘要界面差异后，已针对更新的实际网页脚本、原生delivery源码和TS重新检查通过，未无故重跑整批；最终网页也已刷新核对。WXML仍须待微信工具可用时完成平台编译和设备验收。


## C12费用与恢复第一批

整批`quality-gate-final.log`为 **505 passed、1 skipped，ALL: PASS**，含真实PostgreSQL。最终按编辑版本查询已有任务/另建生成入口的定向27项通过，`final-contract.log`，与整批重叠不累加。`web-billing.log`、`web-retry.log`、`native-billing.log`、studio/review/delivery离线日志及`typecheck.log`通过；最终费用布局调整后，实际网页脚本已再次检查并刷新观察。证据目录`data/testing/m2-billing/`。

覆盖普通视频从真实目录选择报价、固定输入/模型/价格、到期/单次消费/原Job重放、服务时长/格式限制、首次外部提交前价格变化阻止且保留已知远端查询、项目上限/币种/CAS、真实PG竞争、取消保留待对账、后续再次失败不重放原恢复动作、大金额字符串精度和未知金额不显示免费。两端真实脚本/源码执行断线原键、存储失败、预算冲突原输入/原基线、隐藏/账号变化、原生已有Job查询与明确另建入口。

本机网页为显式元数据QA：项目预算0.2保存，0.1低于0.15已用/保留被拒绝且原输入刷新后恢复；之后明确载入当前0.2。取消测试Job后仍保留0.05待对账；恢复原Job仅回执为queued，不增Job，随后通过取消结束测试轮询。报价从既有M1配置读取v1、6秒、1080×1920和CNY0.06预占，展示价格版本，空间与项目预算阻止确认。无效QA认证值只用于目录构造，没有真实Provider凭据/探测/Worker/媒体调用。5条Job、3条费用都是直接种入的夹具，不能当作实际产出或实际消费；0个render Job、0个ProviderOperation，3条动作回执。`fixtures.json`、`browser-evidence.json`、`finance-overview.png`、`quote-budget-blocked.png`保留记录。

C12综合分析报价、真实费用、双端断网与设备验收、C10高级质检和M2整体继续；下一步补齐分析/规划附带分析确认再推进D批量。0013只在测试/QA迁移，生产0008–0013未执行，M1、域名、云权限/额度及USD10上限不变；微信工具未使用，未提交Git。


## C12综合报价增量（2026-10-18）

- `preparation-quality.log`：全量500 passed / 27 skipped，ALL:PASS。未配置PostgreSQL的本次整批先跳过26项数据库测试；随后在本机Docker的可丢弃PostgreSQL测试库执行全部数据库竞争及综合报价，共43 passed（26数据库+17报价，与全量重叠，不相加），记录`preparation-races-final.log`。每项数据库检查创建独立测试schema并清理，不使用生产库。
- `preparation-client-final.log`：实际网页脚本、原生综合报价/恢复、原生费用恢复及TS通过。另已执行原生studio/review/delivery既有套件通过；没有启动微信开发工具或真机。Ruff全目录通过。
- 浏览器本机实测：带转写分析预占CNY0.5，空间/单任务/项目预算拦截；关闭转写使旧报价失效，重新报价复用已有镜头；计划综合报价同样使用已有镜头且不新增预占。未点击收费任务确认，项目5条既有Job、3条模拟费用、ProviderOperation=0、综合报价消费=0。`preparation-browser-evidence.json`及`analysis-quote-blocked.png`、`plan-quote-cached.png`为证。
- 0014只应用于测试/本机QA；生产仍M1，0008–0014未发布。上述金额是目录预占和模拟费用，不是实际账单或新增媒体验收。下一开发优先级D01/D02，C12真实费用、微信和M2整体验收继续保留。


## D01/D02批次增量（2026-10-18）

全量`m2-batches/quality-final.log`为543 passed / 1 skipped、ALL:PASS，包含真实PostgreSQL31项旧/新竞争检查，其中五项批次检查也单列`postgres.log`，不累加。旧临时PG容器已不存在，本轮使用独立Docker容器video-ai-m2-batch-pg-test、回环55439、可丢弃video_ai_batch_test库；测试各自创建/清理schema，不连接生产库。`native-final.log`为TS及全部原生离线套件；页面输入保存修正后另跑实际网页与原生批次源码，见`clients-final.log`；`overrides-final.log`补充1项实际单项模板/素材/时长/画幅覆盖验证，不累加到整批数字。

本应用浏览器完成已有QA项目的十项配置、固定零新增预占报价、确认、9/10模拟失败展示、原失败子项恢复和10/10汇总；本地规划器只编译已有分析，不执行媒体/供应商。其余九项版本/hash保持不变，项目16 Job、3条既有模拟账务、0 ProviderOperation；证据`browser-evidence.json`及同目录截图。十项真正独立的计划/版本已持久化，不代表十条真实视频或微信端验收。未使用微信工具/设备；D03公平调度/取消/整批渲染、D04审核领取、D05真实双端闭环继续。


## D03 固定版本渲染及所选任务操作增量

本轮全量为563 passed、1 skipped，ALL: PASS，见`data/testing/m2-batch-runs/quality-final.log`；含真实PostgreSQL。专项19项与全量重叠，不累加。TS及七组原生离线检查通过；最后跨页面保护的受影响双端代码再次通过，见`clients-final.log`。网页十项报价合计CNY0.60，额度不足禁止确认，QA未新增渲染/供应商任务；九个此前成功项的文档/版本/计划/时间线hash不变，见`browser-evidence.json`。详细范围与截图见[进度中的D03记录](development-progress.md)。这不代表微信平台/真实媒体或整个M2验收；公平调度、并发限制和批量审核交付仍待推进。生产0008–0016未部署，无新收费云调用。


## D03 调度与容量增量

最终`data/testing/m2-dispatch/increment-final.log`36项通过，包含32项新契约及4项既有恢复兼容检查，含8项真实PostgreSQL竞争；TS及七组原生离线套件通过。当前整库快照`quality-final.log`为592通过、1跳过、1个测试替身协议缺字段失败；该替身已补齐并在36项中通过，未伪称整库ALL:PASS。其他专项与36项重叠，不累加；完整M2待功能闭环集中验收。网页展示批次执行容量，十项版本及原16Job/3条模拟ledger不变，没有新增远端调用。真实供应商和微信平台未验，生产0008–0017未部署。详见[调度进度与截图](development-progress.md)。

# M2 双端接口与状态约定

当前实现中，未完成阶段验收。两端使用同一个 `/api/v1` 业务API、同一账号/空间、任务和产物记录；业务不由页面自行写状态。API的Pydantic schemas为服务端来源，原生TypeScript的阶段性类型位于 `miniprogram/src/shared/contracts.ts`。当前尚未建立完整的自动类型漂移检查，不能声称所有DTO/页面已经双端齐全。

## 公共入口

| 子内容 | 共享入口 | 当前客户端情况 |
| --- | --- | --- |
| 身份 | /auth/capabilities、register、login、refresh、logout、me | 账号基础自动检查和网页注册实测通过；原生TS通过，微信登录及完整双端验收未完成 |
| 项目 | GET/POST /projects、GET /projects/{id} | 网页M1已有；小程序列表/新建/选择源码已写入 |
| 素材 | GET /assets?project_id=&q=&tag=&media_type=、GET/POST /assets/upload-intents、POST /assets/{id}/verify-storage-upload、POST /assets/{id}/labels（兼容PATCH） | 两端上传/恢复/选择/标签源码已写入；网页整理与恢复契约通过，小程序TS通过但尚未实际直传 |
| 分析 | POST /assets/{id}/analyze、GET /jobs/{id} | 网页M1已有受限能力；小程序动作待接入，不把HTTP200当文字质量合格 |
| 自动计划 | POST /jobs/auto-edit、GET /jobs/{id}/timeline | 双端源码已接入模板、时长/画幅与审阅；新规格实际输出和微信工具/真机待验 |
| 渲染 | POST /jobs/{plan_id}/preview、/export；指定编辑版本的render | 服务端生成不可变manifest、预检报价；两端按版本提交/恢复源码已写，实际新增媒体效果及微信验收待完成 |
| 任务恢复 | GET /jobs、/jobs/{id}，POST /jobs/{id}/retry、/cancel | 保留原Job/幂等身份；双端任务队列与恢复交互待C12 |
| 领取 | GET /artifacts?project_id=，POST /artifacts/{id}/access | 网页M1真实通过；小程序领取、播放和保存源码已写，真机待验 |
| 编辑版本 | POST /jobs/{id}/editing、GET /editing/{doc}、GET/POST其下revisions、版本candidates/timeline/render、操作requests收据 | 服务端及双端页面源码已接入锁定/替换/重算/撤销/恢复/冲突、主视频裁切/分割/重排/删除/原声音量与固定版本渲染；网页所列操作、原生类型/离线运行通过，完整多轨和真实双端验收仍待完成 |
| 字幕/原文 | 条件保存operations增加import_transcript/save_subtitle/delete_subtitle；指定revision下GET transcripts、subtitles?format=srt或vtt | 双端字幕/原文/样式和时间方式源码已接入；网页实际文字修订、下载与哈希通过，原生类型及模拟文件写入通过；烧录、字体/同步与微信文件流程未验收 |
| 审核 / 批次 / AI工具 | C10、D01–D05、后续G系列 | 尚未按双端完整交付，不用现有低层接口或占位菜单冒充完成 |

租户创建/列举、任意Job状态变更、内部归档执行、回调及人工费用对账都不是个人客户端入口。客户端不传供应商密钥，不接受客户端Tenant头充当身份，不调用内部接口制造完成状态。

## 状态与失败恢复

任务以服务端 queued/running/processing/succeeded/failed/cancelled 为准，进度来自实际步骤。失败需同时读取失败码、retryable、attempt_count/max_attempts及policy_blocked，不能仅凭按钮自行重排。未知提交保留原动作身份并先查询；同一次操作的重发必须保留请求和幂等键，不能换ID、换供应商或自动再付费生成。

错误暂兼容既有 detail 字符串、校验数组和含 code/message 的对象；网页和小程序分别统一为用户可读错误。认证请求校验剔除input，避免回显密码/token。新DTO不应让前端猜金额单位、时间基准或权限结果。分页、版本冲突和批次局部失败仍需后续任务落实，当前不宣称已有统一分页或If-Match实现。

网页登录恢复只在明确401时刷新并复用原请求体及幂等键；原生客户端使用单例刷新Promise合并各页面请求。网络中断不触发自动POST重试。浏览器多标签页使用Web Locks；小程序内存访问令牌配合可选本机刷新令牌，账号切换清理项目缓存，项目切换阻止迟到列表覆盖新上下文。

视频显示 succeeded 仍须经过质检、归档和服务端领取资格检查。签名URL只在内存短期使用；续签不重新渲染。小程序相册保存仅在平台成功回调后显示成功，下载中断/权限拒绝/空间不足需给明确恢复入口。最终审核、不可变版本和有效分享撤销由C07/C10/C11继续补齐。

素材整理是完整替换用户标签/备注的版本化动作，base_revision必填；冲突409不自动换新版本重发。两端批量操作逐项追加标签，保留各自原有标签/备注，逐项显示结果，不伪装为批次原子成功。原生wx.request不支持PATCH，因此统一提供等价POST入口；不会伪造HTTP方法或跳过身份检查。素材列表按skip/limit分页；原生素材页已写入50条分页，网页完整分页/数量统计仍待C02补齐。

上传发现接口只返回本空间意图的规格、状态和稳定键，不返回历史签名URL/字段/临时凭据；sha256检索可包括已完成项。两端使用`upload:{project_id}:{sha256}`作为新上传稳定键，存在旧记录时保留原键与原规格。核验提示对象不存在才传输；未知结果先核验，授权过期续签同一意图。不同文件不能覆盖恢复记录。页面临时文件与签名只在内存使用；小程序退出不承诺持续上传。

编辑契约见[剪辑版本设计与实现](edit-revisions.md)。保存必须携带base_revision_id和稳定操作键；409保留原输入，不能自动载入head后覆盖。GET操作收据只查询，不提交修改；已提交的旧收据可能指向非当前版本，客户端须读取head_revision_id后载入当前版。恢复是新版本，原计划Job不原地变成后续编辑结果；新版Draft/Final调用指定revision的render入口，并显示来源版本。重试次数保留首次任务预算，不作为新媒体请求的区分依据。

## 尚需完成的C02验收

编辑响应新增只读editor（fps、实际时长、分辨率及轨道片段），不复制媒体/字体地址或授权。新增trim_clip/split_clip/move_clip/remove_clip/set_clip_audio仍走原条件保存及操作收据；源秒数按帧处理，移动序号从0开始。两端精确参数草稿保留原字符串，冲突不自动提交新版；操作含义与边界见[版本与片段说明](edit-revisions.md)。这仍不是完整端点/DTO漂移验收。

完整端点/错误/幂等/分页/版本清单、两端入口映射和自动DTO检查；网页与小程序对同一账号操作同一记录；切账号/项目、过期、断网、重复点击和迟到响应的实测。每项能力必须在两个个人客户端都具备配置、执行、结果和恢复入口后才标记双端完成。


多轨编辑使用相同`POST /editing/{document_id}/revisions`、CAS与幂等收据。双端已接入音轨/叠加保存、片段/轨道删除、轨道静音和层级、主视频fit/入场转场，字段及边界见[编辑版本](edit-revisions.md#音轨叠加和转场命令)。`editor.sources`是已捕获素材的安全摘要；`editor.advanced_support`用于说明本版本尚缺的媒体效果能力，不是客户端执行授权。原始表单按上下文保留，409后重新加载不会自动重放；原记录被删除须明确转新增。渲染最终以服务端准入为准，检查发生在新Job和费用预占之前；已有收据仍可核对。


成片审核由`GET /artifacts/{id}/review`、`POST /artifacts/{id}/reviews`、`GET /artifacts/{id}/review-requests/{key}`及分页history共享。两端提交完全相同的binding_hash/base_review_id/decision/note/issues，不接受客户端指定审核人或QC状态。正式`purpose=download`需精确输出获批；`purpose=preview`用于审阅。Artifact列表/详情补充job_status、job_policy_blocked、edit_revision_number，原始manifest输出的版号为null。原生work-detail已提供意见、问题位置/类别/程度、历史、继续编辑和相册保存；网页播放器旁有同等入口。未知结果保留原键/正文，409保留原始输入并要求刷新再决定，过期/变更在服务器再次校验。详见[成片审核](edit-revisions.md#成片审核绑定与交付)。

## C11作品与交付契约

| 接口（前缀`/api/v1`） | 方法与含义 | 双端入口 |
| --- | --- | --- |
| `/projects/{id}/works` | GET，state/cursor/limit；items、next_cursor、has_more、scanned、total_jobs、total_outputs。空items仍可能has_more，不按空页擅自结束 | 网页项目作品；小程序works |
| `/projects/{id}/works/compare` | GET，left/right为同项目输出ID，skip/limit分页差异；comparison_hash固定输入，source_labels仅当前页需要的名称；不创建Job | 网页比较面板；小程序compare |
| `/artifacts/{id}/delivery-file` | GET，kind=manifest/srt/vtt，visibility=internal/public，expected_binding核对本输出；UTF-8 content、sha256、filename、binding_hash | 网页交付面板；小程序delivery写文件后由用户选择接收位置 |
| `/delivery/config` | GET，只读是否已配置分享、时限及原因，不返回密钥 | 双端分享区域 |
| `/artifacts/{id}/shares` | POST创建（Idempotency-Key）；GET skip/limit所有者记录，不带链接 | 双端分享设置/记录 |
| `/artifacts/{id}/share-requests/{key}` | GET原创建回执；核对不提交 | 双端未知结果恢复 |
| `/artifacts/{id}/shares/{share_id}/link` | POST读取仍有效的链接；不自动写入剪贴板或发送 | 双端明确获取/复制 |
| `/artifacts/{id}/shares/{share_id}/revoke` | POST单向撤销；重复操作不改变首次撤销时间 | 双端分享记录 |
| `/shared/inspect`、`/shared/access`、`/shared/file` | POST，以正文token兑换摘要、短期媒体或公开文件；独立接收页，默认no-store/no-referrer，不回显校验输入 | `/share#token`接收页 |

分享创建绑定`binding_hash`和最新批准`review_id`，设置label、audience（workspace/anyone_with_link）、allow_download、include_subtitles、expires_in_seconds。字幕权限不能超出下载权限。默认workspace只播放；有效期60秒至7天并受归档期限限制。所有者权限由服务端成员记录判断，编辑者不能创建/列举/取链接/撤销；workspace接收方仍须同租户有效会话，外链受众按令牌能力访问。

两端只持久化分享表单与原操作元数据，原始链接、媒体地址和文件路径只在页面内存。初次明确4xx可结束操作；结果未知时保持同一key/正文，不能以新设置覆盖原提交。离开页面、切账号/项目、迟到响应不得恢复旧链接或启动迟到文件分享。导出必须先核对artifact/binding和UTF-8 SHA-256；公开接收页另核对通用文件名。下载/保存不是重新渲染。

比较只读输出固定快照。两端可切换同一播放器，在两份输出共同时间范围定位；按成片时间比较不承诺同一源镜头自动对齐。片段差异分页和完整长文字均可展开；空轨/轨道数量及规格通过两侧摘要查看。原生平台编译、实际播放、相册/文件接收权限、批次分组和真实媒体分享验收仍待完成，不能由离线契约检查替代。


## C12费用与恢复契约

| 接口（前缀`/api/v1`） | 输入及响应 | 双端呈现 |
| --- | --- | --- |
| `/billing/overview` | GET，可选project_id；项目累计或空间本月、独立币种、空间政策及项目上限 | 网页费用面板；小程序billing |
| `/billing/entries` | GET，可选project_id、skip/limit；安全费用行、调整合计、净额、next_skip/has_more，当前范围全部历史 | 费用记录分页 |
| `/billing/jobs`、`/billing/jobs/{id}` | GET，项目/状态分页或单任务；状态、次数、执行代次和允许动作，不含原始payload | 任务查询、取消/恢复 |
| `/projects/{id}/spending-limit` | POST（兼容PUT），base_revision/currency/limit_micros，所有者条件保存 | 项目上限、币种、原输入保留、显式载入当前值 |
| `/editing/{doc}/revisions/{rev}/render-quote` | POST RenderRequest，稳定报价键；固定版本、预占上限、价格/模型、10分钟到期及当前预算原因 | 生成前报价与明确确认 |
| `/timelines/{id}/render-quote`、`/jobs/{id}/render-quote` | POST相同请求，兼容时间线和已完成原计划；不创建媒体任务 | 原计划接入能力，用户制作页优先固定编辑版本 |
| `/billing/render-quotes/{id}` | GET，只读原报价状态和已消费Job；不替用户提交 | 回执核对；实际确认仍重新校验 |
| `/billing/jobs/{id}/actions` | POST，action、expected_status/attempt/generation及稳定操作键 | 取消或恢复原Job，原文持久化后才发送 |
| `/billing/jobs/{id}/actions/{key}` | GET，found/action/current；核对不提交 | 断线先查，沿用原键/原文继续 |
| `/jobs?revision_id=...` | GET，配合project_id、job_type及order=desc/limit；精确编辑版本过滤 | 原生先查已知任务，避免把查看误作另建收费 |

新增费用接口所有`*_micros`响应为十进制字符串，客户端用字符串格式化并分币种显示；保存上限使用安全整数、最多6位小数。不把空值转换成0；待对账零保留显示金额未知。空间预算修改和真实对账仍是内部权限，普通所有者只能增加项目限制或调整自身项目上限，不能提高空间额度。

新渲染确认携带quote_id，原任务回执优先返回；输入或价格变化、报价过期/已消费、超服务范围会拒绝新提交，不能自动刷新价格后替用户确认。旧远端恢复继续原作业，另建生成使用新报价及新逻辑键，明确说明新的费用操作。原生制作页普通入口先查已有Job，已有输出去work-detail，待处理/失败去billing；网页另建入口与查看/恢复入口分开。

两端任务动作仅保存第一方请求元数据，未知结果不换键，不扩大force或最大重试次数；不同代次/状态条件返回409，刷新后由用户决定新动作。预算冲突保留原输入与原base_revision，显式载入当前值才换基线。前后台切换刷新不会自动提交；账号/项目变化及迟到响应不得更新旧界面。C12当前仍缺分析及规划附带分析的综合报价和真实跨端/费用验收，完整DTO漂移与设备检查继续C02/C03及M2验收。


## C12分析与计划综合报价

普通会话调用`POST /preparation/analysis-quotes`或`POST /preparation/plan-quotes`（Idempotency-Key），只创建报价。分析请求选1–100个同项目素材，设置镜头分析、转写、模型名称、超时和max_attempts；额外执行参数/价格不接受。计划沿用AutoEditCreate设置，素材范围在报价时固定。`GET /preparation/quotes/{id}`返回逐项cached/reused/new、原任务建议、分币种预占和当前预算是否允许，不返回源地址/分析快照。已有结果可用时无需配置供应商；新增分析须有已验收价格。

`POST /preparation/quotes/{id}/confirm`正文只能为空对象、使用稳定Idempotency-Key，原子确认整批；成功返回根计划Job（分析单独报价时为空）及逐素材Job/复用情况。`GET /preparation/quotes/{id}/requests/{key}`可读取原回执，消费后过期仍有效。分析结果已缓存时job_id允许为空，不可把它当作未提交再收费。两份报价竞争可在确认时降为复用，实际新增预占不超过确认的报价；发生阻断时整个事务回滚。

会话`/jobs/auto-edit`须携带quote_id及与报价完全一致的设置，返回原父Job，兼容原计划提交恢复。`/assets/{id}/analyze`的报价别名用于确有原/新Job的单素材场景；完全缓存请使用综合确认接口。development_header保留旧内部开发兼容入口，不能当作公开用户绕过流程。确认未知时先查询原回执或沿用原报价/同键/原正文；原提交未知后的4xx也保留缓存。未确认分析、计划、渲染之间都不能互相换成新的费用操作。网页和原生入口在输入改变/过期时重新报价；切换项目不展示旧报价。上传只提示报价，不自动分析。


## D01/D02批次配置与计划

`POST /batches`（project_id/name/shared/items + Idempotency-Key）只建立草稿；GET /batches?project_id分页及GET /batches/{id}读取逐项状态。shared支持template_id、asset_ids、target_duration、aspect_ratio；每项有稳定key/name/overrides，覆盖仅允许同四字段，空覆盖继承共用，明确null时长使用模板。最多100项及100个不同素材，无隐式笛卡尔展开。草稿可不完整，POST /batches/{id}/validate（base_revision）返回定位key/field的issues；不会创建媒体任务。

`POST /batches/{id}/configuration`需base_revision与稳定键，CAS冲突保留客户端输入；GET /batches/{id}/configuration/requests/{key}返回原revision/snapshot及当前head_revision。GET /batches/requests/{key}查询创建回执。提交后配置固定，用户可明确复制为新草稿。

`POST /batches/{id}/quotes`（base_revision、稳定键）创建共享分析报价，GET对应/quotes/{quote_id}读取；返回确切项数、不同素材数、逐项已有/复用/新增和分币种总预占。逐项费用按首次引用分配，共用素材只计一次，渲染不在此次报价内。内部batch_analysis报价不可通过单素材/普通报价接口消费。

`POST /batches/{id}/confirm`只接受quote_id/base_revision和原键，同事务创建共享分析（如需要）、汇总Job及逐项独立计划。GET /batches/{id}/requests/{key}?quote_id=...查询固定结果回执。已消费报价过期后仍能重放同键；不得替换另一报价或参数。过期未消费、或批次单调版本/已提交状态证明原操作永久不可提交时，在批次锁下返回can_abandon；保存查询可带base_revision取得同类证明。未找到回执本身不表示可换新提交。客户端处理成功后才清除待核对；隐藏、迟到响应及未知后的4xx不丢原记录。

BatchItem返回原job_id及对应编辑文档/当前头版本引用；打开普通制作页继续单项编辑/渲染/审核。汇总status包含draft/running/succeeded/partial_success/attention/cancelled，counts逐项解释；父Job仅汇总、无单独费用，不允许普通取消/重试。D03固定版本渲染轮次与所选原任务操作已接入，公平调度/子项限流及D04批量审核/领取尚待实现。


### D03 固定版本渲染与所选原任务操作

- `POST /batches/{id}/render-quotes`：稳定请求键，1–100个明确的`{key,revision_id}`、quality与force_new。版本必须为所选项当前头；返回逐项版本/复用或新增/价格及按币种汇总，不返回内部报价、来源或签名地址。顺序按原子项排列。报价不创建Job或预占，10分钟到期。
- `POST /batches/{id}/render-confirm`：原报价ID及固定确认键；同事务消费子报价、复查输入/价格/额度、创建渲染轮次及独立子Job。默认复用同版同参数原任务；并发期间已提交的同版任务可降为复用。明确新增另行计费，存在该编辑文档的处理中渲染、未知远端或待对账费用时禁止新增。原失败任务应先恢复。版本变化整批拒绝，不自动替换成最新版本。
- `GET /batches/{id}/runs`及`/{run_id}`返回固定版本历史、原Job及安全任务状态、派生汇总；可按artifact_id进入现有审核/领取入口。刷新时当前较旧轮次即使不在最新一页，也单独读取保留。`batch.render`父Job只汇总，不能通过普通取消/恢复API操作。
- `POST /batches/{id}/phases/{phase_id}/actions`：phase为`plan`或轮次ID。明确选择子项key、原job_id及预期status/attempt/generation，action为retry/cancel。逐项applied或跳过；固定回执不随后续状态改变。先锁全部原Job/Workflow，再进入可能使用预算锁的逐项操作。共用分析不会隐式取消。复用的是同一个原Job，其他轮次引用也受操作影响；本地取消不是供应商取消证明或退款。
- 回执：`GET /batches/{id}/render-requests/{key}?quote_id=...`、`/phases/{phase_id}/actions/requests/{key}`。未知结果保留原键/正文，重复403也不丢弃；仅服务器证明过期且从未消费的报价可放弃。页面隐藏或切换账号后收到成功，仍保留原记录供恢复；不修改另一项目的界面。批次生成与普通创作、分析、草稿提交和费用操作互相拦截新提交，原请求核对不受阻。

这些入口已在网页及原生小程序源码接入；微信平台、真实批量媒体、供应商取消与最终账单尚未验收。小程序本机检查不会调用微信开发者工具。


### D03 调度与执行容量

本机数据库Worker现在统一调度分析、计划、其他AI和渲染四类队列，仍保留各类型入口。先筛除未到退避时间、外部运行时及分析依赖尚在执行的任务，再按个人空间→项目→批次/单条→能力类型→新增/原任务查询→原任务的持久轮次选择。一次poll中同一Job最多处理一次；各类型保留本次limit上限。较早的大批次、反复轮询和新到的单项均有轮转机会；归档及派生汇总继续独立推进。

执行容量默认全局8、空间4、项目3、批次2，可通过统一部署的`DISPATCH_*_LIMIT`设置。统计running任务及submitting/pending/unknown远端操作，已取消但远端未知的原操作仍占容量；相同Job不重复计算。原远端查询复用自己的容量，即使上限后来降低也可继续对账；未提交项继续排队，不消耗尝试预算。`BatchView.execution`提供本批次active、batch_limit和说明，双端生成页显示。

实际提交前还使用所选供应商与capability的数据库门禁，遵守服务目录的max_concurrent；缺少身份的未知提交按可能占用该服务处理，不凭空释放容量。受信终态回调或迟到结果只释放执行容量，不恢复交付资格或推定退款。供应商已明确结束的原操作不能被重新当成新提交；新的生成仍需新报价。

首次收费提交复查现有预占及当前空间/月度/项目限制，防止前项实际超支后继续扩散；跨月未提交预占在同一预算锁下复查并迁入本月，保留原期间说明。原远端查询不做新的费用准入，不因余额不足而失去查询机会。排队取消、未知账单保留、最终结算的原有语义保持。当前仅数据库Worker接入；这不是已经部署Temporal或完成供应商容量压测的声明。

## 固定轮次批量审核与交付

网页`batch-delivery.js`和原生`pages/batch-delivery`使用同一[批量交付契约](batch-delivery.md)。从生成记录进入固定轮次，按状态筛选，明确选择、核对每份输出与旧审核后提交批准/退回。组审核保留逐项CAS结果；归属不符整组拒绝。原请求在发送前持久保存，隐藏/换账号后的迟到结果不清除记录；与普通制作、分析报价、批次和费用操作互相保护。

清单返回实际纳入/排除项、总字节和文件SHA，客户端验证后导出。包创建返回`disposition=new|reused`；相同有效清单可复用原包或原失败恢复任务，终止但远端未知时不另建。网页逐项显示“下载已发起”，不能推定浏览器保存完成。原生下载不附平台身份头，核对长度/SHA后再调用相册/文件导出，逐项计数实际成功；可主动获取并复制短期包链接，不将URL、云对象地址或临时文件路径写入持久缓存。尚无微信平台/真机验收结果。

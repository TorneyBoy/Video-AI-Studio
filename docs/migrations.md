# 数据库迁移

项目使用应用内的轻量版本化迁移执行器，不依赖本地 GPU 或额外的数据库迁移服务。迁移记录保存在 `schema_migrations` 表中。

## 执行方式

部署 PostgreSQL 时，在启动 API/Worker 副本前执行一次：

```bash
DATABASE_URL='postgresql+psycopg://user:password@host:5432/video_ai_studio' \
  python -m app.cli migrate
```

本地 SQLite 可以直接运行：

```bash
python -m app.cli migrate
```

API 启动时仍会调用同一个幂等迁移入口，便于本地开发和单实例部署。生产扩容前应先完成一次迁移，再启动多个 API/Worker 副本，避免多个副本同时执行首次结构变更。

## 当前版本

| 版本 | 内容 |
| --- | --- |
| `0000_baseline` | 空库或没有迁移历史的旧库使用当前 ORM 元数据补齐缺失表，并写入基线标记。已有表的列不会由 `create_all` 静默修改。 |
| `0001_jobs_runtime_and_idempotency` | 为旧 `jobs` 表补充 `idempotency_key`、重试控制、失败审计和执行时间列，并补齐查找索引与租户范围的幂等唯一约束。 |
| `0002_budget_tables` | 显式创建 `tenant_budgets` 和 `usage_ledger`，补齐租户、作业和月份查询索引，兼容已经执行过 `0001` 的生产数据库。 |
| `0003_completion_contract` | 执行代次、产物唯一键、供应商操作/事件、费用证据/调整和事务事件；历史重复引用保留并标记。 |
| `0004_storage_upload_intents` | 持久化租户范围幂等上传意图、请求哈希和固定对象引用；不保存短期签名 URL、上传凭据或 OSS 密钥。 |
| `0005_artifact_storage` | 独立记录产物归档路径、大小、哈希、证明和保留期限；归档不自动授予交付资格。 |
| `0006_artifact_archive_tasks` | 归档任务唯一键、租约、错误码、退避与最多 5 次重试；渲染完成后可恢复调度。 |
| `0007_usage_ledger_status` | PostgreSQL 的 `usage_ledger.status` 从 20 扩到 40 字符，允许写入 `pending_reconciliation`；保留已有账务数据。SQLite 无需改列。 |

`0001` 只做可回滚前的加法操作，不删除数据、不读取或改写素材对象。旧库中已有作业的默认值分别为 `attempt_count=0`、`max_attempts=3`、`retryable=false` 和 `policy_blocked=false`；可空时间和错误字段保持为空。

## 发布约定

新增或修改持久化模型时，必须同时添加一个递增版本的迁移函数到 `app/migrations.py`，并在 `tests/test_migrations.py` 增加旧库升级与重复执行测试。不要依靠 `Base.metadata.create_all()` 为已有表补列；它只负责空库初始化和首次基线。

迁移前应完成数据库备份，并在预发布环境验证 `schema_migrations` 中的版本、表结构和关键索引。迁移采用前向兼容策略：先添加结构，再发布读写代码，删除或收紧约束需要独立的后续版本。

## 任务完成契约迁移

`0003_completion_contract` 增加执行代次、供应商操作/事件、费用证据/调整和事务通知表，以及产物角色与序号的唯一约束。历史产物全部保留；同任务同类型同 URI 的重复引用标记为 `archived_duplicate` 并记录原产物 ID，不删除原始证据。

升级前备份数据库。先运行 `python -m app.cli migrate`，再部署新的 API 和 Worker；不要混跑忽略执行代次的旧 Worker。生产迁移由单个维护进程执行。该迁移是增量迁移；回滚应用前需先停止 Worker，并保留新增表和字段，避免丢弃费用与事件证据。恢复完整旧状态应使用升级前备份及对应旧版本，不能仅删除迁移记录。

## M1真实账务与M2身份升级

`0007_usage_ledger_status` 将 PostgreSQL usage_ledger.status 扩至 VARCHAR(40)，解决22字符 pending_reconciliation 被旧20字符字段拒绝的问题。旧金额、证据和状态保留；M1服务器已备份并应用，实际列宽及版本记录确认，SQLite不需要改列。

`0008_personal_identity` 为M2新增 users、memberships、external_identities、user_sessions、refresh_tokens、auth_throttles、auth_events。只建表和约束，不自动给旧Tenant绑定用户，不修改旧Job/产物/账务。旧库保留、重复升级及PostgreSQL刷新竞争已通过当前自动检查；**未对M1服务器运行此迁移**。不能将新增会话模型视为微信接通、公开登录或生产迁移已经完成。

`0009_asset_labels` 新增tenant/asset范围内的标签索引及`(asset_id,tag_key)`唯一约束。标签展示内容和版本保存在Asset的user_tags/user_note/user_labels_revision命名空间；没有修改对象URI、媒体哈希、分析或许可证明。旧库保留与重复迁移、真实PostgreSQL并发标签写入已在当前M2自动检查中通过；0008身份刷新竞争也已通过。**0008/0009均未在M1服务器执行**，生产升级仍须在可回滚的M2部署中实施。

`0010_edit_revisions` 新增edit_documents、plan_revisions、timeline_revisions、edit_heads；版本序号及操作键按文档唯一，head引用已保存版本。旧库升级只建表，不改写原Job、Timeline、产物或费用，不预先给旧项目制造历史快照。新计划在Worker完成事务内建立第一版；旧已完成计划可通过正常编辑入口延迟初始化。SQLite新库/旧库重复迁移及真实PostgreSQL版本并发已通过专项检查。**此迁移尚未运行到ECS，M1服务器仍未执行0008–0010**。下一次部署须备份并按顺序迁移，再检查旧入口和恢复路径；不能把本地迁移通过称为线上升级完成。


`0011_artifact_reviews`新增artifact_reviews，按产物/序号及产物/幂等键唯一，保存可信审核人、绑定摘要、原请求指纹和追加的意见/问题。只建新表，不改写旧Job/时间线/产物/账务，不自动给既有成片批准。审核记录的外键跟随既有产物删除生命周期；完整删除/恢复和保留策略继续H03，不能把追加写入称作永久不可删除审计。当前新库、旧库、重复升级及PostgreSQL真实审核竞争已通过本批检查。**仅测试库和本机QA升级；M1 ECS尚未运行0008–0011**。部署新API前必须先迁移并准备个人所有者账号，配套提供审核入口；正式下载新增审核条件，既有原始manifest输出可按其原内容审核后领取，不以兼容名义自动批准。

`0012_artifact_shares`新增artifact_shares：产物/操作键唯一、令牌摘要唯一，绑定租户、输出指纹、审核ID、受众、可下载/字幕范围、到期和撤销时间。只建表，不自动分享既有成片，不保存原始令牌/链接，不改写旧账务或批准。当前SQLite迁移与真实PostgreSQL同意图/不同意图创建、并发撤销已通过，生产ECS尚未执行0008–0012。上线时需通过受控配置提供至少32字符的`ARTIFACT_SHARE_SECRET`和HTTPS公开Origin；空值保持分享关闭。轮换密钥使旧链接失效，不能承诺旧签名媒体URL立即失效或收回已保存文件。


`0013_billing_workbench`只新增project_budgets、render_quotes、job_actions，不改写既有费用、预算、Job或批准。项目表按project_id唯一，报价按租户/逻辑键唯一，任务动作按Job/逻辑键唯一。报价记录保存输入/能力指纹、价格和到期/消费键，不保存凭据或外部签名；任务动作只保存安全状态回执。项目更新和预算预占共用租户锁；报价消费锁读取时刷新旧Session实例，防止并发两次消费。当前新/旧库重复迁移及真实PostgreSQL项目额度/报价/版本竞争通过；仅测试库和本机QA使用，ECS尚未执行0008–0013。新用户渲染接口启用报价条件前必须同时部署双端入口、迁移和有效服务目录，不能把本地通过称作生产迁移完成。


### 0014_preparation_quotes（未部署生产）

新增preparation_quotes：租户/项目、类型、报价请求键、输入hash、设置与分析/模板/价格快照、10分钟到期、确认键和固定结果回执。租户内报价请求键及非空确认键分别唯一；确认受素材及租户额度锁保护。不存在媒体Job/费用预占时也能保存只读报价。只有确认成功才同事务写入全部子Job、预算预占与父计划。SQLite新/旧库重复迁移及真实PostgreSQL并发检查通过；当前只在测试库和本机QA应用。生产0008–0014仍未应用，发布时须连同网页/小程序报价入口与服务目录一起部署。


### 0015_production_batches（未部署生产）

新增production_batches、batch_items、batch_revisions、batch_quotes。约束包括租户内创建键、批次内子项key/草稿版本/保存键、租户内报价与确认键唯一；父Job和各项计划Job为外键关系，项目/租户隔离。不可变草稿回执、配置/模板快照及共享分析报价引用用于确认；aggregate_watermark记录实际读到的子任务更新时间，区别于汇总扫描时间，避免竞争漏更新。批次/报价锁、素材有序锁与租户预算锁在一次事务中保护整批确认。SQLite重复迁移和真实PostgreSQL竞争通过；生产0008–0015仍未应用。


### 0016_batch_runs（未部署生产）

新增batch_render_quotes、batch_runs、batch_run_items、batch_task_actions，保留明确选定版本的报价、各轮渲染与原任务引用、按项操作固定回执。报价请求键/消费键、quote关联轮次、轮次内子项和租户操作键均有唯一约束。新提交锁批次及报价、按ID有序锁编辑文档、再锁租户额度；普通单版渲染也使用同一文档锁。引用的子任务可由多轮复用，汇总水位来自实际读取的子Job元数据。旧报价与费用不改写；父Job不可作为媒体执行或普通恢复对象。仅测试库和本机QA执行0016；生产0008–0016尚未应用。


### 0017_fair_dispatch（未部署生产）

新增dispatch_clock、dispatch_turns、job_dispatch_stamps和provider_dispatch_gates。唯一约束保护层级轮次及供应商/能力门禁，Job标记随Job删除级联，空间轮次随空间删除级联。迁移只创建表并幂等初始化一行clock，不重置任何Job尝试次数、费用预占或供应商计数。新旧批次根据受所有权校验的父Job/批次/内部分析报价引用分组；旧计划不要求重写payload。生产0008–0017尚未部署。

### 0018_batch_delivery（未部署生产）

新增batch_review_actions、delivery_packages和delivery_package_requests，保存组审核逐项固定回执、包文件集合/远端恢复状态，以及不同请求键复用同一包的独立创建回执。tenant+key、package Job和首创建键具有唯一约束，删除包时创建回执保留结果快照、package引用置空。迁移只创建新表，不改写Job、旧审核、费用或供应商计数。已在SQLite/隔离PostgreSQL及本机元数据QA执行；生产0008–0018均尚未部署。上线须先迁移，再同时升级API及所有Worker；打包默认关闭，见[批量交付](batch-delivery.md)。

### 0019_runtime_observability（未部署生产）

新增job_traces、runtime_counters、worker_pulses、operational_alerts。Job首请求来源和父子继承属于原创建事务，重复回调计数属于原回调事务；回滚不留成功观察。告警转换仅锁自己的固定互斥记录，心跳独立连接，不修改Job/费用或调度记录。已有Job不伪造请求来源、旧回调不回填未保存的重复次数。只在测试和本机隔离观测库执行0019，原本机8091仍为0018；生产0008–0019尚未部署。先迁移再同步升级API与全部Worker，并配置独立METRICS_API_SECRET与采集端。见[基本观测](operational-observability.md)。

### 0020_database_recovery_state（未部署生产）

新增database_recovery_state单例，持久保存数据库实例UUID、active/quarantined状态及恢复backup_id。新库幂等初始化active；已有单例不覆盖。恢复工具在校验原快照后保存quarantined及新UUID，并预先设置数据库级恢复GUC；后续迁移不会清除该GUC或重置单例。执行票据绑定实例，拒绝异库迟到完成；Job/账务/产物记录不回填或重写。仅用于新建的本地测试库；生产0008–0020尚未部署，原本机8091仍为0018。见[备份与隔离恢复](database-recovery.md)，不能把该迁移或清除状态当作已完成生产接管。

### 0021_legacy_tenant_adoptions（未部署生产）

新增legacy_tenant_adoptions：请求UUID主键、Tenant唯一、原数据库实例和固定意图/回执；User外键删除时置空，Tenant删除时级联。只建表，不自动给旧空间分配用户，不移动项目/对象或修改预算/任务。专用CLI仅允许隔离库中的明确空间新增个人所有者，与User/Membership同事务提交；重复请求不重置凭据。具体范围和运维步骤见[旧空间个人账号迁移](legacy-personal-adoption.md)。原0007生产及本机8091/0018没有应用本次迁移，生产0008–0021均未部署。


### 0022_storyboard_authoring（未部署生产）

只新增reference_usage、reference_analyses、storyboard_documents、storyboard_revisions。旧素材不自动设为参考；旧Job、费用、Timeline和媒体对象不变。新增参考固定对象身份和范围证据；分镜版本唯一约束与当前指针支撑CAS/回执。只在本机隔离测试库与真实PG的随机测试schema执行；生产0008–0022未部署，本机8091仍为0018。部署仍需显式迁移并统一API/Worker版本，不能把新增表视为参考智能分析或生产接管已完成。


### 0023_shot_matching（未部署生产）

新增asset_semantic_revisions、shot_match_reports、storyboard_source_revisions、storyboard_source_heads；不变更原列，不回填旧素材标注，不移动对象，不改写旧Job/账务/Timeline。素材标注固定源身份，报告固定分镜与选定输入，采用记录独立追加且以版本指针CAS。真实PG竞争及M1冻结旧库升级回归已通过；仅本机隔离库与随机测试schema执行，生产0008–0023未部署，原8091为0018。见[镜头匹配与来源](shot-matching.md)。

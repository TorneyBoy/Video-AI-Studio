# M2基本观测与故障定位（H01）

本批实现内部诊断、请求/任务关联、安全日志、Worker独立心跳、队列/远端/费用指标与状态告警。它服务于平台运行，不给个人工作台增加运维页面。当前证据为本机隔离数据库、真实PostgreSQL竞争与本地Prometheus；尚未部署生产，也不代表H01在M5的全部能力及通知链路已经验收。

## 实现与效果

| 内容 | 实现方法 | 呈现及可达到的效果 |
| --- | --- | --- |
| 请求链路 | 控制面与媒体网关使用ASGI观察器生成服务端32位请求编号，返回X-Request-ID；忽略客户端自填编号 | JSON日志记录注册路由模板、方法、状态和耗时；未知500返回安全提示及支持编号。请求参数、具体分享token路径、IP、Cookie、鉴权头、正文及异常原文不入日志 |
| Job关联 | 新Job在同一事务保存首个HTTP请求来源；后台子Job只在同空间/项目内继承父来源 | 内部诊断从Job定位原请求、空间、项目、工作流、版本、批次轮次、产物和账务元数据；请求编号不替代Job、远端逻辑操作或Idempotency-Key |
| 已提交状态日志 | SQLAlchemy监听flush后暂存快照，外层commit后才输出；保存点提交合并、回滚丢弃，保留外层最初状态 | 不把回滚状态或未提交的中间状态当成成功。日志输出失败不改变已提交业务结果；原业务完成事务与执行代次保持原规则 |
| Worker存活 | CLI poller运行期间由独立线程每30秒提交一次实例心跳，退出记录stopped_at | 长时间外部IO不被当成心跳消失；实例存活、标识冲突和工作流租约过期分别统计，不自动重做远端任务 |
| 队列年龄 | 按可执行类型、到期时间、策略标记投影；单列首次未执行任务的创建年龄 | 避免容量等待不断刷新时间而掩盖积压，也不把未来重试期限和批次汇总父任务当作可执行队列。实际依赖/容量准入仍以调度器为准 |
| 远端与费用 | 统计未确认操作、未知提交的起始年龄、待对账预占及未解决的冲突/无效费用证据 | 本地取消不被误认为远端结束，账单未知不被当成零费用或退款；没有任何自动签名、取消、重试、结算或供应商探测 |
| 供应商与回调 | 近24小时按最后变动归类的逻辑操作所关联任务，分别统计成功、失败、未判定；重复回调在原事务中累加独立持久计数 | 成功率无有效分母时为null；本地取消/未知不冒充供应商失败。同事件重复不新增计费操作；重复次数只覆盖0019接入后的已提交观察，旧版未保存次数不回填 |
| 告警 | 固定8类条件、持久状态及本次激活次数；CLI观察器只在触发/恢复时输出事件 | 连续相同异常不重复输出激活通知；恢复单列。Prometheus规则覆盖抓取失败、业务异常与持续5xx，并对同数据库的多API副本去重 |

诊断不读取Job完整payload、Provider响应正文、工作流输入/结果、审核意见或源素材地址。已知失败码使用固定允许表，未知码保留短哈希；Provider配置名和外部Job身份也使用标签/短哈希。指标不携租户/项目/任务标识，Provider标签最多16组及other，HTTP使用注册路由模板并限制内存系列数量。内部Job诊断包含金额字符串等必要元数据，只允许平台运维鉴权读取。

## 内部边界与指标协议

`GET /internal/operations`返回聚合快照，`GET /internal/operations/jobs/{id}`返回有上限的Job诊断；两者复用现有`X-Internal-API-Key`内部运维边界，不使用个人会话或租户头授权。内部路由不进入公开OpenAPI和M1私有edge转发清单。

`GET /metrics`单独使用`METRICS_API_SECRET`及`X-Metrics-Key`，该抓取凭据不能通过内部执行/诊断鉴权。session/production未配置时拒绝抓取；仅未设置抓取密钥的非生产development_header模式保留本机无鉴权。响应不缓存，使用显式`text/plain; version=0.0.4; charset=utf-8`及LF字节。

M2将旧版表示当前数量的gauge `video_ai_jobs_total`改为`video_ai_jobs`，防止被当作累计计数；新增gauge也不使用保留的_count/_total后缀。M1仍运行旧镜像/指标名，上线M2时应同步更新现有采集凭据和面板表达式。这是内部监控契约变更，不改Job/费用状态。已有retryable、policy_blocked、queued_retry指标继续保留。

新指标包括：`video_ai_jobs_by_family`、`video_ai_queue_due`、`video_ai_queue_oldest_due_seconds`、`video_ai_queue_oldest_unsubmitted_seconds`、`video_ai_remote_unresolved_jobs`、`video_ai_remote_unknown_jobs`及年龄、`video_ai_workers_live/stale/identity_collisions/expired_workflows`、`video_ai_billing_pending_jobs/oldest_pending_seconds/unresolved_evidence`、`video_ai_callback_duplicates_total`、`video_ai_provider_terminal_jobs_24h`、`video_ai_alert_active`。HTTP请求累计与延迟直方图来自当前API进程，重启会重置；DB投影在多个副本重复，不应跨副本相加。快照的多条查询不构成业务一致性检查点，不能用作费用决策或调度准入。

## 告警与配置

| code | 初始触发条件 |
| --- | --- |
| queue_aging | 到期等待或首次未执行年龄达到300秒 |
| workers_unobserved | 存在到期候选但没有120秒内的新鲜Worker心跳 |
| worker_identity_collision | 多个新鲜实例使用相同Worker标识 |
| workflow_lease_expired | running工作流租约到期 |
| remote_outcome_unknown | submitting/unknown逻辑操作或未知包达到900秒 |
| billing_reconciliation_aging | 待对账预占超过3600秒 |
| billing_adjustment_pending | 存在未处理的adjustment_pending或invalid_amount证据 |
| provider_failure_ratio | 近24小时至少5个已判定关联任务且失败比例不低于50% |

四个时间阈值见`.env.example`的`OBSERVATION_*`，允许90秒至7天；每份快照返回实际阈值。它们是初始运行提示，供应商比率不是供应商SLA，也不是API请求计费次数。H02/H05的真实基线尚未形成，不据此承诺产品成功率或速度。

迁移0019并配置应用环境后可执行（命令继承服务环境，不在命令行中粘贴密钥）：

```sh
python -m app.cli ops-report --json
python -m app.cli ops-report --job-id <原JobID> --json
python -m app.cli ops-check --json
python -m app.cli ops-check --watch --interval 60
```

ops-report只读；不会自动迁移目标库。ops-check只更新自己的固定告警状态/互斥记录，不修改Job、预占、结算、额度或供应商操作。单次检查有活动告警返回2，无告警返回0，读取/评估失败返回1；watch每轮继续观察。JSON模式每轮输出一个包含快照/转换的JSON对象，默认模式输出JSON事件行。监控进程应由平台服务管理器独立托管，不能只在排队Worker自己停机后期待它检测自己。

生产poller仍使用`process-jobs --watch`，自动带独立心跳；一个WORKFLOW_WORKER_ID对应一个副本。心跳不是成功证明，过期租约、未确认费用仍需处理。内部HTTP执行路径、未来Temporal等运行时须各自接入实际存活信号；未出现心跳只表示未观察到，不保证进程已退出。

## Prometheus与日志发布准备

`infra/observability/prometheus.example.yml`使用独立只读抓取密钥文件，不把执行密钥交给采集端。`video-ai-alerts.yml`包含3条规则，`alerts.test.yml`覆盖不可抓取、跨副本去重与恢复、持续5xx、探针错误排除4组行为。HTTP阈值是至少20次/5分钟时5xx超过5%并持续5分钟；业务与抓取规则持续2分钟。规则和API进程当前可执行，正式环境的采集地址、密钥文件、持久卷和容量需随M2发布配置。

本批用官方Prometheus 3.13.4镜像、摘要`sha256:87861b8cf91579109319ebc300f3f1060e6da9c05d6ae8ad15a20c879e84e32e`检查语法、行为及实际指标字节；CI固定同一镜像摘要，promtool执行禁用网络。模板未配置Alertmanager、邮箱、微信或其他外部接收人，不产生消息或云服务开通。正式通知接收与故障演练仍属M5待交付部分。

依据[官方配置说明](https://prometheus.io/docs/prometheus/latest/configuration/configuration/)的HTTP header文件及[告警规则说明](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)编写，版本来源为[官方下载页](https://prometheus.io/download/)。

Docker默认禁用Uvicorn原始access log，由安全JSON事件替代；API/Worker和独立网关Compose日志使用`max-size=10m`、`max-file=3`轮转。手动启动也应带`--no-access-log`。反向代理不得重新记录带签名/分享token的具体URL、参数或鉴权头。没有新增SLS应用日志采集，原授权OSS访问日志范围未变。心跳/来源和告警记录的最终保留策略需随H03/H04完成，当前不自动删除业务或诊断历史。

## 验收边界

本机与离线证据在`data/testing/m2-observability/`；进度中的实际日志分别记录，不能合并为未经运行的全量PASS。本批无真实模型、存储或计费请求；独立空队列Worker证明心跳更新及退出记录，合成的取消任务/未知操作/待对账状态用于证明诊断与告警不推定退款。最终仍需生产部署、持续运行、真实故障/费用定位与外部通知验收，完整M2和M5保持未完成。

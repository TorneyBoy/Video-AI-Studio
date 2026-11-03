# 生产上线 Runbook 与验收清单

本 Runbook 适用于 Video AI Studio 的第一版生产控制面。它假定生产环境没有
GPU，也不在 API 或 Worker 中运行本地模型：视频、音频、动作复刻、人脸替换、
增强和渲染都通过已审核的外部 HTTP Provider 完成，输入和输出只使用外部对象
存储 URI。

## 上线前必须满足的边界

生产环境的四个开关必须保持如下值：

```dotenv
APP_ENVIRONMENT=production
PROVIDER_MODE=api_only
ASSET_STORAGE_MODE=external
AUTH_MODE=trusted_proxy
DEBUG=false
```

`development_mock`、`development_header`、`local` 和 `hybrid` 只用于本地开发或
迁移期间的隔离验证。它们不能作为真实成片服务的默认值。生产镜像不应挂载
`config/providers.example.json` 作为真实注册表；其中的 endpoint 和模型只是结构
示例。

## 必填配置与秘密注入

把 [`.env.example`](../.env.example) 复制到部署系统的变量模板中，秘密通过 Secret
Manager 注入，不要写入 Git、Provider JSON、Dockerfile、镜像层或日志。生产至少
需要完成下表中的项目：

| 配置 | 生产要求 | 验收方式 |
| --- | --- | --- |
| `APP_ENVIRONMENT` | `production`；启动时校验认证、PostgreSQL、存储证明与回调绑定 | 开发边界配置导致明确启动失败 |
| `INTERNAL_API_SECRET` | 仅内部维护调用需要；缺省关闭内部 HTTP 状态写接口 | 用户登录身份不能修改 Job 状态、租约、产物或租户额度 |
| `DATABASE_URL` | PostgreSQL 主库、TLS/网络策略和备份已配置 | `migrate` 后执行 `/readyz`，检查数据库备份 |
| `POSTGRES_PASSWORD` | 随机高强度值；不能使用 Compose 默认值 | 检查部署变量，不在日志中出现 |
| `AUTH_MODE` | `trusted_proxy` | 启动时设置并通过身份边界测试 |
| `TRUSTED_PROXY_SHARED_SECRET` | 与身份网关独立共享的随机值 | 网关签名请求成功，伪造请求失败 |
| `STORAGE_ATTESTATION_SECRET` | 与对象存储网关共享的随机值 | 完成上传证明验收通过 |
| `PROVIDER_REGISTRY_PATH` | 只读挂载的实际注册表 | `provider-preflight` PASS |
| Provider `headers_env` | 每个 API key 均由 Secret Manager 注入 | 预检显示 configured，不显示值 |
| `PROVIDER_WEBHOOK_SECRET` | 使用回调时必须配置；不用回调则关闭供应商回调 | 有效签名成功、无效签名返回 401 |
| `PROVIDER_WEBHOOK_REQUIRE_REQUEST_ID` | `true`；把回调绑定到 URL 中的 Job ID，并要求 `external_job_id` | 缺少或错配 `request_id`/`external_job_id` 被拒绝 |
| `ASSET_STORAGE_MODE` | `external` | 平台不代理媒体字节 |
| `DEBUG` | `false` | 配置审查和日志审查 |

Compose 文件中的默认密码和开发认证值仅为本地启动便利；把 Compose 部署到任何
可访问网络前必须显式覆盖它们。生产对象存储不应启用 Compose 的 MinIO profile，
而应使用组织批准的对象存储和签名网关。

## 发布顺序

### 1. 构建、扫描和数据库迁移

使用固定镜像 digest 构建 API 和 Worker，先完成依赖和镜像扫描，再把注册表作为
只读配置挂载。扩容 API/Worker 前只运行一次迁移：

```bash
docker compose -f infra/docker-compose.yml config --quiet
docker compose -f infra/docker-compose.yml run --rm api python -m app.cli migrate
```

迁移是向前兼容的新增字段/表；本项目不提供自动降级迁移。回滚应用版本时保留
数据库结构，只有在经过备份恢复演练后才回滚数据库。

### 1.1 网络隔离的统一质量门

Pull Request 和镜像发布 admission 必须先运行仓库统一 gate：

```bash
# 快速门：静态检查、Provider 边界契约、前端语法和小型测试集
python scripts/quality_gate.py fast

# 集成门：网络隔离 Provider acceptance、smoke dry-run 和完整测试集
python scripts/quality_gate.py integration

# 发布前检查静态项和完整测试集，避免重复执行快速子集
python scripts/quality_gate.py all
```

Gate 从脚本自身位置解析仓库路径，因此可以从任意工作目录运行；脚本内部 fixture
的相对反斜杠路径会先规范化，覆盖 Windows PowerShell/Git Bash 的路径分隔符差异。它在临时目录中使用 SQLite
和 storage 根目录，并强制 `PROVIDER_MODE=api_only`、`ASSET_STORAGE_MODE=external`、
`DEBUG=false`，不会继承本机 Provider Registry。Provider acceptance 只解析
`config/providers.contract.fixture.json`；Provider smoke 只对
`config/provider-smoke.example.json` 做 dry-run。两者都不会解析 DNS、打开 Provider
网络连接、执行模型或调用 `--execute`，因此不会产生供应商计费请求。

设置 `TEST_POSTGRES_URL` 后，完整测试集会在临时隔离 schema 中运行真实 PostgreSQL 竞争与迁移回归；没有该变量时这些用例明确跳过，不能视作并发验收通过。CI 提供独立 PostgreSQL 测试服务。

该 gate 的通过结果只证明控制面合同、配置结构、API-only 和外部存储边界有效，不能
替代供应商 staging 的真实可达性、质量或账单验收。真实验收必须由授权运维人员在
供应商 staging 环境显式执行 `provider-smoke --execute`，并记录远程 Artifact、用量和
成本；不得把 `--execute` 放入 CI。

### 2. 认证与租户边界

生产边缘网关必须在转发到 API 前：

1. 删除客户端传入的 `X-Tenant-ID` 和全部 `X-Auth-*` 头。
   同时删除 `X-Internal-API-Key`，内部维护流量走独立受控入口。
2. 根据已验证的登录主体映射已预置的 `tenant_id`。
3. 注入 `X-Auth-Principal`、`X-Auth-Tenant-ID`、`X-Auth-Issued-At` 和
   `X-Auth-Signature`。
4. 使用 `HMAC-SHA256(TRUSTED_PROXY_SHARED_SECRET,
   "issued_at\\nprincipal\\ntenant_id")` 生成十六进制签名。
5. 仅允许 API 从内网或网关网络访问，避免客户端绕过网关直连容器端口。

部署验收至少覆盖：没有身份声明返回 401；客户端伪造 `X-Tenant-ID` 不会改变租户；
错误签名和过期时间戳被拒绝；跨租户 URL 资源返回 404/403；合法主体只能看到自己
租户的项目、素材、Job、Artifact 和预算。

租户 provisioning 不通过公开的业务 API 完成。先由受保护的控制面创建租户和预算，
再开放对应主体访问。

### 3. 外部对象存储与上传证明

对象存储网关负责生成短期 `upload_url`、固定 `object_uri`、校验对象大小和 SHA-256，
并在上传完成后签发证明。平台不会 HEAD/GET、下载、探测媒体或执行本地 FFmpeg。

开启 `STORAGE_ATTESTATION_SECRET` 后，以下三类请求必须携带证明：

- `POST /assets/register-remote`；
- `POST /assets/remote-upload-session` 的 `register_only=true`；
- `POST /assets/{asset_id}/remote-upload-complete`。

证明采用 `storage-attestation-v1` canonical JSON（键名字典序、无空白、UTF-8），
绑定 `asset_id`、`object_uri`、`size_bytes`、`sha256`、`nonce` 和短期
`expires_at`，签名头为 `sha256=<HMAC-SHA256 hex>`。网关必须为每次完成生成唯一
nonce；平台会拒绝过期、跨素材、跨 URI、篡改大小/hash 和重复证明。上传未完成的
素材不能进入分析、自动剪辑、Avatar、视频生成或增强作业。

验收一个小视频和一个小音频：直传成功后完成证明可重复提交而不重复改变状态；改动
一个字节、替换 `asset_id`、重放过期 nonce 或提交其他租户素材都必须失败。对象生命周期、
删除请求和 CDN 缓存清理由对象存储网关负责，并应能按平台 Asset ID 追踪。

### 4. Provider 注册、模型和许可

把真实 Provider 网关地址、固定模型版本、区域、商业许可快照、成本和轮询参数写入
部署专用注册表。注册表中只保存环境变量名，不保存密钥。发布 admission 阶段运行：

```bash
PROVIDER_REGISTRY_PATH=/etc/video-ai/providers.json \
python -m app.cli provider-preflight \
  --path "$PROVIDER_REGISTRY_PATH" --json > provider-preflight.json
```

预检是网络隔离检查，不会请求 Provider，也不会证明供应商真实可用。它必须通过
HTTPS、Secret 引用、固定 `model_id`/`version`、`commercial_allowed=true`、成本、
能力覆盖和异步 `poll_path` 检查；报告中不得出现密钥值。供应商 endpoint 的真实
网络 smoke test 应在其允许的 staging 网关执行，使用最小片段、明确预算和专用测试
租户，再核对返回的 `usage`、`cost`、远程 Artifact URI、模型快照和账单。

第一批上线建议按能力分阶段放量：`media_analysis`、`video_editing`、`video_generation`、`voice_tts`、
`voice_clone`、`voice_conversion`、`motion_transfer`、`face_transform`、`lip_sync`
和 `enhancement`。某能力未通过预检时，该能力应保持不可用并返回可审计的
`capability_unavailable`，不能回退到本地模型。

### 5. Provider 回调与异步任务

如果使用平台的异步回调入口：

```text
POST /api/v1/provider-callbacks/{job_id}
X-Provider-Signature: sha256=<HMAC-SHA256(raw-body)>
X-Provider-Timestamp: <可选 Unix 秒>
X-Provider-Name: <provider name>
```

设置 `PROVIDER_WEBHOOK_SECRET` 后，签名原文为 raw body；带时间戳时为
`<timestamp>.<raw-body>`，并受 `PROVIDER_WEBHOOK_MAX_AGE_SECONDS` 限制。回调必须
携带 `event_id`、`status`；生产设置 `PROVIDER_WEBHOOK_REQUIRE_REQUEST_ID=true`，并且必须携带匹配的 `request_id`、
`external_job_id` 和远程 `artifact_uri`；平台在该严格模式下会在进入 Job 状态机前拒绝缺少远程任务 ID 的事件。重复 `event_id` 幂等，终态 Job 不接受
后续冲突状态。

注册表的 `callback_secret_env` 是 HTTP Provider 适配器解析其自身回调时使用的
Provider 级密钥引用；平台公开回调路由使用全局 `PROVIDER_WEBHOOK_SECRET`。两者若
同时启用，必须在网关设计中明确对应关系，不能因为注册表存在引用就认为公开回调
已完成鉴权。未配置全局 secret 时回调入口返回 503。

### 6. 启动、健康和小流量放量

先启动一个 API 和一个 Worker，确认数据库迁移后再扩容：

```bash
docker compose -f infra/docker-compose.yml up -d api worker
curl -fsS https://<api-host>/healthz
curl -fsS https://<api-host>/readyz
```

`/healthz` 只证明进程存活；`/readyz` 必须同时返回数据库和存储检查为 `ok`。生产
负载均衡只把流量转给 `/readyz` 为 200 的 API。Worker 以 `process-jobs --watch` 运行，
重启后由持久化 WorkflowRun、Provider task ID 和幂等键继续处理，不应重新提交已经
接受的远程任务。

先用一个测试租户完成：上传 → 分析 → 自动剪辑 → 预览/导出，以及 TTS、换脸/动作
复刻、视频生成、增强中的至少一条真实 Provider 链路。确认所有结果都是远程 URI，
平台没有本地媒体输出，并核对预算预占、结算、重复回调和撤销授权。

## 监控与告警

把 `/metrics` 只暴露给内部 Prometheus 网络，不对公网开放。M2使用独立`METRICS_API_SECRET`/`X-Metrics-Key`，不将内部执行密钥交给采集端；session/production未配置抓取密钥会拒绝访问。M1旧gauge名`video_ai_jobs_total`在M2改为`video_ai_jobs`，上线时同步更新面板及采集凭据。当前低基数指标为：

| 指标 | 告警建议 |
| --- | --- |
| `video_ai_jobs{status="queued"}` | 持续增长且 Worker 心跳正常时告警 |
| `video_ai_jobs{status="failed"}` | 按发布前基线观察突增 |
| `video_ai_jobs_retryable` | 连续上升表示 Provider/网络异常 |
| `video_ai_jobs_queued_retry` | 长时间非零表示重试或租约恢复异常 |
| `video_ai_jobs_policy_blocked` | 突增表示预算、许可或授权策略阻断 |

同时采集反向代理的 `/healthz`、`/readyz`、Provider callback 的 401/409/5xx、Worker
进程重启和数据库连接池错误。日志只保留 Job ID、Provider 名、能力、失败码、重试
状态和 request ID；禁止记录 Authorization、共享 secret、原始视频、签名原文和完整
任务 payload。按租户查看明细使用 `GET /api/v1/jobs/stats`，不要把租户 ID 放入全局
Prometheus label。

## 回滚与事故处理

1. Provider 质量、延迟或成本异常：先把对应注册表项 `enabled=false` 或切换到已验收
   的上一版本，重新执行预检，再滚动重启 Worker；不要删除正在运行的远程任务记录。
2. API 发布异常：停止新 Worker、保留数据库和对象 URI，回滚到上一镜像，重新执行
   `/readyz` 和一个测试租户闭环。数据库迁移不自动回滚。
3. 认证或签名密钥泄露：立即在 Secret Manager 轮换网关、对象存储和 Provider secret，
   使旧 key 失效，检查审计记录，并暂停对应入口直到 smoke test 通过。
4. 对象存储或 Provider 输出被误删：依据 Artifact provenance 中的远程 URI、Provider
   job ID 和账务记录从供应商恢复；控制面不保留媒体副本。

## 发布完成判定

只有下列项目全部通过，才将测试租户外的流量切到生产：

- [ ] 固定镜像、依赖扫描和 PostgreSQL 备份验证通过。
- [ ] `AUTH_MODE=trusted_proxy`，身份网关已剥离客户端身份头。
- [ ] `ASSET_STORAGE_MODE=external`，对象存储完成证明和跨租户测试通过。
- [ ] `PROVIDER_MODE=api_only`，注册表预检 PASS，模型/许可快照已审核。
- [ ] 至少一条真实 Provider staging smoke test 成功，成本和远程 Artifact 可对账。
- [ ] `/healthz`、`/readyz`、内部 `/metrics` 和告警规则已验证。
- [ ] 上传、分析、自动剪辑、导出和至少一条身份/声音任务闭环通过。
- [ ] 重复请求、重复回调、过期签名、预算超限和撤销授权测试通过。
- [ ] 回滚镜像、Secret 轮换、对象删除和数据库恢复演练有记录。



## M2 数据库Worker容量配置（尚未部署）

所有数据库poller必须使用相同的DISPATCH_GLOBAL_LIMIT、DISPATCH_TENANT_LIMIT、DISPATCH_PROJECT_LIMIT、DISPATCH_BATCH_LIMIT；默认分别8/4/3/2，合法范围1–1024。这些是应用上限，不是放大供应商配额；每个供应商/能力的max_concurrent会另外限制真实提交。M1目录分析与渲染各为1，原验收总调用计数仍独立受网关限制，不得以调度配置重置或增加。

先执行0017再部署API及全部Worker。轮次为持久数据，重启不清空公平性；仅在准入事务中锁clock，提交后才执行媒体/网络。执行路径不反向锁clock。未知远端操作继续占容量，不通过过期定时器当成完成；由可信终态回调、成功查询或明确对账处理解决。取消不会自动停止远端或退款。真实收费提交前重新检查预算，已提交原远端可继续查询，即使预算不足。

调低应用/供应商上限不取消已在运行的工作，新项等到容量允许再进；同一远端的查询可继续。提高应用上限也不能越过供应商max_concurrent、账务上限或M1验收计数。数据库门禁只覆盖已接入的poller/Provider调用路径；以后启用Temporal等运行时也必须沿用相同门禁，不能直接新增一条绕过的执行通道。发布前仍需真实供应商和H05容量/费用验收。

### 批量交付上线前提（0018，尚未生产部署）

批量交付见[运行契约](batch-delivery.md)。先迁移0018并升级全部API/Worker，独立媒体网关同步包含ZIP实现。应用`DELIVERY_PACKAGE_ENABLED=false`、网关`GATEWAY_PACKAGE_ENABLED=false`默认关闭；仅有一侧开启不能完成真实打包。默认100MiB视频、512KiB文本、100视频和网关6个不同包ID配额；同ID恢复不重置配额。`infra/gateway.compose.yml`已有`/app/data`网关卷，`GATEWAY_PACKAGE_TEMP_ROOT=./data/package-work`只在该网关存放临时文件，正常完成或失败会清理本次临时目录。

使用现有租户归档目录读取和包目录写入权限，无需新增云角色或长期AccessKey。启用真实IO前核对累计USD10剩余额度、网关临时磁盘及包保留/回收安排；不能重置M1分析3/6、渲染6/6的原验收计数。包签名最长300秒并受最早归档/包到期限制；到期拒绝领取不代表已从OSS删除。相同清单原远端未知时先恢复/核对原ID，取消与不可恢复终态继续发送网关取消标记，迟到结果不恢复本地交付资格。本批仅离线和本机元数据QA，未启用云端打包或更改DNS。


### M2基本观测发布检查（0019，尚未生产部署）

[基本观测及告警说明](operational-observability.md)包含接口、阈值、字段边界和本机证据。迁移0019后同步升级API/全部poller：API生成服务端请求编号并事务性关联新Job，Worker独立30秒心跳，未知远端/费用继续保留原身份。内部诊断`/internal/operations`仍用平台运维鉴权，不对个人会话开放。抓取密钥只允许`/metrics`，不能用于执行或详细诊断。

`ops-report --json`或`ops-report --job-id <JobID>`只读；`ops-check --watch --interval 60`维护固定告警状态并输出转换事件，不迁移数据库或改任务/账务。应由独立服务管理器托管，避免仅依赖Worker自检。配置模板与3条Prometheus规则在`infra/observability/`，CI已纳入固定摘要的promtool校验；现已在本机真实抓取和告警恢复演练，尚未在ECS安装采集器、配置持久化或对外通知接收人。无需新增RAM角色或云订阅。

Uvicorn手动启动带`--no-access-log`，API/Worker/网关Compose默认`max-size=10m`、`max-file=3`日志轮转。安全JSON日志不输出具体URL/查询参数、Cookie/鉴权头、输入/输出正文或异常原文；反向代理也不能额外记下这些字段。全局指标不含租户/项目/任务ID，DB全局投影跨API副本以max去重，HTTP进程指标按实例采集。心跳/来源/告警历史的保留和备份恢复仍需H03/H04正式验收；本批没有新增SLS应用日志采集或修改原OSS日志范围。

### 元数据隔离恢复（0020，尚未生产部署）

[备份与恢复手册](database-recovery.md)提供PG16加密快照、独立密钥、配置备份、回执和真实新库恢复命令。执行前明确恢复管理员维护库、新目标名、私有scratch及源码之外的备份/密钥保管位置。源库不覆盖；恢复结果先与原快照核对，再只写入quarantined控制记录。迁移0020保留恢复状态。任何测试都不能把M1网关分析/渲染计数恢复成较早值或重置累计费用。

隔离库只能做核对：新API的个人入口返回503，readyz为503，Worker/回调不执行。没有自动激活或回滚业务表。正式接管必须先证明源实例及旧Worker停写和外部执行围栏有效，再核对备份后的远端任务/账单、恢复对象媒体和网关状态，取得目标独占执行权。旧二进制不识别门禁，必须保持停止；不能通过恢复旧镜像自动实现兼容回滚。当前仅本机新建临时库通过所列演练，未备份/恢复生产库、改DNS或部署0008–0020。


### M2源码与显式迁移（尚未部署生产）

[发布准备](release-preparation.md)列出完整候选包、外部SHA/内容ID、校验提取、镜像来源标签和只读预检入口。旧M1打包脚本已停用。生产API/Worker启动不自动执行DDL，必须先在停止写入的维护窗口显式迁移；缺版本或未知/更新版本会拒绝。M2 Compose按profile隔离迁移与执行，要求明确的既有卷/数据库，不创建空网关状态或重置费用计数。源码/镜像构建和本机合成旧库升级通过均不等于生产接管；当前M2真实云/微信与H04回滚等未验收，禁止把旧M1镜像直接视为0020后的可用回滚方案。


### 旧空间个人账号与只读源（0021，未部署生产）

[运维账号准备](legacy-personal-adoption.md)仅对选定隔离库中新建个人所有者，沿用原Tenant和预算。0021不自动分配用户；生成密码只写私有文件，apply按固定请求/数据库实例绑定，不提供原有账号合并或密码重置功能，重复请求不再次变更凭据。安装入口`video-ai-legacy-owner`供一次性运维容器使用，常规API/Worker继续非root，秘密目录/数据库管理员权限不交给个人客户端。

新API/Worker识别只读事务并停止执行/就绪；只读默认值和维护状态可被数据库管理员或显式SQL改变，不能替代源进程停写、连接/存储硬围栏和备份后写入核对。当前无自动激活命令，服务器连接仍需重新核实；未运行本批生产操作。

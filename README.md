# Video AI Studio

使用外部托管媒体 API 的视频生产工作台：网页与微信小程序共享项目、素材、分镜、Canonical Timeline、任务、预算、审核和交付。

下一阶段的模块实现方法、目标效果、界面呈现、验收标准和交付顺序见 [详细设计与交付方案](docs/detailed-design.md)。该方案区分现有实现与待建设计，不代表所有目标能力已经完成。

参考流程与产品页面后的双端功能、微信小程序独立工作台、批量生产及实施增量见 [综合产品方案](docs/integrated-product-design.md)。小程序与网页用户功能对等，按终端调整交互，并同步验收。

2026-11-04 更新的[项目开发规划](docs/development-roadmap.md)保留 A–H 共 59 项任务和 M0–M5，按 R01–R12 安排恢复增量、词级分析、双端编辑、真实渲染与批量、配音、参考分镜、缺镜头生成和生产交付。开源项目的具体采用方式、源码入口和许可见[参考项目实施映射](docs/open-source-reference-plan.md)；实施约束见[项目目标与执行简报](docs/project-goal.md)。

当前实施状态与阶段验收记录见 [执行进度](docs/development-progress.md)；补镜头、人物/纯脸替换和真实样例边界见 [AI制作说明](docs/ai-production.md)。

已发现问题、关闭条件及六个工作包的对应关系见[质量与多场景问题清单](docs/quality-issue-register.md)。

开发环境建议使用 Python 3.13：先创建 `.venv`，再执行 `python -m pip install -c requirements-dev.lock -e '.[dev,gateway,ice,fc]'`。Windows 可使用 `.venv/Scripts/python.exe`，其他平台使用 `.venv/bin/python`。`npm run dev` 自动优先使用该虚拟环境，在 `http://127.0.0.1:4173` 同时提供工作台和 `/api/v1`。仅三个工作台静态文件可公开访问，仓库目录不会被作为文件服务器暴露。

仓库记录中 M0 已验收、M1 指定样本的受限真实成片链路已通过；M2 双端与批量源码已广泛接入、M3 参考分镜部分实现，整体及新增媒体效果/微信真机验收仍待完成。2026-11-04 核对发现旧 ECS 项目已退役，当前未重新部署；0008–0023 没有本轮部署证明。直传与核验见[存储网关](docs/storage-gateway.md)，真实分析与外部镜头函数见[云端分析接入](docs/hosted-analysis.md)；实际环境和证据以[执行进度](docs/development-progress.md)及实施时核对结果为准。

当前已具备：上传会话与开发环境内容上传、远端素材 URI 注册、素材分析作业、时间线 schema 校验、Draft/Final 渲染作业、可解释自动剪辑计划器、自动剪辑 Timeline 编译与预览/导出任务、Provider Gateway、持久化 JobStep/幂等键/状态机、声音/Avatar 授权资产和撤销接口、Artifact provenance manifest。

Provider 接入层提供不绑定厂商的 `HTTPProvider`，可把已有商业 AI 模型 API 接入同一 Job/Artifact/ModelSnapshot 合约。默认 `PROVIDER_MODE=api_only`，不会启动本地 deterministic provider，也不会调用本地 command provider；开发者可显式设置 `PROVIDER_MODE=development_mock` 运行契约测试，mock 只输出 simulated manifest。

真实 Provider 通过 `PROVIDER_REGISTRY_PATH` 或 `PROVIDER_REGISTRY_JSON` 注册，模型版本会经过许可证、区域、商业使用、权重 SHA-256 和运行时快照校验；配置格式见 [Provider 接入](docs/providers.md) 和 [模型注册策略](docs/model-registry.md)。 托管能力模板见 [Provider 注册模板](docs/provider-templates.md)，示例配置位于 `config/providers.example.json`。

上线前可运行网络隔离的 Provider 注册预检；它只检查 HTTPS、Secret 引用、模型许可、能力覆盖和成本字段，不会调用真实外部 API：[Provider 上线预检](docs/provider-preflight.md)。

完整的生产环境变量、可信身份代理、对象存储完成证明、Provider 回调、监控、灰度验收和回滚步骤见 [生产上线 Runbook](docs/production-runbook.md)。

分析、Provider 和渲染 Worker 已自动为每个 Job 创建并 Claim `WorkflowRun`，将租约、重试、恢复和事件审计写入数据库。可用 `recover-workflows` 扫描进程崩溃后的过期租约，也可选装 Temporal 适配层；接口和部署约定见 [持久化工作流](docs/workflows.md)。

Draft 渲染器对单视频轨保留兼容路径，并支持连续视频/图片片段、字幕、图片/视频叠加、xfade 转场和单音轨多片段混音；暂不支持的效果仍会在 FFmpeg 启动前返回明确质量错误。能力边界见 [渲染能力契约](docs/rendering-capabilities.md)。

## Run locally

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # optional; SQLite is the default
python -m app.cli init-db
uvicorn app.main:app --reload
```

OpenAPI is available at `http://localhost:8000/docs` and `/openapi.json`; liveness is `/healthz` and the database/storage readiness probe is `/readyz`.

运维观测：租户可通过 `GET /api/v1/jobs/stats` 查看状态、类型、失败码、可重试和策略阻断聚合，可选 `project_id` 进一步筛选。`/metrics` 提供不含租户 ID 或任务 payload 的低基数 Prometheus 文本指标；`/readyz` 负责数据库与存储可用性检查。

托管 Provider 的租户级月度/单任务预算、预占、结算和账务审计见
[预算与用量](docs/budgets.md)。预算 API 使用整数微单位，Provider 重试和重复
Callback 对同一 Job 幂等结算。

控制面 Worker（编排素材分析、自动剪辑 Timeline 编译、Provider 任务和 Draft/Final 渲染作业；`api_only` 下媒体计算全部由外部 API 完成）：

```bash
python -m app.cli process-jobs --limit 10
# long-running local worker
python -m app.cli process-jobs --watch --limit 10 --interval 2
```

The worker processes media analysis, Provider jobs, and Draft/Final renders in
one poll and persists a WorkflowRun lifecycle for each claimed Job. `--watch`
keeps the process alive for a service manager; without it, the command performs
one poll and exits, which is useful for cron or CI.

Provider 任务默认要求外部 HTTP 模型 API 已通过 `PROVIDER_REGISTRY_PATH` 或
`PROVIDER_REGISTRY_JSON` 配置。未配置时任务会以明确的
`capability_unavailable` 失败并保留审计信息；不会回退到本地模型或本地
deterministic 结果。仅在本地契约测试时使用：

`api_only` 模式下，素材也必须使用 Provider 可访问的 `https://`、`s3://`、
`gs://`、`oss://` 或 Azure 对象 URI；Worker 不会把本地文件上传、下载或交给
FFmpeg。`POST /assets` 可直接注册这类 URI，本地 `/assets/{id}/content` 仅用于
开发环境上传流程。

生产上传建议使用 `POST /api/v1/assets/remote-upload-session`：由外部对象存储
网关提供 presigned `upload_url` 和稳定 `object_uri`，客户端直传后调用
`POST /api/v1/assets/{asset_id}/remote-upload-complete`。平台只保存 URI、大小、
可选 SHA-256 和 ETag 声明，不代理文件字节，也不会向对象存储发起下载或媒体探测。
已有对象可以用 `POST /api/v1/assets/register-remote` 直接登记。配置
`STORAGE_ATTESTATION_SECRET` 后，外部网关必须对完成声明签发绑定 asset/object/完整性
字段的短期 HMAC 证明；`register-remote` 和 `register_only` 需要客户端预先提供
`asset_id`，普通直传 session 使用 API 返回的 ID。

```bash
PROVIDER_MODE=development_mock python -m app.cli process-jobs --limit 10
```

Local development uses the `X-Tenant-ID` header for tenant-scoped endpoints. Configure
`AUTH_MODE=trusted_proxy` in production: the edge gateway must inject signed
`X-Auth-Principal`, `X-Auth-Tenant-ID`, `X-Auth-Issued-At`, and `X-Auth-Signature`
claims, and the API rejects client supplied `X-Tenant-ID`. Tenant records must be
pre-provisioned by the protected control plane before trusted requests arrive.

Create a tenant in development first:

```bash
curl -X POST http://localhost:8000/api/v1/tenants \
  -H 'Content-Type: application/json' \
  -d '{"slug":"acme","name":"Acme Studio"}'
```

Then send the returned tenant `id` as `X-Tenant-ID` when creating projects and related resources. The service enforces tenant ownership for all project, asset, template, timeline, job, artifact, and consent queries.

For PostgreSQL, set e.g. `DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/video_ai_studio`; the same models and API are used.

## Docker Compose

The Compose stack starts a Postgres database, API, and durable worker. The
default host ports are API `8000` and Postgres `5433` so a local MySQL or
Postgres service on `5432` is not overwritten. It also declares an optional
MinIO profile for the future object-storage adapter.

```bash
cp .env.example .env
docker compose -f infra/docker-compose.yml up --build
# optional object storage
docker compose -f infra/docker-compose.yml --profile object-storage up --build
```

The API container exposes `/readyz`; Compose waits for Postgres before starting
API and worker. Generated media is kept in the `media_storage` named volume.
Set `POSTGRES_PASSWORD`, `MINIO_ROOT_PASSWORD`, and application ports in `.env`
before using the stack outside a development machine. The complete variable
template is [.env.example](.env.example).

前端控制台：

```bash
npm run dev
# http://localhost:4173
```

## Quality checks

```bash
# deterministic, network-free fast gate
python scripts/quality_gate.py fast

# full integration contracts plus static Provider acceptance/dry-run
python scripts/quality_gate.py integration

# both gates (used by CI)
python scripts/quality_gate.py all
```

The gate always runs with `PROVIDER_MODE=api_only` and
`ASSET_STORAGE_MODE=external`, uses temporary SQLite/storage paths, and never
opts into a real Provider health probe or smoke request. A staging request is
an explicit operator action documented in [Provider Smoke](docs/provider-smoke.md).


### M2 批次调度增量（未部署生产）

数据库Worker已接入持久的空间/项目/批次轮转、分析依赖/退避前置过滤、应用执行上限及供应商max_concurrent门禁；首次收费提交重新核对现有预占与当前预算，原远端查询不另建费用操作。默认应用上限为全局8、空间4、项目3、批次2，统一由`DISPATCH_*_LIMIT`配置。详情见[客户端契约](docs/client-contract.md)、[运行手册](docs/production-runbook.md)和[推进进度](docs/development-progress.md)。需要0017迁移；生产0008–0017尚未部署，真实容量和微信验收未完成。

### H04 元数据恢复工具（未部署生产）

`python scripts/database_recovery.py` 提供PG16加密备份、校验、新库隔离恢复及备份密钥轮换；需要`operations` extra及PG16客户端/显式既有数据库容器。0020增加数据库实例身份和恢复门禁，防止恢复库启动任务或接收异库执行结果。真实本机演练及边界见[恢复手册](docs/database-recovery.md)与[执行进度](docs/development-progress.md)。工具不激活恢复库；生产/异地、媒体与网关状态恢复、实例接管及发布回滚仍待验收。

### M2 源码与升级准备（未部署生产）

`scripts/package_release.py`统一打包/校验/提取双端、服务端及回归源码，`scripts/build_release_image.py`从校验包构建带源码标签的镜像，`scripts/release_preflight.py`只读检查迁移计划及私有配置。生产API/Worker启动不自动执行DDL，必须先显式迁移；未知/更新版本会拒绝。`.env.m2.example`和`infra/m2.compose.yml`提供不含运行凭据、需既有数据库/卷的模板，执行profile默认不启动。详见[发布准备手册](docs/release-preparation.md)，构建与打包不代表M2、微信或生产验收通过。

0021增加旧空间绑定审计；`video-ai-legacy-owner`或`scripts/adopt_legacy_tenant.py`在隔离库中准备指定空间的新个人账号，保留原项目/费用/对象，重复请求不改密码或发放Session。详见[旧空间账号准备](docs/legacy-personal-adoption.md)。本批同时让新API/Worker拒绝在只读源事务上执行，但只读标记不等于独占接管证明，真实空间尚未分配。

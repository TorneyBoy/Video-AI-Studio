# P0 API 草案

长耗时作业创建（素材分析、渲染、自动剪辑、TTS、Avatar、视频生成、增强）支持 `Idempotency-Key`；通用作业也可在 JSON 中传 `idempotency_key`。API 前缀为 `/api/v1`。

### 认证与租户边界

默认 `AUTH_MODE=development_header`，本地开发和现有契约测试通过 `X-Tenant-ID` 指定租户。生产必须设置 `AUTH_MODE=trusted_proxy` 和随机的 `TRUSTED_PROXY_SHARED_SECRET`。认证网关在转发前必须删除客户端传入的 `X-Tenant-ID`、`X-Auth-*` 头，再注入下面四个已验证并签名的声明：

- `X-Auth-Principal`：认证主体标识
- `X-Auth-Tenant-ID`：已映射的租户 ID，必须已预置在平台数据库中
- `X-Auth-Issued-At`：Unix 秒时间戳
- `X-Auth-Signature`：`HMAC-SHA256(secret, "issued_at\\nprincipal\\ntenant_id")` 的十六进制值

声明有效期由 `TRUSTED_PROXY_MAX_AGE_SECONDS` 控制，默认 300 秒。生产模式会拒绝任何客户端 `X-Tenant-ID`，缺少签名、签名错误、过期声明或 URL 中租户 ID 不匹配时分别返回 400/401/403。租户创建、全量租户列表和删除等控制面操作应通过受保护的租户 provisioning 流程完成；公共 API 不在 trusted proxy 模式暴露这些操作。

## 项目与素材

- `POST /api/v1/projects` 创建项目
- `GET /api/v1/projects` 列出当前租户项目
- `POST /api/v1/assets` 注册素材元数据
- `POST /api/v1/assets/register-remote` 注册已存在于对象存储的素材 URI
- `POST /api/v1/assets/remote-upload-session` 创建直传对象存储的 presigned upload intent
- `POST /api/v1/assets/{asset_id}/remote-upload-complete` 提交直传完成后的大小、hash、ETag 和（生产配置密钥时）对象存储网关签名证明
- `POST /api/v1/assets/{asset_id}/analyze` 创建外部 media-analysis Provider 素材分析作业
- `POST /api/v1/assets/upload-session` 创建开发环境可恢复上传会话；生产 `api_only` Worker 会拒绝其中的本地 URI，应改用外部对象存储直传接口

分析请求体可为空；需要时传入 `shot_analysis`、`transcribe`、`run_transcription`、`whisper_model`、`timeout_seconds` 和扩展 `options`。分析由 Worker 异步转发给已配置的 HTTP Provider，完成后将 Provider 返回的规范化结果写入 `Asset.metadata.analysis`，并创建一个远程 `analysis` Artifact。`api_only` 模式下 Asset URI 必须是 Provider 可访问的 HTTPS 或对象存储 URI；平台不加载本地 faster-whisper 或其他推理模型。

生产素材接入走外部对象存储：客户端先从自己的存储网关获得 `upload_url` 和稳定的 `object_uri`，再调用 `remote-upload-session`。平台只把 `object_uri` 写入 Asset，并把 `upload_url` 原样返回给客户端，不代理文件字节、不发起 HEAD/GET，也不运行媒体探测。客户端用 `PUT`/`POST` 直传后调用 `remote-upload-complete`，平台仅校验声明的大小和可选 SHA-256；存储网关负责实际对象存在性和完整性校验。已经上传的对象可以直接调用 `register-remote`，或在 upload session 中传 `register_only=true`。当配置 `STORAGE_ATTESTATION_SECRET` 时，`register-remote`、`register_only` 和 `remote-upload-complete` 都必须携带对象存储网关的短期 HMAC 证明；证明绑定 `asset_id`、`object_uri`、`size_bytes`、`sha256`、随机 `nonce` 和 `expires_at`，重复请求只能复用同一完成证明。对 `register-remote` 和 `register_only`，客户端必须先提供稳定的 `asset_id` 再让网关签名；普通直传 session 可以由平台生成 `asset_id`，完成证明在上传后绑定返回的 ID。证明的 canonical JSON 使用版本 `storage-attestation-v1`、按键名字典序、无空白序列化后以 UTF-8 编码，字段为 `asset_id`、`expires_at`（UTC epoch 秒）、`nonce`、`object_uri`、`sha256`（无 hash 时为空字符串）、`size_bytes`、`version`；签名为 `sha256=<HMAC-SHA256 hex>`。未配置密钥的开发环境保持原有客户端声明合同。支持的对象 URI scheme 为 `https`、`s3`、`gs`、`oss`、`az` 和 `azure`；HTTPS URL 不得指向 localhost、回环/私有/保留 IP，也不得携带内嵌账号密码；未完成的直传素材不能进入分析或自动剪辑队列。

## 模板与时间线

- `GET /api/v1/templates` 列出模板
- `POST /api/v1/templates` 创建模板
- `POST /api/v1/timelines` 创建时间线
- `POST /api/v1/timelines/{timeline_id}/validate` 校验时间线
- `POST /api/v1/timelines/{timeline_id}/render` 创建 Draft/Final 渲染作业

## 作业与 Artifact

- `POST /api/v1/jobs/auto-edit` 根据模板约束和素材分析创建可审阅的自动剪辑计划；缺少分析时会自动补排 `asset.analyze` 作业
- `GET /api/v1/jobs/{job_id}/timeline` 获取自动剪辑 Worker 编译出的 Canonical Timeline
- `POST /api/v1/jobs/{job_id}/preview` 为已完成自动剪辑计划创建 Draft 预览渲染作业
- `POST /api/v1/jobs/{job_id}/export` 为已完成自动剪辑计划创建 Standard/Final 导出渲染作业
- `POST /api/v1/jobs/voice` 创建受 VoiceProfile 授权保护的 TTS/语音转换任务
- `POST /api/v1/jobs/avatar` 创建受 AvatarProfile 授权保护的口型、动作或换脸任务
- `POST /api/v1/jobs/video-generation` 创建 Storyboard/Shot 生成任务
- `POST /api/v1/jobs/enhancement` 创建带版权确认的增强/授权内容修复任务
- `POST /api/v1/jobs` 创建通用作业，可同时声明步骤
- `GET /api/v1/jobs` 按项目和状态查询作业
- `GET /api/v1/jobs/stats` 查看当前租户的状态、类型、失败码、重试和策略阻断聚合
- `GET /api/v1/jobs/{job_id}` 查看作业状态
- `PATCH|PUT /api/v1/jobs/{job_id}/status` 推进作业状态（`queued → running → succeeded|failed|cancelled`）
- `POST /api/v1/jobs/{job_id}/cancel` 取消可取消作业
- `GET /api/v1/jobs/{job_id}/steps` 查看持久化步骤进度
- `POST /api/v1/jobs/{job_id}/steps` 增加步骤；`PATCH|PUT /api/v1/jobs/{job_id}/steps/{step_id}` 更新步骤状态
- `POST /api/v1/jobs/{job_id}/workflow` 创建持久化工作流租约
- `GET /api/v1/workflows/{workflow_id}/events` 查看事件；`POST .../claim|heartbeat|retry|cancel` 管理租约
- `GET /api/v1/artifacts/{artifact_id}` 查看派生媒体和 provenance

声音、Avatar、视频生成和增强请求可以附带 `model_id`、`model_version`；
Worker 会把它们传给 ModelRegistry，并在 Provider 执行前校验状态、商业许可、
权重 SHA-256 与地区限制。未登记或被暂停的模型不会被调度。

运维端点：`GET /readyz` 检查数据库和存储路径，`GET /metrics` 输出不含租户 ID 或任务 payload 的低基数 Prometheus 指标。生产部署应将 `/metrics` 限制在内部网络。

作业状态只能按 `queued → running → succeeded|failed|cancelled` 推进；终态不能重新排队。`POST /api/v1/jobs/{job_id}/cancel` 可取消 queued 或 running 作业，若作业已经完成则返回 `409`。JobStep 使用同样的单向状态规则，并保存 `started_at`、`completed_at`、`progress` 和错误信息。

带 `Idempotency-Key` 的创建请求按租户去重；相同租户和 key 会返回第一次创建的作业，跨租户可以复用同一个 key。若 HTTP 头与 JSON 中的 `idempotency_key` 不一致，API 返回 `400`；并发创建发生唯一键竞争时，API 会返回已存在的作业或 `409`。

Provider Registry 已使用相同 Job/Artifact 合约接入 ASR、自动剪辑、TTS、视频生成、动作复刻、换脸、增强和配音翻译；产品模式下真实实现由 HTTP Provider 注入，并由 ModelRegistry 锁定模型和许可证快照。`PROVIDER_MODE=api_only` 是默认策略，未配置外部 Provider 时任务会失败并保留可审计错误，不会回退到本地 deterministic 或 command 计算。`PROVIDER_MODE=development_mock` 只用于本地契约测试。

## 身份与声音资产

- `POST /api/v1/consents` 创建动态同意记录的元数据
- `POST /api/v1/consents/{consent_id}/revoke` 撤销授权并阻断后续身份任务
- `POST /api/v1/voice-profiles` 创建有授权范围的声音资产
- `POST /api/v1/voice-profiles/{profile_id}/revoke` 立即撤销声音资产
- `POST /api/v1/avatar-profiles` 创建有授权范围的模特/数字人资产
- `POST /api/v1/avatar-profiles/{profile_id}/revoke` 立即撤销模特资产

声音克隆、换脸和动作复刻 Worker 只能接受状态为 active/verified 的 Profile；撤销或过期后，新的 Job 必须被策略层阻断。

本地开发阶段可用 `python -m app.cli process-jobs` 消费 `asset.analyze`、`auto_edit.plan`、Provider 任务与 `render.*` 作业。每个 Worker Job 都会同时创建和推进 `WorkflowRun`；Provider 任务默认要求已配置的外部 HTTP 模型 API。分析结果写入 `Asset.metadata.analysis` 并生成 `analysis` Artifact；自动剪辑会生成 `Timeline` 和 `timeline.plan` Artifact；预览/导出再复用同一 Timeline 进入渲染队列。需要验证 Job/Artifact 合约时，显式设置 `PROVIDER_MODE=development_mock`，此模式只输出标记为 `simulated` 的 JSON manifest。

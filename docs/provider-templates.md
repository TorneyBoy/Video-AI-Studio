# 托管模型 Provider 注册模板

本文件提供一套不依赖 GPU 的 Provider 注册模板。模板只描述控制面如何调用已有的外部模型服务，不包含供应商密钥，也不假设某一家供应商的请求字段。生产环境应在外部 API 网关中把供应商差异归一化为本项目的 `ProviderRequest`/`ProviderResult` 契约，然后使用 `config/providers.example.json` 的结构注册该网关。

## 为什么使用统一 HTTP 网关

视频编辑、视频生成、声音克隆、数字人、换脸和增强服务的原生 API 在鉴权、任务状态、输入字段、回调和计费单位上通常不同。控制面只接受统一的 JSON 契约：

1. Worker 提交 `request_id`、`capability`、远程素材 URI 和参数。
2. Provider 立即返回 `artifact_uri`，或返回 `task_id`/`job_id`，随后由适配器轮询 `poll_path`。
3. 任务完成后返回远程对象存储 URI、`usage`、`cost` 和模型快照。
4. Provider 可以改为调用签名回调，控制面只保存 URI 和审计信息，不下载媒体、不运行推理。

这样可以在不改 Worker 的情况下替换供应商，也能为同一种能力配置多个 Provider 做质量、地区和成本路由。模板里的 `*.example.com` 地址是占位符，不能直接作为真实供应商端点使用。

## 能力与可选托管产品类型

| 能力 | 典型输入 | 输出 | 可以对接的商业产品类型 | 关键验收点 |
| --- | --- | --- | --- | --- |
| `video_editing` | Canonical Timeline、视频/图片/音频 URI | 成片视频 | 云端视频编辑 API、媒体工作流服务 | 转场、字幕、画幅、音轨和时长必须可复现 |
| `video_generation` | 文本、参考图/视频、风格参数 | 生成视频 | 文生视频、图生视频、视频延展 API | 任务幂等、内容安全、商业使用条款 |
| `voice_tts` | 文本、语言、音色 | 音频 | 云端 TTS 与多语种配音 API | 音频采样率、停顿、用量按字符/秒对账 |
| `voice_clone` | 授权音频、文本/音频 | 克隆音频 | 声音克隆和定制音色 API | 必须绑定 consent ID、保留撤回记录 |
| `voice_conversion` | 远程音频、目标音色 | 转换音频 | 声音转换 API | 只接受明确授权的目标音色 |
| `motion_transfer` | 驱动视频、模特图/视频 | 动作复刻视频 | 数字人、动作控制、姿态迁移 API | 人像授权、遮挡质量、镜头稳定性 |
| `face_transform` | 源脸、目标脸、视频 URI | 换脸视频 | 人脸替换与视频身份转换 API | 双方授权、敏感内容审核、可追溯水印 |
| `lip_sync` | 视频、音频 | 口型同步视频 | 口型同步和数字人口播 API | 语言、嘴型、时长和音画同步 |
| `enhancement` | 视频/音频/图片 URI | 增强媒体 | 超分、去噪、补帧、修复 API | 原始文件保留、质量对比和成本上限 |

可选的真实产品类型包括 Runway、Kling、Luma、Pika、Replicate 上的模型、ElevenLabs、Azure Speech、Google Cloud TTS、HeyGen、Synthesia、Tavus 以及各种云端媒体增强服务。它们的具体 endpoint、模型名称、区域和商业条款会变化，因此建议通过自己的 Provider 网关适配，而不是把某家 SDK 或字段直接写进核心 Worker。上线前应以供应商当前的开发者文档和合同为准，并在 `ModelRegistry` 中登记具体版本。

## 注册模板

完整的五类 Provider 示例在 [config/providers.example.json](../config/providers.example.json)。复制后应完成以下替换：

- `endpoint`、`path`、`poll_path`：替换为内部统一网关的 HTTPS 地址和任务路径。
- `model.model_id`、`model.version`：填入实际供应商模型和版本号。
- `headers_env`：只填写环境变量名，不把密钥放入 JSON、Git 或镜像。
- `callback_secret_env`：填入回调签名密钥对应的环境变量名。
- `cost`：填入供应商价目表的估算值；最终账单以 Provider 返回的 `cost` 为准。
- `license_snapshot`：记录商业使用许可、地区限制、内容限制和合同版本。

一个最小的异步视频编辑注册如下：

```json
{
  "name": "hosted-video-editing",
  "kind": "http",
  "endpoint": "https://video-gateway.example.com",
  "path": "/v1/video/edit",
  "poll_path": "/v1/jobs/{provider_job_id}",
  "headers_env": {
    "Authorization": "VIDEO_EDITING_API_KEY"
  },
  "callback_secret_env": "VIDEO_EDITING_CALLBACK_SECRET",
  "capabilities": [{
    "name": "video_editing",
    "input_types": ["json", "remote_video", "remote_audio"],
    "output_types": ["video"],
    "cost": {
      "per_minute": 0.12,
      "currency": "USD",
      "billing_unit": "minute"
    },
    "commercial_allowed": true
  }],
  "model": {
    "model_id": "hosted-video-editing-v1",
    "version": "2026-01",
    "license_snapshot": {
      "commercial_allowed": true,
      "weights_license": "provider-managed"
    }
  }
}
```

`headers_env` 在加载注册表时解析。缺少对应环境变量会立即报错，避免 Worker 运行到一半才发现没有鉴权。敏感的 `Authorization`、API key、token、secret、password 和 credential header 不允许写入 `headers` 明文，必须使用 `headers_env`。`ConfiguredProviderRegistry.as_dict()` 只返回 `callback_configured` 和能力/成本信息，不会回显密钥。

## Provider 网关的统一响应

立即完成时返回：

```json
{
  "request_id": "job-123",
  "status": "succeeded",
  "artifact_uri": "https://cdn.example.com/renders/job-123.mp4",
  "artifact_type": "video.rendered",
  "model": {
    "provider": "vendor-name",
    "model_id": "vendor-model",
    "version": "2026-01"
  },
  "usage": {"seconds": 12.4},
  "cost": {"amount": 0.12, "currency": "USD"}
}
```

异步提交可以返回：

```json
{
  "request_id": "job-123",
  "status": "queued",
  "task_id": "vendor-task-7"
}
```

适配器会请求 `/v1/jobs/vendor-task-7`（以 `poll_path` 为准）。轮询窗口耗尽时，Worker 持久化 `provider_job_id` 并安全重试；重试会继续轮询，不会重新提交任务。回调请求使用 `X-Provider-Signature: sha256=<HMAC-SHA256(raw-body)>`，重复 `event_id` 会被幂等处理。Provider 应回传平台的 `request_id`；若回调包含该字段，控制面会把它绑定到 URL 中的 `{job_id}`。异步回调还应携带 `external_job_id`，控制面会与 Worker 已持久化的 Provider task ID 比对，拒绝错投到其他 Job 的结果。

Provider 返回的 `artifact_uri` 和素材输入 URI 必须使用 `https`、`s3`、`gs`、`oss` 或 Azure 对象 URI；控制面会拒绝 HTTP、localhost、回环/私有/保留 IP 和内嵌凭据，避免把 Provider 变成 SSRF 通道。素材输入也必须先放在 Provider 可以访问的对象存储中；本项目不会把本地路径发送给外部模型或下载生成文件。

## 上线前检查

提交生产配置前，先执行网络隔离的 `provider-preflight`：

```bash
python -m app.cli provider-preflight --path /etc/video-ai/providers.json
```

命令不会请求任何真实供应商 API；它只验证 endpoint、Secret Manager 环境变量引用、
固定模型版本、商业许可、能力覆盖、成本和轮询字段。完整检查项和 JSON 报告格式见
[Provider 上线预检](provider-preflight.md)。

- 为每项能力至少配置一个已验证的 HTTP Provider；没有配置时任务应明确失败。
- 为每个租户和敏感任务保存 consent ID，并把它传入 Provider 请求。
- 校验模型版本、商业许可、区域和内容限制，再把模型注册为 `approved`。
- 先用小片段验证质量、延迟、回调幂等和实际账单，再放大并发。
- 为每种能力设置单任务时长、最大重试次数和成本上限；超出上限时停止重试。
- 记录 `usage`、`cost`、Provider job ID 和远程 Artifact URI，方便对账和删除请求。
- 生产使用 `PROVIDER_MODE=api_only`；`development_mock` 只用于契约测试，不能作为成片服务。

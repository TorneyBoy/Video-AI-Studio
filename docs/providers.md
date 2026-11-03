# Provider Gateway 与渲染 Worker

控制面只保存 `ProviderRequest`、`ModelSnapshot` 和 `Artifact`，不直接依赖任何模型 SDK。

## 路由规则

Provider Router 后续按以下字段选择实现：

- capability：ASR、TTS、voice clone、T2V、动作复刻、换脸、口型、增强；
- 输入与输出类型；
- 地区和数据驻留要求；
- `commercial_allowed` 和权重许可证；
- 质量分、延迟、显存和成本；
- 同意记录和敏感场景策略。

当 Job 的 `provider_request` 包含 `model_id`、`model_version`、`region` 时，
Router 会从 ModelRegistry 解析具体版本；商业任务还会检查权重许可证、SHA-256
和 approved 状态，并验证 Provider 返回的模型快照没有漂移。

## 执行边界与 Provider 模式

`LocalDeterministicProvider` 覆盖 TTS、声音转换、口型同步、动作复刻、换脸、视频生成和增强任务。它只写出包含输入、模型快照、授权 ID 和参数的 JSON manifest，`ProviderResult.status` 为 `simulated`，用于验证 Job/JobStep/Artifact 的完整闭环。它不声称生成音频或视频，也不会替代真实模型。

Worker 将 Provider 异常归一化为 `code`、`capability`、`provider`、`retryable` 和 `details`，同时写入 Job、JobStep 和 Artifact 的审计字段。

运行模式由 `PROVIDER_MODE` 控制：

| 模式 | 默认 | 允许的 Provider | 用途 |
| --- | --- | --- | --- |
| `api_only` | 是 | 已登记的 HTTP Provider | 生产与真实产品运行，所有 AI 能力调用已有模型 API |
| `development_mock` | 否 | HTTP Provider 与 `local-deterministic` | 本地契约测试；只生成可审计 JSON manifest |

`api_only` 会拒绝注册表中的 `kind=command`，并且不会自动插入
`LocalDeterministicProvider`。没有配置 HTTP Provider 时，任务会以
`capability_unavailable` 失败，而不是静默执行本地替代逻辑。`CommandProvider`
保留在代码中用于隔离测试或未来经明确授权的开发环境，不能作为产品默认路径。
测试可以向 `process_queued_provider_jobs(..., router=...)` 显式注入 fake/mock
router；生产入口使用默认参数，让 Worker 强制遵循 `PROVIDER_MODE`。

`HTTPProvider` 以 JSON 请求调用运营方配置的 HTTPS 端点，并把 408/429/5xx 和传输超时归类为可重试错误。它要求明确的 `artifact_uri` 和 `ModelSnapshot`，不会把密钥或本地路径写进远端请求或错误详情。

托管模型通常先返回 `202` 与 `task_id`。注册表可以增加 `poll_path`（使用
`{provider_job_id}` 占位符）、`poll_interval_seconds`、`poll_timeout_seconds` 和
`max_poll_attempts`。适配器会在有界时间内轮询；超时会抛出可重试的
`provider_async_pending`，Worker 将远端任务 ID 写入失败审计并在下一次重试中继续轮询，
不会重新提交一份任务。`callback_secret_env` 是 HTTPProvider 适配器解析 Provider
级回调时的密钥引用；平台公开的 `/provider-callbacks/{job_id}` 路由使用全局
`PROVIDER_WEBHOOK_SECRET`，两者都启用时必须在外部网关明确映射，不能仅凭注册表字段
推断公开入口已经鉴权。成本与用量应放在响应的 `usage`、`cost` 对象中，或在回调的同名字段
中；它们会进入 Artifact 元数据，金额不会由平台猜测或覆盖。

## 配置真实 Provider

Worker 可以通过 `PROVIDER_REGISTRY_PATH`（JSON 文件）或
`PROVIDER_REGISTRY_JSON` 注册真实 HTTP 适配器。配置同时锁定能力、超时、模型版本、
权重 checksum 和商业许可证快照。生产注册表中的每一项应使用 `kind=http`，并将
HTTPS 端点、认证头和健康检查路径交给部署密钥管理系统注入。

下面的配置示例展示外部视频模型 API 的注册方式：

```json
{
  "providers": [
    {
      "name": "vendor-video-api",
      "kind": "http",
      "endpoint": "https://api.vendor.example",
      "path": "/v1/video/generate",
      "poll_path": "/v1/video/tasks/{provider_job_id}",
      "poll_interval_seconds": 2,
      "poll_timeout_seconds": 30,
      "max_poll_attempts": 15,
      "callback_secret_env": "VENDOR_VIDEO_CALLBACK_SECRET",
      "health_path": "/health",
      "timeout_seconds": 7200,
      "capabilities": [{
        "name": "video_editing",
        "input_types": ["json"],
        "output_types": ["video"],
        "commercial_allowed": true,
        "cost": {"per_minute": 0.12, "currency": "USD", "billing_unit": "minute"}
      }],
      "model": {
        "model_id": "vendor-video-model",
        "version": "2026-01",
        "license_snapshot": {
          "code_license": "vendor-api-terms",
          "weights_license": "vendor-commercial-terms",
          "commercial_allowed": true
        }
      }
    }
  ]
}
```

`ConfiguredProviderRegistry.health(probe=True)` 会调用 HTTP Provider 的 `health_path`；默认
`probe=False` 只返回配置状态，不产生网络请求。健康结果只有 provider 名称、状态、原因、
探测标志和模型快照，不会回显请求体、本地路径或 Authorization 头。路由会跳过
disabled/unavailable Provider；模型快照会
随 Artifact 一起落盘，确保模型升级后历史成片仍可追溯。

## 第一版渲染边界

`development_mock` 下的 `build_ffmpeg_command` 对单视频轨保留兼容路径，并支持连续视频/图片片段、字幕、图片/视频叠加、`xfade` 转场和单音轨多片段混音；不支持的效果会在 FFmpeg 启动前返回质量错误，避免静默丢内容。Render Manifest 会锁定时间线 hash、素材 URI、模型快照、FFmpeg 版本和输出参数。

`api_only` 下 Worker 只校验并发送 Canonical Timeline、远端素材 URI 和渲染参数给
`video_editing` HTTP Provider，不创建本地输出目录、不调用 FFmpeg/ffprobe，也不下载
Provider Artifact。Provider 完成转码并把远程 URI 返回或通过签名回调通知平台。

## 外部异步 Provider 回调

商业 AI API 可以自行完成推理、转码和对象存储上传，再调用：

```text
POST /api/v1/provider-callbacks/{job_id}
X-Provider-Signature: sha256=<HMAC-SHA256(raw-body)>
X-Provider-Name: provider-name
```

Provider 也可以发送 `X-Provider-Timestamp`，此时签名原文为
`<timestamp>.<raw-body>`，并按 `PROVIDER_WEBHOOK_MAX_AGE_SECONDS` 拒绝过期事件。

请求体至少包含 `event_id` 和 `status`。Provider 应回传与平台 Job ID 相同的
`request_id`；控制面在收到该字段时会校验它必须匹配 URL 中的 `{job_id}`。
生产环境应设置 `PROVIDER_WEBHOOK_REQUIRE_REQUEST_ID=true`，把这个字段从“建议”提升
为强制绑定；严格模式同时要求 `external_job_id`，默认值 `false` 只用于迁移仍未回传
这些绑定字段的旧 Provider。
异步任务还应回传 `external_job_id`；若 Worker 已持久化 Provider task ID，控制面会拒绝
不匹配的回调，避免一个远程任务的结果写入另一个 Job。`succeeded` 事件还必须提供远程
`artifact_uri`，支持 `https://`、`s3://`、`gs://`、`oss://` 和 Azure 对象 URL；控制面只保存 URL 和审计元数据，不下载媒体，也不启动本地模型。

```json
{
  "event_id": "evt_01",
  "status": "succeeded",
  "provider": "remote-video-api",
  "external_job_id": "vendor_123",
  "artifact_uri": "https://cdn.example.com/renders/vendor_123.mp4",
  "artifact_type": "video.rendered",
  "metadata": {"duration_seconds": 12.4},
  "usage": {"seconds": 12.4},
  "cost": {"amount": 0.12, "currency": "USD"}
}
```

回调使用 `PROVIDER_WEBHOOK_SECRET` 做 HMAC 校验，重复的 `event_id` 幂等处理，终态任务不会被后续不同事件覆盖。未配置 secret 时接口返回 503，避免未认证回调被当成结果。

## Provider 运行时健康与上线验收

Provider 的健康报告通过内部维护命令生成，不属于租户 API，也不会把供应商状态或密钥暴露给终端用户。默认是**配置只读模式**，只加载注册表并检查适配器是否启用，不发送网络请求、不执行命令、不提交模型任务：

```bash
PROVIDER_REGISTRY_PATH=/etc/video-ai/providers.json \
python -m app.cli provider-health --json
```

需要在受控 staging/运维环境验证供应商网关时，再显式添加 `--probe`。该模式只请求配置的 `health_path`，使用不超过 10 秒的适配器超时，并输出低基数的状态、HTTP 原因和耗时，不输出请求体、响应体或认证头：

```bash
python -m app.cli provider-health \
  --path /etc/video-ai/providers.json \
  --probe
```

健康检查不会提交视频、音频或其他模型任务。默认 `api_only` 仍然只允许注册的 HTTP Provider；本地命令适配器仅用于显式开发/契约测试。报告返回非零退出码时，说明已启用 Provider 不可用，发布流水线应停止或切换到经过批准的 Provider。

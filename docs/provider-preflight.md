# Hosted Provider 上线预检

`provider-preflight` 是一个网络隔离的配置验收命令。它只读取
`PROVIDER_REGISTRY_PATH` 指向的 JSON 和环境变量是否存在，不会访问 Provider
endpoint，不会提交任务，不会执行模型，也不会读取或打印密钥值。因此可以在
CI 和部署 admission 阶段运行，再由另一个经过授权的环境执行真实供应商 smoke
test。

## 生产检查

生产发布前，将真实统一网关地址、模型版本和 Secret Manager 注入的环境变量放入
运行环境，然后执行：

```bash
export PROVIDER_REGISTRY_PATH=/etc/video-ai/providers.json
python -m app.cli provider-preflight --path "$PROVIDER_REGISTRY_PATH"
```

命令返回非零状态表示配置不能进入 `PROVIDER_MODE=api_only`。文本报告只显示
Provider 名称、环境变量名和 `configured=true/false`，不会显示环境变量内容。
需要供 CI 预览模板结构时，可以把未注入的 Secret 引用降级为 warning：

```bash
python -m app.cli provider-preflight \
  --path config/providers.contract.fixture.json \
  --allow-missing-secrets
```

分阶段发布可以显式限定本次必须覆盖的能力；省略时默认要求当前九项托管能力全部
覆盖：

```bash
python -m app.cli provider-preflight \
  --path /etc/video-ai/providers.json \
  --capabilities video_editing,video_generation,voice_tts
```

脚本支持机器读取的报告：

```bash
python -m app.cli provider-preflight \
  --path /etc/video-ai/providers.json \
  --json > provider-preflight.json
```

报告不会执行 `GET /health`。如果需要网络可达性验证，应在供应商允许的
staging 网关中使用独立 smoke test，并携带一个短任务和明确的预算；预检命令本身
不会绕过 API-only/no-GPU 边界。

## 检查内容

- Provider 必须是 `kind=http`，endpoint 必须是没有凭据、查询串或片段的绝对
  `https://` URL。
- `headers_env` 必须引用大写环境变量名，`callback_secret_env` 必须存在且同样
  使用环境变量引用；静态 `Authorization`、API key、token 和 secret header 会被
  拒绝；默认模式要求 Secret Manager 已注入非空值。
- 模型必须有具体的 `model_id` 和固定 `version`，并记录
  `license_snapshot.commercial_allowed=true`、`code_license` 和
  `weights_license`；如果提供 `license_snapshot.status`，必须是
  `approved`。
- 每个能力必须是项目 Provider 合约中的已知名称、明确允许商业使用，并有
  `cost.per_minute` 或 `cost.per_request`、货币和计费单位。零成本可以用于供应商
  的免费额度，但不能省略成本字段。
- 启用的 Provider 必须覆盖当前托管能力集合：视频编辑、视频生成、TTS、声音
  克隆、声音转换、动作复刻、换脸、口型同步和增强。
- 异步 Provider 必须配置 `poll_path`，以便 Worker 在提交后继续轮询同一个远程
  任务；预检不会重复提交任务。

## Contract fixture

`config/providers.contract.fixture.json` 使用 `.example.test` 的 HTTPS 地址和
无效的占位环境变量名，覆盖当前九项托管能力。它不包含真实密钥，也不应该被当作
供应商 endpoint。测试中注入临时值后执行静态预检：

```bash
CONTRACT_VIDEO_API_KEY=fixture \
CONTRACT_VIDEO_CALLBACK_SECRET=fixture \
CONTRACT_VOICE_API_KEY=fixture \
CONTRACT_VOICE_CALLBACK_SECRET=fixture \
CONTRACT_AVATAR_API_KEY=fixture \
CONTRACT_AVATAR_CALLBACK_SECRET=fixture \
python -m app.cli provider-preflight \
  --path config/providers.contract.fixture.json
```

fixture 的作用是锁定 Provider JSON 结构、能力覆盖和报告契约；它不代表任何真实
供应商的价格、模型质量或商业合同。实际接入仍需在自己的 Provider 网关中落实
请求字段归一化、回调签名、内容安全、授权记录和供应商账单对账。

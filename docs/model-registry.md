# Model Registry 设计

模型必须作为可审计的 Provider 资产登记，不能把某个 GitHub 仓库的代码许可证当作整套模型的商业许可。

```text
model_id
provider
version
repo_url
commit_sha
code_license
weights_license
dataset_terms
third_party_licenses
commercial_allowed
allowed_regions
content_restrictions
vram_gb
runtime_image_digest
weights_sha256
quality_benchmark
status                 # draft / approved / suspended / retired
reviewed_at
```

CI 和 Worker 启动时必须拒绝：

- `commercial_allowed=false` 的模型进入商业租户；
- 许可证未知或 checksum 不匹配；
- 目标地区不在允许范围；
- 需要授权但 Job 没有对应 `ConsentRecord`；
- 模型版本被暂停或撤回。

每个 Artifact 的 manifest 保存当时的 model snapshot，因此模型升级不会改变历史成片。

## 代码实现

`app/services/model_registry.py` 提供 `ModelVersion` 和 `ModelRegistry`。注册表
可以从 JSON 文件加载，也可以由部署代码构造；`ProviderRouter` 配置
`model_registry` 后会在选择 Provider 前检查以下条件：

- 版本状态必须是 `approved`；
- `commercial_allowed`、`weights_license` 和 `weights_sha256` 必须满足租户的商业策略；
- Job 请求的 `region` 必须在 `allowed_regions`；
- Provider 返回的 `provider/model_id/version` 必须与选中的注册版本一致。

一个可挂载的注册表条目如下：

```json
{
  "models": [
    {
      "provider": "gpu-video",
      "model_id": "motion-v1",
      "version": "2026.10",
      "capabilities": ["motion_transfer", "video_generation"],
      "repo_url": "https://example.invalid/model",
      "commit_sha": "0123456789abcdef0123456789abcdef01234567",
      "code_license": "Apache-2.0",
      "weights_license": "commercial",
      "commercial_allowed": true,
      "allowed_regions": ["US", "CN"],
      "runtime_digest": "sha256:runtime-image",
      "weights_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "status": "approved"
    }
  ]
}
```

未配置注册表的自定义测试 Router 保持兼容；默认生产 Router 会自动登记本地
deterministic manifest 版本，并对通过 `PROVIDER_REGISTRY_JSON` 或
`PROVIDER_REGISTRY_PATH` 挂载的真实 Provider 使用同一策略校验。

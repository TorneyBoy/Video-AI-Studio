# Provider 健康检查合同

`provider-health` 是内部运维命令，配合 `provider-preflight` 使用：

```bash
python -m app.cli provider-preflight --path /etc/video-ai/providers.json
python -m app.cli provider-health --path /etc/video-ai/providers.json --json
```

第一条是网络隔离的生产配置预检；第二条默认仍是网络隔离的配置状态报告。只有在受控环境明确使用 `--probe` 时，才会向每个启用 Provider 的 `health_path` 发出 `GET`。健康探测不会发送素材、请求参数或任务，也不会执行供应商模型。

JSON 报告字段：

- `source`：注册表文件路径或配置来源；
- `probed`：是否进行了显式网络/可执行文件探测；
- `ok`：所有启用 Provider 是否可用；停用 Provider 不会导致失败；
- `providers[]`：名称、状态、原因、模型快照、`latency_ms`（探测时才有）。

状态的含义：

- `configured`：注册表加载成功，未执行网络探测；
- `ready`：显式探测返回 2xx；
- `unavailable`：探测失败或返回非 2xx；
- `disabled`：运维显式停用。

报告不包含 `headers`、Authorization、Secret、响应正文或本地文件路径。命令仅供内部发布/监控使用，不是按租户授权的业务接口；不要把输出直接转发给终端用户。

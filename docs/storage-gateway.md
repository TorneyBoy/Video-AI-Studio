# OSS 直传与存储网关

此模块属于 M1/B02 的实现，当前尚未通过 M1 集中验证或真实 OSS 验收。控制面只交换 JSON 元数据，独立 `media_gateway` 服务负责 OSS 签名和读取对象校验。不要把 SDK 安装或健康接口返回当作云资源已可用。

## 双端共用流程

1. 登录后调用 `GET /api/v1/storage/config`，读取存储模式和大小限制。
2. 以稳定 `Idempotency-Key` 调用 `POST /api/v1/assets/upload-intents`，提交名称、MIME、大小、可选 SHA-256 和项目。服务端分配素材 ID、持久化请求哈希，再向网关申请凭据。相同键不同输入返回 409。
3. 网页使用返回的 PUT/POST；小程序使用 `wx.uploadFile` 对应的 POST 表单。表单字段原样发送，文件字段最后添加。OSS 凭据只在本次响应和上传请求中使用，不存入数据库或客户端持久化草稿；不要附加平台会话、Cookie 或租户头。
4. 上传完成，调用 `POST /api/v1/assets/{id}/verify-storage-upload`，不接受客户端直接宣称完成。网关读取 OSS 对象，以 ETag 条件请求固定内容，逐块计算实际 SHA-256；大小或哈希不符不签发证明。
5. 控制面校验素材 ID、对象路径、大小、哈希、过期时间、随机数和 HMAC，再原子改为 ready。分析任务在 ready 前被拒绝。

网络失败后，使用同一文件和同一幂等键重新申请；目的对象不能变化。OSS 已成功但浏览器未收到响应时，禁止覆盖可能返回 409，随后执行完成核验即可恢复。不承诺浏览器关闭后仍上传或任意断点续传。当前网页重选同一内容时保留幂等键；小程序临时文件失效后需要用户重新选择文件。

## 网关部署

独立进程入口：

```bash
python -m pip install -c requirements-dev.lock '.[gateway]'
uvicorn media_gateway.main:create_app --factory --host 127.0.0.1 --port 8100 --no-access-log
```

也可使用 `infra/gateway.compose.yml`。填好服务端 `.env.gateway` 后再启动，示例为 `.env.gateway.example`。该配置不得提交；网关仅在回环端口监听，由 HTTPS 代理转发。平台和网关共享两项**不同的**秘密：`STORAGE_GATEWAY_API_KEY`（服务调用）和 `STORAGE_ATTESTATION_SECRET`（对象完成证明）。ECS 部署优先使用 `OSS_CREDENTIAL_MODE=ecs_ram_role` 与 `ECS_RAM_ROLE_NAME=VideoAIStudioStorageRole`，通过 IMDSv2 获取自动刷新临时凭据。项目实例已绑定该角色；实际凭据刷新与访问边界待 M1 集中验证。显式配置 `environment` 模式时，只有网关持有 `OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET` 和可选 `OSS_SESSION_TOKEN`，控制面不持有这些变量。

`OSS_BUCKET`、`OSS_REGION` 必须实际存在；北京 ECS 对应初选地域为 `cn-beijing`。`OSS_USE_INTERNAL_ENDPOINT=true` 仅用于确认同地域的网关对象读取；浏览器签名与下载仍使用外网 HTTPS 域名。默认测试单文件上限 100 MiB，控制面的 `MAX_UPLOAD_BYTES` 应设置为相同或更小值。

OSS Bucket 使用私有 ACL、标准存储、未开启过版本控制。V4 POST 策略限制准确对象 key、文件大小、MIME、私有 ACL、禁止覆盖及短期有效期。因为 OSS 在版本控制 Enabled/Suspended 时忽略禁止覆盖，网关会拒绝此类 Bucket。不要将用户文件名直接作为对象路径。

Web CORS 只允许已确认的工作台 HTTPS 来源、POST/PUT 和必要请求头；微信后台配置实际 Bucket 域名为上传/下载合法域名。控制面 `STORAGE_UPLOAD_HOSTS` / `STORAGE_PUBLIC_HOSTS` 只填写确切域名，不使用通配符。HTTP 重定向不能用于跳转到其他存储域名。

## 机密与持久化

控制面 `storage_upload_intents` 保存输入哈希、素材/租户/项目、固定对象 URI、过期时间与安全错误码，不保存签名 URL、POST policy 或临时凭据。网关独立数据库保存对象绑定及已观察的完整性元数据；重试不反复读取整个已核验文件。访问日志关闭，SDK 原始异常和上游响应体不返回用户。

`GET /healthz` 只证明进程存活，并明确返回 `cloud_verified=false`。云账号、权限、实际上传、对象读取及账单仍需 B06 真实样本证明。B05 已写入受管 OSS 同租户目录复制归档、哈希证明、保留期限、交付资格与短期签名；自动归档队列从已提交的渲染结果恢复调度，具备租约和最多 5 次退避重试。渲染成功与归档完成分别记录；失败归档不能取得播放链接。任意外部 HTTPS 产物导入尚未实现，不能绕过受管目录边界。上述代码与用例待 M1 集中验证。

## 阿里云与预算依据

核对日期：2026-10-18 UTC。测试**累计**上限为 USD 10；用户当次确认后，已开通 OSS 并支付 CNY 20 购买 ICE 功能体验月包，含 1 个月使用期。后续 OSS 存储/请求/流量和媒体测试也纳入总额，不是仅计算剪辑调用。不要自动购买 150 元企业月包或启用续购；智能检索索引即使没有数据也可能持续收费，不是本阶段必需。实际资源与费用状态见[执行进度](development-progress.md)。

官方资料：[OSS POST 协议与限制](https://help.aliyun.com/zh/oss/developer-reference/postobject)、[Python V2 表单签名](https://help.aliyun.com/zh/oss/developer-reference/form-upload-using-oss-sdk-for-python-v2)、[官方 SDK](https://github.com/aliyun/alibabacloud-oss-python-sdk-v2)、[剪辑接口](https://help.aliyun.com/zh/ims/developer-reference/api-ice-2020-11-09-submitmediaproducingjob)、[ICE 计费与订阅](https://help.aliyun.com/zh/ims/billing-overview)、[计费 FAQ](https://help.aliyun.com/zh/ims/support/billing-faq)。

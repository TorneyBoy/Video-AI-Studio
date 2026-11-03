# 自动成片工作台

浏览器端的 `index.html` + `app.js` 提供一条可审阅的主流程：选择项目后上传原始视频，选择风格模板，创建 `auto_edit.plan` 任务，确认入选镜头，再创建 Draft 或 High 渲染任务。

## 流程

1. 注册素材：生产客户端从对象存储网关取得 presigned `upload_url` 与稳定 `object_uri`，调用 `POST /api/v1/assets/remote-upload-session` 后直传字节，再调用 `POST /api/v1/assets/{asset_id}/remote-upload-complete`。已有对象可用 `POST /api/v1/assets/register-remote` 注册；平台只登记 URI 和完整性声明，不下载、不探测、不转码。配置 `STORAGE_ATTESTATION_SECRET` 后，网关还必须为 `register-remote`、`register_only` 或完成请求签发绑定对象 URI/大小/hash/过期时间/nonce 的 HMAC 证明；前两者先由客户端提供稳定 `asset_id`，普通直传 session 则使用 API 返回的 `asset_id`，平台只接受有效证明及其幂等重试。开发环境也可调用 `POST /api/v1/assets/upload-session` 再上传到 `/assets/{asset_id}/content`。视频和音频会追加 `asset.analyze` 任务。
2. 选择素材：工作台读取 `GET /api/v1/assets?project_id=...`，默认勾选已经完成上传的素材；用户可以取消不参与本次计划的素材。
3. 选择模板：工作台读取项目模板。新项目没有模板时会创建三个可验证的内置模板（竖屏短视频、品牌图文、播客封面），模板内容使用 Timeline Engine 的 `TemplateDefinition` 结构。
4. 生成计划：调用 `POST /api/v1/jobs/auto-edit`，携带项目、模板、素材和幂等键。计划结果直接展示在工作台，并持续从 Job 队列同步状态。
5. 预览或导出：Worker 完成后，工作台读取 `GET /jobs/{job_id}/timeline` 获取已持久化的 Canonical Timeline，再调用 `/jobs/{job_id}/preview` 或 `/jobs/{job_id}/export`。Draft 使用 `draft` 质量，高清成片使用 `high` 质量；渲染状态通过 Job 查询轮询，完成后尝试读取关联 Artifact 的可访问 URL 作为浏览器预览。

## 离线和失败行为

没有 API 服务时，界面仍显示内置模板和完整步骤，但创建租户、上传、计划和渲染按钮会保持不可用或给出明确提示。模板创建、计划、Timeline 和渲染错误都在工作台状态区展示，幂等键可以让重复点击复用同一个任务。

本地渲染产物通常是服务器文件路径，浏览器无法直接读取；这时工作台仍会展示 Job/Artifact 完成状态。生产环境应让对象存储 Artifact 返回 `https`、`blob` 或受控下载 URL，前端即可显示 Draft 视频。

## Provider 用量与预算提示

工作台不会主动调用或假定预算 API。若后端在 Job、`payload.budget`、Provider 回调结果或 Artifact metadata 中返回可选的 `budget` 对象，前端会显示预算状态、已用/上限、剩余、周期、重置时间和后端消息；没有该对象时保持现有界面不变。兼容字段包括 `status`/`state`、`allowed`、`warning`、`blocked`/`over_budget`/`exceeded`、`used`、`limit`、`remaining`、`currency`、`period`、`scope`、`reset_at` 和 `message`。其中 `blocked`、`over_budget`、`exceeded` 或 `allowed: false` 会在任务详情和时间线标记“已超预算”，并阻止当前计划继续发起预览或高清导出；预警状态只提示，不阻止任务。

## 前端验证

```bash
node --check app.js
git diff --check
```

前端是静态资源，不依赖构建工具；开发时可执行 `npm run dev`，API 默认使用 `/api/v1`。

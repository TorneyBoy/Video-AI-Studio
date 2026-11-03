# 云端媒体分析接入

状态：M1/B03 阶段集中验证中。完整离线质量门含 PostgreSQL 为 312 passed、1 skipped；后续存储流读取专项 31 passed、区域接入与 SDK 适配专项 40 passed，与完整套件重叠，不相加。真实 OSS 上传/实际 SHA-256、ICE 北京媒体规格、FC 镜头、OSS 缩略图与分析归档均已取得；中文固定样本四句文字与原文仅标点不同，英语合成语音仍有明显误识别。分析能力按此受限样本报告开放给私有验收；两次渲染因 OSS 写入拒绝失败，完整页面交付尚未验收。ICE 媒体信息与 ASR 适配、持久化分析任务、外部镜头函数及替身用例已编写。FC 函数包已上传并创建成功，任务模式和实例上限已保存；ECS 分析调用策略已经创建并绑定；用户已完成核身和浏览器扩展更新。代码、打包与资源配置成功不能视为真实效果验收。

## 服务选型与实际限制

2026-10-18 UTC 核对：阿里云旧 VIAPI 视频理解的开通页面提示“该商品即将退市，已无法新购”。停止该路径，未开通、付费或授予 VIAPI 权限；未创建上海中转桶。提示收据：忽略目录 `data/approval/viapi-unavailable.png`。代码已移除不可新购的适配，开发依赖也不使用该 SDK。

镜头检测改为部署在阿里云北京函数计算 FC 的独立事件函数 `video-ai-shot-analysis`。控制 API 仅交换任务元数据；ECS 上的网关提供指定对象的短期读/写签名、调用云函数并整理结果，不在 ECS 解码、分析或渲染视频。ICE 执行媒体探测、带时间戳的语音识别和最终渲染；OSS 执行单帧截取。

函数源码：`cloud_functions/shot_detection/index.py`、`detector.py`；配置：同目录 `deployment.json`。函数内的 PyAV 解码使用实际帧 PTS，色彩变化检测返回硬切边界。片尾使用与解码器容器信息核对后的 ICE 实测时长，不把平均帧率乘帧号冒充时间戳。初始阈值为像素差 30、直方图 L1 差 0.25、镜头最短 0.25 秒；不宣称能完整识别渐变转场或语义段落，不生成虚假质量、情绪、人脸清晰度等分数。M1 真实样本需验收硬切边界；M2/M3 继续扩展与标注样本评估。

## 已准备的具体配置

- 地域北京，Python 3.12，1 vCPU、1024 MiB 内存、512 MiB 临时盘、超时 180 秒、单实例并发 1、最小实例 0、最大实例 1；不配置预留实例或资源包，不创建 HTTP 触发器。
- 开启异步任务模式；平台自动重试设为 0，事件最长等待 300 秒。每次使用稳定 `shots_<operation_id>`，查询仅匹配同一个任务 ID。任务输入含指定源对象 GET 与指定结果对象 PUT 的 900 秒签名，不含账户长期密钥。函数执行角色为空；无全桶浏览、RAM 管理或任意云资源访问凭据。
- 函数环境仅配置确切项目 OSS 域名；源对象必须属于该租户，结果只能写入该次 `tenants/<tenant>/analyses/<operation>/shot-result.json`。强制 HTTPS、禁止重定向；下载后再次计算实际 SHA-256 与字节数。结果签名限定私有 ACL、禁止覆盖，处理完删除临时文件。输入和签名不写日志。
- 初始测试最多 6 次分析、每条最多 30 秒和 100 MiB、至多 3600 帧、1080P 短边/1920 长边、每条最多 30 张缩略图。配置只是费用边界的一部分，不代表按时长可以精确计算 FC 账单。

打包命令 `python scripts/package_shot_function.py` 仅下载官方 PyPI Linux CPython 3.12 二进制 wheel 并打包，不调用云函数或运行阶段测试。当前打包文件位于忽略目录 `data/fc-artifacts/shot-detection-v1.zip`，大小 51,750,193 字节；摘要 `cfee7b488d67a0d13284124465d2a5f4f49380e349fff8a0460a6ef35b1674ba`。依赖 `av==19.0.1`、`numpy==2.4.3`，随包保留分发元数据与许可证。修改源码后必须重新打包并记录新摘要，不能继续交付此旧包。

## 新增权限的确认与执行状态

已批准的两个角色、三项策略和六个 ICE 接口继续有效。用户已经具体确认下列新增范围及对应 FC 按量服务协议，无需再次索要相同授权：

1. 已在当次确认后创建服务关联角色 `AliyunServiceRoleForFC`，RAM 角色列表显示创建时间 2026-10-17 23:33:47。它绑定平台管理的 `AliyunServiceRolePolicyForFC`，供 FC 访问 VPC、ECS、SLS、镜像服务以及函数/会话管理。它由阿里云维护、只供关联云服务使用，权限不是项目目录级自定义策略，不能将其描述为应用仅访问单个 Bucket。原授权提示截图 `data/approval/fc-service-role-review.png`。
2. 已给已绑定的 `VideoAIStudioStorageRole` 增加自定义策略 `VideoAIStudioAnalysisClientPolicy`：`fc:InvokeFunction` 限北京当前账号此函数；`fc:GetAsyncTask` 限该函数 `shots_*` 任务；另加 `ice:SubmitASRJob`、`ice:GetSmartHandleJob`，后两项按接口要求使用 `Resource=*`，能够查询账号相应智能任务信息。模板 `config/aliyun-analysis-client-policy.template.json` 已生成本账号实际文件 `config/aliyun-analysis-client-policy.json`；账号 ID 从角色 ARN 核对为 `1253063092481585`。用户完成网站安全验证后，控制台确认策略 v1 创建于 2026-10-17 23:44:58；本轮完成绑定，结果显示 1 个成功、0 个失败。收据 `data/approval/analysis-role-granted.png`；旧核身截图仅保留过程记录。无需给 ICE 存储角色追加权限。

服务关联角色持续有效至按云平台规则删除；ECS 新策略持续有效至撤销。函数不创建长期 AccessKey；应用不关联 FC/OSS/ICE FullAccess。函数创建时间 2026-10-17 23:36:49，界面确认 Python 3.12、49.35 MB 代码、1 vCPU/1 GB/512 MB、index.handler、180 秒、函数角色“未启用”及项目域名环境变量。弹性实例上限为 1，未配置保留实例策略（默认最小 0）。任务模式“开启”、重试“0 次”、消息存活“5 分钟”，任务数 0；收据 `data/approval/fc-task-configured.png`。这些是配置读取，不是执行函数测试。

## 持久化与返回结果

`POST /v1/analysis` 在调用云服务前持久化同租户的请求哈希、源对象和试用计数；同键异参返回 409，重复请求不重复占配额。`GET /v1/analysis/{id}` 通过租约推进媒体信息、镜头任务、ASR、缩略图和分析 JSON 归档；数据库事务与云网络调用分离。源哈希由控制面已核验的素材传入，网关实际读取对象核对，云函数下载后再核对。

FC 作业编号在提交前持久化，响应不明确时查询同一个已知编号，不换新编号。ASR 不提供 ClientToken，提交前持久化“submitting”围栏；提交超时或进程中断后标记结果未知，禁止自动重提。保留云作业编号供人工/账单核对。分析结果使用 `media-analysis/2`，明确各阶段状态、镜头边界、字幕片段、缩略图 URI 和来源证据；不完整镜头分析不能进入自动剪辑规划。

私有缩略图写入同租户稳定路径，不把短期链接长期保存在分析 JSON。缩略图不支持/失败/超过数量上限时保留镜头数据并返回 `thumbnails_incomplete`。前端素材预览的短期访问将在后续素材工作台中接入；不通过公开 Bucket ACL 解决显示问题。

## 费用与阶段验收

累计测试预算仍为 USD 10，已支付 ICE 月包 CNY 20。FC 按实际资源、实例存活时间和请求计费，存在出账延迟和最低消费规则，不能由视频秒数推算最终费用；启用镜头函数的分析总费用先保持 `unknown`。ICE ASR 公布价为 CNY 0.0417/分钟、按比例，返回公开价计算分项而非确认账单。OSS 请求、存储、流量、截帧另计并纳入累计预算；不得按未出账视为零元。

本阶段结束集中执行完整质量门、数据库迁移/并发回归以及真实短视频的上传→分析→计划→ICE 渲染→质检→归档→短期访问；核对云作业、源/产物哈希、镜头和字幕时间戳及账单。当前分析已取得真实媒体规格、4/8 秒镜头、三张缩略图及四句中文 ASR；33 个汉字归一化后编辑距离 0，仅为此样本结果。字幕临时目录权限经用户批准修复后，真实云视频已取得，页面完整交付仍在验收；详见 [供应商实测记录](m1-supplier-acceptance.md)。

依据：[FC Python 运行时](https://help.aliyun.com/en/functioncompute/python3/)、[FC 细粒度策略](https://help.aliyun.com/en/functioncompute/policies-and-sample-policies)、[异步任务去重与状态](https://help.aliyun.com/en/functioncompute/asynchronous-task)、[GetAsyncTask](https://help.aliyun.com/zh/functioncompute/api-fc-2023-03-30-getasynctask)、[服务关联角色](https://help.aliyun.com/en/ram/user-guide/service-linked-roles)、[ICE ASR](https://help.aliyun.com/zh/ims/developer-reference/api-ice-2020-11-09-submitasrjob)、[智能任务价格](https://help.aliyun.com/zh/ims/smart-tasks)、[OSS 单帧截取](https://help.aliyun.com/zh/oss/video-snapshots/)。

## 阶段实测中的接入修正

- 官方 SDK 6.20.2 将北京默认映射到旧 `ice.aliyuncs.com`，阶段请求返回 HTTP 503 / ServiceUnavailable，已停止该路径的轮询，保留原分析 operation，不重置配额。按[当前官方地域接入点](https://help.aliyun.com/zh/ims/developer-reference/api-ice-2020-11-09-endpoint)明确配置 `ice.cn-beijing.aliyuncs.com`，不切换地域。实际探测任务 `d993ff581185453da445edc7530aca54`、请求 `01A14DE5-A634-567B-A2F1-E2D153F2A5F0` 返回 12 秒、720×1280、30 fps、AAC/48 kHz/双声道。
- 同步探测响应可能缺编码字段；查询同一个已返回的媒体信息 Job 后取得 H.264/360 帧信息。适配器仅在缺字段时查询同一 Job，不猜编码、不提交另一个探测任务。ASR 输入转换为官方文档中的带地域 endpoint 的 OSS 地址，保留数据库中的稳定 bucket 引用。
- 独立网关的真实 OSS SDK 流对象不支持 `read(size)`；已改用 `iter_bytes(block_size=65536)` 进行有界读取，保留字节上限、SHA-256 与 If-Match 校验。新增真实 SDK StreamBodyReader 回归，避免仅凭 BytesIO 替身误判。
- 自生成验收输入 `data/m1-fixture/source.mp4`：109,977 字节，SHA-256 `1237d906104cea01632e7aabe817e4571f15e3500e0f3303b1d40595320ce5f7`，12 秒，已知硬切 4/8 秒与合成英语旁白；它只是测试输入，不是平台成片。已上传素材 `44bd339a-c566-4f45-8373-f0677e9807c2` 并由实际对象读取核验为 ready。
- 原分析 operation `ba78b9fd14ea36aa3f1c1a4530906c43377d6b9c1a0e9123a09564aec6801b1c`、请求 `m1-supplier-analysis-001` 已持久化。旧接入点错误时尚未进入 FC/ASR；后续继续该 operation，不更换任务 ID 或清空试用计数。请求与存储用量仍须核对账单，不能把失败请求视为费用必定为零。

- 后续实际分析已完成：原 FC 任务成功，首次状态未确认导致的客户端失败经只读云任务/对象核验后恢复原 operation；计数未重置。客户端对未确认状态仅在既定上限内继续查询同一任务。中文第二样本正常完成，原始收据及对比结果见上述供应商实测记录。


原页面分析控制 Job 已在服务端补回可信 CNY0.50 预占后，复用同一云 operation 经正常 retry 恢复成功；没有再次提交分析，源/请求哈希保持不变。未知账单继续 pending_reconciliation，页面明确显示费用待对账。部署与恢复细节见 [M1 实测](m1-supplier-acceptance.md#原页面分析恢复与费用预占)。

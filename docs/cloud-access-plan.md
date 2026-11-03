# 阿里云项目访问配置记录

状态：已完成本文件列出的权限配置。用户完成两个角色创建后，控制台确认 ECS 信任主体正确；将 `AliyunICEDefaultRole` 的受信主体从 `imm.aliyuncs.com` 修正为用户已批准的 `ice.aliyuncs.com`。三项自定义策略创建成功，ECS 两项授权与 ICE 一项授权均显示“已完成”。ECS 实例 `i-2zeag2jfxhhudwe3svpe`（公网 IP `39.97.246.39`）原来没有实例角色，现已绑定 `VideoAIStudioStorageRole`。IMS 引导页显示已开通、已授权、已购买；项目 Bucket 在 IMS 存储管理中的状态为“正常”。这些是云配置证据，尚非真实媒体效果验收证据。

## 已授权对象与范围

| 对象 | 信任与用途 | 权限 | 生效期限 |
| --- | --- | --- | --- |
| `VideoAIStudioStorageRole` | 仅信任 `ecs.aliyuncs.com`，创建后仅绑定本项目公网 IP `39.97.246.39` 对应的实例 | 读取项目 Bucket 版本状态；读写 `video-ai-huayuan-cn-beijing/tenants/*` 下的对象；调用下列六个 ICE 接口 | 角色及授权持续有效，直至解绑或撤销；应用使用自动轮换的临时凭据 |
| `AliyunICEDefaultRole` | 仅信任 `ice.aliyuncs.com`，供 ICE 读取素材并写入结果 | 读取项目 Bucket 信息与地域；读写同一 `tenants/*` 对象目录 | 角色及授权持续有效，直至撤销 |

ECS 角色调用接口清单：`ice:SubmitMediaProducingJob`、`ice:GetMediaProducingJob`、`ice:SubmitSyncMediaInfoJob`、`ice:SubmitMediaInfoJob`、`ice:GetMediaInfoJob`、`ice:GetMediaInfo`。这些 ICE 操作不支持按单个作业划分资源，策略使用 `Resource: "*"`，意味着应用可提交相应作业并查询本账号对应作业/媒资信息。OSS 对象访问权限仍限制于上述 Bucket 的项目目录。后续镜头检测、ASR 等接口在选定适配方案后另外列出，不能借此授权任意 ICE 操作。

配置文件：

- `config/aliyun-ecs-role-trust.json`：ECS 服务信任。
- `config/aliyun-storage-policy.json`：ECS 存储读写权限。
- `config/aliyun-ice-client-policy.json`：ECS 调用指定 ICE 接口。
- `config/aliyun-ice-role-trust.json`：ICE 服务信任。
- `config/aliyun-ice-storage-policy.json`：ICE 限定目录存储读写权限。

不创建长期 AccessKey，不关联 `AliyunOSSFullAccess`、`AliyunICEFullAccess` 或权限覆盖其他产品的默认系统策略。配置不含删除对象、变更 Bucket 公共权限、RAM 管理、DNS 管理权限。Bucket 保持私有、阻止公共访问与 AES256 加密。

## 执行顺序与收据

1. 获得本次具体权限创建、授权和实例绑定的确认：已收到，不重复索要同一范围的确认。
2. 在 RAM 控制台创建自定义策略及两个角色，核对信任策略与授权列表。若同名角色已存在，先核对现有用途，不能覆盖其他项目的角色。
3. 在 ECS 控制台确认 IP 对应的实例 ID 和现有实例角色；若绑定会替换已有角色，应先保留现有用途并重新评估，不能直接替换。
4. 已绑定 ECS 角色。应用凭据适配器已写入 IMDSv2 自动刷新逻辑，尚待部署与 M1 集中验证；不将凭据写入仓库、浏览器持久存储或日志。
5. 在 IMS 存储管理登记该 Bucket；如界面强制要求额外权限，保留失败证据并明确新增范围，不自动扩大到全账号存储。
6. 在 M1 阶段集中验证访问边界、真实媒体处理和费用；当前支付的 CNY 20 计入累计 USD 10 测试预算。

权限授权本身不证明镜头检测、ASR 或渲染质量通过。部署、主域名切换与真实链路验收仍需单独记录结果。

本地收据位于忽略目录 `data/approval/`：`ecs-policies-granted.png`、`ice-trust-corrected.png`、`ice-policy-granted.png`、`ecs-role-bound.png`、`ice-ready.png`、`ice-storage-registered.png`。实际关联策略为 `VideoAIStudioStoragePolicy`、`VideoAIStudioICEClientPolicy` 和 `VideoAIStudioICEStoragePolicy`，内容对应上面的 JSON 文件。

## 后续分析能力增量

旧 VIAPI 视频理解当前无法新购，已放弃此接入路径。用户另外确认独立 FC 镜头函数、平台服务关联角色和限定调用权限，见[云端分析接入](hosted-analysis.md)。`AliyunServiceRoleForFC` 与北京 `video-ai-shot-analysis` 已创建，函数无执行角色，最大实例数 1、默认最小 0、自动重试 0。用户完成账户安全验证后，`VideoAIStudioAnalysisClientPolicy` v1 已创建并绑定 ECS 项目角色，授权结果为 1 个成功、0 个失败；收据 `data/approval/analysis-role-granted.png`。新增权限严格使用实际文件 `config/aliyun-analysis-client-policy.json` 的单函数 Invoke、shots_* 查询和两项 ASR 动作，未追加 FullAccess。

## 已批准的日志与字幕目录增量

SLS 精确定位 ICE 自动字幕文件写入拒绝后，用户另行批准并已应用 `VideoAIStudioICEStoragePolicy` v2：仅对 ICE 角色增加本 Bucket `icetemp/subtitle/*` 的 GetObject/PutObject。该共享临时路径不含租户分区，持续至撤销；没有扩大 ECS、列表、删除、ACL 或公共访问权限。实际源码与本地策略镜像一致，旧版保留。真实带字幕视频已成功，详见[供应商记录](m1-supplier-acceptance.md#cloud-render-20261018)。

用户还批准并已创建平台账号级 `AliyunServiceRoleForSLSAudit`，绑定系统管理策略。它权限超出项目 Bucket，与前述两个限定项目角色分别记录；不能声称账号所有云角色均限项目目录。实际只采集项目 Bucket 的访问日志，最终已停止新增采集，保留 7 天、一个可写分片；角色及平台资源未删除。开通订单和诊断费用边界见[日志记录](m1-logging-plan.md)。

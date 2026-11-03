# M1 OSS 写入拒绝的日志诊断方案

状态：用户已于 2026-10-18 批准本文的开通、服务协议、角色和限定采集范围；已执行并取得真实拒绝记录。诊断完成后已停止新增访问明细采集，保留期仍为 7 天。日志库、Project、服务关联角色未删除，不能将停止采集表述为销户或所有后续费用归零。

## 准备的具体操作

1. 阿里云开通页显示 SLS 两个后付费商品（按使用功能、按写入数据量），仅开通费用 ¥0；需要接受平台对应服务协议。不购买资源包或订阅。
2. CloudLens for OSS 首次启用按平台要求创建服务关联角色 `AliyunServiceRoleForSLSAudit`，绑定 `AliyunServiceRolePolicyForSLSAudit`，信任日志审计服务，持续有效至按平台规则删除。**此平台管理策略超出单项目 Bucket 范围**：可读取账号下多种云资源信息、修改部分数据库/负载均衡日志审计设置、管理 SLS 日志资源，并通过 OOS/云助手运行平台日志安装相关命令。完整当前策略见[官方角色说明](https://help.aliyun.com/zh/sls/manage-the-aliyunserviceroleforslsaudit-service-linked-role)。不得将它描述为只读单桶角色。
3. 实际只为北京 `video-ai-huayuan-cn-beijing` 开启访问明细日志，按使用功能计费，保存 7 天；不安装 ECS 日志代理，不开启其他云产品采集、短信/语音告警、数据加工或外网投递。日志包含请求时间、对象路径、调用身份、来源 IP 等 OSS 请求元数据，将进入本账号 SLS，不向外部团队分享。平台会自动启用 OSS 计量/监控，并可能建立北京专属日志库及河源 CloudLens 元数据 Project。
4. 只查询该项目 Bucket 的写入拒绝，取得实际 Action、对象路径及错误细节后提出确切修复；不通过猜测追加 OSS 权限。额外角色或平台要求如超出上述范围，先停在确认页。

## 费用与保留

[官方费用说明](https://help.aliyun.com/zh/sls/cloudlens-for-oss/)：访问明细日志最近 7 天、每日写入不超过 900 GB 有免费额度，Shard 也有月度额度；读取流量、外网流量、加工、投递以及超额部分可能计费。不能承诺全部查询免费。日志诊断预留不超过 USD 0.25，属于原累计 USD 10 测试预算的一部分，仍需计入 CNY 20 包及其他已用服务；无法确认预算时停止新收费操作。

诊断完成后停止新增访问明细采集，保留 7 天自动到期策略；停止采集不等于删除 Project/LogStore。记录实际剩余资源、Shard 与账单，不能把暂停采集声称为完全销户或无后续费用。需清理不可恢复数据时按用户确认的范围执行。

## 2026-10-18 执行结果

- 两个开通订单 `2006210520930261`、`2006210520940261`，均为 CNY 0，控制台显示开通成功；收据 `data/approval/sls-open-success.png`。
- `AliyunServiceRoleForSLSAudit` 已自动创建，绑定账号级系统策略 `AliyunServiceRolePolicyForSLSAudit`；RAM 授权页确认，收据 `data/approval/sls-role-created.png`。未安装服务器日志代理。
- 实际日志 Project 为北京 `oss-log-1253063092481585-cn-beijing`；访问明细库 `oss-log-store` 保留 7 天、按使用功能计费，关闭自动扩片，两个初始分片合并为一个可写分片，原两个只读分片按平台规则到期。平台另有 `oss-metering-log`、`oss_metric`，其默认 60 天保留与访问明细的 7 天不同。
- 仅为项目 Bucket 开启访问明细采集。初始化时出现的索引未就绪错误在正常查询后消失，实际查得一条新诊断作业的 403 日志；完成后关闭该 Bucket 采集，控制台显示“未开启”。配置收据为 `sls-log-retention.png`、`sls-log-billing-mode.png`、`sls-single-write-shard.png`、`sls-bucket-collection-stopped.png`。
- 第三次受限诊断请求 `m1-supplier-render-003`，6 秒；真实 ICE 作业 `5cf7374c1e924884b1fd36d55ad5e102`。没有修改原两个失败作业或重置配额；当前分析/渲染计数各为 3/6。

## 已证实的拒绝原因与增量修复

OSS 日志时间为 `2026-10-18 08:25:12 UTC`，请求 ID `6AD482687DB8183236B5E9A4`，实际 Action `PutObject`，HTTP 403，`AccessDenied`，EC `0003-00000001`。角色为 `AliyunICEDefaultRole`，对象为：

```text
icetemp/subtitle/5cf7374c1e924884b1fd36d55ad5e102/1792311912340.ass
```

路径中的作业 ID 与第三次 ICE 作业完全一致。它是 ICE 自动生成的字幕辅助文件，处于先前批准的 `tenants/*` 之外；这条记录不能解释为成片输出路径本身的拒绝。原始可见日志和截图保存于 `data/approval/sls-ice-subtitle-access-denied.txt`、`.png`。

查阅当前[剪辑合成参数](https://help.aliyun.com/zh/ims/developer-reference/clip-composition-parameter-description)未发现可配置此内部字幕目录的已文档化字段；不尝试臆造字段或降低 Bucket 安全设置。完整候选策略为 `config/aliyun-ice-storage-policy.proposed-v2.json`，**用户批准后已应用为当前 v2**：仅在 `VideoAIStudioICEStoragePolicy` 新增 `oss:GetObject`、`oss:PutObject` 到本项目 Bucket 的 `icetemp/subtitle/*`。PutObject 是日志已证实所需，GetObject 用于随后读取生成字幕。该目录由 ICE 共享生成，不含租户分区，只供 ICE 角色使用；不增加 ECS 权限、列表、删除、ACL、公共访问或其他 Bucket 权限。授权持续至撤销。

费用页已确认 CNY 20 订阅预付记录，并提示账单明细通常延迟 24 小时更新；当前无可用于该分析作业完整结算的 FC/OSS 明细。未知费用保留为待对账，不填零、不把账号其他项目消费计入本测试。诊断预留 USD 0.25 和累计 USD 10 继续有效，尚无最终诊断账单。

RAM 当前 v2 源码已核对，实际镜像为 `config/aliyun-ice-storage-policy.json`；原 v1 在 RAM 保留，证明 `data/approval/ice-subtitle-policy-v2.png`。render-004、005 均成功生成、读取并核验实际视频，前述拒绝消失；完整结果及抽帧 QA 误判修正见[供应商验收](m1-supplier-acceptance.md#cloud-render-20261018)。最终已停止新增采集，控制台“未开启”证明 `data/approval/sls-bucket-collection-stopped-final.png`；日志、角色及平台自动资源仍存在，没有删除或宣称零后续费用。当前分析 3/6、渲染 5/6，配额未重置，费用上限不变。

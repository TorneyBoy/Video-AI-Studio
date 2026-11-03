# 客户端能力目录与供应商开放流程

M1 正在实现，本文对应 B01；当前接口和模型变更尚待 M1 集中验收。`GET /api/v1/capabilities` 要求有效租户身份，返回目录版本、模型与能力、是否可用、输入/输出类型、公开限制及报价版本，不返回服务端地址、凭据或内部效果报告路径。

能力能开放必须同时满足：供应商已启用、生产计算方式适用、商用策略通过、效果验收对应当前模型版本、报价配置与版本齐全。结构预检通过不会自动变成效果验收通过。

## 注册配置增量

在具体 capability 上增加以下字段，保留原有 `cost`、输入/输出类型和地区配置：

```json
{
  "name": "video_editing",
  "commercial_allowed": true,
  "acceptance": {
    "status": "unverified",
    "report_ref": null,
    "model_version": null
  },
  "price_version": "operator-defined-price-version",
  "limits": {
    "max_duration_seconds": 60,
    "features": ["trim", "subtitles"]
  }
}
```

示例时长仅演示字段，不是当前产品上限。先接入 staging 服务取得真实样本和质量/费用报告，再将该能力的验收状态设为 `verified`，填写内部报告引用与确切模型版本。升级模型后旧报告不会自动批准新版本；停用可将状态改为 `suspended`。

生产创建分析、渲染或生成任务时，服务端选择可用能力，将供应商、模型版本、限制及价格版本写为任务快照。Worker 使用该快照选定的供应商，不能因其他未验收供应商评分更高而绕过开放范围。当前提交后变更报价的完整确认流程在 C12 完成。

## 托管模型的版本证据

若供应商不公开权重哈希，在 `model.license_snapshot` 明确设置 `integrity_mode=provider_version` 与 `integrity_reference`，保留具体模型版本和适用服务条款。该模式仅允许 HTTP 托管适配器，不填造出的 SHA-256。使用权重证据的原策略继续要求真实 checksum；托管声明与权重验证在来源记录中有明确区别。

## M1 验收范围

检查目录不发起网络探测、不暴露秘密；缺少报告、模型版本不符或缺价格时不可用；用户创建的业务任务绑定可用供应商；真实分析/渲染及存储访问在 B02–B06 统一验收。新增目录用例已编写，遵照阶段要求尚未单独运行。

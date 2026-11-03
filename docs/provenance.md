# Provenance 与 C2PA 接入边界

当前工程先生成稳定的 JSON provenance sidecar，记录：

- 原始素材 ID 与 hash；
- Timeline 版本与 Operation；
- Provider、模型版本和运行时快照；
- VoiceProfile / AvatarProfile / Consent ID；
- 用户版权确认；
- 是否包含 AI 生成内容；
- 输出 Artifact 的 manifest hash。

sidecar 由 `write_sidecar` 使用同目录临时文件和原子替换写入，Worker 中断时不会留下半个 JSON；`read_sidecar` 默认会重新计算 hash 并拒绝被修改的文件。该校验属于完整性校验，不等同于生产环境的签名认证。

下一阶段将把 sidecar 映射为 C2PA manifest，并在最终 Render Worker 中签名和嵌入媒体。sidecar 仍然保留，便于内部审计、撤销和错误调查。

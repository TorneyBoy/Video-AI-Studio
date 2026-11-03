# M1 真实供应商验收记录

状态：2026-10-18 UTC，M1 指定样本与受限视频子集已通过真实媒体闭环验收。完整产品、微信小程序、公开部署及最终云账单对账尚未完成。离线测试、资源配置、供应商作业及页面闭环分开记录。

最新状态以下文 Cloud render 20261018 和页面恢复记录为准；此前 testing、失败次数和旧质量门数量保留为历史，不代表当前状态。

## 已取得的真实结果

私有 PostgreSQL、API、Worker、内部 TLS 网关及 SSH 工作台已运行。浏览器项目为“M1 云媒体闭环验收”，身份由服务端签名会话确定。主域名和 Nginx 未迁移。

| 项目 | 实测结果 | 证据 |
| --- | --- | --- |
| OSS 上传与核验 | 素材 ready，109,977 字节；实际读取 SHA-256 与输入一致 | 素材 `44bd339a-c566-4f45-8373-f0677e9807c2`；`data/m1-fixture/supplier-asset.json` |
| ICE 媒体探测 | 12 秒、720×1280、30 fps、H.264、AAC/48 kHz/双声道 | 作业 `87c981d8efe34dfb8100ec7498a61004`；原同步响应缺编码，查询同一作业补齐 |
| FC 镜头检测 | 360 帧；片段 0–4、4–8、8–12 秒，与已知切点一致；云任务 1299 ms、重试 0 | `shots_ba78b9fd14ea36aa3f1c1a4530906c43377d6b9c1a0e9123a09564aec6801b1c` |
| OSS 镜头缩略图 | 2、6、10 秒三张 JPEG，均有实际大小与 SHA-256 | 分析结果的 `metrics.thumbnail_evidence` |
| ICE ASR | 返回两个带时间戳片段，但英语合成语音的识别文字偏差明显 | 作业 `e85e5372729345e9b58a4855e24fddb7`；未判定文字准确率合格 |
| 分析归档 | 私有 JSON 已写入并再次读取核验 | SHA-256 `d510e6ae8c763400c1e61dba1d3ddec394a1bfc6660a06ffa56169f66a8496ef` |

输入 SHA-256 为 `1237d906104cea01632e7aabe817e4571f15e3500e0f3303b1d40595320ce5f7`。它是本地生成的授权测试输入，不是平台渲染结果。分析请求 `m1-supplier-analysis-001`，operation `ba78b9fd14ea36aa3f1c1a4530906c43377d6b9c1a0e9123a09564aec6801b1c`。完整返回收据在服务器 `data/deployment/m1-real-analysis-report.json`。

分析 `completeness=complete` 仅说明所请求阶段返回了符合结构的结果，不代表语音文本准确。镜头检测仅评估明显色彩硬切，不能由此推断渐变、语义分段或真实商家素材效果。字幕仍需人工审阅。

## Mandarin analysis 20261018

第二个固定样本为 12 秒中文合成语音，素材 `8d92b727-d72f-4bf5-9069-f6424643112a`，SHA-256 `70680dc889f64024189787803e1424ef2e3c887cb874e43bdab90a8db523aebc`。原文：“这是自动剪辑测试。第一个镜头是红色。第二个镜头是绿色。最后一个镜头是蓝色。”原始语音用本机已安装的 Microsoft Huihui/SAPI 生成，仅用于验收输入，不代表产品已接入配音能力。

实际请求 `m1-supplier-analysis-mandarin-002`，operation `fb3687868c57bb81fbdf25977c1ccfd9b55ded9cdd73780d0b96e2f75bc38175`：媒体探测 `dde8fd5f55104bbb9194a16c7164f34e`、FC `shots_<operation>`、ASR `10d31c6792904334945e7a909aaa7b48` 均成功，正常状态推进 14 次轮询，无人工恢复。两个切点精确为 4/8 秒，三张缩略图均取得；字幕四句位于 0.07–1.95、2.76–4.56、5.38–7.14、7.92–10.10 秒，均在素材范围内。

`scripts/evaluate_m1_analysis.py` 对比真实收据与输入标注：去掉空白和标点后 33 个汉字、编辑距离 0、此样本 CER=0；不视为通用语音准确率。分析对象 SHA-256 `c0f256af5ada07b4d9442cb40cac2127f82b8eb69c0027f9c03179b68040ea9c`；原始收据文件 SHA-256 `5e958f4b97565b3243ec8193a38d59522645ad001af416bfa482a1385a379be9`。原始收据与英语反例保存在 `data/deployment/evidence-v1/`，对比报告为 `data/deployment/m1-analysis-benchmark.json`。

据此仅将本私有验收环境的 `media-analysis-2-20261018` 分析能力标记 verified，绑定本节报告，限制仍为 30 秒、100 MiB、并发 1、总试用 6 次。它证明指定分析链路及该固定样本可用，不代表英语/真实业务样本或完整 M1 已验收。渲染能力继续 testing，不对用户开放。

## 恢复与失败记录

FC 已接受的任务在首次查询时返回未识别状态，旧客户端过早将 operation 置为失败。后续只读查询确认同一云任务 Succeeded，并读取、核验原结果对象后，显式恢复原 operation 的轮询。恢复收据 `data/deployment/m1-analysis-recovery.json`；配额仍为 1，既有 polls=123 保留，没有再次提交 FC。客户端现对同一身份的未确认状态在既定轮询上限内继续查询，不把未知状态映射为成功。

真实渲染两次均已确定失败，暂停继续提交：

| 请求 | operation | ICE Job | 实际结果 |
| --- | --- | --- | --- |
| `m1-supplier-render-001` | `6d93bb5313b7da610a2ec007e10df3ad2aba2b500e4f1bb253f118ca16458774` | `7dc80cbf869548a99d3554fd23182673` | `ProducePutOSSFileError`，Put oss file failed, reason: AccessDenied |
| `m1-supplier-render-002` | `9bcd1981ea50d1101ebd3fb1aaa4bcf0a5d98853e2ab27fa717c087b8cdc1414` | `f2bdbd1178364982ae2611c3dc752890` | 相同错误；关闭自动 IMS 输出注册、截图与雪碧图后仍失败 |

两次均为 6 秒时间线、两个实际裁切片段及英文字幕，输出位于已授权的 `tenants/<tenant>/renders/<operation>/output.mp4`。策略实际源码已核对为同一 Bucket 的 GetBucketInfo/GetBucketLocation 和 tenants/* 的 GetObject/PutObject，与批准方案一致；不能据此猜测具体缺失权限，不能通过 FullAccess 或公共 Bucket 试错。ICE 返回未提供内部 OSS 拒绝请求的具体 Action。失败查询收据在网关卷 `m1-render-failures.json`。

已根据[官方参数说明](https://help.aliyun.com/zh/ims/developer-reference/clip-composition-parameter-description)将帧率放入 `OutputMediaConfig.Video.Fps`；旧顶层 Fps 不符合此结构。关闭隐式派生文件后，项目自行管理分析与归档位置。以上修改没有将失败作业改为成功，也没有重置试用次数。

## 验证与费用边界

完整离线质量门含真实 PostgreSQL：312 passed、1 skipped，日志 `data/m1-final-gate.txt`。后续针对实际接入问题执行存储流专项 31 passed、区域/ASR 适配专项 40 passed、FC 状态恢复专项 23 passed、ICE 渲染适配专项 20 passed；这些集合重叠，不能相加或称为最新全套数量。相关 Ruff 和前端恢复脚本通过。

已知预付 CNY 20；两次 ASR 的公开价计算分项各 CNY 0.00834，不是确认账单。FC、OSS 与请求费用待出账核对，分析总额仍为 unknown。官方说明合成失败不收剪辑费，但不能由此推断所有关联费用为零。累计 USD 10 上限不变，网关持久配额目前分析 3、渲染 2，均未重置。

下一步：定位 OSS 写入拒绝的确切原因，完成实际页面上传→分析→计划→预览/成片→质检→归档→短期领取与播放。当前不能关闭 B04–B06，也不能认定小程序或公开生产交付已完成。

## 页面接入中的模型身份修正

浏览器在项目 `fd716afb-1b2b-4b0b-8d37-7287667e1bac` 真实直传中文素材，创建控制 Job `95d246d7-e63a-4473-85e1-446fa0eed090`。网关 operation `56435a14f8cf67964c95780da401eed034f72e860ae39d0f1a47098dcb5a0dfd` 已分析成功，但旧网关返回模型供应商别名 `aliyun-hosted`，注册表为 `aliyun-hosted-analysis`，Worker 按预期拒绝该模型快照，未产生可交付产物。

修复将分析/渲染新返回名称分别统一为注册表的 `aliyun-hosted-analysis`、`aliyun-ice-editing`。已完成旧作业仅对确切别名、模型 ID 和版本投影更正，返回 `gateway_identity_correction` 证据；保留原持久化收据、分析对象及其哈希，不接受任意供应商/版本替换，不弱化 ModelRegistry 检查。

同时修复“模型校验拒绝发生在 execute 返回之前时，Worker 未取得结果而漏记费用证据”的问题：异常保留仅供内部对账的原结果及已知远端任务 ID；三类 Worker 在拒绝交付时仍记录费用/使用证据，显式恢复可继续查询原作业，不自动重提。针对模型拒绝与费用保留的新增用例通过；Gateway、完成事务、Provider 执行等专项首轮 30 passed、新增测试因遗漏 approved 标记失败，修正测试准备后该项与模型注册表 11 项均通过。它们与此前测试重叠，不相加为最新全套数量。

此次修复包 `m1-identity-fix-v8.tgz` SHA-256 `3c95edbbfee8ab4166289bf2927ff00c0101d3636811da7e547a3cf562e18330`；源码与不可变镜像摘要见忽略目录 `data/deployment/m1-identity-release-v8.json`。部署或脚本准备本身不表示控制 Job 已恢复，后续需记录实际恢复结果。

## PostgreSQL 账务兼容与原分析结果保留

真实恢复首次在写入 `pending_reconciliation` 时失败：该状态 22 字符，旧 PostgreSQL 列为 VARCHAR(20)，SQLite 未暴露长度问题。已增加 `0007_usage_ledger_status` 扩至 40，旧行及费用证据保留。SQLite 迁移与实际独立 PostgreSQL 定向检查合计 11 项通过，含旧 20 字符列升级、预占保留、未知金额和重复迁移；不是新的全套质量门数量。

控制镜像 v5（`a5bffff54d246dd217f0a979feb0e1e3c9f016b21373068d5bb368da207924cf`）已部署。迁移前停止 Worker、生成服务器受限备份 `data/deployment/control-before-ledger-0007.dump`，随后单进程迁移、重建 API/Worker/私有入口；实际字段长度 40、迁移记录与 `/readyz` 均确认。修复包 SHA-256 `645f9245d24621af2674051217f655fe0f5a3acd4ad39d7854b503197eb6ee03`。

对原页面作业只查询既有云结果，严格检查规范身份、源哈希、镜头/字幕结构后，将已知远端 ID 及原费用证据保存。操作状态由 unknown 变为 result_received，账务为 pending_reconciliation，原 Job 仍 failed、执行次数 1，未再次提交云分析、未请求交付重试。恢复收据位于控制数据卷 `m1-page-analysis-recovery.json`，规范化读取收据 SHA-256 `7c4e41c743cfe20c4e9e388c9bfbfd41b69a999bdc2b98d0eec3a8b35d65b1c7a`。当前仍需处理实际账单延迟与生产分析路由遗漏可信预占的问题，不将保留证据称为页面验收通过。

## OSS 日志诊断

用户批准 SLS 后，第三次 6 秒诊断渲染仍失败，但取得精确证据：ICE 作业 `5cf7374c1e924884b1fd36d55ad5e102` 在写入 `icetemp/subtitle/<同一作业ID>/1792311912340.ass` 时被 OSS 以 403 AccessDenied 拒绝。该临时字幕路径超出已批 `tenants/*`，与输出成片 URI 不同。请求 ID `6AD482687DB8183236B5E9A4`，Action PutObject。诊断后已关闭新增访问明细采集；日志保留 7 天、一个可写分片、无自动扩片。当前配额为分析 3/6、渲染 3/6，未重置。候选最小权限增量及实际日志配置见 [日志诊断记录](m1-logging-plan.md)，未经新增确认不应用。

## Cloud render 20261018

用户明确批准后，RAM 自定义策略 `VideoAIStudioICEStoragePolicy` 当前版本已更新为 v2，仅为 ICE 角色增加项目 Bucket 的 `icetemp/subtitle/*` GetObject/PutObject；ECS 权限不变。当前策略镜像 `config/aliyun-ice-storage-policy.json` 与控制台实际来源语义一致，证明见 `data/approval/ice-subtitle-policy-v2.png`。前述 AccessDenied 已消失。

| 请求 | ICE Job | 实际输出 |
| --- | --- | --- |
| render-004 | `84c7da04bd9645a594f199ce9dee5a61` | 6 秒、720×1280、30fps、H.264/AAC，130360 字节；SHA-256 `3b4a5b6c55d5f5f56999ede5857543aa8335d8ea55fecf96b2aee6a7e14f9403` |
| render-005 | `fb180bccce494728bfd5f692c2259f22` | 相同裁切/字幕内容，显式 H.264/CRF23，120868 字节；SHA-256 `8cb9cd2bcd676fc793eef62c85a924154e48f0fc831dad5bd902b479a879524c` |

render-005 operation `2f0b0db061df6eaf98df48f9373b38904a244533bd7760ba11df1a6f0e280f1d`，manifest `fcace34f19c8650dbb1747eb78965671f4d2be7a12bee6899686d482ba78fa46`。源 1–4 秒红色、4–7 秒绿色映射到输出 0–3、3–6 秒；音量 0.8、静态英文字幕按同一区间切换。实际 OSS 对象再次读取并核验完整哈希，云端媒体探测和结构质检通过。

**抽帧误判已纠正。** 初次并发 PNG 输出出现不完整文字，不能作为云视频缺字证据。连续解码原视频全部 180 帧为 RGB，字幕白色像素范围在红色段稳定至 x=279、绿色段至 x=331；再用输出编码线程数 1、显式 RGB24 导出 89/150/179 帧，文字完整。问题出在本地 QA 的 PNG 输出路径，不是已确认的 ICE 字幕缺陷；不能宣称 CRF 设置修复了字幕。原异常截图保留，修正证据在 `data/deployment/evidence-v3/verified-frame-*.png` 与 `subtitle-frame-measurements.json`。本地仅解码检查实际成片，未替代平台云端渲染。

仅将已实测的单视频轨、裁切、原片音量、画幅 Contain 和基础静态字幕开放，绑定 API 2020-11-09 / SDK6.20.2 / compiler initial-v3-accepted-subset。图片片段、独立音轨、叠加、转场、自定义字体等需专项云端验证，当前执行前拒绝。字幕实测限此英文固定样本，不能推断中文字体和任意样式均通过。

每条成功剪辑按公开费率计算 CNY0.03，`bill_confirmed=false`，不等同于已出账金额。配额目前分析 3/6、渲染 5/6；没有重置。SLS 最终停止采集的证明为 `data/approval/sls-bucket-collection-stopped-final.png`，保留日志/角色/平台资源仍存在，不能声称后续费用全部为零。页面导出、归档和实际领取仍待完成，因此 M1 仍未通过。

## 原页面分析恢复与费用预占

控制 v6 修正生产分析路由遗漏预算预检的问题：所有生产任务先从服务端已验收能力目录锁定版本和保守报价，不能信任客户端零价。未知账单保留正数预占及原费用证据；仅在供应商、模型、版本、目录报价和当前预算均一致时允许业务结果交付，仍标记 pending_reconciliation。真实 PostgreSQL 发现的新任务并发锁死已通过 Tenant FOR NO KEY UPDATE 修复；定向预算/执行/完成与真实 PostgreSQL 复验 61 passed，非新的全套数量。

对旧页面 Job `95d246d7-e63a-4473-85e1-446fa0eed090` 显式记录运维恢复原因，从服务端目录补回 CNY0.50 可信预占，经正常 retry 接口恢复。实际状态 succeeded、尝试次数 2、分析已写入素材；原云 operation、源哈希及 Provider 请求哈希保持不变，未再次提交收费分析。原请求哈希 `f6e1db6b125531f54088b1abc3baca6827bed9846c45a0f78c9f9e31fd6b8525`；恢复收据在控制卷 `m1-page-analysis-reservation-recovery.json`。费用金额仍未知，未作为零费用结清。

控制 v7 同时修正成功任务显示旧失败、待对账显示为预算正常，以及刷新覆盖用户当前模板的问题；离线前端恢复脚本通过。页面正确的品牌模板计划 `8883a200-0ad0-41dd-a83d-45f516c05740` 为 12 秒、三个 4 秒源片段、1080×1920。此前 15 秒模板计划需要显式补帧或变速，当前编译器会拒绝，不以截断源片段方式偷偷补足。

## 阶段质量门与页面导出契约

本轮阶段质量门为 **337 passed、1 skipped**，含真实 PostgreSQL，日志 `data/m1-stage-final-20261018.txt`；Ruff、编译、前端语法和恢复、Provider 契约检查均通过。跳过的是主机缺少 FFmpeg/ffprobe 的旧本地媒体适配器 smoke，未跳过 PostgreSQL 或将其冒充云验收。

随后实际页面高清导出创建 Job `16cfdf8c-86bc-4d34-8f6c-b76e22bdf3a6`，原 manifest hash `742cd2600c69e3250e7e49b78e690c162dfcfe49d41f5b62531a21e8f105d80e`。网关只接受 draft/final，拒绝控制 API 的 high 及 `source=studio-flow`，HTTP422 发生在持久化 operation/云提交之前；真实查询确认 operation 不存在、渲染配额仍 5。修复接受共享 API 的 standard/high/studio 质量档并映射成片配置，保留原 manifest 和哈希；source 仅为受限字符串入口元数据，其余未知编辑选项仍拒绝。定向媒体/网关复验 **29 passed**、Ruff 通过，日志 `data/m1-workspace-render-contract-recheck.txt`，不与上述全套结果相加。

gateway v11 的修复包和镜像摘要见 `data/deployment/m1-workspace-render-release-v11.json`；后续复用原控制 Job 正常重试，不能新建多个收费任务绕过失败或重置配额。


## 页面成片交付与 M1 结论

原高清导出 Job `16cfdf8c-86bc-4d34-8f6c-b76e22bdf3a6` 经正常 retry 成功，尝试次数 2，manifest 哈希保持不变；唯一实际 ICE 作业 `78b9e62243a24d71b5cc55946af0d9c0`，媒体信息作业 `90348a0cf0eb4b61bad2afd56c3d2242`。输出 **12秒、1080×1920、30fps、H.264、有音轨、233776字节**，SHA-256 `5047edb0566fed221fa748f48c7027626c87ffdfb791cf386b95449178b8effe`。2/6/10秒实际帧分别为红/绿/蓝，符合三个4秒镜头。该最终模板未自动烧入 ASR 字幕；英文字幕另由 render-005 验证，不冒充中文字幕合成验收。

Artifact `c49e62a8-22c5-4ddd-ae71-154e12b7c313` 已复制到私有 `tenants/<tenant>/artifacts/<artifact>/output`，哈希/大小与云渲染输出一致，保留期至 **2026-11-17 09:10:48 UTC**。公开 API 只返回300秒以内的短期授权；不将签名 URL 写入元数据/收据。实际 download 返回200、video/mp4、Content-Disposition attachment；2秒诊断授权失效后403、无签名403，重新通过正常 API 取得授权后 Range 返回206及1024字节；未认证401、有效其他租户声明404。第一次人工诊断脚本使用不支持的 purpose=playback 得到422，改为文档定义的 preview 后通过，不属于产品缺陷。

Chrome 从实际工作台链接下载 `C:/Users/torney22/Downloads/output.mp4`，大小及完整哈希一致。控制 v8 将高清产物显示在既有播放器，并明确标注“估算费用”；真实浏览器加载宽高1080×1920、duration12、readyState4、无媒体错误，按播放控件播放至12秒 ended=true。页面证明 `data/approval/m1-final-video-playback.png`，本地交付副本 `data/deployment/evidence-final-v4/m1-page-final.mp4`。v8仅为前端展示调整，语法和已有恢复回归通过；不重复全套。当前部署 control v8 / gateway v11，摘要见对应 release JSON。

最终证据包 `m1-evidence-final-v4.tgz` SHA-256 `8af9b817f5dd17bb8b0b815139c41c52e9f50f5869203a156d85c92f72270055`，已核验并解包到 `data/deployment/evidence-final-v4/`；包含原 Job/manifest、QC/归档收据、恢复操作、领取/隔离结果及实际视频。分析配额3/6、渲染6/6，持久计数从未重置；不再发起新的真实生成测试，后续测试先核对账单及额度。

**费用没有最终结清。** CNY20订阅预付已确认；三条成功渲染按公开费率计算0.03+0.03+0.06，均 bill_confirmed=false。页面 Job 的旧通用金额账簿状态 settled 表示应用已将费率计算的0.06记入用量预算，不代表阿里云出账；原 cost.status=calculated/bill_confirmed=false 与费用证据完整保留，不能将该条描述为供应商确认账单，也不覆盖既有记录。分析的实际总费用仍未知，CNY0.50预占继续保留；FC/OSS/SLS及请求/流量费用待出账，后续差额按追加对账流程处理。累计USD10和SLS诊断预留USD0.25不变，不能据此声称剩余美元金额或零后续费用。

M1 的 B01–B06 在指定私有环境、固定授权样本和上文限定功能范围内通过。未实测功能仍拒绝，英语ASR及真实业务效果保留局限；M2继续双端身份、独立制作、不可变编辑版本、完整字幕音轨和批量工作台。公网域名仍未切换，小程序未创建/真机验证，不能将 M1 通过称为整体 Goal 完成。

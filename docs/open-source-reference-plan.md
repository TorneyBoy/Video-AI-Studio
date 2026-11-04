# 开源参考与实施增量

更新：2026-11-04。基于本轮对 64 个仓库的[只读筛查快照](../third_party/open-source-screening-2026-11-04.json)，选出 12 个主参考和 2 个有条件的架构参考。选择顺序为业务匹配、关键源码与测试、现有架构适配、代码与权重许可、维护情况、Stars；Stars 只是当日快照。

本文补充[开发路线](development-roadmap.md)与[综合产品方案](integrated-product-design.md)，沿用 A–H 任务 ID；实际完成情况只以[执行进度](development-progress.md)及对应证据为准。参考项目入选不表示已经接入、已运行上游测试、已取得商用授权或真实效果已验收。以下“交付物”均为后续实施要求。

## 固定的架构边界

- 保留 FastAPI 控制面、PostgreSQL、DBWorker、Job/ProviderOperation、预算预占与结算、统一完成事务及不可变编辑版本。参考项目不整体替代这些组件。
- 生产维持 `PROVIDER_MODE=api_only`、`ASSET_STORAGE_MODE=external`，媒体计算使用外部托管服务，客户端直传对象存储；不要求用户自备或租用 GPU。上游本地模型、Docker、FFmpeg/WebCodecs 示例只提供合同和算法参考，不直接进入控制面生产执行路径。
- 网页与原生 TypeScript 微信小程序共用 API、能力、操作语义和版本；保留原生小程序，不因上游使用 React、Qt 或 Rust 改写全部客户端。手机必须独立配置、执行、查看结果并恢复失败。
- Canonical Timeline 仍是本平台的编辑权威。上游 EDL、工程文件、HTML composition、语义图只能经过显式适配，不能新增另一套隐式权威状态。
- 真实供应商、模型、价格及支持效果须进入[能力目录](capability-catalog.md)。当前版本不能渲染的编辑效果继续由服务端在新任务和收费提交前拦截。

## 12 个主参考及首个交付物

### 素材理解、文字剪辑与参考策划

| 参考与代码许可 | 学习的合同或算法 | 对应任务与首个交付物 | 官方源码与验证入口 |
| --- | --- | --- | --- |
| **browser-use/video-use**；MIT | 自有素材 EDL、源时间到成片时间的词级字幕映射；剪辑和渲染分离 | **B03、C06、C09、E03/E06**：将文字选择编译成带素材/分析版本身份的源区间，转换为本平台操作和 Timeline；缓存键绑定内容、模型、选项及版本，不照搬路径缓存 | [render.py](https://github.com/browser-use/video-use/blob/43cfc566833548093455c9d4a46bd69913b7e552/helpers/render.py)、[transcribe.py](https://github.com/browser-use/video-use/blob/43cfc566833548093455c9d4a46bd69913b7e552/helpers/transcribe.py)、[字幕测试](https://github.com/browser-use/video-use/blob/43cfc566833548093455c9d4a46bd69913b7e552/tests/test_render_captions.py) |
| **modelscope/FunClip**；MIT | 中文文字/说话人定位到媒体区间；重复文本的多次命中 | **B03、C06、E03/E06**：保留词/句/说话人及命中序号，重复语句须明确选定发生位置；输出可撤回的剪辑建议，不直接删除原素材 | [videoclipper.py](https://github.com/modelscope/FunClip/blob/cdcb53fe3d4b37365178ae4f779296e72b7beb6a/funclip/videoclipper.py)、[重复文本测试](https://github.com/modelscope/FunClip/blob/cdcb53fe3d4b37365178ae4f779296e72b7beb6a/tests/test_duplicate_text_matching.py) |
| **linyqh/NarratoAI**；MIT | 参考抽帧、结构化观察、时间范围、解说与保留原声策略 | **E01–E03、E06**：把观察证据与生成要求分开写入参考/分镜版本，保留来源区间和失败原因；先连接现有来源采用版本到 Timeline，再扩展智能策划 | [参考帧分析](https://github.com/linyqh/NarratoAI/blob/29a11b6cdf61b69ba90321fc5babc160e0ca2657/app/services/documentary/frame_analysis_service.py)、[任务流程](https://github.com/linyqh/NarratoAI/blob/29a11b6cdf61b69ba90321fc5babc160e0ca2657/app/services/task.py)、[分析测试](https://github.com/linyqh/NarratoAI/blob/29a11b6cdf61b69ba90321fc5babc160e0ca2657/tests/test_documentary_frame_analysis_service.py) |

落点为现有 `analysis_contract.py`、`shot_requirements.py`、`auto_edit.py`、`storyboards.py` 与 `shot_matching.py`。路线 **R02** 同时升级分析输出和 `subtitle_authoring.capture_transcript` 捕获的旧 `transcript/1` 快照，保留旧版可读性，不能只加词级响应却让编辑版本丢失新字段。未返回可靠时间戳的服务不能伪造精确剪辑依据。用户标注继续标为人工依据，不能包装成模型语义分析。

### 编辑、品牌模板与确定性渲染

| 参考与代码许可 | 学习的合同或算法 | 对应任务与首个交付物 | 官方源码与验证入口 |
| --- | --- | --- | --- |
| **Augani/openreel-video**；MIT | typed actions、校验/逆操作/分组历史；隔离草稿执行，提交前核对项目 revision，成功后一次进入历史 | **C02、C04、C07–C09、G01**：统一网页/小程序的操作描述与客户端草稿协调器，将动作映射到既有服务端 operations/CAS；先覆盖分割、裁切、移动、字幕和音轨，再扩展 AI 编辑 | [ActionExecutor](https://github.com/Augani/openreel-video/blob/main/packages/core/src/actions/action-executor.ts)、[操作测试](https://github.com/Augani/openreel-video/blob/main/packages/core/src/actions/action-executor.test.ts)、[EditingSession](https://github.com/Augani/openreel-video/blob/main/apps/web/src/services/agent/editing-session.ts)、[会话测试](https://github.com/Augani/openreel-video/blob/main/apps/web/src/services/agent/editing-session.test.ts)、[触屏测试](https://github.com/Augani/openreel-video/blob/main/apps/web/src/components/editor/timeline/Timeline.touch.test.tsx) |
| **mltframework/shotcut**；GPLv3，关键源码头允许后续版本 | 成熟多轨、源区间、转场、字幕和音频的联动；可撤销命令与媒体效果基准 | **B04、C08–C10、H02**：建立主视频/固定轨道/转场重叠/字幕/混音的行为矩阵和固定样本；优先自行实现本平台规则与测试。MLT 工程交接是可选适配，不替换 Timeline | [多轨模型](https://github.com/mltframework/shotcut/blob/master/src/models/multitrackmodel.cpp)、[时间线命令](https://github.com/mltframework/shotcut/blob/master/src/commands/timelinecommands.cpp)、[字幕测试](https://github.com/mltframework/shotcut/blob/master/tests/test_subtitles.cpp)、[混音顺序测试](https://github.com/mltframework/shotcut/blob/master/tests/test_mix_transition_order.cpp)、[许可](https://github.com/mltframework/shotcut/blob/master/COPYING) |
| **heygen-com/hyperframes**；Apache-2.0 | 品牌/商品变量与渲染规格分离；默认值解析后冻结执行计划；多片段音频准备和混音 | **B04、C12、D02–D04、F02/F04**：增加模板参数 schema、版本及固定实例，将每个显式变体编译成可预检清单，再按现有报价/BatchItem/Job 调外部渲染；拒绝隐式组合展开 | [冻结计划](https://github.com/heygen-com/hyperframes/blob/main/packages/cli/src/commands/render/plan.ts)、[计划测试](https://github.com/heygen-com/hyperframes/blob/main/packages/cli/src/commands/render/plan.test.ts)、[混音实现](https://github.com/heygen-com/hyperframes/blob/main/packages/engine/src/services/audioMixer.ts)、[多片段混音测试](https://github.com/heygen-com/hyperframes/blob/main/packages/engine/src/services/audioMixer.manyTracks.test.ts) |

OpenReel 的 `executeMany` 只是遇错停止，不能把它当作原子事务。参考的原子边界是隔离 `EditingSession`；本平台最终仍由服务端版本条件、权限和事务决定是否发布。客户端撤销也不撤销已经发生的供应商费用。

客户端同步收敛统一 TaskView/DTO 和只保存第一方请求元数据的 pending 协调器；结果未知先核对原回执，保留原键/原文。旧 retry/cancel 入口逐步收敛到现有 billing 安全 actions，携带预期状态、尝试次数和执行代次；字段不足时显示不可操作原因，不回退到宽松旧接口。

Shotcut 的 Qt/GPL 实现用于研究行为、制作基准和可选工程互操作；复制或链接代码须单独评估许可，不当成 MIT 网页组件。其本地 Whisper 转写与 Kokorodoki/Kokoro 配音可参考“配置/试听→异步任务→音频素材”的产品流程，生产执行仍接外部 API。[官方转写流程](https://forum.shotcut.org/t/subtitles-speech-to-text/46306)、[官方配音流程](https://forum.shotcut.org/t/subtitles-text-to-speech/50221/1)。

### 配音、声音与生成服务

| 参考与代码许可 | 学习的合同或算法 | 对应任务与首个交付物 | 官方源码与验证入口 |
| --- | --- | --- | --- |
| **QwenAudio/CosyVoice**；Apache-2.0 | 中文/跨语言合成、可复用说话人提示及服务输入输出 | **E05、G03/G04**：托管服务映射为持久异步 TTS 作业，固定文本版本、语言、声音引用和输出规格；同步 PCM 示例不能直接作为公开生产协议 | [调用示例](https://github.com/QwenAudio/CosyVoice/blob/main/example.py)、[模型执行](https://github.com/QwenAudio/CosyVoice/blob/main/cosyvoice/cli/model.py)、[FastAPI 示例](https://github.com/QwenAudio/CosyVoice/blob/main/runtime/python/fastapi/server.py)、[许可](https://github.com/QwenAudio/CosyVoice/blob/main/LICENSE) |
| **QwenLM/Qwen3-TTS**；Apache-2.0 | 预设说话人、音色设计与可复用 voice-clone prompt | **E05、G04**：把授权样本/供应商声音 ID/模型版本绑定 VoiceProfile；普通配音和首次创建克隆声音分开报价，撤销后阻止后续执行 | [推理接口](https://github.com/QwenLM/Qwen3-TTS/blob/main/qwen_tts/inference/qwen3_tts_model.py)、[模型调用自测示例](https://github.com/QwenLM/Qwen3-TTS/blob/main/examples/test_model_12hz_base.py)、[许可](https://github.com/QwenLM/Qwen3-TTS/blob/main/LICENSE) |
| **resemble-ai/chatterbox**；MIT | 参考音频条件下的声音转换；输入音频、目标声音和输出音频分离 | **G05**：先接无训练型托管转换，保留原音频和派生版本，核对内容/时长/听感；共享实例的参考条件状态必须按任务隔离 | [VC 实现](https://github.com/resemble-ai/chatterbox/blob/master/src/chatterbox/vc.py)、[VC 示例](https://github.com/resemble-ai/chatterbox/blob/master/example_vc.py)、[许可](https://github.com/resemble-ai/chatterbox/blob/master/LICENSE) |
| **Huanshere/VideoLingo**；Apache-2.0 | ASR、断句、术语/翻译审校、配音与时长适配分步；版本化转写缓存 | **C09、E05/E06、G03**：原文、修订、译文和合成音频独立版本；超时长先进入修订/调速/重新合成选择，不照搬静默截断音频 | [pipeline](https://github.com/Huanshere/VideoLingo/blob/01f843492759be9bbfd1a5cb697b799cc9de1609/core/pipeline.py)、[缓存测试](https://github.com/Huanshere/VideoLingo/blob/01f843492759be9bbfd1a5cb697b799cc9de1609/tests/test_transcription_cache.py)、[音频生成](https://github.com/Huanshere/VideoLingo/blob/01f843492759be9bbfd1a5cb697b799cc9de1609/core/_10_gen_audio.py) |
| **Wan-Video/Wan2.2**；Apache-2.0 | 文生/图生视频、驱动动画分支的不同输入合同与参数边界 | **E04、G07**：先选一个真实托管能力完成镜头级报价、提交/查询/取消、候选比较与采用，再编译 Timeline；不同模型/分支分别验收 | [生成入口](https://github.com/Wan-Video/Wan2.2/blob/main/generate.py)、[图生视频](https://github.com/Wan-Video/Wan2.2/blob/main/wan/image2video.py)、[Animate](https://github.com/Wan-Video/Wan2.2/blob/main/wan/animate.py)、[测试说明](https://github.com/Wan-Video/Wan2.2/blob/main/tests/README.md)、[许可](https://github.com/Wan-Video/Wan2.2/blob/main/LICENSE.txt) |
| **TMElyralab/MuseTalk**；代码 MIT，依赖另列 | 音频驱动嘴部生成、人物预处理与重复使用人物输入 | **G06**：音频 Artifact＋授权 AvatarProfile→口型作业→逐镜头候选；核对身份、嘴部边缘和音画同步，结果回普通素材库 | [推理](https://github.com/TMElyralab/MuseTalk/blob/main/scripts/inference.py)、[实时示例](https://github.com/TMElyralab/MuseTalk/blob/main/scripts/realtime_inference.py)、[代码与依赖许可](https://github.com/TMElyralab/MuseTalk/blob/main/LICENSE) |

上述模型示例或自测脚本不等于已通过本平台的回归或质量验收。模型调用只发生在合格外部托管服务内；本轮不据此安装权重或建设 GPU 服务。动作驱动、嘴部生成、整个人物替换和精准换脸分别建合同与样本，不能相互代替验收。

## 两个有条件的架构参考

### hypit-ai/hypit：意图图、采用来源与显式复用

参考 Author/Recipe/Run 分离、语义锚点和只执行目标依赖路径，服务于 **E02–E04、F02/F04**。本平台对应为“分镜要求/模板版本→用户采用的来源版本→固定 Timeline/执行清单”，不另设一套权威 DSL 数据库。

当前合同明确 **没有 implicit cache**。每次 `build` 都有新的 Build ID；跨 Build 复用要显式声明 `build-record + satisfy`，用精确的 `build + output` 地址。Planner 依据选择剪除被替代操作；完整 public Output 可以 forward 到历史完成结果，不复制媒体。外部 `file` 仍是活引用，不能代替本平台固定对象/哈希证明。

执行上下文丢失后不会恢复原 attempt，继续工作需要新 Build 显式复用。因此只借图与产物采用规则，**A03/A04、C12、D03 继续用本平台原 Job/ProviderOperation 的远端恢复、结算与迟到完成合同**，不把新 Build 当成原收费请求恢复。

许可是附条件 Apache；当前条款对第三方托管、多租户和商业再分发要求书面授权。未经对应授权不作为本项目托管平台依赖或复制其实现。依据：[LICENSE](https://github.com/hypit-ai/hypit/blob/main/LICENSE)、[Run 合同](https://github.com/hypit-ai/hypit/blob/main/docs/quickstart/run.md)、[Run 测试](https://github.com/hypit-ai/hypit/blob/main/packages/markup/test/run/run.test.ts)、[显式 forward 测试](https://github.com/hypit-ai/hypit/blob/main/packages/result/test/result.test.ts)、[执行上下文丢失测试](https://github.com/hypit-ai/hypit/blob/main/packages/runtime-local/test/execution-process.test.ts)。

### calesthio/OpenMontage：分阶段生产、审批与缓存边界

参考方案、素材、声音、剪辑、渲染之间的阶段合同及人工审批位置，服务于 **E07、F04、H02**；将阶段输出映射为本平台已有版本和 Artifact，将收费动作映射为现有报价确认。单文件系统的产物缓存不等于多租户内容身份、数据库租约或分布式执行恢复。

代码采用 AGPL-3.0；对复用、修改及网络服务的义务单独评估。优先学习合同和边界，在本平台实现，不以 Agent 流程或缓存目录整体替换执行内核。依据：[许可](https://github.com/calesthio/OpenMontage/blob/d21f5d4eaa08422b09c7091757a05affcbabb35b/LICENSE)、[pipeline_loader](https://github.com/calesthio/OpenMontage/blob/d21f5d4eaa08422b09c7091757a05affcbabb35b/lib/pipeline_loader.py)、[asset_cache](https://github.com/calesthio/OpenMontage/blob/d21f5d4eaa08422b09c7091757a05affcbabb35b/lib/asset_cache.py)、[缓存测试](https://github.com/calesthio/OpenMontage/blob/d21f5d4eaa08422b09c7091757a05affcbabb35b/tests/lib/test_asset_cache.py)。

## A–H 实施与验收映射

| 工作包 | 本轮参考产生的实施要求 | 完成证据 |
| --- | --- | --- |
| **A：A02–A05、A08/A09** | 共用操作/产物合同、来源身份及统一完成路径；上游缓存、内存 history 和新 Build 不能绕开本平台幂等、权限、账务与执行代次 | 受影响合同/迁移检查；真实 PostgreSQL 并发验证旧代次、重复回调、取消后迟到结果及费用；不为套上游关闭边界 |
| **B：B01–B06** | 将分析、TTS、视频与渲染适配为既有 HTTPProvider；固定支持字段、模型/价格版本和输出证明；真实服务一次只验一个明确能力 | 注册/准入/失败合同；授权样本实际上传→分析→计划→渲染→归档领取；真实输出及账单单列 |
| **C：C02–C12** | OpenReel 操作协调、原生触屏语义、Shotcut 媒体规则；补共享 DTO 与能力清单；冻结版本的字幕/音轨与预览一致 | 两端同记录/同版操作；409、断线、迟到响应、账号/项目切换；真实烧录、混音、转场和手机保存/分享 |
| **D：D01–D05** | HyperFrames 固定变量/规格映射到现有批次版本和轮次；显式选择子项、复用已采用产物；每个收费操作继续原报价/原键 | 网页和手机各独立十项真实批次，含部分失败、限额、断网恢复、审核、领取及实际费用；本机元数据检查不替代 |
| **E：E01–E07** | 先打通参考证据→手工分镜→来源采用→Timeline，再加智能策划、选定缺口生成和配音；词级选段及语义锚点需用户可修订 | 可追溯参考区间/事实；重复语句明确选中；只对选定缺口创建收费作业；四流程双端完整样本 |
| **F：F01–F05** | 商品事实/品牌资料版本、参数化模板、显式变体清单；不同客户不共享私有来源；成员审核/提醒仍是独立任务 | 修改资料不改历史输出；数量/预算预览与实产一致；角色/撤销及用户选择提醒实测。高星编辑器不代替协作授权设计 |
| **G：G01–G09** | 先用 TTS→口型及声音转换建立工具结果回素材/时间线闭环；每类工具独立能力和授权快照；G02/G08/G09 继续专项供应商评估，不因主参考未覆盖删减 | 代码/权重/服务条款与固定模型绑定；声音/身份/动作/修复/封面各有真实样本；未合格能力保持未完成 |
| **H：H01–H06** | 参考源码与合同版本可追溯；复现边界用例并建立平台验收集；记录真实效果、时延、费用、故障和双端发布 | 源码/模型/配置清单、报告与回滚证据；新版本重验；实际部署、恢复、账单及微信平台/真机结果单列 |

## 实施顺序与每项复用的交付格式

1. **先收敛合同与恢复（A、C02/C07/C12）**：列出同一操作的输入、版本、权限、幂等键、报价、结果和未知状态；把网页/小程序重复状态逻辑收为纯函数与薄平台适配，先保住现有闭环。
2. **再打通现有断点（B、C、D）**：固定服务和样本验证新增编辑效果与十项批量；已有 M1 受限结果不能自动覆盖新增规格、字幕、混音或当前源码。
3. **推进参考与声音（E）**：R06 普通配音与 R07 来源到 Timeline 的编译可在词级合同和编辑操作稳定后并行；参考侧补真实语义与智能编导，随后 R08 只生成选定缺口。编译保留旁白、文字和声音要求，无合格服务时保留真实验收缺口。
4. **扩展品牌与 AI（F、G）**：在明确版本/授权/预算之上增加模板变量、语言变体和专项工具；H 的许可、样本和运维证据随每批累积。

每个实际复用项必须形成一份可审阅记录：**原任务 ID、参考仓库/固定 commit/文件、学习的行为或摘取的代码、代码与权重/服务许可、改动落点、双端入口、兼容边界、检查结果、真实媒体/费用证据、遗留问题**。链接指向 `main/master` 的入口只是阅读定位，实际采用时先冻结 commit，不随上游浮动升级。模型示例没有自动化测试的，明确写“调用示例”，不冒称上游回归通过。

路线 **R07** 允许含“缺镜头/待生成/补拍”项的分镜与来源版本保存，便于继续制作；缺口须显式进入编辑计划，不能偷偷补入其他素材。正式渲染由服务端拒绝未满足必需镜头的版本，待用户明确采用合格来源后再报价与提交。

## 代码、权重与托管许可分别准入

MIT/Apache 表格列说明参考仓库代码的许可；它们不自动覆盖权重、训练数据、声音样本、人物资产、字体、编解码器和全部传递依赖。MuseTalk 的 LICENSE 还单列了多项依赖，不能用 GitHub 元数据中的 `NOASSERTION` 代替阅读文本，也不能据此一并批准模型。

许可记录至少将**代码、核心权重、附属组件、音色/人物授权**分列。按[模型注册](model-registry.md)逐项记录 `code_license`、`weights_license`、`dataset_terms`、第三方许可、地区/商用限制及授权资产；托管服务使用确切模型版本与适用服务条款，不伪造不可取得的权重哈希。需要保留版权/NOTICE、披露修改或履行 copyleft 的复用项，在发布包中分别落实；“只作为外部进程运行”不自动免除许可义务。

公开能力须同时具备供应商许可、价格、版本合同与效果验收。新增适配器、抄入示例、静态通过、真实效果达标和正式生产部署分别记录，不能互相代替。

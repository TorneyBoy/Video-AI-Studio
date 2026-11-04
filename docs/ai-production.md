# AI补镜头与人物替换

已实现制作入口、报价确认、原请求恢复、托管任务、候选结果及显式采用的代码链路；本轮新增场景版本、严格多模型参数、冻结执行提示与选择依据、质量finding和完整讲话证据保护，详见[六工作包进度](development-progress.md)及[问题清单](quality-issue-register.md)。Web 与原生微信小程序均使用同一业务 API；小程序不是 WebView 包装。已取得真实文生视频、官方人物替换示例和自动剪辑实际成片的有限证据；独立本地纯换脸短样例也已产出。**代码实现、样例受限验收、商业生产可用和完整交付分别记录；M2 整体仍未验收。** 示例配置不会自动开放功能。

## 本批三条真实成片与双模型结果

用户首批商品素材已完成真实转写、剪辑及独立复验，提供打底衫49.567秒、口红52.467秒、水杯28.7秒可观看文件；音量差异已修正到-18LUFS，保留原分辨率和字幕，未采用不同款商品图。查看[视频总览](../data/testing/real-delivery-20261104/delivery.html)、[质量报告](../data/testing/real-delivery-20261104/quality-report.md)及[费用报告](../data/testing/real-delivery-20261104/cost-report.md)。

同一水杯参考图的Wan2.2 I2V Plus/Flash两模型实际输出均5秒/800×480/30fps，严格解码通过；本brief因推镜超量及杯底越界均不采用。新增3转写+2生成共5POST，用户单独授权2CNY，usage原价估1.22384CNY、实付待核；旧费用仍保留对账。独立runner按原任务回读确认并新增精确结果主机allowlist后取回，未重提任何模型任务。

这里记录本机真实剪辑与独立供应商调用，不等于生产模型目录/网关/分析适配、托管采用、真机或商业发布已经验收。以下旧单样例及历史批次证据保留。

## 2026-11-04 实现与真实验收状态

| 范围 | 已有证据 | 尚未完成 |
| --- | --- | --- |
| 双端业务代码 | 补镜头、人物替换、纯换脸入口及原子授权、报价/任务/采用/原请求恢复已接入；新增场景、自动/指定模型、严格参数与实际选择依据；双端Node合同和原生TS检查通过 | 微信平台/真机、完整项目制作与交付验收 |
| 自动质量门 | 本轮冻结源码 **1061 passed、74 skipped、179 warnings，ALL: PASS**；pytest 675.67秒，见[完整日志](../data/testing/work-packages-20261104/quality-gate.log)及[冻结源码与验证摘要](../data/testing/work-packages-20261104/verification-summary.json)。原858/74保留历史 | 73项真实PG与1项ffmpeg/ffprobe检查未执行；图片exact、真实ASR/多模型与设备/生产仍待验，不累加专项 |
| 真实文生视频 | `wan2.2-t2v-plus` 的 [wan-real-t2v.mp4](../data/testing/ai-production-20261104/wan-real-t2v.mp4) 已本地取回：5 秒、480×832、30fps、150 帧、无音轨，全片严格解码通过 | 后半段旋转变化较弱；尚未确认全程持续匀速旋转、细微时序质量或通用效果，1080P/I2V 未由该样例验收 |
| 真实人物替换 | 原任务 `edc31143-45b8-4f11-a0c4-36d31c4a48a0` 成功，已取回 [wan-real-person.mp4](../data/testing/ai-production-20261104/wan-real-person.mp4)：710,099 字节、5.2 秒、1252×704、15fps、78 帧；严格解码与基本人物替换抽帧检查通过 | 仅官方 API 示例技术验证；手部边缘偏软、精细口型与连续视听未验收，客户授权/托管归档/控制面采用/生产全链路未通过 |
| 纯换脸 | API/UI和独立能力检查已接入；FaceFusion在隔离本地环境真实产出首48帧，2.002秒、1282×720、23.976fps，局部脸部变化且服装/身体/背景在样本中保留，预览带AI标识 | 模型非商业限制仍阻止生产catalog；未接入主营业务Provider，完整序列、精细质量和客户授权全链路未验收 |
| 自动剪辑复用 | 实际 API→Planner→worker 持久版本→preview manifest→真实成片已贯通；原云Job `a3da7e2b5d7c4ae58dfc1ea68ae01755`，72,526字节，3秒、480×832、30fps、90帧，全片解码及源0–3秒对照通过，均值SSIM 0.988093 | 仅单素材结构裁剪受限验收；生产仍因缺真实shots返回 `analysis_not_ready`，语义/ASR/多素材排序未验收 |
| 新渲染效果 A | 原作业 `06b05feb7ef3456681a4bc73b71bd741` 的210,616字节[MP4](../data/testing/ai-production-20261104/ice-effects/effects-basic-v1.mp4)完成QC：中英字幕、透明图片Contain/位置、独立440Hz音轨、增益及线性淡化部分通过 | Logo实际frame15–135，目标15–134，多1帧；exact Timeline组合未通过 |
| 边界诊断完成 / 原B | 2秒四图原Job `b2e5ec2563cf497fa265fe7aad7a124d`已取回，两套独立60帧QC一致，云回读Timeline逐字段与提交一致 | 四种写法均未得到目标30帧，没有可安全采用的修复；图片exact组合保持未验收关闭，原B未提交，本轮不再追加ICE任务 |

历史完整总门日志：[quality-gate-person-output.log](../data/testing/ai-production-20261104/quality-gate-person-output.log)，人物实际尺寸/模式帧率准入修正已纳入该门。图片诊断没有精确修复；本轮新增场景、多模型和质量守卫后，不再将858/74视为当前全部代码的全量结果。当前专项与最终统一检查在[执行进度](development-progress.md)记录；软件总门不等同图片exact效果验收。更早 [quality-gate.log](../data/testing/ai-production-20261104/quality-gate.log) 的825/74也保留为历史，不累加。T2V 的 [QC 说明](../data/testing/ai-production-20261104/wan-real-t2v.qc.md)、[结构化 QC](../data/testing/ai-production-20261104/wan-real-t2v.qc.json) 和 [严格解码日志](../data/testing/ai-production-20261104/wan-real-t2v.decode.log)记录了有限结论；人物结果见 [QC 说明](../data/testing/ai-production-20261104/wan-real-person.qc.md) 和 [结构化 QC](../data/testing/ai-production-20261104/wan-real-person.qc.json)。这些本机证据位于忽略目录，不随源码分发。

两条样例分别估 **0.70 CNY（T2V）**、**3.12 CNY（人物标准模式，5.2 秒 × 0.6 CNY/输出秒）**，合计估 **3.82 CNY**。实际账单均未核；该合计不是已结算总额，也未包含可能独立产生的 OSS/ICE 等费用。

M1 历史分析 **3/6**、渲染 **6/6** 原样保留；本轮Wan、新增ICE样例和独立本地技术探针另立台账，不重置或扩充旧配额。原累计 **10 USD** 约束、已确认 **20 CNY** 体验包预付和既有待对账费用保持，不推算未经核对的余额或免费额度。

### 人物样例的有限 QC 与输出规格修正

本条替换了完整人物外观及服装，主要动作和背景在抽帧中大体保留；输出可见“通义AI生成”水印。它不是纯换脸，也未证明背景逐像素不变。手指与运动手部边缘偏软，精细手形、口型同步、细微闪烁及整片连续视听仍未验收。

音轨主体的共同解码区间相关系数为 **0.999784**，最佳时移为 **0**；输出 PCM 比原音轨少 **64 毫秒**。尚未分清编码填充和有效尾音截短，不能称为无损保留，也不能据波形相关证明口型同步。该官方示例没有建立客户 Consent，未完成托管 OSS 归档、ICE 探测或控制面采用，不是客户授权业务全链路验收。

实际输出为 **1252×704、15fps**，固定短边 720 的旧假定不成立。人物结果准入修正已完成：按模式核对标准 **15fps** / 专业 **25fps**，保存实际宽高和时长，不改标或缩放媒体来凑固定 720P；相应回归已纳入上述历史858/74总门。专业模式 25fps 是合同范围，未由这条标准模式样例实测通过，客户授权业务全链路仍需单独验收。

### 真实生成素材接回自动剪辑

[业务集成报告](../data/testing/ai-production-20261104/auto-edit-real-source.md)记录隔离开发库中实际 API、Planner、worker 和持久版本链，来源边界、manifest哈希及计划/预览同键重放均通过。其后真实渲染已取回 [auto-edit-real-v1.mp4](../data/testing/ai-production-20261104/auto-edit-real-v1.mp4)，原云Job `a3da7e2b5d7c4ae58dfc1ea68ae01755`，72,526字节；[成片QC](../data/testing/ai-production-20261104/auto-edit-real-v1.qc.md)确认3秒、480×832、30fps、90帧完整解码，源0–3秒按索引逐帧比较均值SSIM **0.988093**，云回读Timeline与planner持久版本/提交编译JSON对应。

本条单素材结构性裁剪真实成片已受限通过；有损重编码后的相似度不等于像素无损，独立最近帧匹配仍有相邻帧歧义。输入没有真实 `shots`，使用 `source_order` 整段回退且没有语义评分；生产分析校验实际仍返回 `analysis_not_ready`。因此不据本条关闭生产语义选片、ASR驱动、多素材排序或整个自动成片阶段，也不顺带验收字幕、叠加、音轨和转场。

### ICE效果A：部分通过及图片结束边界缺陷

[真实成片QC](../data/testing/ai-production-20261104/ice-effects/effects-basic-v1.qc.md)确认中英字幕、透明图片的Contain/位置、独立440Hz音轨和增益通过；实测增益 **0.549783**，线性tri淡入约 **0.601秒**、淡出约 **0.811秒**，按AAC编码帧容差通过。字幕在frame15–134符合目标，Logo却显示到frame135，比目标多1帧（约33.333ms），**exact Timeline整组仍未通过**。没有据此验收自定义字体、多轨混音或转场。

2秒四图诊断已完成，证据见 [image-boundary-v1.qc.md](../data/testing/ai-production-20261104/ice-effects/image-boundary-v1.qc.md)、[结构化QC](../data/testing/ai-production-20261104/ice-effects/image-boundary-v1.qc.json) 和 [原Job云回读](../data/testing/ai-production-20261104/ice-effects/image-boundary-v1.cloud-status.json)。云回读Timeline与提交逐字段一致，排除UI改写；两套独立QC覆盖全部60帧，结论一致。目标 `[0.5,1.5)` 为frame15–44，共30帧：

| 四图中的变体 | Image时间字段 | 实测可见帧 | 结果 |
| --- | --- | --- | --- |
| A 原配置 | In0.5、Out1.5、Duration1 | 15–45，共31帧 | 多1帧 |
| B 仅保留Out | In0.5、Out1.5，无Duration | 15–45，共31帧 | 多1帧 |
| C 仅保留Duration | In0.5、Duration1，无Out | 15–45，共31帧 | 多1帧 |
| D 两字段各减1帧 | In0.5、Out1.4667、Duration0.9667 | 15–43，共29帧 | 少1帧 |

四组均没有得到目标30帧，原因未被证明；保留 `ice-timeline/4`，未猜测更改时间编译或新增“底层已修复”测试，图片exact组合仍未验收。后续仅加入已知ICE原生可见Image组合的付费前质量守卫，不改写逐帧结论；旧已付费原ID继续可查询。此表A/B/C/D是同一个诊断内的图片变体，原B重叠fade仍是另一条尚未提交的样例。本轮新ICE任务共3条，到此不再追加；旧M1分析3/6、渲染6/6不变，未知账单费用仍待对账。

## 场景、多模型与质量合同

商品展示`product_showcase/1`和口播讲解`spoken_explainer/1`均为**draft**。场景以`{id, version, content_hash, prompt_version}`绑定模板、计划、Timeline及AI执行快照，分别规定事实/讲话保护、所需测量、选镜权重和五维评审。当前内置场景和[示例目录](../config/scenarios.example.json)是规则定义，尚无真实质量校准；[基准清单](../config/scenario-benchmark.example.json)只有12个planned目标、0个ready真实样本。同来源组或同素材hash不能同时进入调参与独立验证，合成ASR用例不计入真实项目。

每模型可在目录`limits.request_contract`声明有版本的操作、输入模式、提示模板/版本、参数类型/枚举/范围/必填/默认值、输出短边及供应商字段映射。未知参数、错误输入方式或展开后超长提示在报价前拒绝，时长来自独立请求字段，不能在options再覆盖。报价冻结规范`execution_options`、最终`provider_parameters`及`execution_prompt`，worker使用快照，旧Wan快照保持兼容。新[多模型示例](../config/providers.multimodel.example.json)只是禁用、未定价、未验收的标准网关合同；`example.invalid`端点不代表已接通真实供应商，更不代表同任务两个模型已经验收。

自动选择已移除目录首项兜底。可选择唯一合格模型，或无场景时明确配置的运营默认，这两者均不称为质量排名；场景质量选择要求场景自身verified且有报告，同时模型证据匹配相同id/version/hash。多个合格候选缺少适用证据或排名并列返回409，须指定模型。模型/场景/提示/价格变化不改写原已付费任务，未知结果只查原身份；手动选择和报价读模型返回实际选择依据。

口播路径仅接受匹配来源的media-analysis/2词级对齐与显式安全讲话单元；字幕快照保留为transcript/2，旧无词快照仍为transcript/1。自动选镜、手工trim/split/精细缩时、替换、重排和渲染来源均验证完整单元及实际词边界，替换会刷新证据。当前是证据合同和消费实现，尚未得到生产真实词级ASR，不能据测试元数据宣称自动去停顿/重复或语义质量已通过。

统一finding包含严重性、质量维度、轨道/片段/时间、来源及预期/实测。结构probe报告明确其他感知维度未检测，完整逐帧测量需绑定成片hash、Timeline hash、fps及全帧覆盖，尾音检查需实测有效声音边界与明确容差。已知`aliyun-ice-cloud-editing`原生可见Image在新报价/首次外部执行前返回`renderer_quality_unverified`；不影响其他渲染模型，也不把供应商底层出点缺陷标为修复。

服务端新增项目级`/api/v1/projects/{project_id}/quality/scenarios`、`/artifacts/{artifact_id}`、`POST /reviews`及`GET /reviews/requests/{key}`：评审绑定实际文件hash、精确场景、项目证据和登录审阅身份，幂等重放及本人原回执保留原结果，产物改变标为过时。事实、技术、叙事、画面和声音分开评审，技术硬错误不能被人工高分覆盖；结果固定`commercial_or_release_admission=false`。双端分项评审入口已接，初始未审阅、评分空白，不自动批准。旧ArtifactReview/批准下载门已绑定新证据；失败/提醒/缺审项阻止批准与下载，报告或评审变化须重新批准，预览保留供复核。真实双端完整交付仍按Q046验收。

本轮这些合同开发与自动检查没有新增云调用或费用。商品/口播各5真实项目、至少两种同输入任务合格模型、客户授权的生成→归档→采用全链路、实际声音/图片矩阵及微信真机均仍需分别取得证据。

## 用户操作

1. 进入项目的“AI制作”，或从分镜中选择尚无来源的镜头进入补镜头。先上传并核验自己的素材；仅供参考、未允许输出的素材不能用于制作。
2. 可选商品/口播场景，注意draft提示；选择自动或指定模型。补镜头填写画面描述、时长及所需参考图片。自动模式只填写公共输入，需专属参数时指定模型，表单按服务端合同校验，切换模型会清空旧专属参数。Wan现有适配器仍将`wan2.2-t2v-plus` / `wan2.2-i2v-plus`限制为5秒、480P或1080P；其他模型遵循各自目录合同，不据供应商其他规格推定本项目支持。
3. 人物替换选择原视频与目标人物档案；建档时需选择人物图片、填写主体并明确授权。该流程替换完整人物外观，以原视频动作和时长为输入约束，实际输出时长、尺寸与帧率以媒体测量为准。当前业务入口要求原视频有可信时长且大于 2 秒、小于 30 秒，不接受文字生成要求。可选标准/专业模式；人物结果不承诺固定 720P，也不向人物模型提交生成分辨率参数，不能宣称完全保持源帧率。
4. 点击“获取制作报价”，核对实际模型、选择依据、场景/提示版本、金额、时长和有效期，再点击“确认费用并开始制作”。报价本身不提交模型任务。多个模型无自动依据时需指定后重新获取报价；素材、授权、版本或价格变化会使原报价失效。
5. 网络中断时使用“查询原结果”或“重放原请求”。客户端保留原请求键和正文；不要新建相同制作任务来代替查询。可在“费用与任务恢复”查看原任务。
6. 完成后点击“采用为项目素材”，再在分镜匹配或剪辑中选择该素材。采用不会自动覆盖已有镜头或时间线。当前只有采用后的素材可预览；这不代表已经完成人工效果验收。授权撤回或源素材变化会阻止采用。

## 托管配置与身份

项目控制面及生产部署保持 `PROVIDER_MODE=api_only`、`ASSET_STORAGE_MODE=external`，不把本地模型推理或自备/租用GPU引入该部署。本轮独立本地FaceFusion技术研究的边界见下节，它未修改主项目依赖或生产catalog。新增 [providers.aliyun.ai.example.json](../config/providers.aliyun.ai.example.json) 包含三个可同时注册的固定模型：

| Provider | 模型 | 能力 |
| --- | --- | --- |
| `aliyun-wan-t2v` | `wan2.2-t2v-plus` | `video_generation` |
| `aliyun-wan-i2v` | `wan2.2-i2v-plus` | `video_generation` |
| `aliyun-wan-person` | `wan2.2-animate-mix` | `person_replacement` |

每个 HTTP entry 的 `name`、`model.provider` 和静态 `X-Wan-Provider` 必须相同。网关校验别名与模型，并将身份冻结到持久操作；后续查询使用该身份。未带该头的旧请求仍使用 `aliyun-wan`。别名不是密钥。`model.version` 必须匹配网关 `WAN_MODEL_VERSION`；它是当前网关合同版本标识，不代表已取得不可变的供应商权重或后台构建哈希。

将所需 entries 合并到实际挂载的注册表，保留既有 M1 entries。示例全部 `enabled=false`、模型 `draft`、商用审核未通过、能力 `unverified`。操作人员须先核对服务条款、地域、预算和样本，完成对应模型/版本的媒体验收，再记录真实报告并修改相关审核状态；不可将本文件或离线测试报告冒充真实供应商验收。通过发布包准入也不等于已部署或获准付费调用。

网关独立配置见 [.env.gateway.example](../.env.gateway.example)：默认 `WAN_ENABLED=false`、`WAN_MAX_OPERATIONS=0`。百炼地域 API key 只进入网关的秘密存储；控制面只持有 `MEDIA_GATEWAY_AUTHORIZATION`，值为网关鉴权头的完整内容。OSS/ICE 继续使用各自受控角色；百炼 key 不提供 OSS 归档或 ICE 探测权限。新增 Wan 配额不扩充 M1 配额。默认示例仅适用于北京地域；其他地域需重新核对 endpoint、key、存储、模型、价格及验收。

## 费用、结果与恢复边界

截至 2026-11-04，[阿里云中国站价格表](https://help.aliyun.com/zh/model-studio/model-pricing) 列示北京地域：两个生成模型 480P 为 0.14 元/秒，1080P 为 0.70 元/秒；人物替换标准/专业模式分别为 0.6 / 0.9 元/秒。示例采用所开放参数的最高费率和最大时长保守预占：生成 42 CNY/分钟 × 5 秒 = 3.50 CNY；人物替换 54 CNY/分钟 × 30 秒 = 27 CNY。较短视频或较低档位仍按该上限预占；这些金额不是已确认账单，也未覆盖可能独立产生的 OSS/ICE 等费用，不假定免费额度有效。实际账户价格变化后必须更新 `price_version` 并重新报价。

网关在模型 POST 前提交 `submitting` 状态。提交结果未知时持久隔离，禁止自动重复 POST；已获得供应商任务 ID 时只查询原任务。供应商临时视频 URL 必须先受控下载并保存至 `tenants/{tenant}/generated/{operation}/output.mp4`，经 OSS 实际 SHA256/大小及 ICE 媒体探测后，才返回成功和可采用媒体证据。下载、归档或探测失败不会产生可采用结果。

供应商实际费用未返回时保留 `cost.status=unknown`，不伪造零元。成功或失败均保留远端任务与费用证据；未知账单保持预占等待对账。云端成功后本地归档失败仍可能收费，不能把业务失败等同于供应商未收费。

## 纯换脸当前不可交付

已取得[FaceFusion本地技术报告](../data/testing/ai-production-20261104/facefusion-probe/report.md)及[带AI/本地非商业测试标识的预览](../data/testing/ai-production-20261104/facefusion-probe/face-only-first48.preview.mp4)：官方示例首48帧、2.002秒、1282×720、24000/1001fps（约23.976fps），严格解码通过；局部脸部外观改变，服装、身体、动作与背景在该样本中保留。这是局部脸替换的真实技术证据，未运行其余帧，未验收准确身份、精细口型、多脸、遮挡或长序列。

研究在忽略目录的独立venv/CPU环境运行，下载并校验12个模型共1,618,187,062字节（约1.6GB），未改主项目源码、依赖、生产环境或Provider注册。所用INSwapper/ArcFace等模型有非商业限制，附属组件也需分别核对许可；当前仍不能据此接入商业生产catalog。该官方示例没有建立客户Consent；完整来源、模型和许可证据见报告及其provenance，预览标识不代表权利放行。

当前没有可用且已验收的生产 `face_transform` / `face_only` 供应商。人物替换不能冒充纯换脸。阿里云 [MergeVideoFace](https://help.aliyun.com/zh/viapi/developer-reference/api-video-face-fusion) 虽支持授权的独立单脸视频，但视觉智能开放平台已于 **2026-10-11 停止新用户开通**，计划 2028-04-30 全面停服；本轮核对当前账号的视觉智能开放平台视频生产服务尚未开通，未取得该纯换脸调用能力。该服务使用 videoenhan 的 AK/STS/RAM 鉴权，不能使用百炼 key。[官方退市公告](https://help.aliyun.com/zh/viapi/product-overview/announcement-regarding-the-discontinuation-of-online-services-on-the-visual-intelligence-open-platform)

公告推荐的 Wan 3.0 是迁移线索；尚未证明与原身体、服装、背景严格保持的专用纯换脸合同等价，因此不开放为 `face_only`。商业生产Provider接入与完整样本验收仍未完成，本地48帧技术证据不解除模型许可和客户权利缺口。

## 部署状态

2026-11-04 只读核对 ECS：旧项目位于 `/opt/video-ai/project.retired-20261028` 且已退役，8000 端口未运行；历史 `control v8 / gateway v11` 不是本轮当前在线状态证明。标准 Docker 正运行其他项目，本轮未重启或替换。上述本机实现、总门和单项云样例均不代表本项目已重新部署、域名已切换或 M2 已交付。

供应商合同参考：[文生视频](https://help.aliyun.com/zh/model-studio/legacy-wan-text-to-video-api-reference)、[图生视频](https://help.aliyun.com/zh/model-studio/legacy-image-to-video-api-reference/)、[人物替换](https://help.aliyun.com/zh/model-studio/wan-animate-mix-api)。

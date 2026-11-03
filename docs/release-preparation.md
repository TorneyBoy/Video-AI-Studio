# M2 源码候选包、镜像与升级准备

本页描述 H04 当前实现。源码包具备服务端、网页、小程序和回归资料，打包、校验和镜像构建均不代表 M2 阶段验收或生产上线。微信平台/真机、D03/D04 真实远端和 D05 双端各 10 项仍待完成；当前供应商分析3/6、渲染6/6和累计USD10约束不变。

## 交付物及实现

| 交付物 | 实现 | 最终呈现 |
| --- | --- | --- |
| 完整源码候选包 | 明确组件/后缀/公开配置名单；同时纳入网页、小程序、后端、网关、云函数源码、构建入口、测试、SQL和文档 | `release.tar.gz`，内含逐文件大小/SHA/模式的manifest；外部`receipt.json`记录内容ID和归档SHA |
| 可复现性 | 相同源码字节、标签和Git来源生成相同包；固定tar顺序、模式、UID/GID/时间及gzip时间 | 工作区未提交也能打包，以实际文件内容为准，Git提交和dirty状态只是来源提示 |
| 完整性与提取 | 必须提供外部保留的SHA；先验证整个包、组件入口和依赖摘要，再写全新目录；拒绝链接、重复成员及路径越界 | `verify`、`extract`和绑定外部内容ID的`verify-tree`；不覆盖既有目录 |
| 镜像构建 | 只从校验提取的源码构建，固定Python基础镜像digest，写入源内容ID标签，记录真实image ID | 本机镜像与构建回执；不push、不启动业务、不更改环境或云端配额 |
| 只读升级预检 | 对照包内迁移版本读取PG16现状、隔离状态、未结束Job/远端/预占和无个人所有者空间数量 | JSON迁移清单、数据缺口和回滚限制；不自动迁移、停写或激活 |
| 生产启动门禁 | API/Worker只检查完整的当前schema，不在启动时执行DDL；遇未知/更新版本拒绝 | 必须先显式运行迁移；缺版本不会通过健康启动悄悄补齐 |
| M2 Compose | 使用指定不可变镜像、既有外部数据库/控制卷和私有环境文件；迁移/执行/观察分别设profile | 默认仅API；执行需显式profile；不自动创建数据库、网关卷或公网入口 |
| 升级回归 | 从实际M1 control v8固定镜像在临时PG16执行旧迁移，冻结真实DDL和明确合成记录；当前迁移在独立恢复库演练 | 0007→0020、原任务/费用/产物引用不变、失败事务回滚、重复迁移和隔离保留的证据 |

源码名单排除运行`.env*`、私有小程序配置、数据目录、备份、密钥、证书、媒体、依赖安装目录与缓存。公开env模板拒绝非空真实凭据形式，公开Provider配置不接受字面量密码/Authorization等；未知配置文件/源码类型要求更新名单。该规则不能代替对允许源码正文和供应链的人工审核。测试目录中有明确合成记录和测试字符串，不属于生产数据。

包内Web manifest的21个脚本、小程序app.json登记的13个页面（TS/WXML/JSON）及已固定的js-sha256/许可证摘要会核对；这仅证明源文件完整，不替代微信编译、保存相册、运行效果或完整功能验收。AppID为空会如实记录；AppSecret和供应商凭据不进入客户端。

历史`package_m1_release.py`已停止生成新包，防止用旧范围遗漏小程序或混入后来新增的运行文件；M1历史归档和收据保持。M2统一使用以下入口。

## 构建及复核

输出目录必须全新、父目录已存在，源码和运行配置分开。源码包最高64MiB、单文件16MiB、最多5000文件；工具拒绝捕获期间检测到的文件/名单变化。不要在打包时继续编辑源文件。

```powershell
python scripts/package_release.py build --label m2-review-001 --output-directory D:\video-ai-releases\source-001
python scripts/package_release.py verify --input D:\video-ai-releases\source-001\release.tar.gz --expected-sha256 <归档SHA>
python scripts/package_release.py extract --input D:\video-ai-releases\source-001\release.tar.gz --expected-sha256 <归档SHA> --destination D:\video-ai-releases\checkout-001
python scripts/package_release.py verify-tree --root D:\video-ai-releases\checkout-001 --expected-content-id <内容ID>
python scripts/build_release_image.py --input D:\video-ai-releases\source-001\release.tar.gz --expected-sha256 <归档SHA> --output-directory D:\video-ai-releases\control-001
```

归档SHA应来自已保管/核对的原始收据；从同一不可信下载处同时取得包和摘要，不构成发布者身份认证。当前工具没有数字签名。镜像工具在本机Docker构建，依赖按项目约束安装，构建日志保存在新的私有输出目录；不注入运行环境或云凭据。`--component gateway`单独安装gateway/ice/fc依赖，但不启动网关，也不创建或重置它的数据库/计数。

镜像内容标签为`ink.huayuan.video-ai.source-content-id`。构建回执包含基础镜像、源码ID、image ID和组件，`runtime_acceptance_run=false`保持为构建事实；实际启动/迁移演练使用独立回执，不改写构建收据。构建观察超时需要检查原Docker任务/日志，不自动重复创建构建。

## 升级预检与配置

```powershell
python scripts/release_preflight.py --input D:\video-ai-releases\source-001\release.tar.gz --expected-sha256 <归档SHA> --database-env VIDEO_AI_DATABASE_URL --runtime-env-file D:\secure-video-ai\m2.env
```

数据库URL只通过指定环境变量注入。工具对数据库使用只读一致事务，不执行DDL、费用结算、Job恢复或Provider调用。无版本但有表、历史缺口、未知或更新版本会拒绝；不能据“没有发现错误”推断生产可切换。

私有M2配置以`.env.m2.example`为字段模板，必需值留空；预检把它作为数据解析，不执行shell，不展开变量，不输出配置值。检查`production/session/api_only/external`、HTTPS origin、回调绑定、独立至少32字符的内部/抓取/网关/证明密钥，以及应用Settings约束。DNS/TLS证书、真实数据库路由、Provider验收、域名白名单和文件权限仍未由这份配置检查证明。打包开关暂时保持false。

预检始终将当前源码候选的真实云、微信和M2阶段验收记为`not_attested`，`ready_for_production=false`；这是当前证据状态，没有绕过或自动上线参数。无个人所有者的旧空间只报告数量，不自动给新账号分配原用户资料。

## 迁移与回滚顺序

生产仍为M1 control v8、gateway v11和0007，未执行本页部署。实际操作前需完成以下依赖，当前不能以只读预检代替它们：

1. 固定原镜像和配置，核对费用/远端状态、网关持久卷与验收计数；准备可取回的[加密备份](database-recovery.md)、密钥和对象媒体恢复方案。
2. 取得API、旧Worker、回调及其他写入方全部停写的可靠证据，保留未知远端ID和费用预占。先在新恢复库保持隔离演练升级，核对原数据、实际媒体和身份迁移。
3. 在维护窗口显式运行一次迁移，再启动新API。生产`init_db`只调用版本检查，缺少/未知版本会拒绝启动；手工`migrate`也拒绝未知/更新版本。不要并行执行迁移。
4. 旧M1单租户入口会剥离Cookie，不适合作为M2多用户登录入口。应准备合法HTTPS同源代理、个人账号/旧空间归属及微信平台配置，再验收会话、隔离、旧作品和全部客户端流程。
5. 确认唯一执行实例、远端查询/费用对账和网关状态后再开启执行profile。恢复库不能靠清除GUC或单例状态直接接管；旧二进制不识别恢复门禁，必须保持停止。

`infra/m2.compose.yml`要求明确提供`CONTROL_IMAGE`、`M2_RUNTIME_ENV`、`M2_CA_FILE`、`M2_PROVIDER_REGISTRY`和`M2_CONTROL_DATA_VOLUME`；私有运行文件采用绝对路径。Compose不包含PostgreSQL和网关服务，沿用已核对的既有实例；网关若需升级须单独保留原卷、配额和未知操作。新建空卷不等于恢复成功。

```bash
# 以下只展示依赖完成后的操作顺序，当前没有执行生产命令。
docker compose -f infra/m2.compose.yml --profile migration run --rm migrate
docker compose -f infra/m2.compose.yml up -d api
# 只有完成执行权/账务/媒体核对后才启用：
docker compose -f infra/m2.compose.yml --profile execution up -d worker
```

0020后的运行语义包含个人会话、版本审核、批次调度与数据库实例围栏，即便DDL多为新增表，也不能断言M1镜像能安全回滚运行。当前预检明确标记旧M1镜像不支持直接在升级库接管；优先前向修复，或在源停写/隔离、远端及备份后写入核对后使用已验证快照恢复。没有数据库降级脚本或自动删除新表功能。真正的回滚/接管、RPO/RTO、异地密钥取回和生产运行仍待H04验收。

## 当前证据

固定M1镜像ID为`sha256:227302226b8fcd518ca0dde35b3857da483e2d723c4b6e7659d33fd0dec8c9d5`；捕获器只连接新建本地测试库，不传入生产配置，不调用供应商。冻结样本`tests/fixtures/m1-control-v8.postgres.json`的SHA为`10204d5658a56ba1cf6081c2bcbecd72abb6a650303450a231bb5667d2035d51`，CI回归不依赖本机私有旧镜像。它包含实际PG16旧DDL及合成记录，不是生产数据备份。

本批`data/testing/m2-release/contracts.log`为42 passed，覆盖完整候选包/两端入口、mtime变化下的同字节复现、外部SHA、归档路径/重复/链接、源变化、运行配置、生产不自动DDL，以及实际M1旧库备份/恢复/0007→0020升级和失败事务回滚。迁移从25表19行到63表34行，所有原业务表行及旧迁移记录保留；增加的是迁移记录、调度单例及恢复控制。源库不变、恢复隔离保留、重复迁移无变化。证据见`m1-upgrade-evidence.json`和`migration-failure-evidence.json`。

M2 Compose已通过本机全profile结构校验，没有启动服务或创建卷。源码包、镜像构建及实际运行的结果以`data/testing/m2-release/`下的候选/镜像`receipt.json`、`image-runtime-evidence.json`和`cleanup-evidence.json`为准；缺少回执不能算运行通过。本批不把所列专项数字与之前57项或整个M2拼接成全量结果。微信工具继续按用户通知安排。

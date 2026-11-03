# PostgreSQL 元数据备份与隔离恢复

本工具属于 H04 的一个增量：提供真实 PG16 导出、加密、完整性核对和新库隔离恢复。它不开放恢复库的生产流量，也不完成 Worker 接管、异地备份、OSS 媒体恢复或正式回滚验收。生产仍为 M1 的 0007，未执行本批迁移或部署。

入口为 `python scripts/database_recovery.py`，安装项目后也可用 `video-ai-recovery`。需要项目运行依赖、`operations` extra，以及 PostgreSQL 16 的客户端：

```powershell
python -m pip install -c requirements-dev.lock ".[operations]"
python scripts/database_recovery.py --help
```

客户端可以来自本机 PATH、`--client-binary-directory`，或显式指定的既有 PostgreSQL 容器 `--pg-container`。工具不会创建、重启或修改数据库容器。容器模式使用 Docker CLI，不给应用容器挂载 Docker socket；应在受控运维主机运行。

## 数据与结果

| 内容 | 实现 | 结果与边界 |
| --- | --- | --- |
| 应用元数据 | 源库 REPEATABLE READ / READ ONLY 导出快照；`pg_dump -Fc --snapshot` 使用同一快照 | 保留用户/会话摘要、项目、版本、报价/回执、Job/Workflow、费用、审核、产物引用、调度和观测数据 |
| 恢复校验 | 全部普通/分区应用表逐行流式摘要、行数、主要结构摘要，另核对序列和 large object 的 OID/页 | 恢复结果与加密 manifest 完全相等后才写入隔离控制记录；不通过就保留隔离状态 |
| 配置 | 仅显式 `--config LABEL=PATH` 的常规文件，最多 32 个、各 2MiB | 随元数据加密；恢复默认不导出，显式导出也不自动应用配置或原 DSN |
| 加密 | 随机 32 字节密钥，AES-256-GCM，随机 nonce；版本头参与认证 | 整个密文认证完成后才解析 TAR、导出配置或连接恢复数据库；无明文备份口令参数 |
| 本地文件 | 新建私有目录，POSIX 0700/0600，Windows 当前 SID 的私有 ACL | 明文仅在本次私有 scratch 中，正常成功/失败均清理；输出为 `metadata.vab` 和不含配置值的 `receipt.json` |
| 恢复隔离 | 先创建禁止连接的新库，设置数据库级 `video_ai.recovery_quarantine=on`，再允许连接并恢复 | 原有数据库不覆盖；恢复失败也不开放；新 API、任务领取、回调、恢复器及 poller 拒绝执行 |
| 执行身份 | 0020 单例保存数据库实例 UUID；执行票据绑定 UUID | 另一数据库/恢复前的执行票据不能发布产物或写迟到费用；同一数据库原有迟到费用契约保留 |
| 密钥轮换 | `rewrap` 完整认证旧包后，以新密钥重加密到新目录 | 原备份不覆盖，backup_id 保留，回执关联原密文 SHA；不修改云服务、业务签名或登录密钥 |

不收集数据库角色、密码和对象所有权/ACL，恢复使用 `--no-owner --no-privileges`；目标业务角色与授权需单独部署。媒体字节、OSS 版本、云端任务、RAM/DNS、网关持久任务/验收计数、镜像和运行环境也不属于数据库备份。需要保留的网关状态应另行做受控备份，不得借恢复重置已耗尽的供应商测试计数。

工具面向本项目 PG16 结构，拒绝 foreign tables。结构摘要覆盖列、约束、索引、视图定义、函数、触发器、扩展及序列定义；不宣称替代通用 PostgreSQL 集群/PITR 工具或验证所有自定义扩展、物化视图数据、角色/安全策略。引入这些对象前须扩展恢复计划。库/客户端版本、实际连接到的数据库 OID 和服务启动时间会核对，避免主机连接和 Docker 客户端落到不同实例。

## 密钥与备份

所有目录示例都是占位路径，父目录必须先存在，输出目录必须全新。备份、密钥和 scratch 放在源码目录之外；不要把真实密钥或配置复制到聊天、Git、日志或源代码发布包中。

```powershell
python scripts/database_recovery.py keygen --output-directory D:\secure-video-ai\keys\key-001
```

该命令生成私有 `backup.key`，终端只显示指纹。通过受控配置将文件内容注入 `VIDEO_AI_BACKUP_KEY`，源数据库 URL 注入 `VIDEO_AI_DATABASE_URL`；恢复管理员维护库 URL 注入 `VIDEO_AI_RESTORE_ADMIN_URL`。URL 支持 PostgreSQL / psycopg，查询参数仅支持 SSL 和连接超时，不接受额外 SQL options。密钥与密文应分开保管，异地副本和密钥可取回性仍须正式验收。

```powershell
python scripts/database_recovery.py backup --output-directory D:\secure-video-ai\backups\release-001 --scratch-root D:\secure-video-ai\scratch --config app-env=C:\deploy\video-ai\.env
python scripts/database_recovery.py inspect --input D:\secure-video-ai\backups\release-001\metadata.vab --scratch-root D:\secure-video-ai\scratch
```

容器客户端额外指定 `--pg-container <既有数据库容器名> --client-host 127.0.0.1 --client-port 5432`。这些是客户端在容器内的路由；环境变量 URL 是 Python 运维进程可达的路由。容器内必须能读取所配置的 SSL 证书路径，否则检查失败，不绕过 SSL。

备份最长客户端操作默认 900 秒，可用 `--client-timeout` 设为 1–3600 秒。认证封包最多 32GiB，配置另受上述上限约束；必须预留导出文件、TAR、密文和目标数据库的空间，当前不是磁盘配额或 PITR 服务。超时只终止本次随机 application_name 标记的数据库会话，目标不激活。

导出使用一致快照，并发 UUID 行写入在快照后提交的内容不进入该备份。序列状态不受 MVCC 保护，若导出期间发生 `nextval`，工具拒绝发布这份备份，要求短暂静默窗口。禁止把一致快照解释为 RPO=0：快照之后的数据库写入、已产生的远端工作与账单需要单独处理。进程崩溃/断电可能留下私有 scratch，运维应核对所属路径后清理，不自动递归删除任意目录。

## 恢复与验收

恢复管理员环境指向维护库，具备创建新数据库及读取本次校验所需权限。`--new-database` 只接受 `video_ai_restore_` 加 1–40 个小写字母/数字/下划线，已存在的名字会拒绝；没有覆盖、`--clean` 或删除原库模式。

```powershell
python scripts/database_recovery.py restore --input D:\secure-video-ai\backups\release-001\metadata.vab --new-database video_ai_restore_drill_001 --output-directory D:\secure-video-ai\receipts\drill-001 --scratch-root D:\secure-video-ai\scratch
```

需要人工核对恢复配置时追加 `--export-configs`，只写入新回执目录下的私有 `recovered-config`。不能直接应用其中旧的 `DATABASE_URL` 或密钥。

先使用 `pg_restore --single-transaction --exit-on-error`，然后核对原完整快照。成功后仅写 `database_recovery_state` 的控制差异：实例 UUID、隔离状态、backup_id、更新时间；旧备份没有该表时补建。Job、Workflow 租约、远端身份、费用预占、审核与产物引用均不重置。0020 迁移会保留该单例，不会自动解除隔离；工具不会自动升级业务表或启动应用。

对应新版本应用在隔离库上 `/healthz` 返回 200、`/readyz` 返回 503，已注册的个人 API（包括会话读取和文件领取）返回 `database_recovery_quarantine`；静态页面及受原鉴权保护的只读指标/诊断仍可访问。Worker 启动观察器、任务领取、过期恢复、媒体归档、打包协调与供应商回调均不能推进任务。运维管理员仍能直接修改数据库，因此这是应用恢复门禁，不是数据库权限隔离或源环境关停证明。

工具没有激活命令。正式切换仍需补齐：源实例停写及基础设施围栏证明、备份时间之后的写入与远端费用核对、业务角色/秘密和网关状态恢复、OSS 原对象/版本可取回、旧应用/Worker 全部停止，以及目标独占执行权的接管演练。旧二进制不理解 0020 门禁，不能启动到恢复库；不能靠手工清除状态代替这些步骤。恢复失败的新库保留供核对，不自动删除。

```powershell
python scripts/database_recovery.py rewrap --input D:\secure-video-ai\backups\release-001\metadata.vab --new-key-env VIDEO_AI_BACKUP_NEW_KEY --output-directory D:\secure-video-ai\backups\release-001-key002 --scratch-root D:\secure-video-ai\scratch
```

轮换后以新密钥执行 `inspect` 并实际恢复验证，再按保留策略处理旧包/旧密钥；本工具不主动删除它们。回执包含快照时间、完成时间、实测操作耗时、表/行数量、迁移版本、密文 SHA、密钥指纹和隔离结果，不含逐行摘要、配置内容或连接密码。本机操作耗时不代表生产 RTO 承诺。

## 当前验收范围

本批自动演练使用本机既有 PG16 容器中的全新临时数据库，真实运行 `pg_dump`、加密、认证解密、`pg_restore` 和数据核对。样本包含双端制作/批次交付的合成元数据、审核/包记录、用户/会话、未知远端、CNY 350000 micros 的模拟待对账预占、观测记录，以及额外序列/空及非空 large object。没有真实媒体或真实账单。

实际证据与最终计数登记在[开发进度](development-progress.md)及 `data/testing/m2-recovery/`。包含损坏/错误密钥在数据库动作前被拒绝、归档穿越/重复成员、旧密钥轮换、已有库拒绝、并发快照边界、序列变化拒绝、失败恢复保持隔离、旧库后续迁移保持隔离，以及 API/Worker/回调/异库票据门禁。临时库在用例结束删除；只保留脱敏回执/结果，不保留与PG演练包配套的可用密钥或真实生产备份。

H04 尚待生产/异地恢复、接管与回滚、对象媒体/网关状态、密钥保管及发布环境验收。M2 的 D03/D04 真实远端、D05 双端各 10 项和微信真机，以及 M3–M5 完整范围继续保留。

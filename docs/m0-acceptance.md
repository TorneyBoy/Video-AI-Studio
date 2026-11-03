# M0 可靠基础验收记录

验收基于 `cd959be` 工作区上的 M0 实现，记录时间为 2026 年 10 月 18 日 UTC。代码尚未提交；设计文件和启动前已有改动均保留。M0 验收通过，完整产品 Goal 继续执行。

## 实现结果

| 任务 | 结果与证据 |
| --- | --- |
| A01 | 本地 Python 3.13 虚拟环境与 `requirements-dev.lock` 依赖约束；完整质量门执行成功 |
| A02 | `0003_completion_contract`，执行代次、逻辑操作、不可变事件身份、产物唯一键、费用观察/调整和 Outbox；旧重复产物保留并标记 |
| A03 | Worker 外部调用前持久化供应商身份并释放锁，返回后重锁检查代次/租约；各队列、回调和取消共用完成事务；失败结果事务回滚 |
| A04 | 首笔结算不可覆盖；未知费用待对账；迟到费用不改变交付状态；差异通过带操作者与原因的独立调整记录批准 |
| A05 | 真实 PostgreSQL 7 项回归通过，覆盖重复回调、竞争费用、取消、Worker 返回、过期租约恢复和重复迁移 |
| A06 | FastAPI 同源提供 `/`、`/app.js`、`/styles.css` 和 API；仅允许这三个静态文件；npm 与 Docker 入口同步 |
| A07 | external 模式拒绝通用素材创建绕行、本地上传会话与内容上传；本地原始内容不能二次覆盖；正确远端证明流程保留 |
| A08 | 普通签名身份不能直接写 Job 执行状态、Workflow 租约、Artifact 或租户额度；内部操作还需独立 key |
| A09 | 生产配置校验、回调绑定参数透传、前端恢复测试与 PostgreSQL CI；`all` 不再重复运行快速子集 |

## 验证结果

完整质量门：设置指向本地隔离测试 PostgreSQL 的 `TEST_POSTGRES_URL`，执行 `.venv/Scripts/python.exe scripts/quality_gate.py all`。编译、lint、网页语法与恢复、Provider admission、smoke dry-run 全部通过；完整测试集为 **203 passed、1 skipped**，耗时 31.39 秒。原始输出在本地 `data/m0-recheck.txt`。

随后补充 PostgreSQL Worker 返回与旧执行恢复场景，定向执行 `tests/test_postgres_completion.py`，结果为 **7 passed**，耗时 6.76 秒。该文件包含完整集中验证中已有的 4 项和新补充的 3 项，不将两批测试数量简单相加。原始输出在本地 `data/m0-postgres.txt`。

另已通过 `docker compose -f infra/docker-compose.yml config --quiet`、新增 PostgreSQL 用例 lint 和 `git diff --check`。没有在通过后重复运行无关检查。

唯一跳过项是需要本机 FFmpeg/ffprobe 的开发媒体适配 smoke；本机未安装该可选工具，生产仍保持外部托管媒体计算。真实供应商质量、费用、生产对象存储与小程序真机属于后续阶段，不由本次离线和数据库验收证明。

## 首轮失败与处理

首次完整集为 189 passed、12 failed、1 skipped。已修正旧测试隐含使用本地上传路径、回调事件 ID 内容冲突的预期、Windows 输出长路径与本地命令测试；区分供应商明确拒绝与提交结果未知，前者可按规则恢复，后者保留对账状态并禁止自动重复提交。补充无效金额证据与未知提交回归后，集中质量门通过。

## 下一阶段

M1 从能力目录与存储网关接口开始，随后接实际分析/渲染服务。真实调用等待服务配置与测试费用范围；不依赖这些条件的契约和实现继续推进。

# 旧工作空间绑定个人账号

M1 私有验收空间没有个人用户。M2 的普通注册会新建空间并设置零额度，不能据登录名或OpenID自动取得旧资料。本工具供受控发布操作者在选定的隔离库中，为一个已核实归属、尚无所有者/成员的旧空间创建新账号；用户随后沿用网页Cookie或小程序账号令牌登录。

| 子内容 | 实现方式 | 效果及呈现 |
| --- | --- | --- |
| 确认目标 | 显式Tenant ID、原slug及数据库实例UUID；同时要求quarantined单例和PG恢复GUC，目标事务可写 | `plan`输出作用域，`apply`拒绝连错空间、数据库或维护状态；不从客户端推断归属 |
| 建立个人所有者 | 复用原Tenant ID，只新增User、owner Membership和adoption审计 | 原项目/素材/任务/Workflow/远端ID、审核、对象地址和预算/预占保持，网页与mini账号协议指向同一空间 |
| 首次凭据 | 复用正常登录的scrypt；CLI只读私有文件或指定环境变量，先计算hash再取得数据库锁 | 不接受密码命令行参数，不输出密码/hash或会话；独立`make-password`写全新私有目录 |
| 并发与未知结果 | 同事务锁实例单例与Tenant；请求UUID、Tenant唯一约束及固定意图摘要；User/成员/审计一并提交 | 同请求固定回执，不同请求只能一个所有者；`status`可只读查询未知提交结果 |
| 重放与现有身份 | 原请求仅返回原回执；已有个人所有者、任何成员、同名账号、历史adoption或变更意图均拒绝 | 不合并账号、不移动业务行、不恢复已删除凭据、不重置已有密码 |
| 源只读维护 | 新API/Worker识别PG只读事务，拒绝执行并使readyz503；预检另报告default_read_only | 防止新代码把只读源当成可执行库；不能替代旧进程停写、物理围栏和独占接管证明 |

没有用户侧“认领旧空间”接口，也不把此工具放进个人工作台。操作者需先在发布记录中核实具体空间与人的归属，关联本次请求UUID；工具本身不提供人的身份核验。已有账号或团队成员空间需要另行核实迁移方案，本入口不会自动合并。

## 版本与状态

0021新增`legacy_tenant_adoptions`，只建表，不自动分配旧Tenant、不改Job/费用/文件或原用户。审计保存请求、数据库实例、绑定用户、意图摘要、创建时间和安全回执；不保存原密码或其快速摘要。用户删除后审计的User外键可置空，保留历史绑定，不能借删除凭据再次认领；完整用户/空间删除和保留仍属H03，不宣称审计永久不可删除。

使用[恢复工具](database-recovery.md)选定隔离副本、显式迁移到当前版本后再准备账号。新CLI要求当前schema，不能在M1的0007库上自动迁移。数据库隔离标记是应用维护门禁，具有数据库管理权限的操作者仍能改变它；此处不证明备份持有、对象媒体恢复或源执行实例已关停。

## 运维入口

安装当前项目后，控制镜像内提供`video-ai-legacy-owner`；源码中可用`python scripts/adopt_legacy_tenant.py`，其功能相同。数据库URL由指定环境变量注入，支持受控PG16地址和SSL参数，不在命令行传连接口令。

```bash
video-ai-legacy-owner make-password --output-directory /secure/owner-001
video-ai-legacy-owner plan --database-env VIDEO_AI_DATABASE_URL --tenant-id <旧空间ID> --expected-slug <原slug> --login-name studio_owner
video-ai-legacy-owner apply --database-env VIDEO_AI_DATABASE_URL --request-id <本次UUID> --tenant-id <旧空间ID> --expected-slug <原slug> --database-instance-id <plan返回的UUID> --login-name studio_owner --password-file /secure/owner-001/initial-password.txt
video-ai-legacy-owner status --database-env VIDEO_AI_DATABASE_URL --request-id <本次UUID>
```

`make-password`使用随机256位材料，文件在POSIX为0600、目录0700，在Windows为当前SID私有ACL；已有目录会拒绝。文件保管在源码包之外。`--password-env <变量名称>`可替代密码文件。不要在聊天、命令历史、Git或日志中粘贴值。

在ECS可用已复核镜像执行一次性命令，沿用目标库的私有环境配置；只为读取/生成受控宿主文件时按实际文件权限选择运维用户，正常API/Worker仍使用镜像默认非root用户。挂载初始密码时只读挂载具体文件，不向应用挂载Docker socket或整个运维秘密目录。例如：

```bash
docker run --rm --network host --read-only --cap-drop ALL --security-opt no-new-privileges:true --user 0:0 --env-file /secure/m2.env --mount type=bind,src=/secure/owner-001/initial-password.txt,dst=/run/initial-password.txt,readonly <控制镜像ID> video-ai-legacy-owner apply --database-env DATABASE_URL --request-id <本次UUID> --tenant-id <旧空间ID> --expected-slug <原slug> --database-instance-id <实例UUID> --login-name studio_owner --password-file /run/initial-password.txt
```

该例显式复用私有环境中的`DATABASE_URL`；独立CLI默认读取`VIDEO_AI_DATABASE_URL`，也可用`--database-env`指定其他受控变量。原始环境文件、TLS和访问凭据都不打包进镜像。所有动作只涉及目标元数据，未向云账号创建身份或AccessKey。

若提交结果未知，先按原UUID执行`status`。同库同意图重放只返回原结果，不读新密码文件、不改凭据或发放Session；把不同参数塞进原UUID会拒绝。已结束隔离后使用`status`核对，不再次`apply`。回执成功也不解禁数据库、不启动Worker。

## 登录与单实例保护边界

本批验证正常网页Cookie和mini账号密码令牌都可以访问原空间，其他空间仍返回404；没有用新Tenant替代旧记录。隔离期间，包括登录在内的已注册个人API仍返回503。实际开放需要完成H04独占执行、源停写和媒体/账务核对；之后由单独受控流程启动服务，本工具不负责激活。

PG的`default_transaction_read_only=on`只让新事务默认只读，普通SQL能显式切回READ WRITE，因此它不是物理围栏。本批特意验证这个限制：新版本API/Worker在只读事务上停止执行，但不能由此证明旧M1进程或其他写入方已停止。连接池也可能保留旧会话设置。正式接管仍需实际进程/连接状态、不可绕过的源入口限制和备份后写入核对，不能依据一项状态或心跳推断。

当前服务器SSH握手和Chrome控制台连接未核实恢复，本批没有登录修改服务器、分配真实空间或创建真实个人账号。微信开发工具/真机继续按用户通知安排，mini账号协议检查不是微信平台登录验收。原分析3/6、渲染6/6及累计USD10限制不变。

## 证据

`data/testing/m2-legacy-owner/contracts.log`记录本批66项：作用域/状态拒绝、正常双端账号协议、源业务行保留、并发同/不同键只产生一个owner、历史绑定不恢复删除凭据、真实CLI未知结果重放、只读维护及其可绕过性，并包含原身份/迁移/发布/PG完成竞争回归。Ruff通过；弃用警告保留，不与此前42/57项相加。

`cli-adoption-evidence.json`记录真实PG CLI创建及原回执重放。新的源码候选包、镜像内入口/运行结果和清理状态以本批目录下的对应回执为准；没有回执不能算镜像或部署通过。当前M2真实云/微信及H04硬围栏、正式接管、回滚仍待完成。

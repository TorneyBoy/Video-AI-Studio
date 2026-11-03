# M1 私有验收部署

此环境用于真实媒体闭环阶段验收，尚非公开生产发布。现有 ECS 为 Ubuntu 20.04.6；不直接执行面向新版 Ubuntu 的 Docker 软件源安装，也不自动升级操作系统。采用官方静态 Docker 29.9.1 和 Compose 5.6.0，仅供测试。Docker 官方明确静态安装缺少自动安全更新，不推荐用于生产；M5 必须迁移到受支持系统和受维护的运行时，并完成备份恢复与回滚演练。[官方安装说明](https://docs.docker.com/engine/install/binaries/)

## 部署边界

| 服务 | 绑定地址 | 身份与数据 |
| --- | --- | --- |
| 私有工作台入口 | `127.0.0.1:8080` | 通过已授权 SSH 隧道访问；固定到服务端预置的一个验收租户，剥离所有浏览器身份、Cookie、内部密钥和 Authorization 头，再签发短时身份声明 |
| 控制 API | `127.0.0.1:8000` | `production / trusted_proxy / api_only / external`；只处理元数据，拒绝公开租户管理 |
| 媒体网关 | `127.0.0.1:8100`，内部 TLS | ECS IMDSv2 自动刷新临时凭据；不解码或渲染。只与 OSS、ICE、FC 交换对象和任务信息 |
| PostgreSQL 16 | `127.0.0.1:5434` | 随机凭据、独立持久卷；应用密钥与数据库口令不打印、不入 Git |
| 镜头检测 | 阿里云北京 FC | 代码包与资源配置见 [hosted-analysis.md](hosted-analysis.md)；ECS 镜像不包含 PyAV、NumPy 或 FFmpeg |

`scripts/acceptance_edge.py` 检查 Host、Origin、路径和请求大小，拒绝内部状态变更、回调、租户管理和媒体字节上传。网页通过已签名的 `/api/v1/session` 取得真实身份；开发环境才继续使用本地租户头。此入口不替代 C01 的正式用户登录、会话、微信授权或成员角色功能，不能接到公网 Nginx 或用于多用户试用。

容器使用 host 网络，避免修改 ECS 元数据跳数；服务必须自行绑定 loopback。专用 Docker 守护进程只有 `/run/video-ai-docker.sock`，不建立 TCP API，不创建 bridge，不管理主机防火墙/IP 转发。控制面和网关使用不同镜像与环境文件；API、Worker 和网关以 UID 10001 运行，只读根目录、移除 Linux capabilities，限制内存和进程数。数据库仍保留其官方镜像初始化权限。

内部 CA 只提供控制面到网关的 TLS 信任，证书 SAN 为 `video-ai-gateway.internal`，由容器 hosts 映射至 loopback。浏览器不安装此 CA，也不绕过证书警告。CA 有效 90 天、服务证书 30 天；证书轮换前保留数据库、环境秘密及试用配额。CA 私钥仅存服务器受限目录，网关只挂载服务私钥，控制面只挂载 CA 公钥。

## 已准备和已执行的内容

M1历史源码入口为 `infra/m1.compose.yml`、`infra/Dockerfile.control`，当时使用`scripts/package_m1_release.py`生成v1/v2等归档。该入口现已停用；当前M2源码统一使用[完整候选包与升级工具](release-preparation.md)，包含小程序、测试和构建入口。历史归档及收据保留，不把打包成功视为质量门通过。

当前已在服务器安装 Git、专用 Docker 运行时和 Compose；已生成环境秘密、单租户身份及内部 TLS，未打印秘密。首次源码包 `project-m1-20261018-v1.tar.gz` 摘要 `2584e0fef51e2ff7e02e23071366d8d4e1c1576dc869fde1a6f7047156476e58`。修复后镜像已传入并加载，PostgreSQL、API、Worker、网关及私有入口均已启动；/readyz、签名会话与内部 TLS 实测通过。当前控制镜像 v8（`227302226b8fcd518ca0dde35b3857da483e2d723c4b6e7659d33fd0dec8c9d5`）、网关 v11（`d1ba64490d384b74d8bde182f5b1fa5e0ae8479294f5c84833237be198f27e70`），均以实际不可变 image ID 启动。ICE 字幕临时目录 v2 已经用户批准应用，真实页面12秒高清成片已归档、播放至结束并下载核验；M1受限闭环通过。详情见 [供应商实测记录](m1-supplier-acceptance.md)。

安装包记录：Docker 官方 HTTPS 下载归档 SHA-256 `c158ecc55adb3afb01bbd3539ed3b997d20c26dbdc418080506cc1bf5243ff57`；Compose SHA-256 `40343e21ca777173e69cff5dbafeb37c6f81f3b0d57d9e597f036e95eb63e76a`，与 GitHub 官方 release 元数据的摘要一致。安装脚本在解包前核对摘要，已有运行时或环境时拒绝直接覆盖。静态 Docker 原站在 ECS 连接被重置，改为本地从官方源下载、核对、经已授权 SSH 传输，没有关闭 TLS 校验或使用第三方镜像站。

OSS 已保存一条 CORS 规则，仅允许 `http://localhost:8080`、`http://127.0.0.1:8080`，方法 GET/HEAD/POST/PUT，缓存 300 秒并返回 Vary: Origin。请求头仅 content-type、content-md5、range、x-oss-*；暴露 ETag、x-oss-request-id、x-oss-hash-crc64ecma。Bucket 仍私有、阻止公共访问，CORS 不代替对象签名；控制台提示最长 15 分钟内生效。收据 `data/approval/oss-cors-private-m1.png`。

## 启动与验证顺序

1. 在本地阶段质量门与 PostgreSQL 回归通过后，以实际源码构建两个 `linux/amd64` 镜像，记录摘要，传入并加载至专用运行时；禁止拉取同名未知远程镜像。保存旧镜像及源码包。Compose 使用安装目录中的独立 `docker-compose`，不是未安装插件路径的 `docker compose`。在 shell heredoc 内使用 `run --rm -T --interactive=false`，避免 Compose 吞掉后续标准输入。
2. `scripts/prepare_m1_environment.py --root /opt/video-ai/project` 和 `scripts/prepare_m1_tls.sh /opt/video-ai/project` 只在首次准备时执行，已有文件拒绝覆盖。环境文件权限为 0600，不运行会打印秘密的 `docker compose config` 完整输出。
3. 设置 `DOCKER_HOST=unix:///run/video-ai-docker.sock`，使用 `/opt/video-ai/runtime/cli-plugins/docker-compose --env-file .env.m1.images -f infra/m1.compose.yml`。镜像环境文件使用加载后实际不可变 image ID；`up` 加 `--pull never`。先启动 PostgreSQL，再运行一次 `run --rm -T --interactive=false api python -m app.cli migrate`，随后用 `provision_m1_tenant.py --tenant-id <已生成身份中的租户>` 建立验收工作区，保留既有额度和消耗。
4. 启动 API、网关、Worker 与私有入口。默认两个云执行开关均为 false；先核对本地监听、数据库、TLS、临时凭据和 Provider 配置，再根据已有 FC/ICE 测试授权启用。只输出凭据是否成功及过期状态，不输出凭据本身。
5. 通过 `ssh -N -L 127.0.0.1:8080:127.0.0.1:8080 root@39.97.246.39` 访问本地工作台。先对固定短样本做真实供应商验收，取得确切模型版本和报告；注册表当前 `acceptance.status=testing`，因此用户能力仍显示等待验收，不能提前伪造 verified。
6. 最终真实页面闭环核对素材、分析、镜头/字幕时间戳、计划、渲染质检、归档和短期领取。保留供应商任务 ID、实际哈希和账单证据。累计 USD 10、最多 6 条/每条 30 秒继续生效；持久化配额不因重启或换镜像重置。

分析注册表的 CNY 0.50/请求仅为此受限测试的保守预占值，非供应商报价或最终费用。工作区另设 CNY 10 应用预占上限、CNY 1 单任务上限，不代表累计美元余额，也不包含已付 CNY 20 月包；FC 与 OSS 的实际费用、延迟出账仍须累计核对。云函数费用未知时继续标记 unknown，不能据此释放为零。

## 回滚与下一阶段

替换应用镜像前保留旧 image ID，数据库迁移仅前进，不能用旧代码自动降库。故障时先停 Worker 和两个收费开关，再回退相容的应用/网关镜像；保留所有数据库、任务 ID、未知提交围栏与试用计数。禁止 `down -v`、删除网关库或重置配额来重试收费任务。

主域名与现有 Nginx 本轮保持不变。完成私有验收后，正式公网入口仍需 C01 登录和受支持生产运行时，再依照既有迁移授权配置 `huayuan.ink`、证书与健康检查。此阶段的准备、容器启动及离线测试均不能表述为 M1 真实成片验收通过。

独立配置文件以 bind mount 挂载时，安装工具替换文件 inode 后需重建 API/Worker，使其读取新的注册表；不在有进行中任务时直接切换。当前指定视频渲染子集也已绑定真实验收报告；未实测的图片片段/独立音轨/叠加/转场仍在执行前拒绝。


本轮升级保留所有持久卷、配额、云任务 ID 和回滚镜像；未变更公网监听或 DNS。数据库增加 0007_usage_ledger_status，生产 VARCHAR(40) 已确认；备份 control-before-ledger-0007.dump 及 control-before-reservation-recovery.dump 仅存服务器受限目录。控制 v6 补齐可信预占/未知账单交付策略和 PG 锁修正，v7 修正页面恢复展示；网关 v9 显式固定编码参数、v10 关闭未验收功能、v11 兼容真实网页导出质量档及入口元数据，保留原 manifest/hash。升级收据在 data/deployment/ 的对应 release JSON。最终测试以供应商验收记录为准，不将部署成功视为效果或交付完成。


M1受限页面闭环现已通过：实际高清视频在工作台播放至结束并下载核验，归档/短期失效/续签/隔离证据见供应商验收。control v8展示最终视频并区分估算费用，配合gateway v11。分析3/6、渲染6/6，未重置；新计费生成停止在本阶段已用额度内。历史testing/失败段落为阶段过程，当前状态以上述验收结论为准；公开入口和小程序仍待M2。

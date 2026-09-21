# 独立 Langfuse 本地验收环境

状态：部署文件、镜像摘要、离线策略测试与 Compose 原生解析已验证；**容器启动、真实写入和回读 NOT_RUN**。当前 WSL 没有可用 Docker daemon。本配置为 LF-01 的本地/企业测试入口，不提供生产 HA、自动备份或容量承诺。

## 固定版本与边界

[compose.json](../ops/langfuse/compose.json) 使用 Compose 支持的 JSON 格式，与业务 compose.yaml 分开启动，项目名为 `agent-py-langfuse-local`。Web/Worker 固定 Langfuse **4.38.0**；SDK 保持 **4.15.4**。两者协议兼容性仍需以下真实回读验收。

[images.lock.json](../ops/langfuse/images.lock.json) 记录六个镜像的 manifest 摘要、架构和上游提交。2026-09-21 从 Docker Hub 与 Chainguard 获取 manifest，按原始响应字节计算 SHA-256，并核对 registry 的 Docker-Content-Digest。每个镜像均支持 linux/amd64 与 linux/arm64；启动始终使用 `tag@sha256`，标签后续移动不会改变镜像。PostgreSQL 17、Redis 7、ClickHouse 25.12、Chainguard MinIO latest 是解析来源标签，**摘要才是实际版本锁**。摘要固定不等于漏洞扫描或签名认证。

拓扑与变量参考 [固定上游提交](https://github.com/langfuse/langfuse/blob/4ecaabed8d9c39d0d3ba483f02ba1cca020388a9/docker-compose.yml)，许可保留在 [UPSTREAM-LICENSE](../ops/langfuse/UPSTREAM-LICENSE)。依赖镜像各自遵循其发行许可；部署前核对企业所用功能的授权。基础观测不配置 EE license；组织治理、审计和保留策略等商业功能不可默认视为免费可用，参见 [Langfuse 许可说明](https://langfuse.com/handbook/chapters/open-source)。

仅 Web 3000、对象存储 9090 发布到 **127.0.0.1**；数据库、Redis、ClickHouse、Worker 和 MinIO 控制台不发布端口。使用独立默认网络及五个项目内持久卷，不复用业务数据库。Redis 开启 AOF/noeviction 并带认证健康检查；所有容器日志设置轮转。无主机目录挂载或 Docker socket。关闭 Langfuse 遥测、实验功能和开放注册；管理员及项目由首次启动初始化。

容量起点参考 [官方 Compose 部署文档](https://langfuse.com/self-hosting/deployment/docker-compose)：4 核、16 GiB 内存和足够磁盘（示例 100 GiB）。这不是本项目测得的最小配置或吞吐保证。需另外监测内存、磁盘和队列增长；本地单机栈没有高可用和自动扩缩容。

## 初始化与启动

在仓库根目录运行，需要 Docker Engine 和 Compose v2：

```bash
uv sync --extra dev --extra langfuse --frozen
make langfuse-stack-check
.venv/bin/python scripts/langfuse_stack.py init --tenant demo
```

生成 `.runtime/langfuse-selfhost/stack.env`（服务端凭证）和 `agent.env`（仅 Agent 项目凭证、伪名密钥和预期项目 ID）。目录 0700，文件 0600，不打印密钥。随机生成密码、盐、32 字节十六进制加密密钥和项目 API 密钥；默认管理员邮箱 `operator@agent.local`，密码在 stack.env 中私下查看。初始化使用 [官方 headless provisioning](https://langfuse.com/self-hosting/administration/headless-initialization)。现有目录一律拒绝覆盖；重启使用原文件，**不重新生成 SALT/ENCRYPTION_KEY 或伪名密钥**。本地凭证应进入受控备份，不能作为 CI artifact。

```bash
docker compose --env-file .runtime/langfuse-selfhost/stack.env -f ops/langfuse/compose.json config --quiet
docker compose --env-file .runtime/langfuse-selfhost/stack.env -f ops/langfuse/compose.json pull
docker compose --env-file .runtime/langfuse-selfhost/stack.env -f ops/langfuse/compose.json up -d
docker compose --env-file .runtime/langfuse-selfhost/stack.env -f ops/langfuse/compose.json ps
```

只用 `config --quiet` 校验；普通 `config` 输出含展开后的密钥，不能粘贴到工单或日志。若当前 shell 已有与 stack.env 同名变量，Compose 会优先使用 shell 值；请在无这类变量的 shell 启动。打开 `http://localhost:3000` 登录；等待迁移和 Web/Worker 就绪，基础设施 healthy 不等于端到端可用。

在隔离子 shell 中加载**本脚本生成且可信的** agent.env，执行现有诊断：

```bash
(
  set -a
  . .runtime/langfuse-selfhost/agent.env
  set +a
  .venv/bin/agent-py langfuse-check --expected-project-id "$LANGFUSE_EXPECTED_PROJECT_ID"
  .venv/bin/agent-py langfuse-check --expected-project-id "$LANGFUSE_EXPECTED_PROJECT_ID" --allow-network
)
```

第一条是离线 NOT_RUN；第二条先核对项目，上传两条合成 span 并回读，报告写入 `.runtime/langfuse-live/`。只有 `observations_verified=true` 的 PASS 能说明此合成协议验收通过。INCOMPLETE 不能升级为成功。确认控制台只出现合成元数据，再让宿主机上的 API、Worker、Dispatcher 分别在加载 agent.env 的环境中启动，且业务租户与 `--tenant` 一致。现有业务 Compose 不自动连接这个栈；容器内的 localhost 不是宿主机，不能直接复用本 env。

凭证按一个开发租户/项目生成。额外租户或环境需要单独的 project、密钥和进程绑定；不靠标签实现权限隔离。此本地配置不作为公网部署模板；企业生产需独立落实 HTTPS、身份治理、数据库/对象存储备份、网络隔离和容量验收。

## 停止、恢复和升级

```bash
# 停止并移除容器，保留全部数据卷
docker compose --env-file .runtime/langfuse-selfhost/stack.env -f ops/langfuse/compose.json down
# 后续用相同配置与凭证重新启动
docker compose --env-file .runtime/langfuse-selfhost/stack.env -f ops/langfuse/compose.json up -d
```

不要在恢复流程中加 `down -v`，它会删除数据卷。清理诊断数据应按项目删除/保留流程进行；本配置没有设置自动删除策略，也没有授予批量删除权限。

备份与恢复验收顺序：

1. 暂停向此项目导出，停止 Web/Worker 写入，并等待相关队列处理完成；需要无丢失恢复时保留队列状态。
2. 停止整个栈，对五个已停止卷做一致时点的备份，凭证文件另存到受控密钥库。备份范围包含 PostgreSQL、ClickHouse、MinIO 和 Redis；单独备份 PostgreSQL 不足以恢复轨迹。
3. 在隔离宿主机恢复所有卷、原凭证与原镜像锁；禁止把备份直接覆盖到正在运行的卷。
4. 检查历史诊断仍可回读，再运行一次新的合成诊断，记录恢复耗时及丢失窗口。当前尚未执行此恢复演练，RTO/RPO 未测。

镜像更新是显式操作，不会随普通启动自动修改锁：

```bash
# 联网，仅生成候选文件；不覆盖现有锁和部署
.venv/bin/python scripts/resolve_langfuse_images.py --output .runtime/langfuse-images.candidate.json
```

脚本重新解析锁中标签（含固定 4.38.0）；升级到新 Langfuse 版本时需在变更中同时修改 web/worker 标签、版本、上游提交和上游 Compose 内容摘要，再解析候选。核查上游迁移说明和基础设施要求，审查候选摘要，同步 images.lock.json 与 compose.json。任何查询/摘要验证失败都退出非零，不自动采用旧值冒充新锁。

在备份恢复出的隔离环境执行策略测试、Compose 校验、真实回读、三类业务轨迹及故障/性能测试，保存版本与报告后再批准生产变更。数据库迁移可能不可逆，回退必须恢复旧版本相匹配的全套备份，不能仅把镜像 tag 改回旧值。

## 当前证据

- 六个镜像 manifest 摘要已实际核对；尚未拉取镜像层或运行镜像。
- 离线测试覆盖私有权限、凭证绑定、拒绝覆盖/符号链接、租户输入注入、镜像/端口/共享卷/依赖漂移、registry 摘要和认证端点校验。
- 使用校验过官方 SHA-256 的 Docker Compose **v2.39.4** 执行 `config --quiet` 成功；此检查不启动容器。
- 容器启动、平台回读、项目权限、升级/恢复、真实模型与吞吐验收均待执行。LF-01 保持 partially implemented。

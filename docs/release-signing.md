# 发布签名、续签与撤销

发布摘要固定了内容，独立 pin 限制了允许运行的版本。可选的 Ed25519 发布证明进一步要求受信发布者为指定 release_id、部署 audience、运行环境及有效期签名。签名是独立文件，不进入发布摘要，因此同版本续签、换钥不改变任务身份或 Temporal 队列。

此能力是本地部署策略的一部分。签名不创建授权 Grant、不放开外部写入、不批准部署，也不能证明报告来自可信 CI。`production_ready` 始终为 false。完整版本固定步骤见[发布说明](releases.md)。

## 数据与信任边界

- `release-attestation-v1`：固定 Ed25519、key_id、release_id、audience、environment、issued_at、expires_at 和签名。禁止包内提供公钥或其他未知字段。
- `release-trust-v1`：部署方独立分发的公钥清单；每个 key 有 audience、运行环境、有效时间和 revoked 状态。key_id 唯一，不接受通配 audience。信任清单还限制签名最长寿命，默认 24 小时，可设为 60 秒至 7 天。
- `environment` 对应 `AGENT_ENVIRONMENT` 的 development/test/production，不是任务的 lab/staging/production。不同集群、业务域或部署轨道用独立 audience 区分，例如 `engineering-staging`；租户授权继续由原有身份和 Grant 管理。
- 签名消息前缀为 `agent-py/release-attestation/v1\0`，与审计签名分域。建议使用独立发布密钥，避免职责混用。

验签同时要求 `key.not_before <= issued_at <= 当前时间 < expires_at <= key.not_after`，且寿命至少 60 秒、不超过信任策略上限。拒绝未来签发时间，没有时钟偏差宽限；部署机器需要同步时钟。与历史审计验签不同，发布签名过期就不能继续派发。签发时间不是可信时间戳，未接入时间戳服务、透明日志或 KMS/HSM。

## 本地生成和签署

下面示例只使用测试环境，命令不会联网或创建 Service。先按发布说明构建新的清单并取得经核对的完整 release_id。

```bash
# 密钥保存在本地受控目录。不要把 private.pem 放到应用容器或提交到 Git。
.venv/bin/agent-py release-keygen .runtime/release-keys/v1 \
  release-2026 engineering-staging --environment test

# 替换 SHA 占位符；默认签名寿命 3600 秒。
.venv/bin/agent-py release-sign .runtime/releases/candidate.json \
  .runtime/release-keys/v1/signer.json sha256:REPLACE_WITH_64_HEX \
  .runtime/releases/candidate.attestation.json engineering-staging \
  --environment test --lifetime 3600

.venv/bin/agent-py release-verify .runtime/releases/candidate.attestation.json \
  /trusted/release-trust.json sha256:REPLACE_WITH_64_HEX engineering-staging \
  --environment test
```

生成器创建 private.pem、signer.json 和 trust.json，文件权限 0600，目录新建时为 0700；已有文件一律拒绝覆盖。签署端必须有只允许所有者访问的 Ed25519 私钥。将公钥内容经独立渠道审批并分发为 `/trusted/release-trust.json`，不能以待验证文件旁边出现的 trust.json 自证可信。

`release-sign` 先检查独立传入的 release_id，并重算清单中的评测门禁；它不会声明自己已经构建或运行了清单里的代码。`release-verify` **只验证独立签名**，不读取发布源码；部署前仍需在目标 checkout 上运行 `release-check`。所有输出采用独占创建，续签应写新文件后由运维原子替换，不原地截断线上文件。

## 运行时启用

保留原有 `AGENT_RELEASE_MANIFEST`、`AGENT_RELEASE_EXPECTED_ID` 和 `AGENT_RELEASE_ROOT`，再同时配置：

```dotenv
AGENT_RELEASE_ATTESTATION=/app/releases/current.attestation.json
AGENT_RELEASE_TRUST_STORE=/trusted/release-trust.json
AGENT_RELEASE_AUDIENCE=engineering-staging
```

签名、trust store、audience 缺任意项或未配置发布清单，Settings 拒绝启动。与签名声明相符的 AGENT_ENVIRONMENT 也必须配置。所有 API、Worker、Dispatcher 都要启用同一部署策略；只升级部分进程不构成全局签名门禁。

启动时核验内容、独立 pin、评测和签名。每次发布检查重新读取签名及 trust store，包括新建任务、新动作/推理派发、Dispatcher 派发和 readiness。撤钥、签名过期、文件缺失、损坏、未知 key 或范围不符立即拒绝本次新工作；不缓存上次成功的验签结果，不回退到无签名模式。已启动进程也不能通过删掉签名设置或改变 audience/environment 静默降级，必须重新部署。

`/health/live` 仅标明 `release_signature_required`，不代表当前验签成功；readiness 失败返回 503。程序日志/错误不会输出私钥或清单业务内容。签名文件读取上限 16 KB，信任/签发配置上限 100 KB；拒绝最终路径符号链接、FIFO、非普通文件、重复 JSON 字段和非有限数值。父目录和分发渠道仍由部署方控制。

取消、接管和对账不依赖发布签名有效。签名失效不撤销已经发生的副作用，也不能强行中断在途模型/企业请求；旧回执和费用仍应结算。被拒绝的 Outbox 不标记已投递，修复签名后原任务可继续。

## 轮换与撤销步骤

1. 在新的受控目录生成新 key，独立核对其指纹/公钥后，把新旧公钥同时放入信任清单，保持 key_id 不重复。
2. 用新 key 为**同一 release_id**重新签署，先离线验签，再原子替换线上签名文件。应用进程不需要改变发布 ID 或队列，也无需为续签重建任务。
3. 确认所有副本完成信任清单和签名分发后，撤销或删除旧 key；下次派发检查即使用当前清单。文件分发一致性和生效时延需要部署系统保障，当前没有跨副本撤销广播。
4. 若 key 泄露，先在信任清单设置 revoked=true 或移除，再调查该 key 的签发范围。历史签名也不能继续授权新工作；替代签名和独立发布 pin 都需重新核对。

每个签名带短有效期，运维需在到期前续签并监控 readiness。当前没有自动续签、负责人通知或 KMS 集成。删除所有签名配置后重启仍能选择原有未签名模式；若组织要求强制签名，应由部署准入保证所有副本配置完整。两种模式使用相同 release_id 队列，不能把可选功能当作对恶意部署管理员的安全边界。

## 已验证与未验证

默认测试使用真实 Ed25519 签名，覆盖字段篡改、重签错误域、未知/撤销公钥、错误 audience/environment、时间边界、有效期上限、同 ID 续签换钥、独立 pin、防降级、readiness、Outbox 保留、UNKNOWN 对账、私钥权限和文件读取边界。签名/发布 CLI 可在没有数据库的情况下执行。

没有签名构建来源证明、生产密钥或真实灰度发布；也没有证明被签代码的安全性、模型质量或已安装依赖与锁文件一致。文件检查到使用之间不是分布式事务；在途操作以及其他尚未获得新 trust store 的副本可能继续完成。必须结合只读部署、可信分发和运行时权限控制。

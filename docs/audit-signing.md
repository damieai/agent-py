# 审计签名与离线信任

签名导出是可选功能，默认关闭。它将 v2 一致性快照封装为 `signed-recording-v1`，用 Ed25519 绑定整个录制包摘要、任务、租户、用途、签名密钥 ID 和签发时间。API、CLI 和工作台均支持；请求签名失败时不会退回无签名包。

## 本地启用

在受控目录创建测试密钥，租户必须替换为实际租户 ID：

```bash
.venv/bin/agent-py audit-keygen .runtime/audit-signing audit-2026-09 TENANT_ID --audience agent-audit --days 90
export AGENT_AUDIT_SIGNING_MANIFEST="$PWD/.runtime/audit-signing/signer.json"
.venv/bin/agent-py export TASK_ID audit-signed.json --signed
```

重启配置后的 API，工作台才会显示签名导出可用；也可使用已有 Bearer 凭证请求 `GET /api/v1/tasks/TASK_ID/recording?signed=true`。健康接口只报告配置存在，不保证密钥有效。导出保持原任务权限限制，并在签名后再次复核授权。

生成器创建 `private.pem`、`signer.json` 和 `trust.json`，文件权限 0600，新目录权限 0700，拒绝覆盖已有文件。创建中断可能留下部分文件，应检查后换新目录重试。私钥为未加密 PKCS8 PEM；生产应由运维挂载受控只读秘密文件，保护父目录和 signer 清单，禁止入库。读取拒绝末级符号链接、非普通文件、超过 4096 字节或带组/其他用户权限的私钥；0400、0600 均可。该机制不是 KMS/HSM，也没有自动轮换。

## 独立离线验证

通过独立可信渠道向验证方分发公钥及策略 `trust.json`。不能把审计包发送者随包提供的公钥当成独立信任依据。验证者从业务上下文确定预期租户与用途，而不是直接抄取包内字段：

```bash
.venv/bin/agent-py audit-check audit-signed.json \
  --trust-store /trusted/audit/trust.json \
  --tenant TENANT_ID --audience agent-audit
```

该命令不构建 Service、不连接数据库、没有网络密钥发现或远端回放补齐。带信任参数的无签名包会被拒绝，签名包缺少任一信任参数也会被拒绝。重复 JSON 字段、非有限数字、未知字段、篡改内容和作用域不匹配均不被接受。签名验证后仍执行原来的事件、审批、动作与回执一致性检查。

报告含 `signature_verified`、`key_id`、`audience`、`issued_at` 和当前信任策略摘要 `trust_store_digest`。`remote_state_verified`、`artifact_contents_verified`、`trusted_timestamp` 仍为 false。无签名检查显式报告 `signature_verified=false`。

库调用使用 `ReplayExecutor.from_signed(envelope, trust_path, tenant, audience)`；它验证独立副本后创建严格回放器。直接把签名封装传给普通构造器会被拒绝。

## 轮换与撤销

1. 使用新目录和全新的 key ID 生成下一把密钥；不要把旧 ID 重新绑定到不同公钥。
2. 将新公钥策略加入验证方信任清单的 `keys`，保留需要验证历史包的旧公钥，再切换服务端 signer 配置并重启。
3. 撤销时将验证方清单中的旧密钥 `revoked` 设为 true；该密钥的所有历史签名都会被拒绝。删除密钥同样拒绝其签名。

每个密钥必须显式指定 tenants、audiences、not_before、not_after；不接受通配符，ID 不能重复。时间窗口采用左闭右开，签发时间超过验证者时钟 60 秒会拒绝。密钥窗口过期后仍可验证窗口内签发的历史包，前提是当前信任策略仍允许且未撤销；没有自动最大包年龄限制。

## 格式及证明边界

签名消息为 `agent-py/audit-attestation/v1` 加一个 NUL 字节，再拼接 attestation 的 UTF-8 JSON（键排序、紧凑分隔、保留非 ASCII、禁止非有限数字）。摘要沿用项目 `digest` 编码；这不是 RFC 8785 跨语言规范。公钥为 32 字节、签名为 64 字节的无填充 canonical base64url。以后更改编码需要版本迁移。

签名证明持有受信任私钥者认可了这些字节，不能证明数据库历史真实、审批当时有效、产物正文正确或远端业务动作实际发生。签发时间是签名者自报时间，不是可信时间戳；私钥泄露后攻击者可能回填时间，须撤销密钥。签名不提供追加写存储、删除检测或外部透明日志，签名者仍可签署内部一致的伪造记录。不可改写审计、可信时间戳和生产密钥托管仍是待完成工作。

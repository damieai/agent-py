# 认证公钥轮换与 JWT 校验

API 支持开发 HS256、固定 RSA 公钥、受控本地 JWKS 三种配置路径。固定公钥与 JWKS 互斥；生产只能使用外部 issuer 的 RSA 验证路径，不签发本地 Token。此次实现本地公钥轮换和验证，不包含浏览器 OIDC 登录、discovery、在线 JWKS 下载、Token 交换或刷新。

算法由服务配置固定，不由 Token 的 alg 决定；同时校验 issuer、audience 和 Token 类型。这些边界参考 [RFC 8725 的 JWT 验证建议](https://www.rfc-editor.org/rfc/rfc8725.html) 和 [PyJWT 验证接口](https://pyjwt.readthedocs.io/en/stable/api.html)。

## 配置

```dotenv
AGENT_AUTH_ISSUER=https://identity.example.test/
AGENT_AUTH_AUDIENCE=agent-api
AGENT_AUTH_JWKS_FILE=/run/agent-auth/jwks.json
AGENT_AUTH_TOKEN_TYPE=JWT
AGENT_AUTH_MAX_TOKEN_LIFETIME_SECONDS=3600
AGENT_AUTH_CLOCK_SKEW_SECONDS=0
```

设置 JWKS 时清除 `AGENT_AUTH_PUBLIC_KEY`。JWT 的 typ 必须与配置精确相等；若上游签发 `at+jwt`，相应配置为 `at+jwt`。使用专属于本 API 的 audience，不把登录客户端的 ID Token audience 复用为 API audience。issuer 及信任文件由部署管理员配置，Token 中的 URL、公钥或 kid 不能更改信任来源。

公钥文件是一个 `{"keys": [...]}` 对象，最多 16 个 RSA 公钥、100 KB。每项必需 `kty="RSA"`、唯一 `kid`、base64url 无填充编码的 `n` 和 `e`；可选元数据仅支持 `alg="RS256"`、`use="sig"`、`key_ops=["verify"]`。kid 长度 1—80，字符为字母、数字、点、下划线或连字符。模数 2048—8192 bit，指数为不小于 65537 且小于 2³² 的奇数，整数编码无前导零。

这是刻意收窄的 JWKS 子集：私钥参数、混合算法、证书元数据（如 x5c/x5t）、远端地址及其他未知字段均拒绝。供应商原始 JWKS 若含这些字段，需要运维从可信材料生成符合此契约的本地公钥清单，不能直接任意复制。项目不会从 HTTP 请求或用户上传内容更新该文件。

已有受信任 RSA 公钥 PEM 可在离线环境转换后由管理员部署：

```python
import json
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from jwt.algorithms import RSAAlgorithm

public = serialization.load_pem_public_key(Path("/trusted/issuer-public.pem").read_bytes())
key = json.loads(RSAAlgorithm.to_jwk(public))
key.update(kid="issuer-2026-09", alg="RS256", use="sig", key_ops=["verify"])
print(json.dumps({"keys": [key]}, indent=2))
```

部署前执行 `.venv/bin/agent-py auth-keys-check /run/agent-auth/jwks.json`，输出 kid、算法、位数和 SPKI DER SHA-256 指纹，不输出密钥正文，也不构建 Service 或访问网络。验证成功只表示文件满足解析与密钥策略，信任仍需独立核对发行方和指纹。

## 轮换和紧急撤钥

1. 通过受信渠道取得新公钥，为新密钥使用新的 kid；先把旧、新两把公钥部署到所有 API 副本。
2. 使用同目录临时文件加原子 rename 替换清单，保持父目录和挂载源只有运维可写。允许配置挂载所需的符号链接，但打开后的目标必须是普通文件；FIFO 和其他非普通文件拒绝读取。
3. 在所有副本执行预检、核对指纹及 readiness，再让发行方切换新 kid。当前不提供多副本原子分发或版本协调。
4. 正常轮换需等待旧 Token 的最晚 exp 及允许时钟偏差过去，再移除旧公钥；紧急撤钥可立即移除，但尚未过期的旧 Token 也会失效。

每次认证都重新读取并验证文件，没有缓存和网络回退。删除旧 kid 后，下一次 API 认证及 SSE 下一次事件分页认证会拒绝旧 Token。正在处理的请求可能已经完成认证；公钥撤销不能撤回已发送的数据或已经产生的副作用，也不会自动取消此前接受的后台任务。停止任务仍需取消、接管、撤销 Grant 或紧急停用。

损坏、缺失、超限或存在无效密钥的清单会整体返回 `AUTH_UNCONFIGURED`（503），不沿用旧缓存、不回退到固定公钥或开发 secret。未知 kid、错误签名或无效 Token 返回 `UNAUTHENTICATED`（401）。错误内容不含文件路径、Token 或密钥正文。

JWKS 模式的 `/health/ready` 同时检查数据库及公钥文件，失败时返回 `authentication_keys_unavailable`；`/health/live` 不受文件损坏影响。固定公钥和开发模式仍保持 `api-database-only` 的 readiness 范围。读取文件和构造公钥会增加每次认证成本，当前未做生产认证吞吐/SLO 验收。

## Token 契约与兼容性

- 只接受不超过 16 KB 的三段签名 JWT。header 只支持 alg、typ、kid；不支持 jku/jwk/x5u/x5c/crit/b64 等扩展，JWKS 模式必须带 kid。
- header 和 claims 必须为 UTF-8 JSON 对象，拒绝重复字段、非有限数、非规范 base64url 和未签名 Token。
- 必需 exp、iat、sub、iss、aud；iat/exp 及可选 nbf 必须为整数，不能是字符串、浮点数或布尔值。寿命必须为正且不超过配置上限，默认 3600 秒；上限可配置 60—86400 秒，时钟容差可配置 0—60 秒。
- 还需本项目的 tenant_id、roles、projects、environments 声明。tenant_id/sub 非空且分别不超过 120/160 字符；三种范围列表各最多 100 项，字符串长度分别不超过 80/80/40。身份提供方负责可信映射这些声明，业务访问仍需当前数据库 Grant。

这些检查也收紧了原有固定 RSA/开发 HS256 入口：无 typ、超长寿命或使用可强制转换时间类型的旧 Token 会被拒绝。上线前验证上游声明格式和寿命。开发 `agent-py token` 默认采用配置的 Token 类型和最大寿命；RSA 模式不再签发无法被当前配置验证的开发 HMAC Token。

角色声明仍存在于 Token 中，本轮没有逐会话/逐用户撤销表或角色管理 UI。角色变更通常依赖新 Token；已有 Grant 撤销继续实时生效。认证公钥与审计包签名密钥属于不同信任用途，不应互相复用。

# 生产部署检查 · 问题汇总 · 需求对照 · 测试用例

本文档服务于：**（1）生产/预发上线前的检查打勾与命令索引**；**（2）已知问题、环境风险与安全待办汇总**；**（3）《选型》需求对照与测试用例**。 
**技术框架、架构示意图（§3）、选型落地摘要、队列 JSON 契约**以 **[watermark-java-backend-tech-selection.md](./watermark-java-backend-tech-selection.md)** 为准；本文不重复展开架构长文，仅链回与补「可操作检查项」。  
**服务器上的安装顺序、环境变量与 systemd** 以 **[DEPLOY-SERVER.md](./DEPLOY-SERVER.md)** 为准（仅生产部署与配置）。
**值班告警、排障与回滚流程**以 **[RUNBOOK.md](./RUNBOOK.md)** 为准。  
**安全策略与发布前安全最低要求**以 **[SECURITY-BASELINE.md](./SECURITY-BASELINE.md)** 为准。  
**接口输入输出与错误契约**以 **[API-CONTRACT.md](./API-CONTRACT.md)** 为准。

---

## 0. 与 `DEPLOY-SERVER.md` / 《选型》的分工

| 文档 | 角色 | 主要内容 |
|------|------|----------|
| **[watermark-java-backend-tech-selection.md](./watermark-java-backend-tech-selection.md)** | **技术框架** | §3 纯文本容器/模块图、技术选型、与 §19 差异、队列 JSON / Redis Key 约定等 |
| **[DEPLOY-SERVER.md](./DEPLOY-SERVER.md)** | **生产部署与配置** | **§0 起**公网部署实操流水（命令级）；示例域名与 env |
| **[RUNBOOK.md](./RUNBOOK.md)** | **值班与故障处置** | 告警分级、排障 SOP、回滚与复盘模板 |
| **[SECURITY-BASELINE.md](./SECURITY-BASELINE.md)** | **安全基线** | 鉴权、CSRF/CORS、Cookie、密钥与最小权限要求 |
| **[API-CONTRACT.md](./API-CONTRACT.md)** | **接口契约** | 核心接口样例、错误模型、幂等与版本策略 |
| **本文** | **检查表 + 问题 + 测试** | 上线检查表（链到 DEPLOY 各节）、已知问题/风险、R 对照表、SSR/CSRF 细节、TC 表、Maven/CI 命令 |

**约定**：改拓扑或选型表述时优先改 **《选型》**（`watermark-java-backend-tech-selection.md`）；改部署命令或路径时改 **DEPLOY-SERVER**；增删检查项、排障记录、用例时改 **本文**。

---

## 1. 生产部署检查清单（链至 DEPLOY-SERVER）

在服务器上按顺序执行，并在下表 **「完成」** 列打勾（可拷贝到工单/Release checklist）。

| 顺序 | 检查项 | 在 DEPLOY-SERVER.md 中定位 | 完成 |
|------|--------|---------------------------|------|
| 0 | 云控制台安全组、DNS | 搜索标题 **`### 0）`** | ☐ |
| 1 | 系统更新与基础软件 | 搜索 **`### 1）`** | ☐ |
| 2 | MySQL 安装与库账号 | 搜索 **`### 2）`** | ☐ |
| 3 | Redis | 搜索 **`### 3）`** | ☐ |
| 4 | （可选）MinIO / 仅用 COS | 搜索 **`### 4）`** | ☐ |
| 5 | 运行目录与数据目录 | 搜索 **`### 5）`** | ☐ |
| 6 | 环境变量文件 | 搜索 **`### 6）`** | ☐ |
| 7 | 上传 Jar | 搜索 **`### 7）`** | ☐ |
| 8 | systemd | 搜索 **`### 8）`** | ☐ |
| 9 | 本机健康检查 | 搜索 **`### 9）`** | ☐ |
| 10 | Nginx | 搜索 **`### 10）`** | ☐ |
| 11 | TLS | 搜索 **`### 11）`** | ☐ |
| 12 | 外网自测 | 搜索 **`### 12）`** | ☐ |
| 13 | Python Worker | 搜索 **`### 13）`** | ☐ |
| 14 | Bootstrap 管理员 | 搜索 **`### 14）`** | ☐ |

**脚本化路径**：`deploy/scripts/` 与 [deploy/scripts/README.md](../deploy/scripts/README.md)（`00`～`50` 等）应与上表一致；若脚本与 DEPLOY 正文冲突，**以仓库脚本与服务器实测为准**，并回写本文或 DEPLOY。

> 在 [DEPLOY-SERVER.md](./DEPLOY-SERVER.md) 全文内用 **`### N）`**（N 为 0～14）搜索即可跳到对应步骤。

---

## 2. 已知问题与环境风险汇总

| 编号 | 现象 / 风险 | 影响 | 建议处理 |
|------|-------------|------|----------|
| E-01 | **本机无 Docker** | `EndToEndWatermarkFlowTest`（Testcontainers）被跳过 | 开发机可不装；**CI**（`.github/workflows/backend-java-ci.yml`）已用 `ubuntu-latest` 跑 `mvn verify` |
| E-02 | **WSL 安装 Ubuntu 失败**（如 `0x80072ee7` / `0x80072efd`） | 本机无法用官方源拉发行版 | 换网络/热点、`--web-download`、或仅依赖 CI/服务器 Docker |
| E-03 | **`wsl --update` 报处理器类型不支持** | 本机 WSL 内核未更新 | 从 [WSL GitHub Releases](https://github.com/microsoft/WSL/releases) 下载 **x64** `.msixbundle` 手动安装 |
| E-04 | **PowerShell 下 `mvn -D...` 被误解析** | 命令失败 | 参数加引号：`"-Dtest=..."` |
| E-05 | **CSRF 豁免未写进对外安全说明** | 审计/评审时对齐《选型》§9.1 困难 | 使用本文 **§5** 表 + 在 README 或专章补「威胁模型」 |
| E-06 | **SSR 与《选型》§19 表述不一致** | 读者误以为未按选型实施 | 以 **[watermark-java-backend-tech-selection.md](./watermark-java-backend-tech-selection.md)** §3.4「与 §19 的差异」为准；对外说明写一句决策原因 |

（新增条目请按 **E-07** 递增，并在修订记录中记一笔。）

---

## 3. 依据文档（仓库内）

| 文档 | 路径 |
|------|------|
| 《Java 后端技术选型说明书（重写版）》 | [watermark-java-backend-tech-selection.md](./watermark-java-backend-tech-selection.md) |
| 生产部署与配置（服务器步骤） | [DEPLOY-SERVER.md](./DEPLOY-SERVER.md) |

---

## 4. 需求对照（检查表）

| 编号 | 《选型》/《重构》要点 | 结论 | 说明 |
|------|------------------------|------|------|
| R-01 | 混合架构：Java 编排 + Python Worker 算水印 | **满足** | `WatermarkJobService` + `watermark/worker/redis_stream_worker.py`；Stream Key 默认 `wm:stream:watermark`，与 `WM_JOBS_*` 对齐 |
| R-02 | MySQL + JPA + Flyway；utf8mb4 | **满足** | Flyway 在 `infrastructure`；部署文档强调 utf8mb4 |
| R-03 | Redis：Session、任务态、幂等、限流 | **满足** | Spring Session Redis；`RedisWatermarkJobStateRepository`；`RedisSlidingWindowRateLimiter` + `RateLimitInterceptor`（`wm.rate-limit`） |
| R-04 | COS / MinIO；STS + 预签名；Complete 后入队 | **满足** | `StorageStsApplicationService`、`S3ObjectStorageService`、`POST /api/v1/files/complete`；E2E 覆盖 MinIO 路径 |
| R-05 | REST `/api/v1`；Problem Details（RFC 7807） | **满足** | `GlobalExceptionHandler` 使用 `ProblemDetail` |
| R-06 | 密码 BCrypt；Werkzeug 迁移策略 | **满足** | `LegacyAwarePasswordEncoder` + 登录后升级；单测 `WerkzeugPasswordHasherTest` 等 |
| R-07 | Session 多实例外置 Redis | **满足** | `spring-session-data-redis` |
| R-08 | Cookie Session 场景 CSRF | **部分** | 见 **§5**；须在 README/安全说明中补全威胁模型 |
| R-09 | `Idempotency-Key` | **满足** | `JobsController` + `IdempotencyKeyHasher` + Redis 映射 TTL |
| R-10 | 队列消息 JSON 字段 | **满足** | 《选型》**§10.1** 为契约；Java 与 Worker 保持一致 |
| R-11 | 可观测：Micrometer、队列、存储错误 | **部分** | 已有 `MeterRegistry`、`WmQueueMetricsConfiguration`；COS 按错误码细分与 Grafana 可加强 |
| R-12 | 留存清理 + 分布式锁 | **满足** | `RetentionCleanupRunner` + `RedisDistributedLock` + `wm.retention` |
| R-13 | 《选型》§19：推荐 SPA | **偏离（已决策）** | 见 **§5.1**；说明见 **watermark-java-backend-tech-selection.md** §3.4 / §19 |
| R-14 | Testcontainers + CI | **部分** | E2E 无 Docker 时跳过；CI 上 `mvn verify` 可跑 |
| R-15 | 阶段 4 可选组件 | **不适用** | 《选型》§16 阶段 4 按需；见 **watermark-java-backend-tech-selection.md** §3.4 |

---

## 5. 与《选型》偏差及安全文档化（问题跟踪重点）

### 5.1 SSR（Thymeleaf）vs《选型》§19

- **说明书推荐**：Java 侧重 **REST + OpenAPI**；前端 **Vue/React SPA**。  
- **本项目**：**Thymeleaf SSR（方案 Y）**。  
- **跟踪项**：是否在对外 README 中写明决策人/日期与演进条件（可选）。

### 5.2 CSRF vs《选型》§9.1

- **说明书要求**：Cookie Session 场景应启用 **CSRF 或等价防护**。  
- **实现**：全局 `CookieCsrfTokenRepository` + 下列路径 **`ignoringRequestMatchers`**。

| 方法 | 路径模式 | 备注（待评审补全） |
|------|----------|-------------------|
| POST | `/api/v1/auth/login`、`/api/v1/auth/register` | 匿名无预发 CSRF |
| POST | `/api/v1/files`、`/api/v1/files/complete` | 直传链路 |
| DELETE | `/api/v1/files/*` | 删除 |
| POST | `/api/v1/storage/sts` | STS |
| POST | `/api/v1/jobs/watermark` | 入队 |
| POST/PATCH/DELETE | `/api/v1/admin/users`、`*`、`*/groups/*` | 管理写 |
| PATCH | `/api/v1/users/me/retention` | 留存 |

```60:74:backend-java/web/src/main/java/com/watermarking/web/config/SecurityConfig.java
                .csrf(csrf -> csrf
                        .csrfTokenRepository(CookieCsrfTokenRepository.withHttpOnlyFalse())
                        .ignoringRequestMatchers(
                                new AntPathRequestMatcher("/api/v1/auth/login", "POST"),
                                new AntPathRequestMatcher("/api/v1/auth/register", "POST"),
                                new AntPathRequestMatcher("/api/v1/files", "POST"),
                                new AntPathRequestMatcher("/api/v1/files/*", "DELETE"),
                                new AntPathRequestMatcher("/api/v1/files/complete", "POST"),
                                new AntPathRequestMatcher("/api/v1/storage/sts", "POST"),
                                new AntPathRequestMatcher("/api/v1/jobs/watermark", "POST"),
                                new AntPathRequestMatcher("/api/v1/admin/users", "POST"),
                                new AntPathRequestMatcher("/api/v1/admin/users/*", "PATCH"),
                                new AntPathRequestMatcher("/api/v1/admin/users/*/groups/*", "POST"),
                                new AntPathRequestMatcher("/api/v1/admin/users/*/groups/*", "DELETE"),
                                new AntPathRequestMatcher("/api/v1/users/me/retention", "PATCH")))
```

**待办**：为每条豁免补「信任边界（同源/仅内网）」；`POST /api/v1/auth/logout` **未豁免**，前端须带 **`X-XSRF-TOKEN`**。

---

## 6. 测试用例表（手工 + 自动化映射）

**优先级**：P0 发布阻断 / P1 回归必测 / P2 按需。

| 用例 ID | 优先级 | 模块 | 前置条件 | 步骤摘要 | 期望结果 | 自动化 |
|---------|--------|------|----------|----------|----------|--------|
| TC-AUTH-01 | P0 | 注册 | 无 | `POST /api/v1/auth/register` 合法 body | `201`，返回用户 id | `EndToEndWatermarkFlowTest` |
| TC-AUTH-02 | P0 | 注册校验 | 无 | 非法 body | `400`，`ProblemDetail` | `RequirementsContractSmokeTest` |
| TC-AUTH-03 | P0 | 登录 | 已注册 | `POST /api/v1/auth/login` | `200`，`WMSESSIONID` | `EndToEndWatermarkFlowTest` |
| TC-AUTH-04 | P1 | 登录失败 | 已注册 | 错误密码 | `401` | 建议补测 |
| TC-AUTH-05 | P1 | 登出 | 已登录 | `POST /api/v1/auth/logout` + CSRF | `204` | 手工 / 集成 |
| TC-STOR-01 | P0 | 直传登记 | 已登录 | `POST /api/v1/files/complete` | `200`，有 `file id` | `EndToEndWatermarkFlowTest` |
| TC-STOR-02 | P1 | STS | COS 配置完整 | `POST /api/v1/storage/sts` | 成功或 `503`+Problem | 手工 / 预发 |
| TC-JOB-01 | P0 | 入队 | TC-STOR-01 后 | `POST /api/v1/jobs/watermark` | `Accepted`，`jobId` | `EndToEndWatermarkFlowTest` |
| TC-JOB-02 | P1 | 幂等 | 同 `Idempotency-Key` | 两次请求 | 同 `jobId`（TTL 内） | 待补 |
| TC-JOB-03 | P0 | 任务查询 | 已入队 | `GET /api/v1/jobs/{jobId}` | `status` 一致 | `EndToEndWatermarkFlowTest` |
| TC-WKR-01 | P0 | Worker | 依赖可用 | `python -m watermark.worker.redis_stream_worker` | DB/对象终态正确 | 手工 / 预发 |
| TC-SEC-01 | P1 | 越权文件 | 双用户 | 访问他人 `fileId` | `403/404` | 待补 |
| TC-SEC-02 | P1 | 管理接口 | 非 ADMIN | `/api/v1/admin/**` | `403` | 手工 |
| TC-UI-01 | P2 | Thymeleaf | — | `GET /signin` | `200` | 手工 |
| TC-UI-02 | P2 | 仪表盘 | 与 Security 一致 | `GET /api/v1/stats/dashboard` | `200` JSON | 手工 |
| TC-OPS-01 | P1 | Actuator | 服务启动 | `GET /actuator/health` | `UP` | 手工 / 集成 |
| TC-OPS-01b | P1 | 探活 | — | `GET /health` | `200` `OK` | `RequirementsContractSmokeTest` |
| TC-OPS-02 | P2 | Prometheus | 运维放行 | `GET /actuator/prometheus` | `200` | 手工 |

**自动化类路径**：`web/src/test/java/com/watermarking/web/EndToEndWatermarkFlowTest.java`、`RequirementsContractSmokeTest.java`；`application` / `infrastructure` 下密码与幂等哈希单测。

---

## 7. 建议执行的 Maven / 手工命令

```bash
cd backend-java

mvn -q -pl application,infrastructure -am test

mvn -q -pl web -am test -Dtest=EndToEndWatermarkFlowTest

mvn -q -pl web -am test -Dsurefire.failIfNoSpecifiedTests=false -Dtest=RequirementsContractSmokeTest
```

PowerShell 请为 `-D` 参数加引号，例如 `"-Dsurefire.failIfNoSpecifiedTests=false"`、`"-Dtest=RequirementsContractSmokeTest"`。

---

## 8. 修订记录

| 日期 | 摘要 |
|------|------|
| 2026-05-12 | 初版：对照《选型》与《重构》；测试用例表 + `RequirementsContractSmokeTest`。 |
| 2026-05-12 | 增补架构/选型/SSR/CSRF 长文。 |
| 2026-05-12 | **文档分工**：`DEPLOY-SERVER.md` 定为技术框架+实操流水；本文定为生产检查、问题汇总、R 表、TC；检查清单链至 DEPLOY 各节；问题汇总表 E-01～E-06。 |
| 2026-05-12 | 架构与拓扑迁至 **watermark-java-backend-tech-selection.md**；**DEPLOY-SERVER** 收窄为生产部署与配置；删除对已移除《重构》文档的引用。 |

# 水印系统 Java 后端技术选型说明书（重写版）

> **说明**：本文件为选型说明书的 **Markdown 副本**，可在 Cursor / VS Code 中直接打开阅读。权威源仍可与 Cursor 计划 `java_后端技术选型_a5bdeb75.plan.md`（`%USERPROFILE%\.cursor\plans\`）对照；**Word 版**见同目录 [watermark-java-backend-tech-selection.docx](./watermark-java-backend-tech-selection.docx)（运行 `python build_tech_selection_docx.py` 从计划文件生成）。

本文档在梳理既有讨论的基础上**重写**，作为 **Flask 单体 → Java 后端** 的单一权威选型说明；**Maven/Gradle 坐标以各官方文档为准**，文中不绑定具体补丁版本号。

> 文档导航：部署命令见 [DEPLOY-SERVER.md](./DEPLOY-SERVER.md)，检查与测试见 [REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md](./REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md)，API 契约见 [API-CONTRACT.md](./API-CONTRACT.md)，安全基线见 [SECURITY-BASELINE.md](./SECURITY-BASELINE.md)，值班处置见 [RUNBOOK.md](./RUNBOOK.md)，服务目标见 [OPERATIONS-SLO-SLA.md](./OPERATIONS-SLO-SLA.md)，历史决策见 [adr/](./adr/)。

---

## 1. 文档目的与适用范围

- **目的**：固定后端技术栈、与基础设施边界、以及与 **Python 水印引擎** 的职责划分，支撑实现与评审。
- **范围**：服务端（API、鉴权、元数据、任务编排、对象存储对接）；**不**在首版用 Java 重写 PyTorch/wavmark 等算法，默认 **混合架构**。
- **读者**：**第 3 节（架构示意图）、第 8 节、第 11 节**。

---

## 2. 现状与约束

| 项 | 当前（Flask） | 约束 / 迁移注意 |
|----|----------------|-----------------|
| 应用形态 | [watermark/__init__.py](../../watermark/__init__.py) 单体 | 需拆出清晰 API 与异步边界 |
| 数据 | MySQL + SQLAlchemy | 表 `users` / `groups` / `files` / `user_group_rel` 见 [models.py](../../watermark/models.py) |
| 缓存 | 无 Redis | 多实例 Session、限流、任务态建议引入 Redis |
| 文件 | 本地 `INSTANCE_PATH` + `MEDIA_FOLDERS` | 多副本 K8s 下本地盘不适用，需对象存储 |
| 水印计算 | [AlgorithmSelector](../../watermark/utils/algorithm_selector.py) → Python 多模态 | **首版保留 Python Worker** |

---

## 3. 目标架构（纯文本示意图）

以下均为 **等宽字符框图**，不依赖 Mermaid 渲染；方框内尽量使用 **仓库真实名称**（Maven 模块名、Python 模块路径、中间件与配置前缀）。**部署命令与 env** 仍以 [DEPLOY-SERVER.md](./DEPLOY-SERVER.md) 为准。

### 3.1 运行时容器（进程边界）

逻辑上存在 **两个可部署容器**：**Spring Boot（`backend-java` 的 `web` 模块打出的 JAR）** 与 **`watermark.worker.redis_stream_worker`**；中间件与对象存储为 **外部依赖**。

```
                         +--------------------------------+
                         | Client（浏览器 / HTTP 调用方）   |
                         +----------------+---------------+
                                          | HTTPS
                         +----------------v---------------+
                         | Nginx（生产常见；示例见          |
                         | deploy/nginx-*.conf.example）  |
                         | proxy_pass → 127.0.0.1:8080    |
                         +----------------+---------------+
                                          |
    +-------------------------------------v-------------------------------------+
    | 【容器】Spring Boot — Maven 模块 **`web`**（可执行 JAR）                      |
    | · 对外：Thymeleaf、`/api/v1/**`、`/static/**`、springdoc、Actuator          |
    | · 对内模块依赖见 **§3.2**（`application` / `domain` / `infrastructure`）    |
    +--+----------------------------+----------------------------+--------------+
       |                            |                            |
       | JPA / Flyway               | Spring Data Redis          | S3 SDK /
       |                            |                            | AWS SDK v2（MinIO）
       v                            v                            v
+-------------------+  +-------------------+  +-------------------------------+
| **MySQL**         |  | **Redis**         |  | **对象存储（S3 兼容）/ MinIO** |
| `WM_DATASOURCE_*` |  | `WM_REDIS_*`      |  | `WM_STORAGE_BACKEND`          |
| 元数据事实源       |  | Session·Streams   │  |  对象字节事实源                |
|                   |  | ·任务态·限流·幂等  |  |                               |
+---------^---------+  +---------^---------+  +---------------^---------------+
          |                    |                            |
          | SQLAlchemy         | Streams / Hash             | boto3 / s3
          |                    |                            |
+---------+--------------------+----------------------------+------------------+
| 【容器】**`watermark.worker.redis_stream_worker`**（独立 OS 进程，常 systemd）  |
| · 入口：`python -m watermark.worker.redis_stream_worker`                      |
| · 编排：`watermark.utils.algorithm_selector.AlgorithmSelector`                |
| · 契约：队列 JSON **§10.1**；Redis Key **§10.2**                               |
+------------------------------------------------------------------------------+
```

**异步水印路径（与上图 Redis 段衔接）**：`web` 内业务经 **`infrastructure`** 向 **Redis Streams** `XADD`；**`redis_stream_worker`** `XREADGROUP` 消费 → 调 **`AlgorithmSelector`** → 回写 **MySQL** / 对象存储。

### 3.2 `backend-java` 内部（Maven 模块与依赖方向）

单 JVM 内分层；**依赖建议**：`web` → `application` → `domain`；**`infrastructure`** 向上实现 **`application`** 定义的端口，向下访问 **MySQL / Redis / 对象存储**。

```
+--------------------------------------------------------------------+
| **`web`** — `backend-java/web/`                                    |
| `com.watermarking.web.*` Controller · Thymeleaf · springdoc        |
+-----------------------------------+--------------------------------+
                                    | Spring 注入 / 调用用例
                                    v
+-------------------------------------------------------------------+
| **`application`** — `backend-java/application/`                   |
| `*ApplicationService`（认证、文件、水印任务、管理员）                |
+-----------------------------+-------------------------------------+
                              |
              +---------------+------------------------------------+
              v                                                v
+---------------------------+          +--------------------------------+
| **`domain`**              |          | **`infrastructure`**           |
| `backend-java/domain/`    |          | `backend-java/infrastructure/` |
| 实体 / 值对象 / 领域规则    |          |JPA · Flyway · Redis · 存储     |
+---------------------------+          +-------------+------------------+
                                                     |
                         +-------------+-------------+-------------+
                         v             v             v
                      MySQL         Redis      对象存储 / MinIO
```

**读图要点**

- **§3.1**：**两个进程** — Spring Boot（**`web` JAR**）与 **`watermark.worker.redis_stream_worker`**；**MySQL / 对象存储** 为事实存储，**Redis** 为会话、队列与短时任务态（与第 7、10 节一致）。
- **§3.2**：**一个 JVM** 内的 Maven 模块边界；**`infrastructure`** 不「调用」`domain`，而是 **实现 `application` 所需端口** 并访问外部系统。
- **事实源**：与 DB 终态冲突时以 **MySQL** 为准；Redis 可过期重建。

**控制面 / 数据面**：元数据与权限以 **MySQL** 为准；**对象存储 / MinIO** 仅存对象字节；**Redis** 承载会话、限流、短时任务态；**Python Worker** 只做重计算及必要的对象存储/MySQL 回写。

### 3.3 生产部署拓扑（示例：Nginx + 单 JVM）

与 [DEPLOY-SERVER.md](./DEPLOY-SERVER.md) 步骤一致；域名与网络拓扑可按实际环境替换。

```
                         Internet
                             |
              +------------+-------------+
              |                          |
      www.example.com            api.example.com
              |                          |
       +------v------+            +------v------+
       | Nginx :443 |            | Nginx :443  |
       +------+------+            +------+------+
              +---------+--------+
                        | proxy_pass
                        v
               +--------------------+
               | Spring Boot :8080  |
               | （同 §3.1 容器）     |
               +----------+---------+
          +-----+----------+-----+
          v                v     v
    MySQL :3306      Redis :6379  对象存储（HTTPS API）
          ^                |
          |                | Redis Stream（`WM_JOBS_*` 与 Stream key 对齐）
          |                v
          |         +------+----------------------+
          +---------+ **`redis_stream_worker`**   |
                    |（systemd 独立进程）         |
                    +---------------------------+
```

当 **`www`** 与 **`api`** 均 **`proxy_pass` 到同一 JVM** 时，页面与 **`/api/v1/**`** 多为 **同源**，Session Cookie 常用 **`SameSite=Lax`**（见 `deploy/watermark-api.env.example`）。若 **页面在 `www`、XHR 直连 `api` 子域`**，需 **CORS + `SameSite=None; Secure`**；见 `application-prod.yml`、`wm.cors.*` / `WM_CORS_*`（`web/config/CorsConfig.java`）。**CSRF 与威胁模型**见 [REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md](./REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md)。

### 3.4 技术选型在本仓库的落地摘要

| 领域 | 本仓库落地 |
|------|------------|
| 运行时 | Java 17 + Spring Boot 3.2；模块 **`domain` / `infrastructure` / `application` / `web`** |
| Web | Spring MVC + **Thymeleaf（SSR）** + springdoc-openapi；REST 前缀 **`/api/v1/**`** |
| 安全 | Spring Security 6 + **Session + Spring Session Redis**；`LegacyAwarePasswordEncoder`（Werkzeug 兼容） |
| 数据 | MySQL + JPA + Flyway；生产 **`ddl-auto: validate`** |
| 协作 | Redis：Session、任务态、幂等、限流；**Redis Stream** 投递水印任务 |
| 存储 | 生产默认对象存储（S3 兼容）+ STS/预签名；开发 **MinIO** |
| 计算 | **Python Worker**（`watermark.worker.redis_stream_worker`）消费队列，回写 MySQL/对象存储 |
| 阶段 4（Gateway、Nacos、Kafka 等） | 《选型》§16 **按需**；当前 **单体 + Nginx** 未引入不算缺陷 |

**与 §19 的差异（已决策）**：第 19 节默认叙事为 **SPA + Java 侧重 REST**；本仓库为 **Thymeleaf SSR + 同进程 API**。CSRF 策略与 §9.1「Cookie Session 须 CSRF」的严格表述存在张力时，以 **REQUIREMENTS** 文档中的安全跟踪表为准。

---

## 4. 技术选型总览

| 领域 | 选型 | 说明 |
|------|------|------|
| 语言 / 运行时 | **Java 21**（首选）或 **Java 17** LTS | 与 Spring Boot 3 一致 |
| 应用框架 | **Spring Boot 3.2+** | Jakarta EE 9+；禁止 `javax.*` 与 `jakarta.*` 混用未迁移库 |
| Web | **Spring Web MVC** + Jackson + Validation | 不全站 WebFlux；SSE 可选 |
| 安全 | **Spring Security 6** | BCrypt；Session 集群用 **Spring Session Redis** 或 **JWT**（见第 11 节） |
| ORM / 迁移 | **Spring Data JPA** + **Flyway** | 替代运行时 `ALTER TABLE`（见 [commands.py](../../watermark/commands.py)） |
| 连接池 | **HikariCP** | 短事务；水印编排异步化避免占满连接 |
| 缓存 / 协作 | **Spring Data Redis**（可选 **Redisson** 锁） | Redis **7.x**；生产建议主从/哨兵或云托管 |
| 对象存储（生产） | 对象存储服务（S3 兼容）+ 对应 SDK | STS 直传、分片、预签名下载 |
| 对象存储（开发） | **MinIO** + AWS SDK v2（S3 语义） | 不替代生产路径 |
| API 文档 | **springdoc-openapi** | OpenAPI 3 |
| 错误模型 | **RFC 7807 ProblemDetail** | 统一 `@ControllerAdvice` |
| 可观测 | **Micrometer** + Actuator；日志 JSON；**OpenTelemetry** 可选 | 对象存储错误码、队列深度、任务成功率 |
| 构建 | **Maven** 或 **Gradle Kotlin DSL** | 多模块时 Gradle 常见 |
| 容器 | **Docker Compose**（MySQL、Redis、MinIO、api、worker） | 生产镜像 Temurin/Distroless，非 root |

---

## 5. 应用分层与模块（单体多模块）

| 模块 / 包 | 职责 |
|-----------|------|
| `domain` | 实体、值对象、领域规则（尽量少依赖 Spring） |
| `application` | 用例：认证、文件登记、提交任务、管理员操作 |
| `infrastructure` | JPA、Redis、对象存储 SDK、队列发布、调 Worker 的 HTTP/gRPC |
| `interfaces` / `web` | Controller、DTO、全局异常、Security 配置 |
| `worker-contract`（可选） | 与 Python 约定的 JSON/OpenAPI/proto |

Bounded context 建议：`auth`、`file`、`watermark-job`、`admin`。

---

## 6. 数据层：MySQL + JPA + Flyway

- **字符集**：`utf8mb4`（库 + JDBC URL）。
- **Flyway**：所有结构变更版本化；与现有 Flask 表对齐后再迭代。
- **JPA 实践**：注意 N+1；水印相关长流程 **不要在同一事务内** 包住排队与 Worker 执行。
- **演进**：复杂只读 SQL 可局部引入 **MyBatis** 或 **jOOQ**，与 JPA 并存。

---

## 7. Redis：用途、结构与边界

| 用途 | 结构 | Key 示例 | 备注 |
|------|------|----------|------|
| Session 外置 | Hash（Spring Session） | `spring:session:*` | 多实例必选之一（若不用 JWT） |
| JWT 吊销 | String / Set | `jwt:deny:{jti}` | TTL ≥ token 剩余寿命 |
| 限流 | INCR + EXPIRE 或 ZSET | `rl:{userId}:{api}` | 嵌入/提取、上传回调重点限流 |
| 任务态 | Hash | `job:{jobId}` | `status` / `progress` / `error`；与 `File.processing_status` 终态对齐 |
| 幂等 | String | `idem:{userId}:{key}` | 配合 `Idempotency-Key` |
| 权限缓存 | String/Hash | `perm:{userId}` | 短 TTL + 变更失效 |
| 分布式锁 | Redisson 或 `SET NX EX` | `lock:file:{fileId}` | 定时清理、同文件并发写 |

**原则**：可重建的缓存与短时态在 Redis；**不可丢的业务事实** 以 MySQL（及对象存储对象存在性）为准。若队列可靠性要求极高，勿把 Redis 当作 **唯一** 持久队列源（见第 11 节）。

---

## 8. 对象存储（生产默认）

### 8.1 选型原则

- 生产环境使用稳定的对象存储服务（S3 兼容优先）。
- Java 侧优先采用统一 SDK 抽象，避免业务层绑定厂商专有 API。
- 文档、接口与监控指标采用厂商中立命名，便于后续迁移。

### 8.2 能力与落地要点

- **客户端**：进程内单例存储客户端（Spring Bean），合理超时与连接池。
- **上传**：浏览器/移动端 STS 临时凭证 + 最小权限策略 + 短 TTL；分片上传 + 分片失败重试；Complete 后校验 `ETag/Size` 再更新 MySQL `files` 并入队水印。
- **下载**：私有桶 + 预签名 GET（短 TTL）；大文件优先客户端直拉对象存储，避免 Java 中转带宽瓶颈。
- **成本与合规**：对象存储生命周期策略（标准→低频→归档）与业务 `retention_days` 分工（应用删元数据 + 存储侧降成本）；可选访问日志。
- **可选增强**：SSE、多 AZ、跨区域复制、版本控制；明确平台处理链与业务算法边界。

### 8.3 开发联调：MinIO

- 本地/CI 使用 **MinIO** 降低依赖真实云存储。
- 使用 **AWS SDK for Java v2** 对齐 S3 语义，减少环境切换成本。

---

## 9. 安全与 API 约定

### 9.1 认证与密码迁移

- **密码存储**：BCrypt。Flask **Werkzeug** 哈希与 BCrypt 不兼容 → 上线前定稿：**强制重置** 或 **登录时验旧哈希后重写 BCrypt**。
- **Session vs JWT**：见 **第 11.1 节**；多实例 **Session 必须外置 Redis**。
- **CSRF**：Cookie Session 场景应启用 CSRF 或等价防护；纯 JWT Header 模式按团队规范处理。

### 9.2 REST 与契约（示例）

- `POST /api/v1/auth/login|refresh|logout`
- `GET/PATCH /api/v1/users/me`（含 `retention`）
- `GET/POST/DELETE /api/v1/files`…；`POST /api/v1/files` 在对象存储直传后提交 **bucket/region/key/etag/size**
- `POST /api/v1/jobs/watermark`、`GET /api/v1/jobs/{jobId}`
- `GET /api/v1/admin/...` 对齐现有管理功能

**约定**：`/api/v1` 版本前缀；分页字段团队统一 0/1-based；**`Idempotency-Key`**；错误体 **Problem Details**；上传过渡 `multipart`，目标 **`POST /presign` + `POST /files/complete`**。

---

## 10. 长任务与 Python Worker（混合架构）

- **Java**：鉴权、配额、写库、写 Redis 任务态、投递队列、生成对象存储临时凭证。
- **Python**：消费队列，调用现有 [algorithm_selector](../../watermark/utils/algorithm_selector.py) 能力，读写对象存储，回写 **MySQL**（路径、`processing_status`、`error_message` 等）。

**队列**：首版 **Redis Streams**（或 List+BRPOP）与现有 Redis 投资一致；生产队列若要求更强持久化与 DLQ，演进 **RabbitMQ**。

**任务消息（JSON 建议字段）**：`jobId`、`fileId`、`operation`（embed/extract）、`objectKey` 或本地路径、`mediaType`、`watermarkText`/`watermarkSeed`、`algorithm`（可选）、`traceId`；回写成功/失败与产物 Key。

### 10.1 队列消息 JSON Schema（Java ↔ Worker 契约）

实现须与 OpenAPI 及 Worker 解析逻辑一致；字段名以代码为准，以下为 **约定形状**：

```json
{
  "jobId": "uuid",
  "fileId": 0,
  "operation": "embed|extract",
  "objectKey": "string",
  "bucket": "string",
  "region": "string",
  "mediaType": "image|audio|video|text",
  "watermarkText": "string|null",
  "watermarkSeed": "string|null",
  "algorithm": "string|null",
  "traceId": "string"
}
```

Worker 完成时：更新 MySQL `files.processing_status`、`watermarked_path` 或对象 key、`error_message`；可选写回 `wm:job:{jobId}` 后删除或短 TTL。

### 10.2 Redis Key 命名补充（`wm:` 前缀）

与第 7 节示例 Key 对齐；多系统共 Redis 时建议统一 **`wm:`** 前缀：

| 用途 | 推荐模式（示例） |
|------|------------------|
| Session | `spring:session:*`（Spring Session 默认） |
| 任务态 | `wm:job:{jobId}`（或与第 7 节一致仅用 `job:{jobId}`，团队二选一写进 README） |
| 限流 | `wm:rl:{userId}:{endpoint}` |
| 幂等 | `wm:idem:{userId}:{idempotencyKey}` |
| 分布式锁 | `wm:lock:file:{fileId}` |

**纯 Java 水印**：默认 **不推荐** 作首版（算法与 PyTorch 绑定过重）。

---

## 11. 关键横向决策

### 11.1 认证：Session（Redis）vs JWT

| 维度 | Session + Spring Session Redis | JWT Access + Refresh |
|------|----------------------------------|----------------------|
| 吊销 | 删 Session 即可 | 需 Redis 黑名单或 token 版本 |
| SPA 跨域 | Cookie 策略复杂 | Header 较自然 |
| 建议场景 | 内网、管理端、快速对齐 Flask | 公网 SPA、多端 |

可 **混合**：管理端 Session，开放 API JWT。

### 11.2 队列：Redis vs RabbitMQ vs Kafka

| 方案 | 适用 |
|------|------|
| Redis List / Streams | 中小流量、运维希望组件少 |
| **RabbitMQ** | 强 DLQ、延迟、多消费者公平、队列运维成熟 |
| Kafka | 分析流、事件溯源；首版一般过重 |

### 11.3 ORM：JPA vs MyBatis

- **默认 JPA**；报表/批量极痛时 **局部 MyBatis/jOOQ**。

---

## 12. 配置、密钥与环境

- **敏感配置**：DB、对象存储 AccessKey/SecretKey、STS 角色、JWT 密钥 → **环境变量 / K8s Secret / KMS**，不入 Git。
- **CORS**：固定 Origin；`allowCredentials=true` 时 **禁止 `*`**。

---

## 13. 可观测性、测试与发布

- **指标**：HTTP、JVM、对象存储按错误码、队列深度、任务成功/失败率、分片完成率。
- **日志**：结构化 JSON；**traceId** 贯穿 API 与 Worker（OTel 可选）。
- **测试**：JUnit 5、AssertJ、**Testcontainers**（MySQL、Redis、MinIO）；**WireMock** 模拟 Worker。
- **CI**：单测、镜像（**Jib / Buildpacks**）、**OWASP Dependency-Check** 或 Snyk。

---

## 14. 可选组件（按触发条件）

| 组件 | 何时引入 |
|------|-----------|
| Spring Cloud Gateway | 多服务、统一 TLS/限流/路由 |
| Nacos / Sentinel | 大规模微服务、动态配置与流控 |
| Elasticsearch | 全文检索远超 MySQL LIKE 能力时 |
| Scheduling + **Redis 锁** | **多副本** 下 `retention_days` 清理（建议生产） |
| RabbitMQ | Redis 队列成为瓶颈或可靠性短板时 |
| XXL-JOB / ShedLock | 定时任务规模大、需平台化管理时 |

---

## 15. 常见业务场景与能力映射（简表）

| 场景 | 能力组合 |
|------|-----------|
| 大文件上传 | 对象存储 STS + 分片 + `files/complete` + 入队 |
| 任务进度 | Redis `job:*` + 轮询或 SSE |
| B2B 组织与权限 | Spring Security + RBAC；现有 `Group`/`role`；可选 OIDC |
| 风控 | Redis 限流 + 配额；`Idempotency-Key` |
| 分发 | CDN + 对象存储源；涉密对象仅短 TTL 签名 URL |
| 合规与留存 | `retention_days` + 对象存储生命周期 + 审计表 |
| 开放集成 | OpenAPI + Webhook + API Key/OAuth2 Client |

---

## 16. 分阶段落地里程碑

| 阶段 | 目标 |
|------|------|
| **0** | 冻结：混合架构、SPA vs SSR、队列、认证模式 |
| **1** | Boot + Flyway + JPA 对齐模型 + Security + Actuator + OpenAPI + Compose（MySQL/Redis） |
| **2** | 本地 `StorageService` + 入队 + Python Worker 闭环 + Resilience4j |
| **3** | 对象存储 STS + 分片 + 预签名下载 + 定时清理（锁）+ 限流 + Prometheus/Grafana +（可选）OTel |
| **4** | Gateway、Nacos/Sentinel、OIDC、Kafka/CDC、Webhook（按需） |

**质量**：各阶段保持 Testcontainers 与 CI 不降级。

---

## 17. 职能方向补充（可选）

| 方向 | 说明 |
|------|------|
| 平台工程 | 强调 Spring Boot、任务编排、可靠性与可观测建设 |
| 存储集成 | 强调对象存储接入、临时凭证、分片上传、预签名下载与成本治理 |

---

## 18. 可选技术速查（非默认）

- **框架替代**：Quarkus / Micronaut（原生镜像、冷启动）。
- **调用 Worker**：WebClient / RestClient、OpenFeign、gRPC、**Resilience4j**。
- **工程**：MapStruct、Apache Tika（MIME）、ArchUnit。
- **Java 图像轻处理**：Thumbnailator（**不能**替代 PyTorch 水印）。

---

## 19. 与前端衔接

- **推荐（说明书默认）**：Java 侧重 **REST + OpenAPI**；Vue/React SPA；灰度用 Nginx/Gateway。
- **不推荐首做**：Thymeleaf 全量迁移 Jinja（成本高、收益低）。
- **本仓库实际**：采用 **Thymeleaf SSR + 同进程 `/api/v1`**（方案 Y），与上两条「默认推荐」并存；Cookie、CSRF、CORS 以生产配置与 [REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md](./REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md) 为准。

---

## 20. 修订记录（本版）

- **重写**：合并原分散章节为单线叙述；生产存储采用厂商中立的对象存储叙事；删除与业务无关的招聘导向内容与重复段落。
- **后续**：待决策项见 Cursor 计划文件 YAML `todos`；接口级 OpenAPI 草图可在 `decide-*` 冻结后单独产出。
- **生产部署步骤**：[DEPLOY-SERVER.md](./DEPLOY-SERVER.md)（仅服务器操作与环境变量）。
- **上线检查 / 问题 / 测试**：[REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md](./REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md)。
- **Word 导出**：[watermark-java-backend-tech-selection.docx](./watermark-java-backend-tech-selection.docx)；若仓库内提供生成脚本则按计划文件导出（需 `pip install python-docx`）。
- **Markdown 副本（本文件）**：便于 IDE 阅读；更新时请与 Cursor 计划 `java_后端技术选型_a5bdeb75.plan.md` 手动对齐或重新导出。

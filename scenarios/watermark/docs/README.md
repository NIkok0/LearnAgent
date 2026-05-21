# Watermark Materials Platform

司法材料确权数字水印平台 — 总览（测试知识库）。

## Overview

本平台为司法材料提供数字水印嵌入、任务队列处理与 API 集成。核心组件：

- **Java API** — REST 接口、认证、任务状态查询
- **Worker** — 消费 Redis Stream `wm:jobs:stream` 执行水印算法
- **Object Storage** — 原始文件与处理后文件
- **LearnAgent** — 运维 Copilot，通过 `search_docs` 与受控 HTTP 工具协助排障

## Default Redis Stream Key

Watermark platform 默认任务队列 Redis Stream key 为 **`wm:jobs:stream`**（环境变量 `WM_JOBS_STREAM_KEY`）。LearnAgent `search_docs` 与 Worker 均使用该默认值，除非部署文档另行覆盖。

## Documentation Map

| Document | Purpose |
|----------|---------|
| API-CONTRACT.md | REST 路径与字段 |
| DEPLOY-SERVER.md | 部署、verify-config、环境变量 |
| SECURITY-BASELINE.md | 安全与白名单 |
| RUNBOOK.md | QUEUED/PROCESSING/FAILED 排障 |
| OPERATIONS-SLO-SLA.md | SLO 与告警 |
| REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md | 已知偏差与测试用例 |
| watermark-java-backend-tech-selection.md | Redis Stream、队列 JSON |
| README_ALGORITHM.md | 算法与文件类型 |

## Quick Start

1. 配置 `WM_REDIS_URL` 与 `WM_JOBS_STREAM_KEY`
2. 运行 `verify-config.sh` 或 `verify-config.ps1`
3. 启动 API 与 Worker
4. 登录后查询 `GET /api/v1/files` 或提交水印任务（需审批）

## Agent Usage

- 部署、队列、已知问题 → 先 `search_docs`，引用文档名
- 实时任务状态 → `GET /api/v1/jobs/{id}`
- 创建任务 → 需用户批准且 `COPILOT_ALLOW_JOB_POST=true`

## Test Fixture Notice

本目录下 Markdown 为 LearnAgent RAG 评测用虚构内容，路径默认 `docs/source/` 或由 `WATERMARK_DOCS_PATH` 覆盖。

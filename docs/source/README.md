# 文档语料已迁移

RAG 测试知识库 Markdown 已迁至 **`scenarios/watermark/docs/`**（与 watermark Demo 配置同目录）。

- Kernel 默认场景是 `minimal`，只使用 `scenarios/minimal/docs/`。
- watermark Demo 需要显式设置 `SCENARIO=watermark`。
- 通用覆盖路径使用 `COPILOT_DOCS_PATH`，或在 Scenario `resources.docs_path_env` 中声明专用环境变量。

请勿再向本目录添加语料；watermark Demo 语料编辑 `scenarios/watermark/docs/`。

# LearnAgent Embodied VLA 设计

> **本文负责**：SO-ARM101 / SO-101 VLA 学习闭环、机器人 Capability、数据/模型/部署/评估边界。
> **本文不负责**：真实硬件驱动细节、LeRobot 训练脚本参数大全、机器人力控算法。
> **相关文档**：[capability-design.md](./capability-design.md)、[tool-design.md](./tool-design.md)、[runtime-design.md](./runtime-design.md)、[ci-design.md](./ci-design.md)

## 1. 闭环目标

第一阶段目标是打通可写进简历的最小 VLA 工程闭环：

```text
SO-ARM101 bring-up
  -> LeRobot dataset
  -> ACT baseline
  -> SmolVLA fine-tune
  -> real robot rollout
  -> LearnAgent Timeline / Audit replay
```

LearnAgent 不替代 LeRobot 训练栈；它负责把机器人策略变成可治理、可审计、可恢复的 Agent Capability。

## 2. Capability 边界

`embodied` capability 暴露六个工具：

| Tool | 职责 | 风险 |
|---|---|---|
| `observe_scene` | 返回 frame id、image hash、物体摘要、workspace bounds | low |
| `run_vla_policy` | 根据 instruction + frame 产生 action chunk | medium |
| `execute_action_chunk` | 执行动作块，必须经过安全检查和审批 | high |
| `stop_robot` | 紧急停止，不能被审批延迟 | high |
| `recover_home` | 回到安全 home pose | medium |
| `label_episode_result` | 记录 rollout success/failure | low |

原则：
- 工具结果不记录 raw image、depth frame、完整用户原文或大体积动作日志。
- EventStore 只记录治理元数据：frame id、image hash、policy checkpoint、action count、safety/result。
- 真实 LeRobot / SO-ARM101 驱动接在 adapter 后面，不绕过 ToolRegistry、PolicyGate 和 ToolResultModel。

## 3. 数据与模型

第一版固定任务：桌面抓取整理。

数据最小字段：
- observation：RGB / RGB-D frame 引用与 hash。
- state：关节角、夹爪状态、末端位姿。
- action：action chunk 或下一步动作。
- instruction：语言任务；事件中只保存 hash。
- metadata：episode id、object、lighting、initial pose、success/failure。

训练顺序：
1. ACT baseline：验证数据质量和动作空间。
2. Diffusion Policy：验证连续动作建模。
3. SmolVLA fine-tune：进入 VLA 路线。

## 4. Runtime 回放

一次机器人 rollout 在 Timeline 中应至少能解释：

```text
robot_observation_recorded
  -> vla_policy_inferred
  -> robot_action_executed
  -> robot_episode_labeled
```

Debugger summary 暴露 `robot_rollout`：
- observations
- policy_inferences
- actions
- labels
- failed_actions
- last_policy_checkpoint_id

这使失败定位可以区分：感知失败、策略失败、动作越界、执行失败、人工停止或标签失败。

## 5. 验证入口

本轮只提供 deterministic mock 闭环：

```powershell
python scripts/verify_embodied_vla_v1.py
python scripts/verify_eval_suite.py --profile core-fast
```

验收标准：
- `embodied_so101` Scenario 可加载；
- `embodied` capability 注册六个工具；
- `execute_action_chunk` 需要审批，`stop_robot` 不被审批阻塞；
- mock observe → policy → execute → label 可跑通；
- robot events 通过 contract validation；
- Timeline 能投影 robot rollout summary。


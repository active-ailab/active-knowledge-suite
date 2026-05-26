# Active Knowledge Server 评审变更追踪矩阵

> 文档状态：Trace Matrix  
> 更新日期：2026-05-07  
> 关联文档：
> - [架构与方案设计](./active_knowledge_server_architecture_design.md)
> - [工程 TODO](./active_knowledge_server_engineering_todo.md)

---

## 1. 追踪目标

本文用于把 Phase 0 的评审意见从“问题描述”追踪到：

- 设计改动位置
- 实现任务 ID
- 测试任务 ID
- release gate 或验收门槛

维护规则：

- 新增评审意见时，必须先分配稳定 `review_id`。
- 每条评审意见必须能追到至少一个设计章节、一个实现任务、一个测试任务或 gate。
- 当架构文档、TODO ID 或 gate 名称变化时，必须同步更新本矩阵。
- 关闭评审意见前，相关 D0 设计任务必须完成；实现任务可继续处于后续 Phase。

---

## 2. 评审意见主追踪表

| review_id | 原评审意见 | 设计改动位置 | 设计任务 | 实现任务 ID | 测试任务 ID | 验收 gate | 当前状态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R-01 | 补查询契约附录。 | [9.2 查询意图分类](./active_knowledge_server_architecture_design.md#92-查询意图分类)、[10.7 Skill 路由建议](./active_knowledge_server_architecture_design.md#107-skill-路由建议)、[附录 A：查询契约](./active_knowledge_server_architecture_design.md#附录-a查询契约)。 | `D0-01`、`D0-02`、`D0-03`、`D0-04` | `Q5-01`、`Q5-02`、`Q5-03`、`Q5-04`、`Q5-05`、`Q5-06`、`Q5-07`、`Q5-10`、`M6-02`、`M6-06` | `Q5-01` schema snapshot、`Q5-02` intent 正反例、`Q5-09` 异常状态契约、`E7-01` eval cases | `E7-02` 质量阈值 gate、`E7-05` 失败回归门槛、`E7-07` release checklist | 设计已关闭；实现和 gate 待 Phase 5/7。 |
| R-02 | 补存储一致性附录。 | [7.5 baseline + local overlay 查询模型](./active_knowledge_server_architecture_design.md#75-baseline--local-overlay-查询模型)、[附录 B：存储一致性](./active_knowledge_server_architecture_design.md#附录-b存储一致性)。 | `D0-05` | `S2-01`、`S2-02`、`S2-03`、`S2-04`、`S2-05`、`S2-06`、`S2-07`、`S2-08`、`S2-09`、`P4-06` | `S2-05` tombstone/replacement、`S2-06` relation、`S2-08` validate、`S2-09` clean/compact | `E7-04` 稳定性阈值 gate、`E7-06` 可重复索引 gate、`E7-07` release checklist | 设计已关闭；实现和 gate 待 Phase 2/4/7。 |
| R-03 | 补 profile 规范。 | [profile 表设计](./active_knowledge_server_architecture_design.md#profile)、[8.6 增量索引](./active_knowledge_server_architecture_design.md#86-增量索引)、[11.5 Profile-aware 查询](./active_knowledge_server_architecture_design.md#115-profile-aware-查询)、[附录 C：Profile 规范](./active_knowledge_server_architecture_design.md#附录-cprofile-规范)。 | `D0-06` | `X3-03`、`X3-05`、`P4-02`、`P4-05`、`P4-06`、`Q5-08` | `P4-02` auto 选择确定性测试、`P4-05` profile-conditioned relation、`Q5-08` multi-profile/unresolved 契约 | `E7-02` Profile Correctness、`E7-05` 失败回归门槛、`E7-06` 可重复索引 gate | 设计已关闭；实现和 gate 待 Phase 3/4/5/7。 |
| R-04 | 合并第 14 节和第 19 节为验收门槛。 | [14. 验收门槛与回归策略](./active_knowledge_server_architecture_design.md#14-验收门槛与回归策略)。原第 19 节建议项已合并到 14.7 Gate 映射。 | `D0-07` | `E7-01`、`E7-02`、`E7-03`、`E7-04`、`E7-05`、`E7-06`、`E7-07` | `E7-01` eval cases、`E7-02` quality gate、`E7-03` perf gate、`E7-04` stability gate、`E7-05` regression gate、`E7-06` reproducibility gate | `active-kb eval run --gate v1`、`active-kb validate --gate v1`、`active-kb perf run --gate v1` | 设计已关闭；gate 实现待 Phase 7。 |
| R-05 | 远程安全改成 fail-safe，并拆成本地/远程两套配置。 | [6.5 部署模式与配置样式](./active_knowledge_server_architecture_design.md#65-部署模式与配置样式)、[13.3 远程服务安全](./active_knowledge_server_architecture_design.md#133-远程服务安全)、[18.1 运行状态与健康检查](./active_knowledge_server_architecture_design.md#181-运行状态与健康检查)、[examples/local-single-user.yaml](../examples/local-single-user.yaml)、[examples/remote-shared.yaml](../examples/remote-shared.yaml)。 | `D0-08` | `C1-03`、`C1-05`、`M6-04`、`M6-05`、`O8-03`、`O8-04` | `C1-05` fail-safe config tests、`M6-05` HTTP auth/origin tests、`O8-03`/`O8-04` example validation | `E7-02` blocked/security contract、`E7-04` stability gate、`E7-07` release checklist | 设计已关闭；实现和 deployment docs 待 Phase 1/6/8。 |

---

## 3. Phase 0 设计变更明细

| D0 ID | 设计变更摘要 | 架构文档位置 | 后续实现任务 | 后续测试或 gate |
| --- | --- | --- | --- | --- |
| `D0-01` | 固化 intent 枚举、分类输入输出、触发信号和低置信降级。 | [附录 A.1-A.9](./active_knowledge_server_architecture_design.md#附录-a查询契约)、[9.2 查询意图分类](./active_knowledge_server_architecture_design.md#92-查询意图分类) | `Q5-02`、`M6-06` | `Q5-02` intent fixture、`E7-01` eval cases |
| `D0-02` | 固化 `kb_search` 与专用工具选路、串联和禁止路由。 | [附录 A.10-A.14](./active_knowledge_server_architecture_design.md#a10-v1-工具定位)、[10.7 Skill 路由建议](./active_knowledge_server_architecture_design.md#107-skill-路由建议) | `Q5-02`、`Q5-07`、`M6-02`、`M6-06` | `Q5-09` route/blocked fixture、`E7-02` quality gate |
| `D0-03` | 固化 `QueryResult` 外壳、`result_status`、异常状态格式和 JSON 示例。 | [附录 A.15-A.20](./active_knowledge_server_architecture_design.md#a15-queryresult-统一外壳) | `Q5-01`、`Q5-09`、`Q5-10` | `Q5-01` schema snapshot、`Q5-09` zero/multi/ambiguous/low confidence fixture |
| `D0-04` | 固化 warning level、code 命名、registry、Skill 处理和审计规则。 | [附录 A.21-A.26](./active_knowledge_server_architecture_design.md#a21-warning-对象) | `Q5-01`、`Q5-09`、`M6-02` | `Q5-09` blocked/partial fixture、`E7-02` Warning Quality |
| `D0-05` | 新增 baseline/overlay 合并、tombstone、replacement、FTS/vector、migration、clean 一致性契约。 | [附录 B：存储一致性](./active_knowledge_server_architecture_design.md#附录-b存储一致性)、[7.5](./active_knowledge_server_architecture_design.md#75-baseline--local-overlay-查询模型) | `S2-01` 至 `S2-09`、`P4-06` | `S2-08 validate`、`E7-04`、`E7-06` |
| `D0-06` | 新增 profile identity、`auto` 选择、多候选、multi-profile 展示和重算边界。 | [附录 C：Profile 规范](./active_knowledge_server_architecture_design.md#附录-cprofile-规范)、[11.5](./active_knowledge_server_architecture_design.md#115-profile-aware-查询) | `P4-02`、`P4-05`、`Q5-08` | `P4-02` auto fixture、`Q5-08` profile-aware tests、`E7-02` Profile Correctness |
| `D0-07` | 把评测和补充需求统一为 release gate，定义质量、性能、稳定性和失败回归阈值。 | [14. 验收门槛与回归策略](./active_knowledge_server_architecture_design.md#14-验收门槛与回归策略) | `E7-01` 至 `E7-07` | `active-kb eval run --gate v1`、`active-kb validate --gate v1`、`active-kb perf run --gate v1` |
| `D0-08` | 定义 local/remote 部署模式、fail-safe 启动校验、token/OIDC 抽象和两份示例配置。 | [6.5](./active_knowledge_server_architecture_design.md#65-部署模式与配置样式)、[13.3](./active_knowledge_server_architecture_design.md#133-远程服务安全)、[18.1](./active_knowledge_server_architecture_design.md#181-运行状态与健康检查) | `C1-03`、`C1-05`、`M6-04`、`M6-05`、`O8-03`、`O8-04` | `C1-05` fail-safe tests、`M6-05` HTTP security tests、`E7-02` blocked/security contract |
| `D0-09` | 建立本追踪矩阵，并在架构文档和 TODO 文档中互链。 | [19. 评审变更追踪](./active_knowledge_server_architecture_design.md#19-评审变更追踪) | 后续所有受评审项影响的任务 | `E7-07` release checklist 检查 trace 是否更新 |

---

## 4. Gate 对应关系

| Gate | 覆盖评审项 | 最低要求 |
| --- | --- | --- |
| `E7-01` V1 eval cases | R-01、R-03、R-04 | 查询、profile、warning、存储与增量样本进入 `eval/cases.yaml`。 |
| `E7-02` 质量阈值 gate | R-01、R-03、R-04、R-05 | Schema compliance、Evidence Hit Rate、Top-k Recall、Profile Correctness、Warning Quality、blocked/security contract 达标。 |
| `E7-03` 性能阈值 gate | R-04 | init、server startup、核心查询工具、incremental index 的 P50/P95 可报告。 |
| `E7-04` 稳定性阈值 gate | R-02、R-04、R-05 | 长时间运行、并发只读、索引中断恢复、migration 幂等、partial_ready 可用。 |
| `E7-05` 失败回归门槛 | R-01、R-03、R-04 | 质量指标不得明显倒退；P95 latency 超 20% 默认阻断，显式豁免必须进入报告；新增 bug 必须进入 regression case。 |
| `E7-06` 可重复索引 gate | R-02、R-03、R-04 | 同一 snapshot/profile 重复索引 stable ID 和 checksum 稳定。 |
| `E7-07` Release checklist | R-01 至 R-05 | baseline manifest、版本、schema、gate 报告和本文追踪矩阵都随 release artifact 更新。 |

---

## 5. 关闭标准

评审意见关闭需要满足：

- 对应 D0 设计任务状态为 `[x]`。
- 本矩阵主追踪表包含原意见、设计位置、实现任务、测试任务和 gate。
- 架构文档末尾能跳转到本矩阵。
- 工程 TODO 中 D0-09 能跳转到本矩阵。
- 对应后续实现任务进入 Phase 计划；若未实现，状态不得标成 release-ready。

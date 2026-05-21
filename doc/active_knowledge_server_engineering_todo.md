# Active Knowledge Server 工程化 TODO

> 文档状态：Draft TODO  
> 生成日期：2026-05-06  
> 适用对象：`active-knowledge-server`  
> 依据文档：
> - [Active Knowledge Server 架构与方案设计](./active_knowledge_server_architecture_design.md)
> - [Active Knowledge Server 评审变更追踪矩阵](./active_knowledge_server_review_trace.md)
> - [面向大型 RTOS 项目的知识库 MCP + Skill 全案设计（正式版）](./rtos_engineering_kb_mcp_skill_full_design.md)

---

## 1. 文档目标

本文将架构方案拆解为可实施、可验收、可排期的工程化 TODO 序列。它不是重新描述架构，而是把设计落到以下维度：

- 先补齐影响长期稳定性的契约和附录
- 再按工程依赖顺序实现配置、安全、存储、索引、查询、MCP、评测、运维
- 将评审意见转换成明确任务，插入到主 TODO 序列中
- 为每个阶段给出产出物、依赖关系、验收标准和回归门槛

本文默认采用行业实践中的几条原则：

- **Contract first**：MCP schema、查询返回、存储合并、profile 选择和 warning 分级先定义，再实现。
- **Evidence first**：所有高价值查询必须返回 evidence，低置信、歧义、缺上下文时必须显式 warning。
- **Local-first but upgradeable**：V1 先用 SQLite + FTS5 + LanceDB，本地 baseline + overlay；后端通过 storage adapter 可替换。
- **Fail-safe security**：本地单机默认安全，远程共享服务必须显式启用并满足认证、Origin、审计和 ops 隔离。
- **Regression gated**：每次 parser、retriever、router、storage migration 或 MCP schema 变化，都必须跑契约测试和评测回归。

---

## 2. 任务标记约定

状态：

- `[ ]` 未开始
- `[~]` 进行中
- `[x]` 已完成
- `[!]` 阻塞或需评审决策

优先级：

- `P0`：实现前必须完成，属于契约、数据一致性、安全或验收门槛
- `P1`：V1 MVP 必须完成
- `P2`：V1 可延后但建议尽快完成
- `P3`：V2+ 演进任务

任务类型：

- `DOC`：文档与设计补充
- `CONTRACT`：数据契约、接口契约、schema、行为规范
- `IMPL`：代码实现
- `TEST`：单测、集成测试、契约测试、评测
- `OPS`：部署、运维、迁移、清理、监控
- `SEC`：安全、权限、审计、敏感信息治理

通用完成定义：

- 有明确输入、输出、失败行为和降级策略
- schema 或返回结构有测试覆盖
- 关键行为有 golden case 或 eval case
- 变更已写入设计文档、README 或示例配置
- 不把 SQLite/LanceDB 内部细节泄漏给 MCP 或 Skill

---

## 3. 评审意见到任务映射

完整设计位置、实现任务、测试任务和验收 gate 的可追踪矩阵见 [Active Knowledge Server 评审变更追踪矩阵](./active_knowledge_server_review_trace.md)。

| 评审项 | 转换后的任务 | 插入位置 |
| --- | --- | --- |
| 1. 补查询契约附录 | `D0-01` 至 `D0-04`，并在 `Q5-01` 至 `Q5-07` 落实到实现和测试 | Phase 0、Phase 5 |
| 2. 补存储一致性附录 | `D0-05`，并在 `S2-01` 至 `S2-09` 落实 baseline/overlay 合并、tombstone、replacement、migration、清理 | Phase 0、Phase 2 |
| 3. 补 profile 规范 | `D0-06`，并在 `P4-01` 至 `P4-06`、`Q5-08` 落实 auto 选择、多 profile 展示、增量/全量重算边界 | Phase 0、Phase 4、Phase 5 |
| 4. 合并第 14 节和第 19 节为验收门槛 | `D0-07`，并在 `E7-01` 至 `E7-07` 建立质量、性能、稳定性、失败回归门槛 | Phase 0、Phase 7 |
| 5. 远程安全改成 fail-safe，并拆成本地/远程两套配置 | `D0-08`，并在 `C1-05`、`M6-05`、`O8-03`、`O8-04` 实现配置校验和示例 | Phase 0、Phase 1、Phase 6、Phase 8 |

---

## 4. 里程碑总览

| 里程碑 | 目标 | 主要产出 | 阻断关系 |
| --- | --- | --- | --- |
| Phase 0 | 契约与设计收敛 | 查询契约、存储一致性附录、profile 规范、验收门槛、安全配置样式 | 所有实现前置 |
| Phase 1 | 工程骨架、配置与安全底座 | Python package、CLI、config schema、path guard、auth/audit 骨架 | Phase 2+ |
| Phase 2 | 存储层与 baseline/overlay | SQLite schema、LanceDB adapter、tombstone/replacement、migration、clean | Phase 3+ |
| Phase 3 | Source discovery 与解析底座 | workspace/docs/build connectors、Markdown/front matter、Kconfig/makefile parser | Phase 4+ |
| Phase 4 | 索引流水线与 profile | snapshot、profile、doc/code indexing、incremental jobs、workspace map | Phase 5+ |
| Phase 5 | 查询服务契约实现 | router、retrievers、fusion/rerank、evidence packager、warning contract | Phase 6+ |
| Phase 6 | MCP 与 Skill 接口 | FastMCP tools/resources、ops tools gating、stable schema、Skill 路由示例 | Phase 7+ |
| Phase 7 | 评测与验收门槛 | eval cases、质量阈值、性能阈值、稳定性和失败回归门槛 | Release gate |
| Phase 8 | 运维、部署与发布 | init/index/serve/validate/clean/migrate、local/remote 示例、baseline publish | V1 Release |
| Phase 9 | V2+ 增强 | runtime/impact、compile DB、clang index、多角色知识域、权限治理 | V1 后 |

---

## 5. Phase 0：契约与设计收敛

Phase 0 必须在大规模实现前完成。原因是查询契约、存储一致性、profile 选择、安全模式和验收门槛一旦后补，容易造成实现返工。

### D0-01 查询契约附录：intent 分类规则

- 状态：`[x]`
- 优先级：`P0`
- 类型：`DOC`、`CONTRACT`
- 依赖：架构文档第 9、10、11 节
- 目标：在架构文档中新增“附录 A：查询契约”，明确 Query Router 对用户问题的分类规则。
- 产出：架构文档已新增“附录 A：查询契约”，并补齐第 9.2 节 intent 列表。

TODO：

- [x] 定义 `intent` 枚举：`code_exact`、`code_concept`、`call_trace`、`runtime_flow`、`profile_diff`、`api_lookup`、`widget_lookup`、`workspace_nav`、`product_context`、`project_context`、`evidence_lookup`、`unknown`。
- [x] 为每个 intent 定义触发信号：符号形态、路径形态、宏名、错误码、自然语言描述、profile 关键词、API/控件关键词、运行时关键词。
- [x] 定义 intent 分类输入：`query`、`domain`、`view`、`granularity`、`profile_id`、`snapshot_id`、`caller_tool`、`client_context`。
- [x] 定义 intent 分类输出：`intent`、`confidence`、`matched_signals`、`selected_view`、`selected_granularity`、`profile_resolution`、`warnings`。
- [x] 明确 `unknown` 和低置信分类的降级策略：默认走 `kb_search` 的 hybrid recall，但返回 `low_confidence` warning 和 `next_queries`。

验收标准：

- 每个 intent 至少有 3 个正例和 2 个反例。
- 分类规则可以直接转换成单元测试 fixture。
- Skill 路由矩阵能引用同一组 intent，不再另起一套分类术语。

### D0-02 查询契约附录：`kb_search` 与专用工具选路规则

- 状态：`[x]`
- 优先级：`P0`
- 类型：`DOC`、`CONTRACT`
- 依赖：`D0-01`
- 目标：明确什么时候使用统一入口 `kb_search`，什么时候应直接调用专用工具。
- 产出：架构文档附录 A.10 至 A.14 已定义 V1 工具定位、intent 到工具决策表、串联规则、`kb_search` 边界和禁止路由。

TODO：

- [x] 定义 `kb_search` 的定位：探索性、混合域、低上下文、先召回候选证据。
- [x] 定义专用工具的定位：`code_resolve` 用于符号/路径/宏定位，`docs_search` 用于 API/控件/文档，`code_trace` 用于链路，`config_impact` 用于 profile/宏影响，`workspace_view` 用于结构视图。
- [x] 建立路由决策表：用户问题类型 -> 首选工具 -> 备选工具 -> 何时回退。
- [x] 明确工具串联规则：例如 `docs_search(doc_type=api)` -> `code_resolve` -> `evidence_bundle`。
- [x] 定义禁止路由：Skill 不得直接依赖 ops tools、SQLite 表、LanceDB collection、artifact 内部路径。

验收标准：

- 每个 V1 tool 都有“适用 / 不适用 / 回退”的描述。
- `kb_search` 不被设计成吞掉所有查询的万能入口，专用工具仍有明确价值。
- 路由规则可以写入 Skill 的 `question-intents.md` 或等价文档。

### D0-03 查询契约附录：零结果、多结果、歧义、低置信返回格式

- 状态：`[x]`
- 优先级：`P0`
- 类型：`DOC`、`CONTRACT`
- 依赖：`D0-01`、`D0-02`
- 目标：固定 MCP QueryResult 的异常和不确定性返回格式，避免 Skill 误把不完整结果写成确定结论。
- 产出：架构文档附录 A.15 至 A.20 已定义 QueryResult 统一外壳、`result_status`、confidence 分段、异常状态强制格式、JSON 示例和契约测试要求。

TODO：

- [x] 定义 `result_status` 枚举：`ok`、`zero_result`、`multi_result`、`ambiguous`、`low_confidence`、`partial_ready`、`blocked`、`error`。
- [x] 定义 `confidence` 分段：`high >= 0.80`、`medium 0.50-0.79`、`low < 0.50`，并允许具体工具覆盖阈值。
- [x] 定义零结果返回：`items=[]`、`evidence_refs=[]`、`summary` 不生成确定结论、必须返回 `next_queries` 或 `suggested_filters`。
- [x] 定义多结果返回：候选必须包含 `disambiguation_key`、`entity_type`、`path/module/profile`、`match_reason`、`score`。
- [x] 定义歧义返回：必须明确需要用户或 Skill 补充哪个上下文，如 `profile_id`、`entity_type`、`module`、`domain`。
- [x] 定义低置信返回：允许给“候选线索”，但 `summary` 必须带不确定措辞，且 evidence 不得少于 1 条，除非为零结果。
- [x] 定义 `partial_ready` 返回：展示可用索引范围、缺失 source、最近失败 job、降级链路。
- [x] 定义 `blocked` 返回：路径越权、认证失败、远程安全配置不满足时必须 fail fast。

验收标准：

- 每个 `result_status` 都有 JSON 示例。
- 所有 V1 tools 的返回 schema 共享同一套 `warnings`、`evidence_refs`、`next_queries` 结构。
- 契约测试覆盖零结果、多结果、歧义、低置信、partial_ready、blocked。

### D0-04 查询契约附录：warning 分级

- 状态：`[x]`
- 优先级：`P0`
- 类型：`DOC`、`CONTRACT`
- 依赖：`D0-03`
- 目标：定义 warning 的等级、代码、可恢复性和 Skill 处理方式。
- 产出：架构文档附录 A.21 至 A.26 已定义 Warning 对象、level 语义、code 命名规则、集中 registry、Skill 处理规则、审计与契约测试要求。

TODO：

- [x] 定义 warning level：`info`、`caution`、`degraded`、`blocked`。
- [x] 定义 warning code 命名规则：`profile.unresolved`、`index.partial_ready`、`compile_db.missing`、`retrieval.zero_result`、`security.path_blocked` 等。
- [x] 定义 warning 字段：`level`、`code`、`message`、`details`、`actionable`、`suggested_action`、`affected_sources`、`evidence_refs`。
- [x] 明确 `info`：假设说明，不影响可信度。
- [x] 明确 `caution`：上下文不足或候选较多，需要 disambiguation。
- [x] 明确 `degraded`：缺索引、缺 compile DB、embedding/rerank 不可用，结果可用但降级。
- [x] 明确 `blocked`：安全、权限、schema、路径、配置错误导致不能返回结果。
- [x] 定义 Skill 行为：`blocked` 不生成答案，`degraded` 必须在回答中说明，`caution` 优先补查或列候选。

验收标准：

- warning code 有集中 registry，避免各模块自由发明。
- 每个 warning code 都有单元测试或契约测试。
- audit log 中记录 warning code 和 level，但不记录大段源码。

### D0-05 存储一致性附录

- 状态：`[x]`
- 优先级：`P0`
- 类型：`DOC`、`CONTRACT`
- 依赖：架构文档第 7、15、18 节
- 目标：新增“附录 B：存储一致性”，明确 baseline 与 overlay 的合并、删除、替换、跨库关系、迁移和清理策略。
- 产出：架构文档已新增“附录 B：存储一致性”，并在第 7.5 节补充指向完整一致性契约。

TODO：

- [x] 定义 baseline 只读原则：普通 init/index/query 不写 baseline，只有 `baseline publish` 或 CI release 可写。
- [x] 定义 overlay 优先 join：同 ID 对象 overlay 覆盖 baseline，不同 ID 对象按 snapshot/profile/source scope 合并。
- [x] 定义 tombstone 表：`object_type`、`object_id`、`baseline_id`、`reason`、`created_by_job`、`created_at`。
- [x] 定义 replacement 表：`object_type`、`old_object_id`、`new_object_id`、`scope`、`reason`、`created_by_job`。
- [x] 定义 relation 跨库解析：relation endpoint 可指向 baseline 或 overlay entity，查询时通过 logical entity view 解析，避免 dangling edge。
- [x] 定义 FTS 合并规则：baseline FTS + overlay FTS - tombstone，排序时标记 `source_index`。
- [x] 定义 vector 合并规则：baseline LanceDB + local delta，replacement/tombstone 必须同步影响候选过滤。
- [x] 定义 migration 原则：幂等、先备份、失败不破坏可用旧索引、大版本需人工确认。
- [x] 定义清理策略：cache/tmp 可直接清；old snapshots 按保留策略清；overlay tombstone/replacement 需 compact 后清；baseline 不由普通 clean 删除。
- [x] 定义一致性检查：孤儿 relation、dangling evidence、FTS/chunk 不一致、vector ref 缺失、manifest/schema 不匹配。

验收标准：

- 附录中给出 logical view 的伪 SQL 或接口语义。
- 增量索引删除/替换 baseline 对象时，有明确行为和测试用例。
- `validate` 可以检查 baseline/overlay/FTS/vector 的一致性。

### D0-06 Profile 规范附录

- 状态：`[x]`
- 优先级：`P0`
- 类型：`DOC`、`CONTRACT`
- 依赖：架构文档第 6、7、8、11 节
- 目标：新增“附录 C：Profile 规范”，明确 `auto` 选择算法、多 profile 展示规则、profile 变更触发的重算边界。
- 输出：已在架构文档新增“附录 C：Profile 规范”，并在 11.5 Profile-aware 查询处补充引用。

TODO：

- [x] 定义 profile 主键：`snapshot_id + profile_id + defconfig_hash + dotconfig_hash`。
- [x] 定义 `default_profile=auto` 选择顺序：显式参数 > CLI/env > local config > 当前 `.config` 可信候选 > baseline 默认 profile > unresolved。
- [x] 定义 `.config` 可信候选判定：文件存在、hash 可读、可解析 app/board、mtime 新于最近 profile scan 或 manifest 一致。
- [x] 定义多 `.config` 场景：返回候选列表，不静默随机选择；可按 `profile.priority` 或 `last_used` 排序。
- [x] 定义 multi-profile 查询展示：必须显示每个 profile 的 enabled/disabled/unknown、差异宏、受影响模块和证据。
- [x] 定义 profile 变更的增量重算边界：只变 `.config` 时重算 profile、macro summary、profile-conditioned relations、可达性投影；不重算无关 doc chunks。
- [x] 定义触发全量重算边界：parser/extractor 版本变化、build module 规则变化、compile DB 版本变化、baseline snapshot 变化、embedding model 变化。
- [x] 定义 unresolved profile 行为：返回 `profile.unresolved` warning，允许非 profile-sensitive 查询继续。

验收标准：

- `profile auto` 有确定性测试。
- 两个以上 profile 候选时，返回结构稳定且可被 Skill 展示。
- profile hash 变更导致哪些索引表需要重算有清单。

### D0-07 合并评测与补充需求为可执行验收门槛

- 状态：`[x]`
- 优先级：`P0`
- 类型：`DOC`、`CONTRACT`、`TEST`
- 依赖：架构文档第 14、19 节
- 目标：将现有“评测与验收方案”和“推荐补充需求”合并为 release gate，而不是停留在建议列表。
- 输出：已将架构文档第 14 节改为“验收门槛与回归策略”，并将原第 19 节补充需求合并为 gate 映射。

TODO：

- [x] 在架构文档中将第 14 节和第 19 节合并为“验收门槛与回归策略”。
- [x] 定义质量阈值：Evidence Hit Rate、Top-k Recall、MRR、Profile Correctness、Warning Quality、schema compliance。
- [x] 定义性能阈值：init、server startup、docs_search、code_resolve、kb_search、evidence_bundle、incremental index 的 P50/P95。
- [x] 定义稳定性阈值：长时间运行、并发只读查询、索引中断恢复、migration 幂等、partial_ready 可用性。
- [x] 定义失败回归门槛：质量指标不得明显倒退，安全和契约测试必须 100% 通过，新增 bug 必须加入回归样本。
- [x] 将第 19 节补充需求逐项映射到 gate：可重复索引、可复用 baseline、可解释检索、查询审计、评测闭环、离线 embedding、存储迁移、数据清理、锁机制、front matter、证据最小化。

验收标准：

- release gate 有可运行命令，例如 `active-kb eval run --gate v1`。
- 每个 gate 失败时有明确阻断级别：blocker、warning、advisory。
- CI 或本地 release 流程能引用同一份门槛。

### D0-08 远程安全 fail-safe 与两套配置样式

- 状态：`[x]`
- 优先级：`P0`
- 类型：`DOC`、`CONTRACT`、`SEC`
- 依赖：架构文档第 6、13、18 节
- 目标：把远程安全从“建议启用”改为“默认 fail-safe”，并明确拆出本地单机和远程共享两套配置。
- 输出：已在架构文档定义 `deployment_mode`、远程安全 fail-safe、token/OIDC 抽象和运维校验，并新增两份示例配置。

TODO：

- [x] 定义 `deployment_mode`：`local_single_user`、`remote_shared`。
- [x] 本地单机默认：`stdio`；若启用 HTTP，只允许 `127.0.0.1` 或 `localhost`；ops tools 默认可通过本机配置显式打开；audit 默认开启。
- [x] 远程共享默认：必须 `require_auth=true`；必须配置 `allowed_origins` 且禁止通配；必须启用 audit；ops tools 默认不暴露；host 允许非 loopback 但必须通过安全校验。
- [x] 定义 fail-safe 启动校验：当 host 是 `0.0.0.0` 或非 loopback 且未满足认证/Origin/audit 条件时，server 拒绝启动。
- [x] 定义远程 token/OIDC 适配抽象：V1 可先 token，V2 接 OIDC。
- [x] 给出两份完整 YAML 示例：`examples/local-single-user.yaml`、`examples/remote-shared.yaml`。

本地单机配置样式：

```yaml
deployment_mode: local_single_user
server:
  transport: stdio
  expose_ops_tools: false
  http:
    host: 127.0.0.1
    port: 8765
    require_auth: false
    allowed_origins:
      - http://127.0.0.1
      - http://localhost
security:
  audit:
    enabled: true
```

远程共享配置样式：

```yaml
deployment_mode: remote_shared
server:
  transport: streamable-http
  expose_ops_tools: false
  http:
    host: 0.0.0.0
    port: 8765
    require_auth: true
    auth_provider: token
    allowed_origins:
      - https://chatgpt.com
      - https://your-team-gateway.example.com
    trust_reverse_proxy: true
security:
  audit:
    enabled: true
  secret_scan:
    enabled: true
```

验收标准：

- 非 loopback HTTP 缺认证时启动失败。
- `allowed_origins: ["*"]` 在 `remote_shared` 下启动失败。
- remote_shared 下 ops tools 默认不可见。
- 所有安全启动失败返回 `blocked` 级别错误，并写入 audit/security log。

### D0-09 架构文档变更追踪矩阵

- 状态：`[x]`
- 优先级：`P1`
- 类型：`DOC`
- 依赖：`D0-01` 至 `D0-08`
- 目标：确保评审意见不会只存在于 TODO，而是能回写到架构文档和实现任务。
- 输出：已建立 [Active Knowledge Server 评审变更追踪矩阵](./active_knowledge_server_review_trace.md)，并在架构文档末尾增加“评审变更追踪”入口。

TODO：

- [x] 在架构文档末尾添加“评审变更记录”或单独建立 `doc/active_knowledge_server_review_trace.md`。
- [x] 每条评审意见记录：原意见、设计改动位置、实现任务 ID、测试任务 ID、验收 gate。
- [x] TODO 文档与架构文档互相链接。

验收标准：

- 评审人能从任意一条评审意见追到设计、实现、测试和验收。

---

## 6. Phase 1：工程骨架、配置与安全底座

### C1-01 Python 包与目录骨架

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`
- 依赖：`D0-01` 至 `D0-08`
- 输出：已创建 `active-knowledge-server` Python package、`src/active_knowledge_server/` 分层目录、测试目录、开发工具配置和 `active-kb` entrypoint。

TODO：

- [x] 创建 `active-knowledge-server/pyproject.toml`。
- [x] 创建 `src/active_knowledge_server/` 包目录。
- [x] 创建模块：`config`、`connectors`、`parsers`、`indexing`、`storage`、`query`、`mcp`、`models`、`security`、`eval`。
- [x] 接入测试框架、类型检查、格式化工具。
- [x] 建立 `tests/unit`、`tests/integration`、`tests/contracts`、`tests/fixtures`。

验收标准：

- `uv run pytest` 可运行空测试。
- 包可导入，CLI entrypoint 可打印版本。

### C1-02 CLI 骨架

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`
- 依赖：`C1-01`
- 输出：已实现 `active-kb` 子命令骨架、配置解析与优先级合并、机器可读 JSON 输出和单元测试；`serve` 与 `index` 在后续 Phase 接入真实 FastMCP / indexing pipeline 前返回可验证的执行计划。

TODO：

- [x] 实现 `active-kb init`。
- [x] 实现 `active-kb serve`。
- [x] 实现 `active-kb index`。
- [x] 实现 `active-kb status`。
- [x] 实现 `active-kb validate`。
- [x] CLI 参数优先级遵守：CLI > env > local_config > baseline_config > defaults。

验收标准：

- [x] 所有命令有 `--help`。
- [x] 参数解析和配置合并有单元测试。

### C1-03 Config schema 与配置合并

- 状态：`[x]`
- 优先级：`P1`
- 类型：`CONTRACT`、`IMPL`、`TEST`
- 依赖：`D0-08`、`C1-01`
- 输出：已定义 Pydantic 配置模型、合并后变量展开、schema version、示例配置校验、可操作错误格式和脱敏/路径缩短的配置摘要。

TODO：

- [x] 定义 Pydantic `RuntimeConfig`、`ProjectConfig`、`StorageConfig`、`IndexingConfig`、`QueryConfig`、`SecurityConfig`。
- [x] 支持 `${runtime.workdir}` 这类变量展开。
- [x] 支持 baseline config 和 local config 合并。
- [x] 记录 config schema version。
- [x] 输出配置摘要时脱敏 token、绝对路径按策略缩短。

验收标准：

- [x] 示例配置可通过校验。
- [x] 缺必填字段有可操作错误信息。
- [x] config dump 不泄漏密钥。

### C1-04 Workdir 初始化

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`OPS`
- 依赖：`C1-03`
- 输出：已将 workdir 初始化抽成可复用服务，支持幂等创建 baseline/local 目录、初始化本地配置、检查 baseline manifest、检测误追踪 local runtime 文件，并在不可写时 fail fast。

TODO：

- [x] 创建 `.active-kb/baseline`、`.active-kb/local` 目录结构。
- [x] 初始化 `local/config/active-kb.local.yaml`。
- [x] 初始化 `local/db`、`local/vectors`、`cache`、`logs`、`tmp`、`locks`。
- [x] 检查 baseline manifest 是否存在和可读。
- [x] 检查 `.active-kb/local` 是否被误加入版本管理提示。

验收标准：

- [x] 重复执行 init 幂等。
- [x] workdir 不可写时 fail fast。

### C1-05 Fail-safe 安全配置校验

- 状态：`[x]`
- 优先级：`P0`
- 类型：`SEC`、`CONTRACT`、`IMPL`、`TEST`
- 依赖：`D0-08`、`C1-03`
- 输出：已实现合并配置后的 fail-safe 启动校验，覆盖本地/远程部署模式、loopback 绑定、认证、Origin、audit、ops tool 暴露和结构化 blocked 返回。

TODO：

- [x] 实现 `deployment_mode` 校验。
- [x] 非 loopback HTTP 缺认证时拒绝启动。
- [x] remote_shared 下通配 Origin 拒绝启动。
- [x] remote_shared 下 audit disabled 拒绝启动。
- [x] remote_shared 下 ops tools 默认禁用。
- [x] 本地 HTTP 默认只允许 loopback。

验收标准：

- [x] 安全配置契约测试 100% 通过。
- [x] 所有失败场景返回结构化 `blocked` 错误。

### C1-06 Path Guard

- 状态：`[x]`
- 优先级：`P1`
- 类型：`SEC`、`IMPL`、`TEST`
- 依赖：`C1-03`
- 输出：已实现配置驱动的 Path Guard，覆盖 allowlist 规范化校验、`..` 逃逸阻断、symlink 真实路径校验、显式 symlink escape 例外和 root-relative 展示路径。

TODO：

- [x] 规范化路径并校验 allowlist。
- [x] 阻止 `..` 逃逸。
- [x] 阻止 symlink 跳出 allowlist，除非显式允许。
- [x] 返回相对路径展示策略。
- [x] 为 source docs、workspace、workdir 分别建立测试 fixture。

验收标准：

- [x] 越权读取被阻断并产生 `security.path_blocked`。
- [x] 合法路径跨平台可通过。

### C1-07 审计与日志骨架

- 状态：`[x]`
- 优先级：`P1`
- 类型：`SEC`、`OPS`、`IMPL`
- 依赖：`C1-03`
- 输出：已建立分通道日志与 JSONL audit 骨架，支持 tool call / ops 操作审计、query hash + 安全短预览、warning 摘要、敏感字段/长文本脱敏和滚动日志配置。

TODO：

- [x] 建立 `server.log`、`indexer.log`、`audit.log`、`eval.log`。
- [x] audit 记录：tool、query hash 或短 query、profile、调用方、耗时、结果数量、warning code、ops 操作。
- [x] 不记录大段源码正文和密钥。
- [x] 支持日志轮转配置。

验收标准：

- [x] 每次 tool call 都有 audit 记录。
- [x] 敏感字段脱敏测试通过。

---

## 7. Phase 2：存储层与 baseline/overlay 一致性

### S2-01 Storage adapter 接口

- 状态：`[x]`
- 优先级：`P1`
- 类型：`CONTRACT`、`IMPL`
- 依赖：`D0-05`
- 输出：已定义 `storage/base.py` 稳定契约，覆盖 scope、physical record、logical view、replacement/tombstone 解析、reader/writer/adapter protocol，以及 baseline/overlay 显式写入请求和 baseline publish 写保护。

TODO：

- [x] 定义 `storage/base.py`，包括 source、snapshot、profile、file、chunk、entity、relation、evidence、job 的读写接口。
- [x] 查询层只依赖接口，不直接写 SQL。
- [x] 写接口区分 baseline target 和 overlay target。
- [x] 普通运行默认禁止 baseline 写入。

验收标准：

- [x] query 模块没有 SQLite/LanceDB 直接导入。
- [x] baseline 写入必须经过显式 publish/build target。

### S2-02 SQLite schema 与 migration

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`OPS`、`TEST`
- 依赖：`S2-01`
- 输出：已建立 `baseline_metadata`、`overlay_metadata`、`jobs` 三类 SQLite schema 与 migration 骨架，覆盖 `schema_version` / `migration_history`、幂等迁移、dry-run、existing db 备份、major baseline confirm gate，以及 init 期间本地 overlay/jobs 自动迁移。

TODO：

- [x] 建立 `metadata.db` schema。
- [x] 建立 `overlay.db` schema。
- [x] 建立 `jobs.db` schema。
- [x] 建立 `schema_version` 表。
- [x] migration 幂等执行，支持 dry-run。
- [x] 大版本 migration 前自动备份 local db。

验收标准：

- [x] migration 连续执行 3 次结果一致。
- [x] migration 失败不破坏旧 db。

### S2-03 FTS5 索引

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`S2-02`
- 输出：已建立 baseline/overlay SQLite FTS5 表与 merged logical view 检索实现，覆盖 chunk/entity/doc/code 四类索引、写入同步、tombstone/replacement 过滤、domain/doc_type/profile/source_index 过滤，以及 baseline+overlay 去重单测。

TODO：

- [x] 建立 `chunk_fts`、`entity_fts`、`doc_fts`、`code_fts`。
- [x] 写入 chunk/entity 时同步 FTS。
- [x] 删除或 replacement 时同步 FTS logical view。
- [x] 支持按 domain、doc_type、profile、source_index 过滤。

验收标准：

- [x] FTS 与 chunk/entity 一致性检查通过。
- [x] baseline + overlay 查询结果去重正确。

### S2-04 LanceDB baseline/delta adapter

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`S2-01`

TODO：

- [x] 建立 baseline vector collection 只读访问。
- [x] 建立 local delta vector collection 写入。
- [x] vector ref 写回 chunk。
- [x] 查询时合并 baseline 和 delta 候选。
- [x] tombstone/replacement 后过滤过期向量。

验收标准：

- embedding model version 不匹配时返回 `embedding.version_mismatch` warning。
- delta 删除不影响 baseline 文件。

### S2-05 Tombstone 与 replacement

- 状态：`[x]`
- 优先级：`P0`
- 类型：`CONTRACT`、`IMPL`、`TEST`
- 依赖：`D0-05`、`S2-02`

TODO：

- [x] 实现 tombstone 表和 API。
- [x] 实现 replacement 表和 API。
- [x] 本地删除 baseline 文件或 chunk 时写 tombstone。
- [x] 本地修改 baseline 对象时写 replacement。
- [x] 查询 logical view 中屏蔽 tombstone、优先 replacement。

验收标准：

- [x] 删除 baseline 中存在的文件后，查询不再返回旧 evidence。
- [x] 修改 baseline 中存在的 symbol 后，查询返回 overlay 新对象并标记 `source_index=overlay` 或 `merged`。

### S2-06 跨库 relation 解析

- 状态：`[x]`
- 优先级：`P0`
- 类型：`CONTRACT`、`IMPL`、`TEST`
- 依赖：`S2-05`

TODO：

- [x] relation endpoint 支持 baseline entity 和 overlay entity。
- [x] 查询时解析 logical entity ID。
- [x] replacement 后关系自动指向新对象或返回降级 warning。
- [x] tombstone 后 dangling relation 被过滤或标记。

验收标准：

- [x] `validate` 可发现 orphan relation。
- [x] graph traversal 不返回被 tombstone 屏蔽的节点。

### S2-07 Job 状态与锁

- 状态：`[x]`
- 优先级：`P1`
- 类型：`OPS`、`IMPL`、`TEST`
- 依赖：`S2-02`

TODO：

- [x] 实现 job 状态机：pending、discovering、parsing、extracting、embedding、reporting、ready、failed、partial_ready。
- [x] 实现 SQLite job lock 或 lock file。
- [x] 防止多个 index job 同时写 overlay。
- [x] 支持 job resume 和 retry。

验收标准：

- [x] 并发启动两个 index job 时只有一个获得写锁。
- [x] 单文件解析失败可进入 partial_ready。

### S2-08 存储一致性 validate

- 状态：`[x]`
- 优先级：`P1`
- 类型：`OPS`、`TEST`
- 依赖：`S2-03`、`S2-04`、`S2-06`

TODO：

- [x] 检查 manifest/schema/parser/embedding version。
- [x] 检查 baseline/overlay FTS 与 metadata 一致。
- [x] 检查 vector ref 是否存在。
- [x] 检查 evidence 是否能回到文件。
- [x] 检查 relation endpoint 是否存在。
- [x] 输出 machine-readable report。

验收标准：

- [x] `active-kb validate --format json` 可被 CI 消费。

### S2-09 Clean 与 compact 策略

- 状态：`[x]`
- 优先级：`P2`
- 类型：`OPS`、`IMPL`、`TEST`
- 依赖：`D0-05`、`S2-08`

TODO：

- [x] 实现 `clean --cache`。
- [x] 实现 `clean --tmp`。
- [x] 实现 `clean --old-jobs --keep N`。
- [x] 实现 `clean --old-snapshots --keep N`。
- [x] 实现 overlay compact：合并过期 tombstone/replacement，重建 logical view 相关索引。
- [x] 禁止普通 clean 删除 baseline。

验收标准：

- [x] 清理不会破坏 baseline。
- [x] compact 前后查询结果一致。

---

## 8. Phase 3：Source discovery 与解析底座

### X3-01 Workspace connector

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`C1-06`、`S2-01`

TODO：

- [x] 扫描 Active workspace 顶层 area。
- [x] 识别 repo/submodule 边界和 commit map。
- [x] 生成 file inventory。
- [x] 应用 include/exclude 规则。
- [x] 所有读取经过 path guard。

验收标准：

- 可在没有 compile DB 的 workspace 上生成稳定 inventory。

### X3-02 Source docs connector

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`C1-06`

TODO：

- [x] 扫描 `knowledge-sources/`。
- [x] 支持 api、widgets、engineering、product、design、project、qa、release、learned-seeds。
- [x] 生成 source doc manifest。
- [x] 计算 source docs hash。
- [x] source docs 不存在时创建空目录并 warning。

验收标准：

- source docs hash 可写入 baseline manifest。

### X3-03 Build outputs connector

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`C1-06`

TODO：

- [x] 发现 `configs/**/*defconfig`。
- [x] 发现 `build/.config`、`build/out_hub/.config`。
- [x] 发现 compile DB candidates。
- [x] 记录缺失 compile DB warning。
- [x] 生成 build artifact manifest。

验收标准：

- 缺 compile DB 不阻断 V1 索引。

### X3-04 Markdown/HTML/front matter parser

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`X3-02`

TODO：

- [x] 解析 Markdown heading tree。
- [x] 解析 YAML front matter。
- [x] 支持 API 文档字段：module、version、code_symbols、profiles、authority_level。
- [x] 支持 widget 文档字段：widget、ui_framework、code_paths、tags、authority_level。
- [x] 支持 product/project/design 扩展字段预留。
- [x] HTML 先支持标题、正文、表格基础抽取。

验收标准：

- 每个文档 chunk 都能回到文件和行号。

### X3-05 Kconfig/Config.in/defconfig/.config parser

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`X3-03`

TODO：

- [x] 解析 defconfig 宏。
- [x] 解析 `.config` 宏。
- [x] 抽取 app、board、feature 线索。
- [x] 解析 Config.in/Kconfig symbol、depends、select。
- [x] 生成 profile macro summary。

验收标准：

- 同一 profile 多次解析 hash 稳定。

### X3-06 Makefile/module.mk parser

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`X3-01`

TODO：

- [x] 识别 build module。
- [x] 抽取模块名、源文件、条件宏。
- [x] 关联目录、文件、Config.in。
- [x] 输出 Module entity 和 relations。

验收标准：

- module -> file -> config evidence 可追溯。

### X3-07 C/C++/H 基础 parser

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`X3-01`

TODO：

- [x] V1 接入 ctags 或 Tree-sitter。
- [x] 抽取函数、宏、类型、include、注释、文件头。
- [x] 标记 extractor 和 confidence。
- [x] 不承诺高置信跨 translation unit ref。
- [x] 缺 compile DB 时产生 `compile_db.missing` warning。

验收标准：

- 函数/宏/类型基本定位可被 `code_resolve` 使用。

### X3-08 Secret scan

- 状态：`[x]`
- 优先级：`P1`
- 类型：`SEC`、`IMPL`、`TEST`
- 依赖：`C1-07`

TODO：

- [x] 在索引前扫描密钥、私钥、token、密码、证书。
- [x] 命中后默认跳过 embedding。
- [x] evidence excerpt 脱敏。
- [x] index report 记录文件和原因，不记录敏感原文。

验收标准：

- 密钥 fixture 不进入向量库和 evidence excerpt。

---

## 9. Phase 4：索引流水线、profile 与 workspace map

### P4-01 Snapshot collector

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`X3-01`、`S2-02`

TODO：

- [x] 生成 snapshot 记录。
- [x] 记录 baseline branch、git head、repo manifest hash、created_at、status。
- [x] 支持 current snapshot。
- [x] snapshot ID 可复现。

验收标准：

- 同一 workspace 未变化时 snapshot manifest 稳定。

### P4-02 Profile collector 与 auto 选择

- 状态：`[x]`
- 优先级：`P0`
- 类型：`CONTRACT`、`IMPL`、`TEST`
- 依赖：`D0-06`、`X3-03`、`X3-05`

TODO：

- [x] 实现 profile discovery。
- [x] 实现 `default_profile=auto` 选择算法。
- [x] 支持多 `.config` 候选排序。
- [x] profile unresolved 时不阻断非 profile-sensitive 查询。
- [x] 写入 profile manifest。

验收标准：

- auto 选择确定性测试通过。
- 多候选返回 `profile.multiple_candidates` warning。

### P4-03 文档索引流水线

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`X3-04`、`S2-03`、`S2-04`

TODO：

- [x] 将 Markdown/HTML parse result 转成 doc chunks。
- [x] 抽取 API item、widget item。
- [x] 写入 source、file、chunk、entity、evidence、FTS。
- [x] 对选中 chunk 构建 embedding。
- [x] 记录 doc_type、domain、version、authority_level、freshness。

验收标准：

- `knowledge-sources/api` 和 `knowledge-sources/widgets` 可被索引和搜索。

### P4-04 代码结构索引流水线

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`X3-06`、`X3-07`、`S2-03`

TODO：

- [x] 写入 Directory、Module、File、Symbol entities。
- [x] 写入 contains、defines、belongs_to_module、guarded_by_macro relations。
- [x] 为 function/macro/type/file header 生成 code chunks。
- [x] 关系记录 confidence 和 extractor。

验收标准：

- 可通过 FTS + symbol index 定位函数、宏、文件、目录。

### P4-05 Profile-conditioned relations

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`P4-02`、`P4-04`

TODO：

- [x] 将 defconfig/.config 宏应用到 module/file/symbol 可达性。
- [x] 写入 `profile_id`、`condition_expr`、`confidence`。
- [x] profile hash 变化时只重算 profile-conditioned relations 和投影视图。
- [x] 多 profile 查询时输出 enabled/disabled/unknown。

验收标准：

- 同一宏在不同 profile 下影响范围可查询。

### P4-06 增量索引

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`S2-05`、`S2-07`、`P4-03`、`P4-04`

TODO：

- [x] 文件 hash 变化时重建该文件 chunks/entities/relations/evidence/FTS。
- [x] `.config` 变化时重算 profile 和 profile-conditioned relations。
- [x] parser/extractor version 变化时重建对应产物。
- [x] embedding model 变化时重建向量。
- [x] 删除 baseline 对象时写 tombstone。
- [x] 修改 baseline 对象时写 replacement。

验收标准：

- 修改 1 个文件只影响相关对象，不触发无关 doc 全量重建。
- 增量失败可进入 partial_ready 并返回 warning。

### P4-07 Workspace map 与视角投影

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`P4-04`、`P4-05`
- 输出：已新增 `indexing/workspace_map.py`，生成 workspace tree、`workspace/layer/domain/feature/profile` 五类投影，并在增量索引后写出 `local/artifacts/workspace-maps/current.json`。

TODO：

- [x] 生成 workspace tree。
- [x] 生成 layer/domain/feature/profile 初版投影。
- [x] 使用 Active path mapping：packages/services、packages/apps、ui、uiframework、framework/engine 等。
- [x] 输出 artifacts/workspace-maps。

验收标准：

- [x] 已生成可被 `workspace_view(view=workspace|layer|domain|feature|profile)` 直接消费的基础可用结果。

---

## 10. Phase 5：查询服务与 RAG 契约实现

### Q5-01 Query models 与 response schema

- 状态：`[x]`
- 优先级：`P0`
- 类型：`CONTRACT`、`IMPL`、`TEST`
- 依赖：`D0-03`、`D0-04`
- 输出：已新增 `models/query.py`、`models/evidence.py`、`models/responses.py` 中的统一查询契约模型，并让现有 blocked 响应复用同一 `QueryResult` 外壳；同时补充 `tests/contracts/test_query_model_schemas.py` 与 schema snapshot 基线，以及 `tests/unit/test_query_models.py` 的模型校验单测。

TODO：

- [x] 实现 `QueryRequest`、`QueryResult`、`EvidenceRef`、`Warning`、`Candidate`。
- [x] 所有 tools 共享 `schema_version`、`result_status`、`confidence`、`warnings`、`evidence_refs`、`next_queries`。
- [x] 实现 JSON schema snapshot tests。

验收标准：

- [x] schema 变更会触发契约测试更新。

### Q5-02 Query Router

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`D0-01`、`D0-02`、`Q5-01`
- 输出：已新增 `models/routing.py` 中的稳定路由决策模型，并在 `query/router.py` 实现规则型 Query Router，覆盖 query normalization、intent signals、view/granularity 选择、retriever 权重、profile auto 解析、tool plan 与 route trace；同时补充 `tests/unit/test_query_router.py` 与 `tests/fixtures/query_intents.yaml` 的 fixture 驱动正反例回归。

TODO：

- [x] 实现 query normalization。
- [x] 实现 intent classifier。
- [x] 选择 view、granularity、retriever 权重。
- [x] 执行 profile resolution。
- [x] 输出 matched signals 和 route trace。

验收标准：

- [x] intent 分类正反例测试通过。

### Q5-03 SymbolRetriever

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`P4-04`、`Q5-01`

TODO：

- [ ] 支持函数、宏、类型、文件、模块精确查找。
- [ ] 支持 fuzzy/alias/doc mention 标记。
- [ ] 多候选返回 disambiguation。
- [ ] profile 条件过滤。

验收标准：

- 符号定位 Top-3 Recall 达到 V1 gate。

### Q5-04 FullTextRetriever

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`S2-03`、`Q5-01`

TODO：

- [ ] 查询 chunk_fts/entity_fts/doc_fts/code_fts。
- [ ] 支持 domain、doc_type、module、profile、source_index filter。
- [ ] 输出 match reason 和 score。

验收标准：

- API、控件、路径、错误码类查询有稳定召回。

### Q5-05 VectorRetriever

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`S2-04`、`P4-03`

TODO：

- [ ] 查询 baseline vectors 和 delta vectors。
- [ ] 根据 tombstone/replacement 过滤失效结果。
- [ ] embedding 不可用时降级为 FTS，并返回 warning。
- [ ] 支持离线 embedding provider 配置。

验收标准：

- embedding disabled 时查询仍可用。

### Q5-06 GraphRetriever 与 graph expansion

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`S2-06`、`P4-07`

TODO：

- [ ] 支持 contains、defines、calls、guarded_by_macro、belongs_to_layer、implements_feature 等关系扩展。
- [ ] 支持 max depth。
- [ ] 支持 profile filter。
- [ ] 过滤 tombstone 和 dangling relation。

验收标准：

- module、feature、profile 视角查询能返回局部关系图。

### Q5-07 Fusion、rerank 与上下文组装

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`Q5-03` 至 `Q5-06`

TODO：

- [ ] 实现 RRF 或加权归一化融合。
- [ ] intent-specific 权重：代码精确问题提高 symbol/FTS，文档问题提高 FTS/vector，feature 问题提高 graph。
- [ ] 证据去重：同文件、同 symbol、同 doc section 去重。
- [ ] authority、profile_match、freshness、graph_proximity 加权。
- [ ] 输出 retrieval trace，支持可解释检索。

验收标准：

- 每条 evidence 有召回来源和分数。

### Q5-08 Profile-aware query 行为

- 状态：`[ ]`
- 优先级：`P0`
- 类型：`CONTRACT`、`IMPL`、`TEST`
- 依赖：`D0-06`、`P4-02`、`P4-05`

TODO：

- [ ] 所有代码和关系查询接受 `profile_id`。
- [ ] 未指定 profile 时执行 auto resolution。
- [ ] unresolved 时返回候选 profile 列表和 warning。
- [ ] multi-profile 查询返回 profile matrix。
- [ ] `config_impact(compare_to=...)` 输出差异宏和影响范围。

验收标准：

- profile correct gate 达标。

### Q5-09 零结果/多结果/歧义/低置信契约测试

- 状态：`[ ]`
- 优先级：`P0`
- 类型：`TEST`
- 依赖：`D0-03`、`Q5-01` 至 `Q5-08`

TODO：

- [ ] 构造 zero result fixture。
- [ ] 构造 multi result fixture。
- [ ] 构造 ambiguous fixture。
- [ ] 构造 low confidence fixture。
- [ ] 构造 partial_ready fixture。
- [ ] 构造 blocked fixture。

验收标准：

- 所有 V1 tools 均通过异常状态契约测试。

### Q5-10 Evidence Packager

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`Q5-07`

TODO：

- [ ] 加载 evidence excerpt。
- [ ] 限制 excerpt 长度。
- [ ] 返回相对路径、行号、hash、authority_level。
- [ ] 支持 evidence bundle by entity/query。
- [ ] 敏感内容脱敏。

验收标准：

- MCP 默认不返回大段源码全文。
- 每个关键结果至少有 evidence 或明确 warning。

---

## 11. Phase 6：MCP 与 Skill 接口

### M6-01 FastMCP app

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`Q5-01`

TODO：

- [ ] 初始化 FastMCP server。
- [ ] 注册 tools/resources。
- [ ] 支持 stdio。
- [ ] 支持 streamable-http。
- [ ] 工具返回 Pydantic 结构化对象。

验收标准：

- 本地 MCP client 可调用基础 tools。

### M6-02 V1 查询 tools

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`Q5-02` 至 `Q5-10`

TODO：

- [ ] `kb_search`
- [ ] `docs_search`
- [ ] `code_resolve`
- [ ] `code_context`
- [ ] `code_trace` 初版可先返回 unsupported 或基础 graph trace，但 schema 稳定。
- [ ] `config_impact`
- [ ] `workspace_view`
- [ ] `evidence_bundle`

验收标准：

- 所有 tools 有 readOnly/destructive/idempotent annotation。
- 所有 tools 返回统一 `QueryResult` 或对应稳定 response。

### M6-03 Resources

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`P4-07`、`Q5-10`

TODO：

- [ ] `active://config/current`
- [ ] `active://snapshot/current`
- [ ] `active://profile/{profile_id}`
- [ ] `active://workspace/current/summary`
- [ ] `active://workspace/current/tree`
- [ ] `active://entity/{entity_id}`
- [ ] `active://evidence/{evidence_id}`
- [ ] `active://index/status`

验收标准：

- resources 只读，不触发索引或写状态。

### M6-04 Ops tools gating

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`SEC`、`IMPL`、`TEST`
- 依赖：`C1-05`、`S2-07`

TODO：

- [ ] `ops_get_config`
- [ ] `ops_validate_setup`
- [ ] `ops_index_status`
- [ ] `ops_start_index`
- [ ] `ops_cancel_index`
- [ ] `ops_list_profiles`
- [ ] `ops_list_sources`
- [ ] 默认 `server.expose_ops_tools=false`。
- [ ] remote_shared 下即使配置误开，也需二次安全校验。

验收标准：

- remote_shared 默认 tools list 不包含 ops tools。

### M6-05 HTTP 安全中间件

- 状态：`[ ]`
- 优先级：`P0`
- 类型：`SEC`、`IMPL`、`TEST`
- 依赖：`D0-08`、`C1-05`

TODO：

- [ ] Origin 校验。
- [ ] token auth V1。
- [ ] audit 每次 HTTP tool call。
- [ ] remote_shared 拒绝无认证请求。
- [ ] 本地 loopback 允许无认证但仍 audit。

验收标准：

- HTTP 安全测试 100% 通过。

### M6-06 Skill 路由说明与示例

- 状态：`[ ]`
- 优先级：`P2`
- 类型：`DOC`、`TEST`
- 依赖：`D0-02`、`M6-02`

TODO：

- [ ] 输出 Skill 路由矩阵。
- [ ] 输出 10 个典型问题的 tool call 示例。
- [ ] 输出低置信/歧义时的 Skill 处理示例。
- [ ] 明确 Skill 不依赖 ops tools 和内部存储。

验收标准：

- 示例可以作为 MCP contract smoke test。

---

## 12. Phase 7：评测、验收门槛与失败回归

### E7-01 V1 eval cases

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`TEST`
- 依赖：`D0-07`

TODO：

- [ ] 建立 `eval/cases.yaml`。
- [ ] 至少 10 个符号定位问题。
- [ ] 至少 10 个 API 文档查证问题。
- [ ] 至少 10 个控件使用问题。
- [ ] 至少 10 个 workspace 导航问题。
- [ ] 至少 10 个配置/profile 影响问题。
- [ ] 至少 10 个 feature/domain 跨层问题。
- [ ] 每个 case 标注目标 evidence、期望 intent、期望 warning、profile 条件。

验收标准：

- eval runner 可读取并执行 60 个 V1 cases。

### E7-02 质量阈值 gate

- 状态：`[ ]`
- 优先级：`P0`
- 类型：`TEST`、`CONTRACT`
- 依赖：`E7-01`、`Q5-10`

V1 最低门槛：

- [ ] Schema compliance：100%。
- [ ] Evidence Hit Rate：整体 `>= 0.85`。
- [ ] Top-5 Recall：整体 `>= 0.90`。
- [ ] 符号定位 Top-3 Recall：`>= 0.95`。
- [ ] MRR：整体 `>= 0.75`。
- [ ] Profile Correctness：`>= 0.90`。
- [ ] Warning Quality：`>= 0.85`。
- [ ] blocked/security contract：100%。

验收标准：

- `active-kb eval run --gate quality` 低于 blocker 阈值时失败退出。

### E7-03 性能阈值 gate

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`TEST`、`OPS`
- 依赖：`M6-02`

V1 初始门槛：

- [ ] `serve` 基于已存在 baseline 启动 P95 `<= 10s`。
- [ ] `init --reuse-baseline` P95 `<= 60s`。
- [ ] `docs_search` P95 `<= 2s`。
- [ ] `code_resolve` P95 `<= 1.5s`。
- [ ] `workspace_view` P95 `<= 2s`。
- [ ] `kb_search` P95 `<= 3s`。
- [ ] `evidence_bundle` P95 `<= 3s`。
- [ ] 100 个文件以内增量索引 P95 `<= 10min`。
- [ ] 本地 serve 常驻内存 P95 `<= 4GB`。

验收标准：

- 性能测试报告记录样本量、机器环境、数据规模。

### E7-04 稳定性阈值 gate

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`TEST`、`OPS`
- 依赖：`S2-07`、`M6-02`

TODO：

- [ ] 8 小时本地 serve soak test 无未处理异常。
- [ ] 500 次混合查询 success rate `>= 99%`，blocked/zero_result 不算异常。
- [ ] 索引中断后可 resume 或明确 failed，不留下写锁。
- [ ] migration 连续执行 3 次幂等。
- [ ] partial_ready 下查询可用并返回 warning。
- [ ] 并发只读查询不阻塞。

验收标准：

- 稳定性报告进入 release artifact。

### E7-05 失败回归门槛

- 状态：`[ ]`
- 优先级：`P0`
- 类型：`TEST`、`OPS`
- 依赖：`E7-02` 至 `E7-04`

TODO：

- [ ] 质量指标整体不得比上一 baseline 下降超过 2 个百分点。
- [ ] 任一核心类别 Evidence Hit Rate 不得下降超过 5 个百分点。
- [ ] P95 latency 不得比上一 baseline 恶化超过 20%，除非有明确豁免。
- [ ] 安全、schema、blocked contract 必须 100% 通过，无豁免。
- [ ] 每个线上/评审发现的 bug 必须新增 regression case 后再修复。
- [ ] migration 失败类 bug 必须新增备份和恢复测试。

验收标准：

- `eval-baseline` 可保存并对比上一发布基线。

### E7-06 可重复索引 gate

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`TEST`
- 依赖：`P4-06`

TODO：

- [ ] 同一 snapshot/profile 连续两次索引结果 ID 稳定。
- [ ] chunk/entity/evidence ID 稳定。
- [ ] report 中允许时间字段不同，但核心内容 hash 相同。

验收标准：

- `active-kb eval run --gate reproducibility` 通过。

### E7-07 Release checklist

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`OPS`、`DOC`
- 依赖：`E7-01` 至 `E7-06`

TODO：

- [ ] baseline manifest 完整。
- [ ] source docs hash 已记录。
- [ ] schema/parser/extractor/embedding/MCP schema version 已记录。
- [ ] eval quality/performance/stability gate 通过。
- [ ] `.active-kb/local` 未打包进 release。
- [ ] remote_shared 配置示例通过安全校验。
- [ ] README 包含 init/index/serve/validate/clean/migrate。

验收标准：

- release 前 checklist 可机器检查的项尽量机器检查。

---

## 13. Phase 8：运维、部署与发布

### O8-01 Init/validate/status 端到端

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`OPS`、`IMPL`、`TEST`
- 依赖：`C1-02`、`S2-08`、`P4-02`

TODO：

- [ ] `init --workspace --reuse-baseline`。
- [ ] `validate --format text|json`。
- [ ] `status --format text|json`。
- [ ] 输出 baseline reuse status、profile status、index status、warnings。

验收标准：

- 新用户能按 README 在本地完成初始化。

### O8-02 Index/rebuild/baseline publish

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`OPS`、`IMPL`、`TEST`
- 依赖：`P4-06`、`E7-07`

TODO：

- [ ] `index --incremental --profile auto`。
- [ ] `index --full --target local`。
- [ ] `index --full --target baseline` 仅允许 publish/build mode。
- [ ] `rebuild --vectors`。
- [ ] `baseline validate`。
- [ ] `baseline publish`。

验收标准：

- 普通用户不能误写 baseline。

### O8-03 本地单机部署文档

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`DOC`、`SEC`
- 依赖：`D0-08`、`M6-01`

TODO：

- [ ] 提供 `examples/local-single-user.yaml`。
- [ ] README 写明 stdio 默认。
- [ ] 本地 HTTP 示例只绑定 `127.0.0.1`。
- [ ] 说明如何对接 IDE/Codex/本地 Agent。

验收标准：

- 示例配置可直接通过 `validate`。

### O8-04 远程共享部署文档

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`DOC`、`SEC`
- 依赖：`D0-08`、`M6-05`

TODO：

- [ ] 提供 `examples/remote-shared.yaml`。
- [ ] 说明认证、Origin、HTTPS/gateway、audit、ops tools 禁用。
- [ ] 给出 token rotation 建议。
- [ ] 给出反向代理 header 信任边界说明。
- [ ] 给出 ChatGPT 远程 MCP 接入注意事项。

验收标准：

- 不安全 remote config 在文档中明确标注会启动失败。

### O8-05 可观测性

- 状态：`[ ]`
- 优先级：`P2`
- 类型：`OPS`、`IMPL`
- 依赖：`C1-07`、`M6-02`

TODO：

- [ ] 指标：index_files_total、index_files_failed、index_duration_seconds。
- [ ] 指标：query_latency_seconds、retrieval_candidates_total、evidence_items_returned、warnings_total。
- [ ] 指标：embedding_queue_size、storage_size_bytes。
- [ ] 输出 health summary。

验收标准：

- `status` 能展示最近 query/index 健康状态。

### O8-06 用户反馈闭环

- 状态：`[ ]`
- 优先级：`P2`
- 类型：`OPS`、`TEST`
- 依赖：`Q5-07`、`E7-01`

TODO：

- [ ] 记录有用/无用 evidence 反馈。
- [ ] 记录未命中目标文件/symbol/doc section。
- [ ] 支持将反馈转成 eval case 草稿。
- [ ] learned-seeds 写入需要人工审核状态。

验收标准：

- 反馈不会直接污染权威知识。

---

## 14. Phase 9：V2+ 增强任务

这些任务不阻断 V1，但设计和接口应预留。

### V9-01 Runtime pattern extractor

- 状态：`[ ]`
- 优先级：`P3`
- 类型：`IMPL`、`TEST`

TODO：

- [ ] 抽取 task create wrapper。
- [ ] 抽取 queue send/receive。
- [ ] 抽取 semaphore/event/timer。
- [ ] 抽取 ISR/vector/fault pattern。
- [ ] 为 relation 标记 confidence 和 evidence。

### V9-02 Compile DB 接入

- 状态：`[ ]`
- 优先级：`P3`
- 类型：`IMPL`、`TEST`

TODO：

- [ ] 发现并校验 `compile_commands.json`。
- [ ] 记录 include path、macro、target、toolchain。
- [ ] 将 compile DB hash 写入 snapshot/profile manifest。
- [ ] compile DB 变化触发相关代码索引重建。

### V9-03 clang/clangd-compatible index

- 状态：`[ ]`
- 优先级：`P3`
- 类型：`IMPL`、`TEST`

TODO：

- [ ] 抽取高置信 definition/reference。
- [ ] 合并 ctags/Tree-sitter 低置信结果。
- [ ] 提升 profile 级可达性。
- [ ] 更新 call_trace 和 impact analysis。

### V9-04 多角色知识域扩展

- 状态：`[ ]`
- 优先级：`P3`
- 类型：`IMPL`、`CONTRACT`

TODO：

- [ ] product/design/project connectors。
- [ ] requirement-to-code/design trace。
- [ ] audience-aware query 参数。
- [ ] domain 权威源冲突报告。

### V9-05 服务化存储后端

- 状态：`[ ]`
- 优先级：`P3`
- 类型：`IMPL`、`OPS`

TODO：

- [ ] PostgreSQL/pgvector adapter。
- [ ] Qdrant adapter。
- [ ] 多用户权限模型。
- [ ] 后台任务调度。

---

## 15. 建议实施顺序

推荐以 8 个可交付 sprint 推进，每个 sprint 都保留可演示结果。

| Sprint | 范围 | 结束时可演示能力 |
| --- | --- | --- |
| Sprint 0 | Phase 0 | 架构契约补齐，评审项全部有设计位置、任务位置和验收位置 |
| Sprint 1 | Phase 1 + Phase 2 schema | CLI/config/path guard/security/migration 骨架可运行 |
| Sprint 2 | Phase 2 consistency + Phase 3 docs | baseline/overlay、FTS、source docs、Markdown parser 可用 |
| Sprint 3 | Phase 4 docs/code/profile indexing | API/widget 文档检索、基础符号定位、profile auto 可用 |
| Sprint 4 | Phase 5 query service | `kb_search`、`docs_search`、`code_resolve`、`evidence_bundle` 有契约返回 |
| Sprint 5 | Phase 6 MCP | FastMCP stdio/HTTP、本地安全、tools/resources 可被客户端调用 |
| Sprint 6 | Phase 7 eval gates | 60 个 eval cases、质量/性能/稳定性/失败回归 gate 可运行 |
| Sprint 7 | Phase 8 release ops | init/index/serve/validate/clean/migrate/baseline publish 与部署文档闭环 |

---

## 16. V1 最小可发布范围

V1 可以发布的最低功能边界：

- [ ] `init --reuse-baseline` 可配置任意 Active workspace。
- [ ] 本地 workdir `.active-kb/` 可初始化。
- [ ] baseline 只读、overlay 可写。
- [ ] `knowledge-sources/api` 和 `knowledge-sources/widgets` 可索引。
- [ ] Markdown/front matter 可解析。
- [x] SQLite FTS5 可检索文档、chunk、entity。
- [ ] LanceDB 可选启用，禁用时 FTS 路径可用。
- [ ] workspace、module、file、symbol 基础结构索引可用。
- [ ] `profile auto` 有确定性行为，无法判断时 warning。
- [ ] `kb_search`、`docs_search`、`code_resolve`、`code_context`、`workspace_view`、`evidence_bundle` 可用。
- [ ] `config_impact` 有 profile/macro 初版能力。
- [ ] 所有查询返回 evidence、warnings、schema_version。
- [ ] 零结果、多结果、歧义、低置信、partial_ready、blocked 契约测试通过。
- [ ] fail-safe remote security 测试通过。
- [ ] V1 eval quality/performance/stability gates 达标。

---

## 17. 主要风险与工程应对

| 风险 | 应对任务 |
| --- | --- |
| 查询契约后补导致 Skill 和 server 互相猜测 | `D0-01` 至 `D0-04`、`Q5-01`、`Q5-09` |
| baseline/overlay 合并规则不清，增量污染或旧证据泄漏 | `D0-05`、`S2-05`、`S2-06`、`S2-08` |
| profile auto 随机或不可解释 | `D0-06`、`P4-02`、`Q5-08` |
| 第 14/19 节停留在建议，无法阻断 release | `D0-07`、`E7-01` 至 `E7-07` |
| 远程服务误以不安全配置暴露 | `D0-08`、`C1-05`、`M6-05`、`O8-04` |
| 缺 compile DB 导致代码关系误判 | `X3-07`、`Q5-07`、`V9-02`、`V9-03` |
| 向量召回漂移 | `Q5-04`、`Q5-05`、`Q5-07`、`E7-02` |
| 文档过期或未标权威源 | `X3-04`、`P4-03`、`Q5-07` |
| 索引中断导致半坏状态 | `S2-07`、`P4-06`、`E7-04` |
| 用户反馈直接污染知识库 | `O8-06` |

---

## 18. 下一步推荐动作

短期建议按以下顺序落地：

1. 完成 `D0-01` 至 `D0-09`，把评审意见回写到架构文档并维护追踪矩阵。
2. 建立 `active-knowledge-server` 代码骨架、config schema、fail-safe 校验和 storage migration。
3. 先做文档 RAG MVP，再接入代码结构索引，避免一开始被 compile DB 问题拖住。
4. 在 query service 首个可用版本之前完成异常返回契约测试。
5. 在 release 前用 `E7` 系列 gate 阻断质量、性能、稳定性和安全回归。

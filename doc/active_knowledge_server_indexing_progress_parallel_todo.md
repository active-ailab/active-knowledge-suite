# Active Knowledge Server 索引进度与并行加速 TODO

> 文档状态：Draft TODO  
> 生成日期：2026-05-25  
> 适用对象：`active-knowledge-server`  
> 依据文档：[Active Knowledge Server 索引交互与并行加速设计](./active_knowledge_server_indexing_progress_parallel_design.md)  
> 目标命令：`uv run active-kb index --config ../examples/local-single-user.yaml --incremental --profile auto`

---

## 1. 文档目标

本文把“索引期间无反馈”和“首次/增量索引耗时长”拆成可实施、可验收、可回退的任务序列。规划原则是：

- 先交付可观测进度，立即解除用户“不知道是否卡死”的体验问题。
- 并行只进入读取、解析、记录构建阶段，写库保持单写者语义。
- 写入优化采用批量事务和可配置提交节奏，不引入 SQLite 多 writer 竞争。
- 每一阶段都保留 JSON 输出契约，并以基准数据证明收益。

---

## 2. 行业实践调研结论

### 2.1 可迁移原则

| 领域 | 行业实践 | 对本项目的落地判断 |
| --- | --- | --- |
| 搜索索引写入 | OpenSearch 建议重索引/大批量写入时使用 bulk、降低 refresh/flush 频率，并通过实验寻找合适 bulk size。 | 将 SQLite metadata、FTS、vector ref 写入从高频单条 commit 改成 batch commit；默认值必须通过小/中/大数据集压测确定。 |
| 可见性刷新 | OpenSearch 文档明确 `refresh=true` 会让变更立即可查但有性能代价，也建议生产中避免频繁强制 refresh。 | 索引过程中不要为了“每个文件立刻可查”而每条记录 flush；CLI 进度与查询可见性解耦，最终阶段统一 flush。 |
| SQLite 并发 | SQLite WAL 允许读写并发，但同一 WAL 文件仍只能同时有一个 writer。 | 不做“多线程直接写 SQLite”；采用 `N workers collect + 1 writer commit`，必要时评估 WAL/checkpoint，但不把它当作多 writer 加速方案。 |
| Python 并发 | `concurrent.futures` 提供线程池和进程池；进程池可绕过 GIL，但要求任务和结果可 pickle，且有死锁与启动成本风险。 | v1 统一使用 `ThreadPoolExecutor`，让接口和确定性先稳定；CPU 瓶颈被压测证实后再加 `process/hybrid` 模式。 |
| 终端进度 | Rich `Live`/`Progress` 支持刷新频率、indeterminate progress、日志显示在进度区上方。 | text + TTY 模式启用动态 UI；JSON 或非 TTY 输出保持稳定机器可读结果，避免破坏脚本集成。 |

### 2.2 参考资料

- OpenSearch Tuning for indexing speed: https://docs.opensearch.org/latest/tuning-your-cluster/performance/
- OpenSearch Bulk API refresh: https://docs.opensearch.org/latest/api-reference/document-apis/bulk/
- OpenSearch Refresh API: https://docs.opensearch.org/latest/api-reference/index-apis/refresh/
- SQLite Write-Ahead Logging: https://www.sqlite.org/wal.html
- Python `concurrent.futures`: https://docs.python.org/3/library/concurrent.futures.html
- Rich Live Display: https://rich.readthedocs.io/en/stable/live.html
- Rich Progress Display: https://rich.readthedocs.io/en/stable/progress.html

---

## 3. 本地现状与关键约束

| 观察项 | 代码落点 | 规划影响 |
| --- | --- | --- |
| `index` 命令只在结束后输出摘要 | `active-knowledge-server/src/active_knowledge_server/cli.py` 的 `handle_index` | P1 需要给 CLI 增加 progress renderer，且 JSON 输出不能混入动态事件。 |
| 增量 pipeline 当前无 progress callback | `active-knowledge-server/src/active_knowledge_server/indexing/pipeline.py` 的 `IncrementalIndexPipeline.run` | P1 先新增回调契约和事件模型，再改 CLI。 |
| 增量代码路径会调用整仓 `CodeIndexer.collect` 后只应用 changed bundle | `pipeline.py` 调用 `CodeIndexer.collect`，`code_indexer.py` 串行遍历 inventory | P2 应优先拆出按文件 collect 能力，避免“少量变更触发整仓解析”。 |
| 文档增量已按单文档 manifest collect，但仍串行 | `pipeline.py` 的 `doc_paths_to_collect` 循环，`doc_indexer.py` 串行解析 | P2 可先从 docs 并行 collect 切入，收益和风险都更可控。 |
| SQLite writer 多个 upsert 仍独立连接/commit | `active-knowledge-server/src/active_knowledge_server/storage/sqlite_store.py` 的 `SQLiteStorageWriter` | P3 需要 batch/transaction API，否则 collect 并行后的写入段会成为瓶颈。 |
| 配置中已有 `indexing.workers` | `config/schema.py`、`config/defaults.py` | P2 可以直接让现有字段生效，避免新增用户入口过多。 |
| `rich` 在 lock 中存在但 project dependency 未声明 | `active-knowledge-server/uv.lock` 有 rich，`pyproject.toml` 未直接列出 | P1 要确认是否显式加入 dependency，避免 CLI 运行环境依赖偶然传递依赖。 |

---

## 4. 任务标记约定

状态：

- `[ ]` 未开始
- `[~]` 进行中
- `[x]` 已完成
- `[!]` 阻塞或需评审决策

优先级：

- `P0`：必须先完成，影响契约、兼容性、一致性或验收口径
- `P1`：当前迭代建议交付
- `P2`：可跟随主线交付，但允许拆到后续迭代
- `P3`：增强项或压测后再决策

任务类型：

- `DOC`：文档与示例
- `CONTRACT`：事件、配置、输出、错误语义
- `IMPL`：代码实现
- `TEST`：单测、集成测试、性能测试
- `OPS`：压测、发布、运维脚本

---

## 5. 里程碑总览

| 里程碑 | 推荐窗口 | 目标 | 主要产出 | 是否可独立发布 |
| --- | --- | --- | --- | --- |
| Phase 0 | 0.5-1 天 | 契约和基准先落地 | progress event 契约、benchmark 口径、配置决策 | 是 |
| Phase 1 | 1-2 天 | 索引期间有清晰进度 | rich.Live/Progress UI、Ctrl+C 快照、JSON 兼容 | 是 |
| Phase 2 | 2-4 天 | collect 阶段受控并行 | docs/code 线程池 collect、确定性归并、workers 生效 | 是 |
| Phase 3 | 2-4 天 | 写入段批量化 | SQLite batch writer、vector batch、事务参数 | 是 |
| Phase 4 | 持续 | 压测与默认值固化 | 小/中/大数据集报告、默认 workers/batch size 建议 | Release gate |
| Phase 5 | 可选 | 高级并行模式 | code process/hybrid、热点诊断、ETA 优化 | 否，压测后决策 |

---

## 6. Phase 0：契约、基准与落地边界

Phase 0 的目标是避免实现阶段反复改接口。先固定事件契约、输出边界、压测口径，再进入 UI 和并发实现。

### IP0-01 定义索引进度事件契约

- 状态：`[ ]`
- 优先级：`P0`
- 类型：`CONTRACT`、`DOC`
- 依赖：设计文档第 5 节
- 目标：新增统一 `IndexProgressEvent` 契约，供 pipeline、CLI、测试共用。
- 建议落点：`active_knowledge_server/indexing/progress.py`

TODO：

- [ ] 定义 `phase` 枚举：`plan`、`discover`、`code_collect`、`code_apply`、`doc_collect`、`doc_apply`、`vectors_apply`、`profile_relations`、`workspace_map`、`done`。
- [ ] 定义字段：`stage_total`、`stage_done`、`global_total`、`global_done`、`current_path`、`message`、`warnings_count`、`started_at`、`updated_at`、`eta_seconds`。
- [ ] 定义 callback 类型：`IndexProgressCallback = Callable[[IndexProgressEvent], None]`。
- [ ] 提供 `noop_progress_callback`，保证默认调用路径零行为变化。
- [ ] 明确事件只表达进度，不承载记录内容、源码片段、embedding 或敏感信息。

验收标准：

- 事件对象有 `to_dict()` 或等价 JSON-safe 序列化。
- 单测覆盖必填字段、可选字段、未知阶段不可用。
- 设计文档或 README 能引用同一套字段名，不再另起 UI 私有术语。

### IP0-02 固定 text/JSON 输出边界

- 状态：`[ ]`
- 优先级：`P0`
- 类型：`CONTRACT`、`TEST`
- 依赖：`IP0-01`
- 目标：确保进度 UI 不破坏现有脚本和 JSON 消费方。
- 建议落点：`active_knowledge_server/cli.py`

TODO：

- [ ] 规定 `--format json` 只输出最终 payload，不输出中间动态事件。
- [ ] 规定 text + TTY 才启用 Rich 动态 UI。
- [ ] 规定 text + 非 TTY 使用低频 line progress 或仅最终摘要。
- [ ] 增加 CLI 测试，断言 JSON 输出可被 `json.loads` 直接解析。

验收标准：

- 现有 `index --format json` 快照不因进度事件改变。
- 非 TTY 环境不会输出 ANSI 动态控制序列。

### IP0-03 建立索引性能基线脚本

- 状态：`[ ]`
- 优先级：`P0`
- 类型：`OPS`、`TEST`
- 依赖：无
- 目标：在改并发前拿到可比较的基线，不凭感觉调 workers 和 batch size。
- 建议落点：`active-knowledge-server/scripts/benchmark_index.py` 或 `tests/perf/`

TODO：

- [ ] 支持同一 config 下跑 `workers=1/2/4/8/auto`。
- [ ] 记录 wall time、CPU time、峰值 RSS、索引文件数、chunk/entity/evidence/vector 数、warning 数。
- [ ] 支持冷启动和热缓存两种模式，结果写入 JSONL。
- [ ] 固化小/中/大三档数据集定义：`<5k`、`5k-30k`、`>30k` 文件。

验收标准：

- 每次性能对比能追溯到 config、git commit、数据集规模和机器信息。
- 无基准报告不得调整默认 `workers`、`batch_size`、`commit_interval_ms`。

### IP0-04 明确并发边界与回退开关

- 状态：`[ ]`
- 优先级：`P0`
- 类型：`CONTRACT`、`DOC`
- 依赖：`IP0-03`
- 目标：先定义安全边界，避免实现时误引入多 writer。

TODO：

- [ ] 规定 v1 并行只覆盖 scan/read/parse/chunk/entity/evidence/vector input 构建。
- [ ] 规定 SQLite/LanceDB 写入保持单 writer。
- [ ] 规定 `indexing.workers: 1` 等价关闭并行。
- [ ] 规定失败回退：某个 worker 失败时只降级该文件/文档，pipeline 保持现有 `partial_ready` 语义。

验收标准：

- TODO、设计文档和配置示例对并发边界表述一致。
- 代码评审时可以用该条作为“不接受多线程写库”的 gate。

---

## 7. Phase 1：进度交互先交付

Phase 1 不改变并发和写入语义，只让用户在长时间索引时持续看到“正在做什么、做了多少、最近处理了哪些文件”。

### IP1-01 在增量 pipeline 接入 progress callback

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`IP0-01`
- 建议落点：`indexing/pipeline.py`

TODO：

- [ ] `IncrementalIndexPipeline.run(...)` 新增可选 `progress_callback` 参数，默认 noop。
- [ ] 在 plan 完成后发出 `plan` 事件，带上 global total。
- [ ] 在 deleted code/doc tombstone 循环发出 apply 事件。
- [ ] 在 changed code/doc collect/apply 循环发出当前 path。
- [ ] 在 profile relations、workspace map 阶段发出固定步数事件。
- [ ] 异常降级时更新 `warnings_count`，但不把异常栈塞入进度事件。

验收标准：

- 不传 callback 时现有测试全部保持行为不变。
- 传入 callback 时事件 `global_done` 单调递增，最终到达 `global_total`。

### IP1-02 扩展全量索引进度事件

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`IP1-01`
- 建议落点：`cli.py` 的 `run_full_index(...)` 路径及相关 indexer 调用

TODO：

- [ ] 为 full index 定义 code/docs/profile/workspace_map 的阶段总量。
- [ ] 让 full index 路径复用同一 `IndexProgressEvent` 契约。
- [ ] 无法提前知道总量的阶段先发 `total=None` 或 indeterminate 事件，发现总量后补齐。

验收标准：

- `--full` 和默认增量模式共用同一 CLI renderer。
- 全量模式不会因为总量暂缺导致 UI 报错。

### IP1-03 实现 CLI Rich renderer

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`IP0-02`、`IP1-01`
- 建议落点：`active_knowledge_server/cli.py` 或 `active_knowledge_server/cli_progress.py`

TODO：

- [ ] 增加 `IndexProgressRenderer`，维护 global task、stage task、`deque(maxlen=10)` recent paths。
- [ ] 使用 Rich `Live` 或 `Progress`，刷新频率默认 4-5Hz。
- [ ] 动态 UI 完成后保留最终摘要，避免用户看不到最后状态。
- [ ] 日志或 warning 输出必须通过 Rich console 打在进度区上方，避免破坏进度条。
- [ ] 如果 Rich 不可用，降级为纯文本摘要并给出轻量 warning。

验收标准：

- text + TTY 下可看到总进度、阶段进度、最近文件。
- `--format json` 下没有 Rich 输出。
- renderer 单测覆盖最近文件截断、阶段切换、done 事件。

### IP1-04 补齐依赖声明

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`
- 依赖：`IP1-03`
- 建议落点：`active-knowledge-server/pyproject.toml`

TODO：

- [ ] 确认 `rich` 是否应作为直接 runtime dependency。
- [ ] 如果使用 Rich，显式加入 `dependencies`，不要依赖传递依赖偶然存在。
- [ ] 更新 lock 文件。

验收标准：

- 新环境 `uv sync` 后直接运行 CLI 进度 UI，不依赖开发环境残留包。

### IP1-05 统一 Ctrl+C 中断体验

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`IP1-03`
- 建议落点：`cli.py`

TODO：

- [ ] 捕获 `KeyboardInterrupt` 并停止 Live renderer。
- [ ] 输出中断快照：`phase`、`stage_done/stage_total`、`global_done/global_total`、最后处理文件。
- [ ] 保持普通用户不看到冗长 traceback。
- [ ] 确认中断后重跑增量仍能收敛，不提前保存错误 state。

验收标准：

- 人工验收 `Ctrl+C` 时终端恢复正常光标和换行。
- 中断后再次运行 `index --incremental` 不新增 blocked/error。

---

## 8. Phase 2：并行 collect 与确定性归并

Phase 2 的目标是压缩读取/解析/记录构建耗时。v1 先用线程池，因为它最小化 pickle、进程启动、对象跨进程传输的复杂度。

### IP2-01 让 `indexing.workers` 真正生效

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`IP0-04`
- 建议落点：`config/schema.py`、`indexing/parallel.py`

TODO：

- [ ] 新增 worker 解析函数：`auto` 根据 CPU、文件数和阶段类型给出保守值。
- [ ] `workers=1` 强制串行，作为快速回退开关。
- [ ] 记录实际 worker 数到 result metadata，方便压测和问题追踪。
- [ ] 对空任务、单任务、小仓自动使用串行，避免线程池开销大于收益。

验收标准：

- 配置 `workers=1/2/auto` 在测试中可观察到不同执行路径。
- result 或 debug log 能看到 resolved workers。

### IP2-02 抽象受控并行工具

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`IP2-01`
- 建议落点：`active_knowledge_server/indexing/parallel.py`

TODO：

- [ ] 基于 `ThreadPoolExecutor` 实现 `parallel_map_ordered` 或等价 helper。
- [ ] 支持 `max_in_flight`，避免大仓一次性提交全部任务导致内存峰值过高。
- [ ] 支持 per-item 异常包装，返回 path + error + warning，不让单文件失败杀死整个阶段。
- [ ] 支持 callback 上报 `current_path` 和 done count。
- [ ] 输出按输入 path 排序，保证后续写入和测试快照稳定。

验收标准：

- 单测覆盖顺序稳定、单任务失败、全部失败、取消/中断。
- 并行 helper 不直接依赖 code/doc 业务对象。

### IP2-03 拆分文档单文件 collect

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`IP2-02`
- 建议落点：`indexing/doc_indexer.py`

TODO：

- [ ] 将 `DocumentIndexer.collect` 中单文档解析和记录构建拆成私有方法，如 `_collect_document_entry(...)`。
- [ ] source records 仍按 manifest/category 串行构建。
- [ ] 每个 worker 只返回当前文档的 file/chunk/entity/evidence/embedding input/warnings。
- [ ] embedding preparation 可先在主线程统一处理，避免 secret scanner 与向量写入跨线程复杂化。
- [ ] 归并后按 `storage_relative_path`、`ordinal`、稳定 ID 排序。

验收标准：

- 串行与并行输出的逻辑对象集合等价。
- warning 中保留原始 path。
- 文档增量失败仍返回 `partial_ready`，不污染其他文档。

### IP2-04 拆分代码单文件 collect

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`IP2-02`
- 建议落点：`indexing/code_indexer.py`

TODO：

- [ ] 将读取、hash、`FileRecord`、C family parse、Makefile parse 拆成 `_collect_code_entry(...)`。
- [ ] 目录 anchor、directory contains relation、跨文件汇总关系仍在主线程确定性构建。
- [ ] Makefile sibling scan 保持可控，失败只产生当前文件 warning。
- [ ] 归并 `code_parses`、`makefile_parses`、`file_texts` 时按 relative path 排序。
- [ ] 对 ctags/tree-sitter 等非线程安全依赖做确认；如果不安全，先为该 parser 加串行 fallback。

验收标准：

- 串行与并行输出的 file/chunk/entity/relation/evidence 集合等价。
- 大仓下并行不会打乱实体 ID、relation ID、chunk ordinal。

### IP2-05 增量模式避免少量变更触发整仓 collect

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`IP2-03`、`IP2-04`
- 建议落点：`pipeline.py`、`code_indexer.py`、`doc_indexer.py`

TODO：

- [ ] 为 `CodeIndexer.collect` 增加 `include_paths` 或 bundle-level collect API。
- [ ] 当 `plan.reindex_all_code=False` 时只 collect `changed_code_paths` 需要的 bundle。
- [ ] 保留 reindex all 场景的整仓 collect 路径。
- [ ] 文档路径继续使用 filtered manifest，但改为批量 filtered manifest + 并行 collect，而不是逐文档调用 `collect`。

验收标准：

- 修改 1 个代码文件时，不再读取/解析整仓所有可索引代码文件。
- 增量应用后的 live bundle 与旧实现逻辑等价。

### IP2-06 pipeline 集成并行 collect + 单写者 apply

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`IP2-03`、`IP2-04`、`IP2-05`
- 建议落点：`indexing/pipeline.py`

TODO：

- [ ] code/doc collect 阶段并行产生内存 bundle。
- [ ] apply 阶段仍在主线程按 `relative_path` 排序调用 `_apply_code_bundle`、`_apply_doc_bundle`。
- [ ] vector upsert 仍在主线程，先不做多线程写 vector store。
- [ ] progress 事件区分 collect 和 apply，便于定位是解析慢还是写入慢。

验收标准：

- `workers=1` 和 `workers>1` 的最终索引对象集合等价。
- 单文件失败不会阻塞其他文件 apply。
- `validate --format json` 不新增 blocked/error。

---

## 9. Phase 3：单写者批量提交与写入调优

Phase 3 用于解决 collect 并行后写入段成为瓶颈的问题。核心仍是单 writer，只是降低连接、事务、commit、FTS 同步的固定成本。

### IP3-01 增加 SQLite transaction/batch writer API

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`IP0-04`
- 建议落点：`storage/sqlite_store.py`

TODO：

- [ ] 增加 writer transaction context，如 `with writer.transaction(): ...`。
- [ ] 在一个连接内批量 upsert file/chunk/entity/relation/evidence/vector_ref。
- [ ] chunk/entity 的 FTS 同步与 record upsert 在同一事务内完成。
- [ ] transaction 失败时整体 rollback，不留下半批次 FTS 不一致。

验收标准：

- 单测覆盖 rollback 后 metadata 与 FTS 不出现半写状态。
- 现有单条 upsert API 继续可用，降低改造风险。

### IP3-02 增加 batch size 与 commit interval 配置

- 状态：`[ ]`
- 优先级：`P2`
- 类型：`CONTRACT`、`IMPL`、`TEST`
- 依赖：`IP3-01`、`IP0-03`
- 建议落点：`config/schema.py`、`config/defaults.py`

TODO：

- [ ] 新增 `indexing.writer.batch_size`，初始默认值先保守设置，压测后再调整。
- [ ] 新增 `indexing.writer.commit_interval_ms`，防止长时间无 commit。
- [ ] `batch_size=1` 可回退到近似旧行为。
- [ ] 参数进入 result metadata 和 benchmark 报告。

验收标准：

- 配置校验拒绝非正数。
- 默认值有基准报告支撑。

### IP3-03 改造 pipeline apply 为批量写入

- 状态：`[ ]`
- 优先级：`P2`
- 类型：`IMPL`、`TEST`
- 依赖：`IP3-01`、`IP3-02`
- 建议落点：`indexing/pipeline.py`

TODO：

- [ ] `_apply_code_bundle` 和 `_apply_doc_bundle` 支持批量上下文，避免每条记录独立 commit。
- [ ] tombstone/replacement 与新对象写入保持同一逻辑批次。
- [ ] 按 path 边界记录进度，按 batch 边界提交事务。
- [ ] batch 失败时降级到定位具体 path 的 warning，必要时回退单文件写入以提升可诊断性。

验收标准：

- 并行 collect + batch apply 下结果与串行旧实现等价。
- batch 失败不会保存错误 incremental state。

### IP3-04 评估 SQLite WAL 与 checkpoint 策略

- 状态：`[ ]`
- 优先级：`P2`
- 类型：`OPS`、`IMPL`、`TEST`
- 依赖：`IP3-01`、`IP0-03`
- 建议落点：`storage/sqlite_store.py`

TODO：

- [ ] 压测 `journal_mode=DELETE` 与 `journal_mode=WAL` 的索引吞吐和查询并发影响。
- [ ] 如果启用 WAL，确认仅用于本地文件系统，不支持网络文件系统假设。
- [ ] 评估 `synchronous`、checkpoint 触发时机和 WAL 文件膨胀。
- [ ] 把最终选择写入配置或存储初始化注释。

验收标准：

- 没有压测报告不得默认启用 WAL。
- 启用 WAL 后 `validate`、并发只读稳定性测试通过。

### IP3-05 向量写入批量化

- 状态：`[ ]`
- 优先级：`P2`
- 类型：`IMPL`、`TEST`
- 依赖：`IP2-06`
- 建议落点：`storage/lancedb_store.py`、`pipeline.py`

TODO：

- [ ] 增加 vector writer batch upsert 或批量 flush。
- [ ] 保持 metadata vector_ref 与实际 vector 写入的一致性检查。
- [ ] 对 vector 写入失败返回 degraded warning，并保留 metadata 可用。

验收标准：

- rebuild vectors 场景吞吐提升可量化。
- vector ref 缺失或悬挂能被 `validate` 捕获。

---

## 10. Phase 4：验收、压测与发布 gate

### IP4-01 交互体验验收

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`TEST`
- 依赖：`IP1-03`、`IP1-05`

TODO：

- [ ] 人工运行目标命令，确认可见 global progress、stage progress、recent paths。
- [ ] 人工测试 `Ctrl+C`，确认输出中断快照且终端不乱码。
- [ ] 运行 `--format json`，确认输出为单个最终 JSON。
- [ ] 在非 TTY 环境运行，确认不会刷动态控制字符。

验收标准：

- 用户能在 2 秒内判断当前阶段和最近文件。
- 中断体验没有 traceback 噪音。

### IP4-02 一致性验收

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`TEST`
- 依赖：`IP2-06`、`IP3-03`

TODO：

- [ ] 对同一数据集分别运行 `workers=1` 和 `workers=auto`。
- [ ] 比较 file/chunk/entity/relation/evidence/vector_ref 的逻辑对象集合。
- [ ] 运行 `uv run active-kb validate --config ../examples/local-single-user.yaml --format json`。
- [ ] 运行 `uv run active-kb status --config ../examples/local-single-user.yaml --format json`。

验收标准：

- 并行与串行结果集合等价，允许时间戳/运行统计不同。
- `validate` 不新增 blocked/error。

### IP4-03 性能验收

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`OPS`、`TEST`
- 依赖：`IP0-03`、`IP2-06`、`IP3-03`

TODO：

- [ ] 小仓、中仓、大仓分别跑 `workers=1/2/4/8/auto`。
- [ ] batch size 分别跑 `1/100/200/500/1000` 或按对象量调整。
- [ ] 输出推荐默认值和不推荐区间。
- [ ] 记录是否存在内存峰值、SQLite lock、WAL 膨胀、vector 写入失败。

建议目标：

- 小仓 `<5k` 文件：总耗时改善 `>=20%`，且 UI 无明显闪烁。
- 中仓 `5k-30k` 文件：总耗时改善 `>=30%`。
- 大仓 `>30k` 文件：总耗时改善 `>=35%`，峰值内存不超过串行基线 `2x`。

验收标准：

- 性能报告写入 `doc/` 或 `tests/perf/results/`。
- 默认 worker/batch 配置有数据支撑，而不是拍脑袋。

### IP4-04 回归测试清单

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`TEST`
- 依赖：所有实现任务

TODO：

- [ ] 新增 progress event 单测。
- [ ] 新增 CLI JSON 兼容测试。
- [ ] 新增 renderer recent paths 和 stage 切换测试。
- [ ] 新增 doc/code 并行 collect 等价性测试。
- [ ] 新增 worker 单文件失败降级测试。
- [ ] 新增 batch writer rollback/FTS 一致性测试。
- [ ] 保持现有 incremental pipeline 测试全部通过。

验收标准：

- `uv run pytest` 通过。
- 如新增 perf 测试，默认不进入普通单测慢路径。

---

## 11. Phase 5：后续增强项

### IP5-01 引入 code process/hybrid 模式

- 状态：`[ ]`
- 优先级：`P3`
- 类型：`IMPL`、`TEST`
- 依赖：`IP2-04`、`IP4-03`

TODO：

- [ ] 只有当压测证明 code parse 是 CPU 瓶颈时才实现。
- [ ] 确认 worker 输入输出完全可 pickle。
- [ ] 避免在 process worker 内调用 executor/future 方法。
- [ ] 增加 `indexing.parallel.mode: thread | process | hybrid`。

验收标准：

- process/hybrid 相比 thread 在中/大仓有稳定收益。
- 失败和取消语义与 thread 模式一致。

### IP5-02 高级 ETA 与热点诊断

- 状态：`[ ]`
- 优先级：`P3`
- 类型：`IMPL`、`OPS`
- 依赖：`IP1-03`、`IP4-03`

TODO：

- [ ] 基于滑动窗口计算阶段 ETA。
- [ ] 输出最慢 N 个文件或文档的诊断摘要。
- [ ] 对 parser、embedding、metadata write、vector write 分阶段计时。
- [ ] 将计时信息写入 benchmark JSONL。

验收标准：

- ETA 不稳定时隐藏或标记为估算，避免误导用户。
- 慢文件诊断不输出敏感源码内容。

---

## 12. 推荐实施顺序

1. 完成 `IP0-01` 至 `IP0-04`，锁定契约、输出边界和基准口径。
2. 完成 `IP1-01`、`IP1-03`、`IP1-05`，先发布“可见进度”。
3. 完成 `IP2-01` 至 `IP2-03`，从 docs 并行 collect 获取低风险收益。
4. 完成 `IP2-04` 至 `IP2-06`，再扩展到 code 并行和增量路径过滤。
5. 完成 `IP3-01` 至 `IP3-03`，解决写入瓶颈。
6. 用 `IP4-01` 至 `IP4-04` 作为发布 gate。
7. 只有压测证明确有必要时，再进入 `IP5-01` 和 `IP5-02`。

---

## 13. 第一批建议排期

| 批次 | 任务 | 目标 | 风险 |
| --- | --- | --- | --- |
| Batch A | `IP0-01`、`IP0-02`、`IP1-01`、`IP1-03`、`IP1-05` | 让索引有进度反馈，不改并发 | 低 |
| Batch B | `IP0-03`、`IP2-01`、`IP2-02`、`IP2-03` | docs collect 并行和基准脚本 | 中 |
| Batch C | `IP2-04`、`IP2-05`、`IP2-06` | code collect 并行和增量过滤 | 中高 |
| Batch D | `IP3-01`、`IP3-02`、`IP3-03` | batch writer 和写入提速 | 中高 |
| Batch E | `IP4-01` 至 `IP4-04` | 发布验收与默认值固化 | 中 |

推荐先开 Batch A。它最贴近用户痛点，也能给后续并行改造提供实时观测能力。性能优化这只小野兽，最好先给它装仪表盘，再给油门。

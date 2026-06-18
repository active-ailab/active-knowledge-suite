# Active Knowledge Server 索引加速与断点续建 TODO

> 文档状态：Draft TODO  
> 生成日期：2026-05-29  
> 适用对象：`active-knowledge-server`  
> 依据文档：[Active Knowledge Server 索引加速与断点续建设计](./active_knowledge_server_indexing_acceleration_resume_design.md)  
> 衔接计划：[Active Knowledge Server 索引进度与并行加速 TODO](./active_knowledge_server_indexing_progress_parallel_todo.md)  
> 目标命令：`uv run active-kb index --config ../examples/local-single-user.yaml --incremental --source all --resume auto`

---

## 1. 文档目标

本文把“中断后不能继续构建”和“第二阶段加速”拆成可实施、可验收、可回退的任务序列。它不替代现有进度/并行 TODO，而是接在已有 IP0-IP5 之后：

- 已有 TODO 解决“看得见进度、collect 并行、基础批量写入、benchmark 入口”。
- 本 TODO 解决“索引作业持久化、任务级 checkpoint、恢复策略、全量 staging 发布、向量/embedding 二阶段优化”。

优先级判断：

- 第一优先级是 `job + plan_signature + task checkpoint`，因为它直接解决用户本地手测发现的“耗时长且中断后不能继续”的痛点。
- 第二优先级是把现有 batch writer 能力真正扩展到 incremental apply 的批处理和失败降级。
- 第三优先级才是 collect artifact、embedding/vector cache、full staging、process/hybrid 等更大改造。

---

## 2. 与现有 TODO 的关系

### 2.1 已可复用基础

| 已有能力 | 现有任务 | 复用方式 |
| --- | --- | --- |
| 进度事件与 CLI renderer | `IP0-01`、`IP1-01` 至 `IP1-05` | 新增 job/task 统计后继续复用 `IndexProgressEvent` 和 renderer。 |
| 并行 collect | `IP2-01` 至 `IP2-06` | resume 后只 collect 未完成任务，仍走 `parallel_map_ordered`。 |
| 增量按 path collect | `IP2-05` | task ledger 直接沿用 changed/deleted code/doc path。 |
| SQLite transaction | `IP3-01` | checkpoint 只在 transaction 成功后写入。 |
| writer batch 配置 | `IP3-02` | 继续作为 apply batch 的保守默认值。 |
| vector batch upsert | `IP3-05` | 作为 vector task checkpoint 的提交边界。 |
| benchmark 脚本 | `IP0-03`、`IP4-03` | 扩展 phase timing、resume/crash 场景和 checkpoint 统计。 |
| job store 雏形 | 现有 `indexing/jobs.py` | 从测试/ops runner 扩展为主 pipeline 持久 job store。 |

### 2.2 不在本轮重复做

- 不重做 Rich 进度 UI。
- 不重做基础线程池并行。
- 不默认开启 SQLite WAL。
- 不在没有 benchmark 数据前启用 process/hybrid。
- 不把多线程直接写 SQLite 作为优化路径。

---

## 3. 任务标记约定

状态：

- `[ ]` 未开始
- `[~]` 进行中
- `[x]` 已完成
- `[!]` 阻塞或需评审决策

优先级：

- `P0`：必须先完成，影响恢复语义、一致性、数据安全或 CLI 契约
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

## 4. 里程碑总览

| 里程碑 | 推荐窗口 | 目标 | 主要产出 | 是否可独立发布 |
| --- | --- | --- | --- | --- |
| Phase R0 | 0.5-1 天 | 固定恢复契约 | plan signature、task key、resume policy、输出字段定义 | 是 |
| Phase R1 | 1-2 天 | 主 pipeline job 化 | CLI 创建/恢复 job、lock/heartbeat、job metadata | 是 |
| Phase R2 | 2-4 天 | 增量任务级 checkpoint | task ledger、applied 跳过、Crash/Ctrl+C 恢复 | 是 |
| Phase R3 | 2-4 天 | incremental apply 批处理 | `ApplyBatch`、真实 job id、batch 失败降级 | 是 |
| Phase R4 | 2-5 天 | resume 验收与 benchmark | crash harness、phase timing、恢复耗时报告 | Release gate |
| Phase R5 | 3-6 天 | artifact/cache 二阶段优化 | collect artifact、embedding cache、vector delta/compaction | 可选发布 |
| Phase R6 | 4-8 天 | full staging publish | staging store、validate 后 publish、旧版本清理 | 是，但风险高 |
| Phase R7 | 压测后 | 高级并行与默认值固化 | process/hybrid、WAL 默认值、worker 默认值 | 否，压测后决策 |

---

## 5. Phase R0：恢复契约与边界

Phase R0 只固定契约，不大改执行路径。目标是让后续实现不会在“什么才算可恢复”上反复摇摆。

### AR0-01 定义 plan signature 契约

- 状态：`[x]`
- 优先级：`P0`
- 类型：`CONTRACT`、`TEST`
- 依赖：`IP0-01`、`IP2-05`
- 建议落点：`active_knowledge_server/indexing/resume.py` 或 `indexing/jobs.py`

TODO：

- [x] 新增 `IndexPlanSignature` 或 `make_index_plan_signature(...)` helper。
- [x] signature 输入包含：mode、target、source、snapshot_id、workspace inventory hash、source docs manifest hash、parser schema、profile relation schema、embedding provider/model/enabled、影响解析的 config hash、storage schema version。
- [x] 明确 `workers`、writer batch size、commit interval 不进入 signature。
- [x] 对 signature payload 使用 sorted JSON + sha256，保证跨进程稳定。
- [x] 单测覆盖同输入稳定、无关配置变化不变、parser/embedding/manifest 变化必变。

完成记录：

- `active_knowledge_server/indexing/resume.py` 提供 `IndexPlanSignature`、`make_index_plan_signature(...)`、`diff_plan_signature_payloads(...)` 和 `format_plan_signature_mismatch_reason(...)`。
- `active_knowledge_server/indexing/__init__.py` 已导出 plan signature 契约，供后续 job metadata / JSON 输出接入。

验收标准：

- `plan_signature` 可写入 job metadata 和 JSON 输出。
- signature 不匹配时能够给出可读原因或至少给出 previous/current 摘要。

### AR0-02 定义 deterministic task key 与 task list

- 状态：`[x]`
- 优先级：`P0`
- 类型：`CONTRACT`、`IMPL`、`TEST`
- 依赖：`AR0-01`
- 建议落点：`active_knowledge_server/indexing/tasks.py`

TODO：

- [x] 定义 task key 格式：`code:apply:<path>`、`code:delete:<path>`、`doc:apply:<path>`、`doc:delete:<path>`、`vector:doc:<path>`、`profile:relations`、`workspace:map`。
- [x] 从 `IncrementalIndexPlan` 派生稳定排序 task list。
- [x] 每个 task 携带 `phase`、`source_kind`、`relative_path`、`input_hash`、`schema_version`、`embedding_model`、`required`。
- [x] 将现有 `_incremental_code_paths_to_collect`、`_incremental_doc_paths_to_collect` 的结果纳入 collect task 依赖。
- [x] 单测覆盖全量重建、少量 code/doc 变更、删除、vector rebuild、profile 变更。

完成记录：

- `active_knowledge_server/indexing/tasks.py` 提供 `IndexTask`、`make_index_task_list(...)`、`index_task_list_to_dict(...)` 和 progress total 估算 helper。
- task list 使用稳定排序；collect 依赖通过 `code:collect:<path>` / `doc:collect:<path>` 记录在 apply/vector task 上，主 task key 仍保持验收列表中的格式。

验收标准：

- 同一 plan 多次生成 task list 完全一致。
- task list 数量能对应 progress global total。

### AR0-03 定义 resume policy 与 CLI 契约

- 状态：`[x]`
- 优先级：`P0`
- 类型：`CONTRACT`、`DOC`、`TEST`
- 依赖：`AR0-01`
- 建议落点：`cli.py`、`config/schema.py`

TODO：

- [x] 定义 CLI 参数：`--resume auto|<job_id>`、`--restart`、`--no-resume`、`--job-id <job_id>`。
- [x] 规定默认行为为 `--resume auto`。
- [x] 规定 `--restart` 与 `--resume/--no-resume` 的互斥校验。
- [x] 规定 JSON final payload 新增 `job` 对象，不破坏现有 `result` payload。
- [x] 更新 help 文案和本地集成测试文档命令示例。

行业实践调研结论：

- Google Cloud Storage 的可恢复上传建议中断后“重新运行同一命令即可继续”，因此本项目默认采用 `--resume auto`，减少用户在长索引被打断后的认知负担。
- Wget 的 `--continue` 明确区分继续下载和从头开始，且在不能续传时会回退/提示；本项目对应保留 `--restart` 与 `--no-resume`，避免把“重建”和“续建”混在一起。
- Python `argparse` 原生支持 mutually exclusive group；本项目把 `--resume`、`--restart`、`--no-resume` 放在 parser 层互斥，保证 help、错误码和 CI 行为一致。
- AWS CLI 把 JSON 作为独立输出格式；本项目继续保持 `--format json` stdout 只输出一个最终 JSON payload，进度仍走 stderr 或 text renderer。

完成记录：

- `active_knowledge_server/cli.py` 新增 `resolve_index_resume_policy(...)`、`build_index_job_payload(...)`，并把 `index` parser 扩展为 `--resume auto|JOB_ID | --restart | --no-resume` 加 `--job-id JOB_ID`。
- `active_knowledge_server/config/schema.py` 新增 `IndexResumeMode` 类型，供 CLI/pipeline/job store 后续复用。
- `index --format json` 顶层新增 `job` 对象；`result` 对象保持原结构。增量路径在已有 `IncrementalIndexResult.plan` 上生成 `plan_signature` 与 task 总数，full 路径先输出空 plan/task 字段，等待 AR1/R6 job 化/staging 接入。
- `KeyboardInterrupt` JSON payload 新增 `job` 对象，保证中断场景仍有机器可读的 resume policy。
- `doc/active_knowledge_server_local_full_integration_test.md` 的增量索引示例已显式加入 `--resume auto`。

验收标准：

- `active-kb index --help` 能清楚说明恢复行为。
- `--format json` 输出仍是单个可解析 JSON。

### AR0-04 定义 checkpoint 安全边界

- 状态：`[x]`
- 优先级：`P0`
- 类型：`CONTRACT`、`DOC`、`TEST`
- 依赖：`AR0-02`

TODO：

- [x] 明确 checkpoint 只能在 metadata/vector apply 成功后写入 `applied`。
- [x] 明确 apply 成功但 checkpoint 失败时，下次允许重复 apply。
- [x] 明确 checkpoint 成功后，该 task 可跳过。
- [x] 明确 collect artifact 的 `collected` 状态不是查询可见性边界。
- [x] 明确 `index_state.json` 仍只代表完整成功状态，不作为中间 checkpoint。

行业实践调研结论：

- Flink checkpoint 把输入位置和 operator state 作为一致快照；失败恢复从快照继续处理。因此本项目把 `applied` checkpoint 定义成“已提交事实”的游标，而不是“开始处理”的意图日志。
- Spark Structured Streaming 限制同一 checkpoint location 下可变更的 source/sink/schema；本项目已在 AR0-01 用 plan signature 绑定 manifest、schema、embedding model 和影响解析的配置，signature 不匹配不得复用 task checkpoint。
- Elasticsearch bulk indexing 可延迟 refresh，把“已写入”和“可搜索可见”分开；本项目对应把 collect artifact 的 `collected` 状态排除在查询可见性边界之外，只有 metadata/vector apply 成功后的 `applied` 才能作为跳过依据。
- SQLite WAL 文档把 write、commit 和 WAL checkpoint 区分为不同操作；本项目也明确区分 SQLite WAL checkpoint 与索引 task checkpoint，继续坚持单 writer，并只在应用事务提交成功之后写 task checkpoint。

完成记录：

- `active_knowledge_server/indexing/jobs.py` 新增 `IndexTaskCheckpoint`、`task_checkpoint_key(...)`、`record_task_collected_checkpoint(...)`、`record_task_applied_checkpoint(...)`、`task_has_applied_checkpoint(...)`。
- task checkpoint key 分为 `task:collected:<task_key>` 和 `task:applied:<task_key>`；恢复跳过只认 `task:applied:*`，并校验 task key、phase、input hash、task schema version 都匹配。
- `record_task_applied_checkpoint(...)` 的代码注释明确：只能在 metadata/vector commit 成功后调用；如果 apply 成功但 checkpoint 写入失败，恢复时会再次 apply，要求稳定 ID/upsert/tombstone/replacement 收敛。
- `IncrementalIndexPipeline.save_state(...)` 注释明确 `index_state.json` 是完整成功后的下一轮 diff baseline，不是 in-flight task checkpoint。
- `tests/unit/test_index_jobs.py` 覆盖 checkpoint 写入失败后的重复 apply，以及 `collected` checkpoint 不会触发 task skip。

验收标准：

- 文档和代码注释对“后写 checkpoint + 幂等重放”表达一致。
- 测试能模拟 apply 成功 checkpoint 失败后的重复 apply。

---

## 6. Phase R1：主 pipeline job 化

Phase R1 让 CLI 和 `IncrementalIndexPipeline.run` 真正使用当前 jobs SQLite，而不是只在 ops/test runner 中使用。

### AR1-01 扩展 job store 查询能力

- 状态：`[x]`
- 优先级：`P0`
- 类型：`IMPL`、`TEST`
- 依赖：`AR0-01`
- 建议落点：`indexing/jobs.py`

TODO：

- [x] 增加按 metadata 查询最近可恢复 job 的 helper，例如 `find_resumable_index_job(...)`。
- [x] 增加 `transition_or_update_running_metadata(...)`，便于更新 `last_phase/last_task_key/tasks_*`。
- [x] 增加 lock heartbeat/renew helper，避免长任务超过 TTL。
- [x] 增加 `supersede_job(...)`，供 `--restart` 标记旧 job。
- [x] 单测覆盖 lock 未过期、lock 过期、signature 匹配/不匹配、retry_count/resume_count。

行业实践调研结论：

- Google Cloud Storage resumable upload 把大对象传输拆成可恢复请求，并建议中断后用同一命令继续；本项目对应把 `plan_signature + requested_* metadata` 作为恢复匹配条件，只恢复同一索引计划，不把配置已变化的 job 当作可续建。
- Kubernetes Lease 用 `holderIdentity`、`leaseDurationSeconds`、`renewTime` 协调心跳和 leader election；本项目对应新增 `renew_lock(...)` / `heartbeat_lock(...)`，未过期 lock 阻断恢复，过期 lock 允许后续重新 acquire。
- Celery task 文档强调可重放任务应保持幂等，并允许通过 custom state metadata 上报进度；本项目对应用 `transition_or_update_running_metadata(...)` 更新 `execution_state/last_phase/last_task_key/tasks_*`，而 task skip 仍只认 AR0-04 的 applied checkpoint。

完成记录：

- `active_knowledge_server/indexing/jobs.py` 新增 `find_resumable_index_job(...)`，按 `job_type/write_target/snapshot/profile/status` 预筛，再用 decoded metadata 精确匹配 `plan_signature` 和调用方传入的 `metadata_match`；遇到未过期 `INDEX_JOB_LOCK_ID` 会抛 `JobLockConflictError`，与设计中的 blocked 行为一致。
- `resume_job(..., increment_resume_count=True)` 可原子增加 `resume_count` 并写入 `execution_state=running/resumed_at`；旧调用默认不变。
- `transition_or_update_running_metadata(...)` 支持 pending -> running transition，也支持运行中只更新 metadata，便于主 pipeline 持续写入 `last_phase/last_task_key/tasks_total/tasks_applied/...`。
- `renew_lock(...)` 和 `heartbeat_lock(...)` 会保留原 `acquired_at`，刷新 `expires_at`，并在 lock metadata 写入 `heartbeat_at`；非 owner 续租会抛 `JobLockConflictError`。
- `supersede_job(...)` 会把 pending/running 旧 job 转为 `failed` 并写入 `execution_state=superseded/superseded_by_job_id/superseded_at`；已 failed/partial_ready 的旧 job 只补 superseded metadata，ready job 不允许 supersede。
- `tests/unit/test_index_jobs.py` 从 8 个扩展到 15 个用例，覆盖未过期 lock blocked、过期 lock 可恢复、signature mismatch、resume_count、running metadata 更新、lock renew、supersede 以及老 `IndexJobRunner` 行为。

验收标准：

- job store 不依赖 CLI，可被 MCP ops 和测试复用。
- 老的 `IndexJobRunner` 测试继续通过。

### AR1-02 CLI 创建/恢复 index job

- 状态：`[x]`
- 优先级：`P0`
- 类型：`IMPL`、`TEST`
- 依赖：`AR0-03`、`AR1-01`
- 建议落点：`cli.py`

TODO：

- [x] `handle_index` 在执行前根据 resume policy 创建或恢复 job。
- [x] `KeyboardInterrupt` 时把 job 标记为 interrupted/failed，并释放 lock。
- [x] JSON final payload 增加 `job_id/status/resumed/plan_signature/tasks_*`。
- [x] text progress 首屏或最终摘要显示 job id 和 resumed 状态。
- [x] CLI 测试覆盖 `--resume auto`、`--restart`、`--no-resume`、JSON 输出。

行业实践调研结论：

- Google Cloud Storage resumable upload/gcloud 的模式是中断后重跑同一命令即可继续；本项目对应保留默认 `--resume auto`，减少长索引中断后的用户操作成本。
- Kubernetes Job 使用持久 Job 对象和 terminal condition 表达完成/失败；本项目对应每次 CLI index 都写入持久 `job_id/status/metadata`，Ctrl+C 在 SQLite 状态机中落 `failed`，并用 `metadata.execution_state=interrupted` 表达中断语义。
- AWS CLI 将机器可读 JSON 保持在 stdout，错误/进度走 stderr；本项目继续保证 `--format json` 只输出单个最终 payload，动态进度和中断摘要不污染 JSON。
- `argparse` 的互斥参数组用于把 `--resume`、`--restart`、`--no-resume` 的语义固定在 parser 层；AR1-02 继续沿用 AR0-03 的互斥契约。

完成记录：

- `active_knowledge_server/cli.py` 新增 `IndexJobContext` 和 CLI job 编排 helpers：incremental local 会先生成 plan/signature/task count，再按 `--resume auto`、`--resume JOB_ID`、`--restart`、`--no-resume` 创建或恢复 `SQLiteJobStore` 中的 index job。
- CLI index 执行期间会获取 `INDEX_JOB_LOCK_ID`，通过 progress callback 写入 `last_phase/last_path/global_*` 并 heartbeat lock；正常完成时推进到 `ready/partial_ready`，异常或 Ctrl+C 时 best-effort 标记 `failed` 并释放 lock。
- full index 暂无可恢复 plan signature，因此本轮只创建新的持久 job，不支持 `--resume JOB_ID` 续建；真正 full staging/resume 留给 R6。
- `IncrementalIndexPipeline.run(...)` 新增可选 `plan` 参数，使 CLI 能先 plan 再 job 化执行，避免为了 signature 扫描两遍。
- `tests/unit/test_cli.py` 增加/更新 JSON、`--no-resume --job-id`、中断后 `--resume auto`、`--restart` supersede 旧 job 的覆盖，并验证 jobs SQLite 中的状态和 metadata。

验收标准：

- 中断时用户能看到 job id。
- 不中断的普通 index 行为除新增 `job` payload 外保持兼容。

### AR1-03 Pipeline 接收 job context

- 状态：`[x]`
- 优先级：`P0`
- 类型：`IMPL`、`TEST`
- 依赖：`AR1-02`
- 建议落点：`indexing/pipeline.py`

TODO：

- [x] `IncrementalIndexPipeline.run(...)` 新增可选 `job_store`、`job_id`、`resume_policy` 或 `IndexRunContext`。
- [x] run 开始时写入 `plan_signature`、plan summary、task counts 到 job metadata。
- [x] 阶段切换时更新 `last_phase`。
- [x] task 处理时更新 `last_task_key`。
- [x] pipeline 单测使用 fake/in-memory job store 或临时 jobs SQLite 覆盖 metadata 更新。

行业实践调研结论：

- Apache Flink checkpoint 文档强调 checkpoint 要保存可恢复 state 和输入位置；本项目对应在 pipeline run 开始就持久化 `plan_signature`、plan summary 和 task counts，让恢复/观测先绑定到同一执行计划。
- Spark Structured Streaming 文档强调同一 checkpoint location 下 source/sink/schema 变更受限；本项目继续使用 AR0-01 的 `plan_signature` 作为 job context 的计划身份，避免把不同索引计划混为同一可恢复作业。
- Celery task state 文档支持长任务通过 custom state metadata 上报 `done/total`；本项目对应在 pipeline 阶段和 task 边界更新 `last_phase`、`last_task_key`、`tasks_applied/tasks_failed/tasks_skipped`。
- Kubernetes Job 以持久 Job status/conditions 表示 run-to-completion 状态；本项目让 pipeline 推进 running 状态到 `reporting`，终态 `ready/failed/partial_ready` 仍由 CLI/MCP 编排层收口。

完成记录：

- `active_knowledge_server/indexing/pipeline.py` 新增 `IndexRunContext` 和内部 `_PipelineJobReporter`，`IncrementalIndexPipeline.run(...)` 可接收 `run_context`；未传 context 时旧调用路径保持不变。
- run materialize plan 后会生成 deterministic task list，并写入 `plan_signature`、`plan_signature_payload`、`plan_summary`、`tasks_total`、`tasks_by_phase`、`tasks_by_source_kind`、`tasks_required` 和 `resume_policy` metadata。
- pipeline 进度事件会把主要阶段映射到 jobs SQLite running 状态：discover/plan -> `discovering`，collect -> `parsing`，metadata/profile/workspace apply -> `extracting`，vector apply -> `embedding`，done -> `reporting`。
- code/doc delete、code/doc apply、vector doc、profile relations、workspace map 的 task 边界会更新 `last_task_key`、`last_task`、`last_path` 与 task 计数。
- `tests/unit/test_incremental_pipeline.py` 新增临时 jobs SQLite 单测，覆盖传入 `IndexRunContext` 后 metadata 可查询。

验收标准：

- 不传 job context 时仍保持现有调用路径可用。
- 传 job context 时每个主要阶段都有可查询 job 状态。

### AR1-04 MCP ops 与 CLI job 语义对齐

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`AR1-01`、`AR1-02`
- 建议落点：`mcp/tools.py`

TODO：

- [x] `ops_start_index` 创建的 job metadata 与 CLI 创建的 job 字段一致。
- [x] `ops_index_status` 返回 task 级统计字段，暂无 task 表时从 checkpoint/KV 聚合。
- [x] `ops_cancel_index` 标记 cancel 后让后续 pipeline 检测到并停止未开始 task。
- [x] 预留 `ops_resume_index(job_id)` 工具或在 `ops_start_index` 支持 resume 参数。

行业实践调研结论：

- Spark Structured Streaming 把 checkpoint location 作为恢复边界，并限制同一 checkpoint 下 query/source/schema 变化；本项目对应让 MCP job metadata 沿用 CLI 的 `schema_version`、`requested_mode/target/source`、`resume_policy` 和后续 `plan_signature` 字段，保证不同入口解释同一 job 时不会分叉。参考：https://spark.apache.org/docs/3.5.7/structured-streaming-programming-guide.html
- Apache Flink checkpoint 依赖 durable storage 保存 state，并把 checkpoint 作为恢复 cut-off；本项目继续坚持 task `applied` checkpoint 后写，MCP status 在没有 task 表时从 `job_checkpoint` KV 聚合 task 统计。参考：https://nightlies.apache.org/flink/flink-docs-stable/docs/dev/datastream/fault-tolerance/checkpointing/
- Kubernetes Job suspend/resume 与 graceful termination 的语义是保留 Job 状态，由 controller/进程在边界协作停止；本项目对应把 `ops_cancel_index` 设计为协作式取消：metadata 标记 `cancelled=true`，runner/pipeline 在下一个 task 边界停止未开始 task，不破坏已提交事务。参考：https://kubernetes.io/docs/concepts/workloads/controllers/job/

完成记录：

- `active_knowledge_server/mcp/tools.py` 的 `ops_start_index` 新增 `resume="auto"|"disabled"` 参数，默认 auto；创建的 job metadata 写入 `schema_version=index_job_contract.v1`、`requested_mode`、`requested_target=overlay`、`requested_source`、`resume_policy`、`tasks_total/applied/skipped/failed` 等 CLI 同名字段，并把 `snapshot_id/profile_id` 写入 job record 主字段。
- 新增 `ops_resume_index(job_id)` gated ops 工具，支持把 `failed/partial_ready` 的非取消、非 superseded index job retry 回 `pending`，并写入 `resume_count` 与显式 `resume_policy.mode=job_id`。
- `ops_index_status` 的 job item 新增 `task_stats`，优先读 CLI metadata 字段，同时扫描 `job_checkpoint` 的 `task:collected:*` / `task:applied:*` KV，聚合 `checkpoint_counts`、`applied_by_phase`、`collected_by_phase`；payload 新增 `task_status_counts`。
- `SQLiteJobStore` 新增 `cancel_requested(job_id)`，`IndexJobRunner` 和 `IncrementalIndexPipeline` 的 task 边界会检测取消标记并停止未开始 task；已有 apply 事务不被回滚或篡改。
- `active_knowledge_server/mcp/schemas.py` 的 `OPS_TOOL_NAMES` 暴露 `ops_resume_index`。
- `tests/unit/test_mcp_ops_tools.py` 覆盖 MCP job metadata 对齐、checkpoint task stats 聚合、显式 resume；`tests/unit/test_index_jobs.py` 覆盖取消后 runner 不处理后续未开始文件。

验收标准：

- CLI 和 MCP 对同一 job 的状态解释一致。
- 取消 job 不破坏已提交 metadata。

---

## 7. Phase R2：任务级 checkpoint 与增量恢复

Phase R2 是本专项的核心交付：中断后能够跳过已成功 apply 的文件/文档/向量任务。

### AR2-01 选择 task ledger v1 存储形态

- 状态：`[x]`
- 优先级：`P0`
- 类型：`CONTRACT`、`IMPL`、`TEST`
- 依赖：`AR0-02`
- 建议落点：`indexing/jobs.py`、`storage/sqlite_store.py`

TODO：

- [x] 决策 v1 是否先复用 `job_checkpoint` KV，还是直接新增结构化 `index_task` 表。
- [x] 如果复用 KV，定义 key：`task:<status>:<task_key>`，value 为 JSON task state。
- [x] 确认 v1 不新增表，因此暂不补 migration、row encoder/decoder、maintenance 清理逻辑。
- [x] 实现 `get_task_state`、`set_task_state`、`list_task_states`。
- [x] 单测覆盖状态写入、覆盖更新、按 phase/status 查询。

行业实践调研结论：

- Airflow 把 task instance 表作为 task 运行状态的事实源，并依靠数据库事务避免多 scheduler 下的重复触发；这说明长期形态应是结构化 task 表，但 v1 不必为单机恢复立即引入新 schema。参考：https://airflow.apache.org/docs/apache-airflow/2.2.4/_api/airflow/models/taskinstance/index.html
- Prefect 将 flow/task run 的 state 作为可观测对象，并保留 state history；本项目 v1 只需要 latest checkpoint 来跳过已 applied task，历史状态可留到 `index_task` v2。参考：https://docs.prefect.io/v3/concepts/states
- Temporal 强调持久化运行状态以便失败后恢复/重放；本项目对应坚持 task state 写入 jobs DB，且 checkpoint 后写，未 checkpoint 的任务允许幂等重放。参考：https://temporal.io/
- SQLite JSON 支持适合在 v1 保存小型结构化 payload，但 JSONB 仍不是 O(1) lookup；若后续需要高频按 phase/status 查询、失败任务 retry queue 或 UI 分页，应迁移到结构化 `index_task` 表。参考：https://www.sqlite.org/json1.html

完成记录：

- v1 决策：先复用现有 jobs DB 的 `job_checkpoint` KV，不新增 `index_task` 表，不修改 `storage/sqlite_store.py` jobs schema，也不增加 migration/maintenance 清理面。
- task ledger key 固化为 `task:<status>:<task_key>`，当前 status 为 `collected|applied`；该形态与 AR0-04 已实现的 checkpoint key 保持兼容，避免把 `collected` 误当成可跳过边界。
- value 为 sorted JSON 编码的 `IndexTaskCheckpoint`，包含 `schema_version`、`status`、`task_key`、`phase`、`input_hash`、`task_schema_version`、`updated_at`、`metadata`。
- `active_knowledge_server/indexing/jobs.py` 新增 `get_task_state(...)`、`set_task_state(...)`、`list_task_states(...)`；`record_task_collected_checkpoint(...)` 和 `record_task_applied_checkpoint(...)` 改为复用 `set_task_state(...)`。
- `list_task_states(...)` v1 通过扫描单个 job 的 `job_checkpoint` KV 并在内存中过滤 `phase/status`；这是本地单机与每 job 小任务量下的保守实现。
- v2 迁移触发条件：需要跨 job 查询 task、MCP/CLI 展示大量 task 分页、失败 task retry queue、attempt/warning/record_counts 高频聚合、或 benchmark 显示 KV scan 成为瓶颈时，再新增结构化 `index_task` 表。

验收标准：

- task 状态能在进程重启后保留。
- task 状态不进入 metadata overlay DB，避免污染查询索引。

### AR2-02 实现 applied task 跳过

- 状态：`[x]`
- 优先级：`P0`
- 类型：`IMPL`、`TEST`
- 依赖：`AR2-01`
- 建议落点：`indexing/pipeline.py`

TODO：

- [x] pipeline materialize task list 后读取 task ledger。
- [x] 对 `status=applied` 且 `plan_signature/input_hash/schema` 匹配的 task 标记 skipped。
- [x] skipped task 进入 progress 和 final job stats。
- [x] changed/deleted path 的 collect list 过滤掉已 applied task 对应输入。
- [x] 单测覆盖恢复时只 collect 未完成路径。

行业实践调研结论：

- Spark Structured Streaming 要求同一 checkpoint 下 source/schema 等变更受限；本项目对应在 skip 判定时同时校验 job/checkpoint 的 `plan_signature`，避免不同 plan 复用旧 task checkpoint。参考：https://spark.apache.org/docs/latest/streaming/apis-on-dataframes-and-datasets.html
- Flink checkpoint 保存可恢复 state 和输入位置，失败后从 durable checkpoint 继续；本项目对应把 task ledger 作为 path/task 级恢复游标，已 checkpoint 的 task 在恢复时推进 progress 而不重新 collect/apply。参考：https://nightlies.apache.org/flink/flink-docs-stable/docs/dev/datastream/fault-tolerance/checkpointing/
- AWS Durable Execution 的重试实践强调 at-least-once 重放只适合幂等操作；本项目继续沿用 AR0-04 的“checkpoint 后写”边界，未 checkpoint 的 task 仍允许幂等重放，AR2-02 只跳过已确认 applied 的 task。参考：https://docs.aws.amazon.com/durable-execution/patterns/best-practices/idempotency/
- Airflow 将 `skipped` 作为 task instance 的显式状态；本项目对应把 skipped 计入 progress、job metadata 和最终 JSON payload，而不是静默过滤。参考：https://airflow.apache.org/docs/apache-airflow/2.10.3/core-concepts/tasks.html

完成记录：

- `active_knowledge_server/indexing/pipeline.py` 在 materialize task list 后读取 `job_checkpoint` KV ledger，并通过 `task:applied:<task_key>` 的 checkpoint payload 校验 `status/task_key/phase/input_hash/task_schema_version`；checkpoint metadata 中带 `plan_signature` 时必须与当前 plan 匹配，旧 checkpoint 没带签名时退回校验 job metadata 的 `plan_signature`。
- 新增 `_PipelineJobReporter.task_skipped(...)` 和 pipeline 本地 task stats；skipped task 会发出 `IndexProgressEvent(message="Skipping previously applied task")`，并写入 `tasks_skipped` job metadata。
- code/doc collect path 基于未完成任务过滤；code 额外扣除已 skipped 的 `code:apply:<path>` 输入，避免全局 collect dependency 把已 applied path 带回。
- `IncrementalIndexResult.metadata["tasks"]` 新增 `applied/skipped/failed` 统计；CLI 传入真实 `IndexRunContext`，final job payload 会从 jobs DB 同步最新 task counters，保证 JSON 中 `tasks_skipped` 不丢失。
- `tests/unit/test_incremental_pipeline.py` 新增手工 applied checkpoint 恢复用例：两个 code path 变化时，已 applied 的 `main.c` 不再进入 collect/apply，未完成的 `bt.c` 仍正常 apply，skipped 进入 result/job/progress。

验收标准：

- 手工构造 applied checkpoint 后重跑，不会重新 collect/apply 该 path。
- skipped 统计进入 JSON payload。

### AR2-03 code/doc apply 成功后 checkpoint

- 状态：`[x]`
- 优先级：`P0`
- 类型：`IMPL`、`TEST`
- 依赖：`AR2-01`
- 建议落点：`indexing/pipeline.py`

TODO：

- [x] code delete tombstone 成功后写 `code:delete:<path>` applied。
- [x] code changed bundle apply 成功后写 `code:apply:<path>` applied。
- [x] doc delete tombstone 成功后写 `doc:delete:<path>` applied。
- [x] doc changed bundle apply 成功后写 `doc:apply:<path>` applied。
- [x] checkpoint value 记录 record counts、warning codes、applied_at、job_id。

行业实践调研结论：

- Spring Batch chunk processing 把 read/process/write 聚合到事务边界，commit interval 到达后才提交；restartable job 会跳过已 `COMPLETED` 的 step。本项目对应把 code/doc task checkpoint 放在 writer transaction 成功返回之后，只跳过已 `applied` task。参考：https://docs.spring.io/spring-batch/reference/step/chunk-oriented-processing/commit-interval.html 与 https://docs.spring.io/spring-batch/reference/step/chunk-oriented-processing/restart.html
- Spark Structured Streaming 对同一 checkpoint location 下的 source/sink/schema 变更有限制；本项目在 checkpoint metadata 中写入 `plan_signature`，继续防止不同 plan 复用旧 applied task。参考：https://spark.apache.org/docs/latest/streaming/apis-on-dataframes-and-datasets.html
- Flink checkpoint 是可恢复 state 的 point-in-time snapshot；本项目用 task ledger 模拟文件级 offset，未 checkpoint 的 task 允许幂等重放，已 checkpoint 的 task 才可恢复跳过。参考：https://nightlies.apache.org/flink/flink-docs-release-1.20/docs/concepts/stateful-stream-processing/
- OpenSearch bulk refresh 明确区分“写入已完成”和“搜索可见”；本项目对应只把 metadata writer transaction 成功作为 code/doc `applied` 边界，不把 collect 完成或后续 flush/refresh 文案当作 skip 边界。参考：https://docs.opensearch.org/latest/api-reference/document-apis/bulk/

完成记录：

- `active_knowledge_server/indexing/pipeline.py` 的 code/doc apply 循环在 `_tombstone_deleted_path(...)`、`_apply_code_bundle(...)`、`_apply_doc_bundle(...)` 正常返回后调用 `record_task_applied_checkpoint(...)`，写入 `task:applied:<task_key>`。
- checkpoint metadata 包含 `job_id`、`plan_signature`、`applied_at`、`operation`、`source_kind`、`relative_path`、可选 `storage_relative_path`、`record_counts` 和 `warning_codes`；不包含源码、正文或大对象。
- `mark_task_applied(...)` 只有在传入 checkpoint metadata 时才落 ledger；AR2-04 已为 vector task 在 vector writer flush 成功后补写 `vector:doc:*` applied。
- `tests/unit/test_incremental_pipeline.py` 新增 code apply/delete、doc apply/delete 成功 checkpoint 覆盖，并验证 code apply 抛错时不会写 applied checkpoint。

验收标准：

- checkpoint 只在 writer transaction 成功之后出现。
- apply 抛错时 task 不被标记 applied。

### AR2-04 vector task checkpoint

- 状态：`[x]`
- 优先级：`P0`
- 类型：`IMPL`、`TEST`
- 依赖：`AR2-03`
- 建议落点：`pipeline.py`、`storage/lancedb_store.py`

TODO：

- [x] 每个 doc vector upsert 成功后写 `vector:doc:<path>` applied。
- [x] vector apply 跳过时不重复写 vector payload。
- [x] vector ref 与 vector payload 校验失败时，该 vector task 保持 failed/pending。
- [x] 单测覆盖 vector task 已 applied 时只重写 metadata 或完全跳过的策略。

行业实践调研结论：

- LanceDB 的 `merge_insert` 支持 matched update + unmatched insert 形成 upsert 语义；本项目对应继续用稳定 `vector_ref_id` 让重复 vector upsert 收敛，并在 payload 写后读回校验通过后才提交 metadata ref。参考：https://docs.lancedb.com/tables/update
- Qdrant point loading 明确强调同 ID 重复上传是幂等覆盖；本项目对应在 vector task skip 时完全不重复写 payload，未 checkpoint 的重放仍依赖稳定 ID 覆盖而不是 append。参考：https://qdrant.tech/documentation/manage-data/points/
- Flink checkpoint 用 durable state + 可重放 source 恢复到已完成 checkpoint；本项目对应把 `task:applied:vector:doc:<path>` 作为“已提交 vector payload/ref”的恢复游标，checkpoint 仍然后写。参考：https://nightlies.apache.org/flink/flink-docs-stable/docs/dev/datastream/fault-tolerance/checkpointing/
- Spark Structured Streaming 从 checkpoint location 恢复 progress/state，同时限制 source/schema 等恢复兼容性；本项目继续复用 plan signature 校验，避免 embedding model 或 manifest 变化时误用旧 vector checkpoint。参考：https://spark.apache.org/docs/3.5.6/structured-streaming-programming-guide.html

完成记录：

- `active_knowledge_server/indexing/pipeline.py` 对 doc vector apply 记录 pending checkpoint metadata；`vector_writer.flush()` 成功后才写 `task:applied:vector:doc:<path>`，metadata 包含 `job_id`、`plan_signature`、`applied_at`、`relative_path`、`storage_relative_path`、`record_counts`、`embedding_models` 和 `vector_ref_ids`。
- 已 applied 的 vector task 会进入 skipped 统计并发出 skipped progress event；对应 doc path 不再进入 `doc_paths_to_collect`，因此不会再次调用 vector writer 写 payload。
- `active_knowledge_server/storage/lancedb_store.py` 在 fallback vector collection 写入后立即读回校验 payload row；校验失败会抛错，pipeline 将该 vector task 计为 failed 且不写 applied checkpoint。
- `active_knowledge_server/storage/validation.py` 扩展 vector ref/payload 双向一致性检查：metadata ref 缺 payload 继续报告 `storage.vector_ref_missing`，payload 缺 metadata ref 报告 `storage.vector_payload_orphan`，字段不一致报告 `storage.vector_ref_payload_mismatch`。
- `tests/unit/test_incremental_pipeline.py` 覆盖 successful vector checkpoint、已 applied vector task 不重复写 payload、vector 写入失败不 checkpoint。
- `tests/unit/test_lancedb_store.py` 覆盖 payload 写后读回校验失败时不写 metadata ref。
- `tests/unit/test_storage_validation.py` 覆盖 missing payload、orphan payload、ref/payload mismatch。

验收标准：

- rebuild vectors 中断后重跑能跳过已成功 vectorized 文档。
- `validate` 能发现 vector_ref 悬挂。

### AR2-05 中断/崩溃恢复测试

- 状态：`[x]`
- 优先级：`P0`
- 类型：`TEST`
- 依赖：`AR2-02`、`AR2-03`、`AR2-04`

TODO：

- [x] 增加 pipeline 测试：第 N 个 code apply 后抛出 `KeyboardInterrupt`，重跑跳过前 N 个 task。
- [x] 增加 pipeline 测试：apply 成功但 checkpoint 前抛错，重跑重复 apply 后结果不重复。
- [x] 增加 subprocess 集成测试：SIGTERM 后重跑恢复并 `validate`。
- [x] 增加 JSON payload 断言：`resumed=true`、`tasks_skipped > 0`。

行业实践调研结论：

- Spring Batch 的 restart 语义把“已完成 step 跳过”和“未提交 chunk 重放”区分开；本项目对应分别覆盖“checkpoint 后中断会 skip”和“checkpoint 前中断会 replay”。参考：https://docs.spring.io/spring-batch/reference/step/chunk-oriented-processing/restart.html 与 https://docs.spring.io/spring-batch/reference/step/chunk-oriented-processing.html
- AWS Durable Execution 将默认重试语义定义为 at-least-once，并明确要求外部写入必须幂等；本项目对应为 `apply 成功但 checkpoint 前中断` 增加重放测试，验证稳定 ID/upsert 能把重复 apply 收敛。参考：https://docs.aws.amazon.com/durable-execution/patterns/best-practices/idempotency/
- Spark Structured Streaming 要求恢复后沿用同一 checkpoint 身份；本项目对应在 subprocess 恢复测试里复用同一 `job_id/plan_signature`，并断言最终 JSON `resumed=true`、`tasks_skipped>0`。参考：https://spark.apache.org/docs/3.5.0/structured-streaming-programming-guide.html

完成记录：

- `tests/unit/test_incremental_pipeline.py` 新增两条恢复测试：一条在第 2 个 code task checkpoint 后抛出 `KeyboardInterrupt`，重跑后跳过已 checkpoint task；另一条在 `_apply_code_bundle(...)` 成功返回前注入 `KeyboardInterrupt`，验证未 checkpoint task 会重放且逻辑对象不重复。
- `tests/unit/test_cli.py` 新增子进程集成测试：通过 `sitecustomize.py` 在首个 task checkpoint 后制造可观测停顿，向 `active-kb index --format json` 发送 `SIGTERM`，随后 `--resume auto` 恢复并执行 `validate --format json`。
- `active_knowledge_server/cli.py` 增加仅对 `index` 命令生效的 `SIGTERM -> KeyboardInterrupt` 翻译上下文，使现有 interrupted job 标记、lock 释放和 JSON 中断 payload 能覆盖进程终止场景，而不需要为测试手工篡改 lock TTL。

验收标准：

- 恢复后逻辑对象集合与一次性完整跑等价。
- crash/resume 测试默认可控，不进入特别慢路径。

### AR2-06 index_state 保存策略校验

- 状态：`[x]`
- 优先级：`P1`
- 类型：`TEST`、`IMPL`
- 依赖：`AR2-05`

TODO：

- [x] 确认 partial/interrupted job 不写 `index_state.json`。
- [x] ready job 才保存当前 state。
- [x] resume partial job ready 后保存 state。
- [x] 测试 state 与 task ledger 不一致时以 task ledger 恢复、以 ready 状态收敛。

行业实践调研结论：

- Spring Batch 的 `ExecutionContext` 在 step commit 点持久化，restart 语义依赖“只从已提交边界恢复”；本项目对应 `index_state.json` 只在整次 incremental run 收敛为 ready 后推进，partial/interrupted 继续保留旧 baseline。参考：https://docs.spring.io/spring-batch/reference/step/chunk-oriented-processing/configuring.html 与 https://docs.spring.io/spring-batch/reference/step/chunk-oriented-processing/restart.html
- Spark Structured Streaming 把 checkpoint/commit log 作为恢复真相源，已完成 micro-batch 才推进可恢复进度；本项目对应 task ledger 比旧 `index_state.json` 更优先，恢复时先按 applied checkpoint skip，直到 run 成功再发布新的 diff baseline。参考：https://spark.apache.org/docs/3.5.0/structured-streaming-programming-guide.html
- Kafka consumer 将 committed position 定义为“进程失败后恢复到的位置”，并强调提交的是下一条将处理的 offset；本项目对应 `index_state.json` 只能表示“下一轮 diff 应从哪里开始”，不能在未完成 run 时提前推进。参考：https://kafka.apache.org/42/javadoc/org/apache/kafka/clients/consumer/KafkaConsumer.html

完成记录：

- `active_knowledge_server/indexing/pipeline.py` 的 `save_state(...)` 改为同目录临时文件写入后原子替换，避免 ready 态发布 baseline 时把旧 `index_state.json` 部分覆盖。
- `tests/unit/test_incremental_pipeline.py` 新增 ready run 保存当前 state 的覆盖，并扩展 interrupted/resume 测试：中断前 state 保持旧 baseline，resume 成功后 state 收敛到 `plan.current_state`。
- 现有 `test_incremental_pipeline_returns_partial_ready_when_doc_increment_fails(...)` 继续覆盖 partial_ready 不推进 state；新增断言共同证明 `index_state.json` 只代表“上一次完整成功状态”。

验收标准：

- `index_state.json` 仍然只代表完整成功状态。

---

## 8. Phase R3：Incremental Apply 批处理与诊断

Phase R3 把当前“每个文件一个 transaction”的 incremental apply 进一步批量化，并保持可定位失败路径。

### AR3-01 引入 ApplyBatch 抽象

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`AR2-03`
- 建议落点：`indexing/pipeline.py` 或 `indexing/apply.py`

TODO：

- [x] 定义 `ApplyBatch`，包含 task key、path、old bundle、new bundle、operation、vector writes。
- [x] 按 `max_files_per_transaction`、`max_records_per_transaction`、`commit_interval_ms` 切分 batch。
- [x] 保留确定性排序。
- [x] batch 内 transaction 成功后批量 checkpoint。

完成记录：

- `active_knowledge_server/indexing/pipeline.py` 新增 `ApplyBatchItem`、`ApplyBatch` 和批处理执行 helper，把 code/doc delete、code/doc apply、doc vector apply 统一纳入 batch 抽象。
- metadata apply 改为“单 writer + 单 transaction 批量提交 + 成功后逐 task checkpoint”，保留现有 `_apply_code_bundle` / `_apply_doc_bundle` 入口，避免破坏失败注入和幂等重放测试。
- result metadata 新增 `apply_batches.by_phase` 统计，输出每个 phase 的 batch 数、item 数、record 数及批次峰值。
- 新增 `max_files_per_transaction`、`max_records_per_transaction` 配置字段支撑当前实现；字段默认值和校验已落地，benchmark 契约补充仍放在 `AR3-02`。
- 单测覆盖批次切分规则、code incremental 的 batch metadata，以及 `max_files_per_transaction=1` 与 batched apply 的逻辑结果等价。

验收标准：

- batch apply 与单 path apply 输出等价。
- batch 统计进入 result metadata。

### AR3-02 配置化 apply batch 边界

- 状态：`[x]`
- 优先级：`P1`
- 类型：`CONTRACT`、`IMPL`、`TEST`
- 依赖：`AR3-01`
- 建议落点：`config/schema.py`、`config/defaults.py`

TODO：

- [x] 新增 `indexing.writer.max_files_per_transaction`。
- [x] 新增 `indexing.writer.max_records_per_transaction`。
- [x] 默认值保守设置，允许 `1` 回退到近似旧行为。
- [x] benchmark 记录实际 batch 配置。

完成记录：

- `config/schema.py`、`config/defaults.py` 已暴露 `max_files_per_transaction` / `max_records_per_transaction`，默认值保持 `64 / 2048`，并允许设置为 `1` 快速回退到近似单文件 apply。
- `scripts/benchmark_index.py` 新增 writer transaction 边界 sweep 参数，并把实际 writer 配置与 `apply_batches` metadata 一并写入 benchmark JSONL。
- `eval/index_benchmark.py` 将 writer transaction 边界纳入 scenario key、Markdown 汇总和推荐逻辑，避免 benchmark 只按 `batch_size` / `commit_interval_ms` 归并。
- 示例配置与单测已同步覆盖新字段和兼容回填逻辑。

验收标准：

- 配置校验拒绝非正数。
- 回退配置能快速定位 batch 相关问题。

### AR3-03 batch 失败降级

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`AR3-01`

TODO：

- [x] batch apply 失败后自动二分或降级为单 task apply。
- [x] 单 task 失败写入 task failed，其他 task 继续。
- [x] warning details 只包含 path/error，不包含源码内容。
- [x] 失败 task 不写 applied checkpoint。

行业实践调研结论：

- Spring Batch 的 chunk writer 失败默认回滚当前事务，配合 skip/fault-tolerant 配置可以把坏记录隔离出来、让其他记录继续；本项目对应保留“批事务先回滚，再二分到单 task 定位坏文件”的策略，避免把半批写入误记为成功。参考：https://docs.spring.io/spring-batch/reference/5.1/step/chunk-oriented-processing/controlling-rollback.html 与 https://docs.spring.io/spring-batch/reference/step/chunk-oriented-processing/configuring-skip.html
- Elasticsearch Bulk API 即使整体 `errors=true`，也会返回按提交顺序排列的 item 级结果；本项目对应在 batch 失败后收敛到 task 级 success/failure，而不是只给一个 phase 级笼统报错，便于 resume 时只重放失败项。参考：https://www.elastic.co/guide/en/elasticsearch/reference/8.19/docs-bulk.html
- MongoDB `ordered: false` 的 bulk write 会在单条失败后继续处理剩余操作；本项目对应“单 task 失败不阻塞后续 batch/task”，最终 job 收口为 `partial_ready`，而不是整个 apply phase fail-fast。参考：https://www.mongodb.com/docs/manual/core/bulk-write-operations/index.html

完成记录：

- `active_knowledge_server/indexing/pipeline.py` 为 metadata apply 和 vector apply 引入统一的 batch 降级路径：先按 `ApplyBatch` 执行，失败后递归二分，最终退到单 task apply。
- 单 task 最终失败时，pipeline 会调用 `task_failed` 更新 job 进度统计，继续执行后续 task，并为失败项追加 `index.code_apply_failed` / `index.doc_apply_failed` / `index.vector_apply_failed` warning。
- 新 warning 的 `details` 固定为 `{path, error}`，不再携带源码或文档正文；失败 task 也不会写 `task:applied:*` checkpoint，因此后续 `--resume auto` / retry 仍会重试该任务。
- `tests/unit/test_incremental_pipeline.py` 新增 code/doc/vector 三类“同批一坏一好”的覆盖，验证好 task 仍会 applied、坏 task 无 applied checkpoint、最终结果为 `partial_ready`。

验收标准：

- 一个坏文件不会阻塞同批其他文件最终 applied。
- job 结果为 `partial_ready`，可重试失败 task。

### AR3-04 `created_by_job` 改真实 job id

- 状态：`[x]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`AR1-03`

TODO：

- [x] `_tombstone_deleted_path`、`_diff_and_mark_stale`、`_tombstone_object` 接收 `job_id`。
- [x] tombstone/replacement 的 `created_by_job` 从 `job:incremental_index` 改为真实 `job_id`。
- [x] 无 job context 时保留兼容 fallback。
- [x] 测试覆盖审计字段。

行业实践调研结论：

- Algolia 的异步索引写操作会返回唯一 `taskId`，并要求后续等待、串联依赖和排障都围绕这个真实任务 ID 进行；本项目对应不再把 overlay 审计写成固定 phase 常量，而是把 tombstone/replacement 挂到真实 `job_id`。参考：https://www.algolia.com/doc/guides/sending-and-managing-data/send-and-update-your-data/in-depth/index-operations-are-asynchronous/ 与 https://www.algolia.com/doc/api-reference/api-methods/wait-task
- Elasticsearch 的 reindex 管理 API 明确要求任务在迁移后仍保留原始 task ID，避免调用方看到重复或断裂的执行链；本项目对应让一次 incremental run 的所有补偿写入共享同一个 `job_id`，方便恢复、清理和追责。参考：https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-list-reindex
- Azure AI Search 会按单次 indexer run 保留 execution history、开始结束时间、错误和 warning；本项目对应支持按一个真实 `job_id` 反查这次增量构建产生的 tombstone/replacement，而不是只能看到“某类操作产生过变更”。参考：https://learn.microsoft.com/en-us/azure/search/search-monitor-indexers

完成记录：

- `active_knowledge_server/indexing/pipeline.py` 已把 `job_id` 透传到 `_tombstone_deleted_path`、`_diff_and_mark_stale`、`_tombstone_object` 与 profile relation rebuild 路径，tombstone/replacement 现在优先写入真实 `run_context.job_id`。
- 新增集中 fallback 常量，未提供 job context 时仍回退到 `job:incremental_index`，保证旧调用与无作业上下文场景兼容。
- `tests/unit/test_incremental_pipeline.py` 已覆盖两类审计断言：有 job context 时 replacement / tombstone 的 `created_by_job` 等于真实 `job_id`；无 job context 时保持 fallback 值。

验收标准：

- 可以按 job id 追踪一次增量构建产生的 tombstone/replacement。

---

## 9. Phase R4：观测、Benchmark 与发布 Gate

Phase R4 是 R1-R3 的发布门禁，重点验证“恢复真的省时间”和“不会破坏一致性”。

### AR4-01 扩展 benchmark phase timing

- 状态：`[x]`
- 优先级：`P1`
- 类型：`OPS`、`TEST`
- 依赖：`AR1-03`
- 建议落点：`scripts/benchmark_index.py`、`eval/index_benchmark.py`

TODO：

- [x] 基于 progress events 聚合 phase timing。
- [x] 记录 discover/code_collect/code_apply/doc_collect/doc_apply/vector_apply/profile_relations/workspace_map 耗时。
- [x] 记录 task stats：total/applied/skipped/failed/replayed。
- [x] 汇总报告展示恢复前后 wall time 和 replay overhead。

行业实践调研结论：

- Spark Structured Streaming 的 `StreamingQueryProgress.durationMs` 把单次运行拆成可归因的阶段耗时；本项目对应把 benchmark 原始记录补齐 `phase_timings`，而不是只看总 wall time。
- Flink checkpoint/recovery 观测同时强调 checkpoint duration、restart/recovery time 和 reprocess 成本；本项目对应在 task stats 中单列 `replayed`，避免恢复收益被“总耗时下降”掩盖。
- Elasticsearch indexing tuning 建议把 bulk/indexing 与 refresh 可见性成本分开观察；本项目对应继续保留 writer/parser/embedding 内部 timings，同时新增基于 progress events 的 phase wall time，区分“阶段总耗时”和“阶段内部写入耗时”。

完成记录：

- `active-knowledge-server/scripts/benchmark_index.py` 改为复用真实 index 命令路径，并在每个 sample 上通过 progress callback 聚合 `phase_timings`、`phase_event_counts`、`observed_phases`、`task_stats` 和 `job`/resume 元数据，写入 benchmark JSONL。
- `active-knowledge-server/src/active_knowledge_server/eval/index_benchmark.py` 新增 progress phase timing collector，把运行中的 `vectors_apply` 规范化为报告口径里的 `vector_apply`；scenario summary 现会聚合 phase timing、task stats，并在 Markdown 报告里展示 bottleneck phases 与 resume replay overhead。
- `active-knowledge-server/src/active_knowledge_server/indexing/jobs.py` / `pipeline.py` 新增 task attempt telemetry；恢复执行时如果某个 task 曾开始但没有 applied checkpoint，会在本次结果里计入 `tasks.replayed`，供 benchmark/report 消费。
- 单测已覆盖 phase timing 聚合、resume 报表渲染、task attempt round-trip，以及“中断发生在 apply 成功但 checkpoint 前”时恢复 run 的 `tasks.replayed` 统计。

验证命令：

- `cd active-knowledge-server && uv run python -m pytest tests/unit/test_index_benchmark.py tests/unit/test_index_jobs.py tests/unit/test_incremental_pipeline.py`
- `cd active-knowledge-server && python -m py_compile scripts/benchmark_index.py src/active_knowledge_server/eval/index_benchmark.py src/active_knowledge_server/indexing/jobs.py src/active_knowledge_server/indexing/pipeline.py`

验收标准：

- benchmark JSONL 可解释瓶颈阶段。
- 报告能看出 resume 是否接近“只跑剩余任务”。

### AR4-02 crash/resume benchmark

- 状态：`[~]`
- 优先级：`P1`
- 类型：`OPS`、`TEST`
- 依赖：`AR2-05`

TODO：

- [x] benchmark 增加 `--interrupt-after-task-percent` crash/resume harness。
- [ ] 跑 30%、70%、90% 中断点恢复耗时。
- [x] 记录 replay task 数、skipped task 数、validate 结果。
- [x] 输出恢复收益报告。

行业实践调研结论：

- Spring Batch 官方建议先“测真实作业，再决定是否引入更复杂并行/恢复策略”；本项目对应优先用真实工程仓 `ZeppOS` 跑 crash/resume benchmark，而不是只看 synthetic fixture。参考：https://docs.spring.io/spring-batch/reference/scalability.html
- Spring Batch restart 语义区分“已完成 step 直接跳过”和“需要重跑的未完成 step”；本项目对应在报告里显式记录 `tasks_skipped` 与 `tasks_replayed`，避免只看总耗时误判恢复质量。参考：https://docs.spring.io/spring-batch/reference/step/chunk-oriented-processing/restart.html
- Spark Structured Streaming 的 checkpoint 恢复依赖同一 checkpoint 身份，并要求把恢复后耗时与剩余工作量一起看；本项目对应在 crash/resume 报告中增加 `interrupt.after_task_percent`、`expected_remaining_wall_seconds` 与 `resume_vs_expected_remaining_ratio`。参考：https://spark.apache.org/docs/latest/streaming/apis-on-dataframes-and-datasets.html
- Flink checkpoint 调优文档强调 checkpoint 时延会受真实背压/大状态影响，不能只看“能恢复”，还要看恢复/检查点开销是否吞噬吞吐；本项目对应把 `resume/remainder ratio`、`replay_overhead` 和 `validate_status` 一起进报告。参考：https://nightlies.apache.org/flink/flink-docs-stable/docs/ops/state/large_state_tuning/

完成记录：

- `active-knowledge-server/scripts/benchmark_index.py` 新增 `--interrupt-after-task-percent 30,70,90`，在同一 scenario 下会先跑 fresh baseline，再对每个中断百分比跑“受控中断 + `--resume auto` 恢复 + `validate --strict --format json`”。
- benchmark 现会把 sample workdir 的 `runtime.baseline_dir/local_dir` 与 `storage.{metadata,overlay,jobs,vector,vector_delta,artifacts_root,local_artifacts_root,cache_root}` 全部重定向到 bench root，避免误写默认 `.active-kb`。
- benchmark JSONL 升级为 `index_benchmark_record.v3`：新增 `interrupt`（中断百分比、中断耗时、恢复耗时、理论剩余耗时、`resume_vs_expected_remaining_ratio`）和 `validate`（`status`、`warning_count`、`error_check_count`、`storage_status`、`index_result_status`）字段。
- `active-knowledge-server/src/active_knowledge_server/eval/index_benchmark.py` 会把不同中断百分比当成独立 scenario 汇总，并在 Markdown `Resume Summary` 中展示 `Interrupt`、`Expected remaining`、`Resume/remaining`、`Replay overhead`、`Validate`。
- `active-knowledge-server/tests/unit/test_index_benchmark.py` 已覆盖新的中断百分比分组、恢复摘要渲染和 validate 状态展示。
- `ZeppOS` 真实工程 smoke 方案已验证可生成真实 task 规模：
  - 复制真实仓代表性模块子集（例如 `components/gui` + `configs` + `build/.config`）到临时 workspace 后，fresh 样本可生成约 `815` 个 task。
  - 更大的代表性子集（`components/gui` + `components/hal` + `components/fs` + `application` + `core` + `configs`）可生成约 `1395` 个 task。
  - 更接近整仓的代表性子集（`components` + `core` + `application` + `configs`）可生成约 `4263` 个 task。
  - 这些 smoke 证明 harness 已经接入真实工程；30/70/90 的完整耗时结果仍需单独长跑回填。

推荐实测命令：

- 真实工程代表性模块子集：
  - `rm -rf /tmp/zeppos-bench-workspace && mkdir -p /tmp/zeppos-bench-workspace/components /tmp/zeppos-bench-workspace/build/out_hub`
  - `cp -a /home/gangan/ZeppOS/components/gui /tmp/zeppos-bench-workspace/components/`
  - `cp -a /home/gangan/ZeppOS/configs /tmp/zeppos-bench-workspace/`
  - `cp -a /home/gangan/ZeppOS/build/.config /tmp/zeppos-bench-workspace/build/`
  - `cp -a /home/gangan/ZeppOS/build/out_hub/.config /tmp/zeppos-bench-workspace/build/out_hub/`
  - `cd active-knowledge-server && uv run python scripts/benchmark_index.py --config ../examples/local-single-user.yaml --workspace /tmp/zeppos-bench-workspace --source code --workers auto --repeat 1 --cache-mode cold --interrupt-after-task-percent 30,70,90 --bench-root /tmp/active-kb-ar4-02-zeppos --output /tmp/active-kb-ar4-02-zeppos/records.jsonl --summary-output /tmp/active-kb-ar4-02-zeppos/report.md`
- 更接近整仓的长跑方案：
  - `cd active-knowledge-server && uv run python scripts/benchmark_index.py --config ../examples/local-single-user.yaml --workspace /home/gangan/ZeppOS --source code --workers auto --repeat 1 --cache-mode cold --interrupt-after-task-percent 30,70,90 --bench-root /tmp/active-kb-ar4-02-zeppos-full --output /tmp/active-kb-ar4-02-zeppos-full/records.jsonl --summary-output /tmp/active-kb-ar4-02-zeppos-full/report.md`

下一步建议：

- 先用 `components/gui + configs + build/.config` 这组约 `815` task 的真实子集跑通 30/70/90，确认 `Resume/remaining` 在 70% 点接近 `1.0x` 且 `validate_status=ok`。
- 通过后再把同一命令切到更大的 `ZeppOS` 代表性子集，最后再决定是否需要整仓全量长跑作为 release gate 数据。

验收标准：

- 70% 后中断恢复，总耗时接近剩余 30% 加少量重放开销。

### AR4-03 恢复一致性验收

- 状态：`[~]`
- 优先级：`P1`
- 类型：`TEST`
- 依赖：`AR2-05`、`AR3-03`

TODO：

- [x] 新增可复用验收 harness：`active-knowledge-server/scripts/check_resume_consistency.py`。
- [x] harness 固定对比 `workers=1` 的 fresh run 与 `workers=auto` 的 crash/resume run。
- [x] 报告比较 `file/chunk/entity/relation/evidence/vector_ref` 的 live logical digest 与 sample diff。
- [x] 报告内纳入 `validate --strict --format json` 与 `status --format json` 的结果，并检查 `resumed=true`、`tasks_skipped > 0`、latest job/task 统计。
- [ ] 用真实工程回填一份通过报告；优先 `ZeppOS` 代表性子集，整仓长跑作为 release gate 附件。

行业实践调研结论：

- Spring Batch 官方建议先测“真实作业”再决定是否继续引入更复杂的并行/恢复策略；本项目对应先对 `ZeppOS` 真实代码子集做 fresh vs resume 一致性验收，而不是只比较 synthetic fixture。参考：https://docs.spring.io/spring-batch/reference/scalability.html
- Spark Structured Streaming 要求从同一 checkpoint 恢复时保持输入源/状态 schema 的恢复边界稳定；本项目对应让 AR4-03 固定比较同一 workspace、同一 plan signature 下的 fresh 与 resume 输出，而不是跨配置/跨 schema 混比。参考：https://spark.apache.org/docs/latest/streaming/apis-on-dataframes-and-datasets.html
- Flink task-local recovery 明确“本地副本可以不是字节级相同，但主 checkpoint 的逻辑状态必须完整且可恢复”；本项目对应比较 live logical object 集合与 `validate` 结果，而不是要求 fresh/resume 两次产物文件完全 byte-identical。参考：https://nightlies.apache.org/flink/flink-docs-stable/docs/ops/state/large_state_tuning/

实现记录：

- `active-knowledge-server/src/active_knowledge_server/eval/index_consistency.py` 新增 live collection canonical digest 与 diff helper；对比口径固定为 `file`、`chunk`、`entity`、`relation`、`evidence`、`vector_ref`，并忽略 `created_at/updated_at/freshness_ts` 这类运行时噪声字段。
- `active-knowledge-server/scripts/check_resume_consistency.py` 会在两个独立 workdir 中分别执行：
  - fresh：`workers=1`、`resume disabled`
  - resumed：先受控中断，再 `--resume auto`
- 报告 schema 为 `index_resume_consistency_report.v1`，输出：
  - fresh / resumed 两侧的 `collections.{name}.count/digest`
  - `comparison.collections.{name}` 的 `equal`、`fresh_only_sample`、`resumed_only_sample`、`changed_sample`
  - `validate` 摘要、`status.latest_job` 摘要、`resume_job_check`
- 单测 `active-knowledge-server/tests/unit/test_index_consistency.py` 已覆盖 digest 稳定性、diff sample 和标准 collection 排序；相关 benchmark 单测继续通过。

推荐实测命令：

- `ZeppOS` 代表性 GUI 子集：
  - `rm -rf /tmp/zeppos-ar4-03-gui && mkdir -p /tmp/zeppos-ar4-03-gui/components /tmp/zeppos-ar4-03-gui/configs /tmp/zeppos-ar4-03-gui/build/out_hub`
  - `cp -a /home/gangan/ZeppOS/components/gui /tmp/zeppos-ar4-03-gui/components/`
  - `cp -a /home/gangan/ZeppOS/configs/mhs003 /tmp/zeppos-ar4-03-gui/configs/`
  - `cp -a /home/gangan/ZeppOS/build/.config /tmp/zeppos-ar4-03-gui/build/`
  - `cp -a /home/gangan/ZeppOS/build/out_hub/.config /tmp/zeppos-ar4-03-gui/build/out_hub/`
  - `cd active-knowledge-server && PYTHONPATH=src .venv/bin/python scripts/check_resume_consistency.py --config ../examples/local-single-user.yaml --workspace /tmp/zeppos-ar4-03-gui --source code --bench-root /tmp/active-kb-ar4-03-gui --output /tmp/active-kb-ar4-03-gui/report.json`
- 更小但更快的 storyboard 子集：
  - `rm -rf /tmp/zeppos-ar4-03-storyboard && mkdir -p /tmp/zeppos-ar4-03-storyboard/components/gui/storyboard /tmp/zeppos-ar4-03-storyboard/configs /tmp/zeppos-ar4-03-storyboard/build/out_hub`
  - `cp -a /home/gangan/ZeppOS/components/gui/storyboard/src /tmp/zeppos-ar4-03-storyboard/components/gui/storyboard/`
  - `cp -a /home/gangan/ZeppOS/components/gui/storyboard/include /tmp/zeppos-ar4-03-storyboard/components/gui/storyboard/`
  - `cp -a /home/gangan/ZeppOS/configs/mhs003 /tmp/zeppos-ar4-03-storyboard/configs/`
  - `cp -a /home/gangan/ZeppOS/build/.config /tmp/zeppos-ar4-03-storyboard/build/`
  - `cp -a /home/gangan/ZeppOS/build/out_hub/.config /tmp/zeppos-ar4-03-storyboard/build/out_hub/`
  - `cd active-knowledge-server && PYTHONPATH=src .venv/bin/python scripts/check_resume_consistency.py --config ../examples/local-single-user.yaml --workspace /tmp/zeppos-ar4-03-storyboard --source code --bench-root /tmp/active-kb-ar4-03-storyboard --output /tmp/active-kb-ar4-03-storyboard/report.json`

验收标准：

- 允许时间戳和 job metadata 不同，逻辑对象集合必须等价。

### AR4-04 发布文档与本地手测脚本

- 状态：`[x]`
- 优先级：`P1`
- 类型：`DOC`、`TEST`
- 依赖：`AR4-03`

TODO：

- [x] 更新 `active_knowledge_server_local_full_integration_test.md`，加入 resume 测试流程。
- [x] 增加手测命令：启动 index、Ctrl+C、重跑、观察 `resumed=true`。
- [x] 增加 CI/本地慢测说明，避免 crash 测试默认拖慢普通单测。

行业实践调研结论：

- Spring Batch 官方建议先测真实作业，再决定是否继续引入更复杂的并行或恢复策略；本项目对应在发布文档里默认推荐真实工程 `/home/gangan/ZeppOS`，而不是只给 synthetic fixture。参考：https://docs.spring.io/spring-batch/reference/scalability.html
- Spring Batch restart 语义里，已经成功完成的 step 在重启时应被跳过，只重跑未完成部分；本项目对应要求手测结果明确看到 `resumed=true` 和 `tasks_skipped > 0`，而不是只看“第二次也成功了”。参考：https://docs.spring.io/spring-batch/reference/step/chunk-oriented-processing/restart.html
- Spark Structured Streaming 从同一 checkpoint 恢复时要求输入源和状态边界保持稳定；本项目对应把手测文档固定在“同一 workspace + 同一 workdir + 同一 job/checkpoint 身份”下复跑，不建议中途改 workspace 或切换 schema。参考：https://spark.apache.org/docs/latest/streaming/apis-on-dataframes-and-datasets.html
- Flink 的恢复模型强调 checkpoint 间隔需要在“运行期开销”和“故障后重放量”之间取平衡；本项目对应把 crash/resume smoke 保持为独立慢测，而不是默认塞进所有单测。参考：https://nightlies.apache.org/flink/flink-docs-stable/docs/concepts/stateful-stream-processing/

完成记录：

- [`doc/active_knowledge_server_local_full_integration_test.md`](./active_knowledge_server_local_full_integration_test.md) 新增 `2.1 可恢复索引 smoke`：补齐隔离 workdir 变量、真实 `ZeppOS` 手测命令、`Ctrl+C -> --resume auto` 的复现步骤，以及 `resumed=true` / `tasks_skipped > 0` 的验收方法。
- 新增 [`active-knowledge-server/scripts/manual_resume_smoke.py`](../active-knowledge-server/scripts/manual_resume_smoke.py)：自动执行“一次受控中断 + 一次 resume + validate/status 校验”，默认输出 `index_resume_smoke_report.v1` JSON 报告，适合作为本地 slow smoke 或独立 CI lane。
- 文档已明确这类 crash/resume 验收不要进入默认 `pytest`，而应通过单独脚本运行，避免拖慢普通单测与日常开发回路。

推荐实测命令：

- 真实工程手工复现：
  - 先按 [`active_knowledge_server_local_full_integration_test.md`](./active_knowledge_server_local_full_integration_test.md) 的 `2.1 可恢复索引 smoke` 生成隔离 `RESUME_CONFIG=/tmp/active-kb-ar4-04-resume-smoke/resume-smoke.yaml`
  - `cd active-knowledge-server && uv run active-kb index --config /tmp/active-kb-ar4-04-resume-smoke/resume-smoke.yaml --incremental --source code --no-resume --job-id index:manual-resume-smoke --format json | tee /tmp/active-kb-ar4-04-resume-smoke/first-run.json`
  - 在出现 applied task 后按 `Ctrl+C`
  - `cd active-knowledge-server && uv run active-kb index --config /tmp/active-kb-ar4-04-resume-smoke/resume-smoke.yaml --incremental --source code --resume auto --format json | tee /tmp/active-kb-ar4-04-resume-smoke/resume-run.json`
- 真实工程自动 smoke：
  - `cd active-knowledge-server && uv run python scripts/manual_resume_smoke.py --config ../examples/local-single-user.yaml --workspace /home/gangan/ZeppOS --workdir /tmp/active-kb-ar4-04-resume-smoke --source code --job-id index:manual-resume-smoke --clean --output /tmp/active-kb-ar4-04-resume-smoke/report.json`

CI/本地慢测说明：

- 默认单测继续只覆盖 `tests/unit/` 里的快速契约，不把真实 workspace crash/resume 验收塞进 `pytest` 默认路径。
- 本地开发建议把 `manual_resume_smoke.py`、`check_resume_consistency.py` 和 `benchmark_index.py --interrupt-after-task-percent ...` 视为独立 slow smoke，按需单独执行。
- 如果后续需要接 CI，建议新增单独 job 或 nightly lane，使用真实 `ZeppOS` 或代表性子集，并把产物落到 `/tmp/active-kb-*` 一类隔离 workdir。

验收标准：

- 用户可以按文档复现断点续建。

---

## 10. Phase R5：Collect Artifact 与 Embedding/Vector Cache

Phase R5 是进一步减少重启后重复解析和重复 embedding 的优化。R1-R4 未完成前不建议抢跑。

### AR5-01 collect artifact cache v1

- 状态：`[x]`
- 优先级：`P2`
- 类型：`IMPL`、`TEST`
- 依赖：`AR2-01`
- 建议落点：`indexing/artifacts.py`

TODO：

- [x] 定义 artifact root：`.active-kb/local/artifacts/index-jobs/<job_id>/collect/`。
- [x] 支持 code/doc collect result JSON-safe 编解码。
- [x] 写入 artifact_hash，恢复读取时校验。
- [x] artifact 失败或 schema mismatch 时自动重 collect。
- [x] 清理策略接入 maintenance。

行业实践调研结论：

- Gradle Build Cache 强调 cache key 必须覆盖完整 inputs/outputs，遗漏输入会导致错误命中；本项目对应把 collect artifact 绑定 `plan_signature`、result schema version 和 collect path 集合，命中条件不满足就直接重 collect。参考：https://docs.gradle.org/current/userguide/build_cache.html 与 https://docs.gradle.org/current/userguide/build_cache_concepts.html
- Bazel Remote Cache 把 action metadata 和内容哈希分开存储；本项目对应为 collect artifact 单独写 `artifact_hash`，恢复时先校验 hash，再决定是否复用落盘结果。参考：https://bazel.build/remote/caching
- Spring Batch restart 要求可恢复上下文中的值可序列化，否则恢复能力不可靠；本项目对应不直接持久化 Python 临时对象，而是新增 code/doc collect result 的 JSON-safe codec。参考：https://docs.spring.io/spring-batch/docs/5.0.5-SNAPSHOT/reference/reference/html/index-single.html
- Turborepo 将 cache 建立在任务结果可重复、可确定的前提上；本项目对应只在 schema 与计划签名匹配时命中，否则宁可 miss 也不冒险复用旧 artifact。参考：https://turborepo.dev/docs/crafting-your-repository/caching

完成记录：

- 新增 `active_knowledge_server/indexing/artifacts.py`，定义 `.active-kb/local/artifacts/index-jobs/<job_id>/collect/{code,docs}.json`、`artifact_hash` 校验和 code/doc collect result codec。
- `active_knowledge_server/indexing/pipeline.py` 在 code/doc collect 阶段接入 artifact hit/store：resume 时优先加载 artifact；hash 不匹配、schema 不匹配、缺少期望 path 时自动回退到重 collect；成功落盘后为依赖 task 写 `collected` checkpoint metadata。
- `active_knowledge_server/storage/maintenance.py` 的 `clean --old-jobs` 现在会同步清理 `local/artifacts/index-jobs/<job_id>/`。
- `tests/unit/test_incremental_pipeline.py` 新增 codec round-trip 与“collect 完成、apply 前中断，resume 不重 collect”的覆盖；`tests/unit/test_storage_maintenance.py` 增加 job artifact 目录清理断言。
- 真实工程验证：`ZeppOS` 整仓在 `manual_resume_smoke.py` 的默认 120 秒首 checkpoint 窗口内未完成首个 applied checkpoint；改用 `ZeppOS` 真实 storyboard 代表性子集后，resume job metadata 已观察到 `last_message=\"Reusing collected code artifact\"`，且产出 `collect/code.json`（296 paths / 296 task keys）与 296 条 `task:collected:*` checkpoint，证明 artifact cache 命中链路在真实工程上可工作。

验收标准：

- collect 完成 apply 前中断，恢复时可复用 artifact，不重 parse。

### AR5-02 embedding input/cache

- 状态：`[x]`
- 优先级：`P2`
- 类型：`IMPL`、`TEST`
- 依赖：`AR2-04`

TODO：

- [x] 定义 embedding cache key：model + object_type + content_hash + sanitizer version。
- [x] 缓存 accepted/skipped secret scan 结果。
- [x] 命中时直接生成 vector write。
- [x] provider/local embedding 都经过同一 batcher。

行业实践调研结论：

- Gradle Build Cache 强调 cache key 必须覆盖任务实现、输出属性以及“task inputs 的名称和值”；本项目对应把 embedding cache key 固定为 `model + object_type + content_hash + sanitizer_version`，并额外用 provider 做目录级 namespace，避免同名 model 跨 provider 误命中。参考：https://docs.gradle.org/current/userguide/build_cache_concepts.html
- Turborepo 明确要求被缓存任务是 deterministic；本项目对应只缓存“同一内容、同一模型、同一 sanitizer 版本”的 embedding/secret-scan 结果，并把重复内容折叠到单次计算，命中时直接恢复既有结果而不是重跑。参考：https://turborepo.dev/docs/crafting-your-repository/caching
- OpenAI Embeddings API 支持“一个请求传数组输入”，且官方 rate-limit 建议在共享请求层做 exponential backoff；本项目对应把 provider/local 都收敛到同一个 batcher，按 `indexing.embeddings.batch_size` 统一聚合，后续接远端 provider 时可直接在该层补 retry/backoff。参考：https://developers.openai.com/api/reference/resources/embeddings/methods/create 与 https://help.openai.com/en/articles/6891753-what-are-the-best-practices-for-managing-my-rate-limits-in-the-api

完成记录：

- 新增 `active_knowledge_server/indexing/embedding_cache.py`，实现持久化 `IndexEmbeddingCacheStore`、稳定 cache key、secret-scan skip 结果编码，以及共享的 `prepare_cached_embeddings(...)` batcher。
- `active_knowledge_server/indexing/doc_indexer.py` 的 embedding 准备阶段改为统一经过 cache + batcher：命中 cache 时直接复用 embedding/skip decision，miss 时才进入 secret scan 和 embedding 计算。
- vector write 构造改为“先解出 embedding，再组装 `VectorRefRecord`”，因此 cache hit 可以直接生成 `VectorWrite`，不再重复调用 `embed_text(...)`。
- `IndexedDocuments.metadata` 新增 `embedding_cache` 统计，输出 `cache_hits/cache_misses/cache_stores/deduplicated_inputs/batch_count/computed_embeddings/skipped_inputs`，便于 benchmark 与真实工程复盘。
- `tests/unit/test_doc_indexer.py` 新增重复 collect 命中 cache、secret-scan skip 结果复用两组单测；同时回归 `tests/unit/test_incremental_pipeline.py`，确认增量 pipeline 与现有 vector checkpoint 语义不回退。

真实工程验证：

- 采用 `/home/gangan/ZeppOS/docs` 的 14 个 Markdown 文档作为真实样本；因 source-docs connector 需要顶层分类目录，测试时把这些文件复制到临时 `source-docs/engineering/` 下运行 `DocumentIndexer.collect()`。
- 实测样本共 14 个文档、707 个 chunk、704 个唯一 embedding cache 文件（说明有 3 个重复内容 chunk 在单次 collect 内被折叠复用）。
- 冷启动首轮：`cache_hits=0`、`cache_misses=707`、`cache_stores=704`、`computed_embeddings=704`、`batch_count=22`、`embedding_seconds=0.200234`。
- 热启动第二轮：`cache_hits=707`、`cache_misses=0`、`cache_stores=0`、`computed_embeddings=0`、`batch_count=0`、`embedding_seconds=0.067296`，embedding 阶段耗时下降约 `66.4%`。

验收标准：

- docs 未变但 vector rebuild 被触发时，可复用缓存减少 embedding 计算。

### AR5-03 vector delta segment 与 compaction

- 状态：`[x]`
- 优先级：`P2`
- 类型：`IMPL`、`TEST`
- 依赖：`AR2-04`

TODO：

- [x] 评估当前 JSON collection 全量读写热点。
- [x] 设计 append-only delta segment：`vectors/<object_type>/<job_id>-part-N.jsonl`。
- [x] query reader 合并 base + delta 或 compaction 后读取。
- [x] compaction 成功后 checkpoint，失败不影响已提交 metadata。

行业实践调研结论：

- Apache Hudi 的 Merge-On-Read 把新写入先追加到 log file，再由 compaction 合并回 base file，用更低写放大换取可控的读放大；本项目对应把 overlay vector payload 改成 append-only segment，reader 负责合并，flush 后按阈值做 compaction。参考：https://hudi.apache.org/docs/compaction/ 与 https://hudi.apache.org/docs/concepts/
- Qdrant 明确要求 point upsert 幂等、同 ID 重传覆盖旧值；本项目继续沿用稳定 `vector_ref_id`，segment 合并时按 `vector_ref_id` 取最后一条，保证重复 apply 或 resume replay 可以收敛。参考：https://qdrant.tech/documentation/manage-data/points/
- Weaviate 批量导入建议把 ingestion 与 indexing 解耦，必要时启用 async indexing；本项目对应把“payload append 成功”与“compaction 成功”拆成两个边界，task applied checkpoint 只绑定 payload/ref 提交，compaction 只写 manifest checkpoint，不反向影响已提交 metadata。参考：https://docs.weaviate.io/weaviate/manage-objects/import
- LanceDB 的 `merge_insert` / consistency 文档强调 upsert 与版本可见性分离；本地 fallback store 虽不直接使用 Lance table API，但继续保持“写后校验 payload，再提交 metadata ref，再由 reader 按 manifest 合并”的顺序，减少跨进程可见性歧义。参考：https://docs.lancedb.com/tables/update 与 https://docs.lancedb.com/tables/consistency

完成记录：

- `active_knowledge_server/storage/base.py` 为 `StorageWriteRequest` 增加可选 `job_id`，让 vector writer 能按 job 生成 segment 文件名。
- `active_knowledge_server/indexing/pipeline.py` 在创建 overlay vector writer 时传入真实 `job_id`，segment 文件落盘格式为 `vectors/<object_type>/<job_id>-part-N.jsonl`。
- `active_knowledge_server/storage/lancedb_store.py` 把 overlay `upsert_vectors(...)` 从“整 collection 读出、替换、整文件写回”改成 append-only segment 写入；baseline publish 仍保留 compacted collection 语义。
- fallback reader 现在会合并 `chunk.json` + `chunk/*.jsonl`，并额外校验 metadata `vector_ref` 是否存在且与 payload 一致，避免 payload 先落盘时出现 orphan 向量被误召回。
- manifest schema 升级为 `1.1.0`；collection metadata 新增 `segment_count`、`segment_row_count`、`segments` 和 `last_compaction`。其中 `last_compaction` 作为本地 compaction checkpoint，记录 `compacted_at`、`requested_by_job_id`、合并的 segment 数量和最终 row 数。
- `vector_writer.flush()` 现在只做 best-effort compaction：达到阈值时把当前 live rows compact 到 `<object_type>.json`，然后清空 segment 目录；compaction 失败不会回滚已提交 payload/ref，也不会阻断已写 task checkpoint。
- `tests/unit/test_lancedb_store.py` 新增覆盖：
  - overlay upsert 写 segment 而不是重写 compacted collection；
  - 多 segment flush 后触发 compaction，并把 checkpoint 写入 manifest；
  - orphan payload 在缺少 metadata `vector_ref` 时不会被 query reader 返回。
- `tests/unit/test_incremental_pipeline.py` 的向量读取 helper 已更新为读取 merged vector rows，确保 pipeline 回归测试覆盖新格式。

真实工程验证：

- 使用 `/home/gangan/ZeppOS` 作为真实 workspace；由于 source-docs connector 需要顶层分类目录，测试时把 ZeppOS `components/` 下 60 个真实 Markdown/HTML 文档映射到临时 `source-docs/engineering/components/...` 样本树后执行增量 docs 索引。
- 默认 writer 批次配置下，实测 `59` 个成功文档触发 `413` 条 chunk vectors，overlay vector store 最终只生成 `chunk/index-...-part-000001.jsonl` 与 `manifest.json`，没有重写 `chunk.json`；manifest 记录 `segment_count=1`、`segment_row_count=413`。
- 为了验证“多 batch 不重复整库重写”，把 writer 收紧到 `max_files_per_transaction=4`、`max_records_per_transaction=64` 后再次跑同一批 ZeppOS 文档，`vectors_apply` 被拆成 `20` 个 batch、`826` 条向量写入记录，最终 manifest 记录：
  - `last_compaction.segment_count = 20`
  - `last_compaction.segment_row_count = 413`
  - `last_compaction.row_count = 413`
  - `segment_count = 0`
  - `row_count = 413`
- 这说明真实工程样本下已经形成“多段 append -> 最终单次 compaction”的写路径，而不是每个 batch 都反复重写整个 collection。
- 两次 ZeppOS 实测都存在 1 个 `package.html` 文档 collect partial，因此结果状态为 `partial_ready`；但成功文档的 metadata/vector apply 和 compaction 路径都已完整执行，不影响对 `AR5-03` 的验证。

验收标准：

- 大批量 vector upsert 不再随 batch 次数反复重写整个 collection。

---

## 11. Phase R6：Full Index Staging Publish

Phase R6 用于解决全量索引中断后污染 live target 的问题。它改动面大，建议在增量 resume 稳定后推进。

### AR6-01 设计 staging storage resolver

- 状态：`[~]`
- 优先级：`P2`
- 类型：`CONTRACT`、`IMPL`、`TEST`
- 依赖：`AR1-03`

TODO：

- [x] 为 full local/baseline 生成与 live path 同目录的 staging path：
  - baseline metadata：`metadata.staging.<job_token>.db`
  - local metadata：`overlay.staging.<job_token>.db`
  - baseline vectors：`lancedb.staging.<job_token>/`
  - local vectors：`lancedb-delta.staging.<job_token>/`
- [ ] writer/reader 可在 staging target 上工作。
- [x] staging path 写入 job metadata 与 `index --format json` 的 `job.staging_storage`。
- [x] 中断后同 job 恢复 staging path。

行业实践调研结论：

- Apache Iceberg 使用“先写新 metadata，再原子切换 metadata pointer”的提交模型；对本项目最有价值的启发不是固定文件名，而是“staging 物理产物 + 原子 pointer swap”这两个边界要分离。参考：https://iceberg.apache.org/spec/ 与 https://iceberg.apache.org/docs/1.5.1/reliability/
- SQLite 官方文档说明 WAL checkpoint 与 truncate 是独立操作，且 `TRUNCATE` 只有在 reader 清空后才能把 WAL 收回到 0 字节；因此 metadata publish 前必须把 staging DB 的 checkpoint/truncate 作为显式步骤，而不是假设 close 后总能自动收敛。参考：https://sqlite.org/wal.html 与 https://sqlite.org/pragma.html#pragma_wal_checkpoint
- Python `os.replace()` 文档保证在同一文件系统内提供覆盖式 rename 语义；因此 metadata DB 的 publish 可以用 `os.replace`，但 vector 目录不能依赖目录替换的原子性，仍应走 versioned path + pointer manifest。参考：https://docs.python.org/3/library/os.html#os.replace

完成记录：

- `active_knowledge_server/storage/staging.py` 新增 staging resolver，基于当前配置的 live metadata/vector path 和稳定 `job_token` 推导 staging path，而不是硬编码固定目录名。
- `job_token` 规则为：`filesystem-safe(job_id) + short sha1`。这样 `index:full-stage-smoke` 之类的 job id 在 Linux/macOS/Windows 上都能映射到安全文件名，同时保持同 job 可恢复、不同 job 隔离。
- `prepare_nonresumable_index_job(...)` 现在会在 full job 创建后把 `staging_storage` 写入 jobs DB metadata；`build_index_job_payload(...)` 也会把这部分内容透传到最终 JSON，便于人工手测和后续 pipeline 接线。
- `tests/unit/test_storage_staging.py` 覆盖了 baseline/local 两类 target 的命名规则、同 job 的确定性恢复以及 full job metadata/payload 持久化。

真实工程建议：

- `AR6-01` 优先用 `/home/gangan/ZeppOS` 做 resolver smoke，而不是 synthetic fixture，因为它能同时覆盖真实 `local-single-user.yaml`、真实 workdir 覆盖和真实 job id 约束。
- 本轮建议先验证 “ZeppOS 配置 -> staging path 推导 -> jobs metadata 落盘” 这条链路；真正把 full build 切到 staging writer/reader 执行，再放到 `AR6-02` 一起做 publish 前验证。

验收标准：

- 当前设计/contract 验收：同一 full job 的 staging path 可稳定复现，并可由 CLI/job metadata 暴露给后续执行器使用。
- full build 中断不会修改 live metadata/vector path。

### AR6-02 validate 后 publish pointer

- 状态：`[x]`
- 优先级：`P2`
- 类型：`IMPL`、`TEST`
- 依赖：`AR6-01`

TODO：

- [x] staging build 完成后运行 critical validation。
- [x] SQLite WAL checkpoint/truncate 并关闭连接。
- [x] metadata DB 使用 versioned DB + publish manifest pointer 切换。
- [x] vector directory 使用 versioned path + pointer manifest，避免目录替换非原子。
- [x] publish 成功后 job ready，失败后 live 仍指向旧版本。

验收标准：

- publish 前崩溃不影响旧 live index。
- publish 后 query 使用新版本。

实现备注：

- full local/baseline build 现在真正写入 staging storage，而不是只在 jobs metadata 里记录 staging path。
- publish 改成两段式：先把 staging metadata/vector 物化到 versioned artifact；只有最后一步才原子替换 `*.publish.json` pointer，因此 publish 前崩溃不会污染旧 live。
- live reader/writer 通过 publish manifest 解析当前 metadata/vector 版本；staging path resolver 继续固定使用配置 anchor，不受当前 live pointer 干扰。
- 本地单测已覆盖“materialize 前后 live 不切换”和“staging resolver 忽略 active pointer”。
- 真实工程 smoke：
  - `/home/gangan/ZeppOS` 整仓 full code 已验证能进入真实 staging job 链路，并生成稳定 staging path。
  - 基于 `/home/gangan/ZeppOS/components/gui/storyboard` 的真实代表性子集，在补齐 baseline metadata 后复跑 `local full code`，结果 `validation.status=ok`、`publish.status=published`、job `status=ready`，且正式配置解析到新的 versioned live path。

### AR6-03 旧 staging/live 版本清理

- 状态：`[x]`
- 优先级：`P2`
- 类型：`OPS`、`IMPL`、`TEST`
- 依赖：`AR6-02`

TODO：

- [x] maintenance 支持清理 superseded/failed staging job。
- [x] 保留最近 N 个 live 版本。
- [x] 清理前确认不删除当前 pointer 指向版本。

行业实践调研结论：

- Kubernetes Deployment 通过 `revisionHistoryLimit` 控制历史 ReplicaSet 保留数量；本项目对应把 full publish 产物作为可回滚版本，提供 `--old-live-versions --keep N`，避免 versioned metadata/vector 无限增长。参考：https://kubernetes.io/docs/concepts/workloads/controllers/deployment/
- Google Artifact Registry cleanup policy 支持 delete policy、keep policy 和 keep most recent versions；本项目对应采用“按最近 N 个 token 保留 + 当前 pointer 强制保留”的组合，保证清理不会删除正在服务的版本。参考：https://docs.cloud.google.com/artifact-registry/docs/repositories/cleanup-policy-overview
- Docker prune 支持 `until` / `label` 等过滤条件，而不是无差别删除；本项目对应只清理 jobs metadata 明确记录的 full-index staging path，并校验 staging 文件名确实由 live anchor 派生，避免 maintenance 变成泛化删除器。参考：https://docs.docker.com/reference/cli/docker/image/prune/

完成记录：

- `active_knowledge_server/storage/maintenance.py` 新增 `clean_stale_staging_jobs(...)` 和 `clean_old_published_versions(...)`，并接入 `clean_local_state(...)`。
- `active-kb clean` 新增 `--staging-jobs` 与 `--old-live-versions`；`--keep N` 复用为 live 版本保留数量。
- stale staging 清理范围限定为 terminal `failed/partial_ready` index job 的 `staging_storage`，并同时清理 SQLite staging DB、`-wal`、`-shm` 以及 staging vector dir。
- live version 清理扫描 `metadata.db.versions/` 与 `lancedb*.versions/`，按 token 聚合 metadata/vector 版本；无论 mtime 是否较老，当前 `*.publish.json` pointer 指向的 `publish_token` 都会被保留。
- `storage/publish.py` 抽出 version dir/path helper 与 `current_publish_token(...)`，避免 maintenance 重复拼路径。
- `tests/unit/test_storage_maintenance.py` 覆盖 failed full-index staging 清理，以及“当前 pointer 指向旧版本但仍不可删除”的保留策略。

验证命令：

- `cd active-knowledge-server && uv run python -m pytest tests/unit/test_storage_maintenance.py tests/unit/test_storage_staging.py`
- `cd active-knowledge-server && python -m py_compile src/active_knowledge_server/storage/maintenance.py src/active_knowledge_server/storage/publish.py src/active_knowledge_server/cli.py`

推荐 ZeppOS 实测命令：

- 已有多次 full publish 后清理旧 live 版本：
  - `cd active-knowledge-server && uv run active-kb clean --config /tmp/active-kb-ar6-01-staging/staging-smoke.yaml --old-live-versions --keep 2 --format json`
- 曾经中断或 supersede full staging job 后清理 staging 残留：
  - `cd active-knowledge-server && uv run active-kb clean --config /tmp/active-kb-ar6-01-staging/staging-smoke.yaml --staging-jobs --format json`
- 更贴近真实工程的回归路径：先沿用 `/home/gangan/ZeppOS` 或其 `components/gui/storyboard` 子集执行两次 `local full code` publish，再执行 `--old-live-versions --keep 1`；验收重点是最新 publish 可查询，当前 pointer token 未被清理，旧非 pointer token 被回收。

验收标准：

- 长期本地使用不会无限堆积 staging 数据。

---

## 12. Phase R7：压测后增强项

### AR7-01 process/hybrid code collect

- 状态：`[ ]`
- 优先级：`P3`
- 类型：`IMPL`、`TEST`
- 依赖：`IP5-01`、`AR4-01`

TODO：

- [ ] 只有当 phase timing 证明 code parse CPU bound 时才实现。
- [ ] 给 code collect 输入输出增加 pickle 契约测试。
- [ ] 禁止 process worker 内调用 executor/future。
- [ ] 支持 `indexing.parallel.mode: thread | process | hybrid`。

验收标准：

- process/hybrid 在中/大仓稳定优于 thread，且输出集合等价。

### AR7-02 WAL 默认策略固化

- 状态：`[ ]`
- 优先级：`P3`
- 类型：`OPS`、`CONTRACT`
- 依赖：`IP3-04`、`AR4-01`

TODO：

- [ ] 对 local FS 场景压测 `delete/full`、`wal/full`、`wal/normal`。
- [ ] 记录 query 并发读 p50/p95。
- [ ] 记录 WAL 膨胀与 checkpoint busy。
- [ ] 有数据支撑后再决定是否调整默认值。

验收标准：

- 没有报告不得默认启用 WAL。

### AR7-03 auto workers 默认值二次固化

- 状态：`[ ]`
- 优先级：`P3`
- 类型：`OPS`、`IMPL`
- 依赖：`IP4-03`、`AR4-01`

TODO：

- [ ] 基于小/中/大仓 phase timing 调整 `resolve_indexing_workers`。
- [ ] 根据 RSS 风险设置大仓 worker cap。
- [ ] 记录推荐值和不推荐区间。

验收标准：

- 默认 workers 有数据支撑，且可通过 `workers=1` 回退。

---

## 13. 推荐实施顺序

1. 完成 `AR0-01` 至 `AR0-04`，锁定 plan signature、task key、resume policy 和 checkpoint 安全边界。
2. 完成 `AR1-01` 至 `AR1-03`，让 CLI 与主 pipeline 真正创建/恢复 job。
3. 完成 `AR2-01` 至 `AR2-04`，实现任务级 checkpoint 和跳过已 applied task。
4. 完成 `AR2-05`、`AR2-06`，用中断/崩溃测试证明恢复语义。
5. 完成 `AR3-01` 至 `AR3-04`，把 incremental apply 批处理和真实 job id 审计补上。
6. 完成 `AR4-01` 至 `AR4-04`，作为断点续建发布 gate。
7. 根据 benchmark 结果再推进 `AR5`、`AR6`、`AR7`。

---

## 14. 第一批建议排期

| 批次 | 任务 | 目标 | 风险 |
| --- | --- | --- | --- |
| Batch R-A | `AR0-01`、`AR0-02`、`AR0-03`、`AR0-04` | 固定恢复契约和 CLI 参数 | 低 |
| Batch R-B | `AR1-01`、`AR1-02`、`AR1-03` | 主 pipeline job 化 | 中 |
| Batch R-C | `AR2-01`、`AR2-02`、`AR2-03`、`AR2-04` | 增量任务级 checkpoint | 中高 |
| Batch R-D | `AR2-05`、`AR2-06`、`AR4-03` | 崩溃恢复一致性验收 | 中 |
| Batch R-E | `AR3-01`、`AR3-02`、`AR3-03`、`AR3-04` | apply 批处理和诊断 | 中高 |
| Batch R-F | `AR4-01`、`AR4-02`、`AR4-04` | benchmark、手测和发布文档 | 中 |

推荐先开 Batch R-A 和 R-B。它们能把“恢复”从概念变成可观察的 job 生命周期；R-C 再把真正节省时间的 task checkpoint 接上。

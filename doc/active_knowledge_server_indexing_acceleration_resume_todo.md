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

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`AR1-01`、`AR1-02`
- 建议落点：`mcp/tools.py`

TODO：

- [ ] `ops_start_index` 创建的 job metadata 与 CLI 创建的 job 字段一致。
- [ ] `ops_index_status` 返回 task 级统计字段，暂无 task 表时从 checkpoint/KV 聚合。
- [ ] `ops_cancel_index` 标记 cancel 后让后续 pipeline 检测到并停止未开始 task。
- [ ] 预留 `ops_resume_index(job_id)` 工具或在 `ops_start_index` 支持 resume 参数。

验收标准：

- CLI 和 MCP 对同一 job 的状态解释一致。
- 取消 job 不破坏已提交 metadata。

---

## 7. Phase R2：任务级 checkpoint 与增量恢复

Phase R2 是本专项的核心交付：中断后能够跳过已成功 apply 的文件/文档/向量任务。

### AR2-01 选择 task ledger v1 存储形态

- 状态：`[ ]`
- 优先级：`P0`
- 类型：`CONTRACT`、`IMPL`、`TEST`
- 依赖：`AR0-02`
- 建议落点：`indexing/jobs.py`、`storage/sqlite_store.py`

TODO：

- [ ] 决策 v1 是否先复用 `job_checkpoint` KV，还是直接新增结构化 `index_task` 表。
- [ ] 如果复用 KV，定义 key：`task:<task_key>`，value 为 JSON task state。
- [ ] 如果新增表，补 migration、row encoder/decoder、maintenance 清理逻辑。
- [ ] 实现 `get_task_state`、`set_task_state`、`list_task_states`。
- [ ] 单测覆盖状态写入、覆盖更新、按 phase/status 查询。

验收标准：

- task 状态能在进程重启后保留。
- task 状态不进入 metadata overlay DB，避免污染查询索引。

### AR2-02 实现 applied task 跳过

- 状态：`[ ]`
- 优先级：`P0`
- 类型：`IMPL`、`TEST`
- 依赖：`AR2-01`
- 建议落点：`indexing/pipeline.py`

TODO：

- [ ] pipeline materialize task list 后读取 task ledger。
- [ ] 对 `status=applied` 且 `plan_signature/input_hash/schema` 匹配的 task 标记 skipped。
- [ ] skipped task 进入 progress 和 final job stats。
- [ ] changed/deleted path 的 collect list 过滤掉已 applied task 对应输入。
- [ ] 单测覆盖恢复时只 collect 未完成路径。

验收标准：

- 手工构造 applied checkpoint 后重跑，不会重新 collect/apply 该 path。
- skipped 统计进入 JSON payload。

### AR2-03 code/doc apply 成功后 checkpoint

- 状态：`[ ]`
- 优先级：`P0`
- 类型：`IMPL`、`TEST`
- 依赖：`AR2-01`
- 建议落点：`indexing/pipeline.py`

TODO：

- [ ] code delete tombstone 成功后写 `code:delete:<path>` applied。
- [ ] code changed bundle apply 成功后写 `code:apply:<path>` applied。
- [ ] doc delete tombstone 成功后写 `doc:delete:<path>` applied。
- [ ] doc changed bundle apply 成功后写 `doc:apply:<path>` applied。
- [ ] checkpoint value 记录 record counts、warning codes、applied_at、job_id。

验收标准：

- checkpoint 只在 writer transaction 成功之后出现。
- apply 抛错时 task 不被标记 applied。

### AR2-04 vector task checkpoint

- 状态：`[ ]`
- 优先级：`P0`
- 类型：`IMPL`、`TEST`
- 依赖：`AR2-03`
- 建议落点：`pipeline.py`、`storage/lancedb_store.py`

TODO：

- [ ] 每个 doc vector upsert 成功后写 `vector:doc:<path>` applied。
- [ ] vector apply 跳过时不重复写 vector payload。
- [ ] vector ref 与 vector payload 校验失败时，该 vector task 保持 failed/pending。
- [ ] 单测覆盖 vector task 已 applied 时只重写 metadata 或完全跳过的策略。

验收标准：

- rebuild vectors 中断后重跑能跳过已成功 vectorized 文档。
- `validate` 能发现 vector_ref 悬挂。

### AR2-05 中断/崩溃恢复测试

- 状态：`[ ]`
- 优先级：`P0`
- 类型：`TEST`
- 依赖：`AR2-02`、`AR2-03`、`AR2-04`

TODO：

- [ ] 增加 pipeline 测试：第 N 个 code apply 后抛出 `KeyboardInterrupt`，重跑跳过前 N 个 task。
- [ ] 增加 pipeline 测试：apply 成功但 checkpoint 前抛错，重跑重复 apply 后结果不重复。
- [ ] 增加 subprocess 集成测试：SIGTERM 后重跑恢复并 `validate`。
- [ ] 增加 JSON payload 断言：`resumed=true`、`tasks_skipped > 0`。

验收标准：

- 恢复后逻辑对象集合与一次性完整跑等价。
- crash/resume 测试默认可控，不进入特别慢路径。

### AR2-06 index_state 保存策略校验

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`TEST`、`IMPL`
- 依赖：`AR2-05`

TODO：

- [ ] 确认 partial/interrupted job 不写 `index_state.json`。
- [ ] ready job 才保存当前 state。
- [ ] resume partial job ready 后保存 state。
- [ ] 测试 state 与 task ledger 不一致时以 task ledger 恢复、以 ready 状态收敛。

验收标准：

- `index_state.json` 仍然只代表完整成功状态。

---

## 8. Phase R3：Incremental Apply 批处理与诊断

Phase R3 把当前“每个文件一个 transaction”的 incremental apply 进一步批量化，并保持可定位失败路径。

### AR3-01 引入 ApplyBatch 抽象

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`AR2-03`
- 建议落点：`indexing/pipeline.py` 或 `indexing/apply.py`

TODO：

- [ ] 定义 `ApplyBatch`，包含 task key、path、old bundle、new bundle、operation、vector writes。
- [ ] 按 `max_files_per_transaction`、`max_records_per_transaction`、`commit_interval_ms` 切分 batch。
- [ ] 保留确定性排序。
- [ ] batch 内 transaction 成功后批量 checkpoint。

验收标准：

- batch apply 与单 path apply 输出等价。
- batch 统计进入 result metadata。

### AR3-02 配置化 apply batch 边界

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`CONTRACT`、`IMPL`、`TEST`
- 依赖：`AR3-01`
- 建议落点：`config/schema.py`、`config/defaults.py`

TODO：

- [ ] 新增 `indexing.writer.max_files_per_transaction`。
- [ ] 新增 `indexing.writer.max_records_per_transaction`。
- [ ] 默认值保守设置，允许 `1` 回退到近似旧行为。
- [ ] benchmark 记录实际 batch 配置。

验收标准：

- 配置校验拒绝非正数。
- 回退配置能快速定位 batch 相关问题。

### AR3-03 batch 失败降级

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`AR3-01`

TODO：

- [ ] batch apply 失败后自动二分或降级为单 task apply。
- [ ] 单 task 失败写入 task failed，其他 task 继续。
- [ ] warning details 只包含 path/error，不包含源码内容。
- [ ] 失败 task 不写 applied checkpoint。

验收标准：

- 一个坏文件不会阻塞同批其他文件最终 applied。
- job 结果为 `partial_ready`，可重试失败 task。

### AR3-04 `created_by_job` 改真实 job id

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`IMPL`、`TEST`
- 依赖：`AR1-03`

TODO：

- [ ] `_tombstone_deleted_path`、`_diff_and_mark_stale`、`_tombstone_object` 接收 `job_id`。
- [ ] tombstone/replacement 的 `created_by_job` 从 `job:incremental_index` 改为真实 `job_id`。
- [ ] 无 job context 时保留兼容 fallback。
- [ ] 测试覆盖审计字段。

验收标准：

- 可以按 job id 追踪一次增量构建产生的 tombstone/replacement。

---

## 9. Phase R4：观测、Benchmark 与发布 Gate

Phase R4 是 R1-R3 的发布门禁，重点验证“恢复真的省时间”和“不会破坏一致性”。

### AR4-01 扩展 benchmark phase timing

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`OPS`、`TEST`
- 依赖：`AR1-03`
- 建议落点：`scripts/benchmark_index.py`、`eval/index_benchmark.py`

TODO：

- [ ] 基于 progress events 聚合 phase timing。
- [ ] 记录 discover/code_collect/code_apply/doc_collect/doc_apply/vector_apply/profile_relations/workspace_map 耗时。
- [ ] 记录 task stats：total/applied/skipped/failed/replayed。
- [ ] 汇总报告展示恢复前后 wall time 和 replay overhead。

验收标准：

- benchmark JSONL 可解释瓶颈阶段。
- 报告能看出 resume 是否接近“只跑剩余任务”。

### AR4-02 crash/resume benchmark

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`OPS`、`TEST`
- 依赖：`AR2-05`

TODO：

- [ ] benchmark 增加 `--interrupt-after-task-percent` 或专用 crash harness。
- [ ] 跑 30%、70%、90% 中断点恢复耗时。
- [ ] 记录 replay task 数、skipped task 数、validate 结果。
- [ ] 输出恢复收益报告。

验收标准：

- 70% 后中断恢复，总耗时接近剩余 30% 加少量重放开销。

### AR4-03 恢复一致性验收

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`TEST`
- 依赖：`AR2-05`、`AR3-03`

TODO：

- [ ] 一次性完整跑与 crash/resume 跑比较逻辑对象集合。
- [ ] 比较 file/chunk/entity/relation/evidence/vector_ref。
- [ ] 运行 `validate --strict --format json`。
- [ ] 运行 `status --format json` 检查 job/task 统计。

验收标准：

- 允许时间戳和 job metadata 不同，逻辑对象集合必须等价。

### AR4-04 发布文档与本地手测脚本

- 状态：`[ ]`
- 优先级：`P1`
- 类型：`DOC`、`TEST`
- 依赖：`AR4-03`

TODO：

- [ ] 更新 `active_knowledge_server_local_full_integration_test.md`，加入 resume 测试流程。
- [ ] 增加手测命令：启动 index、Ctrl+C、重跑、观察 `resumed=true`。
- [ ] 增加 CI/本地慢测说明，避免 crash 测试默认拖慢普通单测。

验收标准：

- 用户可以按文档复现断点续建。

---

## 10. Phase R5：Collect Artifact 与 Embedding/Vector Cache

Phase R5 是进一步减少重启后重复解析和重复 embedding 的优化。R1-R4 未完成前不建议抢跑。

### AR5-01 collect artifact cache v1

- 状态：`[ ]`
- 优先级：`P2`
- 类型：`IMPL`、`TEST`
- 依赖：`AR2-01`
- 建议落点：`indexing/artifacts.py`

TODO：

- [ ] 定义 artifact root：`.active-kb/local/artifacts/index-jobs/<job_id>/collect/`。
- [ ] 支持 code/doc collect result JSON-safe 编解码。
- [ ] 写入 artifact_hash，恢复读取时校验。
- [ ] artifact 失败或 schema mismatch 时自动重 collect。
- [ ] 清理策略接入 maintenance。

验收标准：

- collect 完成 apply 前中断，恢复时可复用 artifact，不重 parse。

### AR5-02 embedding input/cache

- 状态：`[ ]`
- 优先级：`P2`
- 类型：`IMPL`、`TEST`
- 依赖：`AR2-04`

TODO：

- [ ] 定义 embedding cache key：model + object_type + content_hash + sanitizer version。
- [ ] 缓存 accepted/skipped secret scan 结果。
- [ ] 命中时直接生成 vector write。
- [ ] provider/local embedding 都经过同一 batcher。

验收标准：

- docs 未变但 vector rebuild 被触发时，可复用缓存减少 embedding 计算。

### AR5-03 vector delta segment 与 compaction

- 状态：`[ ]`
- 优先级：`P2`
- 类型：`IMPL`、`TEST`
- 依赖：`AR2-04`

TODO：

- [ ] 评估当前 JSON collection 全量读写热点。
- [ ] 设计 append-only delta segment：`vectors/<object_type>/<job_id>-part-N.jsonl`。
- [ ] query reader 合并 base + delta 或 compaction 后读取。
- [ ] compaction 成功后 checkpoint，失败不影响已提交 metadata。

验收标准：

- 大批量 vector upsert 不再随 batch 次数反复重写整个 collection。

---

## 11. Phase R6：Full Index Staging Publish

Phase R6 用于解决全量索引中断后污染 live target 的问题。它改动面大，建议在增量 resume 稳定后推进。

### AR6-01 设计 staging storage resolver

- 状态：`[ ]`
- 优先级：`P2`
- 类型：`CONTRACT`、`IMPL`、`TEST`
- 依赖：`AR1-03`

TODO：

- [ ] 为 full local/baseline 生成 `metadata.staging.<job_id>.db` 和 `vectors.staging.<job_id>/`。
- [ ] writer/reader 可在 staging target 上工作。
- [ ] staging path 写入 job metadata。
- [ ] 中断后同 job 恢复 staging path。

验收标准：

- full build 中断不会修改 live metadata/vector path。

### AR6-02 validate 后 publish pointer

- 状态：`[ ]`
- 优先级：`P2`
- 类型：`IMPL`、`TEST`
- 依赖：`AR6-01`

TODO：

- [ ] staging build 完成后运行 critical validation。
- [ ] SQLite WAL checkpoint/truncate 并关闭连接。
- [ ] metadata DB 使用 `os.replace` 或 manifest pointer 切换。
- [ ] vector directory 使用 versioned path + pointer manifest，避免目录替换非原子。
- [ ] publish 成功后 job ready，失败后 live 仍指向旧版本。

验收标准：

- publish 前崩溃不影响旧 live index。
- publish 后 query 使用新版本。

### AR6-03 旧 staging/live 版本清理

- 状态：`[ ]`
- 优先级：`P2`
- 类型：`OPS`、`IMPL`、`TEST`
- 依赖：`AR6-02`

TODO：

- [ ] maintenance 支持清理 superseded/failed staging job。
- [ ] 保留最近 N 个 live 版本。
- [ ] 清理前确认不删除当前 pointer 指向版本。

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

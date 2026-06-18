# Active Knowledge Server 索引加速与断点续建设计

> 文档状态：Design Proposal  
> 生成日期：2026-05-29  
> 适用对象：`active-knowledge-server`  
> 目标：降低首次/增量/向量索引构建耗时，并让构建过程在 Ctrl+C、进程崩溃、机器重启后可从最近安全断点继续。

---

## 1. 结论先行

当前实现已经完成了第一轮进度可视化、受控并行 collect、批量 transaction、向量批量写入和 benchmark 脚本。但这些能力仍然偏向“单次运行加速”，还没有形成真正的“可恢复索引作业”。

推荐方案：

1. 保留当前 `N workers collect + 1 writer apply` 的基本架构，继续避免 SQLite 多 writer。
2. 把 `index` 正式升级为持久化 job：每次索引都有 `job_id`、plan signature、任务清单、任务状态和 checkpoint。
3. 将 checkpoint 粒度定在“文件/文档 apply 成功”和“向量 batch 写入成功”，collect 产物可选落盘缓存。
4. 所有 apply 操作必须幂等，checkpoint 只在提交成功后写入。崩溃后允许重复 apply，靠稳定 ID/upsert/tombstone ID 收敛。
5. 全量索引采用 staging store 构建，最后原子 publish 或 pointer switch；增量 overlay 可以继续边构建边可见，但必须记录已提交任务。
6. 默认启用自动恢复：发现同配置、同 manifest、同目标的 interrupted/failed/partial job 时，继续未完成任务。提供 `--restart` 强制丢弃旧 job。

---

## 2. 当前实现评估

### 2.1 已具备能力

| 能力 | 现状 | 代码落点 |
| --- | --- | --- |
| 增量状态 | 只有“上一次完整成功”的 `index_state.json`，用于 diff 规划。 | `IncrementalIndexPipeline.state_path/load_state/save_state`，`pipeline.py:305-334` |
| 进度事件 | pipeline/code/doc/full index 已发 `IndexProgressEvent`，CLI 可渲染动态/纯文本进度。 | `indexing/progress.py`、`cli_progress.py` |
| 并行 collect | code/doc 都使用 `parallel_map_ordered`，有稳定排序、异常包装和 bounded in-flight。 | `parallel.py:62-162`、`code_indexer.py:263-273`、`doc_indexer.py:299-309` |
| 增量过滤 | code collect 支持 `include_paths`，少量变更不再必须整仓解析。 | `pipeline.py:639-644`、`code_indexer.py:182-205` |
| 单写事务 | SQLite writer 有 `transaction()`，chunk/entity FTS 与元数据在同一事务内同步。 | `sqlite_store.py:2216-2259` |
| 写入批量配置 | 有 `indexing.writer.batch_size`、`max_files_per_transaction`、`max_records_per_transaction` 和 `commit_interval_ms`，默认 `64/64/2048/1000ms`。 | `config/schema.py:237-244`、`defaults.py:127-134` |
| 向量批量写入 | LanceDB writer 支持 `upsert_vectors`，并在同一 metadata transaction 写 vector refs。 | `lancedb_store.py:355-396` |
| job 存储雏形 | 有 `job`、`job_checkpoint`、`job_lock` 表和轻量 `IndexJobRunner`。 | `jobs.py:89-240`、`sqlite_store.py:484-517` |
| 性能基准入口 | benchmark 支持 workers、writer batch、transaction 边界、commit interval、SQLite pragma 组合扫跑。 | `scripts/benchmark_index.py:64-124` |

### 2.2 关键缺口

| 缺口 | 影响 | 说明 |
| --- | --- | --- |
| 主 pipeline 未 job 化 | 中断后无法知道哪些阶段已经安全完成。 | `IndexJobRunner` 只是测试/ops 雏形，`IncrementalIndexPipeline.run` 没有创建/更新真实 job。 |
| checkpoint 不是任务级 | 只能保存最终成功状态，不能跳过已 apply 文件。 | `save_state(plan.current_state)` 只在 `not failed` 后执行，见 `pipeline.py:989-990`。 |
| collect 产物只在内存中 | collect 后、apply 前崩溃会丢失已解析结果。 | 重启后必须重新 parse；大文件/文档场景浪费明显。 |
| apply checkpoint 缺失 | apply 中途崩溃后只能重新跑整轮计划。 | 目前可以靠稳定 ID 重复 upsert 收敛，但无法避免重复工作。 |
| 全量索引直接写目标 store | 中断后目标可能处于部分构建状态，缺少 publish 边界。 | baseline/local full index 应先写 staging，再切换为 live。 |
| 向量写入可能成为 O(N) 热点 | 当前每次按 object_type 读取整个 collection rows 后再写回。 | `lancedb_store.py:381-392` 对大向量集会随批次数放大。 |
| WAL 仍未压测决策 | WAL 可提升读写并发，但仍需本地 FS 和 checkpoint 策略。 | 现有配置面已存在，但默认未启用。 |

---

## 3. 行业实践调研

### 3.1 可迁移原则

| 来源 | 行业实践 | 对本项目的设计约束 |
| --- | --- | --- |
| OpenSearch Bulk API | bulk 用于把大量 document 操作合并为一次请求，降低固定开销；`refresh=true` 有明显性能成本。 | 元数据、FTS、vector ref 继续走批量事务；索引期间进度与查询可见性解耦，最终阶段再统一 flush/refresh。 |
| Elasticsearch indexing tuning | 通过实验找到 bulk size；在可接受可见性延迟时增大 refresh interval；初始大批量加载可临时降低副本成本。 | 默认值必须由 benchmark 支撑；full index 使用 staging/publish，而不是让每条写入立刻成为稳定发布物。 |
| SQLite WAL | WAL 支持 readers 与 writer 并发，但同一 WAL 仍只有一个 writer；checkpoint 频率决定写入吞吐、读性能和 WAL 文件大小的平衡。 | 继续坚持单 writer；WAL 只作为本地 FS 下的可选优化，必须配 checkpoint 观测和回退。 |
| Python `concurrent.futures` | ThreadPool/ProcessPool 接口统一；ProcessPool 可绕过 GIL，但要求 callable/参数/结果可 pickle，且内部等待 future 会死锁。 | v1 继续线程池；只有压测证明确认 code parse CPU 受限后，才引入 process/hybrid，并先做 pickle 契约测试。 |
| Spark Structured Streaming | checkpoint 保存 progress/state，失败或有意停止后从 checkpoint 恢复；重启时对 query/source/schema 变化有限制。 | resume 必须绑定 plan signature；配置、manifest、schema、embedding model 变更时不得复用旧 checkpoint。 |
| Flink checkpoint | checkpoint 是输入位置和 operator state 的一致快照，恢复时回放 checkpoint 之后的记录。 | 本项目用“稳定任务清单 + 已 apply 任务集合”模拟 offsets；未 checkpoint 的任务允许重放，但 apply 必须幂等。 |
| Weaviate / LanceDB | 大批量导入使用 batch vectorization/async indexing；LanceDB OSS 索引构建/更新需要显式管理，Enterprise 异步构建。 | 向量阶段拆成“向量 payload 写入”和“向量索引/compaction”两个阶段；允许后台或最终阶段统一建索引。 |

### 3.2 参考资料

- OpenSearch Bulk API: https://docs.opensearch.org/latest/api-reference/document-apis/bulk/
- OpenSearch Refresh API: https://docs.opensearch.org/latest/api-reference/index-apis/refresh/
- Elasticsearch Tune for indexing speed: https://www.elastic.co/guide/en/elasticsearch/reference/8.19/tune-for-indexing-speed.html
- SQLite WAL: https://www.sqlite.org/wal.html
- Python `concurrent.futures`: https://docs.python.org/3/library/concurrent.futures.html
- Spark Structured Streaming checkpointing: https://spark.apache.org/docs/latest/streaming/apis-on-dataframes-and-datasets.html
- Flink Stateful Stream Processing: https://nightlies.apache.org/flink/flink-docs-release-1.20/docs/concepts/stateful-stream-processing/
- Weaviate Batch import: https://docs.weaviate.io/weaviate/manage-objects/import
- LanceDB Vector Indexes: https://docs.lancedb.com/indexing/vector-index

---

## 4. 加速构建总体方案

### 4.1 目标架构

```text
scan manifests
  -> build deterministic plan
  -> create/resume index job
  -> generate task ledger
  -> parallel collect with bounded in-flight
  -> optional collect artifact cache
  -> single-writer apply in batches
  -> per-task checkpoint after commit
  -> vector compaction/index build
  -> publish final state
```

核心边界：

- worker 只读文件、解析、构建内存记录或本地 artifact，不直接写 SQLite/LanceDB。
- writer 只在主线程提交 metadata/vector refs/tombstones/replacements。
- checkpoint 不要求与 metadata 写入跨 DB 原子提交；通过幂等 apply 保证崩溃重放安全。
- `index_state.json` 继续表示“上一次完整成功状态”，不承担中间进度职责。

### 4.2 Work Pruning 优先于并行

第一层加速不是加 worker，而是减少任务数。

新增 `index_task_ledger` 或复用 `job_checkpoint` 保存以下派生键：

```json
{
  "plan_signature": "sha256:...",
  "task_key": "code:components/foo/bar.c",
  "source_kind": "code",
  "relative_path": "components/foo/bar.c",
  "input_hash": "sha256:...",
  "parser_schema": "code_indexer.v1",
  "embedding_model": null,
  "status": "applied",
  "applied_at": "2026-05-29T00:00:00Z"
}
```

跳过规则：

- `input_hash`、parser schema、embedding model、source kind、target store 都一致，且 task 状态为 `applied`，则跳过 collect/apply。
- Makefile 或 `.mk` 变更触发相关 module/path 的二级依赖任务；当前 `_incremental_code_paths_to_collect` 会把 Makefile 纳入 collect，但还需要更精准的 reverse dependency ledger。
- profile 变更只触发 profile-conditioned relations 和 workspace map，除非 schema 变更。
- embedding model 变更只触发 docs vector task，不必重写 doc metadata。

### 4.3 并行 Collect 升级

当前线程池设计可保留，增强点：

1. 引入 stage profile：`docs_io_thread`、`code_thread`、`code_process_experimental`。
2. `workers=auto` 由静态 cap 改为基于 benchmark 结果和任务数的策略。例如小于 4 个任务串行，中仓 docs cap 6，code cap 4，大仓可按 RSS 限制动态调低。
3. 结果不再必须整阶段留在内存。支持 streaming collect queue：
   - collect worker 产出 `_CollectedCodeEntry` 或 `_CollectedDocumentEntry`。
   - 主线程按路径顺序缓冲小窗口。
   - 达到 `apply_batch_size` 后写入 staging artifact 或直接 apply。
4. code process/hybrid 仅在满足以下条件后开放：
   - 输入输出 dataclass 可 pickle。
   - parser 不依赖不可序列化对象。
   - 串行、thread、process 三种输出集合完全等价。
   - benchmark 在中/大仓稳定优于 thread，且 RSS 未超过阈值。

### 4.4 Embedding 与向量阶段加速

现状里 `EmbeddingsConfig.batch_size` 已存在，但本地 deterministic embedding 没有真正 provider batch 调度。建议拆分为三层：

1. embedding input cache
   - key：`embedding_model_version + object_type + content_hash + sanitizer_version`。
   - 命中时直接复用 embedding 和 vector_ref。
   - secret scan 结果也缓存，避免重复扫描长文档。
2. provider batcher
   - 按 `indexing.embeddings.batch_size` 聚合请求。
   - 支持 provider rate limit、retry/backoff、partial failure。
   - 进度事件增加 `embedding_prepare`、`embedding_compute`、`embedding_apply` 或在 `vectors_apply` metadata 中区分。
3. vector delta writer
   - 避免每个 batch 都读取并重写整个 collection。
   - 本地 v1 可写 append-only delta segment，例如 `vectors/<object_type>/<job_id>-part-N.jsonl`，最终 compaction 合并。
   - 如果接入真实 LanceDB table，则使用 table-level add/upsert 和索引构建 API，最后显式 `wait_for_index` 或记录异步索引状态。

### 4.5 写入阶段批量化

当前 full `collect_and_store` 使用 `_write_in_batches`，但 incremental `_apply_code_bundle/_apply_doc_bundle` 仍按文件开 transaction。建议：

- 新增 `ApplyBatch`：包含若干 file/doc bundle、旧 bundle diff、vector refs。
- 每批在一个 metadata transaction 内处理：
  - stale tombstones/replacements
  - file/chunk/entity/relation/evidence upsert
  - FTS sync
  - vector_ref metadata
- checkpoint 粒度仍按 task 记录；一个 transaction 成功后，把 batch 内每个 task 标记为 `applied`。
- 若 batch 失败，降级为单 task apply，定位失败 path 并记录 warning。

批次边界：

- 默认 `apply_batch_size = indexing.writer.batch_size`，但按“文件数”而不是“记录数”解释时容易失控，建议新增：
  - `indexing.writer.max_files_per_transaction`
  - `indexing.writer.max_records_per_transaction`
  - `indexing.writer.commit_interval_ms`

### 4.6 SQLite WAL 策略

短期不默认启用 WAL。中期在 Phase 4 benchmark 后分场景启用：

- 本地单机、索引期间仍有 query 读：可选 `journal_mode=wal`、`synchronous=normal`、定期 passive checkpoint。
- 网络文件系统、共享目录、不确认本地 FS：保持 `delete/full`。
- 大批量 full build staging：可在 staging DB 使用 WAL，publish 前 checkpoint/truncate，最终发布物回到可迁移状态。

必须观测：

- metadata DB/WAL/SHM 大小
- checkpoint busy/log/checkpointed frames
- 查询读延迟 p50/p95
- writer lock 等待与失败次数

---

## 5. 断点续建设计

### 5.1 语义目标

| 场景 | 期望行为 |
| --- | --- |
| Ctrl+C | CLI 输出最后 phase/path/job_id；job 标记为 `failed` 或 `interrupted` metadata；下次默认自动继续。 |
| 进程崩溃 | lock 过期后可重新获取；已 checkpoint 的任务跳过；未 checkpoint 的任务重放。 |
| 机器重启 | job/checkpoint/artifact 在本地 artifacts/jobs DB 中保留，恢复逻辑同崩溃。 |
| 单文件失败 | 其他任务继续；失败任务进入 `failed`，整 job `partial_ready`；重试只处理失败/未完成任务。 |
| 配置或 manifest 变化 | plan signature 不匹配，不复用旧 checkpoint；提示 restart 或创建新 job。 |

### 5.2 Plan Signature

`plan_signature` 用于判断 checkpoint 是否可复用。建议包含：

- command mode：full/incremental/rebuild vectors
- target：local overlay/baseline
- source：all/code/docs
- snapshot_id
- workspace inventory hash
- source docs manifest hash
- parser/extractor schema versions
- profile relation schema version
- embedding provider/model/enabled
- relevant config hash：workers 可排除，writer batch 可排除；parser flags、docs format flags、secret scan version 必须包含
- storage schema version

不匹配策略：

- 默认：创建新 job，旧 job 标记 `superseded` metadata。
- `--resume JOB_ID`：若不匹配，直接 blocked，要求用户 `--restart`。

### 5.3 Job 与 Task 模型

可以先复用当前 `job_checkpoint` KV 表，后续再迁移到结构化 `job_task` 表。

推荐长期表：

```sql
CREATE TABLE index_task (
  job_id TEXT NOT NULL,
  task_key TEXT NOT NULL,
  phase TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  relative_path TEXT,
  input_hash TEXT,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  artifact_ref TEXT,
  artifact_hash TEXT,
  warning_json TEXT NOT NULL DEFAULT '[]',
  record_counts_json TEXT NOT NULL DEFAULT '{}',
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (job_id, task_key)
);

CREATE INDEX idx_index_task_status
ON index_task(job_id, phase, status);
```

状态流转：

```text
pending -> collecting -> collected -> applying -> applied
                         collected -> failed
pending -> skipped
applying -> failed
failed -> pending  (retry)
```

job 状态可沿用当前 `pending/discovering/parsing/extracting/embedding/reporting/ready/failed/partial_ready`，但 metadata 中补充：

- `execution_state`: scheduled/running/interrupted/superseded
- `plan_signature`
- `last_phase`
- `last_task_key`
- `tasks_total/applied/failed/skipped`
- `resume_count`

### 5.4 Artifact Cache

为避免 collect 后崩溃导致重解析，可选落盘：

```text
.active-kb/local/artifacts/index-jobs/<job_id>/
  plan.json
  tasks.jsonl
  collect/
    code/<task_hash>.json.zst
    docs/<task_hash>.json.zst
  warnings.jsonl
```

安全约束：

- artifact 敏感级别等同于 metadata DB，因为其中可能包含 chunk text/excerpt。
- 仅写入 local artifacts，不写入 shared baseline artifacts，除非 publish 模式显式允许。
- 记录 `artifact_hash`，恢复时校验失败则丢弃并重 collect。
- 提供清理命令：`active-kb clean --old-index-jobs KEEP` 或复用现有 maintenance。

v1 可以先不缓存完整记录，只持久化 task applied checkpoint；这样恢复时会重 collect 未完成任务，但不会重写已 apply 任务。v2 再缓存 collect artifact。

### 5.5 Apply 幂等协议

checkpoint 写入规则：

1. collect 完成后可写 `collected` checkpoint，但这不是可见性边界。
2. metadata/vector apply 成功提交后，写 `applied` checkpoint。
3. 如果 apply 成功但 checkpoint 写入前崩溃，下次会重复 apply。要求稳定 ID/upsert/tombstone/replacement 让重复 apply 收敛。
4. 如果 checkpoint 成功，则认为任务可跳过。checkpoint 必须后写，不能先于 metadata/vector 成功。

已满足基础：

- file/chunk/entity/relation/evidence ID 多数是稳定 hash。
- SQLite 写入是 upsert。
- tombstone/replacement ID 基于 object/scope/reason，天然可重复。

需要补强：

- vector rows 重复 upsert 必须按 `vector_ref_id` 替换而不是 append 重复。
- full staging publish 要么完全成功，要么旧 live store 不受影响。
- `created_by_job` 目前写死 `job:incremental_index`，应改为真实 `job_id`，方便清理和审计。

### 5.6 Resume 算法

```text
index command starts
  -> migrate stores
  -> find or create job
  -> acquire/renew index lock
  -> build current plan
  -> compare plan_signature
  -> materialize deterministic task list
  -> mark removed tasks as skipped/superseded
  -> for each task not applied:
       collect or load artifact
       apply in writer batch
       checkpoint applied
       emit progress
  -> rebuild profile relations / workspace map if required
  -> validate critical invariants
  -> save index_state.json only if job ready
  -> transition job ready/partial_ready
  -> release lock
```

自动恢复策略：

- `--resume auto` 作为默认：
  - 查找同 target/source/mode/profile/snapshot 且 `execution_state in interrupted/running` 或 terminal `partial_ready/failed` 的最近 job。
  - lock 已过期且 plan signature 匹配，则继续。
  - lock 未过期则 blocked，提示当前 owner 和过期时间。
- `--resume JOB_ID`：只尝试指定 job。
- `--restart`：把旧 matching job 标记 superseded，创建新 job。
- `--no-resume`：不查找旧 job，但仍创建新 job。

### 5.7 Full Index Staging

全量索引不建议直接写 live target。设计为：

```text
metadata.live.db
metadata.staging.<job_id>.db
vectors.live/
vectors.staging.<job_id>/
publish-manifest.json
```

工程落地时不应硬编码上述文件名，而应从当前配置的 live path 派生 staging path：

- baseline metadata：`baseline/db/metadata.db -> baseline/db/metadata.staging.<job_token>.db`
- local full metadata：`local/db/overlay.db -> local/db/overlay.staging.<job_token>.db`
- baseline vectors：`baseline/vectors/lancedb -> baseline/vectors/lancedb.staging.<job_token>/`
- local full vectors：`local/vectors/lancedb-delta -> local/vectors/lancedb-delta.staging.<job_token>/`

其中 `job_token` 由 `job_id` 经过 filesystem-safe 归一化后再附加短 hash，要求：

- 同一 `job_id` 多次恢复得到完全一致的 staging path。
- 不同 `job_id` 必然落到不同 staging path。
- path token 不直接暴露 `:`、`/` 等跨平台不安全字符。

流程：

1. full build 写 staging DB/vector root。
2. 每个 task checkpoint 写入 jobs DB。
3. build 完成后运行 validate。
4. checkpoint/truncate WAL，关闭连接。
5. 原子切换：
   - 本地可用 `os.replace` 替换 DB 文件和 manifest pointer。
   - vector directory 使用 versioned path + pointer manifest，避免非原子目录替换。
6. publish 成功后，live pointer 指向新版本，旧版本按保留策略清理。

这样中断不会污染当前可查询 live index。

---

## 6. CLI 与 Ops 交互

建议新增或调整参数：

```bash
active-kb index --resume auto      # 默认
active-kb index --resume <job_id>
active-kb index --restart
active-kb index --no-resume
active-kb index --job-id <job_id>  # 调试/CI 可复现
```

输出增强：

- text progress 增加 `job_id`、`resumed: true/false`、`skipped/applied/failed`。
- JSON final payload 增加：

```json
{
  "job": {
    "job_id": "index:...",
    "status": "ready",
    "resumed": true,
    "plan_signature": "sha256:...",
    "tasks_total": 1000,
    "tasks_applied": 998,
    "tasks_skipped": 2,
    "tasks_failed": 0
  }
}
```

Ops 工具：

- `ops_start_index` 创建 job 后应可选择同步执行或交给 scheduler。
- `ops_index_status` 展示 phase/task 级 checkpoint。
- `ops_cancel_index` 只取消未开始任务，正在 apply 的事务自然完成或 rollback。
- `ops_resume_index(job_id)` 可作为 MCP 显式入口。

---

## 7. 落地路线

### Phase R0：补齐观测与验收

- 跑小/中/大仓 benchmark，覆盖 workers、batch、WAL。
- 在 benchmark JSONL 里增加 phase timing：discover、code_collect、code_finalize、code_apply、doc_collect、embedding、vector_apply、profile_relations、workspace_map。
- 记录最慢 N 个 path，只记录 path、耗时、阶段和对象计数，不记录源码内容。

### Phase R1：Job 化主 pipeline

- `IncrementalIndexPipeline.run` 增加 `job_store/job_id/resume_policy` 参数。
- CLI index 创建或恢复 job，并传入 pipeline。
- job lock 增加 heartbeat/renew，避免长索引超过 TTL。
- 进度事件增加 `job_id` 可选字段，或由 CLI reporter 外层注入。

### Phase R2：任务级 checkpoint

- 生成 deterministic task list。
- 每个 code/doc delete/apply/vector task 成功后 checkpoint。
- resume 时跳过 matching applied task。
- 中断后重跑 incremental，验证不会重 apply 已 checkpoint 任务。

### Phase R3：Apply batch 与失败降级

- 引入 `ApplyBatch`。
- batch 成功后批量 checkpoint。
- batch 失败自动二分或降级到单 task apply。
- `created_by_job` 改真实 job id。

### Phase R4：Collect artifact 与 embedding/vector cache

- 未 apply 的 collected task 可从 artifact 继续。
- embedding cache 按 content_hash/model 复用。
- vector delta append/compaction 或真实 LanceDB table upsert。

### Phase R5：Full staging publish

- full local/baseline 写 staging store。
- validate 成功后 publish pointer。
- 中断恢复 staging job。
- 发布物 WAL checkpoint/truncate 策略固化。

### Phase R6：Process/hybrid 与默认值固化

- 只有 benchmark 证明收益后开启 process/hybrid。
- 默认 `workers/batch_size/WAL` 由报告决定。
- Release gate 加入 crash/resume 集成测试。

---

## 8. 验收标准

### 功能验收

- Ctrl+C 后再次运行同一命令，能显示 `resumed=true` 并跳过已 applied task。
- 进程在 apply 后、checkpoint 前崩溃，恢复后重复 apply 不产生重复逻辑对象。
- 进程在 checkpoint 后崩溃，恢复后不重跑该 task。
- 单文件失败后 job 为 `partial_ready`，重试只处理失败任务。
- plan signature 变化时拒绝复用旧 checkpoint。

### 一致性验收

- `workers=1 --no-resume` 与 `workers=auto --resume auto` 最终逻辑对象集合等价。
- `validate --strict --format json` 不新增 blocked/error。
- FTS、metadata、vector_ref、vector payload 一致性校验通过。

### 性能验收

- 小仓 `<5k` 文件：比当前串行/无恢复基线改善至少 20%。
- 中仓 `5k-30k` 文件：改善至少 30%。
- 大仓 `>30k` 文件：改善至少 35%，RSS 不超过串行基线 2x。
- 崩溃恢复场景：已完成 70% 后恢复，总耗时应接近剩余 30% 加少量重放开销。

### 回归测试建议

- unit：plan signature、task ledger 状态机、checkpoint 序列化、resume policy。
- unit：idempotent apply 重放两次，逻辑对象集合不变。
- integration：subprocess 在第 N 个 task 后 SIGTERM，重跑恢复并 validate。
- integration：full staging 中断后 live pointer 不变。
- perf：workers/batch/WAL 矩阵输出 Markdown 报告。

---

## 9. 推荐优先级

最建议先做 R1 + R2。它们不要求重写存储，也不依赖进程池或 WAL 调参，却能直接解决“中断后不能继续”的核心痛点。

随后做 R3，把已完成的批量 writer 能力真正延伸到 incremental apply。R4/R5 属于收益更大的二阶段工程，适合在 benchmark 显示 vector 或 full build 已成为主要瓶颈后推进。

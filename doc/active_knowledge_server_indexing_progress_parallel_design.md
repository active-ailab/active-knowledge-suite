# Active Knowledge Server 索引交互与并行加速设计

> 文档状态：Design Proposal  
> 生成日期：2026-05-22  
> 适用对象：active-knowledge-server  
> 范围：首次索引慢、缺少反馈；评估并设计可控并行以压缩构建耗时

---

## 1. 问题与目标

### 1.1 用户痛点

首次执行索引命令时：

- 耗时长
- 终端无进度反馈
- 无法判断当前是否卡死、还需多久、已处理哪些文件

目标命令：

- `uv run active-kb index --config ../examples/local-single-user.yaml --incremental --profile auto`

### 1.2 本次设计目标

- 引入可感知的交互信息（总量、当前进度、滚动最近文件）
- 在不破坏一致性前提下，支持并行执行以缩短耗时
- 保持 JSON 输出兼容、保留当前 CLI 契约

### 1.3 非目标

- 不改变 Query/MCP 对外 schema
- 不改变 baseline/overlay 安全写入门槛
- 不在本阶段重写存储引擎

---

## 2. 本地代码现状（基线）

### 2.1 索引入口与执行形态

- CLI `index` 入口在 `active-knowledge-server/src/active_knowledge_server/cli.py:680`
- 增量路径调用 `IncrementalIndexPipeline.run(...)`，见 `active-knowledge-server/src/active_knowledge_server/cli.py:701`
- 全量路径调用 `run_full_index(...)`，见 `active-knowledge-server/src/active_knowledge_server/cli.py:1418`

当前行为：

- text 模式仅在结束后输出一次摘要，无中间进度
- json 模式只返回最终结果，无阶段事件

### 2.2 串行热点

- 增量主循环在 `active-knowledge-server/src/active_knowledge_server/indexing/pipeline.py:485` 起
- 代码变更文件逐个 `_apply_code_bundle(...)`，见 `.../pipeline.py:515-528`
- 文档变更文件逐个 `DocumentIndexer.collect(...)` 并写入，见 `.../pipeline.py:557-629`

### 2.3 写入侧约束

- `SQLiteStorageWriter` 每次 upsert 普遍独立事务/commit，见 `active-knowledge-server/src/active_knowledge_server/storage/sqlite_store.py:2115` 起
- overlay 有单写锁语义，`INDEX_JOB_LOCK_ID = "index:overlay"`，见 `active-knowledge-server/src/active_knowledge_server/indexing/jobs.py:39`

结论：

- 目前是“单线程采集 + 单线程写入 + 末端一次性输出”，交互与吞吐都有优化空间

---

## 3. 行业实践调研摘要

### 3.1 搜索引擎构建实践（OpenSearch）

依据：OpenSearch 官方索引性能调优文档  
链接：https://docs.opensearch.org/latest/tuning-your-cluster/performance/

提炼出的可迁移原则：

- 批量写入优先于高频小写
- 减少过于频繁的 flush/refresh
- 将并发资源优先给索引主路径而非后台合并
- 通过试验寻找最优批次大小，而非拍脑袋设定

### 3.2 SQLite 并发实践

依据：SQLite WAL 文档  
链接：https://www.sqlite.org/wal.html

提炼出的可迁移原则：

- 读写可并发，但同一 DB 文件仍是单写者语义
- 写者数量增加不等于吞吐线性提升，反而可能锁竞争加剧
- 需要控制 checkpoint/flush 节奏，避免吞吐抖动

### 3.3 Python 并发执行实践

依据：Python `concurrent.futures` 文档  
链接：https://docs.python.org/3/library/concurrent.futures.html

提炼出的可迁移原则：

- I/O 密集优先线程池
- CPU 密集优先进程池（绕开 GIL）
- 需要明确 cancel/timeout/异常归并策略

---

## 4. 设计总览

采用两条并行推进线：

- 线 A：交互可视化（方案 A：回调 + rich.Live）
- 线 B：受控并行（并行解析 + 单写入提交）

### 4.1 总体架构

1. Pipeline 输出结构化进度事件
2. CLI text 模式渲染 rich.Live（进度条 + 最近文件滚动）
3. 并行仅作用于“读取/解析/构建记录”阶段
4. 元数据与向量保持单写者提交，批量化事务

---

## 5. 线 A：回调 + rich.Live 设计（推荐）

### 5.1 事件模型

新增统一进度事件（示例字段）：

- `phase`: plan | discover | code_collect | code_apply | doc_collect | doc_apply | vectors_apply | profile_relations | workspace_map | done
- `stage_total`: 当前阶段总量
- `stage_done`: 当前阶段已完成
- `global_total`: 全流程总量
- `global_done`: 全流程已完成
- `current_path`: 当前处理文件
- `message`: 当前阶段简述（可选）
- `warnings_count`: 当前累计 warning 数
- `started_at` / `updated_at`: UTC 时间戳
- `eta_seconds`: 估算剩余时间（可选，按滑动窗口速率估计）

说明：

- `recent_paths` 不进入 pipeline 事件本体，由 CLI 侧基于 `current_path` 维护最近 10 条滚动窗口
- 事件必须保持 JSON-safe，不承载源码片段、记录对象或 embedding 内容

### 5.2 总量定义

增量模式：

- 代码总量：`len(changed_code_paths) + len(deleted_code_paths)`
- 文档总量：`len(doc_paths_to_collect) + len(deleted_doc_paths)`
- 全局总量：代码总量 + 文档总量 + 其他阶段固定步数（profile/workspace_map）

全量模式：

- 代码总量：workspace inventory 中可索引代码文件数
- 文档总量：source docs manifest 中文档文件数

### 5.3 rich 展示策略

终端布局：

- 顶部：总进度条（global）
- 中部：阶段进度条（code/doc 当前阶段）
- 下部：最近文件滚动区（最多 10 条）

刷新策略：

- 默认 5Hz（200ms）
- 仅 text + TTY 模式启用
- text + 非 TTY 模式保留低频纯文本摘要
- json 模式 stdout 只保留最终机器可读结果；若 stderr 是交互式终端，可在 stderr 渲染同一套动态 UI

### 5.4 取消与异常

- `Ctrl+C`：CLI 捕获后输出“已处理/总量/最后文件”快照
- Pipeline 保持现有 partial_ready 语义
- 中断不视为异常栈污染用户体验（可映射为简洁提示）

---

## 6. 线 B：并行加速设计

### 6.1 核心并发原则

采用 `N workers parse + 1 writer commit`：

- 并行阶段：scan/read/parse/chunk/entity 构建
- 串行阶段：SQLite/LanceDB 写入

原因：

- 本地存储当前是 SQLite + FTS + overlay 锁模型
- 多写者并发写库会放大锁竞争与事务抖动

### 6.2 分阶段并发模型

阶段 1（并行 collect）：

- `CodeIndexer.collect` 按文件任务分发
- `DocumentIndexer.collect` 按文档任务分发
- worker 只返回内存对象，不直接写库

阶段 2（批量写入）：

- writer 单线程消费结果队列
- 分批 upsert：`source/file/chunk/entity/relation/evidence/vector_ref`
- 每批统一提交，降低 commit 频率

阶段 3（收尾阶段）：

- profile-conditioned relations 重建（保持串行）
- workspace map 刷新（保持串行）

### 6.3 线程池/进程池选型

建议默认：

- docs：`ThreadPoolExecutor`
- code：先 `ThreadPoolExecutor` 起步；若 CPU 占比高且可序列化成本可控，再切 `ProcessPoolExecutor`

保守策略：

- v1 先统一线程池，避免进程池序列化与跨进程对象构建复杂度
- v2 再引入 code 进程池选项

### 6.4 并行配置建议

扩展配置项（设计建议）：

- `indexing.workers`: 现有字段生效化（auto 或整数）
- `indexing.parallel.enabled`: bool
- `indexing.parallel.mode`: thread | process | hybrid
- `indexing.writer.batch_size`: 默认 200~1000（需压测）
- `indexing.writer.commit_interval_ms`: 默认 200~500

Phase 0 决议：

- 先只固化 `indexing.workers` 的语义，不在 Phase 0 引入新的 `parallel.enabled` 或 `parallel.mode` 配置
- `indexing.workers = 1` 作为串行回退开关

---

## 7. 与现有代码的映射（Design -> Build）

| 设计项 | 本地代码落点 | 实现要点 |
| --- | --- | --- |
| 进度回调接口 | `indexing/pipeline.py:485` | `run()` 新增 progress callback 参数并在关键循环发事件 |
| CLI 动态 UI | `cli.py:680` | text 模式启用 rich.Live，json 模式保持原样 |
| 最近文件滚动 | `cli.py:680` | CLI 侧维护 deque(maxlen=10) |
| 代码并行采集 | `indexing/code_indexer.py:153` | 按文件任务并发 parse，结果汇总排序后返回 |
| 文档并行采集 | `indexing/doc_indexer.py:194` | 按文档并发 parse + embedding input 准备 |
| 单写者批量提交 | `storage/sqlite_store.py:2115` | 引入批量 upsert 与分组事务提交 |
| 锁语义保持 | `indexing/jobs.py:39` | 保持 overlay 单写锁，不引入多 writer job |

---

## 8. 风险与缓解

### 8.1 风险

- 并发后记录顺序不稳定，影响测试快照
- 内存峰值升高（worker 结果暂存）
- 批量事务过大引起长事务阻塞
- 进程池序列化开销抵消收益

### 8.2 缓解

- 统一按 `relative_path` 排序归并再写入
- 增加 in-flight 队列上限与背压
- 可配置 batch size，默认保守值
- 首阶段仅线程池，先拿确定收益

---

## 9. 验收指标与压测口径

### 9.1 交互体验验收

- 索引执行期间可见总进度与阶段进度
- 最近文件区持续滚动更新（<=10 条）
- `Ctrl+C` 后输出中断快照，不出现冗长栈追踪给普通用户

### 9.2 性能验收（建议目标）

- 小仓（<5k 文件）：总耗时改善 >= 20%
- 中仓（5k~30k 文件）：总耗时改善 >= 30%
- 大仓（>30k 文件）：总耗时改善 >= 35%

### 9.3 一致性验收

- 新旧实现生成的逻辑对象集合等价
- `validate --format json` 不新增 blocked/error
- 中断恢复后重跑增量可收敛

---

## 10. 分阶段落地计划

### Phase P0：仅交互（低风险，先交付）

- 引入 progress callback
- CLI rich.Live 渲染
- 不改任何并发与写入语义

价值：

- 立即解决“无反馈、像卡死”问题

### Phase P1：并行 collect + 单写者提交

- code/docs collect 并发化
- writer 仍单线程

价值：

- 在低一致性风险下获取主要吞吐提升

### Phase P2：批量事务与写入优化

- upsert 批量化
- commit 节奏可配置

价值：

- 进一步降低 SQLite 写入成本

### Phase P3：高级模式（可选）

- code 进程池/hybrid
- 更精细的 ETA 与热点阶段诊断

---

## 11. 阻塞项与解阻条件

| 阻塞项 | 影响 | 解阻条件 |
| --- | --- | --- |
| SQLite 写入批量接口缺失 | 无法稳定提速写入段 | 增加 writer 批量 API 或事务包装层 |
| 并发结果顺序不稳定 | 契约快照易抖动 | 统一归并排序规则 |
| 真实数据集压测缺失 | 无法给出默认 workers | 建立小/中/大三档基准并固化推荐值 |
| `Ctrl+C` 用户体验不一致 | 中断体验差 | 统一信号处理与中断摘要输出 |

---

## 12. 推荐决策

推荐采用：

- 方案 A（回调 + rich.Live）立即落地
- 并行采用“并行 collect + 单写者 batch commit”
- 不采用“多线程直接并发写 SQLite”
- 效果
```
Indexing code   ████████████░░░░░░░░░  247/512   0:00:18
Indexing docs   ███████████████████░░   89/103   0:00:06

Recent:
  → drivers/sensor/sensor_hub.c
  → framework/core/task_manager.c
  → application/ui/screen_main.c
  → components/ble/ble_stack.c
  ...
```

原因：

- 兼顾用户体验、吞吐收益与一致性风险
- 与现有 overlay 单写锁模型兼容
- 可阶段化上线，具备可回退能力

---

## 13. 可执行验收命令（设计验收阶段）

交互验收：

- `uv run active-kb index --config ../examples/local-single-user.yaml --incremental --profile auto`

一致性验收：

- `uv run active-kb validate --config ../examples/local-single-user.yaml --format json`
- `uv run active-kb status --config ../examples/local-single-user.yaml --format json`

性能对比验收（建议）:

- 同一数据集分别运行 workers=1/2/4/8，记录 wall time、CPU、内存峰值、warnings 数量

---

## 14. 参考资料

- SQLite WAL: https://www.sqlite.org/wal.html
- OpenSearch indexing performance tuning: https://docs.opensearch.org/latest/tuning-your-cluster/performance/
- Python concurrent.futures: https://docs.python.org/3/library/concurrent.futures.html

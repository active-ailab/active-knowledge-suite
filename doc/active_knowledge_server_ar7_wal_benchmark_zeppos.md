# Active Knowledge Server AR7 WAL Benchmark (ZeppOS)

> 日期：2026-06-24  
> 目标：为 `AR7-02 WAL 默认策略固化` 提供真实工程验证，比较 `delete/full`、`wal/full`、`wal/normal` 在本地单机索引中的 wall time、并发只读延迟与 busy 风险。  
> 工程口径：源工程 `/home/gangan/ZeppOS`，代表性代码子集 `framework/engine/sportEngine`

## 1. 为什么不是直接跑整个 `framework`

直接对 `/home/gangan/ZeppOS` 的 `framework` 全量切片执行完整 benchmark 时，`workspace_map`、overlay、jobs DB 和 artifact 写盘会把临时 workdir 拉到约 `21G`，并触发 `No space left on device`。这和 `AR7-02` 要验证的 SQLite journal 策略不是同一个问题。

因此本轮改为：

- 仍然使用真实 ZeppOS 代码，而不是 synthetic fixture。
- 复制一个可控的真实子工作区，只保留：
  - `framework/engine/sportEngine`
  - `configs/mhs003`
  - `build/.config`
  - `build/out_hub/.config`
- 让索引器继续走真实 `active-kb index` 路径，保留 code collect、code apply、profile relations、workspace map、jobs checkpoint 和 SQLite 写入。

这样既保留真实工程特征，也避免被整仓磁盘容量主导。

## 2. 行业与官方资料结论

- SQLite WAL 官方文档说明：WAL 的核心价值是 reader 和 writer 可以并发，但仍然只有一个 writer，且 checkpoint 策略会直接影响延迟和文件膨胀。  
  https://sqlite.org/wal.html
- SQLite 官方文档明确指出：WAL 不适用于 network filesystem，因此默认策略不能脱离“确认本地文件系统”这个前提。  
  https://sqlite.org/wal.html  
  https://sqlite.org/useovernet.html
- SQLite `PRAGMA synchronous` 文档给出的官方结论是：在 WAL 模式下，`synchronous=NORMAL` 通常是性能与安全的最佳平衡，但会牺牲掉掉电/系统崩溃后的最近事务 durability。  
  https://sqlite.org/pragma.html
- SQLite 官方在 2026-03 公布了 WAL-reset bug：问题覆盖到 `3.51.2` 及更早版本，修复版本为 `3.51.3+`，旧分支 backport 为 `3.50.7` / `3.44.6`。  
  https://sqlite.org/wal.html

结合本项目架构，这意味着：

- 我们仍然坚持单 writer。
- WAL 只能作为“本地单机 + 有并发只读需求”的场景优化。
- 在运行时 SQLite 版本未达到修复版本前，不应把 WAL 直接升为全局默认。

## 3. 本地运行时前提

本次 benchmark 使用的 Python SQLite 版本：

```bash
cd active-knowledge-server
uv run python - <<'PY'
import sqlite3
print(sqlite3.sqlite_version)
PY
```

结果：`3.50.4`

这低于 SQLite 官方回补的 `3.50.7`，因此即使性能数据偏向 WAL，也不足以支持“现在就改仓库全局默认值”。

## 4. 执行命令

### 4.1 准备真实子工作区

```bash
rm -rf /tmp/zeppos-ar7-wal-sport
mkdir -p /tmp/zeppos-ar7-wal-sport/framework/engine
mkdir -p /tmp/zeppos-ar7-wal-sport/configs
mkdir -p /tmp/zeppos-ar7-wal-sport/build/out_hub

cp -a /home/gangan/ZeppOS/framework/engine/sportEngine \
  /tmp/zeppos-ar7-wal-sport/framework/engine/
cp -a /home/gangan/ZeppOS/configs/mhs003 \
  /tmp/zeppos-ar7-wal-sport/configs/
cp -a /home/gangan/ZeppOS/build/.config \
  /tmp/zeppos-ar7-wal-sport/build/
cp -a /home/gangan/ZeppOS/build/out_hub/.config \
  /tmp/zeppos-ar7-wal-sport/build/out_hub/
```

### 4.2 统一 benchmark 配置

配置文件：`examples/local-single-user-zeppos-framework-wal-benchmark.yaml`

关键点：

- `paths.include=framework`
- `embeddings.enabled=false`
- `indexing.parallel.mode=thread`
- `workers=4`
- 通过 `--workspace /tmp/zeppos-ar7-wal-sport` 指向真实子工作区

### 4.3 执行三组 journal 组合

```bash
cd active-knowledge-server
rm -rf /tmp/active-kb-ar7-02-wal-bench
mkdir -p /tmp/active-kb-ar7-02-wal-bench

uv run python scripts/benchmark_index.py \
  --config ../examples/local-single-user-zeppos-framework-wal-benchmark.yaml \
  --workspace /tmp/zeppos-ar7-wal-sport \
  --mode incremental \
  --target local \
  --source code \
  --workers 4 \
  --cache-mode cold \
  --repeat 1 \
  --readonly-probe sqlite_count \
  --readonly-probe-interval-ms 50 \
  --readonly-probe-timeout-ms 100 \
  --sqlite-journal-mode delete \
  --sqlite-synchronous full \
  --sqlite-checkpoint-mode passive \
  --bench-root /tmp/active-kb-ar7-02-wal-bench/delete-full \
  --output /tmp/active-kb-ar7-02-wal-bench/records.jsonl

uv run python scripts/benchmark_index.py \
  --config ../examples/local-single-user-zeppos-framework-wal-benchmark.yaml \
  --workspace /tmp/zeppos-ar7-wal-sport \
  --mode incremental \
  --target local \
  --source code \
  --workers 4 \
  --cache-mode cold \
  --repeat 1 \
  --readonly-probe sqlite_count \
  --readonly-probe-interval-ms 50 \
  --readonly-probe-timeout-ms 100 \
  --sqlite-journal-mode wal \
  --sqlite-synchronous full \
  --sqlite-checkpoint-mode passive \
  --bench-root /tmp/active-kb-ar7-02-wal-bench/wal-full \
  --output /tmp/active-kb-ar7-02-wal-bench/records.jsonl

uv run python scripts/benchmark_index.py \
  --config ../examples/local-single-user-zeppos-framework-wal-benchmark.yaml \
  --workspace /tmp/zeppos-ar7-wal-sport \
  --mode incremental \
  --target local \
  --source code \
  --workers 4 \
  --cache-mode cold \
  --repeat 1 \
  --readonly-probe sqlite_count \
  --readonly-probe-interval-ms 50 \
  --readonly-probe-timeout-ms 100 \
  --sqlite-journal-mode wal \
  --sqlite-synchronous normal \
  --sqlite-checkpoint-mode passive \
  --bench-root /tmp/active-kb-ar7-02-wal-bench/wal-normal \
  --output /tmp/active-kb-ar7-02-wal-bench/records.jsonl
```

### 4.4 生成 Markdown 汇总

```bash
cd active-knowledge-server
uv run python - <<'PY'
from pathlib import Path
from active_knowledge_server.eval.index_benchmark import (
    load_index_benchmark_records,
    render_index_benchmark_markdown,
    summarize_index_benchmark_records,
)

path = Path('/tmp/active-kb-ar7-02-wal-bench/records.jsonl')
report = summarize_index_benchmark_records(load_index_benchmark_records(path))
Path('/tmp/active-kb-ar7-02-wal-bench/report.md').write_text(
    render_index_benchmark_markdown(report),
    encoding='utf-8',
)
PY
```

## 5. 结果摘要

样本规模：

- `workspace_file_count=154`
- `changed_code_paths=154`
- `required tasks=156`

结果：

| Scenario | Wall (s) | Speedup vs `delete/full` | Read p50 (ms) | Read p95 (ms) | Busy count | Metadata DB | WAL file | Checkpoint busy |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `delete/full` | 223.528 | 1.00x | 1.367 | 57.076 | 454 | 100.5 MB | 0 | 0 |
| `wal/full` | 168.509 | 1.33x | 1.262 | 51.532 | 0 | 100.5 MB | 0 | 0 |
| `wal/normal` | 163.942 | 1.36x | 1.084 | 48.559 | 0 | 100.5 MB | 0 | 0 |

补充观察：

- `delete/full` 的只读探针出现了 `454` 次 `database is locked`，说明 rollback journal 下写事务确实会把并发读阻断出来。
- 两组 WAL 样本都没有观测到只读 probe `busy`。
- 三组样本在显式 `passive checkpoint` 之后都没有保留额外 WAL 内容，因此这次 workload 没有出现 WAL 持续膨胀。
- `wal/normal` 相比 `wal/full` 继续快了约 `2.7%`，同时只读 p95 也略低。

## 6. 决策

### 6.1 当前仓库默认值

当前不调整仓库内建默认值，继续保持：

- `storage.sqlite.journal_mode=delete`
- `storage.sqlite.synchronous=full`

原因有两个：

1. 虽然 ZeppOS 真实子工作区 benchmark 明确显示 `wal/normal` 最快，且只读 busy 风险最低，但当前运行时 SQLite 版本是 `3.50.4`，低于官方回补 WAL-reset bug 的 `3.50.7`。
2. 默认配置是跨环境默认，不应假设所有部署都满足“本地单机、稳定本地文件系统、可接受 `NORMAL` durability 语义”。

### 6.2 明确推荐的 opt-in 策略

对于满足以下前提的本地单机部署：

- `storage.sqlite.assume_local_filesystem=true`
- SQLite runtime `>= 3.50.7` 或 `>= 3.51.3`
- 允许 WAL + `synchronous=normal` 在掉电/OS crash 时丢失最近已提交但未 checkpoint 的事务
- 索引期间确实需要并发只读

推荐显式使用：

```yaml
storage:
  sqlite:
    journal_mode: wal
    synchronous: normal
    assume_local_filesystem: true
```

如果更看重 durability 而不是吞吐，可退回：

```yaml
storage:
  sqlite:
    journal_mode: wal
    synchronous: full
    assume_local_filesystem: true
```

## 7. 对 AR7-02 的结论

`AR7-02` 的“默认策略固化”结论不是“默认启用 WAL”，而是：

- 已有真实工程报告，可以明确说明 WAL 在本地单机索引下有收益。
- 但在当前运行时版本与跨环境默认前提下，不修改全局默认值。
- 把 `wal/normal` 固化为“本地单机、显式 opt-in、SQLite 版本达标”时的首选推荐。

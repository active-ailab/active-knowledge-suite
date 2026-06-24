# Active Knowledge Server AR7 Auto Workers Benchmark (ZeppOS)

> 日期：2026-06-24  
> 目标：为 `AR7-03 auto workers 默认值二次固化` 提供真实工程验证，决定 `resolve_indexing_workers(...)` 在默认 `thread` 模式下的 `code` phase 自动 worker 策略。  
> 工程口径：`/home/gangan/ZeppOS`

## 1. 为什么这轮优先看 collect-only

`indexing.workers` 当前只影响 collect/parse 阶段，不影响 SQLite / LanceDB 单 writer apply。

因此本轮把“默认 worker 数”的主证据收敛到 code collect：

- 复用真实 `WorkspaceConnector.scan()` 与 `CodeIndexer` 选文件逻辑
- 复用真实 `_collect_code_entry_task(...)` / `_collect_code_entry(...)`
- 避免把 overlay 写盘、workspace map、jobs checkpoint 等非 worker 因素混进结论

对应脚本：`active-knowledge-server/scripts/benchmark_code_collect.py`

补充说明：

- `AR7-01` 已有 ZeppOS collect-only 报告证明 `process/hybrid` 在 code collect 阶段显著优于 `thread`，见 [active_knowledge_server_ar7_code_collect_benchmark_zeppos.md](./active_knowledge_server_ar7_code_collect_benchmark_zeppos.md)。
- `AR7-03` 关注的是默认 `workers=auto` 在仓库默认 `parallel.mode=thread` 下该怎么定，因此本轮只扫 `thread`。

## 2. 行业与官方资料结论

- Python `ThreadPoolExecutor` 默认值偏向 I/O overlap，官方文档明确说该默认值是为 “I/O bound tasks” 预留更多 worker，而不是为重 Python 对象处理的 CPU 型任务背书。  
  https://docs.python.org/3/library/concurrent.futures.html
- Python `ProcessPoolExecutor` 通过多进程绕过 GIL，更适合需要真正多核并行的 CPU 型任务，但也带来 pickling / 进程管理成本。  
  https://docs.python.org/3/library/concurrent.futures.html
- Joblib 官方文档指出：
  - `threading` backend 主要适用于 bottleneck 在会释放 GIL 的 compiled extension；
  - 轻度 oversubscription 只在大 I/O 场景下可能有益；
  - process backend 会有通信和内存开销。  
  https://joblib.readthedocs.io/en/stable/parallel.html

结合本项目架构，这意味着：

- `docs` collect 仍可继续保守地把 thread 并行当成 I/O overlap 优化路径。
- `code` collect 不能简单套用 “CPU 越多 worker 越多” 或 “thread 默认多开一些” 的经验。
- `code` 的默认 auto 需要显式区分 `thread` 与 `process/hybrid`。

## 3. ZeppOS 三档样本

为避免整仓 workdir 膨胀，同时保持真实工程特征，本轮把 ZeppOS 切成三档真实子工作区：

| 档位 | 工作区 | 代码文件数 | 说明 |
| --- | --- | ---: | --- |
| Small | `framework/engine/sportEngine` + `configs/mhs003` + `build/.config` | 137 | 小仓，代表单模块增量 |
| Medium | `framework/engine` + `configs/mhs003` + `build/.config` | 621 | 中仓，代表单大目录增量 |
| Large | `framework` + `configs/mhs003` + `build/.config` | 4613 | 大仓，代表大型代码切片 |

环境：

- `nproc=8`
- 基准配置：`examples/local-single-user-zeppos-framework-wal-benchmark.yaml`
- `docs=false`
- `embeddings=false`
- `parallel.mode=thread`

## 4. 执行命令

Small：

```bash
cd active-knowledge-server
for w in 1 2 4 8 auto; do
  uv run python scripts/benchmark_code_collect.py \
    --config ../examples/local-single-user-zeppos-framework-wal-benchmark.yaml \
    --workspace /tmp/zeppos-ar7-workers-small \
    --workers "$w" \
    --parallel-modes thread \
    --repeat 1 \
    --output /tmp/active-kb-ar7-03-collect-small/w${w}.json \
    --summary-output /tmp/active-kb-ar7-03-collect-small/w${w}.md
done
```

Medium：

```bash
cd active-knowledge-server
for w in 1 2 4 8 auto; do
  uv run python scripts/benchmark_code_collect.py \
    --config ../examples/local-single-user-zeppos-framework-wal-benchmark.yaml \
    --workspace /tmp/zeppos-ar7-workers-medium \
    --workers "$w" \
    --parallel-modes thread \
    --repeat 1 \
    --output /tmp/active-kb-ar7-03-collect-medium/w${w}.json \
    --summary-output /tmp/active-kb-ar7-03-collect-medium/w${w}.md
done
```

Large：

```bash
cd active-knowledge-server
for w in 1 2 4 8 auto; do
  uv run python scripts/benchmark_code_collect.py \
    --config ../examples/local-single-user-zeppos-framework-wal-benchmark.yaml \
    --workspace /tmp/zeppos-ar7-workers-large \
    --workers "$w" \
    --parallel-modes thread \
    --repeat 1 \
    --output /tmp/active-kb-ar7-03-collect-large/w${w}.json \
    --summary-output /tmp/active-kb-ar7-03-collect-large/w${w}.md
done
```

## 5. 结果摘要

### 5.1 Small：137 code files

| Workers requested | Effective workers | Wall (s) | Relative to `w=1` |
| --- | ---: | ---: | ---: |
| `1` | 1 | 2.173 | 1.00x |
| `2` | 2 | 2.191 | 1.01x slower |
| `4` | 4 | 2.294 | 1.06x slower |
| `8` | 8 | 2.543 | 1.17x slower |
| `auto` | 4 | 2.330 | 1.07x slower |

结论：小仓 thread collect 最优是串行；当前 `auto=4` 明显不是最优。

### 5.2 Medium：621 code files

| Workers requested | Effective workers | Wall (s) | Relative to `w=1` |
| --- | ---: | ---: | ---: |
| `1` | 1 | 3.204 | 1.00x |
| `2` | 2 | 3.373 | 1.05x slower |
| `4` | 4 | 4.026 | 1.26x slower |
| `8` | 8 | 3.902 | 1.22x slower |
| `auto` | 4 | 3.622 | 1.13x slower |

结论：中仓 thread collect 仍然串行最优；`auto=4` 比 `w=1` 慢约 13%。

### 5.3 Large：4613 code files

| Workers requested | Effective workers | Wall (s) | Relative to best |
| --- | ---: | ---: | ---: |
| `1` | 1 | 21.571 | 1.02x slower |
| `2` | 2 | 21.099 | 1.00x |
| `4` | 4 | 22.272 | 1.06x slower |
| `8` | 8 | 21.966 | 1.04x slower |
| `auto` | 4 | 22.109 | 1.05x slower |

结论：大仓 thread collect 终于出现并行收益，但最佳点只到 `w=2`；继续加到 `4/8` 会回退。

## 6. 决策

### 6.1 `code + thread + auto`

固化为更保守的两段式策略：

- `task_count < 4096`：`workers=1`
- `task_count >= 4096`：`workers=min(2, cpu_count, task_count)`

原因：

- Small / Medium 两档都证明 `thread` 并行没有收益。
- Large 档只在 `w=2` 出现轻微收益，`w=4` 已经退化。
- 因此当前 `auto -> cap 4` 的静态策略对默认 `thread` 模式过于激进。

改动后用同一脚本复跑 `workers=auto` 验证：

- small：resolved `1`，wall `2.097s`
- medium：resolved `1`，wall `3.349s`
- large：resolved `2`，wall `20.135s`

### 6.2 `code + process/hybrid + auto`

本轮不收紧，继续保留 `cap=4`。

原因：

- `AR7-01` 现有 ZeppOS collect-only 报告已证明 `process/hybrid` 在 `workers=4` 时有约 `3.6x-3.7x` 的稳定收益。
- `AR7-03` 这次没有重新扫 `process/hybrid` 的 worker 矩阵，因此不凭 thread 数据反推 process 默认值。

### 6.3 `docs + auto`

本轮不调整，继续保持 `cap=6`。

原因：

- 本轮样本以 ZeppOS code 为主，没有足够 docs phase 数据推翻现有保守值。
- 从官方资料看，thread pool 更多 worker 仍然更符合 I/O overlap 场景。

## 7. 不推荐区间

- 默认 `parallel.mode=thread` 下，不推荐 `code auto -> 4`。
- 在当前 Python 线程模型和 ZeppOS 真实工程上，`code + thread + workers>=4` 没有表现出稳定收益。
- 若用户显式要提高 `code` collect 吞吐，优先建议切到 `parallel.mode=hybrid` 或 `process` 再评估，而不是继续加 thread workers。

## 8. 最终结论

`resolve_indexing_workers(...)` 应从“只按 phase 静态 cap”升级为“按 phase + executor mode 决策”：

- `docs` 继续偏 I/O，保守保留 `cap=6`
- `code + thread` 默认更保守：中小仓串行，大仓最多 `2`
- `code + process/hybrid` 继续保留 `cap=4`

这能让默认 `workers=auto` 更贴近当前仓库的默认执行模式，也和 ZeppOS 真实工程数据一致。

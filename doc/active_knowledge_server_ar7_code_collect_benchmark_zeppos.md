# Active Knowledge Server AR7 Code Collect Benchmark (ZeppOS)

> 日期：2026-06-24  
> 目标：为 `AR7-01 process/hybrid code collect` 提供真实工程验证，确认 `thread | process | hybrid` 在 code collect 阶段的收益和等价性。  
> 工程口径：`/home/gangan/ZeppOS`，`paths.include=framework`

## 1. 为什么改成 collect-only benchmark

完整 `active-kb index` benchmark 会把 workspace map、overlay 写盘、jobs checkpoint 等成本混在一起。对 `AR7-01` 来说，这些都不是主要验证对象，而且在 ZeppOS 全量冷启动时曾触发 `No space left on device`。

因此本轮改为只测 code collect：

- 复用真实 `WorkspaceConnector.scan()` 与 `CodeIndexer` 的 code file 选择逻辑
- 直接比较 `_collect_code_entry_task(...)` 在 `thread/process/hybrid` 下的 wall time
- 对 collect 输出计算稳定 `output_signature`，验证三种模式结果等价

对应脚本：`active-knowledge-server/scripts/benchmark_code_collect.py`

## 2. 执行命令

### 2.1 稳定性样本：512 files，repeat=3

```bash
cd active-knowledge-server
uv run python scripts/benchmark_code_collect.py \
  --config ../examples/local-single-user.yaml \
  --paths-include framework \
  --max-files 512 \
  --workers 4 \
  --parallel-modes thread,process,hybrid \
  --repeat 3
```

### 2.2 放大量级：1024 files，repeat=1

```bash
cd active-knowledge-server
uv run python scripts/benchmark_code_collect.py \
  --config ../examples/local-single-user.yaml \
  --paths-include framework \
  --max-files 1024 \
  --workers 4 \
  --parallel-modes thread,process,hybrid \
  --repeat 1
```

## 3. 结果摘要

### 3.1 512 files，repeat=3

| Mode | p50 wall (s) | p50 parent cpu (s) | p50 parser (s) | Speedup vs thread | Output equivalent |
| --- | ---: | ---: | ---: | ---: | --- |
| thread | 1.138 | 1.242 | 4.535 | 1.00x | yes |
| process | 0.305 | 0.230 | 1.007 | 3.73x | yes |
| hybrid | 0.308 | 0.221 | 1.011 | 3.69x | yes |

### 3.2 1024 files，repeat=1

| Mode | wall (s) | parent cpu (s) | parser (s) | Speedup vs thread | Output equivalent |
| --- | ---: | ---: | ---: | ---: | --- |
| thread | 3.256 | 3.487 | 12.971 | 1.00x | yes |
| process | 0.897 | 0.507 | 2.975 | 3.63x | yes |
| hybrid | 0.880 | 0.515 | 2.971 | 3.70x | yes |

## 4. 结论

- 对 ZeppOS `framework` 的真实工程切片，code collect 明显不是纯 I/O 等待，`process/hybrid` 相比 `thread` 有稳定收益。
- `hybrid` 在 code collect 阶段走 process path，结果与 `process` 接近，和设计预期一致。
- 三种模式的 `output_signature` 完全一致，说明 process/hybrid 没有引入 collect 输出漂移。
- `parent cpu` 只统计 benchmark 主进程自身 CPU，用于观察主进程调度开销，不能直接当成 process 子进程总 CPU 的跨模式对比指标。
- 由于本轮是 collect-only benchmark，而不是完整中仓/大仓全流程落盘 benchmark，`AR7-01` 建议维持 `[~]`，后续可继续把样本放大到 `2048/4096` 文件，或扩到 `framework+drivers` 切片再收口。

## 5. 限制

- 这里验证的是 code collect，不代表 full index 的总耗时会线性获得同等倍数收益。
- full pipeline 在 ZeppOS 大切片上仍会受到 SQLite / workspace map / artifact 写盘容量约束。
- 当前结果支持“保留 process/hybrid 为显式 opt-in，并允许继续推进默认值评估”，但还不建议仅凭这一轮就直接改默认模式。

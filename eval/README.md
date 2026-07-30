# RAG 评估说明

本目录存放确定性评估问题、基线和运行结果。

## 目录

```text
eval/
├── data/questions.jsonl
├── baselines/current_baseline.json
└── results/
    ├── tuning_baseline.json
    └── tuning_optimized.json
```

## 当前状态

问题集包含 30 题：

| 类别 | 题数 |
| --- | ---: |
| `exact_term` | 7 |
| `semantic` | 6 |
| `procedure` | 7 |
| `multi_source` | 3 |
| `relationship` | 3 |
| `unanswerable` | 4 |

划分：

- tuning：18 题。
- holdout：12 题。

当前还没有 `eval/results/final.json`，因此尚未完成 holdout 最终验收。

现有 baseline 的检索来源是 Windows 绝对路径，优化结果主要是相对路径。
`scripts/evaluate_rag.py::_normalize_source()` 已在 2026-07-30 修复，可以把
Windows 绝对路径、`data/docs/...` 和相对路径统一转换到 `data/docs` 相对路径。
历史 JSON 中已经写入的 0 分不会自动重算，因此旧 baseline 仍不能直接与新结果比较。

当前 `tuning_optimized.json`：

```text
failed_count: 0
Recall@5: 0.125
Source Hit Rate: 0.125
```

这些结果没有达到实施方案目标。路径代码已经修复，但活动索引仍不一致；
完成影子重建后需要重新运行 tuning 和 holdout。

## 问题格式

每行一个 JSON：

```json
{
  "id": "q001",
  "split": "tuning",
  "category": "exact_term",
  "question": "问题",
  "ground_truth": "标准答案",
  "relevant_sources": [
    {
      "source": "相对于 data/docs 的路径",
      "page": null,
      "section": "章节",
      "evidence": "原文证据"
    }
  ],
  "answer_keywords": ["关键词"],
  "unanswerable": false
}
```

## 命令

```powershell
# 只运行检索、融合和重排
python -m scripts.evaluate_rag --mode retrieval --split tuning --label run

# 运行完整问答
python -m scripts.evaluate_rag --mode full --split tuning --label run

# 参数确定后运行一次 holdout
python -m scripts.evaluate_rag --mode full --split holdout --label final

# 比较结果
python -m scripts.evaluate_rag --compare <baseline.json> <final.json>
```

`full` 模式会调用当前 LLM，可能产生云端费用或占用本地模型计算资源。

## 指标

检索：

- Recall@5、Recall@10。
- MRR@10、nDCG@10。
- Source Hit Rate。

回答：

- Answer Keyword Recall。
- 无答案问题相关指标。

性能：

- 各阶段平均、p50、p95。
- 总耗时平均、p50、p95。

结果文件存在不代表评估通过。验收时还需要确认：

1. 评估集 hash 和语料 fingerprint 一致。
2. baseline 与 final 使用同一种来源路径格式。
3. 活动 Chroma collection 与 SQLite Chunk 数量一致。
4. Provider、模型、Reranker 和参数可比。
5. holdout 没有用于反复调参。

[English](GRAPHITI_SHADOW.md) | **中文**

# Graphiti 影子评测

使用 Graphiti 0.31.2、独立 FalkorDB、FalkorDB Python 驱动 1.8.2 和 `gpt-4.1-mini`。评测数据为两个项目的 7 条已批准合成记录，含 9 道普通、关系和时态问题，每题串行跑两次。Postgres 使用确定性的 hash 向量，旧排序和现行项目排序读取同一份合成数据。

| 题型 | 旧 Postgres Recall@5 | 现行 Postgres Recall@5 | Graphiti Recall@5 |
|---|---:|---:|---:|
| 普通 | 1.0000 | 1.0000 | 0.6667 |
| 关系 | 0.7222 | 1.0000 | 1.0000 |
| 时态 | 1.0000 | 1.0000 | 1.0000 |
| 检索 p95 | 4.7 毫秒 | 6.9 毫秒 | 233.1 毫秒 |

Graphiti 的原始候选没有跨项目。刻意保留的过期来源出现在 18 次原始图谱候选和 16 次旧 Postgres 结果里。现行 Postgres 和经核验的 Graphiti 结果均为 0 次过期命中。Postgres 也拒绝了已被替换、来自其他项目和版本错误的候选。建图用时 28.45 秒，Graphiti 报告 LLM 输入 30,529 tokens、输出 947 tokens。此次未统计向量账单或生产规模的并发。

图谱读取保持关闭。关系和时态召回与 Postgres 持平，普通召回下降。结果只覆盖这一小组合成数据和独立 FalkorDB。FalkorDB Lite 的平台、并发写入与恢复仍待实测。

## 复测

使用一次性的本地 `_test` 数据库和仅绑定回环地址的 FalkorDB。脚本会在这两个测试服务中创建合成数据和图谱前缀，跑完即可清理。把 Graphiti 装在独立于 Porter 服务的环境中。

```console
python -m pip install "graphiti-core[falkordb]==0.31.2"
python -m scripts.eval_graphiti --port 16379
```

为评测进程设置 `PORTER_TEST_DATABASE_URL` 和 `OPENAI_API_KEY`。发给模型的内容来自脚本内的合成数据。[评测脚本](../scripts/eval_graphiti.py) 从 Postgres 重建索引，为各项目使用独立图谱组，并在计分前向 Postgres 核对候选 ID 和版本。正常检索始终由 Postgres 提供。停止评测进程或 FalkorDB 不影响 Postgres 回填与既有 Wiki 工具。

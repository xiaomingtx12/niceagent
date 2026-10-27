---
name: smart-charts
description: 分析 CSV、TSV、TXT、Excel 或 JSON 数据并生成交互式 ECharts HTML 图表。用户要求数据分析、趋势/对比/占比可视化或从表格生成图表时使用；不适用于需要联网抓取、三维渲染或未经确认的统计口径。
metadata:
  my_dify:
    version: "1.0.0"
    category: data
    execution_mode: sandbox
    requires:
      - python
      - pandas
      - numpy
      - openpyxl
allowed-tools:
  - read_file
  - write_file
  - execute
---

# Smart Charts

用包内 CLI 将用户数据转为 `/workspace/` 下的 HTML。先读数据结构和统计口径，再选图；不要打开 HTML 反向猜数据。

## 标准流程

1. **读取与检查**：确认文件格式、编码、工作表、表头行、缺失值和重复实体。脏表头用 CLI 的 `--skiprows`/`--header-row`/`--sheet` 处理。
2. **选图与变换**：按数据形态选择 `line`、`bar`、`pie`、`scatter`、`heatmap`、`boxplot`、`funnel`、`wordcloud` 或 `spreadsheet` 等类型；列重命名、过滤、透视和聚合统一写在 `--transform-code`。
3. **先 dry-run**：执行 `python /skills/smart-charts/scripts/cli.py <input> <type> --dry-run`，读取 stdout 的 `data_preview`、`data_rows` 和 `plot_stats`，核对“按行/按实体”的口径。
4. **正式生成**：根据统计结果写 2~4 句真实解读，通过 `--annotation` 注入 HTML；输出路径必须位于 `/workspace/`，并确认文件存在且非空。
5. **交付**：提供图表链接/路径、图表类型、数据范围、关键假设、解读和任何不支持的情况。数字只能来自 CLI 输出。

## Transform 约束

沙箱只允许 `df`、`pd`、`np`，必须产出名为 `result` 的 DataFrame；禁止 import、open、try 和类定义。列名会被规范化，x/y 轴必须引用规范化名称。不要在生成代码中硬编码宿主机绝对路径。

## 典型命令

```bash
python /skills/smart-charts/scripts/cli.py data.xlsx bar \
  --title "分类汇总" --x-axis name --y-axis value \
  --transform-code "result = df['类别'].value_counts().rename_axis('name').reset_index(name='value')" \
  --output-dir /workspace/charts
```

依赖缺失、数据格式不支持或 CLI 连续两次修正仍失败时，报告实际错误与建议；不得静默改用自写脚本绕过 CLI。详细图表类型、参数和错误码见 [references/REFERENCE.md](references/REFERENCE.md)。

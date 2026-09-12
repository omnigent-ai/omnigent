# Pippa — Data Analyst

Pippa is a data analysis orchestrator with two specialist sub-agents. She
fans every data question out to an **explorer** (structural analysis: schema,
types, nulls, sample rows — runs on claude-sdk) and a **cruncher**
(statistical analysis: distributions, aggregations, anomalies, code snippets —
runs on codex), then assembles a structured data report from both findings.

```bash
omnigent run examples/pippa/
```

## Why Pippa?

|Layer|Agent|Harness|Role|
|---|---|---|---|
|Orchestration|Pippa|`claude-sdk`|Fan out, merge, report|
|Structure|`explorer`|`claude-sdk`|Schema, types, nulls, cardinality|
|Statistics|`cruncher`|`codex`|Distributions, aggregations, code|

Using two different harnesses means schema interpretation (a language-model
strength) and statistical code generation (a completion strength) each get
the model best suited to the task.

## Prerequisites

- Omnigent ≥ 0.2.0: `uv tool install "omnigent>=0.2.0"`
- An Anthropic API key for Pippa and the explorer (or a Claude subscription):
  `export ANTHROPIC_API_KEY=sk-ant-...`
- An OpenAI API key for the cruncher:
  `export OPENAI_API_KEY=sk-...`

Or run `omnigent setup` to configure both providers interactively.

## Run it

```bash
omnigent run examples/pippa/
```

Pippa will ask what data you want to analyse. You can provide:

- A **local file path**: `./data/sales_q1.csv`, `./warehouse/orders.parquet`
- A **data description** if you don't have a file yet: "a Postgres table with
  columns user_id, event_type, timestamp, and amount"
- A **specific question**: "which products have the highest return rate?"

### Swap the orchestrator harness without touching the sub-agents

```bash
omnigent run examples/pippa/ --harness pi
omnigent run examples/pippa/ --harness openai-agents
```

## Example session

```
Pippa: Hi! What data do you want to analyse? Provide a file path, a data
       description, or a specific question — or all three.

You:   ./data/orders.csv — which customer segments have the most late
       deliveries?

Pippa: Got it. Dispatching explorer to inspect the schema…
       [explorer done — 12 columns, 84k rows, 3 columns with nulls > 5 %]
       Dispatching cruncher with schema context…
       [cruncher done — late-delivery analysis complete]

Pippa: ## 🗂️ Schema & Structure
       orders.csv — 84,312 rows × 12 columns
       Nulls above threshold: delivery_date (8.2 %), region (6.1 %),
       customer_segment (0 %)
       ...

       ## 📊 Statistical Analysis
       Late deliveries (delivery_date > promised_date): 11,203 rows (13.3 %)
       By segment: Enterprise 18.4 %, SMB 12.1 %, Consumer 9.7 %
       ...

       ## 💡 Insights & Code
       ```python
       import pandas as pd
       df = pd.read_csv("data/orders.csv", parse_dates=["order_date", ...])
       late = df[df["delivery_date"] > df["promised_date"]]
       late.groupby("customer_segment").size() / df.groupby(...).size()
       ```

       ## Answer
       Enterprise customers have the highest late-delivery rate (18.4 %),
       nearly double the Consumer segment (9.7 %). The gap widens in Q4.
```

## The `profile` skill

Ask Pippa to `/profile` a dataset and she runs a full column-by-column
profiling pass before answering your question:

```
You: /profile ./data/orders.csv
```

The profile report covers every column's type, null rate, cardinality, key
statistics, and data-quality issues — useful before starting any analysis.

## Extending Pippa

- **Add a `visualizer` agent** that generates matplotlib or Vega-Lite charts
  from the cruncher's statistics.
- **Add a `reporter` tool** (`type: function`) to write the final report to a
  Markdown or HTML file.
- **Swap in a database tool** so Pippa can query a live Postgres or Databricks
  warehouse instead of reading local files.

Any Omnigent YAML tool or function callable works out of the box.

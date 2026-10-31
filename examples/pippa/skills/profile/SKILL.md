---
name: profile
description: Run a column-by-column data profile before answering the user's question. Use when the user asks for a data quality report, full profiling pass, or column summary — or types /profile.
---

# profile — full column-by-column data profiling

Normally Pippa fans the user's question out to both analysts and answers it
directly. **profile** goes further: it first runs a comprehensive profiling
pass across every column in the dataset, then uses those findings as grounding
context when answering the user's specific question.

## When to use

- User asks for a "data quality report", "profile", "column summary", or
  "full analysis".
- User types `/profile`.
- The question touches data quality in a way that requires a complete picture
  of every column (e.g. "which columns have data issues?").

## Procedure

1. **Profile pass — dispatch explorer first.**
   Send `explorer` a profiling request: ask it to inspect EVERY column in
   the dataset and return the full structural report (schema, null rates,
   cardinality, sample rows, notable observations for all columns). Use a
   title like `profile-explorer`. End your turn and wait for the result.

2. **Statistical pass — dispatch cruncher with explorer's output.**
   Once explorer returns, send `cruncher` the full explorer report plus the
   user's question. Ask cruncher to compute statistics for EVERY numeric and
   categorical column (not just those relevant to the question), flag outliers
   or anomalies per column, and answer the user's question. Use a title like
   `profile-cruncher`. End your turn and wait for the result.

3. **Assemble the profile report.**
   Once both analysts return, present:

       ## 📋 Data Profile — <dataset name or file path>

       ### Overview
       <row count, column count, total null rate, file size if known>

       ### Column Profiles
       For each column, one row in a markdown table:
       | Column | Type | Nulls | Distinct | Notes |
       |--------|------|-------|----------|-------|
       | ...    | ...  | ...%  | ...      | ...   |

       ### Statistical Highlights
       <cruncher's key stats, distributions, correlations, and anomalies>

       ### Data Quality Issues
       <ranked list: high / medium / low severity, with column name and
        description of the issue>

       ### Answer
       <direct answer to the user's original question, grounded in the
        full profile>

       ### Suggested Next Steps
       <2–4 concrete follow-up actions: cleaning steps, further queries,
        or visualisations worth running>

## Notes

- The profile pass covers ALL columns, even ones not directly asked about —
  the value is in the complete picture.
- If the dataset is very wide (> 50 columns), group columns by type and
  summarise each group rather than producing one row per column — keep the
  report scannable.
- Do not invent statistics. If a value cannot be computed (unsupported format,
  file too large to read in full), mark the cell as `—` and note the
  limitation in the Quality Issues section.

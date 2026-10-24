# Reconcile reliability dimension queries

The headline can apply newer cause exclusions and reconnect classifications than
an exported serving snapshot's stored error flag. Summing that flag in the harness
and version widgets makes their counts disagree with the headline.

The offline repair tool copies the current headline CTEs through
`window_included`, joins that error membership to the existing version-session
population, and retains the dimension dataset's aggregation and output schema.
It supports either a serialized dashboard or the full Lakeview API export.

```sh
python -m dev.reliability_dashboard dashboard-export.json dashboard-candidate.json
```

The input is preserved, an existing output is never overwritten, and unsupported
query shapes are refused. Custom dataset names can be supplied with `--headline`
and `--dimensions`. The tool makes no network requests.

Before publishing, review that only the dimension dataset's `queryLines` changed.
Execute its candidate query and the headline against the same completed snapshot
and filters. Each of the harness and version aggregations must sum to the
headline's seven-day active and error-session counts. Each group's active-session
count must match the old query. Check both a single-harness selection and all
harnesses. Publish through the deployment's normal dashboard workflow only after
that comparison. Keep the original export for rollback.

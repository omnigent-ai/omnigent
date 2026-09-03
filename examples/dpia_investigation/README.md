# DPIA Investigation Agent Bundle

This bundle runs the optional live path behind the DPIA Investigation Desk. It
uses only the synthetic Student Success Alert evidence under `resources/`.
Never use it with real student, disability, wellbeing, hardship, attainment, or
intervention data.

## Roles and sequence

```text
Process Investigator
        |
        +--> Privacy Assessor ---------------------+
        |                                          |
        +--> Sanitized blind Verifier Stage 1      |
                    |                              |
                    +--> frozen artifact + SHA-256|
                                                   v
                                      Verifier Stage 2
```

The Process Investigator establishes facts and questions but cannot make the
screening recommendation. After its artifact validates, the coordinator creates
`verification-stage-1-input.json` with `prepare_stage_one.py`. The Assessor and
fresh blind Verifier are dispatched in parallel before either result is read.
Stage 2 receives the frozen Stage-1 artifact, its computed SHA-256 digest, and
the Assessor artifact.

`validate_artifact.py` checks each JSON Schema and the relationships between
artifacts: case and policy/model versions, evidence and policy references,
eight-dimension coverage, risk IDs, verifier finding IDs, the compromise flag,
and the frozen Stage-1 hash. Any nonzero exit stops the workflow. Exit code `3`
means Stage 1 detected prohibited Assessor context and must restart in a fresh
Verifier session.

## Register and label a live root session

Register the bundle when starting the server:

```bash
omnigent server --agent examples/dpia_investigation
```

The web adapter discovers a root session only when it has both labels:

```text
omnigent.product=dpia-investigation
omnigent.case_id=student-success-alert
```

The current browser processing model is sent in the live request and is
authoritative over the bundled v3 baseline. Live output remains separate from
the reviewed frontend snapshot and is never imported automatically.

## Validate the bundle

The structural suite loads the real Omnigent parser without making an LLM call:

```bash
uv run --no-sync pytest -q \
  tests/e2e/omnigent/test_example_dpia_investigation.py
```

The individual artifact commands are recorded in `config.yaml`. Live artifacts
are written beneath ignored `dpia-output/`; evidence, policy, and schemas remain
immutable inputs.

## Demo boundary

This is a demonstration adapter, not a compliance system of record. Artifacts
use the local session workspace, the officer-facing snapshot uses versioned
browser storage, and neither provides tenant isolation, immutable audit,
production retention/deletion enforcement, private networking, or suitability
for confidential university data.
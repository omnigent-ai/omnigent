# Experimental sandbox actions example

This external package registers an `example` provider through
`omnigent.sandbox_providers`. Its fake controller supports two actions:

| Action | Parameters | Result |
| --- | --- | --- |
| `example.inspect.v1` | `{}` | JSON describing the source handle |
| `example.create_child.v1` | `{"name": "candidate"}` | A typed child handle for a future core handoff |

Both actions require a `source` target. Parameters are strictly validated by
Pydantic models supplied by the provider. The demo constructs its own trusted
target references; a production caller must resolve and authorize them first.

From a development checkout with Omnigent installed, run:

```bash
uv pip install --no-deps -e examples/sandbox-actions
uv run --no-sync python -m omnigent.community.sandbox.action_example
```

The demo uses installed entry-point discovery and checks inspection, child
creation, replay through a new launcher, rejection of changed parameters under
the same operation ID, and child-scoped cleanup. It prints a success message if
all assertions pass.

The controller is single-threaded, and its handles and operation records exist
only in this Python process. The example does not create hosts, copy files, or
contact a sandbox service. It declares no managed-launch support, and
`start_host()` raises an unsupported
capability error. Returned child handles demonstrate the SDK result shape;
they cannot be attached to a running Omnigent session.

Production providers need durable, concurrency-safe idempotency records and
independently terminable child resources. See
[the experimental contract](../../docs/extending/experimental_sandbox_actions.md)
for the core authorization, identity, readiness, and cleanup handoff still to
be implemented.

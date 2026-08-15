# Rovo CLI harness

Omnigent integrates the current standalone Atlassian Rovo CLI through its
Agent Client Protocol server:

```bash
rovo auth login
```

```yaml
executor:
  harness: rovo
```

The builtin harness launches `rovo acp` and does not force YOLO mode. You can
choose Rovo's permission behavior in its own configuration or invoke the CLI
with YOLO when running it outside Omnigent. The `rovodev` alias is also
accepted.

Rovo owns its authentication, model selection, memory, skills, MCP
configuration, and vendor tools. Omnigent supplies its MCP bridge, streams ACP
events, forwards reasoning/images where supported by the CLI, and handles
interrupts, tool permissions, session turns, and cost metadata through the
shared ACP executor.

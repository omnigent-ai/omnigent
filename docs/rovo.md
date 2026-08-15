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

The builtin harness launches `rovo acp` and leaves Rovo's permission behavior
under the control of the Rovo CLI. The `rovodev` alias is also accepted.

Rovo owns its authentication, model selection, memory, skills, MCP
configuration, and vendor tools. Omnigent supplies its MCP bridge, streams ACP
events, forwards reasoning/images where supported by the CLI, and handles
interrupts, tool permissions, session turns, and cost metadata through the
shared ACP executor.

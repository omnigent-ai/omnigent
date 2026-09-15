# CodeBuddy

Use Tencent's CodeBuddy coding agent from Omnigent through its
[Agent Client Protocol (ACP) interface](https://www.codebuddy.cn/docs/cli/acp).
Omnigent runs `codebuddy --acp` on the selected host. CodeBuddy keeps control of
its login, model configuration, and account billing.

## Set up the host

Install and sign in on the machine that will run the agent:

```bash
npm install -g @tencent-ai/codebuddy-code
codebuddy
```

Complete the CLI's browser sign-in, then exit it. An existing working CodeBuddy
login can be reused. See the official [quickstart](https://www.codebuddy.cn/docs/cli/quickstart)
for regional and enterprise login options.

From your project directory, run:

```bash
omnigent run --harness codebuddy -p "Do not use tools. Reply with CODEBUDDY_ACP_OK."
```

The response should stream back through Omnigent. The harness aliases `cbc`
and `codebuddy-code` work too. In the web or desktop UI, choose **CodeBuddy**
from the harness menu on that host; `omnigent setup` also lists it and its
installation and login steps.

## Models and credentials

This integration uses the model configured in CodeBuddy. Set it in the CLI
with `/model` before starting a new Omnigent session. Omnigent's model override
is not enabled for this harness, so passing `--model` is rejected.

Omnigent does not collect a CodeBuddy API key or import its credentials. The
generic ACP subprocess uses a restricted environment; exported CodeBuddy API
keys or custom provider variables are not automatically forwarded. If your
setup needs environment-based authentication, use the existing
[custom ACP configuration](AGENT_YAML_SPEC.md) and explicitly declare the
required `env_passthrough` names.

Tencent's [China-site pricing documentation](https://www.codebuddy.cn/docs/ide/Account/pricing)
describes shared CodeBuddy/WorkBuddy credits under the same account. Account
eligibility and deductions remain CodeBuddy's responsibility. This integration
runs the CodeBuddy CLI; it does not control the WorkBuddy application or
implement credit tracking in Omnigent.

## Verification and limitations

The opt-in test uses a real, signed-in CodeBuddy CLI. It checks streaming text,
context across follow-up turns, and an approved Omnigent tool call through the
ACP MCP relay. It consumes the CLI account's allowance:

```bash
OMNIGENT_E2E_CODEBUDDY=1 \
  python -m pytest tests/e2e/test_codebuddy_acp_e2e.py -v
```

For a nonstandard installation, set `OMNIGENT_CODEBUDDY_PATH` to the executable
or a wrapper. The test fails on startup/login errors when explicitly enabled;
it does not silently skip a broken installation.

CodeBuddy uses the shared ACP permission and cancellation handling. Omnigent
does not advertise ACP terminal delegation, so CodeBuddy keeps its own shell
tools. Model selection in Omnigent, reasoning effort controls, and usage/credit
reporting are separate capabilities.

If startup fails, confirm that `codebuddy` starts and answers a prompt directly
on the selected host, check that `codebuddy --help` lists `--acp`, and complete
any login required by that CLI. A binary being detected does not prove that
its account is signed in.

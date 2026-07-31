"""Entry point for the ``echo-native`` example community harness.

``get_contribution()`` is what core's entry-point loader calls. Per the plugin
import rules it must stay import-light: it constructs pure-data registry rows
and dotted import-path *strings* only — it never imports the runner / CLI /
provider stack (those modules are imported lazily by the resolver at dispatch
time, from the paths named here).

This is the reference native contribution: it pairs one ``NativeCodingAgent``
(identity) with one ``NativeHarnessProvider`` (behavior hook paths), both keyed
``"echo"``, and declares the ``echo-native`` harness id + a reversed
``native-echo`` alias. Every hook path lives under
``omnigent.community.harness.echo.*`` so core's community-prefix validation
accepts it (see ``_validate_native_contribution``).
"""

from __future__ import annotations

from omnigent.harness_capabilities import (
    AuthModel,
    EffortFamily,
    Elicitation,
    ForkHistory,
    HarnessCapabilities,
    IntegrationMode,
    ModelFamily,
    Resume,
)
from omnigent.harness_install_spec import HarnessInstallSpec
from omnigent.harness_plugins import (
    HarnessContribution,
    NativeCodingAgent,
    NativeHarnessProvider,
)

_MODULE = "omnigent.community.harness.echo"


def get_contribution() -> HarnessContribution:
    """Return the ``echo-native`` harness contribution.

    Kept import-light: only registry types + capability enums, no runtime
    modules. The behavior hooks are dotted strings resolved lazily by core.
    """
    agent = NativeCodingAgent(
        key="echo",
        display_name="Echo",
        agent_name="echo-native-ui",
        harness="echo-native",
        wrapper_label="echo-native-ui",
        terminal_name="echo",
    )
    provider = NativeHarnessProvider(
        key="echo",
        # CLI + resume launch entry point (`omnigent echo` would call this once
        # the CLI loop is registry-driven; today it's exercised via resume).
        run_native=f"{_MODULE}.echo_native:run_echo_native",
        # Runner terminal builder — the adapter the launch/attach seam resolves
        # and calls with a NativeLaunchContext.
        auto_create_terminal=f"{_MODULE}.runner:launch_echo",
        # Per-spawn env vars derived from the agent spec.
        spawn_env_builder=f"{_MODULE}.echo_native_bridge:build_echo_native_spawn_env",
        # Built-in agent seeding: materialize the wrapper agent's spec YAML.
        materialize_agent_spec=f"{_MODULE}.echo_native:materialize_echo_agent_spec",
    )
    return HarnessContribution(
        name="omnigent-echo-native",
        valid_harnesses=frozenset({"echo-native"}),
        harness_modules={"echo-native": f"{_MODULE}.inner.echo_native_harness"},
        aliases={"native-echo": "echo-native"},
        native_harnesses=frozenset({"echo-native"}),
        native_agents=(agent,),
        native_providers=(provider,),
        harness_labels={"echo-native": "Echo"},
        install_specs={
            "echo-native": HarnessInstallSpec(
                "Echo",
                "echo",
                package=None,
                install_hint="the echo example ships a stub CLI; no install needed",
            )
        },
        harness_install_keys={"echo-native": "echo-native", "native-echo": "echo-native"},
        capabilities={
            "echo-native": HarnessCapabilities(
                IntegrationMode.NATIVE_TUI,
                Elicitation.APPROVAL_MIRROR,
                Resume.WARM_REATTACH,
                EffortFamily.NONE,
                ModelFamily.MULTI,
                AuthModel.OWN_AUTH,
                subagents=False,
                interrupt=True,
                streaming=True,
                fork_history=ForkHistory.NONE,
                shell_tool_name="bash",
                shell_tool_prompt=(
                    "Use your shell tool to run this exact command: echo omnigent-bench-ok"
                ),
            )
        },
    )

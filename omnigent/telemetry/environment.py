"""Usage-telemetry opt-outs shared by the server client and local launcher."""

from __future__ import annotations

from collections.abc import Mapping

DISABLE_TELEMETRY_ENV_VAR = "OMNIGENT_DISABLE_TELEMETRY"
CI_ENV_VARS = frozenset(
    {
        "CI",
        "GITHUB_ACTIONS",
        "PYTEST_CURRENT_TEST",
        "CIRCLECI",
        "JENKINS_URL",
        "TRAVIS",
        "GITLAB_CI",
        "TF_BUILD",
        "BITBUCKET_BUILD_NUMBER",
        "CODEBUILD_BUILD_ARN",
        "BUILDKITE",
        "TEAMCITY_VERSION",
    }
)


def is_disabled_by_env(env: Mapping[str, str]) -> bool:
    """Check usage-telemetry opt-outs without reading config or cached state."""
    if env.get("OMNIGENT_ANALYTICS", "").strip() == "0":
        return True
    for name in ("DISABLE_TELEMETRY", DISABLE_TELEMETRY_ENV_VAR):
        if env.get(name, "").strip().lower() in ("1", "true", "yes"):
            return True
    return env.get("DO_NOT_TRACK", "").strip() == "1" or any(name in env for name in CI_ENV_VARS)

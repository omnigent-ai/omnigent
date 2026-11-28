"""Route patterns for the agent sources the new-session picker reads."""

import re

# The caller's own agents, GET /v1/agents?scope=user. Agents that earlier tests left
# on the shared server appear here, so a test that stubs the picker's agents stubs this too.
OWN_AGENTS = re.compile(r"/v1/agents\?(?=.*\bscope=user\b)")

"""Re-attach timing shared by the server and the native permission hooks."""

# How long the server keeps an answer submitted while no hook poll is attached.
# A hook that re-POSTs the same elicitation id within this time receives it.
PRE_RESOLVED_ELICITATION_TTL_S = 300.0

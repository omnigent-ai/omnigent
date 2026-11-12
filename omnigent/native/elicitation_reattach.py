"""Re-attach timing shared by the server and the native permission hooks."""

# A native permission hook re-POSTs a held poll that has been silent this long,
# just past the Databricks front door's 300s idle cut. A poll whose socket died
# without a close (a host network outage) only surfaces when this elapses.
HELD_POLL_READ_TIMEOUT_S = 330.0

# How long the server keeps an answer submitted while no hook poll is reading it.
# A hook that re-POSTs the same elicitation id within this time receives it.
# A dead socket can keep the poll looking attached, so the server stores the
# answer at submit time while the hook only notices at the read timeout; the
# answer must outlast that plus a reconnect.
PRE_RESOLVED_ELICITATION_TTL_S = HELD_POLL_READ_TIMEOUT_S + 90.0

"""Host connection management for ``omnigent host``."""

# Exit code for a permanent, non-retryable startup failure: bad or revoked
# credentials, an outdated server. Distinct from a crash so a supervisor can
# stand down instead of retrying a failure that can never succeed — without it
# a bad token in a remote sandbox becomes an invisible restart loop. 78 is
# ``EX_CONFIG`` from sysexits.h.
HOST_FATAL_EXIT_CODE = 78

# Exit code a shell reports for a SIGTERM-killed process (128 + 15). A
# supervisor treats it as a deliberate stop, not a crash.
HOST_SIGTERM_EXIT_CODE = 143

# Runner forkserver status loss

`runner_zygote_status_unavailable` records when a host cannot obtain a forked
runner's exit status from its original parent. Correlate by `session_id`,
`runner_id`, `runner_pid`, `zygote_pid`, hostname and forkserver generation.

`child_alive=true` keeps the runner alive from the host's perspective. Once a
direct process probe confirms it is gone, `reported_runner_exit_code=254` is
the existing unknown-status sentinel. `runner_exit_code_known=false` means
that value is not a measured child exit code. A real child exit of 254 obtained
from a healthy parent does not emit this event.

`loss_reason` distinguishes replacement generations, absent control sockets,
control I/O failures, EOF and invalid replies. `control_error_type` records
only the exception class. `zygote_returncode` and `zygote_signal`, when known,
belong to the original parent, including after a replacement parent starts.
They do not identify what initiated its termination.

Each runner handle emits at most one event while its child is still alive and
one after it is confirmed gone. No environment, command arguments, raw replies
or exception messages are added. Session decisions and exit reporting retain
their existing behavior.

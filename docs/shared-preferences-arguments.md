# Native argument defaults on every entry point

Adopted native sessions apply the frozen per-harness argument list on the
runner. Explicit session arguments follow that list in their original order.
An empty saved list clears the default arguments. No arguments are sorted or
deduplicated: repeated flags can be meaningful to native CLIs.

CLI launches send only explicit arguments after adopting preferences. This
avoids applying the same defaults both on the CLI machine and the runner.
Browser-created sessions reach the same runner behavior. Claude and Codex
already merged arguments on the runner; the other ten native adapters now
follow that model for adopted sessions.

Legacy sessions retain their persisted argument behavior. Temporary-default
launches remain local and never publish. Configured executables and credentials
stay on each machine.

Verification extends the existing CLI/runner argument matrix, native terminal
launch matrix, and host-to-real-child startup scenario. Terminal specs are
captured in the native matrix; the real child composes arguments without running
a vendor model. These checks do not prove every vendor's interpretation of an
arbitrary saved flag.

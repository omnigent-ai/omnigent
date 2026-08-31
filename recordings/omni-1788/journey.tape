# OMNI-1788 facet 1 (before-fix): an expected offline-runner resource 503 is
# logged by the server as ERROR + full traceback.
Output recordings/omni-1788/before-error-log.mp4
Set Shell bash
Set FontSize 15
Set Width 1400
Set Height 800
Set TypingSpeed 30ms
Type "bash repro-work/show_bug.sh"
Enter
Wait+Screen /--> HTTP 503/
Sleep 2s
Type "bash repro-work/show_log.sh"
Enter
Wait+Screen /OmnigentError: runner/
Sleep 4s

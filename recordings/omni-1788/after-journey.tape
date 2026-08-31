# After-fix: the expected offline-runner resource 503 is now logged at INFO,
# with no ERROR line and no traceback.
Output recordings/omni-1788/after-error-log.mp4
Set Shell bash
Set FontSize 15
Set Width 1400
Set Height 800
Set TypingSpeed 30ms
Type "bash resolve-work/show_bug.sh"
Enter
Wait+Screen /--> HTTP 503/
Sleep 2s
Type "bash resolve-work/show_log.sh"
Enter
Wait+Screen /no ERROR line, no traceback/
Sleep 4s

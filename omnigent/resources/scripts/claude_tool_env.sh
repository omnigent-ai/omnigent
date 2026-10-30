# Sourced by Claude Code before each Bash command in an Omnigent pane.
# Tools must not address the private tmux server that runs Claude itself.
unset TMUX TMUX_PANE

# Keep custom setup, including its view of CLAUDE_ENV_FILE, after the scrub.
if [ -n "${OMNIGENT_CLAUDE_USER_ENV_FILE:-}" ] && \
    [ "$OMNIGENT_CLAUDE_USER_ENV_FILE" != "${CLAUDE_ENV_FILE:-}" ]; then
    CLAUDE_ENV_FILE="$OMNIGENT_CLAUDE_USER_ENV_FILE" . "$OMNIGENT_CLAUDE_USER_ENV_FILE"
fi

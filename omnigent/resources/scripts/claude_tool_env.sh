# Sourced by Claude Code before each Bash command in an Omnigent pane.
# Tools must not address the private tmux server that runs Claude itself.
unset TMUX TMUX_PANE
# Claude's stdin pipe never closes, and Claude skips its own </dev/null for
# heredoc commands, so a stray stdin reader (pathless rg, cat) would hang.
exec </dev/null

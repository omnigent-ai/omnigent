Output recordings/OMNI-914/after-claude-silent-opus.mp4
Set FontSize 15
Set Width 1400
Set Height 800
Set Theme "Dracula"
Set TypingSpeed 30ms
Type "# Fixed: an unpinned Claude-SDK session resolved against a live-shape workspace"
Enter
Type "# catalog now emits a WARNING naming the substituted model (opus) instead of"
Enter
Type "# silently routing to the most expensive endpoint. Tests PASS:"
Enter
Sleep 1s
Type "PYTHONPATH=$PWD /home/runner/work/omnigent-internal/omnigent-internal/omnigent-src/.venv/bin/python -m pytest tests/inner/test_model_misresolution.py -k 'unpinned or explicitly_selected' -v --no-header -p no:cacheprovider 2>&1 | grep -vE 'RSS|slow' | tail -12"
Enter
Sleep 18s
Type "# ^ the substitution is observable (WARNING names the resolved opus endpoint),"
Enter
Type "#   and an explicitly selected model is still dispatched verbatim."
Enter
Sleep 4s

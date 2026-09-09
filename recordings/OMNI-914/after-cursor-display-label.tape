Output recordings/OMNI-914/after-cursor-display-label.mp4
Set FontSize 15
Set Width 1400
Set Height 800
Set Theme "Dracula"
Set TypingSpeed 30ms
Type "# Fixed: the Cursor model switcher journey (pick 'Composer' -> send a prompt)."
Enter
Type "# The executor now rejects unresolvable ids BEFORE the SDK boundary with an"
Enter
Type "# actionable error; valid catalog ids still dispatch verbatim. Tests PASS:"
Enter
Sleep 1s
Type "PYTHONPATH=$PWD /home/runner/work/omnigent-internal/omnigent-internal/omnigent-src/.venv/bin/python -m pytest tests/inner/test_model_misresolution.py -k 'cursor_display_label or cursor_typo' -v --no-header -p no:cacheprovider 2>&1 | grep -vE 'RSS|slow' | tail -12"
Enter
Sleep 18s
Type "# ^ both live-facet tests now PASS: the raw label never reaches AsyncAgent.create."
Enter
Sleep 4s

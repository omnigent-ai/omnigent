"""Frozen compatibility namespace for import paths stored outside the repo.

Nothing new goes here. ``nessie.policies`` stays importable because database
rows store its policy handler paths; the other modules are aliases for the old
library API, removed in 0.19.0. See ``docs/ARCHITECTURE.md``.
"""

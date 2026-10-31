"""Skills fetched from git repositories and installed server-side.

The CLI's ``omnigent agent add`` and the skills marketplace share one hardened
GitHub fetch (:mod:`omnigent.skills.git_source`). A fetched checkout is
indexed into a catalog (:mod:`omnigent.skills.catalog`), an installed skill is
stored as a tarball of its directory, and installed skills ride into a session
as bundle skills through :mod:`omnigent.skills.overlay`.
"""

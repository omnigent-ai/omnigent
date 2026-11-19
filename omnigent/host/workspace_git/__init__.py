"""Read-only Git for the workspace API, through one hardened runner.

:mod:`.runner` starts every Git process the API needs, with a clean
environment, configuration that turns off hooks, helpers, filters and
transports, one deadline per call, output caps and bounded cleanup.
:mod:`.files` lists a workspace's files through it.
"""

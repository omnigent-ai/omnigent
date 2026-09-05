"""End-to-end Smart Routing CUJ suite.

Opt-in (marker ``smart_routing`` + ``OMNIGENT_E2E_SMART_ROUTING=1``). The
routing service is a deterministic in-test mock speaking the real
``routes:select`` contract, so no AI-Gateway ``task_v1`` deployment is needed;
everything else — server, host, ``claude`` / ``codex`` CLIs, panes — is real.
"""

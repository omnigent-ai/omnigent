"""Pi system-prompt arguments shared by SDK and native launches."""


def pi_prompt_args(instructions: str | None, mode: str = "append") -> list[str]:
    """Return Pi prompt flags; replacement suppresses SYSTEM.md/APPEND_SYSTEM.md discovery."""
    if mode not in ("append", "replace"):
        raise ValueError("system_prompt_mode must be 'append' or 'replace'")
    if mode == "replace":
        if not instructions or not instructions.strip():
            raise ValueError("system_prompt_mode='replace' requires non-empty instructions")
        # An explicit empty append input suppresses APPEND_SYSTEM.md discovery.
        return ["--system-prompt", instructions, "--append-system-prompt", ""]
    return ["--append-system-prompt", instructions] if instructions else []

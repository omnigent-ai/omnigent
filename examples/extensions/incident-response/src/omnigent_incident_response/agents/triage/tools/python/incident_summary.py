"""A bundled tool that needs no extension package on the runner."""


def incident_summary(service: str, impact: str, next_step: str) -> str:
    """Format a handoff from observations the investigator has already verified."""
    return f"Service: {service}\nImpact: {impact}\nNext step: {next_step}"

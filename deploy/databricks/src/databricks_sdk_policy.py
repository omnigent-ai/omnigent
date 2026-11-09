"""Conditional CLI cache policy for Databricks SDK constructors."""

from collections.abc import Callable
from functools import partial
from inspect import getattr_static, signature


def with_cli_cache_policy[ConstructorResult](
    constructor: Callable[..., ConstructorResult],
) -> Callable[..., ConstructorResult]:
    """Opt into CLI cache freshness only when the constructor declares the field."""
    # SDK descriptors return None on class access, so inspect the declaration.
    try:
        getattr_static(constructor, "databricks_cli_force_refresh")
    except AttributeError:
        return constructor
    return partial(constructor, databricks_cli_force_refresh=False)


def workspace_client_with_cli_cache[ConstructorResult](
    constructor: Callable[..., ConstructorResult], **kwargs: str | None
) -> ConstructorResult:
    """Construct a default client, preserving its public product defaults."""
    try:
        from databricks.sdk.config import Config
    except ImportError:
        return constructor(**kwargs)

    config_constructor = with_cli_cache_policy(Config)
    if config_constructor is Config:
        return constructor(**kwargs)
    # WorkspaceClient accepts Config, not arbitrary Config keyword arguments.
    parameters = signature(constructor).parameters
    config_kwargs = {name: parameters[name].default for name in ("product", "product_version")}
    config_kwargs.update(kwargs)
    return constructor(config=config_constructor(**config_kwargs))

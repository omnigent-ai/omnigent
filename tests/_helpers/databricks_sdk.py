"""Strict SDK constructor doubles for cache-policy consumers."""

from collections.abc import Callable


def sdk_config_double(
    *,
    supports_policy: bool,
    resolved_host: str,
    headers: dict[str, str],
    on_construct: Callable[[dict[str, object]], None],
    configuration_attributes: Callable[[], list[object]] = list,
) -> type:
    class LegacyConfig:
        @staticmethod
        def attributes() -> list[object]:
            return configuration_attributes()

        def __init__(self, *, profile: str | None = None, host: str | None = None) -> None:
            self._initialize(profile, host, {})

        def _initialize(
            self, profile: str | None, host: str | None, options: dict[str, object]
        ) -> None:
            self.host = host or resolved_host
            on_construct({"profile": profile, "host": host, **options})

        def authenticate(self) -> dict[str, str]:
            return dict(headers)

    class UnsetPolicy:
        def __get__(self, instance: object, owner: type | None = None) -> None:
            return None

    class SupportedConfig(LegacyConfig):
        databricks_cli_force_refresh = UnsetPolicy()

        def __init__(
            self,
            *,
            profile: str | None = None,
            host: str | None = None,
            databricks_cli_force_refresh: bool | None = None,
        ) -> None:
            self._initialize(
                profile, host, {"databricks_cli_force_refresh": databricks_cli_force_refresh}
            )

    return SupportedConfig if supports_policy else LegacyConfig

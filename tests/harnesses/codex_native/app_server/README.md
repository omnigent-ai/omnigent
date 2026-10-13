# Codex app server tests

Existing scenarios from `test_codex_native_app_server.py`, grouped by behavior. Test names,
assertions and parameter cases are retained. Counts below are test definitions;
parameterization produces additional collected cases.

| Module | Behavior | Test definitions |
| --- | --- | ---: |
| [test_catalog_cache.py](test_catalog_cache.py) | Catalog cache | 16 |
| [test_catalog_resolution.py](test_catalog_resolution.py) | Catalog resolution | 5 |
| [test_client.py](test_client.py) | Client | 5 |
| [test_discovery.py](test_discovery.py) | Discovery | 18 |
| [test_instructions.py](test_instructions.py) | Instructions | 17 |
| [test_launch_config.py](test_launch_config.py) | Launch config | 19 |
| [test_mcp_config.py](test_mcp_config.py) | Mcp config | 12 |
| [test_model_migration.py](test_model_migration.py) | Model migration | 3 |
| [test_policy_hooks.py](test_policy_hooks.py) | Policy hooks | 21 |
| [test_router_hooks.py](test_router_hooks.py) | Router hooks | 10 |
| [test_session_profiles.py](test_session_profiles.py) | Session profiles | 6 |
| [test_startup.py](test_startup.py) | Startup | 8 |

Explicit fixtures stay in the test module that consumes them.
Helpers shared by multiple modules live in `_support.py`; helpers used by one
module stay beside their tests. Search a retained test name to find an old failure:

```sh
rg 'def test_name' tests/harnesses/codex_native/app_server
uv run --no-sync pytest tests/harnesses/codex_native/app_server --reruns 0 -n 4 --dist loadfile
```

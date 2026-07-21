# Harness Catalog Visibility

## ADDED Requirements

### Requirement: Harness catalog visibility filter
The system SHALL support a persisted `dismissed_harnesses` configuration list that hides matching harness ids from the web UI harness catalog response without mutating the underlying harness registry.

#### Scenario: No dismissed harnesses preserves current catalog
- **GIVEN** the global Omnigent config has no `dismissed_harnesses` key
- **WHEN** an authenticated client requests `GET /v1/harnesses`
- **THEN** the response includes the same harness rows produced by `harness_catalog()` today

#### Scenario: Dismissed harness is omitted from API response
- **GIVEN** the global Omnigent config contains `dismissed_harnesses: ["claude-sdk"]`
- **WHEN** an authenticated client requests `GET /v1/harnesses`
- **THEN** the response does not include a row whose id is `claude-sdk`
- **AND** all non-dismissed harness rows remain present and unchanged

#### Scenario: Unknown dismissed harness entries are ignored safely
- **GIVEN** the global Omnigent config contains a `dismissed_harnesses` entry that is not a valid harness id
- **WHEN** an authenticated client requests `GET /v1/harnesses`
- **THEN** the server does not fail the request
- **AND** valid harness rows are filtered only by valid dismissed ids

### Requirement: Headless harness visibility CLI
The CLI SHALL expose commands to list, hide, and unhide harness ids so users can configure web UI visibility without editing YAML by hand.

#### Scenario: List reports visible and dismissed harnesses
- **GIVEN** the global Omnigent config has zero or more `dismissed_harnesses` entries
- **WHEN** the user runs `omni harness list`
- **THEN** the CLI displays known harness ids with whether each is visible or dismissed

#### Scenario: Hide persists a valid harness id
- **GIVEN** `claude-sdk` is a known harness id
- **WHEN** the user runs `omni harness hide claude-sdk`
- **THEN** `claude-sdk` is stored in `dismissed_harnesses` in the global config
- **AND** subsequent `GET /v1/harnesses` responses omit `claude-sdk`

#### Scenario: Unhide restores a dismissed harness id
- **GIVEN** `claude-sdk` is present in `dismissed_harnesses`
- **WHEN** the user runs `omni harness unhide claude-sdk`
- **THEN** `claude-sdk` is removed from `dismissed_harnesses` in the global config
- **AND** subsequent `GET /v1/harnesses` responses include `claude-sdk` when it is present in `harness_catalog()`

#### Scenario: Invalid harness id is rejected by CLI
- **GIVEN** `not-a-harness` is not a known harness id or alias accepted for visibility configuration
- **WHEN** the user runs `omni harness hide not-a-harness`
- **THEN** the command fails with a clear error
- **AND** the global config is not modified

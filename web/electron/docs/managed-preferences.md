# Managed Preferences (macOS)

Administrators can use macOS MDM Managed Preferences to provide server URLs to
Omnigent Desktop. People can then choose their organization’s server instead of
typing it.

The preference domain is the desktop bundle identifier:

```text
ai.omnigent.desktop
```

## Keys

| Key                                 | Type             | Required | Default | Description                                                                                                                                                                                     |
| ----------------------------------- | ---------------- | -------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `serverUrls`                        | Array of strings | No       | `[]`    | Server URLs to offer, most-preferred first. Each must use `https://`. At most 10.                                                                                                               |
| `databricksInternalFeaturesEnabled` | Boolean          | No       | `false` | Enables Databricks-internal features (e.g. the Arca host option) on windows connected to a Databricks-managed server. Fails closed — anything but an explicit boolean `true` reads as disabled. |

A schemeless host is accepted and interpreted as `https://`. Paths are
preserved, so an administrator can provide a workspace mount directly:

```text
https://my-workspace.cloud.databricks.com/ml/omnigents
```

Entries with the same origin are collapsed, keeping the first. An invalid type,
an insecure or malformed entry, or more than 10 entries rejects the whole list.

## Behavior

Managed servers appear under **Provided by your organization** on the connect
screen and in the in-app server switcher. They are offered, not enforced:

- Omnigent does not connect automatically.
- People can still enter another server URL.
- Managed values are read from macOS on demand rather than copied wholesale
  into `settings.json`.
- A managed workspace path is loaded as configured instead of being reduced to
  its origin.

Applying or removing a profile is reflected the next time the server switcher
is opened or the connect screen is loaded.

## Databricks-internal features

When `databricksInternalFeaturesEnabled` is `true` **and** the window is
connected to a Databricks-managed server (a workspace mount on
`*.databricks.com` / `*.azuredatabricks.net`, or a Databricks App on
`*.databricksapps.com`, https only), the new-session host picker offers
**Run on Arca**: connecting the user's Arca dev instance to the current
server as a host. On any other server the flag reads as disabled. Selecting it
opens a shell-owned connect console that shows the exact command — `arca ssh`
with a remote `isaac omni host --background --non-interactive` — and, after
confirmation, streams the command's live output into an embedded terminal
pane; the instance authenticates with its own Databricks credentials. A
connect already in flight is re-surfaced (console focused, outcome shared)
rather than refused, and the run has a hard timeout so a wedged connection
always settles. No local process outlives the connect — the enrolled host
keeps its own outbound tunnel from the Arca box. Once connected, the option
disappears and the box's host row is tagged **Arca instance** (recognized by
the host id remembered at connect time). Like the server list, the flag is read from
macOS on demand, so profile changes apply without a restart.

## MDM profile example

Use the standard `com.apple.ManagedClient.preferences` payload and the
`ai.omnigent.desktop` application preference domain. Most MDM products expose
this as a custom settings or managed preferences payload.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>PayloadContent</key>
  <array>
    <dict>
      <key>PayloadContent</key>
      <dict>
        <key>ai.omnigent.desktop</key>
        <dict>
          <key>Forced</key>
          <array>
            <dict>
              <key>mcx_preference_settings</key>
              <dict>
                <key>serverUrls</key>
                <array>
                  <string>https://omnigent.corp.example.com</string>
                  <string>https://my-workspace.cloud.databricks.com/ml/omnigents</string>
                </array>
                <key>databricksInternalFeaturesEnabled</key>
                <false/>
              </dict>
            </dict>
          </array>
        </dict>
      </dict>
      <key>PayloadDisplayName</key>
      <string>Omnigent Desktop Managed Preferences</string>
      <key>PayloadIdentifier</key>
      <string>com.example.omnigent.preferences</string>
      <key>PayloadType</key>
      <string>com.apple.ManagedClient.preferences</string>
      <key>PayloadUUID</key>
      <string>45C6C548-5E50-4A42-8319-F437C07D8151</string>
      <key>PayloadVersion</key>
      <integer>1</integer>
    </dict>
  </array>
  <key>PayloadDisplayName</key>
  <string>Omnigent Desktop</string>
  <key>PayloadIdentifier</key>
  <string>com.example.omnigent</string>
  <key>PayloadScope</key>
  <string>User</string>
  <key>PayloadType</key>
  <string>Configuration</string>
  <key>PayloadUUID</key>
  <string>984315D8-3729-4D19-BB34-F052A52F546B</string>
  <key>PayloadVersion</key>
  <integer>1</integer>
</dict>
</plist>
```

Replace the example organization identifiers and UUIDs before deployment.

## Local verification

For development only, the effective preference can be simulated with
`defaults` while a **packaged Omnigent app** is closed. An unpackaged
`electron .` / `just electron-dev` process uses Electron's development bundle
identifier, not `ai.omnigent.desktop`, so it will not see this value:

```bash
defaults write ai.omnigent.desktop serverUrls -array \
  "https://omnigent.corp.example.com" \
  "https://my-workspace.cloud.databricks.com/ml/omnigents"
defaults write ai.omnigent.desktop databricksInternalFeaturesEnabled -bool true
```

Remove the test values with:

```bash
defaults delete ai.omnigent.desktop serverUrls
defaults delete ai.omnigent.desktop databricksInternalFeaturesEnabled
```

Production deployment should use an MDM-forced preference rather than a local
user default.

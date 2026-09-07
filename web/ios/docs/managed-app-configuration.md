# Managed app configuration (iOS)

Administrators can preset the server URLs the Omnigent iOS app offers, so people
in an organization pick their server from a list instead of typing it.

This page is the configuration specification. Apple's guidance is to publish it
where administrators can reach it, so treat the keys and error codes below as a
public contract.

## Requirements

The app must be installed as a **managed app** by a device management service.
Beyond that, either delivery channel works:

| Channel                 | How it is sent                                                             | Requires                                 |
| ----------------------- | -------------------------------------------------------------------------- | ---------------------------------------- |
| Declarative (preferred) | `com.apple.configuration.app.managed` declaration with the `AppConfig` key | Declarative device management; iOS 18.4+ |
| Classic                 | The `com.apple.configuration.managed` app configuration dictionary         | Any MDM that supports app configuration  |

Both carry the same keys. If a service sends both, the declarative configuration
is used, because it is validated with feedback to the administrator (see
[Errors](#errors)).

An unmanaged install, or a managed install with no configuration, behaves exactly
as before: no preset servers, and the person types a URL.

## Keys

| Key          | Type             | Required | Default | Description                                                                      |
| ------------ | ---------------- | -------- | ------- | -------------------------------------------------------------------------------- |
| `serverUrls` | Array of strings | No       | `[]`    | Server URLs to offer, most-preferred first. Each must be `https://`. At most 10. |

Notes:

- A bare host is accepted and read as `https://` — `omnigent.corp.example.com`
  becomes `https://omnigent.corp.example.com`.
- Include the workspace path if your deployment uses one, for example
  `https://my-workspace.cloud.databricks.com/ml/omnigents`. The app can discover
  a Databricks workspace mount on its own, but naming it here skips that lookup.
- Entries that resolve to the same origin are collapsed, keeping the first.
- `http://` is rejected. iOS App Transport Security blocks plain HTTP in release
  builds, so an `http://` server could not load even if it were accepted here.
  Terminate TLS in front of an on-premises server.

## Behavior

Preset servers appear on the connect screen under **"Provided by your
organization"**, above the person's own recent servers, and in the in-app server
switcher.

They are **offered, not enforced**:

- The app does not connect to one automatically. The person still chooses.
- The person can still type any other server URL.
- Preset servers are never written to the device's saved-server list, so
  removing them from the configuration removes them from the app, and they never
  displace a server the person chose themselves.

Updating the configuration takes effect without reinstalling the app. A
declarative configuration is applied as soon as the device receives it; a classic
configuration is picked up the next time the app becomes active, so a change made
while someone is using the app appears when they next return to it.

## Example

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>configuration</key>
    <dict>
        <key>serverUrls</key>
        <array>
            <string>https://omnigent.corp.example.com</string>
            <string>https://my-workspace.cloud.databricks.com/ml/omnigents</string>
        </array>
    </dict>
</dict>
</plist>
```

## Errors

An invalid configuration is **rejected as a whole** — no partial list is offered,
so what the app shows always matches what was configured.

How you find out differs by channel:

- **Declarative:** the device reports the failure to the management service and
  records the code and message below in its event log, which you can retrieve
  with a device log request.
- **Classic:** there is no mechanism to report anything back, so an invalid
  configuration is silently ignored and no servers are offered. If preset servers
  don't appear, check the value against the codes below.

| Code  | Meaning                                  | Fix                                                           |
| ----- | ---------------------------------------- | ------------------------------------------------------------- |
| `100` | An entry is not a valid server URL.      | Check for typos, stray whitespace, or a missing host.         |
| `101` | An entry does not use `https://`.        | Use `https://`; see the note on App Transport Security above. |
| `102` | More than 10 entries.                    | Reduce the list to 10 or fewer.                               |
| `103` | `serverUrls` is not an array of strings. | Send an array, even for a single URL.                         |

Codes at or above `1879048192` (`0x70000000`) are reserved by the system; the
codes above are all in the app-specific range.

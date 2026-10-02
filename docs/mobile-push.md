# Opt-in mobile push (white-label builds)

Mobile push is off by default. Android receives FCM data messages; iOS receives
FCM alerts forwarded through APNs. Stock clients do not register devices.

Enable the migrated server with:

```sh
export OMNIGENT_FEATURES=mobile_push
export OMNIGENT_FCM_CREDENTIALS_FILE=/run/secrets/fcm-service-account.json
```

Append `mobile_push` to any existing comma-separated feature list. Use an explicit
Google service-account JSON for the Firebase project embedded in your clients.
Ambient `GOOGLE_APPLICATION_CREDENTIALS` is ignored. Missing credentials leave
the enabled feature dormant; malformed credentials fail startup. Keep the JSON
outside source control, restrict its filesystem permissions, and mount it as a
secret. Grant the service account a custom Google Cloud role containing only
`cloudmessaging.messages.create` on that project; enable the FCM HTTP v1 API.

Credentials are read once at startup. Rotation requires a restart; already-issued
access tokens stay valid for up to 1 h. Authorization failures back off and emit
rate-limited status/error-code-only warnings.

For iOS, upload the APNs authentication key to **Firebase Console → Project
settings → Cloud Messaging**. Never upload an APNs key to Omnigent. Configure
the correct bundle identifier and APNs environment in the white-label build.

`GET /v1/info` advertises `push.fcm.enabled`, `push.fcm.project_id`, and
`push.preview`, never credentials. Distinct authenticated users register with
`PUT /v1/mobile-push/devices/{installation_id}` using `platform` (`android` or
`ios`), `fcm_token`, and `firebase_project_id`; unregister with `DELETE` on the
same path. Clients must DELETE on logout. Auth-disabled and reserved local-single-user identities are rejected.
Registrations expire after 30 days without re-registration. Session owners and
explicit per-user readers receive notifications; public sharing does not opt
anyone in. Clients must opt in and re-register after token rotation or login.

Titles and, when enabled, assistant-message previews pass through Google and
Apple. Preview is **off by default**. To opt in, set `mobile_push_preview: true`
in the server's non-secret YAML configuration, or set
`OMNIGENT_MOBILE_PUSH_PREVIEW=1` (environment takes precedence). Previews are
read at send time, limited to 120 characters, and never saved in the outbox.
Raw errors, prompts, remediation text, and provider responses are never sent.

Completion/error notifications settle for 10 seconds and are cancelled on new
input or resumed activity. Leased delivery is at-least-once: crashes after a
successful send can duplicate a notification, and crashes before an intent is
committed can lose it. Notifications expire after one hour; transient failures
retry at most five times, respecting bounded `Retry-After` delays. Clients
collapse notifications by session and use the shared golden content fixture at
`tests/fixtures/mobile_push_content.json` for push, polling, and in-app text.

**Rollback:** keep the migration and remove `mobile_push` from
`OMNIGENT_FEATURES`. Routes return 404 and no Google calls are made.
This flag-off roll-forward is the preferred rollback. The server automatically
migrates at startup; deploy the schema release first, then the feature code. Additive
tables are inert for running old replicas, but an old replica restarting after
the migration refuses to boot against the newer schema. During a rolling deploy,
replace all replicas with this release rather than restarting old replicas.
To roll code back past this release, stop all new replicas and run from the
repository root against the same database:

```sh
OMNIGENT_DB_URL=… alembic -c omnigent/db/alembic.ini downgrade mm1a2b3c4d5e
```

Then start the older server. The downgrade drops both tables, loses registrations
and pending deliveries, and requires clients to re-register.

**Leaked credential:** delete/revoke the service-account key in Google Cloud,
then rotate it and replace the mounted secret. Unsetting the server config does
not revoke a leaked key.

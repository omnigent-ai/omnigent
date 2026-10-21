# New-session shared defaults

On a server advertising `shared_preferences`, the composer waits up to five
seconds for its selected target's defaults. Project and explicit session choices
win. Saved harnesses and models remain requested when unavailable; the user can
change this session's selection or repair the machine/provider.

Connected machines prepare through the authenticated owner tunnel. The server
supplies a complete revision-consistent snapshot; the host adds namespace-bound
machine overrides and reads portable project defaults. This does not copy user
credentials or create an offline CLI login profile. The host must advertise
`preference_prepare` and acknowledge the exact snapshot. Managed new-session
previews read shared defaults directly; managed runtime wiring is separate.

The browser prepares once per account, host and working directory, and on explicit
Retry. Workspace typing is debounced; message typing, focus and ordinary session
switches never synchronize defaults. A failed load offers Retry or an explicit
"Use temporary defaults" choice. An unused workspace links to Settings for the
one-time import/choice. Late replies cannot replace a temporary choice or another
account's preparation.

JSON and uploaded-bundle creates carry the same `preference_snapshot`. Temporary
creates carry only `omnigent.preferences.temporary=true`. Existing session
switches keep using session overrides, and no composer action publishes shared
edits. The server/session handoff described in [saved sessions](shared-preferences-session.md)
keeps the snapshot unchanged on resume.

The [feature map](../feature-map/preferences.md) separates component create-payload
coverage, desktop/mobile browser recovery, and real two-host scripted-provider
execution. These checks do not validate managed deployment, live vendor models,
or SDK default-model application.

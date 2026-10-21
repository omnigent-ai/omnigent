# Saved session preferences

An adopted session stores its `PreferenceLaunch` in
`omnigent_conversation_metadata.preference_snapshot`. This is a nullable compressed
binary column, alongside the saved inference configuration. It does not expand
the 512-character session override field or add writes during messages.

The snapshot contains the original account/workspace namespace, acknowledged
shared values and machine overrides. Explicit session model/harness/argument
changes remain in the existing session fields. Credentials, commands and
installations are excluded by the preference schema. Payloads must fit the
65,535-byte compressed limit supported by every database backend.

JSON and uploaded-bundle creates accept `preference_snapshot`. The server verifies
its namespace against the authenticated workspace before persistence. A temporary
session cannot include an adopted snapshot. Single-user ephemeral servers can
execute an already frozen offline choice without a preference store. Multi-user
servers require a matching preference authority.

Children and forks copy the saved snapshot. Cross-account forks of adopted
sessions require starting a new session with that account's own preferences.
Ordinary session edits, host replacement and subsequent shared edits do not
rewrite the snapshot. Existing rows stay NULL and retain legacy behavior.

Host launch frames carry the saved snapshot and an explicit `preferences_prepared`
marker. A fresh host writes a private handoff directly from that frame; it does
not need the original machine's cache or user login. A NULL saved choice cannot
trigger new adoption on resume. The versioned runner initialization envelope
checks that the runner loaded the same snapshot, preventing reuse across different
choices. The server requires an explicit snapshot acknowledgement from adopted
runners; an older runner cannot silently ignore the handoff. Old host frames
without the marker retain the previous startup protocol.

This slice supplies the durable handoff. Browser new-session selection, managed
startup/policy wiring and lifecycle limits are separate steps before activation.
The feature map records the real two-host/mock-provider recipe and its limits.

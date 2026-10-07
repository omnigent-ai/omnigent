# Native account profiles and Antigravity controls

Use an isolated server with three Codex account agents and four Antigravity account agents. Keep the ordinary built-in Codex and Antigravity agents enabled alongside them.

1. Open the new-session agent selector. Verify all seven configured profiles have distinct names and IDs. Open a profile's settings, leave the menu, and reopen it; the profile must remain selectable. Repeat through the custom-agent list and the in-session fork selector.
2. Start one session for each Codex profile. Verify each private session configuration is seeded from its configured `executor.config.codex_home`, matching the account used by `codex-acc1`, `codex-acc2`, or `codex-acc3`. Do not print credentials. Resume a stopped test session and verify the account remains unchanged.
3. Start one session for each Antigravity profile. Verify its private Gemini directory is seeded from that profile's `executor.config.gemini_dir`, matching `agy-acc1` through `agy-acc4`. Changing one session's model must not change another session's settings or the source profile.
4. Before sending a message, open an Antigravity profile's configuration. Select Gemini 3.8 Flash and each supported effort, Low, Medium, and High. Verify the session creation request retains the selected profile ID, model, and effort.
5. In a running Antigravity session, change the Flash effort, then send a short message. Verify the actual native user-input request uses the corresponding model variant from the live catalog. A changed UI label alone is insufficient evidence.
6. Select Claude Sonnet 4.6 Thinking before launch and during a session. Verify any prior Flash effort is cleared and a subsequent native user-input request selects Sonnet. The installed CLI does not support a separate Sonnet effort flag, so the UI must not offer unsupported effort levels.
7. Return to Flash. Verify Low, Medium, and High are available again. Refresh the page and verify the selected profile and supported model settings persist.
8. Repeat the selector and configuration steps with the keyboard. Verify visible labels, focus, and Escape dismissal. Confirm ordinary built-in vendor labels and icons remain correct.

Record screenshots of the selector and model/effort menus, plus a sanitized API or native-request cross-check. Distinguish browser/component tests, real process routing, and completed live provider replies in the verification report. Stop only the test sessions and isolated processes created for this run.

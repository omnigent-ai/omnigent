# omnigent-ui-sdk

Terminal UI components (Rich + prompt_toolkit) for building
[omnigent](https://github.com/omnigent-ai/omnigent) frontends.

`omnigent-ui-sdk` provides the reusable rendering and input building blocks used
by the omnigent CLI to display streaming agent output in the terminal. It is
released in lockstep with the core `omnigent` package at a matching version:

```bash
pip install omnigent-ui-sdk
```

See the [omnigent repository](https://github.com/omnigent-ai/omnigent) for full
documentation.

## Editing prompts

Press **Ctrl+G**, or **Ctrl+X** followed by **Ctrl+E**, to edit the current draft
in your external editor (`VISUAL`, then `EDITOR`). Collapsed pastes are expanded
for editing. Save and close the editor to return to the draft; press Enter when
you are ready to send it. During history search, Ctrl+G still cancels the search.

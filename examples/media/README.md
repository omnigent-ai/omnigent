# media

A single agent that **generates images and short videos** through an MCP server.
It shows how to use an asynchronous, billed MCP tool: submit once, poll for the
result, and return a link.

## Layout

```
media/
├── config.yaml                   # the agent (claude-sdk brain, no model pinned)
└── tools/mcp/tensorscale.yaml    # MCP server — auto-discovered, limited to four tools
```

## Run it

You need a Claude provider (`omnigent setup`) and a
[TensorScale](https://tensorscale.io/) API key. Generation is billed to your
TensorScale account.

```bash
read -rs TENSORSCALE_API_KEY && export TENSORSCALE_API_KEY
omnigent run examples/media/
```

Check the connection first. This generates nothing:

> Call list_resolutions for FLUX3-Image-Klein and text_to_image. Do not generate anything.

Then ask for media:

> Make an image of a red paper boat on a calm lake at sunrise.

> Now make a video of the boat drifting across the lake.

The agent replies with a link. **Links expire after 15 minutes, so save the file
right away.** Chat shows these results as links: it doesn't load remote images, and
it only plays remote videos whose path ends in a video extension.

## Use it from a shared server

Register the agent so it appears in the picker under **Other...**:

```bash
export OMNIGENT_RUNNER_ENV_PASSTHROUGH=TENSORSCALE_API_KEY
omnigent server --agent examples/media/     # terminal 1
omnigent host http://127.0.0.1:6767         # terminal 2, same two variables set
```

The server stores the `${TENSORSCALE_API_KEY}` placeholder and everyone's requests
are billed to that one key.

`omnigent run` is different: it puts the **actual key** in the agent bundle it
uploads. Collaborators with Edit access to the session can download that bundle, so
use a dedicated key. Before v0.22.0, Read access was enough.

## Change it

- **More tools:** add `image_to_image`, `image_to_video`, `reference_to_video`,
  `file_upload` or `list_models` to `tools` in `tensorscale.yaml`. Keep
  `get_task_status`.
- **Another provider:** TensorScale is an example, not a required backend. Replace
  `tensorscale.yaml` with another media MCP server and update the tool names and
  defaults in the prompt.
- **Saving files:** the example has no `os_env`, so the agent can't write files. You
  save results from the links.

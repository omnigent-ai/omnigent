"""Synthetic GitHub-flavored Markdown sized to a byte target.

The document repeats a section built from the ordinary constructs a design
note contains (headings, prose, lists, a table, a fenced block, a quote) so a
multi-MiB file exercises the viewer the way a real large document would,
without depending on any particular source file.
"""

from __future__ import annotations

_SECTION_TEMPLATE = """\
## Section {index}

This paragraph describes component {index} of the system and how it hands work
to its neighbours. It mentions **bold terms**, _emphasised terms_ and
`inline_code_{index}` so inline marks are parsed in every section.

- Input queue for component {index}
- Output queue for component {index}
- Retry policy: three attempts, exponential backoff

1. Validate the request
2. Transform the payload
3. Publish the result

| Field | Type | Notes |
| ----- | ---- | ----- |
| id_{index} | string | Primary key |
| created_at | timestamp | UTC |
| state | enum | queued, running, done |

```python
def handle_{index}(payload: dict) -> dict:
    return {{"id": payload["id"], "state": "done"}}
```

> Component {index} must stay idempotent because retries replay the request.

"""


def build_large_markdown(target_bytes: int) -> str:
    """Return GFM text of at least ``target_bytes`` UTF-8 bytes.

    :param target_bytes: Minimum size of the returned text in bytes.
    :returns: A document starting with an H1 followed by repeated sections.
    """
    parts = ["# Large design document\n\n"]
    size = len(parts[0].encode("utf-8"))
    index = 0
    while size < target_bytes:
        section = _SECTION_TEMPLATE.format(index=index)
        parts.append(section)
        size += len(section.encode("utf-8"))
        index += 1
    return "".join(parts)

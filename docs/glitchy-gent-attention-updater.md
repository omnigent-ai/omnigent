# Glitchy Gent Attention Updater

One-shot updater for `/E/omnigent-vault/wiki/glitchy-gent-attention-tickets.md`.
It reads current Omnigent session snapshots and recent item metadata, classifies
compact P0/P1/P2/P3 tickets, and rewrites only the marked generated section of
the board.

It does not restart, cancel, interrupt, repair, archive, delete, or dispatch
runtime work. It only sends `GET` requests to the Omnigent server.

## Dry Run

```bash
python3 -m omnigent.glitchy_gent_attention \
  --server http://192.168.2.1:6767 \
  --vault /E/omnigent-vault \
  --dry-run
```

If that anchor is not reachable from the current machine, pass the reachable
Omnigent server URL:

```bash
python3 -m omnigent.glitchy_gent_attention \
  --server http://192.168.0.169:6767 \
  --vault /E/omnigent-vault \
  --dry-run
```

## Update The Board

```bash
python3 -m omnigent.glitchy_gent_attention \
  --server http://192.168.2.1:6767 \
  --vault /E/omnigent-vault
```

The updater extracts known `conv_...` IDs from:

- `wiki/glitchy-gent-control-room.md`
- `wiki/glitchy-gent-incident-ledger.md`
- `wiki/glitchy-gent-attention-tickets.md`

It also inspects the most recently updated live sessions from
`GET /v1/sessions?kind=any`. Known archived sessions are rechecked by direct
session ID so stale failures can resolve when their live state becomes healthy.

## Validation

```bash
python3 -m pytest tests/test_glitchy_gent_attention.py
```

# UC OSS Skillpack POC (store-only)

Store Omnigent **skills / knowledge packs** in a Dockerized
[Unity Catalog OSS](https://github.com/unitycatalog/unitycatalog) server, then
**list** and **pull** them back.

**Scope A — STORE-ONLY.** No AI-gateway or policy wiring. This is a
proof-of-concept for using UC OSS as a blob + metadata registry for skill
packs. **POC / not for production.**

---

## What it does

- Stands up the open-source Unity Catalog server locally in Docker (REST API on
  `http://localhost:8080`).
- Creates a catalog `unity`, schema `omnigent`, and an EXTERNAL volume
  `unity.omnigent.skillpacks` backed by a local directory.
- `skillpack push <skill_dir>` — tar.gz's a skill directory (reusing the
  project's `SKILL.md` frontmatter parser for name/description), stores the
  blob in the volume, and upserts a metadata row for the `unity.omnigent.skills`
  table.
- `skillpack list` — reads the metadata and prints name / version / updated /
  description.
- `skillpack pull <name> [dest]` — fetches the blob and extracts it.

### How bytes are stored (important POC honesty note)

The managed-Databricks backend (`omnigent/stores/artifact_store/databricks_volumes.py`)
uploads file bytes through the `/files` REST API; the S3 backend
(`omnigent/stores/artifact_store/s3.py`) uses `boto3`. **UC OSS has neither** —
its REST surface is *metadata* (catalogs, schemas, volumes, tables) plus
temporary storage-credential vending for cloud volumes. The bytes live at the
volume's `storage_location`.

So this POC store (`UnityCatalogOSSArtifactStore`):

1. Calls the UC REST API to **resolve the volume's `storage_location`** (a
   `file://` path for a local server — the local analogue of credential
   vending).
2. Reads/writes blob bytes **directly on that filesystem path**, which is made
   host-visible via a Docker **bind mount** (`./data`).

Likewise, UC OSS has **no row-level DML over REST**, so the skill metadata
"rows" are kept as a JSON manifest blob inside the volume
(`_manifest/skills.json`), and the CLI *registers* `unity.omnigent.skills` as an
EXTERNAL table pointing at that manifest so the three-level name exists and is
inspectable (`GET /tables/unity.omnigent.skills`). A deliberate store-only
shortcut.

### `list()` and the ArtifactStore ABC

The live `ArtifactStore` ABC defines only `put/get/delete/exists`. This POC adds
`list(prefix)` **only on the concrete `UnityCatalogOSSArtifactStore`** — the
`skillpack` CLI needs to enumerate packs, and a local UC OSS volume is cheap to
walk on the filesystem. Keeping `list` off the ABC is the least-invasive choice:
the `local` / `s3` / `databricks_volumes` backends are untouched.

---

## Prerequisites

- Docker (with `docker compose` v2, or `docker-compose` v1).
- Python 3.12 with this repo installed (the CLI imports `omnigent`). Run the CLI
  from the repo root, or with `PYTHONPATH` set to the repo root (the `demo.sh`
  script does this for you).

---

## 1. Start the server

```bash
cd examples/uc-oss-skillpack
make up            # or: ./run.sh up
```

`make up` will:
1. `docker compose up -d` the pinned `unitycatalog/unitycatalog:v0.5.0` image
   (REST API on `:8080`).
2. Wait for the REST API to answer.
3. Run `./bootstrap.sh` to create the catalog / schema / volume (idempotent).

Check it:

```bash
make status
curl -s localhost:8080/api/2.1/unity-catalog/volumes/unity.omnigent.skillpacks | python3 -m json.tool
```

### Fallback: build the image from source

If `unitycatalog/unitycatalog:v0.5.0` can't be pulled, build it locally and
retag (the compose file references that exact tag):

```bash
git clone --depth 1 --branch v0.5.0 https://github.com/unitycatalog/unitycatalog
cd unitycatalog
docker build -t unitycatalog/unitycatalog:v0.5.0 .
```

Then `make up` as above.

---

## 2. Point the CLI at the server

The bootstrap prints these; export them in your shell:

```bash
export UC_OSS_URI=http://localhost:8080
export UC_OSS_VOLUME=unity.omnigent.skillpacks
# Host path of the bind-mounted volume storage (so the host-side CLI can
# read/write the same bytes the container's file:// volume points at):
export UC_OSS_VOLUME_LOCAL_PATH="$(pwd)/data/skillpacks"
```

| Env var | Meaning | Default |
|---|---|---|
| `UC_OSS_URI` | UC OSS server base URI | `http://localhost:8080` |
| `UC_OSS_VOLUME` | three-level volume name | `unity.omnigent.skillpacks` |
| `UC_OSS_TOKEN` | bearer token (local UC OSS needs none) | unset |
| `UC_OSS_VOLUME_LOCAL_PATH` | host mount of the volume storage | resolve from REST `storage_location` |

> **Why `UC_OSS_VOLUME_LOCAL_PATH`?** The volume's `storage_location`
> (`file:///home/unitycatalog/etc/data/skillpacks`) is only valid *inside* the
> container. On the host, those bytes live at `./data/skillpacks` via the bind
> mount, so the CLI needs the host path. Omit it only if you run a
> non-containerized UC server on this same host.

---

## 3. End-to-end walkthrough

Push one of Denny's real skills, list it, and pull it back:

```bash
# from repo root (so `omnigent` imports), or set PYTHONPATH=$(pwd)
PY=examples/uc-oss-skillpack/skillpack.py

# pack only (no server needed) — shows parsed frontmatter + blob size
python "$PY" pack ~/.claude/skills/startup-score-sheet

# push a skill pack (blob + metadata)
python "$PY" push ~/.claude/skills/startup-score-sheet

# push a bundled repo skill too
python "$PY" push examples/polly/skills/investigate

# list what's stored
python "$PY" list
# NAME                         VER  UPDATED              DESCRIPTION
# investigate                    1  2026-08-01 00:22:10  Delegate read-only investigation ...
# startup-score-sheet            1  2026-08-01 00:22:03  Evaluate a startup using ...

# pull one back into a temp dir
python "$PY" pull startup-score-sheet /tmp/pulled-skill
ls /tmp/pulled-skill/startup-score-sheet/SKILL.md
```

The CLI works against Denny's real skill libraries — his personal
`~/.claude/skills/*` and this repo's bundled `examples/polly/skills/*` — so a
`push <dir>` against either works out of the box.

---

## One-command demo

With the server already up (`make up`):

```bash
./demo.sh          # or: make demo
```

`demo.sh` pushes a real skill (prefers `~/.claude/skills/startup-score-sheet`,
falls back to the bundled `examples/polly/skills/investigate`), lists it, pulls
it into a fresh temp dir, and diffs the pulled tree against the source to prove
the round-trip.

---

## Teardown

```bash
make down          # stop + remove the container (keeps ./data)
make clean         # also delete ./data (wipes stored packs)
```

---

## Files

| File | Purpose |
|---|---|
| `docker-compose.yml` | UC OSS server (pinned `v0.5.0`), `:8080`, `./data` bind mount |
| `run.sh` | `up` / `down` / `clean` / `logs` / `status` |
| `Makefile` | `make up/down/clean/logs/status/demo` wrappers |
| `bootstrap.sh` | Create catalog / schema / volume over REST (idempotent) |
| `skillpack.py` | The `pack` / `push` / `list` / `pull` CLI |
| `demo.sh` | One-command end-to-end round-trip demo |

The store backend lives at `omnigent/stores/artifact_store/unitycatalog_oss.py`
(`UnityCatalogOSSArtifactStore`), alongside the local, S3, and Databricks
backends.

## What was assumed / stubbed (for reviewers)

- **Image tag**: pinned to `unitycatalog/unitycatalog:v0.5.0` (a published tag
  on Docker Hub at time of writing). Fallback build instructions above.
- **Credential vending**: for a local `file://` volume there are no cloud
  credentials to vend, so the store resolves the storage location and
  reads/writes the bind-mounted filesystem directly.
  `generate_temporary_volume_credentials` is implemented for parity but not on
  the happy path. A cloud (`s3://`) volume is explicitly rejected unless
  `local_root` is set.
- **Metadata table**: registered as an EXTERNAL JSON table for inspectability;
  the actual rows live in the `_manifest/skills.json` blob because UC OSS has no
  row-level DML over REST.
- The Docker path is exercised manually via this README / `demo.sh`; the unit
  tests mock the REST API and do **not** require Docker.

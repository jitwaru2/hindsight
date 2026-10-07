# hindsight-ext-cortana

Cortana's extension package for this fork of Hindsight. The fork's branch `cortana` is cut
from upstream tag `v0.10.2`; our behavior lives here, loaded through the engine's extension
slots, and the engine's own code stays as upstream released it. The binding specification is
`docs/work/hindsight/specs/HINDSIGHT.md` in Cortana's vault.

Unlike upstream's extension folders, this one is an installable distribution with its own
`pyproject.toml`. It depends only on libraries the engine already depends on, and not on the
engine itself, which is the host process.

## Slots

The server loads one class per slot from its environment:

```bash
HINDSIGHT_API_OPERATION_VALIDATOR_EXTENSION=hindsight_ext_cortana:CortanaOperationHooks
HINDSIGHT_API_HTTP_EXTENSION=hindsight_ext_cortana:CortanaHttpExtension
HINDSIGHT_API_MCP_EXTENSION=hindsight_ext_cortana:CortanaMcpExtension
HINDSIGHT_API_TENANT_EXTENSION=hindsight_ext_cortana:CortanaTenantExtension
```

Set all four together. The tenant extension owns the migrations and the bank-scoped table
declarations; the separate worker (`hindsight-worker`) needs the same variables as the API.

| Module | What it holds |
| --- | --- |
| `hooks` | `CortanaOperationHooks`: validators that accept unchanged; `on_retain_complete`, where structuring and supersession run (HSIGHT-4, HSIGHT-5); the retrieval log hooks (HSIGHT-7) |
| `http` | `CortanaHttpExtension`: routes under `/ext/cortana/`; today `GET /ext/cortana/status` |
| `mcp` | `CortanaMcpExtension`: tools on the engine's `/mcp` (HSIGHT-6); none yet |
| `tenant` | `CortanaTenantExtension`: the engine's default tenant plus our migrations and bank-scoped tables |
| `migrations` | the Alembic branch `cortana` and the table names |
| `rules`, `reconcile`, `structuring`, `gate` | filled by HSIGHT-5, HSIGHT-4 and HSIGHT-8 |
| `cli` | `hindsight-cortana` |
| `verify_base` | the base check behind `hindsight-cortana verify-base` |

## Tables and migrations

`claims`, `attributes`, `ledger` and `retrievals` (specification section 4.2) are created by the
revisions in `hindsight_ext_cortana/migrations/versions/`. They form an Alembic tree of their own,
labelled `cortana`, whose base depends on the engine's head at v0.10.2 (`e5b1c7d3a902`). The
engine applies it at startup in its own migration run, under its advisory lock. Because our base
depends on the engine's head, `alembic_version` records our head in place of the engine's (Alembic
treats the engine's head as applied through ours); the engine reads that table only through
Alembic. No table has a foreign key to an engine table, the ledger refuses `UPDATE` through a
trigger, and the tenant extension declares all four as bank-scoped, so the engine's bank deletion
and `hindsight-admin` backup and restore include them.

A new revision goes in `versions/` with `down_revision` set to the current `cortana` head. The
engine has no `alembic.ini`; generate one with `alembic.command.revision`, configured as
`hindsight_api.migrations` configures a run (core's `alembic` folder as the script location, core's
`versions` and ours as version locations) and `head="cortana@head"`.

## Tests

The deterministic suite runs on the engine's own fixtures: an embedded PostgreSQL through pg0 (its
own instance, `hindsight-ext-cortana-test` on port 5557, separate from the engine's test instance),
the local embedding and reranking models, and the `mock` LLM provider. Nothing calls a model.

```bash
cd hindsight-extensions/cortana
uv sync
uv run ruff check . && uv run ruff format --check . && uv run deptry .
uv run pytest
```

`.python-version` pins 3.14, production's interpreter. `.github/workflows/cortana.yml` runs the
same steps on Python 3.11 (the development checkout's) and 3.14 for every push to `cortana`.

## A scratch server from the development checkout

The embed daemon's development path runs the API with
`uv run --project <checkout>/hindsight-api-slim --extra all hindsight-api`, which installs the
engine's project but not this package. Install it once, editable, into the checkout's environment;
the engine's `pyproject.toml` stays untouched:

```bash
uv pip install --python <checkout>/.venv/bin/python --editable <checkout>/hindsight-extensions/cortana
```

`uv run` syncs inexactly, so the install survives every daemon start. A plain `uv sync` in the
checkout is exact and removes it; repeat the install after one (HSIGHT-1's syncs used
`--inexact`). Then put the four variables above in the scratch profile and start the daemon from
the checkout as HSIGHT-1 documents.

## Production

Installed beside the engine from the release tag (specification section 13.1):

```bash
uv tool install hindsight-embed==0.10.2 \
  --with 'hindsight-api-slim[all] @ git+https://github.com/jitwaru2/hindsight@<tag>#subdirectory=hindsight-api-slim' \
  --with 'hindsight-ext-cortana @ git+https://github.com/jitwaru2/hindsight@<tag>#subdirectory=hindsight-extensions/cortana'
```

## verify-base

`hindsight-cortana verify-base <RECORD> <package dir>` proves the fork's base is the release we
run. It compares the checkout's `hindsight-api-slim/hindsight_api/` with the SHA-256 values in the
installed wheel's `RECORD`: every file the RECORD lists must exist with the same hash, and the
checkout must hold no package file the RECORD lacks (`__pycache__` is ignored).

```bash
hindsight-cortana verify-base \
  <site-packages>/hindsight_api_slim-0.10.2.dist-info/RECORD \
  hindsight-api-slim/hindsight_api
```

The same check runs without the engine installed, under any Python 3.9 or later, as
`python3 -I hindsight-extensions/cortana/hindsight_ext_cortana/verify_base.py <RECORD> <dir>`.

Use `hindsight_api_slim-<version>.dist-info/RECORD`, which lists the code. The
`hindsight_api-<version>.dist-info` beside it is a metadata-only package with no code to compare.
The command prints matching, mismatched, missing and extra counts with the paths that differ, and
exits 0 only when everything matches. Repeat it after every upstream pull, against a fresh install
of the new upstream version, before tagging a `v<upstream>-cortana.<n>` release.

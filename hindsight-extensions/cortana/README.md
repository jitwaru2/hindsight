# hindsight-ext-cortana

Cortana's extension package for this fork of Hindsight. The fork's branch `cortana` is cut
from upstream tag `v0.10.2`; our behavior lives here, loaded through the engine's extension
slots, and the engine's own code stays as upstream released it.

The package itself (`pyproject.toml`, the extension classes, migrations, tests) is not built
yet. Today this folder holds one tool.

## verify-base

`hindsight_ext_cortana/verify_base.py` proves the fork's base is the release we run. It
compares the checkout's `hindsight-api-slim/hindsight_api/` with the SHA-256 values in the
installed wheel's `RECORD`: every file the RECORD lists must exist with the same hash, and
the checkout must hold no package file the RECORD lacks (`__pycache__` is ignored). It is
the first form of the planned `hindsight-cortana verify-base` command and uses only the
Python standard library.

Run it from the repository root against an install of the same upstream version:

```bash
python3 -I hindsight-extensions/cortana/hindsight_ext_cortana/verify_base.py \
  <site-packages>/hindsight_api_slim-0.10.2.dist-info/RECORD \
  hindsight-api-slim/hindsight_api
```

Use `hindsight_api_slim-<version>.dist-info/RECORD`, which lists the code. The
`hindsight_api-<version>.dist-info` beside it is a metadata-only package with no code to
compare. The command prints matching, mismatched, missing and extra counts with the paths
that differ, and exits 0 only when everything matches.

Repeat it after every upstream pull, against a fresh install of the new upstream version,
before tagging a `v<upstream>-cortana.<n>` release.

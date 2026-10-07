"""Cortana's database tables and the Alembic branch that creates them.

The revisions in ``versions/`` form their own Alembic tree, labelled ``cortana``, whose base
depends on the engine's head at v0.10.2. The engine applies them in its own migration run, under
its advisory lock and into its ``alembic_version`` table, because the tenant extension names this
folder through ``alembic_version_locations`` (see ``hindsight_api.extensions.base``).

The tables (specification section 4.2) are bank-scoped: the tenant extension declares them, so the
engine's bank deletion and ``hindsight-admin`` backup and restore include them. None has a foreign
key to an engine table, because retirement moves a fact out of ``memory_units``.
"""

from functools import cache
from pathlib import Path

from alembic.script import ScriptDirectory

VERSIONS_DIR = Path(__file__).parent / "versions"
BRANCH = "cortana"
TABLES = ("claims", "attributes", "ledger", "retrievals")


@cache
def _scripts() -> ScriptDirectory:
    """Alembic's view of the engine's revisions and ours, as the engine's migration run sees them."""
    import hindsight_api

    core = Path(hindsight_api.__file__).parent / "alembic"
    return ScriptDirectory(str(core), version_locations=[str(core / "versions"), str(VERSIONS_DIR)])


def head_revision() -> str:
    """The newest revision of the ``cortana`` branch."""
    return _scripts().get_revision(f"{BRANCH}@head").revision


def branch_revisions() -> frozenset[str]:
    """Every revision of the ``cortana`` branch: those whose file is in ``versions/``."""
    return frozenset(
        script.revision for script in _scripts().walk_revisions() if Path(script.path).parent == VERSIONS_DIR
    )

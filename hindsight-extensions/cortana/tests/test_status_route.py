"""``GET /ext/cortana/status`` answers through the engine's app."""

from hindsight_ext_cortana.migrations import TABLES, head_revision


async def test_status_reports_the_branch_current_and_the_tables_present(cortana_client):
    response = await cortana_client.get("/ext/cortana/status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["extension"] == "hindsight-ext-cortana"
    assert body["database_schema"] == "public"
    assert body["migrations"] == {
        "branch": "cortana",
        "head": head_revision(),
        "applied": head_revision(),
        "current": True,
    }
    assert body["tables"] == {table: True for table in TABLES}

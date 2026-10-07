"""The structuring fixtures (specification 11, item 2) load, validate and match the instructions
they were cut under. Synthetic fixtures ship here; real ones stay on the operator's machine and are
checked when their folder exists (``HINDSIGHT_CORTANA_REAL_FIXTURES``)."""

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from hindsight_ext_cortana import extraction
from hindsight_ext_cortana.structuring.fixtures import (
    REAL_FIXTURES_ENV,
    StructuringFixture,
    load_fixtures,
    real_fixtures_dir,
)

SYNTHETIC = Path(__file__).parent / "fixtures" / "structuring"


def _current_instructions():
    return extraction.VERSION, hashlib.sha256(extraction.instructions().encode()).hexdigest()


def _check(fixtures):
    assert fixtures
    for fixture in fixtures:
        assert (fixture.extraction.instructions_version, fixture.extraction.instructions_sha256) == (
            _current_instructions()
        ), f"{fixture.name} was cut under other instructions: re-cut it"
        # Every fixture carries the case extraction exists for: a provisional claim on a key that a
        # settled claim then holds as current.
        assert any(key.current and any(claim.provisional for claim in key.claims) for key in fixture.expected), (
            fixture.name
        )


def test_the_synthetic_fixtures_validate():
    fixtures = load_fixtures(SYNTHETIC)
    assert {f.source.kind for f in fixtures} == {"session", "document"}
    _check(fixtures)


def test_the_real_fixtures_validate_when_present():
    directory = real_fixtures_dir()
    if directory is None:
        pytest.skip(f"no real fixtures on this machine; set {REAL_FIXTURES_ENV} to their folder to check them")
    _check(load_fixtures(directory))


def test_a_current_claim_must_be_a_settled_claim_on_its_key():
    raw = json.loads((SYNTHETIC / "synthetic-session.json").read_text())
    raw["expected"][0]["current"] = raw["expected"][0]["claims"][0]["fact"]  # the provisional one
    with pytest.raises(ValidationError, match="not a non-provisional claim"):
        StructuringFixture.model_validate(raw)


def test_an_expected_claim_must_name_a_fact_of_the_fixture():
    raw = json.loads((SYNTHETIC / "synthetic-session.json").read_text())
    raw["expected"][0]["claims"][0]["fact"] = "f999"
    with pytest.raises(ValidationError, match="names fact f999"):
        StructuringFixture.model_validate(raw)


def test_the_real_fixture_folder_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv(REAL_FIXTURES_ENV, str(tmp_path))
    assert real_fixtures_dir() == tmp_path
    monkeypatch.setenv(REAL_FIXTURES_ENV, str(tmp_path / "absent"))
    assert real_fixtures_dir() is None

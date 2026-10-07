"""The extraction instructions (specification 5.1): versioned and pinned, carried into the engine's
prompt by its custom mode with its output schema unchanged, and applied to a bank, strategies
included, through the engine's own configuration API."""

import hashlib
from dataclasses import replace

import pytest
from hindsight_api.config import HindsightConfig
from hindsight_api.engine.retain.fact_extraction import build_chunk_prompt_parts

from hindsight_ext_cortana import extraction

# A change to instructions.md is a release (specification 11): raise VERSION, re-pin this hash,
# re-cut the structuring fixtures, and run the gate.
PINNED_VERSION = "1"
PINNED_SHA256 = "f46e574fdd297d1da6d4b3e65358316a60c6af10ed1c34aa2d7af7e19fa94efe"

# A line of the engine's concise guidelines, which custom mode replaces.
CONCISE_ONLY = "CONSOLIDATE related statements into ONE fact when possible."


def _prompt(mode: str, instructions: str | None):
    config = replace(HindsightConfig.from_env(), retain_extraction_mode=mode, retain_custom_instructions=instructions)
    return build_chunk_prompt_parts(config, chunk="text")


def test_the_instructions_are_pinned_to_their_version():
    assert extraction.VERSION == PINNED_VERSION
    assert hashlib.sha256(extraction.instructions().encode()).hexdigest() == PINNED_SHA256


def test_custom_mode_puts_the_instructions_in_place_of_the_concise_guidelines():
    # The engine doubles lone braces in the instructions; there must be none to double.
    assert "{" not in extraction.instructions() and "}" not in extraction.instructions()
    custom = _prompt("custom", extraction.instructions())
    assert extraction.instructions() in custom.system_prompt
    assert CONCISE_ONLY not in custom.system_prompt
    assert CONCISE_ONLY in _prompt("concise", None).system_prompt


def test_the_output_schema_is_the_engines_own():
    custom = _prompt("custom", extraction.instructions()).response_schema
    concise = _prompt("concise", None).response_schema
    assert custom.model_json_schema() == concise.model_json_schema()


def test_the_updates_switch_every_model_strategy_and_leave_the_others():
    strategies = {
        "conversation": {"retain_mission": "m", "retain_extraction_mode": "concise", "retain_chunk_size": 12000},
        "document": {"retain_extraction_mode": "verbose"},
        "decision": {"retain_extraction_mode": "chunks"},
        "plain": {"retain_chunk_size": 4000},
    }
    updates = extraction.bank_config_updates(strategies)
    assert updates["retain_extraction_mode"] == "custom"
    assert updates["retain_custom_instructions"] == extraction.instructions()
    assert updates["retain_strategies"] == {
        "conversation": {"retain_mission": "m", "retain_extraction_mode": "custom", "retain_chunk_size": 12000},
        "document": {"retain_extraction_mode": "custom"},
        "decision": {"retain_extraction_mode": "chunks"},
        "plain": {"retain_chunk_size": 4000},
    }
    assert strategies["conversation"]["retain_extraction_mode"] == "concise", "the input is not modified"
    assert "retain_strategies" not in extraction.bank_config_updates(None)


def test_a_strategy_with_instructions_of_its_own_is_refused():
    with pytest.raises(ValueError, match="sets its own retain_custom_instructions"):
        extraction.bank_config_updates({"odd": {"retain_custom_instructions": "x"}})


async def test_applied_through_the_engine_every_strategy_extracts_under_the_instructions(cortana_client):
    bank = "/v1/default/banks/extraction-config"
    strategies = {
        "conversation": {"retain_extraction_mode": "concise", "retain_chunk_size": 12000},
        "document": {"retain_extraction_mode": "concise"},
        "decision": {"retain_extraction_mode": "chunks"},
    }
    response = await cortana_client.patch(
        f"{bank}/config", json={"updates": {"retain_strategies": strategies, "retain_default_strategy": "document"}}
    )
    assert response.status_code == 200, response.text
    response = await cortana_client.patch(
        f"{bank}/config", json={"updates": extraction.bank_config_updates(strategies)}
    )
    assert response.status_code == 200, response.text
    config = (await cortana_client.get(f"{bank}/config")).json()["config"]
    assert config["retain_custom_instructions"] == extraction.instructions()
    assert {name: s["retain_extraction_mode"] for name, s in config["retain_strategies"].items()} == {
        "conversation": "custom",
        "document": "custom",
        "decision": "chunks",
    }
    for strategy in ("conversation", "document"):
        response = await cortana_client.post(
            f"{bank}/prompts/preview", json={"operation": "retain", "strategy": strategy}
        )
        assert response.status_code == 200, response.text
        system = response.json()["messages"][0]
        text = "".join(block["text"] for block in system["blocks"] if block["active"])
        assert extraction.instructions() in text, strategy
        assert CONCISE_ONLY not in text, strategy

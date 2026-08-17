"""`.env.example` has to list every variable the application reads.

Requirement 6 is that a stranger copies `.env.example`, generates two tokens, and the
documented command works. That only holds if the file is complete -- a setting the code
reads but the example never mentions is a setting nobody knows to set, and it will be
discovered as a runtime surprise rather than as a line in a file. So this is checked
rather than maintained by hand.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from doctask.config import Settings, settings
from doctask.services.pricing import load_price_table

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"

# Set only by the test harness, never read by the application itself.
TEST_ONLY = {"DOCTASK_TEST_DATABASE_URL"}


def _documented() -> set[str]:
    """Every DOCTASK_* name the example file names, commented-out lines included."""
    return set(re.findall(r"^#?\s*(DOCTASK_[A-Z0-9_]+)=", EXAMPLE.read_text(), re.MULTILINE))


def _declared() -> set[str]:
    return {f"DOCTASK_{name.upper()}" for name in Settings.model_fields}


def test_every_setting_the_code_reads_is_in_env_example() -> None:
    missing = _declared() - _documented()
    assert not missing, f".env.example does not mention: {sorted(missing)}"


def test_env_example_invents_no_setting_the_code_ignores() -> None:
    """A variable in the example that nothing reads is worse than useless: it reads as a
    knob, and turning it does nothing."""
    unknown = _documented() - _declared() - TEST_ONLY
    assert not unknown, f".env.example names settings that do not exist: {sorted(unknown)}"


def test_the_defaults_run_offline_with_no_credentials() -> None:
    """The claim the quickstart rests on: unedited defaults need no key and no database."""
    fresh = Settings(_env_file=None)
    assert fresh.llm == "fake", "the default model must not need an API key"
    assert fresh.repository == "memory", "the default repository must not need Postgres"
    assert fresh.vlm_model == "", "no OCR fallback by default; an unreadable page fails loudly"


def test_no_real_credential_is_committed_in_the_example() -> None:
    for line in EXAMPLE.read_text().splitlines():
        if line.startswith(("DOCTASK_LLM_API_KEY=", "DOCTASK_REVIEWER_TOKENS=",
                            "DOCTASK_SERVICE_TOKENS=", "DOCTASK_WATCHER_TOKEN=")):
            _, _, value = line.partition("=")
            assert not value.strip(), f"{line.split('=')[0]} must ship empty, not populated"


def test_the_declared_price_table_exists_and_loads() -> None:
    """`DOCTASK_PRICE_TABLE_PATH` points at a real file, or every cost report is a
    startup error waiting to happen."""
    table = load_price_table(settings.price_table_path)
    assert table.version, "the price table has to declare a version the report can cite"
    assert "fake" in table.prices, (
        "the offline model must be priced explicitly -- a zero that means 'free' and a "
        "zero that means 'we do not know' cannot look the same in a cost report"
    )


def test_the_price_table_is_reviewer_readable_json() -> None:
    raw = json.loads(Path(settings.price_table_path).read_text())
    for name, entry in raw["prices"].items():
        assert "input_per_million_usd" in entry, name
        assert "output_per_million_usd" in entry, name

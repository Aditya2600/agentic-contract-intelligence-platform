"""The collection watcher, offline: no model API key, no real filesystem races.

Requirement C is "a file dropped into a watched directory is ingested exactly once,
with no manual API call." These tests are the exactly-once part, from the angles that
actually break a naive poller: the same bytes arriving twice, a file caught mid-write,
two replicas sweeping the same directory, one bad file next to a good one, and a
restart in the middle of a sweep.

Every scenario drives `CollectionWatcher.poll_once()` directly against an
`InMemoryRepository` and the deterministic `FakeLLM` -- no HTTP, no real clock, no
background task. Time only moves when a test calls `poll_once()` again.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

import pytest

from doctask import runtime
from doctask.auth import REVIEWER, SERVICE, AuthorizationError, Principal
from doctask.config import settings
from doctask.watcher import ALREADY_RUN, STARTED, UNREADABLE, UNSTABLE, CollectionWatcher

WATCHER_PRINCIPAL = Principal(actor_id="watcher-bot", role=SERVICE)

MSA_TEXT = """MASTER SERVICES AGREEMENT

Payment is due within 30 calendar days of receipt.
Liability is capped at $250,000.
"""

OTHER_TEXT = """MASTER SERVICES AGREEMENT - BRAVO

Either party may terminate the agreement with 60 days' written notice.
"""


@pytest.fixture(autouse=True)
async def memory_services(monkeypatch):
    """Every test gets its own in-memory repository and deterministic model."""
    monkeypatch.setattr(settings, "repository", "memory")
    monkeypatch.setattr(settings, "llm", "fake")
    await runtime.shutdown_services()
    yield
    await runtime.shutdown_services()


async def _collection(watch_dir: Path) -> UUID:
    services = await runtime.get_services()
    collection_id = await services.repository.create_collection("Acme")
    await services.repository.set_collection_watch_path(collection_id, str(watch_dir))
    return collection_id


def _write(path: Path, text: str) -> None:
    path.write_text(text)


async def test_a_settled_file_is_ingested_after_two_stable_polls(tmp_path) -> None:
    await _collection(tmp_path)
    _write(tmp_path / "msa.txt", MSA_TEXT)
    watcher = CollectionWatcher(principal=WATCHER_PRINCIPAL, interval=0)

    first = await watcher.poll_once()
    assert first.outcomes[tmp_path / "msa.txt"] == UNSTABLE
    assert not first.started

    second = await watcher.poll_once()
    assert second.outcomes[tmp_path / "msa.txt"] == STARTED
    assert len(second.started) == 1


async def test_a_file_still_being_written_is_left_alone(tmp_path) -> None:
    """Growth between polls resets the stability clock. Only a poll that sees the same
    size and mtime as the poll before it may ingest."""
    await _collection(tmp_path)
    path = tmp_path / "growing.txt"
    watcher = CollectionWatcher(principal=WATCHER_PRINCIPAL, interval=0)

    _write(path, "MASTER SERVICES AGREEMENT\n\nPart one.\n")
    assert (await watcher.poll_once()).outcomes[path] == UNSTABLE

    # Still growing: every poll during the write sees a new size, so it never settles.
    for chunk in ("Part two.\n", "Part three.\n", "Payment is due within 30 days.\n"):
        _write(path, path.read_text() + chunk)
        report = await watcher.poll_once()
        assert report.outcomes[path] == UNSTABLE
        assert not report.started

    # The write has stopped: the last poll inside the loop already recorded this exact
    # size and mtime once. The next poll to see the same sighting again is what settles
    # it -- one poll interval after the write actually finished, not before.
    final = await watcher.poll_once()
    assert final.outcomes[path] == STARTED


async def test_the_same_bytes_dropped_twice_yield_one_run(tmp_path) -> None:
    """A second drop of identical content -- a different filename, a resync, a retry --
    must resolve to the same run through the content-hash idempotency key."""
    await _collection(tmp_path)
    watcher = CollectionWatcher(principal=WATCHER_PRINCIPAL, interval=0)

    _write(tmp_path / "msa.txt", MSA_TEXT)
    await watcher.poll_once()  # first sighting
    first_run = (await watcher.poll_once()).runs[tmp_path / "msa.txt"]

    # The same bytes arrive again, under a different name -- a duplicate upload.
    _write(tmp_path / "msa-copy.txt", MSA_TEXT)
    await watcher.poll_once()  # first sighting of the copy
    second_report = await watcher.poll_once()

    assert second_report.outcomes[tmp_path / "msa-copy.txt"] == ALREADY_RUN
    assert second_report.runs[tmp_path / "msa-copy.txt"] == first_run

    services = await runtime.get_services()
    run_ids = {run_id for (_, key), run_id in services.repository.runs.items()}
    assert len(run_ids) == 1


async def test_two_watchers_on_one_directory_produce_one_run_per_file(tmp_path) -> None:
    """Two replicas polling the same directory must not race each other into two runs
    over the same bytes -- the idempotency key, not process coordination, is what
    prevents it."""
    await _collection(tmp_path)
    _write(tmp_path / "msa.txt", MSA_TEXT)

    alpha = CollectionWatcher(principal=WATCHER_PRINCIPAL, interval=0)
    bravo = CollectionWatcher(principal=WATCHER_PRINCIPAL, interval=0)

    # Each replica keeps its own stability memory, so each independently needs two
    # sightings of the file before it is willing to ingest it.
    await alpha.poll_once()
    await bravo.poll_once()
    results = await asyncio.gather(alpha.poll_once(), bravo.poll_once())

    outcomes = {r.outcomes[tmp_path / "msa.txt"] for r in results}
    assert outcomes <= {STARTED, ALREADY_RUN}
    assert STARTED in outcomes  # somebody actually started it

    run_ids = {
        report.runs[tmp_path / "msa.txt"]
        for report in results
        if tmp_path / "msa.txt" in report.runs
    }
    assert len(run_ids) == 1

    services = await runtime.get_services()
    assert len({run_id for (_, key), run_id in services.repository.runs.items()}) == 1


async def test_an_unreadable_file_does_not_stall_the_files_behind_it(tmp_path) -> None:
    """A file this server cannot even decode must not block the good file dropped next
    to it in the same directory, and must not be retried every sweep."""
    await _collection(tmp_path)
    bad_path = tmp_path / "corrupt.txt"
    good_path = tmp_path / "msa.txt"
    # Invalid UTF-8: `_extract_txt` raises `ExtractionError` rather than mangling it.
    bad_path.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    _write(good_path, MSA_TEXT)

    watcher = CollectionWatcher(principal=WATCHER_PRINCIPAL, interval=0)
    await watcher.poll_once()  # first sighting of both
    report = await watcher.poll_once()

    assert report.outcomes[bad_path] == UNREADABLE
    assert report.outcomes[good_path] == STARTED
    assert good_path in report.runs

    services = await runtime.get_services()
    good_run = await services.repository.get_run(UUID(report.runs[good_path]))
    assert good_run.status in {"running", "blocked", "committed"}

    # The bad file is not retried: its sighting is unchanged, so a further sweep must
    # not re-read or re-attempt it.
    again = await watcher.poll_once()
    assert again.outcomes[bad_path] == ALREADY_RUN


async def test_an_unreadable_file_is_recorded_as_a_failed_attempt(tmp_path) -> None:
    await _collection(tmp_path)
    path = tmp_path / "corrupt.txt"
    path.write_bytes(b"\xff\xfe garbage \x80")

    watcher = CollectionWatcher(principal=WATCHER_PRINCIPAL, interval=0)
    await watcher.poll_once()
    await watcher.poll_once()

    services = await runtime.get_services()
    failed_runs = [
        run_id
        for run_id, status in services.repository.run_status.items()
        if status == "failed"
    ]
    assert len(failed_runs) == 1
    events = await services.repository.list_events(failed_runs[0])
    assert any(event.stage == "watch" and event.error_class == "extraction" for event in events)


async def test_killing_and_restarting_the_watcher_mid_batch_is_exactly_once(tmp_path) -> None:
    """The watcher holds no state of its own: a fresh instance re-observes from
    scratch, and the durable idempotency key -- not in-memory history -- is what stops
    a file already ingested from being ingested again."""
    await _collection(tmp_path)
    files = {
        "one.txt": MSA_TEXT,
        "two.txt": OTHER_TEXT,
        "three.txt": MSA_TEXT + "\nAn extra clause makes this a distinct document.\n",
    }
    for name, text in files.items():
        _write(tmp_path / name, text)

    first_life = CollectionWatcher(principal=WATCHER_PRINCIPAL, interval=0)
    await first_life.poll_once()  # first sighting of everything
    mid_batch = await first_life.poll_once()  # everything ingested here
    assert mid_batch.count(STARTED) == 3

    # "Killed": the process, and everything it held in memory, is gone.
    del first_life

    # "Restarted": a new instance, no memory of the previous sweeps, same directory.
    second_life = CollectionWatcher(principal=WATCHER_PRINCIPAL, interval=0)
    # It re-observes from nothing, so it needs two sightings again before it will act
    # on any of these files -- and by the second, every one of them already has a run.
    await second_life.poll_once()
    after_restart = await second_life.poll_once()
    assert after_restart.count(STARTED) == 0
    assert after_restart.count(ALREADY_RUN) == 3

    services = await runtime.get_services()
    run_ids = {run_id for (_, key), run_id in services.repository.runs.items()}
    assert len(run_ids) == 3  # one per distinct document, none lost, none duplicated


async def test_the_watcher_refuses_to_run_as_a_reviewer(tmp_path) -> None:
    """Automation must not be able to hold reviewer standing: a background process
    with a human's credential could advance a gate nobody actually looked at."""
    with pytest.raises(AuthorizationError):
        CollectionWatcher(principal=Principal(actor_id="alice", role=REVIEWER))


async def test_a_run_the_watcher_starts_is_attributed_to_it_and_never_a_reviewer(
    tmp_path,
) -> None:
    await _collection(tmp_path)
    _write(tmp_path / "msa.txt", MSA_TEXT)
    watcher = CollectionWatcher(principal=WATCHER_PRINCIPAL, interval=0)
    await watcher.poll_once()
    report = await watcher.poll_once()
    run_id = UUID(report.runs[tmp_path / "msa.txt"])

    services = await runtime.get_services()
    summary = await services.repository.get_run(run_id)
    assert summary.trigger == "watcher"
    assert summary.trigger_document_id is not None

    events = await services.repository.list_events(run_id)
    trigger_event = next(event for event in events if event.stage == "trigger")
    assert WATCHER_PRINCIPAL.actor_id in trigger_event.reason
    assert "service" in trigger_event.reason


async def test_watcher_never_calls_a_resume_path(tmp_path) -> None:
    """It starts runs and stops: nothing in the watcher may advance a run past its
    first human gate. Proven structurally -- the module never imports the resume
    entry points at all -- rather than by exercising one run to a gate and back."""
    import doctask.watcher as watcher_module

    source = Path(watcher_module.__file__).read_text()
    for forbidden in ("resume_run", "decide_reviewed_items", "override_run_blockers"):
        assert forbidden not in source


async def test_an_oversized_file_is_refused_without_being_read(tmp_path) -> None:
    await _collection(tmp_path)
    path = tmp_path / "huge.txt"
    _write(path, MSA_TEXT)
    watcher = CollectionWatcher(principal=WATCHER_PRINCIPAL, interval=0, max_file_bytes=4)

    await watcher.poll_once()
    report = await watcher.poll_once()
    assert report.outcomes[path] == UNREADABLE

    services = await runtime.get_services()
    assert any(status == "failed" for status in services.repository.run_status.values())


async def test_a_directory_that_does_not_exist_yet_is_not_an_error(tmp_path) -> None:
    """The volume may not be mounted yet, or a collection may name a path before
    anything has been dropped there. Either way, the sweep must not raise."""
    services = await runtime.get_services()
    collection_id = await services.repository.create_collection("Acme")
    await services.repository.set_collection_watch_path(
        collection_id, str(tmp_path / "does-not-exist-yet")
    )
    watcher = CollectionWatcher(principal=WATCHER_PRINCIPAL, interval=0)
    report = await watcher.poll_once()
    assert report.outcomes == {}

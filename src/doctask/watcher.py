"""Poll every collection's watched directory and start a run for each new file.

Requirement C: documents arrive by being dropped somewhere, not by somebody remembering
to POST them. This is that arrival path, and it is deliberately the thinnest thing that
can be correct.

**Polling, not inotify.** A poll survives what inotify does not: a restart (there is no
queue of missed events to lose), an NFS or SMB mount (which does not deliver events at
all), and a container boundary (a bind mount written from the host frequently does not
either). Nothing here asks for sub-second latency, and a design whose failure mode is
"the file is picked up on the next sweep" is worth a great deal more than one whose
failure mode is "the file is never picked up and nothing says so".

**Two observations before a file is real.** A 40MB PDF halfway through being copied is a
valid PDF prefix, and extracting it yields a truncated document that reads exactly like a
complete one -- a contract whose liability cap simply is not in the file. So a file is
ingested only once its size and mtime are identical to what the previous poll saw.

**The watcher owns no state.** Everything durable is in tables that already existed:
`runs` for what has been attempted, `run_events` for why, `documents` for the bytes. The
only thing held in memory is the previous poll's `(size, mtime)` per path, and losing it
costs one extra poll of latency and nothing else. Kill this process at any point and the
worst outcome is a file that gets looked at again.

**Idempotency comes from the bytes.** The key is the file's content hash, so a re-drop of
the same bytes, a restart mid-run, and two replicas sweeping the same directory all
resolve to one run through `UNIQUE (collection_id, idempotency_key)`. There is no per-poll
UUID anywhere in here; if there were, every sweep would start a fresh run over a file that
had already been processed.

**It starts runs and stops.** It authenticates as a service principal and refuses to run
as anything else, it never imports a resume path, and every run it starts parks at the
same human gates as one a person started. The register is not something a background
process may move.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import signal
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from doctask.auth import Principal, authenticate, require_service
from doctask.config import settings
from doctask.domain import RunEvent, WatchedCollection
from doctask.repositories.base import Repository
from doctask.runtime import get_services, ingest_file, shutdown_services
from doctask.services.extraction import ExtractionError

logger = logging.getLogger("doctask.watcher")

# The idempotency key a watched file resolves to. Namespaced so it cannot collide with a
# key an API caller chose, and derived from nothing but the bytes so that two replicas,
# a restart, and a re-drop under a different filename all produce the same one.
KEY_PREFIX = "watch:sha256:"

# Names that are somebody else's write in progress, whatever their size says. A partial
# download or an editor swap file is not a document, and it will be renamed to one when
# it is finished.
IGNORED_SUFFIXES = frozenset({".part", ".partial", ".crdownload", ".tmp", ".swp", ".swx"})

# Outcomes for one file in one poll.
STARTED = "started"  # a run was created and the graph was driven
ALREADY_RUN = "already_run"  # this collection has a run for these bytes already
UNSTABLE = "unstable"  # first sighting, or still being written
UNREADABLE = "unreadable"  # recorded as an attempt; never tried again
UNAVAILABLE = "unavailable"  # vanished or unreadable by the OS mid-poll; try later


def watch_key(digest: str) -> str:
    return f"{KEY_PREFIX}{digest}"


@dataclass(frozen=True, slots=True)
class Sighting:
    """What one poll saw of one file. Two equal consecutive sightings mean settled."""

    size: int
    mtime_ns: int


@dataclass(slots=True)
class PollReport:
    """What one sweep did, per file. Returned rather than only logged so a test can
    assert on the decision instead of on its side effects."""

    outcomes: dict[Path, str] = field(default_factory=dict)
    runs: dict[Path, str] = field(default_factory=dict)

    def count(self, outcome: str) -> int:
        return sum(1 for value in self.outcomes.values() if value == outcome)

    @property
    def started(self) -> list[Path]:
        return [path for path, outcome in self.outcomes.items() if outcome == STARTED]


class CollectionWatcher:
    """One sweep of every watched directory, repeated.

    Construct it with the service principal it runs as; `from_settings` is what the
    process entry point uses. Nothing about the instance is durable -- two of these
    against one directory is a supported deployment, not a race to be avoided.
    """

    def __init__(
        self,
        *,
        principal: Principal,
        interval: float = 5.0,
        max_file_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self.principal = require_service(principal)
        self.interval = interval
        self.max_file_bytes = max_file_bytes
        # path -> what the previous poll saw. Transient by design: see the module
        # docstring. A restart re-observes and costs one extra interval.
        self._seen: dict[Path, Sighting] = {}
        # path -> (the sighting it was hashed at, the key those bytes produced). A
        # processed file stays in the directory, and re-reading every one of them on
        # every sweep is how a watch folder with a few hundred contracts in it turns
        # into constant disk traffic. Purely a memo: it is checked against the current
        # sighting, and losing it costs one re-read.
        self._keys: dict[Path, tuple[Sighting, str]] = {}

    @classmethod
    def from_settings(cls) -> CollectionWatcher:
        """Build from configuration, or refuse.

        Fails closed on the credential the same way the API does: no token configured
        authenticates nobody, and a reviewer token is refused outright rather than
        accepted as more than enough permission.
        """
        if not settings.watcher_token:
            raise RuntimeError(
                "the watcher needs DOCTASK_WATCHER_TOKEN set to one of DOCTASK_SERVICE_TOKENS"
            )
        principal = authenticate(settings.watcher_token)
        return cls(
            principal=principal,
            interval=settings.watch_interval,
            max_file_bytes=settings.watch_max_file_bytes,
        )

    # ------------------------------------------------------------------ the loop

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        """Sweep, sleep, repeat, until asked to stop.

        A poll that raises is logged and the loop continues: the failure of one sweep --
        a database blip, a mount that went away -- is not a reason to stop watching, and
        the next sweep re-derives everything it needs from the tables anyway.
        """
        stop = stop or asyncio.Event()
        logger.info(
            "watching every collection with a watch_path, every %.1fs, as %s",
            self.interval,
            self.principal.actor_id,
        )
        while not stop.is_set():
            try:
                await self.poll_once()
            except Exception:  # a sweep must not kill the watcher
                logger.exception("poll failed; continuing")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval)
            except TimeoutError:
                continue
        logger.info("watcher stopped")

    async def poll_once(self) -> PollReport:
        """One sweep of every watched directory.

        The work list is read from the database each time, so a collection that gains a
        `watch_path` while this is running is picked up without a restart.
        """
        services = await get_services()
        report = PollReport()
        # Rebuilt rather than updated, so a file that was deleted stops being remembered
        # and a file dropped again under the same name starts its two-poll wait over.
        settled: dict[Path, Sighting] = {}
        for collection in await services.repository.list_watched_collections():
            for path in _files_in(Path(collection.watch_path)):
                sighting = _sight(path)
                if sighting is None:
                    report.outcomes[path] = UNAVAILABLE
                    continue
                settled[path] = sighting
                if self._seen.get(path) != sighting:
                    # First sighting, or it moved since the last one: still being
                    # written as far as anything here can tell.
                    report.outcomes[path] = UNSTABLE
                    continue
                outcome, run_id = await self._handle(
                    services.repository, collection, path, sighting
                )
                report.outcomes[path] = outcome
                if run_id is not None:
                    report.runs[path] = run_id
        self._seen = settled
        self._keys = {path: entry for path, entry in self._keys.items() if path in settled}
        return report

    # ------------------------------------------------------------------ one file

    async def _handle(
        self,
        repository: Repository,
        collection: WatchedCollection,
        path: Path,
        sighting: Sighting,
    ) -> tuple[str, str | None]:
        """Ingest one settled file, or explain why not.

        Every failure mode returns rather than raises, because the files behind this one
        in the same sweep have nothing to do with it. A scan nobody can read must not
        stop the contract that was dropped next to it from being processed.
        """
        if sighting.size > self.max_file_bytes:
            reason = (
                f"file is {sighting.size} bytes, over the "
                f"{self.max_file_bytes}-byte watch limit"
            )
            key = _oversize_key(path, sighting.size)
            await self._record_attempt(repository, collection, path, key, reason)
            return UNREADABLE, None

        memo = self._keys.get(path)
        data: bytes | None = None
        if memo is not None and memo[0] == sighting:
            key = memo[1]
        else:
            try:
                data = path.read_bytes()
            except OSError as exc:
                # Being written to over a network mount, permissions, a race with a
                # delete. Nothing durable is recorded: a reason to look again, not a
                # verdict on the file.
                logger.warning("could not read %s: %s", path, exc)
                return UNAVAILABLE, None
            key = watch_key(hashlib.sha256(data).hexdigest())
            self._keys[path] = (sighting, key)

        existing = await repository.find_run_by_idempotency_key(collection.id, key)
        if existing is not None:
            # These exact bytes already have a run in this collection -- started, parked
            # at a gate, committed, or failed to extract. Any of those is an answer, and
            # re-reading the file every interval would spend extraction (and, for a scan,
            # the vision model) on a question already settled.
            return ALREADY_RUN, str(existing)

        if data is None:  # memoised key, but the bytes are actually needed now
            try:
                data = path.read_bytes()
            except OSError as exc:
                logger.warning("could not read %s: %s", path, exc)
                return UNAVAILABLE, None

        try:
            run_id, extracted, _ = await ingest_file(
                collection_id=collection.id,
                idempotency_key=key,
                filename=path.name,
                mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                data=data,
                trigger="watcher",
                principal=self.principal,
            )
        except ExtractionError as exc:
            await self._record_attempt(repository, collection, path, key, str(exc))
            return UNREADABLE, None

        logger.info(
            "%s: started run %s over %s (%d blocks)",
            collection.name,
            run_id,
            path.name,
            len(extracted.blocks),
        )
        return STARTED, str(run_id)

    async def _record_attempt(
        self,
        repository: Repository,
        collection: WatchedCollection,
        path: Path,
        key: str,
        reason: str,
    ) -> None:
        """Write down that these bytes were tried and could not be read.

        The record is a `runs` row keyed on the same content hash the successful path
        uses, so the next sweep's existence check finds it and moves on. Without it the
        watcher re-reads the same unreadable scan every interval forever -- and for a
        PDF that falls through to the vision model, pays for it every time.

        A failed run is not a lost document. The row, its event and the filename are all
        there to be listed; what it is not is work the watcher will silently repeat.
        """
        run_id, duplicate = await repository.create_run(
            collection_id=collection.id,
            run_id=uuid4(),
            idempotency_key=key,
            trigger="watcher",
        )
        if duplicate:
            return
        await repository.add_event(
            RunEvent(
                run_id=run_id,
                stage="watch",
                decision="abstain",
                reason=f"{path.name}: {reason}",
                next_node="end",
                error_class="extraction",
            )
        )
        await repository.fail_run(run_id, reason)
        logger.warning("%s: cannot read %s: %s", collection.name, path, reason)


def _oversize_key(path: Path, size: int) -> str:
    """Identity for a file too large to hash without reading it.

    Not a content hash -- reading the bytes is the thing being refused -- so it is the
    path and size instead. It is stable enough to stop the retry loop, and a file that
    grows or shrinks gets a fresh attempt, which is the right answer for one that was
    still being written when it crossed the limit.
    """
    digest = hashlib.sha256(f"{path}:{size}".encode()).hexdigest()
    return f"{KEY_PREFIX}oversize:{digest}"


def _files_in(root: Path) -> list[Path]:
    """Every candidate file under a watched directory, in a stable order.

    Sorted so two replicas sweep in the same order and a test can reason about "the file
    behind the broken one". A directory that does not exist yet is not an error: the
    volume may not be mounted, and it may appear before the next sweep.
    """
    try:
        if not root.is_dir():
            logger.warning("watch path %s is not a directory", root)
            return []
        candidates = sorted(root.rglob("*"))
    except OSError as exc:
        logger.warning("could not list %s: %s", root, exc)
        return []
    return [
        path
        for path in candidates
        if not path.name.startswith(".")
        and path.suffix.lower() not in IGNORED_SUFFIXES
        and _is_file(path)
    ]


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _sight(path: Path) -> Sighting | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return Sighting(size=stat.st_size, mtime_ns=stat.st_mtime_ns)


async def main() -> None:
    """Standalone entry point: `python -m doctask.watcher`.

    SIGTERM and SIGINT set the stop event rather than killing the process where it
    stands, so a `docker compose down` finishes the file in flight. It would survive
    being killed outright too -- that is what the content-hash key is for -- but there
    is no reason to make it prove that on every deploy.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    watcher = CollectionWatcher.from_settings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await watcher.run_forever(stop)
    finally:
        await shutdown_services()


if __name__ == "__main__":
    asyncio.run(main())

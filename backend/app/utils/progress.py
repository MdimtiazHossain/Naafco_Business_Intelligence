"""Live progress for an upload that is still being processed.

An upload is a background job. ``POST /preview`` and ``POST /{id}/commit`` stage
the file, record the batch and answer **202** with a job to watch; a worker in
``upload/jobs.py`` does the work and reports here, and the browser polls
:func:`snapshot` from another thread while it runs. The job id is the batch's own
``upload_uuid``, generated when the row is created rather than accepted from the
caller — the identifier of a stored record is never something a client chose.

Progress is held in process memory rather than in a table, for two reasons that
both come from the pipeline's own design:

* ``etl.pipeline.run_import`` is one transaction. Rows written inside it are
  invisible to another connection until it commits, and a dry run rolls the whole
  thing back — so a progress row written there could never be read in time, and
  would vanish afterwards.
* On SQLite a second writer would block against the ETL's write lock for the
  whole import, which is precisely the period the user is waiting through.

The cost of that choice is that progress is per process: with more than one
uvicorn worker a poll can land on a worker that never saw the upload. That
degrades to "no progress detail", never to a wrong number — :func:`snapshot`
returns ``None`` and the client falls back to an indeterminate state while the
upload request itself continues normally.

Nothing here is allowed to break an import. Every reporting call is wrapped so
that a bug in progress accounting can never turn a good upload into a failed one.

It lives in ``utils`` rather than in ``upload``, where it is used, because the
ETL pipeline has to emit progress too and ``upload`` already imports ``etl``.
``app.utils`` is the one package that imports nothing from a feature package, so
a reporter defined here can be held by both layers without a cycle.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger("app.upload.progress")


class Phase:
    """The stages a file passes through, in the words the operator reads.

    These are the phases the *server* controls. The browser adds one before them
    — sending the bytes — which only the browser can measure.
    """

    #: Accepted and staged, waiting for a worker. A job can sit here when another
    #: import is already running, and saying so is better than a bar that has not
    #: started for reasons the operator cannot see.
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    READING = "READING"
    #: Every source row written to its staging table, before a word of it has
    #: been validated. Its own phase because it is its own wait: reading the
    #: file and writing what was read are different work, and while they shared
    #: READING's band the second had nowhere to move once the first had used it
    #: up — an 8.8-second insert on a 100,000-row file reported as a frozen bar.
    STAGING = "STAGING"
    VALIDATING = "VALIDATING"
    MAPPING = "MAPPING"
    IMPORTING = "IMPORTING"
    #: The bulk write itself, kept separate from IMPORTING because on a large
    #: file it is the single longest step — a 20 000-row import spends most of
    #: its time here — and folding it into the previous phase left the bar
    #: frozen at 99% for the part the operator waits through most.
    WRITING = "WRITING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    #: Stopped by the user. Terminal like the other two, and distinct from both:
    #: the run neither finished nor broke.
    CANCELLED = "CANCELLED"
    #: Asked to stop, still unwinding. Brief, but it is what the operator sees
    #: between pressing Cancel and the transaction finishing its rollback, and
    #: "Cancelling…" is a truer answer during that window than either "running"
    #: or "cancelled".
    CANCELLING = "CANCELLING"

    ALL = (QUEUED, PREPARING, READING, STAGING, VALIDATING, MAPPING, IMPORTING,
           WRITING, CANCELLING, COMPLETED, FAILED, CANCELLED)
    TERMINAL = (COMPLETED, FAILED, CANCELLED)


#: What share of the server-side work each phase represents, and therefore how
#: the percentage advances. These are shares of *duration*, not of row counts,
#: and they are measured rather than estimated — the numbers below come from
#: timing every phase of a validation over 5,000, 20,000 and 100,000-row files
#: of each format, and they hold to within a few points across all three sizes.
#: The bar still only moves when real work completes: within a phase it
#: interpolates on rows actually processed, and it never moves on a timer.
#:
#: **One table cannot describe both formats.** Reading the file is 57% of an
#: Excel import and 5% of a delimited one — openpyxl builds the entire sheet in
#: one call before a single row can be had, while a CSV is read a line at a
#: time — so a single set of weights leaves the bar frozen through most of one
#: format or racing through most of the other. They are two tables for the same
#: reason a report states its unit: the quantity genuinely differs.
#:
#: The keys of :data:`PHASE_WEIGHTS_BY_SOURCE` are ``etl.readers.SOURCE_TYPE_*``
#: values, spelled here rather than imported because ``etl.readers`` imports this
#: module and the cycle would be real. ``test_upload_progress`` pins them against
#: the readers' own constants, so a source type renamed there fails a test rather
#: than silently falling back to the default table.

#: Delimited text, and the default for any source this table does not name.
#: Default because it is the *pessimistic* choice: it allots reading 5%, so a
#: source that actually spends longer there under-reports its progress rather
#: than claiming work it has not done — which is the direction this project errs
#: in everywhere else.
DELIMITED_PHASE_WEIGHTS: dict[str, int] = {
    #: A queued job has done none of the work, so it contributes nothing to the
    #: bar. It is a phase because the operator needs to be told *why* nothing is
    #: happening, not because any progress has been made.
    Phase.QUEUED: 0,
    Phase.PREPARING: 2,
    Phase.READING: 5,
    Phase.STAGING: 12,
    Phase.VALIDATING: 27,
    Phase.MAPPING: 10,
    Phase.IMPORTING: 12,
    Phase.WRITING: 32,
}

#: Excel. Reading dominates, and most of that is inside a single opaque
#: ``load_workbook`` call that cannot report a position — which is why this
#: phase is the one place the bar legitimately sits still for a long time.
EXCEL_PHASE_WEIGHTS: dict[str, int] = {
    Phase.QUEUED: 0,
    Phase.PREPARING: 2,
    Phase.READING: 56,
    Phase.STAGING: 5,
    Phase.VALIDATING: 12,
    Phase.MAPPING: 4,
    Phase.IMPORTING: 7,
    Phase.WRITING: 14,
}


class PhaseScale:
    """One weight table, with each phase's starting point derived from it.

    Derived rather than declared, so the starts and the weights can never
    disagree — the defect that would show up as a bar that jumps or goes
    backwards between two adjacent phases.
    """

    __slots__ = ("name", "weights", "starts")

    def __init__(self, name: str, weights: dict[str, int]) -> None:
        self.name = name
        self.weights = dict(weights)
        starts: dict[str, int] = {}
        running = 0
        for phase, weight in self.weights.items():
            starts[phase] = running
            running += weight
        self.starts = starts

    def percent(self, phase: str, processed: int, total: int) -> int:
        """Where the bar sits: the phase's start plus its share of the way through."""
        if phase in Phase.TERMINAL:
            return 100
        start = self.starts.get(phase, 0)
        weight = self.weights.get(phase, 0)
        if total > 0 and processed > 0:
            fraction = min(1.0, processed / total)
        else:
            fraction = 0.0
        return min(99, int(start + weight * fraction))


#: Master data. A different set of phases in a different order: a master upload
#: reads its file, loads the codes it will be checked against (MAPPING), checks
#: every row (VALIDATING) and writes them (IMPORTING). It never stages and never
#: writes a fact, and it reaches MAPPING *before* VALIDATING — the reverse of the
#: ETL. Read against the transactional table that put the whole validation pass
#: inside a band the bar had already passed, so it sat motionless on the longest
#: step of the run. The order of this dict is the order of the bar.
MASTER_PHASE_WEIGHTS: dict[str, int] = {
    Phase.QUEUED: 0,
    Phase.PREPARING: 2,
    Phase.READING: 18,
    Phase.MAPPING: 20,
    Phase.VALIDATING: 40,
    Phase.IMPORTING: 20,
}

DELIMITED_SCALE = PhaseScale("delimited", DELIMITED_PHASE_WEIGHTS)
EXCEL_SCALE = PhaseScale("excel", EXCEL_PHASE_WEIGHTS)
MASTER_SCALE = PhaseScale("master", MASTER_PHASE_WEIGHTS)

#: Source type -> the weights that describe it. See the note above on the keys.
PHASE_WEIGHTS_BY_SOURCE: dict[str, PhaseScale] = {
    "EXCEL": EXCEL_SCALE,
    "CSV": DELIMITED_SCALE,
}

#: Used until a source announces itself, and for any source not named above —
#: the in-memory reader behind ``/api/import/*``, whose rows are already in hand
#: and which therefore has no read to report at all.
DEFAULT_SCALE = DELIMITED_SCALE


def scale_for(source_type: str | None) -> PhaseScale:
    """The weights for a source type, falling back rather than failing."""
    if not source_type:
        return DEFAULT_SCALE
    return PHASE_WEIGHTS_BY_SOURCE.get(source_type, DEFAULT_SCALE)

#: How long a finished job stays readable. The client polls once more after the
#: upload request returns, to collect the final counts; a few minutes is far more
#: than that needs and keeps a browser that was closed mid-upload from pinning
#: the entry forever.
JOB_TTL = timedelta(minutes=10)

#: Hard cap on tracked jobs. A job token comes from the client, so this is what
#: stops a caller minting tokens in a loop from growing the registry without
#: bound. The oldest entries are evicted first.
MAX_JOBS = 256

#: A job token is opaque to the server, but it is a dictionary key supplied by a
#: caller, so its shape is constrained: a UUID as the browser's ``crypto
#: .randomUUID()`` produces it. Anything else is ignored rather than tracked.
_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                       r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def is_valid_token(token: str | None) -> bool:
    """True when ``token`` is a UUID this registry will track."""
    return bool(token) and bool(_TOKEN_RE.match(token or ""))


class ImportCancelled(Exception):
    """Raised inside a run whose user asked for it to stop.

    Control flow, not a validation failure, which is why it lives here beside the
    reporter that raises it rather than in ``etl.errors`` — that module catalogues
    reasons a *row* was rejected, and a cancelled import has no bad row.

    It propagates out of the pipeline deliberately. The run is one transaction, so
    unwinding is what rolls back everything the import had written; catching it
    and returning normally would commit a half-loaded file, which is precisely the
    outcome cancellation exists to prevent.
    """


@dataclass
class JobProgress:
    """One upload's live state.

    The record counts are deliberately the same six the upload result reports, so
    the numbers on the progress bar and the numbers on the finished summary are
    the same quantities measured at different times — never two different
    definitions of "valid".
    """

    job_id: str
    operation: str = "VALIDATE"          # VALIDATE or IMPORT
    upload_type: str | None = None
    file_name: str | None = None
    phase: str = Phase.PREPARING
    percent: int = 0
    total_records: int = 0
    processed_records: int = 0
    valid_records: int = 0
    invalid_records: int = 0
    imported_records: int = 0
    failed_records: int = 0
    #: Set only once the run is over, so the client can stop polling.
    done: bool = False
    #: A user-safe sentence. Never an exception string: the API's own masking
    #: rule applies here too, and this text is shown verbatim in the browser.
    message: str | None = None
    upload_id: int | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    #: Set when a user asks the run to stop. An ``Event`` rather than a boolean
    #: because it is written by a request thread and read by a worker thread, and
    #: it is the one field a snapshot shares with the live job rather than
    #: copying — a copy of a cancellation flag would never be observed.
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def cancel_requested(self) -> bool:
        return self.cancel.is_set()

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "operation": self.operation,
            "upload_type": self.upload_type,
            "file_name": self.file_name,
            "phase": self.phase,
            "percent": self.percent,
            "done": self.done,
            "message": self.message,
            "upload_id": self.upload_id,
            "cancel_requested": self.cancel_requested,
            "counts": {
                "total_records": self.total_records,
                "processed_records": self.processed_records,
                "valid_records": self.valid_records,
                "invalid_records": self.invalid_records,
                "imported_records": self.imported_records,
                "failed_records": self.failed_records,
            },
            "started_at": self.started_at,
            "updated_at": self.updated_at,
        }


class _Registry:
    """Thread-safe store of in-flight jobs.

    A dict behind a lock rather than anything cleverer: the working set is the
    handful of uploads happening right now, and every operation is O(1) except
    the eviction sweep, which only runs when a job is created.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, JobProgress] = {}
        self._lock = threading.RLock()

    def start(self, job_id: str, **fields: Any) -> JobProgress:
        with self._lock:
            self._evict()
            job = JobProgress(job_id=job_id, **fields)
            self._jobs[job_id] = job
            return job

    def get(self, job_id: str) -> JobProgress | None:
        with self._lock:
            job = self._jobs.get(job_id)
            # Returned by value: the caller serialises it outside the lock, and a
            # live object would keep changing underneath them mid-render.
            return None if job is None else _copy(job)

    def update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            # The percentage is stored as reported, never clamped upwards to the
            # highest figure yet seen. A clamp was tried here and was a mistake:
            # it did not stop the bar misbehaving, it hid three cases where it
            # already did, and it corrupted the one number that outlives the run.
            # ``jobs._mark_cancelled`` copies this figure onto the batch and the
            # audit records it, so a run stopped a fifth of the way through
            # validation was filed at the highest position ever *announced* —
            # 46% where it had reached 24% — making it indistinguishable from one
            # stopped at the end. ``finish`` deliberately does not take a
            # cancelled job to 100 for exactly that reason, and a clamp here
            # undid it. The cure for a bar that walks backwards is to stop the
            # phases overlapping, which is what the weight tables and the calls
            # below now do, not to paper over the symptom.
            for name, value in fields.items():
                setattr(job, name, value)
            job.updated_at = datetime.now(timezone.utc)

    def request_cancel(self, job_id: str) -> bool:
        """Ask a running job to stop. False if it is not running here.

        Only raises the flag; it never writes a status. The worker is the single
        writer of a job's outcome, and letting a request thread also write one is
        how two threads come to disagree about how a run ended.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.done:
                return False
            job.cancel.set()
            job.updated_at = datetime.now(timezone.utc)
            return True

    def discard(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)

    def _evict(self) -> None:
        """Drop expired jobs, then oldest-first if still over the cap."""
        cutoff = datetime.now(timezone.utc) - JOB_TTL
        for job_id in [j for j, p in self._jobs.items() if p.updated_at < cutoff]:
            self._jobs.pop(job_id, None)
        while len(self._jobs) >= MAX_JOBS:
            oldest = min(self._jobs, key=lambda j: self._jobs[j].updated_at)
            self._jobs.pop(oldest, None)

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()


def _copy(job: JobProgress) -> JobProgress:
    """A snapshot of the job's values, sharing its cancel flag.

    Every field is copied by value except ``cancel``, which is passed through as
    the same ``Event``. That is deliberate and load-bearing: a *copied* flag could
    be set by a request thread and never seen by the worker, which is the one
    thing a cancellation must not do.
    """
    return JobProgress(**{name: getattr(job, name) for name in job.__dataclass_fields__})


_REGISTRY = _Registry()


class ProgressReporter:
    """What the pipeline holds. Calling it is always safe and always cheap.

    A reporter with no ``job_id`` is a working no-op, which is what every caller
    that does not want progress — the CLI scripts, the tests, the ``/api/import``
    endpoints — gets by default. That keeps the reporting calls unconditional at
    the call sites instead of scattering ``if progress is not None`` through the
    thirteen steps.
    """

    __slots__ = ("job_id", "_phase", "_total", "_scale", "_scale_fixed", "_entered")

    def __init__(self, job_id: str | None = None) -> None:
        self.job_id = job_id
        self._phase = Phase.PREPARING
        self._total = 0
        self._scale = DEFAULT_SCALE
        self._scale_fixed = False
        self._entered: set[str] = set()

    def entered(self, phase: str) -> bool:
        """Whether this run has already been in ``phase``.

        Asked by a step that is willing to announce a phase but must not
        re-announce one somebody else has already finished: re-entering
        recomputes the bar from that phase's start and walks it back over ground
        already covered. The reader announces READING when it has a reporter,
        and the pipeline fills that band itself when handed a reader that does
        not — an in-memory source has its rows in hand and never reads at all.
        """
        return phase in self._entered

    def source(self, source_type: str | None) -> None:
        """Adopt the weight table for the source actually being read.

        Called by a reader as it starts, which is both the earliest point the
        format is known for certain and a point at which nothing has been
        reported yet — so the table changes while the bar is still at zero and
        no percentage is ever restated under a different scale.

        Defers to :meth:`use_scale`. A master upload knows what shape its run
        has before it opens the file, and that shape matters more than the
        format: the file is small and the phases are ordered differently.
        """
        if not self._scale_fixed:
            self._scale = scale_for(source_type)

    def use_scale(self, scale: "PhaseScale") -> None:
        """Fix the weight table for a run whose shape the caller already knows.

        Set before the file is opened, so the reader's own choice never displaces
        it and no percentage is ever restated under a different table.
        """
        self._scale = scale
        self._scale_fixed = True

    @property
    def enabled(self) -> bool:
        return self.job_id is not None

    # -- emission -----------------------------------------------------------

    def checkpoint(self) -> None:
        """Stop the run here if the user has asked it to stop.

        Called from :meth:`phase` and :meth:`rows`, which the pipeline already
        invokes at every point it is safe to abandon — between phases, every few
        hundred rows, and after each write chunk. That is why cancellation needed
        no new call sites: the places that report progress are exactly the places
        where stopping is safe.
        """
        if not self.enabled:
            return
        job = _REGISTRY.get(self.job_id)  # type: ignore[arg-type]
        if job is not None and job.cancel_requested:
            raise ImportCancelled(self.job_id)

    def phase(self, phase: str, *, message: str | None = None,
              total: int | None = None) -> None:
        """Enter a phase. The percentage jumps to that phase's starting point."""
        if not self.enabled:
            return
        self.checkpoint()
        try:
            self._phase = phase
            self._entered.add(phase)
            if total is not None:
                self._total = total
            fields: dict[str, Any] = {
                "phase": phase,
                "percent": self._scale.percent(phase, 0, 0),
            }
            if total is not None:
                fields["total_records"] = total
                fields["processed_records"] = 0
            if message is not None:
                fields["message"] = message
            _REGISTRY.update(self.job_id, **fields)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - progress must never fail an import
            logger.debug("progress phase update failed", exc_info=True)

    def rows(self, processed: int, *, valid: int | None = None,
             invalid: int | None = None) -> None:
        """Report position within the current phase, and stop if asked to."""
        if not self.enabled:
            return
        self.checkpoint()
        try:
            fields: dict[str, Any] = {
                "processed_records": processed,
                "percent": self._scale.percent(self._phase, processed, self._total),
            }
            if valid is not None:
                fields["valid_records"] = valid
            if invalid is not None:
                fields["invalid_records"] = invalid
            _REGISTRY.update(self.job_id, **fields)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            logger.debug("progress row update failed", exc_info=True)

    def counts(self, **counts: int) -> None:
        """Set record counters directly, for a step that is not row-by-row."""
        if not self.enabled:
            return
        try:
            _REGISTRY.update(self.job_id, **counts)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            logger.debug("progress count update failed", exc_info=True)

    def finish(self, *, message: str | None = None, failed: bool = False,
               cancelled: bool = False, **counts: Any) -> None:
        """Close the job, however it ended.

        Never calls :meth:`checkpoint`: this is the one method that must run to
        completion for a *cancelled* job, and raising here would leave the job
        marked running forever.

        A cancelled job is not taken to 100%. It did not finish, and a full bar
        would say it had — the percentage it actually reached is the honest thing
        to leave on screen.
        """
        if not self.enabled:
            return
        try:
            phase = (Phase.CANCELLED if cancelled
                     else Phase.FAILED if failed else Phase.COMPLETED)
            _REGISTRY.update(
                self.job_id,  # type: ignore[arg-type]
                phase=phase, done=True,
                **({} if cancelled else {"percent": 100}),
                **({"message": message} if message is not None else {}),
                **counts,
            )
        except Exception:  # noqa: BLE001
            logger.debug("progress finish failed", exc_info=True)

    # -- convenience --------------------------------------------------------

    def every(self, index: int, total: int) -> bool:
        """True when row ``index`` is worth reporting.

        A million-row file must not take a lock a million times. Reporting about
        two hundred times over a file is smooth to the eye and negligible in
        cost, and small files still report on every row.
        """
        if total <= 0:
            return False
        step = max(1, total // 200)
        return index % step == 0 or index == total


# ---------------------------------------------------------------------------
# Module-level API
# ---------------------------------------------------------------------------


def start(job_id: str | None, *, operation: str, upload_type: str | None = None,
          file_name: str | None = None) -> ProgressReporter:
    """Register a job and return the reporter for it.

    An absent or malformed token yields a no-op reporter, so a caller that does
    not want progress — or a client that sent something odd — simply gets an
    import with no progress rather than an error.
    """
    if not is_valid_token(job_id):
        return ProgressReporter(None)
    assert job_id is not None
    _REGISTRY.start(job_id, operation=operation, upload_type=upload_type,
                    file_name=file_name, message=None)
    return ProgressReporter(job_id)


def snapshot(job_id: str) -> dict[str, Any] | None:
    """The job's current state, or ``None`` if this process never saw it."""
    job = _REGISTRY.get(job_id)
    return None if job is None else job.to_dict()


def request_cancel(job_id: str) -> bool:
    """Ask a running job to stop. ``False`` if this process is not running it.

    A ``False`` here is not "cancellation failed" — it means there is nothing
    running to stop, because the job already finished, was never started, or
    belongs to another worker process. The caller decides what that means by
    looking at the batch's status, which is the authority.
    """
    return _REGISTRY.request_cancel(job_id)


def is_cancelled(job_id: str) -> bool:
    """Whether a stop has been asked for. For reporting, not for control flow."""
    job = _REGISTRY.get(job_id)
    return job is not None and job.cancel_requested


def discard(job_id: str | None) -> None:
    if job_id:
        _REGISTRY.discard(job_id)


def clear() -> None:
    """Drop every tracked job. For tests."""
    _REGISTRY.clear()


__all__ = [
    "Phase",
    "PhaseScale",
    "PHASE_WEIGHTS_BY_SOURCE",
    "DELIMITED_PHASE_WEIGHTS",
    "EXCEL_PHASE_WEIGHTS",
    "MASTER_PHASE_WEIGHTS",
    "MASTER_SCALE",
    "DEFAULT_SCALE",
    "scale_for",
    "JOB_TTL",
    "MAX_JOBS",
    "ImportCancelled",
    "JobProgress",
    "ProgressReporter",
    "is_valid_token",
    "start",
    "snapshot",
    "request_cancel",
    "is_cancelled",
    "discard",
    "clear",
]

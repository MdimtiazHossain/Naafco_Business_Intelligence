"""What a load has to be *told*, read off the file as a proposal only.

Two settings are properties of the file rather than of the dataset, and neither
may be taken silently:

* **how the file signs a deduction** — two real receivables extracts disagree,
  and running either through the other's rule roughly doubles every balance;
* **what the file states in full** — a restating file stands down the rows it
  stops naming, so the scope of that claim decides what gets voided.

This module reads both off the file and hands them back as *defaults for a
person to confirm*. It never applies either. The distinction matters most for the
second: inferring from a file's contents what to stand down is the most dangerous
form of invented data this platform could commit, because a file that
accidentally omitted a company would erase that company's book and the erasure
would be indistinguishable from a correct restatement. A default that is shown,
explained and confirmed is a different thing from a value that is assumed.

Scanning costs no extra parse. A reader parses its source once, caches the rows
and hands out a fresh iterator whenever it is asked, so this walk shares the read
with the preview and the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import credit
from .datasets import DatasetSpec, map_headers
from .readers import SourceReader


@dataclass(frozen=True)
class Declarations:
    """The defaults this file suggests, and the evidence behind each."""

    #: ``None`` when the dataset asks no such question.
    convention: credit.ConventionEvidence | None = None
    #: Distinct values of ``spec.restatement_scope_field``, in first-seen order.
    #: Empty when the dataset cannot be restated or the column is absent.
    scope_values: tuple[str, ...] = ()
    #: How many rows carried each scope value, so a preview can say what a
    #: company's presence in the file actually amounts to — one stray row
    #: mentioning company 3000 must not quietly widen a restatement to it.
    scope_counts: dict[str, int] = field(default_factory=dict)

    @property
    def requires_confirmation(self) -> bool:
        """Whether a person has to state something this file cannot settle."""
        return self.convention is not None and not self.convention.decisive


def scan(spec: DatasetSpec, reader: SourceReader) -> Declarations:
    """Read the file's own evidence for each declaration the load needs.

    Values are taken raw, exactly as the reader yields them, rather than through
    the cleaning pipeline: this runs *before* the load, and the load is what
    needs the answer. A value too malformed to interpret is skipped rather than
    guessed at, which for the convention means it contributes no evidence and for
    the scope means the row's own code is simply not among the proposals.
    """
    wants_convention = spec.requires_deduction_convention
    scope_field = spec.restatement_scope_field
    if not wants_convention and scope_field is None:
        return Declarations()

    header_map, _ = map_headers(spec, reader.headers)
    scope_values: list[str] = []
    scope_counts: dict[str, int] = {}
    records: list[dict[str, Any]] = []

    for row in reader:
        record = {
            canonical: value
            for header, value in row.values.items()
            if (canonical := header_map.get(str(header))) is not None
        }
        if wants_convention:
            records.append(record)
        if scope_field is not None:
            raw = record.get(scope_field)
            if raw is None:
                continue
            value = str(raw).strip()
            if not value:
                continue
            if value not in scope_counts:
                scope_values.append(value)
            scope_counts[value] = scope_counts.get(value, 0) + 1

    return Declarations(
        convention=credit.detect_convention(records) if wants_convention else None,
        scope_values=tuple(scope_values),
        scope_counts=scope_counts,
    )


__all__ = ["Declarations", "scan"]

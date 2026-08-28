"""Loading a country target from a file instead of typing it.

The Country Target grid is where one figure per material is entered by hand.
For a plan with three hundred materials that is three hundred keystrokes, so
this is the same act performed from a spreadsheet — and it is *the same act*,
not a second way in.

**Every accepted row goes through** :func:`country.set_lines`. The upload
validates first so it can report every problem at once, but it never writes a
line itself: a material's existence, its company, and the readability of its
volume are checked in one place, and an upload path with its own copy of those
rules is how a file comes to be able to state something the grid would refuse.

**A country target upload is all-or-nothing.** The ETL rejects individual rows
and imports the rest, which is right for a transaction file where one bad
invoice line does not change what the others say. A country target is not that:
it is one deliberate list, and loading 297 of 300 materials leaves the target
short by whatever the other three were worth — the same reason
:func:`country.totals` suppresses a partial sum rather than showing one. So a
file with any unreadable row applies nothing, and the preview lists **every**
problem so the file can be fixed in one pass rather than three.

**It updates what it names and leaves the rest alone.** Materials already on the
version and absent from the file keep their figures; the preview says how many,
so nobody has to guess. Deleting them would be a REPLACE, and this platform's
upload modes are INSERT / UPDATE / UPSERT with no REPLACE — a file that was
missing a sheet must not be able to empty a target somebody spent a week
agreeing.

**Nothing is applied by the call that reads the file.** Preview stages the file
and reports; apply re-reads and **re-validates** the staged bytes before writing.
Re-validating is not belt-and-braces: the version can be approved, or a material
retired, between the two calls, and the preview is a photograph rather than a
promise.

**It runs in the request rather than as a job.** A country target is one row per
material — hundreds, not the hundreds of thousands an allocation or a
transaction import produces — so the work is a single small read and a single
small write, and a background job would add a status to poll for something that
finishes before the response does.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.permission_filter import UserContext
from ..database.models import DimMaterial
from ..database.models_target import TargetCountryLine, TargetPlan, TargetVersion
from ..upload.files import StoredUpload
from ..utils.text import normalize_code
from . import audit as target_audit, country
from .errors import TargetManagementError

#: Headers this reader accepts for each column, lower-cased and stripped.
#:
#: Aliases rather than one exact spelling, for the reason every dataset spec in
#: this project gives: the file comes from whoever produces it, and rejecting
#: ``SKU Code`` because the schema says ``Material Code`` refuses a correct file
#: over a synonym. The Target file's own column is still headed ``SKU Code`` —
#: that is what the planners call it — so it is accepted here too.
MATERIAL_HEADERS: tuple[str, ...] = (
    "material code", "material", "material_code", "sku code", "sku",
    "sku_code", "item code", "product code", "code",
)
VOLUME_HEADERS: tuple[str, ...] = (
    "target volume", "volume", "target_volume", "target vol", "qty",
    "target quantity in volume", "annual volume",
)

#: The template's column order, and the guidance the screen shows beside the
#: download button. The notes are deliberately **not** written into the file:
#: the reader would see them as rows naming a material called "# One row per
#: material", and a template its own reader refuses is worse than one carrying
#: no instructions at all.
TEMPLATE_COLUMNS: tuple[str, ...] = ("Material Code", "Target Volume")
TEMPLATE_NOTES: tuple[str, ...] = (
    "One row per material. A material may appear only once.",
    "Target Volume is a number. Thousands separators are fine; a blank, a "
    "letter or a negative value is refused rather than read as zero.",
    "Materials already on this version and absent from the file keep their "
    "figures — the upload updates what it names and removes nothing.",
    "The file must name only materials belonging to the plan's company.",
)


class UploadUnreadable(TargetManagementError):
    """The file could not be opened, or carries neither of the two columns."""

    code = "TARGET_UPLOAD_UNREADABLE"

    def __init__(self, detail: str) -> None:
        super().__init__(
            f"country target file unreadable: {detail}",
            user_message=detail,
            details={},
        )


class UploadRejected(TargetManagementError):
    """The file was read and some rows are not usable. Nothing was applied.

    Carries every problem rather than the first, because a file fixed one error
    at a time takes as many uploads as it has mistakes.
    """

    code = "TARGET_UPLOAD_REJECTED"

    def __init__(self, rejected: list[dict[str, Any]]) -> None:
        super().__init__(
            f"{len(rejected)} rows rejected",
            user_message=(
                f"{len(rejected)} row{'s' if len(rejected) != 1 else ''} in this "
                f"file cannot be used, so nothing has been applied. A country "
                f"target is one list: loading part of it would leave the target "
                f"short by whatever the rest was worth."
            ),
            details={"rejected": rejected[:50], "rejected_count": len(rejected)},
        )


@dataclass
class RowResult:
    """One line of the file, and what became of it."""

    row_number: int
    material_code: str | None
    raw_volume: Any
    volume: float | None = None
    #: The version's current figure for this material, or ``None`` if new.
    current_volume: float | None = None
    error: str | None = None

    #: A row naming a material and stating no figure. Not an error and not a
    #: change: the template pre-fills every material of the company, so most
    #: rows arrive blank, and rejecting them would make the template unusable.
    #: Counted and reported rather than dropped in silence.
    skipped: bool = False

    @property
    def status(self) -> str:
        if self.error:
            return "REJECTED"
        if self.skipped:
            return "SKIPPED"
        if self.current_volume is None:
            return "NEW"
        if self.volume == self.current_volume:
            return "UNCHANGED"
        return "CHANGED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_number": self.row_number,
            "material_code": self.material_code,
            "raw_volume": (None if self.raw_volume is None
                           else str(self.raw_volume)[:64]),
            "volume": self.volume,
            "current_volume": self.current_volume,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class Preview:
    """What the file says, what it would change, and what stops it."""

    rows: list[RowResult] = field(default_factory=list)
    untouched: int = 0
    material_header: str | None = None
    volume_header: str | None = None

    @property
    def rejected(self) -> list[RowResult]:
        return [row for row in self.rows if row.error]

    @property
    def accepted(self) -> list[RowResult]:
        """Rows that will actually be written — a figure was stated."""
        return [row for row in self.rows if not row.error and not row.skipped]

    @property
    def skipped(self) -> list[RowResult]:
        return [row for row in self.rows if row.skipped and not row.error]

    def to_dict(self) -> dict[str, Any]:
        accepted = self.accepted
        return {
            "rows": [row.to_dict() for row in self.rows],
            "counts": {
                "read": len(self.rows),
                "rejected": len(self.rejected),
                "new": sum(1 for row in accepted if row.status == "NEW"),
                "changed": sum(1 for row in accepted if row.status == "CHANGED"),
                "unchanged": sum(1 for row in accepted
                                 if row.status == "UNCHANGED"),
                #: Listed with no figure. Reported so a mostly-blank template
                #: reads as "nothing stated for these" rather than as rows that
                #: quietly went missing.
                "skipped": len(self.skipped),
                #: Lines already on the version that this file does not name.
                #: Reported because "the upload removes nothing" is a promise a
                #: reader should be able to see kept rather than take on trust.
                "untouched": self.untouched,
            },
            "applicable": not self.rejected and bool(accepted),
            "headers": {
                "material": self.material_header,
                "volume": self.volume_header,
            },
            "notes": self.notes(),
        }

    def notes(self) -> list[str]:
        notes: list[str] = []
        if self.rejected:
            notes.append(
                f"{len(self.rejected)} row"
                f"{'s' if len(self.rejected) != 1 else ''} cannot be used, so "
                f"nothing will be applied. A country target is one list — "
                f"loading part of it would leave the target short by whatever "
                f"the rest was worth.")
        elif not self.accepted:
            notes.append(
                "The file states no figures. Enter a Target Volume against at "
                "least one material and upload it again.")
        if self.skipped:
            notes.append(
                f"{len(self.skipped)} material"
                f"{'s are' if len(self.skipped) != 1 else ' is'} listed with no "
                f"Target Volume. A blank is “nothing stated here”, not a "
                f"target of zero, so {'they keep' if len(self.skipped) != 1 else 'it keeps'} "
                f"whatever figure the version already holds.")
        if self.untouched:
            notes.append(
                f"{self.untouched} material"
                f"{'s' if self.untouched != 1 else ''} already on this version "
                f"{'are' if self.untouched != 1 else 'is'} not named in the "
                f"file and will keep {'their' if self.untouched != 1 else 'its'} "
                f"figure. This upload updates what it names and removes "
                f"nothing.")
        return notes


def _match_header(headers: list[str],
                  accepted: tuple[str, ...]) -> str | None:
    """The first header that names this column, or ``None``.

    Compared on a trimmed, lower-cased, single-spaced form so ``Material  Code``
    and ``material code`` are the same column, while the header is reported back
    exactly as the file spelled it — the file is the record of what arrived.
    """
    for header in headers:
        squashed = " ".join(str(header or "").strip().lower().split())
        if squashed in accepted:
            return header
    return None


def read(stored: StoredUpload, session: Session, *, plan: TargetPlan,
         version: TargetVersion, sheet_name: str | None = None) -> Preview:
    """Read the file and say what applying it would do. Writes nothing.

    Every row is checked, including rows after the first failure: a file fixed
    one error at a time takes as many uploads as it has mistakes.
    """
    try:
        reader = stored.reader(sheet_name=sheet_name)
        headers = list(reader.headers)
    except Exception as exc:  # noqa: BLE001 - any read failure is one message
        raise UploadUnreadable(
            "That file could not be read. Upload an .xlsx or .csv saved from "
            "the template."
        ) from exc

    material_header = _match_header(headers, MATERIAL_HEADERS)
    volume_header = _match_header(headers, VOLUME_HEADERS)
    if material_header is None or volume_header is None:
        missing = " and ".join(
            name for name, found in (("Material Code", material_header),
                                     ("Target Volume", volume_header))
            if found is None)
        raise UploadUnreadable(
            f"The file has no {missing} column. Its columns are: "
            f"{', '.join(str(header) for header in headers[:10]) or '(none)'}. "
            f"Download the template to see the expected headings.")

    preview = Preview(material_header=str(material_header),
                      volume_header=str(volume_header))

    existing = {
        line.material_code: float(line.target_volume or 0)
        for line in session.execute(
            select(TargetCountryLine).where(
                TargetCountryLine.version_id == version.version_id)
        ).scalars()
    }

    seen: dict[str, int] = {}
    for source_row in reader:
        raw_code = source_row.get(material_header)
        raw_volume = source_row.get(volume_header)
        if _blank(raw_code) and _blank(raw_volume):
            # A wholly empty row is spacing, not a mistake. A row with one of
            # the two is a mistake and is reported below.
            continue

        code = normalize_code(raw_code) if not _blank(raw_code) else None
        result = RowResult(row_number=source_row.row_number,
                           material_code=code, raw_volume=raw_volume)

        if not code:
            result.error = "No Material Code on this row."
        elif code in seen:
            result.error = (
                f"{code} appears twice — on row {seen[code]} and here. A "
                f"country target states one figure per material, and choosing "
                f"between two would be a guess.")
        elif _blank(raw_volume):
            # Named, with nothing stated for it. The template pre-fills every
            # material of the company, so this is the ordinary case rather than
            # a mistake — and it must not become a target of zero.
            seen[code] = source_row.row_number
            result.skipped = True
        else:
            seen[code] = source_row.row_number
            try:
                result.volume = country._coerce_volume(code, raw_volume)
            except country.InvalidVolume as exc:
                result.error = exc.user_message

        if not result.error:
            result.current_volume = existing.get(code)
        preview.rows.append(result)

    _check_materials(session, plan, preview)
    named = {row.material_code for row in preview.rows if row.material_code}
    preview.untouched = len({code for code in existing if code not in named})
    return preview


def _check_materials(session: Session, plan: TargetPlan,
                     preview: Preview) -> None:
    """Reject codes the master lacks, and codes belonging to another company.

    One query for the whole file rather than one per row, and the *same* two
    rules :func:`country.set_lines` applies — a plan for one company must not be
    able to state a target for a material that company does not deal in, and a
    material naming no company at all is refused too, because putting it in this
    plan would assert it belongs here.
    """
    codes = {row.material_code for row in preview.rows
             if row.material_code and not row.error}
    if not codes:
        return
    known = {
        material.material_code: material.company_code
        for material in session.execute(
            select(DimMaterial).where(DimMaterial.material_code.in_(codes))
        ).scalars()
    }
    for row in preview.rows:
        if row.error or not row.material_code:
            continue
        if row.material_code not in known:
            row.error = (
                f"{row.material_code} is not in the Material Master. Load or "
                f"correct the master before planning against it.")
        elif known[row.material_code] != plan.company_code:
            owner = known[row.material_code] or "no company"
            row.error = (
                f"{row.material_code} belongs to {owner}, and this plan is for "
                f"{plan.company_code}.")


def apply(session: Session, user: UserContext, *, plan: TargetPlan,
          version: TargetVersion, stored: StoredUpload,
          sheet_name: str | None = None) -> dict[str, Any]:
    """Re-read the staged file, re-validate it and write the accepted lines.

    Re-validated rather than trusting the preview: the version can have been
    approved, or a material retired, between the two calls. The preview is a
    photograph, not a promise.

    Writing goes through :func:`country.set_lines`, so an uploaded figure and a
    typed one are validated, audited and versioned by exactly the same code.
    """
    country.assert_editable(version)
    preview = read(stored, session, plan=plan, version=version,
                   sheet_name=sheet_name)
    if preview.rejected:
        raise UploadRejected([row.to_dict() for row in preview.rejected])
    if not preview.accepted:
        raise UploadUnreadable(
            "The file states no Target Volume for any material, so there is "
            "nothing to apply. A blank is “nothing stated here” rather than a "
            "target of zero — enter a figure against at least one material.")

    result = country.set_lines(
        session, user, version=version, plan=plan,
        entries=[(row.material_code, row.volume) for row in preview.accepted])

    target_audit.record(
        session, action=target_audit.TargetAction.TARGET_UPLOADED,
        plan_id=plan.plan_id, version_id=version.version_id,
        actor=user.username, actor_role=user.role,
        node_label=f"Country target · {stored.original_name}",
        old_value=None,
        new_value=(f"{result['created']} added, {result['updated']} changed, "
                   f"{len(preview.accepted)} rows read"),
    )
    return {
        **result,
        "rows_read": len(preview.accepted),
        "skipped": len(preview.skipped),
        "untouched": preview.untouched,
        "file_name": stored.original_name,
    }


def template(session: Session, plan: TargetPlan,
             version: TargetVersion) -> tuple[bytes, str]:
    """A CSV pre-filled with this plan's own materials and current figures.

    Pre-filled rather than blank, and with the version's current volumes in it,
    because the commonest use is *adjusting* a target rather than entering one
    from nothing — and a template listing only materials of the plan's company
    makes the commonest rejection impossible to hit by accident.

    A material with no figure yet is left **blank**, not zero: a blank round-trips
    as "nothing stated here" and a zero would round-trip as a real target of
    nothing.

    **The file carries data and nothing else.** It first shipped with
    :data:`TEMPLATE_NOTES` written into trailing rows, which was friendly and
    wrong: :func:`read` saw them as rows naming a material called ``# One row
    per material`` and rejected the template this module had just produced. A
    file that its own reader refuses is worse than one with no instructions in
    it, so the guidance lives on the screen beside the download button and the
    CSV stays something that round-trips.
    """
    existing = {
        line.material_code: line.target_volume
        for line in session.execute(
            select(TargetCountryLine).where(
                TargetCountryLine.version_id == version.version_id)
        ).scalars()
    }
    materials = session.execute(
        select(DimMaterial.material_code, DimMaterial.material_description)
        .where(DimMaterial.company_code == plan.company_code,
               DimMaterial.is_deleted.is_(False))
        .order_by(DimMaterial.material_code)
    ).all()

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow([*TEMPLATE_COLUMNS, "Material Description"])
    for code, description in materials:
        volume = existing.get(code)
        writer.writerow([
            code,
            "" if volume is None else _plain(volume),
            description or "",
        ])

    name = f"country-target-{plan.plan_code}-V{version.version_no}.csv"
    # UTF-8 with a BOM: Excel on Windows reads a plain UTF-8 CSV as Latin-1 and
    # turns every Bangla description into mojibake, and this file is meant to be
    # opened in Excel.
    return buffer.getvalue().encode("utf-8-sig"), name


def _plain(value: Any) -> str:
    """A volume as digits, with no thousands separator and no trailing zeros.

    The file round-trips: whatever this writes must be readable by
    :func:`country._coerce_volume` without a person editing it first.
    """
    number = Decimal(str(value)).normalize()
    return format(number, "f")


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


__all__ = [
    "MATERIAL_HEADERS",
    "VOLUME_HEADERS",
    "TEMPLATE_COLUMNS",
    "TEMPLATE_NOTES",
    "UploadUnreadable",
    "UploadRejected",
    "RowResult",
    "Preview",
    "read",
    "apply",
    "template",
]

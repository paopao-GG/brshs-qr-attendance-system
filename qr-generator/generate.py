"""Roster parsing, change detection, and QR image writing. No UI, so this stays
testable on its own.

One PNG per student, named after the student. The payload is the signed form from
trackify.core.qrcodes -- a bare LRN in a QR would be forged by editing digits, which
is the exact failure that module exists to prevent.

Re-running against an updated roster only writes what actually changed. That matters
because a printed card is a physical object: if a student's code is unchanged, their
card is still valid and reprinting it is waste. The previous run's manifest.csv is the
state -- no database, and staff can read it in Excel.
"""

from __future__ import annotations

import csv
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterable

# Reach the repo root for trackify.core.qrcodes, the way scripts/make_qr.py does.
# APPENDED, not inserted at 0: the repo root holds its own app.py (the kiosk), and
# putting it first would shadow this folder's modules both here and under PyInstaller.
sys.path.append(str(Path(__file__).resolve().parents[1]))

from trackify.core.qrcodes import encode  # noqa: E402

# A DepEd Learner Reference Number is normally 12 digits, for every learner -- the
# number is issued once and follows the learner for life, so transferring schools
# changes its first six digits (the issuing school), not its length.
#
# This is recorded but NOT enforced. By decision, whatever the sheet holds is encoded
# exactly as typed: the roster is the authority here, not this constant. The count is
# reported as a neutral note so the fact stays visible without being called an error.
STANDARD_LRN_DIGITS = 12

HEADER_NAME = "name of student:"
BANNER_VALUES = {"male", "female", "female:", "male:"}

QR_BOX_SIZE = 12
QR_BORDER = 4          # the spec quiet zone; below 4 the webcam decoder starts missing

MANIFEST_FIELDS = ["section", "name", "lrn", "lrn_digits", "payload", "filename"]
SKIPPED_FIELDS = ["section", "name", "lrn", "cell", "reason"]
CHANGES_FIELDS = ["change", "section", "name", "lrn", "filename", "detail"]

# Characters Windows forbids outright, plus the ASCII control range.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass(frozen=True)
class StudentRow:
    section: str
    name: str
    lrn: str | None
    source: str          # "11-Initiative!B3" -- so a report row points at a cell


@dataclass(frozen=True)
class Planned:
    """One roster row resolved to the file and payload it should end up with."""
    row: StudentRow
    payload: str
    filename: str                       # relative to out_dir, e.g. "11-Ingenuity\\X.png"
    previous: dict | None = None        # matching row from the last run, if any


@dataclass
class Changes:
    """What a run would do, worked out before anything is written."""
    new: list[Planned] = field(default_factory=list)
    updated: list[Planned] = field(default_factory=list)     # LRN changed -> REPRINT
    moved: list[Planned] = field(default_factory=list)       # renamed/moved section
    repaired: list[Planned] = field(default_factory=list)    # PNG missing from disk
    unchanged: list[Planned] = field(default_factory=list)
    removed: list[dict] = field(default_factory=list)        # gone from the roster
    skipped: list[StudentRow] = field(default_factory=list)  # no LRN, no code possible
    nonstandard: list[StudentRow] = field(default_factory=list)
    had_previous_run: bool = False

    @property
    def to_write(self) -> list[Planned]:
        """Rows whose image must actually be rendered."""
        return self.new + self.updated + self.repaired

    @property
    def total_changes(self) -> int:
        return (len(self.new) + len(self.updated) + len(self.moved)
                + len(self.repaired) + len(self.removed))

    @property
    def needs_reprint(self) -> list[Planned]:
        """Cards already in someone's hand that are now wrong."""
        return self.updated


@dataclass
class Summary:
    written: int = 0
    unchanged: int = 0
    moved: int = 0
    updated: int = 0
    new: int = 0
    repaired: int = 0
    removed: int = 0
    skipped: int = 0
    nonstandard: int = 0
    total_codes: int = 0                # every code in the output folder afterwards
    per_section: dict[str, int] = field(default_factory=dict)
    manifest_path: Path | None = None
    skipped_path: Path | None = None
    changes_path: Path | None = None


def parse_lrn(value: object) -> str | None:
    """Return the LRN as plain digits, or None if the cell is empty.

    The spreadsheet stores LRNs as numbers, so openpyxl hands back 1.11995150037e11.
    str() on that keeps the exponent, which would silently encode a nonsense id, so
    every path here ends at an integer.
    """
    if value is None:
        return None

    if isinstance(value, bool):        # bool is an int subclass; never a valid LRN
        return None

    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not value.is_integer():
            return None
        number = int(value)
    else:
        text = str(value).strip()
        # Tolerate the ways a human types one in: "4035-8015-0012", "403 580 150012".
        text = re.sub(r"[\s\-]", "", text)
        if not text or text.lower().startswith("lrn"):
            return None
        try:
            decimal = Decimal(text)
        except InvalidOperation:
            return None
        if decimal != decimal.to_integral_value():
            return None
        number = int(decimal)

    return str(number) if number > 0 else None


def safe_filename(name: str) -> str:
    """Windows-safe stem for a student name.

    The trailing period on "Almuena, Jan Adriel M." is deliberately kept: the middle
    initial is part of the name, and the full filename ends in ".png" rather than a
    dot, so NTFS is fine with it. Non-ASCII (the "n" in Penafiel) is kept too.
    """
    cleaned = unicodedata.normalize("NFC", name).strip()
    cleaned = _ILLEGAL.sub("-", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # A path component may not END in a dot or space, though one before ".png" is fine.
    cleaned = cleaned.rstrip(" ")

    if not cleaned:
        return "unnamed"
    if cleaned.split(".")[0].upper() in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:150]


def read_roster(xlsx_path: str | Path) -> list[StudentRow]:
    """Every student row across every sheet. Sheet name is the section."""
    import openpyxl

    workbook = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    rows: list[StudentRow] = []
    try:
        for sheet in workbook.worksheets:
            for index, (lrn_cell, name_cell) in enumerate(
                sheet.iter_rows(min_col=1, max_col=2, values_only=True), start=1
            ):
                name = str(name_cell).strip() if name_cell is not None else ""
                # Blank column B drops the MALE / FEMALE: banner rows for free,
                # since those carry the word in column A and nothing in B.
                if not name or name.lower() in BANNER_VALUES:
                    continue
                if name.lower() == HEADER_NAME:
                    continue
                rows.append(StudentRow(
                    section=sheet.title,
                    name=name,
                    lrn=parse_lrn(lrn_cell),
                    source=f"{sheet.title}!B{index}",
                ))
    finally:
        workbook.close()
    return rows


def read_manifest(out_dir: str | Path) -> list[dict]:
    """The previous run's manifest, or [] if this folder has never been used."""
    path = Path(out_dir) / "manifest.csv"
    if not path.is_file():
        return []
    try:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            return [r for r in csv.DictReader(handle) if r.get("name")]
    except (OSError, csv.Error):
        # A corrupt or half-written manifest must not block a run; the worst case is
        # that everything looks new and gets regenerated.
        return []


def plan_changes(
    rows: Iterable[StudentRow], out_dir: str | Path, secret: str
) -> Changes:
    """Work out what a run would do, without writing anything."""
    if not secret:
        raise ValueError("QR secret is empty; codes would be unverifiable.")

    out_dir = Path(out_dir)
    previous = read_manifest(out_dir)
    by_name = {r["name"]: r for r in previous}
    by_lrn = {r["lrn"]: r for r in previous if r.get("lrn")}

    changes = Changes(had_previous_run=bool(previous))
    used: dict[str, int] = {}
    matched: set[int] = set()          # id() of previous rows we accounted for

    for row in rows:
        if not row.lrn:
            changes.skipped.append(row)
            continue
        if len(row.lrn) != STANDARD_LRN_DIGITS:
            changes.nonstandard.append(row)

        payload = encode(int(row.lrn), secret)

        stem = safe_filename(row.name)
        target = f"{safe_filename(row.section)}\\{stem}.png"
        # The roster has no duplicate names today, but a later edit could add one and
        # a silently overwritten file is worse than an awkward name.
        if target in used:
            used[target] += 1
            target = f"{safe_filename(row.section)}\\{stem} ({used[target]}).png"
        else:
            used[target] = 1

        # Match on name first, then LRN: a name match with a different LRN is the
        # "adviser filled in the missing number" case, which must reprint. An LRN
        # match with a different name is a spelling fix, which must not.
        prev = by_name.get(row.name) or by_lrn.get(row.lrn)
        if prev is not None:
            matched.add(id(prev))

        planned = Planned(row=row, payload=payload, filename=target, previous=prev)

        if prev is None:
            changes.new.append(planned)
        elif prev.get("payload") != payload:
            changes.updated.append(planned)
        elif prev.get("filename") != target:
            changes.moved.append(planned)
        elif not (out_dir / target).is_file():
            changes.repaired.append(planned)
        else:
            changes.unchanged.append(planned)

    changes.removed = [p for p in previous if id(p) not in matched]
    return changes


def _qr_image(payload: str):
    import qrcode

    code = qrcode.QRCode(
        box_size=QR_BOX_SIZE,
        border=QR_BORDER,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
    )
    code.add_data(payload)
    code.make(fit=True)
    # Saved at native module size. Resampling a QR softens the module edges and is
    # the reason scripts/make_qr.py's sheet images decode worse than they should.
    return code.make_image(fill_color="black", back_color="white")


def apply_changes(
    changes: Changes,
    out_dir: str | Path,
    progress: Callable[[str], None] = lambda message: None,
) -> Summary:
    """Write only what the plan says changed, then rewrite the reports."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = Summary(
        unchanged=len(changes.unchanged), moved=len(changes.moved),
        updated=len(changes.updated), new=len(changes.new),
        repaired=len(changes.repaired), removed=len(changes.removed),
        skipped=len(changes.skipped), nonstandard=len(changes.nonstandard),
    )

    for row in changes.skipped:
        progress(f"  SKIP    {row.section:<14} {row.name}  (no LRN)")
    for row in changes.nonstandard:
        progress(f"  note    {row.section:<14} {row.name}  "
                 f"({len(row.lrn)}-digit LRN, used as typed)")

    # Renames first: moving the existing file keeps the bytes, so a card already
    # printed under the old spelling stays valid and needs no reprint.
    for planned in changes.moved:
        destination = out_dir / planned.filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = out_dir / (planned.previous or {}).get("filename", "")
        try:
            if source.is_file():
                os.replace(source, destination)
            else:
                _qr_image(planned.payload).save(destination)
        except OSError:
            _qr_image(planned.payload).save(destination)
        progress(f"  MOVED   {planned.row.section:<14} {planned.row.name}  "
                 f"(same code, file renamed)")

    for label, group in (("NEW", changes.new),
                         ("UPDATED", changes.updated),
                         ("REPAIR", changes.repaired)):
        for planned in group:
            destination = out_dir / planned.filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            _qr_image(planned.payload).save(destination)
            summary.written += 1

            if label == "UPDATED":
                old = (planned.previous or {}).get("lrn", "")
                detail = f"  (LRN {old} -> {planned.row.lrn}, REPRINT)"
            elif label == "REPAIR":
                detail = "  (image was missing)"
            else:
                detail = ""
            progress(f"  {label:<7} {planned.row.section:<14} "
                     f"{planned.row.name}{detail}")

    for gone in changes.removed:
        progress(f"  REMOVED {gone.get('section',''):<14} {gone.get('name','')}  "
                 f"(no longer in the roster; its file was left in place)")

    # The manifest must describe the whole current roster, not just this run, or the
    # next comparison starts from a partial picture.
    current = changes.unchanged + changes.moved + changes.to_write
    current.sort(key=lambda p: (p.row.section, p.row.name))
    manifest = [{
        "section": p.row.section, "name": p.row.name, "lrn": p.row.lrn,
        "lrn_digits": str(len(p.row.lrn)), "payload": p.payload,
        "filename": p.filename,
    } for p in current]

    summary.total_codes = len(manifest)
    for entry in manifest:
        summary.per_section[entry["section"]] = \
            summary.per_section.get(entry["section"], 0) + 1

    summary.manifest_path = _write_csv(out_dir / "manifest.csv",
                                       MANIFEST_FIELDS, manifest)
    summary.skipped_path = _write_csv(out_dir / "skipped.csv", SKIPPED_FIELDS, [{
        "section": r.section, "name": r.name, "lrn": "",
        "cell": r.source, "reason": "missing LRN",
    } for r in changes.skipped])
    summary.changes_path = _write_csv(out_dir / "changes.csv", CHANGES_FIELDS,
                                      _change_rows(changes))
    return summary


def _change_rows(changes: Changes) -> list[dict]:
    rows = []
    for planned in changes.new:
        rows.append({"change": "new", "section": planned.row.section,
                     "name": planned.row.name, "lrn": planned.row.lrn,
                     "filename": planned.filename, "detail": "added to the roster"})
    for planned in changes.updated:
        old = (planned.previous or {}).get("lrn", "")
        rows.append({"change": "updated", "section": planned.row.section,
                     "name": planned.row.name, "lrn": planned.row.lrn,
                     "filename": planned.filename,
                     "detail": f"LRN was {old}; old card must be reprinted"})
    for planned in changes.moved:
        old = (planned.previous or {}).get("filename", "")
        rows.append({"change": "moved", "section": planned.row.section,
                     "name": planned.row.name, "lrn": planned.row.lrn,
                     "filename": planned.filename,
                     "detail": f"same code; was {old}"})
    for planned in changes.repaired:
        rows.append({"change": "repaired", "section": planned.row.section,
                     "name": planned.row.name, "lrn": planned.row.lrn,
                     "filename": planned.filename,
                     "detail": "image was missing and was regenerated"})
    for gone in changes.removed:
        rows.append({"change": "removed", "section": gone.get("section", ""),
                     "name": gone.get("name", ""), "lrn": gone.get("lrn", ""),
                     "filename": gone.get("filename", ""),
                     "detail": "no longer in the roster; file left in place"})
    return rows


def generate(
    rows: Iterable[StudentRow],
    out_dir: str | Path,
    secret: str,
    progress: Callable[[str], None] = lambda message: None,
) -> Summary:
    """Plan and apply in one call, for callers that do not need a preview."""
    changes = plan_changes(rows, out_dir, secret)
    return apply_changes(changes, out_dir, progress)


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> Path:
    # utf-8-sig so Excel opens the "n"-with-tilde names correctly instead of mojibake.
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _baked_secret() -> str:
    """The secret compiled into the distributed exe by build.bat.

    Absent when running from source, which is why it is the last resort rather than
    the first: a developer's .env still wins, so rotating locally needs no rebuild.
    """
    try:
        from _baked import SECRET           # type: ignore[import-not-found]
    except Exception:
        return ""
    return str(SECRET).strip()


def read_secret(start: Path | None = None) -> str:
    """TRACKIFY_QR_SECRET from the environment, else a .env, else the baked-in value.

    Matches trackify.core.config._load_dotenv: the environment always wins.
    """
    from_env = os.environ.get("TRACKIFY_QR_SECRET", "").strip()
    if from_env:
        return from_env

    start = start or Path(__file__).resolve().parent
    candidates = [start / ".env", *(parent / ".env" for parent in start.parents)]
    if getattr(sys, "frozen", False):
        candidates.insert(0, Path(sys.executable).resolve().parent / ".env")

    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            for raw in candidate.read_text(encoding="utf8").splitlines():
                line = raw.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == "TRACKIFY_QR_SECRET":
                    return value.strip()
        except OSError:
            continue

    return _baked_secret()

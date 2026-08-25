# Student QR Generator

Reads `student-info.xlsx` and writes **one QR PNG per student, named after the
student**. Runs on a plain Windows PC — this is the tool that does not need the Pi.

```
qr-out/
  11-Initiative/   Almuena, Jan Adriel M..png   Arcilla, Tyrone M..png   ...
  11-Ingenuity/    ...
  11-Innovative/   ...
  manifest.csv     every code written: section, name, LRN, lrn_digits, payload, file
  skipped.csv      the students with no LRN, with the cell each came from
```

## Two audiences

**School staff** get `dist\TRACKIFY QR Generator\` — an exe and a text file, nothing
else. No Python, no dependencies, no secret to type. Build it with:

```
build.bat
```

Zip that folder and send it the way you would send a password (see the secret note
below). No roster ships inside it: staff browse for whichever sheet the adviser sent,
which stops them working from a stale bundled copy.

**You**, from source:

```
pip install -r requirements.txt
.venv\Scripts\python.exe main.py                    # the window
.venv\Scripts\python.exe main.py --cli              # no window
.venv\Scripts\python.exe main.py --cli --dry-run    # report changes, write nothing
```

Use the venv interpreter explicitly. Double-clicking `main.py` hands it to whatever
Python Windows has associated with `.py`, which is usually missing `qrcode`.

## Re-runs only write what changed

Point it at the **same output folder** every time. That folder's `manifest.csv` is the
state: it is how the app knows what it produced last time. A fresh folder makes
everything look new.

| Result | Meaning | Reprint? |
|---|---|---|
| `NEW` | student added to the roster | print their card |
| `UPDATED` | their LRN changed — new payload | **yes, the old card is dead** |
| `MOVED` | name or section spelling changed | no — the file is renamed, code identical |
| `REPAIRED` | the PNG was missing from disk | only if the card was lost too |
| `REMOVED` | gone from the sheet; file left in place | collect their card |
| `SAME` | untouched | no |

The GUI shows this summary and asks before writing anything. `changes.csv` in the
output folder records what moved on the last run.

Matching is by **name first, then LRN**. A name match with a different LRN is the
"adviser filled in the missing number" case, which must reprint; an LRN match with a
different name is a spelling fix, which must not.

## The QR secret

Every code is signed, so a QR cannot be forged by editing digits — this reuses
`trackify/core/qrcodes.py` rather than reimplementing it. The payload looks like:

```
TRK-111995150037-abfdb0c6
     |            |
     LRN          truncated HMAC over the LRN
```

Resolution order is environment variable, then a `.env` walking up from the exe, then
the value **compiled into the exe** by `bake.py` at build time. Baking is why school
staff never see or type it; a developer's `.env` still wins, so rotating locally needs
no rebuild.

**It must be byte-identical to the secret the kiosk runs with.** A different secret
produces codes that look fine, print fine, and are rejected at the gate. The Generate
button stays disabled while the field is empty rather than letting anyone produce an
unverifiable batch.

Two consequences of baking, accepted deliberately:

- **The distributed folder is a key.** Anyone holding the exe can mint a valid code for
  any LRN. Send it like a password, not as a public link.
- **Rotating the secret means rebuilding and redistributing.** `build.bat` writes
  `_baked.py`, builds, then deletes it; that file is gitignored so an interrupted build
  cannot leak the secret into the repository.

## What the current roster produces

124 student rows in, **103 codes out**:

| | Rows | Codes | Skipped |
|---|---|---|---|
| 11-Initiative | 43 | 37 | 6 |
| 11-Ingenuity | 42 | 41 | 1 |
| 11-Innovative | 39 | 25 | 14 |
| **Total** | **124** | **103** | **21** |

`skipped.csv` lists one kind of problem: the **21 students whose LRN cell is blank**,
so no code is possible. Fill those in and re-run — the tool simply adds what was
missing.

### LRN length is not validated

The LRN is encoded **exactly as the spreadsheet has it**. A DepEd LRN is normally 12
digits for every learner — it is issued once and follows the learner for life, so
transferring schools changes its first six digits (the issuing school), not its
length. Five LRNs in this roster are 11 or 13 digits, and by decision they are used as
typed rather than corrected or rejected:

| Student | As typed | Digits |
|---|---|---|
| `Almuena, Yuri Alyssa M.` | 1119955150048 | 13 |
| `Raytana, Ma. Angelica M.` | 4035801510043 | 13 |
| `Gomez, Anica Eunice L.` | 1116535150042 | 13 |
| `Thankappan, Mary Faith Ragie L.` | 49006150157 | 11 |
| `Tolosa, Lorraine Krisha Mae C.` | 11180046587 | 11 |

They still get codes and are **not** listed in `skipped.csv`; the run prints a
one-line `Note` and `manifest.csv` records each LRN's length in `lrn_digits`. The
trade-off to know about: if any of these is a typo, its code encodes a number DepEd
will not recognise, and correcting the sheet later means reprinting that card.

## Reading the spreadsheet

The parser takes the sheet name as the section and skips the `MALE` / `FEMALE:`
banner rows and the repeated header row automatically, so the layout can keep its
current shape.

One trap worth knowing about: **LRNs are stored in the sheet as numbers, not text.**
Excel hands `111995150037` back as `1.11995150037E11`, and anything that calls `str()`
on that silently encodes an exponent instead of an LRN. `parse_lrn` routes every value
through `Decimal` so this cannot happen.

## Printing

Each PNG is 396x396 at native module size — never rescale it with a smoothing filter,
which softens the module edges and is what makes a code fail to read. Print at least
**25 mm** wide, and laminate **matte, not glossy**; glare on a glossy card is the usual
reason a code will not scan.

## Known gap before these codes will scan

These codes carry the **LRN**. The kiosk currently decodes a payload and looks the
number up as `students.id`, the autoincrement primary key
(`trackify/core/service.py:96`) — `students.lrn` is stored but read nowhere. Until the
roster is imported with `students.id = LRN`, or `student_row()` is changed to look up
by `lrn`, a scan will land on the "unrecognised code" screen even though the signature
is valid. `manifest.csv` is exactly the input that importer needs.

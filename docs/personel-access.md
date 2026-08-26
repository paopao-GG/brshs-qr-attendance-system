# TRACKIFY — Personnel Access to Attendance Records

How staff open the attendance register, correct it, see what was changed, and export it.

Companion documents:

| Document | Covers |
|---|---|
| [flow.md](flow.md) | §4.2 the correction rule, §8 privacy and access |
| [analytics-model.md](analytics-model.md) | §1 how excused absences affect the attendance rate |
| [prohibited-items.md](prohibited-items.md) | The other password-free screen on the same kiosk |

---

## 1. The flow

```
scan students all morning
        │
        ▼
staff press "Attendance records" on the waiting screen
        │
        ▼
password  ── wrong ──▶ refused, 5 tries then locked for a minute
        │
        ▼
register for one section, one month
        │
        ├── click a day             ─▶ correct it (type, reason, your name)
        ├── Edit log                ─▶ every change ever made to this section
        ├── Student roster          ─▶ import, edit and deactivate students (section 9)
        ├── Class suspension        ─▶ excuse a whole section for a date
        ├── Send weekly summaries   ─▶ queue one attendance text per guardian
        ├── Export XLSX             ─▶ the same grid as a spreadsheet
        ├── Export analytics        ─▶ six-sheet workbook: trend, risk, AHP, screening
        ├── Change password         ─▶ rotate the shared password
        └── Close                   ─▶ back to the gate
```

The button sits on the **waiting screen only** — never over a result. [flow.md](flow.md) §8 says
the station screen shows the current student and nothing else, and a register is the definition
of history.

---

## 2. What the password can and cannot prove

The paper will make claims about record integrity, so this needs stating precisely.

**It can prove:** that a change happened, when, what the value was before and after, and the
reason given. That is already far more than a paper register offers, where a correction is a pen
stroke over the original with no history at all.

**It cannot prove who.** One shared password authenticates nobody — a typed name is a claim, and
anyone holding the password can type any name. So:

- `attendance_days.corrected_by` — the foreign key to a real account — stays **NULL**
- the typed name goes in `corrected_by_name`, and in `audit_log.actor_name`
- both screens say the name is unverified, in those words

Keeping the two columns apart is the point. Writing a typed name into `corrected_by` would make
an unverified claim indistinguishable from an authenticated one, which is precisely the confusion
an audit trail exists to prevent.

**State this as a limitation in the paper.** It is a small and honest one. Individual logins
are what would close it; they are not built.

### The password itself

- **There is no default.** First use asks staff to *set* one. A known default shipped in the
  client's repository is a back door, not a convenience.
- Stored as an **argon2** hash in the `app_settings` table — never in `config.toml`, which is
  committed to git, and never in plain text.
- Changing it requires the current one, so nobody can lock the staff out of their own records
  from an already-open screen.
- Asked **on every open**, not once per session. "Unlocked earlier today" is not a reason to show
  one student's history to whoever is standing at the gate now.
- Five wrong attempts locks the dialog for a minute. In-process only: someone who can restart the
  application resets the counter. This deters a student who finds an unattended keyboard, not
  someone with the machine.

---

## 3. Corrections

The rule from [flow.md](flow.md) §4.2, which is the whole point:

> Original records are **never overwritten.** A correction is a new row that supersedes the
> original; both remain, and the audit log preserves the chain.

That is what protects the Phase III comparison against manually recorded attendance. If a
correction edited the original row, *"what the system recorded"* and *"what a human decided
afterwards"* would be the same number and the comparison would measure nothing.

### The four types

| Type | Status written | Effect on the attendance rate |
|---|---|---|
| **Excused absence** | `excused` | **Leaves the denominator.** 37 present of 40 sessions with 3 excused is 37/37 = 100%, not 92.5% |
| **Online participation** | `online` | Counts as present |
| **Class suspension** | `excused` + flag `class_suspension`, for **every student in the section** | Removes the day for the whole section |
| **Data error** | staff choose: `present` · `late` · `absent` · `excused` · `online` | Corrects a wrong record — including *"they really were absent"* |

Only **data error** lets staff pick the resulting status. The other three name a specific
circumstance and the status follows from it; offering a free choice would invite recording an
excused absence as "present".

**Reason and name are both mandatory.** A correction with no reason is indistinguishable from
tampering after the fact, and one with no name leaves an audit trail recording everything except
the first thing anyone will ask.

### Class suspension is per section, and the schema is not

`school_days` is keyed by **date alone** — it has no section column — so the existing
`sessions.suspend_day()` is school-wide and cannot express *"8-Bonifacio had no classes today"*.

Rather than reshape that table, a section suspension writes **one ordinary correction per
student**, each individually audited, plus a summary row. The rate arithmetic is identical
because excused already leaves the denominator, and a per-student trail is what anyone reviewing
one child's record actually needs to see.

### The supersede sequence

`idx_attendance_live` is a partial unique index on `(student_id, date) WHERE superseded_by IS
NULL` — exactly one live row per student per day. That makes both obvious orderings illegal:

| Attempt | Fails because |
|---|---|
| Insert the new row live first | Two live rows → **UNIQUE violation** |
| Mark the old row superseded first | Points at an id that does not exist yet → **FOREIGN KEY violation** |

So the new row is **born already superseded**, the old one steps down to point at it, and only
then is the new row released:

```sql
INSERT ... superseded_by = old_id;               -- not live, FK valid
UPDATE old SET superseded_by = new_id;           -- old steps down, FK valid
UPDATE new SET superseded_by = NULL;             -- new becomes live
```

Three statements in one transaction, never two live rows, never a dangling reference. It looks
convoluted and it is not optional — see the comment in `trackify/core/corrections.py`.

---

## 4. The register

One section, one month. Students down the side, days across the top, one letter per cell — the
shape of the SF2 register the school already uses on paper.

```
 Attendance register                                 8-Bonifacio  ·  August 2026
 Section [8-Bonifacio] [August] [2026]
 [Edit log] [Student roster] [Class suspension] [Change password]
 [Send weekly summaries] [Export analytics] [Export XLSX] [Close]
 ✓ present   L late   ✗ absent   E excused   O online   shaded = set by a person
 ─────────────────────────────────────────────────────────────────────────────
                    3  4  5  6  7 ... 13 14        P    L    A    E    Rate
 Aquino, Rafael     ✓  E  ✓  ✓  ✓      E  ✓       11    0    2    2    85%
 Castillo, Mateo    ✓  ✓  ✓  ✗  ✓      E  ✓       13    0    1    1    93%
```

### The cell language

| Status | Cell |
|---|---|
| present | green **check** |
| absent | red **cross** |
| late | amber **L** |
| excused | blue **E** |
| online | blue **O** |
| no record | empty |

Present and absent get shapes rather than letters because a register is read column by column
looking for absences, and a shape finds the eye faster than a letter does. Both are **drawn from
SVG paths** in `trackify/ui/icons.py`, not typed as characters — the same reasoning that kept
emoji off the inspection page: a glyph that depends on a font present on the development laptop
and absent on the Pi fails on the only machine that matters.

The **export keeps letters** (`P L A E O`) deliberately. That is the convention on the SF2
register staff already use on paper, and a check character in a spreadsheet depends on the font
of whoever opens it — Excel, LibreOffice, or Google Sheets. Letters render everywhere and print
cleanly. The on-screen legend names both.

### What the shading means

- **Shaded cells were set by a person, not a scan.** Quieter than the glyph it sits behind, but
  still visible: it is the only thing on the register separating what the scanner recorded from
  what a human decided afterwards, and without it a reader has to open the edit log to know
  whether they are looking at evidence or at a judgement.
- **Weekends are tinted** so an empty Saturday reads as "no class" rather than as a gap in the
  data.
- **A month with no records shows `-`, not 0%.** An undefined rate is not the same as never
  attending, and 0% would read as catastrophic.

## 5. The edit log

Every correction ever made to the section, newest first: when, who typed their name, which
student and date, old value → new value, and the reason.

The student's **name is written into the audit row**, not joined at read time. An audit entry
should be readable on its own years later, and it should record the name as it stood when the
change was made.

## 6. Export

`Export XLSX` writes the same grid via `openpyxl`, with the school name, the section, the month,
a legend, and per-student totals. Corrected cells are tinted in the spreadsheet too, and a note
at the top says what the tint means.

A file shaped like the form staff already know is a file they will actually check; a database
dump gets filed and ignored.

---

## 7. The gate always wins

The records screen is a **page inside the kiosk window**, not a separate window.

> A separate window takes focus away from the kiosk's hidden scan input, so the gate would
> silently stop accepting scans while records were open, with nothing on screen to say why. As a
> page, the scan input keeps focus and **any scan closes the records page and returns to the
> gate.**

Nothing is lost when it closes: corrections save one at a time as they are made, so there is
never a half-finished form to discard.

---

## 8. Where this lives

| Piece | File |
|---|---|
| Correction types, the supersede sequence, the register query, the edit log | `trackify/core/corrections.py` |
| Roster matching, the import plan, single-student edits, deactivation | `trackify/core/enrolment.py` |
| Reading the school's spreadsheet | `trackify/core/roster.py` |
| The roster screen and its dialogs | `trackify/ui/roster.py` |
| Password hashing, change, and the attempt lockout | `trackify/core/security.py` |
| XLSX register export | `trackify/export/xlsx.py` |
| The analytics workbook | `trackify/export/analytics.py` |
| Weekly summaries and the absence reminder | `trackify/notify/periodic.py` |
| Register, edit log, and all the dialogs | `trackify/ui/records.py` |
| The button, and the gate-wins rule | `trackify/ui/kiosk.py` |

---

## 9. The student roster

The same password opens a second screen: **Student roster**, beside the register. It is
what makes the system maintainable without the developer — a transferee in November, or a
parent who changes number, is a job for whoever is at the kiosk.

```
 Student roster                                          103 students · 3 sections
 [Search…            ]  [All sections ▾]      [Import XLSX] [Edit] [Deactivate] [Back]
 ─────────────────────────────────────────────────────────────────────────────
  LRN            NAME                     SECTION        GUARDIAN      CONTACT
  111995150037   Almuena, Jan Adriel M.   11-Initiative  Almuena, E.   0947 817 9371
  432511150038   Arado, Sean Eusef M.     11-Ingenuity   -             -    no contact
```

### One rule for who is a student

**An LRN and a name.** Guardian details are optional and editable on screen.

This is the same rule `qr-generator` uses, and that matters more than it looks. When the
two disagreed — the generator carding anyone with an LRN, the importer demanding a parent
contact as well — the school ended up with **103 printed cards against a database holding
73**, and 30 students presented a perfectly valid card that read *"Student not found"*.
Refusing a student for an empty contact column would also mean the only way to fix that
column is Excel, which is the thing this screen replaces.

### What an import may and may not do

| | |
|---|---|
| **May** | create a student, correct a name, move a section, **fill in** a blank guardian detail |
| **May not** | grant or revoke `consent_on_file`, reactivate a deactivated student, **blank out** a guardian detail a person had typed in |

The last one is not a detail. The office spreadsheet is chronically incomplete — that is
the premise of this whole screen — so if importing it nulled every number staff had
entered, the next import would quietly undo an afternoon's work. **A blank cell means "no
information", not "delete what you have."** Clearing a number is a deliberate act in the
edit dialog, by a person, with a reason.

Consent is excluded for a different reason: `queue.py` checks it before enqueueing
anything, and it is the RA 10173 record. An emailed spreadsheet is not the authority for
that; a person ticking a box having seen the signed form is.

### Matching, and the duplicate it prevents

**LRN first, then name and section** — the order `qr-generator` uses too.

Matching on LRN alone looks obviously right and quietly corrupts the roster. A student
already in the system whose LRN is *corrected* in the sheet would not match, would import
as new, and the school would hold that child twice: one row with their attendance
history, another with their working card. The name fallback catches exactly that, and
reports it as **LRN CHANGED** rather than a plain update, because it has a consequence
that happens off-screen:

> **Their printed card stops working.** The payload is signed over the LRN. Reprint it —
> the QR generator will report that student as `UPDATED`.

### Nothing is written until it is confirmed

`Import XLSX` shows a preview first — new, updated, LRN changed, unchanged, not in this
file, skipped — and writes nothing until Import is pressed. A hundred rows arriving from
a file the adviser emailed is not something to apply on trust.

Students **in the system but absent from the file are left alone**, and listed. An adviser
importing one section's list would otherwise wipe the other two.

### Deactivate, never delete

`scan_events.student_id` is `ON DELETE RESTRICT`: a student who has ever scanned cannot be
deleted, and should not be — their attendance history is the record. Deactivating sets
`active = 0`, and `ScanService.student_row()` filters on it, so their card stops at the
gate immediately. They stay listed, marked `inactive`, so they can be readmitted.

Every insert, update and deactivation writes an `audit_log` row carrying the typed name,
under the same limitation as §2: it records a claim, not an authenticated identity.

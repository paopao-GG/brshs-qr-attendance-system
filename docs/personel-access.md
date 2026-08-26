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
        ├── click a day ─▶ correct it (type, reason, your name)
        ├── Edit log     ─▶ every change ever made to this section
        ├── Export XLSX  ─▶ the same grid as a spreadsheet
        └── Close        ─▶ back to the gate
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
(build step 14) are what would close it.

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
 Section [8-Bonifacio] [August] [2026]   [Edit log] [Suspension] [Export XLSX] [Close]
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
| Password hashing, change, and the attempt lockout | `trackify/core/security.py` |
| XLSX export | `trackify/export/xlsx.py` |
| Register, edit log, and all the dialogs | `trackify/ui/records.py` |
| The button, and the gate-wins rule | `trackify/ui/kiosk.py` |

# TRACKIFY — Technical Design Document

**Tracker for Real-time Attendance and Campus safety, Keeping Intelligent Feedback for Yielding reports**

This is the engineering design of record. [flow.md](flow.md) argues *why* the system behaves
as it does; this file says *what it is made of*. Where the two overlap, this one links rather
than repeats.

**Scale:** 43 modules, ~10,600 lines, 687 tests across 33 test modules.

---

## 1. Purpose and scope

A student presents a QR card at the gate. The system identifies them, records an arrival or a
departure, screens their bag, and texts a guardian — then, over a term, reports who is falling
behind on attendance and why.

### What it is not

Stating this precisely matters more than the feature list, because most of the design follows
from it.

- **Not a sensor platform.** The metal detector is a separate handheld unit operated by a
  person. No reading enters the database; only a human conclusion does. There is no
  `reading_strength` column and no ROC curve, because there is no instrument to characterise.
- **Not an access control system.** It records that a student passed; it never opens or closes
  anything, and it cannot stop someone entering.
- **Not an authority.** The composite risk score *recommends review*. Every consequence is
  decided by a person. A band is an input to a conversation, not a sanction.
- **Not multi-user.** One shared password gates the records screen. It authenticates a role,
  not a person — see §7.

---

## 2. Architecture

Three concurrent parts, one process.

```
                      +---------------------------+
   QR card  ------->  |  KioskWindow  (UI thread) |
   webcam / HID       |   camera -> decoder ->    |
                      |   ScanGate -> ScanService |
                      +-------------+-------------+
                                    | queues rows
                                    v
                      +---------------------------+
                      |  notifications  (SQLite)  |
                      +-------------+-------------+
                                    | 4s drain tick
                      +-------------v-------------+
                      |  SmsWorker  (QThread)     |
                      |   own connection          |
                      |   GsmProvider -> COM3     |
                      +---------------------------+
```

### Why the worker is a separate thread with its own connection

A 2G SMS submit takes **3–10 seconds**, and on a serial modem that is every message, not a
worst case. On the UI thread it would freeze the gate mid-queue — the way most PyQt
applications fail in the field.

Two rules follow, and both are enforced rather than documented:

- **A `sqlite3.Connection` is never shared across threads.** `db.connect()` is thread-local:
  every thread that asks gets its own. The worker opens its own in `start()`, which runs on the
  worker thread, not in `__init__`, which does not.
- **No widget is touched from the worker.** Everything leaves by Qt signal —
  `stats_changed(QueueStats)` and `alarm(str)`.

`tests/test_worker_thread.py` asserts `provider.send()` does not execute on the UI thread. That
is the contract, tested rather than assumed.

### The gate never waits on anything slow

Scanning writes rows and returns. Notifications are *queued*, never sent inline. Screening is
recorded after attendance is already committed, so a screening outcome can never affect whether
attendance was recorded ([flow.md](flow.md) §3 step 6).

### Degradation

Every subsystem fails soft, because a school morning cannot depend on a USB cable:

| Failure | Behaviour |
|---|---|
| Camera dead | Status bar turns amber; keyboard/HID input still works |
| GSM module absent or not answering | Status bar reads `SMS: gsm unavailable`; **the queue is not drained**, so rows stay `pending` with their retry budget intact |
| pyserial missing | Reported as a status, not an exception |
| Database missing | Refuses to start with the command that fixes it |

The GSM case is the subtle one. Draining into an absent module would run every queued
notification through `_retry_or_fail`, and the backoff ladder reaches the retry limit in under
two hours — the morning's texts would be permanently `failed` by mid-morning, and plugging the
module in at noon would send nothing.

---

## 3. Data model

16 tables. SQLite, WAL mode, `foreign_keys = ON`, `isolation_level = None` with explicit
transactions.

| Table | Holds |
|---|---|
| `students`, `sections`, `users` | Roster, class groups, staff accounts. `students.sex` is nullable — DepEd SF2 needs it, a student without one must still enrol and scan |
| `school_days` | Per-date thresholds, **frozen on first use** |
| `scan_events` | Append-only record of every card presented |
| `attendance_days` | One live row per student per date |
| `notifications`, `sms_ledger` | The outbound queue and daily spend |
| `screening_events`, `incidents`, `custody_items`, `hazard_requests` | Gate screening and its consequences |
| `risk_scores`, `ahp_weights` | Analytics output and the weights that produced it |
| `audit_log`, `app_settings` | Who changed what; the records password hash |

### Four structures that carry the design

**The partial unique index.**

```sql
CREATE UNIQUE INDEX idx_attendance_live
    ON attendance_days(student_id, date) WHERE superseded_by IS NULL;
```

At most one *live* row per student per day, while superseded rows stay for history. An
attendance correction is not an UPDATE — it is a new row, with the old one pointing at it.

**The supersede sequence.** Because of that index, a correction takes three statements in a
specific order ([corrections.py](../trackify/core/corrections.py)): insert the new row *already
superseded*, repoint the old row at it, then release the new row to live. Every intermediate
state satisfies both the index and the self-referencing foreign key. Do not hand-roll this;
call `corrections.correct()`.

**`ON DELETE RESTRICT` on `scan_events.student_id`.** A student who has ever scanned can never
be deleted. Deactivation (`active = 0`) is the mechanism, and it exists so that attendance
history cannot be destroyed by a roster edit.

**Frozen school days.** `school_days` stamps `entry_open`, `late_threshold`, `dismissal_time`
and `early_departure_cutoff` from config the first time a date is touched. That row, not
`config.toml`, is the authority afterwards — so editing the late threshold mid-study cannot
retroactively reclassify past attendance.

### Schema evolution

`ensure_columns()` adds missing columns (additive only — SQLite cannot drop or retype one).
Widening a `CHECK` constraint needs a full table rebuild: `_widen_notification_triggers()` does
the documented 12-step copy, driven off `NOTIFICATION_TRIGGERS`, with `foreign_keys` off around
the rename and the whole thing in one transaction.

---

## 4. Module map

Each package owns one layer and must not reach past it.

### `trackify/core/` — domain logic, no Qt

| Module | Owns |
|---|---|
| `db.py` | Connections, schema bootstrap, migrations, `transaction`, `audit` |
| `config.py` | `config.toml` + `.env`; every threshold, never hardcoded |
| `qrcodes.py` | Payload encode/decode and HMAC verification |
| `sessions.py` | Which thresholds apply on a date; the freeze |
| `attendance.py` | `record_scan`, direction state machine, debounce, `close_open_days` |
| `corrections.py` | The supersede sequence, register rendering, edit log |
| `screening.py` | Screening taxonomy and incident validation — pure, DB-free |
| `custody.py` | The custody chain: collect → release → return / dispose |
| `service.py` | `ScanService` — the façade the UI calls |
| `roster.py`, `enrolment.py` | Spreadsheet parsing; applying a roster to the database |
| `mobile.py`, `security.py` | Number normalisation; argon2 password hashing, attempt gate |

### `trackify/notify/` — outbound messages

`provider.py` (the abstraction), `gsm.py` (SIM800C over AT commands), `gsm7.py` (alphabet and
segment counting), `queue.py` (enqueue, claim, drain, retry), `coalesce.py` (siblings into one
text), `limits.py` (spend breaker, token bucket), `periodic.py` (weekly summary,
absence reminder).

### `trackify/ui/` — Qt, no domain logic

`kiosk.py`, `records.py`, `roster.py`, `screening.py`, `custody.py`, `camera.py`, `worker.py`,
`icons.py`. Icons are **SVG paths, not font glyphs** — a glyph that is missing on the Pi renders
as a blank box, and the gate cannot afford an unreadable button.

### `trackify/analytics/` and `trackify/export/`

`trend.py` (OLS on daily rates, Durbin–Watson), `risk.py` (pooled logistic model, the
composite and the incident floor, bands), `ahp.py` (weights and the consistency check), `screening.py`
(descriptive counts). Read-only over the database except `risk.compute(persist=True)`.
`export/xlsx.py` writes the SF2-*shaped* working register (letters, corrections shading, a
per-student rate); `export/sf2.py` writes the DepEd form itself, to the geometry of the LIS
workbook the school submits; `export/analytics.py` writes the six-sheet workbook.

### Dependency direction

`ui → core → db`. Analytics reads the database directly and depends on nothing above it. The
UI never imports `sqlite3` cursors of its own; it goes through `ScanService` or a `core` module.

---

## 5. Design decisions

Beyond the four rules in [flow.md](flow.md) §2:

**The QR payload is keyed on the LRN, not the row id.** A printed card is a physical object
that outlives any particular database. Keying it on an autoincrement id means a reseed silently
invalidates every card already handed out; the LRN follows the learner for life. This was
changed *after* 103 cards had been printed, and the change is what made those cards survive.

**Saturating exponentials, not min–max.** `1 - exp(-rate * n)` with fixed constants. Min–max
lets one extreme student set the maximum and silently rescale everyone else, so scores could
not be compared across sections or across time — which would destroy any longitudinal claim.

**Prohibited item is a band floor, not a weighted criterion.** A confirmed incident sets a
minimum band by severity (`config.toml [risk.incident_floor]`) and leaves the composite
untouched. A weighted fourth criterion was tried and reverted -- it needed a pairwise judgement
nobody on the real panel had actually made, over too few incidents to fit or defend a weight for
either -- see [prohibited-items.md](prohibited-items.md) §9.

**At-most-once SMS.** An ambiguous send — the module going silent after the message body is
written — is parked as `unknown` and **never auto-retried**. A missed text is recoverable; a
duplicate erodes a parent's trust in the system. There is no provider dashboard to reconcile
against, so a human decides.

**Two gates. Consent is the one that travels with the database.** `queue.enqueue` refuses
without `consent_on_file`, before policy — so a database handed to somebody else carries its own
permissions. `SMS_LIVE` in `.env` is the station-wide switch, applied at drain: it decides
whether this machine texts anybody at all, defaults to false, and says nothing about any other
copy of the database. Neither substitutes for the other. See
[sms-notifications.md](sms-notifications.md) §7.

**Every threshold is configuration.** Late time, dismissal, saturation constants, band cutoffs,
absence limits. A band boundary decides whether a real child is referred to guidance; that is
an institutional decision, and the export says who made it.

---

## 6. Interfaces

### QR payload

```
TRK-{lrn}-{hmac8}          e.g.  TRK-999900000018-b56f694a
```

`hmac8` is the first 8 hex characters of `HMAC-SHA256(secret, str(lrn))`, compared with
`hmac.compare_digest`. The secret is `TRACKIFY_QR_SECRET`, from the environment only. An
unsigned or mis-signed code is rejected as `FORGED` and never resolves to a student.

The standalone `qr-generator/` tool bakes the same secret at build time so cards printed on
another machine verify here.

### GSM (SIM800C over USB serial, 115200 8N1)

Open → `AT` probe → `ATE0`, `AT+CMEE=2`, `AT+CMGF=1`, `AT+CSCS="GSM"`, `AT+CMGD=1,4` → health
read (`ATI`, `AT+CPIN?`, `AT+CSQ`, `AT+CREG?`, `AT+CSCA?`, `AT+CBC`) → send with `AT+CMGS`,
body, Ctrl-Z.

The **2-second `AT` probe** exists because `_read_until` never returns early on silence: without
it, a port that is not the module costs `init_timeout` on each of ~13 commands, roughly two
minutes of a wedged worker thread. On Windows the first COM port is usually Bluetooth, so this
is the normal case, not the exotic one.

Bodies are validated as **single-segment GSM-7** at enqueue time, not at send time — failing at
double cost on every retry is worse than failing once in the queue.

### Exports

`export_register(conn, section_id, year, month, path)` — one section, one calendar month,
SF2-shaped, weekends tinted, corrected cells highlighted. The **working** register.

`export_sf2(conn, section_id, year, month, path, config=..., school_name=...)` — the DepEd
School Form 2, built rather than filled from a template: the school's own file has 17 male and
22 female rows baked into its merges, and inserting rows for a class of 41 tears every merge and
border below them. 47 columns, 25 day slots, a male block above a female block, summary panel,
two signature lines.

Its codes are the form's, and they are not the register's — blank for present, `x` for absent, a
shaded cell for tardy. An excused day is **blank and named in REMARKS**, following TRACKIFY's own
rule that it leaves the rate denominator rather than counting against a student; marking it `x`
would contradict the register produced from the same database. A month with more than 25 class
days raises `Sf2Error` rather than dropping a column, and a student with no `sex` recorded goes
into a visible third block rather than being silently omitted.
`export_analytics(conn, config, path, section_id=..., ...)` — six sheets; every sheet is written
even when it cannot be computed, stating what is missing and how much is needed. A missing sheet
reads as a crash and a zero reads as a finding.

### Configuration

`config.toml` for behaviour, `.env` for what is machine-local or secret
(`TRACKIFY_QR_SECRET`, `SMS_LIVE`). New optional sections are merged over defaults so an older
`config.toml` still loads.

---

## 7. Constraints and known limitations

| Constraint | Consequence |
|---|---|
| **2G phase-out** (NTC MC 002-09-2025) | SIM800C is 2G-only and can no longer be type-approved or imported. Fine for the study; a dated expiry for a school deployment |
| **SIM800C power draw** | ~2A on the transmit burst against 0.5–0.9A from a USB port. Presents as a mid-send disconnect, not an error. Idle voltage proves nothing; the sag happens under load |
| **No scheduler** | The end-of-day close hangs off the kiosk clock tick. A day the kiosk never ran past dismissal is never closed. Weekly summaries are a button someone presses, deliberately — 71 texts at once should be a decision |
| **One shared password** | It cannot prove *who*. `corrected_by` stays NULL and the typed name goes in `corrected_by_name`, labelled unverified on screen |
| **No detector metrics** | The handheld unit has no data output. Procedure metrics (coverage, alarm rate, confirmation rate) replaced the ROC curve that was planned |
| **Attendance data is simulated** | `scripts/simulate_term.py` generated it. No figure derived from it may be reported as a finding |
| **Windows-developed** | Never run on the target Raspberry Pi 5 |

---

## 8. Deployment

Windows, for development:

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
cp .env.example .env          # then set TRACKIFY_QR_SECRET
python scripts/seed_demo.py   # imports data/student-list.xlsx
python app.py --windowed --provider console
```

**Raspberry Pi 5 (the gate station).** Debian 13 trixie marks its system Python
`EXTERNALLY-MANAGED`, so the venv is mandatory rather than tidy -- `pip install` outside one
is refused. Install from `requirements.lock`, not from `requirements.txt`: the lock is the
exact set verified on the Pi, and the floors in requirements.txt now resolve to major-version
jumps (zxing-cpp 2 -> 3, qrcode 7 -> 8, pytest 8 -> 9) that nobody has tested that morning.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock
cp .env.example .env          # then set TRACKIFY_QR_SECRET
.venv/bin/python scripts/seed_demo.py --reset
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q    # expect green before going further
.venv/bin/python app.py --windowed --provider console
```

Verified on Raspberry Pi 5 (4 GB), Debian 13 trixie, aarch64, glibc 2.41, CPython 3.13.5,
labwc/Wayland. PySide6 6.11's aarch64 wheel needs glibc >= 2.39, which trixie satisfies; Qt
runs on the Wayland platform plugin with no `QT_QPA_PLATFORM` override, and QtMultimedia uses
the FFmpeg backend bundled in the wheel. No apt packages beyond a stock desktop image.

**Autostart at the gate.** Two files, and the split is deliberate:

- `~/.config/systemd/user/trackify-kiosk.service` -- journald logging
  (`journalctl --user -u trackify-kiosk -f`), operator control (`systemctl --user restart
  trackify-kiosk`), and `Restart=on-failure`. On-failure and *not* always: Escape closes the
  kiosk and exits 0, and `Restart=always` would relaunch it instantly, leaving staff with no
  way out of a frameless fullscreen window.
- `~/.config/labwc/autostart` -- runs `systemctl --user start trackify-kiosk.service` once the
  compositor is up. The unit is deliberately **not** `systemctl --user enable`d:
  `graphical-session.target` is not active in this user manager on Raspberry Pi OS, and
  `default.target` fires before `WAYLAND_DISPLAY` exists.

  A user `autostart` **replaces** `/etc/xdg/labwc/autostart` rather than extending it, so that
  file must keep the system's own four lines (`pcmanfm-pi`, `wf-panel-pi`, `kanshi`,
  `lxsession-xdg-autostart`) or the desktop loses its panel and file manager.

**Reopening it by hand.** Escape closes the kiosk -- deliberately, so nobody is trapped in a
frameless fullscreen window -- and the way back is the **TRACKIFY Scan Station** desktop icon
(`~/Desktop/trackify-kiosk.desktop`, also in the application menu). It runs
`~/.local/bin/trackify-kiosk`, which is `systemctl --user start trackify-kiosk.service` preceded
by a `reset-failed` to clear a `StartLimitBurst` lockout.

Starting the *unit* rather than `app.py` is the point: clicking twice cannot bring up two kiosks
contending for the camera and the same SQLite WAL, and the icon and a cold boot then take an
identical path. The icon is generated into `~/.local/share/icons/` rather than committed --
[ui/icons.py](../trackify/ui/icons.py) rules out image assets in the repo, and that reasoning
holds for in-app icons; launcher chrome degrades to a generic glyph, not a blank button.

**Providers.** `console` (prints, the default), `null` (counts, for a pilot), `gsm` (real).
Console and null never spend load and never text a real parent, which is what makes it safe to
exercise the whole pipeline before go-live.

Both are labelled **`SMS: console (not sending)`** in the kiosk status bar, in the ordinary grey
rather than the amber reserved for faults -- a provider chosen at startup is a setting, not
something to fix. Each provider declares this itself (`NotificationProvider.sends_real_messages`)
so the UI never matches on provider names.

The label matters because **both mark their notifications `sent`**: they return `ok=True`, and
`queue.drain` records the row as delivered even though nothing left the machine. The only trace
afterwards is a `provider_message_id` reading `console-4` or `null-4`. Do not read `sent` counts
from a console or null run as evidence that a guardian was told anything.

**Before real messages leave:** run `python scripts/test_sms.py --check` to confirm the module
answers, the SIM is registered and the supply is above 3600 mV — then set `SMS_LIVE=true` in
`.env` and restart. Until that flag is set the station queues normally and sends nothing, and
the status bar says so. It defaults to false: a station that has not been deliberately switched
on stays silent.

**Modes.** `--windowed` (development), default fullscreen kiosk, `--custody` (the custody desk:
no camera, no SMS worker), `--no-camera` (HID scanner only).

**Diagnostics.** `scripts/check_camera.py` separates "camera won't open" from "code won't
decode" from "decodes but fails HMAC". `scripts/test_sms.py` does the staged SMS bring-up.
`scripts/simulate_term.py` generates demonstration data and `--clear` removes it again.

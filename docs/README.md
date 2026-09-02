# TRACKIFY — Documentation

**Tracker for Real-time Attendance and Campus safety, Keeping Intelligent Feedback for Yielding reports**

QR-based attendance monitoring, campus-safety incident recording, and predictive risk analytics
for a secondary school. Deployed on a Raspberry Pi 5 (4 GB) with a handheld metal detector at
the gate, evaluated over a 20-day school run.

Research: [researcher name redacted] · Bicol Regional Science High School, Region V ·
2026 Division Science and Technology Fair · Computational Science (Individual)

---

## Documents

| Document | Purpose |
|---|---|
| **[TDD.md](TDD.md)** | Technical Design Document — architecture, data model, module map, interfaces, deployment. **Start here if you are reading the code.** |
| **[flow.md](flow.md)** | System flow — actors, entry process, exception paths, records written. **Start here if you are reading the design.** |
| **[prohibited-items.md](prohibited-items.md)** | Screening at the gate: outcomes, incidents, custody chain, why incidents are reported descriptively rather than scored |
| **[analytics-model.md](analytics-model.md)** | Attendance rate, trend regression, absence probability, AHP weighting, the composite risk score and incident floor, bands |
| **[sms-notifications.md](sms-notifications.md)** | GSM-module SMS transport, store-and-forward queue, message templates, spend limits, privacy |
| **[personel-access.md](personel-access.md)** | The records screen: password gate, attendance corrections, roster import, exports |
| **[updating.md](updating.md)** | Getting a change from the development PC onto the deployed Pi: the standard procedure, and what each kind of change needs on top of it — schema, dependencies, `config.toml`, the roster, a reset, a rollback |
| **[hardware.md](hardware.md)** | Raspberry Pi 5 station, camera and GSM bring-up, screening procedure metrics. §§1–4, 6, 7, 9, 10 describe a detector the project no longer builds |
| **[research-plan-review.md](research-plan-review.md)** | Defects found in `RESEARCH-PLANCURRENT.docx`, with fixes, by priority |
| `RESEARCH-PLANCURRENT.docx` | The research plan itself. Source of record — not modified by these docs |
| `SF2_2025_Grade-10-Year-IV-RESILIENT.xls` | The school's own LIS-generated School Form 2. The reference the export's geometry was read out of — not modified, not read at runtime |

---

## The four rules everything else follows

Stated in full in [flow.md](flow.md) §2. In short:

1. **The sensor never writes to a student record.** A detector reading is a device event; only a
   guard-confirmed finding is attached to a person.
2. **The scan arms the screening.** Attribution is deterministic, never inferred from a time
   window — a screening hangs off the `scan_event_id` that armed it.
3. **Declare-first is mandatory.** Phone, laptop, and tumbler go in a tray before the bag is
   checked, or the false-positive rate makes the system useless.
4. **Screening is universal, not sampled.** Every student who scans in is screened, and the
   outcome is recorded **including the clears** — they are the denominator of the confirmation
   rate. This reverses an earlier sampling design that existed only because the coil box handled
   one bag at a time; a handheld detector does not have that constraint. See
   [prohibited-items.md](prohibited-items.md) §2.

---

## Key decisions

| Decision | Choice | Where it is argued |
|---|---|---|
| Detector | **Separate handheld unit, operated by a person.** The DIY pulse-induction coil box was deferred; the metrics it would have produced are not available and the honest procedure metrics replaced them | [hardware.md](hardware.md) §8, [prohibited-items.md](prohibited-items.md) §1 |
| Screening basis | Universal, every arrival, clears recorded | [prohibited-items.md](prohibited-items.md) §2 |
| QR input | USB webcam; a USB HID scanner is a drop-in upgrade | [hardware.md](hardware.md) §5 |
| QR payload | Keyed on the **LRN**, HMAC-signed. A printed card outlives any one database, so keying it on a row id would silently invalidate every card on a reseed | [TDD.md](TDD.md) §6 |
| SMS transport | **SIM800C GSM module** on USB serial. Reverses the earlier HTTP-API decision on cost and privacy; 2G's phase-out under NTC MC 002-09-2025 is a documented limitation | [sms-notifications.md](sms-notifications.md) §1 |
| Register exports | **Two, not one.** `Export XLSX` is the working register — letters, corrections shading, a per-student rate — and `Export SF2` is the DepEd form, which carries none of that because it is a submission | [personel-access.md](personel-access.md) §6 |
| SF2 layout | **Built, not filled from a template.** The school's file has 17 male and 22 female rows baked into its merges; inserting rows for a class of 41 tears every merge below them | [personel-access.md](personel-access.md) §6.2 |
| Where sex comes from | The `MALE` / `FEMALE` **banner rows** in the office spreadsheet — the only place that file records it. The importer reads them as data rather than skipping them | [personel-access.md](personel-access.md) §6.3 |
| SMS delivery bias | **At-most-once.** An ambiguous send is parked as `unknown` for a human, never auto-retried — a missed text is recoverable, a duplicate erodes trust | [flow.md](flow.md) §4.1 |
| Notification gate | `consent_on_file`, checked at enqueue. No consent, no message, whatever the policy says | [sms-notifications.md](sms-notifications.md) §6 |
| Absence prediction | Logistic regression — a linear model cannot output a probability | [analytics-model.md](analytics-model.md) §3–4 |
| Risk normalisation | Saturating exponentials, not min–max: one extreme student must not rescale everyone else | [analytics-model.md](analytics-model.md) §6 |
| Prohibited items in risk | **Not a weighted criterion.** A confirmed incident sets a MINIMUM band by severity, keyed off `config.toml [risk.incident_floor]`; it raises the band and leaves the composite untouched. A weighted 4th criterion was tried and reverted — it needed a pairwise judgement nobody on the real panel had made | [prohibited-items.md](prohibited-items.md) §9 |
| Risk coverage | Scored for everyone, acted on above threshold | [analytics-model.md](analytics-model.md) §8 |

---

## Status

**Built and under test.** 44 modules, ~11,500 lines, **780 passing tests** across 34 test
modules, green on the deployment Pi as well as on Windows. The full entry flow, screening, custody chain, records screen with corrections, roster
import, SMS queue over a live SIM800C, analytics and all three exports are implemented and
exercised.

Verified end to end on real hardware: all seven guardian message types have been sent from the
module to a live handset.

| Area | State |
|---|---|
| Scan → attendance → notification | Working, on hardware |
| Screening, incidents, custody | Working |
| Records screen, corrections, roster import, XLSX export | Working |
| DepEd SF2 export | Working — every student is classified M or F |
| Analytics (trend, risk, AHP, screening) and the workbook | Working |
| Weekly summary and absence reminder SMS | Working |
| Adviser dashboard, per-user logins | **Not built** — one shared password gates the records screen; see [personel-access.md](personel-access.md) §3 |
| Raspberry Pi deployment | **Done** — running on the Pi 5 gate station: 780 tests green on-device, kiosk and custody desk verified fullscreen under labwc/Wayland, camera live, autostart armed. SMS not yet exercised on the Pi (see below) |

### The attendance data is simulated

The database is populated by `scripts/simulate_term.py`, which generates a plausible two weeks
of gate traffic. **Those numbers are invented.** They exist so the exports and the analytics
have something to render, and the script prints that warning every time it runs.

**No figure derived from them may be reported as a finding.** The trend slope, the p-value, the
R², the AUC and the band distribution are all outputs of a simulator. Clear the range with
`python scripts/simulate_term.py --clear` before the real pilot.

The **Pi's** database has never been simulated into: it was seeded from the office sheet
and nothing else, so `scan_events`, `attendance_days` and `notifications` are all empty. If
you run the simulator on the station to demonstrate the exports, clear it again before the
pilot starts.

### Remaining before the pilot

1. ~~Re-import `data/student-list.xlsx` from the roster screen.~~ — closed by the Pi bring-up.
   The station's database was seeded fresh from the current office sheet (`seed_demo.py
   --reset`): **110 students across 3 sections, every one classified M or F**, so the SF2 export
   has what it needs. 15 rows were skipped and 30 students have no guardian number on file —
   both reported in `data/seed-report.txt` and fixable from the roster screen. The corrected LRN
   still means one printed card must be reprinted
2. Apply the Priority 1 fixes in [research-plan-review.md](research-plan-review.md) to the .docx
3. Collect signed consent — `consent_on_file` is 1 for a single student (the synthetic demo
   row), so the SMS queue refuses every one of the 110 imported students. Correct behaviour, not
   a fault: a silent queue on the Pi is the consent gate working
4. ~~Deploy to the Pi 5 and run the kiosk from it~~ — done. The real `TRACKIFY_QR_SECRET` is
   in `.env`, so printed cards verify; the kiosk runs `--provider gsm`; and outbound SMS is
   governed by **`SMS_LIVE`** in `.env` (station-wide) plus `consent_on_file` (per student).
   `SMS_LIVE` is currently **false**, so the station queues normally and sends nothing.

   The station also has a desktop launcher, **TRACKIFY Scan Station**, for reopening the kiosk
   after someone presses Escape. It starts the same systemd unit the boot autostart does, so it
   cannot bring up a second instance. See [TDD.md](TDD.md) §8

   **The SIM800C is not usable yet, and the fault is power, not software.** The CH340 bridge
   enumerates from USB alone, so `/dev/ttyUSB0` appears whether or not the module behind it is
   alive. It now answers `AT` and completes most of the init sequence, then drops off the bus
   partway through `AT+CMGD` and re-enumerates in a loop — device numbers 70→71→72 within two
   seconds, with the USB PID changing from `1a86:7523` to `1a86:0323`. That is the brownout
   [hardware.md](hardware.md) §5 warns about: the module pulls up to 2 A and a USB port supplies
   0.5–0.9 A. **Feed VBAT from a supply that can deliver 2 A**, then re-run
   `scripts/test_sms.py --check`. Until it is stable the kiosk shows `SMS: gsm unavailable` in
   amber and leaves the queue undrained with its retry budget intact, which is the designed
   behaviour.

   Note also that `find_port()` matches only `1a86:7523`. If this board legitimately settles on
   `1a86:0323` once powered properly, that PID needs adding.
5. One-week pilot on a single section before the 20-day run

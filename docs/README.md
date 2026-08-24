# TRACKIFY — Documentation

**Tracker for Real-time Attendance and Campus safety, Keeping Intelligent Feedback for Yielding reports**

QR-based attendance monitoring, campus-safety incident recording, and predictive risk analytics
for a secondary school. Deployed on a Raspberry Pi 5 (4 GB) with a custom coil-based bag
screening box, evaluated over a 20-day school run.

Research: [researcher name redacted] · Bicol Regional Science High School, Region V ·
2026 Division Science and Technology Fair · Computational Science (Individual)

---

## Documents

| Document | Purpose |
|---|---|
| **[flow.md](flow.md)** | System flow — actors, entry process, exception paths, records written. **Start here.** |
| **[hardware.md](hardware.md)** | Raspberry Pi 5 station, DIY pulse-induction detector box, wiring, safety, calibration, test protocol, BOM |
| **[analytics-model.md](analytics-model.md)** | Attendance rate, trend regression, per-student absence probability, AHP weighting, composite risk score |
| **[sms-notifications.md](sms-notifications.md)** | Semaphore SMS integration, store-and-forward queue, message templates, cost model, privacy |
| **[research-plan-review.md](research-plan-review.md)** | Defects found in `RESEARCH-PLANCURRENT.docx`, with fixes, by priority |
| `RESEARCH-PLANCURRENT.docx` | The research plan itself. Source of record — not modified by these docs |

---

## The four rules everything else follows

Stated in full in [flow.md](flow.md) §2. In short:

1. **The sensor never writes to a student record.** A detector reading is a device event; only a
   guard-confirmed finding is attached to a person.
2. **The scan arms the box.** Attribution is deterministic, never inferred from a time window.
3. **Declare-first is mandatory.** Phone, laptop, and tumbler go in a tray before the bag goes
   in the box, or the false-positive rate makes the system useless.
4. **Screening is randomly sampled**, not universal — matching the surprise-inspection basis and
   resolving throughput.

---

## Key decisions

| Decision | Choice | Where it is argued |
|---|---|---|
| Detector form factor | Bag-in-a-box, not a walk-through archway | [hardware.md](hardware.md) §2 |
| Detector topology | Pulse induction, single coil, no nulling | [hardware.md](hardware.md) §4 |
| Real-time sensing | RP2040 Pico front end; the Pi 5 has no ADC and Linux cannot hold µs timing | [hardware.md](hardware.md) §1 |
| QR input | USB webcam in V1; a USB HID scanner is a drop-in upgrade | [hardware.md](hardware.md) §5 |
| SMS transport | Semaphore API; **no** GSM module — 2G-only modules are caught by NTC MC 003-09-2025 | [sms-notifications.md](sms-notifications.md) §1 |
| Notification policy | Exception-only, not every arrival | [sms-notifications.md](sms-notifications.md) §4 |
| Absence prediction | Logistic regression — a linear model cannot output a probability | [analytics-model.md](analytics-model.md) §3–4 |
| Risk normalisation | Saturating exponentials, not min–max | [analytics-model.md](analytics-model.md) §6 |
| Risk coverage | Scored for everyone, acted on above threshold | [analytics-model.md](analytics-model.md) §8 |

---

## Status

Documentation only — no implementation yet.

Suggested order of work:

1. Apply the Priority 1 fixes in [research-plan-review.md](research-plan-review.md) to the .docx
2. Build the Pi 5 station: QR scan → student lookup → attendance record ([hardware.md](hardware.md) §10, steps 1–2)
3. Build and characterise the detector ([hardware.md](hardware.md) §10, steps 3–10)
4. Wire in notifications with `ConsoleProvider`, then `NullProvider` on a pilot section
5. One-week pilot on a single section before the 20-day run

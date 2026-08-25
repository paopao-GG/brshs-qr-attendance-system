# Review — `RESEARCH-PLANCURRENT.docx`

Findings against the current research plan, ordered by how much damage each does if it reaches
a judge unfixed. **The .docx has not been modified.** This is a fix list.

Reviewed: 2026-08-22 · Document: *Development and Assessment of TRACKIFY* · LEARNER, Demo
Learner · Bicol Regional Science High School, Region V · Computational Science (Individual)

---

## Priority 1 — fix before submission

### 1. "NOTIFIER" leftovers from a previous project

The system is named TRACKIFY, but a prior project's name survives in four places:

| Location | Current text |
|---|---|
| §G, ¶3 | *"…determining whether **NOTIFIER** effectively addresses the limitations of the manual attendance process…"* |
| Table 4 title | *"Performance Evaluation of **NOTIFIER** (Students)"* |
| Table 5 title | *"Parents' Evaluation of **NOTIFIER**"* |
| Table 6 title | *"Teachers' Evaluation of **NOTIFIER** System"* |

Especially damaging in §G, where the same paragraph uses "TRACKIFY" two sentences earlier.

**Fix:** replace all four with TRACKIFY.

---

### 2. The acronym is expanded two different ways

Four instances read:

> Tracker for Real-time Attendance **and** Campus safety, **Keeping** Intelligent Feedback for Yielding reports

One instance — the `Title:` line in the header block on the first page of the plan proper —
reads:

> Tracker for Real-time Attendance, Campus Safety **and** Intelligent Feedback for Yielding reports

The second version also drops the **K**, so it no longer spells TRAC**K**IFY.

**Fix:** standardise on the first version. Also normalise capitalisation — *"Campus safety"*
should be *"Campus Safety"* throughout — and close the missing space in
*"TRACKIFY(Tracker"* in the Null Hypothesis.

---

### 3. §F does not mention the detector at all

§F lists three risks: data corruption, data privacy, device malfunction. There is nothing about
the metal detector — no electrical hazard, no coil energy, no false-positive consequences, no
handling of a device that will be operated near students by school staff.

This is the section a safety reviewer reads first, and the omission is conspicuous given that
the study's distinguishing feature is a custom-built detector.

**Fix:** add the risks and mitigations from [hardware.md](hardware.md) §6 — coil node voltage
and enclosure, MOSFET flyback, sacrificial front end protecting the Pi, isolated 12 V supply,
lid interlock, and the fact that **no person enters the field** because the bag-box form factor
confines it to the enclosure. That last point is a genuine strength and should be stated
explicitly rather than left for a reviewer to wonder about.

Add one more risk not currently anywhere in the document: **false accusation.** A detector
alarm on an innocent student is a real harm. The mitigation is architectural — see item 4.

---

### 4. Missing consent, assent, and third-party disclosure

The plan discusses the Data Privacy Act in §A as background, but never states what the study
itself will do about it. Three gaps:

- **No parental consent or student assent procedure.** The study involves minors, records
  sensitive information about them, and notifies their guardians.
- **A record stating that a prohibited item was found on a named minor is sensitive personal
  information** under RA 10173. It is not equivalent to attendance data and needs its own
  handling, access control, and retention rule.
- ~~**No disclosure that data leaves campus.**~~ **Resolved by the transport change.** SMS
  now goes out through an on-campus GSM module rather than a third-party API, so guardian
  numbers and student identifiers never reach an external provider. §F should state that
  positively. Note the remaining limitation instead: the module is 2G-only and 2G is being
  phased out under NTC MC 002-09-2025. See [sms-notifications.md](sms-notifications.md) §1.

**Fix:** add a subsection to §F covering consent and assent, data classification, access
control by role, retention period, and the third-party transfer.

---

### 5. Linear regression cannot produce a probability of absence

§G specifies linear regression between attendance and number of school days, and the system
flow described it as producing "a student's possibility of absence."

A linear function is unbounded — it will return values above 1 and below 0, which are not
probabilities. Separately, regressing *cumulative* attendance on day index yields R² ≈ 0.99
trivially, because the slope is just the attendance rate restated. That looks like a strong
result and contains no information.

**Fix:** split the claim into two models, as specified in
[analytics-model.md](analytics-model.md) §2–§4:

- **Linear regression** on the **daily attendance rate** vs school-day index → school-wide
  trend. Report slope, 95% CI, R², and a Durbin–Watson statistic (time series are
  autocorrelated, which OLS assumes away).
- **Logistic regression** → per-student P(absent). Bounded in (0,1) by construction. Report
  precision, recall, and AUC rather than accuracy, since absences are the rare class.

---

### 6. AHP has no consistency check and no named source of judgements

§G introduces AHP to determine weights but does not say who provides the pairwise judgements or
how their coherence is verified. Without a consistency ratio, AHP is just assigning weights by
hand with extra steps.

**Fix:**
- Name the panel — **guidance counsellor and discipline officer**, not the researcher — and
  record the elicitation session and the raw matrix.
- Report **CR = CI/RI** and require **CR ≤ 0.10**.
- Use **three criteria, not two.** A 2×2 pairwise matrix is always perfectly consistent by
  construction, so CR = 0 regardless of the inputs and the check proves nothing. At n = 3 it
  becomes meaningful. Worked example in [analytics-model.md](analytics-model.md) §5.

---

### 7. The risk formula adds quantities with different units

The formula as described multiplies a weight by a regression output and adds a weight times a
**raw count** of items. A probability lives in [0,1]; a count is an unbounded integer. Added
directly, the count term dominates and the weights become decorative.

**Fix:** normalise every term to [0,1] before weighting. See
[analytics-model.md](analytics-model.md) §6, which uses saturating exponentials rather than
min–max — min–max is unstable because a single outlier rescales the entire cohort and destroys
comparability across sections and over time.

---

### 8. Risk assessment is gated so narrowly it cannot be validated

§G states the assessment will be conducted *"only if the student has been detected frequently
with a prohibited item, has multiple absences **and** has a record of misfollowing school
rules."*

Two problems. A student meeting all three conditions is already an obvious case, so the model
adds nothing — the point of prediction is to flag students *before* they are obvious. And
scoring only already-flagged students leaves no negative cases, so precision, recall, and AUC
cannot be computed and the model cannot be shown to work at all.

**Fix:** compute the score for every consenting student; **act** only above threshold. The
operational restraint is unchanged; the model becomes validatable. See
[analytics-model.md](analytics-model.md) §8.

---

## Priority 2 — citation defects

Each of these is checkable in a minute by a judge with a phone.

### 9. DepEd Order No. 44, s. 2023

§A cites it as *"Policy Guidelines on the Implementation of the School Calendar and Activities."*
The bibliography entry is *"DM 044, S. 2023 – Interim Guidelines for the Quality Assurance and
Monitoring and Evaluation of the National Educators Academy of the Philippines Core Programs."*

These are two different issuances, and one is a **Memorandum (DM)** while the body text calls it
an **Order (DO)**.

**Fix:** verify against the actual issuance you intend. The school calendar policy for
SY 2023–2024 is generally **DO 22, s. 2023** — confirm on deped.gov.ph before citing.

### 10. DepEd Order No. 36, s. 2016

§A cites it as providing *"guidelines on the monitoring and reporting of student attendance and
absenteeism."* Your own bibliography correctly identifies DO 36, s. 2016 as *"Policy Guidelines
on Awards and Recognition for the K to 12 Basic Education Program."*

The bibliography is right; the in-text claim is wrong.

**Fix:** for attendance recording and the SF2 school form, **DO 11, s. 2018** (Guidelines on the
Preparation and Checking of School Forms) is the likely intended reference. Verify before
citing.

### 11. The 20% absence rule

§A attributes the *"absences exceeding 20% of the prescribed number of class periods may receive
a failing grade"* rule to DO 44, s. 2023.

**Fix:** this rule is normally traced to **DO 8, s. 2015** (Policy Guidelines on Classroom
Assessment for the K to 12 Program). Verify and re-attribute.

### 12. DepEd issuance 006, s. 2026 — type and title disagree

| | Text used |
|---|---|
| §A | *"**Deped Order** No. 006, s. 2026, **Revised Guidelines on School Safety and Security**"* |
| Bibliography | *"**DepEd Memorandum** No. 006, s. 2026: **Guidelines on Ensuring a Safe and Motivating Learning Environment (ESMLE)**"* |

Different issuance type and different title for the same number.

**Fix:** reconcile both to the actual issuance. Also correct *"Deped"* → *"DepEd"*.

### 13. Rafiq, Afzal and Kamran — year mismatch

Cited **(2021)** in §A; the bibliography entry reads **(2022)**. The bibliography also has a
missing period after the initial (*"Kamran, F (2022)"*).

**Fix:** the linked VFAST article should settle the year. Make both match.

### 14. Non-citable source in the bibliography

> Games, J. (n.d.). *pdfcoffee.com_las-week-1-to-4-practical-research-2-pdf-free.* Scribd.

A scraped document on Scribd/pdfcoffee, with no identifiable author, date, or publisher. It is
not a citable academic source, and the entry is also **never cited in the body text**.

**Fix:** remove it. If the underlying content is needed, cite the original learning-activity
sheet or a proper methodology text.

### 15. Orphan citation

> Creative Safety Supply. (n.d.). *ANSI safety colors.*

Appears in the bibliography but is never referenced in the body.

**Fix:** either cite it where the colour scheme is justified — plausibly for status indicators
in the UI, which would be a reasonable design justification — or remove it.

---

## Priority 3 — accuracy and completeness

### 16. §E does not describe the detector

Phase I says data will be *"gathered from the located prohibited item"* but never explains how
items are located, by what instrument, with what accuracy, or who makes the determination. A
reader cannot reproduce the method as written.

**Fix:** add the instrument description, the declare-first protocol, the random sampling design,
and — most importantly — that **the detector triggers review and a human decides.** Source
material in [hardware.md](hardware.md) §2–§4 and [flow.md](flow.md) §2.

### 17. Screening is described as universal; it should be sampled

§D refers to *"surprise inspections,"* which implies sampling, but §E reads as though every
student is screened. At realistic handling times a single station cannot screen every student
within an entry window.

**Fix:** state the random selection design explicitly and report the **selection rate as a study
parameter.** This also means incident counts are sampled, not censused — every incident
statistic is a lower bound and should be reported as such.

### 18. No detector performance metrics planned

Table 2 (*Controlled Environment Feature Testing*) has only Status and Remarks columns for
four features. For a study whose novel component is a custom detector, "Working / Not working"
is thin evidence.

**Fix:** add sensitivity, specificity, and an **ROC curve** with a justified operating
threshold, from the protocol in [hardware.md](hardware.md) §8. An ROC curve is the single
strongest result available to this study and it costs only a structured afternoon of testing.

### 19. Table 2 omits the detector and the analytics

The four features listed are QR Attendance Logging, SMS Notification Alerts, Graphical Reports
(SF2), and Teacher Account and Dashboard. Missing: metal detection, incident recording, risk
scoring, and the attendance-correction workflow — all of which are stated features in §B.

**Fix:** extend Table 2 to cover every feature claimed in research question 1.

### 20. Hypotheses are not operationalised

The null and alternative hypotheses claim improvement in *"accuracy, efficiency, or
reliability"* without defining how any of the three is measured or what statistical test decides
them.

**Fix:** define each with a measure and a test, e.g. accuracy = agreement rate between system
and manual records over the same sessions; efficiency = mean seconds per record, compared with a
paired t-test; reliability = uptime and successful-notification rate. State the significance
level.

### 21. Consistency and mechanical issues

- §D and §A refer to *"automated prohibited-item detection."* The system does not automate
  *detection of prohibited items* — it automates **metal screening**, and a human determines
  whether an item is prohibited. This wording overclaims and is worth correcting in your own
  interest, since it is exactly the claim a judge will probe.
- §C opens *"The development and assessment of the **(title)** system"* — an unfilled
  placeholder.
- §C: *"being able to be one to those students who might use harmful objects"* is garbled.
- §A: *"the university of the East"*, *"the technological Institute of the Philippines"* —
  capitalisation.
- Table 3 lists parameters (Reliability, User Satisfaction, Compatibility) that do not match the
  three constructs named in research question 3 (functional suitability, performance efficiency,
  usability). Tables 4–6 also differ from each other. Align all evaluation instruments with the
  constructs you actually claim to measure, or amend research question 3 to match the tables.

---

## Summary

| Priority | Count | Character |
|---|---|---|
| P1 — before submission | 8 | Naming errors, safety and ethics gaps, methodological defects |
| P2 — citations | 7 | Mismatches between body and bibliography; one non-citable source |
| P3 — completeness | 6 | Under-specified method, missing metrics, inconsistent instruments |

Items 1, 2, 3, 4, and 14 are the ones a reviewer will notice without looking for them.

Items 5, 6, 7, and 8 are the ones that determine whether the analysis holds up — and each has a
worked correction in [analytics-model.md](analytics-model.md).

**Next step:** these can be applied to the .docx as tracked changes on request. Ordering the
work as P1 → P2 → P3 puts the highest-visibility fixes first.

# TRACKIFY — Analytics and Risk Model

The mathematics behind the statistical summaries, the attendance forecast, and the composite
risk score. This replaces the model description previously compressed into step 11 of
[flow.md](flow.md).

---

## 1. Attendance rate

The unit is the **school day**, one in/out pair per student.

> Earlier drafts of this document made AM and PM separate sessions. That was superseded when
> scanning moved to one in/out pair per day ([flow.md](flow.md) §6), and the formula below reads
> "sessions" throughout for continuity — but a session and a day are now the same thing. Halving
> the resolution was a real cost of that change and belongs in the limitations.

```
AttendanceRate_i  =  present_sessions_i / eligible_sessions_i
```

| Term | Definition |
|---|---|
| `present_sessions` | Days with status `present`, `late`, or `online` |
| `eligible_sessions` | All recorded days, **minus** class suspensions, **minus** excused absences |

Both are computed from the **live** `attendance_days` row only — superseded rows are the history
of a correction, not additional days. `trackify/core/corrections.register()` is the
implementation and the screen and the XLSX export both read it, so the number a teacher sees and
the number the analysis uses cannot drift apart.

Two deliberate choices:

- **Late counts as present.** Tardiness is a separate signal and is modelled separately in §5.
  Folding it into attendance would double-count it.
- **Excused absences leave the denominator**, they are not counted as present. A student excused
  for 3 of 40 sessions and present for the other 37 has a rate of 37/37 = 100%, not 37/40.
  Counting excused as present would inflate the rate; counting them as absent would penalise a
  student for a sanctioned absence.

Class suspensions are removed for the whole section (see [flow.md](flow.md) §4.2).

---

## 2. Linear regression — attendance trend

This is the model named in §G of the research plan. It answers **one** question: *is attendance
across the school rising or falling over the observation period?*

```
ŷ = a + b·x
```

where `x` is the school-day index (1…20) and **`y` is the daily attendance rate**, school-wide
or per-section.

### What to report

Slope `b`, intercept `a`, R², p-value for `b`, and the 95% confidence interval on `b`. The
slope is the finding: it is the change in attendance rate per school day.

### Two things to get right

**Do not regress *cumulative* attendance on day index.** If `y` is a running total, the fit is
near-deterministic — R² comes out around 0.99 and the slope is simply the average attendance
rate restated. It looks like a strong result and contains no information. Regress the **daily
rate**.

**OLS assumes independent observations, and time series are not independent.** Consecutive
school days are autocorrelated. Run a **Durbin–Watson test** (jamovi provides it) and report it.
A value near 2 indicates no autocorrelation; substantial departure means the p-value on your
slope is optimistic and you should say so. Explicitly acknowledging this is a stronger result
than ignoring it, and a judge who knows regression will look for exactly this.

With 20 points, statistical power is low. Report the confidence interval, not just the slope.

### Verification

Compute this in the system **and** independently in jamovi. Matching coefficients from two
independent implementations is a legitimate validation step — put it in your results. The
Trend sheet of the analytics export gives you the daily rates to paste in.

Both traps above are enforced in code rather than left to discipline: `trend.daily_rates()`
returns per-day rates and nothing accumulates them, and Durbin–Watson is computed on every
fit and reported whether or not anyone asked.

---

## 3. Why linear regression cannot give "probability of absence"

The earlier `flow.md` asked the linear model to output "a student's possibility of absence."
It cannot, for a structural reason: a linear function is unbounded. It will return 1.4 and
−0.2, and neither is a probability.

The trend model in §2 also operates on the **school**, not on individual students, and the
school-day index is a poor predictor of any particular student's behaviour.

For per-student risk you need a model whose output is bounded in [0, 1]. That is §4.

---

## 4. Logistic regression — per-student absence probability

```
                                    1
P(absent)_i,t  =  ───────────────────────────────────────
                   1 + exp( −( β₀ + β₁x₁ + … + βₖxₖ ) )
```

The logistic function is bounded in (0, 1) by construction, so its output is a probability.

### Features

Deliberately few, and all computable at the moment of prediction:

| Feature | Rationale |
|---|---|
| Absences in the previous 5 sessions | Recent behaviour is the strongest predictor |
| Tardies in the previous 5 sessions | Tardiness often precedes absence |
| Consecutive absences immediately prior | Captures runs, which behave differently from scattered absences |
| Day-of-week indicator (Monday / Friday) | Well-documented attendance effect |
| ~~Cumulative confirmed incidents~~ | **Dropped.** Over 20 days this will be zero for
  almost every student. A near-constant predictor carries no information, destabilises the
  fit, and invites the obvious question of why it was included. Report incidents
  descriptively instead — see [prohibited-items.md](prohibited-items.md) §9 |

### Is 20 days enough data?

Yes for a **pooled** model, no for per-student models — and the distinction matters.

Pooled across a cohort, 200 students × 20 days ≈ **4,000 observations**. The standard rule of
thumb for logistic regression is ≥ 10 **events per predictor variable**. At a 5% absence rate
that is ~200 absence events, supporting up to ~20 predictors. Five features is comfortable.

What 20 days does **not** support is a separate model per student (~20 observations each), or
any claim about seasonal or grading-period effects. Fit one pooled model with per-student
features. State both points in your limitations.

Watch for **class imbalance** — if absences are rare, a model that always predicts "present"
scores high accuracy and is useless. Report **precision, recall, and AUC**, not accuracy.

---

## 5. AHP — deriving the weights

The three risk components are not equally important, and the weights should not be invented by
the researcher. The Analytic Hierarchy Process derives them from expert judgement and — this is
the part usually skipped — **provides a test of whether that judgement was coherent.**

### Criteria

Three, not two:

1. **Absence risk** — `P(absent)` from §4
2. **Tardiness** — accumulated lateness
3. **Early departure** — accumulated departures before the cutoff

> **Prohibited item is deliberately not a fourth criterion.** It was tried as one, briefly —
> the research plan's formula literally reads `Risk = w_A·absence + w_T·tardiness +
> w_E·early_departure + w_I·prohibited_item`. Doing that honestly required a pairwise
> judgement ("prohibited item vs. absence") that nobody on the real panel had actually made;
> one was proposed and saved to `ahp_weights` attributed to the guidance counsellor and
> discipline officer regardless, which is a fabricated number wearing a real person's name.
> [prohibited-items.md](prohibited-items.md) §9 has the full three-act history and the
> arithmetic argument for why a weight could not have worked well even if the judgement had
> been real; §6 and §7 below cover what prohibited item does instead — a severity-keyed band
> floor.
>
> **This means the implemented formula deviates from the research plan's literal wording**:
> three weighted criteria plus a policy rule, not a four-term weighted sum. State that
> explicitly in the write-up.

> **Use three criteria, not two.** A 2×2 pairwise matrix is *always* perfectly consistent —
> CR = 0 by construction, no matter what numbers you enter. With only two criteria the
> consistency check is vacuous and the AHP adds nothing over just picking two weights. At
> n = 3 the check becomes real.

### Who supplies the judgements

The school's **guidance counsellor and discipline officer**, not the researcher. Record the
session: who participated, when, and the raw matrix. This is expert elicitation and it is what
makes the weights defensible rather than arbitrary. The research plan did not originally name a
source for these judgements — see [research-plan-review.md](research-plan-review.md), item 6,
now resolved by the elicitation recorded in §5.1.

### Method

Pairwise comparisons on Saaty's 1–9 scale (1 = equal, 3 = moderate, 5 = strong, 7 = very
strong, 9 = extreme; reciprocals for the inverse). Weights via the **geometric mean of each
row**, which is simpler to compute and explain than the principal eigenvector and adequate for
n = 3:

```
w_i_raw = ( ∏ⱼ a_ij )^(1/n)          w_i = w_i_raw / Σ_k w_k_raw
```

### Worked example

Panel judgements — absence moderately more important than tardiness (3); the third criterion
strongly more important than absence (so `a₁₃` = 1/5) and very strongly more important than
tardiness (`a₂₃` = 1/7):

|  | Absence | Tardiness | Early departure |
|---|---|---|---|
| **Absence** | 1 | 3 | 1/5 |
| **Tardiness** | 1/3 | 1 | 1/7 |
| **Early departure** | 5 | 7 | 1 |

> **These numbers are illustrative, not elicited.** They are what `ahp.DOCUMENTED_MATRIX`
> ships as a placeholder so risk is computable on day one, and every export marks anything
> derived from them as *not elicited*. Note in particular that they put **0.73 on early
> departure** — plausible when the third criterion was *incidents*, and questionable now:
> under them the largest reachable composite is `0.1884 + 0.0810 = 0.2694`, below the 0.30
> cutoff, so **no student could ever leave the Low band**. They remain here as the worked
> example for the arithmetic, and are superseded in practice by §5.1.

### 5.1 Adopted weights

Recorded in `ahp_weights` version 1, elicited from the **guidance counsellor and discipline
officer, Bicol Regional Science High School**, 27 August 2026. These, not the example above,
are what the exports use.

|  | Absence | Tardiness | Early departure |
|---|---|---|---|
| **Absence** | 1 | 5 | 4 |
| **Tardiness** | 1/5 | 1 | 1/2 |
| **Early departure** | 1/4 | 2 | 1 |

```
w_A = 0.6833    w_T = 0.1168    w_E = 0.1998        (Σ = 1.0000 ✓)
λmax = 3.0246    CI = 0.0123    CR = 0.0212         ✓ ≤ 0.10
```

Absence dominates, which is both what a school ranks first and what this study is about. The
consistency ratio is 0.0212 — comfortably coherent, and a stronger result than the example's
0.0559.

A four-criterion version of this matrix (adding prohibited item) was briefly saved as version 2
on 2 September 2026 — its fourth comparison was a working proposal, not a judgement the panel
had actually made, saved under their name regardless. It was retired the same day when the
fourth criterion itself was reverted to a band floor — see the box above and
[prohibited-items.md](prohibited-items.md) §9. Version 2's row stays in `ahp_weights` as a
record of what was tried and why it was undone; version 1 is active again.

**These live in the database, not in the code.** `scripts/seed_demo.py --reset` clears
`ahp_weights` along with everything else, so a reseed drops back to the placeholder. To put
them back:

```python
from trackify.core import db
from trackify.analytics import ahp
conn = db.connect()
ahp.save(conn, ((1, 5, 4), (1/5, 1, 1/2), (1/4, 2, 1)),
         elicited_from="Guidance counsellor and discipline officer, "
                       "Bicol Regional Science High School",
         elicited_at="2026-08-27")
conn.commit()
```

### 5.2 The illustrative derivation, worked through

Everything below derives the **illustrative** matrix of §5, not the adopted weights of §5.1.
It is kept because it shows the arithmetic on numbers that are easy to check by hand, and
because `tests/test_ahp.py` asserts these figures to four decimal places. **Do not report
these as the study's weights** — §5.1 has those.

Row geometric means:

```
Absence         : (1 × 3 × 0.2)^(1/3)         = 0.6^(1/3)      = 0.8434
Tardiness       : (0.3333 × 1 × 0.1429)^(1/3)  = 0.04762^(1/3) = 0.3625
Early departure : (5 × 7 × 1)^(1/3)            = 35^(1/3)      = 3.2711
                                                        Sum   = 4.4770
```

Normalised weights:

```
w_A = 0.8434 / 4.4770 = 0.1884
w_T = 0.3625 / 4.4770 = 0.0810
w_E = 3.2711 / 4.4770 = 0.7306        (Σ = 1.0000 ✓)
```

### Consistency check — required

```
λmax = (1/n) · Σᵢ (Aw)ᵢ / wᵢ
CI   = (λmax − n) / (n − 1)
CR   = CI / RI
```

Computing `Aw` for this matrix gives row ratios of 3.0649, 3.0650, 3.0648, so
**λmax = 3.0649**.

```
CI = (3.0649 − 3) / 2 = 0.0325
CR = 0.0325 / 0.58    = 0.056        ✓  ≤ 0.10, judgements are consistent
```

Random Index (RI) by matrix size:

| n | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|
| RI | 0.00 | 0.58 | 0.90 | 1.12 | 1.24 | 1.32 |

**If CR > 0.10, the weights are not usable.** Return to the panel, show them the inconsistency,
and re-elicit. Report the final CR in your results — it is evidence the weighting is sound.

---

## 6. Composite risk score

```
Risk_i  =  w_A · P(absent)_i  +  w_T · T_i  +  w_E · E_i
```

All three terms are in [0, 1) and the weights sum to 1, so `Risk_i ∈ [0, 1)`.

### Why normalisation is not optional

The original formulation multiplied a regression output by a weight and added a weight times a
**raw count** of items. Those quantities have different units and wildly different magnitudes —
a probability lives in [0, 1] while a count can be any integer. Added directly, the count term
dominates entirely and the weights become decorative. Every term must be mapped to a common
scale first.

### Tardiness term

```
T_i = 1 − exp( −μ · n_late,i )        with μ = 0.2
```

### Early departure term

```
E_i = 1 − exp( −ν · n_early,i )       with ν = 0.25
```

`n_early` counts days flagged `early_departure` — a departure before
`early_departure_cutoff`, which `attendance.py` already records on the attendance day.

| n_early | 0 | 1 | 2 | 4 | 8 | 12 |
|---|---|---|---|---|---|---|
| **E** | 0.000 | 0.221 | 0.393 | 0.632 | 0.865 | 0.950 |

### Why saturating exponentials rather than min–max

Min–max normalisation (`x / x_max`) is **unstable**: one extreme student sets `x_max` and
rescales everyone else. Add a single student with eight early departures and every other
student's score silently drops. Scores then cannot be compared across sections, across cohorts,
or across time — which destroys any longitudinal claim.

The saturating form uses **fixed** constants, so a given behaviour always maps to the same
score. It also reflects the intended meaning: the difference between zero and one early
departure matters much more than the difference between nine and ten.

`ν` and `μ` are **policy parameters**, set with the school and reported as configuration —
`mu_tardiness` and `nu_early_departure` in `config.toml`. ν = 0.25 places four early
departures at 0.63.

### Worked example

Student X, after 20 school days: `P(absent)` = 0.42 from §4; 3 tardies; 2 early departures.

```
T = 1 − exp(−0.20 × 3) = 1 − 0.5488 = 0.4512
E = 1 − exp(−0.25 × 2) = 1 − 0.6065 = 0.3935

Risk = 0.1884 × 0.42  +  0.0810 × 0.4512  +  0.7306 × 0.3935
     = 0.0791         +  0.0365           +  0.2875
     = 0.4031
```

> **This example uses the illustrative weights of §5.2, not the adopted ones.** It is the
> arithmetic that `tests/test_risk.py` pins to four decimal places, so the worked example and
> the code cannot drift apart. Under the **adopted** weights of §5.1
> (`w_A = 0.6833`, `w_T = 0.1168`, `w_E = 0.1998`) the same student scores
> `0.6833 × 0.42 + 0.1168 × 0.4512 + 0.1998 × 0.3935 = 0.4183` — also Monitor, by a
> different route: absence now carries the score instead of early departure.

→ **0.40, Monitor band.** `tests/test_risk.py` asserts these figures to four decimal
places, so this example and the code cannot drift apart silently.

### Prohibited-item incidents: a floor, not a fourth term

A confirmed incident does **not** enter the formula above. It sets a **minimum band** instead,
keyed to severity — see [prohibited-items.md](prohibited-items.md) §9 for the full argument and
§7 below for the table.

The short version is arithmetic rather than principle. Through the same saturating transform,
one incident maps to `1 − exp(−0.25) = 0.2212`, and Monitor starts at 0.30. Raising a band on a
single incident would need a weight of `0.30 / 0.2212 = 1.356`, and **the weights sum to 1** — so
a student found with a bladed weapon would still have scored *"Low"* however the panel weighted
it. A weighted fourth criterion cannot express "this one event matters on its own"; a floor can.

The composite is unchanged by an incident. Only the band moves, and `risk_scores.band_source`
records which rule decided it.

---

## 7. Risk bands

| Band | Range | Action |
|---|---|---|
| Low | 0.00 – 0.29 | No action; routine reporting only |
| Monitor | 0.30 – 0.54 | Adviser is made aware; no referral |
| Elevated | 0.55 – 0.74 | Adviser referral; parent conference per DepEd guidance |
| High | 0.75 – 1.00 | Guidance referral; documented intervention plan |

**A band boundary determines whether a real student is referred; that is an institutional
decision, not a researcher's.** `config.toml` records who set the cutoffs and when
(`[risk.bands] set_by`, `set_on`), and every export prints that attribution. Left blank — as on
a fresh install — the export says the cutoffs are placeholders instead, which is what an
unattributed cutoff is.

In force: set by the guidance counsellor and discipline officer, 27 August 2026.

### The incident floor

A confirmed prohibited-item incident sets a **minimum** band. It may raise a band, never lower
one, and it leaves the composite untouched.

| Severity | Typical category | Minimum band |
|---|---|---|
| 1 | tool with a legitimate school use | Monitor |
| 2 | other prohibited object | Monitor |
| 3 | pointed, not bladed | Elevated |
| 4 | bladed, or blunt impact | High |

`config.toml` → `[risk.incident_floor]`, and like the cutoffs above these are the school's to
set. The Risk sheet reports the count, the categories, the maximum severity, a descriptive
`Prohibited item I` score (`max_severity / 4`, context only — it does not enter the composite
or the band) and a **`Band source`** column saying which rule applied — without it a *High*
against a 0.06 composite reads as an arithmetic error.

The score **recommends review. It never imposes a sanction.** A person decides in every case —
the same principle as Rule 1 in [flow.md](flow.md).

---

## 8. Compute risk for everyone, act on it selectively

The research plan currently says risk assessment is conducted only if a student *"has been
detected frequently with a prohibited item, has multiple absences **and** has a record of
misfollowing school rules."*

That AND-gate creates two problems:

1. **It defeats early warning.** A student meeting all three conditions is already an obvious
   case. The value of a predictive model is flagging students *before* they are obvious.
2. **It makes validation impossible.** Scoring only already-flagged students leaves no negative
   cases, so you cannot compute precision, recall, or AUC — you cannot demonstrate the model
   works at all.

**Compute the score for every consenting student; act only above the threshold.** You keep the
same restraint in practice, and you gain a validatable model. Visibility remains restricted to
guidance and administrators regardless of score.

---

## 9. Statistical summaries

Per section, grade level, and school-wide:

- Attendance rate — mean, SD, distribution
- Present / late / absent / excused counts per session and per day
- Attendance trend regression (§2) with slope CI and Durbin–Watson
- Tardiness frequency distribution
- Incidents by category and severity tier
- Screening procedure: coverage, alarm rate, and **confirmation rate** — see
  [prohibited-items.md](prohibited-items.md) §1. Detector TPR/FPR and an ROC curve are
  **not available**: the detector is an off-the-shelf device, so its characteristics are
  the manufacturer's, not a finding of this study
- Risk band distribution

For the Phase III comparison against manual attendance, report **agreement rate, discrepancy
count and direction, and time-per-record for both methods** — that is the evidence for the
accuracy and efficiency claims in the hypotheses.

---

## 10. Limitations

State these in the paper. Naming them costs nothing and pre-empts the questions a judge would
otherwise ask.

1. **20 school days** is a short window. It supports a pooled model; it does not support
   per-student models, seasonal effects, or grading-period effects.
2. **Single-site study.** Weights, thresholds, and the model are fitted to one school and do
   not transfer without re-fitting.
3. **AHP weights are subjective by design.** They encode one panel's judgement. The consistency
   ratio shows the judgement was coherent — not that it was correct.
4. **Detector cannot classify.** It responds to metal, not to prohibited items. Every reported
   incident passed human verification; detection metrics measure the *instrument*, not the
   system's accuracy at identifying contraband.
5. **Incident counts are a census of what was found, not of what was carried.** Screening is
   universal — every student who scans in is screened — so there is no selection rate to
   report. But a detector responds to metal and a person decides what is prohibited, so a
   student's incident total remains a lower bound on what actually passed the gate.
6. **Autocorrelation** in the daily time series inflates the apparent significance of the trend
   slope (§2).
7. **Class imbalance** if absences are rare; accuracy is the wrong metric (§4).
8. **The risk score is not validated against outcomes.** Within 20 days you can show the model
   predicts next-day absence; you cannot show that intervening on high-risk students changed
   anything. Do not claim you can.

---

## 11. Where this lives

| Piece | File |
|---|---|
| §2 linear regression, Durbin–Watson, daily rates | `trackify/analytics/trend.py` |
| §4 pooled logistic model, §6 composite, §7 bands | `trackify/analytics/risk.py` |
| §5 AHP weights and the consistency check | `trackify/analytics/ahp.py` |
| Screening and incident counts (descriptive) | `trackify/analytics/screening.py` |
| The workbook | `trackify/export/analytics.py` |
| The button | `trackify/ui/records.py` — *Export analytics* |
| Demonstration data | `scripts/simulate_term.py` |

`mu_tardiness`, `nu_early_departure` and the band cutoffs are read from `config.toml`, never
hardcoded.

### Where the numbers in the current export came from

**The attendance in the database is simulated.** `scripts/simulate_term.py` generated it —
ten school days, 103 students, absence archetypes, Monday and Friday effects, a rainy Tuesday.
Everything the analytics report about it is therefore an output of that simulator, not an
observation:

| Sheet | What is currently in it |
|---|---|
| Trend | a slope, R², p-value, confidence interval and Durbin–Watson over invented daily rates |
| Risk | 103 composites and bands over invented attendance |
| Model | precision, recall and AUC from a model fitted to invented absences |
| Screening | coverage, alarm rate and confirmation rate over invented screenings |

**None of it may be reported as a finding.** The script prints that warning on every run and
records it in `app_settings['simulated_data']`, but the exported workbook carries no watermark,
so nothing on the face of the file distinguishes it from real data.

Clear it before the pilot:

```
python scripts/simulate_term.py --clear
```

The design is deliberately honest about the *shape* of what it generates — the model reports
AUC ≈ 0.5 when the absences really are random, and no long-run trend is baked in, so the
regression reports "no significant trend" rather than a manufactured result. That makes it a
good test of the analytics. It does not make it evidence.

### What the export does when there is no data

Every sheet is written regardless and states what is missing — *"a trend line needs at least
3 school days with attendance recorded; the database has 1"*. A missing sheet reads as a
crash and a zero reads as a finding, and on day one neither is true.

Two fallbacks worth knowing, both labelled in the output rather than silent:

- **No panel elicited yet** → the illustrative matrix from §5 is used and everything derived
  from it is marked *PLACEHOLDER — must not be reported as a finding*.
- **Too few absence events to fit** → `P(absent)` falls back to each student's observed
  absence rate, and the Risk sheet says `observed rate (model not fitted)` in its own column.
  An observed frequency describes the past; a model prediction forecasts the next day. They
  must never be confused for one another.

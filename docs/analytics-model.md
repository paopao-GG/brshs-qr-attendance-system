# TRACKIFY — Analytics and Risk Model

The mathematics behind the statistical summaries, the attendance forecast, and the composite
risk score. This replaces the model description previously compressed into step 11 of
[flow.md](flow.md).

---

## 1. Attendance rate

Sessions, not days, are the unit — the system records AM and PM separately, and counting whole
days discards half the resolution.

```
AttendanceRate_i  =  present_sessions_i / eligible_sessions_i
```

| Term | Definition |
|---|---|
| `present_sessions` | Sessions with status `present`, `late`, or `online` |
| `eligible_sessions` | All scheduled sessions, **minus** class suspensions, **minus** excused absences |

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
independent implementations is a legitimate validation step — put it in your results.

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
3. **Prohibited-item incidents** — severity-weighted confirmed incidents

> **Use three criteria, not two.** A 2×2 pairwise matrix is *always* perfectly consistent —
> CR = 0 by construction, no matter what numbers you enter. With only two criteria the
> consistency check is vacuous and the AHP adds nothing over just picking two weights. At n = 3
> the check becomes real.

### Who supplies the judgements

The school's **guidance counsellor and discipline officer**, not the researcher. Record the
session: who participated, when, and the raw matrix. This is expert elicitation and it is what
makes the weights defensible rather than arbitrary. The research plan does not currently name a
source for these judgements — see [research-plan-review.md](research-plan-review.md), item 10.

### Method

Pairwise comparisons on Saaty's 1–9 scale (1 = equal, 3 = moderate, 5 = strong, 7 = very
strong, 9 = extreme; reciprocals for the inverse). Weights via the **geometric mean of each
row**, which is simpler to compute and explain than the principal eigenvector and adequate for
n = 3:

```
w_i_raw = ( ∏ⱼ a_ij )^(1/n)          w_i = w_i_raw / Σ_k w_k_raw
```

### Worked example

Panel judgements — absence moderately more important than tardiness (3); incidents strongly
more important than absence (so `a₁₃` = 1/5); incidents very strongly more important than
tardiness (`a₂₃` = 1/7):

|  | Absence | Tardiness | Incidents |
|---|---|---|---|
| **Absence** | 1 | 3 | 1/5 |
| **Tardiness** | 1/3 | 1 | 1/7 |
| **Incidents** | 5 | 7 | 1 |

Row geometric means:

```
Absence    : (1 × 3 × 0.2)^(1/3)        = 0.6^(1/3)      = 0.8434
Tardiness  : (0.3333 × 1 × 0.1429)^(1/3) = 0.04762^(1/3) = 0.3625
Incidents  : (5 × 7 × 1)^(1/3)          = 35^(1/3)       = 3.2711
                                                   Sum   = 4.4770
```

Normalised weights:

```
w_A = 0.8434 / 4.4770 = 0.1884
w_T = 0.3625 / 4.4770 = 0.0810
w_I = 3.2711 / 4.4770 = 0.7306        (Σ = 1.0000 ✓)
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
Risk_i  =  w_A · P(absent)_i  +  w_T · T_i  +  w_I · S_i
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

### Incident severity term

```
S_i = 1 − exp( −λ · Σₖ sev_k )        with λ = 0.25,  sev_k ∈ {1, 2, 3, 4}
```

Severity is the school's existing sanction tier, so the scale is already institutionally
defined rather than invented here.

| Σ sev | 0 | 1 | 2 | 4 | 8 | 12 |
|---|---|---|---|---|---|---|
| **S** | 0.000 | 0.221 | 0.393 | 0.632 | 0.865 | 0.950 |

### Why saturating exponentials rather than min–max

Min–max normalisation (`x / x_max`) is **unstable**: one extreme student sets `x_max` and
rescales everyone else. Add a single student with eight incidents and every other student's
score silently drops. Scores then cannot be compared across sections, across cohorts, or
across time — which destroys any longitudinal claim.

The saturating form uses **fixed** constants, so a given behaviour always maps to the same
score. It also reflects the intended meaning: the difference between zero and one incident
matters much more than the difference between nine and ten.

`λ` and `μ` are **policy parameters**, set with the school and reported as configuration.
λ = 0.25 places a single severity-4 incident at 0.63.

### Worked example

Student X, after 20 school days: `P(absent)` = 0.42 from §4; 3 tardies; one confirmed
severity-3 incident.

```
T = 1 − exp(−0.2 × 3)  = 1 − 0.5488 = 0.4512
S = 1 − exp(−0.25 × 3) = 1 − 0.4724 = 0.5276

Risk = 0.1884 × 0.42  +  0.0810 × 0.4512  +  0.7306 × 0.5276
     = 0.0791         +  0.0365           +  0.3855
     = 0.501
```

→ **0.50, Monitor band.**

---

## 7. Risk bands

| Band | Range | Action |
|---|---|---|
| Low | 0.00 – 0.29 | No action; routine reporting only |
| Monitor | 0.30 – 0.54 | Adviser is made aware; no referral |
| Elevated | 0.55 – 0.74 | Adviser referral; parent conference per DepEd guidance |
| High | 0.75 – 1.00 | Guidance referral; documented intervention plan |

**These cutoffs are placeholders and must be set by the school.** A band boundary determines
whether a real student is referred; that is an institutional decision, not a researcher's.
Record who set them and when.

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
5. **Random screening means incident counts are sampled**, not censused. A student's incident
   total is a lower bound. Report the selection rate alongside any incident statistic.
6. **Autocorrelation** in the daily time series inflates the apparent significance of the trend
   slope (§2).
7. **Class imbalance** if absences are rare; accuracy is the wrong metric (§4).
8. **The risk score is not validated against outcomes.** Within 20 days you can show the model
   predicts next-day absence; you cannot show that intervening on high-risk students changed
   anything. Do not claim you can.

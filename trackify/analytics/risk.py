"""Per-student absence probability and the composite risk score.

docs/analytics-model.md sections 4, 6 and 7.

    P(absent) from a POOLED logistic model      bounded in (0, 1) by construction
    T = 1 - exp(-mu * n_late)                   mu from config.toml
    E = 1 - exp(-nu * n_early_departure)        nu from config.toml
    Risk = w_A*P + w_T*T + w_E*E                weights from ahp.active()

Three things worth knowing before changing anything here.

**Why logistic and not the linear trend.** A linear function is unbounded; asked for a
probability it returns 1.4 and -0.2. The trend model in trend.py also describes the
SCHOOL, not a student, so it cannot answer "will this child be absent" at all. Section 3
of the doc exists solely to say this.

**Why pooled and not per-student.** Twenty school days gives roughly twenty observations
per student, nowhere near enough to fit five coefficients. Pooled across the cohort it is
thousands of rows with per-student features, which is what makes the model estimable.

**Why saturating exponentials and not min-max.** Min-max is unstable: one student with
eight early departures sets the maximum and silently rescales everyone else, so scores
cannot be compared across sections or over time. Fixed constants mean a given behaviour
always maps to the same number.

The score RECOMMENDS REVIEW. It never imposes a sanction -- a person decides in every
case, the same principle as Rule 1 in flow.md.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field

from ..core.db import utcnow
from . import ahp

# Section 4's features, with "cumulative confirmed incidents" DROPPED per
# prohibited-items.md section 9: over a 20-day study it is zero for nearly every student,
# and a near-constant predictor carries no information while destabilising the fit.
FEATURES = ("absent_prev_5", "late_prev_5", "consecutive_absences",
            "is_monday", "is_friday")

WINDOW = 5

# Below this the logistic fit is not trustworthy and p_absent falls back to the student's
# observed absence rate. Ten events per predictor is the standard rule of thumb; five
# features means fifty absences before the model is on solid ground.
MIN_EVENTS = len(FEATURES) * 10
MIN_ROWS = 50

MODEL_FITTED = "logistic model"
MODEL_OBSERVED = "observed rate (model not fitted)"


@dataclass(frozen=True)
class StudentRisk:
    student_id: int
    lrn: str
    name: str
    section: str
    p_absent: float
    p_absent_source: str
    tardiness: float
    early_departure: float
    composite: float
    band: str
    n_late: int
    n_early: int
    n_absent: int
    n_days: int


@dataclass(frozen=True)
class ModelQuality:
    """Precision, recall and AUC -- deliberately not accuracy.

    Section 4: if absences are rare, a model that always predicts "present" scores high
    accuracy and is useless. These three are the ones that expose that.
    """

    n_rows: int
    n_events: int
    precision: float
    recall: float
    roc_auc: float
    true_pos: int
    false_pos: int
    true_neg: int
    false_neg: int
    coefficients: dict[str, float] = field(default_factory=dict)
    intercept: float = 0.0
    validation: str = ""

    @property
    def event_rate(self) -> float:
        return self.n_events / self.n_rows if self.n_rows else 0.0


@dataclass(frozen=True)
class RiskReport:
    rows: list[StudentRisk] = field(default_factory=list)
    weights: ahp.Weights | None = None
    model: ModelQuality | None = None
    # Why the model was not fitted, when it was not.
    model_note: str = ""
    computed_at: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.rows)

    def by_band(self) -> dict[str, int]:
        counts = {band: 0 for band in ("Low", "Monitor", "Elevated", "High")}
        for row in self.rows:
            counts[row.band] = counts.get(row.band, 0) + 1
        return counts


def band_for(score: float, config) -> str:
    """Map a composite onto the school's bands.

    Cutoffs come from config.toml, never hardcoded: a band boundary decides whether a
    real child is referred to guidance, which is an institutional decision and not a
    researcher's. analytics-model.md section 7 says so explicitly.
    """
    risk = config.risk
    if score < risk.band_low:
        return "Low"
    if score < risk.band_monitor:
        return "Monitor"
    if score < risk.band_elevated:
        return "Elevated"
    return "High"


def saturating(count: int, rate: float) -> float:
    """1 - exp(-rate * count), the shape both the T and E terms use.

    Bounded in [0, 1), and the difference between zero and one occurrence matters much
    more than between nine and ten -- which is the intended meaning, not a side effect.
    """
    return 1.0 - math.exp(-rate * max(count, 0))


def _history(conn: sqlite3.Connection, *, section_id: int | None = None,
             end: str | None = None) -> dict[int, list[sqlite3.Row]]:
    """Every live attendance day per student, oldest first."""
    sql = ["""SELECT a.student_id, a.date, a.status, a.flags
              FROM attendance_days a
              JOIN students s ON s.id = a.student_id
              WHERE a.superseded_by IS NULL AND s.active = 1"""]
    params: list = []
    if section_id is not None:
        sql.append("AND s.section_id = ?")
        params.append(section_id)
    if end:
        sql.append("AND a.date <= ?")
        params.append(end)
    sql.append("ORDER BY a.student_id, a.date")

    history: dict[int, list[sqlite3.Row]] = {}
    for row in conn.execute(" ".join(sql), params):
        history.setdefault(row["student_id"], []).append(row)
    return history


def _rows_for_fit(history: dict[int, list[sqlite3.Row]]):
    """Build the pooled training set: one row per student per school day.

    Every feature is computed from days STRICTLY BEFORE the day being predicted. Letting
    the target day into its own features is leakage, and it produces a model that scores
    beautifully and predicts nothing.
    """
    from datetime import date as Date

    features, targets = [], []
    for days in history.values():
        for index, day in enumerate(days):
            if day["status"] in ("excused", "online"):
                continue                      # not an attendance opportunity
            prior = days[:index]
            if not prior:
                continue                      # nothing to predict from
            window = prior[-WINDOW:]

            consecutive = 0
            for earlier in reversed(prior):
                if earlier["status"] == "absent":
                    consecutive += 1
                else:
                    break

            weekday = Date.fromisoformat(day["date"]).weekday()
            features.append([
                sum(1 for d in window if d["status"] == "absent"),
                sum(1 for d in window if d["status"] == "late"),
                consecutive,
                1 if weekday == 0 else 0,
                1 if weekday == 4 else 0,
            ])
            targets.append(1 if day["status"] == "absent" else 0)

    return features, targets


def _fit(features, targets) -> tuple[object, ModelQuality] | tuple[None, str]:
    """Fit the pooled logistic model, or explain why it could not be."""
    n_rows = len(targets)
    n_events = sum(targets)

    if n_rows < MIN_ROWS:
        return None, (f"Only {n_rows} student-days available; a pooled logistic model "
                      f"needs at least {MIN_ROWS}.")
    if n_events == 0:
        return None, ("No absences recorded, so there is nothing for a model of absence "
                      "to learn from.")
    if n_events == n_rows:
        return None, "Every recorded day is an absence; there are no negative cases."
    if n_events < MIN_EVENTS:
        return None, (f"Only {n_events} absence events. The rule of thumb is 10 events "
                      f"per predictor, so {len(FEATURES)} features need "
                      f"{MIN_EVENTS}.")

    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import confusion_matrix, precision_score, recall_score, roc_auc_score
    from sklearn.model_selection import StratifiedKFold, cross_val_predict

    x = np.asarray(features, dtype=float)
    y = np.asarray(targets, dtype=int)
    model = LogisticRegression(max_iter=1000, class_weight="balanced")

    # Cross-validated predictions for the metrics: in-sample precision and recall are
    # optimistic, and this model's whole claim is that it generalises to a day it has
    # not seen.
    folds = min(5, int(y.sum()), int((y == 0).sum()))
    if folds >= 2:
        predicted = cross_val_predict(model, x, y, cv=StratifiedKFold(folds))
        scores = cross_val_predict(model, x, y, cv=StratifiedKFold(folds),
                                   method="predict_proba")[:, 1]
        validation = f"{folds}-fold cross-validation"
    else:
        model.fit(x, y)
        predicted = model.predict(x)
        scores = model.predict_proba(x)[:, 1]
        validation = "in-sample (too few events to cross-validate; optimistic)"

    model.fit(x, y)
    tn, fp, fn, tp = confusion_matrix(y, predicted, labels=[0, 1]).ravel()

    quality = ModelQuality(
        n_rows=n_rows, n_events=int(n_events),
        precision=float(precision_score(y, predicted, zero_division=0)),
        recall=float(recall_score(y, predicted, zero_division=0)),
        roc_auc=float(roc_auc_score(y, scores)),
        true_pos=int(tp), false_pos=int(fp), true_neg=int(tn), false_neg=int(fn),
        coefficients={name: float(value)
                      for name, value in zip(FEATURES, model.coef_[0])},
        intercept=float(model.intercept_[0]),
        validation=validation,
    )
    return model, quality


def _current_features(days: list[sqlite3.Row]) -> list[float]:
    """Features as they stand today, for predicting the next school day."""
    from datetime import date as Date, timedelta

    window = days[-WINDOW:]
    consecutive = 0
    for earlier in reversed(days):
        if earlier["status"] == "absent":
            consecutive += 1
        else:
            break

    following = Date.fromisoformat(days[-1]["date"]) + timedelta(days=1)
    return [
        sum(1 for d in window if d["status"] == "absent"),
        sum(1 for d in window if d["status"] == "late"),
        consecutive,
        1 if following.weekday() == 0 else 0,
        1 if following.weekday() == 4 else 0,
    ]


def compute(conn: sqlite3.Connection, config, *, section_id: int | None = None,
            end: str | None = None, persist: bool = False) -> RiskReport:
    """Score every active student. Section 8: compute for everyone, act selectively."""
    history = _history(conn, section_id=section_id, end=end)
    weights = ahp.active(conn)
    computed_at = utcnow()

    if not history:
        return RiskReport(weights=weights, computed_at=computed_at,
                          model_note="No attendance has been recorded yet.")

    features, targets = _rows_for_fit(history)
    model, quality_or_note = _fit(features, targets)
    quality = quality_or_note if model is not None else None
    note = "" if model is not None else quality_or_note

    students = {
        row["id"]: row for row in conn.execute(
            """SELECT s.id, s.lrn, s.first_name, s.last_name,
                      sec.name AS section_name, sec.grade_level
               FROM students s JOIN sections sec ON sec.id = s.section_id""")
    }

    rows: list[StudentRisk] = []
    for student_id, days in history.items():
        student = students.get(student_id)
        if student is None:
            continue

        counted = [d for d in days if d["status"] in ("present", "late", "absent")]
        n_absent = sum(1 for d in counted if d["status"] == "absent")
        n_late = sum(1 for d in counted if d["status"] == "late")
        n_early = sum(1 for d in days if "early_departure" in (d["flags"] or ""))

        if model is not None:
            import numpy as np
            p_absent = float(model.predict_proba(
                np.asarray([_current_features(days)], dtype=float))[0][1])
            source = MODEL_FITTED
        else:
            # Not a silent substitution: the source travels with the number into the
            # export, so nobody reads an observed frequency as a model prediction.
            p_absent = n_absent / len(counted) if counted else 0.0
            source = MODEL_OBSERVED

        tardiness = saturating(n_late, config.risk.mu_tardiness)
        early = saturating(n_early, config.risk.nu_early_departure)
        composite = (weights.absence * p_absent
                     + weights.tardiness * tardiness
                     + weights.early_departure * early)

        rows.append(StudentRisk(
            student_id=student_id, lrn=student["lrn"],
            name=f"{student['last_name']}, {student['first_name']}",
            section=f"{student['grade_level']}-{student['section_name']}",
            p_absent=p_absent, p_absent_source=source,
            tardiness=tardiness, early_departure=early,
            composite=composite, band=band_for(composite, config),
            n_late=n_late, n_early=n_early, n_absent=n_absent, n_days=len(counted),
        ))

    rows.sort(key=lambda r: r.composite, reverse=True)

    if persist:
        for row in rows:
            conn.execute(
                """INSERT INTO risk_scores
                   (student_id, computed_at, p_absent, tardiness_score,
                    early_departure_score, composite, band, weights_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (row.student_id, computed_at, row.p_absent, row.tardiness,
                 row.early_departure, row.composite, row.band, weights.version),
            )

    return RiskReport(rows=rows, weights=weights, model=quality,
                      model_note=note, computed_at=computed_at)

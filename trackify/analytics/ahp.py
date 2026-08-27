"""AHP weights for the composite risk score.

docs/analytics-model.md section 5. Three criteria -- absence risk, tardiness, early
departure -- weighted from pairwise expert judgement rather than numbers a researcher
picked, and checked for whether that judgement was coherent.

The consistency ratio is the part usually skipped and the reason this is worth doing at
all. It is also why there are three criteria and not two: a 2x2 pairwise matrix is
PERFECTLY consistent by construction, CR = 0 whatever numbers go in, so the check is
vacuous and the AHP adds nothing over picking two weights by hand.

Weights come from the geometric mean of each row rather than the principal eigenvector.
For n = 3 the two agree closely, and this one can be explained to a panel and checked on
paper -- which matters more here than the last decimal place.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass

from ..core.db import utcnow

# The criteria, in matrix order, and the keys of the weights_json blob persisted in
# ahp_weights. They are NOT the risk_scores column names -- those are p_absent,
# tardiness_score and early_departure_score; a comment here claimed otherwise for a
# long time and it was never true.
#
# This is now load-bearing rather than documentation: Weights is built by zipping it
# against the derived vector, and as_dict reads it back. It used to be neither -- the
# row order was hand-mapped to weights[0], weights[1], weights[2] and restated a third
# time in as_dict, so the constant asserted a guarantee that nothing enforced. Swapping
# two rows of the matrix would have silently applied the tardiness weight to absence.
# test_ahp.py pins it against the dataclass field order.
CRITERIA = ("absence", "tardiness", "early_departure")

# Saaty's Random Index. CR is CI/RI, so this is what makes the check calibrated to the
# size of the matrix rather than an absolute threshold.
RANDOM_INDEX = {1: 0.0, 2: 0.0, 3: 0.58, 4: 0.90, 5: 1.12, 6: 1.24, 7: 1.32,
                8: 1.41, 9: 1.45, 10: 1.49}

MAX_CR = 0.10

# The illustrative matrix from analytics-model.md section 5, used ONLY as a placeholder
# until a real panel is elicited. See active() -- anything derived from it is marked
# unelicited, because these are not anybody's judgements.
DOCUMENTED_MATRIX = (
    (1.0,     3.0,     1.0 / 5),
    (1.0 / 3, 1.0,     1.0 / 7),
    (5.0,     7.0,     1.0),
)


class AHPError(ValueError):
    pass


@dataclass(frozen=True)
class Weights:
    """Derived weights plus the evidence that they are usable."""

    absence: float
    tardiness: float
    early_departure: float
    lambda_max: float
    ci: float
    cr: float
    n: int
    version: int | None = None
    elicited_from: str = ""
    elicited_at: str = ""
    # False when these came from DOCUMENTED_MATRIX because no panel has been recorded.
    elicited: bool = False

    @property
    def consistent(self) -> bool:
        return self.cr <= MAX_CR

    def as_dict(self) -> dict[str, float]:
        return {name: getattr(self, name) for name in CRITERIA}

    @property
    def caveat(self) -> str:
        """What an export has to say about these weights, if anything."""
        if not self.elicited:
            return ("PLACEHOLDER WEIGHTS - no panel has been elicited. These come from "
                    "the illustrative matrix in analytics-model.md section 5 and must "
                    "not be reported as a finding. Run an elicitation session with the "
                    "guidance counsellor and discipline officer, then save the real "
                    "matrix.")
        if not self.consistent:
            return (f"INCONSISTENT - CR = {self.cr:.3f} exceeds {MAX_CR}. These "
                    "judgements are not usable; re-elicit.")
        return ""


def _validate(matrix) -> list[list[float]]:
    rows = [list(map(float, row)) for row in matrix]
    n = len(rows)
    if n < 3:
        # Not a size quibble. At n = 2 the consistency ratio is 0 no matter what is
        # entered, so the check that justifies using AHP at all cannot fail.
        raise AHPError(
            "AHP needs at least 3 criteria. A 2x2 pairwise matrix is perfectly "
            "consistent by construction, so its consistency ratio proves nothing."
        )
    if any(len(row) != n for row in rows):
        raise AHPError("The pairwise matrix must be square.")
    for i in range(n):
        if not math.isclose(rows[i][i], 1.0, abs_tol=1e-9):
            raise AHPError("A criterion compared with itself must be 1.")
        for j in range(n):
            if rows[i][j] <= 0:
                raise AHPError("Pairwise judgements must be positive.")
            if not math.isclose(rows[i][j], 1.0 / rows[j][i], rel_tol=1e-6):
                raise AHPError(
                    f"The matrix is not reciprocal at ({i + 1}, {j + 1}): "
                    f"{rows[i][j]:.4g} and {rows[j][i]:.4g} are not inverses."
                )
    return rows


def derive(matrix=DOCUMENTED_MATRIX, *, elicited: bool = False,
           version: int | None = None, elicited_from: str = "",
           elicited_at: str = "") -> Weights:
    """Weights, lambda_max, CI and CR from a pairwise comparison matrix."""
    rows = _validate(matrix)
    n = len(rows)

    # Row geometric means, normalised. analytics-model.md section 5.
    raw = [math.prod(row) ** (1.0 / n) for row in rows]
    total = sum(raw)
    weights = [value / total for value in raw]

    # lambda_max = mean over i of (Aw)_i / w_i.
    ratios = []
    for i in range(n):
        weighted = sum(rows[i][j] * weights[j] for j in range(n))
        ratios.append(weighted / weights[i])
    lambda_max = sum(ratios) / n

    ci = (lambda_max - n) / (n - 1)
    ri = RANDOM_INDEX.get(n)
    if not ri:
        raise AHPError(f"No Random Index defined for n = {n}.")
    cr = ci / ri

    return Weights(
        **dict(zip(CRITERIA, weights, strict=True)),
        lambda_max=lambda_max, ci=ci, cr=cr, n=n, version=version,
        elicited_from=elicited_from, elicited_at=elicited_at, elicited=elicited,
    )


def save(conn: sqlite3.Connection, matrix, *, elicited_from: str,
         elicited_at: str | None = None, activate: bool = True) -> Weights:
    """Record a panel's judgements. Refuses an inconsistent matrix.

    CR > 0.10 means the panel contradicted itself -- A over B, B over C, C over A. The
    doc calls such weights unusable, and storing them behind a warning is how they end
    up in the paper anyway, so this refuses instead.
    """
    if not elicited_from.strip():
        raise AHPError(
            "Record who supplied these judgements. Unattributed weights are the "
            "researcher's own numbers with extra steps."
        )

    weights = derive(matrix, elicited=True)
    if not weights.consistent:
        raise AHPError(
            f"Consistency ratio {weights.cr:.3f} exceeds {MAX_CR}. These judgements "
            "contradict each other and are not usable. Show the panel the "
            "inconsistency and re-elicit."
        )

    elicited_at = elicited_at or utcnow()
    version = (conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM ahp_weights").fetchone()[0])

    if activate:
        conn.execute("UPDATE ahp_weights SET active = 0")
    conn.execute(
        """INSERT INTO ahp_weights
           (version, matrix_json, weights_json, lambda_max, ci, cr,
            elicited_from, elicited_at, active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (version, json.dumps([list(map(float, r)) for r in matrix]),
         json.dumps(weights.as_dict()), weights.lambda_max, weights.ci, weights.cr,
         elicited_from.strip(), elicited_at, 1 if activate else 0),
    )
    return derive(matrix, elicited=True, version=version,
                  elicited_from=elicited_from.strip(), elicited_at=elicited_at)


def active(conn: sqlite3.Connection) -> Weights:
    """The weights in force, or the documented placeholder when none are recorded.

    Never raises and never returns None: risk has to be computable on day one, before a
    panel has been convened. The fallback is marked `elicited = False` and carries a
    caveat, so an export can say so rather than quietly presenting an example as a
    finding.
    """
    row = conn.execute(
        "SELECT * FROM ahp_weights WHERE active = 1 ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return derive(DOCUMENTED_MATRIX, elicited=False)

    return derive(json.loads(row["matrix_json"]), elicited=True,
                  version=row["version"], elicited_from=row["elicited_from"],
                  elicited_at=row["elicited_at"])


def matrix_of(conn: sqlite3.Connection, weights: Weights) -> list[list[float]]:
    """The pairwise matrix behind a set of weights, for the export sheet."""
    if weights.version is None:
        return [list(row) for row in DOCUMENTED_MATRIX]
    row = conn.execute("SELECT matrix_json FROM ahp_weights WHERE version = ?",
                       (weights.version,)).fetchone()
    return json.loads(row["matrix_json"]) if row else [list(r) for r in DOCUMENTED_MATRIX]

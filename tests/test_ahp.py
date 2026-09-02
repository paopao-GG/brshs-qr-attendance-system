"""AHP weights and the consistency check.

docs/analytics-model.md section 5 works the whole thing through by hand, so the doc is the
oracle here: if these numbers drift, either the code is wrong or the doc is, and one of
them has to be fixed rather than the test relaxed.
"""
import math

import pytest

from trackify.analytics import ahp
from trackify.analytics.ahp import AHPError


def test_the_documented_example_reproduces_exactly():
    """analytics-model.md section 5: 0.1884 / 0.0810 / 0.7306, lambda 3.0649, CR 0.056."""
    w = ahp.derive(ahp.DOCUMENTED_MATRIX)

    assert round(w.absence, 4) == 0.1884
    assert round(w.tardiness, 4) == 0.0810
    assert round(w.early_departure, 4) == 0.7306
    assert round(w.lambda_max, 4) == 3.0649
    assert round(w.ci, 4) == 0.0324
    assert round(w.cr, 3) == 0.056


def test_the_weights_sum_to_one():
    w = ahp.derive()
    assert math.isclose(w.absence + w.tardiness + w.early_departure, 1.0, abs_tol=1e-12)


def test_equal_judgements_give_equal_weights():
    w = ahp.derive(((1, 1, 1), (1, 1, 1), (1, 1, 1)))

    assert math.isclose(w.absence, 1 / 3, abs_tol=1e-12)
    assert math.isclose(w.cr, 0.0, abs_tol=1e-12)


# --- the consistency check --------------------------------------------------

def test_an_inconsistent_matrix_is_refused_by_save(conn):
    """A over B, B over C, and C over A. The doc calls such weights unusable; storing
    them behind a warning is how they reach the paper anyway."""
    circular = ((1, 9, 1 / 9), (1 / 9, 1, 9), (9, 1 / 9, 1))
    assert ahp.derive(circular).cr > ahp.MAX_CR

    with pytest.raises(AHPError, match="Consistency ratio"):
        ahp.save(conn, circular, elicited_from="panel")


def test_a_two_by_two_matrix_is_rejected_as_vacuous():
    """Section 5's reason for three criteria: at n = 2, CR is 0 whatever is entered, so
    the check that justifies using AHP cannot fail."""
    with pytest.raises(AHPError, match="perfectly consistent by construction"):
        ahp.derive(((1, 3), (1 / 3, 1)))


def test_a_non_reciprocal_matrix_is_refused():
    """If A is 3x B, then B must be 1/3 of A. Anything else is a typo, and it silently
    skews every weight."""
    with pytest.raises(AHPError, match="not reciprocal"):
        ahp.derive(((1, 3, 0.2), (0.5, 1, 1 / 7), (5, 7, 1)))


def test_a_diagonal_that_is_not_one_is_refused():
    with pytest.raises(AHPError, match="compared with itself"):
        ahp.derive(((2, 3, 0.2), (1 / 3, 1, 1 / 7), (5, 7, 1)))


# --- storage ----------------------------------------------------------------

def test_saving_records_who_supplied_the_judgements(conn):
    w = ahp.save(conn, ahp.DOCUMENTED_MATRIX,
                 elicited_from="Guidance counsellor and discipline officer")

    assert w.elicited is True
    assert w.version == 1
    row = conn.execute("SELECT * FROM ahp_weights").fetchone()
    assert row["elicited_from"].startswith("Guidance")
    assert round(row["cr"], 3) == 0.056


def test_unattributed_weights_are_refused(conn):
    """Weights with no named source are the researcher's own numbers with extra steps."""
    with pytest.raises(AHPError, match="who supplied"):
        ahp.save(conn, ahp.DOCUMENTED_MATRIX, elicited_from="   ")


def test_saving_again_supersedes_the_previous_version(conn):
    ahp.save(conn, ahp.DOCUMENTED_MATRIX, elicited_from="first panel")
    second = ahp.save(conn, ((1, 2, 1 / 3), (0.5, 1, 1 / 4), (3, 4, 1)),
                      elicited_from="second panel")

    assert second.version == 2
    assert ahp.active(conn).version == 2
    active_rows = conn.execute(
        "SELECT COUNT(*) FROM ahp_weights WHERE active = 1").fetchone()[0]
    assert active_rows == 1, "exactly one set of weights can be in force"


# --- the placeholder --------------------------------------------------------

def test_with_no_panel_recorded_the_weights_are_marked_unelicited(conn):
    """Risk has to be computable on day one, but an illustrative matrix must never be
    mistaken for somebody's judgement."""
    w = ahp.active(conn)

    assert w.elicited is False
    assert w.version is None
    assert "PLACEHOLDER" in w.caveat
    assert "must not be reported as a finding" in w.caveat


def test_elicited_weights_carry_no_caveat(conn):
    ahp.save(conn, ahp.DOCUMENTED_MATRIX, elicited_from="the panel")
    assert ahp.active(conn).caveat == ""


def test_a_matrix_sized_for_a_different_criteria_set_falls_back_rather_than_raising(conn):
    """A 4x4 elicited while prohibited_item was briefly a fourth criterion cannot be
    zipped against 3 fields -- active() has to fall back to the placeholder rather
    than crash on a database that still has that row."""
    import json

    stale = ((1, 5, 4, 1 / 5), (1 / 5, 1, 1 / 2, 1 / 9),
             (1 / 4, 2, 1, 1 / 9), (5, 9, 9, 1))
    conn.execute(
        """INSERT INTO ahp_weights
           (version, matrix_json, weights_json, lambda_max, ci, cr,
            elicited_from, elicited_at, active)
           VALUES (1, ?, '{}', 0, 0, 0, 'old panel', '2026-01-01', 1)""",
        (json.dumps(stale),),
    )

    w = ahp.active(conn)

    assert w.elicited is False
    assert w.stale_criteria is True
    assert "different number of criteria" in w.caveat


def test_the_active_weights_survive_a_round_trip(conn):
    saved = ahp.save(conn, ahp.DOCUMENTED_MATRIX, elicited_from="the panel")
    loaded = ahp.active(conn)

    assert round(loaded.absence, 10) == round(saved.absence, 10)
    assert round(loaded.early_departure, 10) == round(saved.early_departure, 10)


def test_the_matrix_can_be_recovered_for_the_export(conn):
    ahp.save(conn, ahp.DOCUMENTED_MATRIX, elicited_from="the panel")
    matrix = ahp.matrix_of(conn, ahp.active(conn))

    assert round(matrix[0][1], 4) == 3.0
    assert round(matrix[2][0], 4) == 5.0


# --- the criteria order is a contract, not a convention ---------------------

def test_criteria_match_the_weights_fields_in_order():
    """Weights is built by zipping CRITERIA against the derived vector, so a mismatch
    here applies the tardiness weight to absence -- silently, and the CR check would
    still pass because the matrix is unchanged."""
    import dataclasses
    fields = [f.name for f in dataclasses.fields(ahp.Weights)][:len(ahp.CRITERIA)]
    assert tuple(fields) == ahp.CRITERIA


def test_criteria_are_the_persisted_weight_keys(conn):
    """weights_json round-trips through these names, so renaming one silently orphans
    a saved weight set rather than failing loudly."""
    import json
    saved = ahp.save(conn, ahp.DOCUMENTED_MATRIX, elicited_from="panel")
    stored = json.loads(conn.execute(
        "SELECT weights_json FROM ahp_weights WHERE version = ?",
        (saved.version,)).fetchone()[0])
    assert set(stored) == set(ahp.CRITERIA)


def test_as_dict_is_keyed_by_criteria():
    derived = ahp.derive(ahp.DOCUMENTED_MATRIX)
    assert list(derived.as_dict()) == list(ahp.CRITERIA)

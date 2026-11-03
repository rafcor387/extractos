from io import BytesIO

from openpyxl import load_workbook
import pandas as pd
import pytest

from movements import (apply_edits, consolidate, empty_movements, from_records,
                       to_csv_bytes, to_excel_bytes, totals)


@pytest.fixture
def frame():
    return from_records([
        {"fecha": "2026-05-19 21:50:35", "glosa": "Abono", "tipo": "INGRESO", "monto": 55.0, "origen": "YASTA"},
        {"fecha": "2026-05-22 09:29:13", "glosa": "Pago", "tipo": "EGRESO", "monto": 15.0, "origen": "YASTA"},
        {"fecha": "2026-05-29", "glosa": "Depósito", "tipo": "INGRESO", "monto": 75.0, "origen": "BNB"},
    ])


def test_consolidation_deduplicates_exact_rows_only(frame):
    assert len(consolidate(empty_movements(), frame)) == 3
    assert len(consolidate(frame, frame)) == 3
    another = frame.iloc[[0]].copy()
    another.loc[0, "fecha"] = "2026-05-19 21:50:36"
    assert len(consolidate(frame, another)) == 4


def test_filtered_edits_preserve_unseen_rows_and_update_totals(frame):
    edited = apply_edits(frame, [1], {0: {"glosa": "Pago corregido", "monto": 25.0, "tipo": "INGRESO"}})
    assert edited.iloc[1]["glosa"] == "Pago corregido"
    pd.testing.assert_frame_equal(frame.iloc[[0, 2]], edited.iloc[[0, 2]])
    assert totals(edited) == (155.0, 0.0, 155.0)
    assert frame.iloc[1]["monto"] == 15.0


@pytest.mark.parametrize("changes", [{"monto": -5}, {"monto": None}, {"monto": float("inf")},
                                      {"monto": 0}, {"tipo": "OTRO"}, {"glosa": " "}, {"origen": "BNB"}])
def test_invalid_edits_are_atomic(frame, changes):
    before = frame.copy()
    with pytest.raises(ValueError):
        apply_edits(frame, [1], {0: changes})
    pd.testing.assert_frame_equal(frame, before)


def test_csv_and_excel_roundtrip(frame):
    csv = pd.read_csv(BytesIO(to_csv_bytes(frame)))
    xlsx = pd.read_excel(BytesIO(to_excel_bytes(frame)), dtype={"monto": float})
    pd.testing.assert_frame_equal(csv, frame)
    pd.testing.assert_frame_equal(xlsx, frame)


def test_formula_like_glosa_exported_as_literal(frame):
    frame.loc[0, "glosa"] = "=1+1"
    output = to_excel_bytes(frame)
    workbook = load_workbook(BytesIO(output))
    assert workbook.active["B2"].data_type != "f"
    assert workbook.active["D2"].data_type == "n"
    assert "'=1+1" in to_csv_bytes(frame).decode("utf-8-sig")
    assert frame.loc[0, "glosa"] == "=1+1"

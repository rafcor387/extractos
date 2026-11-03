import json
from io import BytesIO
from pathlib import Path

from streamlit.testing.v1 import AppTest

from movements import from_records, totals
from tests.pdf_factory import make_pdf

APP = str(Path(__file__).parents[1] / "app.py")


def seeded_app():
    at = AppTest.from_file(APP)
    at.session_state["df_movimientos"] = from_records([
        {"fecha": "2026-05-19 21:50:35", "glosa": "Abono", "tipo": "INGRESO", "monto": 55.0, "origen": "YASTA"},
        {"fecha": "2026-05-22 09:29:13", "glosa": "Pago", "tipo": "EGRESO", "monto": 15.0, "origen": "YASTA"},
    ])
    return at.run()


def test_empty_app_requires_bank_and_files():
    at = AppTest.from_file(APP).run()
    assert not at.exception
    assert at.radio[0].value is None
    assert at.button[0].disabled
    assert at.get("file_uploader")[0].proto.disabled
    assert len(at.session_state["df_movimientos"]) == 0


def test_bank_selection_enables_separate_uploader_and_preserves_consolidation():
    at = seeded_app()
    before = at.session_state["df_movimientos"].copy()
    assert at.get("file_uploader")[0].proto.disabled
    at.radio(key="upload_origin").set_value("BNB").run()
    assert not at.exception
    first = at.get("file_uploader")[0].proto
    assert not first.disabled
    assert "BNB" in first.label
    at.radio(key="upload_origin").set_value("YASTA").run()
    assert not at.exception
    second = at.get("file_uploader")[0].proto
    assert not second.disabled
    assert "YASTA" in second.label
    assert first.id != second.id
    assert at.session_state["df_movimientos"].equals(before)


def test_filters_metrics_clear():
    at = seeded_app()
    assert not at.exception
    assert [metric.value for metric in at.metric] == ["Bs 55.00", "Bs 15.00", "Bs 40.00"]
    at.selectbox[1].select("EGRESO").run()
    assert not at.exception
    assert len(at.dataframe[0].value) == 1
    assert len(at.session_state["df_movimientos"]) == 2
    at.button[1].click().run()
    assert not at.exception
    assert at.session_state["df_movimientos"].empty


def test_editor_callback_and_invalid_edit_rollback():
    at = seeded_app()
    at.selectbox[1].select("EGRESO").run()
    # AppTest has no cell-edit API: inject the same protobuf delta as the browser.
    # Keep all other widget states, including the selected filter.
    states = at._tree.get_widget_states()
    editor = states.widgets.add()
    editor.id = at.dataframe[0].proto.id
    editor.string_value = json.dumps({"edited_rows": {"0": {"monto": 25.0}}, "added_rows": [], "deleted_rows": []})
    at._run(states)
    assert not at.exception
    assert at.session_state["df_movimientos"].iloc[1]["monto"] == 25.0
    assert [metric.value for metric in at.metric] == ["Bs 55.00", "Bs 25.00", "Bs 30.00"]
    states = at._tree.get_widget_states()
    editor = states.widgets.add()
    editor.id = at.dataframe[0].proto.id
    editor.string_value = json.dumps({"edited_rows": {"0": {"monto": -5}}, "added_rows": [], "deleted_rows": []})
    at._run(states)
    assert not at.exception
    assert at.session_state["df_movimientos"].iloc[1]["monto"] == 25.0
    assert "No se guardó" in at.error[0].value


def test_multiple_uploads_deduplication_errors_and_clear(monkeypatch):
    import app

    state = {}
    monkeypatch.setattr(app.st, "session_state", state)
    app.initialize_state()
    valid = BytesIO(make_pdf([["Billetera Yasta", "19/5/2026 21:50:35 Abono CRÉDITO 55.00"]]))
    valid.name = "valido.pdf"
    invalid = BytesIO(b"corrupt")
    invalid.name = "invalido.pdf"
    app.process_uploads([valid, invalid, valid], "YASTA")
    assert len(state["df_movimientos"]) == 1
    assert [item["estado"] for item in state["load_results"]] == ["Procesado", "Error", "Procesado"]
    assert state["load_results"][2]["duplicados"] == 1
    bnb = BytesIO(make_pdf([["Banco Nacional de Bolivia", "03/10/2026 11:26 Pago DÉBITO 20.00"]]))
    bnb.name = "bnb.pdf"
    app.process_uploads([bnb], "BNB")
    assert len(state["df_movimientos"]) == 2
    assert set(state["df_movimientos"]["origen"]) == {"BNB", "YASTA"}
    assert totals(state["df_movimientos"]) == (55.0, 20.0, 35.0)
    app.clear_data()
    assert state["df_movimientos"].empty
    assert state["load_results"] == []
    assert state["upload_revision"] == 1


def test_selected_bank_routes_parser_and_next_load_combines_banks(monkeypatch):
    import app

    state = {}
    monkeypatch.setattr(app.st, "session_state", state)
    app.initialize_state()
    yasta = BytesIO(make_pdf([["Billetera Yasta", "19/5/2026 21:50:35 Abono CRÉDITO 55.00"]]))
    yasta.name = "yasta.pdf"
    bnb = BytesIO(make_pdf([["Banco Nacional de Bolivia", "03/10/2026 11:26 Pago DÉBITO 20.00"]]))
    bnb.name = "bnb.pdf"
    state["upload_origin"] = "YASTA"
    app.process_selected_uploads([yasta], "YASTA")
    assert state["upload_origin"] is None
    assert state["upload_revision"] == 1
    assert state["df_movimientos"]["origen"].tolist() == ["YASTA"]
    state["upload_origin"] = "BNB"
    state["filter_origin"] = "YASTA"
    state["filter_type"] = "INGRESO"
    app.process_selected_uploads([bnb], "BNB")
    assert state["upload_origin"] is None
    assert state["upload_revision"] == 2
    assert state["filter_origin"] == state["filter_type"] == "Todos"
    assert state["df_movimientos"]["origen"].tolist() == ["YASTA", "BNB"]
    assert totals(state["df_movimientos"]) == (55.0, 20.0, 35.0)
    assert state["load_results"][0]["origen"] == "BNB"


def test_failed_load_keeps_bank_and_data_for_retry(monkeypatch):
    import app

    state = {}
    monkeypatch.setattr(app.st, "session_state", state)
    app.initialize_state()
    state["upload_origin"] = "BNB"
    wrong = BytesIO(make_pdf([["Billetera Yasta", "19/5/2026 21:50:35 Abono CRÉDITO 55.00"]]))
    wrong.name = "yasta.pdf"
    app.process_selected_uploads([wrong], "BNB")
    assert state["upload_origin"] == "BNB"
    assert state["upload_revision"] == 0
    assert state["df_movimientos"].empty
    assert state["load_results"][0]["estado"] == "Error"


def test_no_bank_cannot_process_upload(monkeypatch):
    import app

    state = {}
    monkeypatch.setattr(app.st, "session_state", state)
    app.initialize_state()
    app.process_selected_uploads([BytesIO(b"not a pdf")], None)
    assert state["df_movimientos"].empty
    assert state["load_results"] == []


def test_ui_processes_yasta_then_bnb_in_same_consolidation():
    # AppTest cannot upload files directly. Replace only the file picker;
    # exercise the real bank selector, processing button, callbacks and editor.
    at = AppTest.from_string('''
from io import BytesIO
from unittest.mock import patch
import streamlit as st
from app import main
from tests.pdf_factory import make_pdf

def pick_file(*args, **kwargs):
    bank = st.session_state.get("upload_origin")
    if bank is None:
        return []
    lines = {"YASTA": ["Billetera Yasta", "19/5/2026 21:50:35 Abono CRÉDITO 55.00"],
             "BNB": ["BNB", "03/10/2026 11:26 Pago DÉBITO 20.00"]}
    uploaded = BytesIO(make_pdf([lines[bank]]))
    uploaded.name = bank + ".pdf"
    return [uploaded]

with patch("app.st.file_uploader", side_effect=pick_file):
    main()
''').run()
    at.radio(key="upload_origin").set_value("YASTA").run()
    at.button[0].click().run()
    assert not at.exception
    assert at.radio(key="upload_origin").value is None
    assert len(at.session_state["df_movimientos"]) == 1
    at.radio(key="upload_origin").set_value("BNB").run()
    at.button[0].click().run()
    assert not at.exception
    assert at.radio(key="upload_origin").value is None
    assert at.session_state["df_movimientos"]["origen"].tolist() == ["YASTA", "BNB"]
    assert len(at.dataframe[-1].value) == 2
    assert [metric.value for metric in at.metric] == ["Bs 55.00", "Bs 20.00", "Bs 35.00"]

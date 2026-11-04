import os
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from movements import COLUMNS, consolidate, totals
from parsers import ParseError, parse_bnb, parse_yasta
from parsers.bnb_parser import _table_records as bnb_tables
from parsers.common import parse_amount
from parsers.yasta_parser import _table_records as yasta_tables
from tests.pdf_factory import bnb_section_stream, make_pdf, make_pdf_streams


def test_yasta_text_fallback_multiline_and_multiple_pages():
    raw = make_pdf([
        ["Billetera Movil Yasta", "FECHA HORA DESCRIPCIÓN TIPO MONTO",
         "19/5/2026 21:50:35 Transferencia recibida CRÉDITO 1,055.00", "Motivo: Caja chica",
         "La información del extracto está sujeta a verificación de la Billetera Yasta", "Página 1 de 2"],
        ["Billetera Movil Yasta", "FECHA HORA DESCRIPCIÓN TIPO MONTO",
         "22/5/2026 09:29:13 Transferencia QR DÉBITO -15.00", "Motivo: Libreria", "Página 2 de 2"],
    ])
    df = parse_yasta(raw)
    assert list(df.columns) == COLUMNS
    assert df["fecha"].tolist() == ["2026-05-19 21:50:35", "2026-05-22 09:29:13"]
    assert df["tipo"].tolist() == ["INGRESO", "EGRESO"]
    assert df["monto"].tolist() == [1055.0, 15.0]
    assert df["origen"].tolist() == ["YASTA", "YASTA"]
    assert df.iloc[0]["glosa"] == "Transferencia recibida Motivo: Caja chica"
    assert df.iloc[1]["glosa"] == "Transferencia QR Motivo: Libreria"


def test_yasta_tables_support_separate_hour_and_wrapped_description():
    class Page:
        def extract_tables(self):
            return [[["FECHA", "HORA", "DESCRIPCIÓN", "TIPO", "MONTO"],
                     ["19/5/2026", "21:50:35", "Transferencia\nMotivo: Prueba", "CRÉDITO", "55.00"]]]
    assert yasta_tables(Page()) == [{"fecha": "2026-05-19 21:50:35", "glosa": "Transferencia Motivo: Prueba",
                                     "tipo": "INGRESO", "monto": 55.0, "origen": "YASTA"}]


def test_bnb_table_debit_credit_never_uses_balance():
    class Page:
        def extract_tables(self):
            return [[["Fecha", "Glosa", "Débito", "Crédito", "Saldo"],
                     ["03/11/2026", "Pago", "1.234,56", "0,00", "25.000,00"],
                     ["03/11/2026", "Abono", "-", "75,00", "25.075,00"]]]
    rows = bnb_tables(Page())
    assert [r["monto"] for r in rows] == [1234.56, 75.0]
    assert [r["tipo"] for r in rows] == ["EGRESO", "INGRESO"]
    assert rows[0]["fecha"] == "2026-11-03"


def test_bnb_text_and_optional_hour():
    df = parse_bnb(make_pdf([["BANCO NACIONAL DE BOLIVIA", "03/11/2026 10:12:34 Pago DÉBITO 25,50",
                             "03/11/2026 Depósito CRÉDITO 100.00"]]))
    assert df["fecha"].tolist() == ["2026-11-03 10:12:34", "2026-11-03"]
    assert totals(df) == (100.0, 25.5, 74.5)


def test_bnb_positioned_sections_multiline_and_ignored_columns():
    raw = make_pdf_streams([
        bnb_section_stream("Depósitos", "Créditos", [("03/10/2026", "11:26", "ABONO EN CUENTA\nCuenta Origen: 123. Nombre: Prueba", "90.00")]),
        bnb_section_stream("Retiros", "Débitos", [("05/10/2026", "13:08", "DEBITO INTERBANCARIO\nCuenta Destino: 456.\nAdicional: Compra", "30.50")]),
    ])
    df = parse_bnb(raw)
    assert df["fecha"].tolist() == ["2026-10-03 11:26:00", "2026-10-05 13:08:00"]
    assert df["tipo"].tolist() == ["INGRESO", "EGRESO"]
    assert df["glosa"].tolist() == ["ABONO EN CUENTA Cuenta Origen: 123. Nombre: Prueba",
                                   "DEBITO INTERBANCARIO Cuenta Destino: 456. Adicional: Compra"]
    assert totals(df) == (90.0, 30.5, 59.5)
    assert not df["glosa"].str.contains("IGNORAR_REF|XYZ123").any()


def test_bnb_both_sections_on_same_page():
    deposit = bnb_section_stream("Depósitos", "Créditos", [("03/10/2026", "11:26", "ABONO", "90.00")])
    withdrawal = bnb_section_stream("Retiros", "Débitos", [("05/10/2026", "13:08", "PAGO", "30.50")], offset=220)
    df = parse_bnb(make_pdf_streams([deposit + b"\n" + withdrawal]))
    assert totals(df) == (90.0, 30.5, 59.5)


@pytest.mark.parametrize("continuous_lines", [False, True])
def test_bnb_keywords_and_row_lines_across_pages_without_repeated_headers(continuous_lines):
    # Solo el primer bloque tiene encabezado. Los títulos identifican el tipo,
    # aunque las secciones ocupen varias hojas y empiecen en orden inverso.
    pages = [
        bnb_section_stream("Retiros", "Débitos", [("01/08/2026", "10:01", "PAGO UNO", "10.00")],
                           continuous_lines=continuous_lines, show_total=False),
        bnb_section_stream("Retiros (continuación)", "Débitos", [("02/08/2026", "10:02", "PAGO DOS\nDetalle completo", "20.00")],
                           continuous_lines=continuous_lines, show_header=False),
        bnb_section_stream("Depósitos / Créditos", "Créditos", [("03/08/2026", "10:03", "ABONO UNO", "30.00")],
                           continuous_lines=continuous_lines, show_header=False, show_total=False),
        bnb_section_stream("", "Créditos", [("04/08/2026", "10:04", "ABONO DOS", "40.00")],
                           continuous_lines=continuous_lines, show_header=False),
    ]
    df = parse_bnb(make_pdf_streams(pages))
    assert df["tipo"].tolist() == ["EGRESO", "EGRESO", "INGRESO", "INGRESO"]
    assert df["glosa"].tolist() == ["PAGO UNO", "PAGO DOS Detalle completo", "ABONO UNO", "ABONO DOS"]
    assert totals(df) == (70.0, 30.0, 40.0)


def test_bnb_new_section_without_header_on_same_page():
    deposit = bnb_section_stream("Depósitos", "Créditos", [("03/10/2026", "11:26", "ABONO", "90.00")])
    withdrawal = bnb_section_stream("Retiros", "Débitos", [("05/10/2026", "13:08", "PAGO", "30.50")],
                                    offset=220, show_header=False)
    df = parse_bnb(make_pdf_streams([deposit + b"\n" + withdrawal]))
    assert df["tipo"].tolist() == ["INGRESO", "EGRESO"]
    assert totals(df) == (90.0, 30.5, 59.5)


def test_bnb_note_after_last_row_does_not_become_a_movement():
    body = bnb_section_stream("Retiros", "Débitos", [("03/10/2026", "11:26", "PAGO", "30.50")])
    # Hay una nota y otra línea antes del total. No pertenecen a la última fila.
    note = b"\nBT /F1 8 Tf 40 620 Td (Aviso del banco) Tj ET\n40 615 m 580 615 l S"
    df = parse_bnb(make_pdf_streams([body + note]))
    assert df["glosa"].tolist() == ["PAGO"]
    assert totals(df) == (0.0, 30.5, -30.5)


def test_bnb_broken_separator_uses_clear_whitespace_between_complete_rows():
    body = bnb_section_stream("Retiros", "Débitos", [
        ("03/10/2026", "11:26", "PAGO UNO", "30.50"),
        ("04/10/2026", "11:26", "PAGO DOS", "10.00"),
    ], continuous_lines=True)
    body = body.replace(b"40 632 m 580 632 l S", b"")
    df = parse_bnb(make_pdf_streams([body]))
    assert df["glosa"].tolist() == ["PAGO UNO", "PAGO DOS"]
    assert totals(df) == (0.0, 40.5, -40.5)


@pytest.mark.parametrize("filled", [False, True])
def test_bnb_tall_rows_lowered_titles_and_staggered_headers(filled):
    descriptions = ["PAGO LARGO\n" + "\n".join(f"Detalle {n}" for n in range(8)),
                    "PAGO CORTO", "OTRO PAGO\n" + "\n".join(f"Adicional {n}" for n in range(10))]
    body = bnb_section_stream("Retiros", "Débitos", [
        ("03/10/2026", "11:26", descriptions[0], "30.50"),
        ("04/10/2026", "12:10", descriptions[1], "10.00"),
        ("05/10/2026", "13:20", descriptions[2], "20.00"),
    ], offset=150, continuous_lines=True, row_heights=[110, 42, 140],
        filled_separators=filled, header_gap=12, header_stagger=8)
    df = parse_bnb(make_pdf_streams([body]))
    assert df["glosa"].tolist() == [" ".join(value.split()) for value in descriptions]
    assert df["fecha"].tolist() == ["2026-10-03 11:26:00", "2026-10-04 12:10:00", "2026-10-05 13:20:00"]
    assert totals(df) == (0.0, 60.5, -60.5)


def test_bnb_date_at_start_of_description_line_is_not_another_row():
    body = bnb_section_stream("Depósitos", "Créditos", [
        ("03/10/2026", "11:26", "ABONO\n01/10/2026 fecha del comprobante\nDetalle final", "90.00"),
    ], row_heights=[70])
    df = parse_bnb(make_pdf_streams([body]))
    assert len(df) == 1
    assert df.iloc[0]["glosa"] == "ABONO 01/10/2026 fecha del comprobante Detalle final"


def test_bnb_new_page_header_recalibrates_columns_before_counting_dates():
    first = bnb_section_stream("Depósitos", "Créditos", [("01/10/2026", "11:26", "PRIMERO", "10.00")])
    second = bnb_section_stream("Retiros", "Débitos", [("02/10/2026", "12:30", "PAGO\n01/10/2026 comprobante", "20.00")], row_heights=[70])
    second = b"q 0.5 0 0 0.5 0 0 cm\n" + second + b"\nQ"
    df = parse_bnb(make_pdf_streams([first, second]))
    assert df["glosa"].tolist() == ["PRIMERO", "PAGO 01/10/2026 comprobante"]
    assert totals(df) == (10.0, 20.0, -10.0)


def test_bnb_continuation_with_no_top_border_keeps_full_description():
    first = bnb_section_stream("Depósitos", "Créditos", [("01/10/2026", "11:26", "PRIMERO", "10.00")], show_total=False)
    continued = bnb_section_stream("", "Créditos", [("02/10/2026", "12:30", "SEGUNDO\nDetalle arriba de la fecha\nDetalle final", "20.00")],
                                   show_header=False, row_heights=[80], continuous_lines=True)
    continued = continued.replace(b"40 674 m 580 674 l S", b"")
    df = parse_bnb(make_pdf_streams([first, continued]))
    assert df["glosa"].tolist() == ["PRIMERO", "SEGUNDO Detalle arriba de la fecha Detalle final"]
    assert totals(df) == (30.0, 0.0, 30.0)


def test_bnb_three_column_alias_headers_use_the_section_keyword():
    body = bnb_section_stream("Detalle de Retiros", "Débitos", [("03/10/2026", "11:26", "PAGO COMPLETO", "30.50")],
                              continuous_lines=True)
    omitted = ("(Referencia)", "(Comprobante)", "(ITF)", "(IGNORAR_REF)", "(XYZ123)", "(0.00)")
    body = b"\n".join(line for line in body.splitlines() if not any(value.encode() in line for value in omitted))
    body = body.replace("(Descripción)".encode("cp1252"), b"(Concepto)").replace("(Débitos)".encode("cp1252"), b"(Importe)")
    df = parse_bnb(make_pdf_streams([body]))
    assert df["glosa"].tolist() == ["PAGO COMPLETO"]
    assert totals(df) == (0.0, 30.5, -30.5)


def test_bnb_right_aligned_amount_heading_does_not_cut_digits():
    body = bnb_section_stream("Retiros", "Débitos", [("03/10/2026", "11:26", "PAGO", "1,230.50")], continuous_lines=True)
    body = body.replace("530 680 Td (Débitos)".encode("cp1252"), "550 680 Td (Débitos)".encode("cp1252"))
    df = parse_bnb(make_pdf_streams([body]))
    assert df["monto"].tolist() == [1230.5]
    assert df["glosa"].tolist() == ["PAGO"]


@pytest.mark.parametrize("amount_header,title", [("Débitos", "Retiros"), ("Créditos", "Depósitos")])
@pytest.mark.parametrize("continuous_lines", [False, True])
def test_bnb_joined_itf_amount_header_recalibrates_columns_on_new_page(amount_header, title, continuous_lines):
    first = bnb_section_stream("Depósitos", "Créditos", [("01/09/2026", "10:00", "ABONO", "10.00")])
    second = bnb_section_stream(title, amount_header, [("02/09/2026", "15:17", "MOVIMIENTO COMPLETO", "20.00")],
                                columns=(40, 120, 360, 490, 543, 553, 600), continuous_lines=continuous_lines)
    # En el segundo diseño los encabezados ITF y Débitos/Créditos se tocan.
    # El monto se desplaza con su columna; reutilizar la primera geometría
    # termina cortando el importe o mezclándolo con el comprobante.
    second = second.replace(b"553 680 Td", b"556 680 Td")
    second = second.replace(b"(Fecha Hora)", b"(FechaHora)")
    second = second.replace(b"(XYZ123)", b"(1P12345674)")
    second = b"\n".join(line for line in second.splitlines() if b"(0.00)" not in line)
    df = parse_bnb(make_pdf_streams([first, second]))
    assert df["monto"].tolist() == [10.0, 20.0]
    assert df["tipo"].tolist() == ["INGRESO", "EGRESO" if title == "Retiros" else "INGRESO"]
    assert df["glosa"].tolist() == ["ABONO", "MOVIMIENTO COMPLETO"]


def test_bnb_indented_description_header_does_not_pollute_date_or_truncate_glosa():
    description = "DEBITO EN CUENTA\nCuenta Destino: Prueba"
    body = bnb_section_stream("Retiros", "Débitos", [("01/09/2026", "15:17", description, "30.50")])
    # El encabezado y los extremos de los tramos están más a la derecha que
    # la descripción real: la celda de fecha terminaba incluyendo DEBI/Cuen.
    body = body.replace(b"120 680 Td", b"140 680 Td")
    body = body.replace(b"120 674", b"140 674").replace(b"120 632", b"140 632")
    df = parse_bnb(make_pdf_streams([body]))
    assert df["fecha"].tolist() == ["2026-09-01 15:17:00"]
    assert df["glosa"].tolist() == ["DEBITO EN CUENTA Cuenta Destino: Prueba"]
    assert df["monto"].tolist() == [30.5]


def test_bnb_signed_withdrawal_reconciles_the_reported_334_20_difference():
    # 22 cargos positivos y una reversión de 167.10: al perder el signo,
    # los 23 importes sumaban 4,277.11 en lugar de 3,942.91.
    rows = [(f"{day:02d}/08/2026", "15:17", f"PAGO {day}", "100.00") for day in range(1, 22)]
    rows += [("22/08/2026", "15:17", "PAGO FINAL", "2,010.01"),
             ("23/08/2026", "15:17", "REVERSIÓN", "-167.10")]
    cover = (b"BT /F1 10 Tf 40 740 Td 16 TL (BNB) Tj T* "
             b"(Total de creditos 100.00) Tj T* (Total de debitos 3,942.91) Tj ET")
    deposits = bnb_section_stream("Depósitos", "Créditos", [("01/08/2026", "10:00", "ABONO", "100.00")])
    withdrawals = b"q 1 0 0 0.5 0 360 cm\n" + bnb_section_stream("Retiros", "Débitos", rows) + b"\nQ"
    df = parse_bnb(make_pdf_streams([cover, deposits, withdrawals, b""]))
    assert len(df) == 24
    assert totals(df) == (267.1, 4110.01, -3842.91)
    reversal = df.loc[df["glosa"] == "REVERSIÓN"].iloc[0]
    assert reversal["tipo"] == "INGRESO" and reversal["monto"] == 167.1
    assert "reversión" in df.attrs["bnb_validation"]


@pytest.mark.parametrize("printed", ["-25.00", "(25.00)"])
def test_bnb_negative_deposit_is_an_expense_only_when_bank_totals_confirm_it(printed):
    body = bnb_section_stream("Depósitos", "Créditos", [
        ("01/09/2026", "10:00", "ABONO", "100.00"),
        ("02/09/2026", "11:00", "REVERSIÓN DE ABONO", printed),
    ])
    summary = b"\nBT /F1 8 Tf 40 40 Td (Total de creditos 75.00) Tj ET"
    df = parse_bnb(make_pdf_streams([body + summary]))
    assert list(df.columns) == COLUMNS
    assert df["monto"].tolist() == [100.0, 25.0]
    assert df["tipo"].tolist() == ["INGRESO", "EGRESO"]
    assert totals(df) == (100.0, 25.0, 75.0)


def test_bnb_negative_charge_not_reversed_when_summary_uses_magnitudes():
    raw = make_pdf([["BNB", "Total de débitos 125.00", "01/09/2026 Pago DÉBITO 100.00",
                     "02/09/2026 Otro pago DÉBITO -25.00"]])
    df = parse_bnb(raw)
    assert df["tipo"].tolist() == ["EGRESO", "EGRESO"]
    assert totals(df) == (0.0, 125.0, -125.0)


def test_bnb_inconsistent_signed_summary_and_section_total_rejected():
    raw = make_pdf([["BNB", "Total de débitos 75.00", "01/09/2026 Pago DÉBITO 100.00",
                     "02/09/2026 Reversión DÉBITO -25.00", "Total retiros 125.00"]])
    with pytest.raises(ParseError, match="no son compatibles entre sí"):
        parse_bnb(raw)


def test_bnb_still_rejects_mismatch_when_neither_signed_nor_magnitude_total_matches():
    raw = make_pdf([["BNB", "Total de débitos 80.00", "01/09/2026 Pago DÉBITO 100.00",
                     "02/09/2026 Reversión DÉBITO -25.00"]])
    with pytest.raises(ParseError, match="no coinciden.*signos impresos"):
        parse_bnb(raw)


def test_bnb_overlapping_rows_are_rejected_even_when_amounts_would_add_up():
    body = bnb_section_stream("Retiros", "Débitos", [
        ("03/10/2026", "11:26", "PAGO UNO\nDetalle A\nDetalle B\nDetalle C\nDetalle D", "30.50"),
        ("04/10/2026", "11:26", "PAGO DOS", "10.00"),
    ], continuous_lines=True)
    body = body.replace(b"40 632 m 580 632 l S", b"")
    with pytest.raises(ParseError, match="Página 1.*separar con certeza"):
        parse_bnb(make_pdf_streams([body]))


def test_bnb_incomplete_native_row_reports_section_date_and_page():
    body = bnb_section_stream("Retiros", "Débitos", [("03/10/2026", "11:26", "PAGO", "???")])
    with pytest.raises(ParseError, match="Página 1: Retiros, movimiento 1.*03/10/2026.*monto"):
        parse_bnb(make_pdf_streams([body]))


def test_bnb_summary_mismatch_includes_expected_actual_difference_and_page_counts():
    raw = make_pdf([["BNB", "Total de débitos 100.00", "03/11/2026 Pago DÉBITO 90.00"]])
    with pytest.raises(ParseError) as error:
        parse_bnb(raw)
    message = str(error.value)
    assert "90.00" in message and "100.00" in message and "-10.00" in message
    assert "pág. 1: 1 movimientos" in message


def test_bnb_repeated_totals_cannot_silently_bypass_validation():
    raw = make_pdf([["BNB", "Total de créditos 100.00", "03/11/2026 Abono CRÉDITO 90.00", "Total de créditos 100.00"]])
    with pytest.raises(ParseError, match="no coinciden"):
        parse_bnb(raw)
    valid = make_pdf([["BNB", "Total de créditos 90.00", "03/11/2026 Abono CRÉDITO 90.00", "Total de créditos 90.00"]])
    assert "coinciden" in parse_bnb(valid).attrs["bnb_validation"]


def test_bnb_total_with_no_value_does_not_consume_a_date_on_the_next_page():
    raw = make_pdf([["BNB", "03/11/2026 Abono CRÉDITO 10.00", "Total depósitos"],
                    ["04/11/2026 Otro abono CRÉDITO 20.00"]])
    df = parse_bnb(raw)
    assert totals(df) == (30.0, 0.0, 30.0)
    assert "no incluye totales comparables" in df.attrs["bnb_validation"]


@pytest.mark.parametrize("repeat_header", [False, True])
def test_bnb_sections_continue_across_pages_without_repeated_title(repeat_header):
    pages = [
        bnb_section_stream("Depósitos", "Créditos", [("01/10/2026", "10:01", "INGRESO UNO", "10.00")],
                           show_total=False, footer="Página 1 de 5"),
        bnb_section_stream("", "Créditos", [("02/10/2026", "10:02", "INGRESO DOS", "20.00")],
                           show_header=repeat_header, show_total=False, footer="Página 2 de 5"),
        bnb_section_stream("Depósitos (continuación)", "Créditos", [("03/10/2026", "10:03", "INGRESO TRES", "30.00")],
                           show_header=repeat_header, footer="Página 3 de 5"),
        bnb_section_stream("Retiros", "Débitos", [("04/10/2026", "10:04", "EGRESO UNO", "4.00")],
                           show_total=False, footer="Página 4 de 5"),
        bnb_section_stream("", "Débitos", [("05/10/2026", "10:05", "EGRESO DOS", "5.00")],
                           show_header=repeat_header, footer="Página 5 de 5"),
    ]
    df = parse_bnb(make_pdf_streams(pages))
    assert df["glosa"].tolist() == ["INGRESO UNO", "INGRESO DOS", "INGRESO TRES", "EGRESO UNO", "EGRESO DOS"]
    assert df["tipo"].tolist() == ["INGRESO"] * 3 + ["EGRESO"] * 2
    assert totals(df) == (60.0, 9.0, 51.0)


def test_bnb_section_switch_on_continuation_page_without_deposit_total():
    first = bnb_section_stream("Depósitos", "Créditos", [("01/10/2026", "10:01", "INGRESO UNO", "10.00")],
                               show_total=False)
    continued = bnb_section_stream("", "Créditos", [("02/10/2026", "10:02", "INGRESO DOS", "20.00")],
                                   show_header=False, show_total=False)
    withdrawal = bnb_section_stream("Retiros", "Débitos", [("03/10/2026", "10:03", "EGRESO UNO", "3.00")], offset=220)
    df = parse_bnb(make_pdf_streams([first, continued + b"\n" + withdrawal]))
    assert df["tipo"].tolist() == ["INGRESO", "INGRESO", "EGRESO"]
    assert totals(df) == (30.0, 3.0, 27.0)


def test_bnb_section_header_mismatch_on_continuation_page_rejected():
    first = bnb_section_stream("Depósitos", "Créditos", [("01/10/2026", "10:01", "INGRESO", "10.00")], show_total=False)
    wrong = bnb_section_stream("", "Débitos", [("02/10/2026", "10:02", "PAGO", "2.00")])
    with pytest.raises(ParseError, match="Página 2.*incompatible"):
        parse_bnb(make_pdf_streams([first, wrong]))


def test_bnb_total_closes_section_and_prevents_stray_rows_on_next_page():
    first = bnb_section_stream("Depósitos", "Créditos", [("01/10/2026", "10:01", "INGRESO", "10.00")])
    stray = bnb_section_stream("", "Créditos", [("02/10/2026", "10:02", "FUERA DE SECCION", "2.00")],
                               show_header=False, show_total=False)
    with pytest.raises(ParseError, match="Página 2"):
        parse_bnb(make_pdf_streams([first, stray]))


def test_bnb_section_and_header_must_agree():
    raw = make_pdf_streams([bnb_section_stream("Retiros", "Créditos", [("03/10/2026", "11:26", "PAGO", "90.00")])])
    with pytest.raises(ParseError, match="incompatible"):
        parse_bnb(raw)


def test_bnb_summary_mismatch_rejects_partial_statement():
    raw = make_pdf([["BNB", "Total de créditos 100.00", "03/11/2026 Abono CRÉDITO 90.00"]])
    with pytest.raises(ParseError, match="no coinciden"):
        parse_bnb(raw)


def test_bnb_section_total_checked_without_account_summary():
    raw = make_pdf([["BNB", "03/11/2026 Abono CRÉDITO 90.00", "Total depósitos 100.00"]])
    with pytest.raises(ParseError, match="no coinciden"):
        parse_bnb(raw)


def test_bnb_blank_page_does_not_close_active_section():
    first = bnb_section_stream("Depósitos", "Créditos", [("01/10/2026", "10:01", "PRIMERO", "10.00")], show_total=False)
    last = bnb_section_stream("", "Créditos", [("02/10/2026", "10:02", "ULTIMO", "20.00")], show_header=False)
    df = parse_bnb(make_pdf_streams([first, b"", last]))
    assert df["glosa"].tolist() == ["PRIMERO", "ULTIMO"]
    assert totals(df) == (30.0, 0.0, 30.0)


def test_different_banks_accounts_and_months_consolidated_together():
    first = parse_bnb(make_pdf([["BNB Cuenta 123", "03/10/2026 11:26 Abono CRÉDITO 90.00"]]))
    second = parse_bnb(make_pdf([["BNB Cuenta 456", "04/11/2026 12:30 Pago DÉBITO 25.00"]]))
    yasta = parse_yasta(make_pdf([["Billetera Yasta", "19/5/2026 21:50:35 Transferencia CRÉDITO 55.00"]]))
    combined = consolidate(consolidate(first, second), yasta)
    assert len(combined) == 3
    assert set(combined["origen"]) == {"BNB", "YASTA"}
    assert totals(combined) == (145.0, 25.0, 120.0)
    assert len(consolidate(combined, first)) == 3


@pytest.mark.parametrize("parser,brand", [(parse_yasta, "BNB"), (parse_bnb, "Yasta")])
def test_wrong_bank_rejected(parser, brand):
    with pytest.raises(ParseError, match="origen seleccionado"):
        parser(make_pdf([[brand]]))


@pytest.mark.parametrize("raw", [b"not a pdf", b"%PDF-1.4 corrupt"])
def test_invalid_pdf(raw):
    with pytest.raises(ParseError):
        parse_yasta(raw)


def test_no_text_requires_ocr():
    with pytest.raises(ParseError, match="OCR"):
        parse_yasta(make_pdf([[]]))


@pytest.mark.parametrize("line", ["22/5/2026 09:29:13 Pago DÉBITO ???", "22/5/2026 Pago DÉBITO 10.00",
                                 "32/5/2026 09:29:13 Pago DÉBITO 10.00"])
def test_malformed_movement_rejects_entire_pdf(line):
    raw = make_pdf([["Billetera Yasta", "19/5/2026 21:50:35 Abono CRÉDITO 55.00", line]])
    with pytest.raises(ParseError, match="Página 1"):
        parse_yasta(raw)


@pytest.mark.parametrize("text,expected", [("1,234.56", 1234.56), ("-1.234,56", 1234.56),
                                            ("15,50", 15.5), ("986.05", 986.05), ("1,000", 1000.0)])
def test_amount_formats(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize("text", ["1,23.45", "1.23.45,67", "1,234.567", "--15.00"])
def test_invalid_amount_grouping_rejected(text):
    with pytest.raises(ParseError):
        parse_amount(text)


@pytest.mark.skipif(not os.getenv("YASTA_SAMPLE_PDF"), reason="PDF real opcional, no incluido en el proyecto")
def test_actual_yasta_sample_table_and_text_fallback():
    raw = Path(os.environ["YASTA_SAMPLE_PDF"]).read_bytes()
    table_df = parse_yasta(raw)
    with patch("parsers.yasta_parser._table_records", return_value=[]):
        text_df = parse_yasta(raw)
    pd.testing.assert_frame_equal(table_df, text_df)
    assert len(table_df) == 4
    assert totals(table_df) == (1041.05, 1001.05, 40.0)
    assert table_df["fecha"].tolist() == ["2026-05-19 21:50:35", "2026-05-22 09:29:13",
                                         "2026-05-29 09:29:42", "2026-05-29 11:19:32"]
    assert all("\n" not in glosa for glosa in table_df["glosa"])


@pytest.mark.skipif(not os.getenv("BNB_SAMPLE_PDF"), reason="PDF real opcional, no incluido en el proyecto")
def test_actual_bnb_sample():
    df = parse_bnb(Path(os.environ["BNB_SAMPLE_PDF"]).read_bytes())
    assert list(df.columns) == COLUMNS
    assert len(df) == 36
    assert df["tipo"].value_counts().to_dict() == {"EGRESO": 26, "INGRESO": 10}
    assert totals(df) == (1328.08, 1753.73, -425.65)
    assert df.iloc[0]["fecha"] == "2026-10-13 11:26:00"
    assert df.iloc[-1]["fecha"] == "2026-10-31 23:59:00"
    assert df.iloc[-1]["glosa"] == "RETENCION IVA - LPZ/AGENCIA CENTRAL"
    assert "Pago pr st" not in df.iloc[0]["glosa"]  # Referencia excluida.
    assert not df["glosa"].str.contains(r"1P\w+").any()  # Comprobantes excluidos.
    assert df["origen"].eq("BNB").all()


@pytest.mark.skipif(not os.getenv("BNB_SECOND_SAMPLE_PDF"), reason="Segundo PDF BNB real opcional, no incluido en el proyecto")
def test_actual_bnb_second_sample_joined_debit_header():
    df = parse_bnb(Path(os.environ["BNB_SECOND_SAMPLE_PDF"]).read_bytes())
    assert list(df.columns) == COLUMNS
    assert len(df) == 20
    assert df["tipo"].value_counts().to_dict() == {"INGRESO": 11, "EGRESO": 9}
    assert totals(df) == (1043.5, 180.07, 863.43)
    assert df.loc[df["tipo"] == "EGRESO", "monto"].tolist() == [20.0, 30.0, 14.0, 5.0, 6.0, 3.0, 1.0, 101.0, 0.07]
    assert df.iloc[-1]["glosa"] == "RETENCION IVA - LPZ/AGENCIA CENTRAL"
    assert df.iloc[11]["fecha"] == "2026-09-09 10:33:00"
    assert df.iloc[11]["glosa"].startswith("DEBITO EN CUENTA (MOVIL) - BNB NET Cuenta Destino: ")
    assert not df["glosa"].str.contains(r"1P\w+").any()


@pytest.mark.skipif(not (os.getenv("BNB_SAMPLE_PDF") and os.getenv("YASTA_SAMPLE_PDF")), reason="PDFs reales opcionales")
def test_actual_bnb_and_yasta_combined():
    bnb = parse_bnb(Path(os.environ["BNB_SAMPLE_PDF"]).read_bytes())
    yasta = parse_yasta(Path(os.environ["YASTA_SAMPLE_PDF"]).read_bytes())
    combined = consolidate(bnb, yasta)
    assert len(combined) == 40
    assert totals(combined) == (2369.13, 2754.78, -385.65)

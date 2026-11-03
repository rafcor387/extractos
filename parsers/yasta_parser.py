"""Yasta: tabla FECHA HORA | DESCRIPCIÓN | TIPO | MONTO.

Calibrado con el extracto de mayo 2026 proporcionado. Se prefieren tablas para
conservar glosas multilínea; TEXT_ROW es la alternativa para PDFs sin bordes.
"""

from __future__ import annotations

import re
import pandas as pd

from movements import from_records
from .common import (
    AMOUNT_PATTERN, DATE_PATTERN, DATE_START, TIME_PATTERN, ParseError,
    cell, clean, header_map, movement_type, normalize, open_statement,
    parse_amount, parse_date,
)

# Ajustar aquí si Yasta cambia las etiquetas o disposición del texto.
TEXT_ROW = re.compile(
    rf"^({DATE_PATTERN})\s+({TIME_PATTERN})\s+(.+?)\s+"
    rf"(CR[ÉE]DITO|D[ÉE]BITO)\s+({AMOUNT_PATTERN})\s*$", re.I
)
FOOTER_PREFIXES = ("LA INFORMACION", "PAGINA ", "EXTRACTO DE CUENTA", "BILLETERA MOVIL", "TU EXTRACTO", "RANGO DE CONSULTA", "NUMERO DE", "TIPO:", "FECHA Y HORA:", "SALDO ACTUAL", "RESUMEN DE CUENTA", "MONTO DE", "FECHA HORA")


def _record(date: str, description: str, kind: str, amount: str) -> dict:
    return {"fecha": parse_date(date, require_time=True), "glosa": clean(description),
            "tipo": movement_type(kind), "monto": parse_amount(amount), "origen": "YASTA"}


def _table_records(page) -> list[dict]:
    records: list[dict] = []
    for table in page.extract_tables():
        mapping: dict[str, int] = {}
        for row in table:
            candidate = header_map(row)
            if {"fecha", "glosa", "tipo", "monto"} <= candidate.keys():
                mapping = candidate
                continue
            if not mapping:
                continue
            date = cell(row, mapping, "fecha")
            description = cell(row, mapping, "glosa")
            kind, amount = cell(row, mapping, "tipo"), cell(row, mapping, "monto")
            if DATE_START.match(date):
                hour = cell(row, mapping, "hora")
                records.append(_record(f"{date} {hour}".strip(), description, kind, amount))
            elif not date and description and not kind and not amount and records:
                records[-1]["glosa"] = clean(records[-1]["glosa"] + " " + description)
            elif any((date, description, kind, amount)):
                raise ParseError("Fila Yasta no reconocida en la tabla; requiere calibración.")
    return records


def _text_records(text: str) -> list[dict]:
    records: list[dict] = []
    for raw_line in text.splitlines():
        line = clean(raw_line)
        if not line or normalize(line).startswith(FOOTER_PREFIXES):
            continue
        match = TEXT_ROW.fullmatch(line)
        if match:
            date, hour, description, kind, amount = match.groups()
            records.append(_record(f"{date} {hour}", description, kind, amount))
        elif DATE_START.match(line):
            raise ParseError("Movimiento Yasta no reconocido; revisa fecha, hora, tipo y monto.")
        elif records:
            records[-1]["glosa"] = clean(records[-1]["glosa"] + " " + line)
    return records


def parse_yasta(pdf_bytes: bytes) -> pd.DataFrame:
    """Devuelve las cinco columnas estándar, con fecha y hora local del PDF."""
    records: list[dict] = []
    with open_statement(pdf_bytes, "YASTA") as (pdf, texts):
        for number, (page, text) in enumerate(zip(pdf.pages, texts), start=1):
            try:
                page_records = _table_records(page)
                text_dates = sum(bool(DATE_START.match(line)) for line in text.splitlines())
                if not page_records:
                    page_records = _text_records(text)
                if len(page_records) != text_dates:
                    raise ParseError("La cantidad de filas extraídas no coincide con las fechas detectadas; requiere revisión.")
                records.extend(page_records)
            except ParseError as exc:
                raise ParseError(f"Página {number}: {exc}") from exc
    if not records:
        raise ParseError("No se encontraron movimientos Yasta. El formato puede requerir calibración.")
    return from_records(records)

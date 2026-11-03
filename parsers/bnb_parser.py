"""BNB: secciones de Depósitos/Créditos y Retiros/Débitos.

Calibrado con el extracto de octubre 2026. Sus filas tienen separadores
horizontales, pero no verticales: reconstruimos las columnas desde sus bordes.
Referencia, comprobante e ITF nunca forman parte de los cinco valores de salida.
Se conserva la alternativa genérica para tablas y texto de otros formatos.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
import pandas as pd
from pdfplumber.page import Page

from movements import from_records
from .common import (
    AMOUNT_PATTERN, DATE_PATTERN, TIME_PATTERN, ParseError,
    cell, clean, header_map, movement_type, normalize, open_statement,
    parse_amount, parse_date,
)

TEXT_ROW = re.compile(
    rf"^({DATE_PATTERN})(?:\s+({TIME_PATTERN}))?\s+(.+?)\s+"
    rf"(CR[ÉE]DITO|D[ÉE]BITO|ABONO|CARGO|INGRESO|EGRESO)\s+({AMOUNT_PATTERN})\s*$", re.I
)
# En el PDF real la descripción puede quedar pegada a la fecha al extraer texto.
BNB_DATE_START = re.compile(rf"^\s*{DATE_PATTERN}(?!\d)")


@dataclass
class TableLayout:
    columns: tuple[float, ...]
    labels: list[str]
    kind: str


@dataclass
class SectionState:
    """La sección y sus columnas sobreviven a los saltos de página."""

    kind: str | None = None
    layout: TableLayout | None = None
    native: bool = False


def _layout_headers(page: Page) -> list[dict]:
    """Localiza encabezados completos y deduce sus columnas sin posiciones fijas."""
    dates = page.search(r"\bFecha\b", case=False)
    descriptions = page.search(r"Descripci[oó]n", case=False)
    references = page.search(r"Referencia", case=False)
    amounts = page.search(r"Cr[eé]ditos|D[eé]bitos", case=False)
    headers = []
    for date in dates:
        aligned = [
            next((hit for hit in hits if abs(hit["top"] - date["top"]) < 2), None)
            for hits in (descriptions, references, amounts)
        ]
        if any(hit is None for hit in aligned):
            continue
        description, reference, amount = aligned
        edges = [edge for edge in page.edges if edge["orientation"] == "h"
                 and date["bottom"] < edge["top"] < date["bottom"] + 6]
        xs = sorted({edge[key] for edge in edges for key in ("x0", "x1")})
        if len(xs) < 4 or not (date["x0"] < description["x0"] < reference["x0"] < amount["x0"]):
            raise ParseError("No se pudieron reconstruir las columnas del extracto BNB.")
        labels = [""] * (len(xs) - 1)
        for anchor, label in ((date, "Fecha Hora"), (description, "Descripción"), (amount, amount["text"])):
            index = next((i for i in range(len(xs) - 1) if xs[i] - 0.1 <= anchor["x0"] < xs[i + 1] - 0.1), None)
            if index is None:
                raise ParseError("Encabezado BNB fuera de las columnas detectadas.")
            labels[index] = label
        kind = "INGRESO" if normalize(amount["text"]) == "CREDITOS" else "EGRESO"
        headers.append({"top": date["top"], "bottom": date["bottom"],
                        "layout": TableLayout(tuple(xs), labels, kind)})
    return headers


def _region_tables(page: Page, layout: TableLayout, top: float, bottom: float) -> list[list]:
    """Recorta exclusivamente las filas de una sección entre dos límites."""
    xs = layout.columns
    dates = [hit for hit in page.search(DATE_PATTERN)
             if xs[0] - 0.1 <= hit["x0"] and hit["x1"] <= xs[1] + 0.1
             and top <= hit["top"] < bottom]
    if not dates:
        return []
    # Los separadores de la columna fecha delimitan filas completas, incluidas
    # las líneas de descripción que empiezan por encima de la fecha centrada.
    separators = sorted({edge["top"] for edge in page.edges if edge["orientation"] == "h"
                         and edge["x0"] <= xs[0] + 0.1 and edge["x1"] >= xs[1] - 0.1
                         and top <= edge["top"] <= bottom})
    first_date = min(hit["top"] for hit in dates)
    preceding = [y for y in separators if y <= first_date]
    if not preceding:
        raise ParseError("La continuación BNB no tiene un separador que permita delimitar la primera fila.")
    start = max(preceding)
    last_date_bottom = max(hit["bottom"] for hit in dates)
    following = [y for y in separators if y >= last_date_bottom]
    end = max(following) if following else bottom
    tables = page.crop((xs[0], start, xs[-1], end)).extract_tables({
        "vertical_strategy": "explicit", "explicit_vertical_lines": list(xs),
        "horizontal_strategy": "lines", "explicit_horizontal_lines": [start, end],
        "snap_tolerance": 2, "intersection_tolerance": 3,
        "text_x_tolerance": 1, "text_y_tolerance": 2,
    })
    return [[layout.labels, *table] for table in tables]


def _positioned_tables(page: Page, state: SectionState) -> list[tuple[list, str]] | None:
    """Recorre títulos, encabezados y totales en orden de lectura.

    Un pie de página conserva la sección abierta. Un total la cierra; un título
    nuevo cambia de sección. Cada tabla tiene su propio ámbito, incluso cuando
    Depósitos y Retiros comparten página. Una continuación sin título ni
    encabezado reutiliza las columnas de su sección anterior.
    """
    headers = _layout_headers(page)
    if headers:
        state.native = True
    if not headers and state.layout is None:
        return None
    titles = page.search(r"(?m)^\s*(Dep[oó]sitos|Retiros)(?:\s*(?:\(continuaci[oó]n\)|[-:]?\s*continuaci[oó]n))?\s*$", case=False)
    totals = page.search(r"(?m)^\s*Total\s+(dep[oó]sitos|retiros)\b", case=False)
    footers = page.search(r"(?m)^\s*P[aá]gina\s+\d+\s+de\s+\d+\s*$", case=False)
    events = [(hit["top"], "title", hit) for hit in titles]
    events += [(hit["top"], "header", hit) for hit in headers]
    events += [(hit["top"], "total", hit) for hit in totals]
    events += [(hit["top"], "footer", hit) for hit in footers]
    events.append((page.height, "end", {"bottom": page.height}))
    batches = []
    cursor = 0.0
    for boundary, event, hit in sorted(events, key=lambda item: item[0]):
        if state.kind and state.layout and boundary > cursor:
            batches.extend((table, state.kind) for table in _region_tables(page, state.layout, cursor, boundary - 0.1))
        if event == "title":
            kind = "INGRESO" if normalize(hit["text"]).startswith("DEPOSITOS") else "EGRESO"
            if kind != state.kind:
                state.layout = None
            state.kind = kind
        elif event == "header":
            layout = hit["layout"]
            if state.kind is not None and state.kind != layout.kind:
                raise ParseError("El encabezado de créditos/débitos es incompatible con la sección BNB activa.")
            state.kind, state.layout = layout.kind, layout
        elif event == "total":
            kind = "INGRESO" if "DEPOSITOS" in normalize(hit["text"]) else "EGRESO"
            if state.kind is not None and state.kind != kind:
                raise ParseError("El total BNB no corresponde a la sección activa.")
            state.kind, state.layout = None, None
        elif event in {"footer", "end"}:
            break
        cursor = hit["bottom"] + 0.1
    return batches


def _table_records(page, section: str | None = None, *, state: SectionState | None = None) -> list[dict]:
    records: list[dict] = []
    batches = _positioned_tables(page, state or SectionState(section)) if isinstance(page, Page) else None
    if batches is None:
        batches = [(table, section) for table in page.extract_tables()]
    for table, section in batches:
        mapping: dict[str, int] = {}
        for row in table:
            candidate = header_map(row)
            if {"fecha", "glosa"} <= candidate.keys() and (
                "debito" in candidate or "credito" in candidate or {"tipo", "monto"} <= candidate.keys()
            ):
                mapping = candidate
                if section == "INGRESO" and "debito" in mapping and "credito" not in mapping:
                    raise ParseError("La sección Depósitos contiene un encabezado de débitos incompatible.")
                if section == "EGRESO" and "credito" in mapping and "debito" not in mapping:
                    raise ParseError("La sección Retiros contiene un encabezado de créditos incompatible.")
                continue
            if not mapping:
                continue
            date, description = cell(row, mapping, "fecha"), cell(row, mapping, "glosa")
            if BNB_DATE_START.match(date):
                hour = cell(row, mapping, "hora")
                if "debito" in mapping and "credito" in mapping:
                    debit_text = cell(row, mapping, "debito")
                    credit_text = cell(row, mapping, "credito")
                    debit = parse_amount(debit_text) if debit_text not in {"", "-", "—"} else 0.0
                    credit = parse_amount(credit_text) if credit_text not in {"", "-", "—"} else 0.0
                    if (debit > 0) == (credit > 0):
                        raise ParseError("La fila debe tener un único débito o crédito mayor que cero.")
                    kind, amount = ("EGRESO", debit) if debit > 0 else ("INGRESO", credit)
                elif "debito" in mapping or "credito" in mapping:
                    field = "debito" if "debito" in mapping else "credito"
                    kind = "EGRESO" if field == "debito" else "INGRESO"
                    amount = parse_amount(cell(row, mapping, field))
                else:
                    kind = movement_type(cell(row, mapping, "tipo"))
                    amount = parse_amount(cell(row, mapping, "monto"))
                records.append({"fecha": parse_date(f"{date} {hour}".strip()), "glosa": description,
                                "tipo": kind, "monto": amount, "origen": "BNB"})
            elif not date and description and records and not any(
                cell(row, mapping, key) for key in ("debito", "credito", "tipo", "monto")
            ):
                records[-1]["glosa"] = clean(records[-1]["glosa"] + " " + description)
            elif any(clean(value) for value in row):
                if normalize(date).startswith(("TOTAL", "SALDO")) or normalize(description).startswith(("TOTAL", "SALDO")):
                    continue
                raise ParseError("Fila BNB no reconocida; ajusta los encabezados del parser.")
    return records


def _text_records(text: str) -> list[dict]:
    records = []
    for line in text.splitlines():
        line = clean(line)
        match = TEXT_ROW.fullmatch(line)
        if match:
            date, hour, description, kind, amount = match.groups()
            records.append({"fecha": parse_date(f"{date} {hour or ''}".strip()), "glosa": description,
                            "tipo": movement_type(kind), "monto": parse_amount(amount), "origen": "BNB"})
        elif BNB_DATE_START.match(line):
            raise ParseError("Fila BNB sin columnas o tipo reconocible; se necesita calibrar el parser con este PDF.")
    return records


def _validate_summary(records: list[dict], texts: list[str]) -> None:
    """Contrasta los movimientos con el resumen del extracto si está disponible."""
    text = normalize("\n".join(texts))
    for label, section, kind in (("CREDITOS", "DEPOSITOS", "INGRESO"), ("DEBITOS", "RETIROS", "EGRESO")):
        actual_cents = sum(round(record["monto"] * 100) for record in records if record["tipo"] == kind)
        for heading in (f"TOTAL DE {label}", f"TOTAL {section}"):
            matches = re.findall(rf"\b{heading}\s+({AMOUNT_PATTERN})", text)
            if len(matches) == 1 and round(parse_amount(matches[0]) * 100) != actual_cents:
                raise ParseError(f"Los {label.lower()} extraídos no coinciden con el total BNB. No se añadirá un extracto parcial.")


def parse_bnb(pdf_bytes: bytes) -> pd.DataFrame:
    records: list[dict] = []
    state = SectionState()
    with open_statement(pdf_bytes, "BNB") as (pdf, texts):
        for number, (page, text) in enumerate(zip(pdf.pages, texts), start=1):
            try:
                page_records = _table_records(page, state=state)
                if not page_records and not state.native:
                    page_records = _text_records(text)
                text_dates = sum(bool(BNB_DATE_START.match(line)) for line in text.splitlines())
                if len(page_records) != text_dates:
                    raise ParseError("No se pudieron interpretar todas las filas fechadas de esta página.")
                records.extend(page_records)
            except ParseError as exc:
                raise ParseError(f"Página {number}: {exc}") from exc
    if not records:
        raise ParseError("No se encontraron movimientos BNB. El formato puede requerir calibración.")
    _validate_summary(records, texts)
    return from_records(records)

"""BNB: secciones de Depósitos/Créditos y Retiros/Débitos.

Las secciones Depósitos y Retiros pueden continuar en otras páginas. Sus filas
se leen entre separadores horizontales y las columnas se deducen del encabezado.
Referencia, comprobante e ITF nunca forman parte de los cinco valores de salida.
Se conserva la alternativa genérica para tablas y texto de otros formatos.
"""

from __future__ import annotations

import re
from statistics import median
from dataclasses import dataclass, replace
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
SECTION_TITLE = (
    r"(?m)^[ \t]*(?:(?:Detalle|Movimientos)[ \t]+de[ \t]+)?(Dep[oó]sitos|Retiros)[ \t]*:?"
    r"(?:[ \t]*(?:/|-)[ \t]*(?:Cr[eé]ditos|D[eé]bitos))?"
    r"(?:[ \t]*(?:\(continuaci[oó]n\)|[-:]?[ \t]*continuaci[oó]n))?[ \t]*$"
)


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
    page_expected: int | None = None


def _height(hit: dict) -> float:
    return hit["bottom"] - hit["top"]


def _date_hits(page: Page, layout: TableLayout, top: float, bottom: float) -> list[dict]:
    left, right = layout.columns[:2]
    return [hit for hit in page.search(DATE_PATTERN)
            if left - 1 <= hit["x0"] and hit["x1"] <= right + 1
            and top <= hit["top"] < bottom]


def _separators(page: Page, left: float, right: float, top: float, bottom: float) -> list[float]:
    """Une tramos, bordes dobles y barras gruesas que separan filas completas."""
    edges = [edge for edge in page.edges if edge["orientation"] == "h"
             and top <= edge["top"] <= bottom and edge["x1"] > left and edge["x0"] < right]
    groups: list[list[dict]] = []
    for edge in sorted(edges, key=lambda edge: edge["top"]):
        if groups and edge["top"] - groups[-1][0]["top"] <= 2:
            groups[-1].append(edge)
        else:
            groups.append([edge])
    bands = []
    for group in groups:
        intervals = sorted((max(left, edge["x0"]), min(right, edge["x1"])) for edge in group)
        end, covered = left, 0.0
        for start, stop in intervals:
            covered += max(0, stop - max(start, end))
            end = max(end, stop)
        if intervals[0][0] > left + 3 or end < right - 3 or covered < (right - left) * 0.85:
            continue
        half_width = max(float(edge.get("linewidth", 0) or 0) for edge in group) / 2
        bands.append((group[0]["top"] - half_width, group[-1]["top"] + half_width))
    # Una barra rellena se representa con dos bordes, no con un grosor de trazo.
    bands.extend((rect["top"], rect["bottom"]) for rect in page.rects
                 if rect["height"] <= 12 and rect["x0"] <= left + 3 and rect["x1"] >= right - 3
                 and top <= rect["top"] and rect["bottom"] <= bottom)
    merged: list[list[float]] = []
    for start, end in sorted(bands):
        if merged and start <= merged[-1][1] + 2:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return [(start + end) / 2 for start, end in merged]


def _cell_text(page: Page, left: float, top: float, right: float, bottom: float,
               *, exclude: list[dict] | None = None) -> str:
    region = page.crop((left, top, right, bottom))
    region = region.filter(lambda obj: obj.get("object_type") != "char" or (
        left <= (obj["x0"] + obj["x1"]) / 2 < right
        and top <= (obj["top"] + obj["bottom"]) / 2 < bottom
        and not any(hit["x0"] <= (obj["x0"] + obj["x1"]) / 2 <= hit["x1"]
                    and hit["top"] <= (obj["top"] + obj["bottom"]) / 2 <= hit["bottom"]
                    for hit in (exclude or []))
    ))
    return region.extract_text(x_tolerance=1, y_tolerance=2) or ""


def _text_gaps(page: Page, layout: TableLayout, top: float, bottom: float) -> list[tuple[float, float]]:
    """Espacios en blanco de toda la fila, para separadores ausentes o rotos."""
    chars = [char for char in page.chars if char["text"].strip()
             and layout.columns[0] <= (char["x0"] + char["x1"]) / 2 < layout.columns[-1]
             and top <= char["top"] and char["bottom"] <= bottom]
    occupied: list[list[float]] = []
    for char in sorted(chars, key=lambda char: char["top"]):
        if occupied and char["top"] <= occupied[-1][1] + 1:
            occupied[-1][1] = max(occupied[-1][1], char["bottom"])
        else:
            occupied.append([char["top"], char["bottom"]])
    minimum = max(3, median([char["height"] for char in chars]) * 0.45) if chars else 3
    return [(a[1], b[0]) for a, b in zip(occupied, occupied[1:]) if b[0] - a[1] >= minimum]


def _layout_headers(page: Page) -> list[dict]:
    """Localiza encabezados completos y deduce sus columnas sin posiciones fijas."""
    dates = page.search(r"\bFecha\b", case=False)
    descriptions = page.search(r"Descripci[oó]n|Glosa|Concepto|Detalle", case=False)
    amounts = page.search(r"\b(?:Cr[eé]ditos?|D[eé]bitos?|Monto|Importe)\b", case=False)
    titles = page.search(SECTION_TITLE, case=False)
    headers = []
    for date in dates:
        aligned = [
            next((hit for hit in sorted(hits, key=lambda hit: abs(hit["top"] - date["top"]))
                  if hit["x0"] > date["x0"]
                  and abs(hit["top"] - date["top"]) <= max(_height(hit), _height(date)) * 1.5), None)
            for hits in (descriptions, amounts)
        ]
        if any(hit is None for hit in aligned):
            continue
        description, amount = aligned
        amount_name = normalize(amount["text"])
        if amount_name.startswith(("CREDITO", "DEBITO")):
            kind = "INGRESO" if amount_name.startswith("CREDITO") else "EGRESO"
        else:
            preceding = [hit for hit in titles if hit["bottom"] <= date["top"]]
            if not preceding:
                continue
            kind = "INGRESO" if normalize(preceding[-1]["groups"][0]) == "DEPOSITOS" else "EGRESO"
        other_amounts = [hit for hit in amounts if hit != amount
                         and abs(hit["top"] - amount["top"]) < 2
                         and normalize(hit["text"]).startswith(("CREDITO", "DEBITO"))]
        if other_amounts:
            # Los diseños con débito y crédito en una misma tabla usan el lector
            # genérico de ambas columnas, sin descartar ninguna de las dos.
            continue
        header_bottom = max(hit["bottom"] for hit in (date, *aligned))
        edges = [edge for edge in page.edges if edge["orientation"] == "h"
                 and header_bottom <= edge["top"] <= header_bottom + max(_height(date) * 3, 24)]
        # Solo el separador más cercano al encabezado define sus columnas.
        if edges:
            edge_top = min(edge["top"] for edge in edges)
            edges = [edge for edge in edges if abs(edge["top"] - edge_top) <= 2]
        xs = sorted({edge[key] for edge in edges for key in ("x0", "x1")})
        if not (date["x0"] < description["x0"] < amount["x0"]):
            raise ParseError("No se pudieron reconstruir las columnas del extracto BNB.")
        if len(xs) < 4:
            # Algunos PDFs dibujan una sola línea continua: sus extremos no
            # revelan columnas. En ese caso usamos los anclajes del encabezado.
            anchors = [description, amount]
            for pattern in (r"Referencia|\bRef\.?", r"Comprobante", r"\bITF\b"):
                anchors.extend(hit for hit in page.search(pattern, case=False)
                               if description["x0"] < hit["x0"] < amount["x0"]
                               and abs(hit["top"] - date["top"]) <= _height(date) * 1.5)
            # Un encabezado de monto puede estar alineado a la derecha. Abrir
            # la columna hasta el inicio de los importes evita cortar sus dígitos.
            ignored_right = max((hit["x1"] for hit in anchors if hit not in (description, amount)),
                                default=description["x1"])
            numbers = [hit for hit in page.search(r"[+-]?\d[\d.,]*[.,]\d{2}\b")
                       if hit["top"] > header_bottom and hit["x0"] > ignored_right]
            amount_left = amount["x0"]
            if numbers:
                rightmost = max(hit["x1"] for hit in numbers)
                amount_left = min(amount_left, *(hit["x0"] for hit in numbers if hit["x1"] >= rightmost - 3))
            boundaries = {hit["x0"] for hit in anchors if hit is not amount}
            boundaries.add(amount_left)
            xs = [min(xs[0], date["x0"]) if xs else date["x0"],
                  *sorted(boundaries),
                  max(xs[-1], amount["x1"]) if xs else page.width]
        labels = [""] * (len(xs) - 1)
        for anchor, label in ((date, "Fecha Hora"), (description, "Descripción"),
                              (amount, "Créditos" if kind == "INGRESO" else "Débitos")):
            index = next((i for i in range(len(xs) - 1) if xs[i] - 0.1 <= anchor["x0"] < xs[i + 1] - 0.1), None)
            if index is None:
                raise ParseError("Encabezado BNB fuera de las columnas detectadas.")
            labels[index] = label
        headers.append({"top": min(hit["top"] for hit in (date, *aligned)), "bottom": header_bottom,
                        "layout": TableLayout(tuple(xs), labels, kind)})
    return headers


def _region_tables(page: Page, layout: TableLayout, top: float, bottom: float) -> list[list]:
    """Lee cada registro entre sus líneas, sin inferir una tabla de toda la página."""
    xs = layout.columns
    dates = sorted(_date_hits(page, layout, top, bottom), key=lambda hit: hit["top"])
    if not dates:
        return []
    # Los separadores de la columna fecha delimitan filas completas, incluidas
    # las líneas de descripción que empiezan por encima de la fecha centrada.
    separators = _separators(page, xs[0], xs[-1], top, bottom)
    gaps = _text_gaps(page, layout, top, bottom)
    # Una división solo se necesita entre fechas consecutivas. Si una línea está
    # rota, un espacio vacío que cruce TODAS las columnas puede sustituirla.
    boundaries = []
    for first, second in zip(dates, dates[1:]):
        between = [y for y in separators if first["bottom"] <= y <= second["top"]]
        if between:
            boundaries.append(between[len(between) // 2])
            continue
        candidates = [(end - start, (start + end) / 2) for start, end in gaps
                      if first["bottom"] <= start and end <= second["top"]]
        candidates.sort(reverse=True)
        if not candidates or (len(candidates) > 1 and candidates[0][0] < candidates[1][0] * 1.5):
            raise ParseError(f"No se pudo separar con certeza el movimiento del {first['text']} "
                             f"del {second['text']}: falta una línea o un espacio claro entre sus registros.")
        boundaries.append(candidates[0][1])
    preceding = [y for y in separators if y <= dates[0]["top"]]
    if preceding:
        start = max(preceding)
    else:
        before = [(a + b) / 2 for a, b in gaps if b <= dates[0]["top"]]
        start = max(before) if before else top
    following = [y for y in separators if y >= dates[-1]["bottom"]]
    end = min(following) if following else bottom
    limits = [start, *boundaries, end]
    rows = []
    mapping = header_map(layout.labels)
    for date, start, end in zip(dates, limits, limits[1:]):
        # Recortar una fila por vez conserva descripciones que comienzan antes
        # de la fecha centrada y evita que pies o tablas vecinas creen filas falsas.
        row = []
        for left, right in zip(xs, xs[1:]):
            row.append(_cell_text(page, left, start, right, end))
        # La fecha se toma de su coincidencia exacta. Los extremos de líneas
        # decorativas y la sangría del encabezado no definen su texto real.
        hours = [hit for hit in page.search(TIME_PATTERN)
                 if start <= hit["top"] and hit["bottom"] <= end
                 and date["x0"] - 1 <= hit["x0"] < min(xs[1], date["x1"] + _height(date) * 2)]
        if hours:
            hours.sort(key=lambda hit: abs(hit["x0"] - date["x0"]))
            aligned = [hit for hit in hours if abs(hit["x0"] - hours[0]["x0"]) < 1]
            if len(aligned) > 1:
                raise ParseError(f"Hay varias horas alineadas con la fecha {date['text']} en un mismo registro.")
            hours = [hours[0]]
        row[mapping["fecha"]] = clean(date["text"] + " " + (hours[0]["text"] if hours else ""))
        description_col = mapping["glosa"]
        # Recuperar también el inicio de la glosa que antes caía dentro de la
        # celda de fecha; no basta con borrar DEBI/Cuen y perder información.
        row[description_col] = _cell_text(page, min(xs[description_col], date["x1"]), start,
                                          xs[description_col + 1], end, exclude=[date, *hours])
        rows.append(row)
    return [[layout.labels, *rows]]


def _positioned_tables(page: Page, state: SectionState) -> list[tuple[list, str]] | None:
    """Recorre títulos, encabezados y totales en orden de lectura.

    Un pie de página conserva la sección abierta. Un total la cierra; un título
    nuevo cambia de sección. Cada tabla tiene su propio ámbito, incluso cuando
    Depósitos y Retiros comparten página. Una continuación sin título ni
    encabezado reutiliza las columnas de su sección anterior.
    """
    state.page_expected = None
    headers = _layout_headers(page)
    if headers:
        state.native = True
    if not headers and state.layout is None:
        return None
    headers.sort(key=lambda header: header["top"])
    regions = []
    if state.layout:
        regions.append((state.layout, 0, headers[0]["top"] if headers else page.height))
    for i, header in enumerate(headers):
        start = 0 if i == 0 and state.layout is None else header["bottom"]
        end = headers[i + 1]["top"] if i + 1 < len(headers) else page.height
        regions.append((header["layout"], start, end))
    # Las fechas dentro de la glosa no son movimientos adicionales. Se audita
    # la columna de fecha, incluso para detectar filas fuera de una sección.
    state.page_expected = len({(round(hit["top"], 2), round(hit["x0"], 2), hit["text"])
                               for layout, top, bottom in regions for hit in _date_hits(page, layout, top, bottom)})
    titles = page.search(SECTION_TITLE, case=False)
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
            kind = "INGRESO" if normalize(hit["groups"][0]) == "DEPOSITOS" else "EGRESO"
            if state.layout and kind != state.kind:
                # Un nuevo título puede cambiar de sección sin repetir columnas.
                # La geometría se conserva; solo cambia la columna de monto.
                labels = [label if normalize(label) not in {"CREDITOS", "DEBITOS"}
                          else ("Créditos" if kind == "INGRESO" else "Débitos")
                          for label in state.layout.labels]
                state.layout = replace(state.layout, labels=labels, kind=kind)
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
            state.kind = None
        elif event in {"footer", "end"}:
            break
        cursor = hit["bottom"] + 0.1
    return batches


def _signed_amount(value: str) -> float:
    """Conserva el signo impreso BNB sin cambiar el lector compartido de Yasta."""
    text = clean(value).replace(" ", "").replace("\u2212", "-")
    text = re.sub(r"^(?:Bs\.?|BOB)", "", text, flags=re.I)
    negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
    return -parse_amount(value) if negative else parse_amount(value)


def _bnb_record(date: str, description: str, kind: str, signed_amount: float) -> dict:
    # La sección y el signo se conservan para validar el resumen bancario.
    # from_records elimina estos metadatos internos de las cinco columnas finales.
    return {"fecha": parse_date(date), "glosa": description,
            "tipo": kind, "monto": abs(signed_amount), "origen": "BNB",
            "_bnb_section": kind, "_bnb_signed_amount": signed_amount}


def _row_record(row: list, mapping: dict[str, int]) -> dict:
    date = cell(row, mapping, "fecha")
    hour = cell(row, mapping, "hora")
    description = cell(row, mapping, "glosa")
    if not description:
        raise ParseError("No se pudo leer la descripción completa del movimiento.")
    if "debito" in mapping and "credito" in mapping:
        debit_text = cell(row, mapping, "debito")
        credit_text = cell(row, mapping, "credito")
        debit = _signed_amount(debit_text) if debit_text not in {"", "-", "—"} else 0.0
        credit = _signed_amount(credit_text) if credit_text not in {"", "-", "—"} else 0.0
        if (debit != 0) == (credit != 0):
            raise ParseError("La fila debe tener un único débito o crédito distinto de cero.")
        kind, amount = ("EGRESO", debit) if debit != 0 else ("INGRESO", credit)
    elif "debito" in mapping or "credito" in mapping:
        field = "debito" if "debito" in mapping else "credito"
        kind = "EGRESO" if field == "debito" else "INGRESO"
        amount = _signed_amount(cell(row, mapping, field))
    else:
        kind = movement_type(cell(row, mapping, "tipo"))
        amount = _signed_amount(cell(row, mapping, "monto"))
    if amount == 0:
        raise ParseError("El importe del movimiento debe ser distinto de cero.")
    return _bnb_record(f"{date} {hour}".strip(), description, kind, amount)


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
                try:
                    records.append(_row_record(row, mapping))
                except ParseError as exc:
                    title = {"INGRESO": "Depósitos", "EGRESO": "Retiros"}.get(section, "BNB")
                    raise ParseError(f"{title}, movimiento {len(records) + 1} ({date}): {exc}") from exc
            elif not date and description and records and not any(
                cell(row, mapping, key) for key in ("debito", "credito", "tipo", "monto")
            ):
                records[-1]["glosa"] = clean(records[-1]["glosa"] + " " + description)
            elif any(clean(value) for value in row):
                if normalize(date).startswith(("TOTAL", "SALDO")) or normalize(description).startswith(("TOTAL", "SALDO")):
                    continue
                raise ParseError(f"No se reconoció la fila después del movimiento {len(records)}: "
                                 "no se pudo separar su fecha, descripción e importe.")
    return records


def _text_records(text: str) -> list[dict]:
    records = []
    for line in text.splitlines():
        line = clean(line)
        match = TEXT_ROW.fullmatch(line)
        if match:
            date, hour, description, kind, amount = match.groups()
            records.append(_bnb_record(f"{date} {hour or ''}".strip(), description,
                                       movement_type(kind), _signed_amount(amount)))
        elif BNB_DATE_START.match(line):
            raise ParseError("Fila BNB sin columnas o tipo reconocible; se necesita calibrar el parser con este PDF.")
    return records


def _validate_summary(records: list[dict], texts: list[str]) -> int:
    """Contrasta los movimientos con el resumen del extracto si está disponible."""
    lines = [normalize(line) for text in texts for line in text.splitlines() if clean(line)]
    checks = 0
    signed_sections = set()
    for label, section, kind in (("CREDITOS", "DEPOSITOS", "INGRESO"), ("DEBITOS", "RETIROS", "EGRESO")):
        section_records = [record for record in records if record.get("_bnb_section", record["tipo"]) == kind]
        actual_cents = sum(round(record["monto"] * 100) for record in section_records)
        signed_cents = sum(round(record.get("_bnb_signed_amount", record["monto"]) * 100) for record in section_records)
        modes = {"magnitude", "signed"}
        for heading in (f"TOTAL DE {label}", f"TOTAL {section}"):
            matches = []
            for i, line in enumerate(lines):
                match = re.fullmatch(rf"{heading}\s*:?\s+({AMOUNT_PATTERN})", line)
                if match:
                    matches.append(round(parse_amount(match[1]) * 100))
                elif re.fullmatch(rf"{heading}\s*:?", line) and i + 1 < len(lines):
                    # Un total puede estar debajo de la etiqueta. Nunca tomar
                    # el día de una fecha de la siguiente página como importe.
                    amount = re.fullmatch(AMOUNT_PATTERN, lines[i + 1])
                    if amount:
                        matches.append(round(parse_amount(amount[0]) * 100))
            if not matches:
                continue
            checks += 1
            # Una misma cifra puede repetirse en el resumen y en otra hoja.
            # También puede haber varios totales de sección que sí se suman.
            expected = sum(matches)
            expected_values = {expected}
            if len(set(matches)) == 1:
                expected_values.add(matches[0])
                if expected not in (actual_cents, signed_cents):
                    expected = matches[0]
            matches_modes = set()
            if actual_cents in expected_values:
                matches_modes.add("magnitude")
            if signed_cents in expected_values:
                matches_modes.add("signed")
            previous_modes = modes
            modes = modes & matches_modes
            if not modes:
                if matches_modes and previous_modes:
                    raise ParseError(f"Los totales BNB de {section.title()} no son compatibles entre sí: "
                                     "unos incluyen los signos impresos y otros suman los valores absolutos. "
                                     "No se añadirá un extracto sin una validación consistente.")
                sign_detail = (f" Con los signos impresos, la suma es {signed_cents / 100:,.2f}."
                               if signed_cents != actual_cents else "")
                raise ParseError(
                    f"Los {label.lower()} extraídos no coinciden con el total BNB: "
                    f"{len(section_records)} registros suman {actual_cents / 100:,.2f}; "
                    f"«{heading.title()}» indica {expected / 100:,.2f}; "
                    f"diferencia {(actual_cents - expected) / 100:+,.2f}.{sign_detail} "
                    "No se añadirá un extracto parcial."
                )
        if modes == {"signed"}:
            signed_sections.add(kind)
    # Solo interpretar un importe negativo como reversión si TODOS los totales
    # presentes de su sección lo confirman. No escoger filas ni ajustar montos.
    for record in records:
        if record.get("_bnb_section") in signed_sections and record["_bnb_signed_amount"] < 0:
            record["tipo"] = "EGRESO" if record["_bnb_section"] == "INGRESO" else "INGRESO"
    return checks


def parse_bnb(pdf_bytes: bytes) -> pd.DataFrame:
    records: list[dict] = []
    state = SectionState()
    page_counts = []
    with open_statement(pdf_bytes, "BNB") as (pdf, texts):
        for number, (page, text) in enumerate(zip(pdf.pages, texts), start=1):
            try:
                page_records = _table_records(page, state=state)
                if not page_records and not state.native:
                    page_records = _text_records(text)
                expected = state.page_expected
                if expected is None:
                    expected = sum(bool(BNB_DATE_START.match(line)) for line in text.splitlines())
                if len(page_records) != expected:
                    raise ParseError(f"Se detectaron {expected} movimientos por fecha y se leyeron {len(page_records)}. "
                                     "Hay filas incompletas o fuera de las secciones Depósitos/Retiros.")
                records.extend(page_records)
                page_counts.append(f"pág. {number}: {len(page_records)} movimientos")
            except ParseError as exc:
                raise ParseError(f"Página {number}: {exc}") from exc
    if not records:
        raise ParseError("No se encontraron movimientos BNB. El formato puede requerir calibración.")
    try:
        checks = _validate_summary(records, texts)
    except ParseError as exc:
        raise ParseError(f"{exc} Lectura por página: {'; '.join(page_counts)}.") from exc
    result = from_records(records)
    deposits = sum(record.get("_bnb_section", record["tipo"]) == "INGRESO" for record in records)
    withdrawals = len(records) - deposits
    validation = f"BNB: {deposits} depósitos y {withdrawals} retiros. "
    validation += ("Los importes coinciden con los totales del documento." if checks
                   else "Filas verificadas; el documento no incluye totales comparables.")
    reversals = sum(record["tipo"] != record.get("_bnb_section", record["tipo"]) for record in records)
    if reversals:
        validation += f" Se conservaron {reversals} {'reversión validada' if reversals == 1 else 'reversiones validadas'} por los totales del banco."
    result.attrs["bnb_validation"] = validation
    return result

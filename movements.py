"""Contrato de datos, consolidación y exportación sin persistencia."""

from __future__ import annotations

from io import BytesIO
import math
from typing import Iterable, Mapping

import pandas as pd
from openpyxl.styles import Font, PatternFill

COLUMNS = ["fecha", "glosa", "tipo", "monto", "origen"]
TYPES = ("INGRESO", "EGRESO")
ORIGINS = ("BNB", "YASTA")


def empty_movements() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="float64" if c == "monto" else "object") for c in COLUMNS})


def validate_movements(df: pd.DataFrame) -> pd.DataFrame:
    """Normaliza y valida el contrato sin cambiar índices ni inventar horas."""
    if list(df.columns) != COLUMNS:
        raise ValueError(f"Se requieren exactamente estas columnas: {', '.join(COLUMNS)}.")
    result = df.copy()
    if result.empty:
        return result.astype({"monto": "float64"})
    for idx, row in result.iterrows():
        label = f"Registro {idx}: "
        date_text = str(row["fecha"])
        try:
            # Solo aceptamos fechas normalizadas por nuestros parsers.
            from datetime import datetime

            fmt = "%Y-%m-%d %H:%M:%S" if " " in date_text else "%Y-%m-%d"
            parsed_date = datetime.strptime(date_text, fmt)
            if parsed_date.strftime(fmt) != date_text:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(label + "fecha inválida.") from None
        if not isinstance(row["glosa"], str) or not row["glosa"].strip():
            raise ValueError(label + "la glosa no puede estar vacía.")
        if row["tipo"] not in TYPES or row["origen"] not in ORIGINS:
            raise ValueError(label + "tipo u origen no permitido.")
        try:
            amount = float(row["monto"])
        except (TypeError, ValueError):
            raise ValueError(label + "el monto debe ser numérico.") from None
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError(label + "el monto debe ser positivo y finito.")
        result.at[idx, "monto"] = round(amount, 2)
        if result.at[idx, "monto"] <= 0:
            raise ValueError(label + "el monto redondeado debe ser mayor que cero.")
        result.at[idx, "glosa"] = " ".join(row["glosa"].split())
    return result.astype({"monto": "float64"})


def from_records(records: Iterable[Mapping[str, object]]) -> pd.DataFrame:
    return validate_movements(pd.DataFrame(records, columns=COLUMNS))


def consolidate(current: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    parts = [validate_movements(frame) for frame in (current, incoming) if not frame.empty]
    if not parts:
        return empty_movements()
    return pd.concat(parts, ignore_index=True).drop_duplicates(subset=COLUMNS).reset_index(drop=True)


def apply_edits(current: pd.DataFrame, row_indices: list[int], edits: Mapping) -> pd.DataFrame:
    """Mapea posiciones del editor filtrado a índices del consolidado completo."""
    candidate = current.copy()
    for position, changes in edits.items():
        idx = row_indices[int(position)]
        for column, value in changes.items():
            if column not in {"glosa", "tipo", "monto"}:
                raise ValueError("Solo puedes editar glosa, tipo y monto.")
            candidate.at[idx, column] = value
    return validate_movements(candidate)


def remove_movements(current: pd.DataFrame, row_indices: Iterable[int]) -> pd.DataFrame:
    """Elimina filas sin mutar el original y rechaza selecciones de índices obsoletos."""
    selected = set(row_indices)
    if selected.difference(current.index):
        raise ValueError("La selección de registros ya no corresponde al consolidado actual.")
    return current.drop(index=list(selected)).reset_index(drop=True)


def totals(df: pd.DataFrame) -> tuple[float, float, float]:
    income = round(float(df.loc[df["tipo"] == "INGRESO", "monto"].sum()), 2)
    expense = round(float(df.loc[df["tipo"] == "EGRESO", "monto"].sum()), 2)
    return income, expense, round(income - expense, 2)


def export_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Evita que las descripciones se interpreten como fórmulas en hojas de cálculo."""
    result = validate_movements(df)
    result["glosa"] = result["glosa"].map(
        lambda text: "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text
    )
    return result


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return export_frame(df).to_csv(index=False, lineterminator="\n").encode("utf-8-sig")


def to_excel_bytes(df: pd.DataFrame) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        export_frame(df).to_excel(writer, sheet_name="Movimientos", index=False)
        sheet = writer.sheets["Movimientos"]
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(color="FFFFFF", bold=True)
            cell.fill = PatternFill("solid", fgColor="087F8C")
        for col, width in {"A": 23, "B": 80, "C": 15, "D": 17, "E": 12}.items():
            sheet.column_dimensions[col].width = width
        for cell in sheet["D"][1:]:
            cell.number_format = '#,##0.00'
    return output.getvalue()

"""Utilidades compartidas. Ninguna función guarda el PDF en disco."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import BytesIO
import re
import unicodedata
from typing import Iterator

import pdfplumber

DATE_PATTERN = r"\d{1,2}[/-]\d{1,2}[/-]\d{4}"
TIME_PATTERN = r"\d{1,2}:\d{2}(?::\d{2})?"
DATE_START = re.compile(rf"^\s*{DATE_PATTERN}\b")
AMOUNT_PATTERN = r"[+-]?\s*\d[\d.,]*"


class ParseError(ValueError):
    """PDF inválido, origen incorrecto o formato que requiere calibración."""


def clean(value: object) -> str:
    return " ".join(str(value or "").split())


def normalize(value: object) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", clean(value)) if not unicodedata.combining(c)).upper()


def parse_date(value: str, *, require_time: bool = False) -> str:
    value = clean(value).replace("/", "-")
    match = re.fullmatch(rf"({DATE_PATTERN})(?:\s+({TIME_PATTERN}))?", value)
    if not match or (require_time and not match[2]):
        raise ParseError("No se pudo reconocer la fecha y hora del movimiento.")
    fmt = "%d-%m-%Y"
    if match[2]:
        fmt += " %H:%M:%S" if match[2].count(":") == 2 else " %H:%M"
    try:
        date = datetime.strptime(value, fmt)
    except ValueError:
        raise ParseError("El PDF contiene una fecha u hora inválida.") from None
    return date.strftime("%Y-%m-%d %H:%M:%S" if match[2] else "%Y-%m-%d")


def parse_amount(value: str) -> float:
    """Acepta 1,234.56, 1.234,56, 1234,56 y signos; devuelve magnitud."""
    text = clean(value).replace(" ", "").replace("\u2212", "-")
    text = re.sub(r"^(?:Bs\.?|BOB)", "", text, flags=re.I)
    negative_parentheses = text.startswith("(") and text.endswith(")")
    if negative_parentheses:
        text = text[1:-1]
    if not re.fullmatch(r"[+-]?\d+(?:[.,]\d+)*", text):
        raise ParseError("El PDF contiene un monto no reconocido.")
    if "." in text and "," in text:
        decimal_sep = "." if text.rfind(".") > text.rfind(",") else ","
        group_sep = "," if decimal_sep == "." else "."
        pattern = rf"[+-]?\d{{1,3}}(?:{re.escape(group_sep)}\d{{3}})+{re.escape(decimal_sep)}\d{{1,2}}"
        if not re.fullmatch(pattern, text):
            raise ParseError("Agrupación de miles o decimales inválida en un monto.")
        text = text.replace(group_sep, "").replace(decimal_sep, ".")
    elif "," in text or "." in text:
        sep = "," if "," in text else "."
        parts = text.split(sep)
        if len(parts[-1]) == 3 and all(len(p) == 3 for p in parts[1:]):
            text = "".join(parts)
        elif len(parts) == 2 and len(parts[-1]) in (1, 2):
            text = text.replace(sep, ".")
        else:
            raise ParseError("Separadores de monto ambiguos; ajusta el parser.")
    try:
        amount = abs(Decimal(text)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise ParseError("No se pudo convertir un monto del PDF.") from None
    return float(amount)


def movement_type(value: str) -> str:
    text = normalize(value)
    if text in {"CREDITO", "INGRESO", "ABONO"}:
        return "INGRESO"
    if text in {"DEBITO", "EGRESO", "CARGO"}:
        return "EGRESO"
    raise ParseError("No se pudo identificar crédito o débito de un movimiento.")


@contextmanager
def open_statement(pdf_bytes: bytes, origin: str) -> Iterator[tuple[pdfplumber.PDF, list[str]]]:
    if not isinstance(pdf_bytes, bytes) or b"%PDF-" not in pdf_bytes[:1024]:
        raise ParseError("El archivo no es un PDF válido.")
    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            texts = [page.extract_text() or "" for page in pdf.pages]
            text = normalize("\n".join(texts))
            if not text:
                raise ParseError("Este PDF no contiene texto extraíble. Necesita OCR antes de cargarlo.")
            valid = bool(re.search(r"\bYASTA\b", text)) if origin == "YASTA" else bool(
                re.search(r"\bBNB\b|BANCO NACIONAL DE BOLIVIA", text)
            )
            if not valid:
                raise ParseError(f"El PDF no se identifica como {origin}. Revisa el origen seleccionado.")
            yield pdf, texts
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError("No se pudo leer el PDF. Puede estar dañado, protegido con contraseña o tener un formato incompatible.") from exc


def header_map(row: list[str | None]) -> dict[str, int]:
    """Alias de encabezados: principal punto de calibración para tablas nuevas."""
    mapping: dict[str, int] = {}
    for i, cell in enumerate(row):
        text = normalize(cell)
        if "FECHA" in text:
            mapping["fecha"] = i
        elif text == "HORA":
            mapping["hora"] = i
        elif any(alias in text for alias in ("DESCRIPCION", "GLOSA", "CONCEPTO", "DETALLE")):
            mapping["glosa"] = i
        elif text in {"TIPO", "TIPO MOVIMIENTO", "TIPO DE MOVIMIENTO"}:
            mapping["tipo"] = i
        elif "DEBITO" in text or "CARGO" in text:
            mapping["debito"] = i
        elif "CREDITO" in text or "ABONO" in text:
            mapping["credito"] = i
        elif text in {"MONTO", "IMPORTE", "VALOR"}:
            mapping["monto"] = i
    return mapping


def cell(row: list, mapping: dict[str, int], name: str) -> str:
    index = mapping.get(name)
    return clean(row[index]) if index is not None and index < len(row) else ""

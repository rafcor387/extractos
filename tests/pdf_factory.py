"""PDFs de texto sintéticos sin dependencias adicionales ni archivos en disco."""


def make_pdf(pages: list[list[str]]) -> bytes:
    streams = []
    for lines in pages:
        escaped = [line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in lines]
        streams.append(("BT /F1 10 Tf 40 740 Td 16 TL " + " ".join(f"({line}) Tj T*" for line in escaped) + " ET").encode("cp1252"))
    return make_pdf_streams(streams)


def make_pdf_streams(streams: list[bytes]) -> bytes:
    objects: list[bytes] = [b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    kids = []
    for stream in streams:
        page_num, content_num = len(objects) + 1, len(objects) + 2
        kids.append(f"{page_num} 0 R")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_num} 0 R >>".encode())
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = f"<< /Type /Pages /Count {len(kids)} /Kids [{' '.join(kids)}] >>".encode()
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.extend(f"{offset:010} 00000 n \n".encode())
    result.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return bytes(result)


def bnb_section_stream(title: str, amount_header: str, rows: list[tuple[str, str, str, str]], *,
                       offset: int = 0, show_header: bool = True, show_total: bool = True,
                       footer: str = "", continuous_lines: bool = False,
                       row_heights: list[int] | None = None, filled_separators: bool = False,
                       header_gap: int = 0, header_stagger: int = 0) -> bytes:
    """Sección BNB realista: separadores horizontales, fecha/hora en dos líneas."""
    commands = []

    def text(x: float, y: float, value: str) -> None:
        value = value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        commands.append(f"BT /F1 8 Tf {x} {y - offset} Td ({value}) Tj ET")

    def separator(y: float) -> None:
        if filled_separators:
            commands.append(f"40 {y - offset - 3} 540 6 re f")
            return
        xs = [40, 580] if continuous_lines else [40, 120, 360, 420, 490, 530, 580]
        for left, right in zip(xs, xs[1:]):
            commands.append(f"{left} {y - offset} m {right} {y - offset} l S")

    text(40, 740, "Banco Nacional de Bolivia")
    if title:
        text(40, 710, title)
    if show_header:
        for x, label in zip([40, 120, 360, 420, 490, 530], ["Fecha Hora", "Descripción", "Referencia", "Comprobante", "ITF", amount_header]):
            text(x, 680 + header_gap - (header_stagger if label == "Descripción" else 0), label)
    separator(674)
    heights = row_heights or [42] * len(rows)
    used_height = 0
    for i, (date, hour, description, amount) in enumerate(rows):
        y = 660 - used_height
        centered = y - (heights[i] - 42) / 2
        text(40, centered, date)
        text(40, centered - 10, hour)
        for line, part in enumerate(description.split("\n")):
            text(120, y - line * 10, part)
        text(360, centered, "IGNORAR_REF")
        text(420, centered, "XYZ123")
        text(490, centered, "0.00")
        text(540, centered, amount)
        separator(y - heights[i] + 14)
        used_height += heights[i]
    if show_total:
        text(40, 650 - used_height, "Total " + ("depósitos" if amount_header == "Créditos" else "retiros"))
    if footer:
        text(450, 20, footer)
    return "\n".join(commands).encode("cp1252")

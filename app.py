"""Ejecutar: python -m streamlit run app.py"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from movements import (
    ORIGINS, TYPES, apply_edits, consolidate, empty_movements,
    to_csv_bytes, to_excel_bytes, totals,
)
from parsers import ParseError, parse_bnb, parse_yasta


def initialize_state() -> None:
    if "df_movimientos" not in st.session_state:
        st.session_state["df_movimientos"] = empty_movements()
    st.session_state.setdefault("editor_revision", 0)
    st.session_state.setdefault("upload_revision", 0)
    st.session_state.setdefault("upload_origin", None)
    st.session_state.setdefault("load_results", [])


def clear_data() -> None:
    st.session_state["df_movimientos"] = empty_movements()
    st.session_state["editor_revision"] += 1
    st.session_state["upload_revision"] += 1
    st.session_state["load_results"] = []
    st.session_state["upload_origin"] = None
    st.session_state["filter_origin"] = "Todos"
    st.session_state["filter_type"] = "Todos"
    st.session_state.pop("edit_error", None)


def save_edits(editor_key: str, row_indices: list[int]) -> None:
    edits = st.session_state[editor_key].get("edited_rows", {})
    try:
        st.session_state["df_movimientos"] = apply_edits(
            st.session_state["df_movimientos"], row_indices, edits
        )
        st.session_state.pop("edit_error", None)
    except (ValueError, TypeError) as exc:
        st.session_state["edit_error"] = f"No se guardó la edición: {exc}"
    st.session_state["editor_revision"] += 1


def process_uploads(files: list, origin: str) -> None:
    parser = {"BNB": parse_bnb, "YASTA": parse_yasta}[origin]
    results = []
    for uploaded in files:
        try:
            # UploadedFile y BytesIO: nunca se escribe el PDF al sistema de archivos.
            incoming = parser(uploaded.getvalue())
            current = st.session_state["df_movimientos"]
            combined = consolidate(current, incoming)
            added = len(combined) - len(current)
            st.session_state["df_movimientos"] = combined
            results.append({"archivo": uploaded.name, "origen": origin, "estado": "Procesado", "extraídos": len(incoming),
                            "añadidos": added, "duplicados": len(incoming) - added, "detalle": ""})
        except (ParseError, ValueError) as exc:
            results.append({"archivo": uploaded.name, "origen": origin, "estado": "Error", "extraídos": 0,
                            "añadidos": 0, "duplicados": 0, "detalle": str(exc)})
        except Exception:
            results.append({"archivo": uploaded.name, "origen": origin, "estado": "Error", "extraídos": 0,
                            "añadidos": 0, "duplicados": 0,
                            "detalle": "Error inesperado al procesar el archivo. Revisa que sea un extracto compatible."})
    st.session_state["load_results"] = results
    st.session_state["editor_revision"] += 1


def process_selected_uploads(files: list, origin: str | None) -> None:
    """Añade una carga y prepara la selección de banco para la siguiente.

    Se ejecuta como callback antes de renderizar los widgets, para reiniciar
    únicamente la selección/carga y conservar el consolidado en memoria.
    """
    if origin not in ORIGINS or not files:
        return
    with st.spinner(f"Leyendo extractos de {origin}…"):
        process_uploads(files, origin)
    if any(item["estado"] == "Procesado" for item in st.session_state["load_results"]):
        st.session_state["upload_revision"] += 1
        st.session_state["upload_origin"] = None
        st.session_state["filter_origin"] = "Todos"
        st.session_state["filter_type"] = "Todos"


def main() -> None:
    st.set_page_config(page_title="Consolidador de extractos", page_icon="📑", layout="wide")
    initialize_state()
    st.title("Consolidador de extractos")
    st.caption("BNB y Yasta · Fecha y hora, glosa, tipo, monto y origen · Sin base de datos")

    with st.sidebar:
        st.header("Cargar extractos")
        origin = st.radio("1. ¿De qué banco son los PDFs que vas a añadir?", ORIGINS,
                          index=None, key="upload_origin")
        files = st.file_uploader(
            f"2. Sube los extractos de {origin}" if origin else "2. Primero elige el banco",
            type=["pdf"], accept_multiple_files=True, disabled=origin is None,
            key=f"uploads_{st.session_state['upload_revision']}_{origin or 'sin_origen'}",
        )
        st.caption("Después de procesar una carga, elige el banco de la siguiente. Puedes alternar BNB y Yasta: todas las cargas se añaden a la misma tabla.")
        st.button("Procesar y añadir al consolidado", type="primary", disabled=not files or origin is None,
                  on_click=process_selected_uploads, args=(files, origin))
        st.button("Limpiar datos", on_click=clear_data, disabled=st.session_state["df_movimientos"].empty and not files)
        st.divider()
        st.caption("Los PDFs y los movimientos se procesan en memoria. Descarga el consolidado antes de cerrar o recargar la sesión.")
        st.caption("BNB y Yasta: calibrados con los PDFs de ejemplo. Los PDFs de imagen requieren OCR previo.")

    results = st.session_state["load_results"]
    if results:
        with st.expander("Resultado de la última carga", expanded=True):
            st.dataframe(pd.DataFrame(results), hide_index=True, width="stretch")
            if any(item["estado"] == "Error" for item in results):
                st.warning("Algunos archivos no se añadieron. Revisa el detalle de cada error.")
            else:
                st.success(f"Se añadieron {sum(item['añadidos'] for item in results)} movimientos. Se omitieron {sum(item['duplicados'] for item in results)} duplicados exactos.")

    df = st.session_state["df_movimientos"]
    income, expense, net = totals(df)
    st.subheader("Resumen del consolidado completo")
    st.caption("Una sola tabla acumula los movimientos de todas las cargas BNB y Yasta, de distintos meses y cuentas.")
    for col, label, value in zip(st.columns(3), ("Total ingresos", "Total egresos", "Saldo neto"), (income, expense, net)):
        col.metric(label, f"Bs {value:,.2f}")
    st.caption("Saldo neto = ingresos menos egresos de los movimientos cargados; no representa el saldo bancario actual. Consolida extractos en la misma moneda.")
    if df.empty:
        st.info("Elige BNB o Yasta, carga tus PDFs y pulsa «Procesar y añadir al consolidado».")
        return

    counts = df["origen"].value_counts()
    st.caption(f"Consolidado: {len(df)} movimientos · BNB: {counts.get('BNB', 0)} · Yasta: {counts.get('YASTA', 0)}")
    st.subheader("Movimientos")
    left, right = st.columns(2)
    filter_origin = left.selectbox("Origen", ("Todos", *ORIGINS), key="filter_origin")
    filter_type = right.selectbox("Tipo", ("Todos", *TYPES), key="filter_type")
    visible = df.copy()
    if filter_origin != "Todos":
        visible = visible.loc[visible["origen"] == filter_origin]
    if filter_type != "Todos":
        visible = visible.loc[visible["tipo"] == filter_type]
    st.caption(f"{len(visible)} de {len(df)} movimientos. Edita glosa, tipo o monto y confirma la celda con Enter. La fecha incluye la hora cuando el PDF la proporciona.")
    if error := st.session_state.get("edit_error"):
        st.error(error)
    editor_key = f"editor_{st.session_state['editor_revision']}_{filter_origin}_{filter_type}"
    st.data_editor(
        visible, key=editor_key, on_change=save_edits, args=(editor_key, visible.index.tolist()),
        hide_index=True, num_rows="fixed", width="stretch", disabled=["fecha", "origen"],
        column_config={
            "fecha": st.column_config.TextColumn("Fecha / hora", width="medium"),
            "glosa": st.column_config.TextColumn("Glosa", width="large", required=True),
            "tipo": st.column_config.SelectboxColumn("Tipo", options=list(TYPES), required=True),
            "monto": st.column_config.NumberColumn("Monto (Bs)", min_value=0.01, step=0.01, format="%.2f", required=True),
            "origen": st.column_config.TextColumn("Origen"),
        },
    )
    scope = st.radio("Datos a exportar", ("Consolidado completo", "Vista filtrada"), horizontal=True)
    export_df = df if scope == "Consolidado completo" else visible
    st.caption(f"La descarga incluirá {len(export_df)} registros y las cinco columnas estándar.")
    csv_col, excel_col = st.columns(2)
    csv_col.download_button("Descargar CSV", data=to_csv_bytes(export_df), file_name="consolidado.csv",
                            mime="text/csv", disabled=export_df.empty, width="stretch")
    excel_col.download_button("Descargar Excel (.xlsx)", data=to_excel_bytes(export_df), file_name="consolidado.xlsx",
                              mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                              disabled=export_df.empty, width="stretch")


if __name__ == "__main__":
    main()

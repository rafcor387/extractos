"""Ejecutar: python -m streamlit run app.py"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from movements import (
    ORIGINS, TYPES, apply_edits, consolidate, empty_movements,
    remove_movements, to_csv_bytes, to_excel_bytes, totals,
)
from parsers import ParseError, parse_bnb, parse_yasta


def initialize_state() -> None:
    if "df_movimientos" not in st.session_state:
        st.session_state["df_movimientos"] = empty_movements()
    st.session_state.setdefault("editor_revision", 0)
    st.session_state.setdefault("upload_revision", 0)
    st.session_state.setdefault("upload_origin", None)
    st.session_state.setdefault("load_results", [])
    st.session_state.setdefault("rows_to_delete", set())


def clear_data() -> None:
    st.session_state["df_movimientos"] = empty_movements()
    st.session_state["editor_revision"] += 1
    st.session_state["upload_revision"] += 1
    st.session_state["load_results"] = []
    st.session_state["upload_origin"] = None
    st.session_state["filter_origin"] = "Todos"
    st.session_state["filter_type"] = "Todos"
    st.session_state["rows_to_delete"] = set()
    st.session_state.pop("editor_snapshot", None)
    st.session_state.pop("download_payloads", None)
    st.session_state.pop("delete_notice", None)
    st.session_state.pop("edit_error", None)


def save_edits(editor_key: str, row_indices: list[int]) -> None:
    edits = st.session_state[editor_key].get("edited_rows", {})
    try:
        selected = set(st.session_state["rows_to_delete"])
        data_edits = {}
        for position, changes in edits.items():
            idx = row_indices[int(position)]
            if "eliminar" in changes:
                if changes["eliminar"]:
                    selected.add(idx)
                else:
                    selected.discard(idx)
            corrections = {key: value for key, value in changes.items() if key != "eliminar"}
            if corrections:
                data_edits[position] = corrections
        if data_edits:
            st.session_state["df_movimientos"] = apply_edits(
                st.session_state["df_movimientos"], row_indices, data_edits
            )
            st.session_state["editor_revision"] += 1
        st.session_state["rows_to_delete"] = selected
        st.session_state.pop("edit_error", None)
    except (ValueError, TypeError) as exc:
        st.session_state["edit_error"] = f"No se guardó la edición: {exc}"
        st.session_state["editor_revision"] += 1


def delete_selected_rows() -> None:
    selected = st.session_state["rows_to_delete"]
    if not selected:
        return
    try:
        st.session_state["df_movimientos"] = remove_movements(st.session_state["df_movimientos"], selected)
        st.session_state["delete_notice"] = f"Se eliminaron {len(selected)} movimientos del consolidado. Los totales y las descargas ya están actualizados."
        st.session_state["rows_to_delete"] = set()
        st.session_state["editor_revision"] += 1
        st.session_state.pop("editor_snapshot", None)
        st.session_state.pop("download_payloads", None)
        st.session_state.pop("edit_error", None)
    except ValueError as exc:
        st.session_state["edit_error"] = str(exc)


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
            st.session_state["rows_to_delete"] = set()
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
        st.button("Procesar y añadir al consolidado", key="process_uploads_button", type="primary", disabled=not files or origin is None,
                  on_click=process_selected_uploads, args=(files, origin))
        st.button("Limpiar datos", key="clear_data_button", on_click=clear_data, disabled=st.session_state["df_movimientos"].empty and not files)
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

    render_consolidation()


@st.fragment
def render_consolidation() -> None:
    """Actualiza el consolidado sin volver a ejecutar la carga y el resto de la página."""
    if notice := st.session_state.pop("delete_notice", None):
        st.success(notice)
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
    st.caption(f"{len(visible)} de {len(df)} movimientos. Edita glosa, tipo o monto y confirma la celda con Enter. Para quitar registros, marca la columna Eliminar y pulsa «Eliminar seleccionados».")
    if error := st.session_state.get("edit_error"):
        st.error(error)
    editor_key = f"editor_{st.session_state['editor_revision']}_{filter_origin}_{filter_type}"
    selected = st.session_state["rows_to_delete"]
    # Conservar tanto la clave como los datos base evita reconstruir el editor
    # al marcar casillas y conserva su desplazamiento. Los cambios del widget
    # se aplican sobre esta base hasta editar datos, filtrar, cargar o eliminar.
    snapshot = st.session_state.get("editor_snapshot")
    if snapshot is None or snapshot["key"] != editor_key:
        editor_view = visible.copy()
        editor_view.insert(0, "eliminar", pd.Series([idx in selected for idx in visible.index], index=visible.index, dtype="bool"))
        snapshot = {"key": editor_key, "data": editor_view}
        st.session_state["editor_snapshot"] = snapshot
    st.data_editor(
        snapshot["data"], key=editor_key, on_change=save_edits, args=(editor_key, visible.index.tolist()),
        hide_index=True, num_rows="fixed", width="stretch", disabled=["fecha", "origen"],
        column_config={
            "eliminar": st.column_config.CheckboxColumn("Eliminar", help="Marca los movimientos que deseas quitar y pulsa Eliminar seleccionados.", width="small", required=True),
            "fecha": st.column_config.TextColumn("Fecha / hora", width="medium"),
            "glosa": st.column_config.TextColumn("Glosa", width="large", required=True),
            "tipo": st.column_config.SelectboxColumn("Tipo", options=list(TYPES), required=True),
            "monto": st.column_config.NumberColumn("Monto (Bs)", min_value=0.01, step=0.01, format="%.2f", required=True),
            "origen": st.column_config.TextColumn("Origen"),
        },
    )
    st.caption(f"{len(selected)} movimientos marcados en todo el consolidado. La selección se conserva al cambiar los filtros; los registros dejan de sumarse cuando pulsas el botón de eliminar.")
    st.button("Eliminar seleccionados", key="delete_selected_button", on_click=delete_selected_rows,
              disabled=not selected, type="primary")
    scope = st.radio("Datos a exportar", ("Consolidado completo", "Vista filtrada"), horizontal=True)
    export_df = df if scope == "Consolidado completo" else visible
    st.caption(f"La descarga incluirá {len(export_df)} registros y las cinco columnas estándar.")
    export_token = (st.session_state["editor_revision"], scope,
                    filter_origin if scope == "Vista filtrada" else None,
                    filter_type if scope == "Vista filtrada" else None)
    payloads = st.session_state.get("download_payloads")
    if payloads is None or payloads["token"] != export_token:
        payloads = {"token": export_token, "csv": to_csv_bytes(export_df), "xlsx": to_excel_bytes(export_df)}
        st.session_state["download_payloads"] = payloads
    csv_col, excel_col = st.columns(2)
    csv_col.download_button("Descargar CSV", data=payloads["csv"], file_name="consolidado.csv",
                            mime="text/csv", disabled=export_df.empty, width="stretch", on_click="ignore")
    excel_col.download_button("Descargar Excel (.xlsx)", data=payloads["xlsx"], file_name="consolidado.xlsx",
                              mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                              disabled=export_df.empty, width="stretch", on_click="ignore")


if __name__ == "__main__":
    main()

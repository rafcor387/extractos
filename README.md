# Consolidador de extractos BNB y Yasta

Aplicación local con Python 3.12 y Streamlit. Los PDFs se leen con
`pdfplumber.open(io.BytesIO(...))`. No hay base de datos, caché global de
movimientos ni archivos PDF temporales: los datos viven en `st.session_state`.

## Ejecutar en Windows

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Abre la dirección local que muestre Streamlit (normalmente
http://localhost:8501). Si el entorno `.venv` ya tiene las dependencias,
solo necesitas el último comando.

## Versión y despliegue en Streamlit Community Cloud

El proyecto declara **Streamlit 1.42.0** en `requirements.txt`. Los anchos de
tablas y descargas usan `use_container_width=True`, compatible con esa versión;
`width="stretch"` y `on_click="ignore"` en las descargas requieren versiones
posteriores. Mantén la misma versión de Streamlit al probar y al desplegar:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -c "import streamlit; print(streamlit.__version__)"
.\.venv\Scripts\python.exe -m pytest -q
```

Para publicar una corrección, sube `app.py` actualizado al repositorio y rama
que utiliza tu aplicación de Community Cloud. Si cambias las dependencias,
sube también `requirements.txt`. Espera a que termine la actualización y revisa
los logs del despliegue. Si hace falta reiniciar, utiliza **Manage app → Reboot
app**; recuerda descargar antes los datos de tu sesión en memoria.

Si el navegador muestra `Failed to fetch dynamically imported module` después
de actualizar Streamlit, prueba a recargar con **Ctrl+F5** o abrir una ventana
privada para descartar recursos JavaScript antiguos en la caché. Si también
ocurre en una sesión nueva, revisa los logs y la conexión: ese mensaje de carga
por sí solo no demuestra un error en los parsers de los bancos.

## Uso

1. Selecciona **BNB** o **YASTA**. La carga de archivos permanece deshabilitada
   hasta que elijas el banco.
2. Carga uno o varios PDFs de esa entidad y pulsa **Procesar y añadir al consolidado**.
3. Tras procesar una carga válida, el selector de banco y el cargador se reinician.
   Elige el banco de la siguiente carga; puedes alternar BNB y Yasta tantas veces
   como necesites. Cambiar el banco prepara un cargador nuevo para esa entidad.
   Todos los meses y cuentas se acumulan en una única tabla con totales conjuntos.
   Los archivos válidos se añaden aunque otro archivo de la misma carga falle.
   La vista vuelve a mostrar todos los bancos y tipos al añadir una carga válida.
4. Revisa los movimientos y, si hace falta, edita **glosa**, **tipo** o **monto**.
   Confirma cada celda con Enter. Se recalculan los totales de todo el consolidado.
5. Filtra por origen o tipo. Las correcciones afectan al registro original,
   también cuando trabajas sobre una vista filtrada.
6. Para quitar movimientos, marca su casilla en la columna **Eliminar** y pulsa
   **Eliminar seleccionados**. La selección se conserva al cambiar filtros y
   el botón elimina todos los marcados, incluso si algún filtro los oculta.
   Al borrarlos dejan de contar en las métricas y en las descargas. Marcar una
   casilla por sí solo no elimina el registro. La columna de selección pertenece
   a la interfaz; el DataFrame consolidado y las exportaciones mantienen las cinco
   columnas estándar.
   Puedes marcar varias casillas seguidas conservando el desplazamiento de la
   tabla. La selección actualiza solo el área del consolidado y reutiliza las
   descargas preparadas; los archivos se regeneran cuando cambian los datos o
   la vista elegida para exportar.
7. Descarga CSV o Excel. Elige entre el **consolidado completo** y la **vista filtrada**.
8. **Limpiar datos** borra los movimientos y reinicia la carga de la sesión.

Descarga el resultado antes de cerrar/recargar la sesión o reiniciar el servidor:
no hay recuperación de datos persistidos. No subas PDFs de entidades diferentes
en una misma carga; cambia el selector entre cargas.

Las sumatorias se calculan sobre el consolidado actual en memoria, con las
correcciones y eliminaciones realizadas. Los filtros solo cambian la vista de
la tabla; las métricas corresponden a todo el consolidado. No se crea un CSV
en disco automáticamente: CSV y Excel se preparan en RAM y se guardan en tu
dispositivo cuando los descargas. Volver a importar un PDF puede restituir
movimientos que hayas borrado de la tabla.

## Contrato de salida

| Columna | Contenido |
| --- | --- |
| `fecha` | `YYYY-MM-DD HH:MM:SS` cuando el extracto incluye hora; `YYYY-MM-DD` si no la incluye |
| `glosa` | Descripción completa con espacios normalizados, incluidos los motivos multilínea |
| `tipo` | Exclusivamente `INGRESO` o `EGRESO` |
| `monto` | `float` positivo, con dos decimales |
| `origen` | Exclusivamente `BNB` o `YASTA` |

La hora se conserva como hora local escrita en el documento, sin conversión de
zona horaria. En Yasta es obligatoria. Crédito = ingreso; débito = egreso.
El saldo neto es ingresos menos egresos, no el saldo bancario del documento.
Las métricas suponen extractos en la misma moneda (Bs); no hay conversión de divisas.

Se eliminan duplicados exactos de las cinco columnas al consolidar. Dos operaciones
legítimas con exactamente los mismos cinco valores se consideran duplicadas;
revisa este caso en extractos BNB que no incluyan hora. Después de una corrección,
volver a cargar el PDF original puede añadir la versión original del registro.

## Parsers y calibración

- `parsers/yasta_parser.py`: calibrado con el extracto Yasta de ejemplo de mayo
  de 2026. Primero extrae tablas para conservar descripciones que ocupan varias
  líneas. Si no encuentra tablas, usa `TEXT_ROW` y une las líneas de continuación.
- `parsers/bnb_parser.py`: calibrado con el extracto BNB de octubre de 2026.
  Reconstruye las columnas desde los encabezados y separadores horizontales.
  Depósitos/Créditos se convierten en `INGRESO` y Retiros/Débitos en `EGRESO`.
  Une fecha y hora aunque estén en líneas diferentes y conserva la descripción
  completa; omite referencia, comprobante e ITF. Comprueba los totales extraídos
  contra el resumen y los totales de sección BNB cuando están presentes.
  Mantiene la sección activa entre páginas, incluso si una continuación no
  repite el título ni el encabezado. Un total cierra la sección y un nuevo título
  cambia a la siguiente; también admite ambas secciones en la misma página.
  Mantiene la alternativa genérica de tablas o texto para otros diseños.
- `parsers/common.py`: alias de encabezados en `header_map`, fechas, conversión
  de montos y validación básica de la marca de la entidad. Esta validación no
  certifica la autenticidad del documento.
- `movements.py`: esquema, validación, consolidación, edición y exportadores.
- `app.py`: interfaz y estado por sesión.

Los parsers rechazan filas fechadas incompletas para evitar añadir un extracto
parcial silenciosamente. Si cambia el diseño del banco, ajusta los alias de
encabezados, las expresiones `TEXT_ROW` y, cuando haga falta, la configuración de
`page.extract_tables()`. Un formato no reconocido genera un mensaje por archivo.
Los PDFs cifrados, dañados o de imágenes sin texto extraíble no se procesan.
No se incluye OCR: «escanear» aquí significa extraer el texto/tablas del PDF.

El CSV usa UTF-8 con BOM y coma como separador. Las descripciones que comienzan
con caracteres de fórmula se exportan como texto literal para hojas de cálculo.
El Excel conserva los montos como números y usa formato de dos decimales.

## Pruebas

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

Prueba opcional sobre el PDF original, sin copiarlo al proyecto:

```powershell
$env:YASTA_SAMPLE_PDF = 'C:\Users\rafco\Downloads\yasta.pdf'
$env:BNB_SAMPLE_PDF = 'C:\Users\rafco\Downloads\bnb.pdf'
.\.venv\Scripts\python.exe -m pytest -q
```

Las pruebas usan PDFs sintéticos creados en memoria. El PDF real no se incluye
en el repositorio y el código de la aplicación no depende de su ubicación.

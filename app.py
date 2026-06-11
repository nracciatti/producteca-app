from __future__ import annotations

import os
import pandas as pd
import streamlit as st

from automation import parse_skus, run_job, BASE_DIR, SESSION_FILE

st.set_page_config(page_title="Producteca Fotos", layout="wide")

APP_USERNAME = os.getenv("APP_USERNAME", "").strip()
APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()

def check_auth() -> bool:
    if not APP_PASSWORD:
        return True
    if st.session_state.get("auth_ok"):
        return True
    st.title("Producteca Fotos")
    st.warning("Esta app está protegida.")
    username = ""
    if APP_USERNAME:
        username = st.text_input("Usuario")
    password = st.text_input("Contraseña", type="password")
    if st.button("Ingresar", type="primary"):
        valid_username = not APP_USERNAME or username == APP_USERNAME
        if valid_username and password == APP_PASSWORD:
            st.session_state["auth_ok"] = True
            st.rerun()
        st.error("Usuario o contraseña incorrectos.")
    return False

if not check_auth():
    st.stop()

st.title("Producteca · Fotos")
st.caption("Automatización de fotos con Producteca, Playwright e imágenes desde Kinderland.")

with st.sidebar:
    st.subheader("Estado")
    st.write(f"Carpeta base: `{BASE_DIR}`")
    st.write(f"session.json local: {'✅' if SESSION_FILE.exists() else '❌'}")
    st.write(f"HEADLESS env: `{os.getenv('HEADLESS', 'false')}`")
    st.divider()
    st.subheader("Uso")
    st.write("1. Copiá `session.json`")
    st.write("2. Elegí Visible")
    st.write("3. Probá con 1 SKU")

left, right = st.columns([2, 1])
with left:
    raw_skus = st.text_area("SKUs (uno por línea)", value="", height=220, placeholder="31660008\n314000420")
with right:
    st.subheader("Opciones")
    execution_mode = st.radio("Modo de navegador", options=["Visible", "Oculto (headless)"], index=0)
    headless = execution_mode == "Oculto (headless)"
    process = st.button("Procesar", type="primary", use_container_width=True)
    st.caption("Producteca se usa para operar el producto activo; las fotos se descargan desde Kinderland.")

metrics_placeholder = st.empty()
progress_placeholder = st.empty()
status_placeholder = st.empty()
result_placeholder = st.empty()

with st.expander("Ver logs", expanded=False):
    logs_placeholder = st.empty()

if process:
    skus = parse_skus(raw_skus)
    if not skus:
        st.error("Pegá al menos un SKU.")
        st.stop()

    def progress_callback(items: list[dict]) -> None:
        df = pd.DataFrame(items)
        if df.empty:
            return
        done = df["status"].isin(["OK", "Error", "No encontrado"]).sum()
        total = len(df)
        ok = (df["status"] == "OK").sum()
        err = df["status"].isin(["Error", "No encontrado"]).sum()
        c1, c2, c3, c4 = metrics_placeholder.columns(4)
        c1.metric("Total", total)
        c2.metric("Procesados", int(done))
        c3.metric("OK", int(ok))
        c4.metric("Error", int(err))
        progress_placeholder.progress(done / total if total else 0.0, text=f"{done}/{total} procesados")
        friendly = df[["index", "sku", "status", "step", "images_detected", "last_event", "error"]].copy()
        friendly.columns = ["#", "SKU", "Estado", "Paso actual", "Imágenes", "Último evento", "Error"]
        status_placeholder.dataframe(friendly, use_container_width=True, hide_index=True)

    with st.spinner("Ejecutando automatización..."):
        logs_placeholder.info("Los logs se guardan en archivo durante la ejecución para evitar bloquear el navegador.")
        result = run_job(skus, log_callback=None, progress_callback=progress_callback, headless=headless)

    c1, c2, c3, c4 = result_placeholder.columns(4)
    c1.metric("Total", result["total"])
    c2.metric("OK", result["ok"])
    c3.metric("Error", result["error"])
    c4.metric("Log", "Guardado")

    result_df = pd.DataFrame(result["results"])
    if not result_df.empty:
        st.subheader("Resultado final")
        st.dataframe(result_df, use_container_width=True, hide_index=True)
        csv_bytes = result_df.to_csv(index=False).encode("utf-8")
        st.download_button("Descargar reporte CSV", data=csv_bytes, file_name="resultado_producteca.csv", mime="text/csv")

    try:
        with open(result["log_file"], "r", encoding="utf-8") as f:
            logs_placeholder.code(f.read()[-20000:], language="text")
    except Exception:
        pass

    st.success(f"Finalizado. Log guardado en: {result['log_file']}")
else:
    st.info("La app está lista. Cuando ejecutes vas a ver progreso, logs y reporte final.")

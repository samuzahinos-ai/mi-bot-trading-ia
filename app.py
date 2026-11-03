import csv
import io
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import streamlit as st

try:
    from google import genai
except Exception:
    genai = None

st.set_page_config(page_title="Bot de trading | Paper 24/7", page_icon="📊", layout="wide")

OWNER = "samuzahinos-ai"
REPO = "mi-bot-trading-ia"
STATE_URL = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/bot-state/paper_state.json"
WORKFLOW_URL = f"https://github.com/{OWNER}/{REPO}/actions/workflows/paper-bot.yml"
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")


def default_state():
    return {
        "cash": 1000.0,
        "initial_balance": 1000.0,
        "position": None,
        "trades": [],
        "realized_pnl": 0.0,
        "wins": 0,
        "losses": 0,
        "peak_equity": 1000.0,
        "max_drawdown_pct": 0.0,
        "last_scan": None,
        "worker_last_run": None,
        "last_error": None,
        "last_comment": "Esperando la primera ejecución automática.",
        "radar": {},
        "bot_enabled": True,
    }


def fetch_state():
    url = f"{STATE_URL}?v={int(time.time())}"
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "paper-trading-dashboard"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            state = default_state()
            state.update(json.loads(response.read().decode("utf-8")))
            return state, None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return default_state(), "El motor aún no ha guardado su primera lectura. Ejecuta el flujo de GitHub Actions."
        return default_state(), f"GitHub respondió con HTTP {exc.code}."
    except Exception as exc:
        return default_state(), f"No se pudo leer el estado remoto: {str(exc)[:180]}"


def age_minutes(timestamp):
    if not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 60.0)
    except Exception:
        return None


st.title("📊 Bot de trading con simulación")
st.caption("Motor programado en GitHub Actions · Datos públicos de Kraken · Solo dinero ficticio · Sin órdenes reales")

with st.sidebar:
    st.header("Control remoto")
    st.markdown(f"[Abrir GitHub Actions para pausar o reanudar nuevas entradas](<{WORKFLOW_URL}>)")
    st.caption(
        "En GitHub: Actions → Bot paper trading cada 5 minutos → Run workflow. "
        "Marca o desmarca allow_entries y pulsa Run workflow. "
        "Aunque desactives nuevas entradas, el motor seguirá gestionando una posición abierta."
    )
    st.divider()
    st.write("**Supuestos del simulador**")
    st.write("Comisión: 0,10 % por lado")
    st.write("Deslizamiento: 0,05 % por lado")
    st.write("Riesgo teórico: 1 % por operación")
    st.write("Máxima asignación: 25 % del efectivo")
    st.write("Pérdida diaria máxima: 3 %")
    st.divider()
    st.caption("El motor se ejecuta cada 5 minutos. El panel puede dormir sin detener el motor.")


@st.fragment(run_every="60s")
def dashboard():
    state, read_warning = fetch_state()

    if read_warning:
        st.warning(read_warning)

    last_run = state.get("worker_last_run") or state.get("last_scan")
    age = age_minutes(last_run)
    if age is None:
        st.info("Estado: esperando la primera ejecución de GitHub Actions.")
    elif age > 15:
        st.error(f"El motor no informa de una ejecución reciente. Último ciclo hace {age:.0f} minutos.")
    elif state.get("bot_enabled", True):
        st.success(f"Motor activo · última ejecución hace {age:.1f} minutos (horario aproximado cada 5 minutos).")
    else:
        st.warning(f"Motor en pausa de nuevas entradas · última ejecución hace {age:.1f} minutos. Gestiona posiciones abiertas.")

    position = state.get("position")
    prices = {symbol: item["price"] for symbol, item in state.get("radar", {}).items()}
    if position:
        mark = prices.get(position["symbol"], position.get("last_price", position["entry_price"]))
        equity = state["cash"] + position["quantity"] * mark * (1.0 - 0.001)
    else:
        equity = state["cash"]
    initial = float(state.get("initial_balance", 1000.0))
    pnl = equity - initial
    pnl_pct = 100.0 * pnl / initial if initial else 0.0
    total_closed = int(state.get("wins", 0)) + int(state.get("losses", 0))
    win_rate = 100.0 * state["wins"] / total_closed if total_closed else None

    cols = st.columns(4)
    cols[0].metric("Patrimonio estimado", f"{equity:,.2f} USDT", f"{pnl_pct:+.2f} %")
    cols[1].metric("Efectivo libre", f"{state['cash']:,.2f} USDT")
    cols[2].metric("Operaciones cerradas", str(total_closed), f"{state['wins']} ganadas / {state['losses']} perdidas")
    cols[3].metric("Drawdown máximo", f"{state.get('max_drawdown_pct', 0.0):.2f} %")

    if position:
        st.subheader(f"Posición simulada abierta: {position['symbol']}")
        pcols = st.columns(4)
        pcols[0].metric("Precio de entrada", f"{position['entry_price']:.6f}")
        pcols[1].metric("Cantidad", f"{position['quantity']:.8f}")
        pcols[2].metric("Stop loss", f"{position['stop_loss']:.6f}")
        pcols[3].metric("Take profit", f"{position['take_profit']:.6f}")
    else:
        st.subheader("Sin posición abierta")

    st.subheader("Radar de mercado")
    radar_rows = []
    for symbol, item in state.get("radar", {}).items():
        radar_rows.append({
            "Activo": symbol,
            "Precio": item["price"],
            "Tendencia": item["regime"],
            "RSI": item["rsi"],
            "Stoch RSI": item["stoch_rsi"],
            "Libro compra %": item["buy_pressure"],
            "Volumen x": item["volume_ratio"],
            "Puntuación": item["score"],
            "Motivos": item["reasons"],
        })
    if radar_rows:
        st.table(radar_rows)
    else:
        st.info("El radar estará disponible después del primer ciclo automático.")

    st.caption(f"Último escaneo UTC: {state.get('last_scan') or 'pendiente'}")
    st.write(f"**Comentario del motor:** {state.get('last_comment', '-')}")
    if state.get("last_error"):
        st.warning(f"Incidencias de mercado: {state['last_error']}")

    st.subheader("Registro reciente")
    trades = state.get("trades", [])
    if trades:
        st.table(trades[:50])
        output = io.StringIO()
        fields = ["time", "symbol", "side", "price", "quantity", "pnl", "reason", "candle_time"]
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(trades)
        st.download_button(
            "Descargar operaciones CSV",
            data=output.getvalue(),
            file_name="operaciones_paper_trading.csv",
            mime="text/csv",
        )
    else:
        st.caption("Todavía no hay operaciones guardadas.")

    st.caption(
        f"Win rate: {win_rate:.1f} %" if win_rate is not None else "Win rate: sin operaciones cerradas"
    )
    if state.get("losses", 0):
        gain_amount = sum(t["pnl"] for t in trades if t.get("pnl") is not None and t["pnl"] > 0)
        loss_amount = sum(abs(t["pnl"]) for t in trades if t.get("pnl") is not None and t["pnl"] < 0)
        st.caption(f"Profit factor reciente: {gain_amount / loss_amount:.2f}" if loss_amount else "Profit factor: no disponible.")
    elif any(t.get("pnl", 0) and t["pnl"] > 0 for t in trades):
        st.caption("Profit factor: infinito (todavía no hay operaciones perdedoras cerradas).")
    else:
        st.caption("Profit factor: no disponible todavía.")


dashboard()

st.divider()
st.subheader("Análisis descriptivo opcional con Gemini")
st.caption("Gemini solo comenta los datos del radar; no controla el motor automático ni autoriza operaciones.")
if st.button("Analizar el radar con Gemini"):
    api_key = None
    try:
        api_key = st.secrets.get("GEMINI_API_KEY")
    except Exception:
        pass
    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    state_for_ai, warning = fetch_state()
    if not api_key:
        st.warning("Gemini está desactivado. Puedes añadir GEMINI_API_KEY en los Secrets de Streamlit.")
    elif genai is None:
        st.error("No está instalado google-genai. Revisa requirements.txt.")
    elif not state_for_ai.get("radar"):
        st.warning("Todavía no hay datos de radar.")
    else:
        try:
            client = genai.Client(api_key=api_key)
            compact = [
                {
                    "activo": key,
                    "precio": value["price"],
                    "tendencia": value["regime"],
                    "rsi": value["rsi"],
                    "score": value["score"],
                    "razones": value["reasons"],
                }
                for key, value in state_for_ai["radar"].items()
            ]
            prompt = (
                "Resume en español este radar de mercado con fines educativos. "
                "Señala riesgos e incertidumbres. No des órdenes de compra o venta ni prometas rentabilidad. "
                "Aclara que la puntuación heurística no es una probabilidad de éxito. Datos: "
                + json.dumps(compact, ensure_ascii=False)
            )
            response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
            st.write(response.text or "Gemini no devolvió texto.")
        except Exception as exc:
            st.error(f"No se pudo consultar Gemini: {str(exc)[:250]}")

st.divider()
st.warning(
    "La automatización corre por intervalos de GitHub Actions, no como un proceso continuo. "
    "Puede retrasarse o perder ejecuciones. Las comisiones, el deslizamiento y los precios de simulación "
    "son aproximaciones; no demuestran ventaja estadística ni garantizan resultados. "
    "Esta aplicación no envía órdenes al exchange."
)

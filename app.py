import csv
import io
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from paper_engine import FEE_RATE, LOOKBACKS, PAIRS, SLIPPAGE_RATE, STRATEGY_VERSION, default_state, portfolio_equity, total_cash

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
LOCAL_TZ = ZoneInfo("Europe/Madrid")


def fetch_state():
    url = f"{STATE_URL}?v={int(time.time())}"
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "paper-trading-dashboard"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            loaded = json.loads(response.read().decode("utf-8"))
        if loaded.get("version") != STRATEGY_VERSION:
            # La rama aún contiene el estado de la estrategia anterior: se espera al primer ciclo nuevo.
            state = default_state()
            state["worker_last_run"] = loaded.get("worker_last_run")
            return state, "Esperando el primer ciclo de la estrategia v2 (el motor aún no ha migrado el estado)."
        state = default_state()
        state.update(loaded)
        return state, None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return default_state(), "El motor aún no ha guardado su primera lectura. Ejecuta el flujo de GitHub Actions."
        return default_state(), f"GitHub respondió con HTTP {exc.code}."
    except Exception as exc:
        return default_state(), f"No se pudo leer el estado remoto: {str(exc)[:180]}"


def parse_utc(timestamp):
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except Exception:
        return None


def local_time(timestamp, fmt="%d/%m/%Y %H:%M:%S"):
    """Convierte un instante ISO en UTC a la hora de Madrid para mostrarlo."""
    dt = parse_utc(timestamp)
    return dt.astimezone(LOCAL_TZ).strftime(fmt) if dt else "pendiente"


def human_age(minutes):
    if minutes < 90:
        return f"{minutes:.0f} min"
    hours, rest = divmod(int(minutes), 60)
    return f"{hours} h {rest:02d} min"


def age_minutes(timestamp):
    dt = parse_utc(timestamp)
    if not dt:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 60.0)


st.title("📊 Bot de trading con simulación")
st.caption(
    "Estrategia v2: seguimiento de tendencia en velas de 4 h · Motor programado en GitHub Actions · "
    "Datos públicos de Kraken · Solo dinero ficticio · Sin órdenes reales"
)

with st.sidebar:
    st.header("Control remoto")
    st.markdown(f"[Abrir GitHub Actions para pausar o reanudar nuevas entradas](<{WORKFLOW_URL}>)")
    st.caption(
        "En GitHub: Actions → Bot paper trading cada 5 minutos → Run workflow. "
        "Marca o desmarca allow_entries y pulsa Run workflow. "
        "Aunque desactives nuevas entradas, el motor seguirá gestionando las posiciones abiertas."
    )
    st.divider()
    st.write("**Supuestos del simulador**")
    st.write(f"Comisión: {FEE_RATE * 100:.2f} % por lado (nivel inicial de Kraken)".replace(".", ","))
    st.write(f"Deslizamiento: {SLIPPAGE_RATE * 100:.2f} % por lado".replace(".", ","))
    st.write(f"Capital repartido en {len(PAIRS) * len(LOOKBACKS)} huchas ({len(PAIRS)} pares × {len(LOOKBACKS)} ventanas)")
    st.write("Riesgo por operación: 5 % de su hucha")
    st.write("Stop de seguridad: 4 × ATR")
    st.divider()
    st.caption("El motor decide con velas de 4 h y se ejecuta cada 5 minutos para vigilar stops y salidas. "
               "El panel puede dormir sin detener el motor.")


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
        st.error(
            f"El motor no informa de una ejecución reciente. Último ciclo hace {human_age(age)} "
            f"({local_time(last_run)}, hora de Madrid)."
        )
        st.caption(
            "Causa habitual: GitHub omite muchas ejecuciones programadas en repositorios pequeños. "
            "Solución definitiva: configurar el disparador externo de LEEME_24-7.md. "
            f"Para ejecutar un ciclo ahora: [abrir el flujo y pulsar Run workflow](<{WORKFLOW_URL}>)."
        )
    elif state.get("bot_enabled", True):
        st.success(f"Motor activo · última ejecución hace {age:.1f} minutos (horario aproximado cada 5 minutos).")
    else:
        st.warning(f"Motor en pausa de nuevas entradas · última ejecución hace {age:.1f} minutos. Gestiona posiciones abiertas.")

    legacy = state.get("legacy_v1")
    if legacy:
        st.info(
            f"Cuenta reiniciada con la estrategia v2. La estrategia anterior (scalping a 1 minuto) terminó con "
            f"{legacy['equity']:,.2f} USDT ({legacy['return_pct']:+.2f} %), {legacy['wins']} ganadas / "
            f"{legacy['losses']} perdidas: el coste de cada operación superaba el movimiento medio del precio."
        )

    radar = state.get("radar", {})
    prices = {symbol: item["price"] for symbol, item in radar.items()}
    equity = portfolio_equity(state, prices)
    initial = float(state.get("initial_balance", 1000.0))
    pnl_pct = 100.0 * (equity - initial) / initial if initial else 0.0
    total_closed = int(state.get("wins", 0)) + int(state.get("losses", 0))
    positions = [
        (sleeve, sleeve["position"]) for sleeve in state["sleeves"].values() if sleeve.get("position")
    ]

    cols = st.columns(4)
    cols[0].metric("Patrimonio estimado", f"{equity:,.2f} USDT", f"{pnl_pct:+.2f} %")
    cols[1].metric("Efectivo libre", f"{total_cash(state):,.2f} USDT")
    cols[2].metric("Operaciones cerradas", str(total_closed), f"{state.get('wins', 0)} ganadas / {state.get('losses', 0)} perdidas", delta_color="off")
    cols[3].metric("Drawdown máximo", f"{state.get('max_drawdown_pct', 0.0):.2f} %")

    history = state.get("equity_history", [])
    if len(history) >= 2:
        frame = pd.DataFrame(history, columns=["Fecha", "Patrimonio (USDT)"])
        frame["Fecha"] = pd.to_datetime(frame["Fecha"], utc=True, errors="coerce").dt.tz_convert(LOCAL_TZ)
        st.line_chart(frame.dropna().set_index("Fecha"), height=220)

    st.subheader(f"Posiciones abiertas ({len(positions)})" if positions else "Sin posiciones abiertas")
    if positions:
        rows = []
        for sleeve, pos in positions:
            mark = prices.get(pos["symbol"], pos.get("last_price", pos["entry_price"]))
            rows.append({
                "Activo": pos["symbol"],
                "Ventana": sleeve["lookback"],
                "Entrada": pos["entry_price"],
                "Precio actual": mark,
                "Resultado %": 100.0 * (mark / pos["entry_price"] - 1.0),
                "Stop de seguridad": pos["stop"],
                "Abierta (Madrid)": local_time(pos.get("entry_time"), "%d/%m %H:%M"),
            })
        st.dataframe(
            pd.DataFrame(rows), hide_index=True, width="stretch",
            column_config={
                "Entrada": st.column_config.NumberColumn(format="%.6g"),
                "Precio actual": st.column_config.NumberColumn(format="%.6g"),
                "Stop de seguridad": st.column_config.NumberColumn(format="%.6g"),
                "Resultado %": st.column_config.NumberColumn(format="%+.2f"),
            },
        )
    else:
        st.caption("Es normal pasar días sin posiciones: el bot solo compra cuando un cierre de 4 h rompe un máximo de 60 a 240 velas.")

    st.subheader("Radar de mercado")
    radar_rows = [{
        "Activo": symbol,
        "Precio": item["price"],
        "Estado": item["status"],
        "Posición en el canal %": item["channel_pos"],
        "Distancia a ruptura %": item["dist_breakout"],
        "Volatilidad ATR %": item["atr_pct"],
    } for symbol, item in radar.items()]
    if radar_rows:
        st.dataframe(
            pd.DataFrame(radar_rows), hide_index=True, width="stretch",
            column_config={
                "Precio": st.column_config.NumberColumn(format="%.6g"),
                "Posición en el canal %": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f"),
                "Distancia a ruptura %": st.column_config.NumberColumn(format="%.2f"),
                "Volatilidad ATR %": st.column_config.NumberColumn(format="%.2f"),
            },
        )
        st.caption(
            "Posición en el canal: dónde está el precio entre el mínimo y el máximo de las últimas 240 velas de 4 h (≈40 días). "
            "Distancia a ruptura: cuánto debe subir para superar el máximo de 60 velas (≈10 días); "
            "la compra exige un cierre de 4 h por encima, no un simple toque."
        )
    else:
        st.info("El radar estará disponible después del primer ciclo automático.")

    st.caption(
        f"Último escaneo: {local_time(state.get('last_scan'))} (hora de Madrid) · "
        f"UTC: {state.get('last_scan') or 'pendiente'}"
    )
    st.write(f"**Comentario del motor:** {state.get('last_comment', '-')}")
    if state.get("last_error"):
        st.warning(f"Incidencias de mercado: {state['last_error']}")

    st.subheader("Registro reciente")
    trades = state.get("trades", [])
    if trades:
        shown = pd.DataFrame(trades[:50])
        if "candle_time" in shown:
            shown["candle_time"] = pd.to_datetime(shown["candle_time"], unit="ms", utc=True, errors="coerce") \
                .dt.tz_convert(LOCAL_TZ).dt.strftime("%d/%m %H:%M")
        if "time" in shown:
            shown["time"] = shown["time"].map(local_time)
        st.dataframe(shown, hide_index=True, width="stretch")
        output = io.StringIO()
        fields = ["time", "symbol", "side", "lookback", "price", "quantity", "pnl", "return_pct", "reason", "candle_time"]
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

    gross_profit, gross_loss = float(state.get("gross_profit", 0.0)), float(state.get("gross_loss", 0.0))
    if total_closed:
        st.caption(f"Win rate: {100.0 * state['wins'] / total_closed:.1f} % · Resultado realizado: {state.get('realized_pnl', 0.0):+.2f} USDT")
    else:
        st.caption("Win rate: sin operaciones cerradas")
    if gross_loss > 0:
        st.caption(f"Profit factor: {gross_profit / gross_loss:.2f}")
    elif gross_profit > 0:
        st.caption("Profit factor: infinito (todavía no hay operaciones perdedoras cerradas).")
    else:
        st.caption("Profit factor: no disponible todavía.")


dashboard()

with st.expander("¿Cómo decide el bot y qué cabe esperar?"):
    st.markdown(
        """
**Qué hace.** Cada 4 horas se cierra una vela. Si el cierre supera el máximo de las últimas 60, 90, 120, 180 o 240 velas
(10 a 40 días), el bot compra en esa «ventana». Vende cuando una vela cierra por debajo del mínimo de la mitad de esas velas,
o si el precio cae 4 ATR bajo la entrada (stop de seguridad). Son 25 huchas independientes: 5 pares × 5 ventanas.

**Por qué no opera cada minuto.** Con comisiones de Kraken (0,40 % por lado en el nivel inicial) cada ida y vuelta cuesta
más de un 1 %. Una estrategia necesita ganancias medias claramente mayores que eso, y solo los movimientos de varios días las dan.

**Qué cabe esperar (simulación histórica con datos de Binance 2018–2026, comisión 0,40 % + 0,05 %, no es una promesa):**

| | Estrategia | Comprar y mantener los 5 pares |
|---|---|---|
| 2023–oct 2026, rentabilidad anual | ≈ 20 % | ≈ 46 % |
| 2023–oct 2026, peor caída | ≈ −31 % | ≈ −70 % |
| 2022 (mercado bajista) | ≈ −10 % | ≈ −69 % |
| 2025 | ≈ −4 % | ≈ −26 % |

- **Gana menos en mercados alcistas fuertes** y protege mucho más en los bajistas.
- **Pierde más operaciones de las que gana** (se ganan entre el 36 % y el 38 %) y encadena rachas de hasta 21 pérdidas seguidas.
  Los beneficios vienen de pocas operaciones grandes.
- **Puede pasar años por debajo de su máximo**: en la simulación, hasta 1.031 días sin nuevo máximo de patrimonio.
- Con comisión de 0,80 % (órdenes a mercado) la rentabilidad anual fuera de muestra baja a ≈ 16 %.
- Los datos son de Binance y los pares de Kraken en USDT tienen menos liquidez; en la práctica habrá diferencias.
        """
    )

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
                    "estado": value["status"],
                    "posicion_en_canal_pct": value["channel_pos"],
                    "distancia_a_ruptura_pct": value["dist_breakout"],
                    "volatilidad_atr_pct": value["atr_pct"],
                }
                for key, value in state_for_ai["radar"].items()
            ]
            prompt = (
                "Resume en español este radar de mercado con fines educativos. La estrategia compra cuando una vela de 4 horas "
                "cierra por encima del máximo de las últimas 60 a 240 velas. "
                "Señala riesgos e incertidumbres. No des órdenes de compra o venta ni prometas rentabilidad. Datos: "
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
    "son aproximaciones; los resultados históricos no garantizan resultados futuros. "
    "Esta aplicación no envía órdenes al exchange."
)

# Bot de trading con simulación (paper trading)

Esta aplicación utiliza datos públicos de Kraken y simula operaciones con saldo ficticio. **No envía órdenes reales al exchange, no necesita claves de trading y no debe considerarse asesoramiento financiero.**

## Arquitectura gratuita

- **Motor automático:** GitHub Actions ejecuta un ciclo cada 5 minutos en un ejecutor estándar de Linux. El disparador fiable es un programador externo gratuito (cron-job.org) que envía un evento `repository_dispatch`; el calendario de GitHub queda como respaldo porque omite muchas ejecuciones. Pasos en [LEEME_24-7.md](LEEME_24-7.md).
- **Panel:** Streamlit Community Cloud muestra el estado publicado por el motor. El panel puede dormir por inactividad sin detener el motor.
- **Persistencia:** el motor guarda un único archivo `paper_state.json` en la rama `bot-state`. La rama se actualiza sustituyendo la instantánea anterior para no generar cientos de commits de historial.
- **Exchange:** Kraken, solo con endpoints públicos de mercado. No se crean órdenes.

GitHub indica que el uso de ejecutores estándar alojados por GitHub es gratuito en repositorios públicos. El calendario tiene un intervalo mínimo de 5 minutos; las ejecuciones programadas pueden retrasarse o perderse durante periodos de carga y se desactivan automáticamente si el repositorio público permanece 60 días sin actividad. Por eso es una automatización gratuita por intervalos, **no un servicio de trading continuo con disponibilidad garantizada**.

## Qué hace el motor (estrategia v2)

- Opera **seguimiento de tendencia en velas de 4 horas** sobre BTC, ETH, SOL, XRP y DOGE (contra USDT), solo en largo.
- Compra cuando una vela de 4 h **cierra por encima del máximo de las últimas 60, 90, 120, 180 o 240 velas** (10 a 40 días). Cada ventana
  es una «hucha» independiente: 5 pares × 5 ventanas = 25 huchas de 40 USDT.
- Vende cuando una vela cierra por debajo del mínimo de la mitad de esas velas, o si el precio cae **4 ATR** bajo la entrada (stop de seguridad).
- Arriesga como máximo el 5 % de cada hucha hasta el stop.
- Simula comisión de 0,40 % por lado (nivel inicial de Kraken) y 0,05 % de deslizamiento. Se pueden cambiar con `FEE_RATE` y `SLIPPAGE_RATE`.
- Se ejecuta cada 5 minutos para vigilar stops y salidas, aunque solo decide compras con velas cerradas de 4 h.
- Si GitHub se salta ejecuciones, al volver revisa todas las velas cerradas desde la entrada: no se pierden stops ni salidas.
- Permite pausar nuevas entradas sin detener la gestión de las posiciones abiertas.
- Gemini es opcional y solo genera comentarios descriptivos desde el panel; no controla el motor ni autoriza operaciones.

**Por qué esta estrategia:** la v1 (scalping a 1 minuto) perdió las 49 operaciones porque cada una costaba más que el movimiento que buscaba.
La validación de la v2 con 9 años de datos, con sus resultados y sus límites, está en [INFORME_BACKTEST.md](INFORME_BACKTEST.md).
Lo esencial: es normal ganar menos de la mitad de las operaciones, pasar días sin operar y tener rachas largas por debajo del máximo.

## Primer arranque

El flujo se inicia automáticamente al cambiar el código y luego se programa cada cinco minutos. También se puede lanzar manualmente:

1. Abre la pestaña **Actions** del repositorio.
2. Selecciona **Bot paper trading cada 5 minutos**.
3. Pulsa **Run workflow**.
4. Marca `allow_entries` para permitir nuevas compras simuladas, o desmárcala para pausarlas.
5. Pulsa **Run workflow** otra vez para confirmar.

Aunque se desactiven nuevas entradas, el motor sigue ejecutándose para gestionar la posición abierta. El panel Streamlit contiene un enlace al flujo de trabajo.

## Ejecución local del motor

1. Instala Python 3.10 o posterior.
2. Instala dependencias:

   ```bash
   python -m pip install -r requirements-worker.txt
   ```

3. Ejecuta un ciclo:

   ```bash
   python worker.py
   ```

Por defecto se usa Kraken. Para cambiarlo localmente, define `EXCHANGE_ID` como `kraken`, `binance`, `bybit` o `coinbase`. El calendario de GitHub Actions está configurado para Kraken.

## Gemini opcional

En **App settings → Secrets** de Streamlit se puede añadir una clave nueva:

```toml
GEMINI_API_KEY = "TU_CLAVE_NUEVA"
```

No escribas claves en `app.py`, en GitHub ni en mensajes. Gemini no es necesario para que el worker funcione.

## Estado y privacidad

La rama pública `bot-state` contiene saldo simulado, operaciones, métricas y lecturas de mercado. No contiene claves ni credenciales. Al pasar de la v1 a la v2 el motor archiva un resumen de la cuenta anterior y empieza una cuenta ficticia nueva de 1.000 USDT.

## Límites

- GitHub Actions ejecuta ciclos cada 5 minutos aproximadamente; no comprueba el mercado segundo a segundo.
- Sin el disparador externo, el calendario de GitHub puede dejar horas sin ejecutar el motor.
- GitHub puede retrasar u omitir ejecuciones programadas y puede desactivar el calendario tras 60 días sin actividad del repositorio.
- Streamlit Community Cloud suspende el panel tras 12 horas sin tráfico, pero eso no detiene GitHub Actions.
- Comisión, deslizamiento, liquidez y ejecución son supuestos simplificados.
- El backtest (ver informe) usa datos de Binance, no incluye todos los costes reales y su selección no fue totalmente ciega: hace falta observación prolongada en papel antes de confiar en ella.
- No añadas claves de API de trading ni permisos de retirada.

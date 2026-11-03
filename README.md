# Bot de trading con simulación (paper trading)

Esta aplicación analiza mercados spot públicos con CCXT y simula operaciones con saldo ficticio. **No envía órdenes reales** y no debe considerarse asesoramiento financiero.

## Características

- Escanea BTC/USDT, ETH/USDT, SOL/USDT, XRP/USDT y DOGE/USDT, siempre que el exchange seleccionado publique esos mercados.
- Usa únicamente velas de 1 minuto cerradas para construir las señales; ignora la última vela, que normalmente sigue abierta.
- Calcula EMA 50/200, RSI, Stoch RSI, MACD, ATR, VWAP móvil, volumen relativo y presión aproximada del libro de órdenes.
- La puntuación es una regla heurística transparente, **no una probabilidad de ganar**.
- Simula comisión en compra y venta y un supuesto de deslizamiento en ambas.
- Dimensiona la posición usando un presupuesto de riesgo teórico del 1 % y limita el capital asignado al 25 % del efectivo.
- Bloquea nuevas entradas al alcanzar el límite diario de pérdida configurado.
- Si se habilita Gemini, su función es exclusivamente comentar los datos; no altera la puntuación ni autoriza operaciones.
- Guarda el estado en SQLite local.

## Ejecución local

1. Instala Python 3.10 o posterior.
2. Abre una terminal en la carpeta del proyecto.
3. Instala dependencias:

   \`\`\`bash
   python -m pip install -r requirements.txt
   \`\`\`

4. Ejecuta:

   \`\`\`bash
   streamlit run app.py
   \`\`\`

Por defecto se usa Kraken. Para seleccionar otro exchange compatible, define la variable de entorno \`EXCHANGE_ID\` como \`kraken\`, \`binance\`, \`bybit\` o \`coinbase\`. La aplicación comprueba qué pares están disponibles y omite los que falten.

## Streamlit Community Cloud

En **App settings → Secrets**, añade una clave nueva (después de revocar cualquier clave que se haya compartido accidentalmente):

\`\`\`toml
GEMINI_API_KEY = "TU_NUEVA_CLAVE"
\`\`\`

La clave no debe escribirse en \`app.py\`, en GitHub ni en mensajes. Gemini es opcional; la estrategia simulada funciona sin ella.

## Persistencia y disponibilidad

La base SQLite (\`paper_trading.sqlite3\`) se guarda en el sistema de archivos local de la instancia. En Streamlit Community Cloud, el almacenamiento local **no es persistente ni está garantizado**: una reconstrucción, suspensión o sustitución de la instancia puede perder el saldo ficticio y el historial. La ejecución periódica de Streamlit tampoco equivale a un servicio 24/7 garantizado. Para conservar historial y operar de forma continua habría que migrar el estado a una base de datos externa y desplegar el motor en un servicio de ejecución permanente.

## Límites del simulador

- Comisión, deslizamiento, liquidez y reglas de ejecución son supuestos simplificados.
- Los stops se evalúan con máximos y mínimos de velas cerradas; si stop y objetivo aparecen dentro de la misma vela, se asume primero el stop.
- Los datos históricos y el libro de órdenes pueden variar entre proveedores.
- No incluye todavía un backtest riguroso, walk-forward, optimización fuera de muestra ni pruebas estadísticas de ventaja.
- No añadas claves de API de trading ni permisos de retirada. Esta versión solo necesita datos públicos de mercado.

## Próximos pasos recomendados

1. Crear pruebas unitarias para indicadores y contabilidad.
2. Añadir un backtester que incluya comisiones, deslizamiento y prevención de look-ahead.
3. Evaluar la estrategia en periodos separados y en varios regímenes de mercado.
4. Migrar el almacenamiento a PostgreSQL u otra base de datos persistente antes de depender del historial.
5. Mantenerlo en paper trading durante un periodo suficiente y comparar los resultados con un benchmark simple.

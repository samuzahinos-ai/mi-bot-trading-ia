# Backtest

`bt.py` es el simulador con el que se eligió y se comprobó la estrategia v2. Los resultados completos
y sus límites están en [`../INFORME_BACKTEST.md`](../INFORME_BACKTEST.md).

Es código de análisis, no forma parte del bot: el flujo de GitHub Actions no lo ejecuta. Para repetir las pruebas hace falta
preparar un `data.pkl` con las velas (ver la cabecera de `bt.py`) y definir `BT_DATA`. Ese `data.pkl` no está en el repositorio.

La prueba `ConsistencyTests` de `tests/test_paper_engine.py` comprueba que el motor real replica las reglas del simulador.

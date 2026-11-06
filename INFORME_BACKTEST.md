# Informe de validación de la estrategia v2

**Resumen en una línea:** la estrategia v1 (scalping a 1 minuto) estaba condenada por sus costes; la v2 (tendencia en velas de
4 horas) tiene expectativa positiva en 9 años de datos incluso con comisiones realistas, pero **gana menos que simplemente
mantener las monedas en mercados alcistas** y exige paciencia. No es una promesa de beneficios.

## 1. Por qué fallaba la v1

Sobre las 49 operaciones cerradas (todas perdedoras):

| Dato | Valor |
|---|---|
| Resultado | 0 ganadas / 49 perdidas, −3,89 % |
| Coste de una ida y vuelta simulado | 0,30 % (2 × 0,10 % comisión + 2 × 0,05 % deslizamiento) |
| Pérdida media por operación | ≈ 0,30 % del importe: casi exactamente el coste fijo |
| Distancia del stop | ≈ 0,10–0,15 % |
| Take profit | Solo 2 de 49; **ambos cerraron en pérdidas** (−0,40 USDT y −0,02 USDT): el objetivo ni cubría el coste |
| Duración típica | Minutos |

El bot esperaba mover el precio un 0,1 % para ganar, pagando un 0,3 % por intentarlo. Perder siempre era el resultado
matemáticamente esperable, no mala suerte. Además, las comisiones simuladas (0,10 %) eran 4 veces inferiores a las
publicadas por Kraken para cuentas pequeñas (nivel inicial: 0,40 % maker / 0,80 % taker).

## 2. Datos y método

- **Datos:** velas spot de Binance de BTC, ETH, SOL, XRP y DOGE contra USDT, 2017–30/10/2026 (SOL desde 2020, DOGE desde 2019),
  del repositorio público `Speirsy11/crypto-dataset`. Marcos 1 h, 4 h y 1 d.
- **Costes por defecto:** 0,40 % por lado + 0,05 % de deslizamiento (se probaron de 0,10 % a 0,80 %).
- **Ejecución:** la señal se calcula con el cierre de la vela y se ejecuta en la apertura de la siguiente (sin mirar el futuro).
- **Cartera:** una «hucha» independiente por activo (y por ventana en el conjunto), igual tamaño, solo largos, sin apalancamiento.
- **Periodos:** *TRAIN* 2018–2022 (incluye dos mercados bajistas y el máximo de 2021) y *TEST* 2023–oct 2026.

## 3. El marco temporal decide (Donchian, comisión 0,40 %, rentabilidad anual compuesta)

| Estrategia | TRAIN | TEST |
|---|---|---|
| 1 h, ruptura 24/12 velas | −12,7 % | **−50,7 %** |
| 1 h, ruptura 48/24 | +10,2 % | **−21,7 %** |
| 1 h, cruce EMA 20/50 | +11,6 % | **−27,4 %** |
| 1 h, ruptura 168/72 (≈ semanal) | +69,9 % | +14,1 % |
| 4 h, ruptura 60/30 | +55,1 % | +29,2 % |
| 4 h, ruptura 120/60 | +63,3 % | +32,9 % |
| 4 h, cruce EMA 20/50 | +60,0 % | +25,0 % |
| 1 d, ruptura 20/10 | +49,6 % | +17,3 % |
| 1 d, ruptura 55/20 | +62,7 % | +14,6 % |
| 1 d, momentum 90 días | +38,9 % | +9,4 % |
| 1 d, reversión RSI(2) | +5,2 % | +0,7 % |
| *Comprar y mantener los 5 pares* | +37,9 % | +45,8 % |

Cuanto más corto el marco, peor: las operaciones de horas pierden contra los costes en los dos periodos.

## 4. Robustez de los parámetros (4 h, comisión 0,40 %)

Con ventanas de entrada de **60 a 240 velas** (10 a 40 días), todas las combinaciones probadas ganan en TRAIN y en TEST
(TEST: +8 % a +33 % anual según ventana). Por debajo de 60 y por encima de 300 la ventaja se degrada. Por eso la v2 no apuesta a una
ventana, sino que reparte el capital entre cinco: 60, 90, 120, 180 y 240.

## 5. Variantes del conjunto

| Variante | TRAIN anual / caída máx. | TEST anual / caída máx. |
|---|---|---|
| Conjunto, salida N/2 | 62,6 % / −68,6 % | 24,5 % / −47,2 % |
| + stop 6 ATR, riesgo 5 % | 35,7 % / −46,5 % | 17,6 % / −22,5 % |
| **+ stop 4 ATR, riesgo 5 % (v2)** | **45,3 % / −47,4 %** | **20,0 % / −31,3 %** |
| + filtro SMA 200 días | 66,1 % / −66,5 % | 18,9 % / −36,4 % |
| + filtro SMA + stop + trailing 4 ATR | 18,7 % / −11,0 % | 6,7 % / −18,6 % |
| *Comprar y mantener* | 37,9 % / −88,7 % | 45,8 % / −70,1 % |

La última variante tenía el mejor ratio en TRAIN (Sharpe 1,41) pero su ventaja desapareció en TEST: se descartó por eso.

## 6. Sensibilidad a las comisiones (v2, rentabilidad anual en TEST)

| Comisión por lado | Deslizamiento 0,05 % | Deslizamiento 0,15 % |
|---|---|---|
| 0,10 % | 23,6 % | 22,7 % |
| 0,25 % | 21,8 % | 20,9 % |
| **0,40 %** | **20,0 %** | 19,1 % |
| 0,60 % | 17,8 % | 17,0 % |
| 0,80 % (órdenes a mercado) | 15,7 % | 14,9 % |

## 7. Año a año (v2 frente a comprar y mantener los 5 pares)

| Año | v2 | Comprar y mantener |
|---|---|---|
| 2018 | −4,1 % | −72,1 % |
| 2019 | +14,4 % | +0,6 % |
| 2020 | +106,8 % | +172,6 % |
| 2021 | +214,2 % | +2.908,5 % |
| 2022 | −10,3 % | −69,1 % |
| 2023 | +42,9 % | +255,5 % |
| 2024 | +50,6 % | +149,1 % |
| 2025 | −3,8 % | −25,7 % |
| 2026 (hasta 30/10) | +1,0 % | −25,4 % |

## 8. Cómo es vivir con ella

- Unas **149 operaciones al año** repartidas en las 25 huchas (≈ 12 al mes).
- **Gana entre el 36 % y el 38 % de las operaciones.** La operación mediana pierde un 3,5 %; los beneficios salen de pocas operaciones grandes.
- Racha máxima de **21 pérdidas seguidas**.
- **Hasta 1.031 días (≈ 2,8 años) sin nuevo máximo de patrimonio**, y estuvo con una caída superior al 10 % durante el 84 % del periodo.
- En TEST, por activo (rentabilidad anual v2 frente a comprar y mantener): BTC 15 % vs 50 %, ETH 12 % vs 18 %, SOL 27 % vs 82 %,
  XRP 19 % vs 41 %, DOGE 26 % vs 2 %.

## 9. Límites que conviene tener presentes

1. **La selección no fue ciega.** La variante final se eligió comparando resultados en los dos periodos; TEST ya no es una validación
   limpia. La validación honesta es la operativa en papel hacia delante, que empieza ahora.
2. **Dependencia de pocas operaciones enormes.** En TRAIN una sola operación en DOGE (2021) multiplicó por 55. TEST no contiene nada parecido;
   por eso sus cifras (≈ 20 % anual) son más realistas que las de TRAIN (≈ 45 %).
3. **Sesgo de supervivencia:** los cinco activos son los que hoy siguen siendo grandes.
4. **Los datos son de Binance**; los pares USDT de Kraken tienen menos liquidez y precios algo distintos.
5. **Muestra alcista:** 2023–2026 fue un periodo fuerte para cripto. En él la estrategia gana menos que mantener; su valor está en limitar las caídas.
6. Solo posiciones largas: en un mercado bajista largo la estrategia se queda en efectivo y no gana nada.
7. El simulador no modela rechazos de órdenes, tamaños mínimos, caídas del exchange ni huecos de liquidez.
8. El simulador del backtest comprobaba el stop antes que la salida por señal en una misma vela, lo cual es ligeramente
   más pesimista que la realidad. El motor real ejecuta primero la salida por señal (al abrir la vela). Afectó a 2 de 148 operaciones en BTC.

## 10. Lo que no se ha probado

Más activos (ADA, BCH, TRX…), ajuste de tamaño por volatilidad de la cartera, filtros de régimen más sofisticados y posiciones cortas.

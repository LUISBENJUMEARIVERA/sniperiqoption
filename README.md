# AUTOTRADER QUANTUM 10X
### Sistema Autónomo de Trading Cuantitativo y Auto-Aprendizaje para Investigación en Cuenta DEMO

---

## 📌 Visión General del Proyecto

**AutoTrader Quantum 10X** es una plataforma autónoma de trading algorítmico diseñada específicamente para fines de investigación estadística y cuantitativa sobre **IQ Option**, con un bloqueo estricto por software que **únicamente opera en saldo de práctica (PRACTICE / DEMO)**.

El sistema ejecuta en segundo plano un **motor de confluencia simultánea (Ensemble)** que evalúa los últimos 150 puntos y velas del mercado en tiempo real a través de **10 estrategias cuantitativas de alta rentabilidad**. Solo cuando el consenso ponderado confirma una probabilidad igual o superior al **80%**, el disparador autónomo entra al mercado, configurando la ventana temporal de expiración y auditando cada voto de cada indicador en una base de datos local SQLite.

Posteriormente, el módulo de **Auto-Aprendizaje** analiza el histórico forense de operaciones ganadas y perdidas para optimizar los pesos de cada estrategia y generar automáticamente combinaciones de mayor efectividad.

---

## 🚀 Inicio Rápido (1 Clic)

### Opción A: Ejecutable Windows (Doble Clic)
Haz doble clic sobre el archivo:
```bash
run.bat
```
o ejecuta en PowerShell:
```powershell
.\start.ps1
```

El servidor local se levantará en `http://localhost:8000` y abrirá automáticamente tu navegador con el Dashboard en vivo.

---

## 🖥️ Características de la Interfaz Web (Dashboard)

1. **Contadores Forenses en Tiempo Real (Requisito Principal)**:
   - **Operaciones Totales**: Conteo de trades completados en segundo plano.
   - **Operaciones Ganadas (Wins)**: Conteo en verde de operaciones exitosas.
   - **Operaciones Perdidas (Losses)**: Conteo en rojo de operaciones fallidas.
   - **Tasa de Acierto (Win Rate %)**: Indicador visual dinámico con barra de progreso.
   - **Beneficio Demo Neto ($)**: PnL acumulado en cuenta de práctica.
2. **Radar de Confluencia en Vivo**:
   - Medidores de probabilidad instantánea para **CALL** y **PUT**.
   - Marcador visual de umbral del 80%.
   - Alerta con destello dorado cuando una señal alcanza o supera el 80% antes del disparo.
3. **Tabla de las 10 Estrategias**:
   - Peso actual ($W_i$) asignado a cada estrategia.
   - Precisión histórica acumulada (%).
   - Voto de la última vela en tiempo real (▲ CALL / ▼ PUT / Neutro).
4. **Motor de Auto-Aprendizaje ("⚡ ANALIZAR Y OPTIMIZAR AHORA")**:
   - Analiza las operaciones registradas.
   - Premia con mayor peso a las estrategias con mayor tasa de acierto y penaliza los falsos positivos.
   - Descubre y reporta combinaciones sinérgicas (ej: *Bollinger + MACD + EMA Ribbon = 88% Win Rate*).
5. **Historial de Operaciones (SQLite)**:
   - Registro de cada disparo con ID único, timestamp, activo, dirección, probabilidad calculada, resultado y desglose de estrategias que votaron a favor.

---

## 🧠 Las 10 Estrategias Cuantitativas Implementadas

| # | Estrategia | Rol en el Ensamble |
|---|---|---|
| **1** | **Triple EMA Ribbon (9, 21, 55)** | Filtro macro y micro de alineación tendencial tendencial continua |
| **2** | **Dynamic RSI Reversal (14)** | Identificación de agotamiento y giro en zonas sobrecompradas/sobrevendidas |
| **3** | **MACD Momentum Cross (12, 26, 9)** | Detección de aceleración y cruce de histograma sobre la línea cero |
| **4** | **Bollinger Squeeze & Piercing (20, 2)** | Rechazo de reversión a la media tras perforación de bandas extremas |
| **5** | **Stochastic RSI Timing (14, 14, 3, 3)** | Gatillo fino de punto de giro en micro-ciclos |
| **6** | **SuperTrend Volatility (10, 3.0)** | Filtro direccional contra la volatilidad dominante |
| **7** | **Candlestick Price Action** | Reconocimiento de velas Martillo, Estrella Fugaz y Envolventes |
| **8** | **Support & Resistance Pivots** | Rebotes en fractales y zonas de liquidez de precios recientes |
| **9** | **Volume Flow Direction (OBV)** | Validación de inyección de volumen institucional en el movimiento |
| **10** | **ADX Trend Strength (14)** | Filtro anti-rango; descarta operaciones si el mercado está plano ($ADX < 25$) |

$$\text{Probabilidad Concurrente } P(\text{Acción}) = \frac{\sum_{i=1}^{10} W_i \cdot \text{Voto}_i}{\sum W_i} \times 100\% \ge 80\%$$

---

## 🔒 Guardrail de Seguridad: Bloqueo de Cuenta DEMO

Para garantizar la integridad y seguridad de la investigación:
- El cliente fuerza `iq.change_balance("PRACTICE")`.
- Si el WebSocket reporta cualquier balance diferente a `"PRACTICE"`, el hilo detiene inmediatamente la ejecución con una excepción crítica.
- Cuenta con **Modo Simulador Estocástico Local**, permitiendo probar la interfaz, el registro de bases de datos y el optimizador sin necesidad de ingresar credenciales reales o durante fines de semana fuera de horario de mercado.

---

## 📁 Estructura del Código

```
AUTOTRADER/
├── app/
│   ├── config.py         # Configuración y umbrales (80%, PRACTICE)
│   ├── database.py       # Base de datos SQLite, registro de trades y KPIs
│   ├── strategies.py     # Las 10 estrategias matemáticas implementadas
│   ├── ensemble.py       # Ponderador y cálculo de probabilidad concurrente
│   ├── optimizer.py      # Motor de autoaprendizaje y optimización de pesos
│   ├── engine.py         # Motor de fondo, cliente IQ Option y dispatcher
│   └── server.py         # Servidor FastAPI y WebSocket en tiempo real
├── frontend/
│   ├── index.html        # Dashboard visual interactivo
│   ├── style.css         # Estética terminal fintech dark mode
│   └── app.js            # Lógica reactiva de WebSocket y controles
├── data/
│   └── trades.db         # Registro permanente de operaciones y métricas
├── main.py               # Entrada principal del servidor
├── run.bat               # Lanzador directo para Windows
└── start.ps1             # Lanzador PowerShell
```

let socket = null;
let reconnectTimer = null;
let currentStrategies = [];
let currentAccountMode = 'PRACTICE';

const DEFAULT_PAIRS = [
  "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "EURJPY-OTC", 
  "EURGBP-OTC", "USDCHF-OTC", "NZDUSD-OTC", "GBPJPY-OTC", "AUDCAD-OTC",
  "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURJPY", "USDCAD", "XAUUSD", "BTCUSD"
];

// Garantizar que la tarjeta exista en el DOM (crea tarjetas dinámicas con símbolos formateados y payout)
function ensureAssetCardExists(asset) {
  const cardId = `card-${asset}`;
  if (document.getElementById(cardId)) return;

  const grid = document.getElementById('multiAssetsGrid');
  if (!grid) return;

  // Remover mensaje vacío si existe
  const emptyState = document.getElementById('radarEmptyState');
  if (emptyState) emptyState.remove();

  const card = document.createElement('div');
  card.className = 'card-pair-quant';
  card.id = cardId;
  
  let displaySymbol = asset;
  if (asset.includes("-op")) {
    const raw = asset.replace("-op", "");
    displaySymbol = (raw.length === 6) ? `${raw.slice(0, 3)}/${raw.slice(3)}` : raw;
  } else if (asset.includes("-OTC")) {
    const raw = asset.replace("-OTC", "");
    displaySymbol = (raw.length === 6) ? `${raw.slice(0, 3)}/${raw.slice(3)} (OTC)` : `${raw} (OTC)`;
  } else if (asset.length === 6 && !asset.includes("-")) {
    displaySymbol = `${asset.slice(0, 3)}/${asset.slice(3)}`;
  } else if (asset === "XAUUSD") {
    displaySymbol = "🥇 ORO (XAU/USD)";
  } else if (asset === "BTCUSD") {
    displaySymbol = "₿ BITCOIN (BTC/USD)";
  }

  const isJpy = asset.includes("JPY");
  const isBtc = asset.includes("BTC");
  const isXau = asset.includes("XAU");
  const samplePrice = isBtc ? "68,450.00" : (isXau ? "2,748.50" : (isJpy ? "152.300" : "1.08500"));

  card.innerHTML = `
    <div class="pair-card-header">
      <div class="pair-title-box">
        <span class="pair-symbol">${displaySymbol}</span>
        <span class="pair-regime-badge" id="regime-${asset}" style="color: var(--accent-cyan);">🚀 TENDENCIA</span>
        <span class="martingale-active-badge" id="martingale-${asset}" style="display: none;">⚡ RECUPERACIÓN G1</span>
      </div>
      <div class="pair-price-box">
        <span class="pair-price" id="price-${asset}">${samplePrice}</span>
        <span class="pair-time" id="time-${asset}">En vivo</span>
      </div>
    </div>
    <div class="pair-mini-radar">
      <div class="radar-row">
        <div class="radar-row-labels">
          <span class="tag-call">PROBABILIDAD CALL</span>
          <span class="text-green" id="callval-${asset}">0.0%</span>
        </div>
        <div class="radar-track">
          <div class="radar-fill fill-call" id="callbar-${asset}" style="width: 0%;"></div>
          <div class="radar-threshold-mark"></div>
        </div>
      </div>
      <div class="radar-row">
        <div class="radar-row-labels">
          <span class="tag-put">PROBABILIDAD PUT</span>
          <span class="text-red" id="putval-${asset}">0.0%</span>
        </div>
        <div class="radar-track">
          <div class="radar-fill fill-put" id="putbar-${asset}" style="width: 0%;"></div>
          <div class="radar-threshold-mark"></div>
        </div>
      </div>
    </div>
    <div class="pair-card-footer">
      <span class="pair-action-pill" id="action-${asset}" style="background: var(--bg-tertiary); color: var(--text-muted);">EN ESPERA</span>
      <span class="pair-status-text" id="status-${asset}">⏳ Cierre en: 60s</span>
    </div>
  `;
  grid.appendChild(card);
}

// Reloj continuo local garantizado (Sincronizado MTF: 1M + 5M)
function startLocalClock() {
  const updateClock = () => {
    const now = Math.floor(Date.now() / 1000);
    const sec1m = now % 60;
    const rem1m = 60 - sec1m;
    const min5m = Math.floor((now % 300) / 60) + 1;
    const is5mExpansion = (min5m <= 4);
    const isWindow = (rem1m <= 2 || sec1m <= 1);
    
    const clockEl = document.getElementById('dispCandleClock');
    if (clockEl) {
      const secStr = sec1m < 10 ? '0' + sec1m : sec1m;
      clockEl.textContent = `1M: 00:${secStr} (${rem1m}s) • 5M: Min ${min5m}/5`;
      clockEl.className = isWindow ? "meta-value text-green" : "meta-value highlight-cyan";
    }

    // Actualizar estados de cierre en todas las tarjetas activas en pantalla
    document.querySelectorAll('[id^="status-"]').forEach(statusEl => {
      const cardEl = statusEl.closest('.card-pair-quant');
      if (cardEl && !cardEl.classList.contains('triggered')) {
        statusEl.textContent = `⏳ 5M (${min5m}/5) • Cierre 1M: ${rem1m}s`;
      }
    });
  };

  updateClock();
  setInterval(updateClock, 1000);
}

// Cargar credenciales guardadas automáticamente
async function loadSavedCredentials() {
  try {
    const res = await fetch('/api/credentials');
    const data = await res.json();
    if (data.email) {
      const emailEl = document.getElementById('cfgEmail');
      if (emailEl) emailEl.value = data.email;
    }
    if (data.password) {
      const passEl = document.getElementById('cfgPassword');
      if (passEl) passEl.value = data.password;
    }
  } catch (e) {
    console.error("No se pudieron cargar credenciales guardadas:", e);
  }
}

// Inicialización de WebSocket
function initWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${window.location.host}/ws`;

  socket = new WebSocket(wsUrl);

  socket.onopen = () => {
    const st = document.getElementById('socketStatus');
    if (st) {
      st.textContent = "WebSocket: Conectado en tiempo real";
      st.style.color = "var(--accent-green)";
    }
    if (reconnectTimer) clearTimeout(reconnectTimer);
  };

  socket.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      updateDashboard(data);
    } catch (e) {
      console.error("Error parseando WebSocket payload:", e);
    }
  };

  socket.onclose = () => {
    const st = document.getElementById('socketStatus');
    if (st) {
      st.textContent = "WebSocket: Desconectado. Reconectando...";
      st.style.color = "var(--accent-red)";
    }
    reconnectTimer = setTimeout(initWebSocket, 3000);
  };

  socket.onerror = (err) => {
    console.error("WebSocket error:", err);
    socket.close();
  };
}

// Actualizar Dashboard con datos en vivo directamente en el DOM pre-renderizado
function updateDashboard(data) {
  const engine = data.engine || {};
  const kpi = data.kpi || {};

  // Estado del Motor
  const isRunning = (engine.status === "RUNNING");
  const pulseEl = document.getElementById('statusPulse');
  if (pulseEl) pulseEl.className = isRunning ? "pulse-indicator active" : "pulse-indicator";

  const badgeEl = document.getElementById('engineStatusBadge');
  if (badgeEl) {
    badgeEl.textContent = engine.status || "STOPPED";
    badgeEl.className = isRunning ? "meta-value engine-badge running" : "meta-value engine-badge";
  }

  const sidebarStatus = document.getElementById('sidebarEngineStatus');
  if (sidebarStatus) {
    sidebarStatus.textContent = isRunning ? "ACTIVO" : "DETENIDO";
    sidebarStatus.style.color = isRunning ? "var(--accent-green)" : "var(--accent-red)";
  }

  const btnStart = document.getElementById('btnStart');
  const btnStop = document.getElementById('btnStop');
  if (btnStart) btnStart.disabled = isRunning;
  if (btnStop) btnStop.disabled = !isRunning;

  // Metadata & Modo de Cuenta (Demo vs Real)
  const isRealMode = (engine.mode && engine.mode.toUpperCase().includes('REAL')) || currentAccountMode === 'REAL';
  currentAccountMode = isRealMode ? 'REAL' : 'PRACTICE';

  const modeEl = document.getElementById('dispMode');
  if (modeEl) {
    if (isRealMode) {
      modeEl.textContent = "⚠️ CUENTA REAL ⚠️";
      modeEl.className = "meta-value mode-badge-real";
    } else {
      modeEl.textContent = engine.mode || "PRACTICE (DEMO)";
      modeEl.className = "meta-value";
    }
  }

  const btnSwitch = document.getElementById('btnSwitchAccount');
  const btnSwitchText = document.getElementById('btnSwitchAccountText');
  const btnSwitchIcon = document.getElementById('btnSwitchAccountIcon');
  if (btnSwitch) {
    if (isRealMode) {
      btnSwitch.classList.add('in-real-mode');
      if (btnSwitchIcon) btnSwitchIcon.textContent = '🛡️';
      if (btnSwitchText) btnSwitchText.textContent = 'VOLVER A CUENTA DEMO';
    } else {
      btnSwitch.classList.remove('in-real-mode');
      if (btnSwitchIcon) btnSwitchIcon.textContent = '💰';
      if (btnSwitchText) btnSwitchText.textContent = 'OPERAR EN CUENTA REAL';
    }
  }

  const balEl = document.getElementById('dispBalance');
  if (balEl) balEl.textContent = `$${(engine.balance || 0).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;

  // KPIs
  const totalEl = document.getElementById('kpiTotal');
  if (totalEl) totalEl.textContent = kpi.total_trades || 0;

  // Operaciones Abiertas en Vivo
  const activeTrades = data.active_trades || engine.active_trades || [];
  const openCount = activeTrades.length;
  const openEl = document.getElementById('kpiOpen');
  if (openEl) {
    openEl.textContent = openCount;
    openEl.className = openCount > 0 ? "kpi-value highlight-gold" : "kpi-value";
  }
  const openSubEl = document.getElementById('kpiOpenSub');
  if (openSubEl) {
    if (openCount === 0) {
      openSubEl.textContent = "Sin operaciones en curso";
      openSubEl.style.color = "var(--text-muted)";
    } else {
      const activeSymbols = activeTrades.map(t => `${t.asset} (${t.action})`).join(' • ');
      openSubEl.textContent = activeSymbols;
      openSubEl.style.color = "var(--accent-gold)";
    }
  }

  const winsEl = document.getElementById('kpiWins');
  if (winsEl) winsEl.textContent = kpi.wins || 0;

  const lossEl = document.getElementById('kpiLosses');
  if (lossEl) lossEl.textContent = kpi.losses || 0;

  const wrEl = document.getElementById('kpiWinRate');
  if (wrEl) wrEl.textContent = `${(kpi.win_rate || 0).toFixed(1)}%`;

  const wrBar = document.getElementById('winRateBar');
  if (wrBar) wrBar.style.width = `${Math.min(100, kpi.win_rate || 0)}%`;
  
  const profit = kpi.net_profit || 0;
  const profEl = document.getElementById('kpiProfit');
  if (profEl) {
    profEl.textContent = `${profit >= 0 ? '+' : ''}$${profit.toFixed(2)}`;
    profEl.className = `kpi-value ${profit >= 0 ? 'text-green' : 'text-red'}`;
  }

  // Actualizar las tarjetas en tiempo real con el umbral ESTRICTO del usuario
  const userThreshold = Number(document.getElementById('cfgThreshold')?.value) || 80.0;
  
  // Actualizar la marca visual del umbral en todos los radares
  document.querySelectorAll('.radar-threshold-mark').forEach(mark => {
    mark.style.left = `${userThreshold}%`;
  });

  const signalsMap = engine.assets_signals || {};
  const activeAssets = Object.keys(signalsMap);
  const engineAssets = engine.active_assets || [];

  // Actualizar el Radar Autónomo de Pares en el DOM
  const autoListEl = document.getElementById('autoAssetsList');
  if (autoListEl && engineAssets.length > 0) {
    let badgesHtml = '<span style="font-size: 0.72rem; font-family: var(--font-mono); color: #06b6d4; font-weight: 700; margin-right: 0.25rem;">PARES EN RADAR:</span>';
    engineAssets.forEach(a => {
      const sig = signalsMap[a] || {};
      const p = sig.payout ? `${sig.payout.toFixed(0)}%` : '86%';
      badgesHtml += `<span style="background: rgba(6, 182, 212, 0.15); color: #e5e7eb; border: 1px solid rgba(6, 182, 212, 0.4); padding: 0.2rem 0.55rem; border-radius: 4px; font-size: 0.75rem; font-family: var(--font-mono); font-weight: 700;">🟢 ${a} <span style="color: var(--accent-green); font-size: 0.7rem;">(${p})</span></span>`;
    });
    autoListEl.innerHTML = badgesHtml;
  }

  const activeCountEl = document.getElementById('activeAssetsCount');
  if (activeCountEl && (engineAssets.length > 0 || activeAssets.length > 0)) {
    const count = engineAssets.length > 0 ? engineAssets.length : activeAssets.length;
    activeCountEl.textContent = `${count} ACTIVOS ÓPTIMOS EN ESCANEO CONTINUO`;
  }

  // Renderizar exclusivamente los pares identificados activamente por el radar
  const allPairsToRender = engineAssets.length > 0 ? engineAssets : (activeAssets.length > 0 ? activeAssets : []);
  
  // Limpiar cualquier tarjeta que ya no esté en la rotación activa
  if (allPairsToRender.length > 0) {
    document.querySelectorAll('.card-pair-quant').forEach(card => {
      const assetId = card.id.replace('card-', '');
      if (!allPairsToRender.includes(assetId)) {
        card.remove();
      }
    });
  }

  allPairsToRender.forEach(a => ensureAssetCardExists(a));

  allPairsToRender.forEach(asset => {
    const item = signalsMap[asset];
    if (!item) return;

    ensureAssetCardExists(asset);

    const cardEl = document.getElementById(`card-${asset}`);
    const priceEl = document.getElementById(`price-${asset}`);
    const timeEl = document.getElementById(`time-${asset}`);
    const regimeEl = document.getElementById(`regime-${asset}`);
    const callvalEl = document.getElementById(`callval-${asset}`);
    const callbarEl = document.getElementById(`callbar-${asset}`);
    const putvalEl = document.getElementById(`putval-${asset}`);
    const putbarEl = document.getElementById(`putbar-${asset}`);
    const actionEl = document.getElementById(`action-${asset}`);
    const statusEl = document.getElementById(`status-${asset}`);

    const callProb = item.prob_call || 0.0;
    const putProb = item.prob_put || 0.0;
    const dominant = item.dominant_action || 'EN ESPERA';
    const domProb = item.dominant_probability || 0.0;
    const isTriggered = (callProb >= userThreshold || putProb >= userThreshold);

    // Formato de precio según el tipo de activo
    if (priceEl && item.price) {
      if (asset.includes('BTC') || asset.includes('XAU')) {
        priceEl.textContent = Number(item.price).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
      } else if (asset.includes('JPY')) {
        priceEl.textContent = Number(item.price).toFixed(3);
      } else {
        priceEl.textContent = Number(item.price).toFixed(5);
      }
    }

    if (timeEl) {
      const payoutText = item.payout ? ` • ${item.payout.toFixed(0)}% Payout` : '';
      timeEl.textContent = `${item.updated_at || 'En vivo'}${payoutText}`;
    }

    // Régimen y Macro 5M
    if (regimeEl && item.regime) {
      const macroTxt = item.macro_bias ? ` • ${item.macro_bias}` : '';
      if (item.regime === 'TENDENCIAL') {
        regimeEl.innerHTML = `🚀 TENDENCIA${macroTxt}`;
        regimeEl.style.color = 'var(--accent-cyan)';
      } else {
        regimeEl.innerHTML = `🔄 REVERSIÓN${macroTxt}`;
        regimeEl.style.color = 'var(--accent-gold)';
      }
    }

    // Barras de Confluencia
    if (callvalEl) callvalEl.textContent = `${callProb.toFixed(1)}%`;
    if (callbarEl) callbarEl.style.width = `${Math.min(100, callProb)}%`;

    if (putvalEl) putvalEl.textContent = `${putProb.toFixed(1)}%`;
    if (putbarEl) putbarEl.style.width = `${Math.min(100, putProb)}%`;

    // Botón de Acción Dominante
    if (actionEl) {
      if (dominant === 'CERRADO') {
        actionEl.innerHTML = '⏸️ CERRADO';
        actionEl.className = 'pair-action-pill';
        actionEl.style.background = 'rgba(107, 114, 128, 0.2)';
        actionEl.style.color = '#9ca3af';
      } else if (dominant === 'CALL') {
        actionEl.innerHTML = `▲ CALL (${domProb.toFixed(1)}%)`;
        actionEl.className = 'pair-action-pill text-green';
        actionEl.style.background = 'rgba(16, 185, 129, 0.2)';
      } else if (dominant === 'PUT') {
        actionEl.innerHTML = `▼ PUT (${domProb.toFixed(1)}%)`;
        actionEl.className = 'pair-action-pill text-red';
        actionEl.style.background = 'rgba(239, 68, 68, 0.2)';
      } else {
        actionEl.innerHTML = 'EN ESPERA';
        actionEl.className = 'pair-action-pill';
        actionEl.style.background = 'var(--bg-tertiary)';
      }
    }

    // Tarjeta disparada
    if (cardEl) {
      if (dominant === 'CERRADO') {
        cardEl.classList.remove('triggered');
        if (statusEl) {
          statusEl.innerHTML = '⏸️ Mercado no disponible en este horario';
          statusEl.className = 'pair-status-text';
          statusEl.style.color = '#6b7280';
        }
      } else if (isTriggered) {
        cardEl.classList.add('triggered');
        if (statusEl) {
          statusEl.innerHTML = item.is_pullback ? '🎯 ¡SNIPER PULLBACK CONFIRMADO!' : '⚡ ¡DISPARO CONFIRMADO!';
          statusEl.className = 'pair-status-text alert-active';
        }
      } else {
        cardEl.classList.remove('triggered');
        if (statusEl && !statusEl.classList.contains('alert-active')) {
          if (item.is_pullback) {
            statusEl.innerHTML = '🎯 Detectando pullback a EMA9...';
            statusEl.className = 'pair-status-text';
          } else {
            statusEl.className = 'pair-status-text';
          }
        }
      }
    }

    // Badge de Martingala / Recuperación
    const martBadgeEl = document.getElementById(`martingale-${asset}`);
    if (martBadgeEl) {
      const martLvl = item.martingale_level || 0;
      const nextAmt = item.next_amount || 0;
      if (martLvl > 0) {
        martBadgeEl.style.display = 'inline-block';
        martBadgeEl.textContent = `⚡ RECUPERACIÓN G${martLvl} ($${Number(nextAmt).toFixed(2)})`;
      } else {
        martBadgeEl.style.display = 'none';
      }
    }
  });

  if (engine.last_signal && engine.last_signal.votes) {
    renderStrategiesLive(engine.last_signal.votes);
  }

  // Actualizar tabla de historial en tiempo real inmediatamente vía WebSocket
  if (data.trades) {
    renderTradesTable(data.trades);
  }
}

// Cargar métricas de estrategias
async function loadStrategies() {
  try {
    const res = await fetch('/api/strategies');
    currentStrategies = await res.json();
    renderStrategiesLive({});
  } catch (e) {
    console.error("Error cargando estrategias:", e);
  }
}

// Renderizar tabla de 10 estrategias
function renderStrategiesLive(liveVotes) {
  if (!currentStrategies || currentStrategies.length === 0) return;

  const tbody = document.getElementById('strategiesTbody');
  if (!tbody) return;

  let html = '';
  currentStrategies.forEach(s => {
    const vote = liveVotes[s.strategy_name];
    let voteBadge = '<span style="color: var(--text-muted)">Neutro</span>';
    if (vote > 0) {
      voteBadge = `<span class="tag-call">▲ CALL (+${vote.toFixed(1)})</span>`;
    } else if (vote < 0) {
      voteBadge = `<span class="tag-put">▼ PUT (${vote.toFixed(1)})</span>`;
    }

    const pctWeight = (s.weight * 100).toFixed(1);
    const wr = s.accuracy.toFixed(1);
    const wrColor = s.accuracy >= 60 ? 'text-green' : (s.accuracy < 45 ? 'text-red' : 'text-main');

    html += `
      <tr>
        <td><strong>${s.strategy_name}</strong></td>
        <td>${pctWeight}%</td>
        <td>${s.total_signals}</td>
        <td class="text-green">${s.winning_signals}</td>
        <td class="text-red">${s.losing_signals}</td>
        <td class="${wrColor}"><strong>${wr}%</strong></td>
        <td>${voteBadge}</td>
      </tr>
    `;
  });

  tbody.innerHTML = html;
}

// Renderizar tabla de operaciones registradas en tiempo real sin recargar página
function renderTradesTable(trades) {
  if (!trades) return;
  const tbody = document.getElementById('tradesTbody');
  if (!tbody) return;

  let html = '';
  if (trades.length === 0) {
    html = '<tr><td colspan="9" style="text-align:center; color: var(--text-muted); padding: 2rem;">No hay operaciones registradas aún. El motor colocará órdenes automáticamente según el umbral.</td></tr>';
  } else {
    trades.forEach(t => {
      let resBadge = '<span class="badge-pending">⏳ PENDIENTE</span>';
      if (t.result === 'WIN') resBadge = '<span class="badge-win">✅ GANADA</span>';
      if (t.result === 'LOSS') resBadge = '<span class="badge-loss">❌ PERDIDA</span>';
      if (t.result === 'RECOVERED') resBadge = '<span class="badge-pending" style="color:#38bdf8;border-color:#38bdf8;">♻️ RECUPERADA G1</span>';
      if (t.result === 'CYCLE_G0') resBadge = '<span class="badge-pending" style="color:#94a3b8;border-color:#475569;">↪️ PASO G0 (CICLO G1)</span>';
      if (t.result === 'EQUAL') resBadge = '<span class="badge-pending">⚖️ EMPATE</span>';

      const profitStr = t.profit !== 0 ? `${t.profit > 0 ? '+' : ''}$${t.profit.toFixed(2)}` : '$0.00';
      const profitClass = t.profit > 0 ? 'text-green' : (t.profit < 0 ? 'text-red' : '');

      const votes = t.strategy_votes || {};
      const countVotes = Object.values(votes).filter(v => (t.action === 'CALL' && v > 0) || (t.action === 'PUT' && v < 0)).length;

      html += `
        <tr>
          <td><code>#${t.trade_uid}</code></td>
          <td>${t.timestamp}</td>
          <td><strong>${t.asset}</strong></td>
          <td><strong class="${t.action === 'CALL' ? 'tag-call' : 'tag-put'}">${t.action}</strong></td>
          <td>$${Number(t.amount || 0).toFixed(2)}</td>
          <td><strong>${Number(t.probability || 0).toFixed(1)}%</strong></td>
          <td>${resBadge}</td>
          <td class="${profitClass}"><strong>${profitStr}</strong></td>
          <td style="font-size: 0.75rem;">${countVotes}/10 estrategias a favor</td>
        </tr>
      `;
    });
  }

  tbody.innerHTML = html;
}

// Cargar historial de operaciones registradas en SQLite
async function loadTrades() {
  try {
    const res = await fetch('/api/trades?limit=50');
    const trades = await res.json();
    renderTradesTable(trades);
  } catch (e) {
    console.error("Error cargando trades:", e);
  }
}

// Alternar visibilidad de opciones de Martingala
function toggleMartingaleDropdown() {
  const chk = document.getElementById('cfgMartingale');
  const group = document.getElementById('martingaleOptionsGroup');
  if (group) {
    group.style.display = (chk && chk.checked) ? 'flex' : 'none';
  }
}

// Alternar ver/ocultar contraseña
function togglePasswordVisibility() {
  const pInput = document.getElementById('cfgPassword');
  const pBtn = document.getElementById('btnTogglePassword');
  if (!pInput) return;
  if (pInput.type === 'password') {
    pInput.type = 'text';
    if (pBtn) pBtn.textContent = '🔒';
  } else {
    pInput.type = 'password';
    if (pBtn) pBtn.textContent = '👁';
  }
}

// Iniciar Bot
async function handleStart() {
  const btnStart = document.getElementById('btnStart');
  const banner = document.getElementById('connectionBanner');
  
  if (btnStart) {
    btnStart.disabled = true;
    btnStart.textContent = "⏳ Conectando con IQ Option Demo...";
  }

  if (banner) {
    banner.style.display = "flex";
    banner.className = "connection-banner";
    banner.textContent = "Iniciando conexión segura con servidores de IQ Option...";
  }

  const useMartingale = document.getElementById('cfgMartingale')?.checked ?? true;
  const martingaleLevels = parseInt(document.getElementById('cfgMartingaleLevels')?.value || 1);

  const payload = {
    email: document.getElementById('cfgEmail')?.value.trim() || "",
    password: document.getElementById('cfgPassword')?.value || "",
    assets: [], // MODO 100% AUTÓNOMO: El motor descubre y rota los mejores 8-10 pares por payout
    asset: "EURUSD-OTC",
    timeframe_min: parseInt(document.getElementById('cfgTimeframe')?.value || 1),
    amount: parseFloat(document.getElementById('cfgAmount')?.value || 10),
    min_probability: parseFloat(document.getElementById('cfgThreshold')?.value || 80),
    mode: currentAccountMode || "PRACTICE",
    use_simulator: document.getElementById('cfgSim')?.checked || false,
    use_martingale: useMartingale,
    martingale_levels: Math.min(1, martingaleLevels),
    use_adaptive_threshold: false, // Umbral 100% estricto del usuario
    use_confidence_scaling: false,  // Importe 100% estricto del usuario
    use_sniper_pullback: false,     // Entradas estrictamente al cierre de vela confirmada (segundos 58-59 o 00)
    min_payout: (document.getElementById('cfgPayout')?.checked ?? true) ? 80.0 : 0.0
  };

  const threshEl = document.getElementById('threshText');
  if (threshEl) threshEl.innerHTML = `&ge; ${payload.min_probability}%`;

  try {
    const res = await fetch('/api/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const result = await res.json();
    
    if (result.success) {
      if (btnStart) {
        btnStart.disabled = true;
        btnStart.textContent = "▶ INICIAR ESCÁNER EN SEGUNDO PLANO";
      }
      const btnStop = document.getElementById('btnStop');
      if (btnStop) btnStop.disabled = false;

      if (banner) {
        banner.className = "connection-banner success";
        banner.textContent = `✓ ${result.message}`;
      }
    } else {
      throw new Error(result.message || "Error al iniciar");
    }
  } catch (e) {
    if (banner) {
      banner.className = "connection-banner error";
      banner.textContent = `Fallo al iniciar: ${e.message}`;
    }
    if (btnStart) {
      btnStart.disabled = false;
      btnStart.textContent = "▶ INICIAR ESCÁNER EN SEGUNDO PLANO";
    }
  }
}

// Detener Bot
async function handleStop() {
  const banner = document.getElementById('connectionBanner');
  try {
    const res = await fetch('/api/stop', { method: 'POST' });
    const result = await res.json();
    if (result.success) {
      const btnStart = document.getElementById('btnStart');
      const btnStop = document.getElementById('btnStop');
      if (btnStart) btnStart.disabled = false;
      if (btnStop) btnStop.disabled = true;
      if (banner) {
        banner.className = "connection-banner";
        banner.textContent = "⏹ Escáner detenido.";
      }
    }
  } catch (e) {
    alert("Error al detener el motor: " + e.message);
  }
}

// Apagar Servidor y Liberar Puerto 8000
async function handleShutdown() {
  const confirmShutdown = confirm(
    "¿Deseas APAGAR completamente el servidor de AutoTrader y liberar el puerto 8000?\n\n" +
    "El proceso se cerrará limpiamente y no necesitarás usar Ctrl+C en la consola."
  );
  if (!confirmShutdown) return;

  const btnShutdown = document.getElementById('btnShutdown');
  const btnShutdownHeader = document.getElementById('btnShutdownHeader');
  if (btnShutdown) {
    btnShutdown.disabled = true;
    btnShutdown.textContent = "⏳ Apagando servidor...";
  }
  if (btnShutdownHeader) {
    btnShutdownHeader.disabled = true;
    btnShutdownHeader.textContent = "⏳ Apagando...";
  }

  try {
    await fetch('/api/shutdown', { method: 'POST' });
  } catch (e) {
    // Normal: el servidor corta la conexión al salir
  }

  // Cerrar WebSocket si está activo
  if (typeof ws !== 'undefined' && ws) {
    try { ws.close(); } catch (_) {}
  }

  // Mostrar modal de apagado
  const modal = document.getElementById('shutdownModal');
  if (modal) {
    modal.style.display = 'flex';
  } else {
    document.body.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:center;height:100vh;background:#0b0f19;color:#fff;font-family:sans-serif;text-align:center;padding:2rem;">
        <div style="background:#131c2e;padding:3rem;border-radius:12px;border:1px solid #1f293d;max-width:500px;">
          <div style="font-size:3.5rem;margin-bottom:1rem;">🔌</div>
          <h2 style="margin-bottom:0.5rem;color:#f87171;">Servidor Apagado</h2>
          <p style="color:#94a3b8;font-size:1.05rem;line-height:1.6;">El motor se ha detenido y el puerto 8000 ha sido liberado. Ya puedes cerrar esta pestaña.</p>
        </div>
      </div>
    `;
  }
}

// Disparar Motor de Auto-Aprendizaje
async function triggerOptimization() {
  const btn = document.getElementById('btnOptimize');
  const resBox = document.getElementById('optimizationResult');
  
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Analizando correlaciones...";
  }

  try {
    const res = await fetch('/api/optimize', { method: 'POST' });
    const data = await res.json();

    if (resBox) {
      resBox.style.display = "block";

      if (!data.success) {
        resBox.innerHTML = `
          <h4>ℹ️ Información de Optimización</h4>
          <p>${data.message}</p>
        `;
      } else {
        let combosHtml = '';
        if (data.top_combos && data.top_combos.length > 0) {
          combosHtml = '<div style="margin-top: 0.6rem;"><strong>Top Combinaciones Descubiertas:</strong><ul>';
          data.top_combos.forEach(c => {
            combosHtml += `<li><code>${c.combination}</code> &rarr; <strong>${c.win_rate}% Win Rate</strong> (${c.wins}G / ${c.losses}P)</li>`;
          });
          combosHtml += '</ul></div>';
        }

        resBox.innerHTML = `
          <h4>⚡ Conclusiones del Algoritmo de Auto-Aprendizaje</h4>
          <p><strong>Operaciones analizadas:</strong> ${data.trades_analyzed}</p>
          <p>${data.message}</p>
          ${combosHtml}
          <p style="margin-top: 0.5rem; font-size: 0.75rem; color: var(--accent-cyan);">
            ✓ Los nuevos pesos ponderados se han recalculado automáticamente e inyectado al ensamble en tiempo real.
          </p>
        `;

        await loadStrategies();
        await loadTrades();
      }
    }
  } catch (e) {
    alert("Error durante la optimización: " + e.message);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "⚡ ANALIZAR Y OPTIMIZAR AHORA";
    }
  }
}

// ==========================================================
// VISTA Y NAVEGACIÓN: MONITOR EN VIVO VS HISTÓRICO CUANTITATIVO
// ==========================================================
let currentActiveView = 'live';
let currentHistStartDate = null;
let currentHistEndDate = null;
let currentHistPreset = 'all';

function switchView(viewName) {
  currentActiveView = viewName;
  const viewLive = document.getElementById('viewLive');
  const viewHistory = document.getElementById('viewHistory');
  const btnLive = document.getElementById('btnNavLive');
  const btnHistory = document.getElementById('btnNavHistory');

  if (viewName === 'live') {
    if (viewLive) viewLive.style.display = 'flex';
    if (viewHistory) viewHistory.style.display = 'none';
    if (btnLive) btnLive.classList.add('active');
    if (btnHistory) btnHistory.classList.remove('active');
  } else if (viewName === 'history') {
    if (viewLive) viewLive.style.display = 'none';
    if (viewHistory) viewHistory.style.display = 'flex';
    if (btnLive) btnLive.classList.remove('active');
    if (btnHistory) btnHistory.classList.add('active');

    // Cargar análisis histórico acumulado inmediatamente al entrar
    fetchHistoricalAnalysis(currentHistStartDate, currentHistEndDate);
  }
}

// Cargar datos cuantitativos filtrados desde la API persistente SQLite
async function fetchHistoricalAnalysis(startDate = null, endDate = null) {
  try {
    let url = '/api/history';
    const params = [];
    if (startDate) params.push(`start_date=${encodeURIComponent(startDate)}`);
    if (endDate) params.push(`end_date=${encodeURIComponent(endDate)}`);
    if (params.length > 0) {
      url += '?' + params.join('&');
    }

    const res = await fetch(url);
    const data = await res.json();
    const kpi = data.kpi || {};
    const trades = data.trades || [];

    // Renderizar KPIs Históricos
    const histTotalEl = document.getElementById('histKpiTotal');
    if (histTotalEl) histTotalEl.textContent = kpi.total_trades || 0;

    const histWinsEl = document.getElementById('histKpiWins');
    if (histWinsEl) histWinsEl.textContent = kpi.wins || 0;

    const histLossesEl = document.getElementById('histKpiLosses');
    if (histLossesEl) histLossesEl.textContent = kpi.losses || 0;

    const wr = kpi.win_rate || 0.0;
    const histWrEl = document.getElementById('histKpiWinRate');
    if (histWrEl) histWrEl.textContent = `${wr.toFixed(1)}%`;

    const histWrBar = document.getElementById('histWinRateBar');
    if (histWrBar) histWrBar.style.width = `${Math.min(100, wr)}%`;

    const profit = kpi.net_profit || 0.0;
    const histProfitEl = document.getElementById('histKpiProfit');
    if (histProfitEl) {
      histProfitEl.textContent = `${profit >= 0 ? '+' : ''}$${profit.toFixed(2)}`;
      histProfitEl.className = `kpi-value ${profit >= 0 ? 'text-green' : 'text-red'}`;
    }

    // Badge de alcance
    const badgeEl = document.getElementById('histRangeBadge');
    if (badgeEl) {
      if (!startDate && !endDate) {
        badgeEl.textContent = `Histórico Completo Acumulado (${kpi.total_trades || 0} operaciones)`;
      } else {
        const s = startDate || 'Inicio';
        const e = endDate || 'Hoy';
        badgeEl.textContent = `Filtrado: ${s} a ${e} (${kpi.total_trades || 0} operaciones)`;
      }
    }

    // Renderizar tabla forense histórica
    renderHistoricalTradesTable(trades);
  } catch (e) {
    console.error("Error obteniendo análisis histórico:", e);
  }
}

// Renderizar tabla de operaciones históricas
function renderHistoricalTradesTable(trades) {
  const tbody = document.getElementById('histTradesTbody');
  if (!tbody) return;

  if (!trades || trades.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" style="text-align: center; color: var(--text-muted); padding: 2.5rem;">No se encontraron operaciones registradas en el período o rango seleccionado.</td></tr>';
    return;
  }

  let html = '';
  trades.forEach(t => {
    let resBadge = '<span class="badge-pending">⏳ PENDIENTE</span>';
    if (t.result === 'WIN') resBadge = '<span class="badge-win">✅ GANADA</span>';
    if (t.result === 'LOSS') resBadge = '<span class="badge-loss">❌ PERDIDA</span>';
    if (t.result === 'RECOVERED') resBadge = '<span class="badge-pending" style="color:#38bdf8;border-color:#38bdf8;">♻️ RECUPERADA G1</span>';
    if (t.result === 'CYCLE_G0') resBadge = '<span class="badge-pending" style="color:#94a3b8;border-color:#475569;">↪️ PASO G0 (CICLO G1)</span>';
    if (t.result === 'EQUAL') resBadge = '<span class="badge-pending">⚖️ EMPATE</span>';

    const profitStr = t.profit !== 0 ? `${t.profit > 0 ? '+' : ''}$${t.profit.toFixed(2)}` : '$0.00';
    const profitClass = t.profit > 0 ? 'text-green' : (t.profit < 0 ? 'text-red' : '');

    const votes = t.strategy_votes || {};
    const countVotes = Object.values(votes).filter(v => (t.action === 'CALL' && v > 0) || (t.action === 'PUT' && v < 0)).length;

    html += `
      <tr>
        <td><code>#${t.trade_uid}</code></td>
        <td>${t.timestamp}</td>
        <td><strong>${t.asset}</strong></td>
        <td><strong class="${t.action === 'CALL' ? 'tag-call' : 'tag-put'}">${t.action}</strong></td>
        <td>$${Number(t.amount || 0).toFixed(2)}</td>
        <td><strong>${Number(t.probability || 0).toFixed(1)}%</strong></td>
        <td>${resBadge}</td>
        <td class="${profitClass}"><strong>${profitStr}</strong></td>
        <td style="font-size: 0.75rem;">${countVotes}/10 estrategias a favor</td>
      </tr>
    `;
  });

  tbody.innerHTML = html;
}

// Formatear Date a YYYY-MM-DD local
function formatDateToLocal(d) {
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

// Aplicar filtros rápidos de fecha
function applyQuickHistoryFilter(preset) {
  currentHistPreset = preset;
  
  // Actualizar estilos activos de los chips
  ['chipAll', 'chipToday', 'chipYesterday', 'chip7d', 'chip30d'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.remove('active');
  });

  const now = new Date();
  const inputStart = document.getElementById('histStartDate');
  const inputEnd = document.getElementById('histEndDate');

  if (preset === 'all') {
    const activeChip = document.getElementById('chipAll');
    if (activeChip) activeChip.classList.add('active');
    if (inputStart) inputStart.value = '';
    if (inputEnd) inputEnd.value = '';
    currentHistStartDate = null;
    currentHistEndDate = null;
    fetchHistoricalAnalysis(null, null);
  } else if (preset === 'today') {
    const activeChip = document.getElementById('chipToday');
    if (activeChip) activeChip.classList.add('active');
    const todayStr = formatDateToLocal(now);
    if (inputStart) inputStart.value = todayStr;
    if (inputEnd) inputEnd.value = todayStr;
    currentHistStartDate = todayStr;
    currentHistEndDate = todayStr;
    fetchHistoricalAnalysis(todayStr, todayStr);
  } else if (preset === 'yesterday') {
    const activeChip = document.getElementById('chipYesterday');
    if (activeChip) activeChip.classList.add('active');
    const yest = new Date();
    yest.setDate(now.getDate() - 1);
    const yestStr = formatDateToLocal(yest);
    if (inputStart) inputStart.value = yestStr;
    if (inputEnd) inputEnd.value = yestStr;
    currentHistStartDate = yestStr;
    currentHistEndDate = yestStr;
    fetchHistoricalAnalysis(yestStr, yestStr);
  } else if (preset === '7d') {
    const activeChip = document.getElementById('chip7d');
    if (activeChip) activeChip.classList.add('active');
    const past7 = new Date();
    past7.setDate(now.getDate() - 7);
    const startStr = formatDateToLocal(past7);
    const endStr = formatDateToLocal(now);
    if (inputStart) inputStart.value = startStr;
    if (inputEnd) inputEnd.value = endStr;
    currentHistStartDate = startStr;
    currentHistEndDate = endStr;
    fetchHistoricalAnalysis(startStr, endStr);
  } else if (preset === '30d') {
    const activeChip = document.getElementById('chip30d');
    if (activeChip) activeChip.classList.add('active');
    const past30 = new Date();
    past30.setDate(now.getDate() - 30);
    const startStr = formatDateToLocal(past30);
    const endStr = formatDateToLocal(now);
    if (inputStart) inputStart.value = startStr;
    if (inputEnd) inputEnd.value = endStr;
    currentHistStartDate = startStr;
    currentHistEndDate = endStr;
    fetchHistoricalAnalysis(startStr, endStr);
  }
}

// Filtrar por fechas manuales
function handleFilterCustomDates() {
  const inputStart = document.getElementById('histStartDate');
  const inputEnd = document.getElementById('histEndDate');
  const startVal = inputStart ? inputStart.value : null;
  const endVal = inputEnd ? inputEnd.value : null;

  ['chipAll', 'chipToday', 'chipYesterday', 'chip7d', 'chip30d'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.remove('active');
  });

  currentHistStartDate = startVal || null;
  currentHistEndDate = endVal || null;
  fetchHistoricalAnalysis(currentHistStartDate, currentHistEndDate);
}

function reloadCurrentHistoryView() {
  fetchHistoricalAnalysis(currentHistStartDate, currentHistEndDate);
}

// ==========================================================
// CONTROL DE CUENTA DEMO VS CUENTA REAL CON CONFIRMACIÓN
// ==========================================================
function handleAccountModeClick() {
  if (currentAccountMode === 'REAL') {
    // Si ya está en real, cambiar a demo directamente
    switchAccountMode('PRACTICE');
  } else {
    // Si está en demo, mostrar modal de advertencia obligatoria
    const modal = document.getElementById('realAccountModal');
    if (modal) modal.style.display = 'flex';
  }
}

function cancelRealAccountModal() {
  const modal = document.getElementById('realAccountModal');
  if (modal) modal.style.display = 'none';
  // El bot permanece activo pero en DEMO
  console.log(">> Usuario canceló paso a cuenta real. Bot continúa en DEMO.");
}

async function confirmSwitchToReal() {
  const modal = document.getElementById('realAccountModal');
  if (modal) modal.style.display = 'none';
  // El bot entra en CUENTA REAL
  await switchAccountMode('REAL');
}

async function switchAccountMode(targetMode) {
  try {
    const res = await fetch('/api/account_mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: targetMode })
    });
    const result = await res.json();
    if (result.success) {
      currentAccountMode = targetMode;
      const isReal = (targetMode === 'REAL');
      
      const modeEl = document.getElementById('dispMode');
      if (modeEl) {
        modeEl.textContent = isReal ? "⚠️ CUENTA REAL ⚠️" : "PRACTICE (DEMO)";
        if (isReal) modeEl.classList.add('mode-badge-real');
        else modeEl.classList.remove('mode-badge-real');
      }

      const btnSwitch = document.getElementById('btnSwitchAccount');
      const btnSwitchText = document.getElementById('btnSwitchAccountText');
      const btnSwitchIcon = document.getElementById('btnSwitchAccountIcon');
      if (btnSwitch) {
        if (isReal) {
          btnSwitch.classList.add('in-real-mode');
          if (btnSwitchIcon) btnSwitchIcon.textContent = '🛡️';
          if (btnSwitchText) btnSwitchText.textContent = 'VOLVER A CUENTA DEMO';
        } else {
          btnSwitch.classList.remove('in-real-mode');
          if (btnSwitchIcon) btnSwitchIcon.textContent = '💰';
          if (btnSwitchText) btnSwitchText.textContent = 'OPERAR EN CUENTA REAL';
        }
      }

      if (isReal) {
        alert("⚠️ ATENCIÓN: El bot ahora está operando en CUENTA REAL. Las órdenes se ejecutarán con saldo real.");
      } else {
        alert("🛡️ MODO DEMO: El bot ahora está operando con saldo de práctica (DEMO).");
      }
    } else {
      alert("Error al cambiar de cuenta: " + (result.message || "Error desconocido"));
    }
  } catch (e) {
    alert("Error de conexión al cambiar de cuenta: " + e.message);
  }
}

// ==========================================================
// BLANQUEAR HISTORIAL DE OPERACIONES (REINICIO LIMPIO)
// ==========================================================
async function handleClearHistory() {
  const confirmed = confirm(
    "¿Deseas BLANQUEAR completamente el histórico de operaciones?\n\n" +
    "Esta acción restablecerá el registro de operaciones, los aciertos y fallos a cero para que la investigación estadística no se vea afectada por sesiones previas."
  );
  if (!confirmed) return;

  try {
    const res = await fetch('/api/clear_history', { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      alert("✓ " + data.message);
      // Refrescar paneles inmediatamente
      await loadTrades();
      await loadStrategies();
      if (currentActiveView === 'history') {
        reloadCurrentHistoryView();
      }
      // Resetear visualmente KPIs en pantalla
      ['kpiTotal', 'kpiWins', 'kpiLosses', 'kpiOpen'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.textContent = '0';
      });
      const wrEl = document.getElementById('kpiWinRate');
      if (wrEl) wrEl.textContent = '0.0%';
      const pEl = document.getElementById('kpiProfit');
      if (pEl) {
        pEl.textContent = '$0.00';
        pEl.className = 'kpi-value';
      }
    }
  } catch (e) {
    alert("Error blanqueando histórico: " + e.message);
  }
}

// Inicialización segura sin depender de que DOMContentLoaded haya pasado
function initApp() {
  startLocalClock();
  loadSavedCredentials();
  initWebSocket();
  loadStrategies();
  loadTrades();

  // Asegurar que Reentradas de Recuperación estén activas por defecto
  const cfgM = document.getElementById('cfgMartingale');
  if (cfgM) {
    cfgM.checked = true;
    toggleMartingaleDropdown();
  }

  // Enlace directo de eventos
  const bStart = document.getElementById('btnStart');
  if (bStart) bStart.onclick = handleStart;

  const bStop = document.getElementById('btnStop');
  if (bStop) bStop.onclick = handleStop;

  const bOpt = document.getElementById('btnOptimize');
  if (bOpt) bOpt.onclick = triggerOptimization;
}

window.handleStart = handleStart;
window.handleStop = handleStop;
window.triggerOptimization = triggerOptimization;
window.toggleMartingaleDropdown = toggleMartingaleDropdown;
window.togglePasswordVisibility = togglePasswordVisibility;
window.switchView = switchView;
window.applyQuickHistoryFilter = applyQuickHistoryFilter;
window.handleFilterCustomDates = handleFilterCustomDates;
window.reloadCurrentHistoryView = reloadCurrentHistoryView;
window.handleAccountModeClick = handleAccountModeClick;
window.cancelRealAccountModal = cancelRealAccountModal;
window.confirmSwitchToReal = confirmSwitchToReal;
window.handleClearHistory = handleClearHistory;

// Ejecución segura sin depender de que DOMContentLoaded haya pasado
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initApp);
} else {
  initApp();
}

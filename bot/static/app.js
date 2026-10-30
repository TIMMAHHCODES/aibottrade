/* Solana Bot dashboard — vanilla JS, no dependencies.
 *
 * Polls /api/state every 2s. Everything else loads on demand. All mutating
 * calls send Content-Type: application/json, which is what makes the
 * server's cross-origin defence work (a JSON POST forces a preflight that
 * fails, because no Access-Control-Allow-Origin is ever sent).
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const POLL_MS = 2000;

  const ui = {
    schema: null,
    tab: 'settings',
    tradesLoaded: false,
    seenError: null,
    pendingMode: null,
  };

  // ── helpers ────────────────────────────────────────────────────────

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    });
    let body = null;
    try { body = await res.json(); } catch (_) { /* non-JSON error body */ }
    if (!res.ok) {
      throw new Error((body && body.error) || `HTTP ${res.status}`);
    }
    return body;
  }

  const post = (path, payload) =>
    api(path, { method: 'POST', body: JSON.stringify(payload) });

  const num = (n) =>
    n === null || n === undefined || Number.isNaN(Number(n))
      ? null
      : Number(n);

  function fmt(n, dp = 2) {
    const v = num(n);
    if (v === null) return '—';
    return v.toLocaleString(undefined, {
      minimumFractionDigits: dp,
      maximumFractionDigits: dp,
    });
  }

  function fmtSigned(n, dp = 4) {
    const v = num(n);
    if (v === null) return '—';
    const s = fmt(Math.abs(v), dp);
    return (v > 0 ? '+' : v < 0 ? '−' : '') + s;
  }

  const sign = (n) => {
    const v = num(n);
    return v === null || v === undefined ? 'neutral' : v > 0 ? 'pos' : v < 0 ? 'neg' : 'neutral';
  };

  // Everything rendered from the API goes through this. The data is our
  // own, but escaping on output is cheap and removes a whole class of bug.
  const esc = (s) =>
    String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  function dur(seconds) {
    if (seconds === null || seconds === undefined) return '—';
    let s = Math.max(0, Math.round(seconds));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    s %= 60;
    if (m < 60) return `${m}m ${s}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
  }

  function ago(iso) {
    if (!iso) return '—';
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return '—';
    return dur((Date.now() - t) / 1000) + ' ago';
  }

  function shortSig(sig) {
    if (!sig) return '<span class="muted">—</span>';
    return `<span class="sig-sm" title="${esc(sig)}">${esc(sig.slice(0, 10))}…</span>`;
  }

  let toastTimer = null;
  function toast(message, kind = '') {
    const old = document.querySelector('.toast');
    if (old) old.remove();
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.textContent = message;
    document.body.appendChild(el);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.remove(), 4200);
  }

  function alertBox(kind, message) {
    return `<div class="alert ${kind}">${esc(message)}</div>`;
  }

  // ── modal ──────────────────────────────────────────────────────────

  function confirmDialog({ title, body, phrase = null, okLabel = 'Confirm' }) {
    return new Promise((resolve) => {
      const modal = $('modal');
      const input = $('modalPhrase');
      const ok = $('modalOk');

      $('modalTitle').textContent = title;
      $('modalBody').textContent = body;
      $('modalOk').textContent = okLabel;
      $('modalPhraseWrap').classList.toggle('hidden', !phrase);
      $('modalPhraseText').textContent = phrase || '';

      input.value = '';
      ok.disabled = !!phrase;
      modal.classList.remove('hidden');
      if (phrase) input.focus();

      const done = (result) => {
        modal.classList.add('hidden');
        ok.onclick = null;
        cancel.onclick = null;
        input.oninput = null;
        input.onkeydown = null;
        resolve(result);
      };

      const cancel = $('modalCancel');
      ok.onclick = () => done(true);
      cancel.onclick = () => done(false);
      input.oninput = () => { ok.disabled = input.value !== phrase; };
      input.onkeydown = (e) => {
        if (e.key === 'Enter' && input.value === phrase) done(true);
        if (e.key === 'Escape') done(false);
      };
      modal.onclick = (e) => { if (e.target === modal) done(false); };
    });
  }

  // ── state rendering ────────────────────────────────────────────────

  function render(s) {
    const alerts = [];
    if (s.configError) {
      alerts.push(alertBox('error', `Settings were rejected: ${s.configError}`));
    }
    if (s.cycle.error) {
      alerts.push(alertBox('warn', `Last cycle failed: ${s.cycle.error}`));
    }
    if (!s.running) {
      alerts.push(alertBox('info',
        'The trading loop is not running. The dashboard can change settings, ' +
        'but nothing will trade until you start it with  python main.py run.'));
    }
    $('alerts').innerHTML = alerts.join('');

    // header
    $('pairLabel').textContent =
      `${s.pair.base} / ${s.pair.quote} · ${s.pair.timeframe}m candles`;

    const modeBtn = $('modeBtn');
    if (s.mode === 'live') {
      modeBtn.textContent = 'LIVE — switch to dry run';
      modeBtn.className = 'btn btn-danger';
    } else {
      modeBtn.textContent = 'DRY RUN — go live';
      modeBtn.className = 'btn';
    }
    $('liveBanner').classList.toggle('hidden', s.mode !== 'live');

    const runBadge = $('runBadge');
    runBadge.textContent = s.running ? 'RUNNING' : 'IDLE';
    runBadge.className = 'badge ' + (s.running ? 'on' : 'warn');

    const kill = $('killBtn');
    if (s.risk.killSwitch) {
      kill.textContent = `KILL SWITCH ON (${s.risk.killSwitchFile})`;
      kill.classList.add('engaged');
      kill.onclick = releaseKill;
    } else {
      kill.textContent = 'Kill switch';
      kill.classList.remove('engaged');
      kill.onclick = engageKill;
    }

    // headline cards
    $('price').textContent = fmt(s.price, 2);
    $('priceAsOf').textContent = s.priceAt ? `· ${ago(s.priceAt)}` : '';
    $('pricePair').textContent = `${s.pair.quote} per ${s.pair.base}`;

    const sig = $('signal');
    sig.textContent = s.signal || '—';
    sig.className = 'card-value signal ' + (s.signal ? `sig-${s.signal}` : '');
    $('signalSub').textContent = s.cycle.lastAt
      ? `last cycle ${ago(s.cycle.lastAt)}`
      : 'waiting for first cycle';

    const rsi = num(s.indicators && s.indicators.rsi);
    $('rsi').textContent = rsi === null ? '—' : fmt(rsi, 1);
    const fill = $('rsiFill');
    fill.style.width = rsi === null ? '0%' : `${Math.min(100, Math.max(0, rsi))}%`;
    fill.className = 'meter-fill' + (rsi > 70 ? ' full' : rsi < 30 ? ' warn' : '');

    if (s.indicators) {
      const up = s.indicators.emaFast > s.indicators.emaSlow;
      $('emaTrend').textContent = up ? 'BULLISH' : 'BEARISH';
      $('emaTrend').className = 'card-value ' + (up ? 'pos' : 'neg');
      $('emaSub').textContent =
        `fast ${fmt(s.indicators.emaFast, 4)} / slow ${fmt(s.indicators.emaSlow, 4)}`;
    }

    $('unreal').textContent = fmtSigned(s.pnl.unrealized, 4);
    $('unreal').className = 'card-value ' + sign(s.pnl.unrealized);
    $('unrealSub').textContent =
      `${s.positions.length} open · ${s.pair.quote}`;

    $('real').textContent = fmtSigned(s.pnl.realized, 4);
    $('real').className = 'card-value ' + sign(s.pnl.realized);

    // risk strip
    $('dailySpend').textContent =
      `${fmt(s.risk.dailySpent, 2)} / ${fmt(s.risk.dailyCap, 2)} ${s.pair.quote}`;
    const pct = s.risk.dailyCap > 0 ? (s.risk.dailySpent / s.risk.dailyCap) * 100 : 0;
    const dFill = $('dailyFill');
    dFill.style.width = `${Math.min(100, pct)}%`;
    dFill.className = 'meter-fill' + (pct >= 100 ? ' full' : pct >= 70 ? ' warn' : '');

    $('openLots').textContent = `${s.risk.openCount} / ${s.risk.maxOpen}`;
    $('tradeSize').textContent = `${fmt(s.risk.tradeSize, 2)} ${s.pair.quote}`;
    $('tpsl').textContent = `${fmt(s.risk.takeProfitPct, 1)}% / ${fmt(s.risk.stopLossPct, 1)}%`;
    $('nextCandle').textContent =
      s.cycle.nextIn === null ? (s.running ? '—' : 'idle') : dur(s.cycle.nextIn);
    $('lastCycle').textContent =
      `${ago(s.cycle.lastAt)}${s.cycle.duration ? ` · ${s.cycle.duration}s` : ''}`;

    // closed-trade statistics
    const st = s.stats || {};
    $('stTrades').textContent = st.trades || 0;
    if (st.winRate === null || st.winRate === undefined) {
      $('stWinRate').textContent = '—';
      $('stWinFill').style.width = '0%';
      $('stWL').textContent = '—';
    } else {
      $('stWinRate').textContent = `${fmt(st.winRate, 1)}%`;
      const wFill = $('stWinFill');
      wFill.style.width = `${Math.min(100, Math.max(0, st.winRate))}%`;
      wFill.className = 'meter-fill' + (st.winRate >= 50 ? '' : ' warn');
      $('stWL').textContent = `${st.wins} / ${st.losses}`;
    }
    const avgPnl = st.avgPnl;
    const avgEl = $('stAvg');
    avgEl.textContent = (avgPnl === null || avgPnl === undefined)
      ? '—'
      : `${fmtSigned(avgPnl, 4)} ${s.pair.quote}`;
    avgEl.className = 'strip-value ' + sign(avgPnl);
    $('stBest').textContent =
      (st.bestPnl === null || st.bestPnl === undefined)
        ? '—' : fmtSigned(st.bestPnl, 4);
    $('stWorst').textContent =
      (st.worstPnl === null || st.worstPnl === undefined)
        ? '—' : fmtSigned(st.worstPnl, 4);

    renderPositions(s);
    refreshFormValues(s.settings);

    // A rejected reload surfaces once, not on every poll.
    const key = s.configError || null;
    if (key && key !== ui.seenError) toast('Settings rejected: ' + key, 'err');
    ui.seenError = key;
  }

  function renderPositions(s) {
    const host = $('positions');
    $('posCount').textContent = s.positions.length
      ? `${s.positions.length} open`
      : '';

    if (!s.positions.length) {
      host.className = 'empty';
      host.textContent = s.mode === 'live'
        ? 'No open positions.'
        : 'No open positions. (Dry run: entries are simulated, not executed.)';
      return;
    }

    const rows = s.positions.map((p) => `
      <tr>
        <td>${esc(p.id.slice(0, 14))}
          <span class="tag ${p.signature ? 'live' : 'dry'}">${p.signature ? 'live' : 'dry'}</span></td>
        <td>${dur(p.ageSeconds)}</td>
        <td>${fmt(p.baseAmount, 6)}</td>
        <td>${fmt(p.entryPrice, 4)}</td>
        <td>${fmt(p.currentPrice, 4)}</td>
        <td>${fmt(p.value, 4)}</td>
        <td class="${sign(p.pnlQuote)}">${fmtSigned(p.pnlQuote, 4)}</td>
        <td class="${sign(p.pnlPct)}">${fmtSigned(p.pnlPct, 2)}%</td>
        <td class="pos">${fmt(p.takeProfitPrice, 4)}</td>
        <td class="neg">${fmt(p.stopLossPrice, 4)}</td>
      </tr>`).join('');

    host.className = '';
    host.innerHTML = `
      <table>
        <thead><tr>
          <th>Lot</th><th>Age</th><th>Amount</th><th>Entry</th><th>Current</th>
          <th>Value</th><th>PnL</th><th>PnL %</th><th>TP @</th><th>SL @</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  // ── settings form ──────────────────────────────────────────────────

  async function loadSchema() {
    if (ui.schema) return;
    const data = await api('/api/schema');
    ui.schema = data.schema;
    ui.originalValues = { ...data.values };

    $('pairHelp').textContent =
      'Locked while running. Changing the pair would orphan positions ' +
      'denominated in the old token — edit .env and restart to do that.';
    $('pairFields').innerHTML = data.schema
      .filter((f) => f.group === 'Pair')
      .map(fieldHtml).join('');

    const groups = {};
    data.schema
      .filter((f) => f.group !== 'Pair')
      .forEach((f) => { (groups[f.group] = groups[f.group] || []).push(f); });

    $('settingsGroups').innerHTML = Object.entries(groups)
      .map(([name, fields]) => `
        <div class="set-group">
          <h3>${esc(name)}</h3>
          <div class="field-grid">${fields.map(fieldHtml).join('')}</div>
        </div>`)
      .join('');

    fillForm(data.values);
    bindForm();
  }

  function fieldHtml(f) {
    const id = 'f_' + f.key;
    let control;
    if (f.kind === 'choice') {
      control = `<select id="${id}" data-key="${f.key}" ${f.readOnly ? 'disabled' : ''}>
        ${f.choices.map((c) =>
          `<option value="${esc(c.value)}">${esc(c.label)}</option>`).join('')}
      </select>`;
    } else {
      const type = f.kind === 'int' || f.kind === 'float' ? 'number' : 'text';
      control = `<input id="${id}" data-key="${f.key}" type="${type}"
        ${f.min !== null && f.min !== undefined ? `min="${f.min}"` : ''}
        ${f.max !== null && f.max !== undefined ? `max="${f.max}"` : ''}
        ${f.step ? `step="${f.step}"` : ''}
        ${f.readOnly ? 'readonly' : ''}
        autocomplete="off" spellcheck="false">`;
    }
    return `
      <div class="field" data-field="${f.key}">
        <label for="${id}">${esc(f.label)}${f.unit ? `<span class="unit">${esc(f.unit)}</span>` : ''}</label>
        ${control}
        ${f.help ? `<div class="help">${esc(f.help)}</div>` : ''}
      </div>`;
  }

  /** Baseline each field against the last value the server confirmed. */
  function syncDirty(el) {
    const original = ui.originalValues ? ui.originalValues[el.dataset.key] : undefined;
    const dirty = String(el.value) !== String(original ?? '');
    el.closest('.field').classList.toggle('dirty', dirty);
  }

  /**
   * Fill every field. Used once, after schema load, when nothing can be
   * mid-edit yet.
   */
  function fillForm(values) {
    if (!ui.schema || !values) return;
    document.querySelectorAll('[data-key]').forEach((el) => {
      if (values[el.dataset.key] === undefined) return;
      el.value = values[el.dataset.key];
      syncDirty(el);
    });
  }

  /**
   * Re-sync from server state on every poll, but never clobber what the
   * user is typing: dirty fields are left exactly as they are.
   */
  function refreshFormValues(values) {
    if (!ui.schema || !values) return;
    document.querySelectorAll('[data-key]').forEach((el) => {
      const field = el.closest('.field');
      if (field.classList.contains('dirty')) return;
      if (values[el.dataset.key] === undefined) return;
      el.value = values[el.dataset.key];
      ui.originalValues[el.dataset.key] = values[el.dataset.key];
    });
  }

  function bindForm() {
    document.querySelectorAll('[data-key]').forEach((el) => {
      const handler = () => syncDirty(el);
      el.addEventListener('input', handler);
      el.addEventListener('change', handler);
    });
  }

  function collectChanges() {
    const changes = {};
    document.querySelectorAll('#settingsGroups [data-key]').forEach((el) => {
      const key = el.dataset.key;
      const original = ui.originalValues ? ui.originalValues[key] : undefined;
      if (String(el.value) === String(original ?? '')) return;
      changes[key] = String(el.value).trim();
    });
    return changes;
  }

  async function saveSettings() {
    const changes = collectChanges();
    if (!Object.keys(changes).length) {
      toast('Nothing changed.');
      return;
    }
    const btn = $('saveBtn');
    btn.disabled = true;
    try {
      const res = await post('/api/settings', { changes });
      ui.originalValues = { ...ui.originalValues, ...res.values };
      document.querySelectorAll('.field.dirty').forEach((f) => f.classList.remove('dirty'));
      toast(`Saved ${Object.keys(changes).length} setting(s).`, 'ok');
      await poll();
    } catch (err) {
      // The server already rolled .env back; just tell them why.
      toast(err.message, 'err');
    } finally {
      btn.disabled = false;
    }
  }

  // ── trades & log ───────────────────────────────────────────────────

  async function loadTrades() {
    const data = await api('/api/trades?limit=500');
    const host = $('trades');
    if (!data.trades.length) {
      host.className = 'empty';
      host.textContent = 'No trades yet.';
      return;
    }
    const rows = data.trades.map((t) => {
      const isBuy = t.side === 'BUY';
      const amount = isBuy ? t.quote_spent : t.quote_received;
      const pnl = isBuy ? null : t.pnl_quote;
      const pct = isBuy ? null : t.pnl_pct;
      return `<tr>
        <td>${esc((t.logged_at || '').replace('T', ' ').slice(0, 19))}</td>
        <td><span class="tag ${esc(t.side)}">${esc(t.side)}</span></td>
        <td><span class="tag ${t.mode === 'live' ? 'live' : 'dry'}">${t.mode === 'live' ? 'LIVE' : 'DRY'}</span></td>
        <td>${fmt(amount, 4)} ${esc(t.quote_symbol || '')}</td>
        <td class="${sign(pnl)}">${pnl === null ? '—' : fmtSigned(pnl, 4)}</td>
        <td class="${sign(pct)}">${pct === null ? '—' : fmtSigned(pct, 2) + '%'}</td>
        <td>${isBuy ? `<span class="muted">entry @ ${fmt(t.entry_price, 4)}</span>`
                    : esc(t.exit_reason || '—')}</td>
        <td>${shortSig(t.signature)}</td>
      </tr>`;
    }).join('');

    host.className = '';
    host.innerHTML = `
      <table>
        <thead><tr>
          <th>Time (UTC)</th><th>Side</th><th>Mode</th><th>Amount</th>
          <th>PnL</th><th>PnL %</th><th>Detail</th><th>Signature</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
    ui.tradesLoaded = true;
  }

  async function loadLog() {
    const data = await api('/api/log?lines=300');
    const html = data.lines.map((line) => {
      const m = line.match(/\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b/);
      const lvl = m ? m[1] : 'INFO';
      return `<span class="lvl-${lvl}">${esc(line)}</span>`;
    }).join('\n');
    const view = $('logView');
    view.innerHTML = html || '<span class="muted">(log is empty)</span>';
    view.scrollTop = view.scrollHeight;
  }

  // ── chart ──────────────────────────────────────────────────────────
  //
  // Rendered with TradingView's lightweight-charts (MIT, loaded from a CDN).
  // The library is optional: if it fails to load the rest of the dashboard
  // keeps working and the chart panel explains itself.

  const chartState = {
    chart: null,
    candlesSeries: null,
    emaFastSeries: null,
    emaSlowSeries: null,
    payload: null,
    trades: [],
    loading: false,
    timeframe: 15,
  };

  const chartAvailable = () => typeof window.LightweightCharts !== 'undefined';

  function chartMsg(text) {
    const el = $('chartMsg');
    if (!el) return;
    el.textContent = text || '';
    el.classList.toggle('hidden', !text);
  }

  function initChart() {
    if (!chartAvailable()) return false;
    if (chartState.chart) return true;

    const el = $('chart');
    if (!el) return false;

    // CrosshairMode must be read from the library: hard-coding 0/1 would
    // silently break if the enum ever changed.
    const CrosshairMode = window.LightweightCharts.CrosshairMode || {};

    chartState.chart = window.LightweightCharts.createChart(el, {
      autoSize: true,
      layout: {
        background: { color: 'transparent' },
        textColor: getComputedStyle(document.body).getPropertyValue('--muted').trim()
          || '#8b93a1',
      },
      grid: {
        vertLines: { color: 'rgba(255,255,255,0.05)' },
        horzLines: { color: 'rgba(255,255,255,0.05)' },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: 'rgba(255,255,255,0.08)' },
      timeScale: { borderColor: 'rgba(255,255,255,0.08)' },
      localization: { locale: undefined },
    });

    chartState.candlesSeries = chartState.chart.addCandlestickSeries({
      upColor: '#26a69a',
      downColor: '#ef5350',
      borderVisible: false,
      wickUpColor: '#26a69a',
      wickDownColor: '#ef5350',
    });
    chartState.emaFastSeries = chartState.chart.addLineSeries({
      color: '#7c5cff', lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
    });
    chartState.emaSlowSeries = chartState.chart.addLineSeries({
      color: '#f0a020', lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
    });
    return true;
  }

  /** Map an indicator series (shorter than the candles) onto candle times. */
  function alignSeries(pack, candles) {
    if (!pack || !pack.values) return [];
    const out = [];
    for (let i = 0; i < pack.values.length; i += 1) {
      const idx = pack.start + i;
      if (idx < 0 || idx >= candles.length) continue;
      out.push({ time: candles[idx].time, value: pack.values[i] });
    }
    return out;
  }

  /**
   * Snap a trade's timestamp onto its candle. lightweight-charts rejects
   * marker times that fall between bars, and GeckoTerminal's candles are
   * aligned to the timeframe, so flooring to the bar boundary is exact.
   */
  function snapToBar(ts, timeframeMinutes) {
    const step = Math.max(1, timeframeMinutes) * 60;
    return Math.floor(ts / step) * step;
  }

  function buildMarkers() {
    const payload = chartState.payload;
    if (!payload || !payload.candles || !chartState.trades.length) return [];
    const first = payload.candles[0].time;
    const last = payload.candles[payload.candles.length - 1].time;

    const markers = [];
    for (const t of chartState.trades) {
      const isBuy = t.side === 'BUY';
      const price = isBuy ? t.entry_price : t.exit_price;
      if (!t.logged_at || !price) continue;
      const parsed = Date.parse(t.logged_at);
      if (Number.isNaN(parsed)) continue;

      const time = snapToBar(parsed / 1000, payload.timeframe);
      if (time < first || time > last) continue;

      markers.push({
        time,
        position: isBuy ? 'belowBar' : 'aboveBar',
        color: isBuy ? '#26a69a' : '#ef5350',
        shape: isBuy ? 'arrowUp' : 'arrowDown',
        text: `${isBuy ? 'B' : 'S'} ${fmt(price, 2)}`,
      });
    }
    // Markers must be ascending or the library throws.
    return markers.sort((a, b) => a.time - b.time);
  }

  function renderChart() {
    const payload = chartState.payload;
    if (!payload || !payload.candles || !payload.candles.length) {
      chartMsg('No candles yet.');
      return;
    }

    chartState.candlesSeries.setData(payload.candles.map((c) => ({
      time: c.time, open: c.open, high: c.high, low: c.low, close: c.close,
    })));

    const showEma = $('chartEma').checked;
    chartState.emaFastSeries.applyOptions({ visible: showEma });
    chartState.emaSlowSeries.applyOptions({ visible: showEma });
    if (showEma) {
      chartState.emaFastSeries.setData(alignSeries(payload.series.emaFast, payload.candles));
      chartState.emaSlowSeries.setData(alignSeries(payload.series.emaSlow, payload.candles));
    }

    chartState.candlesSeries.setMarkers(
      $('chartTrades').checked ? buildMarkers() : []
    );

    chartMsg('');
  }

  async function loadChart() {
    if (!chartAvailable()) {
      chartMsg('Chart library could not be loaded (CDN unreachable?). '
             + 'Everything else on this page still works.');
      return;
    }
    if (!initChart() || chartState.loading) return;

    chartState.loading = true;
    try {
      // Trades come along so the chart can mark our own entries/exits.
      const [candles, trades] = await Promise.all([
        api('/api/candles?limit=300'),
        api('/api/trades?limit=500').catch(() => ({ trades: [] })),
      ]);
      chartState.payload = candles;
      chartState.timeframe = candles.timeframe;
      chartState.trades = trades.trades || [];
      renderChart();
      $('chartTitle').textContent =
        `${candles.base} / ${candles.quote} · ${candles.timeframe}m`;
    } catch (err) {
      chartMsg(`Chart unavailable: ${err.message}`);
    } finally {
      chartState.loading = false;
    }
  }

  // ── trending ───────────────────────────────────────────────────────

  function fmtUsd(v) {
    const n = num(v);
    if (n === null) return '—';
    if (n >= 1) {
      return n.toLocaleString(undefined, {
        minimumFractionDigits: 4, maximumFractionDigits: 4,
      });
    }
    return n.toPrecision(4);
  }

  function fmtCompact(v) {
    const n = num(v);
    if (n === null) return '—';
    if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
    if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
    if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
    return n.toFixed(0);
  }

  const shortMint = (m) =>
    m && m.length > 12 ? `${m.slice(0, 6)}…${m.slice(-4)}` : (m || '—');

  async function loadTrending() {
    const host = $('trending');
    try {
      const data = await api('/api/trending?limit=25');
      const tokens = data.tokens || [];
      if (!tokens.length) {
        host.className = 'empty';
        host.textContent = 'No trending tokens right now.';
        return;
      }
      const rows = tokens.map((t) => `
        <tr>
          <td><strong>${esc(t.baseSymbol)}</strong>
              <span class="muted"> / ${esc(t.quoteSymbol)}</span></td>
          <td class="num">$${fmtUsd(t.priceUsd)}</td>
          <td class="num">$${fmtCompact(t.volume24h)}</td>
          <td class="num">$${fmtCompact(t.marketCap)}</td>
          <td class="mono muted" title="${esc(t.baseMint)}">${esc(shortMint(t.baseMint))}</td>
        </tr>`).join('');

      host.className = '';
      host.innerHTML = `
        <table>
          <thead><tr>
            <th>Token</th><th>Price (USD)</th><th>24h volume</th>
            <th>Market cap</th><th>Mint</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>`;
    } catch (err) {
      host.className = 'empty';
      host.textContent = `Could not load trending list: ${err.message}`;
    }
  }

  // ── controls ───────────────────────────────────────────────────────

  async function engageKill() {
    try {
      await post('/api/kill', { engage: true });
      toast('Kill switch ENGAGED. No new positions will open.', 'ok');
      await poll();
    } catch (err) {
      toast(err.message, 'err');
    }
  }

  async function releaseKill() {
    const ok = await confirmDialog({
      title: 'Release kill switch?',
      body: 'New entries become possible again on the next BUY signal.',
      phrase: 'resume',
      okLabel: 'Release',
    });
    if (!ok) return;
    try {
      await post('/api/kill', { engage: false, confirm: 'resume' });
      toast('Kill switch released.', 'ok');
      await poll();
    } catch (err) {
      toast(err.message, 'err');
    }
  }

  async function toggleMode() {
    const isLive = document.body.dataset.mode === 'live';

    if (isLive) {
      // Going dry is the safe direction: no typed phrase.
      const ok = await confirmDialog({
        title: 'Switch to dry run?',
        body: 'The bot stops executing real swaps. Open positions stay on ' +
              'the books and will be simulated from here.',
        okLabel: 'Go to dry run',
      });
      if (!ok) return;
      try {
        await post('/api/mode', { dryRun: true });
        toast('Switched to DRY RUN.', 'ok');
      } catch (err) { toast(err.message, 'err'); }
    } else {
      const ok = await confirmDialog({
        title: 'Go LIVE with real money?',
        body: 'From the next qualifying signal the bot will sign and broadcast ' +
              'real transactions that spend your funds. Make sure you are using ' +
              'a throwaway wallet with an amount you can afford to lose.',
        phrase: 'GO LIVE',
        okLabel: 'Go live',
      });
      if (!ok) return;
      try {
        await post('/api/mode', { dryRun: false, confirm: 'GO LIVE' });
        toast('LIVE TRADING enabled.', 'err');
      } catch (err) { toast(err.message, 'err'); }
    }
    await poll();
  }

  // ── tabs ───────────────────────────────────────────────────────────

  async function showTab(name) {
    ui.tab = name;
    document.querySelectorAll('.tab').forEach((b) =>
      b.classList.toggle('active', b.dataset.tab === name));
    ['settings', 'trades', 'log'].forEach((t) =>
      $('tab-' + t).classList.toggle('hidden', t !== name));

    try {
      if (name === 'settings') await loadSchema();
      if (name === 'trades') await loadTrades();
      if (name === 'log') await loadLog();
    } catch (err) {
      toast(`Could not load ${name}: ${err.message}`, 'err');
    }
  }

  // ── polling ────────────────────────────────────────────────────────

  let polling = false;
  async function poll() {
    if (polling) return;
    polling = true;
    try {
      const s = await api('/api/state');
      document.body.dataset.mode = s.mode;
      if (!ui.originalValues) ui.originalValues = { ...s.settings };
      render(s);
    } catch (err) {
      $('alerts').innerHTML =
        alertBox('error', `Dashboard cannot reach the bot: ${err.message}`);
    } finally {
      polling = false;
    }
  }

  async function tick() {
    await poll();
    if (ui.tab === 'trades' && ui.tradesLoaded) {
      try { await loadTrades(); } catch (_) { /* keep last good render */ }
    }
    if (ui.tab === 'log' && $('logAuto').checked) {
      try { await loadLog(); } catch (_) { /* keep last good render */ }
    }
  }

  // ── boot ───────────────────────────────────────────────────────────

  function bind() {
    document.querySelectorAll('.tab').forEach((b) =>
      b.addEventListener('click', () => showTab(b.dataset.tab)));
    $('saveBtn').addEventListener('click', saveSettings);
    $('modeBtn').addEventListener('click', toggleMode);
    $('logRefresh').addEventListener('click', () =>
      loadLog().catch((e) => toast(e.message, 'err')));
    $('chartRefresh').addEventListener('click', () =>
      loadChart().catch((e) => toast(e.message, 'err')));
    // Re-render only, not refetch: the data is already in chartState.
    $('chartEma').addEventListener('change', () => {
      try { renderChart(); } catch (_) { /* no chart to update */ }
    });
    $('chartTrades').addEventListener('change', () => {
      try { renderChart(); } catch (_) { /* no chart to update */ }
    });
  }

  bind();
  showTab('settings').catch((e) => toast(e.message, 'err'));
  poll();
  loadChart();
  loadTrending();
  setInterval(tick, POLL_MS);
  // The chart and the trending list move far slower than the bot does, and
  // GeckoTerminal's free tier only allows ~30 requests a minute -- polling
  // them at the dashboard's 2s cadence would spend the whole budget on them
  // alone. The server caches both, so these intervals are a display choice.
  setInterval(() => { loadChart().catch(() => {}); }, 60_000);
  setInterval(() => { loadTrending().catch(() => {}); }, 300_000);
})();

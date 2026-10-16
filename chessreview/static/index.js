// Home page: upload PGNs, show analysis progress, stats and the games list.
import { api, el, gameRow, GAME_TABLE_HEAD } from './common.js';

const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('file-input');
const uploadResult = document.getElementById('upload-result');
const progressBlock = document.getElementById('progress');
const progressBar = document.getElementById('progress-bar');
const progressLabel = document.getElementById('progress-label');
const progressErrors = document.getElementById('progress-errors');
const stopBtn = document.getElementById('stop-btn');
const startBtn = document.getElementById('start-btn');

// ------------------------------------------------------------ upload

['dragenter', 'dragover'].forEach(type => dropzone.addEventListener(type, e => {
  e.preventDefault();
  dropzone.classList.add('over');
}));
['dragleave', 'drop'].forEach(type => dropzone.addEventListener(type, e => {
  e.preventDefault();
  dropzone.classList.remove('over');
}));
dropzone.addEventListener('drop', e => upload(e.dataTransfer.files));
fileInput.addEventListener('change', () => upload(fileInput.files));

async function upload(files) {
  if (!files.length) return;
  const form = new FormData();
  for (const f of files) form.append('files', f);
  uploadResult.textContent = `Uploading ${files.length} file(s)…`;
  try {
    const r = await api('/api/upload', { method: 'POST', body: form });
    const skipped = Object.entries(r.skipped).map(([why, n]) => `${n} ${why}`).join(', ');
    uploadResult.textContent = `${r.files} file(s), ${r.games_seen} games: ${r.imported} new, ` +
      `${r.duplicates} duplicates` + (skipped ? `, skipped ${skipped}` : '') + '.';
    loadGames();
    pollProgress();
  } catch (err) {
    uploadResult.textContent = `Upload failed: ${err.message}`;
  }
  fileInput.value = '';
}

// ------------------------------------------------------------ chess.com sync

const syncBtn = document.getElementById('sync-btn');
const syncResult = document.getElementById('sync-result');

async function loadSyncStatus() {
  const status = await api('/api/sync');
  document.getElementById('since-label').hidden = Boolean(status.last_month);
  syncResult.textContent = status.last_month
    ? `Synced up to ${status.last_month}. New games since then will be fetched.`
    : `Fetches ${status.username}'s games directly; no download needed.`;
}

syncBtn.addEventListener('click', async () => {
  syncBtn.disabled = true;
  syncResult.textContent = 'Fetching games from chess.com…';
  try {
    const r = await api('/api/sync', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ since: document.getElementById('since').value }),
    });
    syncResult.textContent = `${r.imported} new game(s), ${r.duplicates} already here` +
      (r.months.length ? ` (${r.months[0]} to ${r.months[r.months.length - 1]})` : '') + '.';
    document.getElementById('since-label').hidden = true;
    loadGames();
    pollProgress();
  } catch (err) {
    syncResult.textContent = `Sync failed: ${err.message}`;
  }
  syncBtn.disabled = false;
});

// ------------------------------------------------------------ progress

let polling = false;

async function pollProgress() {
  if (polling) return;
  polling = true;
  let wasRunning = false;
  while (true) {
    let p;
    try { p = await api('/api/progress'); } catch { break; }
    const running = p.state === 'running' || p.state === 'stopping';
    renderProgress(p, running);
    if (!running) {
      if (wasRunning) loadGames();
      break;
    }
    if (wasRunning && p.games_done !== renderProgress.lastDone) loadGames();
    renderProgress.lastDone = p.games_done;
    wasRunning = true;
    await new Promise(r => setTimeout(r, 1000));
  }
  polling = false;
}

function renderProgress(p, running) {
  const show = running || p.pending > 0 || p.errors.length > 0;
  progressBlock.hidden = !show;
  if (!show) return;
  const pct = Math.round(p.fraction * 100);
  progressBar.style.width = running ? `${pct}%` : (p.pending ? '0%' : '100%');
  if (running) {
    const eta = p.eta_seconds == null ? '' : ` · about ${formatDuration(p.eta_seconds)} left`;
    progressLabel.textContent = `${p.state === 'stopping' ? 'Stopping' : 'Analysing'}: ` +
      `game ${p.games_done} of ${p.games_total} (${pct}%)${eta}`;
  } else {
    progressLabel.textContent = p.pending ? `${p.pending} game(s) waiting for analysis.` : 'Analysis complete.';
  }
  stopBtn.hidden = !running;
  startBtn.hidden = running || !p.pending;
  progressErrors.textContent = p.errors.join(' · ');
}

function formatDuration(s) {
  s = Math.round(s);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

stopBtn.addEventListener('click', () => api('/api/analysis/stop', { method: 'POST' }));
startBtn.addEventListener('click', async () => {
  await api('/api/analysis/start', { method: 'POST' });
  pollProgress();
});

// ------------------------------------------------------------ stats + games

async function loadGames() {
  const games = await api('/api/games');
  renderStats(games);
  renderGames(games);
}

function renderStats(games) {
  const stats = document.getElementById('stats');
  stats.replaceChildren();
  if (!games.length) return;
  const count = r => games.filter(g => g.my_result === r).length;
  const [w, d, l] = [count('win'), count('draw'), count('loss')];
  const analysed = games.filter(g => g.analysed);
  const recent = analysed.slice(0, 20).filter(g => g.my_accuracy != null);
  const avgAcc = recent.length ? recent.reduce((t, g) => t + g.my_accuracy, 0) / recent.length : null;
  const perGame = key => analysed.length ? (analysed.reduce((t, g) => t + (g[key] || 0), 0) / analysed.length).toFixed(1) : '–';
  const latest = games.find(g => (g.my_color === 'white' ? g.white_elo : g.black_elo));
  const rating = latest ? (latest.my_color === 'white' ? latest.white_elo : latest.black_elo) : '–';
  const tiles = [
    [rating, `rating (${latest ? latest.time_class : ''})`],
    [games.length, `games (${w}W ${d}D ${l}L)`],
    [avgAcc === null ? '–' : `${avgAcc.toFixed(1)}%`, 'accuracy, last 20 games'],
    [perGame('blunders'), 'blunders per game'],
    [perGame('mistakes'), 'mistakes per game'],
    [`${analysed.length}/${games.length}`, 'analysed'],
  ];
  for (const [value, label] of tiles) {
    stats.append(el('div', { class: 'stat' }, el('div', { class: 'value' }, value), el('div', { class: 'label' }, label)));
  }
}

function renderGames(games) {
  document.getElementById('games-count').textContent = `${games.length} games`;
  document.getElementById('games-head').innerHTML = GAME_TABLE_HEAD;
  document.getElementById('games-body').replaceChildren(...games.slice(0, 8).map(gameRow));
}

loadGames();
loadSyncStatus();
pollProgress();

// Home page: upload PGNs, show analysis progress, stats and the games list.
import { api, el } from './common.js';

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
  const sum = key => analysed.reduce((t, g) => t + (g[key] || 0), 0);
  const perGame = key => analysed.length ? (sum(key) / analysed.length).toFixed(1) : '–';
  const tiles = [
    [games.length, 'games'],
    [`${Math.round((w + d / 2) / games.length * 100)}%`, `score (${w}W ${d}D ${l}L)`],
    [`${analysed.length}/${games.length}`, 'analysed'],
    [perGame('blunders'), 'blunders per game'],
    [perGame('mistakes'), 'mistakes per game'],
    [perGame('inaccuracies'), 'inaccuracies per game'],
  ];
  for (const [value, label] of tiles) {
    stats.append(el('div', { class: 'stat' }, el('div', { class: 'value' }, value), el('div', { class: 'label' }, label)));
  }
}

function renderGames(games) {
  document.getElementById('games-count').textContent = `${games.length} games`;
  const body = document.getElementById('games-body');
  body.replaceChildren(...games.map(g => {
    const opp = g.my_color === 'white' ? g.black : g.white;
    const oppElo = g.my_color === 'white' ? g.black_elo : g.white_elo;
    const errors = g.analysed
      ? `${g.inaccuracies || 0} / ${g.mistakes || 0} / ${g.blunders || 0}`
      : el('span', { class: 'muted' }, 'pending');
    return el('tr', { onclick: () => { location.href = `/game/${g.id}`; } },
      el('td', {}, g.date),
      el('td', {}, el('span', { class: `dot ${g.my_color}`, title: g.my_color })),
      el('td', {}, opp, oppElo ? el('span', { class: 'muted' }, ` (${oppElo})`) : null),
      el('td', { class: `result-${g.my_result}` }, g.my_result),
      el('td', { title: g.opening }, g.opening_family),
      el('td', {}, Math.ceil(g.num_plies / 2)),
      el('td', {}, errors),
    );
  }));
}

document.getElementById('reset-btn')?.addEventListener('click', async () => {
  const n = document.querySelectorAll('#games-body tr').length;
  if (!n) return;
  if (!confirm(`Delete all ${n} games and their analysis?\n\nUse this to start afresh with a new batch. ` +
               `Your PGN files are not touched, so you can re-upload them later (they will be re-analysed).`)) return;
  await api('/api/games', { method: 'DELETE' });
  uploadResult.textContent = `Deleted ${n} games.`;
  progressBlock.hidden = true;
  loadGames();
});

loadGames();
pollProgress();

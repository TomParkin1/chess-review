// Home page: first run (username, first sync), sync and upload, analysis progress, stats and
// the games list. Each card loads on its own, so one failing call doesn't blank the page.
import { api, el, emptyState, gameRow, guard, GAME_TABLE_HEAD } from './common.js';
import { renderWeaknesses } from './insights.js';

const $ = id => document.getElementById(id);
const ACCOUNT = window.ACCOUNT;

// ------------------------------------------------------------ first run: the username

function initOnboarding() {
  const form = $('account-form');
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const button = form.querySelector('button');
    button.disabled = true;
    $('account-result').textContent = 'Checking with chess.com…';
    try {
      await api('/api/account', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: $('username-input').value.trim() }),
      });
      location.reload();     // Home now offers the first sync
    } catch (err) {
      $('account-result').textContent = err.message;
      $('account-result').classList.add('error');
      button.disabled = false;
    }
  });
  $('username-input').focus();
}

// ------------------------------------------------------------ upload

function initUpload() {
  const dropzone = $('dropzone');
  const fileInput = $('file-input');
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
}

async function upload(files) {
  if (!files.length) return;
  const uploadResult = $('upload-result');
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
  $('file-input').value = '';
}

// ------------------------------------------------------------ chess.com sync

async function loadSyncStatus() {
  const status = await api('/api/sync');
  $('since-label').hidden = Boolean(status.last_month) || Boolean($('first-sync'));
  $('sync-result').textContent = status.last_month
    ? `Synced up to ${status.last_month}. New games since then will be fetched.`
    : `Fetches ${status.username}'s games directly from chess.com; no download needed.`;
}

async function loadArchiveNote() {
  const a = await api('/api/account?archives=1');
  if (a.months) {
    $('all-history-note').textContent =
      `${a.months} month${a.months === 1 ? '' : 's'} of games, since ${formatMonth(a.first_month)}`;
  }
}

const formatMonth = ym => {
  const [y, m] = ym.split('/').map(Number);
  return new Date(y, m - 1).toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
};

function initSync() {
  const syncBtn = $('sync-btn');
  syncBtn.addEventListener('click', async () => {
    syncBtn.disabled = true;
    const first = document.querySelector('input[name="first-since"]:checked');
    $('sync-result').textContent = 'Fetching games from chess.com…';
    try {
      const r = await api('/api/sync', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ since: first ? first.value : $('since').value }),
      });
      $('sync-result').textContent = `${r.imported} new game(s), ${r.duplicates} already here` +
        (r.months.length ? ` (${r.months[0]} to ${r.months[r.months.length - 1]})` : '') + '.';
      $('since-label').hidden = true;
      $('first-sync')?.remove();
      loadGames();
      loadTraining();
      pollProgress();
    } catch (err) {
      $('sync-result').textContent = `Sync failed: ${err.message}`;
    }
    syncBtn.disabled = false;
  });
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
      if (wasRunning) { loadGames(); loadTraining(); }
      break;
    }
    if (wasRunning && p.games_done !== renderProgress.lastDone) { loadGames(); loadTraining(); }
    renderProgress.lastDone = p.games_done;
    wasRunning = true;
    await new Promise(r => setTimeout(r, 1000));
  }
  polling = false;
}

function renderProgress(p, running) {
  const show = running || p.pending > 0 || p.errors.length > 0;
  $('progress').hidden = !show;
  if (!show) return;
  const pct = Math.round(p.fraction * 100);
  $('progress-bar').style.width = running ? `${pct}%` : (p.pending ? '0%' : '100%');
  if (running) {
    const eta = p.eta_seconds == null ? '' : ` · about ${formatDuration(p.eta_seconds)} left`;
    $('progress-label').textContent = `${p.state === 'stopping' ? 'Stopping' : 'Analysing'}: ` +
      `game ${p.games_done} of ${p.games_total} (${pct}%)${eta}`;
  } else {
    $('progress-label').textContent = p.pending ? `${p.pending} game(s) waiting for analysis.` : 'Analysis complete.';
  }
  $('stop-btn').hidden = !running;
  $('start-btn').hidden = running || !p.pending;
  $('progress-errors').textContent = p.errors.join(' · ');
}

function formatDuration(s) {
  s = Math.round(s);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

// ------------------------------------------------------------ stats + games

function loadGames() {
  return guard($('games-card'), async () => {
    const games = await api('/api/games');
    renderStats(games);
    renderGames(games);
  }, 'your games');
}

function renderStats(games) {
  const stats = $('stats');
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
  $('games-count').textContent = `${games.length} games`;
  $('games-head').innerHTML = GAME_TABLE_HEAD;
  $('games-body').replaceChildren(...(games.length ? games.slice(0, 8).map(gameRow)
    : [el('tr', { class: 'empty-row' }, el('td', { colspan: 9 }, emptyState('No games yet',
        'Sync your chess.com games above, or drop PGN files below.')))]));
}

async function loadTraining() {
  const title = $('train-cta-title');
  const sub = $('train-cta-sub');
  const btn = $('train-cta-btn');
  let s, today;
  try {
    [s, today] = await Promise.all([api('/api/train/stats'), api('/api/today')]);
  } catch (err) {
    title.textContent = "Today's workout";
    sub.textContent = `Couldn't load it: ${err.message}`;
    sub.classList.add('error');
    return;
  }
  sub.classList.remove('error');
  const plan = today.plan;
  const left = plan.total - plan.finished;
  const { total, analysed } = today.games;
  if (!total || !analysed) {
    title.textContent = "Today's workout";
    sub.textContent = total ? `Analysing your games (${analysed} of ${total} done). Your first workout is nearly ready.`
      : 'Sync your games to get your first workout.';
    btn.className = 'btn';
    btn.textContent = 'Open';
  } else {
    title.textContent = plan.complete || !plan.total
      ? "Today's workout: done ✓" : `Today's workout: ${left} position${left === 1 ? '' : 's'}`;
    sub.textContent = `🔥 ${s.streak}-day streak · ${s.mastered} mastered · ${s.learning} learning`;
    btn.className = left && !plan.complete ? 'btn primary' : 'btn';
    btn.textContent = plan.finished && !plan.complete ? 'Continue' : 'Open';
  }
  const session = s.session;
  const card = $('session-card');
  card.hidden = !session.open;
  if (session.open) {
    $('session-cta-title').textContent = `${session.games} new game${session.games === 1 ? '' : 's'}`;
    const waiting = session.games - session.analysed;
    $('session-cta-sub').textContent = waiting
      ? `${session.puzzles} mistakes ready · ${waiting} still analysing`
      : `${session.puzzles} mistakes to learn from`;
  }
}

async function loadWeaknesses() {
  const d = await api('/api/insights?last=100');
  const card = $('weakness-card');
  card.hidden = !(d.games >= 10 && d.weaknesses?.length);
  if (!card.hidden) renderWeaknesses($('weaknesses'), d.weaknesses, '100');
}

async function loadProgress() {
  const p = await api('/api/progress/report');
  $('progress-card').hidden = !p.games;
  $('progress-summary').textContent = p.summary;
}

// ------------------------------------------------------------ start

if (!ACCOUNT.username) {
  initOnboarding();
} else {
  initUpload();
  initSync();
  $('stop-btn').addEventListener('click', () => api('/api/analysis/stop', { method: 'POST' }).catch(() => {}));
  $('start-btn').addEventListener('click', async () => {
    await api('/api/analysis/start', { method: 'POST' });
    pollProgress();
  });
  loadGames();
  guard($('sync-result').parentElement, loadSyncStatus, 'the sync status');
  if ($('first-sync')) loadArchiveNote().catch(() => { /* the plain label is fine */ });
  // Optional cards stay hidden if they can't load; the error is in the console.
  loadProgress().catch(err => console.error(err));
  loadWeaknesses().catch(err => console.error(err));
  loadTraining();
  pollProgress();
}

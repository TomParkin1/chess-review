// Settings page: display preferences (saved in this browser), mistake thresholds (saved on
// the server, they change the classification) and the reset button.
import { getSettings, saveSettings, sound } from './board.js';
import { api } from './common.js';

function render() {
  const s = getSettings();
  document.querySelectorAll('[data-board]').forEach(b => b.classList.toggle('selected', b.dataset.board === s.board));
  document.querySelectorAll('[data-pieces]').forEach(b => b.classList.toggle('selected', b.dataset.pieces === s.pieces));
  document.querySelectorAll('[data-setting]').forEach(c => { c.checked = Boolean(s[c.dataset.setting]); });
}

document.querySelectorAll('button[data-board]').forEach(b =>
  b.addEventListener('click', () => { saveSettings({ board: b.dataset.board }); render(); }));
document.querySelectorAll('button[data-pieces]').forEach(b =>
  b.addEventListener('click', () => { saveSettings({ pieces: b.dataset.pieces }); render(); }));
document.querySelectorAll('[data-setting]').forEach(c =>
  c.addEventListener('change', () => {
    saveSettings({ [c.dataset.setting]: c.checked });
    if (c.dataset.setting === 'sound' && c.checked) sound.correct();
  }));

document.getElementById('reset-btn').addEventListener('click', async () => {
  if (!confirm('Delete all games, their analysis and your review history?\n\nchess.com sync can fetch the games again (they will be re-analysed).')) return;
  const r = await api('/api/games', { method: 'DELETE' });
  document.getElementById('reset-result').textContent = `Deleted ${r.deleted} games.`;
});

// ------------------------------------------------------------ mistake thresholds (server-side)

const fmt = v => v.join(' / ');

async function loadThresholds() {
  const t = await api('/api/settings/thresholds');
  document.getElementById('adaptive-note').textContent = t.rating
    ? `Each move is judged at its player's level. At your rating (${t.rating}, band ${t.band}): ` +
      `${fmt(t.adaptive_thresholds)} win% points.`
    : "Each move is judged at its player's level (needs ratings in your games).";
  document.getElementById('standard-note').textContent =
    `The same for everyone: ${fmt(t.standard_thresholds)} win% points (inaccuracy / mistake / blunder).`;
  document.querySelector(`input[name="th-mode"][value="${t.adaptive ? 'adaptive' : 'standard'}"]`).checked = true;
}

const radios = document.querySelectorAll('input[name="th-mode"]');
radios.forEach(radio => radio.addEventListener('change', async () => {
  const out = document.getElementById('th-result');
  out.textContent = 'Reclassifying all your games…';
  radios.forEach(r => { r.disabled = true; });
  try {
    const r = await api('/api/settings/thresholds', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ adaptive: radio.value === 'adaptive' }),
    });
    out.textContent = `Done: ${r.games} games reclassified. You now have ${r.inaccuracies} inaccuracies, ` +
      `${r.mistakes} mistakes and ${r.blunders} blunders.`;
  } catch (err) {
    out.textContent = `Failed: ${err.message}`;
  }
  radios.forEach(r => { r.disabled = false; });
  loadThresholds();
}));

// ------------------------------------------------------------ puzzle library

async function loadLibrary() {
  const s = await api('/api/library/status');
  const p = s.progress;
  document.getElementById('library-note').textContent = s.count
    ? `${s.count.toLocaleString()} puzzles (rated 400–3000) from the Lichess puzzle database, downloaded ${s.fetched_on}. ` +
      `Your puzzle rating: ${s.rating} after ${s.played} puzzles.`
    : 'Not downloaded yet. A one-time, streamed download of about 300 MB; about 5 MB is kept.';
  const btn = document.getElementById('library-btn');
  btn.textContent = s.count ? 'Update the puzzle library' : 'Download the puzzle library';
  btn.disabled = p.state === 'running';
  document.getElementById('library-progress').hidden = p.state !== 'running' && p.state !== 'error';
  const pct = p.bytes_total ? Math.round(100 * p.bytes_read / p.bytes_total) : 0;
  document.getElementById('library-bar').style.width = `${pct}%`;
  document.getElementById('library-label').textContent = p.state === 'error' ? `Download failed: ${p.error}`
    : `Downloading and sampling: ${pct}% (${p.rows.toLocaleString()} puzzles read)`;
  if (p.state === 'running') setTimeout(loadLibrary, 1000);
}

document.getElementById('library-btn').addEventListener('click', async () => {
  await api('/api/library/fetch', { method: 'POST' });
  loadLibrary();
});

render();
loadThresholds();
loadLibrary();

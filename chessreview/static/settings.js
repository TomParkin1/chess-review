// Settings page: my chess.com username, backups (back up, restore, export), display preferences
// (saved in this browser), mistake thresholds (saved on the server, they change the
// classification), the puzzle library and the reset button.
import { getSettings, saveSettings, sound } from './board.js';
import { api, el, guard } from './common.js';

const $ = id => document.getElementById(id);
const post = (url, body) => api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

function render() {
  const s = getSettings();
  document.querySelectorAll('[data-board]').forEach(b => b.classList.toggle('selected', b.dataset.board === s.board));
  document.querySelectorAll('[data-setting]').forEach(c => { c.checked = Boolean(s[c.dataset.setting]); });
}

document.querySelectorAll('button[data-board]').forEach(b =>
  b.addEventListener('click', () => { saveSettings({ board: b.dataset.board }); render(); }));
document.querySelectorAll('[data-setting]').forEach(c =>
  c.addEventListener('change', () => {
    saveSettings({ [c.dataset.setting]: c.checked });
    if (c.dataset.setting === 'sound' && c.checked) sound.correct();
  }));

document.getElementById('reset-btn').addEventListener('click', async () => {
  if (!confirm('Delete all games, their analysis and your review history?\n\nA backup is made first. chess.com sync can fetch the games again (they will be re-analysed).')) return;
  try {
    const r = await api('/api/games', { method: 'DELETE' });
    $('reset-result').textContent = `Deleted ${r.deleted} games.` + (r.deleted ? ' A backup was made first.' : '');
  } catch (err) {
    $('reset-result').textContent = `Failed: ${err.message}`;
  }
  loadBackups();
});

// ------------------------------------------------------------ chess.com account

$('account-form').addEventListener('submit', async e => {
  e.preventDefault();
  const name = $('username-input').value.trim();
  const current = window.ACCOUNT.username || '';
  const out = $('account-result');
  if (name.toLowerCase() === current.toLowerCase() && window.ACCOUNT.username_source === 'app') {
    out.textContent = 'No change.';
    return;
  }
  if (current && name.toLowerCase() !== current.toLowerCase() && window.ACCOUNT.games &&
      !confirm(`Switch to ${name}?\n\nYour ${window.ACCOUNT.games} games stay. The next sync fetches ${name}'s ` +
               'games from the start. To start afresh instead, use Delete all games first.')) return;
  out.textContent = 'Checking with chess.com…';
  try {
    const a = await post('/api/account', { username: name });
    window.ACCOUNT = a;
    out.textContent = `Saved. chess.com has ${a.months} month${a.months === 1 ? '' : 's'} of games for ${a.username}.`;
  } catch (err) {
    out.textContent = err.message;
  }
});

// ------------------------------------------------------------ backups

const when = iso => new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
const mb = bytes => `${(bytes / 1e6).toFixed(1)} MB`;

async function loadBackups() {
  const d = await api('/api/backups');
  const list = d.backups;
  const counts = {};
  list.forEach(b => { const k = ['daily', 'weekly'].includes(b.kind) ? b.kind : 'other'; counts[k] = (counts[k] || 0) + 1; });
  const total = list.reduce((t, b) => t + b.size, 0);
  $('backup-note').textContent = (list.length
    ? `Last backup: ${when(list[0].at)} (${list[0].label.toLowerCase()}). ` +
      `${counts.daily || 0} daily, ${counts.weekly || 0} weekly and ${counts.other || 0} other copies (${mb(total)}) in ${d.folder}. `
    : `No backups yet. They go to ${d.folder}. `) +
    'A copy is made every day while Chess Review runs, and before Delete all games, reclassifying, ' +
    'database upgrades, puzzle library updates and restores.';
  $('backup-list').replaceChildren(...list.map(b => el('li', { class: 'backup-row' },
    el('div', {}, el('strong', {}, when(b.at)), el('div', { class: 'muted small' },
      `${b.label} · ${b.games ?? '?'} games · ${mb(b.size)}`)),
    el('button', { class: 'btn small', onclick: () => restore(b) }, 'Restore'))));
  if (!list.length) $('backup-list').replaceChildren(el('li', { class: 'muted small' }, 'No backups yet.'));
}

async function restore(b) {
  if (!confirm(`Replace all your data with the copy from ${when(b.at)} (${b.label.toLowerCase()}, ${b.games ?? '?'} games)?` +
               '\n\nYour current data is backed up first, so you can undo this.')) return;
  await runRestore(() => post('/api/backups/restore', { name: b.name }));
}

async function runRestore(request) {
  const out = $('backup-result');
  out.textContent = 'Restoring…';
  try {
    const r = await request();
    out.textContent = `Restored: you now have ${r.games} games. Your previous data was saved first ` +
      '("Before restoring a backup" in the list).';
  } catch (err) {
    out.textContent = `Restore failed: ${err.message}`;
  }
  loadBackups();
}

$('restore-file').addEventListener('change', async () => {
  const file = $('restore-file').files[0];
  $('restore-file').value = '';
  if (!file || !confirm(`Replace all your data with ${file.name}?\n\nYour current data is backed up first, so you can undo this.`)) return;
  const form = new FormData();
  form.append('file', file);
  await runRestore(() => api('/api/backups/restore', { method: 'POST', body: form }));
});

$('backup-btn').addEventListener('click', async () => {
  $('backup-btn').disabled = true;
  try {
    await api('/api/backups', { method: 'POST' });
    $('backup-result').textContent = 'Backed up.';
  } catch (err) {
    $('backup-result').textContent = `Backup failed: ${err.message}`;
  }
  $('backup-btn').disabled = false;
  loadBackups();
});

$('restore-toggle').addEventListener('click', () => {
  $('restore-panel').hidden = !$('restore-panel').hidden;
  $('restore-toggle').setAttribute('aria-expanded', String(!$('restore-panel').hidden));
});

// Export history: a consistent copy of the database, downloaded as a file.
$('export-btn').addEventListener('click', async () => {
  $('export-btn').disabled = true;
  try {
    const res = await fetch('/api/backups/export');
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
    const name = /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') || '')?.[1] || 'chess-review.db';
    const url = URL.createObjectURL(await res.blob());
    el('a', { href: url, download: name }).click();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
    $('backup-result').textContent = `Exported ${name}. Keep it somewhere safe; Restore → "Restore from a file" loads it.`;
  } catch (err) {
    $('backup-result').textContent = `Export failed: ${err.message}`;
  }
  $('export-btn').disabled = false;
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
const sleep = ms => new Promise(r => setTimeout(r, ms));
radios.forEach(radio => radio.addEventListener('change', async () => {
  const out = document.getElementById('th-result');
  out.textContent = 'Reclassifying all your games…';
  radios.forEach(r => { r.disabled = true; });
  try {
    await post('/api/settings/thresholds', { adaptive: radio.value === 'adaptive' });
    // Every game is reclassified in the background; the rest of the app keeps working meanwhile.
    let t = await api('/api/settings/thresholds');
    while (t.rebuild.running) {
      if (t.rebuild.total) out.textContent = `Reclassifying… ${t.rebuild.done} of ${t.rebuild.total} games`;
      await sleep(500);
      t = await api('/api/settings/thresholds');
    }
    if (t.rebuild.error) throw new Error(t.rebuild.error);
    out.textContent = `Done: ${t.rebuild.total} games reclassified. You now have ${t.counts.inaccuracies} inaccuracies, ` +
      `${t.counts.mistakes} mistakes and ${t.counts.blunders} blunders.`;
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
      `Your puzzle rating: ${s.rating} after ${s.played} puzzles.` +
      (s.outdated ? ' Update it to sample the newest puzzle types too (queen, bishop, knight and queen-and-rook endgames).' : '')
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
  try {
    await api('/api/library/fetch', { method: 'POST' });
  } catch (err) {
    document.getElementById('library-note').textContent = `Couldn't start the download: ${err.message}`;
  }
  loadLibrary();
});

render();
guard($('backups-card'), loadBackups, 'your backups');
guard(document.querySelector('input[name="th-mode"]').closest('.card'), loadThresholds, 'the thresholds');
guard($('library-btn').closest('.card'), loadLibrary, 'the puzzle library status');

// ------------------------------------------------------------ endgame tablebase (E3)

function showTablebase(s) {
  $('tablebase-toggle').checked = s.enabled;
  $('tablebase-note').textContent = s.enabled
    ? `On: ${s.cached.toLocaleString()} position${s.cached === 1 ? '' : 's'} kept for offline play.`
    : 'Off: Stockfish alone plays and judges endgames.';
}
$('tablebase-toggle').addEventListener('change', async () => {
  const toggle = $('tablebase-toggle');
  toggle.disabled = true;
  try {
    showTablebase(await api('/api/settings/tablebase', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                         body: JSON.stringify({ enabled: toggle.checked }) }));
  } catch (err) {
    toggle.checked = !toggle.checked;
    $('tablebase-note').textContent = `Couldn't change it: ${err.message}`;
  } finally {
    toggle.disabled = false;
  }
});
guard($('tablebase-card'), async () => showTablebase(await api('/api/settings/tablebase')), 'the tablebase setting');

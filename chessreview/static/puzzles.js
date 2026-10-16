// Puzzle library (chessreview/library.py): Lichess puzzles around my puzzle rating, optionally
// by theme. If the library isn't downloaded yet, offer the download with a progress bar.
import { api } from './common.js';
import { createPuzzle } from './puzzle.js';

const $ = id => document.getElementById(id);
let items = [];
let index = 0;
const session = { solved: 0, failed: 0 };

const puzzle = createPuzzle({
  onResult: (item, result) => {
    if (result === 'solved') session.solved += 1;
    else session.failed += 1;
    $('session-score').textContent = `This session: ✓ ${session.solved} · ✗ ${session.failed}`;
    setTimeout(refreshRating, 700);   // after the result has been stored
  },
  onNext: () => next(),
});

async function refreshRating() {
  const s = await api('/api/library/status');
  $('rating-line').textContent = `Your puzzle rating: ${s.rating}` +
    (s.played ? ` · ${s.played} puzzles played` : ' · starts at your chess.com rating');
  return s;
}

async function load() {
  const theme = $('theme').value;
  const data = await api(`/api/library/next?n=10${theme ? `&theme=${theme}` : ''}`);
  items = data.items;
  index = 0;
  if (!items.length) {
    $('review').hidden = true;
    $('rating-line').textContent = 'No new puzzles for this theme near your rating.';
    return;
  }
  $('review').hidden = false;
  show();
}

function show() {
  $('counter').textContent = `Puzzle ${session.solved + session.failed + 1}`;
  puzzle.show(items[index]);
}

function next() {
  index += 1;
  if (index >= items.length) load();
  else show();
}

async function pollFetch() {
  const s = await api('/api/library/status');
  const p = s.progress;
  $('fetch-progress').hidden = p.state === 'idle';
  const pct = p.bytes_total ? Math.round(100 * p.bytes_read / p.bytes_total) : 0;
  $('fetch-bar').style.width = `${pct}%`;
  if (p.state === 'running') {
    $('fetch-label').textContent = `Downloading and sampling: ${pct}% (${p.rows.toLocaleString()} puzzles read)`;
    setTimeout(pollFetch, 1000);
  } else if (p.state === 'error') {
    $('fetch-label').textContent = `Download failed: ${p.error}`;
    $('fetch-btn').disabled = false;
  } else if (s.count) {
    start();
  }
}

$('fetch-btn').addEventListener('click', async () => {
  $('fetch-btn').disabled = true;
  await api('/api/library/fetch', { method: 'POST' });
  pollFetch();
});
$('theme').addEventListener('change', load);

async function start() {
  const s = await refreshRating();
  $('library-empty').hidden = s.count > 0;
  if (s.count) load();
  else if (s.progress.state === 'running') { $('fetch-btn').disabled = true; pollFetch(); }
}

start();

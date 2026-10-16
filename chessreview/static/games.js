// Games page: every imported game, filterable, newest first.
import { api, gameRow, GAME_TABLE_HEAD } from './common.js';

const $ = id => document.getElementById(id);
const games = await api('/api/games');

for (const tc of [...new Set(games.map(g => g.time_class))].sort()) {
  $('f-time').append(new Option(tc.charAt(0).toUpperCase() + tc.slice(1), tc));
}
$('games-head').innerHTML = GAME_TABLE_HEAD;

function render() {
  const q = $('search').value.trim().toLowerCase();
  const shown = games.filter(g =>
    (!$('f-result').value || g.my_result === $('f-result').value) &&
    (!$('f-color').value || g.my_color === $('f-color').value) &&
    (!$('f-time').value || g.time_class === $('f-time').value) &&
    (!q || [g.white, g.black, g.opening].some(v => (v || '').toLowerCase().includes(q))));
  $('games-count').textContent = shown.length === games.length
    ? `${games.length} games` : `${shown.length} of ${games.length} games`;
  $('games-body').replaceChildren(...shown.map(gameRow));
}

for (const id of ['search', 'f-result', 'f-color', 'f-time']) $(id).addEventListener('input', render);
render();

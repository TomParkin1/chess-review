// Settings page: display preferences (saved in this browser) and the reset button.
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

render();

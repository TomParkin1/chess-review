// Runs on every page: the due-count badge on the sidebar's Train link.
import { api } from './common.js';

try {
  const s = await api('/api/train/stats');
  const todo = s.due + s.new;
  const link = document.querySelector('.sidebar a[data-section="train"]');
  if (link && todo) {
    const badge = document.createElement('span');
    badge.className = 'nav-badge';
    badge.textContent = todo;
    badge.title = `${s.due} due, ${s.new} new`;
    link.append(badge);
  }
} catch { /* the badge is a nicety */ }

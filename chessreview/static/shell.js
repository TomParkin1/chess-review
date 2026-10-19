// Runs on every page: the badge on the sidebar's Today link = what is left of today's workout
// (it disappears once the workout is complete). The Today page calls showTodayBadge(plan) as
// blocks are finished.
import { api } from './common.js';

export function showTodayBadge(plan) {
  const link = document.querySelector('.sidebar a[data-section="train"]');
  if (!link) return;
  link.querySelector('.nav-badge')?.remove();
  const left = plan.total - plan.finished;
  if (left <= 0 || plan.complete) return;
  const badge = document.createElement('span');
  badge.className = 'nav-badge';
  badge.textContent = left;
  badge.title = `${left} left in today's workout`;
  link.append(badge);
}

try {
  showTodayBadge((await api('/api/today')).plan);
} catch { /* the badge is a nicety */ }

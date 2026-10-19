// Runs on every page: the badge on the sidebar's Today link = what is left of today's workout
// (it disappears once the workout is complete; the Today page calls showTodayBadge(plan) as
// blocks are finished), and a safety net that shows any error a page didn't handle itself.
import { api, el } from './common.js';

// One banner at the top of the page for uncaught errors, so a broken page never just sits there.
function showBanner(message) {
  let banner = document.getElementById('app-banner');
  if (!banner) {
    banner = el('div', { id: 'app-banner', class: 'app-banner', role: 'alert' },
      el('span', { class: 'app-banner-text' }),
      el('button', { class: 'btn small', onclick: () => location.reload() }, 'Reload'),
      el('button', { class: 'btn small', title: 'Dismiss', onclick: () => banner.remove() }, '✕'));
    document.querySelector('main')?.prepend(banner);
  }
  banner.querySelector('.app-banner-text').textContent = `Something went wrong: ${message}`;
}
window.addEventListener('unhandledrejection', e => showBanner(e.reason?.message || String(e.reason)));
window.addEventListener('error', e => {
  if (String(e.message).startsWith('ResizeObserver loop')) return;    // a harmless browser notice
  showBanner(e.error?.message || e.message || 'unknown error');
});

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

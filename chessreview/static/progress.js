// Progress: my last N games vs the N before (chessreview/progress.py), rolling charts,
// mistake types and weekly training.
import { lineChart } from './charts.js';
import { api, el } from './common.js';

const $ = id => document.getElementById(id);
const FORMATS = {
  1: v => v.toFixed(1),
  0: v => String(Math.round(v)),
  '%': v => `${Math.round(v)}%`,
};

function tile(h) {
  const fmt = FORMATS[h.format];
  const value = h.current == null ? '–' : fmt(h.current);
  let delta = '';
  if (h.baseline != null && h.current != null) {
    const arrow = h.current > h.baseline ? '▲' : h.current < h.baseline ? '▼' : '=';
    delta = el('div', { class: `delta ${h.better === true ? 'good' : h.better === false ? 'bad' : ''}` },
               `${arrow} was ${fmt(h.baseline)}`);
  }
  return el('div', { class: 'stat' }, el('div', { class: 'value' }, value), el('div', { class: 'label' }, h.label), delta);
}

function chart(id, r, key, opts = {}) {
  lineChart($(id), {
    labels: r.labels.map(d => `${r.window} games up to ${d}`),
    series: [{ name: opts.name || key, values: r.series[key], color: 'var(--series-you)', line: true }],
    ...opts,
  });
}

async function load() {
  const r = await api('/api/progress/report');
  $('summary').textContent = r.summary;
  $('progress-scope').textContent = r.baseline
    ? `Your last ${r.window} games compared with the ${r.window} before, measured at your current level` +
      (r.rating_now ? ` (rating ${r.rating_now}).` : '.')
    : `Your last ${r.current.games} games. A comparison appears after ${r.window + 10} analysed games.`;
  $('tiles').replaceChildren(...r.headline.map(tile));
  document.querySelectorAll('.rolling-note').forEach(n => { n.textContent = `rolling ${r.window}-game average`; });
  $('winning-note').textContent = `games where you were +${r.winning_cp / 100} or better`;

  chart('chart-blunders', r, 'blunders', { name: 'Blunders per game', yFormat: v => v.toFixed(1) });
  chart('chart-threats', r, 'threats_missed', { name: 'Threats missed per game', yFormat: v => v.toFixed(1) });
  chart('chart-conversion', r, 'conversion', { name: 'Won from winning positions', yDomain: [0, 100], yFormat: v => `${Math.round(v)}%` });
  chart('chart-accuracy', r, 'accuracy', { name: 'Accuracy', yFormat: v => `${Math.round(v)}%` });

  const per = v => (v == null ? '–' : v.toFixed(2));
  $('kinds').tBodies[0].replaceChildren(...r.kinds.map(k => el('tr', {},
    el('td', {}, k.label), el('td', {}, per(k.baseline)), el('td', {}, per(k.current)),
    el('td', { class: k.change == null ? '' : k.change < -0.005 ? 'good' : k.change > 0.005 ? 'bad' : '' },
       k.change == null ? '–' : `${k.change > 0 ? '+' : ''}${k.change.toFixed(2)}`))));

  const pct = v => (v == null ? '–' : `${Math.round(v)}%`);
  const sr = r.self_review;
  if (sr.tries) {
    $('self-review-note').textContent = `Turning points you found yourself: ${sr.hits} of your last ${sr.tries} ` +
      `games reviewed (${pct(sr.rate)}) · per week below`;
  }
  $('training').tBodies[0].replaceChildren(...(r.training.length ? r.training.map(w => el('tr', {},
    el('td', {}, w.from), el('td', {}, w.days), el('td', {}, w.attempts), el('td', {}, pct(w.solve_rate)),
    el('td', {}, pct(w.threat_rate)))) : [el('tr', {}, el('td', { colspan: 5, class: 'muted' }, 'No training yet.'))]));
}

load();

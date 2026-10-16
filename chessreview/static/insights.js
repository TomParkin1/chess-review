// Insights: trends, phases, mistake types, clock, colours, openings and top weaknesses.
import { barList, lineChart } from './charts.js';
import { api, el } from './common.js';

const $ = id => document.getElementById(id);
const pct = v => (v == null ? '–' : `${v.toFixed(0)}%`);
const ICONS = { kind: '🎯', phase: '⏱️', opening: '📖', low_clock: '⏰', color: '♟️' };

export function trainHref(train) {
  const params = new URLSearchParams({ status: '', ...train });
  return `/review?${params}`;
}

export function renderWeaknesses(container, weaknesses) {
  if (!weaknesses.length) {
    container.replaceChildren(el('p', { class: 'muted' }, 'No clear weaknesses yet: play and analyse a few more games.'));
    return;
  }
  container.replaceChildren(...weaknesses.map((w, i) => {
    const key = Object.keys(w.train)[0];
    return el('a', { class: 'weakness', href: trainHref(w.train) },
      el('div', { class: 'weakness-rank' }, `#${i + 1}`),
      el('div', { class: 'weakness-icon' }, ICONS[key] || '💡'),
      el('h3', {}, w.title),
      el('p', { class: 'muted small' }, w.detail),
      el('span', { class: 'btn small primary' }, 'Train this →'));
  }));
}

async function load() {
  const params = new URLSearchParams();
  if ($('f-time').value) params.set('time_class', $('f-time').value);
  if ($('f-last').value) params.set('last', $('f-last').value);
  const d = await api(`/api/insights?${params}`);
  if (!$('f-time').options.length || $('f-time').options.length === 1) {
    for (const tc of d.time_classes || []) $('f-time').append(new Option(tc[0].toUpperCase() + tc.slice(1), tc));
  }
  if (!d.games) {
    $('tiles').replaceChildren(el('div', { class: 'stat' }, el('div', { class: 'value' }, '0'), el('div', { class: 'label' }, 'analysed games')));
    return;
  }
  const o = d.overall;
  $('tiles').replaceChildren(...[
    [d.games, 'games analysed'], [pct(o.score), 'score'], [pct(o.accuracy), 'average accuracy'],
    [o.blunders_per_game.toFixed(1), 'blunders per game'], [o.mistakes_per_game.toFixed(1), 'mistakes per game'],
  ].map(([v, label]) => el('div', { class: 'stat' }, el('div', { class: 'value' }, v), el('div', { class: 'label' }, label))));

  renderWeaknesses($('weaknesses'), d.weaknesses);

  // Rating: the most-played time class in the selection (ratings differ per time class).
  const counts = {};
  d.trend.forEach(t => { counts[t.time_class] = (counts[t.time_class] || 0) + 1; });
  const mainTc = Object.entries(counts).sort((a, b) => b[1] - a[1])[0]?.[0];
  const rated = d.trend.filter(t => t.time_class === mainTc && t.elo);
  $('rating-note').textContent = mainTc ? `${mainTc}, ${rated.length} games` : '';
  lineChart($('rating-chart'), {
    labels: rated.map(t => `${t.date} vs ${t.opponent} (${t.result})`),
    series: [{ name: 'Rating', values: rated.map(t => t.elo), color: 'var(--series-you)', line: true }],
    onClick: i => { location.href = `/game/${rated[i].id}`; },
  });

  const acc = d.trend.map(t => t.accuracy);
  const rolling = acc.map((_, i) => {
    const w = acc.slice(Math.max(0, i - 9), i + 1).filter(v => v != null);
    return w.length >= 3 ? w.reduce((a, b) => a + b, 0) / w.length : null;
  });
  lineChart($('accuracy-chart'), {
    labels: d.trend.map(t => `${t.date} vs ${t.opponent} (${t.result})`),
    series: [
      { name: 'Game', values: acc, color: 'var(--series-you)', dots: true, dotOpacity: 0.55 },
      { name: '10-game average', values: rolling, color: 'var(--series-opp)', line: true },
    ],
    yDomain: [0, 100], yFormat: v => `${Math.round(v)}%`,
    onClick: i => { location.href = `/game/${d.trend[i].id}`; },
  });

  barList($('phase-bars'), d.phases.map(p => ({
    label: p.phase[0].toUpperCase() + p.phase.slice(1), value: p.accuracy, display: pct(p.accuracy),
    title: `${p.moves} moves · ${p.errors_per_game.toFixed(1)} mistakes+blunders per game`,
    href: trainHref({ phase: p.phase }),
  })), { max: 100 });
  const worstPhase = [...d.phases].filter(p => p.accuracy != null).sort((a, b) => a.accuracy - b.accuracy)[0];
  $('phase-note').textContent = worstPhase
    ? `Most of your errors per game come in the ${[...d.phases].sort((a, b) => b.errors_per_game - a.errors_per_game)[0].phase}.` : '';

  barList($('kind-bars'), d.kinds.slice(0, 8).map(k => ({
    label: k.label, value: k.count, display: `${k.count} (${(k.share * 100).toFixed(0)}%)`,
    href: trainHref({ kind: k.kind }),
  })), { color: 'var(--mistake)' });

  const clock = d.clock;
  barList($('clock-bars'), clock.buckets.map(b => ({
    label: b.label, value: b.rate || 0, display: b.moves ? `${(b.rate || 0).toFixed(1)}` : '–',
    title: `${b.blunders} blunders in ${b.moves} moves`,
  })), { color: 'var(--blunder)' });
  const fast = clock.fast, slow = clock.slow;
  $('clock-note').textContent = clock.games_with_clocks
    ? `Based on ${clock.games_with_clocks} games with clock times. ` +
      (fast.rate != null && slow.rate != null
        ? `Moves made in under 5 s: ${fast.rate.toFixed(1)} blunders per 100; slower moves: ${slow.rate.toFixed(1)}.` : '')
    : 'Clock times come with chess.com sync; uploaded PGN downloads have none.';

  $('colors').replaceChildren(...d.colors.map(c => el('a', { class: 'color-tile', href: trainHref({ color: c.color }) },
    el('span', { class: `dot ${c.color}` }), el('strong', {}, ` As ${c.color}`),
    el('div', { class: 'value' }, pct(c.score)), el('div', { class: 'muted small' },
      `score in ${c.games} games · ${pct(c.accuracy)} accuracy`))));

  const SHOWN = 12;
  const rows = d.openings.map(op => {
    const bar = el('span', { class: 'wdl', title: `${op.wins}W ${op.draws}D ${op.losses}L` },
      el('span', { class: 'w', style: `flex:${op.wins}` }), el('span', { class: 'd', style: `flex:${op.draws}` }),
      el('span', { class: 'l', style: `flex:${op.losses}` }));
    return el('tr', { onclick: () => { location.href = trainHref({ opening: op.family, color: op.color }); } },
      el('td', {}, op.family), el('td', {}, el('span', { class: `dot ${op.color}` })),
      el('td', { class: 'num' }, op.games), el('td', {}, bar, el('span', { class: 'muted small' }, ` ${op.wins}/${op.draws}/${op.losses}`)),
      el('td', { class: 'num' }, pct(op.score)), el('td', { class: 'num' }, pct(op.accuracy)),
      el('td', { class: 'num' }, pct(op.opening_accuracy)), el('td', { class: 'num' }, op.errors_per_game.toFixed(1)));
  });
  const body = $('openings').tBodies[0];
  body.replaceChildren(...rows.slice(0, SHOWN));
  if (rows.length > SHOWN) {
    const more = el('tr', { class: 'show-more' }, el('td', { colspan: 8 },
      el('button', { class: 'btn small', onclick: () => body.replaceChildren(...rows) },
         `Show all ${rows.length} openings`)));
    body.append(more);
  }
}

if (document.getElementById('f-time')) {
  $('f-time').addEventListener('change', load);
  $('f-last').addEventListener('change', load);
  load();
}

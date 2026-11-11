// Insights: trends, phases, mistake types, clock, colours, openings and top weaknesses.
import { barList, lineChart } from './charts.js';
import { api, el, emptyState, guard } from './common.js';

const $ = id => document.getElementById(id);
const pct = v => (v == null ? '–' : `${v.toFixed(0)}%`);
const ICONS = { kind: '🎯', phase: '⏱️', opening: '📖', low_clock: '⏰', fast: '⚡', color: '♟️' };

// Practice link for a weakness; `last` keeps the same game range the weakness was found in.
export function trainHref(train, last = currentLast()) {
  const params = new URLSearchParams({ status: '', last: last || '', ...train });
  return `/review?${params}`;
}

function currentLast() {
  return document.getElementById('f-last')?.value ?? '';
}

export function renderWeaknesses(container, weaknesses, last = currentLast()) {
  if (!weaknesses.length) {
    container.replaceChildren(el('p', { class: 'muted' }, 'No clear weaknesses yet: play and analyse a few more games.'));
    return;
  }
  container.replaceChildren(...weaknesses.map((w, i) => {
    const key = Object.keys(w.train)[0];
    return el('a', { class: 'weakness', href: trainHref(w.train, last) },
      el('div', { class: 'weakness-rank' }, `#${i + 1}`),
      el('div', { class: 'weakness-icon' }, ICONS[key] || '💡'),
      el('h3', {}, w.title),
      el('p', { class: 'muted small' }, w.detail),
      el('span', { class: 'btn small primary' }, 'Train this →'));
  }));
}

let loadSeq = 0;     // a newer filter makes an older reply stale
async function load() {
  const seq = ++loadSeq;
  const params = new URLSearchParams();
  if ($('f-time').value) params.set('time_class', $('f-time').value);
  if ($('f-last').value) params.set('last', $('f-last').value);
  const d = await api(`/api/insights?${params}`);
  if (seq !== loadSeq) return;
  if (!$('f-time').options.length || $('f-time').options.length === 1) {
    for (const tc of d.time_classes || []) $('f-time').append(new Option(tc[0].toUpperCase() + tc.slice(1), tc));
  }
  const sections = document.querySelectorAll('main > section.card, main > .insight-grid');
  sections.forEach(e => { e.hidden = !d.games; });
  if (!d.games) {
    $('tiles').replaceChildren($('f-time').value      // "last N games" can't empty a non-empty history
      ? emptyState('No analysed games', 'None match this time control.')
      : emptyState('No analysed games yet', 'Sync your chess.com games on Home. Once they are analysed, your ' +
                   'trends, weaknesses and openings appear here.', { href: '/', label: 'Go to Home' }));
    return;
  }
  const o = d.overall;
  $('tiles').replaceChildren(...[
    [d.games, 'games analysed'], [pct(o.score), 'score'], [pct(o.accuracy), 'average accuracy'],
    [o.blunders_per_game.toFixed(1), 'blunders per game'], [o.mistakes_per_game.toFixed(1), 'mistakes per game'],
  ].map(([v, label]) => el('div', { class: 'stat' }, el('div', { class: 'value' }, v), el('div', { class: 'label' }, label))));

  renderWeaknesses($('weaknesses'), d.weaknesses);
  const scope = $('f-last').value ? `your last ${$('f-last').value} games` : 'all your games';
  $('weakness-scope').textContent = `Based on ${scope}${$('f-time').value ? ` (${$('f-time').value})` : ''}, compared with your own average`;

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
  const u = clock.usual;
  const secs = ms => (ms >= 60000 ? `${+(ms / 60000).toFixed(1)} min` : `${+(ms / 1000).toFixed(1)} s`);
  $('clock-note').textContent = clock.games_with_clocks
    ? `Based on ${clock.games_with_clocks} games with clock times. Time left is a share of the time control ` +
      `(base + 40 × increment), so blitz and rapid compare fairly` +
      (u ? `: at ${u.label}, 30% is ${secs(u.bucket_ms[0])}, time trouble (10%) starts at ${secs(u.low_ms)} and ` +
           `3% is ${secs(u.bucket_ms[2])}. ` : '. ') +
      (fast.rate != null && slow.rate != null
        ? `Very fast moves${u ? ` (under ${secs(u.fast_ms)} at ${u.label})` : ''}: ${fast.rate.toFixed(1)} blunders per 100; ` +
          `slower moves: ${slow.rate.toFixed(1)}.` : '')
    : 'Clock times come with chess.com sync; uploaded PGN downloads have none.';

  $('colors').replaceChildren(...d.colors.map(c => el('a', { class: 'color-tile', href: trainHref({ color: c.color }) },
    el('span', { class: `dot ${c.color}` }), el('strong', {}, ` As ${c.color}`),
    el('div', { class: 'value' }, pct(c.score)), el('div', { class: 'muted small' },
      `score in ${c.games} games · ${pct(c.accuracy)} accuracy`))));

  renderEndgames(d);

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

// ------------------------------------------------------------ endgames (E4)

const wdlBar = r => el('span', { class: 'wdl', title: `${r.won}W ${r.drawn}D ${r.lost}L` },
  el('span', { class: 'w', style: `flex:${r.won}` }), el('span', { class: 'd', style: `flex:${r.drawn}` }),
  el('span', { class: 'l', style: `flex:${r.lost}` }));
const ofGames = r => (r.games ? `${r.won} of ${r.games}` : 'none yet');

function renderEndgames(d) {
  const e = d.endgames;
  const phases = Object.fromEntries(d.phases.map(p => [p.phase, p]));
  const bar = `${e.winning_cp >= 0 ? '+' : ''}${(e.winning_cp / 100).toFixed(1)}`;
  $('endgame-scope').textContent = `"clearly" = ${bar} or more at your rating`;
  const tile = (value, label, title = '') => el('div', { class: 'stat', title },
    el('div', { class: 'value' }, value), el('div', { class: 'label' }, label));
  const conv = e.endgame_conversion, saves = e.saves;
  $('endgame-tiles').replaceChildren(
    tile(`${e.games}`, `games reached an endgame (${pct(e.share * 100)})`),
    tile(pct(conv.rate), `won from clearly winning endgames (${ofGames(conv)})`,
         `Of all positions where you were clearly winning: ${pct(e.conversion.rate)} won (${ofGames(e.conversion)})`),
    tile(saves.games ? `${saves.won + saves.drawn} of ${saves.games}` : '–', 'saved from clearly losing endgames',
         `${saves.won} won, ${saves.drawn} drawn`),
    tile(pct(phases.endgame?.accuracy), `endgame accuracy (middlegame ${pct(phases.middlegame?.accuracy)})`));
  $('endgame-types').tBodies[0].replaceChildren(...e.types.map(t =>
    el('tr', { onclick: () => { location.href = trainHref({ phase: 'endgame' }); } },
      el('td', {}, t.label), el('td', { class: 'num' }, t.games),
      el('td', {}, wdlBar(t), el('span', { class: 'muted small' }, ` ${t.won}/${t.drawn}/${t.lost}`)),
      el('td', { class: 'num' }, pct(t.score)), el('td', { class: 'num' }, pct(t.accuracy)),
      el('td', { class: 'num' }, t.errors_per_game.toFixed(1)))));
  $('endgame-types').closest('.table-wrap').hidden = !e.types.length;
  $('endgame-note').textContent = e.games
    ? 'The type is the material at your first endgame move. Endgame accuracy runs high for everyone (many moves are ' +
      'simple), so compare endgames with each other and over time rather than with the middlegame.'
    : 'None of these games reached an endgame.';
  loadTraining();
}

let trainingLoaded = false;     // the same whatever the filters: load once
function loadTraining() {
  if (trainingLoaded) return;
  trainingLoaded = true;
  api('/api/insights/endgame-training').then(t => {
    const link = (href, text) => el('a', { href }, text);
    $('endgame-training').replaceChildren('Your endgame training: ',
      link('/convert', `essential endgames ${t.essentials.solved} of ${t.essentials.total} solved` +
                       (t.essentials.mastered ? ` (${t.essentials.mastered} mastered)` : '')), ' · ',
      link('/convert', `won positions ${t.positions.converted} converted of ${t.positions.tried} tried`), ' · ',
      link('/puzzles?theme=endgame', t.puzzles.tried
        ? `endgame puzzles ${t.puzzles.clean} of ${t.puzzles.tried} clean first time` : 'no endgame puzzles yet'), ' · ',
      link(trainHref({ phase: 'endgame' }), `your endgame mistakes ${t.mistakes.studied} of ${t.mistakes.total} studied`));
  }).catch(() => { trainingLoaded = false; $('endgame-training').textContent = ''; });
}

if (document.getElementById('f-time')) {     // the Insights page (Home only borrows renderWeaknesses)
  const reload = () => guard(document.querySelector('main'), load, 'your insights');
  $('f-time').addEventListener('change', reload);
  $('f-last').addEventListener('change', reload);
  reload();
}

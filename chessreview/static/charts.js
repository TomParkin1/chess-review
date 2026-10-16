// Small SVG charts for Insights, following the dataviz rules: one y-axis, 2px lines,
// >= 8px markers with a surface ring, thin bars with rounded data-ends, recessive solid
// gridlines, and a hover layer (crosshair + tooltip) on every plot.
import { el } from './common.js';

const NS = 'http://www.w3.org/2000/svg';

function svgEl(parent, tag, attrs) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  parent.append(node);
  return node;
}

function niceTicks(min, max, count = 4) {
  const span = max - min || 1;
  const step = Math.pow(10, Math.floor(Math.log10(span / count)));
  const err = span / count / step;
  const nice = step * (err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1);
  const ticks = [];
  for (let v = Math.ceil(min / nice) * nice; v <= max + 1e-9; v += nice) ticks.push(+v.toFixed(6));
  return ticks;
}

/**
 * Line chart over games.
 * series: [{ name, values: [number|null], color, line: bool, dots: bool }]
 * labels: tooltip title per index. yFormat: value -> string. yDomain: [min, max] (optional).
 */
export function lineChart(container, { series, labels, yFormat = v => String(Math.round(v)), yDomain = null,
                                        height = 200, onClick = null }) {
  container.replaceChildren();
  const n = labels.length;
  const all = series.flatMap(s => s.values).filter(v => v != null);
  if (!n || !all.length) { container.append(el('p', { class: 'muted small' }, 'Not enough data yet.')); return; }
  const W = 640, H = height, padL = 40, padR = 12, padT = 12, padB = 22;
  let [lo, hi] = yDomain || [Math.min(...all), Math.max(...all)];
  if (!yDomain) { const pad = (hi - lo) * 0.1 || 10; lo -= pad; hi += pad; }
  const x = i => padL + (n === 1 ? (W - padL - padR) / 2 : (i / (n - 1)) * (W - padL - padR));
  const y = v => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);

  const wrap = el('div', { class: 'chart-wrap' });
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('class', 'linechart');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', series.map(s => s.name).join(', '));
  for (const t of niceTicks(lo, hi)) {
    svgEl(svg, 'line', { x1: padL, x2: W - padR, y1: y(t), y2: y(t), class: 'gridline' });
    svgEl(svg, 'text', { x: padL - 6, y: y(t) + 3, class: 'tick', 'text-anchor': 'end' }).textContent = yFormat(t);
  }
  for (const s of series) {
    if (s.line) {
      let d = '';
      s.values.forEach((v, i) => { if (v != null) d += `${d ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`; });
      svgEl(svg, 'path', { d, fill: 'none', stroke: s.color, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' });
    }
    if (s.dots) {
      s.values.forEach((v, i) => {
        if (v != null) svgEl(svg, 'circle', { cx: x(i), cy: y(v), r: 4, fill: s.color, stroke: 'var(--panel)', 'stroke-width': 2, opacity: s.dotOpacity ?? 1 });
      });
    }
  }
  const cross = svgEl(svg, 'line', { y1: padT, y2: H - padB, class: 'crosshair', visibility: 'hidden' });
  wrap.append(svg);
  const tip = el('div', { class: 'chart-tip', hidden: true });
  wrap.append(tip);
  container.append(wrap);

  const indexAt = e => {
    const r = svg.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    return Math.max(0, Math.min(n - 1, Math.round(((px - padL) / (W - padL - padR)) * (n - 1))));
  };
  svg.addEventListener('mousemove', e => {
    const i = indexAt(e);
    const r = svg.getBoundingClientRect();
    cross.setAttribute('x1', x(i)); cross.setAttribute('x2', x(i)); cross.setAttribute('visibility', 'visible');
    tip.hidden = false;
    tip.style.left = `${(x(i) / W) * r.width}px`;
    tip.style.top = '0px';
    tip.replaceChildren(el('strong', {}, labels[i]),
      ...series.filter(s => s.values[i] != null).map(s => el('div', {},
        el('span', { class: 'tip-swatch', style: `background:${s.color}` }), ` ${s.name}: ${yFormat(s.values[i])}`)));
  });
  svg.addEventListener('mouseleave', () => { cross.setAttribute('visibility', 'hidden'); tip.hidden = true; });
  if (onClick) {
    svg.style.cursor = 'pointer';
    svg.addEventListener('click', e => onClick(indexAt(e)));
  }
}

/** Horizontal bars with direct labels: rows = [{ label, value, display, title, href }]. */
export function barList(container, rows, { max = null, color = 'var(--series-you)' } = {}) {
  container.replaceChildren();
  if (!rows.length) { container.append(el('p', { class: 'muted small' }, 'Not enough data yet.')); return; }
  const top = max ?? Math.max(...rows.map(r => r.value || 0), 1e-9);
  for (const r of rows) {
    const fill = el('span', { class: 'hb-fill' });
    fill.style.width = `${Math.max(0, (r.value || 0) / top) * 100}%`;
    fill.style.background = color;
    const row = el(r.href ? 'a' : 'div', { class: 'hb-row', href: r.href || null, title: r.title || null },
      el('span', { class: 'hb-label' }, r.label),
      el('span', { class: 'hb-track' }, fill),
      el('span', { class: 'hb-value' }, r.display ?? String(r.value)));
    container.append(row);
  }
}

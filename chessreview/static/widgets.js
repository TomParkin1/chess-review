// Small display widgets shared by the training pages.

// A Chessly-style progress ring: `done` out of `total`, with a big label in the middle.
export function progressRing(done, total, big, small) {
  const r = 52, c = 2 * Math.PI * r;
  const share = total ? Math.min(1, done / total) : 1;
  const wrap = document.createElement('div');
  wrap.className = 'ring';
  wrap.innerHTML = `
    <svg viewBox="0 0 120 120" width="132" height="132" role="img" aria-label="${done} of ${total} done today">
      <circle cx="60" cy="60" r="${r}" fill="none" stroke="var(--panel-2)" stroke-width="12"/>
      <circle cx="60" cy="60" r="${r}" fill="none" stroke="var(--green)" stroke-width="12" stroke-linecap="round"
              stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - share)}" transform="rotate(-90 60 60)"/>
    </svg>
    <div class="ring-label"><div class="ring-big"></div><div class="ring-small"></div></div>`;
  wrap.querySelector('.ring-big').textContent = big;
  wrap.querySelector('.ring-small').textContent = small;
  return wrap;
}

// A horizontal bar with a label and a count, for summary breakdowns.
export function countBar(label, count, max, color = 'var(--panel-3)') {
  const row = document.createElement('div');
  row.className = 'count-bar';
  row.innerHTML = '<span class="cb-label"></span><span class="cb-track"><span class="cb-fill"></span></span><span class="cb-n"></span>';
  row.querySelector('.cb-label').textContent = label;
  row.querySelector('.cb-n').textContent = count;
  const fill = row.querySelector('.cb-fill');
  fill.style.width = `${max ? (count / max) * 100 : 0}%`;
  fill.style.background = color;
  return row;
}

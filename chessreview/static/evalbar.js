// Vertical evaluation bar (like Lichess): the white part grows with White's winning chances.
import { winChance } from './common.js';

// Short enough for the 24 px bar: "1.2", "-0.5", "12", "M3" (mate either way).
function barLabel(ev) {
  if (ev.mate !== null && ev.mate !== undefined) return `M${Math.abs(ev.mate)}`;
  const v = (ev.cp || 0) / 100;
  return v.toFixed(Math.abs(v) < 9.95 ? 1 : 0).replace(/^-0(\.0)?$/, '0.0');
}

export function createEvalBar(container) {
  const bar = document.createElement('div');
  bar.className = 'evalbar';
  bar.innerHTML = '<div class="evalbar-white"></div><span class="evalbar-label"></span><div class="evalbar-mid"></div>';
  container.prepend(bar);
  const white = bar.querySelector('.evalbar-white');
  const label = bar.querySelector('.evalbar-label');
  let orientation = 'white';

  return {
    // ev: {cp, mate} from White's point of view, or null for "unknown"; text overrides the label.
    set(ev, orient = orientation, text = null) {
      orientation = orient;
      bar.classList.toggle('flipped', orientation === 'black');
      bar.classList.toggle('unknown', !ev);
      if (!ev) {
        white.style.height = '50%';
        label.textContent = '';
        return;
      }
      const share = (1 + winChance(ev)) / 2;           // 0..1 of the bar that is white
      white.style.height = `${(share * 100).toFixed(1)}%`;
      label.textContent = text ?? barLabel(ev);
      // Put the label at the end of whichever side is better.
      label.classList.toggle('on-white', share >= 0.5);
    },
    hide(hidden = true) { bar.classList.toggle('concealed', hidden); },
  };
}

// Vertical evaluation bar (like Lichess): the white part grows with White's winning chances.
import { formatEval, winChance } from './common.js';

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
      const formatted = formatEval(ev).replace(/^\+/, '').replace(/^#/, 'M');
      label.textContent = text ?? formatted.replace(/^M-/, 'M');
      // Put the label at the end of whichever side is better.
      label.classList.toggle('on-white', share >= 0.5);
    },
    hide(hidden = true) { bar.classList.toggle('concealed', hidden); },
  };
}

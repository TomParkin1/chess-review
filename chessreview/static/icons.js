// Move-classification badges as small inline SVG icons (sidebar icons live in base.html).
// The badges are our own simple designs (a coloured disc with a glyph), in the spirit of
// chess.com's Game Review without copying its artwork.

export const MARKS = {
  brilliant:  { label: 'Brilliant',  color: '#26c2a3', glyph: '!!' },
  great:      { label: 'Great',      color: '#749bbf', glyph: '!' },
  best:       { label: 'Best',       color: '#81b64c', glyph: 'star' },
  excellent:  { label: 'Excellent',  color: '#96bc4b', glyph: 'check2' },
  good:       { label: 'Good',       color: '#95af8a', glyph: 'check' },
  book:       { label: 'Book',       color: '#a88865', glyph: 'book' },
  forced:     { label: 'Forced',     color: '#97938e', glyph: 'arrow' },
  inaccuracy: { label: 'Inaccuracy', color: '#f7c045', glyph: '?!' },
  mistake:    { label: 'Mistake',    color: '#ffa459', glyph: '?' },
  miss:       { label: 'Miss',       color: '#ff7769', glyph: 'x' },
  blunder:    { label: 'Blunder',    color: '#fa412d', glyph: '??' },
};

// Order used for the report's count table (as on chess.com).
export const MARK_ORDER = ['brilliant', 'great', 'best', 'excellent', 'good', 'book',
                           'inaccuracy', 'mistake', 'miss', 'blunder'];

const GLYPHS = {
  star: '<path d="M12 5.2l1.95 4.02 4.43.6-3.22 3.08.79 4.4L12 15.2l-3.95 2.1.79-4.4-3.22-3.08 4.43-.6z" fill="#fff"/>',
  check: '<path d="M7.5 12.3l3 3 6-6.4" stroke="#fff" stroke-width="2.4" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
  check2: '<path d="M5.6 12.6l2.6 2.6 5-5.4M10.6 13.4l1.8 1.8 5-5.4" stroke="#fff" stroke-width="2.1" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
  book: '<path d="M6.5 7.5c2-.6 3.8-.3 5.5.8 1.7-1.1 3.5-1.4 5.5-.8v9c-2-.6-3.8-.3-5.5.8-1.7-1.1-3.5-1.4-5.5-.8z" fill="#fff"/><path d="M12 8.3v9" stroke="#a88865" stroke-width="1"/>',
  arrow: '<path d="M7 12h9M13 8.5l3.5 3.5-3.5 3.5" stroke="#fff" stroke-width="2.3" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
  x: '<path d="M8.5 8.5l7 7M15.5 8.5l-7 7" stroke="#fff" stroke-width="2.6" stroke-linecap="round"/>',
};

export function markIcon(mark, size = 18) {
  const m = MARKS[mark];
  if (!m) return '';
  const inner = GLYPHS[m.glyph] ||
    `<text x="12" y="16.2" text-anchor="middle" font-size="${m.glyph.length > 1 ? 10.5 : 13}" font-weight="800" font-family="system-ui, sans-serif" fill="#fff">${m.glyph}</text>`;
  return `<svg class="mark-icon" width="${size}" height="${size}" viewBox="0 0 24 24" role="img" aria-label="${m.label}">` +
         `<circle cx="12" cy="12" r="11" fill="${m.color}" stroke="rgba(0,0,0,.25)" stroke-width="1"/>${inner}</svg>`;
}

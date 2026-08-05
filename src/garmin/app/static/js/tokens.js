/**
 * Resolve a CSS custom property to a real colour value.
 *
 * Several design tokens are declared as color-mix(...) expressions --
 * --lg-text-3 and --lg-border-h among them. getPropertyValue returns that
 * declaration *verbatim*, not a computed colour, so anything that needs an
 * actual colour string gets an unparseable value. Plotly's failure mode is
 * silent: it either drops the element or falls back to black.
 *
 * This has already caused two visible defects -- black bars where muted grey
 * was intended on the Modeling page, and invisible separator rules on the
 * Activities session timeline -- so it lives here rather than being copied
 * into each template, where the fixed and unfixed versions drifted apart.
 *
 * Resolving through a throwaway element forces the browser to compute a real
 * rgb() value. Results are cached; call it freely.
 */
(function () {
  const cache = new Map();

  window.token = function token(name) {
    if (cache.has(name)) return cache.get(name);
    const probe = document.createElement('span');
    probe.style.color = `var(${name})`;
    probe.style.display = 'none';
    document.body.appendChild(probe);
    const resolved = getComputedStyle(probe).color;
    probe.remove();
    cache.set(name, resolved);
    return resolved;
  };

  /**
   * Return `color` at the given opacity (0-1).
   *
   * Needed because token() hands back a computed `rgb(r, g, b)` string, and
   * the obvious way to make a translucent fill -- appending a two-digit hex
   * alpha, as in `color + '33'` -- only works on a hex colour. On an rgb()
   * string it produces `rgb(25, 118, 210)33`, which is not valid CSS, so
   * Plotly silently drops the alpha and paints the band fully opaque. That
   * is exactly how the uncertainty bands on the Health and Running trends
   * lost their transparency.
   *
   * Handles rgb()/rgba() and 3-, 6- and 8-digit hex, and returns the input
   * unchanged if it is some other format rather than guessing.
   */
  window.withAlpha = function withAlpha(color, alpha) {
    if (!color) return color;
    const rgb = String(color).match(/^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/i);
    if (rgb) return `rgba(${rgb[1]}, ${rgb[2]}, ${rgb[3]}, ${alpha})`;

    const hex = String(color).trim().match(/^#([0-9a-f]{3,8})$/i);
    if (hex) {
      let body = hex[1];
      if (body.length === 3) body = body.split('').map((c) => c + c).join('');
      if (body.length >= 6) {
        const [r, g, b] = [0, 2, 4].map((i) => parseInt(body.slice(i, i + 2), 16));
        return `rgba(${r}, ${g}, ${b}, ${alpha})`;
      }
    }
    return color;
  };

  // Themes swap the underlying values, so a cached colour would go stale.
  const media = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)');
  if (media && media.addEventListener) {
    media.addEventListener('change', () => cache.clear());
  }
})();

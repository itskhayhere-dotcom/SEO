// Minimal inline-SVG line charts (no dependencies). <div class="chart" data-src="/api/..."> -> DA / PA / Spam history.
document.querySelectorAll('.chart').forEach(async el => {
  const d = await (await fetch(el.dataset.src)).json();
  const series = [['da', 'DA', '#2b6cb0'], ['pa', 'PA', '#2e8b57'], ['spam', 'Spam', '#c0392b'], ['traffic', 'Traffic', '#8e6bbf']]
    .map(([k, n, c]) => ({n, c, p: (d[k] || []).map(([t, v]) => [new Date(t.replace(' ', 'T') + 'Z').getTime(), v]).filter(x => x[1] != null)}))
    .filter(s => s.p.length);
  if (!series.length) { el.innerHTML = '<span class="muted">No history yet - run “Refresh DA/PA/Spam”.</span>'; return; }
  const W = 760, H = 220, m = 32, all = series.flatMap(s => s.p);
  const x0 = Math.min(...all.map(p => p[0])), x1 = Math.max(...all.map(p => p[0]), x0 + 1);
  const X = t => m + (t - x0) / (x1 - x0) * (W - 2 * m), Y = v => H - m - v / 100 * (H - 2 * m);
  let g = [0, 25, 50, 75, 100].map(v => `<line x1="${m}" x2="${W - m}" y1="${Y(v)}" y2="${Y(v)}" stroke="currentColor" opacity=".12"/><text x="4" y="${Y(v) + 4}" font-size="10" fill="currentColor" opacity=".6">${v}</text>`).join('');
  series.forEach(s => {
    g += `<polyline fill="none" stroke="${s.c}" stroke-width="2" points="${s.p.map(p => X(p[0]) + ',' + Y(p[1])).join(' ')}"/>` +
         s.p.map(p => `<circle cx="${X(p[0])}" cy="${Y(p[1])}" r="3" fill="${s.c}"><title>${s.n} ${p[1]} · ${new Date(p[0]).toLocaleString()}</title></circle>`).join('');
  });
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}">${g}</svg><div class="legend">${series.map(s => `<span style="color:${s.c}">● ${s.n}</span>`).join(' &nbsp; ')} &nbsp; (each point = one metrics run)</div>`;
});

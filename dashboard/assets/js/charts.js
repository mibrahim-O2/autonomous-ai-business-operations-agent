// Dependency-free SVG charts. Colours are design tokens, so charts follow the light and dark themes.
import { h } from "./ui.js";

export const PALETTE = {
  completed: "var(--c-completed)", failed: "var(--c-failed)", running: "var(--c-running)", paused: "var(--c-waiting)",
  waiting_approval: "var(--c-waiting)", cancelled: "var(--c-cancelled)", pending: "var(--c-pending)",
};
export const colorFor = (status) => PALETTE[status] || "var(--c-cancelled)";

export function donut(segments, centerLabel) {
  const total = segments.reduce((sum, s) => sum + s.value, 0);
  const radius = 42, circumference = 2 * Math.PI * radius;
  const svg = h("svg", { class: "chart", viewBox: "0 0 120 120", role: "img", "aria-label": segments.map((s) => `${s.label}: ${s.value}`).join(", ") || "No data" });
  svg.style.maxWidth = "156px";
  svg.append(h("circle", { cx: 60, cy: 60, r: radius, fill: "none", "stroke-width": 13, style: { stroke: "var(--surface-3)" } }));
  let offset = 0;
  for (const segment of segments.filter((s) => s.value > 0)) {
    const length = (segment.value / total) * circumference;
    const arc = h("circle", { cx: 60, cy: 60, r: radius, fill: "none", "stroke-width": 13, "stroke-linecap": "butt",
      "stroke-dasharray": `${Math.max(0, length - 1.2)} ${circumference - length + 1.2}`, "stroke-dashoffset": -offset, transform: "rotate(-90 60 60)", style: { stroke: segment.color } });
    arc.append(h("title", { text: `${segment.label}: ${segment.value}` }));
    svg.append(arc);
    offset += length;
  }
  svg.append(h("text", { x: 60, y: 68, "text-anchor": "middle", class: "donut-center", text: centerLabel }));
  const legend = h("div", { class: "legend" }, segments.map((s) => h("span", {}, h("i", { style: { background: s.color } }), `${s.label} ${s.value}`)));
  return h("div", { class: "donut-wrap" }, svg, legend);
}

// Stacked bars: rows = [{label, parts: [{key, value, color}]}]. Built in HTML, not SVG, so the
// labels keep their size on a phone instead of shrinking with the drawing.
export function stackedBars(rows) {
  const max = Math.max(1, ...rows.map((r) => r.parts.reduce((a, p) => a + p.value, 0)));
  const label = rows.map((r) => `${r.label}: ${r.parts.filter((p) => p.value).map((p) => `${p.key} ${p.value}`).join(", ")}`).join("; ");
  return h("div", { class: "hbars", role: "img", "aria-label": label }, rows.map((row) => {
    const total = row.parts.reduce((a, p) => a + p.value, 0);
    return h("div", { class: "hbar" },
      h("span", { class: "hbar-label", text: row.label.replaceAll("_", " ") }),
      h("div", { class: "hbar-track" }, row.parts.filter((p) => p.value > 0).map((p) => h("span", {
        class: "hbar-part", title: `${p.key.replaceAll("_", " ")}: ${p.value}`,
        style: { width: `${(p.value / max) * 100}%`, background: p.color },
      }))),
      h("span", { class: "hbar-total", text: String(total) }));
  }));
}

// Daily columns: days = [{date, completed, failed, ...}], keys = statuses to stack.
export function columns(days, keys) {
  const width = 640, height = 190, bottom = 24, top = 10, left = 28;
  const max = Math.max(1, ...days.map((d) => keys.reduce((a, k) => a + (d[k] || 0), 0)));
  const slot = (width - left) / Math.max(days.length, 1), barW = Math.min(34, slot * 0.6);
  const svg = h("svg", { class: "chart", viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": days.map((d) => `${d.date}: ${keys.map((k) => `${k} ${d[k] || 0}`).join(", ")}`).join("; ") || "No data" });
  for (const fraction of [0, 0.5, 1]) {
    const y = top + (height - bottom - top) * (1 - fraction);
    svg.append(h("line", { x1: left, x2: width, y1: y, y2: y, class: "grid-line" }));
    svg.append(h("text", { x: left - 6, y: y + 4, "text-anchor": "end", text: String(Math.round(max * fraction)) }));
  }
  days.forEach((day, i) => {
    let y = height - bottom;
    const x = left + i * slot + (slot - barW) / 2;
    for (const key of keys) {
      const value = day[key] || 0;
      if (!value) continue;
      const barH = (value / max) * (height - bottom - top);
      y -= barH;
      const rect = h("rect", { x, y, width: barW, height: barH, rx: 3, style: { fill: colorFor(key) } });
      rect.append(h("title", { text: `${day.date} ${key.replaceAll("_", " ")}: ${value}` }));
      svg.append(rect);
    }
    svg.append(h("text", { x: x + barW / 2, y: height - 6, "text-anchor": "middle", text: day.date.slice(5) }));
  });
  return svg;
}

export function scoreBar(value, max = 1) {
  const fill = h("div", { class: "bar-fill" });
  fill.style.width = `${Math.max(0, Math.min(1, value / max)) * 100}%`;
  return h("div", { class: "bar-track", role: "img", "aria-label": `score ${value}` }, fill);
}

// A labelled horizontal meter: label, value text, and a bar scaled to max.
export function meter(label, value, max, text) {
  return h("div", { class: "meter" }, h("span", { class: "small", text: label }), h("strong", { class: "small", text: text ?? String(value) }), scoreBar(value, max));
}

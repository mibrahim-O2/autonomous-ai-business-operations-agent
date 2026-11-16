// Dependency-free SVG charts.
import { h } from "./ui.js";

export const PALETTE = { completed: "#16a34a", failed: "#dc2626", running: "#2563eb", paused: "#d97706", waiting_approval: "#d97706", cancelled: "#64748b", pending: "#6366f1" };

export function donut(segments, centerLabel) {
  const total = segments.reduce((sum, s) => sum + s.value, 0);
  const radius = 42, circumference = 2 * Math.PI * radius;
  const svg = h("svg", { class: "chart", viewBox: "0 0 120 120", role: "img", "aria-label": segments.map((s) => `${s.label}: ${s.value}`).join(", ") || "No data" });
  svg.style.maxWidth = "150px";
  svg.append(h("circle", { cx: 60, cy: 60, r: radius, fill: "none", stroke: "var(--surface-2)", "stroke-width": 14 }));
  let offset = 0;
  for (const segment of segments.filter((s) => s.value > 0)) {
    const length = (segment.value / total) * circumference;
    svg.append(h("circle", { cx: 60, cy: 60, r: radius, fill: "none", stroke: segment.color, "stroke-width": 14,
      "stroke-dasharray": `${length} ${circumference - length}`, "stroke-dashoffset": -offset, transform: "rotate(-90 60 60)" }));
    offset += length;
  }
  svg.append(h("text", { x: 60, y: 66, "text-anchor": "middle", class: "donut-center", text: centerLabel }));
  const legend = h("div", { class: "legend" }, segments.map((s) => {
    const swatch = h("i"); swatch.style.background = s.color;
    return h("span", {}, swatch, `${s.label} ${s.value}`);
  }));
  return h("div", { class: "donut-wrap" }, svg, legend);
}

// Stacked bars: rows = [{label, parts: [{key, value, color}]}]
export function stackedBars(rows) {
  const width = 620, barH = 16, gap = 14, left = 150, max = Math.max(1, ...rows.map((r) => r.parts.reduce((a, p) => a + p.value, 0)));
  const height = rows.length * (barH + gap) + 6;
  const svg = h("svg", { class: "chart", viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Runs by workflow" });
  rows.forEach((row, i) => {
    const y = i * (barH + gap) + 4;
    svg.append(h("text", { x: 0, y: y + 12, text: row.label.replaceAll("_", " ") }));
    let x = left;
    const total = row.parts.reduce((a, p) => a + p.value, 0);
    svg.append(h("rect", { x: left, y, width: width - left - 34, height: barH, rx: 5, fill: "var(--surface-2)" }));
    for (const part of row.parts.filter((p) => p.value > 0)) {
      const w = (part.value / max) * (width - left - 34);
      const rect = h("rect", { x, y, width: w, height: barH, rx: 5, fill: part.color });
      rect.append(h("title", { text: `${part.key}: ${part.value}` }));
      svg.append(rect);
      x += w;
    }
    svg.append(h("text", { x: width, y: y + 12, "text-anchor": "end", text: String(total) }));
  });
  return svg;
}

export function scoreBar(value, max = 1) {
  const fill = h("div", { class: "bar-fill" });
  fill.style.width = `${Math.max(0, Math.min(1, value / max)) * 100}%`;
  return h("div", { class: "bar-track", role: "img", "aria-label": `score ${value}` }, fill);
}

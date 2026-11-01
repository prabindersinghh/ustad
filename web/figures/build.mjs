// Ustad verification gauge — an isometric instrument that is the loop.
// A sampled frame rides in under a gauge; a grounding probe drops onto the
// step's target feature; the needle swings into PASS / HOLD / LOOK AGAIN and
// the lamp takes the verdict's colour. Slide between three frames: one done
// right, one done wrong, one the camera can't read.
import { writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import * as k from "../../kit/iso-kit.mjs";
import { kitScript } from "../../scripts/inline-kit.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const theme = process.argv.includes("--light") ? "light" : "dark";

const WIDTH = 680;
const HEIGHT = 450;

const PLATE = { x: -150, y: -78, w: 310, d: 150, r: 8 };
const PLATE_H = 12;
const FOOT_H = 5;

const POST = [-120, 2];
const POST_TOP = 132;
const CLAMP_Z = 108;

const DIAL = [60, 0];
const DIAL_R = 22;
const DIAL_Z = 88;
const DIAL_H = 10;
const DIAL_TOP = DIAL_Z + DIAL_H;
const PER = 26;                       // probe px per needle sweep unit

const RAIL_FROM = -96;
const RAIL_TO = 120;
const CARRIAGE = { x: -30, y: -26, w: 60, d: 52, r: 3 };
const CARRIAGE_Z = 3;
const CARRIAGE_TOP = 9;

// Three frames. The target boss height is how firmly the step reads; the
// verdict and lamp colour follow from it.
const FRAMES = [
  { h: 19, verdict: "PASS", conf: "0.94", col: "#12a06a", note: "cap seated flush" },
  { h: 7,  verdict: "HOLD", conf: "0.88", col: "#e5484d", note: "valve left open" },
  { h: 13, verdict: "LOOK AGAIN", conf: "", col: "#e8900a", note: "view obscured" },
];
const TILE_W = 44, TILE_D = 40, STEP = 54;
const CENTRE = -TILE_W / 2;
const STOPS = FRAMES.map((_, i) => DIAL[0] - CENTRE - STEP * i);
const REACH = [STOPS[STOPS.length - 1] - 8, STOPS[0] + 8];

const P = k.fitProjection(
  [
    ...k.boxCorners(PLATE, -PLATE_H - FOOT_H, 0),
    [POST[0], POST[1], POST_TOP + 2],
    [DIAL[0] + DIAL_R, 0, DIAL_TOP],
  ],
  WIDTH, HEIGHT, { pad: 32, azimuth: 44 },
);

const at = (p) => k.iso(p, P);
const S = (paths, style) => k.solidSvg(paths, style);
const L = (d, style) => k.lineSvg(d, style);
const D = (pts, style) => k.dotsSvg(pts, style);
const byDepth = (pts) => [...pts].sort((a, b) => k.depthOf([a[0], a[1], 0], P) - k.depthOf([b[0], b[1], 0], P));

function plate() {
  const out = [`<path class="iso-halo" d="${k.haloOf(PLATE, -PLATE_H - FOOT_H, PLATE_H, P)}"/>`];
  for (const [x, y] of byDepth(k.corners(PLATE, 24))) {
    out.push(S(k.cylinder(x, y, 7, -PLATE_H - FOOT_H, 1.6, P, 28), { tone: "lo" }));
    out.push(S(k.cylinder(x, y, 5.4, -PLATE_H - FOOT_H + 1.6, FOOT_H - 1.6, P, 28), { tone: "mid" }));
  }
  out.push(S(k.slabOf(PLATE, -PLATE_H, PLATE_H, P, 8, 1.6), { tone: "mid" }));
  out.push(L(k.planOutline(k.insetPlan(PLATE, 5), 0, P), { tone: "lo" }));
  const screws = k.corners(PLATE, 10);
  out.push(L(screws.map(([x, y]) => k.ring(x, y, 2.2, 0, P, 16)).join(""), { tone: "lo" }));
  out.push(D(screws.map(([x, y]) => at([x, y, 0])), { size: 0.5 }));
  // engraved name strip with ticks along the front edge
  const strip = { x: -120, y: PLATE.y + PLATE.d - 15, w: 250, d: 7, r: 1 };
  out.push(S(k.slabOf(strip, 0, 0.6, P, 3), { tone: "lo", crease: "none" }));
  const ticks = k.topTicks(strip.x + 4, strip.x + strip.w - 4, 4, 5, strip.y + 0.8, 0.6, [2, 3.6], P);
  out.push(L(ticks.minor, { tone: "lo" }), L(ticks.major, { tone: "mid" }));
  out.push(k.faceTextSvg(k.topMatrix([strip.x + 6, strip.y + 4.6], 0.6, P, "x"), "USTAD  VERIFY", { size: 2.4, tone: "lo" }));
  return out.join("");
}

function rails() {
  const out = [];
  for (const y of [-24, 19]) {
    out.push(S(k.slabOf({ x: RAIL_FROM, y, w: RAIL_TO - RAIL_FROM, d: 5, r: 1 }, 0, 3, P, 3, 0.4), { tone: "mid" }));
    out.push(D([RAIL_FROM + 7, RAIL_TO - 7].map((x) => at([x, y + 2.5, 3])), { size: 0.4 }));
  }
  for (const x of [RAIL_FROM - 5, RAIL_TO]) {
    out.push(S(k.slabOf({ x, y: -27, w: 5, d: 50, r: 1.2 }, 0, 7, P, 3, 0.5), { tone: "mid" }));
    out.push(D([at([x + 2.5, -22, 7]), at([x + 2.5, 22, 7])], { size: 0.45 }));
  }
  return out.join("");
}

function fixture() {
  const out = [];
  // post, clamp, arm carrying the dial
  out.push(S(k.cylinder(POST[0], POST[1], 5.4, 0, CLAMP_Z - 4, P, 28), { tone: "mid", crease: "none" }));
  out.push(L([34, 66, 98].map((z) => k.sideArc(POST[0], POST[1], 5.4, z, P, 16)).join(""), { tone: "faint" }));
  const clamp = { x: POST[0] - 8, y: POST[1] - 8, w: 16, d: 16, r: 2.5 };
  out.push(S(k.slabOf(clamp, CLAMP_Z - 4, 13, P, 4, 0.6), { tone: "mid" }));
  out.push(S(k.cylinder(POST[0], POST[1], 3.6, CLAMP_Z + 9, POST_TOP - CLAMP_Z - 9, P, 28), { tone: "mid", crease: "none" }));
  out.push(S(k.cylinder(POST[0], POST[1], 4, POST_TOP, 2, P, 28), { tone: "mid" }));
  const arm = { x: POST[0] + 8, y: -4, w: DIAL[0] - DIAL_R - POST[0] - 8 + 3, d: 8, r: 2 };
  out.push(S(k.slabOf(arm, CLAMP_Z, 5, P, 4, 0.5), { tone: "mid" }));
  out.push(D([at([arm.x + 5, 0, CLAMP_Z + 5]), at([arm.x + arm.w - 6, 0, CLAMP_Z + 5])], { size: 0.45 }));
  // thumbscrew on the clamp
  const thumb = [clamp.x + 8, clamp.y + clamp.d + 3];
  out.push(S(k.cylinder(thumb[0], thumb[1], 2.8, CLAMP_Z, 6, P, 20), { tone: "mid" }));
  out.push(L(k.knurl(thumb[0], thumb[1], 2.8, CLAMP_Z, CLAMP_Z + 6, 18, P), { tone: "lo" }));
  return out.join("");
}

function carriage() {
  const out = [`<path class="iso-halo" d="${k.haloOf(k.insetPlan(CARRIAGE, 3), CARRIAGE_Z, 0.1, P)}" style="filter:blur(4px)"/>`];
  out.push(S(k.slabOf(CARRIAGE, CARRIAGE_Z, CARRIAGE_TOP - CARRIAGE_Z, P, 5, 0.6), { tone: "mid" }));
  out.push(D([-24, -8, 8, 24].map((x) => at([x, CARRIAGE.y + CARRIAGE.d - 2.6, CARRIAGE_TOP])), { size: 0.4, tone: "lo" }));
  out.push(L(k.lineOnTop([CARRIAGE.x, -17], [CARRIAGE.x + CARRIAGE.w, -17], CARRIAGE_TOP, P), { tone: "faint" }));
  out.push(L(k.lineOnTop([CARRIAGE.x, 17], [CARRIAGE.x + CARRIAGE.w, 17], CARRIAGE_TOP, P), { tone: "faint" }));
  // three frame tiles, each with a raised target boss
  FRAMES.forEach((frame, i) => {
    const bx = -TILE_W / 2 + STEP * i;
    const tile = { x: bx, y: -TILE_D / 2, w: TILE_W, d: TILE_D, r: 2 };
    out.push(S(k.slabOf(tile, CARRIAGE_TOP, 2.4, P, 3, 0.4), { tone: "mid", crease: "lo" }));
    out.push(L(k.planOutline(k.insetPlan(tile, 3), CARRIAGE_TOP + 2.4, P, 3), { tone: "faint" }));
    // the target feature the probe reads: a raised boss
    const boss = { x: bx + TILE_W / 2 - 8, y: -8, w: 16, d: 16, r: 2 };
    out.push(S(k.slabOf(boss, CARRIAGE_TOP + 2.4, frame.h, P, 3, 0.5), { tone: "hi", crease: "lo" }));
    out.push(L(k.planOutline(k.insetPlan(boss, 2.4), CARRIAGE_TOP + 2.4 + frame.h, P, 3), { tone: "faint" }));
  });
  // push handle
  const handle = [CARRIAGE.x + CARRIAGE.w - 8, CARRIAGE.y + CARRIAGE.d + 3];
  out.push(S(k.cylinder(handle[0], handle[1] + 1, 3, CARRIAGE_Z, 5, P, 20), { tone: "mid" }));
  out.push(L(k.knurl(handle[0], handle[1] + 1, 3, CARRIAGE_Z, CARRIAGE_Z + 5, 18, P), { tone: "lo" }));
  return out.join("");
}

// probe: the grounding arm that drops onto the target boss (built at rest, moved in z)
function probe() {
  const out = [];
  out.push(S(k.cylinder(DIAL[0], DIAL[1], 3, 0, 2.6, P, 20), { tone: "hi", lit: true }));
  out.push(S(k.cylinder(DIAL[0], DIAL[1], 1.5, 2.6, 54, P, 16), { tone: "hi", crease: "none", lit: true }));
  return out.join("");
}

function dial() {
  const out = [];
  const [x, y] = DIAL;
  out.push(S(k.cylinder(x, y, 3, 58, DIAL_Z - 58, P, 20), { tone: "mid" }));
  out.push(S(k.cylinder(x, y, DIAL_R, DIAL_Z, DIAL_H, P, 64, 0.8), { tone: "hi", crease: "lo" }));
  out.push(L(k.knurl(x, y, DIAL_R, DIAL_TOP - 3.4, DIAL_TOP, 84, P, 0.3), { tone: "lo" }));
  out.push(L(k.sideArc(x, y, DIAL_R, DIAL_TOP - 3.4, P, 40), { tone: "faint" }));
  out.push(L(k.ring(x, y, DIAL_R - 2.2, DIAL_TOP, P, 64), { tone: "lo" }));
  const ticks = k.radialTicks(x, y, DIAL_R - 3, 36, 6, DIAL_TOP, [1.3, 2.8], P);
  out.push(L(ticks.minor, { tone: "lo" }), L(ticks.major, { tone: "mid" }));
  // three verdict zone labels, engraved at the needle rest angles
  const zoneText = [["PASS", -0.48], ["LOOK", 0], ["HOLD", 0.48]];
  for (const [word, ang] of zoneText) {
    const lx = x + Math.sin(ang) * (DIAL_R - 8);
    const ly = y - Math.cos(ang) * (DIAL_R - 8);
    out.push(k.faceTextSvg(k.topMatrix([lx, ly], DIAL_TOP, P, "x"), word, { size: 2.1, tone: "lo", anchor: "middle" }));
  }
  out.push(L(k.ring(x, y, 1.6, DIAL_TOP, P, 16), { tone: "mid" }));
  return out.join("");
}

// verdict lamp on a short stand; its colour is set live
function lamp() {
  const lx = 118, ly = -44;
  const out = [];
  out.push(S(k.cylinder(lx, ly, 6, 0, 10, P, 24), { tone: "mid" }));
  out.push(L(k.ring(lx, ly, 6, 10, P, 24), { tone: "lo" }));
  const dome = `<g data-part="lamp">${S(k.cylinder(lx, ly, 4.4, 10, 5, P, 24), { tone: "hi", lit: true })}</g>`;
  out.push(dome);
  out.push(L(k.ring(lx, ly, 4.4, 15, P, 20), { tone: "faint" }));
  return out.join("");
}

const needleAt = (turn) => {
  const angle = turn;
  const [x, y] = DIAL;
  return k.lineOnTop(
    [x - Math.sin(angle) * 3.6, y + Math.cos(angle) * 3.6],
    [x + Math.sin(angle) * (DIAL_R - 5), y - Math.cos(angle) * (DIAL_R - 5)],
    DIAL_TOP + 0.2, P,
  );
};
// map boss height -> needle angle (rad): tall reads PASS (left), short HOLD (right)
const angleOf = (h) => (13 - h) * 0.08;

const startX = STOPS[0];
const restTop = CARRIAGE_TOP + 2.4 + FRAMES[0].h;

const svg = k.figureSvg({
  width: WIDTH, height: HEIGHT,
  label:
    "An isometric verification gauge. A carriage carries three sampled frames under a dial gauge on a post. A grounding probe drops onto each frame's raised target. The needle swings into PASS, LOOK AGAIN or HOLD, and the lamp takes the verdict's colour. This is Ustad's loop: see, ground, judge, act.",
  body: [
    plate(),
    rails(),
    fixture(),
    lamp(),
    `<g data-part="carriage" transform="${k.translateAlong(P, [startX, 0, 0])}">${carriage()}</g>`,
    `<g data-part="probe" transform="${k.translateAlong(P, [0, 0, restTop])}">${probe()}</g>`,
    dial(),
    `<path data-part="needle" class="iso-line" data-tone="lit" data-free="start" d="${needleAt(angleOf(FRAMES[0].h))}" style="stroke-width:1.2"/>`,
  ],
});

const live = `
const P = ${JSON.stringify(P)};
const FRAMES = ${JSON.stringify(FRAMES)};
const DIAL = ${JSON.stringify(DIAL)};
const DIAL_R = ${DIAL_R}, DIAL_TOP = ${DIAL_TOP}, STEP = ${STEP}, TILE_W = ${TILE_W};
const BASE = ${CARRIAGE_TOP + 2.4}, REACH = ${JSON.stringify(REACH)}, stops = ${JSON.stringify(STOPS)};
const stage = document.querySelector(".iso-stage");
const svg = stage.querySelector("svg");
const carriage = svg.querySelector('[data-part="carriage"]');
const probe = svg.querySelector('[data-part="probe"]');
const needle = svg.querySelector('[data-part="needle"]');
const lampPaths = svg.querySelectorAll('[data-part="lamp"] path');
const readout = document.querySelector("[data-readout]");
const still = matchMedia("(prefers-reduced-motion: reduce)");
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const needleAt = ${needleAt.toString().replace(/k\.lineOnTop/g, "lineOnTop")};
const angleOf = ${angleOf.toString()};
const frameUnder = (cx) => {
  let best = 0, bestD = 1e9;
  for (let i = 0; i < FRAMES.length; i++) { const d = Math.abs(cx - stops[i]); if (d < bestD) { bestD = d; best = i; } }
  return best;
};
const heightUnder = (cx) => {
  for (let i = 0; i < FRAMES.length; i++) {
    const bx = -TILE_W / 2 + STEP * i + TILE_W / 2 - 8;   // boss near-x on the tile
    if (DIAL[0] >= cx + bx - 10 && DIAL[0] <= cx + bx + 18) return BASE + FRAMES[i].h;
  }
  return BASE;
};
let x = ${startX}, target = x, tip = { at: ${restTop}, rate: 0 };
let frame = 0, last = 0, clock = 0, touring = true, visible = false, idle = 0;
const tourAt = (t) => stops[Math.floor(t / 2.6) % stops.length];
function draw() {
  carriage.setAttribute("transform", translateAlong(P, [x, 0, 0]));
  probe.setAttribute("transform", translateAlong(P, [0, 0, tip.at]));
  const h = tip.at - BASE;
  needle.setAttribute("d", needleAt(angleOf(h)));
  const i = frameUnder(x);
  const f = FRAMES[i];
  lampPaths.forEach((p) => { p.style.fill = f.col; });
  const conf = f.conf ? " \\u00b7 conf " + f.conf : "";
  const text = f.note + ": " + f.verdict + conf;
  if (readout.textContent !== text) readout.textContent = text;
  stage.setAttribute("aria-valuetext", text);
}
function tick(now) {
  const dt = last ? Math.min((now - last) / 1000, 1 / 30) : 1 / 60;
  last = now;
  const calm = still.matches;
  if (touring) { clock += dt; target = tourAt(clock); }
  x = calm ? target : x + (target - x) * (1 - Math.exp(-dt / 0.3));
  if (Math.abs(target - x) < 0.01) x = target;
  const want = heightUnder(x);
  const stiff = 420, drag = calm ? 2 * Math.sqrt(420) : 26;
  for (let n = 0; n < 4; n++) { tip.rate += ((want - tip.at) * stiff - tip.rate * drag) * dt / 4; tip.at += tip.rate * dt / 4; }
  if (Math.abs(want - tip.at) < 0.005 && Math.abs(tip.rate) < 0.005) { tip.at = want; tip.rate = 0; }
  draw();
  const settled = !touring && x === target && tip.rate === 0 && tip.at === want;
  frame = visible && !settled ? requestAnimationFrame(tick) : 0;
}
const run = () => { if (!frame && visible) { last = 0; frame = requestAnimationFrame(tick); } };
const steer = (next) => { touring = false; target = clamp(next, REACH[0], REACH[1]); clearTimeout(idle); idle = setTimeout(() => { touring = !still.matches; clock = 0; run(); }, 3800); run(); };
new IntersectionObserver((e) => { visible = e[e.length - 1].isIntersecting; if (visible) run(); }, { rootMargin: "120px 0px" }).observe(stage);
if (still.matches) touring = false;
stage.addEventListener("pointermove", (e) => {
  const box = svg.getBoundingClientRect();
  const share = clamp(((e.clientX - box.left) / box.width - 0.2) / 0.6, 0, 1);
  steer(REACH[0] + share * (REACH[1] - REACH[0]));
});
stage.addEventListener("pointerleave", () => steer(target));
stage.addEventListener("keydown", (e) => {
  const here = stops.reduce((b, s, i) => (Math.abs(s - target) < Math.abs(stops[b] - target) ? i : b), 0);
  let to = null;
  if (e.key === "ArrowRight" || e.key === "ArrowUp") to = Math.max(0, here - 1);
  if (e.key === "ArrowLeft" || e.key === "ArrowDown") to = Math.min(stops.length - 1, here + 1);
  if (e.key === "Home") to = 0;
  if (e.key === "End") to = stops.length - 1;
  if (to === null) return;
  e.preventDefault();
  steer(stops[to]);
});
draw();
`;

const body = k.plateHtml({
  fig: "Fig 1",
  title: "Verification gauge",
  hint: "Hover to slide between frames",
  readout: `${FRAMES[0].note}: ${FRAMES[0].verdict} · conf ${FRAMES[0].conf}`,
  keys: [
    { mark: "lit", label: "Grounding probe and needle: what moves when a frame is judged" },
    { mark: "raised", label: "Three sampled frames: done right, done wrong, unreadable" },
  ],
  caption:
    "Ustad's loop as one instrument. A sampled frame rides in, the grounding probe drops onto the step's target, and the needle commits to PASS, HOLD or LOOK AGAIN. The lamp takes the verdict's colour. The reading comes from the frame, not from a guess.",
  body: `<div class="iso-stage" tabindex="0" role="slider" aria-label="Verification gauge: arrow keys move between frames" aria-valuemin="0" aria-valuemax="2" aria-valuenow="0">${svg}</div>`,
});

// Ustad's cool blue-white paper instead of the kit's warm grey, so the figure
// belongs to the site it sits in.
const COOL = `<style>
.iso[data-theme="light"]{
  --anatomy-page:#ffffff;--anatomy-card:#ffffff;--anatomy-text:#4a5670;--anatomy-strong:#0a1020;--anatomy-muted:#8592aa;
  --anatomy-paper:#ffffff;--anatomy-face:#ffffff;--anatomy-top:#ffffff;
  --anatomy-shade-0:#d6dfee;--anatomy-shade-1:#e1e8f4;--anatomy-shade-2:#ebf0f9;--anatomy-shade-3:#f4f7fc;
  --anatomy-lit-top:#ffffff;--anatomy-lit-shade:#e9effa;
  --anatomy-hi:#14203a;--anatomy-mid:#8494b0;--anatomy-lo:#b5c2d8;--anatomy-faint:#d6dfed;--anatomy-lit:#1a56e0;
  --anatomy-halo:#5d74a3;--anatomy-halo-opacity:.26;--anatomy-dot:#26324d;
}</style>`;
const embed = process.argv.includes("--embed");
// Embedded in the landing page: the page supplies the frame and the caption.
const EMBED = `<style>
html,body{background:#fff!important;margin:0}
.iso-page{padding:8px 12px!important;margin:0 auto!important}
.iso-plate{border:0!important;box-shadow:none!important;background:transparent!important}
.iso-legend{display:none!important}
</style>`;

const out = embed ? "ustad-gauge-embed.html" : theme === "light" ? "ustad-gauge-light.html" : "ustad-gauge.html";
const styled = (theme === "light" ? COOL : "") + (embed ? EMBED : "") + body;
writeFileSync(
  join(HERE, out),
  k.pageHtml({ title: "Ustad verification gauge", theme, body: styled, script: kitScript() + live, width: embed ? 860 : 650 }),
);
console.log("wrote", out);

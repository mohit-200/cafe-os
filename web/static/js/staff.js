import { api, h, minutesBetween, mount, poller, toast } from "./dom.js";

const app = document.getElementById("app");
const NEED_ICONS = { quiet: "🤫", power: "🔌", calls: "📞", wifi: "📶", meeting: "🤝", quick: "⚡", outdoor: "🌿", comfy: "🛋️" };
const NOISE = [[1, "🔈", "Quiet"], [3, "🔉", "Moderate"], [5, "🔊", "Loud"]];
const SCENARIOS = [
  ["lunch_rush", "🍽️ Lunch rush"],
  ["barista_break", "☕ Barista on break"],
  ["quiet_fill", "🤫 Quiet zone fills"],
  ["reset", "↺ Reset demo"],
];

const S = { cafeId: null, token: null, board: null, busy: false };
const tokenKey = () => `cafeos.staff.${S.cafeId}`;

function readToken() {
  try { return sessionStorage.getItem(tokenKey()); } catch { return null; }
}
function saveToken(t) {
  try { if (t) sessionStorage.setItem(tokenKey(), t); else sessionStorage.removeItem(tokenKey()); } catch { /* private mode */ }
}

const base = () => `/api/staff/cafes/${S.cafeId}`;
const headers = () => ({ Authorization: `Bearer ${S.token}` });

async function init() {
  const params = new URLSearchParams(location.search);
  S.cafeId = params.get("cafe") || (await api("/api/config")).cafe_id;
  S.token = readToken();
  if (!S.token) return renderLogin();
  poller(refresh, 2000);
}

async function refresh() {
  if (!S.token) return;
  const since = S.board ? `?since=${encodeURIComponent(S.board.version)}` : "";
  try {
    const b = await api(`${base()}/board${since}`, { headers: headers() });
    if (!b.unchanged) {
      S.board = b;
      render();
    }
  } catch (e) {
    if (e.status === 401) signOut();
    else throw e;
  }
}

async function send(path, body) {
  if (S.busy) return;
  S.busy = true;
  try {
    S.board = await api(`${base()}${path}`, { method: "POST", headers: headers(), body });
    render();
  } catch (e) {
    if (e.status === 401) return signOut();
    toast(e.message);
  } finally {
    S.busy = false;
  }
}

function signOut() {
  saveToken(null);
  S.token = null;
  S.board = null;
  renderLogin();
}

// ---------- login ----------

function renderLogin() {
  const input = h("input", { type: "password", inputmode: "numeric", autocomplete: "current-password", maxlength: "12", "aria-label": "Staff PIN", placeholder: "PIN" });
  const err = h("p", { class: "err", role: "alert" });
  const submit = async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/staff/login", { method: "POST", body: { cafe_id: S.cafeId, pin: input.value } });
      S.token = r.token;
      saveToken(r.token);
      poller(refresh, 2000);
    } catch (ex) {
      err.textContent = ex.status === 401 ? "That PIN didn't work." : ex.message;
      input.value = "";
      input.focus();
    }
  };
  mount(app, h("div", { class: "login" },
    h("form", { class: "card", onsubmit: submit },
      h("div", { class: "brand faint" }, "◐ CAFÉ OS"),
      h("h1", null, "Staff board"),
      h("p", { class: "muted" }, "Enter the shift PIN"),
      input,
      h("button", { class: "btn primary block", type: "submit" }, "Open board"),
      err)));
  input.focus();
}

// ---------- board ----------

function render() {
  const b = S.board;
  if (!b) return;
  mount(app,
    topBar(b),
    h("div", { class: "board" },
      h("section", null,
        h("div", { class: "col-title" }, h("h2", null, "Zones"), h("span", { class: "faint" }, "Bars show the next 2 hours: free · tight · full")),
        h("div", { class: "zones-grid" }, b.zones.map(zoneTile))),
      h("section", null,
        h("div", { class: "col-title" }, h("h2", null, "Needs attention"), h("span", { class: "faint" }, `${b.alerts.length} open`)),
        h("div", { class: "feed" }, b.alerts.length ? b.alerts.map(alertCard) : h("div", { class: "empty" }, "All calm. Nothing needs you right now.")),
        h("div", { class: "col-title" }, h("h2", null, "Arriving"), h("span", { class: "faint" }, `${b.incoming.length} planned`)),
        h("div", { class: "visits" }, b.incoming.length ? b.incoming.map((v) => visitCard(v, b)) : h("div", { class: "empty" }, "No planned arrivals yet.")),
        h("div", { class: "col-title" }, h("h2", null, "Seated with a plan"), h("span", { class: "faint" }, `${b.seated.length}`)),
        h("div", { class: "visits" }, b.seated.length ? b.seated.map((v) => visitCard(v, b)) : h("div", { class: "empty" }, "Nobody seated through CAFÉ OS yet.")),
        h("div", { class: "stats" },
          h("span", null, `📝 ${b.stats.declared_today} visit${b.stats.declared_today === 1 ? "" : "s"} planned today`),
          h("span", null, `⏳ Guests stay ~${b.live.overstay_min} min past their plan (learned)`),
          b.stats.rated ? h("span", null, `🎯 ${b.stats.accurate}/${b.stats.rated} said the plan was accurate`) : null))),
    b.demo_controls ? h("div", { class: "demo" },
      h("span", { class: "label" }, "Demo"),
      SCENARIOS.map(([name, label]) => h("button", {
        class: "btn small ghost", type: "button",
        onclick: () => { if (name !== "reset" || confirm("Reset the demo café? All visits will be cleared.")) send("/scenario", { name }); },
      }, label))) : null);
}

function topBar(b) {
  const stepper = (label, value, field) => h("span", { class: "stat" },
    h("span", { class: "faint" }, label), h("b", null, value),
    h("span", { class: "stepper" },
      h("button", { type: "button", "aria-label": `Fewer ${label}`, onclick: () => send("/live", { [field]: -1 }) }, "−"),
      h("button", { type: "button", "aria-label": `More ${label}`, onclick: () => send("/live", { [field]: 1 }) }, "+")));
  const waitClass = b.wait.p90 >= 14 ? "critical" : b.wait.p90 >= 9 ? "warn" : "ok";
  return h("header", { class: "bar" },
    h("div", null, h("div", { class: "faint" }, "◐ CAFÉ OS · staff"), h("h1", null, b.cafe.name)),
    h("span", { class: "clock" }, b.now),
    h("span", { class: `pill ${waitClass}` }, `☕ ${b.wait.p50}–${b.wait.p90} min wait`),
    stepper("Baristas", b.live.baristas, "baristas_delta"),
    stepper("Queue", b.live.queue_len, "queue_delta"),
    b.live.rush ? h("span", { class: "pill critical" }, "Rush forecast") : null,
    b.orders_paused ? h("span", { class: "pill warn" }, "Phone orders paused") : null,
    h("span", { class: "spacer" }),
    h("span", { class: "row faint" }, h("span", { class: "live-dot" }), "live"),
    h("button", { class: "btn small ghost", type: "button", onclick: signOut }, "Sign out"));
}

function zoneTile(z) {
  const tags = [z.has_power ? "🔌" : null, z.call_friendly ? "📞" : "🚫📞"].filter(Boolean).join(" ");
  return h("article", { class: `card zone ${z.steering ? "steering" : ""}` },
    h("div", { class: "zone-head" },
      h("span", { "aria-hidden": "true" }, z.emoji),
      h("span", { class: "name" }, z.name),
      h("span", { class: "tags", title: "Power / calls" }, tags),
      h("span", { class: "spacer" }),
      z.steering ? h("span", { class: "pill warn" }, "Steering away") : null),
    h("div", { class: "occ" },
      h("button", { class: "round", type: "button", "aria-label": `${z.name}: one seat freed`, onclick: () => send(`/zones/${z.id}`, { occupied_delta: -1 }) }, "−"),
      h("span", { class: "num" }, z.occupied, h("small", null, ` / ${z.seats}`)),
      h("button", { class: "round", type: "button", "aria-label": `${z.name}: one more seat taken`, onclick: () => send(`/zones/${z.id}`, { occupied_delta: 1 }) }, "+"),
      h("span", { class: "spacer" }),
      z.planned ? h("span", { class: "pill info" }, `${z.planned} planned`) : null),
    h("div", { class: "noise" },
      h("span", { class: `pill ${z.noise_label}` }, z.noise_label),
      h("span", { class: "spacer" }),
      NOISE.map(([lvl, icon, label]) => h("button", {
        type: "button", class: z.pulse_noise === lvl ? "on" : "", title: `Report: ${label}`, "aria-label": `${z.name} noise ${label}`,
        onclick: () => send(`/zones/${z.id}`, { noise: lvl }),
      }, icon))),
    h("div", null,
      h("div", { class: "strip", role: "img", "aria-label": `${z.name} forecast for the next two hours` },
        z.strip.map((s) => h("span", { class: s.state, title: `${s.t}: ~${Math.max(0, s.free)} free` }))),
      h("div", { class: "strip-axis" }, h("span", null, "now"), h("span", null, "+1h"), h("span", null, "+2h"))));
}

function alertCard(a) {
  return h("article", { class: `card alert ${a.severity}` },
    h("div", { class: "title" }, a.title),
    h("div", { class: "text" }, a.text),
    h("div", { class: "row" },
      a.action_label ? h("button", { class: "btn small primary", type: "button", onclick: () => send(`/alerts/${encodeURIComponent(a.id)}`, { action: "accept" }) }, a.action_label) : null,
      h("button", { class: "btn small ghost", type: "button", onclick: () => send(`/alerts/${encodeURIComponent(a.id)}`, { action: "dismiss" }) }, a.action_label ? "Dismiss" : "Got it")));
}

function visitCard(v, b) {
  const needs = v.needs.map((n) => NEED_ICONS[n] || "").join("");
  const btn = (label, path, body, cls = "ghost") => h("button", { class: `btn small ${cls}`, type: "button", onclick: () => send(path, body) }, label);
  let timing;
  if (v.status === "seated") {
    const over = minutesBetween(v.leave_by, b.now);
    timing = h("span", { class: `pill ${over > 0 ? "warn" : "ok"}` }, over > 0 ? `${over} min past ${v.leave_by}` : `leaves ${v.leave_by}`);
  } else {
    timing = h("span", { class: "pill info" }, v.status === "arrived" ? "Here now" : `ETA ${v.arrive_by}`);
  }
  const orderNext = { sent: ["Start order", "accept"], accepted: ["Mark ready", "ready"], ready: ["Collected", "collected"] };
  return h("article", { class: "card visit" },
    h("div", { class: "top" },
      h("span", { class: "code" }, v.code),
      h("div", null,
        h("div", null, `${v.zone_emoji} ${v.zone_name}${v.party_size > 1 ? ` · 👥 ${v.party_size}` : ""}`),
        h("div", { class: "faint" }, `${v.arrive_by} → ${v.leave_by} · ${v.duration_min} min`)),
      h("span", { class: "spacer" }),
      h("span", { class: "needs", title: v.needs.join(", ") }, needs),
      timing),
    v.late_min ? h("span", { class: "pill warn" }, `Running ${v.late_min} min late`) : null,
    v.replan_pending ? h("span", { class: "pill info" }, "Guest is choosing a new option") : null,
    v.order ? h("div", { class: "order" },
      h("span", null, v.order.items.join(", ")),
      h("span", { class: "spacer" }),
      h("span", { class: "faint" }, v.order.status),
      orderNext[v.order.status] ? btn(orderNext[v.order.status][0], `/orders/${v.id}`, { action: orderNext[v.order.status][1] }, "primary") : null) : null,
    h("div", { class: "btns" },
      v.status === "confirmed" ? btn("Arrived", `/visits/${v.id}`, { action: "arrived" }) : null,
      v.status !== "seated" ? btn("Seated", `/visits/${v.id}`, { action: "seated" }) : null,
      v.status === "confirmed" ? btn("No-show", `/visits/${v.id}`, { action: "no_show" }) : null,
      v.status === "seated" ? btn("Left", `/visits/${v.id}`, { action: "left" }, "primary") : null));
}

init().catch((e) => mount(app, h("p", { class: "err" }, `Couldn't load: ${e.message}`)));

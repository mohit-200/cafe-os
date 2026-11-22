import { api, h, hhmm, minutesBetween, mount, poller, storage, tierBadge, toast, TIERS } from "./dom.js";

const app = document.getElementById("app");
const clockEl = document.getElementById("clock");

const S = {
  cafeId: null,
  pulse: null,
  view: "home", // home | clarify | plan | visit
  text: "",
  busy: false,
  error: null,
  intentResult: null,
  extraSummary: [],
  visit: null,
  token: null,
  selected: null,
  order: new Set(),
  lastVisitJson: "",
};

const prefs = storage.get("cafeos.prefs") || {};
const EXAMPLES = [
  "Work 90 min, two video calls, quiet please, out by 4",
  "45 minutes before my next meeting. Quiet place and a coffee",
  "Catching up with a friend for an hour, sofas please",
  "Quick cold brew to go",
];
const BAND = { likely: "Seat likely", possible: "Seat possible", unlikely: "Seats scarce" };
const ACTIVE = ["confirmed", "arrived", "seated"];

const visitKey = () => `cafeos.visit.${S.cafeId}`;
const visitPath = (id) => `/api/cafes/${S.cafeId}/visits/${id}`;
const auth = () => ({ "X-Visit-Token": S.token });

// ---------- data ----------

async function init() {
  const m = location.pathname.match(/^\/c\/([\w-]{1,64})/);
  S.cafeId = m ? m[1] : (await api("/api/config")).cafe_id;
  const saved = storage.get(visitKey());
  if (saved && saved.id && saved.token) {
    try {
      S.token = saved.token;
      S.visit = await api(visitPath(saved.id), { headers: auth() });
      S.lastVisitJson = JSON.stringify(S.visit);
      if (ACTIVE.includes(S.visit.status) || (S.visit.status === "done" && !S.visit.feedback)) S.view = "visit";
      else forgetVisit();
    } catch {
      forgetVisit();
    }
  }
  poller(refresh, 3000);
}

function forgetVisit() {
  storage.del(visitKey());
  S.visit = null;
  S.token = null;
  S.lastVisitJson = "";
}

async function refresh() {
  const since = S.pulse ? `?since=${encodeURIComponent(S.pulse.version)}` : "";
  const p = await api(`/api/cafes/${S.cafeId}/pulse${since}`);
  const first = !S.pulse;
  if (!p.unchanged) {
    S.pulse = p;
    clockEl.textContent = `${p.now} · café time`;
    if (first) render();
    else updatePulse();
  }
  if (S.view === "visit" && S.visit) {
    const v = await api(visitPath(S.visit.id), { headers: auth() });
    const json = JSON.stringify({ ...v, now: undefined });
    if (json !== S.lastVisitJson) {
      const hadReplan = !!(S.visit && S.visit.replan);
      S.visit = v;
      S.lastVisitJson = json;
      if (v.replan && !hadReplan && navigator.vibrate) navigator.vibrate(120);
      render();
    } else {
      S.visit.now = v.now;
      updateCountdown();
    }
  }
}

// ---------- actions ----------

async function submit(textOverride) {
  const text = (textOverride ?? S.text).trim();
  if (textOverride) S.text = textOverride;
  if (text.length < 2) {
    S.error = "Tell us a little about your visit.";
    return render();
  }
  S.busy = true;
  S.error = null;
  S.extraSummary = [];
  render();
  try {
    S.intentResult = await api(`/api/cafes/${S.cafeId}/intent`, { method: "POST", body: { text } });
    if (S.intentResult.question) {
      S.view = "clarify";
    } else {
      return makePlan();
    }
  } catch (e) {
    S.error = e.message;
  }
  S.busy = false;
  render();
}

async function makePlan(patch, label) {
  if (patch) {
    S.intentResult.intent = { ...S.intentResult.intent, ...patch };
    if (label) S.extraSummary.push(`⏱ ${label}`);
  }
  S.busy = true;
  render();
  try {
    const r = await api(`/api/cafes/${S.cafeId}/plans`, {
      method: "POST",
      body: { intent: S.intentResult.intent, text: S.text },
    });
    S.visit = r.visit;
    S.token = r.token;
    S.selected = r.visit.plan.id;
    S.order = new Set(r.visit.intent.order_item_ids);
    S.view = "plan";
    S.error = null;
  } catch (e) {
    S.error = e.message;
    if (S.view !== "clarify") S.view = "home";
  }
  S.busy = false;
  render();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function confirmVisit() {
  S.busy = true;
  S.error = null;
  render();
  try {
    const items = S.pulse && S.pulse.orders_paused ? [] : [...S.order];
    const v = await api(`${visitPath(S.visit.id)}/confirm`, {
      method: "POST",
      headers: auth(),
      body: { option_id: S.selected, order_item_ids: items },
    });
    S.visit = v;
    S.lastVisitJson = JSON.stringify({ ...v, now: undefined });
    storage.set(visitKey(), { id: v.id, token: S.token });
    if (items.length) {
      prefs.lastOrder = items;
      storage.set("cafeos.prefs", prefs);
    }
    S.view = "visit";
    toast(v.order ? "You're confirmed. Your order is with the barista." : "You're confirmed. The café knows you're coming.");
  } catch (e) {
    if (e.status === 409 && e.data && e.data.options && e.data.options.length) {
      S.visit.plan = e.data.options[0];
      S.visit.alternatives = e.data.options.slice(1);
      S.selected = S.visit.plan.id;
    }
    S.error = e.message;
  }
  S.busy = false;
  render();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function act(action, extra = {}) {
  if (action === "cancel" && !confirm("Cancel this visit? The café will free up your spot.")) return;
  S.busy = true;
  render();
  try {
    const v = await api(`${visitPath(S.visit.id)}/action`, { method: "POST", headers: auth(), body: { action, ...extra } });
    S.visit = v;
    S.lastVisitJson = JSON.stringify({ ...v, now: undefined });
    if (action === "cancel") {
      forgetVisit();
      S.view = "home";
      toast("Visit cancelled.");
    }
    if (action === "feedback") {
      forgetVisit();
      S.view = "home";
      toast("Thanks! That helps the café plan better.");
    }
    if (action === "late") toast(v.replan ? "Got it. We've updated your plan." : "Got it. The café has been told.");
  } catch (e) {
    toast(e.message);
  }
  S.busy = false;
  render();
}

async function chooseReplan(optionId) {
  S.busy = true;
  render();
  try {
    const v = await api(`${visitPath(S.visit.id)}/replan`, { method: "POST", headers: auth(), body: { option_id: optionId } });
    S.visit = v;
    S.lastVisitJson = JSON.stringify({ ...v, now: undefined });
    toast(`Switched to ${v.plan.zone_name}. The café has been updated.`);
  } catch (e) {
    toast(e.message);
  }
  S.busy = false;
  render();
}

function startOver() {
  S.view = "home";
  S.error = null;
  S.intentResult = null;
  if (S.visit && !ACTIVE.includes(S.visit.status)) forgetVisit();
  render();
}

// ---------- views ----------

function render() {
  if (!S.pulse) return;
  const views = { home: viewHome, clarify: viewClarify, plan: viewPlan, visit: viewVisit };
  mount(app, views[S.view]());
}

function pulseContent() {
  const p = S.pulse;
  return [
    h("div", { class: "pulse-head" },
      h("strong", null, "Right now"),
      h("span", { class: "faint" }, `☕ ${p.wait.p50}–${p.wait.p90} min at the counter`)),
    h("div", { class: "zones" },
      p.zones.filter((z) => z.type !== "booth").map((z) =>
        h("div", { class: "zone-chip" },
          h("div", null, z.emoji),
          h("div", { class: "name" }, z.name),
          h("div", { class: "meta" }, h("span", { class: `dot ${z.state}` }), `${z.likely_free} free · ${z.noise_label}`)))),
  ];
}

function updatePulse() {
  const el = document.getElementById("pulse");
  if (el) mount(el, pulseContent());
}

function viewHome() {
  const p = S.pulse;
  const ta = h("textarea", {
    id: "ask",
    maxlength: "500",
    placeholder: "e.g. I need to work for 90 minutes, take two calls, and leave by 4",
    "aria-label": "Describe your visit",
    oninput: (e) => { S.text = e.target.value; },
    onkeydown: (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } },
  });
  ta.value = S.text;
  const examples = [...EXAMPLES];
  if (prefs.lastOrder && prefs.lastOrder.length) {
    const names = prefs.lastOrder.map((id) => (p.menu.find((m) => m.id === id) || {}).name).filter(Boolean);
    if (names.length) examples.unshift(`My usual: ${names.join(" and ")}, 30 min`);
  }
  return h("div", { class: "rise" },
    h("section", { class: "hero" },
      h("h1", null, p.cafe.name),
      h("p", { class: "muted" }, `${p.cafe.neighbourhood} · open ${p.cafe.open_time}–${p.cafe.close_time}`)),
    h("section", { class: "card pulse", id: "pulse" }, pulseContent()),
    h("section", { class: "card composer" },
      h("h2", null, "What do you need from this visit?"),
      h("div", { class: "input-wrap" }, ta, micButton(ta)),
      h("div", { class: "examples" }, examples.map((ex) => h("button", { class: "chip", type: "button", onclick: () => submit(ex) }, ex))),
      h("button", { class: "btn primary block", type: "button", disabled: S.busy, onclick: () => submit() },
        S.busy ? "Planning your visit…" : "Plan my visit"),
      S.error ? h("p", { class: "err", role: "alert" }, S.error) : null,
      h("p", { class: "faint", style: { "margin-top": "10px", "text-align": "center" } },
        p.ai ? "✨ Understood by Gemini. Seats and waits come from live café data." : "Seats and waits come from live café data.")),
    h("div", { class: "legend" }, Object.keys(TIERS).map(tierBadge)),
  );
}

function understood() {
  const r = S.intentResult;
  if (!r) return null;
  const chips = [...r.summary, ...S.extraSummary];
  return h("div", null,
    h("div", { class: "source" }, r.source === "gemini" ? "✨ What Gemini understood" : "What we understood"),
    h("div", { class: "understood" },
      chips.length ? chips.map((c) => h("span", { class: "chip static" }, c)) : h("span", { class: "faint" }, "Just a visit"),
      h("button", { class: "chip", type: "button", onclick: startOver }, "✎ Edit")));
}

function viewClarify() {
  const q = S.intentResult.question;
  return h("div", { class: "rise" },
    understood(),
    h("section", { class: "card sheet" },
      h("h2", null, q.text),
      h("div", { class: "chips" },
        q.chips.map((c) => h("button", { class: "chip", type: "button", disabled: S.busy, onclick: () => makePlan(c.patch, c.label) }, c.label))),
      S.error ? h("p", { class: "err", role: "alert" }, S.error) : null));
}

function timeline(items) {
  return h("ul", { class: "timeline" },
    items.map((it) => h("li", null,
      h("span", { class: "ic", "aria-hidden": "true" }, it.icon),
      h("span", { class: "tx" }, it.text),
      tierBadge(it.tier))));
}

function viewPlan() {
  const v = S.visit;
  const options = [v.plan, ...v.alternatives];
  const o = options.find((x) => x.id === S.selected) || v.plan;
  const others = options.filter((x) => x.id !== o.id);
  const paused = S.pulse.orders_paused;
  const n = S.order.size;
  return h("div", { class: "rise" },
    understood(),
    h("section", { class: "card plan-hero" },
      h("div", { class: "emoji", "aria-hidden": "true" }, o.zone_emoji),
      h("h2", null, o.zone_name),
      h("div", { class: "headline" }, o.headline),
      h("div", { class: "badges" },
        h("span", { class: `pill ${o.seat_band}` }, `${BAND[o.seat_band]} · ${Math.round(o.seat_likelihood * 100)}%`),
        h("span", { class: "pill" }, `${hhmm(o.arrive_by)} → ${hhmm(o.leave_by)}`),
        h("span", { class: `pill ${o.noise_label}` }, o.noise_label)),
      o.warnings.map((w) => h("div", { class: "warn-box" }, w)),
      timeline(o.items)),
    h("h3", { class: "section-title" }, "Your order (optional)"),
    paused
      ? h("p", { class: "faint" }, "📋 The café has paused phone orders while the counter is busy. You can order when you arrive.")
      : h("div", null,
        h("div", { class: "menu" },
          S.pulse.menu.map((m) => h("button", {
            class: `chip ${S.order.has(m.id) ? "on" : ""}`,
            type: "button",
            "aria-pressed": S.order.has(m.id) ? "true" : "false",
            onclick: () => {
              if (S.order.has(m.id)) S.order.delete(m.id);
              else if (S.order.size < 4) S.order.add(m.id);
              render();
            },
          }, `${m.emoji} ${m.name}`))),
        h("p", { class: "faint", style: { "margin-top": "8px" } }, "Sent to the barista only when you confirm. Pay at the counter, no card details here.")),
    others.length ? h("h3", { class: "section-title" }, "Other options") : null,
    h("div", { class: "alts" },
      others.map((x) => h("button", { class: "card alt", type: "button", onclick: () => { S.selected = x.id; render(); window.scrollTo({ top: 0, behavior: "smooth" }); } },
        h("span", { class: "emoji", "aria-hidden": "true" }, x.zone_emoji),
        h("span", null, h("div", { class: "name" }, x.zone_name), h("div", { class: "sub" }, x.headline)),
        h("span", { class: `pill ${x.seat_band}` }, BAND[x.seat_band])))),
    S.error ? h("p", { class: "err", role: "alert" }, S.error) : null,
    h("div", { class: "dock" },
      h("div", { class: "dock-inner" },
        h("button", { class: "btn primary", type: "button", disabled: S.busy, onclick: confirmVisit },
          S.busy ? "Confirming…" : n && !paused ? `Confirm visit & send order (${n})` : "Confirm visit"),
        h("p", { class: "faint" }, "Nothing is charged. You can cancel anytime."))));
}

function viewVisit() {
  const v = S.visit;
  const steps = ["confirmed", "arrived", "seated", "done"];
  const idx = steps.indexOf(v.status);
  const labels = { confirmed: "Planned", arrived: "Arrived", seated: "Seated", done: "Done" };
  return h("div", { class: "rise" },
    v.replan && v.status === "confirmed" ? replanCard(v) : null,
    h("section", { class: "card ticket" },
      h("div", { class: "label" }, "Your visit code"),
      h("div", { class: "code" }, v.code),
      h("div", { class: "zone" }, `${v.plan.zone_emoji} ${v.plan.zone_name}`),
      h("div", { class: "faint" }, v.status === "done" ? "Thanks for visiting" : "Show this code at the counter"),
      h("div", { class: "steps" },
        steps.map((s, i) => h("div", { class: `step ${i <= idx ? "done" : ""}` },
          h("span", { class: "bullet" }, i <= idx ? "✓" : ""), labels[s])))),
    v.status === "done" ? null : h("section", { class: "card countdown", id: "countdown" }, countdownContent()),
    v.order ? orderStatus(v.order) : null,
    v.status === "done" ? feedbackCard() : h("section", { class: "card", style: { padding: "4px 16px", "margin-top": "12px" } }, timeline(v.plan.items)),
    visitActions(v));
}

function countdownContent() {
  const v = S.visit;
  if (!v.now) return [];
  if (v.status === "seated") {
    const left = minutesBetween(v.now, hhmm(v.plan.leave_by));
    return [
      h("div", null, h("div", { class: "faint" }, "Planned departure"), h("div", { class: "big" }, hhmm(v.plan.leave_by))),
      h("span", { class: `pill ${left < 10 ? "tight" : "ok"}` }, left > 0 ? `${left} min left` : "Time's up, no rush"),
    ];
  }
  const until = minutesBetween(v.now, hhmm(v.plan.arrive_by));
  return [
    h("div", null, h("div", { class: "faint" }, v.status === "arrived" ? "You're here" : "Arrive by"), h("div", { class: "big" }, hhmm(v.plan.arrive_by))),
    h("span", { class: `pill ${v.plan.seat_band}` }, v.status === "arrived" ? BAND[v.plan.seat_band] : until > 0 ? `in ${until} min` : "now"),
  ];
}

function updateCountdown() {
  const el = document.getElementById("countdown");
  if (el) mount(el, countdownContent());
}

function orderStatus(order) {
  const names = Object.fromEntries(S.pulse.menu.map((m) => [m.id, `${m.emoji} ${m.name}`]));
  const label = {
    sent: ["Sent to the barista", "info"],
    accepted: ["Being made now", "tight"],
    ready: ["Ready, pick it up at the counter 🔔", "ok"],
    collected: ["Enjoy!", "ok"],
  }[order.status];
  return h("section", { class: "card order-status" },
    h("span", null, order.item_ids.map((i) => names[i] || i).join(", ")),
    h("span", { class: "spacer" }),
    h("span", { class: `pill ${label[1]}` }, label[0]));
}

function replanCard(v) {
  return h("section", { class: "card replan rise", role: "alert" },
    h("h3", null, "Heads up: things changed"),
    h("p", null, v.replan.reason),
    h("div", { class: "opts" },
      v.replan.options.map((o) => {
        const keep = o.zone_id === v.plan.zone_id;
        const why = o.items.find((i) => i.kind === "departure") || o.items.find((i) => i.kind === "seat");
        return h("div", { class: "opt" },
          h("div", { class: "row" },
            h("strong", null, `${o.zone_emoji} ${o.zone_name}`),
            h("span", { class: `pill ${o.seat_band}` }, BAND[o.seat_band])),
          h("div", { class: "why" }, `${o.headline} · wait ${o.wait_p50}–${o.wait_p90} min`),
          why ? h("div", { class: "why" }, why.text) : null,
          h("button", { class: `btn small ${keep ? "ghost" : "primary"}`, type: "button", disabled: S.busy, onclick: () => (keep ? act("keep") : chooseReplan(o.id)) },
            keep ? "Keep my plan" : `Switch to ${o.zone_name}`));
      })));
}

function visitActions(v) {
  const b = (label, fn, cls = "") => h("button", { class: `btn ${cls}`, type: "button", disabled: S.busy, onclick: fn }, label);
  if (v.status === "confirmed") {
    return h("div", { class: "actions" },
      b("📍 I'm here", () => act("arrived"), "primary wide"),
      b("Running 10 min late", () => act("late", { minutes: 10 }), "ghost"),
      b("Cancel visit", () => act("cancel"), "ghost"));
  }
  if (v.status === "arrived") {
    return h("div", { class: "actions" },
      b("🪑 I'm seated", () => act("seated"), "primary wide"),
      b("Cancel visit", () => act("cancel"), "ghost wide"));
  }
  if (v.status === "seated") {
    return h("div", { class: "actions" }, b("👋 Leaving now", () => act("leaving"), "primary wide"));
  }
  return h("div", { class: "actions" }, b("Plan another visit", () => { forgetVisit(); startOver(); }, "ghost wide"));
}

function feedbackCard() {
  return h("section", { class: "card feedback" },
    h("h3", null, "Was your plan accurate?"),
    h("div", { class: "row" },
      h("button", { class: "btn", type: "button", onclick: () => act("feedback", { feedback: "accurate" }) }, "👍 Spot on"),
      h("button", { class: "btn", type: "button", onclick: () => act("feedback", { feedback: "off" }) }, "👎 Not quite")));
}

function micButton(ta) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) return null;
  const btn = h("button", { class: "mic", type: "button", "aria-label": "Speak your request" }, "🎙️");
  btn.addEventListener("click", () => {
    const rec = new SR();
    rec.lang = navigator.language || "en-IN";
    rec.interimResults = true;
    btn.classList.add("listening");
    rec.onresult = (e) => {
      const t = [...e.results].map((r) => r[0].transcript).join(" ");
      ta.value = t;
      S.text = t;
    };
    rec.onend = () => btn.classList.remove("listening");
    rec.onerror = () => btn.classList.remove("listening");
    rec.start();
  });
  return btn;
}

init().catch((e) => mount(app, h("p", { class: "err" }, `Couldn't load the café: ${e.message}`)));

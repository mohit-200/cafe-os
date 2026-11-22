// Tiny DOM + API helpers. All user-visible strings go through text nodes (no innerHTML), so nothing injects markup.

export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "style") for (const [p, val] of Object.entries(v)) el.style.setProperty(p, val);
    else if (k === "value") el.value = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function mount(root, ...nodes) {
  root.replaceChildren(...nodes.flat().filter(Boolean));
}

export async function api(path, { method = "GET", body, headers = {} } = {}) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const detail = data && typeof data.detail === "string" ? data.detail : "Something didn't look right. Please check and try again.";
    const err = new Error(detail);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

export const storage = {
  get(key) { try { return JSON.parse(localStorage.getItem(key)); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ } },
  del(key) { try { localStorage.removeItem(key); } catch { /* private mode */ } },
};

// Polls only while the tab is visible: cheap on Cloud Run (short requests) and on battery.
export function poller(fn, ms) {
  let timer = null;
  let running = false;
  let first = true;
  const loop = async () => {
    clearTimeout(timer);
    if ((first || document.visibilityState === "visible") && !running) {
      first = false;
      running = true;
      try { await fn(); } catch (e) { console.warn("poll failed", e); }
      running = false;
    }
    timer = setTimeout(loop, ms);
  };
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") loop(); });
  loop();
  return { now: loop };
}

export const TIERS = {
  prediction: { label: "Prediction", icon: "🔮" },
  recommendation: { label: "Suggestion", icon: "💡" },
  commitment: { label: "Confirmed", icon: "✅" },
  policy: { label: "Café rule", icon: "📋" },
};

export function tierBadge(tier) {
  const t = TIERS[tier] || TIERS.prediction;
  return h("span", { class: `tier ${tier}`, title: t.label }, t.label);
}

// ISO timestamps carry the café's UTC offset, so the HH:MM inside them is already café-local.
export const hhmm = (iso) => (iso ? iso.slice(11, 16) : "");

export function toast(message, ms = 2600) {
  const el = h("div", { class: "toast rise", role: "status" }, message);
  document.body.append(el);
  setTimeout(() => el.remove(), ms);
}

export function minutesBetween(hmA, hmB) {
  const [ah, am] = hmA.split(":").map(Number);
  const [bh, bm] = hmB.split(":").map(Number);
  return bh * 60 + bm - (ah * 60 + am);
}

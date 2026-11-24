// AP Guardian portal - vanilla JS, Cognito Hosted UI with authorization code + PKCE.
// All untrusted data (invoice text, reasons) is rendered with textContent, never innerHTML.
(async function () {
  const cfg = await (await fetch("config.json")).json();
  const store = {
    get: (k) => { try { return sessionStorage.getItem(k); } catch { return null; } },
    set: (k, v) => { try { sessionStorage.setItem(k, v); } catch { /* ignore */ } },
    del: (k) => { try { sessionStorage.removeItem(k); } catch { /* ignore */ } },
  };
  const $ = (s) => document.querySelector(s);
  const money = (c) => (c == null ? "" : "$" + (Number(c) / 100).toLocaleString(undefined, { minimumFractionDigits: 2 }));

  // ---------- auth ----------
  const b64url = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  async function login() {
    const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)));
    const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
    const state = b64url(crypto.getRandomValues(new Uint8Array(16)));
    store.set("pkce_verifier", verifier); store.set("oauth_state", state);
    const q = new URLSearchParams({ response_type: "code", client_id: cfg.clientId, redirect_uri: cfg.redirectUri,
      scope: "openid email profile", code_challenge_method: "S256", code_challenge: challenge, state });
    location.href = `${cfg.cognitoDomain}/oauth2/authorize?${q}`;
  }
  async function handleRedirect() {
    const p = new URLSearchParams(location.search);
    if (!p.get("code")) return;
    if (p.get("state") !== store.get("oauth_state")) { alert("Login state mismatch"); return; }
    const body = new URLSearchParams({ grant_type: "authorization_code", client_id: cfg.clientId, code: p.get("code"),
      redirect_uri: cfg.redirectUri, code_verifier: store.get("pkce_verifier") });
    const r = await fetch(`${cfg.cognitoDomain}/oauth2/token`, { method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" }, body });
    if (r.ok) store.set("id_token", (await r.json()).id_token);
    history.replaceState({}, "", location.pathname);
  }
  function claims() {
    const t = store.get("id_token"); if (!t) return null;
    try {
      const c = JSON.parse(atob(t.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
      if (c.exp * 1000 < Date.now()) { store.del("id_token"); return null; }
      return c;
    } catch { return null; }
  }
  function logout() {
    store.del("id_token");
    location.href = `${cfg.cognitoDomain}/logout?client_id=${cfg.clientId}&logout_uri=${encodeURIComponent(cfg.redirectUri)}`;
  }

  async function api(path, opts = {}) {
    const r = await fetch(`/api/${path}`, { ...opts, headers: { "Content-Type": "application/json",
      Authorization: store.get("id_token"), ...(opts.headers || {}) } });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || data.message || r.statusText);
    return data;
  }

  // ---------- rendering helpers ----------
  function row(tbody, cells) {
    const tr = document.createElement("tr");
    for (const c of cells) {
      const td = document.createElement("td");
      if (c instanceof Node) td.appendChild(c); else td.textContent = c == null ? "" : String(c);
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  function btn(label, onClick) { const b = document.createElement("button"); b.textContent = label; b.onclick = onClick; return b; }

  // ---------- tabs ----------
  const groups = () => new Set((claims() || {})["cognito:groups"] || []);
  const loaders = {
    async exceptions() {
      const tb = $("#exceptions tbody"); tb.textContent = "";
      const isClerk = groups().has("ap-clerk");
      for (const e of await api("exceptions")) {
        const actions = document.createElement("span");
        if (isClerk && e.status === "PENDING") {
          actions.append(btn("Approve", () => decide(e.exception_id, "APPROVED")), " ",
                         btn("Reject", () => decide(e.exception_id, "REJECTED")));
        }
        row(tb, [e.invoice_id, e.vendor_id, e.po_number, money(e.amount_cents), e.reason, e.status, actions]);
      }
    },
    async invoices() {
      const tb = $("#invoices tbody"); tb.textContent = "";
      for (const i of await api("invoices")) {
        row(tb, [i.invoice_id, i.vendor_id, i.vendor_invoice_number, i.po_number, money(i.amount_cents), i.status,
                 btn("View", () => viewInvoice(i.invoice_id))]);
      }
    },
    async pos() {
      const tb = $("#pos tbody"); tb.textContent = "";
      for (const p of await api("pos")) {
        row(tb, [p.po_number, p.vendor_id, money(p.amount_total_cents), money(p.amount_remaining_cents), p.status, p.created_by]);
      }
      const form = $("#po-form");
      form.hidden = !groups().has("procurement");
      if (!form.hidden) {
        const sel = form.querySelector("select"); sel.textContent = "";
        for (const v of await api("vendors")) {
          if (v.status !== "APPROVED") continue;
          const o = document.createElement("option"); o.value = v.vendor_id; o.textContent = `${v.name} (${v.vendor_id})`; sel.appendChild(o);
        }
      }
    },
    async ledger() {
      const tb = $("#ledger tbody"); tb.textContent = "";
      for (const l of await api("ledger")) {
        row(tb, [l.payment_id, l.invoice_id, l.po_number, money(l.amount_cents), l.approved_by, l.created_at]);
      }
    },
  };

  async function viewInvoice(id) {
    const d = await api(`invoices/${encodeURIComponent(id)}`);
    const box = $("#invoice-detail"); box.textContent = "";
    const h = document.createElement("h3"); h.textContent = `Invoice ${d.invoice_id}`; box.appendChild(h);
    const a = document.createElement("a"); a.href = d.pdf_url; a.target = "_blank"; a.rel = "noopener noreferrer";
    a.textContent = "Open PDF (link valid 5 minutes)"; box.appendChild(a);
    const pre = document.createElement("pre");
    const { pdf_url, ...rest } = d; pre.textContent = JSON.stringify(rest, null, 2); box.appendChild(pre);
  }

  async function decide(id, decision) {
    if (!confirm(`${decision} invoice ${id}?`)) return;
    try { await api(`exceptions/${encodeURIComponent(id)}/decision`, { method: "POST", body: JSON.stringify({ decision }) }); }
    catch (e) { alert(e.message); }
    loaders.exceptions();
  }

  $("#po-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target); const msg = ev.target.querySelector(".msg");
    try {
      const po = await api("pos", { method: "POST", body: JSON.stringify(Object.fromEntries(f)) });
      msg.textContent = `Created ${po.po_number}${po.emailed ? " and emailed the vendor" : " (email not sent: SES sender not configured)"}`;
      ev.target.reset(); loaders.pos();
    } catch (e) { msg.textContent = e.message; }
  });

  function show(tab) {
    document.querySelectorAll(".tab").forEach((s) => (s.hidden = s.id !== tab));
    loaders[tab]().catch((e) => alert(e.message));
  }

  // ---------- boot ----------
  await handleRedirect();
  $("#login").onclick = login; $("#logout").onclick = logout;
  const c = claims();
  if (c) {
    $("#who").textContent = `${c.email || c["cognito:username"]} [${[...groups()].join(", ") || "no group"}]`;
    $("#login").hidden = true; $("#logout").hidden = false; $("#tabs").hidden = false; $("#signedout").hidden = true;
    document.querySelectorAll("#tabs button").forEach((b) => (b.onclick = () => show(b.dataset.tab)));
    show("exceptions");
  }
})();

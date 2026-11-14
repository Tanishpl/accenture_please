"use strict";

const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((ok) => setTimeout(ok, ms));

async function api(path, opts = {}) {
  const isForm = opts.body instanceof FormData;
  const res = await fetch(path, {
    ...opts,
    headers: opts.body && !isForm ? { "Content-Type": "application/json" } : {},
    body: opts.body && !isForm ? JSON.stringify(opts.body) : opts.body,
  });
  if (res.status === 204) return null;
  const type = res.headers.get("content-type") || "";
  const data = type.includes("json") ? await res.json() : await res.blob();
  if (!res.ok) throw new Error((data && data.detail) || res.statusText);
  return data;
}

/* h("p.lead", {id: "x"}, "text", child, ...) -- builds DOM without innerHTML */
function h(spec, attrs, ...children) {
  const [tag, ...classes] = spec.split(".");
  const n = document.createElement(tag || "div");
  if (classes.length) n.className = classes.join(" ");
  if (attrs && (typeof attrs !== "object" || attrs instanceof Node || Array.isArray(attrs))) {
    children.unshift(attrs);
    attrs = null;
  }
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v === null || v === undefined) continue;
    if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "text") n.textContent = v;
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return n;
}

const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
const ARROW = () => {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", "arrow");
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
  p.setAttribute("d", "M12.5 5.5 19 12l-6.5 6.5-1.4-1.4 4.1-4.1H5v-2h10.2l-4.1-4.1z");
  p.setAttribute("fill", "currentColor");
  svg.append(p);
  return svg;
};

/* ================= patient service ================= */

function initPatient() {
  const state = { sid: null, status: {}, record: null, rows: [], credential: null, practice: null, reasonEdit: null };

  api("/api/status").then((s) => { state.status = s; }).catch(() => {});

  function render(...nodes) {
    const view = $("view");
    view.replaceChildren(...nodes.filter(Boolean));
    window.scrollTo(0, 0);
    const heading = view.querySelector("h1");
    if (heading) { heading.setAttribute("tabindex", "-1"); heading.focus({ preventScroll: true }); }
  }

  function backLink(onClick, label = "Back") {
    return h("div.back-link", h("a", { href: "#", onclick: (e) => { e.preventDefault(); onClick(); } }, "‹ ", label));
  }

  function insetText(...content) { return h("div.inset-text", ...content); }

  /* ---------- start ---------- */

  function startView() {
    render(
      h("h1", "Book a GP appointment"),
      h("p.lead", "Answer a few questions and we'll phone your GP surgery to book an appointment for you, so you don't have to wait in the 8am phone queue."),
      h("div.care-card.urgent",
        h("div.care-card-heading", h("h2", "Do not use this service if you need help now")),
        h("div.care-card-body",
          h("p", "Call 999 if it's a life-threatening emergency."),
          h("p", "Call 111 if you need help urgently but it's not an emergency."))),
      h("h2", "How it works"),
      h("ol.list",
        h("li", "Answer 11 short questions about why you need an appointment and when suits you."),
        h("li", "Check your answers."),
        h("li", "We phone your surgery, say we're an automated assistant, and book an appointment for you."),
        h("li", "We tell you the time of your appointment.")),
      h("h2", "Your information"),
      h("p", "Your answers and your voice are processed on this device. The surgery is only told that a registered patient would like an appointment, roughly what it's about, and when suits you."),
      h("p", "The assistant never gives your date of birth, address or NHS number. If the surgery needs those, they'll ask to speak to you."),
      h("button.button.start", { type: "button", onclick: start }, "Start now", ARROW()),
    );
  }

  async function start() {
    const r = await api("/api/session", { method: "POST" });
    state.sid = r.session_id;
    handle(r);
  }

  function handle(r) {
    if (r.type === "question") return questionView(r);
    if (r.type === "review") { state.record = r.record; state.rows = r.rows; state.reasonEdit = null; return checkView(); }
    if (r.type === "crisis") return crisisView(r);
  }

  /* ---------- question ---------- */

  function questionView(q) {
    const id = `q-${q.key}`;
    const errorSlot = h("span.error-message", { id: `${id}-error`, hidden: true });
    let body;
    let read;

    if (q.options.length) {
      const name = id;
      const type = q.multi ? "checkbox" : "radio";
      const rawParts = q.multi ? q.value.split(",").map((s) => s.trim()).filter(Boolean) : [q.value.trim()].filter(Boolean);
      const known = new Set(q.options.map((o) => o.toLowerCase()));
      const otherText = rawParts.filter((p) => !known.has(p.toLowerCase())).join(", ");
      const exclusive = q.multi ? q.options.find((o) => /^(any time|nothing needed)$/i.test(o)) : null;

      const item = (label, i, extraAttrs = {}) => {
        const inputId = `${name}-${i}`;
        const checked = rawParts.some((p) => p.toLowerCase() === label.toLowerCase());
        return h(`div.${type}-item`,
          h(`input.${type}-input`, { id: inputId, name, type, value: label, checked, ...extraAttrs }),
          h(`label.${type}-label`, { for: inputId }, label));
      };

      const items = h(`div.${type}es`);
      q.options.forEach((label, i) => {
        if (label === exclusive) return;
        items.append(item(label, i));
      });

      let otherInput = null;
      if (q.other) {
        const [otherLabel, otherPrompt] = q.other;
        const otherId = `${name}-other`;
        otherInput = h("input.input", { id: `${otherId}-text`, type: q.key === "contact_number" ? "tel" : "text", value: otherText, autocomplete: q.key === "contact_number" ? "tel" : "off" });
        const reveal = h("div.conditional", { id: `${otherId}-reveal`, hidden: !otherText },
          h("label.label", { for: `${otherId}-text` }, otherPrompt), otherInput);
        items.append(
          h(`div.${type}-item`,
            h(`input.${type}-input`, { id: otherId, name, type, value: "__other", checked: !!otherText, "aria-controls": `${otherId}-reveal` }),
            h(`label.${type}-label`, { for: otherId }, otherLabel)),
          reveal);
      }
      if (exclusive) {
        items.append(h("div.divider", "or"), item(exclusive, q.options.indexOf(exclusive), { "data-exclusive": "true" }));
      }

      items.addEventListener("change", (e) => {
        const t = e.target;
        if (q.multi && t.checked) {
          for (const box of items.querySelectorAll("input[type=checkbox]")) {
            if (box !== t && (t.dataset.exclusive ? true : box.dataset.exclusive)) box.checked = false;
          }
        }
        const otherBox = items.querySelector('input[value="__other"]');
        if (otherBox) {
          const reveal = $(`${otherBox.id}-reveal`);
          reveal.hidden = !otherBox.checked;
          if (otherBox.checked && t === otherBox) otherInput.focus();
        }
      });

      body = h("fieldset.fieldset", { "aria-describedby": [q.hint ? `${id}-hint` : "", `${id}-error`].filter(Boolean).join(" ") },
        h("legend.fieldset-legend", h("h1", q.text)),
        q.hint ? h("div.hint", { id: `${id}-hint` }, q.hint) : null,
        errorSlot,
        items);

      read = () => {
        const picked = [...items.querySelectorAll("input:checked")];
        if (!picked.length) return { error: q.multi ? "Select at least one option" : "Select an option" };
        const parts = [];
        for (const p of picked) {
          if (p.value === "__other") {
            const v = otherInput.value.trim();
            if (!v) return { error: `Enter ${q.other[1].toLowerCase().replace(/\?$/, "")}`, focus: otherInput };
            parts.push(v);
          } else parts.push(p.value);
        }
        return { text: parts.join(", ") };
      };
    } else {
      const ta = h("textarea.textarea", { id, name: id, rows: 5, "aria-describedby": `${id}-hint ${id}-error` });
      ta.value = q.value || "";
      body = h("div.form-group",
        h("h1", h("label", { for: id }, q.text)),
        q.hint ? h("div.hint", { id: `${id}-hint` }, q.hint) : null,
        errorSlot,
        ta);
      read = () => (ta.value.trim() ? { text: ta.value.trim() } : { error: "Tell us what you'd like to see a doctor about", focus: ta });
    }

    const errorSummary = h("div.error-summary", { role: "alert", tabindex: "-1", hidden: true });
    const form = h("form", { novalidate: true, onsubmit: async (e) => {
      e.preventDefault();
      const out = read();
      if (out.error) {
        errorSummary.replaceChildren(h("h2", "There is a problem"), h("ul", h("li", h("a", { href: `#${id}` }, out.error))));
        errorSummary.hidden = false;
        errorSlot.textContent = out.error;
        errorSlot.hidden = false;
        body.classList.add("has-error");
        errorSummary.focus();
        return;
      }
      submitBtn.disabled = true;
      try {
        handle(await api(`/api/session/${state.sid}/answer`, { method: "POST", body: { text: out.text } }));
      } catch (err) {
        submitBtn.disabled = false;
        alert(err.message);
      }
    } }, body);

    const submitBtn = h("button.button", { type: "submit" }, "Continue");
    form.append(submitBtn);

    if (state.status.stt && !q.options.length) form.append(voiceButton());

    render(
      q.can_go_back ? backLink(async () => handle(await api(`/api/session/${state.sid}/back`, { method: "POST" }))) : backLink(startView),
      h("span.caption", `Question ${q.progress.number} of ${q.progress.total}`),
      errorSummary,
      q.ack ? insetText(h("p", q.ack)) : null,
      form,
    );
  }

  function voiceButton() {
    let recorder = null;
    const btn = h("button.button.secondary", { type: "button", onclick: async () => {
      if (recorder && recorder.state === "recording") { recorder.stop(); return; }
      let stream;
      try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
      catch { alert("We couldn't use your microphone. You can type your answer instead."); return; }
      const chunks = [];
      recorder = new MediaRecorder(stream);
      recorder.ondataavailable = (e) => chunks.push(e.data);
      recorder.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        btn.textContent = "Processing on this device…";
        const form = new FormData();
        form.append("file", new Blob(chunks, { type: "audio/webm" }), "answer.webm");
        try { handle(await api(`/api/session/${state.sid}/audio`, { method: "POST", body: form })); }
        catch (err) { alert(err.message); btn.textContent = "Speak your answer instead"; }
      };
      recorder.start();
      btn.textContent = "Stop recording";
    } }, "Speak your answer instead");
    return btn;
  }

  /* ---------- check your answers ---------- */

  function checkView() {
    const list = h("dl.summary-list");
    for (const row of state.rows) {
      list.append(h("div.summary-row",
        h("dt.summary-key", row.label),
        h("dd.summary-value", cap(row.value)),
        h("dd.summary-actions", h("a", { href: "#", onclick: async (e) => {
          e.preventDefault();
          handle(await api(`/api/session/${state.sid}/change`, { method: "POST", body: { key: row.key } }));
        } }, "Change", h("span.visually-hidden", ` ${row.label.toLowerCase()}`)))));
    }

    const summary = h("p", state.record.spoken_summary);
    const reasonInput = h("input.input.width-20", { id: "reason-edit", value: state.reasonEdit ?? state.record.reason_for_contact });
    const details = h("details.details",
      h("summary", h("span", "Change how we describe what it's about")),
      h("div.details-text",
        h("label.label", { for: "reason-edit" }, "Describe the general area in a few words"),
        h("div.hint", "For example, “knee problem” or “skin problem”. Do not include symptoms."),
        reasonInput,
        h("button.button.secondary.small", { type: "button", onclick: async () => {
          state.reasonEdit = reasonInput.value.trim();
          const prefs = state.record.preferences;
          const r = await api("/api/summary-preview", { method: "POST", body: { reason: state.reasonEdit, urgency: prefs.urgency, times: prefs.times.join(", ") } });
          summary.textContent = r.spoken_summary;
          details.open = false;
        } }, "Update")));

    render(
      backLink(async () => handle(await api(`/api/session/${state.sid}/back`, { method: "POST" }))),
      h("h1", "Check your answers"),
      list,
      h("h2", "What the surgery will be told"),
      h("p", "This is the only thing the assistant will say about you on the phone. Your answers above stay on this device."),
      insetText(summary),
      details,
      h("h2", "Book your appointment"),
      h("p", "By continuing, you're allowing the assistant to phone your surgery and book an appointment for you. It will tell them it's an automated assistant."),
      h("button.button", { type: "button", onclick: confirm }, "Confirm and book"),
    );
  }

  async function confirm(e) {
    e.target.disabled = true;
    const edits = state.reasonEdit ? { reason_for_contact: state.reasonEdit } : {};
    try {
      const r = await api(`/api/session/${state.sid}/confirm`, { method: "POST", body: { edits } });
      state.record = r.record;
      state.credential = r.credential;
      state.practice = r.practice;
      bookingView();
    } catch (err) {
      e.target.disabled = false;
      alert(err.message);
    }
  }

  /* ---------- booking ---------- */

  function bookingView() {
    const permission = h("div");
    const callArea = h("div");
    const auditArea = h("div");
    const speakResult = h("div", { "aria-live": "polite" });

    function renderPermission() {
      const ok = state.practice.status === "authorised";
      const c = state.credential;
      const rows = [
        ["Status", h(`strong.tag.${ok ? "tag-green" : "tag-red"}`, ok ? "Active" : "Removed")],
        ["Allows the assistant to", "book or cancel appointments and order repeat prescriptions"],
        ["Surgery", "your registered GP surgery only"],
        ["Your identity", "held by your surgery, never by the assistant"],
        ["Expires", new Date(c.expires).toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" })],
        ["Reference", c.credential_id],
      ];
      permission.replaceChildren(
        h("dl.summary-list.no-actions", rows.map(([k, v]) => h("div.summary-row", h("dt.summary-key", k), h("dd.summary-value", v)))),
        ok
          ? h("button.button.warning", { type: "button", onclick: async () => {
              const r = await api(`/api/session/${state.sid}/revoke`, { method: "POST" });
              state.credential = r.credential; state.practice = r.practice; renderPermission();
            } }, "Remove permission")
          : h("button.button.secondary", { type: "button", onclick: async () => {
              const r = await api(`/api/session/${state.sid}/credential`, { method: "POST" });
              state.credential = r.credential; state.practice = r.practice; renderPermission();
            } }, "Give permission again"),
      );
      callBtn.disabled = !ok;
    }

    const scenario = h("select.select", { id: "scenario" },
      h("option", { value: "full" }, "Receptionist asks things we can't answer"),
      h("option", { value: "simple" }, "Straightforward booking"),
      h("option", { value: "decline" }, "Surgery won't speak to an assistant"),
      h("option", { value: "garbled" }, "Bad line"),
      h("option", { value: "voicemail" }, "Surgery closed"));

    const callBtn = h("button.button", { type: "button", onclick: placeCall }, "Call the surgery now");

    async function placeCall() {
      callBtn.disabled = true;
      const tbody = h("tbody");
      const table = h("table.table",
        h("caption", "Call transcript"),
        h("thead", h("tr", h("th", { scope: "col" }, "Time"), h("th", { scope: "col" }, "Speaker"), h("th", { scope: "col" }, "What was said"))),
        tbody);
      callArea.replaceChildren(table);
      try {
        const r = await api(`/api/session/${state.sid}/call`, { method: "POST", body: { scenario: scenario.value } });
        for (const ev of r.events) {
          const spoken = ["speech", "ivr", "voicemail"].includes(ev.kind);
          await sleep(spoken ? 500 : 200);
          const who = ev.speaker === "agent" ? "Assistant" : ev.speaker === "practice" ? "Surgery" : "";
          tbody.append(h(`tr${spoken ? "" : ".event"}`,
            h("td.time", `${Math.floor(ev.t / 60)}:${String(Math.floor(ev.t % 60)).padStart(2, "0")}`),
            h("td", spoken ? who : ""),
            h("td", ev.text.replace(/^\[|\]$/g, ""))));
        }
        callArea.prepend(outcome(r));
        loadAudit();
      } catch (err) {
        callArea.replaceChildren(h("div.warning-callout", h("h3", "Call not made"), h("p", err.message)));
      } finally {
        callBtn.disabled = state.practice.status !== "authorised";
      }
    }

    function outcome(r) {
      const list = r.unanswered.length
        ? [h("h3", "Questions we couldn't answer for you"), h("ul.list.bullet", r.unanswered.map((u) => h("li", u)))]
        : [];
      if (r.outcome === "booked") {
        return h("div",
          h("div.panel", h("h2.panel-title", "Appointment booked"), h("div.panel-body", r.agreed)),
          h("p", "We've added this to the record of what was said on your behalf, below."),
          ...list);
      }
      const titles = {
        declined: ["The surgery would not speak to an assistant", "Please call the surgery yourself. Your answers are saved on this device so you have them ready."],
        unparsed_offer_callback: ["We couldn't understand the surgery", "The line was unclear. We offered to call back."],
        retry_scheduled: ["The surgery is closed", `We'll try again at ${r.next_attempt ? new Date(r.next_attempt).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }) : "a later time"}. We'll make no more than 3 attempts today.`],
        attempt_cap_reached: ["We've tried 3 times today", "We've stopped so it doesn't look like an automated dialler. Please call the surgery yourself."],
        ended_without_booking: ["No appointment was offered", "Please call the surgery yourself."],
      };
      const [title, text] = titles[r.outcome] || [r.outcome, ""];
      return h("div.warning-callout", h("h3", title), h("p", text), ...list);
    }

    async function loadAudit() {
      const r = await api(`/api/session/${state.sid}/audit`);
      if (!r.entries.length) { auditArea.replaceChildren(h("p", "Nothing has been said on your behalf yet.")); return; }
      auditArea.replaceChildren(h("details.details", { open: true },
        h("summary", h("span", `Show the full record (${r.entries.length} entries)`)),
        h("div.details-text", h("table.table.small",
          h("thead", h("tr", h("th", "Time"), h("th", "Heard"), h("th", "Said or did"))),
          h("tbody", r.entries.map((e) => h("tr",
            h("td.time", new Date(e.timestamp).toLocaleTimeString("en-GB")),
            h("td", e.heard || ""),
            h("td", e.said || "",
              e.agreed ? h("div", h("strong.tag.tag-green", "Agreed"), " ", e.agreed) : null,
              e.unanswered ? h("div", h("strong.tag.tag-red", "Not answered")) : null))))))));
    }

    async function trySpeak(field) {
      try {
        const blob = await api(`/api/session/${state.sid}/speak?field=${encodeURIComponent(field)}`);
        if (blob) new Audio(URL.createObjectURL(blob)).play();
        speakResult.replaceChildren(insetText(h("p", h("strong.tag.tag-green", "Allowed"), " ",
          blob ? "Playing. The voice was generated on this device." : `Voice output isn't installed, so here it is as text: “${state.record.spoken_summary}”`)));
      } catch (err) {
        speakResult.replaceChildren(insetText(h("p", h("strong.tag.tag-red", "Blocked"), " ", `The code refused: ${err.message}. This is enforced by a function, not by an instruction to an AI.`)));
      }
    }

    render(
      h("h1", "Your request is ready"),
      h("p.lead", state.record.spoken_summary),
      h("h2", "Permission to book for you"),
      h("div.warning-callout.compact", h("h3", "Demo only"),
        h("p", "This permission is simulated. It shows how a proxy credential from the NHS National proxy service could work for software. That service does not support this today.")),
      permission,
      h("h2", "Call the surgery"),
      h("div.form-group", h("label.label", { for: "scenario" }, "How the surgery responds"), h("div.hint", "This demo uses a scripted receptionist."), scenario),
      callBtn,
      callArea,
      h("h2", "What was said on your behalf"),
      auditArea,
      h("h2", "Check the voice boundary"),
      h("p", "The assistant's voice can only read three fields from your record. Everything else is refused in code."),
      h("div.button-group",
        h("button.button.secondary", { type: "button", onclick: () => trySpeak("spoken_summary") }, "Speak the summary"),
        h("button.button.secondary", { type: "button", onclick: () => trySpeak("clinical_detail") }, "Try to speak your symptoms")),
      speakResult,
      h("p", h("a", { href: "#", onclick: (e) => { e.preventDefault(); startView(); } }, "Start a new request")),
    );
    renderPermission();
    loadAudit();
  }

  /* ---------- crisis ---------- */

  function crisisView(r) {
    const telButton = (label, tel, cls = "") => h(`a.button${cls}`, { href: `tel:${tel}` }, label);
    if (r.kind === "red_flag") {
      render(
        h("div.care-card.immediate",
          h("div.care-card-heading", h("h1", "Call 999 now")),
          h("div.care-card-body",
            h("p", "This needs urgent help now. Call 999. If you are not sure, call 111."),
            h("div.button-group", telButton("Call 999", "999", ".reverse"), telButton("Call 111", "111", ".secondary-reverse")))),
        h("p", "We have not booked an appointment and we will not phone your surgery."),
        h("p", h("a", { href: "#", onclick: (e) => { e.preventDefault(); startView(); } }, "Start again")),
      );
    } else {
      render(
        h("div.care-card.urgent",
          h("div.care-card-heading", h("h1", "Help is available")),
          h("div.care-card-body",
            h("p", "You can talk to Samaritans any time, day or night. Calls are free."),
            telButton("Call Samaritans on 116 123", "116123"),
            h("p", "If you're in immediate danger or have harmed yourself, call 999."),
            h("p", "You can also contact your GP surgery directly and tell them it's urgent."),
            h("div.button-group", telButton("Call 999", "999", ".warning"), telButton("Call your GP surgery", "01632960123", ".secondary")))),
        h("p", "We've stopped the booking and have not saved what you wrote."),
      );
    }
  }

  $("home").addEventListener("click", (e) => { e.preventDefault(); startView(); });
  startView();
}

/* ================= data boundary panel ================= */

function initPanels() {
  let mode = "local";
  const setMode = (m) => {
    mode = m;
    for (const [id, val] of [["mode-local", "local"], ["mode-naive", "naive"]]) {
      $(id).setAttribute("aria-selected", String(m === val));
      $(id).classList.toggle("selected", m === val);
    }
    document.body.classList.toggle("naive", m === "naive");
    lastKey = "";
    refresh();
  };
  $("mode-local").onclick = () => setMode("local");
  $("mode-naive").onclick = () => setMode("naive");

  const PRETTY = {
    description: "Description", duration: "How long", trajectory: "Better or worse", seen_before_for_this: "Seen before",
    taking_anything: "Taking", reason: "Description", seen_before: "Seen before", urgency: "How soon",
    appointment_type: "Type", times: "Times", preferred_clinician: "Clinician", access_needs: "Help at appointment",
    contact_number: "Contact number",
  };

  function value(v) {
    if (typeof v === "string") return h("p.val", v);
    if (Array.isArray(v) && v.every((t) => t && t.role)) {
      const answers = v.filter((t) => t.role === "patient");
      if (!answers.length) return h("p.val.muted", "No answers yet");
      return h("ol.answers", answers.slice(-6).map((t) => h("li", t.text)),
        answers.length > 6 ? h("li.more", `and ${answers.length - 6} earlier`) : null);
    }
    if (v && typeof v === "object") {
      if (v.request_id) {
        v = { "Reference": v.request_id.slice(0, 8), "Said to surgery": v.reason_for_contact,
              "Confirmed": v.confirmed_by_patient ? "yes" : "not yet", "Access needs": v.access_needs.join(", ") || "none" };
      }
      const entries = Object.entries(v).filter(([, x]) => x !== "" && x !== null);
      if (!entries.length) return h("p.val.muted", "Nothing yet");
      return h("dl.kv", entries.map(([k, x]) => [
        h("dt", PRETTY[k] || k),
        h("dd", Array.isArray(x) ? x.join(", ") : typeof x === "boolean" ? (x ? "yes" : "no") : String(x)),
      ]));
    }
    return h("p.val", String(v));
  }

  function item(label, v, dest, bytes) {
    return h("div.item",
      h("div.item-head", h("strong", label), dest ? h("span.dest", dest) : null),
      value(v),
      bytes !== undefined ? h("div.bytes", `${bytes.toLocaleString()} bytes`) : null);
  }

  let lastKey = "";
  async function refresh() {
    let d;
    try { d = await api(`/api/boundary?mode=${mode}`); } catch { return; }
    const key = JSON.stringify(d);
    if (key === lastKey) return;
    lastKey = key;

    $("stays").replaceChildren(...d.stays.map((s) => item(s.label, s.value)));
    const leaves = d.leaves.map((l) => item(l.label, l.value, `To: ${l.dest}`, l.bytes));
    if (d.crisis) {
      leaves.unshift(h("p.empty", d.crisis === "distress" ? "Crisis support shown. Nothing stored, nothing sent." : "Emergency advice shown. No call placed."));
    }
    if (!leaves.length) {
      leaves.push(h("p.empty", d.session_id
        ? "Nothing has left this device. Once she confirms, the surgery hears one sentence."
        : "Start a request to see the data flow."));
    }
    $("leaves").replaceChildren(...leaves);
    $("fields-out").textContent = d.fields_out;
    $("bytes-out").textContent = d.bytes_out.toLocaleString();
    $("vendor-out").textContent = d.leaves.filter((l) => /cloud|vendor/i.test(l.dest)).length;
  }

  refresh();
  setInterval(refresh, 1000);
}

if (document.body.dataset.page === "patient") initPatient();
if (document.body.dataset.page === "panels") initPanels();

"use strict";

const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body && !(opts.body instanceof FormData) ? JSON.stringify(opts.body) : opts.body,
  });
  if (res.status === 204) return null;
  const type = res.headers.get("content-type") || "";
  const data = type.includes("json") ? await res.json() : await res.blob();
  if (!res.ok) throw new Error((data && data.detail) || res.statusText);
  return data;
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function show(id, on = true) { $(id).classList.toggle("hidden", !on); }

/* ---------------- patient page ---------------- */

function initPatient() {
  let sid = null;
  let record = null;
  let recorder = null;

  api("/api/status").then((s) => {
    const pills = $("status");
    for (const [k, label] of [["stt", "Local speech-in"], ["tts", "Local speech-out"], ["ollama", "Local model"]]) {
      pills.append(el("span", "pill " + (s[k] ? "ok" : "off"), `${label}: ${s[k] ? "on" : "off"}`));
    }
    if (!s.stt) { $("mic").disabled = true; $("mic").title = "faster-whisper not installed: typed input only"; }
  });

  function say(role, text) {
    const msg = el("div", "msg " + role, text);
    $("chat").append(msg);
    $("chat").scrollTop = $("chat").scrollHeight;
  }

  function handle(result) {
    if (result.type === "question") say("assistant", result.text);
    if (result.type === "crisis") return crisis(result);
    if (result.type === "review") {
      say("assistant", result.text);
      record = result.record;
      renderReview();
      $("answer-form").classList.add("hidden");
    }
  }

  $("start").onclick = async () => {
    $("chat").replaceChildren();
    ["review", "after", "crisis"].forEach((id) => show(id, false));
    $("answer-form").classList.remove("hidden");
    $("call-log").replaceChildren(); $("call-outcome").replaceChildren(); $("audit").replaceChildren();
    const r = await api("/api/session", { method: "POST" });
    sid = r.session_id;
    $("start").textContent = "Start over";
    handle(r);
    $("answer").focus();
  };

  $("answer-form").onsubmit = async (e) => {
    e.preventDefault();
    const text = $("answer").value.trim();
    if (!text || !sid) return;
    $("answer").value = "";
    say("patient", text);
    handle(await api(`/api/session/${sid}/answer`, { method: "POST", body: { text } }));
  };

  $("mic").onclick = async () => {
    if (!sid) return;
    if (recorder && recorder.state === "recording") { recorder.stop(); return; }
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const chunks = [];
    recorder = new MediaRecorder(stream);
    recorder.ondataavailable = (e) => chunks.push(e.data);
    recorder.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      $("mic").textContent = "Mic"; $("mic").classList.remove("recording");
      const form = new FormData();
      form.append("file", new Blob(chunks, { type: "audio/webm" }), "answer.webm");
      try {
        const r = await api(`/api/session/${sid}/audio`, { method: "POST", body: form });
        say("patient", r.transcript);
        handle(r);
      } catch (err) { say("assistant", err.message); }
    };
    recorder.start();
    $("mic").textContent = "Stop"; $("mic").classList.add("recording");
  };

  const FIELDS = [
    ["reason_for_contact", "What it is about (said aloud)", (r) => r.reason_for_contact],
    ["description", "Your description (never said aloud)", (r) => r.clinical_detail.description],
    ["duration", "How long", (r) => r.clinical_detail.duration],
    ["trajectory", "Better, worse or the same", (r) => r.clinical_detail.trajectory],
    ["taking_anything", "Taking anything for it", (r) => r.clinical_detail.taking_anything],
    ["urgency", "How soon", (r) => r.preferences.urgency],
    ["appointment_type", "Come in or phone call", (r) => r.preferences.appointment_type],
    ["times", "Days and times (separate with ;)", (r) => r.preferences.times.join("; ")],
    ["preferred_clinician", "Preferred doctor or nurse", (r) => r.preferences.preferred_clinician || ""],
    ["access_needs", "Help on the day (separate with ;)", (r) => r.access_needs.join("; ")],
  ];

  function renderReview() {
    const form = $("review-form");
    form.replaceChildren();
    for (const [key, label, get] of FIELDS) {
      const wrap = el("label", "field", label);
      const input = el("input");
      input.name = key;
      input.value = get(record) || "";
      wrap.append(input);
      form.append(wrap);
    }
    $("summary-preview").textContent = record.spoken_summary;
    show("review");
    $("review").scrollIntoView({ behavior: "smooth" });
  }

  $("confirm").onclick = async () => {
    const edits = {};
    for (const input of $("review-form").querySelectorAll("input")) edits[input.name] = input.value;
    const r = await api(`/api/session/${sid}/confirm`, { method: "POST", body: { edits } });
    record = r.record;
    $("summary-preview").textContent = record.spoken_summary;
    $("confirm").textContent = "Confirmed";
    $("confirm").disabled = true;
    renderCredential(r.credential, r.practice);
    show("after");
    $("after").scrollIntoView({ behavior: "smooth" });
  };

  function renderCredential(cred, practice) {
    $("credential").textContent = JSON.stringify(cred, null, 2);
    $("practice-status").textContent = practice.status;
    $("practice-status").className = practice.status === "authorised" ? "good" : "bad";
    show("revoke", practice.status === "authorised");
    show("reissue", practice.status !== "authorised");
  }

  $("revoke").onclick = async () => {
    const r = await api(`/api/session/${sid}/revoke`, { method: "POST" });
    renderCredential(r.credential, r.practice);
  };
  $("reissue").onclick = async () => {
    const r = await api(`/api/session/${sid}/credential`, { method: "POST" });
    renderCredential(r.credential, r.practice);
  };

  async function trySpeak(field) {
    $("speak-result").className = "sub";
    try {
      const blob = await api(`/api/session/${sid}/speak?field=${encodeURIComponent(field)}`);
      if (!blob) {
        $("speak-result").textContent = `Allowed. Kokoro is not installed, so as text: "${record.spoken_summary}"`;
        return;
      }
      $("speak-result").textContent = "Allowed. Playing, generated on this device.";
      new Audio(URL.createObjectURL(blob)).play();
    } catch (err) {
      $("speak-result").textContent = `Refused by the speech boundary: ${err.message}`;
      $("speak-result").className = "sub bad";
    }
  }
  $("speak-summary").onclick = () => trySpeak("spoken_summary");
  $("speak-clinical").onclick = () => trySpeak("clinical_detail");

  $("place-call").onclick = async () => {
    const log = $("call-log");
    const out = $("call-outcome");
    log.replaceChildren(); out.replaceChildren();
    $("place-call").disabled = true;
    try {
      const r = await api(`/api/session/${sid}/call`, { method: "POST", body: { scenario: $("scenario").value } });
      for (const ev of r.events) {
        await new Promise((ok) => setTimeout(ok, ev.kind === "silent" ? 150 : 450));
        const line = el("div", `turn ${ev.speaker} ${ev.kind}`);
        line.append(el("span", "t", `t+${ev.t.toFixed(1)}s`), el("span", "who", ev.speaker), el("span", "what", ev.text));
        log.append(line);
        log.scrollTop = log.scrollHeight;
      }
      const box = el("div", "outcome " + (r.outcome === "booked" ? "good" : "warn"));
      box.append(el("strong", "", r.agreed ? `Booked: ${r.agreed}` : `Outcome: ${r.outcome.replaceAll("_", " ")}`));
      if (r.opening_latency !== null) box.append(el("p", "sub", `Opening line spoken ${r.opening_latency}s after pickup (limit 2s).`));
      if (r.next_attempt) box.append(el("p", "sub", `Next attempt scheduled: ${new Date(r.next_attempt).toLocaleTimeString()}`));
      if (r.unanswered.length) {
        box.append(el("p", "", "Things I could not answer for you:"));
        const ul = el("ul");
        r.unanswered.forEach((u) => ul.append(el("li", "", u)));
        box.append(ul);
      }
      out.append(box);
    } catch (err) {
      out.append(el("div", "outcome bad", `Call not placed: ${err.message}`));
    } finally {
      $("place-call").disabled = false;
    }
  };

  $("show-audit").onclick = async () => {
    const r = await api(`/api/session/${sid}/audit`);
    const box = $("audit");
    box.replaceChildren();
    if (!r.entries.length) box.append(el("p", "sub", "Nothing has been said on your behalf yet."));
    for (const e of r.entries) {
      const row = el("div", "audit-row");
      row.append(el("span", "t", new Date(e.timestamp).toLocaleTimeString()), el("span", "who", e.speaker));
      const body = el("div");
      if (e.heard) body.append(el("div", "", `Heard: ${e.heard}`));
      if (e.said) body.append(el("div", "", `Said: ${e.said}`));
      if (e.agreed) body.append(el("div", "good", `Agreed: ${e.agreed}`));
      if (e.unanswered) body.append(el("div", "bad", `Could not answer: ${e.unanswered}`));
      row.append(body);
      box.append(row);
    }
  };

  function crisis(result) {
    $("answer-form").classList.add("hidden");
    const actions = $("crisis-actions");
    actions.replaceChildren();
    const call = (label, tel, cls = "") => {
      const a = el("a", "btn " + cls, label);
      a.href = `tel:${tel}`;
      actions.append(a);
    };
    if (result.kind === "red_flag") {
      $("crisis-title").textContent = "This needs urgent help now";
      $("crisis-msg").textContent = result.message;
      call("Ring 999", "999", "danger");
      call("Ring 111 if unsure", "111");
    } else {
      $("crisis-title").textContent = "You don't have to deal with this alone";
      $("crisis-msg").textContent = result.message;
      call("Samaritans: 116 123", "116123");
      call("Ring 999 if in danger", "999", "danger");
      call("Ring my GP directly", "01632960123");
    }
    const close = el("button", "secondary", "Close");
    close.onclick = () => show("crisis", false);
    actions.append(close);
    show("crisis");
  }
}

/* ---------------- boundary panels ---------------- */

function initPanels() {
  let mode = "local";
  const setMode = (m) => {
    mode = m;
    $("mode-local").classList.toggle("on", m === "local");
    $("mode-naive").classList.toggle("on", m === "naive");
    document.body.classList.toggle("naive", m === "naive");
    refresh();
  };
  $("mode-local").onclick = () => setMode("local");
  $("mode-naive").onclick = () => setMode("naive");

  function fmt(value) {
    if (typeof value === "string") return value;
    if (Array.isArray(value) && value.every((v) => v && v.role)) {
      return value.map((t) => `${t.role === "patient" ? "Her" : "Assistant"}: ${t.text}`).join("\n");
    }
    return JSON.stringify(value, null, 2);
  }

  function item(label, value, dest) {
    const box = el("div", "item");
    const head = el("div", "item-head");
    head.append(el("strong", "", label));
    if (dest) head.append(el("span", "dest", dest));
    box.append(head, el("pre", "", fmt(value)));
    return box;
  }

  async function refresh() {
    let d;
    try { d = await api(`/api/boundary?mode=${mode}`); } catch { return; }
    $("stays").replaceChildren(...d.stays.map((s) => item(s.label, s.value)));
    const leaves = d.leaves.map((l) => item(l.label, l.value, `${l.dest} · ${l.bytes} B`));
    if (!leaves.length) leaves.push(el("p", "empty", d.session_id ? "Nothing has left this device." : "Start a request to see the data flow."));
    if (d.crisis) leaves.unshift(el("p", "empty", d.crisis === "distress" ? "Crisis support shown. Nothing stored, nothing sent." : "Red flag: urgent help shown. No call placed."));
    $("leaves").replaceChildren(...leaves);
    $("fields-out").textContent = d.fields_out;
    $("bytes-out").textContent = d.bytes_out.toLocaleString();
  }

  refresh();
  setInterval(refresh, 1000);
}

if (document.body.dataset.page === "patient") initPatient();
if (document.body.dataset.page === "panels") initPanels();

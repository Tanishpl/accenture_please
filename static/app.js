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

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
}

function show(id, on = true) { $(id).classList.toggle("hidden", !on); }

/* ================= patient page ================= */

function initPatient() {
  let sid = null;
  let record = null;
  let recorder = null;
  let busy = false;
  let selected = new Set();

  api("/api/status").then((s) => {
    const pills = $("status");
    for (const [k, label] of [["stt", "Voice input"], ["tts", "Voice output"], ["ollama", "Local AI model"]]) {
      pills.append(el("span", "pill " + (s[k] ? "ok" : "off"), `${label}: ${s[k] ? "on, local" : "not installed"}`));
    }
    if (!s.stt) { $("mic").disabled = true; $("mic").title = "Voice input needs faster-whisper installed. Typing works."; }
  }).catch(() => {});

  function setStep(n) {
    for (const li of $("steps").children) {
      const s = Number(li.dataset.step);
      li.classList.toggle("active", s === n);
      li.classList.toggle("done", s < n);
    }
  }

  /* ---- chat ---- */

  function bubble(role, text) {
    const b = el("div", "msg " + role, text);
    $("chat").append(b);
    b.scrollIntoView({ block: "end", behavior: "smooth" });
    return b;
  }

  async function assistantSays(...lines) {
    for (const line of lines.filter(Boolean)) {
      const typing = bubble("assistant typing", "");
      typing.append(el("span"), el("span"), el("span"));
      await sleep(Math.min(250 + line.length * 6, 750));
      typing.remove();
      bubble("assistant", line);
    }
  }

  function setReplyControls(q) {
    const chips = $("chips");
    chips.replaceChildren();
    selected = new Set();
    for (const label of q.options || []) {
      const c = el("button", "chip", label);
      c.type = "button";
      c.onclick = () => {
        if (!q.multi) return submit(label);
        // "Nothing needed" / "Any time" are exclusive with the rest
        const exclusive = /^(nothing|any time|no )/i;
        if (selected.has(label)) selected.delete(label);
        else {
          if (exclusive.test(label)) selected.clear();
          else for (const s of [...selected]) if (exclusive.test(s)) selected.delete(s);
          selected.add(label);
        }
        for (const b of chips.querySelectorAll(".chip")) b.classList.toggle("on", selected.has(b.textContent));
        $("done-multi").disabled = selected.size === 0;
      };
      chips.append(c);
    }
    if (q.multi) {
      const done = el("button", "chip done", "Done");
      done.id = "done-multi";
      done.type = "button";
      done.disabled = true;
      done.onclick = () => submit([...selected].join(", "));
      chips.append(done);
    }
    $("answer").placeholder = q.placeholder || "Type your answer";
    $("answer").value = "";
    autosize();
    if (!q.options || !q.options.length) $("answer").focus();
  }

  function setProgress(p) {
    if (!p) return;
    const pct = Math.round((p.done / p.total) * 100);
    $("progress-bar").style.width = pct + "%";
    $("progress-label").textContent = p.done >= p.total ? "All done" : `About ${p.total - p.done} short question${p.total - p.done === 1 ? "" : "s"} left`;
  }

  async function handle(result) {
    setProgress(result.progress);
    if (result.type === "crisis") return crisis(result);
    if (result.type === "question") {
      // Short acks share the question's bubble; longer ones (skips, opinion) get their own.
      if (result.ack && result.ack.length < 20) await assistantSays(`${result.ack} ${result.text}`);
      else await assistantSays(result.ack, result.text);
      setReplyControls(result);
    }
    if (result.type === "review") {
      await assistantSays(result.text);
      show("reply", false);
      record = result.record;
      renderReview();
    }
  }

  async function submit(text) {
    text = (text || "").trim();
    if (!text || !sid || busy) return;
    busy = true;
    $("chips").replaceChildren();
    $("answer").value = "";
    autosize();
    bubble("patient", text);
    try {
      await handle(await api(`/api/session/${sid}/answer`, { method: "POST", body: { text } }));
    } catch (err) {
      bubble("assistant error", `Something went wrong: ${err.message}`);
    } finally {
      busy = false;
    }
  }

  function autosize() {
    const a = $("answer");
    a.style.height = "auto";
    a.style.height = Math.min(a.scrollHeight, 140) + "px";
  }
  $("answer").addEventListener("input", autosize);
  $("answer").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit($("answer").value); }
  });
  $("answer-form").onsubmit = (e) => { e.preventDefault(); submit($("answer").value); };

  async function start() {
    $("chat").replaceChildren();
    $("call-log").replaceChildren();
    $("call-outcome").replaceChildren();
    $("audit").replaceChildren(el("p", "sub", "Nothing yet."));
    ["welcome", "review", "after", "crisis"].forEach((id) => show(id, false));
    ["interview", "reply", "steps", "restart"].forEach((id) => show(id));
    $("confirm").disabled = false;
    $("confirm").textContent = "Yes, that's right. Book it";
    setStep(1);
    const r = await api("/api/session", { method: "POST" });
    sid = r.session_id;
    await handle(r);
  }
  $("start").onclick = start;
  $("restart").onclick = start;

  /* ---- voice in ---- */

  $("mic").onclick = async () => {
    if (!sid) return;
    if (recorder && recorder.state === "recording") { recorder.stop(); return; }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch { bubble("assistant error", "I couldn't use the microphone. You can type instead."); return; }
    const chunks = [];
    recorder = new MediaRecorder(stream);
    recorder.ondataavailable = (e) => chunks.push(e.data);
    recorder.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      $("mic").classList.remove("recording");
      const form = new FormData();
      form.append("file", new Blob(chunks, { type: "audio/webm" }), "answer.webm");
      try {
        const r = await api(`/api/session/${sid}/audio`, { method: "POST", body: form });
        bubble("patient", r.transcript);
        await handle(r);
      } catch (err) { bubble("assistant error", err.message); }
    };
    recorder.start();
    $("mic").classList.add("recording");
  };

  /* ---- review ---- */

  const GROUPS = [
    ["What it's about", [
      ["reason_for_contact", "In a few words (the surgery hears this)", (r) => r.reason_for_contact],
      ["description", "In your words (private, never said aloud)", (r) => r.clinical_detail.description, "textarea"],
    ]],
    ["Private details", [
      ["duration", "How long", (r) => r.clinical_detail.duration],
      ["trajectory", "Better, worse or the same", (r) => r.clinical_detail.trajectory],
      ["taking_anything", "Taking for it", (r) => r.clinical_detail.taking_anything || "nothing"],
    ]],
    ["Your appointment", [
      ["urgency", "How soon", (r) => r.preferences.urgency],
      ["appointment_type", "Come in or phone", (r) => r.preferences.appointment_type],
      ["times", "Times that suit", (r) => r.preferences.times.join(", ")],
      ["preferred_clinician", "Doctor or nurse", (r) => r.preferences.preferred_clinician || "no preference"],
    ]],
    ["Help on the day", [
      ["access_needs", "Anything that would help", (r) => r.access_needs.join(", ") || "nothing"],
    ]],
  ];

  function renderReview() {
    setStep(2);
    const form = $("review-form");
    form.replaceChildren();
    for (const [title, fields] of GROUPS) {
      const fs = el("fieldset", "group");
      fs.append(el("legend", "", title));
      for (const [key, label, get, kind] of fields) {
        const wrap = el("label", "field");
        wrap.append(el("span", "", label));
        const input = el(kind || "input");
        input.name = key;
        input.value = get(record) || "";
        if (kind === "textarea") input.rows = 2;
        input.addEventListener("input", previewSummary);
        wrap.append(input);
        fs.append(wrap);
      }
      form.append(fs);
    }
    $("summary-preview").textContent = record.spoken_summary;
    show("review");
    $("review").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function edits() {
    const out = {};
    for (const input of $("review-form").querySelectorAll("input, textarea")) {
      let v = input.value.trim();
      if (["taking_anything", "access_needs"].includes(input.name) && /^(nothing|none|no)$/i.test(v)) v = "";
      if (input.name === "preferred_clinician" && /^no( preference)?$/i.test(v)) v = "";
      out[input.name] = v;
    }
    return out;
  }

  let previewTimer;
  function previewSummary() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => {
      const e = edits();
      try {
        const r = await api("/api/summary-preview", { method: "POST", body: { reason: e.reason_for_contact, urgency: e.urgency, times: e.times } });
        $("summary-preview").textContent = r.spoken_summary;
      } catch { /* preview only */ }
    }, 250);
  }

  $("confirm").onclick = async () => {
    $("confirm").disabled = true;
    try {
      const r = await api(`/api/session/${sid}/confirm`, { method: "POST", body: { edits: edits() } });
      record = r.record;
      $("summary-preview").textContent = record.spoken_summary;
      $("confirm").textContent = "Confirmed";
      for (const i of $("review-form").querySelectorAll("input, textarea")) i.disabled = true;
      renderCredential(r.credential, r.practice);
      setStep(3);
      show("after");
      $("after").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      $("confirm").disabled = false;
      alert(err.message);
    }
  };

  /* ---- credential ---- */

  function renderCredential(cred, practice) {
    const dl = $("credential");
    dl.replaceChildren();
    const rows = [
      ["Issued by", cred.issued_by],
      ["Allowed to", "book or cancel appointments, order repeat prescriptions"],
      ["Covers", "your own GP practice only"],
      ["Your identity", "held by the practice, never by the assistant"],
      ["Expires", new Date(cred.expires).toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" })],
      ["Reference", cred.credential_id],
    ];
    for (const [k, v] of rows) dl.append(el("dt", "", k), el("dd", "", v));
    const ok = practice.status === "authorised";
    $("practice-status").textContent = ok ? "Active" : "Revoked";
    $("practice-status").className = "badge " + (ok ? "good" : "bad");
    show("revoke", ok);
    show("reissue", !ok);
    $("place-call").disabled = !ok;
    $("place-call").title = ok ? "" : "Give permission again to place a call";
  }

  $("revoke").onclick = async () => {
    const r = await api(`/api/session/${sid}/revoke`, { method: "POST" });
    renderCredential(r.credential, r.practice);
  };
  $("reissue").onclick = async () => {
    const r = await api(`/api/session/${sid}/credential`, { method: "POST" });
    renderCredential(r.credential, r.practice);
  };

  /* ---- voice boundary ---- */

  async function trySpeak(field) {
    const out = $("speak-result");
    out.className = "result";
    try {
      const blob = await api(`/api/session/${sid}/speak?field=${encodeURIComponent(field)}`);
      out.classList.add("good");
      if (!blob) { out.textContent = `Allowed. (Voice output not installed, so here it is as text:) "${record.spoken_summary}"`; return; }
      out.textContent = "Allowed. Playing, generated on this device.";
      new Audio(URL.createObjectURL(blob)).play();
    } catch (err) {
      out.classList.add("bad");
      out.textContent = `Blocked by the code, not by a prompt: ${err.message}.`;
    }
  }
  $("speak-summary").onclick = () => trySpeak("spoken_summary");
  $("speak-clinical").onclick = () => trySpeak("clinical_detail");

  /* ---- call ---- */

  $("place-call").onclick = async () => {
    const log = $("call-log");
    const out = $("call-outcome");
    log.replaceChildren();
    out.replaceChildren();
    $("place-call").disabled = true;
    try {
      const r = await api(`/api/session/${sid}/call`, { method: "POST", body: { scenario: $("scenario").value } });
      for (const ev of r.events) {
        const isSpeech = ev.kind === "speech" || ev.kind === "ivr" || ev.kind === "voicemail";
        await sleep(isSpeech ? 550 : 250);
        const line = el("div", `turn ${ev.speaker} ${ev.kind}`);
        if (isSpeech) {
          line.append(el("span", "who", ev.speaker === "agent" ? "Assistant" : "Surgery"), el("span", "what", ev.text));
        } else {
          line.append(el("span", "what", ev.text.replace(/^\[|\]$/g, "")));
        }
        line.append(el("span", "t", `${ev.t.toFixed(1)}s`));
        log.append(line);
        log.scrollTop = log.scrollHeight;
      }
      out.append(outcomeCard(r));
      loadAudit();
    } catch (err) {
      out.append(el("div", "outcome bad", `Call not placed. ${err.message}`));
    } finally {
      $("place-call").disabled = $("practice-status").textContent !== "Active";
    }
  };

  function outcomeCard(r) {
    const titles = {
      booked: `Booked: ${r.agreed}`,
      declined: "The surgery wouldn't talk to an assistant",
      unparsed_offer_callback: "The line was too unclear",
      retry_scheduled: "The surgery was closed",
      attempt_cap_reached: "Tried 3 times today, stopping",
      ended_without_booking: "No appointment was offered",
    };
    const box = el("div", "outcome " + (r.outcome === "booked" ? "good" : "warn"));
    box.append(el("strong", "", titles[r.outcome] || r.outcome));
    if (r.outcome === "declined") box.append(el("p", "", "Please ring them yourself. Your answers are saved below so you have them ready."));
    if (r.next_attempt) box.append(el("p", "", `I'll try again at ${new Date(r.next_attempt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}.`));
    if (r.unanswered.length) {
      box.append(el("p", "", "Things I couldn't answer for you:"));
      const ul = el("ul");
      r.unanswered.forEach((u) => ul.append(el("li", "", u)));
      box.append(ul);
    }
    if (r.opening_latency !== null) box.append(el("p", "sub small", `Said it was an assistant ${r.opening_latency}s after pickup (Ofcom guidance: within 2s).`));
    return box;
  }

  /* ---- audit ---- */

  async function loadAudit() {
    const r = await api(`/api/session/${sid}/audit`);
    const box = $("audit");
    box.replaceChildren();
    if (!r.entries.length) { box.append(el("p", "sub", "Nothing yet.")); return; }
    for (const e of r.entries) {
      const row = el("div", "audit-row");
      row.append(el("span", "t", new Date(e.timestamp).toLocaleTimeString()));
      const body = el("div");
      if (e.heard) body.append(el("div", "heard", `Heard: ${e.heard}`));
      if (e.said) body.append(el("div", "", `${e.speaker === "agent" ? "Said" : "Event"}: ${e.said}`));
      if (e.agreed) body.append(el("div", "good", `Agreed: ${e.agreed}`));
      if (e.unanswered) body.append(el("div", "bad", `Couldn't answer: ${e.unanswered}`));
      row.append(body);
      box.append(row);
    }
  }
  $("show-audit").onclick = loadAudit;

  /* ---- crisis ---- */

  function crisis(result) {
    show("reply", false);
    const actions = $("crisis-actions");
    actions.replaceChildren();
    const call = (label, tel, cls = "") => {
      const a = el("a", "btn " + cls, label);
      a.href = `tel:${tel}`;
      actions.append(a);
    };
    if (result.kind === "red_flag") {
      $("crisis-title").textContent = "This needs urgent help now";
      call("Ring 999", "999", "danger");
      call("Ring 111 if you're not sure", "111");
    } else {
      $("crisis-title").textContent = "You don't have to deal with this alone";
      call("Samaritans, free, any time: 116 123", "116123", "primary");
      call("Ring 999 if you're in danger", "999", "danger");
      call("Ring my GP directly", "01632960123");
    }
    $("crisis-msg").textContent = result.message;
    const close = el("button", "link", "Close");
    close.onclick = () => show("crisis", false);
    actions.append(close);
    show("crisis");
  }
}

/* ================= boundary panels ================= */

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

  const PRETTY = {
    description: "Description", duration: "How long", trajectory: "Trend", seen_before_for_this: "Seen before",
    taking_anything: "Taking", reason: "Description", seen_before: "Seen before", urgency: "How soon",
    appointment_type: "Type", times: "Times", preferred_clinician: "Clinician", access_needs: "Help on the day",
    contact_number: "Contact number",
  };

  function render(value) {
    if (typeof value === "string") return el("p", "val", value);
    if (Array.isArray(value) && value.every((v) => v && v.role)) {
      const box = el("div", "mini-chat");
      value.slice(-8).forEach((t) => box.append(el("div", "mini " + t.role, t.text)));
      if (value.length > 8) box.prepend(el("div", "more", `${value.length - 8} earlier lines`));
      return box;
    }
    if (value && typeof value === "object") {
      if (value.request_id) {
        const r = value;
        value = {
          "Request": r.request_id.slice(0, 8),
          "Reason": r.reason_for_contact,
          "Confirmed by her": r.confirmed_by_patient ? "yes" : "not yet",
          "Clinical fields": Object.keys(r.clinical_detail).length,
          "Access needs": r.access_needs.length || "none",
        };
      }
      const dl = el("dl", "kv small");
      const entries = Object.entries(value).filter(([, v]) => v !== "" && v !== null);
      if (!entries.length) return el("p", "val muted", "Nothing yet");
      for (const [k, v] of entries) {
        const shown = Array.isArray(v) ? v.join(", ") : typeof v === "boolean" ? (v ? "yes" : "no") : String(v);
        dl.append(el("dt", "", PRETTY[k] || k), el("dd", "", shown));
      }
      return dl;
    }
    return el("p", "val", String(value));
  }

  function item(label, value, dest, bytes) {
    const box = el("div", "item");
    const head = el("div", "item-head");
    head.append(el("strong", "", label));
    if (dest) head.append(el("span", "dest", `→ ${dest}`));
    box.append(head, render(value));
    if (bytes !== undefined) box.append(el("span", "bytes", `${bytes.toLocaleString()} bytes`));
    return box;
  }

  let lastKey = "";
  async function refresh() {
    let d;
    try { d = await api(`/api/boundary?mode=${mode}`); } catch { return; }
    const key = JSON.stringify(d);
    if (key === lastKey) return;
    lastKey = key;

    $("stays").replaceChildren(...d.stays.map((s) => item(s.label, s.value)));
    const leaves = d.leaves.map((l) => item(l.label, l.value, l.dest, l.bytes));
    if (d.crisis) {
      leaves.unshift(el("p", "empty", d.crisis === "distress"
        ? "Crisis support shown. Nothing stored, nothing sent."
        : "Red flag: urgent help shown. No call placed."));
    }
    if (!leaves.length) {
      leaves.push(el("p", "empty", d.session_id
        ? "Nothing has left this device. The surgery will only hear one sentence, once she confirms."
        : "Start a request on the left to watch the data flow."));
    }
    $("leaves").replaceChildren(...leaves);
    $("fields-out").textContent = d.fields_out;
    $("bytes-out").textContent = d.bytes_out.toLocaleString();
    $("health-out").textContent = d.leaves.filter((l) => /cloud|vendor/i.test(l.dest)).length;
  }

  refresh();
  setInterval(refresh, 1000);
}

if (document.body.dataset.page === "patient") initPatient();
if (document.body.dataset.page === "panels") initPanels();

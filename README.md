# GP Booking Assistant: local-first

Hackathon build, Accenture challenge track.

A patient describes what she needs on her own device. A bounded interview builds a
structured record, she confirms it, and an agent phones the surgery, says it is an AI
acting for a registered patient, and books from her answers. Everything about her body
and her voice is processed on her device.

**The claim is minimisation by architecture, not by vendor promise.** Retention
controls stop storage; they do not stop processing. So no health data reaches any AI
vendor at all.

## Run it

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. The left side is the patient app; the right side is the
data boundary view (also standalone at `/panels`).

### Speaking instead of typing

"Speak your answers instead" on the start page reads each question aloud and listens,
stopping when she goes quiet. One long answer to the first question can fill several
later ones ("it's been getting worse for two weeks, mornings are best"), and spoken
answers like "I don't mind" are matched to the options.

- **Listening** uses local Whisper via faster-whisper. The model downloads once (~140MB)
  and loads at startup. Chrome's built-in speech recognition is deliberately not used,
  because it sends audio to Google.
- **Speaking** uses only voices installed on the device (`localService`), e.g. Microsoft
  Hazel on Windows. Network voices are filtered out.

### Local models for weak hardware

The reasoning job is narrow (pull ~10 fields out of what she said), so small models are
enough. Rule-based extraction always runs, so the app works instantly with no model at all;
the model only runs once, on her opening description.

| Device | Speech in (`GP_AGENT_WHISPER`) | Reasoning (`ollama pull ...`) |
|---|---|---|
| Old laptop, 4GB RAM | `tiny.en` | `qwen2.5:1.5b-instruct` or `llama3.2:1b` |
| Typical 8GB laptop | `base.en` (default) | `qwen2.5:3b-instruct` |
| 16GB+ | `small.en` | a 7B model, set with `GP_AGENT_LLM` |

The app uses the first installed model from its preference list, so pulling a smaller one
is all a weaker device needs.

Tests: `pytest`

## Layout

```
app/
  main.py          FastAPI, serves UI and API (localhost only)
  interview.py     the fixed question set, extraction, record building
  record.py        schema, SQLite, audit log, SPEAKABLE allowlist
  safety.py        red flags, distress, opinion-seeking
  call.py          agent turn logic, fallback table, pacing
  speech.py        local STT/TTS, and the speech boundary
  delegation.py    mocked delegation credential, revoke
static/            index.html (patient), panels.html (boundary view), app.js, styles.css
mock/receptionist.py   scripted practice side, five scenarios
docs/DPIA.md       pre-filled DPIA
tests/             end-to-end and boundary tests
```

## Hard constraints, and where each is enforced

| Constraint | Enforced in |
|---|---|
| No clinical content leaves the device | No cloud client exists in the codebase. `speakable_text()` only reads allowlisted fields; tests assert clinical terms are never spoken |
| No patient audio leaves the device | `speech.transcribe` runs faster-whisper locally; upload is deleted after transcription |
| No cloud speech synthesis | `speech.kokoro_synthesise` is local; there is no fallback to a cloud TTS |
| Never authenticates as the patient | The record holds no DOB, address or NHS number; identity questions get a fixed refusal |
| Never triages or advises | Clinical questions get a fixed refusal on the call; opinion-seeking is deflected in the interview |
| Patient confirms before any call | `run_call` raises `CallRefused` unless `confirmed_by_patient` |
| Only allowlisted fields spoken | `SPEAKABLE` in `record.py`, checked in `speech.speakable_text` |

The speech boundary, in full:

```python
SPEAKABLE = {"spoken_summary", "reason_for_contact", "preferences"}

def speakable_text(record: dict, field: str) -> str:
    if field not in SPEAKABLE:
        raise ValueError(f"{field} is not speakable")
    ...
```

The "Try to speak clinical detail" button in the UI shows this refusal live.

The spoken summary is a deterministic template built only from the reason phrase and
preferences, not free model output, so a local model cannot leak a symptom into it.

## Demo script

1. Start a request. Type: *"My right knee hurts, it's been getting worse for 4 days"*.
   The duration and trend are picked up, so those questions are skipped.
2. Watch the boundary view: the left column fills, the right says nothing has left.
3. Flip to **Naive cloud build**: the same answers fill the right column, with bytes.
4. Finish the interview, correct a field, confirm. The credential appears, labelled mocked.
5. Play the spoken summary, then try to speak clinical detail: refused.
6. Place a call with **Full: exercises every fallback**. The agent opens within 2s,
   refuses the DOB check, refuses the clinical question, says it doesn't know the email,
   then books Thursday 9:20 and reads it back.
7. Show the audit log and the list of things it could not answer.
8. Revoke authority and try to call again: refused.
9. Safety: start over and type *"I've got crushing chest pain"*: 999/111 screen, no call.

## Call fallbacks

| Situation | Behaviour |
|---|---|
| Question the record answers | Answers briefly from allowlisted fields |
| Question the record does not answer | "I do not have that answer. I can ask her and ring back, or connect her now if she is free." |
| Clinical question | "I cannot answer that, I am not clinical..." |
| Identity check | "I cannot confirm her identity, I am an assistant..." |
| Practice declines an AI | "Understood, I will ask her to call you directly." Ends and flags it |
| Three unparseable turns | Offers to connect her or call back |
| Voicemail or closed | Hangs up, schedules a retry, capped at 3 attempts a day |

## What is not built

- **Real telephony.** Out of scope by design: the thesis is about data boundaries, and a
  live line is the likeliest thing to break on stage. The practice side is scripted in
  `mock/receptionist.py`; its lines can be voiced with ElevenLabs for the demo. ElevenLabs
  is never used for anything the patient says.
- **Patient voice cloning.** Deliberately excluded: her voice is biometric, voice cloning
  is outside that vendor's own Zero Retention Mode, and the agent announces it is an AI
  anyway.

## Declared gaps

See `docs/DPIA.md` section 7. In short: the carrier hears the call; local storage is
unencrypted in the demo; detecting a live human is unreliable; the delegation credential
is mocked because no standard exists; a small model can mis-summarise (mitigated by
confirmation and no triage); practices may refuse an AI caller.

## Lines

- **Vendor tier.** We did not build it so we could afford enterprise retention controls
  later. We built it so we don't need them, because retention controls stop storage, not
  processing.
- **Voice.** We hold an ElevenLabs subscription and deliberately did not use it for
  anything the patient says. We accepted worse audio to keep her voice on her device.
- **Identity.** The agent never proves she is who she says she is. An agent that never
  holds her credentials cannot be used to impersonate her.
- **Baseline.** The alternative isn't privacy. It's asking a neighbour, who has no
  processing agreement, no access controls, no audit log and no legal duty.

# GP Queue Assistant

A patient fills in one form. We phone their GP surgery, wait in the queue,
press the IVR keys, and when a receptionist answers, an ElevenLabs voice agent
books the appointment using the patient's details. The patient gets an SMS
with the result. If the receptionist needs the patient, we ring them and join
them into the call.

```
Web form ─► FastAPI ─► Twilio outbound call ─► GP surgery
                          │
   Phase 1: on hold       │  <Start><Stream> forks audio ─► /ws/hold ─► Deepgram STT
   (cheap)                │  classifier: hold / ivr_menu / human / closed
                          │  ivr_menu ─► <Play digits>
                          ▼  human ─► swap TwiML
   Phase 2: talking       <Connect><Stream> ─► /ws/agent ◄─► ElevenLabs agent websocket
                          tools: record_booking · connect_patient · report_outcome · end_call
   Phase 3 (optional)     Twilio conference: receptionist + patient
```

Why two phases: queues can last 30 to 60 minutes. Running the voice agent the
whole time costs per minute, and recorded announcements confuse it. Deepgram
plus a short classifier call per utterance is much cheaper.

## Layout

| File | What it does |
|---|---|
| `app/main.py` | Routes: intake API, status API, manual controls, Twilio webhooks, websockets |
| `app/telephony.py` | Twilio calls, TwiML for each phase, conference handoff, SMS |
| `app/hold_listener.py` | Phase 1: Twilio audio ─► Deepgram ─► classifier ─► press keys / take over |
| `app/classifier.py` | Rules (queue announcements, repeats) + Claude for menus and "is that a person?" |
| `app/agent_bridge.py` | Phase 2: audio bridge Twilio ⇄ ElevenLabs, client tool handlers |
| `app/models.py` | Intake validation, call session state |
| `app/static/index.html` | Intake form + live status page |
| `scripts/setup_agent.py` | Creates the ElevenLabs agent (prompt, tools, μ-law audio) |

## Setup

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # fill in keys
ngrok http 8000                 # put the https URL in PUBLIC_URL
.venv/bin/python scripts/setup_agent.py   # prints ELEVENLABS_AGENT_ID for .env
.venv/bin/uvicorn app.main:app --port 8000
```

Open the ngrok URL, fill in the form, and use a teammate's phone number as
the "surgery". Tests: `.venv/bin/python -m pytest`.

You need:
- **Twilio**: a number that can call UK numbers. Trial accounts can only call verified numbers.
- **ElevenLabs**: an API key. Choose a British voice and set `ELEVENLABS_VOICE_ID`.
  The agent must use `ulaw_8000` for both input and output; the setup script sets this.
- **Deepgram** (optional): an API key for live transcription while on hold.
  Without it, press **Receptionist answered** on the status page when someone picks up.
- **Anthropic** (optional): credentials for the hold classifier. Without them it uses keyword rules.

## Demo script

1. The teammate's phone rings ("the surgery"). They play a recording: "For
   prescriptions press 1, for appointments press 2." The log shows `Pressed 2`.
2. They play hold music and "You are number 3 in the queue". The log shows `[hold]`.
3. They answer: "Good morning, Riverside Surgery, Sam speaking." The agent takes over.
4. They offer a slot. The agent confirms it, the status page shows the booking, and the SMS arrives.
5. Optional: "I need to speak to the patient." The patient's phone rings and the two are connected.

If transcription is unreliable on the day, use the **Press a key** and
**Receptionist answered** buttons. The demo still works end to end.

## Test before the demo

- **Does the hold-audio fork survive a key press?** Pressing a key replaces the
  call's TwiML. The code assumes the `<Start><Stream>` fork keeps running when
  that happens, but I couldn't confirm it in Twilio's docs. Make one test call
  and press a key. If the log stops showing `heard` lines afterwards, make
  `twiml_press_digits` start the stream again.
- **Exact ElevenLabs fields**: the agent and tool payloads in `setup_agent.py`
  follow the current API reference, but check the agent in the dashboard after
  creating it.

## Before real-world use

- Validate `X-Twilio-Signature` on the webhooks (see the TODO in `main.py`).
- Sessions are kept in memory and dropped an hour after the call. Health data
  is special-category data under UK GDPR, so encrypt it, keep it for as little
  time as possible, and document a lawful basis.
- The agent says it is an AI and only shares details when asked. Some
  practices will still insist on speaking to the patient; the
  `connect_patient` handoff covers that case.
- Intake rejects emergency wording and directs people to 999 or 111. Extend
  this list with clinical input.

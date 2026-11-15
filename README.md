# GP Queue Assistant

A patient fills in one form. We phone their GP surgery, wait in the queue,
press the IVR keys, and when a receptionist answers, a voice agent books the
appointment using the patient's details. The patient gets an SMS with the
result. If the receptionist needs the patient, we ring them and join them into
the call.

```
Web form ─► FastAPI ─► Twilio outbound call ─► GP surgery
                          │
   Each phrase on the line: <Gather input="speech"> ─► POST /twiml/heard/{id}
                          │
   On hold                │  classifier: hold / ivr_menu / human / closed
                          │  ivr_menu ─► <Play digits>, keep listening
                          ▼  human ─► agent's opening line
   Talking                ElevenLabs agent (text-only) ─► ElevenLabs TTS ─► <Play>
                          booking ─► SMS · connect_patient ─► conference · hang_up
```

### Why it's built this way: Twilio trial limits

The project runs on a Twilio **trial** account, which:

- **blocks `<Stream>`**, so there's no live audio. Twilio transcribes each
  phrase itself (`<Gather input="speech">`) and we reply with new TwiML.
- **ends the call after 10 `<Gather>`/`<Redirect>` callbacks.** Every phrase
  heard costs one, so a call gets about 9 exchanges, hold time included. To
  stretch that:
  - each response stacks several `<Gather>`s, so silence and hold music cost nothing;
  - the agent's opening line is a template, so taking over needs no extra turn;
  - Claude is told how many turns are left and wraps up on the last one.
  The status page shows the turns left. Set `MAX_HOPS=0` on a paid account.
- **waits only 5 seconds for each TwiML response.** The ElevenLabs agent
  usually replies in under a second; each turn is capped at
  `REPLY_TIMEOUT_SECONDS`, after which the agent asks the receptionist to
  repeat themselves.
- plays a "press any key to execute your code" message at the start of every
  call, only calls verified numbers, and caps calls at 10 minutes.

The agent's brain is an ElevenLabs agent in **text-only mode**: one
conversation websocket per call, each transcribed phrase sent as a
`user_message`, and its client tools (`record_booking`, `connect_patient`,
`report_outcome`, `hang_up`) drive the call. Anything it says after
`hang_up`/`connect_patient` is dropped.

Speech synthesis starts as soon as the reply text exists. `/audio/{id}.mp3`
streams the ElevenLabs output to Twilio's `<Play>` while it is still being
generated.

## Layout

| File | What it does |
|---|---|
| `app/main.py` | Routes: intake API, status API, manual controls, Twilio webhooks, agent audio |
| `app/call_flow.py` | What to do with each phrase heard: hold logic, takeover, agent turns, booking, hand-off |
| `app/agent.py` | Talks to the ElevenLabs text-only agent: one turn per phrase, tool calls, turn budget |
| `app/classifier.py` | Rules (queue announcements, menus, greetings, repeats), plus Claude if a key is set |
| `scripts/setup_agent.py` | Creates the ElevenLabs text-only agent (prompt + client tools) |
| `app/voice.py` | ElevenLabs text-to-speech, streamed and cached per phrase |
| `app/telephony.py` | TwiML builders (trial-safe verbs only), placing calls, SMS |
| `app/models.py` | Intake validation, call session state |
| `app/static/index.html` | Intake form + live status page |

## Setup

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # fill in keys
ngrok http 8000                 # put the https URL in PUBLIC_URL
.venv/bin/python scripts/setup_agent.py   # prints ELEVENLABS_AGENT_ID for .env
.venv/bin/uvicorn app.main:app --port 8000 --reload
```

Open the ngrok URL, fill in the form, and use a teammate's phone number as
the "surgery". Tests: `.venv/bin/python -m pytest`.

You need:
- **Twilio**: a trial account is fine. The surgery and patient numbers must be
  verified caller IDs on the account.
- **ElevenLabs** (required): an API key, and the agent id from
  `scripts/setup_agent.py`. The default voice is "George" (British); set
  `ELEVENLABS_VOICE_ID` to change it.

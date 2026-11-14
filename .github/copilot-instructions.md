# Project: GP phone-queue AI agent

## What this is
A voice agent that phones a GP surgery on a patient's behalf, waits in the
queue, and answers the receptionist's questions using only the patient's
pre-filled details. It hands the call to the patient whenever it is unsure.
Hackathon prototype for the Accenture Challenge.

## Stack (already decided, do not suggest replacements)
- Python 3.12, FastAPI, WebSockets, pydantic
- Twilio Programmable Voice with Media Streams (8kHz mulaw audio)
- Deepgram streaming speech-to-text
- Anthropic Claude via the `anthropic` SDK for the conversation LLM,
  streaming enabled
- Postgres and Redis later; not needed yet

## Hard rules
- The conversation agent may only state facts from the patient record. If
  something is not in the record, it must call `request_handoff`, never guess.
- The agent never gives or interprets medical advice.
- The agent must disclose it is an AI at the start of every call.
- Red-flag symptom checks are deterministic code (keyword rules), never LLM
  judgement.
- Use fake data only. Never put real names, NHS numbers or phone numbers in
  code, tests, logs or commit history.
- Never log full transcripts or personal details at INFO level.
- Never hard-code secrets. Read them from environment variables.

## Conventions
- Type hints everywhere; small functions; one module per pipeline stage
  (telephony, stt, llm, tts, safety, handoff).
- Every pipeline stage records timestamps so we can measure latency per turn.
- Write pytest tests for behaviour, especially refusal and handoff cases.
- Prefer streaming and async; avoid blocking calls on the audio path.

## How to work
- Make small changes. Do one task at a time and keep the app runnable.
- Before using the Twilio, Deepgram or Anthropic APIs, check their current
  docs rather than relying on memory, because APIs change.
- If a requirement is unclear, ask before building.
- Explain non-obvious code in comments so the team can present it.
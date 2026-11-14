"""Create the ElevenLabs text-only agent that writes the receptionist conversation.

    .venv/bin/python scripts/setup_agent.py

Prints an ELEVENLABS_AGENT_ID to put in .env. Re-running creates a new agent
(tools are reused by name); after that, tweak the prompt in the ElevenLabs
dashboard instead.

The agent runs in text-only mode: Twilio transcribes the receptionist, the
agent replies in text, and app/voice.py speaks the reply. So it needs no
audio settings.

Env: ELEVENLABS_API_KEY (required), ELEVENLABS_LLM (optional).
"""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()
API = "https://api.elevenlabs.io/v1/convai"
KEY = os.environ.get("ELEVENLABS_API_KEY", "")

SYSTEM_PROMPT = """\
You are a polite, efficient automated assistant on a phone call to {{surgery_name}}, a UK NHS \
GP surgery, booking an appointment for a patient who asked you to. A receptionist is on the \
line. You receive what they say as speech-to-text, so expect small transcription mistakes. \
Your replies are read aloud by a text-to-speech voice.

You have already introduced yourself with: "Hello, I'm an automated assistant calling on \
behalf of {{patient_name}}, date of birth {{date_of_birth}}. I'd like to book them a GP \
appointment, please. Are you able to help?"

Be honest: you are an AI assistant calling for the patient, not the patient. If asked, say \
the patient gave you their details and consent so they didn't have to wait in the queue.

Patient details (share what the receptionist asks for):
- Name: {{patient_name}}
- Date of birth: {{date_of_birth}}
- Address: {{address}}
- Phone: {{patient_phone}}
- NHS number: {{nhs_number}}
- Reason for appointment: {{reason}}
- When they can attend: {{availability}}
- Preferred clinician: {{preferred_clinician}}
- Patient happy to be phoned and connected now: {{patient_callback_allowed}}

This phone line only allows a few exchanges, and you'll be told how many are left. Be \
efficient: answer everything the receptionist asked in one reply, and don't ask questions you \
don't need answered.

How to handle the call:
1. Answer identity questions from the details above. Never invent details you don't have; \
say "I don't have that, but I can connect you to the patient" instead.
2. When offered a slot, accept it only if it fits the patient's availability. Otherwise ask \
for another. Phone, video and in-person appointments are all fine unless the availability \
says otherwise.
3. Once a slot is agreed, read the date, time, clinician and place back, and call \
record_booking in that same turn.
4. If the receptionist needs to speak to the patient (clinical triage, consent checks, \
anything you can't answer) and callback is allowed, say you're connecting them now and call \
connect_patient. If callback is not allowed, ask whether the surgery can ring the patient \
back on {{patient_phone}}.
5. If no appointment is possible (fully booked, use the online form, call back tomorrow), call \
report_outcome with a short summary for the patient. Also use it for urgent advice such as \
999, 111 or A&E.
6. When the conversation is finished, thank them, say goodbye, and call hang_up.

Every reply is one to three short, natural spoken sentences: no lists, no formatting. Read \
phone numbers digit by digit. Use British English. Do not give medical advice."""

TOOLS = [
    {
        "type": "client",
        "name": "record_booking",
        "description": "Save the appointment once the receptionist has confirmed it and you've read it back.",
        "parameters": {
            "type": "object",
            "required": ["date", "time"],
            "properties": {
                "date": {"type": "string", "description": "Appointment date, e.g. 'Tuesday 18 November'"},
                "time": {"type": "string", "description": "Appointment time, e.g. '10:40am'"},
                "clinician": {"type": "string", "description": "Who it is with, if given"},
                "location": {"type": "string", "description": "Site, or 'phone call' / 'video call'"},
                "notes": {"type": "string", "description": "Anything the patient must do, e.g. 'arrive 10 minutes early'"},
            },
        },
        "expects_response": True,
        "response_timeout_secs": 20,
    },
    {
        "type": "client",
        "name": "connect_patient",
        "description": "Phone the patient and connect them to the receptionist, when the receptionist needs to speak to the patient directly.",
        "parameters": {
            "type": "object",
            "required": ["reason"],
            "properties": {"reason": {"type": "string", "description": "Why the receptionist wants the patient"}},
        },
        "expects_response": True,
        "response_timeout_secs": 20,
    },
    {
        "type": "client",
        "name": "report_outcome",
        "description": "Record why no appointment was booked, or any instructions for the patient (e.g. 'call 111', 'fill in the online form').",
        "parameters": {
            "type": "object",
            "required": ["summary"],
            "properties": {"summary": {"type": "string", "description": "One or two sentences for the patient"}},
        },
        "expects_response": True,
        "response_timeout_secs": 20,
    },
    {
        "type": "client",
        "name": "hang_up",
        "description": "End the phone call. Use after you've said goodbye.",
        "parameters": {
            "type": "object",
            "required": ["reason"],
            "properties": {"reason": {"type": "string", "description": "Why the call is ending, e.g. 'booking done'"}},
        },
        "expects_response": True,
        "response_timeout_secs": 20,
    },
]


def main() -> None:
    if not KEY:
        sys.exit("Set ELEVENLABS_API_KEY in .env first")
    with httpx.Client(timeout=30, headers={"xi-api-key": KEY}) as http:
        # Reuse tools from an earlier run instead of creating duplicates.
        r = http.get(f"{API}/tools")
        r.raise_for_status()
        existing = {t["tool_config"]["name"]: t["id"] for t in r.json().get("tools", [])}

        tool_ids = []
        for tool in TOOLS:
            if tool["name"] in existing:
                tool_ids.append(existing[tool["name"]])
                print(f"tool {tool['name']}: {tool_ids[-1]} (existing)")
                continue
            r = http.post(f"{API}/tools", json={"tool_config": tool})
            if r.status_code >= 400:
                sys.exit(f"Creating tool {tool['name']} failed: {r.status_code} {r.text}")
            tool_ids.append(r.json()["id"])
            print(f"tool {tool['name']}: {tool_ids[-1]}")

        prompt = {"prompt": SYSTEM_PROMPT, "tool_ids": tool_ids}
        if os.environ.get("ELEVENLABS_LLM"):
            prompt["llm"] = os.environ["ELEVENLABS_LLM"]

        body = {
            "name": "GP queue booking assistant (text)",
            "conversation_config": {
                # Empty first message: the agent waits for the receptionist. Our app
                # speaks the fixed opener itself so taking over costs no time.
                "agent": {"first_message": "", "language": "en", "prompt": prompt},
                "conversation": {"text_only": True, "max_duration_seconds": 900},
                # Receptionists pause while typing: don't re-prompt them.
                "turn": {"turn_timeout": 60},
            },
        }
        r = http.post(f"{API}/agents/create", json=body)
        if r.status_code >= 400:
            sys.exit(f"Creating agent failed: {r.status_code} {r.text}")
        print(f"\nAdd to .env:\nELEVENLABS_AGENT_ID={r.json()['agent_id']}")


if __name__ == "__main__":
    main()

"""Create the ElevenLabs tools + voice agent used for the receptionist conversation.

    .venv/bin/python scripts/setup_agent.py

Prints an ELEVENLABS_AGENT_ID to put in .env. Re-running creates a new agent;
after that, tweak the prompt/voice in the ElevenLabs dashboard instead.

Env: ELEVENLABS_API_KEY (required), ELEVENLABS_VOICE_ID (optional: pick a
British English voice from the voice library), ELEVENLABS_LLM (optional).
"""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()
API = "https://api.elevenlabs.io/v1/convai"
KEY = os.environ.get("ELEVENLABS_API_KEY", "")

SYSTEM_PROMPT = """\
You are a polite, efficient automated assistant phoning {{surgery_name}}, a UK NHS GP surgery, \
on behalf of a patient who has asked you to book them an appointment. A receptionist has just \
answered after the patient's call waited in the queue.

Be honest: you are an AI assistant calling for the patient, not the patient. If asked, say \
the patient gave you their details and consent so they didn't have to wait in the queue.

Patient details (only share what the receptionist asks for, and spell names, postcodes or \
numbers clearly when asked):
- Name: {{patient_name}}
- Date of birth: {{date_of_birth}}
- Address: {{address}}
- Phone: {{patient_phone}}
- NHS number: {{nhs_number}}
- Reason for appointment: {{reason}}
- When they can attend: {{availability}}
- Preferred clinician: {{preferred_clinician}}
- Patient happy to be phoned and connected now: {{patient_callback_allowed}}

How to handle the call:
1. Open briefly: say you're an automated assistant calling for {{patient_name}} to book a GP \
appointment, and ask if they can help.
2. Answer identity questions from the details above. Never invent details you don't have; say \
"I don't have that, but I can connect you to the patient" instead.
3. When offered a slot, accept it only if it fits the patient's availability. Otherwise ask \
for another. Phone, video and in-person appointments are all fine unless the availability says otherwise.
4. Once agreed, read the date, time, clinician and place back to confirm, then call \
record_booking.
5. If the receptionist needs to speak to the patient (clinical triage, consent checks, \
anything you can't answer) and callback is allowed, call connect_patient. If not allowed, \
ask whether the surgery can ring the patient back on {{patient_phone}}.
6. If no appointment is possible (fully booked, use the online form, call back tomorrow), call \
report_outcome with a short summary of what they said.
7. Thank them and call end_call.

Keep every reply short and natural; receptionists are busy. Use British English. Do not give \
medical advice. If the receptionist suggests anything urgent (999, 111, A&E), record it with \
report_outcome so the patient is told."""

FIRST_MESSAGE = (
    "Hello, I'm an automated assistant calling on behalf of {{patient_name}}. "
    "I'd like to book them a GP appointment, please. Are you able to help?"
)

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
    {"type": "system", "name": "end_call", "description": "", "params": {"system_tool_type": "end_call"}},
]


def main() -> None:
    if not KEY:
        sys.exit("Set ELEVENLABS_API_KEY in .env first")
    headers = {"xi-api-key": KEY}
    with httpx.Client(timeout=30, headers=headers) as http:
        tool_ids = []
        for tool in TOOLS:
            r = http.post(f"{API}/tools", json={"tool_config": tool})
            if r.status_code >= 400:
                sys.exit(f"Creating tool {tool['name']} failed: {r.status_code} {r.text}")
            tool_ids.append(r.json()["id"])
            print(f"tool {tool['name']}: {tool_ids[-1]}")

        prompt = {"prompt": SYSTEM_PROMPT, "tool_ids": tool_ids}
        if os.environ.get("ELEVENLABS_LLM"):
            prompt["llm"] = os.environ["ELEVENLABS_LLM"]
        tts = {"agent_output_audio_format": "ulaw_8000"}
        if os.environ.get("ELEVENLABS_VOICE_ID"):
            tts["voice_id"] = os.environ["ELEVENLABS_VOICE_ID"]

        body = {
            "name": "GP queue booking assistant",
            "conversation_config": {
                "agent": {"first_message": FIRST_MESSAGE, "language": "en", "prompt": prompt},
                "tts": tts,
                # Twilio media streams are 8kHz mu-law, so no resampling is needed.
                "asr": {"user_input_audio_format": "ulaw_8000"},
                # Receptionists pause while typing: be patient before re-prompting.
                "turn": {"turn_timeout": 15, "turn_eagerness": "patient"},
            },
        }
        r = http.post(f"{API}/agents/create", json=body)
        if r.status_code >= 400:
            sys.exit(f"Creating agent failed: {r.status_code} {r.text}")
        print(f"\nAdd to .env:\nELEVENLABS_AGENT_ID={r.json()['agent_id']}")


if __name__ == "__main__":
    main()

import os

os.environ.setdefault("PUBLIC_URL", "https://test.example")
os.environ["ELEVENLABS_API_KEY"] = ""  # Polly <Say>, so TwiML can be built without an event loop
os.environ["ELEVENLABS_AGENT_ID"] = "agent_test"
os.environ["MAX_HOPS"] = "10"

import pytest
from fastapi.testclient import TestClient

from app import agent, call_flow, main, telephony
from app.classifier import Heard, HoldClassifier, _menu_digit_for_appointments
from app.models import CallSession, CallStatus, PatientRequest, normalise_uk_phone

PATIENT = dict(
    full_name="Alex Smith", date_of_birth="14 March 1990", address="1 High St, Durham DH1 1AA",
    patient_phone="07700 900123", nhs_number="485 777 3456", reason="Persistent cough",
    availability="Weekday mornings", surgery_name="Riverside Surgery", surgery_phone="0191 496 0000",
    consent=True,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No real Twilio or SMS calls in tests."""
    sent = []

    async def fake_sms(to, body):
        sent.append((to, body))

    async def fake_call(session_id, to):
        return "CA123"

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(telephony, "send_sms", fake_sms)
    monkeypatch.setattr(telephony, "place_call", fake_call)
    monkeypatch.setattr(telephony, "call_patient", noop)
    return sent


def scripted_agent(monkeypatch, replies):
    """Replace the ElevenLabs conversation with canned replies."""
    async def fake_turn(self, heard, note):
        return replies.pop(0)

    async def no_connection(self):
        return None

    monkeypatch.setattr(agent.ReceptionistAgent, "_turn", fake_turn)
    monkeypatch.setattr(agent.ReceptionistAgent, "_run", no_connection)


def test_phone_normalisation():
    assert normalise_uk_phone("07700 900123") == "+447700900123"
    assert normalise_uk_phone("0044 191 496 0000") == "+441914960000"
    with pytest.raises(ValueError):
        normalise_uk_phone("hello")


def test_menu_digit():
    text = "For prescriptions press 1. For appointments press 2. For test results press 3."
    assert _menu_digit_for_appointments(text) == "2"


@pytest.mark.asyncio
async def test_classifier_rules():
    c = HoldClassifier("Riverside Surgery", "cough")
    assert (await c.classify("You are number 5 in the queue.")).heard == Heard.HOLD
    v = await c.classify("Welcome. For appointments, press 1. For prescriptions, press 2.")
    assert v.heard == Heard.IVR_MENU and v.digits == "1"
    assert (await c.classify("Good morning, Riverside Surgery, Sam speaking, how can I help?")).heard == Heard.HUMAN
    # The same recorded line heard twice is a recording, not a person.
    assert (await c.classify("Good morning, Riverside Surgery, Sam speaking, how can I help?")).heard == Heard.HOLD
    assert (await c.classify("The surgery is now closed.")).heard == Heard.CLOSED


def test_twiml_uses_only_trial_safe_verbs():
    hold = telephony.twiml_hold("abc", digits="1")
    assert hold.count('<Gather') == telephony.HOLD_GATHERS
    assert 'action="https://test.example/twiml/heard/abc"' in hold and 'input="speech"' in hold
    assert 'digits="w1"' in hold
    reply = telephony.twiml_reply("abc", "Hello there")
    assert "<Say" in reply and "Hello there" in reply and reply.endswith("<Hangup /></Response>")
    for xml in (hold, reply, telephony.twiml_conference("abc", starts_conference=True, intro="Hi")):
        assert "<Stream" not in xml and "<Start" not in xml and "<Connect" not in xml


def test_intake_and_emergency():
    client = TestClient(main.app)
    r = client.post("/api/calls", json=PATIENT)
    assert r.status_code == 200
    status = client.get(f"/api/calls/{r.json()['id']}").json()
    assert status["status"] == "dialling"
    assert "Alex" not in str(status)  # no personal data on the status API

    r = client.post("/api/calls", json={**PATIENT, "reason": "crushing chest pain"})
    assert r.status_code == 422 and "999" in r.json()["detail"]

    r = client.post("/api/calls", json={**PATIENT, "consent": False})
    assert r.status_code == 422


def test_full_call_books_appointment(monkeypatch, offline):
    scripted_agent(monkeypatch, [
        agent.Reply(say="Yes, that's 14 March 1990."),
        agent.Reply(
            say="Tuesday 18 November at 10:40 with Dr Patel. Thank you, goodbye.",
            next="hang_up",
            booking={"date": "Tuesday 18 November", "time": "10:40am", "clinician": "Dr Patel",
                     "location": "Riverside", "notes": ""},
        ),
    ])
    client = TestClient(main.app)
    sid = client.post("/api/calls", json=PATIENT).json()["id"]

    assert "<Gather" in client.post(f"/twiml/start/{sid}").text
    heard = lambda text: client.post(f"/twiml/heard/{sid}", data={"SpeechResult": text}).text

    assert 'digits="w1"' in heard("For appointments press 1. For prescriptions press 2.")
    assert "<Gather" in heard("You are number 3 in the queue.")
    opener = heard("Good morning, Riverside Surgery, Sam speaking, how can I help?")
    assert "automated assistant" in opener and "Alex Smith" in opener
    assert "14 March 1990" in heard("Can you confirm the date of birth?")
    bye = heard("I can do Tuesday at 10:40 with Dr Patel.")
    assert "goodbye" in bye and bye.endswith("<Hangup /></Response>")

    status = client.get(f"/api/calls/{sid}").json()
    assert status["status"] == "booked" and status["booking"]["time"] == "10:40am"
    assert status["turns_left"] == 10 - 1 - 5
    assert any("10:40am" in body for _, body in offline)


@pytest.mark.asyncio
async def test_last_turn_forces_wrap_up(monkeypatch):
    scripted_agent(monkeypatch, [agent.Reply(say="Could you check Friday too?")])
    session = CallSession(patient=PatientRequest(**PATIENT))
    session.phase, session.agent, session.hops = "agent", agent.ReceptionistAgent(session.patient), 8

    xml = await call_flow.heard(session, "We're fully booked this week.")
    assert session.turns_left() == 0 and session.phase == "done"
    assert xml.endswith("<Hangup /></Response>") and "<Gather" not in xml


@pytest.mark.asyncio
async def test_connect_patient(monkeypatch):
    scripted_agent(monkeypatch, [agent.Reply(say="Connecting you to the patient now.", next="connect_patient")])
    session = CallSession(patient=PatientRequest(**PATIENT))
    session.phase, session.agent = "agent", agent.ReceptionistAgent(session.patient)

    xml = await call_flow.heard(session, "I need to speak to the patient.")
    assert "<Conference" in xml and session.status == CallStatus.HANDED_TO_PATIENT


class FakeElevenLabs:
    """Plays back scripted server events after each user_message, like the live test showed."""

    def __init__(self, agent_obj, script):
        self.agent, self.script, self.sent = agent_obj, script, []

    async def send(self, raw):
        import json

        msg = json.loads(raw)
        self.sent.append(msg)
        if msg["type"] == "user_message":
            for event in self.script:
                self.agent._events.put_nowait(event)


def say(text):
    return {"type": "agent_response", "agent_response_event": {"agent_response": text}}


def tool(name, **params):
    return {"type": "client_tool_call", "client_tool_call": {"tool_name": name, "tool_call_id": f"t-{name}", "parameters": params}}


def connected_agent(script):
    a = agent.ReceptionistAgent(PatientRequest(**PATIENT))
    a._ws = FakeElevenLabs(a, script)
    a._ready.set()
    return a


@pytest.mark.asyncio
async def test_turn_with_booking_keeps_follow_up_line():
    a = connected_agent([
        say("Just to read it back: Tuesday at 10:40am with Dr Patel."),
        tool("record_booking", date="Tuesday 18 November", time="10:40am", clinician="Dr Patel"),
        say("Is there anything else you need?"),
    ])
    r = await a.reply("Tuesday 10:40 with Dr Patel?", turns_left=4)
    assert r.booking["time"] == "10:40am" and r.next == "listen" and r.error is None
    assert r.say == "Just to read it back: Tuesday at 10:40am with Dr Patel. Is there anything else you need?"
    sent = [m["type"] for m in a._ws.sent]
    assert sent == ["contextual_update", "user_message", "client_tool_result"]


@pytest.mark.asyncio
async def test_turn_drops_narration_after_hang_up():
    a = connected_agent([say("Thank you, goodbye."), tool("hang_up", reason="done"), say("The call has ended.")])
    r = await a.reply("That's everything.", turns_left=2)
    assert r.next == "hang_up" and r.say == "Thank you, goodbye."


@pytest.mark.asyncio
async def test_turn_without_connection_falls_back():
    a = agent.ReceptionistAgent(PatientRequest(**PATIENT))
    a._error = "the ElevenLabs conversation closed"
    a._ready.set()
    r = await a.reply("Hello?", turns_left=3)
    assert r.say == agent.FALLBACK_SAY and "closed" in r.error

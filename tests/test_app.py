import os

os.environ.setdefault("PUBLIC_URL", "https://test.example")
os.environ["ANTHROPIC_API_KEY"] = ""  # force keyword rules in tests

import pytest
from fastapi.testclient import TestClient

from app import agent_bridge, main, telephony
from app.classifier import Heard, HoldClassifier, _menu_digit_for_appointments
from app.models import CallSession, CallStatus, PatientRequest, normalise_uk_phone

PATIENT = dict(
    full_name="Alex Smith", date_of_birth="14 March 1990", address="1 High St, Durham DH1 1AA",
    patient_phone="07700 900123", nhs_number="485 777 3456", reason="Persistent cough",
    availability="Weekday mornings", surgery_name="Riverside Surgery", surgery_phone="0191 496 0000",
    consent=True,
)


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
    c._client = None
    assert (await c.classify("You are number 5 in the queue.")).heard == Heard.HOLD
    v = await c.classify("Welcome. For appointments, press 1. For prescriptions, press 2.")
    assert v.heard == Heard.IVR_MENU and v.digits == "1"
    assert (await c.classify("Good morning, Riverside Surgery, Sam speaking, how can I help?")).heard == Heard.HUMAN
    # The same recorded line heard twice is a recording, not a person.
    assert (await c.classify("Good morning, Riverside Surgery, Sam speaking, how can I help?")).heard == Heard.HOLD
    assert (await c.classify("The surgery is now closed.")).heard == Heard.CLOSED


def test_twiml():
    hold = telephony.twiml_start_hold("abc")
    assert 'url="wss://test.example/ws/hold/abc"' in hold and "<Pause" in hold
    assert 'digits="w1"' in telephony.twiml_press_digits("abc", "1")
    agent = telephony.twiml_connect_agent("abc")
    assert "<Stop>" in agent and "wss://test.example/ws/agent/abc" in agent


def test_intake_and_emergency(monkeypatch):
    async def fake_call(session_id, to):
        return "CA123"

    monkeypatch.setattr(telephony, "place_call", fake_call)
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


@pytest.mark.asyncio
async def test_record_booking_tool(monkeypatch):
    sent = []

    async def fake_sms(to, body):
        sent.append((to, body))

    monkeypatch.setattr(telephony, "send_sms", fake_sms)
    session = CallSession(patient=PatientRequest(**PATIENT))
    result, err = await agent_bridge.run_tool(
        session, "record_booking", {"date": "Tue 18 Nov", "time": "10:40am", "clinician": "Dr Patel"}
    )
    assert not err and session.status == CallStatus.BOOKED
    assert sent and "10:40am" in sent[0][1] and sent[0][0] == "+447700900123"

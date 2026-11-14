import hashlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app import delegation, speech, telephony
from app import record as record_mod
from app.call import CallBrain, CallRefused
from app.record import Preferences, Record

PUBLIC = "https://abc.trycloudflare.com"
TOKEN = "test-auth-token"


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(record_mod, "DB_PATH", tmp_path / "test.db")
    record_mod.init_db()
    monkeypatch.setattr(telephony, "CONFIG", telephony.PhoneConfig(
        account_sid="AC" + "0" * 30 + "1234", auth_token=TOKEN, from_number="+441234567890",
        public_url=PUBLIC, tunnel="manual"))
    monkeypatch.setattr(telephony, "CALLS", {})
    monkeypatch.setattr(speech, "TTS_DIR", tmp_path / "tts")

    def fake_tts(text):
        (tmp_path / "tts").mkdir(exist_ok=True)
        p = tmp_path / "tts" / f"{hashlib.sha256(text.encode()).hexdigest()}.wav"
        p.write_bytes(b"RIFF" + b"\0" * 60)
        return p

    monkeypatch.setattr(speech, "synthesise_to_file", fake_tts)
    monkeypatch.setattr(speech, "warm_phrases", lambda phrases: None)
    monkeypatch.setattr(speech, "local_tts_available", lambda: True)


def make_record(confirmed=True) -> Record:
    r = Record(
        reason_for_contact="knee problem",
        preferences=Preferences(urgency="this week", appointment_type="face to face", times=["weekday mornings"]),
        access_needs=["speak slowly and clearly"],
    )
    r.clinical_detail.description = "my right knee hurts going up stairs"
    r.clinical_detail.taking_anything = "paracetamol"
    r.spoken_summary = "A registered patient would like an appointment, this week, ideally weekday mornings. It is about a knee problem."
    r.confirmed_by_patient = confirmed
    return r


def signed_post(client, path, data):
    sig = RequestValidator(TOKEN).compute_signature(PUBLIC + path, data)
    return client.post(path, data=data, headers={"host": "abc.trycloudflare.com", "x-twilio-signature": sig})


def test_gather_plays_local_audio_and_never_uses_say():
    xml = telephony.twiml_gather("abc", "Hello there.")
    assert "<Gather" in xml and 'input="speech"' in xml and 'language="en-GB"' in xml
    assert 'actionOnEmptyResult="true"' in xml and 'speechModel="phone_call"' in xml
    assert f"{PUBLIC}/twilio/heard/abc" in xml
    assert f"<Play>{PUBLIC}/twilio/audio/" in xml
    assert "<Say" not in xml
    bye = telephony.twiml_goodbye("Goodbye.")
    assert "<Play>" in bye and "<Hangup" in bye and "<Say" not in bye


def test_tunnel_only_reaches_twilio_routes():
    from app.main import app

    c = TestClient(app)
    public = {"host": "abc.trycloudflare.com"}
    assert c.get("/", headers=public).status_code == 404
    assert c.get("/api/status", headers=public).status_code == 404
    assert c.get("/api/phone/config", headers=public).status_code == 404
    assert c.get("/api/boundary", headers={"x-forwarded-for": "1.2.3.4"}).status_code == 404
    assert c.get("/").status_code == 200
    assert c.get("/api/phone/config").status_code == 200


def test_unsigned_or_forged_webhooks_are_rejected():
    from app.main import app

    c = TestClient(app)
    headers = {"host": "abc.trycloudflare.com"}
    assert c.post("/twilio/answer/x", data={"CallSid": "CA1"}, headers=headers).status_code == 403
    bad = {**headers, "x-twilio-signature": "forged"}
    assert c.post("/twilio/heard/x", data={"SpeechResult": "hi"}, headers=bad).status_code == 403


def test_audio_route_serves_only_hashed_files(tmp_path):
    from app.main import app

    c = TestClient(app)
    p = speech.synthesise_to_file("It is about a knee problem.")
    ok = c.get(f"/twilio/audio/{p.name}", headers={"host": "abc.trycloudflare.com"})
    assert ok.status_code == 200 and ok.headers["content-type"] == "audio/wav"
    assert c.get("/twilio/audio/..%2Fsecret.wav").status_code == 404
    assert c.get("/twilio/audio/notahash.wav").status_code == 404


def test_config_status_never_returns_secrets():
    from app.main import app

    c = TestClient(app)
    body = c.post("/api/phone/config", json={"from_number": "+447700900000"}).json()
    text = str(body)
    assert TOKEN not in text and telephony.CONFIG.account_sid not in text
    assert body["account_sid_hint"].endswith("1234")
    assert body["from_number"] == "+447700900000"
    assert body["ready"] is True


def test_place_call_validates_and_uses_twilio(monkeypatch):
    created = {}

    class FakeCalls:
        def create(self, **kw):
            created.update(kw)
            return SimpleNamespace(sid="CA123")

    monkeypatch.setattr(telephony, "_client", lambda: SimpleNamespace(calls=FakeCalls()))
    cred = delegation.issue_credential()

    with pytest.raises(CallRefused):
        telephony.place_call("s1", make_record(confirmed=False), cred, "+447700900123")
    with pytest.raises(CallRefused):
        telephony.place_call("s1", make_record(), cred, "07700 900123")

    live = telephony.place_call("s1", make_record(), cred, "+44 7700 900123")
    assert created["to"] == "+447700900123" and created["timeout"] == 20
    assert created["url"] == f"{PUBLIC}/twilio/answer/{live.call_id}"
    assert created["status_callback"].endswith(f"/twilio/status/{live.call_id}")
    assert live.twilio_sid == "CA123" and telephony.CALLS[live.call_id] is live

    delegation.revoke_credential(cred["credential_id"])
    with pytest.raises(CallRefused):
        telephony.place_call("s1", make_record(), cred, "+447700900123")


def test_full_live_conversation_answers_safely():
    from app import main

    c = TestClient(main.app)
    rec = make_record()
    sid = "sess-live"
    main.SESSIONS[sid] = main.Session(interview=SimpleNamespace(transcript=[], answers={}, crisis=None), record=rec)
    live = telephony.LiveCall(call_id="call1", session_id=sid, request_id=rec.request_id,
                              to_number="+447700900123", brain=CallBrain(rec, max_turns=8))
    telephony.CALLS["call1"] = live

    r = signed_post(c, "/twilio/answer/call1", {"CallSid": "CA1", "CallStatus": "in-progress"})
    assert r.status_code == 200 and "<Gather" in r.text and "<Say" not in r.text
    assert live.opening_latency is not None and live.opening_latency < 2

    def say(text):
        resp = signed_post(c, "/twilio/heard/call1", {"SpeechResult": text})
        assert resp.status_code == 200 and "<Say" not in resp.text
        return resp, live.events[-1]["text"]

    _, reply = say("Okay, what's it about?")
    assert "knee problem" in reply
    _, reply = say("Can you give me her date of birth?")
    assert "confirm her identity" in reply.lower()
    _, reply = say("Does it sound like she needs to be seen today?")
    assert "clinical" in reply.lower()
    _, reply = say("What's her email address?")
    assert "have that" in reply.lower()
    n_unanswered = len(live.brain.unanswered)
    assert n_unanswered >= 3
    resp = signed_post(c, "/twilio/heard/call1", {"SpeechResult": ""})
    assert "<Gather" in resp.text
    _, reply = say("I've got Thursday at 9:20 with Dr Shah, would that work?")
    assert "Thursday" in reply
    resp, reply = say("Yes, that's booked.")
    assert "<Hangup" in resp.text

    s = c.get(f"/api/session/{sid}/live-call")
    assert s.status_code == 404  # session wasn't started through the API
    main.SESSIONS[sid].live_call_id = "call1"
    summary = c.get(f"/api/session/{sid}/live-call").json()
    assert summary["agreed"].startswith("Thursday") and summary["outcome"] == "booked"
    assert len(summary["unanswered"]) >= 3

    signed_post(c, "/twilio/status/call1", {"CallStatus": "completed"})
    assert live.ended and live.finished

    spoken = " ".join(main.SESSIONS[sid].live_spoken).lower()
    for secret in ("stairs", "paracetamol", "speak slowly", "right knee hurts"):
        assert secret not in spoken
    boundary = c.get("/api/boundary", params={"sid": sid}).json()
    live_items = [i for i in boundary["leaves"] if i["label"] == "Agent line on live call"]
    assert live_items and all(i["dest"] == "Phone line, via Twilio" for i in live_items)
    del main.SESSIONS[sid]

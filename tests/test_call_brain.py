import pytest

from app import delegation
from app import record as record_mod
from app.call import CallBrain, CallRefused, classify, find_time, parse_offer, preflight
from app.interview import InterviewSession
from app.record import get_audit

ANSWERS = {
    "reason": "My right knee hurts, worse going up stairs",
    "duration": "4 days",
    "trajectory": "worse",
    "seen_before": "no",
    "taking_anything": "paracetamol",
    "urgency": "this week",
    "appointment_type": "face to face",
    "times": "weekday mornings",
    "preferred_clinician": "no",
    "access_needs": "hard of hearing, speak slowly and clearly",
    "contact_number": "yes",
}
CLINICAL_SECRETS = ("stairs", "right knee hurts", "paracetamol", "4 days", "hard of hearing")


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(record_mod, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("app.interview._ollama_up", False)
    monkeypatch.setattr("app.interview._ollama_model", None)
    record_mod.init_db()


@pytest.fixture
def rec():
    s = InterviewSession(session_id="brain-test")
    r = s.start()
    while r["type"] == "question":
        r = s.submit_answer(ANSWERS[r["key"]])
    record = s.build_record()
    record.confirmed_by_patient = True
    return record


def no_secrets(brain):
    spoken = " ".join(brain.spoken_out).lower()
    for secret in CLINICAL_SECRETS:
        assert secret not in spoken, secret


@pytest.mark.parametrize("said,intent", [
    ("who is this", "who"),
    ("whos calling please", "who"),
    ("are you a robot", "who"),
    ("is this ai", "who"),
    ("can i speak to her", "speak_to_patient"),
    ("could you put her on", "speak_to_patient"),
    ("how can i help", "reason"),
    ("what can i do for you", "reason"),
    ("whats it regarding", "reason"),
    ("can i take her date of birth", "identity"),
    ("does it sound like she needs to be seen today", "clinical"),
    ("we dont speak to robots sorry she'll have to call herself", "decline"),
    ("i've got thursday at nine twenty with doctor shah", "offer"),
    ("whats her email address for the confirmation", "unknown_question"),
    ("we are really busy this morning", "unknown_question"),
    ("er hang on", "unparsed"),
])
def test_classify_lowercase_unpunctuated(said, intent):
    assert classify(said) == intent


@pytest.mark.parametrize("said,time", [
    ("nine twenty", "9:20"),
    ("half nine", "9:30"),
    ("half past ten", "10:30"),
    ("ten thirty", "10:30"),
    ("two pm", "2pm"),
    ("quarter to ten", "9:45"),
    ("at 9:20", "9:20"),
    ("at nine on friday", "9:00"),
    ("i've got 10.40am", "10:40am"),
])
def test_spoken_times(said, time):
    assert find_time(said) == time


def test_parse_offer_lowercase():
    assert parse_offer("i've got thursday at nine twenty with doctor shah") == "Thursday at 9:20 with Dr Shah"


def test_new_intent_replies(rec):
    b = CallBrain(rec)
    b.opening("hello riverside surgery")
    reply, end = b.respond("who is this")
    assert "automated assistant" in reply and not end
    reply, end = b.respond("can i speak to her")
    assert "isn't on this call" in reply and "email" in reply and not end
    reply, end = b.respond("how can i help")
    assert reply == rec.spoken_summary and not end
    no_secrets(b)


def test_offer_then_plain_yes_books(rec):
    b = CallBrain(rec)
    b.opening()
    reply, end = b.respond("i can do friday at half nine")
    assert "Friday at 9:30" in reply and not end
    reply, end = b.respond("yeah")
    assert end and b.outcome == "booked" and b.agreed == "Friday at 9:30"
    b.finish()
    b.finish()
    ended = [e for e in get_audit(rec.request_id) if e["said"].startswith("Call ended")]
    assert len(ended) == 1 and ended[0]["agreed"] == "Friday at 9:30"


def test_offer_then_no_asks_for_another(rec):
    b = CallBrain(rec)
    b.respond("monday at 2pm")
    reply, end = b.respond("no")
    assert "What else do you have" in reply and "Weekday mornings" in reply and not end
    reply, end = b.respond("tuesday at ten thirty then")
    assert "Tuesday at 10:30" in reply
    _, end = b.respond("that's right")
    assert end and b.agreed == "Tuesday at 10:30"


def test_close_when_nothing_pending(rec):
    b = CallBrain(rec)
    reply, end = b.respond("ok thanks bye")
    assert end and b.outcome == "ended_without_booking" and "Goodbye" in reply


def test_turn_limit(rec):
    b = CallBrain(rec, max_turns=3)
    b.respond("who is this")
    b.respond("how can i help")
    reply, end = b.respond("whats her email address")
    assert end and b.outcome == "turn_limit" and reply.startswith("I'll let her know")


def test_turn_limit_does_not_override_booking(rec):
    b = CallBrain(rec, max_turns=2)
    b.respond("wednesday at 10:40")
    _, end = b.respond("yes")
    assert end and b.outcome == "booked"


def test_silence_twice_ends(rec):
    b = CallBrain(rec)
    reply, end = b.silence()
    assert not end and "still there" in reply
    reply, end = b.respond("")
    assert end and b.outcome == "ended_without_booking"


def test_silence_counter_resets_after_speech(rec):
    b = CallBrain(rec)
    b.silence()
    b.respond("who is this")
    _, end = b.silence()
    assert not end


def test_safe_fallbacks_suggest_next_step(rec):
    b = CallBrain(rec)
    for line in ("can i take her date of birth", "does it sound like she needs to be seen today",
                 "whats her email address for the confirmation"):
        reply, end = b.respond(line)
        assert not end
        assert any(w in reply for w in ("call", "email", "comes in", "ring"))
    assert len(b.unanswered) == 3
    no_secrets(b)


def test_decline_ends(rec):
    b = CallBrain(rec)
    reply, end = b.respond("sorry we don't deal with automated callers")
    assert end and b.outcome == "declined" and "call you directly" in reply


def test_unparsed_streak_offers_callback(rec):
    b = CallBrain(rec)
    for _ in range(2):
        _, end = b.respond("er hang on")
        assert not end
    reply, end = b.respond("um")
    assert end and b.outcome == "unparsed_offer_callback" and "email" in reply


def test_no_clinical_detail_across_long_conversation(rec):
    b = CallBrain(rec, max_turns=30)
    b.opening()
    for line in ("how can i help", "whats it about", "how soon does she want to be seen",
                 "would she like to come in or a phone call", "when suits her", "which doctor",
                 "what's the problem exactly", "is she taking anything", "how long has it been going on",
                 "is she hard of hearing", "anything we should know for the day"):
        b.respond(line)
    no_secrets(b)


def test_preflight_rules(rec):
    rec.confirmed_by_patient = False
    with pytest.raises(CallRefused):
        preflight(rec, delegation.issue_credential())
    rec.confirmed_by_patient = True
    cred = delegation.issue_credential()
    preflight(rec, cred)
    delegation.revoke_credential(cred["credential_id"])
    with pytest.raises(CallRefused):
        preflight(rec, cred)

import pytest
from fastapi.testclient import TestClient

from app import record as record_mod
from app.call import CallRefused, classify, run_call
from app.interview import InterviewSession
from app.safety import check_distress, check_red_flags
from app.speech import speakable_text


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(record_mod, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("app.interview._ollama_up", False)
    record_mod.init_db()


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


def finished_session() -> InterviewSession:
    s = InterviewSession(session_id="test-session")
    result = s.start()
    while result["type"] == "question":
        result = s.submit_answer(ANSWERS[result["key"]])
    assert result["type"] == "review"
    return s


def test_red_flags_detected():
    assert check_red_flags("I've got chest pain and my arm feels numb")
    assert check_red_flags("my baby is floppy and won't wake")
    assert not check_red_flags("pain in my knee going up stairs")


def test_distress_detected():
    assert check_distress("I can't go on like this")
    assert not check_distress("I can't go up the stairs")


def test_interview_builds_record_and_skips_answered():
    s = InterviewSession(session_id="x")
    s.start()
    r = s.submit_answer("my knee has been getting worse for 4 days")
    assert r["key"] == "seen_before"  # duration and trajectory inferred
    s2 = finished_session()
    rec = s2.build_record()
    assert rec.reason_for_contact == "knee problem"
    assert "knee" not in rec.spoken_summary.lower().replace("knee problem", "")
    assert "stairs" not in rec.spoken_summary
    assert rec.clinical_detail.taking_anything == "paracetamol"
    assert rec.access_needs == ["hard of hearing", "speak slowly and clearly"]


def test_tapped_options_are_normalised():
    s = InterviewSession(session_id="x")
    s.start()
    s.submit_answer("my shoulder")
    for text in ("A few days", "Getting worse", "No, this is new", "No", "This week",
                 "Phone call", "Weekday mornings, After 5pm", "No preference", "Nothing needed"):
        r = s.submit_answer(text)
    assert r["key"] == "contact_number" and "07700" in r["text"]
    rec = s.build_record()
    assert rec.clinical_detail.trajectory == "worse"
    assert rec.clinical_detail.taking_anything == ""
    assert rec.preferences.times == ["weekday mornings", "after 5pm"]
    assert rec.preferences.preferred_clinician is None
    assert rec.access_needs == []
    assert rec.spoken_summary == (
        "A registered patient would like an appointment, this week, ideally weekday mornings "
        "or after 5pm. It is about a shoulder problem."
    )


def test_opinion_deflected_but_answer_kept():
    s = InterviewSession(session_id="x")
    s.start()
    r = s.submit_answer("I have a rash on my arm and is it serious?")
    assert r["ack"].startswith("That's one for the doctor")
    assert r["key"] == "duration"
    assert s.reason_phrase == "skin problem"
    r = s.submit_answer("is it serious?")
    assert r["key"] == "duration"


def test_back_undoes_answer_and_inferences():
    s = InterviewSession(session_id="x")
    s.start()
    r = s.submit_answer("my knee has been getting worse for 4 days")
    assert r["key"] == "seen_before" and "skipped" in r["ack"]
    r = s.go_back()
    assert r["key"] == "reason" and r["value"].startswith("my knee")
    assert "duration" not in s.answers and "trajectory" not in s.answers


def test_change_from_review_returns_to_review():
    s = finished_session()
    r = s.change("urgency")
    assert r["key"] == "urgency" and r["value"] == "this week"
    r = s.submit_answer("Today if possible")
    assert r["type"] == "review"
    assert s.build_record().preferences.urgency == "today if possible"


def test_spoken_ramble_fills_several_questions():
    s = InterviewSession(session_id="x")
    s.start()
    r = s.submit_answer(
        "Well it's my knee really, it's been getting worse for about two weeks, I've been taking "
        "paracetamol. I'm a bit hard of hearing. Mornings are best for me, and I'd like to be seen this week.",
        spoken=True,
    )
    assert r["heard"].startswith("Well it's my knee")
    assert r["key"] == "seen_before"
    for k in ("duration", "trajectory", "taking_anything", "urgency", "times", "access_needs"):
        assert k in s.answers, k
    assert s.answers["times"] == "weekday mornings"
    assert s.answers["access_needs"] == "speak slowly and clearly"


def test_symptom_timing_is_not_a_time_preference():
    s = InterviewSession(session_id="x")
    s.start()
    s.submit_answer("my back hurts most in the morning")
    assert "times" not in s.answers


@pytest.mark.parametrize("key,said,expected", [
    ("trajectory", "oh it's definitely got worse", "worse"),
    ("seen_before", "no never", "no"),
    ("seen_before", "yes a few months back", "yes, longer ago"),
    ("urgency", "as soon as you can really", "today if possible"),
    ("appointment_type", "I don't mind either way", "either face to face or a phone call"),
    ("appointment_type", "can they just ring me", "a phone call"),
    ("times", "mornings or after work", "weekday mornings, after 5pm"),
    ("preferred_clinician", "no I don't mind", "no"),
    ("access_needs", "I use a wheelchair", "step-free access"),
    ("contact_number", "yes that's right", "yes"),
    ("contact_number", "no it's 07700 900 456", "no it's 07700 900 456"),
])
def test_spoken_answers_map_to_options(key, said, expected):
    s = InterviewSession(session_id="x")
    s.start()
    for q in QUESTIONS_BEFORE(key):
        s.answers[q] = "x"
    r = s.submit_answer(said, spoken=True)
    assert s.answers[key] == expected
    assert r.get("heard") == said


def QUESTIONS_BEFORE(key):
    from app.interview import QUESTION_KEYS
    return QUESTION_KEYS[: QUESTION_KEYS.index(key)]


def test_distress_drops_content():
    s = InterviewSession(session_id="x")
    s.start()
    s.submit_answer("my back")
    r = s.submit_answer("honestly I want to end it all")
    assert r["type"] == "crisis" and r["kind"] == "distress"
    assert s.answers == {} and s.transcript == []


def test_speech_boundary_refuses_clinical_fields():
    rec = finished_session().build_record().to_dict()
    assert speakable_text(rec, "spoken_summary")
    for field in ("clinical_detail", "access_needs", "patient_ref"):
        with pytest.raises(ValueError):
            speakable_text(rec, field)


def test_call_refused_without_confirmation_or_with_revoked_credential():
    from app import delegation

    rec = finished_session().build_record()
    cred = delegation.issue_credential()
    with pytest.raises(CallRefused):
        run_call(rec, cred)
    rec.confirmed_by_patient = True
    delegation.revoke_credential(cred["credential_id"])
    with pytest.raises(CallRefused):
        run_call(rec, cred)


def test_full_call_exercises_fallbacks_and_never_leaks_clinical_detail():
    from app import delegation

    rec = finished_session().build_record()
    rec.confirmed_by_patient = True
    res = run_call(rec, delegation.issue_credential(), "full")
    assert res.outcome == "booked"
    assert "Thursday at 9:20" in res.agreed
    assert res.opening_latency <= 2
    spoken = " ".join(res.spoken_out).lower()
    assert "can't confirm her identity" in spoken
    assert "not clinical" in spoken
    assert "don't have that answer" in spoken
    for secret in ("stairs", "paracetamol", "4 days", "hard of hearing"):
        assert secret not in spoken
    assert len(res.unanswered) == 3


@pytest.mark.parametrize("scenario,outcome", [
    ("decline", "declined"),
    ("garbled", "unparsed_offer_callback"),
    ("voicemail", "retry_scheduled"),
])
def test_call_fallback_scenarios(scenario, outcome):
    from app import delegation

    rec = finished_session().build_record()
    rec.confirmed_by_patient = True
    assert run_call(rec, delegation.issue_credential(), scenario).outcome == outcome


def test_classify():
    assert classify("Can I take her date of birth?") == "identity"
    assert classify("What's her email address?") == "unknown_question"
    assert classify("Does it sound like she needs to be seen today?") == "clinical"


def test_api_end_to_end():
    from app.main import app

    c = TestClient(app)
    r = c.post("/api/session").json()
    sid = r["session_id"]
    while r["type"] == "question":
        r = c.post(f"/api/session/{sid}/answer", json={"text": ANSWERS[r["key"]]}).json()
    assert r["type"] == "review"
    assert c.post(f"/api/session/{sid}/call", json={"scenario": "simple"}).status_code == 409
    b = c.get("/api/boundary", params={"sid": sid, "mode": "local"}).json()
    assert b["fields_out"] == 0
    naive = c.get("/api/boundary", params={"sid": sid, "mode": "naive"}).json()
    assert naive["bytes_out"] > 0

    conf = c.post(f"/api/session/{sid}/confirm", json={"edits": {}}).json()
    assert conf["practice"]["status"] == "authorised"
    assert c.get(f"/api/session/{sid}/speak", params={"field": "clinical_detail"}).status_code == 403
    call = c.post(f"/api/session/{sid}/call", json={"scenario": "simple"}).json()
    assert call["outcome"] == "booked"
    b = c.get("/api/boundary", params={"sid": sid, "mode": "local"}).json()
    labels = {i["label"] for i in b["leaves"]}
    assert labels <= {"Spoken summary", "Number dialled", "Agent line on call"}
    assert c.get(f"/api/session/{sid}/audit").json()["entries"]

    c.post(f"/api/session/{sid}/revoke")
    assert c.post(f"/api/session/{sid}/call", json={"scenario": "simple"}).status_code == 409


def test_red_flag_stops_booking_via_api():
    from app.main import app

    c = TestClient(app)
    sid = c.post("/api/session").json()["session_id"]
    r = c.post(f"/api/session/{sid}/answer", json={"text": "crushing chest pain since this morning"}).json()
    assert r["type"] == "crisis" and r["kind"] == "red_flag"
    assert c.post(f"/api/session/{sid}/confirm", json={"edits": {}}).status_code == 409

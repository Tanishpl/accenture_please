# Data Protection Impact Assessment: GP Booking Assistant

**Pre-filled by the supplier, for review and adoption by the deploying GP practice.**
Version 0.1 (hackathon draft) · Prepared 2026-11-14 · Status: draft, not legal advice.

---

## 1. Summary

The GP Booking Assistant helps a registered patient book a routine GP appointment
by phone without having to do the 8am phone scramble herself. On her own device it
interviews her with a fixed set of receptionist questions, builds a structured
record, and asks her to confirm it. It then phones the practice, says that it is an
automated assistant acting for a registered patient, and books from her confirmed
answers.

The privacy design is **minimisation by architecture**. Speech recognition, language
processing, speech synthesis and storage all run on the patient's device. No health
data, and no patient audio, is sent to the supplier or to any AI vendor.

## 2. Controller and processor: which model this DPIA covers

There are two ways to deploy this tool, and they produce different legal positions.

| | Practice-adopted (this DPIA) | Patient self-installed |
|---|---|---|
| Who decides to use it | The GP practice offers it to its patients | The patient, independently |
| Controller | The GP practice | The supplier (for any processing it does) |
| Supplier role | Processor, under a Data Processing Deed | Controller |
| Article 6 basis | 6(1)(e) public task | 6(1)(a) consent |
| Article 9 condition | 9(2)(h) health or social care, with the safeguards of DPA 2018 Sch.1 Part 1 para 2 | 9(2)(a) explicit consent |

**We are building the practice-adopted model.** Reasons:

1. The agent's authority to act comes from the practice (a delegation credential issued
   through the National proxy service, see section 7). That is a practice decision.
2. Clinical safety for deployment sits with the deploying organisation under DCB0160,
   with the supplier's obligations under DCB0129. Those standards assume this shape.
3. Consent is a weak basis where the alternative to saying yes is the 8am queue. Public
   task plus 9(2)(h) is the basis the practice already relies on for appointments.
4. It places the product inside the existing NHS supplier route (DSPT, DTAC, Data
   Processing Deed), which an AI product has already been assured through.

Note: because processing is on-device, the supplier receives no personal data in
normal operation in either model. The supplier's processor duties therefore focus on
the software itself: security of the code, update integrity, and incident support.

## 3. What data is processed

| Data | Category | Where processed | Where stored | Leaves the device? |
|---|---|---|---|---|
| Patient voice audio | Biometric (special category where used to identify) | On device (faster-whisper) | Not stored; discarded after transcription | **No** |
| Interview transcript | Health data | On device | In memory for the session | **No** |
| Clinical detail (description, duration, trajectory, medication, history) | Health data | On device (rules, optional local Ollama model) | Local SQLite | **No** |
| Preferences (urgency, appointment type, times, clinician) | Personal data | On device | Local SQLite | Spoken on the call |
| Reason for contact (short, non-clinical, e.g. "knee problem") | Health data (minimal) | On device | Local SQLite | Spoken on the call |
| Access needs | Health data, potentially | On device | Local SQLite | **No** (not on the speakable allowlist) |
| Spoken summary | Derived, minimal | On device (deterministic template) | Local SQLite | Spoken on the call |
| Practice phone number | Not personal | On device | Config | To the telephony carrier |
| Audit log of each call | Personal data | On device | Local SQLite | **No** |
| Delegation credential | Personal data (reference only) | On device | Local SQLite | Presented to the practice (mocked) |
| Crisis disclosures (self-harm, suicidal thoughts) | Health data | Detected on device | **Never stored** | **No** |

Data the agent **never** holds or speaks: date of birth, address, NHS number, or any
other identity-verification fact. It cannot authenticate as the patient.

## 4. Data flow

```
 patient's device:  mic -> faster-whisper -> rules / local model -> record -> patient confirms
                                                                        |
                                              allowlisted fields only (spoken_summary,
                                              reason_for_contact, preferences)
                                                                        |
                                                                   Kokoro TTS
                                                                        |
 leaves device:                                          telephony carrier -> GP practice
```

The only third party in the path is the telephony carrier, which carries call audio as
it would for any phone call. NHS England has already rolled out cloud telephony to GP
practices, so this is an existing, assessed category of risk. We do not claim
end-to-end locality.

The speaking allowlist is enforced in code (`app/speech.py`, `speakable_text`). A
request to speak any other field raises an error. It is not a prompt instruction.

## 5. Necessity and proportionality

- **Minimisation.** Only three fields can be spoken. Clinical detail stays in the
  record for the patient and is never read out.
- **Accuracy.** A local model can mis-summarise. The patient must confirm or correct the
  record before any call is placed, and the spoken summary is a deterministic template,
  not free model output.
- **Storage limitation.** Audio is deleted after transcription. Crisis content is never
  written. Records and audit entries stay on the device under the patient's control.
- **Transparency.** The agent announces it is an AI within two seconds of pickup
  (Ofcom silent-call guidance) and offers to connect the patient.
- **Rights.** The patient can read the full audit log of what was said on her behalf,
  and can revoke the agent's authority in one tap.

## 6. Risks and mitigations

| Risk | Likelihood | Severity | Mitigation | Residual |
|---|---|---|---|---|
| Clinical detail disclosed on the call | Low | High | Code-level speaking allowlist; template summary; tests assert no clinical terms are spoken | Low |
| Patient audio reaching a vendor | Very low | High | STT on device; no cloud STT code path exists | Very low |
| Agent used to impersonate the patient | Low | High | Agent never holds or recites identity facts; refuses identity checks | Low |
| Mis-summarised record leads to wrong booking | Medium | Medium | Patient confirms record; agent never triages | Low to medium |
| Emergency booked as routine | Low | High | Red flag detection stops the flow, directs to 999/111, no call placed | Low |
| Crisis disclosure stored | Low | High | Detected and dropped; not logged; Samaritans 116 123 shown | Low |
| Lost or stolen device exposes record | Medium | Medium | Production: encryption at rest, device lock, remote wipe guidance. **Not done in demo** | Medium |
| Call pattern resembles an automated dialler | Low | Medium | 15s minimum ring, 3 attempts per day, spaced retries | Low |
| Agent coerced into clinical judgement | Medium | Medium | Fixed refusal for clinical questions; offers to connect the patient | Low |

## 7. Declared gaps

We state these ourselves.

1. **Carrier.** The phone carrier processes call audio. Unavoidable, and already part of
   the NHS stack.
2. **Encryption at rest.** Local storage is unencrypted in the demo. Production needs
   encryption at rest and a lost-device plan, because on-device makes the device the
   weak point.
3. **Human detection.** Telling a live human apart from a recorded queue message is
   unreliable. One test of a comparable consumer feature put it at roughly 85%.
4. **Delegation.** No verifiable standard for delegating authority to a software agent
   exists today. The credential is mocked and labelled as such. This is a policy ask:
   extend registered proxy authority, via the National proxy service, to software agents.
5. **Model error.** A small local model can mis-summarise. That is a clinical safety risk,
   not just a bug, and needs a DCB0129 hazard log and DCB0160 deployment assessment.
6. **Refusal.** Practices may refuse to deal with an AI caller. Handled as a fallback
   (the patient is told to call directly), not solved.
7. **Direction of travel.** If practices adopt AI on their side, this becomes two systems
   talking and the phone call drops out. That is where this goes, not what was built.

## 8. Assurance route

| Requirement | Status |
|---|---|
| DSPT (supplier) | To complete before deployment |
| DTAC | To complete; clinical safety, data protection, technical security, interoperability, usability |
| DCB0129 (supplier clinical safety case, hazard log) | Draft hazards listed in section 6 |
| DCB0160 (practice deployment) | Practice to complete |
| Data Processing Deed | Standard NHS template, supplier as processor |
| Ofcom persistent misuse policy | Pacing limits in `app/call.py` |

## 9. Sign-off

| Role | Name | Decision | Date |
|---|---|---|---|
| Practice Data Protection Officer | | | |
| Practice Caldicott Guardian | | | |
| Clinical Safety Officer | | | |
| Supplier | | | |

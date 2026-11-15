# Legal considerations

QueueSkip GP is a hackathon prototype. This note lists the legal and
regulatory issues a real deployment in the UK would need to address. It is a
starting point for discussion, not legal advice.

## What data we handle

For each call the app collects the patient's name, date of birth, address,
mobile number, NHS number (optional), the reason for the appointment and their
availability. The reason for the appointment is **health data**, which UK GDPR
treats as *special category data* with stricter rules. The call itself also
captures the receptionist's voice and words.

| Data | Where it goes today |
|---|---|
| Patient details, appointment reason | Our server (in memory), ElevenLabs (agent prompt), the surgery (spoken on the call) |
| Call audio and transcripts | Twilio (telephony and speech recognition), ElevenLabs (agent replies, text to speech) |
| Booking confirmation | Twilio (SMS to the patient) |

## Why we would move to local AI

We used ElevenLabs for convenience during the hackathon. It receives the
patient's details and the conversation, which makes it a *data processor* for
health data, likely outside the UK. That brings obligations we would rather
avoid:

- a data processing agreement with the provider (UK GDPR Art. 28);
- a lawful mechanism for transfers outside the UK, such as the UK–US data
  bridge or an International Data Transfer Agreement (UK GDPR Chapter V);
- checking how long the provider keeps audio and transcripts and whether it
  uses them to train models;
- explaining all of this to patients in our privacy notice.

Running the AI ourselves on UK-hosted infrastructure keeps the patient's data
under our control. A local setup would need three parts: speech to text (for
example Whisper), a language model for the agent's replies (for example a
Llama-family model via Ollama or llama.cpp), and text to speech (for example
Piper). Check each model's licence allows commercial use.

Local AI does not remove every processor: **the phone call still goes through
a telephony provider** (Twilio today), so call audio and SMS content still
pass through a third party. Twilio's built-in speech recognition should be
replaced by our local speech to text, and a UK-based SIP provider would avoid
international transfers for the call itself.

## Data protection (UK GDPR and Data Protection Act 2018)

- **Lawful basis:** we need an Article 6 basis (likely consent or contract)
  *and* an Article 9 condition for health data (likely explicit consent). The
  form's consent checkbox is a start; the wording should name what is shared,
  with whom, and why.
- **DPIA:** a Data Protection Impact Assessment is required, because we
  process health data with new technology (UK GDPR Art. 35).
- **Minimisation:** only ask for what surgeries need. The NHS number is
  already optional. The agent's opening line gives the patient's name and
  date of birth before anyone asks, so whoever answers hears them; consider
  confirming it's the right surgery first.
- **Retention:** sessions are held in memory and lost on restart. A real
  service needs a stated retention period and deletion after the call.
- **Rights and transparency:** a privacy notice, and a way for patients to
  access or delete their data.
- **Registration:** the operator must pay the ICO data protection fee.
- **Security:** HTTPS everywhere, verify Twilio webhook signatures (a TODO in
  `app/main.py`), and protect API keys.

## NHS requirements

- **DSPT:** organisations handling NHS patient data are expected to complete
  the Data Security and Protection Toolkit.
- **DTAC:** NHS organisations assess digital health tools against the Digital
  Technology Assessment Criteria before adopting them.
- **Clinical safety:** if the service is judged to affect patient care, the
  clinical risk standards DCB0129 (manufacturer) and DCB0160 (deployer) apply.
- **Medical device rules:** the app must not triage or give medical advice, or
  it may count as software as a medical device under MHRA rules. Today it only
  books appointments and redirects possible emergencies to 999 or 111.
- **Branding:** the NHS logo may only be used by NHS services. We use the
  open-source NHS.UK frontend styles (MIT licence) without the logo, and the
  footer states we are not an NHS service (on the `feat/nhs-style` branch).

## How the NHS currently approaches AI

The NHS is actively adopting AI, but inside its own governance. As of late 2026:

- **National rollout:** NHS England is rolling out Microsoft 365 Copilot to
  around 500,000 staff, and has announced an AI triage tool in the NHS App and
  wider use of AI note-taking.
  ([June 2026](https://www.england.nhs.uk/2026/06/500000-nhs-staff-to-get-new-artificial-intelligence-tools-to-help-free-up-more-time-for-patients/),
  [July 2026](https://www.england.nhs.uk/2026/07/nhs-accelerates-artificial-intelligence-rollout-to-cut-waiting-times-and-improve-care-for-millions/))
- **Ambient voice technology (AI scribes):** NHS England's
  [guidance](https://www.england.nhs.uk/long-read/guidance-on-the-use-of-ai-enabled-ambient-scribing-products-in-health-and-care-settings)
  is the closest precedent for us, because it covers AI listening to and
  processing conversations. GP practices adopting a product are expected to
  hold a clinical safety case, a hazard log and a monitoring plan, and to
  complete a DPIA. Reported updates in 2026 add a template DPIA, a supplier
  registry and a position that transparency is essential even where explicit
  consent isn't required. Check the current version, as it is revised often.
- **Liability stays local:** practices remain liable for how they use AI
  products, so a surgery will want to know who is accountable for what our
  agent says.
- **Information governance:** NHS England says existing consent and IG rules
  apply to AI in most cases; see the
  [AI knowledge repository](https://digital.nhs.uk/services/ai-knowledge-repository/ai-in-practice).

What this means for us: the NHS's guidance is written for AI that *NHS
organisations* deploy. An AI acting for the *patient* and calling in from
outside isn't directly covered. The safest course is to meet the same bar
voluntarily: a DPIA, a clinical safety case, transparency at the start of
every call, and a clear route to a human (the patient).

## Arguing that a digital representative should be accepted

There is no NHS rule that explicitly covers an AI calling a surgery for a
patient. But the existing rules are about *the patient's consent and
identity*, not about *who or what carries the message*. That supports the
following argument.

1. **Patients can already have someone act for them.** Relatives and carers
   routinely ring surgeries for patients with their consent. NHS England's
   [proxy access guidance](https://www.england.nhs.uk/long-read/proxy-access/)
   formalises this for online services, based on the patient's consent (or a
   lasting power of attorney). The basis is the patient's authority, not that
   the representative is a person.
2. **Mediated calls are already accepted.** Through
   [Relay UK](https://rnid.org.uk/information-and-support/technology-and-products/relay-uk/),
   a relay assistant sits in the middle of the call, and the
   [Accessible Information Standard](https://www.england.nhs.uk/long-read/accessible-information-standard-implementation-guidance/)
   requires practices to support patients' communication needs, including
   relay services. Surgeries already book appointments through a third party
   relaying the patient's words.
3. **The NHS itself puts software between patients and practices.** Online
   consultation forms, the NHS App and its planned AI triage all mean a
   patient's request reaches the practice through software. An agent that
   makes a phone call on the patient's instructions is the same idea over a
   different channel.
4. **It is the patient's own disclosure.** The patient chooses to share their
   details, and our agent only asks for an appointment. It does not request
   records or clinical information, which is where practices apply the
   strictest identity checks.
5. **Equality and access.** Many patients can't wait on hold at 8am: people
   with some disabilities, shift workers, carers. Practices must make
   reasonable adjustments under the Equality Act 2010, and an assistant that
   holds the line for them is one.

Limits we should be upfront about:

- A surgery can still refuse to deal with a third party, or ask to speak to
  the patient. Our `connect_patient` tool exists for this.
- Practices verify identity differently. We should send what they need (name,
  date of birth, address) and nothing more.
- To strengthen the case, we could give surgeries a way to check consent, such
  as a reference the patient also receives by SMS, and seek endorsement from
  an ICB or Local Medical Committee before going live.

## Calls, AI disclosure and consent

- **Acting for the patient:** the agent must say it is an automated assistant
  acting for the patient, never pretend to be them. Surgeries may refuse to
  deal with third parties or ask for extra identity checks.
- **Receptionists' data:** their voice and words are transcribed and processed.
  The opening line discloses the AI; the privacy notice should cover them too.
- **Connecting the patient:** only ring the patient if they opted in on the form.
- **Call conduct:** Ofcom's persistent misuse rules (Communications Act 2003)
  cover silent and abandoned calls, so the app must not repeatedly dial a
  surgery or leave calls silent.

## Other

- **Accessibility:** the Equality Act 2010 applies; aim for WCAG 2.2 AA.
- **Liability:** terms of use should cover wrong or missed bookings, and make
  clear that the surgery's confirmation is what counts.
- **Fairness:** surgeries may object to automated queueing. Agreeing a
  partnership with practices before launch would avoid disputes.
- **Third-party terms:** check Twilio's and any AI provider's terms on health
  data and automated calling.

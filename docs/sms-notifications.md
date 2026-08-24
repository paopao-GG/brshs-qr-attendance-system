# TRACKIFY — SMS Notifications

**Decision: SMS API (Semaphore). No GSM/LTE hardware module.**

---

## 1. Why an API and not a GSM module

The comparison was going to be a judgement call about reliability and cost. Philippine
regulation largely settled it.

**NTC Memorandum Circular No. 003-09-2025** (issued 28 August 2025) orders the phase-out of 2G
and 3G mobile networks, with nationwide 3G shutdown by **31 December 2026** and 2G following on
a separate area-specific schedule. Critically for a hardware build, the same circular provides
that the NTC **no longer accepts type-approval applications for devices with exclusive or
primary 2G/3G capability**, and that importation and sale of such devices is prohibited.

A **SIM800L or SIM900A — the modules almost every tutorial uses — is a 2G-only device.** For a
system deployed across the 2026–2027 school year, that means building on a network being
decommissioned, using a device class now restricted from import and sale. It is a fair question
for a judge to ask, and there is no good answer to it.

The remaining hardware option would be an LTE Cat-1 module (SIM7600, A7670C) at roughly
₱1,500–2,800. That is viable, but against an API it still carries the power problem (transmit
bursts draw ~2 A and must never come off the Pi's rail), serial throughput of ~3–6 s per
message, and the risk of a consumer SIM being flagged for spam when it sends near-identical
messages to hundreds of numbers.

| | **Semaphore API** (chosen) | GSM/LTE module |
|---|---|---|
| Regulatory exposure | None | 2G-only modules restricted; LTE required |
| Hardware | None | Module, power supply, antenna, enclosure |
| Cost | ~₱0.50/SMS, no setup fee | ₱1,500–2,800 + SIM load |
| Throughput | Hundreds per minute | ~3–6 s each, serial |
| Delivery status | Provider message ID and status | Weak, unreliable via raw AT commands |
| Bulk sending | Designed for it | Consumer SIMs get spam-flagged |
| Internet needed | **Yes — the main trade-off** | No |
| Student data leaves campus | **Yes — must be disclosed** | No |
| Pi 5 integration | An HTTPS POST | UART config, level shifting, power design |

### The two costs of this choice, and how they are handled

1. **The internet becomes a single point of failure.** Handled by the store-and-forward queue
   in §3 — an outage delays notifications, it never loses them.
2. **Guardian mobile numbers and student identifiers go to a third party.** This is a Data
   Privacy Act matter and must be disclosed in the consent form and in §F of the research plan.
   See §6 and [research-plan-review.md](research-plan-review.md) item 8. It cannot be handled
   technically — it has to be disclosed.

---

## 2. Provider abstraction

Notification logic must not know which provider it is talking to. One narrow interface,
implementations behind it:

```
NotificationProvider
  ├── send(recipient, body) -> {ok, provider_message_id, error}
  └── name

SemaphoreProvider     # production
ConsoleProvider       # development — prints, never sends
NullProvider          # dry-run for the pilot, counts without sending
```

This exists for three practical reasons, not architectural neatness:

- You can develop and demo the entire flow without spending credits or texting real parents
- The pilot can run end-to-end with `NullProvider` and prove the queue works before a single
  real message goes out
- If Semaphore is unavailable on deployment day, swapping providers touches one class

### Semaphore request shape

```
POST https://api.semaphore.co/api/v4/messages

  apikey      = <from environment>
  number      = 09171234567          # or comma-separated for bulk
  message     = <body>
  sendername  = <registered sender name>
```

Sender names must be registered and approved by Semaphore before use; unregistered sends fall
back to their default sender. **Register yours early** — approval is not instant and a school
notification arriving from a generic sender is far less credible to a parent.

Verify current endpoints, parameter names, and status values against
[semaphore.co/docs](https://semaphore.co/docs) before implementing — treat the above as the
shape, not as gospel.

---

## 3. Store-and-forward queue

Nothing in the entry flow ever waits on the network. Sending is a background concern.

```
enqueue → notifications(status=pending) → worker → provider → sent | failed
```

| Rule | Reason |
|---|---|
| The entry flow **only** writes a `pending` row | A slow API must never hold up the queue at the gate |
| A separate worker process does all sending | Network latency is isolated from the UI |
| Exponential backoff on failure (e.g. 30 s, 2 m, 10 m, 30 m) | Survives a WiFi outage without hammering the API |
| Retry limit, then `status=failed` | Bounded; failures become visible rather than infinite |
| **Unsent count shown persistently on the dashboard** | A sustained outage is noticed the same day, not discovered in the data afterwards |
| Every attempt logged with timestamp and result | Delivery evidence for the study |
| Idempotency key per (student, trigger, date, session) | A worker restart must not double-send |

That last row matters more than it looks. Without an idempotency key, a crash between "sent"
and "recorded as sent" causes a duplicate on restart — and a parent receiving two contradictory
messages about their child undermines confidence in the whole system.

---

## 4. Cost model

At ₱0.50 per message, for 200 students over the 20-day deployment:

| Policy | Volume | Cost | Notes |
|---|---|---|---|
| Every arrival, AM + PM | ~8,000 | **₱4,000** | Every scan texts a parent, twice daily |
| Every arrival, AM only | ~4,000 | **₱2,000** | Still one text per student per day |
| **Exception-only** (recommended) | ~600–700 | **₱300–350** | Absences, lates, confirmed incidents |
| Exception-only + weekly digest | ~1,400 | **₱700** | Adds a Friday summary per student |

**Recommend exception-only.** It is roughly a 90% cost reduction, and it is also better
product design: a daily "your child arrived on time" text is ignored within a week, and once
parents are ignoring the messages, the absence notification gets ignored too. Scarcity is what
makes the absence alert land.

Make the policy **configurable and per-guardian opt-in**, and log the policy in force so it can
be reported alongside results.

---

## 5. Message templates

Keep every message inside **160 characters** so it bills as one credit.

| Trigger | Template |
|---|---|
| Absence | `TRACKIFY: {first_name} ({section}) was not recorded present for {session} on {date}. Please contact the school if this is unexpected.` |
| Late | `TRACKIFY: {first_name} ({section}) arrived late at {time} on {date}.` |
| Confirmed incident | `TRACKIFY: A school policy matter involving {first_name} ({section}) was recorded on {date}. Please contact {adviser} or the school office.` |
| Weekly digest | `TRACKIFY weekly: {first_name} ({section}) - present {p}/{t} sessions, {l} late. Full report available from the class adviser.` |

**Incident messages deliberately say nothing specific.** SMS is not a secure channel — it is
unencrypted, and it goes to a handset that may be shared, lost, or read by someone else. Naming
a confiscated item over SMS would be an unnecessary disclosure of sensitive personal
information. The message says a matter was recorded and directs the parent to the school, where
the conversation can happen properly.

### The character-set trap

SMS uses the **GSM-7** alphabet, which fits 160 characters per message. A single character
outside that alphabet forces the whole message into **UCS-2**, which fits only **70** — so the
message silently splits and bills double, and long messages get truncated.

Characters that will break this, in order of how likely you are to hit them:

| Character | Where it comes from |
|---|---|
| `'` `'` `"` `"` **smart quotes** | **Copy-pasting a template out of Word.** The single most common cause |
| `—` `–` em dash, en dash | Same — Word autocorrects `-` into these |
| **`₱` peso sign** | Typing a peso amount. GSM-7 has `£ $ ¥ ¤` but **not** `₱` |
| Emoji | Anything decorative |
| `^ { } \ [ ] ~ \|` `€` | In GSM-7's *extension* table — each counts as **two** characters |

Note that `ñ`, `Ñ`, `ä`, `ö`, `ü`, `à`, `é` **are** in GSM-7, so Filipino surnames like Peña
and Muñoz are safe. The danger is punctuation from a word processor, not names.

**Mitigation:** validate every rendered message before enqueueing — check it against the GSM-7
character set and log or reject anything outside it. Use `PHP` peso amounts as `PHP 500`
rather than `₱500`. Write templates in a plain-text editor, never Word.

---

## 6. Privacy

- **Disclose the third-party transfer.** Guardian mobile numbers and student identifiers are
  transmitted to Semaphore. This belongs in the consent form and in §F of the research plan.
- **Minimise the payload.** First name and section, not full name. No LRN, no item description,
  no risk score, ever.
- **Consent gates participation.** No notification is sent for a student without
  `students.consent_on_file`. Enforce this in the enqueue path, not just the UI.
- **Verify guardian numbers** at enrolment. A wrong number sends a child's attendance data to a
  stranger — the most likely real-world privacy incident in this whole system, and far more
  likely than a database breach.
- **Retention.** Set a period for `notifications` rows. Message bodies contain student names and
  should not accumulate indefinitely.
- **Opt-out.** Provide a route for a guardian to stop messages, and honour it.

---

## 7. Configuration and secrets

```
SEMAPHORE_API_KEY=...
SEMAPHORE_SENDER_NAME=...
NOTIFICATION_POLICY=exception_only
NOTIFICATION_RETRY_LIMIT=5
```

- API key from **environment variables only.** Never a literal in source, never in a committed
  config file.
- `.env` in `.gitignore` from the first commit. A key committed once is compromised even after
  it is removed — git keeps history.
- Commit a `.env.example` with empty values so the required variables are documented.
- Keep separate keys for development and deployment if the provider allows it.

---

## 8. Failure modes

| Failure | Behaviour | Visibility |
|---|---|---|
| No internet | Queue holds `pending`, retries with backoff | Unsent count on dashboard |
| API returns an error | Retry to limit, then `failed` | Unsent count + log |
| Out of credits | All sends fail | **Add a low-balance check to the daily startup routine** — silently running out mid-deployment is the realistic way this breaks |
| Invalid/dead number | Provider rejects; marked `failed` | Flagged for adviser to correct at source |
| Worker crash | Pending rows survive in SQLite; resume on restart | Idempotency key prevents double-send |
| Sender name not yet approved | Sends go out under provider default | Check before deployment day |
| Message exceeds 160 chars | Splits, bills double | Caught by the pre-enqueue validator (§5) |

---

## 9. Testing before deployment

1. `ConsoleProvider` — full flow, zero credits spent
2. `NullProvider` on the pilot section — proves the queue, retry, and idempotency paths work
   end to end without texting anyone
3. Live send to **your own number** for every template
4. Character-set validator unit tests, including a template pasted from Word with smart quotes
5. Pull the network cable mid-session — confirm rows stay `pending`, confirm they flush on
   reconnect, confirm nothing is lost or duplicated
6. Kill the worker mid-send — confirm no duplicate on restart
7. Confirm the low-credit warning fires
8. Verify guardian numbers for the pilot section against school records **before** the first
   live send

Step 5 and step 8 are the two that matter most. Everything else is recoverable.

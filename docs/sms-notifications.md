# TRACKIFY — SMS Notifications

**Decision: LC SIM800C V3 GSM module on USB serial, Smart/TNT SIM.**

> **This reverses the earlier decision in this document**, which chose the Semaphore HTTP
> API and argued against a GSM module. The reasoning that follows is the reversal, kept
> visible rather than quietly rewritten, because the objection it overrules is real and
> has to be answered in the paper.

---

## 1. Why a GSM module, and what it costs to choose one

### The original objection still stands

**NTC Memorandum Circular 002-09-2025** orders the phase-out of 2G and 3G mobile networks:
nationwide 3G shutdown by **31 December 2026**, 2G following on a separate area-specific
schedule. The same circular provides that the NTC **no longer accepts type-approval
applications for devices with exclusive 2G or 3G capability**, and that importation and
market entry of such devices is prohibited.

*(Earlier drafts of this document cited this as MC 003-09-2025. Verify the number against
the NTC PDF before it goes into the paper.)*

**A SIM800C is a 2G-only device.** Everything the earlier draft said about that remains
true. It was not wrong; it was outweighed.

### What outweighed it

| | GSM module (chosen) | HTTP SMS API |
|---|---|---|
| Cost per message | **≈ ₱0 on an unli-text promo** | ₱0.35–0.50 |
| Cost for a 20-day, 200-student study | **≈ ₱0** after the module | **≈ ₱2,800** |
| Internet needed | **No** | Yes — a single point of failure at a school |
| Student data leaves campus | **No** | **Yes** — a Data Privacy Act disclosure |
| Regulatory life | **Expires with 2G** | Indefinite |
| Throughput | 3–10 s per message, serial | Hundreds per minute |
| Delivery status | A wrapping 0–255 reference, no dashboard | Provider message ID and status |
| Sender identity | The SIM's own number | A registered sender ID |
| Hardware | Module, 2 A supply, antenna, enclosure | None |

Two of those lines are the whole argument. The cost difference is the study's entire SMS
budget, and **the privacy position genuinely improves**: guardian numbers and student
identifiers no longer leave the campus for a third party. The earlier draft listed that
disclosure as a cost that "cannot be handled technically — it has to be disclosed". It no
longer has to be, and [research-plan-review.md](research-plan-review.md) item 8 should be
updated to say so.

### The honest statement of the limitation

For the **study** — roughly 20 days in 2026 — 2G is live on Smart and this works. For a
**school deployment across 2027 and beyond**, the transport has an expiry date, and the
system will need either an HTTP API or an LTE Cat-1 module (SIM7600, A7670C, ₱1,500–2,800).

This belongs in the paper's limitations section rather than being hidden, and it is
arguably a finding: a low-cost build of this kind is standing on infrastructure being
switched off.

### Three practical consequences

1. **Throughput.** 400 messages a day is 20–65 minutes of near-continuous transmission.
   The queue is asynchronous so the scan station never blocks, but guardian coalescing
   stops being a courtesy to sibling families and becomes a throughput measure.
2. **No sender ID.** Guardians see the SIM's own number. The `TRACKIFY:` prefix in the
   body carries the whole burden of identifying the school — and parents can reply, to an
   inbox nobody reads. SIM storage must be cleared or outgoing sends eventually fail.
3. **Power, which is what actually goes wrong.** The transmit burst draws up to 2 A; a
   laptop USB port supplies 0.5–0.9 A. The resulting brownout resets look exactly like a
   dead SIM or a bad AT sequence. See [hardware.md](hardware.md) §5.

### Networks

**Smart or Globe only.** DITO launched as a 4G/5G-only operator and never deployed 2G, so
a SIM800C cannot register on it at all.

---

## 2. Provider abstraction

Notification logic must not know which provider it is talking to. One narrow interface,
implementations behind it:

```
NotificationProvider
  ├── send(recipient, body) -> {ok, provider_message_id, error}
  └── name

GsmProvider           # production -- SIM800C on a USB serial port
ConsoleProvider       # development — prints, never sends
NullProvider          # dry-run for the pilot, counts without sending
```

This exists for three practical reasons, not architectural neatness:

- You can develop and demo the entire flow without spending credits or texting real parents
- The pilot can run end-to-end with `NullProvider` and prove the queue works before a single
  real message goes out
- Swapping the transport touches one class. This was proven, not hoped for: moving from
  the Semaphore/PhilSMS HTTP provider to a serial modem left the queue, coalescing, spend
  breaker, QThread worker and kiosk completely unchanged

### The AT command shape

```
AT+CMGF=1                        text mode, not PDU
AT+CSCS="GSM"                    GSM-7 charset, matching notify/gsm7.py
AT+CMGS="+639171234567"    ->  > prompt
{body}<Ctrl-Z, 0x1A>       ->  +CMGS: 23   then   OK
```

There is **no sender name to register**. The recipient sees the SIM's own mobile number,
which is why every template opens with `TRACKIFY:` — that prefix is the only thing telling
a parent who the message is from.

Two commands in the init sequence are easy to omit and expensive to omit:

- `AT+CSCA?` — the SMS centre number. **Blank means every send fails silently.**
- `AT+CMGD=1,4` — clear stored messages. Replies and delivery reports accumulate in SIM
  storage, and **once it is full, outgoing sends start failing.** On a SIM whose number
  parents can reply to, that is weeks away, not years.

`+CMGS` returns a message reference in 0–255 that **wraps**. Store it for the log; it
cannot be reconciled against anything, because there is no provider dashboard.

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

- **No third-party transfer.** This improved when the transport moved to a GSM module:
  guardian mobile numbers and student identifiers now go from the Pi straight to the
  mobile network, never to an SMS provider's servers. The consent form should say so
  positively rather than carrying the disclosure the API version needed.
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

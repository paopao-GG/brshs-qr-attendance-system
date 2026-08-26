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
longer has to be, and [research-plan-review.md](research-plan-review.md) item 4 should be
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

**The cost table above is pre-module and now mostly historical.** With a SIM800C on a school
SIM the marginal cost is a load top-up, not ₱0.50 a message — §1 puts it at **≈ ₱0** after the
hardware. What the table still measures correctly is *attention*, and that is the real
constraint.

**The argument for exception-only stands on product grounds.** A daily "your child arrived on
time" text is ignored within a week, and once parents are ignoring the messages the absence
notification gets ignored too. Scarcity is what makes the absence alert land.

**`config.toml` currently ships `policy = "in_and_out"`**, which texts on arrival and
departure. That is the right default for a *pilot*, where the point is to demonstrate the
system working and to catch wrong numbers early, and the wrong default for a term — switch to
`exception_only` before the 20-day run. The policy in force is configuration, so it can be
reported alongside the results.

Per-guardian opt-out already exists (`students.notify_optin`) and is checked at enqueue.

---

## 5. Message templates

Keep every message inside **160 characters** so it bills as one credit.

Verbatim from `trackify/notify/queue.py`. Every one has been sent from the module to a live
handset and fits a single GSM-7 segment.

| Trigger | Template | Chars |
|---|---|---|
| `arrival` | `TRACKIFY: {first} ({section}) arrived {time} on {date}.` | 64 |
| `late` | `TRACKIFY: {first} ({section}) arrived late at {time} on {date}.` | 72 |
| `departure` | `TRACKIFY: {first} ({section}) left school {time} on {date}.` | 68 |
| `absent` | `TRACKIFY: {first} ({section}) was not recorded present on {date}. Please contact the school if unexpected.` | 114 |
| `incident` | `TRACKIFY: Please contact the school today regarding {first} ({section}).` | 76 |
| `summary` | `TRACKIFY: {first} ({section}) week of {period}: present {present}, late {late}, absent {absent} of {days} school days.` | 98 |
| `reminder` | `TRACKIFY: {first} ({section}): {absent} absences in {period}. {clause} Please contact the school if there is a difficulty at home.` | 146 |

`{first}` is the **first name only** — not the full name, and never the LRN. `summary` and
`reminder` are periodic rather than event-driven; see §5.1.

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

There is **no API key** — the transport is a module on a serial port, not a web service. Two
secrets, both environment-only:

```
# .env  -- gitignored, never committed
TRACKIFY_QR_SECRET=...        # HMAC key for QR payloads
SMS_ALLOWLIST=09171234567     # comma-separated; see below
```

Behaviour lives in `config.toml`, not the environment:

```toml
[notifications]
policy = "in_and_out"           # or exception_only
coalesce_window_minutes = 3
retry_limit = 5
backoff_seconds = [30, 120, 600, 1800, 3600]
weekly_summary = true
absence_reminders = true
monthly_absence_limit = 3
absence_warn_at = 2

[limits]
daily_message_cap = 1000
per_recipient_daily_cap = 6
requests_per_second = 2
```

**`SMS_ALLOWLIST` is the control that makes live testing safe.** With the real transport
pointed at a roster of real guardian numbers, one mistake texts a stranger's parent. When the
allowlist is populated, only those numbers can be reached and everything else is marked
`suppressed` with the reason recorded.

**It restricts nothing when empty.** That is deliberate — a school in production must not have
to enumerate 71 numbers — but it means the allowlist cannot be the only guard. The consent
check in `queue.enqueue` is the one that travels with the database.

`.env` has been in `.gitignore` from the first commit. A secret committed once is compromised
even after removal, because git keeps history.

---

## 8. Failure modes

The internet is no longer in this path at all. What replaced it:

| Failure | Behaviour | Visibility |
|---|---|---|
| **Module unplugged or not answering** | The queue is **not drained**. Rows stay `pending` with `retry_count` untouched and go out on the first pass after it returns | Status bar reads `SMS: gsm unavailable`, amber, reason on hover |
| **Wrong serial port** (on Windows, usually Bluetooth) | A 2-second `AT` probe fails it fast instead of spending `init_timeout` on each of ~13 commands | Same amber indicator, within seconds |
| **Brownout mid-send** | The transmit burst pulls ~2A against a USB port's 0.5-0.9A. Reported `ambiguous` and parked `unknown` — **never auto-retried** | Unsent count; a human reconciles |
| **Silence after Ctrl-Z** | The message may already have reached the SMS centre. Same at-most-once rule: `unknown`, not a retry | Unsent count |
| SIM not registered / no load / blank SMSC | `health().blocker()` names the one thing wrong before the body is written, so the refusal is unambiguous and safe to retry later | Send result, and `scripts/test_sms.py --check` |
| Module answers and says ERROR | Definite failure. Retry to limit with backoff, then `failed` | Unsent count |
| Worker crash mid-send | Claimed rows survive as `sending`; `reconcile_stale` marks them `unknown` on restart rather than resending | Alarm on next launch |
| Daily spend cap reached | `SpendBreaker` trips; further sends `suppressed` | `SMS: HALTED`, latched until restart |
| Recipient not on `SMS_ALLOWLIST` | `suppressed` with the number in the reason | Queue monitor |
| No consent on file | Refused at enqueue; no row is written at all | Returned reason, counted in the summary run |
| Body would exceed one segment | Refused **at enqueue**, not at send — failing at double cost on every retry is worse than failing once | Returned reason |

---

## 9. Testing before deployment

1. `ConsoleProvider` — full flow, zero credits spent
2. `NullProvider` on the pilot section — proves the queue, retry, and idempotency paths work
   end to end without texting anyone
3. Live send to **your own number** for every template
4. Character-set validator unit tests, including a template pasted from Word with smart quotes
5. **Unplug the module mid-session** — confirm the status bar goes amber within seconds,
   confirm rows stay `pending` with `retry_count` unchanged, confirm they flush when it is
   plugged back in
6. Kill the worker mid-send — confirm no duplicate on restart (the row becomes `unknown`)
7. Confirm `scripts/test_sms.py --check` reports supply voltage; a laptop USB port will read
   fine at idle and still brown out on the transmit burst
8. Verify guardian numbers for the pilot section against school records **before** the first
   live send

Step 5 and step 8 are the two that matter most. Everything else is recoverable.

---

## 10. Where this lives

| Piece | File |
|---|---|
| Provider abstraction, Console and Null | `trackify/notify/provider.py` |
| SIM800C over AT commands, health, availability | `trackify/notify/gsm.py` |
| GSM-7 alphabet, segment counting, truncation | `trackify/notify/gsm7.py` |
| Enqueue, claim, drain, retry, idempotency | `trackify/notify/queue.py` |
| Sibling coalescing into one message | `trackify/notify/coalesce.py` |
| Spend breaker, token bucket, allowlist | `trackify/notify/limits.py` |
| Weekly summary and absence reminder | `trackify/notify/periodic.py` |
| The drain worker on its own thread | `trackify/ui/worker.py` |
| Staged live bring-up | `scripts/test_sms.py` |

### 5.1 Periodic messages

Two shapes, deliberately different.

**The weekly summary** is a batch. Every consenting guardian gets one message covering the
school week — including a week of perfect attendance, because most parents never hear from a
school unless something is wrong. It is sent when someone presses **Send weekly summaries** on
the records screen, never automatically: seventy-odd texts leaving at once is an event a person
should decide, and the dialog shows the count before anything is queued. Pressing it twice
queues nothing the second time; the week is in the idempotency key.

**The absence reminder** is one student crossing a threshold, sent the day it happens. It rides
the end-of-day close, where the absence is detected and where the absence notification already
goes out. It fires at exactly two counts — `absence_warn_at` and `monthly_absence_limit` — and
then goes quiet. A text on every further absence is nagging, and by then the conversation
belongs to a person.

Neither names a consequence. "Please contact the school" is the whole ask; what follows is the
school's decision, not an SMS template's.

An **excused** absence does not count toward the limit. A corrected day is read at its
corrected value, so a parent is never warned about an absence an adviser has already excused.

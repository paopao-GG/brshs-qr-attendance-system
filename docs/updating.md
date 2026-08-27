# Updating the deployed station

How a change made on the development PC reaches the Raspberry Pi at the gate.

The station holds the only copy of the attendance data. Everything here is written so that a
mistake costs you a restart, not a term's records.

---

## Three rules

**1. Back up before you pull.** `data/trackify.db` is the only irreplaceable thing on the
station. The code is in git; the roster can be re-imported; the attendance cannot.

**2. Stop the kiosk first.** The serial port is exclusive — the kiosk and any script cannot both
hold it — and the database should not be open while you swap the code underneath it.

**3. Never update on a school morning.** Run the test suite before you restart. Forty seconds
against discovering a broken pull with students queuing at the gate.

---

## The standard procedure

This is the whole thing. Most updates need nothing else.

```bash
cd ~/trackify/brshs-qr-attendance-system

# 1. Stop the kiosk (releases the database and the serial port)
systemctl --user stop trackify-kiosk

# 2. Back up. VACUUM INTO, never cp -- see "Why not cp" below.
python3 -c "import sqlite3; c=sqlite3.connect('data/trackify.db'); \
c.execute('VACUUM INTO ?', ('data/trackify.backup-$(date +%F-%H%M).db',)); c.close()"

# 3. Pull
git pull

# 4. Dependencies -- only if requirements.lock changed in that pull
.venv/bin/pip install -r requirements.lock

# 5. Prove it still works BEFORE you put it back in front of students
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q

# 6. Start
systemctl --user start trackify-kiosk

# 7. Watch it come up
journalctl --user -u trackify-kiosk -f
```

Check the status bar afterwards: `Cam: ok`, and the SMS indicator saying what you expect.

### Did requirements.lock change?

```bash
git diff HEAD@{1} --name-only | grep -q requirements.lock && echo "REINSTALL NEEDED" || echo "no dependency change"
```

Skipping step 4 when it was needed fails as an `ImportError` on startup — the kiosk will not
open at all, which at least fails loudly.

---

## What changed → what else you need

| What changed | Reinstall deps | Restart | Anything else |
|---|---|---|---|
| Docs, comments, README | no | no | nothing |
| UI text, a bug fix, a feature | no | **yes** | nothing |
| A feature with a new library | **yes** | **yes** | nothing |
| Database schema (new column, widened CHECK) | no | **yes** | **nothing — it is automatic** |
| `config.toml` | no | **yes** | resolve the conflict deliberately — see below |
| The roster (new students, corrected numbers) | no | no | re-import in the app, **not** a reseed |
| `TRACKIFY_QR_SECRET` | no | **yes** | **every printed card must be reprinted** |

---

## Scenario 1 — Docs or comments only

```bash
git pull
```

Nothing else. Markdown files are not loaded at runtime and the kiosk never reads them. No
restart, no reinstall.

## Scenario 2 — Code change, no new dependency

The common case: a bug fix, changed wording on the screen, a tweak to how something renders.

Run the standard procedure. Python does not hot-reload, so **the restart is what applies it** —
a pull on its own changes nothing about the running kiosk.

## Scenario 3 — A feature that adds a library

`.venv/` is gitignored, so `git pull` never installs anything. Step 4 is not optional here.

```bash
.venv/bin/pip install -r requirements.lock
```

Install from **`requirements.lock`**, not `requirements.txt`. The lock is the exact set verified
on this Pi; the floors in `requirements.txt` resolve to whatever PyPI offers that morning, which
nobody has tested.

## Scenario 4 — A database schema change

**Nothing to do. Restart and it is applied.**

`init_db()` runs on every startup and does three things ([db.py](../trackify/core/db.py)):

- re-runs `schema.sql`, which is all `CREATE TABLE IF NOT EXISTS`, so existing tables are left
  alone and genuinely new tables are created
- `ensure_columns()` adds any column in `MIGRATIONS` that the database does not have yet
- `_widen_notification_triggers()` rebuilds the `notifications` table if its CHECK constraint
  has been widened

So a new column added on the PC simply appears on the Pi after a restart, with existing rows
carrying its default. Your attendance data is not touched.

**The one limit:** this is additive only. SQLite cannot drop or retype a column, so a change
that needs either is a hand-written migration and will say so in its commit message. If you
pull one of those, read it before restarting.

## Scenario 5 — `config.toml` changed on both machines

This is the one that bites, because `config.toml` **is tracked by git** while `.env` and `data/`
are not.

If you tuned `late_threshold` on the Pi and someone edited the same file on the PC, `git pull`
either refuses or produces a conflict:

```bash
git stash                 # park the Pi's local tuning
git pull
git stash pop             # replay it; resolve any conflict by hand
```

**Resolve it deliberately.** These belong to the school, not to a development machine:

```toml
[risk.bands]
low = 0.30
set_by = "Guidance counsellor and discipline officer, ..."
set_on = "2026-08-27"
```

A band cutoff decides whether a real student is referred, and `set_by` / `set_on` record who
made that decision. Losing them to a merge replaces an institutional decision with a developer's
default, silently.

**The permanent fix**, worth doing before the 20-day run if you expect to tune on the Pi — treat
it the way `.env` is already treated:

```bash
git rm --cached config.toml
cp config.toml config.toml.example      # ship the shape, not the school's values
echo "config.toml" >> .gitignore
git add config.toml.example .gitignore
git commit -m "chore: untrack config.toml; it is per-station"
```

After that the Pi's configuration is its own and no pull can overwrite it.

## Scenario 6 — Updating the roster

**Do not reseed.** Use the roster screen in the app: open the records screen, enter the password,
and import the updated `data/student-list.xlsx`.

The import is additive and safe on a live database — it creates students it has not seen and
reports what it skipped. It has no relationship to `git pull` at all; the roster is data, not
code.

Two things to know:

- **An import never grants consent.** New students are always created with
  `consent_on_file = 0`, because a spreadsheet column cannot record consent under RA 10173.
  Only a person ticking the box in the roster screen, having seen the signed form, can.
- **A corrected LRN invalidates that student's printed card.** The QR payload is signed over the
  LRN, so changing it means the old card no longer verifies. The import warns you; reprint that
  one card.

## Scenario 7 — Resetting the database

```bash
.venv/bin/python scripts/seed_demo.py --reset
```

**On a deployed station this is almost never what you want.** It is for starting over, not for
updating.

| Destroyed | Survives |
|---|---|
| All attendance records | Printed QR cards — still valid |
| All scan events | `.env`, including the QR secret |
| The whole notification queue and SMS ledger | `config.toml` |
| Screening events, incidents, custody records | `data/student-list.xlsx` |
| The audit log | |
| **The records password** — unset, must be chosen again on next launch | |

Printed cards survive because the payload is derived from **(LRN, secret)** and never stored in
the database. Row ids restart at 1 on a reset, and it does not matter — the card was never keyed
on them.

If you only want to clear demonstration traffic and keep the real roster, use the simulator's
own switch instead, which removes only the range it generated:

```bash
.venv/bin/python scripts/simulate_term.py --clear
```

## Scenario 8 — Rolling back a bad update

```bash
systemctl --user stop trackify-kiosk

git log --oneline -10                       # find the last good commit
git checkout <sha>                          # or: git reset --hard <sha>
.venv/bin/pip install -r requirements.lock  # that revision's dependency set

# only if the data is also wrong -- code rollbacks rarely need this
cp data/trackify.backup-YYYY-MM-DD-HHMM.db data/trackify.db

systemctl --user start trackify-kiosk
```

Roll the code back first and restart. Restore the database only if the data itself is damaged —
schema migrations are additive, so an older build reading a newer database is usually fine.

---

## Why not `cp` for the backup

The database runs in **WAL mode**: recent writes live in `data/trackify.db-wal` until a
checkpoint folds them into the main file. Copying `trackify.db` alone silently loses them, and
you do not find out until you restore.

`VACUUM INTO` writes a single consistent file with everything in it. This is the project's own
convention — `scripts/simulate_term.py` uses it before it touches anything.

```bash
python3 -c "import sqlite3; c=sqlite3.connect('data/trackify.db'); \
c.execute('VACUUM INTO ?', ('data/trackify.backup.db',)); c.close()"
```

`data/` is gitignored, so backups written there are never committed — which matters, because
they contain 110 real minors' records.

---

## What a pull can never touch

Safe by design, on every update:

| | Why |
|---|---|
| `.env` | gitignored — your QR secret and `SMS_LIVE` stay put |
| `data/` | gitignored — database, roster, backups, generated QR sheets |
| `.venv/` | gitignored — which is exactly why step 4 exists |
| `~/.config/systemd/user/trackify-kiosk.service` | outside the repo |
| `~/.config/labwc/autostart` | outside the repo |
| `~/Desktop/trackify-kiosk.desktop` | outside the repo |

The last three mean the kiosk keeps starting at boot, and the desktop launcher keeps working,
no matter what you pull. If you change how the kiosk is *launched* — a new command-line flag,
say — you must edit the service file by hand and `systemctl --user daemon-reload`; git will not
do it for you.

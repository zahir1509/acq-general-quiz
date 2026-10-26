# Prelims Quiz

A local-network quiz platform for a **multi-pass written prelims**: every team
answers on its phone while the questions go up on the projector. Built for
50-100 teams on a single offline Wi-Fi router, with the quizmaster on a
laptop. Python 3.8+ standard library only: no `pip install`, no Node, no
internet.

## The format

```
LOBBY -> PASS 1 -> PASS 2 -> PASS 3 -> LOCKED -> REVEAL
```

| Phase | Projector | Phones |
|---|---|---|
| **Pass 1** | one question at a time, as the quizmaster clicks *Next* | the same question, an **Answer** box and a private **Notes** box. Both editable, saved automatically |
| **Pass 2** | the same walk-through again, from question 1 | everything pre-filled: your earlier answer and your Notes. Edit either |
| **Pass 3** | "PASS 3" + instructions + a big countdown (default 2:00). No questions shown | a grid of all questions: tap any one, change the answer. Notes are shown read-only |
| **Locked** | "Answers locked" | "Answers locked" |
| **Reveal** | each question first, then its answer when you press **Show answer** (one at a time or six per page) | the questions so far; each answer, your own answer and your marks appear only once shown, and your running score counts only the answers shown so far |

* The **latest answer a team saved is its final answer.**
* There is no pounce, bounce or negative marking. Scoring is flat points per
  question (default 10, overridable per question) plus optional part-marks
  decided by the reviewer.
* The existing *show question text on phones* toggle is kept (SETUP). There is
  a second toggle for **pictures / audio / video on phones**, off by default.
  Media always plays on the projector, and the scorer controls it
  (play / pause / restart / seek / volume) from the RUN tab.

## Quick start

```bash
python3 server.py --seed          # 20 demo teams + 20 demo questions
python3 server.py                 # a real event (data/wiz.db)
python3 server.py --reset         # new event: clear teams/answers, keep questions
python3 server.py --port 8080 --pin 4321 --ip 192.168.0.7
```

| URL | Who |
|---|---|
| `/scorer` | quizmaster laptop (any number of co-scorers; PIN printed at startup, default `pyaar`) |
| `/welcome` | projector, before the start: Wi-Fi name, join QR, "n of N teams joined" |
| `/screen` | projector, during the quiz (click once on it to allow sound/video control) |
| `/board` | projector, scoreboard; more than 20 teams rotate through pages of 20 |
| `/j` | team phones (the printed slip's QR pre-fills the team number) |

Setup order: **TEAMS** (create N teams, print join slips) -> **QUESTIONS**
(import the CSV) -> **SETUP** (Wi-Fi name, toggles) -> **RUN**.

Tests: `python3 tests/test_flow.py`, `python3 tests/test_qr.py`,
and a 100-phone simulation: `python3 tests/loadtest.py --teams 100 --flaky 0.05`.

## Marking: how answers are graded

A decision attaches to an **answer**, not to a team. Everyone who wrote the
same thing is scored by the same decision.

1. **Automatic, full marks, only on a normalised exact match** with the
   official answer or an *Also Accepted* alternate. Normalisation ignores
   case, accents, punctuation, spacing, a leading *the/a/an*, `&` vs `and`
   and number words (`seven` = `7`). `The Ganges`, `ganges` and `Gan-ges`
   are one answer.
2. **Never automatic: anything merely similar.** A fuzzy score cannot be both
   safe and complete: `1947`/`1948`, `Serbia`/`Siberia`, `Hindu`/`Hindi` are
   one edit apart and different answers. Similar answers are *suggested* to the
   reviewer ("close to Jaipur, 92%", "contains Gandhi") and sorted to the top,
   never scored for you. A different number is never suggested.
3. **Review (REVIEW tab).** Unreviewed answers are grouped, so 40 teams who
   wrote the same wrong thing are one row. One click (or `C` / `H` / `W`) marks
   the group correct, half or wrong, with any fraction available. Deciding
   *correct* is also what makes later identical answers score automatically.
   *Rest of this question = wrong* finishes a question in one click.
4. Blank answers score 0 and never appear in review.

Anything not yet reviewed counts as 0 and is flagged on the REVIEW tab and in
RUN before you reveal.

## Built for 50-100 teams on one router

**Backend**

* One **writer thread with group commit**: concurrent saves are committed in
  one transaction with `synchronous=FULL`, so an acknowledgement to a phone
  means the answer is on disk, and 100 simultaneous saves cost a few fsyncs.
  Readers use their own WAL connections and never block it.
* **Idempotent, ordered saves.** Each edit carries a revision. A retry after a
  lost reply is accepted as a no-op; an older edit that arrives late can never
  overwrite a newer one.
* **Bounded load per phone:** batched saves, a per-team rate limit with
  `429` back-off, small body limits, a cap on event-stream connections.
* **Phone frames are identical for everyone** and serialised once per change,
  not once per phone. Anything specific to a team (its answers) is fetched on
  demand, so there is no per-team fan-out.
* Projector and scorer updates caused by saves are **coalesced** (at most
  about 2 a second) instead of one per answer.
* CSV backups are debounced and atomic; `backups/journal.csv` gets one
  appended line per accepted change.
* Socket tuning: listen backlog 512 (the default of 5 drops phones that join
  in the same second), `TCP_NODELAY`, idle keep-alive sockets reaped after
  30 s, slow event-stream clients dropped after 20 s so threads cannot pile up.

**Phones**

* Every keystroke goes to `localStorage` first and the server second. A dropped
  connection, a reload or a locked screen loses nothing: unsent edits are
  retried with exponential back-off and jitter, and the header shows
  **ALL SAVED / SAVING / RETRYING / NOT SAVED** at all times.
* Saves are debounced with random jitter (so 100 phones do not save in
  lockstep), flushed when you leave the field, and sent with `keepalive` when
  the page is hidden.
* If the event stream stalls, a tiny poll takes over, then the stream is
  rebuilt after a random delay, so a Wi-Fi blip does not make 100 phones
  reconnect in the same instant.
* The countdown runs against an estimate of the **server clock**, so every
  phone agrees. When Pass 3 ends, an edit typed before the lock that arrives up
  to 10 s late is still accepted (the phone's server-time stamp proves it
  predates the lock); anything typed after is refused.
* Only the current question's text (a few hundred bytes) moves in passes 1 and
  2. The full list is fetched once for Pass 3, gzip-compressed and cached.
  Pages are served gzip-compressed with ETags, and the phone pages use a tiny
  icon rather than a 300 KB logo.

**Scorer**

* The RUN tab shows all teams as a tile grid (online / offline / answered), the
  REVIEW tab groups answers, and TEAMS / SCORES have search. Nothing renders
  one row per team per question.

### Router checklist (single offline 5 GHz router)

* Put the laptop on **Ethernet**, not Wi-Fi, if you can: it is the server for
  every phone. Give it a DHCP reservation so the join QR never changes.
* Use a **5 GHz-only network** for the quiz (a separate SSID from 2.4 GHz),
  on a clean channel. Keep AP/client isolation **off**.
* Check the router's **client limit and DHCP pool**: you need 100+ leases
  (150 is safe) with a long lease time.
* A consumer router with 100 phones is the real bottleneck, not the server.
  If it struggles, add a **second access point** (wired to the first, same
  SSID, different channel) before blaming the software.
* The Wi-Fi has no internet. Tell teams to **turn mobile data off** and to
  tap *Stay connected* if the phone warns that the network has no internet,
  or it may quietly switch to the cellular network.
* Keep pictures small (under ~150 KB) if you turn on *media on phones*. Audio
  and video are best left to the projector. The QUESTIONS tab flags large files.
* Rehearse with the real number of phones. `tests/loadtest.py` shows the
  server side; only a rehearsal shows the Wi-Fi side.

## Question CSV

`question-template.csv` (also downloadable in the QUESTIONS tab). Columns are
matched by name, in any order:

`Question Number · Question · Answer · Also Accepted · Points · Image URL · Notes`

`Also Accepted` is separated with `|`. Files named `5.jpg`, `5_1.png` or
`5.mp3` in `media/` attach to question 5 automatically; pictures for the
answer slide go in `media/answers/` with the same names.

## Layout

```
server.py            HTTP server, app wiring, background threads
wiz/engine.py        phases, save rules, grading, scoring
wiz/db.py            SQLite, group-commit writer
wiz/hub.py           event-stream fan-out
wiz/api.py           frames, routes, CSV import/export
wiz/text.py          answer normalisation and suggestions
wiz/backup.py        journal + debounced CSV backups
static/              scorer, team, screen, board, welcome, join
tests/               unit/integration tests and the load test
```

Data lives in `data/wiz.db` (one event = one file) with CSV copies in
`data/backups/`; a clear or reset archives a timestamped copy first.

## TODO

**Requested**

- [ ] **Scorer authentication.** Today the default PIN is in the
  source (`wiz/db.py`), the PIN is stored in plain text in `data/wiz.db`, and
  one shared PIN gives full control, so anyone who has read the repo can sign
  in to a scorer page on the quiz Wi-Fi. Ideas, roughly in order of value:
  - No default PIN: generate a random PIN on first run, print it in the
    terminal, and store only a salted hash.
  - Per-person scorer access instead of one shared PIN: the laptop operator
    approves each new scorer device from a "pending devices" list, can see who
    is signed in, and can revoke one device without changing anything for the
    rest.
  - Serve the admin API and the destructive actions (clear, master reset,
    change PIN) to `localhost` only unless a device has been approved.
  - Bind the token to the device and give it an expiry, rather than deriving
    it from the PIN.
- [ ] **Team off-screen alert and tracker.** Tell the scorer when a team's
  phone leaves the quiz page (tab switch, app switch, screen lock) and keep a
  per-team record of it.
  - The phone reports page visibility and sends a light heartbeat only while
    the page is in front (every ~15 s, not every 2 s, to protect the Wi-Fi).
    The server decides "away" from silence, since a closing tab cannot be
    trusted to say goodbye.
  - Scorer: a banner for teams currently away, and a per-team count and total
    time away, split by phase (passes 1-3 matter, the lobby does not).
  - Evidence, not proof: a Wi-Fi drop looks the same as a tab switch, so
    show it as a signal and let the quizmaster decide.
  - Keep it optional (a SETUP toggle), as it is the main extra load on the
    network.

**Suggested improvements**

- [ ] **Rehearsal / dry-run mode** that fills the quiz with simulated teams
  and answers, so the quizmaster can practise the flow and the review screen
  without touching a real event. Reuse `tests/loadtest.py`.
- [ ] **Undo for marking.** Marking decisions are reversible one by one
  (clear), but there is no "undo my last 10 clicks". An undo stack for
  grading would make fast keyboard review safer.
- [ ] **Tie-breaks:** a tie-break question, or rank ties by earliest correct
  answers (the answer history already has the timestamps).
- [ ] **Resume a team without a PIN** when a phone dies, by letting the
  scorer hand over the session from the TEAMS tab with a one-time code. Today
  RESET issues a new PIN, which the team must retype.
- [ ] **Smarter review suggestions:** suggest accepting a recurring
  misspelling for *all* questions of the same type, and flag answers that
  contain the official answer inside a longer sentence.
- [ ] **Answer-slide polish:** show each answer's explanation or source line
  (a new CSV column) on the projector after the answer is shown.
- [ ] **Offline-first phone page** (a service worker that caches the page
  shell), so reopening the page after a lost connection or a browser restart
  does not need the server to load.
- [ ] **Live network health view** in the scorer: phones with failed or
  retrying saves, slowest teams, and a warning when many phones reconnect at
  once. Phones already know their own save state and could report it.
- [ ] **Accessibility:** larger text option on phones, high-contrast
  projector theme, and a keyboard-only path through the scorer.
- [ ] **Automated browser tests** for the phone, projector and scorer pages
  (currently only the server side is covered by tests).
- [ ] **Post-event report:** a printable results page with per-question
  accuracy and the most common wrong answers, to help write the next quiz.
- [ ] **Real-network rehearsal checklist** (phones, router, channel, client
  limit) kept next to the load test, with the numbers from each rehearsal.
- [ ] **Top scorers**
- [ ] **Allow teams to name themselves**
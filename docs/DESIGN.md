# Design notes

How the tracker works and the rules that keep it honest. Every threshold, window, cutover and
cost figure lives in `tracker/routes.py` (defaults) and the routes config; bag and meal figures
live in `tracker/bag_fees.py`. This page explains them and does not restate them.

## What it tracks

A list of series from the routes config. Each series is one city pair and one trip type, and one-way
and return are separate series: on full-service carriers a return is sometimes *cheaper* than the
one-way, so the two are different products with different histories. A reverse leg (someone flying
*to* KUL) is its own one-way series and is never folded into a return. 1 adult, economy, up to 1 stop.

The production run tracked seven series over three city pairs: one fixed-date real trip (one-way,
7-night return and the reverse leg) and two "watching" pairs, KUL-HKG and KUL-CAN, each one-way plus
7-night return. The measurements below come from that run.

## How a day's reading is taken

Each run prices every departure date in the route's window and files the cheapest *sensible* fare
found anywhere in it as the day's reading (`daily_best`, one row per day / series / source / basis).
Percentiles are computed against `daily_best`, not the raw `quotes` table, whose spread mostly
describes how much departure dates differ from each other rather than whether today is cheap.

Two window shapes, chosen per route:

| Kind | Window | Step |
|---|---|---|
| Real trip | fixed calendar dates (`window_dates`) | 1 day |
| Watching | rolling 20–270 days from today | 7 days |

### Why a real trip gets FIXED dates (the bug this replaced)

Until 6-Sep-2026 every route used one rolling 60–180 day grid re-anchored to today. That sounds
neutral and is not: **it silently compares different travel dates on different days.** On 22-Jul it
priced departures 5-Nov–5-Mar; by 2-Sep it priced 1-Nov–1-Mar. Measured on `KUL-CAN-RT`, the cheapest
reading moved from RM827 (depart 4-Oct) to RM605 (depart 28-Feb), an apparent RM222 saving that was
entirely the window drifting out of the Christmas peak into a dead February.

Every rank built on that series inherited the error, which is why top-3, top-2 and top-1 all fired on
nearly the same days (**32 / 32 / 29 over the same 46-day history**): in a series that only slides one
way, almost every reading is a new record. The symptom that exposed it was the message itself: prices
kept "dropping" for no market reason.

Fixed dates fix it outright: the same departure dates are priced every day, so a price change is a
price change. Watching routes keep a rolling window (there is no trip to fix them to), but at 20–270
days it spans a full year of seasonality and can no longer drift from one season into another. It does
**not** fully fix them (one "cheapest today" over ~36 dates still lets a February fare beat a December
one), which is why they open with 👀 and not ✈️.

A fixed grid is only fixed until `today + MIN_LEAD_DAYS` passes its start; after that it loses a date a
day from the front and the daily minimum drifts upward from arithmetic. I did not give that a rolling
cutover, because it would leave the route permanently in warm-up. The booking decision should fall
before the contraction starts.

### Changing a window REQUIRES a history cutover

`daily_best` is a **minimum over that day's grid**, so the grid defines what the number means. A wider
grid is a superset of a narrower one, so its minimum can only be lower or equal. Widen without a
cutover and every route steps down on its first run, gets ranked against readings of a narrower
product, and pushes *"lowest of 47 daily checks"* for a record that never happened. Same defect as the
sliding window, except it **fires** alerts instead of suppressing them. A first attempt at the 6-Sep
widening did exactly this and was caught at review. Measured before deciding: the 60–89 day bucket was
the **cheapest** of the four held (median RM1,067 vs 1,193 / 1,222 / 1,095), and widening added nearer
dates, so a step-down was likely, not merely possible.

So every route carries `history_since`, and `flight_db.history()` excludes everything before it. The
cost is honest and temporary: 46 readings discarded, and every route printed the warm-up branch until it
had 21 of its own (~3 weeks). Losing 46 readings was the cheaper mistake.

`flight_alert.py --verify-cutover` checks that post-cutover readings were collected on the current
grid. It returns **0** proven clean, **1** stale readings found, **2** not proven. Before the first
post-change collection it can only return 2, and that is the honest answer, not a pass. On 23-Sep-2026
it returned 0 over 119 post-cutover day-series.

### Cheapest *sensible*, not cheapest

`DIRECT_PREFERENCE` (15%) requires a connecting itinerary to beat the best direct by a clear margin
before it can be selected. The first sweep found Scoot at RM554 beating a direct AirAsia at RM575: RM21
saved for a **21-hour layover**. This is applied when **selecting** the fare, never only when formatting
the message; filtering just the display would leave the alert quoting one price while the history was
built from a different, cheaper one.

It is a live trade-off, not a safe default. At 15% it dropped Royal Brunei KUL→HKG (RM659, 16h20,
1 stop) in favour of a direct AirAsia at RM767, a **14%** saving and a near-miss.

## How "cheap" is decided

A fare must be **rare AND materially cheap**, both gates, never either: in the bottom
`PERCENTILE_TRIGGER` (20%) of the series' own history *and* at or below `MAX_PCT_OF_TYPICAL` (0.95) of
that history's median. Then a 3-day cooldown per route, which a further 8% drop overrides.

**Why the second gate exists (22-Aug-2026).** The first version was a rank test alone. A rank test fires
on a fixed share of days forever, so the alert rate was set by the threshold rather than by the market.
Measured against the live database, **11 of the 25 alerts sent 1-Aug→22-Aug saved only RM8–RM38**; the
worst pushed "cheaper than 83%" for RM8 off a RM1,120 fare. On a tight series the bottom 20% sat at 99%
of typical.

Measured per alert on the advertised fare, the two populations separated cleanly:

| Group | % of typical | Saved |
|---|---|---|
| real deals | 77.9% – 89.6% | RM70 – RM131 |
| noise | 96.5% – 99.3% | RM8 – RM38 |

Measured per basis (which is how `assess` applies the gate), the gap has an occupant: `KUL-HKG-OW`
with-bag fired at 91.7% of its own median, a real deal. The nearest suppressed per-basis instance was
95.9%. So 0.95 clears 91.7 with room and has ~0.9pp of margin on the noise side. 0.97 would readmit 7 of
the 16 suppressed alerts. The full ladder is in the comment above `MAX_PCT_OF_TYPICAL`.

**Known and accepted consequence:** on the history as it stood, 7 of the 14 series-bases could no longer
fire at the lows they had actually shown. Two whole series, the real trip's return and its reverse leg,
could not alert on either basis at their observed levels. The measure is the low against its own median:
their best day ever sat within ~3.5% of typical, so there was no material deal in them to find. Their
wider peak-to-trough came from their *expensive* days, which is nothing to alert on.

These figures were measured on the 46 pre-cutover readings, drawn from the sliding window. They are
why the second gate exists; they are not a basis for the next tuning decision, which needs a re-measure
on post-cutover data.

**A series whose price has never moved stays silent**, and the log says so. Zero variance carries no
information about whether today is a good day to buy. A flat series that breaks *below* its shelf is no
longer flat and stays eligible.

**Two bases.** Every route is judged on the fare as advertised (`nobag`) and a full-service-comparable
basis (`bag20`): fare + 20kg checked bag + one pre-book meal per leg (meals added 4-Aug-2026, for
fairness to budget carriers). Either basis being cheap fires the alert and the message always shows both.
The cooldown is keyed on the route, so this never doubles the message count. The two bases are
**separate minima over the same grid**: adding a bag can change which date and carrier is cheapest
(on 5-Sep-2026 on KUL⇄HKG: 13 Jan without a bag, 17 Feb with one).

The honesty rules:

- **Below `MIN_HISTORY_DAYS` (21) readings there is no percentile.** Until then the alerter falls back to
  Google's own low/typical/high verdict; if that is missing it stays silent rather than rounding "we don't
  know yet" up to "cheap".
- **The warm-up verdict is never turned into a percentage.** It is a three-way call; "cheaper than 84%"
  would manufacture precision.
- **The comparison span is generated from the real history** (`_span_phrase`): "the past 3 weeks" early,
  "the past year" only once that is true.
- **Budget fares exclude checked bags** and the message says so rather than normalising the fee away.

## Sources

**Google Flights is the only source.** It covers AirAsia and the other budget carriers that dominate KUL
routes, prices the full grid, and supplies the warm-up verdict. `daily_best` still keys rows by `source` so
a second source can never be blended into one series: their minima would come from different sample sizes,
and a source outage would look like a price crash.

`gflights.py` has no third-party dependencies. It hand-builds the protobuf `tfs` search filter (~40 lines
of varint encoding) and reads results from **aria-label** text rather than Google's minified CSS class
names, which churn. Two assertions guard the silent-corruption cases: the round-trip/one-way wording must
match what was asked for, and prices are only accepted from a label reading "Malaysian ringgit".

### Rejected: Amadeus

Amadeus' Flight Offers Search and 1-year price quartiles were meant to be the warm-up baseline. It never
ran: the developer account registration could not be completed (24-Jul-2026), so `configured()` was False
on every run. Measured before removal on 6-Sep-2026: `daily_best` 644 rows, 100% `source='google'`;
`price_metrics` 0 rows.

It was not harmless dead code. `assess()` had a branch that fed `typical` from an Amadeus year-long,
all-dates median into the field the message renders as **"normal day"**, which everywhere else means the
median of our own readings: two quantities under one word, one config file away from the phone. The code
is kept in `tracker/retired/` with revival notes.

Google's own verdict turned out to be a better fit, not just a cheaper one. Amadeus is GDS-only and largely
misses AirAsia; judging an AirAsia fare against a baseline computed without AirAsia reads artificially cheap
and biases toward false alerts. Probe before building (KUL→HKG, 8 dates, +30d to +300d): **5 TYPICAL,
3 HIGH**, present on 7/7 series. The verdict is **date-relative**: RM554 read HIGH while RM565 read TYPICAL,
because each departure date is judged against its own seasonal history. So it is stored per departure date
on the winning fare, and it judges only the no-bag fare. `low` was not seen in that probe; the trigger path
for it is proven by test, not by a live sighting.

## The message

The layout went through three rounds (twice on 22-Jul-2026, rewritten 6-Sep-2026). The rewrite came from
one piece of feedback: the message was confusing, and it never said what it was tracking or what "trend"
meant. The old version showed two percentages, `74% of typical` and `cheaper than 92%`, and never said what
either was measured against. At 34 readings one day is worth 2.9 points, so "92%" meant "beat 31 of 34"
while sounding like it came from hundreds, and both were computed on the sliding-window series.

Rules that came out of it:

1. **The opener says whether this is a real trip or a watch.** `✈️ <place> — cheap right now` versus
   `👀 Hong Kong — watching`.
2. **Every number states its own scope in words.** `RM 805 · both legs`, never a bare figure or a `⇄` glyph
   left to carry the meaning.
3. **One line per idea.** Joined with `·` the anchors ran to 58 characters and wrapped.
4. **"Normal day" is the MEDIAN and the message says so.** One freak RM3,000 reading would drag an average
   up and make every ordinary day look like a bargain.
5. **"Previous best", never "best ever", with its date.** The low excludes today, so on a record day "best
   ever RM 520" sat above "lowest of 34" while today was RM 500. The date matters because "3 weeks ago" and
   "yesterday" say opposite things about whether to wait.
6. **An ordinal rank instead of a percentage.** "2nd lowest of 34 daily checks" carries its own sample size
   and stays honest at 200.
7. **Ties are honest at every rank.** Rank counts readings strictly below today, so a fare matching a
   standing low is `joint-lowest`. This retired the `CHEAPEST EVER` label: a fare parked at RM602 for 32
   readings claimed the superlative every 3 days as the cooldown lapsed.
8. **Warn that today's cheapest may be a different departure date from yesterday's.**
9. **Line width is a correctness property.** Every closing-block line is ≤ 46 characters. One reached 67 and
   the phone broke it mid-number (`RM 1,3` / `80 all-in`).
10. **Bag status is one line**, never a label plus a caveat saying the same thing.
11. **IATA codes in the route label, plain place names in the opener**; a `'yy` suffix only when a date
    crosses into another year.
12. **No provenance lines.** `Today: N fares over M dates` was cut: it answered "should I trust this", not
    "should I book".

A real render (`KUL-HKG-RT`, 5-Sep-2026 data):

```
👀 Hong Kong — watching
KUL ⇄ HKG · return · 7 nights

RM 805 · both legs
  normal day RM 1,047
  previous best RM 805, 2 days ago
  joint-lowest of 46 daily checks
AirAsia | (Wed) 13 Jan '27 → (Wed) 20 Jan '27 · 1:20 PM 4h10 · Direct
No checked bag

RM 938 · both legs
  normal day RM 1,300
  previous best RM 937, 3 days ago
  joint-2nd lowest of 46 daily checks
Malaysia Airlines | (Wed) 17 Feb '27 → (Wed) 24 Feb '27 · 7:50 PM 4h00 · Direct
With 20kg bag + meals (included in this fare)

WHAT THIS IS COMPARING
We price this route once a day and keep the
cheapest. 46 checks over the past 7 weeks.
"Normal day" is the MIDDLE of those readings —
half were dearer, half cheaper:
  RM 1,047 no bag
  RM 1,300 all-in
Today's cheapest may be a different departure
date from yesterday's.

Also worth checking (bag varies by fare):
Batik Air RM1,449 · (Wed) 18 Nov
Cebu Pacific RM1,589 · (Wed) 3 Feb '27

Google Flights, checked 17:45 MYT.
```

During warm-up the closing block instead says it holds only N of 21 days, that this is Google's call and
not ours, and that the verdict is about the no-bag fare only.

## Checked bags

**This is not a live lookup.** Google's search filter has no reachable checked-bag field: every plausible
protobuf field 1–45 was tested with a falsifiable signature (a real bag filter must push budget carriers
*up* while full-service stays *flat*) and every valid one came back inert. Budget carriers price bags
dynamically per flight, so no static source exists either. `bag_fees.py` holds an **assumption** with four
confidence levels kept apart:

| Level | Meaning | Example |
|---|---|---|
| `included` | 20kg comes with the economy fare; a fact | Malaysia Airlines, Cathay, China Southern |
| `estimate` | a sourced figure that still varies by flight | AirAsia, Scoot |
| `ambiguous` | sells both bag-inclusive and bag-free families | Batik Air |
| `unknown` | no source; never priced, never assumed free | Spring, Vietjet, Cebu Pacific |

An `ambiguous` or `unknown` fare is excluded from the with-bag winner but still **named with its date** in the
message, since it may be the best deal on offer. On the first real sweep the with-bag winner was a
**different airline on 4 of 7 series**, so this matters. To correct a figure: edit `bag_fees.py`, then run
`migrate_bags.py` to rebuild `daily_best` from `quotes`.

## Schedule and delivery

| Cron (UTC) | Local (MYT) | Job |
|---|---|---|
| `15 8 * * *` | 16:15 | `collect_flights.py`, full sweep |
| `45 9 * * *` | 17:45 | `flight_alert.py`, evaluate and push |

Measured cost: 3.63–3.79 s per date-lookup; 327 lookups took ~20 minutes. The alerter was moved from 09:00
to 09:45 UTC when the grid roughly tripled, giving a 90-minute gap. It **aborts** if the day's `runs` row has
no `finished_at` instead of evaluating a half-collected day. If Google starts rate-limiting, the first move
is step 2 on the fixed-date routes, which is a grid change and needs its own cutover.

Changing the **collection** time is not free: a consistent sampling hour is part of what makes the series
comparable day to day.

Delivery goes through `tracker/notify.py` (Telegram, then ntfy; credentials from env vars). Messages go out
only 16:00–23:59 local time. Anything generated outside that window is queued, never dropped, and delivered
by `notify.py --flush` or the next in-window push.

**Health.** In production an external hourly health check (not in this repo) watched four things, and the
code writes what it needs:

- the alerter finished: silence is this job's healthy state, so a crashing alerter looks like a quiet
  market. The log tail must end in the `done — N alert(s) sent` sentinel, which every clean run prints,
  including runs that send nothing. The abort line deliberately does not match it.
- the collector: read from the `runs` table, not log mtime (a crashed run still appends to the log).
- a fixed-date trip window closing: a route past its dates is skipped, so it counts as neither ok nor
  failed and every other number stays green. It is a separate key on purpose: folded into the collector
  check it would sit red every day and mask every later fault. The collector writes plain place names into
  `runs.note`, never route keys, because the note reaches the phone.
- the Google verdict still parsing: one query missing it is normal; every route missing it on one day is a
  broken parser.

Not watched yet: **intermittent low coverage.** A run that prices fewer dates than its grid suppresses on
the day (a minimum over fewer dates can only rise), but the inflated reading joins the median, so the next
clean day reads cheaper than it is. Intermittent shortfalls push the trigger in the firing direction, one day
late. The collector logs `WARNING low coverage` below 50%, but the run still completes as ok.

## Gotchas found while building this

1. **Printing ✈️/👀/`·` under cron is an encoding hazard.** Run the scripts under `env -i` after any new
   glyph to catch a `UnicodeEncodeError` with no `LANG` set.
2. **Google intermittently returns HTTP 200 with a full page and no price labels**, and the identical request
   succeeds seconds later. Retrying only on exceptions misses it, and the date silently drops from the grid.
   `gflights.search(retry_empty=2)` handles it.
3. **A genuinely unpriced far date is not a failure** (airlines have not loaded fares on every pairing
   months out). The coverage warning separates that from a parser break.
4. **The string `captcha` appears in Google's JS bundle on healthy pages.** Not a block signal.
5. **`upsert_daily_best` only overwrites on a strictly lower price.** Right during a day's collection, but it
   made a rebuild a silent no-op until `migrate_bags.py` was made to `DELETE` first.
6. **Carrier names must match Google's spelling**: `THAI`, and combined itineraries like
   `EVA Air and Air Macau`. Budget carriers are matched first, because one bag-charging operator anywhere in
   the chain makes the whole fare bag-excluding.
7. **A new column in `SCHEMA` does not reach an existing database** (`CREATE TABLE IF NOT EXISTS` is a
   no-op). `_add_missing_columns()` carries the forward migration; declare new columns last so migrated and
   fresh databases match.
8. **The Google verdict is date-relative** and cannot be compared across dates.
9. **`percentile_of` counts readings strictly cheaper than today, and its complement is a different
   quantity.** `100 - percentile_of(...)` counts every tie as a day today beat; on a flat series that read
   100 and rendered "CHEAPEST EVER" about a constant. `rank` also counts strictly-below. Do not simplify
   either into a complement.
10. **Don't reintroduce a superlative.** A real record states its own scope ("lowest of N checks").
11. **The `alerts` table stores half of a send's reasoning**: its `percentile` is the rank gate only, and
    `price` is always the advertised fare even when the basis was `bag20`. The log line carries both.
12. **A window change without a `history_since` cutover corrupts every rank**, in the direction that fires.
13. **A closed travel window is invisible in `ok_series` / `failed_series`** (see Health).
14. **The alerter must never push off a half-collected day, and exemptions are per route, not per run.**
    A cron run aborts; `--day` and `--preview` warn and cannot send; `--force` renders only the routes it
    was given and sends nothing. The first version exempted the whole run whenever `--force` named one route,
    so the others pushed live off partial data; a later version pushed live on a healthy day for every route
    it was not given (measured: 2 real pushes). Both were caught at review. `--test-push` is the one flag that
    deliberately reaches the phone, and it writes no `alerts` row so it cannot trigger a later cooldown.

## Deploying safely

- Render before sending. Use `--force <keys> --day <last collected day>`, not `--preview`: during warm-up
  nothing triggers, so `--preview` composes zero messages, and a bare `--force` in the morning has no
  reading for today. If the alerter prints `RENDERED NOTHING` and exits 2, the layout step did not run.
- Run `--verify-cutover` after any window change, and again after the first collection that follows it.
- Editing crontab with `crontab -l | sed ... | crontab -` is dangerous: an empty stream installs an empty
  crontab and stops every job, including the health check. Back up to a file, check it is non-empty, and
  feed `sed` the file.

# Maintenance Action Center: Maple Freight

**User:** Maintenance Lead. **Question:** what does the maintenance team need to act on tonight?
**Output:** at most 10 ranked actions (the Ops Manager's daily capacity), each with priority, reason,
next action, score breakdown and evidence that cites the source row (`file:id`).

## Run
```bash
# backend
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && python manage.py runserver      # :8000
python manage.py test core                                          # 18 tests
# frontend
cd frontend && npm install && npm run dev                           # http://localhost:5173
```
Data is read from `data/` (override with `MAPLE_DATA_DIR`). No database is used: the CSVs are the source of truth.

## Top 3 tonight (as of 2026-09-21, data received through 18:47 EDT)

| # | Vehicle | Evidence | Next action |
|---|---|---|---|
| 1 | **V-137** Critical | Coolant temp rose 104 → 109 → 113 → 118 °C (Sept 17–20). WO-5540 replaced the sensor and cleared it "OK to dispatch" at 07:40 on Sept 21. At 14:32 it threw **P0524 low oil pressure (96 kPa)**: reference says *Critical, stop immediately*, while the vendor labelled it MEDIUM. | Do not dispatch. Inspect the oil system. Re-open WO-5540. |
| 2 | **V-108** Critical | Backfill: **P0217 over-temp 107 °C** at 12:05 EDT, still running (57 min idling) at 13:09, then **P0524 oil pressure 88 kPa** at 15:41. Both are Critical in the reference, both labelled MEDIUM by the vendor. No work order. This was not visible until 18:47. | Do not dispatch. Inspect the cooling and oil systems tonight. |
| 3 | **V-151** High | P0300 misfire on Sept 20 with no work order. **2,350 km overdue** for service (4.7× any other vehicle). Service A (WO-5541) isn't booked until Oct 5. | Inspect the misfire before next dispatch and pull Service A forward. |

The other seven: V-126, V-113, V-131, V-111 (misfires with no work order, still being driven), V-158 (misfire, booked Sept 22), V-157, V-147 (lean code plus overdue).

## How the ranking works (deterministic, `backend/core/analysis.py`)
- **Urgency comes from `dtc_reference.csv`, not vendor severity:** Critical 100, High 60, Medium 25, Low 0.
- +10 for each additional unresolved Medium+ code. +5 per repeat of the same code (max +15).
- **+40 if the repair didn't hold:** a High/Critical fault starting within 72 h of a closed work order.
- +10 if the vehicle was driven after a High/Critical fault.
- Overdue maintenance: +40 for ≥1,000 km, +15 for ≥300 km, +5 for >0 km.
- **−20 if a covering work order is booked within 48 h.** A booking lowers the score but doesn't remove the action, and a booking further out doesn't count.
- Priority follows reference urgency (Critical/High), otherwise score ≥60 → High, else Medium.
- **Excluded, with reasons shown:**
  - Parked vehicles (V-102: telematics unit removed, not dispatched).
  - Faults resolved by a closed work order with no recurrence afterwards.
  - Low-urgency codes (P0420, P0128).
  - Anything below the 10/day cutoff.

## Assumptions
- Local time = EDT (UTC−4). Every `maintenance_due` alert is at 10:00Z, and the vendor says they fire at 06:00 local.
- "Tonight" = the evening of Sept 21. The data ends at 17:27 EDT, and the backfill arrived at 18:47 EDT.
- The latest `maintenance_due` reading per vehicle is current. Positive values mean km overdue (per vendor notes).
- A work order "covers" a code if its text names the code or a matching keyword (e.g. "misfire" → P0300).

## What the data contradicted
- **Vendor severity does not follow DTC urgency.**
  - Both Critical codes are labelled MEDIUM.
  - 31 Low-urgency P0420 emissions alerts are labelled HIGH.
  - Filtering on vendor-HIGH engine faults (the current practice) would miss both #1 and #2.
- **"Each alert is delivered exactly once"** is false: there are 134 exact duplicates (5.7%), removed before scoring.
- **"D-17 is the worst driver":** D-17 is #1 by raw count but **#20 of 60** by severe events per 1,000 km. They drive the highest-km vehicle, which runs firmware 4.2.1. That firmware produces about 6× more harsh-braking alerts, 87% of them at 0.40–0.44 g.
- **"V-137 was a bad sensor, fixed this morning":** possibly true for the sensor, but the vehicle threw a Critical oil-pressure code the same afternoon.

## What changed after the new information

**Input:** `hamilton_backfill.csv`, 10 alerts held by Hamilton Depot's gateway and delivered at 18:47 EDT, 1.4–6.7 h after the events.

**How we incorporated it:**
- The backfill was merged with `alerts.csv` into one alert stream rather than appended to the results.
- Dedupe, resolution, "driven since", scoring and ranking were all re-run on that stream, ordered by **event time**.
- Each evidence row keeps both `event_time` and `received_time`. Late rows are flagged in the API (`late_arrival`, `has_late_evidence`, a late-batch `data_quality` item) and in the UI (banner, "late data" badge, Received column).

**What changed:**
1. **V-108 went from not ranked to #2 Critical.**
   - Before, its only fault (P0171) had been fixed by WO-5512, so it was listed as resolved.
   - The backfill shows two *new* Critical codes the same afternoon (over-temp 107 °C, then oil pressure 88 kPa), with the vehicle still running in between.
   - The old P0171 fix still stands. It's now shown as history on V-108's card, not as an exclusion.
   - V-108 no longer appears in the excluded list.
2. **The top 3 changed** from V-137, V-151, V-126 to **V-137, V-108, V-151**. V-126 moves to #4.
3. **V-124** got a new P0171 (lean). It now scores 40 but ranks #12, below the cutoff.
4. **V-126, V-113, V-131** each gained one more driving alert since their unresolved misfire. That reinforces "still being dispatched" but doesn't change their scores.
   - V-126 was recorded at 104 km/h in a 60 zone.
   - V-131 was recorded at 131 km/h with no posted limit.
5. **No change** for V-135 and V-145, which only had driving events.

**Why V-108 is #2, not #1:** both V-137 and V-108 are Critical, and the action is the same for both: don't dispatch tonight. V-137 scores higher (140 vs 120) because its fault appeared 7 h after a repair cleared it for dispatch (+40). We kept the formula unchanged rather than tune it to get a preferred order.

**What Maple Freight should do differently tonight:**
- Pull V-108 tonight alongside V-137.
- Treat the vendor's statement that *"all depots report normal gateway connectivity"* as wrong for Hamilton. Hamilton vehicles may still have events we haven't received, so a quiet Hamilton vehicle isn't proof of a healthy one.

## Where AI is used (and where it isn't)

**The ranking is deterministic on purpose.**
- A maintenance decision ("don't dispatch V-137") has to be reproducible, auditable back to a CSV row, and changeable by editing a visible rule.
- An LLM ranking would be none of those things.
- The data also shows the trap: the vendor's own automated severity mislabels both Critical codes.
- So scores, priorities, order, exclusions and recommended actions all come from `core/analysis.py`, with no model involved.

**AI is used in exactly one place: the "AI Shift Brief"** (`GET /api/brief`, `core/brief.py`, with the provider call in `core/services/ai.py`).
- After ranking, only the **Top 3's structured facts** go to the model: vehicle, priority, reason, recommended action, and evidence details.
- The model returns `{"brief": "..."}` with a 2–4 sentence summary for the shift.
- The UI labels it **"AI-generated shift brief"**.

**How the output is constrained and verified (`brief.validate`).** The brief is rejected if it:
- mentions any vehicle ID that isn't in the Top 3;
- mentions any DTC code or work-order ID that isn't in the supplied evidence;
- includes any number that doesn't appear in the supplied evidence (so no invented temperatures, pressures or km);
- pairs a vehicle with a priority that differs from the deterministic one (e.g. calling a High vehicle Critical);
- is empty, too long, or not 1–5 sentences.

If the brief is rejected, the API returns a **deterministic template brief** instead. The rejected text and the reasons are kept in the response for audit.

**The product works without AI.**
- `/api/actions` never calls the model.
- The brief is a separate request, so if the key, network or model fails, the cards still render, and the panel shows "Shift brief (deterministic fallback) · AI unavailable".
- The API key is read from `backend/.env` and never reaches React.
- No new dependency was added; the SDK was already in `requirements.txt`.
- Tests cover all of this: a valid AI output is used, a hallucinated vehicle/number/priority is rejected, an API failure falls back, and the Top 3 is unchanged.

**Honest limit:** the checks are lexical (IDs, codes, numbers, priority words). They don't catch a reworded causal claim, such as "the sensor fix failed because…". A production version would add claim-level checks or human sign-off.

## What we did not do
- No live feed or scheduled re-scoring. We have no authentication, persistence of decisions, or work-order write-back.
- No driver-safety ranking. The D-17 check is reported as a finding only.
- Telematics health (unplugged/camera alerts outside the update window) is reported, not ranked.
- No LLM in the ranking. The ranking, reasons and actions are all deterministic templates.

## How we checked the result
- 18 Django tests (12 ranking, 6 AI brief):
  - The top 3 order.
  - The score equals the sum of its breakdown for every action.
  - Exactly 134 duplicates removed.
  - The backfill is merged and flagged as late.
  - A parked vehicle is never an action.
  - A vehicle that is an action never appears as excluded.
  - Bookings reduce but don't remove an action.
  - Resolution cites its work order.
  - Every evidence row cites a source file.
- The diagnosis was cross-checked by hand against the raw CSVs (V-137, V-108, V-151, D-17).
- The endpoint was checked directly and through the Vite proxy. The UI was checked in a headless browser at desktop and mobile widths with no console errors.

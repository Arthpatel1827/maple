"""Deterministic maintenance ranking for Maple Freight.

Pipeline: load CSVs -> dedupe alerts -> explicit UTC/local times -> per-vehicle
findings from named rules -> score_breakdown -> ranked actions.

No LLM is involved here. Every evidence item cites its source row
(file:id) so a reviewer can audit it back to the supplied data.
"""
import csv
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(os.getenv("MAPLE_DATA_DIR", Path(__file__).resolve().parents[2] / "data"))

# ASSUMPTION (verified): depots run on Eastern Daylight Time in Sept 2026.
# Evidence: every maintenance_due alert is 10:00Z, vendor says they fire at 06:00 local.
# Fixed offset on purpose: explicit, no tzdata dependency, valid for this 14-day window.
LOCAL_TZ = timezone(timedelta(hours=-4), "EDT")

DAILY_CAPACITY = 10  # Ops Manager can act on ~10 things/day

# ---- Scoring weights (the whole formula lives here) -------------------------
URGENCY_POINTS = {"Critical": 100, "High": 60, "Medium": 25, "Low": 0}
ADDITIONAL_FAULT_POINTS = 10       # each extra unresolved Medium+ DTC
RECURRENCE_POINTS = 5              # per repeat of the top fault, capped
RECURRENCE_CAP = 15
REPAIR_DID_NOT_HOLD_POINTS = 40    # High/Critical fault within 72h of a closed WO
REPAIR_WINDOW = timedelta(hours=72)
DRIVEN_SINCE_FAULT_POINTS = 10     # High/Critical fault and vehicle still dispatched
OVERDUE_TIERS = [(1000, 40), (300, 15), (1, 5)]  # km overdue -> points
BOOKED_SOON_POINTS = -20           # covering WO booked within 48h
BOOKED_SOON_WINDOW = timedelta(hours=48)

DRIVING_TYPES = {"harsh_braking", "harsh_accel", "harsh_cornering", "speeding", "idling"}
# WO text that counts as covering a DTC (besides the code itself appearing).
DTC_KEYWORDS = {"P0300": ["misfire"], "P0217": ["overheat"], "P0171": ["lean"], "P0128": ["thermostat"]}
MAINTENANCE_KEYWORDS = ["service", "oil and filters"]
PARKED_KEYWORDS = ["not dispatched", "parked"]


# ---- Loading -----------------------------------------------------------------

def _read_csv(name):
    with open(DATA_DIR / name, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _utc(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _local_naive(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ) if s else None


def _fmt_local(dt):
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M EDT")


ALERT_FILES = ["alerts.csv", "hamilton_backfill.csv"]  # one alert stream; missing files are skipped


def load():
    raw, sources = [], {}
    for name in ALERT_FILES:
        if not (DATA_DIR / name).exists():
            continue
        rows = _read_csv(name)
        for r in rows:
            r["source_file"] = name
        sources[name] = len(rows)
        raw.extend(rows)
    # Process in event-time order so "first seen" and dedupe are file-order independent.
    raw.sort(key=lambda r: (r["event_time_utc"], r["source_file"]))
    seen, alerts, duplicates = {}, [], []
    for a in raw:
        key = (a["vehicle_id"], a["alert_type"], a["event_time_utc"], a["value"])
        if key in seen:
            duplicates.append((a["alert_id"], seen[key]))
            continue
        seen[key] = a["alert_id"]
        a["event_utc"] = _utc(a["event_time_utc"])
        a["received_utc"] = _utc(a["received_time_utc"])
        alerts.append(a)

    dtc_ref = {}
    for r in _read_csv("dtc_reference.csv"):
        r["urgency"] = r["reference_urgency"].split(" - ")[0].strip()
        dtc_ref[r["dtc_code"]] = r

    work_orders = _read_csv("work_orders.csv")
    for w in work_orders:
        w["opened"] = _local_naive(w["opened_local"])
        w["closed"] = _local_naive(w["closed_local"])
        w["text"] = f'{w["work_done"]} {w["technician_note"]}'.lower()
        m = re.search(r"(\d{4}-\d{2}-\d{2})", w["technician_note"])
        w["booked_for"] = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=LOCAL_TZ) if m else None

    vehicles = {v["vehicle_id"]: v for v in _read_csv("vehicles.csv")}
    return {"raw_count": len(raw), "alert_sources": sources, "alerts": alerts, "duplicates": duplicates,
            "dtc_ref": dtc_ref, "work_orders": work_orders, "vehicles": vehicles}


# ---- Helpers -------------------------------------------------------------------

def _ev(type_, detail, source, **extra):
    return {"type": type_, "detail": detail, "source": source, **extra}


def _alert_ev(type_, detail, a):
    lag_h = (a["received_utc"] - a["event_utc"]).total_seconds() / 3600
    return _ev(type_, detail, f'{a["source_file"]}:{a["alert_id"]}',
               event_time_utc=a["event_time_utc"], event_time_local=_fmt_local(a["event_utc"]),
               received_time_utc=a["received_time_utc"], received_time_local=_fmt_local(a["received_utc"]),
               late_arrival=lag_h >= 1)


def _wo_ev(type_, detail, w):
    return _ev(type_, detail, f'work_orders.csv:{w["wo_id"]}')


def _covers_dtc(w, code):
    return code.lower() in w["text"] or any(k in w["text"] for k in DTC_KEYWORDS.get(code, []))


def _covers_maintenance(w):
    return any(k in w["text"] for k in MAINTENANCE_KEYWORDS)


# ---- Core analysis ----------------------------------------------------------------

def analyze():
    d = load()
    alerts, dtc_ref, vehicles = d["alerts"], d["dtc_ref"], d["vehicles"]
    as_of = max(a["event_utc"] for a in alerts)

    by_vehicle = defaultdict(list)
    for a in alerts:
        by_vehicle[a["vehicle_id"]].append(a)
    for lst in by_vehicle.values():
        lst.sort(key=lambda a: a["event_utc"])
    wos_by_vehicle = defaultdict(list)
    for w in d["work_orders"]:
        wos_by_vehicle[w["vehicle_id"]].append(w)

    actions, excluded = [], []

    for vid in sorted(vehicles):
        v_alerts = by_vehicle.get(vid, [])
        v_wos = wos_by_vehicle.get(vid, [])

        # Rule: parked vehicles are not dispatch actions.
        parked = [w for w in v_wos if w["status"] == "Open" and any(k in w["text"] for k in PARKED_KEYWORDS)]
        if parked:
            w = parked[0]
            last = v_alerts[-1] if v_alerts else None
            after = [a for a in v_alerts if a["event_utc"] >= w["opened"]]
            note = (f"{len(after)} alerts after the WO opened: check whether it is really parked"
                    if after else "no alerts of any kind since the WO opened")
            excluded.append({
                "vehicle_id": vid, "item": "Vehicle", "reason": "Parked and not dispatched (open work order)",
                "evidence": [_wo_ev("work_order", f'{w["status"]}: {w["work_done"]}. {w["technician_note"]}', w)]
                + ([_alert_ev("last_alert", f'Last alert: {last["alert_type"]}; {note}', last)] if last else []),
            })
            continue

        breakdown, evidence, actions_text = [], [], []
        unresolved = []          # (urgency_points, first_alert, fault_alerts, ref)
        booked_soon_wo = None
        has_critical = has_high = False

        # Group engine faults by DTC.
        faults = defaultdict(list)
        for a in v_alerts:
            if a["alert_type"] == "engine_fault" and a["dtc_code"]:
                faults[a["dtc_code"]].append(a)

        closed_wos = [w for w in v_wos if w["status"] == "Closed" and w["closed"]]

        for code, fa in faults.items():
            ref = dtc_ref.get(code, {"urgency": "Low", "meaning": "Unknown code", "reference_urgency": "Unknown"})
            last = fa[-1]
            covering = [w for w in v_wos if _covers_dtc(w, code)]
            resolving = [w for w in covering if w["status"] == "Closed" and w["closed"] and w["closed"] >= last["event_utc"]]
            if resolving:
                w = resolving[0]
                excluded.append({
                    "vehicle_id": vid, "item": f"{code} ({ref['meaning']})",
                    "reason": "Resolved by closed work order; no recurrence of this code after closure",
                    "evidence": [_alert_ev("fault", f"Last {code} before repair", last),
                                 _wo_ev("work_order", f'Closed {w["closed_local"]}: {w["work_done"]}. {w["technician_note"]}', w)],
                })
                continue
            if ref["urgency"] == "Low":
                continue  # monitored only; summarized in data_quality
            unresolved.append((URGENCY_POINTS[ref["urgency"]], fa, ref, code, covering))

        unresolved.sort(key=lambda u: (-u[0], u[1][0]["event_utc"]))

        if unresolved:
            pts, fa, ref, code, covering = unresolved[0]
            has_critical = ref["urgency"] == "Critical"
            has_high = ref["urgency"] == "High"
            vendor_sevs = sorted({a["vendor_severity"] for a in fa})
            breakdown.append({"rule": "dtc_urgency", "points": pts,
                              "detail": f"{code}: reference urgency {ref['urgency']} (vendor severity said {'/'.join(vendor_sevs)})"})
            evidence.append(_ev("dtc_reference", f"{code} = {ref['meaning']}: {ref['reference_urgency']}", f"dtc_reference.csv:{code}"))
            for a in fa:
                val = f' value {a["value"]} {a["unit"]}' if a["value"] else ""
                evidence.append(_alert_ev("fault", f'{code}{val}, vendor severity {a["vendor_severity"]}', a))
            if not covering:
                evidence.append(_ev("no_work_order", f"No work order references {code}", "work_orders.csv"))

            # Recurrence of the top fault.
            if len(fa) > 1:
                rp = min(RECURRENCE_POINTS * (len(fa) - 1), RECURRENCE_CAP)
                breakdown.append({"rule": "recurrence", "points": rp, "detail": f"{code} seen {len(fa)} times"})

            # Additional unresolved Medium+ faults.
            for p2, fa2, ref2, code2, _ in unresolved[1:]:
                breakdown.append({"rule": "additional_fault", "points": ADDITIONAL_FAULT_POINTS,
                                  "detail": f"Also unresolved {code2} ({ref2['urgency']}: {ref2['meaning']})"})
                a2 = fa2[-1]
                val2 = f' value {a2["value"]} {a2["unit"]}' if a2["value"] else ""
                evidence.append(_alert_ev("fault", f"{code2} ({ref2['meaning']}){val2}, reference urgency {ref2['urgency']}, "
                                                   f"vendor severity {a2['vendor_severity']}", a2))

            if ref["urgency"] in ("Critical", "High"):
                first = fa[0]
                # Repair did not hold: this fault started shortly after a closed WO.
                recent_repair = [w for w in closed_wos if w["closed"] <= first["event_utc"] <= w["closed"] + REPAIR_WINDOW]
                if recent_repair:
                    w = recent_repair[-1]
                    hrs = (first["event_utc"] - w["closed"]).total_seconds() / 3600
                    breakdown.append({"rule": "repair_did_not_hold", "points": REPAIR_DID_NOT_HOLD_POINTS,
                                      "detail": f'{code} appeared {hrs:.0f}h after {w["wo_id"]} was closed'})
                    evidence.append(_wo_ev("recent_repair", f'Closed {w["closed_local"]}: {w["work_done"]}. Note: "{w["technician_note"]}"', w))
                    # Show the faults that repair claimed to fix, so the trend is visible on the card.
                    for c_prev, fa_prev in faults.items():
                        if c_prev != code and _covers_dtc(w, c_prev):
                            for a in fa_prev:
                                val = f' {a["value"]} {a["unit"]}' if a["value"] else ""
                                evidence.append(_alert_ev("prior_fault", f'{c_prev}{val} (before {w["wo_id"]})', a))
                # Still being driven after the fault.
                driven = [a for a in v_alerts if a["alert_type"] in DRIVING_TYPES and a["event_utc"] > fa[-1]["event_utc"]]
                if driven:
                    breakdown.append({"rule": "driven_since_fault", "points": DRIVEN_SINCE_FAULT_POINTS,
                                      "detail": f"{len(driven)} driving alert{'s' if len(driven) != 1 else ''} after the last {code}"})
                    evidence.append(_alert_ev("driven_since_fault", f"{len(driven)} driving alert{'s' if len(driven) != 1 else ''} since the fault; latest: {driven[-1]['alert_type']}", driven[-1]))

            # Existing booking reduces (never removes) urgency, only if it is soon.
            for w in covering:
                if w["status"] in ("Open", "Scheduled"):
                    when = w["booked_for"] or w["opened"]
                    soon = when - as_of <= BOOKED_SOON_WINDOW
                    evidence.append(_wo_ev("booking", f'{w["status"]}: {w["work_done"]}. {w["technician_note"]}', w))
                    if soon:
                        booked_soon_wo = w
                        breakdown.append({"rule": "booked_soon", "points": BOOKED_SOON_POINTS,
                                          "detail": f'{w["wo_id"]} booked for {when:%Y-%m-%d} (within 48h)'})

        # Maintenance overdue (latest reading; positive = km overdue per vendor notes).
        md = [a for a in v_alerts if a["alert_type"] == "maintenance_due" and a["value"]]
        overdue = 0
        if md:
            latest = md[-1]
            overdue = int(float(latest["value"]))
            if overdue > 0:
                pts_od = next(p for limit, p in OVERDUE_TIERS if overdue >= limit)
                breakdown.append({"rule": "maintenance_overdue", "points": pts_od, "detail": f"{overdue} km overdue"})
                evidence.append(_alert_ev("maintenance_due", f'{overdue} km overdue (vendor severity {latest["vendor_severity"]})', latest))
                maint_wos = [w for w in v_wos if _covers_maintenance(w) and w["status"] in ("Open", "Scheduled")]
                for w in maint_wos:
                    evidence.append(_wo_ev("booking", f'{w["status"]}: {w["work_done"]}. {w["technician_note"]}', w))

        score = sum(b["points"] for b in breakdown)
        if not breakdown or score <= 0:
            continue

        # Priority: driven by reference urgency, not by vendor severity.
        if has_critical:
            priority = "Critical"
        elif has_high or score >= 60:
            priority = "High"
        else:
            priority = "Medium"

        # Recommended action (deterministic templates).
        rules = {b["rule"] for b in breakdown}
        if unresolved:
            _, fa, ref, code, covering = unresolved[0]
            if ref["urgency"] == "Critical":
                crit = [r["meaning"].lower() for _, _, r, _, _ in unresolved if r["urgency"] == "Critical"]
                actions_text.append(f"Do not dispatch. Pull from service tonight and inspect: {' + '.join(crit)}.")
            elif booked_soon_wo:
                actions_text.append(f'Keep off the road until {booked_soon_wo["wo_id"]} ({booked_soon_wo["work_done"].lower()}) happens as booked.')
            elif ref["urgency"] == "High":
                actions_text.append(f"Open a work order and inspect {code} ({ref['meaning'].lower()}) before next dispatch.")
            else:
                actions_text.append(f"Inspect {code} ({ref['meaning'].lower()}) at next service.")
            if "repair_did_not_hold" in rules:
                wo = next(e["source"].split(":")[1] for e in evidence if e["type"] == "recent_repair")
                actions_text.append(f"Re-open {wo}: a new fault appeared after it was cleared.")
        if overdue > 0:
            far = [w for w in v_wos if _covers_maintenance(w) and w["status"] in ("Open", "Scheduled")
                   and w["booked_for"] and w["booked_for"] - as_of > BOOKED_SOON_WINDOW]
            if far:
                actions_text.append(f'Pull {far[0]["wo_id"]} forward from {far[0]["booked_for"]:%Y-%m-%d}; vehicle is {overdue} km overdue.')
            else:
                actions_text.append(f"Book service ({overdue} km overdue).")

        positives = [b for b in sorted(breakdown, key=lambda b: -b["points"]) if b["points"] > 0][:3]
        reason = "; ".join(b["detail"] for b in positives + [b for b in breakdown if b["points"] < 0])
        first_fault = unresolved[0][1][0]["event_utc"] if unresolved else as_of
        actions.append({
            "vehicle_id": vid,
            "depot": vehicles[vid]["depot"],
            "vehicle_class": vehicles[vid]["vehicle_class"],
            "priority": priority,
            "score": score,
            "reason": reason,
            "recommended_action": " ".join(actions_text),
            "score_breakdown": breakdown,
            "evidence": evidence,
            "_sort": (-score, first_fault, vid),
        })

    # Low-urgency DTCs are monitored, not actioned. Show what that hides, incl. vendor-HIGH labels.
    low = [a for a in alerts if a["alert_type"] == "engine_fault" and dtc_ref.get(a["dtc_code"], {}).get("urgency") == "Low"]
    for code in sorted({a["dtc_code"] for a in low}):
        lst = [a for a in low if a["dtc_code"] == code]
        excluded.append({
            "vehicle_id": f"{len({a['vehicle_id'] for a in lst})} vehicles",
            "item": f"{code} ({dtc_ref[code]['meaning']})",
            "reason": f"Reference urgency Low ({dtc_ref[code]['reference_urgency']}): monitor, not dispatch-blocking. "
                      f"{sum(a['vendor_severity'] == 'HIGH' for a in lst)} of {len(lst)} were labelled HIGH by the vendor.",
            "evidence": [_ev("dtc_reference", dtc_ref[code]["reference_urgency"], f"dtc_reference.csv:{code}")],
        })

    actions.sort(key=lambda a: a["_sort"])
    for i, a in enumerate(actions, 1):
        a.pop("_sort")
        a["rank"] = i
    top, rest = actions[:DAILY_CAPACITY], actions[DAILY_CAPACITY:]

    # A vehicle that is an action tonight is never shown as "excluded". Its resolved
    # faults move onto its card as history (e.g. V-108's P0171 fix still stands, but the vehicle is Critical).
    top_ids = {a["vehicle_id"] for a in top}
    for e in [e for e in excluded if e["vehicle_id"] in top_ids]:
        card = next(a for a in top if a["vehicle_id"] == e["vehicle_id"])
        for ev in e["evidence"]:
            if ev["source"].startswith("work_orders.csv"):
                card["evidence"].append({**ev, "type": "resolved_history",
                                         "detail": f'{e["item"]} was resolved earlier and has not recurred: {ev["detail"]}'})
        excluded.remove(e)

    for a in top:
        a["has_late_evidence"] = any(ev.get("late_arrival") for ev in a["evidence"])
    for a in rest:
        excluded.append({"vehicle_id": a["vehicle_id"], "item": f'{a["priority"]} (score {a["score"]})',
                         "reason": f"Below daily capacity cutoff ({DAILY_CAPACITY} actions/day)",
                         "evidence": [], "score_breakdown": a["score_breakdown"]})

    return {
        "as_of_utc": as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of_local": _fmt_local(as_of),
        "received_through_local": _fmt_local(max(a["received_utc"] for a in alerts)),
        "alert_sources": d["alert_sources"],
        "timezone_note": "Alert times are UTC in the source; work orders are local. Local = EDT (UTC-4), "
                         "inferred from maintenance_due alerts at 10:00Z = 06:00 local per vendor notes.",
        "daily_capacity": DAILY_CAPACITY,
        "summary": {
            "recommended_actions": len(top),
            "critical": sum(a["priority"] == "Critical" for a in top),
            "high": sum(a["priority"] == "High" for a in top),
            "medium": sum(a["priority"] == "Medium" for a in top),
            "candidates_considered": len(actions),
        },
        "scoring_rules": {
            "dtc_urgency": URGENCY_POINTS, "additional_fault": ADDITIONAL_FAULT_POINTS,
            "recurrence": f"+{RECURRENCE_POINTS} per repeat, max +{RECURRENCE_CAP}",
            "repair_did_not_hold": f"+{REPAIR_DID_NOT_HOLD_POINTS} if High/Critical fault starts within 72h of a closed WO",
            "driven_since_fault": f"+{DRIVEN_SINCE_FAULT_POINTS} if driving alerts after a High/Critical fault",
            "maintenance_overdue": {f">={k} km": p for k, p in OVERDUE_TIERS},
            "booked_soon": f"{BOOKED_SOON_POINTS} if a covering WO is booked within 48h",
        },
        "actions": top,
        "excluded": excluded,
        "data_quality": data_quality(d, as_of),
    }


# ---- Data quality + stakeholder checks -----------------------------------------------

def data_quality(d, as_of):
    alerts, raw_count, dtc_ref, vehicles = d["alerts"], d["raw_count"], d["dtc_ref"], d["vehicles"]
    out = []
    dups = d["duplicates"]
    out.append({"finding": "Duplicate alerts", "kind": "FACT",
                "detail": f"{len(dups)} of {raw_count} alerts are exact duplicates (same vehicle, type, event time, value) "
                          f"with different alert_ids. Vendor notes claim each alert is delivered exactly once. Removed before scoring.",
                "examples": [f"alerts.csv:{a} duplicates {b}" for a, b in dups[:3]]})

    for name in d["alert_sources"]:
        if name == "alerts.csv":
            continue
        lst = [a for a in alerts if a["source_file"] == name]
        if lst:
            lags = [(a["received_utc"] - a["event_utc"]).total_seconds() / 3600 for a in lst]
            out.append({"finding": f"Late-arriving batch: {name}", "kind": "FACT",
                        "detail": f"{len(lst)} alerts (after dedupe) for {len({a['vehicle_id'] for a in lst})} vehicles, "
                                  f"events {_fmt_local(min(a['event_utc'] for a in lst))} to {_fmt_local(max(a['event_utc'] for a in lst))}, "
                                  f"received up to {_fmt_local(max(a['received_utc'] for a in lst))} "
                                  f"(lag {min(lags):.1f}-{max(lags):.1f}h). Merged into one stream and re-scored by event time.",
                        "examples": [f'{name}:{a["alert_id"]}' for a in lst[:3]]})

    late = [a for a in alerts if a["received_utc"] - a["event_utc"] > timedelta(hours=24)]
    out.append({"finding": "Late-arriving alerts", "kind": "FACT",
                "detail": f"{len(late)} alerts arrived more than 24h after the event. Ranking uses event_time_utc, not received time.",
                "examples": [f'alerts.csv:{a["alert_id"]}' for a in late[:3]]})

    ef = [a for a in alerts if a["alert_type"] == "engine_fault" and a["dtc_code"] in dtc_ref]
    mism = Counter((a["dtc_code"], dtc_ref[a["dtc_code"]]["urgency"], a["vendor_severity"]) for a in ef
                   if (dtc_ref[a["dtc_code"]]["urgency"] in ("Critical", "High")) != (a["vendor_severity"] == "HIGH"))
    out.append({"finding": "Vendor severity does not follow DTC urgency", "kind": "FACT",
                "detail": "Engine-fault urgency uses dtc_reference.csv instead. Mismatches: " +
                          ", ".join(f"{c} (ref {u}) labelled {s} x{n}" for (c, u, s), n in mism.most_common())})

    sp = [a for a in alerts if a["alert_type"] == "speeding" and not a["posted_limit_kmh"]]
    out.append({"finding": "Speeding severity without a posted limit", "kind": "FACT",
                "detail": f"{len(sp)} speeding alerts have no posted limit; {sum(a['vendor_severity'] == 'HIGH' for a in sp)} are still labelled HIGH."})

    md = [a for a in alerts if a["alert_type"] == "maintenance_due"]
    out.append({"finding": "maintenance_due is sparse and its severity ignores the value", "kind": "FACT",
                "detail": f"{len(md)} readings across {len({a['vehicle_id'] for a in md})} vehicles in 14 days (vendor says daily). "
                          "E.g. +2350 km overdue is LOW while -1066 (not due) is MEDIUM. ASSUMPTION: latest reading per vehicle is current."})

    for ty in ("device_unplugged", "camera_offline"):
        lst = [a for a in alerts if a["alert_type"] == ty]
        inwin = sum(a["event_utc"].astimezone(LOCAL_TZ).hour == 23 for a in lst)
        out.append({"finding": f"{ty} outside the nightly update window", "kind": "FACT",
                    "detail": f"{len(lst) - inwin} of {len(lst)} occur outside 23:00-00:00 local, so most are not the expected OTA window. "
                              "Not ranked as maintenance actions (telematics, not vehicle health)."})

    fw = defaultdict(list)
    for a in alerts:
        if a["alert_type"] == "harsh_braking":
            fw[vehicles[a["vehicle_id"]]["device_firmware"]].append(float(a["value"]))
    nveh = Counter(v["device_firmware"] for v in vehicles.values())
    out.append({"finding": "Firmware 4.2.1 inflates harsh-braking counts", "kind": "FACT + INFERENCE",
                "detail": "; ".join(f"{f}: {len(g) / nveh[f]:.1f} per vehicle, {100 * sum(x < 0.45 for x in g) / len(g):.0f}% at 0.40-0.44g"
                                    for f, g in sorted(fw.items())) +
                          ". INFERENCE: 4.2.1 lowered the detection threshold rather than revealing riskier driving."})

    out.append(_d17_check(alerts, vehicles))
    out.append(_v137_check(d))
    return out


def _d17_check(alerts, vehicles):
    km = {v["assigned_driver"]: int(v["km_driven_14d"]) for v in vehicles.values()}
    raw = Counter(a["driver_id"] for a in alerts)
    severe = Counter(a["driver_id"] for a in alerts if
                     (a["alert_type"] == "harsh_braking" and float(a["value"]) >= 0.5) or
                     (a["alert_type"] == "speeding" and a["posted_limit_kmh"] and
                      float(a["value"]) - float(a["posted_limit_kmh"]) >= 20))
    rate = sorted(km, key=lambda dr: -severe[dr] / km[dr])
    raw_rank = [dr for dr, _ in raw.most_common()].index("D-17") + 1
    veh = next(v for v in vehicles.values() if v["assigned_driver"] == "D-17")
    return {"finding": "Stakeholder claim: D-17 is the worst driver", "kind": "FACT + INFERENCE",
            "detail": f"D-17 ranks #{raw_rank} by raw alert count ({raw['D-17']}) but #{rate.index('D-17') + 1} of {len(km)} "
                      f"by severe events (braking >=0.5g or >=20 km/h over limit) per 1,000 km. D-17 drives {veh['vehicle_id']} "
                      f"({veh['km_driven_14d']} km, firmware {veh['device_firmware']}). INFERENCE: high count is mileage + firmware, "
                      "not the worst driving. Not a maintenance action."}


def _v137_check(d):
    w = next((w for w in d["work_orders"] if w["vehicle_id"] == "V-137"), None)
    p = [a for a in d["alerts"] if a["vehicle_id"] == "V-137" and a["dtc_code"] == "P0217"]
    temps = " -> ".join(f'{a["value"]}C' for a in p)
    return {"finding": "Stakeholder claim: V-137 overheating was a bad sensor, fixed this morning", "kind": "FACT + INFERENCE",
            "detail": f"FACT: P0217 temps rose {temps} over {len(p)} days; {w['wo_id']} replaced the sensor and cleared the "
                      f"vehicle at {w['closed_local']} local. A Critical P0524 (low oil pressure) followed the same afternoon. "
                      "INFERENCE: a steady rise looks more like real overheating than a faulty sensor; the vehicle needs inspection either way."}

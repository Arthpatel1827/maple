import { useEffect, useState } from "react";
import { api } from "./api";

// All business logic lives in the backend (core/analysis.py).
// This component only renders what /api/actions returns.

function PriorityBadge({ priority }) {
  return <span className={`badge badge-${priority.toLowerCase()}`}>{priority}</span>;
}

function Evidence({ items }) {
  return (
    <table className="evidence">
      <thead>
        <tr><th>Type</th><th>Detail</th><th>Event (local)</th><th>Received (local)</th><th>Source</th></tr>
      </thead>
      <tbody>
        {items.map((e, i) => (
          <tr key={i} className={e.late_arrival ? "late-row" : ""}>
            <td className="mono">{e.type}</td>
            <td>{e.detail}</td>
            <td className="mono nowrap">{e.event_time_local || "—"}</td>
            <td className="mono nowrap">
              {e.received_time_local || "—"}
              {e.late_arrival && <span className="late-tag">late</span>}
            </td>
            <td className="mono">{e.source}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ActionCard({ action }) {
  const [open, setOpen] = useState(action.rank === 1);
  const isTop3 = action.rank <= 3;
  return (
    <article className={`card ${isTop3 ? "card-top" : ""} card-${action.priority.toLowerCase()}`}>
      <div className="card-head">
        <span className="rank">#{action.rank}</span>
        <PriorityBadge priority={action.priority} />
        <h3>{action.vehicle_id}</h3>
        <span className="muted">{action.vehicle_class} · {action.depot}</span>
        {action.has_late_evidence && <span className="late-tag" title="Some evidence arrived 1h+ after the event">late data</span>}
        <span className="score" title="Sum of score_breakdown points">score {action.score}</span>
      </div>

      <p className="action"><strong>Next action:</strong> {action.recommended_action}</p>
      <p className="reason"><strong>Why:</strong> {action.reason}</p>

      <button className="link" onClick={() => setOpen(!open)} aria-expanded={open}>
        {open ? "Hide evidence" : `Show evidence (${action.evidence.length})`}
      </button>

      {open && (
        <div className="details">
          <h4>Score breakdown</h4>
          <ul className="breakdown">
            {action.score_breakdown.map((b, i) => (
              <li key={i}>
                <span className={`pts ${b.points < 0 ? "neg" : ""}`}>{b.points > 0 ? "+" : ""}{b.points}</span>
                <span className="mono rule">{b.rule}</span> {b.detail}
              </li>
            ))}
          </ul>
          <h4>Evidence</h4>
          <Evidence items={action.evidence} />
        </div>
      )}
    </article>
  );
}

function Section({ title, children, defaultOpen = false }) {
  return (
    <details className="section" open={defaultOpen}>
      <summary>{title}</summary>
      {children}
    </details>
  );
}

export default function App() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  const [brief, setBrief] = useState(null);

  useEffect(() => {
    api.get("/actions").then(setData).catch((e) => setError(e.message));
    // Separate request: if the brief fails, the ranked actions still render.
    api.get("/brief").then(setBrief).catch(() => setBrief(null));
  }, []);

  if (error) return <main className="page"><p className="error">Could not load actions: {error}</p></main>;
  if (!data) return <main className="page"><p className="muted">Loading maintenance actions…</p></main>;

  const { summary } = data;
  const excludedReviewed = data.excluded.filter((e) => !e.reason.startsWith("Below daily capacity"));
  const belowCutoff = data.excluded.filter((e) => e.reason.startsWith("Below daily capacity"));

  return (
    <main className="page">
      <header className="header">
        <h1>Maintenance Action Center</h1>
        <p className="muted">Maple Freight · Maintenance Operations</p>
        <p className="asof">
          Latest event <strong>{data.as_of_local}</strong> <span className="muted">({data.as_of_utc})</span>
          {" · "}received through <strong>{data.received_through_local}</strong>
        </p>
      </header>

      {data.data_quality.filter((q) => q.finding.startsWith("Late-arriving batch")).map((q, i) => (
        <div key={i} className="banner">
          <strong>{q.finding}.</strong> {q.detail}
        </div>
      ))}

      <section className="summary">
        <div className="tile"><span className="num">{summary.recommended_actions}</span>Recommended actions</div>
        <div className="tile tile-critical"><span className="num">{summary.critical}</span>Critical</div>
        <div className="tile tile-high"><span className="num">{summary.high}</span>High</div>
        <div className="tile tile-medium"><span className="num">{summary.medium}</span>Medium</div>
      </section>

      {brief && brief.brief && (
        <section className="brief">
          <div className="brief-label">
            {brief.source === "ai"
              ? <>AI-generated shift brief <span className="muted">· {brief.model} · checked against Top 3 evidence</span></>
              : <>Shift brief (deterministic fallback) <span className="muted">· {brief.validation?.passed === false ? "AI output failed verification" : "AI unavailable"}</span></>}
          </div>
          <p>{brief.brief}</p>
          <p className="muted small">Explains the ranked Top 3 ({brief.generated_from.join(", ")}). It does not change scores, priorities or order.</p>
        </section>
      )}

      <h2>What needs attention tonight?</h2>
      <p className="muted small">
        Ranked by deterministic rules over the supplied data (DTC urgency from the reference file, not vendor severity).
        Capped at {data.daily_capacity} actions/day; {belowCutoff.length} lower-priority candidates held back.
      </p>

      {data.actions.map((a) => <ActionCard key={a.vehicle_id} action={a} />)}

      <Section title={`Checked and excluded (${excludedReviewed.length})`}>
        <table className="evidence">
          <thead><tr><th>Vehicle</th><th>Item</th><th>Why excluded</th><th>Source</th></tr></thead>
          <tbody>
            {excludedReviewed.map((e, i) => (
              <tr key={i}>
                <td className="mono nowrap">{e.vehicle_id}</td>
                <td>{e.item}</td>
                <td>{e.reason}</td>
                <td className="mono">{e.evidence.map((ev) => ev.source).join(", ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title={`Below today's cutoff (${belowCutoff.length})`}>
        <ul className="plain">
          {belowCutoff.map((e) => (
            <li key={e.vehicle_id}><span className="mono">{e.vehicle_id}</span> {e.item}: {e.score_breakdown.map((b) => b.detail).join("; ")}</li>
          ))}
        </ul>
      </Section>

      <Section title={`Data quality & stakeholder checks (${data.data_quality.length})`}>
        <ul className="plain">
          {data.data_quality.map((q, i) => (
            <li key={i}>
              <strong>{q.finding}</strong> <span className="tag">{q.kind}</span>
              <div>{q.detail}</div>
            </li>
          ))}
        </ul>
        <p className="muted small">{data.timezone_note}</p>
      </Section>
    </main>
  );
}

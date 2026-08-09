import React, { useState } from "react";

type ReviewItem = {
  id: string;
  kind: "register_update" | "conflict";
  target_key: string;
  state: "pending" | "approved" | "rejected";
  payload: {
    before?: { value?: unknown } | null;
    after?: { value?: unknown };
    conflict?: { kind: string; rationale: string } | null;
    reason?: string;
    force_review?: boolean;
  };
};

export function App() {
  const [runId, setRunId] = useState("");
  // The reviewer's own credential. The identity a decision is recorded under comes
  // from this token on the server, so the console cannot name who approved anything.
  const [token, setToken] = useState("");
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [decisions, setDecisions] = useState<Record<string, string>>({});
  const [message, setMessage] = useState("");

  const auth = () => ({ Authorization: `Bearer ${token}` });

  async function loadItems() {
    const response = await fetch(`/api/runs/${runId}/review-items`, { headers: auth() });
    if (!response.ok) {
      setMessage(`${response.status}: ${(await response.json()).detail}`);
      return;
    }
    setItems(await response.json());
  }

  async function submit() {
    const response = await fetch(`/api/runs/${runId}/resume`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...auth() },
      body: JSON.stringify({ decisions }),
    });
    const body = await response.json();
    setMessage(JSON.stringify(body, null, 2));
  }

  return (
    <main>
      <header>
        <p className="eyebrow">VENDOR DOCUMENT ANALYST</p>
        <h1>Obligations review gate</h1>
        <p>Every proposal remains pending until an explicit item-level decision.</p>
      </header>

      <section className="toolbar">
        <input value={runId} onChange={(e) => setRunId(e.target.value)} placeholder="Run UUID" />
        <input
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="Reviewer token"
          type="password"
        />
        <button onClick={loadItems}>Load review</button>
      </section>

      <section className="cards">
        {items.map((item) => (
          <article key={item.id}>
            <div className="card-head">
              <strong>{item.target_key}</strong>
              {item.payload.conflict && <span>{item.payload.conflict.kind.toUpperCase()}</span>}
              {item.payload.force_review && <span>QUARANTINED SOURCE</span>}
            </div>
            <div className="diff">
              <pre>{JSON.stringify(item.payload.before?.value ?? null, null, 2)}</pre>
              <pre>{JSON.stringify(item.payload.after?.value, null, 2)}</pre>
            </div>
            <blockquote>{item.payload.conflict?.rationale ?? item.payload.reason}</blockquote>
            <div className="actions">
              <button onClick={() => setDecisions({ ...decisions, [item.id]: "approved" })}>Approve</button>
              <button onClick={() => setDecisions({ ...decisions, [item.id]: "rejected" })}>Reject</button>
              <small>{decisions[item.id] ?? "pending"}</small>
            </div>
          </article>
        ))}
      </section>

      {items.length > 0 && <button className="commit" onClick={submit}>Submit item decisions</button>}
      {message && <pre className="result">{message}</pre>}
    </main>
  );
}

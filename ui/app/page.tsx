"use client";

import { useCallback, useEffect, useRef, useState } from "react";

type Dup = { posting_id: string; score: number; title: string };
type PostingRecord = {
  external_id: string;
  title: string;
  location?: { city?: string; state?: string; country?: string; remote_type?: string };
  salary?: { min?: number; max?: number; currency?: string; period?: string } | null;
  category?: { label?: string; onet_code?: string };
  duplicates?: Dup[];
  policy?: { severity?: string; flags?: string[] };
  confidence?: number;
  trajectory?: string[];
};
type Item = { external_id: string; status: string; review_reason: string; record: PostingRecord | null };

const SAMPLES: { label: string; body: Record<string, string> }[] = [
  {
    label: "Discriminatory (policy block)",
    body: { title: "Warehouse Packer - men only", description: "No women. Forklift experience required.", location_raw: "Austin, United States", salary_raw: "$18/hr" },
  },
  {
    label: "Clean (auto-resolves)",
    body: { title: "Senior Python Engineer", description: "Backend role. Kafka, Postgres, hybrid.", location_raw: "Bangalore, India", salary_raw: "$120k-$150k/yr" },
  },
  {
    label: "Ambiguous (low confidence)",
    body: { title: "Mystery Opportunity", description: "Exciting role, details TBD.", location_raw: "", salary_raw: "" },
  },
];

export default function Page() {
  const [items, setItems] = useState<Item[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [flash, setFlash] = useState("");
  const idRef = useRef(0);

  const refresh = useCallback(async () => {
    try {
      const r = await fetch("/api/pending", { cache: "no-store" });
      const d = await r.json();
      setItems(d.items || []);
    } catch {
      /* transient; next tick retries */
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 2000);
    return () => clearInterval(t);
  }, [refresh]);

  async function submit(body: Record<string, string>) {
    idRef.current += 1;
    const external_id = `ui-${Date.now()}-${idRef.current}`;
    setFlash(`Submitted ${external_id}…`);
    await fetch("/api/postings", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ source: "ui", external_id, ...body }),
    });
    setTimeout(refresh, 800);
  }

  async function decide(id: string, action: "approve" | "reject") {
    setBusy(id);
    await fetch(`/api/postings/${encodeURIComponent(id)}/${action}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ reviewer: "ui", note: action }),
    });
    setFlash(`${action === "approve" ? "Approved" : "Rejected"} ${id}`);
    setBusy(null);
    setTimeout(refresh, 600);
  }

  return (
    <main className="mx-auto max-w-4xl px-6 py-10">
      <header className="mb-8">
        <h1 className="text-2xl font-semibold">Conductor — Approval Queue</h1>
        <p className="mt-1 text-sm text-slate-400">
          Durable job-posting intake agent. Low-confidence, policy-blocked, or duplicate postings pause
          here via a Temporal signal and resume when you decide.
        </p>
      </header>

      <section className="mb-10">
        <h2 className="mb-2 text-sm font-medium uppercase tracking-wide text-slate-400">Submit a posting</h2>
        <div className="flex flex-wrap gap-2">
          {SAMPLES.map((s) => (
            <button
              key={s.label}
              onClick={() => submit(s.body)}
              className="rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm hover:border-slate-500"
            >
              {s.label}
            </button>
          ))}
        </div>
        {flash && <p className="mt-3 text-xs text-emerald-400">{flash}</p>}
      </section>

      <section>
        <h2 className="mb-3 text-sm font-medium uppercase tracking-wide text-slate-400">
          Pending review ({items.length})
        </h2>
        {items.length === 0 && (
          <p className="rounded-md border border-dashed border-slate-800 px-4 py-8 text-center text-sm text-slate-500">
            Nothing awaiting review. Submit an ambiguous or policy-blocked posting above.
          </p>
        )}
        <div className="space-y-4">
          {items.map((it) => (
            <article key={it.external_id} className="rounded-lg border border-slate-800 bg-slate-900/60 p-4">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <p className="font-medium">{it.record?.title || it.external_id}</p>
                  <p className="text-xs text-slate-500">{it.external_id}</p>
                </div>
                <span className="rounded bg-amber-500/15 px-2 py-1 text-xs font-medium text-amber-300">
                  {it.review_reason?.split(":")[0] || "review"}
                </span>
              </div>

              <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-3">
                <Field k="Category" v={it.record?.category?.label} />
                <Field
                  k="Location"
                  v={[it.record?.location?.city, it.record?.location?.country].filter(Boolean).join(", ") || "—"}
                />
                <Field k="Remote" v={it.record?.location?.remote_type} />
                <Field
                  k="Salary"
                  v={
                    it.record?.salary
                      ? `${it.record.salary.min ?? "?"}–${it.record.salary.max ?? "?"} ${it.record.salary.currency}`
                      : "—"
                  }
                />
                <Field k="Confidence" v={it.record?.confidence?.toFixed(2)} />
                <Field k="Policy" v={it.record?.policy?.severity} />
              </dl>

              <p className="mt-2 text-xs text-slate-500">
                trajectory: {it.record?.trajectory?.join(" → ") || "—"}
              </p>
              {it.record?.duplicates && it.record.duplicates.length > 0 && (
                <p className="mt-1 text-xs text-rose-400">
                  near-duplicate of {it.record.duplicates[0].posting_id} (score {it.record.duplicates[0].score})
                </p>
              )}
              <p className="mt-1 text-xs text-slate-500">reason: {it.review_reason}</p>

              <div className="mt-4 flex gap-2">
                <button
                  disabled={busy === it.external_id}
                  onClick={() => decide(it.external_id, "approve")}
                  className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
                >
                  Approve
                </button>
                <button
                  disabled={busy === it.external_id}
                  onClick={() => decide(it.external_id, "reject")}
                  className="rounded-md border border-rose-700 px-4 py-2 text-sm font-medium text-rose-300 hover:bg-rose-950 disabled:opacity-50"
                >
                  Reject
                </button>
              </div>
            </article>
          ))}
        </div>
      </section>
    </main>
  );
}

function Field({ k, v }: { k: string; v?: string | null }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-slate-500">{k}</dt>
      <dd className="text-slate-200">{v || "—"}</dd>
    </div>
  );
}

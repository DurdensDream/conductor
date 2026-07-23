import { proxy } from "../../../lib";

const ALLOWED = new Set(["approve", "reject", "cancel"]);

export async function POST(req: Request, ctx: { params: Promise<{ id: string; action: string }> }) {
  const { id, action } = await ctx.params;
  if (!ALLOWED.has(action)) {
    return new Response(JSON.stringify({ error: "bad action" }), { status: 400 });
  }
  const body = await req.text();
  return proxy(`/postings/${encodeURIComponent(id)}/${action}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: body || "{}",
  });
}

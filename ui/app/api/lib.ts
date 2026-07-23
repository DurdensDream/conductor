// Server-side only: the control-API base URL never reaches the browser (no CORS, and the
// internal Compose hostname stays private). Browser -> Next route handler -> API.
export const API = process.env.CONDUCTOR_API_URL || "http://localhost:8000";

export async function proxy(path: string, init?: RequestInit): Promise<Response> {
  try {
    const res = await fetch(`${API}${path}`, { ...init, cache: "no-store" });
    const body = await res.text();
    return new Response(body, { status: res.status, headers: { "content-type": "application/json" } });
  } catch (err) {
    return new Response(JSON.stringify({ error: `upstream unreachable: ${err}` }), {
      status: 502,
      headers: { "content-type": "application/json" },
    });
  }
}

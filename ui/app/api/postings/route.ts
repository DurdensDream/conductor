import { proxy } from "../lib";

export async function POST(req: Request) {
  const body = await req.text();
  return proxy("/postings", { method: "POST", headers: { "content-type": "application/json" }, body });
}

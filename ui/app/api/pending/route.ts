import { proxy } from "../lib";

export const dynamic = "force-dynamic";

export async function GET() {
  return proxy("/pending");
}

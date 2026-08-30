import { NextResponse } from "next/server";

export async function GET() {
  const upstream = process.env.API_UPSTREAM ?? "http://localhost:8000";
  try {
    const response = await fetch(`${upstream}/api/v1/health`, { cache: "no-store" });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json({ status: "offline", detail: "API unavailable" }, { status: 503 });
  }
}

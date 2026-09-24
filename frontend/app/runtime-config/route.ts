// The API address is read at request time, so one Docker image works for any deployment.
// (NEXT_PUBLIC_* values are frozen at build time; this route is not.)
export const dynamic = "force-dynamic";

export function GET() {
  return Response.json({
    apiUrl: process.env.API_PUBLIC_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000",
  });
}

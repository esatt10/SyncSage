import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

export function HealthBadge() {
  const health = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    // Every open tab polls this. The endpoint is trivial, but the API is one
    // Python process: while it is answering a question the poll queues behind
    // that work, and a 15s cadence meant the badge could read "offline"
    // precisely when the server was busiest — reporting a problem it had a
    // hand in creating. A slower beat still catches a genuinely down API.
    refetchInterval: 45000,
    refetchIntervalInBackground: false,
  });
  const ok = health.data?.status === "ok";
  return (
    <span className="health-badge" title={ok ? "API reachable" : "API unreachable"}>
      <span className={`health-badge__dot${ok ? "" : " health-badge__dot--bad"}`} />
      {ok ? "connected" : health.isLoading ? "checking…" : "offline"}
    </span>
  );
}

import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

export function HealthBadge() {
  const health = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    refetchInterval: 15000,
  });
  const ok = health.data?.status === "ok";
  return (
    <span className="health-badge" title={ok ? "API reachable" : "API unreachable"}>
      <span className={`health-badge__dot${ok ? "" : " health-badge__dot--bad"}`} />
      {ok ? "connected" : health.isLoading ? "checking…" : "offline"}
    </span>
  );
}

import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { Explainable } from "../explain/Explainable";

export function HealthBadge() {
  const health = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    refetchInterval: 15000,
  });
  const ok = health.data?.status === "ok";
  return (
    <Explainable id="health.badge" as="span" className="health-badge">
      <span className={`status-dot ${ok ? "status-dot--ok" : "status-dot--down"}`} />
      {ok ? "healthy" : health.isLoading ? "checking…" : "unreachable"}
    </Explainable>
  );
}

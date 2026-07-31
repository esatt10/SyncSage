import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { CopyLine } from "./CopyLine";

/**
 * The MCP surface, made visible.
 *
 * The same knowledge base the chat panel queries is exposed to coding agents
 * over MCP; this dialog shows the transports, the ready-to-paste client
 * config, and the tool list so it is obvious what an attached agent can do.
 */
export function McpDialog({ onClose }: { onClose: () => void }) {
  const info = useQuery({ queryKey: ["mcp-info"], queryFn: api.mcpInfo });

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal modal--wide" onClick={(event) => event.stopPropagation()}>
        <header className="modal__header">
          <h2>Connect a coding agent</h2>
          <button className="btn btn--ghost btn--icon" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        {info.isLoading ? (
          <p className="muted small">
            <span className="spinner" /> Loading…
          </p>
        ) : info.isError ? (
          <div className="banner banner--error">{(info.error as Error).message}</div>
        ) : info.data ? (
          <>
            {!info.data.enabled ? (
              <div className="banner banner--warn">
                MCP is disabled in this config (<code>server.mcp.enabled</code>).
              </div>
            ) : null}

            <div className="section">
              <h3 className="section__title">Add to Claude Code or Cursor</h3>
              <CopyLine command={`syncsage client-config claude-code -c ${info.data.config_path} -o .mcp.json`} />
              {info.data.streamable_http_url ? (
                <>
                  <p className="muted small" style={{ margin: "10px 0 6px" }}>
                    Or connect over streamable HTTP:
                  </p>
                  <CopyLine command={info.data.streamable_http_url} />
                </>
              ) : null}
            </div>

            {info.data.client_configs["claude-code"] ? (
              <div className="section">
                <h3 className="section__title">.mcp.json</h3>
                <pre className="content-block">{info.data.client_configs["claude-code"]}</pre>
              </div>
            ) : null}

            <div className="section">
              <h3 className="section__title">
                Tools an attached agent gets ({info.data.tools.length})
              </h3>
              <table className="data-table">
                <tbody>
                  {info.data.tools.map((tool) => (
                    <tr key={tool.name}>
                      <td style={{ whiteSpace: "nowrap" }}>
                        <code>{tool.name}</code>
                      </td>
                      <td className="muted small">{tool.description}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}

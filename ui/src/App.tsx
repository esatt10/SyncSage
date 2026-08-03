import { Route, Routes } from "react-router-dom";
import { TopBar } from "./components/TopBar";
import { Notebook } from "./pages/Notebook";
import { SourcesPage } from "./pages/SourcesPage";
import { ConfigPage } from "./pages/ConfigPage";
import { SessionProvider } from "./state/session";

/**
 * The session store sits ABOVE the router on purpose: routes unmount as you
 * navigate, so any workspace state held inside a page would be destroyed by
 * the act of visiting Sources or Settings. Up here, only an action changes it.
 */
export function App() {
  return (
    <SessionProvider>
      <div className="app">
        <TopBar />
        <main className="app__main">
          <Routes>
            <Route path="/" element={<Notebook />} />
            <Route path="/sources" element={<SourcesPage />} />
            <Route path="/config" element={<ConfigPage />} />
          </Routes>
        </main>
      </div>
    </SessionProvider>
  );
}

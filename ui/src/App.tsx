import { Route, Routes } from "react-router-dom";
import { TopBar } from "./components/TopBar";
import { ReferencePanel } from "./explain/ReferencePanel";
import { GraphWorkspace } from "./pages/GraphWorkspace";
import { SourcesPage } from "./pages/SourcesPage";
import { ConfigPage } from "./pages/ConfigPage";

export function App() {
  return (
    <div className="app">
      <TopBar />
      <main className="app__main">
        <Routes>
          <Route path="/" element={<GraphWorkspace />} />
          <Route path="/sources" element={<SourcesPage />} />
          <Route path="/config" element={<ConfigPage />} />
        </Routes>
      </main>
      <ReferencePanel />
    </div>
  );
}

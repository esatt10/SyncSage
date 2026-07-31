import { Route, Routes } from "react-router-dom";
import { TopBar } from "./components/TopBar";
import { Notebook } from "./pages/Notebook";
import { SourcesPage } from "./pages/SourcesPage";
import { ConfigPage } from "./pages/ConfigPage";

export function App() {
  return (
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
  );
}

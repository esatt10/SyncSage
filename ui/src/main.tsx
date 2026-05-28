import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "katex/dist/katex.min.css";
import { App } from "./App";
import { ExplainProvider } from "./explain/ExplainContext";
import "./styles.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false, retry: 1 } },
});

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <ExplainProvider>
          <App />
        </ExplainProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);

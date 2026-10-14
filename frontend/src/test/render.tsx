import { ReactElement } from "react";
import { render } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Violation } from "@/types/policy";

export function renderWithProviders(ui: ReactElement, { path = "/", route = "/" } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path={path} element={ui} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

export const violation = (over: Partial<Violation> = {}): Violation => ({
  id: "v1",
  trace_id: "t1",
  agent_id: "a1",
  policy_id: "p1",
  policy_name: "confirm_before_delete",
  policy_version: 1,
  severity: "high",
  at_seq: 2,
  counterexample: [2],
  message: "delete_user without prior confirmation",
  mode: "batch",
  detected_at: "2026-10-14T05:00:00Z",
  ...over,
});

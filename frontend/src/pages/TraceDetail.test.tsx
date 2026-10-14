import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import TraceDetail, { recentlyPending } from "./TraceDetail";
import { tracesApi } from "@/api/traces";
import { tracePoliciesApi } from "@/api/tracePolicies";
import { useWsStore } from "@/stores/wsStore";
import { renderWithProviders, violation } from "@/test/render";
import { Trace } from "@/types/trace";

vi.mock("@/api/traces", () => ({ tracesApi: { get: vi.fn() } }));
vi.mock("@/api/tracePolicies", () => ({ tracePoliciesApi: { traceViolations: vi.fn() } }));
const traces = vi.mocked(tracesApi);
const violations = vi.mocked(tracePoliciesApi.traceViolations);

const names = ["read_secret", "search", "send_email", "delete_user"];
const trace = {
  id: "t1", trace_uid: "abcdef123456", status: "success", environment: "prod", created_at: "2026-10-14T05:00:00Z",
  latency_ms: 10, cost_usd: 0, input: "", output: "", error_message: null, tokens_input: 0, tokens_output: 0,
  pii_flags: [], injection_score: 0, meta: {},
  spans: names.map((name, seq) => ({
    id: `s${seq}`, trace_id: "t1", parent_span_id: null, seq, kind: "tool", name, input: {}, output: {},
    latency_ms: 1, status: "success", started_at: "", ended_at: null,
  })),
} as unknown as Trace;

const exfil = violation({ id: "v1", policy_name: "no_exfil_after_secret", at_seq: 2, counterexample: [0, 2] });
const confirm = violation({ id: "v2", policy_id: "p2", policy_name: "confirm_before_delete", at_seq: 3, counterexample: [3] });

const highlighted = () =>
  Array.from(document.querySelectorAll("[data-counterexample]")).map((n) => n.getAttribute("data-seq"));
const show = () => renderWithProviders(<TraceDetail />, { path: "/traces/:id", route: "/traces/t1" });

beforeEach(() => {
  vi.clearAllMocks();
  useWsStore.setState({ lastViolation: null, toasts: [] });
  traces.get.mockResolvedValue(trace);
});

describe("TraceDetail violations", () => {
  it("highlights the first violation's counterexample, then the selected one", async () => {
    violations.mockResolvedValue([exfil, confirm]);
    show();
    await screen.findByText("no_exfil_after_secret");
    expect(violations).toHaveBeenCalledWith("t1");
    expect(highlighted()).toEqual(["0", "2"]);
    expect(screen.getByText("violated here").closest("[data-seq]")).toHaveAttribute("data-seq", "2");

    fireEvent.click(screen.getByText("confirm_before_delete"));
    expect(highlighted()).toEqual(["3"]);
    expect(screen.getByText("violated here").closest("[data-seq]")).toHaveAttribute("data-seq", "3");
  });

  it("selects the violation passed in navigation state", async () => {
    violations.mockResolvedValue([exfil, confirm]);
    renderWithProviders(<TraceDetail />, { path: "/traces/:id", route: { pathname: "/traces/t1", state: { violation: "v2" } } });
    await screen.findByText("no_exfil_after_secret");
    expect(highlighted()).toEqual(["3"]);
  });

  it("marks only the violating event while the counterexample is pending", async () => {
    violations.mockResolvedValue([violation({ mode: "live", counterexample: [], counterexample_status: "pending", at_seq: 3 })]);
    show();
    expect(await screen.findByText(/Counterexample pending/)).toBeInTheDocument();
    expect(highlighted()).toEqual([]);
    expect(screen.getByText("violated here").closest("[data-seq]")).toHaveAttribute("data-seq", "3");
  });

  it("shows the trace when violations cannot be loaded", async () => {
    violations.mockRejectedValue(new Error("501"));
    show();
    expect(await screen.findByText("Violations unavailable.")).toBeInTheDocument();
    expect(screen.getByText("delete_user")).toBeInTheDocument();
    expect(highlighted()).toEqual([]);
  });

  it("does not refetch on mount for a violation that arrived earlier", async () => {
    useWsStore.setState({ lastViolation: confirm });
    violations.mockResolvedValue([confirm]);
    show();
    await screen.findByText("confirm_before_delete");
    await new Promise((r) => setTimeout(r, 50));
    expect(violations).toHaveBeenCalledTimes(1);
  });

  it("refetches when a live violation arrives for this trace only", async () => {
    violations.mockResolvedValue([]);
    show();
    await screen.findByText("No policy violations.");
    expect(violations).toHaveBeenCalledTimes(1);

    act(() => useWsStore.getState().receive({ type: "violation", data: violation({ trace_id: "other" }) }));
    violations.mockResolvedValue([confirm]);
    act(() => useWsStore.getState().receive({ type: "violation", data: confirm }));
    expect(await screen.findByText("confirm_before_delete")).toBeInTheDocument();
    await waitFor(() => expect(highlighted()).toEqual(["3"]));
    expect(violations).toHaveBeenCalledTimes(2);
  });
});

describe("recentlyPending", () => {
  const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString();
  it("polls only for recent pending rows", () => {
    const pending = { mode: "live" as const, counterexample: [], counterexample_status: "pending" as const };
    expect(recentlyPending(violation({ ...pending, detected_at: ago(1) }))).toBe(true);
    expect(recentlyPending(violation({ ...pending, detected_at: ago(11) }))).toBe(false);
    expect(recentlyPending(violation({ ...pending, counterexample_status: "minimal", detected_at: ago(1) }))).toBe(false);
    expect(recentlyPending(violation({ ...pending, counterexample_status: "none", detected_at: ago(1) }))).toBe(false);
  });
});

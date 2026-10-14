import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import ViolationToasts, { TOAST_MS } from "./ViolationToasts";
import { useWsStore } from "@/stores/wsStore";
import { violation } from "@/test/render";

const push = (v = violation()) => act(() => useWsStore.getState().receive({ type: "violation", data: v }));
const renderToasts = () => render(<MemoryRouter><ViolationToasts /></MemoryRouter>);

beforeEach(() => useWsStore.setState({ violations: [], toasts: [] }));
afterEach(() => {
  vi.useRealTimers();
});

describe("ViolationToasts", () => {
  it("shows a live violation with a link to the trace", () => {
    renderToasts();
    push(violation({ policy_name: "no_exfil_after_secret", severity: "critical", at_seq: 4 }));
    expect(screen.getByRole("status")).toHaveTextContent("no_exfil_after_secret");
    expect(screen.getByText("critical")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open trace at event #4" })).toHaveAttribute("href", "/traces/t1");
  });

  it("omits the link for violations without a trace", () => {
    renderToasts();
    push(violation({ id: null, trace_id: null, mode: "precheck" }));
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("dismisses on click and after a timeout", () => {
    vi.useFakeTimers();
    renderToasts();
    push(violation({ id: "a" }));
    fireEvent.click(screen.getByLabelText("Dismiss"));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    push(violation({ id: "b" }));
    act(() => vi.advanceTimersByTime(TOAST_MS - 1));
    expect(screen.getByRole("status")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(1));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});

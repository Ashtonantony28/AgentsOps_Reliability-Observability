import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import TraceSpanTree from "./TraceSpanTree";
import { Span } from "@/types/trace";

const span = (i: number, name: string, seq: number | null = i): Span => ({
  id: `s${i}`, trace_id: "t1", parent_span_id: null, seq, kind: "tool", name,
  input: {}, output: {}, latency_ms: 1, status: "success", started_at: "", ended_at: null,
});

const nodes = (c: HTMLElement) => Array.from(c.querySelectorAll("[data-seq]"));

describe("TraceSpanTree", () => {
  const spans = [span(0, "read_secret"), span(1, "search"), span(2, "send_email")];

  it("highlights counterexample events and the violating event", () => {
    const { container } = render(<TraceSpanTree spans={spans} counterexample={[0, 2]} atSeq={2} />);
    expect(nodes(container).map((n) => n.hasAttribute("data-counterexample"))).toEqual([true, false, true]);
    expect(screen.getAllByText("counterexample")).toHaveLength(2);
    expect(screen.getByText("violated here").closest("[data-seq]")).toHaveAttribute("data-seq", "2");
  });

  it("highlights nothing without a selected violation", () => {
    const { container } = render(<TraceSpanTree spans={spans} />);
    expect(container.querySelector("[data-counterexample]")).toBeNull();
    expect(screen.queryByText("violated here")).not.toBeInTheDocument();
  });

  it("matches by seq, falling back to position for spans without one", () => {
    const { container } = render(
      <TraceSpanTree spans={[span(0, "a", null), span(1, "b", 5)]} counterexample={[0, 5]} atSeq={1} />
    );
    expect(nodes(container).map((n) => n.getAttribute("data-seq"))).toEqual(["0", "5"]);
    expect(nodes(container).every((n) => n.hasAttribute("data-counterexample"))).toBe(true);
    expect(screen.queryByText("violated here")).not.toBeInTheDocument();
  });

  it("keeps the empty state", () => {
    render(<TraceSpanTree spans={[]} counterexample={[0]} />);
    expect(screen.getByText("No spans recorded.")).toBeInTheDocument();
  });
});

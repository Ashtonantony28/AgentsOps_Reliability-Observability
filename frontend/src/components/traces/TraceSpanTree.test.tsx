import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import TraceSpanTree, { spanSeqs } from "./TraceSpanTree";
import { Span } from "@/types/trace";

const span = (i: number, name: string, seq: number | null = i): Span => ({
  id: `s${i}`, trace_id: "t1", parent_span_id: null, seq, kind: "tool", name,
  input: {}, output: {}, latency_ms: 1, status: "success", started_at: "", ended_at: null,
});

const nodes = (c: HTMLElement) => Array.from(c.querySelectorAll("[data-seq]"));
const highlightedSeqs = (c: HTMLElement) =>
  Array.from(c.querySelectorAll("[data-counterexample]")).map((n) => n.getAttribute("data-seq"));

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

  it("matches by seq", () => {
    const { container } = render(<TraceSpanTree spans={[span(0, "a", 3), span(1, "b", 5)]} counterexample={[5]} atSeq={3} />);
    expect(nodes(container).map((n) => n.getAttribute("data-seq"))).toEqual(["3", "5"]);
    expect(highlightedSeqs(container)).toEqual(["5"]);
    expect(screen.getByText("violated here").closest("[data-seq]")).toHaveAttribute("data-seq", "3");
  });

  it("numbers every span by start order when any lacks a seq, like the backend", () => {
    const spans = [
      { ...span(0, "legacy", null), started_at: "2026-10-14T05:00:02Z" },
      { ...span(1, "first", 0), started_at: "2026-10-14T05:00:00Z" },
      { ...span(2, "second", 1), started_at: "2026-10-14T05:00:01Z" },
    ];
    expect([...spanSeqs(spans)]).toEqual([["s1", 0], ["s2", 1], ["s0", 2]]);
    const { container } = render(<TraceSpanTree spans={spans} counterexample={[0]} />);
    expect(nodes(container).map((n) => n.getAttribute("data-seq"))).toEqual(["2", "0", "1"]);
    expect(highlightedSeqs(container)).toEqual(["0"]);
  });

  it("keeps the empty state", () => {
    render(<TraceSpanTree spans={[]} counterexample={[0]} />);
    expect(screen.getByText("No spans recorded.")).toBeInTheDocument();
  });
});

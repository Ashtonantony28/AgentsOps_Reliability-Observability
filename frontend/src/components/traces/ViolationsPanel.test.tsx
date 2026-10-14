import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import ViolationsPanel from "./ViolationsPanel";
import { violation } from "@/test/render";

describe("ViolationsPanel", () => {
  it("shows an empty state", () => {
    render(<ViolationsPanel violations={[]} selected={null} onSelect={() => {}} />);
    expect(screen.getByText("No policy violations.")).toBeInTheDocument();
  });

  it("lists policy, severity, position and counterexample events", () => {
    const v = violation({ counterexample: [0, 2] });
    render(<ViolationsPanel violations={[v]} selected={v} onSelect={() => {}} />);
    expect(screen.getByText("confirm_before_delete")).toBeInTheDocument();
    expect(screen.getByText("high")).toBeInTheDocument();
    expect(screen.getByText("at event #2")).toBeInTheDocument();
    expect(screen.getByText("Counterexample: #0, #2")).toBeInTheDocument();
    expect(screen.getByRole("button", { pressed: true })).toBeInTheDocument();
  });

  it("marks a live violation without a counterexample as pending", () => {
    render(
      <ViolationsPanel violations={[violation({ mode: "live", counterexample: [], counterexample_status: "pending" })]} selected={null} onSelect={() => {}} />
    );
    expect(screen.getByText(/Counterexample pending/)).toBeInTheDocument();
    expect(screen.queryByText(/^Counterexample:/)).not.toBeInTheDocument();
  });

  it("flags a fallback counterexample as maybe not minimal", () => {
    render(<ViolationsPanel violations={[violation({ counterexample: [0, 1, 2], counterexample_status: "fallback" })]} selected={null} onSelect={() => {}} />);
    expect(screen.getByText(/Counterexample: #0, #1, #2/)).toHaveTextContent("may not be minimal");
  });

  it("says when a violation has no counterexample", () => {
    render(<ViolationsPanel violations={[violation({ mode: "live", counterexample: [], counterexample_status: "none" })]} selected={null} onSelect={() => {}} />);
    expect(screen.getByText("No counterexample for this violation.")).toBeInTheDocument();
    expect(screen.queryByText(/pending|regardless/)).not.toBeInTheDocument();
  });

  it("explains an empty minimal counterexample, live or batch", () => {
    render(<ViolationsPanel violations={[violation({ mode: "live", counterexample: [] })]} selected={null} onSelect={() => {}} />);
    expect(screen.getByText(/regardless of any event/)).toBeInTheDocument();
    expect(screen.queryByText(/pending/)).not.toBeInTheDocument();
  });

  it("explains an empty counterexample from the batch check", () => {
    render(<ViolationsPanel violations={[violation({ counterexample: [] })]} selected={null} onSelect={() => {}} />);
    expect(screen.getByText(/regardless of any event/)).toBeInTheDocument();
    expect(screen.queryByText(/pending/)).not.toBeInTheDocument();
  });

  it("selects a violation on click", () => {
    const a = violation();
    const b = violation({ id: "v2", policy_id: "p2", policy_name: "retry_cap" });
    const onSelect = vi.fn();
    render(<ViolationsPanel violations={[a, b]} selected={a} onSelect={onSelect} />);
    fireEvent.click(screen.getByText("retry_cap"));
    expect(onSelect).toHaveBeenCalledWith(b);
    expect(screen.getAllByRole("button").map((x) => x.getAttribute("aria-pressed"))).toEqual(["true", "false"]);
  });
});

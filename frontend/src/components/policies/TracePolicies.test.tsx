import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import TracePolicies from "./TracePolicies";
import { tracePoliciesApi } from "@/api/tracePolicies";
import { renderWithProviders } from "@/test/render";
import { TracePolicy } from "@/types/policy";

vi.mock("@/api/agents", () => ({ agentsApi: { list: vi.fn().mockResolvedValue([]) } }));
vi.mock("@/api/tracePolicies", async (orig) => ({
  ...(await orig<typeof import("@/api/tracePolicies")>()),
  tracePoliciesApi: {
    list: vi.fn(), update: vi.fn(), remove: vi.fn(), create: vi.fn(),
    validate: vi.fn().mockResolvedValue({ ok: true, errors: [] }),
  },
}));
const api = vi.mocked(tracePoliciesApi);

const policy: TracePolicy = {
  id: "p1", org_id: "o1", name: "confirm_before_delete", severity: "high", action: "block", agent_id: null,
  environment: "prod", enabled: true, version: 3, created_at: "", updated_at: "",
  source: 'policy confirm_before_delete severity high:\n  always tool("delete_*") -> once event("confirmation")\n',
};

beforeEach(() => {
  vi.clearAllMocks();
  api.list.mockResolvedValue([policy]);
});

describe("TracePolicies", () => {
  it("lists policies with severity, action, version and scope", async () => {
    renderWithProviders(<TracePolicies />);
    expect(await screen.findByText("confirm_before_delete")).toBeInTheDocument();
    expect(screen.getByText("high")).toBeInTheDocument();
    expect(screen.getByText("block")).toBeInTheDocument();
    expect(screen.getByText("v3")).toBeInTheDocument();
    expect(screen.getByText("all agents · prod")).toBeInTheDocument();
  });

  it("toggles, deletes and opens the editor", async () => {
    api.update.mockResolvedValue(policy);
    api.remove.mockResolvedValue({ ok: true });
    renderWithProviders(<TracePolicies />);
    fireEvent.click(await screen.findByRole("checkbox"));
    await waitFor(() => expect(api.update).toHaveBeenCalledWith("p1", { enabled: false }));
    fireEvent.click(screen.getByText("delete"));
    await waitFor(() => expect(api.remove).toHaveBeenCalledWith("p1"));

    fireEvent.click(screen.getByText("edit"));
    expect(screen.getByLabelText("Policy source")).toHaveValue(policy.source);
    fireEvent.click(screen.getByText("Cancel"));
    expect(screen.queryByLabelText("Policy source")).not.toBeInTheDocument();
  });

  it("shows a write error such as a non-admin 403", async () => {
    api.remove.mockRejectedValue(Object.assign(new Error("x"), {
      response: { status: 403, data: { error: { code: "forbidden", message: "admin role required" } } },
    }));
    renderWithProviders(<TracePolicies />);
    fireEvent.click(await screen.findByText("delete"));
    expect(await screen.findByText("admin role required")).toBeInTheDocument();
  });

  it("shows an empty state and the new-policy editor", async () => {
    api.list.mockResolvedValue([]);
    renderWithProviders(<TracePolicies />);
    expect(await screen.findByText("No trace policies yet.")).toBeInTheDocument();
    fireEvent.click(screen.getByText("New policy"));
    expect(screen.getByRole("button", { name: "Create" })).toBeInTheDocument();
    expect(screen.queryByText("No trace policies yet.")).not.toBeInTheDocument();
  });
});

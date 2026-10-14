import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import PolicyEditor, { PolicyErrorList } from "./PolicyEditor";
import { tracePoliciesApi } from "@/api/tracePolicies";
import { TracePolicy } from "@/types/policy";

vi.mock("@/api/agents", () => ({ agentsApi: { list: vi.fn().mockResolvedValue([{ id: "a1", name: "billing-bot" }]) } }));
vi.mock("@/api/tracePolicies", async (orig) => ({
  ...(await orig<typeof import("@/api/tracePolicies")>()),
  tracePoliciesApi: { validate: vi.fn(), create: vi.fn(), update: vi.fn() },
}));
const api = vi.mocked(tracePoliciesApi);

const httpError = (status: number, error: any) => Object.assign(new Error(`status ${status}`), { response: { status, data: { error } } });

const BAD = `policy p severity high:\n  always tool("x") ->`;
const BAD_ERR = { line: 2, col: 21, message: "expected a formula" };

function renderEditor(props: Partial<Parameters<typeof PolicyEditor>[0]> = {}) {
  const onDone = vi.fn();
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <PolicyEditor onDone={onDone} {...props} />
    </QueryClientProvider>
  );
  return { onDone, source: screen.getByLabelText("Policy source") as HTMLTextAreaElement };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.validate.mockImplementation(async (src: string) =>
    src === BAD ? { ok: false, errors: [BAD_ERR] } : { ok: true, errors: [] });
});

describe("PolicyErrorList", () => {
  it("shows position, message and a caret under the column", () => {
    render(<PolicyErrorList source={BAD} errors={[BAD_ERR]} />);
    expect(screen.getByText("Line 2, col 21: expected a formula")).toBeInTheDocument();
    const pre = screen.getByRole("alert").querySelector("pre")!;
    expect(pre.textContent).toBe(`  always tool("x") ->\n${" ".repeat(20)}^`);
  });

  it("omits the snippet when the line is out of range", () => {
    render(<PolicyErrorList source="x" errors={[{ line: 9, col: 1, message: "eof" }]} />);
    expect(screen.getByText("Line 9, col 1: eof")).toBeInTheDocument();
    expect(screen.getByRole("alert").querySelector("pre")).toBeNull();
  });
});

describe("PolicyEditor", () => {
  it("validates as you type and shows server errors inline", async () => {
    const { source } = renderEditor();
    fireEvent.change(source, { target: { value: BAD } });
    expect(await screen.findByText("Line 2, col 21: expected a formula")).toBeInTheDocument();
    expect(api.validate).toHaveBeenLastCalledWith(BAD);
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();

    fireEvent.change(source, { target: { value: "policy ok severity low:\n  always true" } });
    await waitFor(() => expect(screen.queryByText(/expected a formula/)).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Create" })).toBeEnabled();
  });

  it("drops stale errors when validation itself fails", async () => {
    const { source } = renderEditor();
    fireEvent.change(source, { target: { value: BAD } });
    await screen.findByText(/expected a formula/);
    api.validate.mockRejectedValue(new Error("network"));
    fireEvent.change(source, { target: { value: "policy ok severity low:\n  always true" } });
    await waitFor(() => expect(screen.queryByText(/expected a formula/)).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Create" })).toBeEnabled();
  });

  it("creates a policy with its scope and closes", async () => {
    api.create.mockResolvedValue({} as TracePolicy);
    const { onDone, source } = renderEditor();
    fireEvent.change(source, { target: { value: "policy ok severity low:\n  always true" } });
    fireEvent.change(screen.getByLabelText("Action"), { target: { value: "block" } });
    fireEvent.change(screen.getByLabelText("Environment"), { target: { value: "prod" } });
    await screen.findByRole("option", { name: "billing-bot" });
    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: "a1" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(api.create).toHaveBeenCalledWith({
      source: "policy ok severity low:\n  always true", action: "block", agent_id: "a1", environment: "prod", enabled: true,
    });
  });

  it("shows the parse errors from a 422 on save", async () => {
    api.create.mockRejectedValue(httpError(422, {
      code: "invalid_policy", message: "bad", details: { errors: [{ line: 1, col: 8, message: "unknown tag @nope" }] },
    }));
    const { onDone } = renderEditor();
    await waitFor(() => expect(api.validate).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText("Line 1, col 8: unknown tag @nope")).toBeInTheDocument();
    expect(onDone).not.toHaveBeenCalled();
  });

  it("shows other save errors as a message", async () => {
    api.create.mockRejectedValue(httpError(409, { code: "policy_name_taken", message: "policy name 'p' already exists" }));
    renderEditor();
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText("policy name 'p' already exists")).toBeInTheDocument();
  });

  it("edits an existing policy through update", async () => {
    const policy = {
      id: "p1", source: "policy ok severity low:\n  always true", action: "alert", agent_id: null,
      environment: null, enabled: false,
    } as TracePolicy;
    api.update.mockResolvedValue(policy);
    const { onDone, source } = renderEditor({ policy });
    expect(source.value).toBe(policy.source);
    expect(screen.getByLabelText("Action")).toHaveValue("alert");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(api.update).toHaveBeenCalledWith("p1", {
      source: policy.source, action: "alert", agent_id: null, environment: null, enabled: false,
    });
    expect(api.create).not.toHaveBeenCalled();
  });
});

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import AcceptInvite from "./AcceptInvite";
import Integrations from "./Integrations";
import Settings from "./Settings";
import { authApi, integrationsApi } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import { renderWithProviders } from "@/test/render";

vi.mock("@/api/auth", () => ({
  authApi: {
    me: vi.fn(), members: vi.fn(), listApiKeys: vi.fn(), sessions: vi.fn(), invite: vi.fn(), acceptInvite: vi.fn(),
    changePassword: vi.fn(),
  },
  integrationsApi: { list: vi.fn(), create: vi.fn(), rotateSigningSecret: vi.fn() },
}));
const auth = vi.mocked(authApi);
const integrations = vi.mocked(integrationsApi);
const me = { user: { id: "u1", email: "a@b.co", full_name: "A" }, organizations: [], role: "admin", active_org_id: "o1" };

beforeEach(() => {
  vi.clearAllMocks();
  useAuthStore.getState().clear();
  auth.me.mockResolvedValue(me);
  auth.members.mockResolvedValue([]);
  auth.listApiKeys.mockResolvedValue([]);
  auth.sessions.mockResolvedValue([]);
  integrations.list.mockResolvedValue([]);
});

describe("Settings invite", () => {
  it("sends the invite and shows where it went, never a password", async () => {
    auth.invite.mockResolvedValue({ email: "new@b.co", role: "viewer", status: "pending", invite_id: "i1",
                                    expires_at: "2026-10-24T00:00:00" });
    renderWithProviders(<Settings />);
    fireEvent.change(await screen.findByPlaceholderText("invite@example.com"), { target: { value: "new@b.co" } });
    fireEvent.change(screen.getByDisplayValue("developer"), { target: { value: "viewer" } });
    fireEvent.click(screen.getByText("Invite"));
    const result = await screen.findByTestId("invite-result");
    expect(auth.invite).toHaveBeenCalledWith({ email: "new@b.co", role: "viewer" });
    expect(result).toHaveTextContent("Invite emailed to new@b.co");
    expect(document.body.textContent).not.toMatch(/ChangeMe|password:/i);
    expect(screen.queryByRole("option", { name: "owner" })).toBeNull();
  });

  it("says when an existing account was added", async () => {
    auth.invite.mockResolvedValue({ email: "old@b.co", role: "admin", status: "added", invite_id: null, expires_at: null });
    renderWithProviders(<Settings />);
    fireEvent.change(await screen.findByPlaceholderText("invite@example.com"), { target: { value: "old@b.co" } });
    fireEvent.click(screen.getByText("Invite"));
    expect(await screen.findByTestId("invite-result")).toHaveTextContent("old@b.co is now a admin.");
  });

  it("changes the password and keeps the new session", async () => {
    auth.changePassword.mockResolvedValue({ access_token: "a2", refresh_token: "r2", token_type: "bearer" });
    renderWithProviders(<Settings />);
    fireEvent.change(await screen.findByPlaceholderText("Current password"), { target: { value: "old-pass" } });
    fireEvent.change(screen.getByPlaceholderText(/New password/), { target: { value: "New-pass1234" } });
    fireEvent.click(screen.getByText("Change"));
    await screen.findByText(/Password changed/);
    expect(auth.changePassword).toHaveBeenCalledWith({ current_password: "old-pass", new_password: "New-pass1234" });
    expect(useAuthStore.getState()).toMatchObject({ accessToken: "a2", refreshToken: "r2" });
  });
});

describe("AcceptInvite", () => {
  const show = (route: string) =>
    renderWithProviders(<AcceptInvite />, { path: "/accept-invite", route });

  it("accepts with the token from the link and signs in", async () => {
    auth.acceptInvite.mockResolvedValue({ access_token: "a1", refresh_token: "r1", token_type: "bearer" });
    show("/accept-invite?token=tok_123");
    fireEvent.change(screen.getByPlaceholderText(/Choose a password/), { target: { value: "Secret1234!" } });
    fireEvent.click(screen.getByText("Join"));
    await waitFor(() => expect(useAuthStore.getState().user?.email).toBe("a@b.co"));
    expect(auth.acceptInvite).toHaveBeenCalledWith({ token: "tok_123", password: "Secret1234!" });
    expect(useAuthStore.getState()).toMatchObject({ accessToken: "a1", refreshToken: "r1", orgId: "o1" });
  });

  it("shows the server's refusal", async () => {
    auth.acceptInvite.mockRejectedValue({ response: { data: { error: { message: "invalid_invite" } } } });
    show("/accept-invite?token=used");
    fireEvent.change(screen.getByPlaceholderText(/Choose a password/), { target: { value: "Secret1234!" } });
    fireEvent.click(screen.getByText("Join"));
    expect(await screen.findByText("invalid_invite")).toBeInTheDocument();
    expect(useAuthStore.getState().accessToken).toBeNull();
  });

  it("needs a token", () => {
    show("/accept-invite");
    expect(screen.getByText("This invite link has no token.")).toBeInTheDocument();
  });
});

describe("Integrations signing secret", () => {
  const row = { id: "i1", org_id: "o1", kind: "webhook", name: "Hook", config: {}, enabled: true, last_sync_at: null,
                last_status: "unknown", error_count: 0, last_error: "", created_at: "2026-10-21T00:00:00" };

  it("shows the secret once on create and again on rotation", async () => {
    integrations.create.mockResolvedValue({ ...row, signing_secret: "a".repeat(64) });
    integrations.list.mockResolvedValue([row]);
    integrations.rotateSigningSecret.mockResolvedValue({ signing_secret: "b".repeat(64) });
    renderWithProviders(<Integrations />);
    fireEvent.click(screen.getByText("Add integration"));
    expect(await screen.findByTestId("signing-secret")).toHaveTextContent("a".repeat(64));
    fireEvent.click(await screen.findByText("new secret"));
    await waitFor(() => expect(screen.getByTestId("signing-secret")).toHaveTextContent("b".repeat(64)));
    expect(integrations.rotateSigningSecret).toHaveBeenCalledWith("i1");
  });
});

export type TokenPair = {
  access_token: string;
  refresh_token: string;
  token_type: string;
};

export type InviteRequest = {
  email: string;
  role?: string;
};

// POST /api/auth/invite from SG-P1 on; carries no password or token
export type Invite = {
  email: string;
  role: string;
  status: "added" | "pending";
  invite_id: string | null;
  expires_at: string | null;
};

export type AcceptInviteRequest = {
  token: string;
  password: string;
};

export type PasswordChangeRequest = {
  current_password: string;
  new_password: string;
};

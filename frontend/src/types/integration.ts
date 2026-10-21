export type Integration = {
  id: string;
  org_id: string;
  kind: string;
  name: string;
  config: Record<string, any>;
  enabled: boolean;
  last_sync_at: string | null;
  last_status: string;
  error_count: number;
  last_error: string;
  created_at: string;
};

// signing_secret is shown once, on create
export type IntegrationCreated = Integration & { signing_secret: string | null };

export type SigningSecret = {
  signing_secret: string;
};

export type SlackOAuthStart = {
  url: string;
};

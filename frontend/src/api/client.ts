import axios, { AxiosError } from "axios";
import { useAuthStore } from "@/stores/authStore";

export const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";
export const WS_URL = import.meta.env.VITE_WS_URL || "ws://localhost:8000";

export const api = axios.create({
  baseURL: API_URL,
  headers: { "Content-Type": "application/json" },
});

api.interceptors.request.use((cfg) => {
  const token = useAuthStore.getState().accessToken;
  if (token) {
    cfg.headers.Authorization = `Bearer ${token}`;
  }
  return cfg;
});

let refreshing: Promise<string> | null = null;

api.interceptors.response.use(
  (r) => r,
  async (err: AxiosError) => {
    const original: any = err.config;
    if (err.response?.status === 401 && !original?._retry) {
      original._retry = true;
      const refresh = useAuthStore.getState().refreshToken;
      if (!refresh) {
        useAuthStore.getState().clear();
        return Promise.reject(err);
      }
      try {
        if (!refreshing) {
          refreshing = axios
            .post(`${API_URL}/api/auth/refresh`, { refresh_token: refresh })
            .then((r) => {
              const { access_token, refresh_token } = r.data;
              useAuthStore.getState().setTokens(access_token, refresh_token);
              return access_token as string;
            })
            .finally(() => {
              refreshing = null;
            });
        }
        const newToken = await refreshing;
        original.headers.Authorization = `Bearer ${newToken}`;
        return api(original);
      } catch (e) {
        useAuthStore.getState().clear();
        return Promise.reject(e);
      }
    }
    return Promise.reject(err);
  }
);

export function errorMessage(err: any): string {
  return (
    err?.response?.data?.error?.message ||
    err?.response?.data?.detail ||
    err?.message ||
    "Unknown error"
  );
}

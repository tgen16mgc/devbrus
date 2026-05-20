/**
 * API client for CloakBrowser Manager backend.
 */

export interface Profile {
  id: string;
  name: string;
  fingerprint_seed: number;
  proxy: string | null;
  timezone: string | null;
  locale: string | null;
  platform: string;
  user_agent: string | null;
  screen_width: number;
  screen_height: number;
  gpu_vendor: string | null;
  gpu_renderer: string | null;
  hardware_concurrency: number | null;
  humanize: boolean;
  human_preset: string;
  headless: boolean;
  geoip: boolean;
  clipboard_sync: boolean;
  auto_launch: boolean;
  color_scheme: string | null;
  launch_args: string[];
  notes: string | null;
  group: string | null;
  sort_order: number;
  proxy_status: string | null;
  user_data_dir: string;
  created_at: string;
  updated_at: string;
  tags: { tag: string; color: string | null }[];
  status: "running" | "stopped";
  vnc_ws_port: number | null;
  cdp_url: string | null;
}

export interface ProfileCreateData {
  name: string;
  fingerprint_seed?: number | null;
  proxy?: string | null;
  timezone?: string | null;
  locale?: string | null;
  platform?: string;
  user_agent?: string | null;
  screen_width?: number;
  screen_height?: number;
  gpu_vendor?: string | null;
  gpu_renderer?: string | null;
  hardware_concurrency?: number | null;
  humanize?: boolean;
  human_preset?: string;
  headless?: boolean;
  geoip?: boolean;
  clipboard_sync?: boolean;
  auto_launch?: boolean;
  color_scheme?: string | null;
  launch_args?: string[];
  notes?: string | null;
  group?: string | null;
  sort_order?: number;
  proxy_status?: string | null;
  tags?: { tag: string; color: string | null }[];
}

export interface LaunchResult {
  profile_id: string;
  status: string;
  vnc_ws_port: number | null;
  display: string | null;
  cdp_url: string | null;
}

export interface SystemStatus {
  running_count: number;
  binary_version: string;
  profiles_total: number;
}

export interface OperatorResult {
  profile_id: string;
  status: "ok" | "error";
  detail?: string;
  url?: string | null;
  title?: string;
  page_status?: number | string | null;
  screenshot?: string;
  vnc_ws_port?: number | null;
  display?: string | null;
}

export interface OperatorBulkResponse {
  action: "launch" | "stop" | "restart";
  results: OperatorResult[];
}

export interface OperatorAutomationResponse {
  action: string;
  results: OperatorResult[];
}

export interface NativeGridFrame {
  title: string;
  left: number;
  top: number;
  width: number;
  height: number;
}

export interface OperatorNativeGridResponse {
  status: "ok" | "partial" | "error";
  results: OperatorResult[];
  frames: NativeGridFrame[];
}

export interface OperatorNativeGridInput {
  profileIds: string[];
  columns: number;
  rows: number;
  bounds: { left: number; top: number; width: number; height: number };
  gap?: number;
  scale?: number;
  strategy?: "index" | "title";
  apply?: boolean;
}

export interface OperatorImportCsvResponse {
  created: number;
  skipped: Array<{ row: number; name?: string; reason: string }>;
  invalid: Array<{ row: number; name?: string; reason: string }>;
  profile_ids: string[];
}

export interface OperatorLayout {
  id: string;
  name: string;
  mode: "dashboard" | "native";
  columns: number;
  rows: number;
  tile_scale: number;
  monitor: string | null;
  profile_order: string[];
  group: string | null;
  created_at: string;
  updated_at: string;
}

export type OperatorLayoutInput = Omit<OperatorLayout, "id" | "created_at" | "updated_at">;

export interface OperatorEvent {
  id: string;
  event_type: string;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface MetadataExport {
  profiles: Array<Record<string, unknown>>;
  layouts: OperatorLayout[];
  events: OperatorEvent[];
}

class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

// Global 401 callback — set by App to trigger login page on auth failure
let _onUnauthorized: (() => void) | null = null;
export function setOnUnauthorized(cb: (() => void) | null) {
  _onUnauthorized = cb;
}

async function request<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    if (res.status === 401 && _onUnauthorized) {
      _onUnauthorized();
      throw new ApiError(401, "Unauthorized");
    }
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, body.detail || res.statusText);
  }
  return res.json();
}

export const api = {
  authStatus: () =>
    request<{ auth_required: boolean; authenticated: boolean }>("/api/auth/status"),

  login: (token: string) =>
    request<{ ok: boolean }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),

  logout: () =>
    request<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),

  listProfiles: () => request<Profile[]>("/api/profiles"),

  getProfile: (id: string) => request<Profile>(`/api/profiles/${id}`),

  createProfile: (data: ProfileCreateData) =>
    request<Profile>("/api/profiles", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  updateProfile: (id: string, data: Partial<ProfileCreateData>) =>
    request<Profile>(`/api/profiles/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),

  deleteProfile: (id: string) =>
    request<{ ok: boolean }>(`/api/profiles/${id}`, { method: "DELETE" }),

  launchProfile: (id: string) =>
    request<LaunchResult>(`/api/profiles/${id}/launch`, { method: "POST" }),

  stopProfile: (id: string) =>
    request<{ ok: boolean }>(`/api/profiles/${id}/stop`, { method: "POST" }),

  getStatus: () => request<SystemStatus>("/api/status"),

  importCsv: (csvText: string, validateProxies = false) =>
    request<OperatorImportCsvResponse>("/api/operator/import-csv", {
      method: "POST",
      body: JSON.stringify({ csv_text: csvText, validate_proxies: validateProxies }),
    }),

  bulkProfiles: (
    action: "launch" | "stop" | "restart",
    profileIds: string[],
    concurrency = 5,
  ) =>
    request<OperatorBulkResponse>("/api/operator/bulk", {
      method: "POST",
      body: JSON.stringify({ action, profile_ids: profileIds, concurrency }),
    }),

  automateProfiles: (
    action: string,
    profileIds: string[],
    options: { url?: string; concurrency?: number } = {},
  ) =>
    request<OperatorAutomationResponse>("/api/operator/automation", {
      method: "POST",
      body: JSON.stringify({
        action,
        profile_ids: profileIds,
        url: options.url,
        concurrency: options.concurrency ?? 5,
      }),
    }),

  gridNativeWindows: (input: OperatorNativeGridInput) =>
    request<OperatorNativeGridResponse>("/api/operator/native-grid", {
      method: "POST",
      body: JSON.stringify({
        profile_ids: input.profileIds,
        columns: input.columns,
        rows: input.rows,
        bounds: input.bounds,
        gap: input.gap ?? 10,
        scale: input.scale ?? 1,
        strategy: input.strategy ?? "index",
        apply: input.apply ?? true,
      }),
    }),

  listLayouts: () => request<OperatorLayout[]>("/api/operator/layouts"),

  createLayout: (data: OperatorLayoutInput) =>
    request<OperatorLayout>("/api/operator/layouts", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  updateLayout: (id: string, data: Partial<OperatorLayoutInput>) =>
    request<OperatorLayout>(`/api/operator/layouts/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),

  deleteLayout: (id: string) =>
    request<{ ok: boolean }>(`/api/operator/layouts/${id}`, { method: "DELETE" }),

  listEvents: (limit = 100) =>
    request<OperatorEvent[]>(`/api/operator/events?limit=${encodeURIComponent(limit)}`),

  exportMetadata: () => request<MetadataExport>("/api/operator/export-metadata"),

  importMetadata: (data: Partial<MetadataExport>) =>
    request<unknown>("/api/operator/import-metadata", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  setClipboard: (id: string, text: string) =>
    request<{ ok: boolean }>(`/api/profiles/${id}/clipboard`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),

  getClipboard: (id: string) =>
    request<{ text: string }>(`/api/profiles/${id}/clipboard`),
};

import { describe, it, expect, vi, beforeEach } from "vitest";
import { api } from "./api";

// Mock fetch globally
const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

function jsonResponse(data: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    json: () => Promise.resolve(data),
  };
}

beforeEach(() => {
  mockFetch.mockReset();
});

// ── listProfiles ────────────────────────────────────────────────────────────

describe("api.listProfiles", () => {
  it("returns profile array on success", async () => {
    const profiles = [{ id: "1", name: "Test" }];
    mockFetch.mockResolvedValueOnce(jsonResponse(profiles));
    const result = await api.listProfiles();
    expect(result).toEqual(profiles);
    expect(mockFetch).toHaveBeenCalledWith("/api/profiles", {
      headers: { "Content-Type": "application/json" },
    });
  });
});

// ── createProfile ───────────────────────────────────────────────────────────

describe("api.createProfile", () => {
  it("sends POST with JSON body", async () => {
    const profile = { id: "2", name: "New" };
    mockFetch.mockResolvedValueOnce(jsonResponse(profile));
    await api.createProfile({ name: "New" });
    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toBe("/api/profiles");
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({ name: "New" });
  });
});

// ── updateProfile ───────────────────────────────────────────────────────────

describe("api.updateProfile", () => {
  it("sends PUT with JSON body", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ id: "1", name: "Updated" }));
    await api.updateProfile("1", { name: "Updated" });
    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toBe("/api/profiles/1");
    expect(options.method).toBe("PUT");
  });
});

// ── deleteProfile ───────────────────────────────────────────────────────────

describe("api.deleteProfile", () => {
  it("sends DELETE request", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ ok: true }));
    const result = await api.deleteProfile("1");
    expect(result).toEqual({ ok: true });
    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toBe("/api/profiles/1");
    expect(options.method).toBe("DELETE");
  });
});

// ── launchProfile ───────────────────────────────────────────────────────────

describe("api.launchProfile", () => {
  it("sends POST to launch endpoint", async () => {
    const result = { profile_id: "1", status: "running", vnc_ws_port: 6100, display: ":100" };
    mockFetch.mockResolvedValueOnce(jsonResponse(result));
    const data = await api.launchProfile("1");
    expect(data.vnc_ws_port).toBe(6100);
    expect(mockFetch.mock.calls[0][0]).toBe("/api/profiles/1/launch");
  });
});

// ── stopProfile ─────────────────────────────────────────────────────────────

describe("api.stopProfile", () => {
  it("sends POST to stop endpoint", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ ok: true }));
    await api.stopProfile("1");
    expect(mockFetch.mock.calls[0][0]).toBe("/api/profiles/1/stop");
  });
});

// ── setClipboard ────────────────────────────────────────────────────────────

describe("api.setClipboard", () => {
  it("sends POST with text body", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ ok: true }));
    await api.setClipboard("1", "hello");
    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toBe("/api/profiles/1/clipboard");
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({ text: "hello" });
  });
});

// ── getClipboard ────────────────────────────────────────────────────────────

describe("api.getClipboard", () => {
  it("returns clipboard text", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ text: "copied" }));
    const result = await api.getClipboard("1");
    expect(result.text).toBe("copied");
  });
});

// ── Operator cockpit endpoints ─────────────────────────────────────────────

describe("operator cockpit api", () => {
  it("imports CSV text", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ created: 1, skipped: [], invalid: [], profile_ids: ["p1"] }));
    await api.importCsv("profile_name\np1", true);
    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toBe("/api/operator/import-csv");
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({
      csv_text: "profile_name\np1",
      validate_proxies: true,
    });
  });

  it("sends bulk profile actions with fixed concurrency", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ action: "launch", results: [] }));
    await api.bulkProfiles("launch", ["p1", "p2"], 5);
    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toBe("/api/operator/bulk");
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({
      action: "launch",
      profile_ids: ["p1", "p2"],
      concurrency: 5,
    });
  });

  it("sends automation actions", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ action: "open_url", results: [] }));
    await api.automateProfiles("open_url", ["p1"], { url: "https://example.com" });
    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toBe("/api/operator/automation");
    expect(JSON.parse(options.body)).toEqual({
      action: "open_url",
      profile_ids: ["p1"],
      url: "https://example.com",
      concurrency: 5,
    });
  });

  it("creates layouts", async () => {
    const layout = {
      id: "l1",
      name: "Work",
      mode: "dashboard",
      columns: 4,
      rows: 4,
      tile_scale: 0.82,
      monitor: null,
      profile_order: [],
      group: null,
      created_at: "now",
      updated_at: "now",
    };
    mockFetch.mockResolvedValueOnce(jsonResponse(layout));
    await api.createLayout({
      name: "Work",
      mode: "dashboard",
      columns: 4,
      rows: 4,
      tile_scale: 0.82,
      monitor: null,
      profile_order: [],
      group: null,
    });
    expect(mockFetch.mock.calls[0][0]).toBe("/api/operator/layouts");
  });
});

// ── Error handling ──────────────────────────────────────────────────────────

describe("error handling", () => {
  it("throws ApiError with detail on non-ok response", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 404,
      statusText: "Not Found",
      json: () => Promise.resolve({ detail: "Profile not found" }),
    });
    await expect(api.getProfile("bad")).rejects.toThrow("Profile not found");
  });

  it("falls back to statusText when response is not JSON", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      json: () => Promise.reject(new Error("not json")),
    });
    await expect(api.getStatus()).rejects.toThrow("Internal Server Error");
  });
});

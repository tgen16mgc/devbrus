import {
  ArrowLeft,
  ArrowRight,
  Columns3,
  Download,
  ExternalLink,
  FileUp,
  Grid3X3,
  LayoutDashboard,
  MonitorUp,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  Square,
  Upload,
  ZoomIn,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api, type OperatorEvent, type OperatorLayout, type Profile } from "../lib/api";
import { ProfileViewer } from "./ProfileViewer";
import { StatusIndicator } from "./StatusIndicator";

type GridMode = "overview" | "work" | "focus";
type ViewMode = "dashboard" | "native";

interface OperatorCockpitProps {
  profiles: Profile[];
  onRefresh: () => Promise<void>;
  onSelectProfile: (id: string) => void;
  onNewProfile: () => void;
}

const GRID_PRESETS: Record<GridMode, { label: string; columns: number; rows: number; scale: number }> = {
  overview: { label: "Overview 50", columns: 10, rows: 5, scale: 0.52 },
  work: { label: "Work 16", columns: 4, rows: 4, scale: 0.82 },
  focus: { label: "Focus 4", columns: 2, rows: 2, scale: 1 },
};

function nativeReadableScale(mode: GridMode, selectedCount: number) {
  if (mode === "overview") return selectedCount <= 6 ? 1 : 0.75;
  if (mode === "work") return selectedCount <= 4 ? 1 : 0.9;
  return 1;
}

const SAMPLE_CSV = `profile_name,proxy_url,group,notes
profile_001,http://user:pass@host:8080,batch_a,optional note
profile_002,,testing_no_proxy,local test profile`;

function resultSummary(results: Array<{ status: string }>) {
  const ok = results.filter((r) => r.status === "ok").length;
  const failed = results.length - ok;
  return `${ok} ok${failed ? `, ${failed} failed` : ""}`;
}

export function OperatorCockpit({ profiles, onRefresh, onSelectProfile, onNewProfile }: OperatorCockpitProps) {
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [gridMode, setGridMode] = useState<GridMode>("work");
  const [viewMode, setViewMode] = useState<ViewMode>("dashboard");
  const [url, setUrl] = useState("");
  const [csvText, setCsvText] = useState("");
  const [metadataText, setMetadataText] = useState("");
  const [validateProxies, setValidateProxies] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [layouts, setLayouts] = useState<OperatorLayout[]>([]);
  const [events, setEvents] = useState<OperatorEvent[]>([]);
  const [layoutName, setLayoutName] = useState("");

  const filteredProfiles = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return profiles;
    return profiles.filter((profile) => {
      const haystack = [
        profile.name,
        profile.group ?? "",
        profile.proxy ?? "",
        profile.platform,
        profile.proxy_status ?? "",
      ].join(" ").toLowerCase();
      return haystack.includes(needle);
    });
  }, [profiles, search]);

  const selectedProfiles = useMemo(
    () => profiles.filter((profile) => selectedIds.has(profile.id)),
    [profiles, selectedIds],
  );

  const runningProfiles = useMemo(
    () => filteredProfiles.filter((profile) => profile.status === "running"),
    [filteredProfiles],
  );

  const preset = GRID_PRESETS[gridMode];
  const visibleProfiles = gridMode === "focus"
    ? (selectedProfiles.length ? selectedProfiles : runningProfiles).slice(0, 4)
    : runningProfiles;

  useEffect(() => {
    setSelectedIds((prev) => new Set([...prev].filter((id) => profiles.some((profile) => profile.id === id))));
  }, [profiles]);

  useEffect(() => {
    void refreshOperatorData();
  }, []);

  const refreshOperatorData = async () => {
    const [nextLayouts, nextEvents] = await Promise.all([
      api.listLayouts().catch(() => []),
      api.listEvents(8).catch(() => []),
    ]);
    setLayouts(nextLayouts);
    setEvents(nextEvents);
  };

  const setSelection = (id: string, checked: boolean) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const selectedProfileIds = [...selectedIds];
  const runningCount = profiles.filter((profile) => profile.status === "running").length;
  const noProxyCount = profiles.filter((profile) => !profile.proxy).length;

  const runAction = async (label: string, action: () => Promise<string>) => {
    setBusy(true);
    setMessage(null);
    try {
      const summary = await action();
      setMessage(`${label}: ${summary}`);
      await onRefresh();
      await refreshOperatorData();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : `${label} failed`);
    } finally {
      setBusy(false);
    }
  };

  const requireSelection = () => {
    if (selectedProfileIds.length === 0) {
      setMessage("Select at least one profile.");
      return false;
    }
    return true;
  };

  const bulk = (action: "launch" | "stop" | "restart") => {
    if (!requireSelection()) return;
    void runAction(action, async () => {
      const response = await api.bulkProfiles(action, selectedProfileIds, 5);
      return resultSummary(response.results);
    });
  };

  const automate = (action: string) => {
    if (!requireSelection()) return;
    if (action === "open_url" && !url.trim()) {
      setMessage("Enter a URL first.");
      return;
    }
    void runAction(action, async () => {
      const response = await api.automateProfiles(action, selectedProfileIds, {
        url: url.trim() || undefined,
        concurrency: 5,
      });
      return resultSummary(response.results);
    });
  };

  const arrangeNativeGrid = () => {
    if (!requireSelection()) return;
    const selectedRunningIds = selectedProfiles
      .filter((profile) => profile.status === "running")
      .map((profile) => profile.id);
    if (selectedRunningIds.length === 0) {
      setMessage("Selected profiles must be running.");
      return;
    }
    void runAction("Native grid", async () => {
      const response = await api.gridNativeWindows({
        profileIds: selectedRunningIds,
        columns: preset.columns,
        rows: preset.rows,
        bounds: {
          left: 0,
          top: 0,
          width: window.screen.availWidth || 1440,
          height: window.screen.availHeight || 900,
        },
        gap: 10,
        scale: nativeReadableScale(gridMode, selectedRunningIds.length),
        strategy: "index",
      });
      return `${resultSummary(response.results)} · ${response.frames.length} frames`;
    });
  };

  const importCsv = () => {
    if (!csvText.trim()) {
      setMessage("Paste CSV text first.");
      return;
    }
    void runAction("CSV import", async () => {
      const response = await api.importCsv(csvText, validateProxies);
      return `${response.created} created, ${response.skipped.length} skipped, ${response.invalid.length} invalid`;
    });
  };

  const saveLayout = () => {
    const name = layoutName.trim() || GRID_PRESETS[gridMode].label;
    void runAction("Save layout", async () => {
      const layout = await api.createLayout({
        name,
        mode: viewMode,
        columns: preset.columns,
        rows: preset.rows,
        tile_scale: preset.scale,
        monitor: viewMode === "native" ? "default" : null,
        profile_order: visibleProfiles.map((profile) => profile.id),
        group: null,
      });
      setLayoutName("");
      return layout.name;
    });
  };

  const exportMetadata = () => {
    void runAction("Export metadata", async () => {
      const data = await api.exportMetadata();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const href = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = href;
      a.download = "cloakbrowser-operator-export.json";
      a.click();
      URL.revokeObjectURL(href);
      return `${data.profiles.length} profiles, ${data.layouts.length} layouts`;
    });
  };

  const importMetadata = () => {
    if (!metadataText.trim()) {
      setMessage("Paste metadata JSON first.");
      return;
    }
    void runAction("Import metadata", async () => {
      const parsed = JSON.parse(metadataText);
      await api.importMetadata(parsed);
      setMetadataText("");
      return "imported";
    });
  };

  return (
    <div className="min-h-full bg-surface-0 text-gray-100">
      <div className="border-b border-border bg-surface-1 px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold">Operator Cockpit</h2>
            <p className="text-xs text-gray-500">
              {profiles.length} profiles · {runningCount} running · {selectedIds.size} selected · {noProxyCount} local IP
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button className="btn-secondary flex items-center gap-1.5" onClick={onNewProfile}>
              <Plus className="h-3.5 w-3.5" />
              Profile
            </button>
            <button className="btn-secondary flex items-center gap-1.5" onClick={() => void onRefresh()}>
              <RefreshCw className="h-3.5 w-3.5" />
              Refresh
            </button>
            <button className="btn-secondary flex items-center gap-1.5" onClick={exportMetadata} disabled={busy}>
              <Download className="h-3.5 w-3.5" />
              Export
            </button>
          </div>
        </div>

        <div className="mt-3 grid grid-cols-1 gap-3 xl:grid-cols-[minmax(260px,340px)_1fr]">
          <div className="space-y-3">
            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-gray-500" />
              <input
                className="input pl-8"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Search profiles, group, proxy..."
              />
            </div>
            <div className="flex gap-2">
              <button
                className="btn-secondary flex-1"
                onClick={() => setSelectedIds(new Set(filteredProfiles.map((profile) => profile.id)))}
              >
                Select visible
              </button>
              <button className="btn-secondary flex-1" onClick={() => setSelectedIds(new Set())}>
                Clear
              </button>
            </div>
          </div>

          <div className="grid gap-2 lg:grid-cols-[1fr_auto]">
            <div className="flex gap-2">
              <input
                className="input"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                placeholder="https://example.com"
              />
              <button className="btn-primary flex items-center gap-1.5" onClick={() => automate("open_url")} disabled={busy}>
                <ExternalLink className="h-3.5 w-3.5" />
                Open URL
              </button>
            </div>
            <div className="flex flex-wrap gap-2">
              <button className="btn-secondary" onClick={() => bulk("launch")} disabled={busy}>Launch</button>
              <button className="btn-secondary" onClick={() => bulk("restart")} disabled={busy}>Restart</button>
              <button className="btn-danger flex items-center gap-1.5" onClick={() => bulk("stop")} disabled={busy}>
                <Square className="h-3.5 w-3.5" />
                Stop
              </button>
            </div>
          </div>
        </div>

        {message && (
          <div className="mt-3 rounded-md border border-border bg-surface-2 px-3 py-2 text-xs text-gray-300">
            {message}
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 gap-0 xl:grid-cols-[320px_1fr]">
        <aside className="border-b border-border bg-surface-1 xl:border-b-0 xl:border-r">
          <div className="max-h-[calc(100vh-156px)] overflow-y-auto p-3">
            <div className="mb-3 flex items-center justify-between">
              <span className="text-xs font-semibold uppercase text-gray-500">Profiles</span>
              <span className="text-xs text-gray-500">{filteredProfiles.length}</span>
            </div>
            <div className="space-y-1">
              {filteredProfiles.map((profile) => (
                <label
                  key={profile.id}
                  className="flex cursor-pointer items-start gap-2 rounded-md border border-transparent px-2 py-2 hover:border-border-hover hover:bg-surface-2"
                >
                  <input
                    type="checkbox"
                    className="mt-1"
                    checked={selectedIds.has(profile.id)}
                    onChange={(event) => setSelection(profile.id, event.target.checked)}
                  />
                  <span className="min-w-0 flex-1" onDoubleClick={() => onSelectProfile(profile.id)}>
                    <span className="flex items-center gap-2">
                      <StatusIndicator status={profile.status} />
                      <span className="truncate text-sm font-medium">{profile.name}</span>
                    </span>
                    <span className="mt-1 flex flex-wrap gap-1 text-[11px] text-gray-500">
                      {profile.group && <span>{profile.group}</span>}
                      <span>{profile.proxy ? "Proxy" : "Local IP"}</span>
                      <span>{profile.platform}</span>
                    </span>
                  </span>
                </label>
              ))}
            </div>

            <div className="mt-5 space-y-2 border-t border-border pt-4">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold uppercase text-gray-500">CSV Import</span>
                <button className="text-xs text-gray-400 hover:text-gray-200" onClick={() => setCsvText(SAMPLE_CSV)}>
                  Sample
                </button>
              </div>
              <textarea
                className="input h-28 font-mono text-xs"
                value={csvText}
                onChange={(event) => setCsvText(event.target.value)}
                placeholder="profile_name,proxy_url,group,notes"
              />
              <label className="flex items-center gap-2 text-xs text-gray-400">
                <input
                  type="checkbox"
                  checked={validateProxies}
                  onChange={(event) => setValidateProxies(event.target.checked)}
                />
                Validate proxy format on import
              </label>
              <button className="btn-secondary flex w-full items-center justify-center gap-1.5" onClick={importCsv} disabled={busy}>
                <FileUp className="h-3.5 w-3.5" />
                Import CSV
              </button>
            </div>

            <div className="mt-5 space-y-2 border-t border-border pt-4">
              <span className="text-xs font-semibold uppercase text-gray-500">Metadata Import</span>
              <textarea
                className="input h-24 font-mono text-xs"
                value={metadataText}
                onChange={(event) => setMetadataText(event.target.value)}
                placeholder='{"profiles":[],"layouts":[],"events":[]}'
              />
              <button className="btn-secondary flex w-full items-center justify-center gap-1.5" onClick={importMetadata} disabled={busy}>
                <Upload className="h-3.5 w-3.5" />
                Import JSON
              </button>
            </div>
          </div>
        </aside>

        <main className="min-w-0">
          <div className="sticky top-0 z-10 border-b border-border bg-surface-1 px-3 py-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap gap-2">
                {(["overview", "work", "focus"] as GridMode[]).map((mode) => (
                  <button
                    key={mode}
                    className={gridMode === mode ? "btn-primary" : "btn-secondary"}
                    onClick={() => setGridMode(mode)}
                  >
                    {GRID_PRESETS[mode].label}
                  </button>
                ))}
                <button
                  className={viewMode === "dashboard" ? "btn-primary flex items-center gap-1.5" : "btn-secondary flex items-center gap-1.5"}
                  onClick={() => setViewMode("dashboard")}
                >
                  <LayoutDashboard className="h-3.5 w-3.5" />
                  Dashboard
                </button>
                <button
                  className={viewMode === "native" ? "btn-primary flex items-center gap-1.5" : "btn-secondary flex items-center gap-1.5"}
                  onClick={() => setViewMode("native")}
                >
                  <MonitorUp className="h-3.5 w-3.5" />
                  Native
                </button>
              </div>

              <div className="flex flex-wrap gap-2">
                <button className="btn-secondary flex items-center gap-1.5" onClick={() => automate("new_tab")} disabled={busy}>
                  <Plus className="h-3.5 w-3.5" />
                  Tab
                </button>
                <button className="btn-secondary" onClick={() => automate("reload")} disabled={busy}>
                  <RotateCcw className="h-3.5 w-3.5" />
                </button>
                <button className="btn-secondary" onClick={() => automate("back")} disabled={busy}>
                  <ArrowLeft className="h-3.5 w-3.5" />
                </button>
                <button className="btn-secondary" onClick={() => automate("forward")} disabled={busy}>
                  <ArrowRight className="h-3.5 w-3.5" />
                </button>
                <button className="btn-secondary" onClick={() => automate("inspect")} disabled={busy}>
                  Inspect
                </button>
              </div>
            </div>
          </div>

          <div className="border-b border-border bg-surface-0 px-3 py-2">
            <div className="flex flex-wrap items-center gap-2">
              <Columns3 className="h-4 w-4 text-gray-500" />
              <span className="text-xs text-gray-500">
                {preset.columns} columns · {visibleProfiles.length} live tiles · scale {preset.scale}
              </span>
              <input
                className="input h-8 max-w-52"
                value={layoutName}
                onChange={(event) => setLayoutName(event.target.value)}
                placeholder="Layout name"
              />
              <button className="btn-secondary flex items-center gap-1.5" onClick={saveLayout} disabled={busy}>
                <Save className="h-3.5 w-3.5" />
                Save layout
              </button>
              {layouts.slice(0, 4).map((layout) => (
                <button
                  key={layout.id}
                  className="rounded-md border border-border px-2 py-1 text-xs text-gray-400 hover:text-gray-200"
                  onClick={() => {
                    setViewMode(layout.mode);
                    setMessage(`Layout selected: ${layout.name}`);
                  }}
                >
                  {layout.name}
                </button>
              ))}
            </div>
          </div>

          {viewMode === "native" ? (
            <div className="flex min-h-[520px] items-center justify-center">
              <div className="max-w-md text-center">
                <Grid3X3 className="mx-auto mb-3 h-8 w-8 text-gray-500" />
                <h3 className="text-sm font-semibold">Native window grid</h3>
                <p className="mt-2 text-sm text-gray-500">
                  Arrange selected running CloakBrowser windows with the current grid preset.
                </p>
                <button
                  className="btn-primary mt-4 inline-flex items-center gap-1.5"
                  onClick={arrangeNativeGrid}
                  disabled={busy}
                >
                  <Grid3X3 className="h-3.5 w-3.5" />
                  Arrange selected windows
                </button>
              </div>
            </div>
          ) : (
            <div
              className="grid gap-2 p-2"
              style={{
                gridTemplateColumns: `repeat(${preset.columns}, minmax(180px, 1fr))`,
              }}
            >
              {visibleProfiles.map((profile) => (
                <section
                  key={profile.id}
                  className="min-w-0 overflow-hidden rounded-md border border-border bg-black"
                  style={{ height: gridMode === "overview" ? 180 : gridMode === "work" ? 260 : 420 }}
                >
                  <div className="flex items-center justify-between border-b border-border bg-surface-2 px-2 py-1">
                    <button className="truncate text-xs font-medium hover:text-accent" onClick={() => onSelectProfile(profile.id)}>
                      {profile.name}
                    </button>
                    <span className="flex items-center gap-1 text-[11px] text-gray-500">
                      {profile.proxy ? "Proxy" : "Local IP"}
                      <ZoomIn className="h-3 w-3" />
                    </span>
                  </div>
                  <ProfileViewer
                    profileId={profile.id}
                    cdpUrl={profile.cdp_url}
                    vncWsPort={profile.vnc_ws_port}
                    clipboardSync={profile.clipboard_sync}
                    onDisconnect={() => void onRefresh()}
                  />
                </section>
              ))}
              {visibleProfiles.length === 0 && (
                <div className="col-span-full flex min-h-[360px] items-center justify-center text-sm text-gray-500">
                  Launch profiles to see live tiles.
                </div>
              )}
            </div>
          )}

          {events.length > 0 && (
            <div className="border-t border-border bg-surface-1 px-3 py-2">
              <div className="mb-2 text-xs font-semibold uppercase text-gray-500">Recent events</div>
              <div className="grid gap-1 text-xs text-gray-500 md:grid-cols-2">
                {events.map((event) => (
                  <div key={event.id} className="truncate rounded bg-surface-2 px-2 py-1">
                    {event.event_type} · {new Date(event.created_at).toLocaleTimeString()}
                  </div>
                ))}
              </div>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

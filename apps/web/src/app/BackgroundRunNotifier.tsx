import { isTauri } from "@tauri-apps/api/core";
import { sendNotification } from "@tauri-apps/plugin-notification";
import { useEffect, useRef } from "react";

import type { LocalRun } from "../api";
import { syncDesktopBackgroundMode } from "./desktopPreferences";
import { loadDevicePreferences } from "./devicePreferences";

export function notificationForRunStatus(status: string): { title: string; body: string } | null {
  if (status === "review_required") {
    return { title: "Run ready for review", body: "Open Corvus to inspect the changes." };
  }
  if (status === "completed") {
    return { title: "Run completed", body: "Open Corvus to review the result." };
  }
  if (status === "failed" || status === "interrupted") {
    return { title: "Run needs attention", body: "Open Corvus to view redacted diagnostics." };
  }
  return null;
}

export function BackgroundRunNotifier({
  listRuns,
  storage,
  workspaceId
}: {
  listRuns(): Promise<LocalRun[]>;
  storage: Storage;
  workspaceId: string;
}) {
  const observed = useRef(new Map<string, string>());
  useEffect(() => {
    if (!isTauri()) return;
    let active = true;
    const preferences = loadDevicePreferences(storage, workspaceId);
    void syncDesktopBackgroundMode(preferences.runInBackground).catch(() => undefined);
    async function poll(): Promise<void> {
      const runs = await listRuns();
      if (!active) return;
      const notificationsEnabled = loadDevicePreferences(storage, workspaceId).nativeNotifications;
      for (const run of runs) {
        const previous = observed.current.get(run.id);
        observed.current.set(run.id, run.status);
        if (!notificationsEnabled || previous === undefined || previous === run.status) continue;
        const notification = notificationForRunStatus(run.status);
        if (notification !== null) sendNotification(notification);
      }
    }
    void poll().catch(() => undefined);
    const timer = window.setInterval(() => void poll().catch(() => undefined), 15_000);
    return () => { active = false; window.clearInterval(timer); };
  }, [listRuns, storage, workspaceId]);
  return null;
}

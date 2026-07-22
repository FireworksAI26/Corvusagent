import { invoke, isTauri } from "@tauri-apps/api/core";
import { disable, enable, isEnabled } from "@tauri-apps/plugin-autostart";
import {
  isPermissionGranted,
  requestPermission
} from "@tauri-apps/plugin-notification";

const CORVUS_STORAGE_PREFIX = "corvus.";

type PersistDesktopPreferences = (payload: string) => Promise<void>;

function serializeCorvusPreferences(storage: Storage): string {
  const snapshot: Record<string, string> = {};
  for (let index = 0; index < storage.length; index += 1) {
    const key = storage.key(index);
    if (key === null || !key.startsWith(CORVUS_STORAGE_PREFIX)) continue;
    const value = storage.getItem(key);
    if (value !== null) snapshot[key] = value;
  }
  return JSON.stringify(snapshot);
}

function hydrateCorvusPreferences(storage: Storage, payload: string): void {
  const parsed: unknown = JSON.parse(payload);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("desktop_preferences_invalid");
  }
  for (const [key, value] of Object.entries(parsed)) {
    if (key.startsWith(CORVUS_STORAGE_PREFIX) && typeof value === "string") {
      storage.setItem(key, value);
    }
  }
}

export class MirroredPreferenceStorage implements Storage {
  private pendingSave: Promise<void> = Promise.resolve();

  constructor(
    private readonly storage: Storage,
    private readonly persist: PersistDesktopPreferences
  ) {}

  get length(): number {
    return this.storage.length;
  }

  clear(): void {
    this.storage.clear();
    this.queueSave();
  }

  getItem(key: string): string | null {
    return this.storage.getItem(key);
  }

  key(index: number): string | null {
    return this.storage.key(index);
  }

  removeItem(key: string): void {
    this.storage.removeItem(key);
    if (key.startsWith(CORVUS_STORAGE_PREFIX)) this.queueSave();
  }

  setItem(key: string, value: string): void {
    this.storage.setItem(key, value);
    if (key.startsWith(CORVUS_STORAGE_PREFIX)) this.queueSave();
  }

  async flush(): Promise<void> {
    await this.pendingSave;
  }

  private queueSave(): void {
    const payload = serializeCorvusPreferences(this.storage);
    this.pendingSave = this.pendingSave
      .then(() => this.persist(payload))
      .catch((error: unknown) => {
        console.error("Corvus could not persist desktop preferences.", error);
      });
  }
}

export async function createDesktopPreferenceStorage(storage: Storage): Promise<Storage> {
  if (!isTauri()) return storage;
  try {
    const payload = await invoke<string | null>("load_desktop_preferences");
    if (payload !== null) hydrateCorvusPreferences(storage, payload);
  } catch (error: unknown) {
    console.error("Corvus could not restore desktop preferences.", error);
  }
  return new MirroredPreferenceStorage(storage, (payload) => (
    invoke<void>("save_desktop_preferences", { payload })
  ));
}

export interface DesktopDeviceSettings {
  runInBackground: boolean;
  launchAtLogin: boolean;
  nativeNotifications: boolean;
}

export function desktopControlsAvailable(): boolean {
  return isTauri();
}

export async function syncDesktopBackgroundMode(enabled: boolean): Promise<void> {
  if (!isTauri()) return;
  await invoke("set_background_mode", { enabled });
}

export async function applyDesktopDeviceSettings(settings: DesktopDeviceSettings): Promise<void> {
  if (!isTauri()) throw new Error("desktop_controls_unavailable");
  await syncDesktopBackgroundMode(settings.runInBackground);
  const launchAtLoginEnabled = await isEnabled();
  if (settings.launchAtLogin !== launchAtLoginEnabled) {
    if (settings.launchAtLogin) await enable();
    else await disable();
  }
  if (settings.nativeNotifications && !await isPermissionGranted()) {
    const permission = await requestPermission();
    if (permission !== "granted") throw new Error("notification_permission_denied");
  }
}

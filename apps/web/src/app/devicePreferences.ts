const DEVICE_PREFERENCES_KEY_PREFIX = "corvus.device-preferences.v1";
const DEVICE_PREFERENCES_VERSION = 1;

export type ThemePreference = "system" | "light" | "dark";
export type ResponseTone = "concise" | "balanced" | "detailed";
export type SendKeyMode = "adaptive" | "enter" | "ctrl-enter";
export type SafetyGuidance = "standard" | "detailed";

export interface DevicePreferences {
  version: typeof DEVICE_PREFERENCES_VERSION;
  theme: ThemePreference;
  responseTone: ResponseTone;
  customRules: string;
  mcpNotes: string;
  sendKeyMode: SendKeyMode;
  safetyGuidance: SafetyGuidance;
  runInBackground: boolean;
  launchAtLogin: boolean;
  nativeNotifications: boolean;
}

export const DEFAULT_DEVICE_PREFERENCES: DevicePreferences = Object.freeze({
  version: DEVICE_PREFERENCES_VERSION,
  theme: "system",
  responseTone: "balanced",
  customRules: "",
  mcpNotes: "",
  sendKeyMode: "adaptive",
  safetyGuidance: "standard",
  runInBackground: false,
  launchAtLogin: false,
  nativeNotifications: false
});

function storageKey(workspaceId: string): string {
  return `${DEVICE_PREFERENCES_KEY_PREFIX}.${workspaceId}`;
}

function normalizeDevicePreferences(value: unknown): DevicePreferences | null {
  if (typeof value !== "object" || value === null) return null;
  const candidate = value as Record<string, unknown>;
  const baseIsValid = (
    candidate.version === DEVICE_PREFERENCES_VERSION &&
    (candidate.theme === "system" || candidate.theme === "light" || candidate.theme === "dark") &&
    (candidate.responseTone === "concise" ||
      candidate.responseTone === "balanced" ||
      candidate.responseTone === "detailed") &&
    typeof candidate.customRules === "string" &&
    typeof candidate.mcpNotes === "string"
  );
  const sendKeyModeIsValid = candidate.sendKeyMode === undefined ||
      candidate.sendKeyMode === "adaptive" ||
      candidate.sendKeyMode === "enter" ||
      candidate.sendKeyMode === "ctrl-enter";
  const safetyGuidanceIsValid = candidate.safetyGuidance === undefined ||
    candidate.safetyGuidance === "standard" || candidate.safetyGuidance === "detailed";
  const desktopSettingsAreValid = [
    candidate.runInBackground,
    candidate.launchAtLogin,
    candidate.nativeNotifications
  ].every((value) => value === undefined || typeof value === "boolean");
  if (!baseIsValid || !sendKeyModeIsValid || !safetyGuidanceIsValid || !desktopSettingsAreValid) return null;
  return {
    version: DEVICE_PREFERENCES_VERSION,
    theme: candidate.theme as ThemePreference,
    responseTone: candidate.responseTone as ResponseTone,
    customRules: candidate.customRules as string,
    mcpNotes: candidate.mcpNotes as string,
    sendKeyMode: (candidate.sendKeyMode ?? DEFAULT_DEVICE_PREFERENCES.sendKeyMode) as SendKeyMode,
    safetyGuidance: (candidate.safetyGuidance ?? DEFAULT_DEVICE_PREFERENCES.safetyGuidance) as SafetyGuidance,
    runInBackground: candidate.runInBackground === true,
    launchAtLogin: candidate.launchAtLogin === true,
    nativeNotifications: candidate.nativeNotifications === true
  };
}

export function loadDevicePreferences(storage: Storage, workspaceId: string): DevicePreferences {
  const key = storageKey(workspaceId);
  const serialized = storage.getItem(key);
  if (serialized === null) return { ...DEFAULT_DEVICE_PREFERENCES };
  try {
    const parsed = JSON.parse(serialized) as unknown;
    const normalized = normalizeDevicePreferences(parsed);
    if (normalized !== null) return normalized;
  } catch {
    // Invalid device-only data is removed below and never becomes runtime authority.
  }
  storage.removeItem(key);
  return { ...DEFAULT_DEVICE_PREFERENCES };
}

export function saveDevicePreferences(
  storage: Storage,
  workspaceId: string,
  preferences: DevicePreferences
): void {
  storage.setItem(storageKey(workspaceId), JSON.stringify(preferences));
}

import { describe, expect, it } from "vitest";

import { MemoryStorage } from "../test/memoryStorage";
import {
  DEFAULT_DEVICE_PREFERENCES,
  loadDevicePreferences,
  saveDevicePreferences
} from "./devicePreferences";

describe("device preferences", () => {
  it("keeps settings isolated by workspace on this device", () => {
    const storage = new MemoryStorage();
    saveDevicePreferences(storage, "workspace-a", {
      ...DEFAULT_DEVICE_PREFERENCES,
      responseTone: "concise",
      theme: "dark",
      sendKeyMode: "ctrl-enter",
      safetyGuidance: "detailed"
    });

    expect(loadDevicePreferences(storage, "workspace-a")).toMatchObject({
      responseTone: "concise",
      theme: "dark",
      sendKeyMode: "ctrl-enter",
      safetyGuidance: "detailed"
    });
    expect(loadDevicePreferences(storage, "workspace-b")).toEqual(DEFAULT_DEVICE_PREFERENCES);
  });

  it("rejects malformed or obsolete values instead of partially trusting them", () => {
    const storage = new MemoryStorage();
    storage.setItem("corvus.device-preferences.v1.workspace-a", JSON.stringify({
      version: 2,
      theme: "dark",
      responseTone: "concise"
    }));

    expect(loadDevicePreferences(storage, "workspace-a")).toEqual(DEFAULT_DEVICE_PREFERENCES);
    expect(storage.getItem("corvus.device-preferences.v1.workspace-a")).toBeNull();
  });

  it("migrates original v1 preferences without losing existing settings", () => {
    const storage = new MemoryStorage();
    storage.setItem("corvus.device-preferences.v1.workspace-a", JSON.stringify({
      version: 1,
      theme: "dark",
      responseTone: "concise",
      customRules: "Keep the original rule.",
      mcpNotes: "Keep the original MCP note."
    }));

    expect(loadDevicePreferences(storage, "workspace-a")).toEqual({
      ...DEFAULT_DEVICE_PREFERENCES,
      theme: "dark",
      responseTone: "concise",
      customRules: "Keep the original rule.",
      mcpNotes: "Keep the original MCP note."
    });
  });
});

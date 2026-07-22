import { describe, expect, it, vi } from "vitest";

import { MirroredPreferenceStorage } from "./desktopPreferences";

class MemoryStorage implements Storage {
  private readonly values = new Map<string, string>();

  get length(): number {
    return this.values.size;
  }

  clear(): void {
    this.values.clear();
  }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  key(index: number): string | null {
    return [...this.values.keys()][index] ?? null;
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

describe("MirroredPreferenceStorage", () => {
  it("persists Corvus keys while leaving unrelated browser data out of the native mirror", async () => {
    const storage = new MemoryStorage();
    const persist = vi.fn<(payload: string) => Promise<void>>().mockResolvedValue(undefined);
    const mirrored = new MirroredPreferenceStorage(storage, persist);

    mirrored.setItem("unrelated", "ignored");
    mirrored.setItem("corvus.local-first-run", "complete");
    mirrored.setItem("corvus.workspace-preference", "developer");
    await mirrored.flush();

    expect(persist).toHaveBeenCalledTimes(2);
    expect(JSON.parse(persist.mock.calls.at(-1)![0])).toEqual({
      "corvus.local-first-run": "complete",
      "corvus.workspace-preference": "developer"
    });
  });

  it("updates the native mirror after a Corvus preference is removed", async () => {
    const storage = new MemoryStorage();
    const persist = vi.fn<(payload: string) => Promise<void>>().mockResolvedValue(undefined);
    const mirrored = new MirroredPreferenceStorage(storage, persist);

    mirrored.setItem("corvus.local-first-run", "complete");
    mirrored.removeItem("corvus.local-first-run");
    await mirrored.flush();

    expect(JSON.parse(persist.mock.calls.at(-1)![0])).toEqual({});
  });
});

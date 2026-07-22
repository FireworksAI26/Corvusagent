import { describe, expect, it } from "vitest";

import { MemoryStorage } from "../test/memoryStorage";
import { loadDeviceThreads, saveDeviceThreads, type DeviceThread } from "./conversationStorage";

function thread(index: number, messageCount = 1): DeviceThread {
  const timestamp = new Date(Date.UTC(2026, 0, 1, 0, 0, index)).toISOString();
  return {
    id: `thread-${index}`,
    title: `Thread ${index}`,
    createdAt: timestamp,
    updatedAt: timestamp,
    messages: Array.from({ length: messageCount }, (_, messageIndex) => ({
      id: `message-${index}-${messageIndex}`,
      role: messageIndex % 2 === 0 ? "user" as const : "assistant" as const,
      content: "x".repeat(50_000),
      createdAt: timestamp
    }))
  };
}

describe("conversationStorage", () => {
  it("bounds persisted history while retaining the newest conversation", () => {
    const storage = new MemoryStorage();
    const result = saveDeviceThreads(
      storage,
      "bounded",
      Array.from({ length: 12 }, (_, index) => thread(index, 8))
    );

    expect(result).toEqual({ saved: true, truncated: true });
    const loaded = loadDeviceThreads(storage, "bounded");
    expect(loaded[0].id).toBe("thread-11");
    expect(JSON.stringify(loaded).length).toBeLessThanOrEqual(3_000_000);
  });

  it("reports quota failure instead of throwing from the UI persistence effect", () => {
    const storage = new MemoryStorage();
    storage.setItem = () => { throw new DOMException("quota", "QuotaExceededError"); };

    expect(() => saveDeviceThreads(storage, "quota", [thread(1)])).not.toThrow();
    expect(saveDeviceThreads(storage, "quota", [thread(1)])).toEqual({
      saved: false,
      truncated: false
    });
  });
});

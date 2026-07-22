const STORAGE_VERSION = 1;
const STORAGE_PREFIX = "corvus.local-conversations.v1";
const MAX_PERSISTED_THREADS = 50;
const MAX_MESSAGES_PER_THREAD = 200;
const MAX_MESSAGE_CHARACTERS = 50_000;
const MAX_SERIALIZED_CHARACTERS = 3_000_000;

export interface DeviceMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt: string;
}

export interface DeviceThread {
  id: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  messages: DeviceMessage[];
  workingDirectory?: string;
  repositoryId?: string;
  repositoryName?: string;
}

interface DeviceConversationState { version: typeof STORAGE_VERSION; threads: DeviceThread[]; }

function key(scope: string): string { return `${STORAGE_PREFIX}.${scope}`; }

function validMessage(value: unknown): value is DeviceMessage {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return typeof candidate.id === "string" &&
    (candidate.role === "user" || candidate.role === "assistant") &&
    typeof candidate.content === "string" && typeof candidate.createdAt === "string";
}

function validThread(value: unknown): value is DeviceThread {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return typeof candidate.id === "string" && typeof candidate.title === "string" &&
    typeof candidate.createdAt === "string" && typeof candidate.updatedAt === "string" &&
    Array.isArray(candidate.messages) && candidate.messages.every(validMessage) &&
    (candidate.workingDirectory === undefined || typeof candidate.workingDirectory === "string") &&
    (candidate.repositoryId === undefined || typeof candidate.repositoryId === "string") &&
    (candidate.repositoryName === undefined || typeof candidate.repositoryName === "string");
}

export function loadDeviceThreads(storage: Storage, scope: string): DeviceThread[] {
  const storageKey = key(scope);
  const value = storage.getItem(storageKey);
  if (value === null) return [];
  try {
    const parsed = JSON.parse(value) as { version?: unknown; threads?: unknown };
    if (parsed.version === STORAGE_VERSION && Array.isArray(parsed.threads) && parsed.threads.every(validThread)) return parsed.threads;
  } catch { /* Invalid device-only data is removed below. */ }
  storage.removeItem(storageKey);
  return [];
}

export interface DeviceConversationSaveResult { saved: boolean; truncated: boolean; }

export function saveDeviceThreads(
  storage: Storage,
  scope: string,
  threads: readonly DeviceThread[]
): DeviceConversationSaveResult {
  let truncated = threads.length > MAX_PERSISTED_THREADS;
  const ordered = [...threads]
    .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt))
    .slice(0, MAX_PERSISTED_THREADS)
    .map((thread) => {
      const selectedMessages = thread.messages.slice(-MAX_MESSAGES_PER_THREAD);
      if (selectedMessages.length !== thread.messages.length) truncated = true;
      return {
        ...thread,
        messages: selectedMessages.map((message) => {
          const content = message.content.slice(0, MAX_MESSAGE_CHARACTERS);
          if (content.length !== message.content.length) truncated = true;
          return { ...message, content };
        })
      };
    });

  // Keep the newest useful history without repeatedly serializing the entire
  // state. If the next thread does not fit, a logarithmic search retains as
  // many of its newest messages as the exact serialized budget permits.
  const persisted: DeviceThread[] = [];
  let encodedLength = JSON.stringify({ version: STORAGE_VERSION, threads: [] }).length;
  for (const thread of ordered) {
    const serializedThread = JSON.stringify(thread);
    const separatorLength = persisted.length === 0 ? 0 : 1;
    if (encodedLength + separatorLength + serializedThread.length <= MAX_SERIALIZED_CHARACTERS) {
      persisted.push(thread);
      encodedLength += separatorLength + serializedThread.length;
      continue;
    }

    truncated = true;
    let low = 0;
    let high = thread.messages.length;
    let fittingThread: DeviceThread | undefined;
    while (low <= high) {
      const count = Math.floor((low + high) / 2);
      const candidate = { ...thread, messages: count === 0 ? [] : thread.messages.slice(-count) };
      const candidateLength = JSON.stringify(candidate).length;
      if (encodedLength + separatorLength + candidateLength <= MAX_SERIALIZED_CHARACTERS) {
        fittingThread = candidate;
        low = count + 1;
      } else {
        high = count - 1;
      }
    }
    if (fittingThread !== undefined) persisted.push(fittingThread);
    break;
  }

  const state: DeviceConversationState = { version: STORAGE_VERSION, threads: persisted };
  const encoded = JSON.stringify(state);
  try {
    storage.setItem(key(scope), encoded);
    return { saved: true, truncated };
  } catch {
    return { saved: false, truncated };
  }
}

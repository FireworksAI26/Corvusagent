import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";

import { BrandMark } from "../components/Brand";

import type {
  ConversationApi,
  ConversationRepository,
  ProviderCatalogEntry,
  ProviderId,
  RunnableProviderId,
  RunEventStream,
  RunMode,
  SafetyPreview,
  SafetyReceipt,
  ThinkingLevel
} from "./conversationApi";
import { loadDeviceThreads, saveDeviceThreads, type DeviceThread } from "./conversationStorage";
import { loadDevicePreferences } from "./devicePreferences";
import { featureErrorMessage } from "./featureFeedback";
import { FALLBACK_PROVIDERS } from "./providerDefaults";

type Experience = "developer" | "everyday";
type RunStatus = "idle" | "working" | "needs_input" | "completed" | "cancelled" | "failed";
type ToolActivity = { id: string; label: string; status: "started" | "completed" };
type ModelUsage = { inputTokens?: number; cachedInputTokens?: number; outputTokens?: number };
type AssistantChunkKind = "block" | "continuation";
type ComposerOption<T extends string> = { value: T; label: string; description?: string; disabled?: boolean };
const TITLE_MAX = 72;
const COMPOSER_MIN_ROWS = 2;
const COMPOSER_MAX_ROWS = 8;
const COMPOSER_FALLBACK_LINE_HEIGHT_PX = 24;
const THINKING_LABELS: Record<ThinkingLevel, string> = {
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "Extra high",
  max: "Max"
};

function MarkdownMessage({ children }: { children: string }) {
  return <div className="message-markdown"><ReactMarkdown rehypePlugins={[rehypeSanitize]} remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown></div>;
}

function resizeComposer(textarea: HTMLTextAreaElement): void {
  const styles = window.getComputedStyle(textarea);
  const parsedLineHeight = Number.parseFloat(styles.lineHeight);
  const lineHeight = Number.isFinite(parsedLineHeight)
    ? parsedLineHeight
    : COMPOSER_FALLBACK_LINE_HEIGHT_PX;
  const verticalChrome = Number.parseFloat(styles.paddingTop || "0")
    + Number.parseFloat(styles.paddingBottom || "0")
    + Number.parseFloat(styles.borderTopWidth || "0")
    + Number.parseFloat(styles.borderBottomWidth || "0");
  const minimumHeight = lineHeight * COMPOSER_MIN_ROWS + verticalChrome;
  const maximumHeight = lineHeight * COMPOSER_MAX_ROWS + verticalChrome;
  textarea.style.height = "auto";
  textarea.style.height = `${Math.min(Math.max(textarea.scrollHeight, minimumHeight), maximumHeight)}px`;
  textarea.style.overflowY = textarea.scrollHeight > maximumHeight ? "auto" : "hidden";
}

function applyModelLabels(
  provider: ProviderCatalogEntry,
  labels: Record<string, string>
): ProviderCatalogEntry {
  const prefix = `${provider.id}:`;
  const discoveredIds = new Set(provider.models.map((model) => model.id));
  const configuredModels = Object.entries(labels)
    .filter(([key]) => key.startsWith(prefix) && !discoveredIds.has(key.slice(prefix.length)))
    .map(([key, label]) => ({ id: key.slice(prefix.length), label, recommended: false }));
  return {
    ...provider,
    models: [
      ...provider.models.map((model) => ({
        ...model,
        label: labels[`${prefix}${model.id}`] ?? model.label
      })),
      ...configuredModels
    ]
  };
}

function ComposerSelect<T extends string>({
  ariaLabel,
  className = "",
  disabled = false,
  onChange,
  options,
  value
}: {
  ariaLabel: string;
  className?: string;
  disabled?: boolean;
  onChange(value: T): void;
  options: readonly ComposerOption<T>[];
  value: T;
}) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const listboxId = useId();
  const selectedIndex = Math.max(0, options.findIndex((option) => option.value === value));
  const selectedLabel = options[selectedIndex]?.label ?? value;

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, [open]);

  function nextEnabledIndex(from: number, delta: number): number {
    if (options.length === 0) return 0;
    let next = from;
    do { next = (next + delta + options.length) % options.length; }
    while (options[next]?.disabled && next !== from);
    return options[next]?.disabled ? from : next;
  }

  return <div
    className={`composer-select ${className}`.trim()}
    onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
    }}
    ref={rootRef}
  >
    <button
      aria-controls={listboxId}
      aria-activedescendant={open ? `${listboxId}-option-${activeIndex}` : undefined}
      aria-expanded={open}
      aria-haspopup="listbox"
      aria-label={ariaLabel}
      className="composer-select__trigger"
      disabled={disabled}
      onClick={() => { setActiveIndex(selectedIndex); setOpen((current) => !current); }}
      onKeyDown={(event) => {
        if (event.key === "Escape") { setOpen(false); return; }
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          const direction = event.key === "ArrowDown" ? 1 : -1;
          setActiveIndex((current) => nextEnabledIndex(open ? current : selectedIndex, direction));
          setOpen(true);
          return;
        }
        if (open && (event.key === "Home" || event.key === "End")) {
          event.preventDefault();
          const edge = event.key === "Home" ? 0 : options.length - 1;
          setActiveIndex(options[edge]?.disabled ? nextEnabledIndex(edge, event.key === "Home" ? 1 : -1) : edge);
          return;
        }
        if (open && (event.key === "Enter" || event.key === " ")) {
          event.preventDefault();
          const option = options[activeIndex];
          if (option && !option.disabled) onChange(option.value);
          setOpen(false);
        }
      }}
      role="combobox"
      type="button"
    ><span>{selectedLabel}</span><span aria-hidden="true" className="composer-select__chevron">⌄</span></button>
    {open ? <div
      aria-label={ariaLabel}
      className="composer-select__menu"
      id={listboxId}
      onPointerDown={(event) => event.preventDefault()}
      role="listbox"
    >
      {options.map((option, index) => <div
        aria-disabled={option.disabled || undefined}
        aria-label={option.label}
        aria-selected={option.value === value}
        data-active={index === activeIndex || undefined}
        id={`${listboxId}-option-${index}`}
        key={option.value}
        onClick={() => { if (option.disabled) return; onChange(option.value); setOpen(false); }}
        onPointerMove={() => { if (!option.disabled) setActiveIndex(index); }}
        role="option"
      ><span>{option.label}</span>{option.description ? <small>{option.description}</small> : null}</div>)}
    </div> : null}
  </div>;
}

function Icon({ name }: { name: "history" | "new" | "send" | "stop" | "download" | "shield" | "agent" | "model" | "thinking" | "mode" }) {
  const path = {
    history: "M3 12a9 9 0 1 0 3-6.7M3 4v5h5M12 7v5l3 2",
    new: "M12 5v14M5 12h14",
    send: "m4 4 16 8-16 8 3-8-3-8Zm3 8h13",
    stop: "M7 7h10v10H7z",
    download: "M12 3v12m0 0 5-5m-5 5-5-5M5 21h14",
    shield: "M12 3 5 6v5c0 4.6 2.8 8.1 7 10 4.2-1.9 7-5.4 7-10V6l-7-3Zm-3 9 2 2 4-4",
    agent: "M8 9h8M9 15h6M12 3v3M6 6h12v14H6z",
    model: "M5 7h14M5 12h14M5 17h9",
    thinking: "M9 18h6M10 22h4M8 13a5 5 0 1 1 8 0c-1 1-2 2-2 3h-4c0-1-1-2-2-3",
    mode: "M4 4h16v16H4zM9 9h6v6H9z"
  }[name];
  return <svg aria-hidden="true" className="ui-icon" fill="none" viewBox="0 0 24 24"><path d={path} stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" /></svg>;
}

function activityLabel(activity: unknown): string {
  const labels: Record<string, string> = {
    command: "Running checks",
    files: "Updating files",
    mcp: "Using an MCP tool",
    search: "Inspecting the project",
    sandbox: "Sandbox initialized",
    blocked: "Blocked an unsafe action",
    workaround: "Continuing with a safe workaround",
    screening: "Screening the finished artifact"
  };
  return typeof activity === "string" ? labels[activity] ?? "Working" : "Working";
}

function preserveSpecificActivityLabel(current: ToolActivity | undefined, nextLabel: string): string {
  const genericLabels = new Set(["Run command", "Run a sandboxed command", "Use tool"]);
  return current !== undefined && genericLabels.has(nextLabel) ? current.label : nextLabel;
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} bytes`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function safeMessage(reason: unknown): string {
  return featureErrorMessage(reason, "run");
}

export function assistantStreamBatchSize(pendingTokens: number): number {
  return Math.max(1, Math.ceil(pendingTokens / 120));
}

function usageValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : undefined;
}

function usageSummary(usage: ModelUsage): string {
  const format = new Intl.NumberFormat();
  return [
    usage.inputTokens === undefined ? null : `${format.format(usage.inputTokens)} input`,
    usage.cachedInputTokens === undefined ? null : `${format.format(usage.cachedInputTokens)} cached`,
    usage.outputTokens === undefined ? null : `${format.format(usage.outputTokens)} output`
  ].filter((item): item is string => item !== null).join(" · ");
}

export function ConversationWorkspace({ api, experience, newThreadSignal = 0, onOpenProjects, storage, storageScope, workingDirectory }: {
  api: ConversationApi;
  experience: Experience;
  newThreadSignal?: number;
  onOpenProjects?(): void;
  storage: Storage;
  storageScope: string;
  workingDirectory?: string;
}) {
  const streamRef = useRef<RunEventStream | null>(null);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const [threads, setThreads] = useState<DeviceThread[]>(() => loadDeviceThreads(storage, storageScope));
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(() => threads[0]?.id ?? null);
  const activeThreadIdRef = useRef<string | null>(selectedThreadId);
  const assistantTextRef = useRef("");
  const assistantTargetRef = useRef("");
  const assistantQueueRef = useRef<string[]>([]);
  const assistantTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const completedRunRef = useRef<{ runId: string; status: "completed" | "needs_input" } | null>(null);
  const activePromptRef = useRef("");
  const confirmationDialogRef = useRef<HTMLDivElement | null>(null);
  const confirmationTriggerRef = useRef<HTMLElement | null>(null);
  const [composer, setComposer] = useState("");
  const [assistantText, setAssistantText] = useState("");
  const [runId, setRunId] = useState<string | null>(null);
  const [runStatus, setRunStatus] = useState<RunStatus>("idle");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [providers, setProviders] = useState<ProviderCatalogEntry[]>(FALLBACK_PROVIDERS);
  const [providerError, setProviderError] = useState("");
  const [preferenceError, setPreferenceError] = useState("");
  const [storageNotice, setStorageNotice] = useState("");
  const [providerRefresh, setProviderRefresh] = useState(0);
  const [providerId, setProviderId] = useState<RunnableProviderId>("codex");
  const [modelId, setModelId] = useState(FALLBACK_PROVIDERS[0].models[0]?.id ?? "");
  const [thinking, setThinking] = useState<ThinkingLevel>("medium");
  const [mode, setMode] = useState<RunMode>("chat");
  const [mcpEnabled, setMcpEnabled] = useState(false);
  const [optionsOpen, setOptionsOpen] = useState(false);
  const [runNote, setRunNote] = useState("");
  const [thinkingNote, setThinkingNote] = useState("");
  const [toolActivities, setToolActivities] = useState<ToolActivity[]>([]);
  const [modelUsage, setModelUsage] = useState<ModelUsage | null>(null);
  const [artifactReady, setArtifactReady] = useState(false);
  const [artifactName, setArtifactName] = useState("corvus-finished-project.zip");
  const [artifactDownloading, setArtifactDownloading] = useState(false);

  useLayoutEffect(() => {
    if (composerRef.current !== null) resizeComposer(composerRef.current);
  }, [composer]);
  const [safetyPreview, setSafetyPreview] = useState<SafetyPreview | null>(null);
  const [safetyLoading, setSafetyLoading] = useState(true);
  const [safetyError, setSafetyError] = useState("");
  const [safetyRefresh, setSafetyRefresh] = useState(0);
  const [safetyDetailsOpen, setSafetyDetailsOpen] = useState(false);
  const [confirmation, setConfirmation] = useState<{ prompt: string; safety: SafetyPreview } | null>(null);
  const [interruption, setInterruption] = useState<{ prompt: string; detail: string } | null>(null);
  const [receipt, setReceipt] = useState<SafetyReceipt | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [projectMenuOpen, setProjectMenuOpen] = useState(false);
  const [repositories, setRepositories] = useState<ConversationRepository[]>([]);
  const newThreadSignalRef = useRef(newThreadSignal);
  const noun = experience === "developer" ? "thread" : "conversation";
  const sendKeyMode = loadDevicePreferences(storage, storageScope).sendKeyMode;
  const selected = threads.find((thread) => thread.id === selectedThreadId) ?? null;
  const provider = providers.find((entry) => entry.id === providerId) ?? FALLBACK_PROVIDERS[0];
  const providerModels = provider.models ?? [];
  const providerThinkingLevels = provider.thinking_levels ?? [];

  useEffect(() => {
    if (!confirmation) return;
    const appRoot = document.getElementById("root");
    appRoot?.setAttribute("inert", "");
    confirmationDialogRef.current?.querySelector<HTMLElement>("button")?.focus();
    return () => {
      appRoot?.removeAttribute("inert");
      confirmationTriggerRef.current?.focus();
    };
  }, [confirmation]);

  useEffect(() => () => {
    streamRef.current?.close();
    if (assistantTimerRef.current !== null) clearTimeout(assistantTimerRef.current);
  }, []);
  useEffect(() => {
    const result = saveDeviceThreads(storage, storageScope, threads);
    setStorageNotice(result.saved
      ? result.truncated ? "Older conversation history was trimmed to keep local storage responsive." : ""
      : "Conversation history could not be saved on this device. The current conversation remains open.");
  }, [storage, storageScope, threads]);
  useEffect(() => {
    if (api.listRepositories === undefined) return;
    let current = true;
    void api.listRepositories().then((items) => {
      if (current) setRepositories(items);
    }).catch(() => {
      if (current) setRepositories([]);
    });
    return () => { current = false; };
  }, [api]);
  useEffect(() => {
    let current = true;
    setSafetyLoading(true);
    setSafetyError("");
    setSafetyPreview(null);
    void api.getSafetyPreview(providerId, mode, mode === "build" && mcpEnabled).then((preview) => {
      if (current) setSafetyPreview(preview);
    }).catch(() => {
      if (current) setSafetyError("Safety policy unavailable. Runs are paused until Corvus can verify it.");
    }).finally(() => {
      if (current) setSafetyLoading(false);
    });
    return () => { current = false; };
  }, [api, providerId, mode, mcpEnabled, safetyRefresh]);
  useEffect(() => {
    let current = true;
    void Promise.allSettled([api.listProviders(), api.getPreferences()]).then(([catalogResult, preferencesResult]) => {
      if (!current) return;
      if (catalogResult.status === "rejected") {
        setProviders(FALLBACK_PROVIDERS);
        setProviderError("Corvus could not verify local providers. Retry discovery before starting a run.");
        return;
      }
      const catalog = catalogResult.value;
      if (catalog.length === 0) {
        setProviders(FALLBACK_PROVIDERS);
        setProviderError("No supported local agents were detected. Install Codex CLI or Claude Code, then retry.");
        return;
      }
      const readyCatalog = catalog.filter((entry) => entry.status === "ready");
      if (readyCatalog.length === 0) {
        setProviders(FALLBACK_PROVIDERS);
        setProviderError("No configured agents are ready. Configure a local CLI or API key in Settings, then retry.");
        return;
      }
      const preferences = preferencesResult.status === "fulfilled" ? preferencesResult.value : null;
      const runnableCatalog = readyCatalog.filter((entry) =>
        (entry.models?.length ?? 0) > 0 && (entry.thinking_levels?.length ?? 0) > 0
      ).map((entry) => applyModelLabels(entry, preferences?.model_labels ?? {}));
      if (runnableCatalog.length === 0) {
        setProviders(FALLBACK_PROVIDERS);
        setProviderError("A local agent was verified, but discovery returned no supported models or thinking levels. Retry discovery before starting a run.");
        return;
      }
      setProviders(runnableCatalog);
      setProviderError("");
      setPreferenceError(preferences === null
        ? "Saved model preferences are unavailable. Corvus is using verified provider defaults for this conversation."
        : "");
      const preferred = preferences === null ? undefined : runnableCatalog.find((entry) =>
        entry.id === preferences.default_provider && entry.status === "ready"
      );
      const ready = preferred ?? runnableCatalog.find((entry) =>
        entry.status === "ready"
      );
      if (ready !== undefined) {
        const readyModels = ready.models ?? [];
        const readyThinkingLevels = ready.thinking_levels ?? [];
        setProviderId(ready.id as RunnableProviderId);
        setModelId(
          preferred !== undefined
            && preferences?.default_model !== null
            && preferences?.default_model !== undefined
            && readyModels.some((model) => model.id === preferences.default_model)
            ? preferences.default_model
            : readyModels[0]?.id ?? ""
        );
        setThinking(
          preferences !== null && readyThinkingLevels.includes(preferences.default_effort)
            ? preferences.default_effort
            : readyThinkingLevels.includes("medium") ? "medium" : readyThinkingLevels[0] ?? "medium"
        );
        const preferredMode = ready.id === "codex" ? preferences?.default_mode ?? "chat" : "chat";
        setMode(preferredMode);
        setMcpEnabled(preferredMode === "build" && ready.supports_mcp && (preferences?.mcp_enabled ?? false));
      }
    });
    return () => { current = false; };
  }, [api, providerRefresh]);

  const createConversation = useCallback((repository: ConversationRepository | null = null): DeviceThread => {
    const now = new Date().toISOString();
    const created: DeviceThread = {
      id: crypto.randomUUID(),
      title: experience === "developer" ? "New thread" : "New conversation",
      createdAt: now,
      updatedAt: now,
      repositoryId: repository?.id,
      repositoryName: repository?.display_name,
      workingDirectory: repository?.path,
      messages: []
    };
    setThreads((current) => [created, ...current]);
    setSelectedThreadId(created.id);
    activeThreadIdRef.current = created.id;
    setAssistantText("");
    assistantTextRef.current = "";
    assistantTargetRef.current = "";
    assistantQueueRef.current = [];
    completedRunRef.current = null;
    setToolActivities([]);
    setModelUsage(null);
    setHistoryOpen(false);
    return created;
  }, [experience]);

  function selectRepository(repository: ConversationRepository | null): void {
    if (selected) {
      setThreads((current) => current.map((item) => item.id === selected.id ? {
        ...item,
        repositoryId: repository?.id,
        repositoryName: repository?.display_name,
        workingDirectory: repository?.path
      } : item));
    } else {
      createConversation(repository);
    }
    setProjectMenuOpen(false);
  }

  useEffect(() => {
    if (newThreadSignalRef.current === newThreadSignal) return;
    if (runStatus === "working") return;
    newThreadSignalRef.current = newThreadSignal;
    createConversation();
  }, [createConversation, newThreadSignal, runStatus]);

  function finishRun(status: Exclude<RunStatus, "idle" | "working">, text?: string): void {
    const threadId = activeThreadIdRef.current;
    if (text && threadId) {
      const now = new Date().toISOString();
      setThreads((current) => current.map((thread) => thread.id === threadId ? {
        ...thread,
        updatedAt: now,
        messages: [...thread.messages, { id: crypto.randomUUID(), role: "assistant", content: text, createdAt: now }]
      } : thread));
    }
    setAssistantText("");
    assistantTextRef.current = "";
    assistantTargetRef.current = "";
    assistantQueueRef.current = [];
    completedRunRef.current = null;
    if (assistantTimerRef.current !== null) clearTimeout(assistantTimerRef.current);
    assistantTimerRef.current = null;
    setRunStatus(status);
    setRunNote("");
    setThinkingNote("");
    streamRef.current?.close();
    streamRef.current = null;
  }

  function drainAssistantQueue(): void {
    if (assistantTimerRef.current !== null) return;
    const tick = () => {
      assistantTimerRef.current = null;
      const reduceMotion = typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      const count = reduceMotion
        ? assistantQueueRef.current.length
        : assistantStreamBatchSize(assistantQueueRef.current.length);
      const tokens = assistantQueueRef.current.splice(0, count).join("");
      if (tokens !== "") {
        assistantTextRef.current += tokens;
        setAssistantText(assistantTextRef.current);
      }
      if (assistantQueueRef.current.length > 0) {
        assistantTimerRef.current = setTimeout(tick, 16);
        return;
      }
      const completed = completedRunRef.current;
      if (completed !== null) {
        completedRunRef.current = null;
        finishRun(completed.status, assistantTargetRef.current);
      }
    };
    assistantTimerRef.current = setTimeout(tick, 0);
  }

  function enqueueAssistantText(text: string, kind: AssistantChunkKind): void {
    const needsParagraphBoundary = kind === "block" && assistantTargetRef.current !== "" &&
      !/\s$/.test(assistantTargetRef.current) && !/^\s/.test(text);
    const normalized = `${needsParagraphBoundary ? "\n\n" : ""}${text}`;
    assistantTargetRef.current += normalized;
    assistantQueueRef.current.push(...(normalized.match(/\S+\s*|\s+/g) ?? [normalized]));
    drainAssistantQueue();
  }

  function loadSafetyReceipt(activeRunId: string): void {
    void api.getSafetyReceipt(activeRunId).then(setReceipt).catch(() => {
      setError("The run ended, but its safety receipt is not available yet.");
    });
  }

  function listen(stream: RunEventStream, activeRunId: string): void {
    streamRef.current?.close();
    streamRef.current = stream;
    stream.addEventListener("started", () => setRunNote("Protected runtime started"));
    stream.addEventListener("message", ({ data }) => {
      try {
        const event = JSON.parse(data) as { payload?: { text?: unknown } } | null;
        if (typeof event?.payload?.text === "string") {
          enqueueAssistantText(
            event.payload.text,
            providerId === "codex" || providerId === "claude" ? "block" : "continuation"
          );
        }
      } catch { setError("Corvus received an unreadable run event."); }
    });
    stream.addEventListener("usage", ({ data }) => {
      try {
        const event = JSON.parse(data) as { payload?: {
          input_tokens?: unknown; cached_input_tokens?: unknown; output_tokens?: unknown;
        } } | null;
        const next: ModelUsage = {
          inputTokens: usageValue(event?.payload?.input_tokens),
          cachedInputTokens: usageValue(event?.payload?.cached_input_tokens),
          outputTokens: usageValue(event?.payload?.output_tokens)
        };
        if (Object.values(next).some((value) => value !== undefined)) {
          setModelUsage((current) => ({ ...current, ...Object.fromEntries(
            Object.entries(next).filter(([, value]) => value !== undefined)
          ) }));
        }
      } catch { setError("Corvus received unreadable model-usage evidence."); }
    });
    stream.addEventListener("thinking", ({ data }) => {
      try {
        const event = JSON.parse(data) as { payload?: { text?: unknown; summary?: unknown } } | null;
        const note = event?.payload?.summary ?? event?.payload?.text;
        if (typeof note === "string") setThinkingNote(note);
      } catch { setError("Corvus received an unreadable thinking event."); }
    });
    stream.addEventListener("status", ({ data }) => {
      try {
        const event = JSON.parse(data) as { payload?: {
          activity?: unknown; label?: unknown; status?: unknown; tool_id?: unknown;
        } } | null;
        setRunNote(activityLabel(event?.payload?.activity));
        const payload = event?.payload;
        if (
          typeof payload?.tool_id === "string" && typeof payload.label === "string" &&
          (payload.status === "started" || payload.status === "completed")
        ) {
          setToolActivities((current) => {
            const existing = current.findIndex((activity) => activity.id === payload.tool_id);
            const previous = existing < 0 ? undefined : current[existing];
            const next = {
              id: payload.tool_id as string,
              label: preserveSpecificActivityLabel(previous, payload.label as string),
              status: payload.status as "started" | "completed"
            };
            return existing < 0
              ? [...current, next]
              : current.map((activity, index) => index === existing ? next : activity);
          });
        }
      } catch { setError("Corvus received an unreadable status event."); }
    });
    stream.addEventListener("artifact", ({ data }) => {
      setArtifactReady(true);
      try {
        const event = JSON.parse(data) as { payload?: { download_name?: unknown } } | null;
        if (typeof event?.payload?.download_name === "string") setArtifactName(event.payload.download_name);
      } catch { /* The artifact remains downloadable with the safe fallback filename. */ }
    });
    stream.addEventListener("needs_input", () => {
      setError("");
      setInterruption({
        prompt: activePromptRef.current,
        detail: "Corvus finished this step and is waiting for your confirmation. Reply in the composer to continue."
      });
    });
    stream.addEventListener("completed", ({ data }) => {
      let status: "completed" | "needs_input" = "completed";
      try {
        const event = JSON.parse(data) as { payload?: { status?: unknown } } | null;
        if (event?.payload?.status === "needs_input") status = "needs_input";
      } catch { setError("Corvus received an unreadable completion event."); }
      loadSafetyReceipt(activeRunId);
      if (assistantQueueRef.current.length > 0 || assistantTimerRef.current !== null) {
        completedRunRef.current = { runId: activeRunId, status };
      } else {
        finishRun(status, assistantTargetRef.current);
      }
    });
    stream.addEventListener("cancelled", () => {
      loadSafetyReceipt(activeRunId);
      finishRun("cancelled");
    });
    stream.addEventListener("failed", ({ data }) => {
      let reasonCode = "run_failed";
      try {
        const event = JSON.parse(data) as { payload?: { reason_code?: unknown } } | null;
        if (typeof event?.payload?.reason_code === "string") reasonCode = event.payload.reason_code;
      } finally {
        loadSafetyReceipt(activeRunId);
        const partialResponse = assistantTargetRef.current;
        if (safetyPreview?.level === "read_only" && (
          reasonCode.startsWith("process_") || reasonCode.includes("sandbox") || reasonCode.includes("permission")
        )) {
          setError("");
          setInterruption({
            prompt: activePromptRef.current,
            detail: "Read-only mode prevented this run from making the requested change. The response already received is preserved."
          });
        } else if (reasonCode.includes("model")) {
          setError("The selected model is not available for this provider. Change it in Settings, then try again.");
        } else if (reasonCode === "provider_output_limit") {
          setError("The provider response reached Corvus's 100 KB limit. The response received so far is preserved; ask for a shorter continuation.");
        } else if (reasonCode === "provider_deadline_exceeded") {
          setError("The provider exceeded the three-minute response deadline. The response received so far is preserved; retry with a narrower request.");
        } else {
          setError("This run stopped before it could finish. You can retry or change its safety mode.");
        }
        finishRun("failed", partialResponse);
      }
    });
    stream.onTerminalError(() => {
      if (streamRef.current !== stream) return;
      setError("Connection to this run ended before completion. Start the run again to continue.");
      finishRun("failed", assistantTargetRef.current);
    });
  }

  async function startPrompt(prompt: string, safety: SafetyPreview): Promise<void> {
    const thread = selected ?? createConversation();
    const now = new Date().toISOString();
    setThreads((current) => current.map((item) => item.id === thread.id ? {
      ...item,
      title: item.messages.length === 0 ? prompt.slice(0, TITLE_MAX) : item.title,
      updatedAt: now,
      messages: [...item.messages, { id: crypto.randomUUID(), role: "user", content: prompt, createdAt: now }]
    } : item));
    setComposer("");
    setAssistantText("");
    assistantTextRef.current = "";
    assistantTargetRef.current = "";
    assistantQueueRef.current = [];
    completedRunRef.current = null;
    if (assistantTimerRef.current !== null) clearTimeout(assistantTimerRef.current);
    assistantTimerRef.current = null;
    activeThreadIdRef.current = thread.id;
    activePromptRef.current = prompt;
    setRunStatus("working");
    setBusy(true);
    setError("");
    setRunNote("");
    setThinkingNote("");
    setToolActivities([]);
    setModelUsage(null);
    setArtifactReady(false);
    setArtifactName("corvus-finished-project.zip");
    setReceipt(null);
    setInterruption(null);
    try {
      const run = await api.startRun(prompt, {
        provider: providerId,
        model: modelId || null,
        effort: thinking,
        mode,
        mcp_enabled: mode === "build" && mcpEnabled,
        safety_digest: safety.policy_digest,
        ...(thread.messages.length > 0 ? {
          context: thread.messages.slice(-20).map(({ role, content }) => ({ role, content }))
        } : {}),
        ...(thread.repositoryId ? { repository_id: thread.repositoryId } : {})
      }, crypto.randomUUID());
      setRunId(run.run_id);
      if (run.working_directory) {
        setThreads((current) => current.map((item) => item.id === thread.id
          ? { ...item, workingDirectory: run.working_directory }
          : item));
      }
      listen(api.openRunEvents(run.run_id), run.run_id);
    } catch (reason) {
      setRunStatus("failed");
      setError(safeMessage(reason));
    } finally { setBusy(false); }
  }

  async function send(event: FormEvent): Promise<void> {
    event.preventDefault();
    const prompt = composer.trim();
    if (
      prompt === "" || runStatus === "working" || provider.status !== "ready" ||
      safetyPreview === null || safetyLoading
    ) return;
    if (safetyPreview.requires_confirmation) {
      confirmationTriggerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      setConfirmation({ prompt, safety: safetyPreview });
      return;
    }
    await startPrompt(prompt, safetyPreview);
  }

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>): void {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    const modifierSend = event.ctrlKey || event.metaKey;
    const shouldSend = modifierSend || sendKeyMode === "enter" || (
      sendKeyMode === "adaptive" && !composer.includes("\n")
    );
    if (!shouldSend) return;
    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  }

  function handleConfirmationKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key === "Escape") {
      event.preventDefault();
      setConfirmation(null);
      return;
    }
    if (event.key !== "Tab") return;
    const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>("button:not([disabled])"));
    if (controls.length === 0) return;
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  async function stop(): Promise<void> {
    if (runId === null) return;
    const activeRunId = runId;
    setBusy(true);
    try {
      const result = await api.cancelRun(activeRunId);
      if (result.state === "running") {
        setRunNote("Cancellation was not accepted; the run is still working.");
        return;
      }
      loadSafetyReceipt(activeRunId);
      finishRun(result.state);
    } catch (reason) { setError(safeMessage(reason)); }
    finally { setBusy(false); }
  }

  async function saveFinishedProject(): Promise<void> {
    if (runId === null || artifactDownloading) return;
    setArtifactDownloading(true);
    setError("");
    try {
      const savedPath = await api.downloadArtifact(
        runId,
        receipt?.artifact?.download_name ?? artifactName
      );
      if (savedPath !== null) setStorageNotice(`Finished project saved to ${savedPath}`);
    } catch {
      setError("Corvus could not save the finished project. Choose another location and try again.");
    } finally {
      setArtifactDownloading(false);
    }
  }

  return (
    <section className="conversation-workspace" aria-label="Corvus conversations">
      <div className="conversation-panel">
        <header className="conversation-panel__header">
          <div><h1>{selected?.title ?? (experience === "developer" ? "New thread" : "New conversation")}</h1><span className="conversation-context" title={selected?.workingDirectory ?? workingDirectory}>{selected?.workingDirectory ?? workingDirectory ?? "Corvus agent workspace"}</span></div>
          <div className="conversation-actions">
            <button aria-expanded={historyOpen} aria-label="Conversation history" className="icon-button" data-component-source="lucide-message-square" onClick={() => setHistoryOpen((open) => !open)} type="button"><Icon name="history" /></button>
            <button aria-label={`New ${noun}`} className="icon-button" disabled={runStatus === "working"} onClick={() => createConversation()} title={runStatus === "working" ? "Stop the active run before starting another conversation" : undefined} type="button"><Icon name="new" /></button>
          </div>
          {historyOpen ? <div className="conversation-history" role="dialog" aria-label="Conversation history panel">
            <strong>{experience === "developer" ? "Threads" : "Conversations"}</strong>
            {threads.length === 0 ? <p className="thread-list__empty">No conversations yet</p> : null}
            <div className="thread-list__items">{threads.map((thread) => <button aria-current={selectedThreadId === thread.id ? "true" : undefined} disabled={runStatus === "working"} key={thread.id} onClick={() => { setSelectedThreadId(thread.id); activeThreadIdRef.current = thread.id; setAssistantText(""); assistantTextRef.current = ""; setModelUsage(null); setReceipt(null); setToolActivities([]); setArtifactReady(false); setRunStatus("idle"); setInterruption(null); setError(""); setHistoryOpen(false); }} type="button"><strong>{thread.title}</strong><span>{new Date(thread.updatedAt).toLocaleDateString()}</span></button>)}</div>
          </div> : null}
        </header>
        <div className="message-transcript">
          {(selected?.messages.length ?? 0) === 0 && assistantText === "" ? <div className="conversation-empty"><BrandMark className="conversation-mark" decorative /><h2>{experience === "developer" ? "What would you like to start?" : "What can Corvus help you finish?"}</h2><p>Ask a question or hand off a complete task. Build mode works in an isolated sandbox and returns the finished project.</p><div className="starter-prompts">{(experience === "developer" ? ["Inspect this repository", "Fix the failing tests", "Build a small feature"] : ["Plan my week", "Draft a clear update", "Organize these notes"]).map((starter) => <button key={starter} onClick={() => setComposer(starter)} type="button">{starter}</button>)}</div></div> : null}
          {selected?.messages.map((message) => <article className={`message message--${message.role}`} key={message.id}><span>{message.role === "user" ? "You" : "Corvus"}</span><MarkdownMessage>{message.content}</MarkdownMessage></article>)}
          {runStatus === "working" ? <section className="run-activity" aria-label="Run status: working"><div className="run-activity__status" role="status"><span className="activity-pulse" /><strong>Working</strong><span>{runNote || safetyPreview?.label || "Policy verified"}</span></div>{thinkingNote ? <p className="run-activity__thought">{thinkingNote}</p> : null}{toolActivities.length > 0 ? <details aria-label="Tool activity" role="region"><summary>{`Activity · ${toolActivities.length} ${toolActivities.length === 1 ? "step" : "steps"}`}</summary><ol className="tool-activity__list">{toolActivities.map((activity) => <li key={activity.id}><span aria-hidden="true" className={`tool-activity__state tool-activity__state--${activity.status}`} /><span>{activity.label}</span><small>{activity.status === "completed" ? "Completed" : "In progress"}</small></li>)}</ol></details> : null}</section> : null}
          {runStatus !== "working" && toolActivities.length > 0 ? <details aria-label="Tool activity" className="tool-activity" role="region"><summary>{`Activity · ${toolActivities.length} ${toolActivities.length === 1 ? "step" : "steps"}`}</summary><ol className="tool-activity__list">{toolActivities.map((activity) => <li key={activity.id}><span aria-hidden="true" className={`tool-activity__state tool-activity__state--${activity.status}`} /><span>{activity.label}</span><small>{activity.status === "completed" ? "Completed" : "Stopped"}</small></li>)}</ol></details> : null}
          {assistantText !== "" ? <article className="message message--assistant"><span>Corvus</span><MarkdownMessage>{assistantText}</MarkdownMessage></article> : null}
          {modelUsage ? <p aria-label="Model usage" className="model-usage" role="status">Model usage · {usageSummary(modelUsage)}</p> : null}
          {artifactReady && runId !== null ? <button className="artifact-download" disabled={artifactDownloading} onClick={() => void saveFinishedProject()} type="button"><Icon name="download" />{artifactDownloading ? "Choose where to save…" : "Download finished project"}</button> : null}
          {receipt ? <section aria-label="Safety receipt" className="safety-receipt">
            <header><Icon name="shield" /><div><strong>Safety receipt</strong><span>{receipt.status === "completed" ? "Run completed inside its locked policy" : `Run ${receipt.status}`}</span></div></header>
            <div className="safety-receipt__facts">
              <span>{receipt.original_project_modified ? "Original project changed" : "Original project unchanged"}</span>
              <span>No blanket host access</span>
              {receipt.artifact?.secret_screening === "passed" ? <span>Artifact screening passed</span> : receipt.artifact ? <span>Artifact was not secret-screened</span> : null}
            </div>
            {receipt.activities.length > 0 ? <ul className="safety-receipt__activity">{receipt.activities.map((activity) => <li key={activity}>{activity}</li>)}</ul> : null}
            <details>
              <summary>Verification details</summary>
              <dl className="safety-evidence">
                <div><dt>Execution</dt><dd>{receipt.safety.execution}</dd></div>
                <div><dt>Files</dt><dd>{receipt.safety.filesystem}</dd></div>
                <div><dt>Network</dt><dd>{receipt.safety.network}</dd></div>
                <div><dt>External tools</dt><dd>{receipt.mcp_used ? "An approved MCP tool was used during this run." : receipt.safety.mcp}</dd></div>
                <div><dt>Approvals</dt><dd>{receipt.approval}</dd></div>
                <div><dt>Output</dt><dd>{receipt.safety.output}</dd></div>
                {receipt.artifact ? <>
                  <div><dt>Artifact</dt><dd>{receipt.artifact.download_name} · {formatBytes(receipt.artifact.size_bytes)}</dd></div>
                  <div><dt>Secret screening</dt><dd>{receipt.artifact.secret_screening === "passed" ? "Passed before export" : "Not scanned"}</dd></div>
                  <div className="safety-evidence__wide"><dt>Artifact SHA-256</dt><dd><code>{receipt.artifact.sha256_digest}</code></dd></div>
                </> : null}
                <div className="safety-evidence__wide"><dt>Policy SHA-256</dt><dd><code>{receipt.safety.policy_digest}</code></dd></div>
                <div className="safety-evidence__wide"><dt>Run ID</dt><dd><code>{receipt.run_id}</code></dd></div>
              </dl>
            </details>
          </section> : null}
          {runStatus !== "idle" && runStatus !== "working" ? <p className="run-result-status" aria-label={`Run status: ${runStatus}`} data-status={runStatus}>{runStatus === "needs_input" ? "Needs input" : runStatus[0].toUpperCase() + runStatus.slice(1)}</p> : null}
          {interruption ? <section aria-label="Run paused by safety settings" className="run-interruption">
            <div><Icon name="shield" /><div><strong>Corvus paused this run</strong><p>{interruption.detail}</p></div></div>
            <div className="run-interruption__actions"><button className="button button--primary" onClick={() => { setMode("build"); setMcpEnabled(false); setComposer(interruption.prompt); setInterruption(null); setError(""); }} type="button">Switch to Build</button><button className="button" onClick={() => { setInterruption(null); setError(""); }} type="button">Stop here</button></div>
          </section> : null}
        </div>
        {providerError ? <p className="conversation-error" role="alert">{providerError} <button aria-label="Retry providers" className="text-button" onClick={() => setProviderRefresh((value) => value + 1)} type="button">Retry</button></p> : null}
        {preferenceError ? <p aria-label="Preference recovery" className="conversation-notice" role="status">{preferenceError}</p> : null}
        {storageNotice ? <p className={storageNotice.startsWith("Conversation history could not") ? "conversation-error" : "conversation-notice"} role={storageNotice.startsWith("Conversation history could not") ? "alert" : "status"}>{storageNotice}</p> : null}
        {safetyError ? <p className="conversation-error" role="alert">{safetyError} <button aria-label="Retry safety policy" className="text-button" onClick={() => setSafetyRefresh((value) => value + 1)} type="button">Retry</button></p> : null}
        {error ? <p className="conversation-error" role="alert">{error}</p> : null}
        <form className="composer" onSubmit={(event) => void send(event)}>
          <label className="sr-only" htmlFor="corvus-composer">Message Corvus</label><textarea aria-label="Message Corvus" id="corvus-composer" onChange={(event) => setComposer(event.target.value)} onKeyDown={handleComposerKeyDown} placeholder={experience === "developer" ? "Ask Corvus to work in this repository…" : "Describe what you want to get done…"} ref={composerRef} rows={COMPOSER_MIN_ROWS} value={composer} />
          <div className="composer__controls">
            <div className="composer-project"><button aria-expanded={projectMenuOpen} className="composer-project__trigger" disabled={runStatus === "working"} onClick={() => setProjectMenuOpen((open) => !open)} type="button">Project · {selected?.repositoryName ?? "Agent directory"}</button>{projectMenuOpen ? <section aria-label="Project context" className="composer-project__menu"><strong>Project context</strong><button aria-current={selected?.repositoryId === undefined ? "true" : undefined} onClick={() => selectRepository(null)} type="button"><span>Agent directory</span><small>A fresh Corvus workspace for this thread</small></button>{repositories.map((repository) => <button aria-current={selected?.repositoryId === repository.id ? "true" : undefined} key={repository.id} onClick={() => selectRepository(repository)} type="button"><span>{repository.display_name}</span><small>{repository.path}</small></button>)}<button onClick={() => { setProjectMenuOpen(false); onOpenProjects?.(); }} type="button"><span>Connect or create a project</span><small>GitHub repository, local folder, or new project</small></button></section> : null}</div>
            <ComposerSelect<RunMode> ariaLabel="Run mode" onChange={(next) => { setMode(next); if (next === "chat") setMcpEnabled(false); }} options={[{ value: "chat", label: "Chat", description: "Answer without changing files" }, { value: "build", label: "Build", description: "Work inside a protected sandbox", disabled: providerId !== "codex" }]} value={mode} />
            <button aria-controls="composer-run-options" aria-expanded={optionsOpen} className="composer-options-trigger" onClick={() => setOptionsOpen((open) => !open)} type="button">Run options</button>
            <button aria-expanded={safetyDetailsOpen} aria-label="View safety details" className={`safety-chip safety-chip--${safetyPreview?.level ?? "loading"}`} disabled={safetyLoading} onClick={() => setSafetyDetailsOpen((open) => !open)} title="Click to see details" type="button"><Icon name="shield" /><span>{safetyLoading ? "Checking safety" : safetyPreview?.label ?? "Safety unavailable"}</span></button>
            {runStatus === "working" ? <button aria-label="Stop" className="composer-submit composer-submit--stop" disabled={busy} onClick={() => void stop()} type="button"><Icon name="stop" /></button> : <button aria-label={mode === "build" ? "Build project" : "Send message"} className="composer-submit" data-component-source="shadcn-button" disabled={busy || composer.trim() === "" || provider.status !== "ready" || safetyPreview === null || safetyLoading} type="submit"><Icon name="send" /></button>}
          </div>
          {optionsOpen ? <section aria-label="Run options panel" className="composer__advanced" id="composer-run-options">
            {runStatus === "working" ? <p className="composer-options-note">Changes apply to your next message.</p> : null}
            <span className="composer-option-label">Provider</span>
            <ComposerSelect<ProviderId> ariaLabel="Agent provider" className="composer-select--provider" onChange={(next) => {
              const nextProvider = providers.find((entry) => entry.id === next);
              if (nextProvider?.status !== "ready") return;
              setProviderId(next as RunnableProviderId);
              const nextModels = nextProvider.models ?? [];
              const nextThinkingLevels = nextProvider.thinking_levels ?? [];
              setModelId(nextModels[0]?.id ?? "");
              setThinking(nextThinkingLevels.includes("medium") ? "medium" : nextThinkingLevels[0] ?? "medium");
              if (next !== "codex") { setMode("chat"); setMcpEnabled(false); }
            }} options={providers.filter((entry) => entry.status === "ready").map((entry) => ({ value: entry.id, label: entry.label, description: "Verified on this device" }))} value={providerId} />
            <span className="composer-option-label">Model</span><ComposerSelect<string> ariaLabel="Agent model" className="composer-select--model" disabled={providerModels.length === 0 && modelId === ""} onChange={setModelId} options={[...(modelId !== "" && !providerModels.some((entry) => entry.id === modelId) ? [{ value: modelId, label: modelId, description: "Saved preference" }] : []), ...providerModels.map((entry) => ({ value: entry.id, label: entry.label, description: entry.recommended ? "Recommended" : "Available" }))]} value={modelId} />
            <span className="composer-option-label">Thinking</span><ComposerSelect<ThinkingLevel> ariaLabel="Thinking level" disabled={providerThinkingLevels.length === 0} onChange={setThinking} options={providerThinkingLevels.map((level) => ({ value: level, label: THINKING_LABELS[level], description: level === "low" ? "Fastest" : level === "medium" ? "Balanced" : level === "high" ? "More reasoning" : "Deepest reasoning" }))} value={thinking} />
            {mode === "build" ? <label className="mcp-toggle"><input aria-label="Allow configured MCP servers" checked={mcpEnabled} onChange={(event) => setMcpEnabled(event.target.checked)} type="checkbox" /><span>MCP</span></label> : null}
          </section> : null}
          {safetyDetailsOpen && safetyPreview ? <section aria-label="Safety details" className="safety-details"><header><Icon name="shield" /><div><strong>{safetyPreview.label}</strong><span>{safetyPreview.summary}</span></div></header><dl><div><dt>Files</dt><dd>{safetyPreview.filesystem}</dd></div><div><dt>Network</dt><dd>{safetyPreview.network}</dd></div><div><dt>Tools</dt><dd>{safetyPreview.mcp}</dd></div><div><dt>Output</dt><dd>{safetyPreview.output}</dd></div></dl></section> : null}
          {mode === "build" && mcpEnabled ? <p className="composer__notice">Configured MCP servers may access external systems. Corvus will work only inside a fresh build sandbox.</p> : null}
        </form>
        {confirmation ? createPortal(<div aria-label="Confirm protected build" aria-modal="true" className="safety-confirmation" onKeyDown={handleConfirmationKeyDown} ref={confirmationDialogRef} role="dialog"><div className="safety-confirmation__sheet"><header><Icon name="shield" /><div><span>{confirmation.safety.label}</span><h2>Confirm protected build</h2></div></header><p>{confirmation.safety.summary}</p><dl><div><dt>Workspace</dt><dd>{confirmation.safety.filesystem}</dd></div><div><dt>Network</dt><dd>{confirmation.safety.network}</dd></div><div><dt>External tools</dt><dd>{confirmation.safety.mcp}</dd></div><div><dt>Approval</dt><dd>{confirmation.safety.approvals}</dd></div></dl><div className="safety-confirmation__actions"><button className="text-button" onClick={() => setConfirmation(null)} type="button">Cancel</button><button className="primary-action" onClick={() => { const pending = confirmation; setConfirmation(null); void startPrompt(pending.prompt, pending.safety); }} type="button">Continue in sandbox</button></div></div></div>, document.body) : null}
      </div>
    </section>
  );
}

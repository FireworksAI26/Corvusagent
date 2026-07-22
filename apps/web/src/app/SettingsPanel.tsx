import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import type { GitHubAuthStatus } from "../api";
import type {
  ConversationApi,
  McpServerConfiguration,
  ProviderCatalogEntry,
  ProviderCredentialId,
  ProviderCredentialStatus,
  ResponseTone,
  RuntimePreferences,
  ThinkingLevel,
  RunnableProviderId
} from "./conversationApi";
import { ConversationApiError } from "./conversationApi";
import {
  loadDevicePreferences,
  saveDevicePreferences,
  type SafetyGuidance,
  type SendKeyMode,
  type ThemePreference
} from "./devicePreferences";
import {
  applyDesktopDeviceSettings,
  desktopControlsAvailable,
  type DesktopDeviceSettings
} from "./desktopPreferences";
import type { ExperienceMode, WorkspaceKind } from "./preferences";
import { FALLBACK_PROVIDERS } from "./providerDefaults";
import { featureErrorMessage } from "./featureFeedback";

type SettingsCategory = "general" | "models" | "agent" | "mcp" | "safety" | "appearance" | "account";
type ModelsView = "defaults" | "providers";
type LocalProviderId = "codex" | "claude";
type SettingsApi = Pick<ConversationApi, "getPreferences" | "listProviders" | "updatePreferences" | "listProviderCredentials" | "connectProviderCredential" | "verifyProviderCredential" | "removeProviderCredential" | "listMcpServers" | "addMcpServer" | "removeMcpServer" | "loginMcpServer">;
type AccountConnectionsApi = {
  authenticateGitHub(): Promise<GitHubAuthStatus>;
  getGitHubAuthStatus(): Promise<GitHubAuthStatus>;
};

function AccountBrandIcon({ provider }: { provider: "github" | "google" }) {
  if (provider === "github") {
    return <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M12 .7A11.5 11.5 0 0 0 8.4 23c.6.1.8-.3.8-.6v-2.2c-3.4.7-4.1-1.4-4.1-1.4-.6-1.4-1.4-1.8-1.4-1.8-1.1-.8.1-.8.1-.8 1.3.1 2 1.3 2 1.3 1.1 2 3 1.4 3.7 1 .1-.8.4-1.4.8-1.7-2.7-.3-5.5-1.3-5.5-5.7 0-1.3.5-2.3 1.2-3.1-.1-.3-.5-1.6.1-3.1 0 0 1-.3 3.2 1.2a11 11 0 0 1 5.8 0c2.2-1.5 3.2-1.2 3.2-1.2.6 1.5.2 2.8.1 3.1.8.8 1.2 1.8 1.2 3.1 0 4.4-2.8 5.4-5.5 5.7.4.4.8 1.1.8 2.2v3.3c0 .3.2.7.8.6A11.5 11.5 0 0 0 12 .7Z" /></svg>;
  }
  return <svg aria-hidden="true" viewBox="0 0 24 24"><path fill="#4285F4" d="M22.6 12.2c0-.7-.1-1.5-.2-2.2H12v4.3h6a5.2 5.2 0 0 1-2.2 3.3v2.8h3.6c2.1-2 3.2-4.8 3.2-8.2Z"/><path fill="#34A853" d="M12 23c3 0 5.5-1 7.4-2.6l-3.6-2.8c-1 .7-2.3 1.1-3.8 1.1-2.9 0-5.4-2-6.3-4.6H2v2.9A11.2 11.2 0 0 0 12 23Z"/><path fill="#FBBC05" d="M5.7 14.1a6.8 6.8 0 0 1 0-4.2V7H2a11.2 11.2 0 0 0 0 10l3.7-2.9Z"/><path fill="#EA4335" d="M12 5.3c1.7 0 3.2.6 4.3 1.7l3.2-3.1A10.7 10.7 0 0 0 2 7l3.7 2.9c.9-2.7 3.4-4.6 6.3-4.6Z"/></svg>;
}

const API_PROVIDERS: ReadonlyArray<{ id: ProviderCredentialId; label: string; environment: string }> = [
  { id: "openai", label: "OpenAI", environment: "OPENAI_API_KEY" },
  { id: "anthropic", label: "Anthropic", environment: "ANTHROPIC_API_KEY" },
  { id: "gemini", label: "Gemini", environment: "GEMINI_API_KEY" },
  { id: "xai", label: "xAI", environment: "XAI_API_KEY" },
  { id: "openrouter", label: "OpenRouter", environment: "OPENROUTER_API_KEY" },
  { id: "together", label: "Together AI", environment: "TOGETHER_API_KEY" },
  { id: "fireworks", label: "Fireworks AI", environment: "FIREWORKS_API_KEY" },
  { id: "groq", label: "Groq", environment: "GROQ_API_KEY" },
  { id: "mistral", label: "Mistral AI", environment: "MISTRAL_API_KEY" },
  { id: "deepseek", label: "DeepSeek", environment: "DEEPSEEK_API_KEY" }
];

const THINKING_LABELS: Record<ThinkingLevel, string> = {
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "Extra high",
  max: "Max"
};
const MODEL_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$/;

const CATEGORIES: ReadonlyArray<{ id: SettingsCategory; label: string }> = [
  { id: "general", label: "General" },
  { id: "models", label: "Models" },
  { id: "agent", label: "Agent" },
  { id: "mcp", label: "MCP" },
  { id: "safety", label: "Safety" },
  { id: "appearance", label: "Appearance" },
  { id: "account", label: "Account" }
];

const CATEGORY_DESCRIPTIONS: Record<SettingsCategory, string> = {
  general: "Your workspace profile, input behavior, and desktop preferences.",
  models: "Configure provider defaults exactly as your account exposes them.",
  agent: "Guidance applied to new runs.",
  mcp: "Configure which external tool servers Build runs may use.",
  safety: "Review the enforced boundaries for local agent runs.",
  appearance: "Choose how Corvus looks on this device.",
  account: "Manage identity and connected services."
};

const DEFAULT_RUNTIME_PREFERENCES: RuntimePreferences = {
  version: 0,
  default_provider: "codex",
  default_model: null,
  model_labels: {},
  default_effort: "medium",
  default_mode: "chat",
  mcp_enabled: false,
  response_tone: "balanced",
  custom_rules: "",
  updated_at: null
};

function modelPreferenceKey(providerId: LocalProviderId, modelId: string): string {
  return `${providerId}:${modelId}`;
}

function title(value: string): string {
  return value[0].toUpperCase() + value.slice(1);
}

function safeError(reason: unknown): string {
  return featureErrorMessage(reason, "settings");
}

function isHttpsMcpUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "https:"
      && url.hostname !== ""
      && url.username === ""
      && url.password === "";
  } catch {
    return false;
  }
}

function SettingsRow({ children, description, label }: {
  children: ReactNode;
  description: string;
  label: string;
}) {
  return (
    <div className="settings-row">
      <div><strong>{label}</strong><p>{description}</p></div>
      <div className="settings-row__control">{children}</div>
    </div>
  );
}

export function SettingsPanel({
  api,
  connectionsApi,
  experience,
  googleSignedIn = false,
  onGoogleSignIn,
  onBack,
  onExperienceChange,
  profileEditable = true,
  applyDesktopSettings = applyDesktopDeviceSettings,
  desktopAvailable = desktopControlsAvailable(),
  storage,
  workspaceId,
  workspaceKind
}: {
  api?: SettingsApi;
  connectionsApi?: AccountConnectionsApi;
  experience: ExperienceMode;
  googleSignedIn?: boolean;
  onGoogleSignIn?(): Promise<void> | void;
  onBack?(): void;
  onExperienceChange(experience: ExperienceMode): Promise<void>;
  profileEditable?: boolean;
  applyDesktopSettings?(settings: DesktopDeviceSettings): Promise<void>;
  desktopAvailable?: boolean;
  storage: Storage;
  workspaceId: string;
  workspaceKind: WorkspaceKind;
}) {
  const [category, setCategory] = useState<SettingsCategory>("general");
  const [modelsView, setModelsView] = useState<ModelsView>("defaults");
  const [theme, setTheme] = useState<ThemePreference>(() => loadDevicePreferences(storage, workspaceId).theme);
  const [sendKeyMode, setSendKeyMode] = useState<SendKeyMode>(() => loadDevicePreferences(storage, workspaceId).sendKeyMode);
  const [safetyGuidance, setSafetyGuidance] = useState<SafetyGuidance>(() => loadDevicePreferences(storage, workspaceId).safetyGuidance);
  const [runInBackground, setRunInBackground] = useState(() => loadDevicePreferences(storage, workspaceId).runInBackground);
  const [launchAtLogin, setLaunchAtLogin] = useState(() => loadDevicePreferences(storage, workspaceId).launchAtLogin);
  const [nativeNotifications, setNativeNotifications] = useState(() => loadDevicePreferences(storage, workspaceId).nativeNotifications);
  const [runtime, setRuntime] = useState<RuntimePreferences>(DEFAULT_RUNTIME_PREFERENCES);
  const [savedRuntime, setSavedRuntime] = useState<RuntimePreferences>(DEFAULT_RUNTIME_PREFERENCES);
  const [providers, setProviders] = useState<ProviderCatalogEntry[]>(FALLBACK_PROVIDERS);
  const [providerModelDrafts, setProviderModelDrafts] = useState<Record<"codex" | "claude", string>>({
    codex: FALLBACK_PROVIDERS.find((provider) => provider.id === "codex")?.models[0]?.id ?? "",
    claude: FALLBACK_PROVIDERS.find((provider) => provider.id === "claude")?.models[0]?.id ?? ""
  });
  const [newModelDrafts, setNewModelDrafts] = useState<Record<LocalProviderId, string>>({
    codex: "",
    claude: ""
  });
  const [providerDiscoveryError, setProviderDiscoveryError] = useState("");
  const [providerRefresh, setProviderRefresh] = useState(0);
  const [credentials, setCredentials] = useState<ProviderCredentialStatus[]>([]);
  const [credentialDrafts, setCredentialDrafts] = useState<Partial<Record<ProviderCredentialId, string>>>({});
  const [verifiedModels, setVerifiedModels] = useState<Partial<Record<ProviderCredentialId, string[]>>>({});
  const [mcpServers, setMcpServers] = useState<McpServerConfiguration[]>([]);
  const [mcpLoading, setMcpLoading] = useState(false);
  const [mcpError, setMcpError] = useState("");
  const [mcpRefresh, setMcpRefresh] = useState(0);
  const [mcpName, setMcpName] = useState("");
  const [mcpUrl, setMcpUrl] = useState("");
  const [profileExperience, setProfileExperience] = useState<ExperienceMode>(experience);
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [connectionBusy, setConnectionBusy] = useState<"github" | "google" | null>(null);
  const [connectionError, setConnectionError] = useState("");
  const [githubStatus, setGitHubStatus] = useState<GitHubAuthStatus | null>(null);
  const [githubStatusLoading, setGitHubStatusLoading] = useState(connectionsApi !== undefined);
  const [unsavedBarDismissed, setUnsavedBarDismissed] = useState(false);
  const [exitConfirmation, setExitConfirmation] = useState(false);
  const exitDialogRef = useRef<HTMLElement>(null);
  const profileLabel = `${title(experience)} / ${title(workspaceKind)}`;
  const credentialControlsAvailable = api?.connectProviderCredential !== undefined;
  const mcpUrlIsValid = mcpUrl.trim() === "" || isHttpsMcpUrl(mcpUrl.trim());
  const canAddMcpServer = api?.addMcpServer !== undefined
    && mcpName.trim() !== ""
    && isHttpsMcpUrl(mcpUrl.trim());
  const selectedProvider = useMemo(
    () => providers.find((provider) => provider.id === runtime.default_provider),
    [providers, runtime.default_provider]
  );
  const selectedThinkingLevels = selectedProvider?.status === "ready"
    ? selectedProvider.thinking_levels
    : [];

  useEffect(() => {
    if (connectionsApi === undefined) {
      setGitHubStatusLoading(false);
      return;
    }
    let current = true;
    setGitHubStatusLoading(true);
    void connectionsApi.getGitHubAuthStatus().then((nextStatus) => {
      if (current) setGitHubStatus(nextStatus);
    }).catch(() => {
      if (current) setConnectionError("GitHub status is unavailable. You can retry sign-in below.");
    }).finally(() => {
      if (current) setGitHubStatusLoading(false);
    });
    return () => { current = false; };
  }, [connectionsApi]);

  async function connectAccount(provider: "github" | "google"): Promise<void> {
    setConnectionBusy(provider);
    setConnectionError("");
    try {
      if (provider === "google") {
        if (onGoogleSignIn === undefined) throw new Error("google_sign_in_unavailable");
        await onGoogleSignIn();
        return;
      }
      if (connectionsApi === undefined) throw new Error("github_sign_in_unavailable");
      const nextStatus = await connectionsApi.authenticateGitHub();
      setGitHubStatus(nextStatus);
      if (!nextStatus.authenticated) throw new Error("github_authentication_failed");
    } catch (reason) {
      setConnectionError(featureErrorMessage(reason, provider === "github" ? "github" : "settings"));
    } finally {
      setConnectionBusy(null);
    }
  }

  useEffect(() => {
    if (!exitConfirmation) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    exitDialogRef.current?.querySelector<HTMLElement>("button")?.focus();
    return () => previousFocus?.focus();
  }, [exitConfirmation]);

  function handleExitDialogKeyDown(event: KeyboardEvent<HTMLElement>): void {
    if (event.key === "Escape") {
      event.preventDefault();
      setExitConfirmation(false);
      return;
    }
    if (event.key !== "Tab") return;
    const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>(
      "button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])"
    ));
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

  useEffect(() => {
    const device = loadDevicePreferences(storage, workspaceId);
    setTheme(device.theme);
    setSendKeyMode(device.sendKeyMode);
    setSafetyGuidance(device.safetyGuidance);
    setRunInBackground(device.runInBackground);
    setLaunchAtLogin(device.launchAtLogin);
    setNativeNotifications(device.nativeNotifications);
    setProfileExperience(experience);
    setStatus("");
    setError("");
    setProviderDiscoveryError("");
    if (api === undefined) {
      setRuntime({
        ...DEFAULT_RUNTIME_PREFERENCES,
        response_tone: device.responseTone,
        custom_rules: device.customRules
      });
      return;
    }
    let current = true;
    setBusy(true);
    void Promise.allSettled([api.getPreferences(), api.listProviders()])
      .then(([preferencesResult, catalogResult]) => {
        if (!current) return;
        const preferences = preferencesResult.status === "fulfilled"
          ? { ...preferencesResult.value, model_labels: preferencesResult.value.model_labels ?? {} }
          : { ...DEFAULT_RUNTIME_PREFERENCES, response_tone: device.responseTone, custom_rules: device.customRules };
        const catalog = catalogResult.status === "fulfilled" ? catalogResult.value : [];
        const knownProviderIds = new Set(FALLBACK_PROVIDERS.map((provider) => provider.id));
        const availableCatalog = [
          ...FALLBACK_PROVIDERS.map((fallback) => catalog.find((provider) => provider.id === fallback.id) ?? fallback),
          ...catalog.filter((provider) => !knownProviderIds.has(provider.id))
        ];
        if (catalogResult.status === "rejected") {
          setProviderDiscoveryError(`${safeError(catalogResult.reason)}. Retry local provider discovery.`);
        } else if (catalog.length === 0) {
          setProviderDiscoveryError("No local providers were verified. Check the Codex CLI installation, then retry discovery.");
        }
        if (preferencesResult.status === "rejected") {
          setError("Saved runtime preferences could not be loaded. Local defaults are shown.");
        }
        const drafts = {
          codex: availableCatalog.find((provider) => provider.id === "codex")?.models[0]?.id ?? "",
          claude: availableCatalog.find((provider) => provider.id === "claude")?.models[0]?.id ?? ""
        };
        if (preferences.default_model !== null) drafts[preferences.default_provider] = preferences.default_model;
        setProviderModelDrafts(drafts);
        setRuntime(preferences);
        setSavedRuntime(preferences);
        setProviders(availableCatalog);
      })
      .finally(() => {
        if (current) setBusy(false);
      });
    if (api.listProviderCredentials !== undefined) {
      void api.listProviderCredentials().then((statuses) => {
        if (current) setCredentials(statuses);
      }).catch(() => {
        if (current) setError("Provider credentials could not be checked. No key was read or exposed.");
      });
    }
    return () => { current = false; };
  }, [api, experience, providerRefresh, storage, workspaceId]);

  useEffect(() => {
    if (api?.listMcpServers === undefined) {
      setMcpServers([]);
      setMcpError("");
      return;
    }
    let current = true;
    setMcpLoading(true);
    setMcpError("");
    void api.listMcpServers().then((servers) => {
      if (current) setMcpServers(servers);
    }).catch(() => {
      if (current) setMcpError("Configured MCP servers could not be loaded. Existing Codex configuration was not changed.");
    }).finally(() => {
      if (current) setMcpLoading(false);
    });
    return () => { current = false; };
  }, [api, mcpRefresh]);

  function updateRuntime<Key extends keyof RuntimePreferences>(
    key: Key,
    value: RuntimePreferences[Key]
  ): void {
    setRuntime((current) => ({ ...current, [key]: value }));
    setDirty(true);
    setStatus("");
    setError("");
    setUnsavedBarDismissed(false);
  }

  function updateProvider(providerId: RunnableProviderId): void {
    const provider = providers.find((entry) => entry.id === providerId);
    const thinkingLevels = provider?.thinking_levels ?? [];
    const effort = thinkingLevels.includes("medium")
      ? "medium"
      : thinkingLevels[0] ?? "medium";
    setRuntime((current) => ({
      ...current,
      default_provider: providerId,
      default_model: (providerId === "codex" || providerId === "claude")
        ? providerModelDrafts[providerId].trim() || null
        : provider?.models[0]?.id ?? null,
      default_effort: effort,
      default_mode: providerId === "codex" ? current.default_mode : "chat",
      mcp_enabled: providerId === "codex" ? current.mcp_enabled : false
    }));
    setDirty(true);
    setStatus("");
  }

  async function saveSettings(): Promise<boolean> {
    const invalidModelLabel = Object.entries(runtime.model_labels ?? {})
      .find(([, label]) => label.trim() === "");
    if (invalidModelLabel !== undefined) {
      const [, modelId] = invalidModelLabel[0].split(":", 2);
      setError(`Display name for ${modelId ?? "this model"} cannot be blank.`);
      setStatus("");
      return false;
    }
    setBusy(true);
    setError("");
    setStatus("");
    const device = loadDevicePreferences(storage, workspaceId);
    const nextDevice = {
      ...device,
      theme,
      responseTone: runtime.response_tone,
      customRules: runtime.custom_rules,
      sendKeyMode,
      safetyGuidance,
      runInBackground,
      launchAtLogin,
      nativeNotifications
    };
    const completedSteps: string[] = [];
    try {
      const desktopChanged = runInBackground !== device.runInBackground
        || launchAtLogin !== device.launchAtLogin
        || nativeNotifications !== device.nativeNotifications;
      const runtimeChanged = runtime.default_provider !== savedRuntime.default_provider
        || runtime.default_model !== savedRuntime.default_model
        || runtime.default_effort !== savedRuntime.default_effort
        || runtime.default_mode !== savedRuntime.default_mode
        || runtime.mcp_enabled !== savedRuntime.mcp_enabled
        || runtime.response_tone !== savedRuntime.response_tone
        || runtime.custom_rules !== savedRuntime.custom_rules
        || JSON.stringify(runtime.model_labels ?? {}) !== JSON.stringify(savedRuntime.model_labels ?? {});
      let persistedRuntime: RuntimePreferences | null = null;
      if (api !== undefined && runtimeChanged) {
        persistedRuntime = await api.updatePreferences({
          expected_version: runtime.version,
          default_provider: runtime.default_provider,
          default_model: runtime.default_model,
          model_labels: runtime.model_labels ?? {},
          default_effort: runtime.default_effort,
          default_mode: runtime.default_mode,
          mcp_enabled: runtime.mcp_enabled,
          response_tone: runtime.response_tone,
          custom_rules: runtime.custom_rules
        });
        // A later device/profile failure must not make the next attempt reuse
        // the now-stale preference version. Acknowledge each durable step as
        // soon as it succeeds and leave only unfinished work dirty.
        setRuntime(persistedRuntime);
        setSavedRuntime(persistedRuntime);
        completedSteps.push("Runtime settings");
      }
      if (desktopAvailable && desktopChanged) {
        await applyDesktopSettings({ runInBackground, launchAtLogin, nativeNotifications });
      }
      saveDevicePreferences(storage, workspaceId, nextDevice);
      document.documentElement.dataset.theme = theme;
      completedSteps.push("Device settings");
      if (profileEditable && profileExperience !== experience) {
        await onExperienceChange(profileExperience);
        completedSteps.push("Workspace profile");
      }
      setStatus(persistedRuntime !== null ? "Saved for this local runtime" : "Saved on this device");
      setDirty(false);
      setUnsavedBarDismissed(false);
      setExitConfirmation(false);
      return true;
    } catch (reason) {
      if (
        reason instanceof ConversationApiError
        && reason.status === 409
        && reason.code === "preferences_version_conflict"
        && reason.detail?.current !== undefined
      ) {
        setRuntime(reason.detail.current);
        setDirty(false);
        setError("Settings changed in another session. The current saved values are loaded for review.");
      } else {
        const failure = safeError(reason);
        setError(completedSteps.length === 0
          ? failure
          : `${completedSteps.join(" and ")} were saved, but ${failure}`);
        setDirty(true);
      }
      return false;
    } finally {
      setBusy(false);
    }
  }

  const savedDevice = loadDevicePreferences(storage, workspaceId);
  const changes = [
    profileExperience !== experience ? `Experience: ${title(experience)} → ${title(profileExperience)}` : null,
    runtime.default_provider !== savedRuntime.default_provider ? `Provider: ${title(savedRuntime.default_provider)} → ${title(runtime.default_provider)}` : null,
    runtime.default_model !== savedRuntime.default_model ? `Model: ${savedRuntime.default_model ?? "Provider default"} → ${runtime.default_model ?? "Provider default"}` : null,
    runtime.default_effort !== savedRuntime.default_effort ? `Thinking: ${title(savedRuntime.default_effort)} → ${title(runtime.default_effort)}` : null,
    runtime.default_mode !== savedRuntime.default_mode ? `Default mode: ${title(savedRuntime.default_mode)} → ${title(runtime.default_mode)}` : null,
    runtime.response_tone !== savedRuntime.response_tone ? `Response style: ${title(savedRuntime.response_tone)} → ${title(runtime.response_tone)}` : null,
    runtime.custom_rules !== savedRuntime.custom_rules ? "Custom rules edited" : null,
    JSON.stringify(runtime.model_labels ?? {}) !== JSON.stringify(savedRuntime.model_labels ?? {}) ? "Model names edited" : null,
    theme !== savedDevice.theme ? `Theme: ${title(savedDevice.theme)} → ${title(theme)}` : null,
    sendKeyMode !== savedDevice.sendKeyMode ? "Message send keys changed" : null,
    safetyGuidance !== savedDevice.safetyGuidance ? "Safety guidance changed" : null,
    runInBackground !== savedDevice.runInBackground ? "Background mode changed" : null,
    launchAtLogin !== savedDevice.launchAtLogin ? "Launch at login changed" : null,
    nativeNotifications !== savedDevice.nativeNotifications ? "Native notifications changed" : null
  ].filter((change): change is string => change !== null);
  const hasUnsavedChanges = dirty || profileExperience !== experience || changes.length > 0;

  function selectCategory(next: SettingsCategory): void {
    setCategory(next);
    setError("");
    setStatus("");
  }

  function updateProviderModel(providerId: "codex" | "claude", value: string): void {
    const provider = providers.find((entry) => entry.id === providerId);
    const thinkingLevels = provider?.thinking_levels ?? [];
    const effort = thinkingLevels.includes(runtime.default_effort)
      ? runtime.default_effort
      : thinkingLevels.includes("medium")
        ? "medium"
        : thinkingLevels[0] ?? "medium";
    setProviderModelDrafts((current) => ({ ...current, [providerId]: value }));
    setRuntime((current) => ({
      ...current,
      default_provider: providerId,
      default_model: value.trim() || null,
      default_effort: effort,
      default_mode: providerId === "codex" ? current.default_mode : "chat",
      mcp_enabled: providerId === "codex" ? current.mcp_enabled : false
    }));
    setDirty(true);
    setStatus("");
    setError("");
    setUnsavedBarDismissed(false);
  }

  function updateModelLabel(
    providerId: LocalProviderId,
    modelId: string,
    discoveredLabel: string | null,
    value: string
  ): void {
    const key = modelPreferenceKey(providerId, modelId);
    setRuntime((current) => {
      const modelLabels = { ...(current.model_labels ?? {}) };
      if (discoveredLabel !== null && value === discoveredLabel) delete modelLabels[key];
      else modelLabels[key] = value;
      return { ...current, model_labels: modelLabels };
    });
    setDirty(true);
    setStatus("");
    setError("");
    setUnsavedBarDismissed(false);
  }

  function addManualModel(providerId: LocalProviderId): void {
    const modelId = newModelDrafts[providerId].trim();
    if (!MODEL_ID_PATTERN.test(modelId)) {
      setError("Use a valid model ID with letters, numbers, dots, underscores, colons, or hyphens.");
      return;
    }
    updateModelLabel(providerId, modelId, null, modelId);
    setNewModelDrafts((current) => ({ ...current, [providerId]: "" }));
  }

  function removeManualModel(providerId: LocalProviderId, modelId: string): void {
    const key = modelPreferenceKey(providerId, modelId);
    setRuntime((current) => {
      const modelLabels = { ...(current.model_labels ?? {}) };
      delete modelLabels[key];
      const provider = providers.find((entry) => entry.id === providerId);
      const replacement = provider?.models[0]?.id ?? null;
      return {
        ...current,
        default_model: current.default_provider === providerId && current.default_model === modelId
          ? replacement
          : current.default_model,
        model_labels: modelLabels
      };
    });
    setDirty(true);
    setUnsavedBarDismissed(false);
  }

  function requestBack(): void {
    if (hasUnsavedChanges) setExitConfirmation(true);
    else onBack?.();
  }

  async function connectCredential(provider: ProviderCredentialId): Promise<void> {
    const credential = credentialDrafts[provider]?.trim() ?? "";
    if (credential === "" || api?.connectProviderCredential === undefined) return;
    setBusy(true);
    setError("");
    try {
      const saved = await api.connectProviderCredential(provider, credential);
      setCredentials((current) => [...current.filter((entry) => entry.provider !== provider), saved]);
      setCredentialDrafts((current) => ({ ...current, [provider]: "" }));
      setStatus("Credential stored in the operating system keyring");
    } catch (reason) {
      setError(safeError(reason));
    } finally {
      setBusy(false);
    }
  }

  async function verifyCredential(provider: ProviderCredentialId): Promise<void> {
    if (api?.verifyProviderCredential === undefined) return;
    setBusy(true);
    setError("");
    try {
      const verified = await api.verifyProviderCredential(provider);
      setVerifiedModels((current) => ({ ...current, [provider]: verified.models }));
      setStatus("Provider verified with its authenticated model catalog");
    } catch (reason) {
      setError(safeError(reason));
    } finally {
      setBusy(false);
    }
  }

  async function removeCredential(provider: ProviderCredentialId): Promise<void> {
    if (api?.removeProviderCredential === undefined) return;
    setBusy(true);
    setError("");
    try {
      const saved = await api.removeProviderCredential(provider);
      setCredentials((current) => [...current.filter((entry) => entry.provider !== provider), saved]);
      setVerifiedModels((current) => ({ ...current, [provider]: [] }));
      setStatus(saved.configured ? "Environment credential remains configured" : "Provider credential removed");
    } catch (reason) {
      setError(safeError(reason));
    } finally {
      setBusy(false);
    }
  }

  async function addMcpServer(): Promise<void> {
    if (!canAddMcpServer || api?.addMcpServer === undefined) return;
    setBusy(true);
    setError("");
    setMcpError("");
    try {
      const server = await api.addMcpServer(mcpName.trim(), mcpUrl.trim());
      setMcpServers((current) => [...current.filter((item) => item.name !== server.name), server]);
      setMcpName("");
      setMcpUrl("");
      setStatus("MCP server added to Codex");
    } catch (reason) {
      setError(safeError(reason));
    } finally {
      setBusy(false);
    }
  }

  async function removeMcpServer(name: string): Promise<void> {
    if (api?.removeMcpServer === undefined) return;
    setBusy(true);
    setError("");
    setMcpError("");
    try {
      await api.removeMcpServer(name);
      setMcpServers((current) => current.filter((server) => server.name !== name));
      setStatus("MCP server removed from Codex");
    } catch (reason) {
      setError(safeError(reason));
    } finally {
      setBusy(false);
    }
  }

  async function loginMcpServer(name: string): Promise<void> {
    if (api?.loginMcpServer === undefined) return;
    setBusy(true);
    setError("");
    setMcpError("");
    try {
      await api.loginMcpServer(name);
      setMcpServers(await api.listMcpServers?.() ?? []);
      setStatus("MCP authentication completed in your browser");
    } catch (reason) {
      setError(safeError(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="settings-workspace">
      <aside className="settings-sidebar">
        {onBack ? <button className="settings-back" onClick={requestBack} type="button"><span aria-hidden="true">←</span> Back to app</button> : null}
        <nav aria-label="Settings categories" className="settings-categories">
          {CATEGORIES.map((item) => (
            <button
              aria-current={category === item.id ? "page" : undefined}
              key={item.id}
              onClick={() => selectCategory(item.id)}
              type="button"
            >{item.label}</button>
          ))}
        </nav>
      </aside>
      <div className="settings-content">
        <div className="settings-section" data-category={category}>
          {category === "general" ? <>
            <div className="settings-section__heading"><h1>General</h1><p>{CATEGORY_DESCRIPTIONS.general}</p></div>
            <SettingsRow description="Shown in the app header and used to tailor navigation." label="Current setup"><span className="settings-value">{profileLabel}</span></SettingsRow>
            <SettingsRow description="Switch between everyday and developer language." label="Experience">
              <select aria-label="Experience" onChange={(event) => { setProfileExperience(event.target.value as ExperienceMode); setDirty(true); setUnsavedBarDismissed(false); }} value={profileExperience}><option value="everyday">Everyday</option><option value="developer">Developer</option></select>
            </SettingsRow>
            <SettingsRow description="Workspace membership and authority stay attached to this workspace." label="Workspace type"><select aria-label="Workspace type" disabled value={workspaceKind}><option value="individual">Individual</option><option value="team">Team</option></select></SettingsRow>
            <div className="settings-field"><span className="settings-field__label">Send messages</span><p>Adaptive sends single-line prompts with Enter and multiline prompts with Ctrl+Enter.</p><div className="segmented-choice" role="radiogroup" aria-label="Composer send keys"><label><input checked={sendKeyMode === "adaptive"} onChange={() => { setSendKeyMode("adaptive"); setDirty(true); }} type="radio" />Adaptive</label><label><input checked={sendKeyMode === "enter"} onChange={() => { setSendKeyMode("enter"); setDirty(true); }} type="radio" />Enter sends</label><label><input checked={sendKeyMode === "ctrl-enter"} onChange={() => { setSendKeyMode("ctrl-enter"); setDirty(true); }} type="radio" />Ctrl+Enter sends</label></div></div>
            <div className="settings-section__subheading"><h3>Desktop background</h3><p>Keep local schedules available and control native startup behavior.</p></div>
            <SettingsRow description={desktopAvailable ? "Closing the window keeps Corvus and its local scheduler in the system tray." : "Available in the installed Corvus desktop app."} label="Run in background"><label className="switch"><input aria-label="Run in background" checked={runInBackground} disabled={!desktopAvailable} onChange={(event) => { setRunInBackground(event.target.checked); setDirty(true); }} type="checkbox" /><span /></label></SettingsRow>
            <SettingsRow description={desktopAvailable ? "Start Corvus after you sign in to this computer." : "Available in the installed Corvus desktop app."} label="Launch at login"><label className="switch"><input aria-label="Launch at login" checked={launchAtLogin} disabled={!desktopAvailable} onChange={(event) => { setLaunchAtLogin(event.target.checked); setDirty(true); }} type="checkbox" /><span /></label></SettingsRow>
            <SettingsRow description={desktopAvailable ? "Show redacted completion and review-required notifications." : "Available in the installed Corvus desktop app."} label="Native notifications"><label className="switch"><input aria-label="Native notifications" checked={nativeNotifications} disabled={!desktopAvailable} onChange={(event) => { setNativeNotifications(event.target.checked); setDirty(true); }} type="checkbox" /><span /></label></SettingsRow>
            {!profileEditable ? <p className="field-note">Profile changes are available after signing in on the web app.</p> : null}
          </> : null}

          {category === "models" ? <>
            <div className="settings-section__heading"><h1>Models</h1><p>{CATEGORY_DESCRIPTIONS.models}</p></div>
            {providerDiscoveryError ? <div className="provider-discovery" role="alert"><span>{providerDiscoveryError}</span><button className="button" disabled={busy} onClick={() => setProviderRefresh((value) => value + 1)} type="button">Retry discovery</button></div> : null}
            <div aria-label="Model settings view" className="settings-view-switch"><button aria-pressed={modelsView === "defaults"} onClick={() => setModelsView("defaults")} type="button">Defaults</button><button aria-pressed={modelsView === "providers"} onClick={() => setModelsView("providers")} type="button">Providers</button></div>
            {modelsView === "defaults" ? <>
              <div className="settings-field"><span className="settings-field__label">Default provider</span><p>Choose the verified provider used when a new conversation opens.</p><div className="segmented-choice" role="radiogroup" aria-label="Default provider">{providers.filter((entry) => entry.status === "ready").map((entry) => <label key={entry.id}><input checked={runtime.default_provider === entry.id} disabled={busy} name="default-provider" onChange={() => updateProvider(entry.id as RunnableProviderId)} type="radio" />{entry.label}</label>)}</div></div>
              <div className="settings-field"><span className="settings-field__label">Default thinking</span><p>Options appear only after the selected provider is verified.</p>{selectedThinkingLevels.length > 0 ? <div className="segmented-choice" role="radiogroup" aria-label="Default thinking">{selectedThinkingLevels.map((effort) => <label key={effort}><input checked={runtime.default_effort === effort} name="default-thinking" onChange={() => updateRuntime("default_effort", effort)} type="radio" />{THINKING_LABELS[effort]}</label>)}</div> : <p className="settings-callout">Thinking options unavailable until provider discovery succeeds.</p>}</div>
              <div className="settings-field"><span className="settings-field__label">Default mode</span><p>Chat is read-only. Build uses a fresh writable sandbox and returns an artifact.</p><div className="segmented-choice" role="radiogroup" aria-label="Default mode"><label><input checked={runtime.default_mode === "chat"} name="default-mode" onChange={() => { updateRuntime("default_mode", "chat"); updateRuntime("mcp_enabled", false); }} type="radio" />Chat</label><label><input checked={runtime.default_mode === "build"} disabled={runtime.default_provider !== "codex"} name="default-mode" onChange={() => updateRuntime("default_mode", "build")} type="radio" />Build</label></div></div>
            </> : <>
              <div className="provider-model-settings">{providers.filter((entry) => entry.id === "codex" || entry.id === "claude").map((entry) => {
                const providerId = entry.id as LocalProviderId;
                const discoveredIds = new Set(entry.models.map((model) => model.id));
                const manualModels = Object.entries(runtime.model_labels ?? {})
                  .filter(([key]) => key.startsWith(`${providerId}:`) && !discoveredIds.has(key.slice(providerId.length + 1)))
                  .map(([key, label]) => ({ id: key.slice(providerId.length + 1), label, recommended: false, manual: true }));
                const models = [
                  ...entry.models.map((model) => ({ ...model, manual: false })),
                  ...manualModels
                ];
                return <section className="provider-model-settings__provider" data-status={entry.status} key={entry.id}>
                  <header><div><h2>{entry.label}</h2><p>{entry.status === "ready" ? entry.status_label : `${entry.status_label}. Models remain visible, but Corvus will not run them until verification succeeds.`}</p></div><span>{models.length} {models.length === 1 ? "model" : "models"}</span></header>
                  <div className="provider-model-list">{models.length === 0 ? <p className="settings-callout">No models discovered. Add a model ID below if this provider supports one on your device.</p> : models.map((model) => {
                    const key = modelPreferenceKey(providerId, model.id);
                    const displayLabel = (runtime.model_labels ?? {})[key] ?? model.label;
                    return <article className="provider-model-row" key={model.id}>
                      <label className="provider-model-default"><input aria-label={`Use ${model.label} by default`} checked={runtime.default_provider === providerId && runtime.default_model === model.id} disabled={busy || entry.status !== "ready"} name="default-model" onChange={() => updateProviderModel(providerId, model.id)} type="radio" /><span>Default</span></label>
                      <label><span>Display name</span><input aria-invalid={displayLabel.trim() === ""} aria-label={`${entry.label} ${model.id} display name`} disabled={busy} maxLength={100} onChange={(event) => updateModelLabel(providerId, model.id, model.manual ? null : model.label, event.target.value)} value={displayLabel} /></label>
                      <div className="provider-model-id"><code>{model.id}</code><small>{model.manual ? "Manually configured" : model.recommended ? "Detected · Recommended" : "Detected"}</small></div>
                      {model.manual ? <button className="text-button" disabled={busy} onClick={() => removeManualModel(providerId, model.id)} type="button">Remove</button> : null}
                    </article>;
                  })}</div>
                  <div className="provider-model-add"><label htmlFor={`new-provider-model-${providerId}`}>Add model ID</label><input id={`new-provider-model-${providerId}`} maxLength={100} onChange={(event) => setNewModelDrafts((current) => ({ ...current, [providerId]: event.target.value }))} placeholder="provider-model-id" value={newModelDrafts[providerId]} /><button className="button" disabled={busy || !MODEL_ID_PATTERN.test(newModelDrafts[providerId].trim())} onClick={() => addManualModel(providerId)} type="button">Add model</button></div>
                </section>;
              })}</div>
              <div className="provider-connections"><div className="settings-section__subheading"><h3>API providers</h3><p>Keys are write-only and remain in your operating system keyring. API providers are Chat-only until a verified sandbox adapter exists.</p>{credentialControlsAvailable ? null : <p className="settings-callout">Open Corvus desktop to manage API credentials through the verified local runtime.</p>}</div>{API_PROVIDERS.map((provider) => { const credentialStatus = credentials.find((entry) => entry.provider === provider.id); const configured = credentialStatus?.configured ?? false; return <section className="provider-connection" key={provider.id}><div><strong>{provider.label}</strong><span>{configured ? `Connected via ${credentialStatus?.source}` : `Not connected, or set ${provider.environment}`}</span>{(verifiedModels[provider.id]?.length ?? 0) > 0 ? <small>{verifiedModels[provider.id]?.join(", ")}</small> : null}</div><label className="sr-only" htmlFor={`provider-key-${provider.id}`}>{provider.label} API key</label><input autoComplete="off" disabled={!credentialControlsAvailable} id={`provider-key-${provider.id}`} onChange={(event) => setCredentialDrafts((current) => ({ ...current, [provider.id]: event.target.value }))} placeholder={configured ? "Paste a replacement key" : "Paste API key"} type="password" value={credentialDrafts[provider.id] ?? ""} /><div className="provider-connection__actions"><button disabled={!credentialControlsAvailable || busy || (credentialDrafts[provider.id]?.trim() ?? "") === ""} onClick={() => void connectCredential(provider.id)} type="button">{configured ? `Replace ${provider.label}` : `Connect ${provider.label}`}</button>{configured ? <><button disabled={busy} onClick={() => void verifyCredential(provider.id)} type="button">Verify {provider.label}</button><button disabled={busy || credentialStatus?.source === "environment"} onClick={() => void removeCredential(provider.id)} title={credentialStatus?.source === "environment" ? `Remove ${provider.environment} from the environment` : undefined} type="button">Remove {provider.label}</button></> : null}</div></section>; })}</div>
            </>}
          </> : null}

          {category === "agent" ? <>
            <div className="settings-section__heading"><h1>Agent</h1><p>{CATEGORY_DESCRIPTIONS.agent}</p></div>
            <SettingsRow description="Choose the usual level of explanation." label="Response style"><select aria-label="Response style" onChange={(event) => updateRuntime("response_tone", event.target.value as ResponseTone)} value={runtime.response_tone}><option value="concise">Concise</option><option value="balanced">Balanced</option><option value="detailed">Detailed</option></select></SettingsRow>
            <div className="settings-field"><label htmlFor="settings-rules">Custom rules</label><p>Presentation guidance only. Rules cannot change sandbox, approval, credential, budget, or authority policy.</p><textarea id="settings-rules" maxLength={20_000} onChange={(event) => updateRuntime("custom_rules", event.target.value)} placeholder="Example: Always include a short verification checklist." rows={7} value={runtime.custom_rules} /></div>
          </> : null}

          {category === "mcp" ? <>
            <div className="settings-section__heading"><h1>MCP</h1><p>{CATEGORY_DESCRIPTIONS.mcp}</p></div>
            <SettingsRow description="MCP tools may access external systems. Corvus keeps them off for ordinary chats." label="Enable by default"><label className="switch"><input aria-label="Enable MCP by default" checked={runtime.mcp_enabled} disabled={runtime.default_provider !== "codex" || runtime.default_mode !== "build"} onChange={(event) => updateRuntime("mcp_enabled", event.target.checked)} type="checkbox" /><span /></label></SettingsRow>
            <div className="settings-section__subheading"><h3>Configured servers</h3><p>These are read directly from your Codex configuration. Credential values are never displayed.</p></div>
            {api?.listMcpServers === undefined ? <p className="settings-callout">Open Corvus desktop to read and manage your Codex MCP configuration.</p> : null}
            {mcpError ? <p className="settings-error" role="alert">{mcpError} <button className="text-button" disabled={mcpLoading} onClick={() => setMcpRefresh((value) => value + 1)} type="button">Retry MCP</button></p> : null}
            <div className="mcp-server-list">{mcpLoading ? <p className="settings-callout">Reading MCP servers from Codex…</p> : !mcpError && mcpServers.length === 0 && api?.listMcpServers !== undefined ? <p className="settings-callout">No MCP servers are configured.</p> : mcpServers.map((server) => <article key={server.name}><div><strong>{server.name}</strong><span>{server.transport.replaceAll("_", " ")} / {server.endpoint}</span><small>{server.auth_status.replaceAll("_", " ")}</small></div><div>{server.auth_status === "not_logged_in" ? <button className="button" disabled={busy} onClick={() => void loginMcpServer(server.name)} type="button">Sign in</button> : null}<button className="button" disabled={busy} onClick={() => void removeMcpServer(server.name)} type="button">Remove</button></div></article>)}</div>
            <div className="mcp-add-server"><h3>Add remote server</h3><p>Use an HTTPS MCP endpoint. OAuth sign-in remains with Codex.</p><label htmlFor="mcp-server-name">Name</label><input id="mcp-server-name" onChange={(event) => setMcpName(event.target.value)} placeholder="example" value={mcpName} /><label htmlFor="mcp-server-url">Server URL</label><input aria-describedby={!mcpUrlIsValid ? "mcp-server-url-error" : undefined} aria-invalid={!mcpUrlIsValid} id="mcp-server-url" onChange={(event) => setMcpUrl(event.target.value)} placeholder="https://example.com/mcp" type="url" value={mcpUrl} />{!mcpUrlIsValid ? <small className="settings-error" id="mcp-server-url-error" role="alert">Use an HTTPS URL without embedded credentials.</small> : null}<button className="button button--primary" disabled={busy || !canAddMcpServer} onClick={() => void addMcpServer()} type="button">Add MCP server</button></div>
          </> : null}

          {category === "safety" ? <>
            <div className="settings-section__heading"><h1>Safety</h1><p>{CATEGORY_DESCRIPTIONS.safety}</p></div>
            <SettingsRow description="Every Build is bound to the exact policy shown before it starts." label="Build confirmation"><span className="settings-value">Always on in this alpha</span></SettingsRow>
            <SettingsRow description="Build work uses a fresh scratch workspace; your original project stays unchanged." label="Workspace isolation"><span className="settings-value">Enforced by runtime</span></SettingsRow>
            <SettingsRow description="Network behavior follows the selected CLI sandbox policy. Corvus grants no separate permission." label="Network"><span className="settings-value">No additional grant</span></SettingsRow>
            <SettingsRow description="Stop remains available while a run is active and sends an owner-scoped cancellation." label="Emergency stop"><span className="settings-value">Available during every run</span></SettingsRow>
            <div className="settings-field"><span className="settings-field__label">Safety guidance</span><p>Choose how much evidence Corvus shows while it works. This never weakens confirmation, isolation, MCP warnings, or sandbox enforcement.</p><div className="segmented-choice" role="radiogroup" aria-label="Safety guidance"><label><input checked={safetyGuidance === "standard"} onChange={() => { setSafetyGuidance("standard"); setDirty(true); }} type="radio" />Standard safety guidance</label><label><input checked={safetyGuidance === "detailed"} onChange={() => { setSafetyGuidance("detailed"); setDirty(true); }} type="radio" />Detailed safety guidance</label></div></div>
            <p className="settings-callout">Completed Build runs include an owner-scoped receipt with the locked policy, observed activity, artifact hash, and screening result.</p>
          </> : null}

          {category === "appearance" ? <>
            <div className="settings-section__heading"><h1>Appearance</h1><p>{CATEGORY_DESCRIPTIONS.appearance}</p></div>
            <SettingsRow description="Follow your system or choose a fixed theme." label="Theme"><select aria-label="Theme" onChange={(event) => { setTheme(event.target.value as ThemePreference); setDirty(true); }} value={theme}><option value="system">System</option><option value="light">Light</option><option value="dark">Dark</option></select></SettingsRow>
          </> : null}

          {category === "account" ? <>
            <div className="settings-section__heading"><h1>Account</h1><p>{CATEGORY_DESCRIPTIONS.account}</p></div>
            <SettingsRow description={api === undefined ? "Open the local app to run agents on this computer." : "Preferences are protected by this paired local session."} label="Runtime"><span className="settings-value">{api === undefined ? "Web / Preview" : "This computer / Connected"}</span></SettingsRow>
            <div className="settings-section__subheading"><h3>Connected accounts</h3><p>Google opens a browser sign-in. GitHub confirms your CLI session or opens GitHub's browser flow when needed. Corvus only connects after you choose it here.</p></div>
            <div className="account-connections">
              <button className="account-connection account-connection--google" disabled={connectionBusy !== null || onGoogleSignIn === undefined || googleSignedIn} onClick={() => void connectAccount("google")} type="button"><AccountBrandIcon provider="google" /><span><strong>{googleSignedIn ? "Google connected" : "Sign in with Google"}</strong><small>{googleSignedIn ? "Your Corvus Web identity is active." : "Open Corvus Web in your browser for identity and device continuity."}</small></span><b aria-hidden="true">{connectionBusy === "google" ? "…" : googleSignedIn ? "✓" : "→"}</b></button>
              <button className="account-connection account-connection--github" disabled={connectionBusy !== null || connectionsApi === undefined || githubStatusLoading || githubStatus?.authenticated === true} onClick={() => void connectAccount("github")} type="button"><AccountBrandIcon provider="github" /><span><strong>{githubStatusLoading ? "Checking GitHub…" : githubStatus?.authenticated ? "GitHub connected" : "Sign in with GitHub"}</strong><small>{githubStatusLoading ? "Checking authorization for this Corvus installation." : githubStatus?.authenticated ? `Authorized for this Corvus installation on ${githubStatus.hostname}.` : "Authorize your GitHub CLI session; a browser opens when sign-in is required."}</small></span><b aria-hidden="true">{connectionBusy === "github" || githubStatusLoading ? "…" : githubStatus?.authenticated ? "✓" : "→"}</b></button>
            </div>
            {connectionError ? <p className="settings-error" role="alert">{connectionError}</p> : null}
          </> : null}

          {category !== "account" ? <div className="settings-actions"><button className="button button--primary" disabled={busy || !hasUnsavedChanges} onClick={() => void saveSettings()} type="button">{busy ? "Saving…" : "Save changes"}</button></div> : null}
          {status ? <p className="save-status" role="status">{status}</p> : null}
          {error ? <p className="settings-error" role="alert">{error}</p> : null}
        </div>
      </div>
      {hasUnsavedChanges && !unsavedBarDismissed ? <section aria-label="Unsaved settings" aria-live="polite" className="settings-unsaved" role="region"><div><strong>You have unsaved changes</strong><span>{changes.join(", ")}</span></div><button className="button button--primary" disabled={busy} onClick={() => void saveSettings()} type="button">Save</button><button className="button" onClick={() => setUnsavedBarDismissed(true)} type="button">Keep editing</button></section> : null}
      {exitConfirmation ? <div className="settings-exit-backdrop"><section aria-label="Unsaved settings confirmation" aria-modal="true" className="settings-exit-dialog" onKeyDown={handleExitDialogKeyDown} ref={exitDialogRef} role="dialog"><h2>Save your changes?</h2><p>These settings have not been saved:</p><ul>{changes.map((change) => <li key={change}>{change}</li>)}</ul><div><button className="button" onClick={() => setExitConfirmation(false)} type="button">Continue editing</button><button className="button" onClick={() => onBack?.()} type="button">Discard changes</button><button className="button button--primary" onClick={() => void saveSettings().then((saved) => { if (saved) onBack?.(); })} type="button">Save and leave</button></div></section></div> : null}
    </section>
  );
}

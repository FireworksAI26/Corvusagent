import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MemoryStorage } from "../test/memoryStorage";
import { ConversationApiError, type ConversationApi } from "./conversationApi";
import { loadDevicePreferences } from "./devicePreferences";
import { SettingsPanel } from "./SettingsPanel";

function settingsApi(): Pick<ConversationApi, "getPreferences" | "listProviders" | "updatePreferences"> {
  return {
    getPreferences: vi.fn().mockResolvedValue({
      version: 2,
      default_provider: "codex",
      default_model: "gpt-5.6-sol",
      model_labels: {},
      default_effort: "high",
      default_mode: "build",
      mcp_enabled: false,
      response_tone: "balanced",
      custom_rules: "",
      updated_at: "2026-07-17T10:00:00Z"
    }),
    listProviders: vi.fn().mockResolvedValue([{
      id: "codex",
      label: "Codex",
      status: "ready",
      runtime: "local",
      status_label: "Detected on this device",
      thinking_levels: ["low", "medium", "high", "xhigh"],
      supports_mcp: true,
      models: [
        { id: "gpt-5.6-sol", label: "GPT-5.6 Sol", recommended: true },
        { id: "gpt-5.6-terra", label: "GPT-5.6 Terra", recommended: false }
      ]
    }]),
    updatePreferences: vi.fn().mockImplementation(async (preferences) => ({
      ...preferences,
      version: preferences.expected_version + 1,
      updated_at: "2026-07-17T10:01:00Z"
    }))
  };
}

describe("SettingsPanel", () => {
  it("tracks profile changes in the shared unsaved bar and avoids unrelated desktop mutations", async () => {
    const applyDesktopSettings = vi.fn().mockResolvedValue(undefined);
    const onExperienceChange = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <SettingsPanel api={settingsApi()} applyDesktopSettings={applyDesktopSettings} desktopAvailable
        experience="developer" onExperienceChange={onExperienceChange} storage={new MemoryStorage()}
        workspaceId="workspace-profile" workspaceKind="individual" />
    );

    await user.selectOptions(screen.getByLabelText("Experience"), "everyday");
    const bar = screen.getByRole("region", { name: "Unsaved settings" });
    expect(screen.getByRole("button", { name: "Keep editing" })).toBeVisible();
    expect(bar).toHaveTextContent("Experience: Developer → Everyday");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onExperienceChange).toHaveBeenCalledWith("everyday"));
    expect(applyDesktopSettings).not.toHaveBeenCalled();
  });

  it("does not commit the workspace profile when runtime settings fail to save", async () => {
    const api = settingsApi();
    vi.mocked(api.updatePreferences).mockRejectedValue(new Error("runtime_save_failed"));
    const onExperienceChange = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <SettingsPanel
        api={api}
        experience="developer"
        onExperienceChange={onExperienceChange}
        storage={new MemoryStorage()}
        workspaceId="workspace-atomic-profile"
        workspaceKind="individual"
      />
    );

    await user.selectOptions(screen.getByLabelText("Experience"), "everyday");
    await user.click(screen.getByRole("button", { name: "Agent" }));
    await user.selectOptions(screen.getByLabelText("Response style"), "concise");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/runtime save failed/i);
    expect(onExperienceChange).not.toHaveBeenCalled();
  });

  it("acknowledges a saved runtime before retrying a failed desktop-only step", async () => {
    const api = settingsApi();
    const applyDesktopSettings = vi.fn()
      .mockRejectedValueOnce(new Error("desktop_apply_failed"))
      .mockResolvedValueOnce(undefined);
    const user = userEvent.setup();
    render(
      <SettingsPanel
        api={api}
        applyDesktopSettings={applyDesktopSettings}
        desktopAvailable
        experience="developer"
        onExperienceChange={vi.fn().mockResolvedValue(undefined)}
        storage={new MemoryStorage()}
        workspaceId="workspace-partial-save"
        workspaceKind="individual"
      />
    );

    await user.click(screen.getByLabelText("Run in background"));
    await user.click(screen.getByRole("button", { name: "Agent" }));
    await user.selectOptions(screen.getByLabelText("Response style"), "concise");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/runtime settings were saved.*desktop/i);
    expect(api.updatePreferences).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(applyDesktopSettings).toHaveBeenCalledTimes(2));
    expect(api.updatePreferences).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("Saved on this device")).toBeVisible();
  });

  it("contains keyboard focus while confirming unsaved settings", async () => {
    const onBack = vi.fn();
    const user = userEvent.setup();
    render(<SettingsPanel api={settingsApi()} experience="developer" onBack={onBack}
      onExperienceChange={vi.fn()} storage={new MemoryStorage()}
      workspaceId="workspace-exit-focus" workspaceKind="individual" />);

    await user.selectOptions(screen.getByLabelText("Experience"), "everyday");
    await user.click(screen.getByRole("button", { name: "Back to app" }));
    const dialog = screen.getByRole("dialog", { name: "Unsaved settings confirmation" });
    expect(screen.getByRole("button", { name: "Continue editing" })).toHaveFocus();
    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(screen.getByRole("button", { name: "Save and leave" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(dialog).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Back to app" })).toHaveFocus();
    expect(onBack).not.toHaveBeenCalled();
  });

  it("shows every provider model, edits labels, and chooses an explicit default", async () => {
    const api = settingsApi();
    render(<SettingsPanel api={api} experience="developer" onExperienceChange={vi.fn()}
      storage={new MemoryStorage()} workspaceId="workspace-model-text" workspaceKind="individual" />);
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Models" }));
    expect(screen.getByRole("heading", { name: "Models", level: 1 })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Settings" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Defaults" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByLabelText("OpenAI API key")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Providers" }));
    const solLabel = await screen.findByRole("textbox", { name: "Codex gpt-5.6-sol display name" });
    expect(solLabel).toHaveValue("GPT-5.6 Sol");
    expect(screen.getByRole("textbox", { name: "Codex gpt-5.6-terra display name" })).toHaveValue("GPT-5.6 Terra");
    await user.clear(solLabel);
    await user.type(solLabel, "My Sol model");
    await user.click(screen.getByRole("radio", { name: "Use GPT-5.6 Terra by default" }));
    expect(screen.getByText("You have unsaved changes")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.updatePreferences).toHaveBeenCalledWith(expect.objectContaining({
      default_provider: "codex",
      default_model: "gpt-5.6-terra",
      model_labels: { "codex:gpt-5.6-sol": "My Sol model" }
    })));
  });

  it("identifies a blank model display name before saving any settings", async () => {
    const api = settingsApi();
    render(<SettingsPanel api={api} experience="developer" onExperienceChange={vi.fn()}
      storage={new MemoryStorage()} workspaceId="workspace-model-validation" workspaceKind="individual" />);
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Models" }));
    await user.click(screen.getByRole("button", { name: "Providers" }));
    const solLabel = await screen.findByRole("textbox", { name: "Codex gpt-5.6-sol display name" });
    await user.clear(solLabel);
    expect(solLabel).toHaveAttribute("aria-invalid", "true");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Display name for gpt-5.6-sol cannot be blank."
    );
    expect(api.updatePreferences).not.toHaveBeenCalled();
  });

  it("adds and removes a manually configured provider model", async () => {
    const api = settingsApi();
    const user = userEvent.setup();
    render(<SettingsPanel api={api} experience="developer" onExperienceChange={vi.fn()}
      storage={new MemoryStorage()} workspaceId="workspace-manual-model" workspaceKind="individual" />);

    await user.click(screen.getByRole("button", { name: "Models" }));
    await user.click(screen.getByRole("button", { name: "Providers" }));
    await user.type(screen.getByLabelText("Add model ID", { selector: "#new-provider-model-codex" }), "custom-codex-model");
    await user.click(screen.getAllByRole("button", { name: "Add model" })[0]);
    expect(screen.getByRole("textbox", { name: "Codex custom-codex-model display name" })).toHaveValue("custom-codex-model");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.updatePreferences).toHaveBeenCalledWith(expect.objectContaining({
      model_labels: { "codex:custom-codex-model": "custom-codex-model" }
    })));

    await user.click(screen.getByRole("button", { name: "Remove" }));
    expect(screen.queryByRole("textbox", { name: "Codex custom-codex-model display name" })).not.toBeInTheDocument();
  });

  it("does not carry an Agent save error into MCP settings", async () => {
    const api = settingsApi();
    vi.mocked(api.updatePreferences).mockRejectedValue(new Error("save_failed"));
    render(<SettingsPanel api={api} experience="developer" onExperienceChange={vi.fn()}
      storage={new MemoryStorage()} workspaceId="workspace-scoped-error" workspaceKind="individual" />);
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Agent" }));
    await user.selectOptions(screen.getByLabelText("Response style"), "concise");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/save failed/i);

    await user.click(screen.getByRole("button", { name: "MCP" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps MCP discovery failure distinct from an empty configuration and supports retry", async () => {
    const listMcpServers = vi.fn()
      .mockRejectedValueOnce(new Error("mcp_list_failed"))
      .mockResolvedValueOnce([{ name: "github", enabled: true, transport: "streamable_http", endpoint: "https://example.test/mcp", auth_status: "authenticated" }]);
    const api = { ...settingsApi(), listMcpServers };
    const user = userEvent.setup();
    render(<SettingsPanel api={api} experience="developer" onExperienceChange={vi.fn()}
      storage={new MemoryStorage()} workspaceId="workspace-mcp-retry" workspaceKind="individual" />);

    await user.click(screen.getByRole("button", { name: "MCP" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not be loaded/i);
    expect(screen.queryByText("No MCP servers are configured.")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry MCP" }));

    expect(await screen.findByText("github")).toBeVisible();
    expect(listMcpServers).toHaveBeenCalledTimes(2);
  });

  it("requires a credential-free HTTPS URL before enabling a remote MCP server", async () => {
    const addMcpServer = vi.fn();
    const api = {
      ...settingsApi(),
      addMcpServer,
      listMcpServers: vi.fn().mockResolvedValue([])
    };
    const user = userEvent.setup();
    render(<SettingsPanel api={api} experience="developer" onExperienceChange={vi.fn()}
      storage={new MemoryStorage()} workspaceId="workspace-mcp-url" workspaceKind="individual" />);

    await user.click(screen.getByRole("button", { name: "MCP" }));
    await user.type(screen.getByLabelText("Name"), "remote-tools");
    await user.type(screen.getByLabelText("Server URL"), "http://example.test/mcp");

    expect(screen.getByRole("button", { name: "Add MCP server" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent(/https url without embedded credentials/i);

    await user.clear(screen.getByLabelText("Server URL"));
    await user.type(screen.getByLabelText("Server URL"), "https://user:secret@example.test/mcp");
    expect(screen.getByRole("button", { name: "Add MCP server" })).toBeDisabled();

    await user.clear(screen.getByLabelText("Server URL"));
    await user.type(screen.getByLabelText("Server URL"), "https://example.test/mcp");
    expect(screen.getByRole("button", { name: "Add MCP server" })).toBeEnabled();
  });

  it("applies explicit background, login, and notification settings to desktop", async () => {
    const storage = new MemoryStorage();
    const api = settingsApi();
    const applyDesktopSettings = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <SettingsPanel
        api={api}
        applyDesktopSettings={applyDesktopSettings}
        desktopAvailable
        experience="developer"
        onExperienceChange={vi.fn().mockResolvedValue(undefined)}
        storage={storage}
        workspaceId="workspace-desktop"
        workspaceKind="individual"
      />
    );

    await user.click(screen.getByLabelText("Run in background"));
    await user.click(screen.getByLabelText("Launch at login"));
    await user.click(screen.getByLabelText("Native notifications"));
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(applyDesktopSettings).toHaveBeenCalledWith({
      runInBackground: true,
      launchAtLogin: true,
      nativeNotifications: true
    }));
    expect(loadDevicePreferences(storage, "workspace-desktop")).toMatchObject({
      runInBackground: true,
      launchAtLogin: true,
      nativeNotifications: true
    });
    expect(api.updatePreferences).not.toHaveBeenCalled();
  });

  it("persists runtime guidance through the backend and appearance on this device", async () => {
    const storage = new MemoryStorage();
    const api = settingsApi();
    render(
      <SettingsPanel
        api={api}
        experience="everyday"
        onExperienceChange={vi.fn().mockResolvedValue(undefined)}
        storage={storage}
        workspaceId="workspace-1"
        workspaceKind="team"
      />
    );
    const user = userEvent.setup();

    expect(screen.getByText("Everyday / Team")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Agent" }));
    await user.selectOptions(screen.getByLabelText("Response style"), "concise");
    await user.type(screen.getByLabelText("Custom rules"), "Always show the next action.");
    await user.click(await screen.findByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(api.updatePreferences).toHaveBeenCalledWith(expect.objectContaining({
      expected_version: 2,
      response_tone: "concise",
      custom_rules: "Always show the next action."
    })));
    expect(screen.getByText("Saved for this local runtime")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Appearance" }));
    await user.selectOptions(screen.getByLabelText("Theme"), "dark");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(loadDevicePreferences(storage, "workspace-1").theme).toBe("dark");
  });

  it("directs connection management to the feature that uses it", async () => {
    const onGoogleSignIn = vi.fn().mockResolvedValue(undefined);
    const connectionsApi = {
      authenticateGitHub: vi.fn().mockResolvedValue({ authenticated: true, hostname: "github.com", login: "lucas" }),
      getGitHubAuthStatus: vi.fn().mockResolvedValue({ authenticated: false, hostname: "github.com" })
    };
    render(
      <SettingsPanel
        connectionsApi={connectionsApi}
        experience="developer"
        onGoogleSignIn={onGoogleSignIn}
        onExperienceChange={vi.fn().mockResolvedValue(undefined)}
        storage={new MemoryStorage()}
        workspaceId="workspace-2"
        workspaceKind="individual"
      />
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Account" }));
    expect(screen.getByText("Web / Preview")).toBeVisible();
    await user.click(screen.getByRole("button", { name: /Sign in with Google/i }));
    expect(onGoogleSignIn).toHaveBeenCalledOnce();
    await user.click(await screen.findByRole("button", { name: /Sign in with GitHub/i }));
    expect(connectionsApi.authenticateGitHub).toHaveBeenCalledOnce();
  });

  it("does not offer GitHub sign-in until the existing authorization check finishes", async () => {
    let resolveStatus: ((value: { authenticated: boolean; hostname: string }) => void) | undefined;
    const status = new Promise<{ authenticated: boolean; hostname: string }>((resolve) => {
      resolveStatus = resolve;
    });
    const connectionsApi = {
      authenticateGitHub: vi.fn(),
      getGitHubAuthStatus: vi.fn().mockReturnValue(status)
    };
    const user = userEvent.setup();
    render(<SettingsPanel connectionsApi={connectionsApi} experience="developer"
      onExperienceChange={vi.fn()} storage={new MemoryStorage()}
      workspaceId="workspace-github-loading" workspaceKind="individual" />);

    await user.click(screen.getByRole("button", { name: "Account" }));
    expect(screen.getByRole("button", { name: /Checking GitHub/i })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /Sign in with GitHub/i })).not.toBeInTheDocument();

    resolveStatus?.({ authenticated: false, hostname: "github.com" });
    expect(await screen.findByRole("button", { name: /Sign in with GitHub/i })).toBeEnabled();
  });

  it("disables API credential entry without a local credential runtime", async () => {
    render(
      <SettingsPanel
        experience="developer"
        onExperienceChange={vi.fn().mockResolvedValue(undefined)}
        storage={new MemoryStorage()}
        workspaceId="workspace-hosted"
        workspaceKind="individual"
      />
    );
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Models" }));
    await user.click(screen.getByRole("button", { name: "Providers" }));

    expect(screen.getByLabelText("OpenAI API key")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Connect OpenAI" })).toBeDisabled();
    expect(screen.getByText(/open Corvus desktop to manage API credentials/i)).toBeVisible();
  });

  it("uses the discovered model identifier as an editable starting value", async () => {
    const api = settingsApi();
    vi.mocked(api.getPreferences).mockResolvedValue({
      ...(await api.getPreferences()),
      default_model: null
    });
    render(
      <SettingsPanel
        api={api}
        experience="developer"
        onExperienceChange={vi.fn().mockResolvedValue(undefined)}
        storage={new MemoryStorage()}
        workspaceId="workspace-model"
        workspaceKind="individual"
      />
    );
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Models" }));
    await user.click(screen.getByRole("button", { name: "Providers" }));
    expect(await screen.findByRole("textbox", { name: "Codex gpt-5.6-sol display name" })).toHaveValue("GPT-5.6 Sol");
    expect(screen.getByText("Detected · Recommended")).toBeVisible();
  });

  it("does not present failed discovery as verified and allows retry", async () => {
    const api = settingsApi();
    vi.mocked(api.listProviders).mockRejectedValue(new Error("catalog unavailable"));
    render(
      <SettingsPanel
        api={api}
        experience="developer"
        onExperienceChange={vi.fn().mockResolvedValue(undefined)}
        storage={new MemoryStorage()}
        workspaceId="workspace-discovery"
        workspaceKind="individual"
      />
    );
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Models" }));
    await user.click(screen.getByRole("button", { name: "Providers" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/catalog unavailable/i);
    await user.click(screen.getByRole("button", { name: "Defaults" }));
    expect(screen.getByText(/thinking options unavailable until provider discovery succeeds/i)).toBeVisible();
    expect(screen.queryByRole("radio", { name: "Low" })).not.toBeInTheDocument();
    expect(screen.queryByText(/CLI and login verified/i)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry discovery" }));
    await waitFor(() => expect(api.listProviders).toHaveBeenCalledTimes(2));
  });

  it("explains the enforced local safety boundaries without offering a bypass", async () => {
    const storage = new MemoryStorage();
    render(
      <SettingsPanel
        api={settingsApi()}
        experience="developer"
        onExperienceChange={vi.fn().mockResolvedValue(undefined)}
        storage={storage}
        workspaceId="workspace-safety"
        workspaceKind="individual"
      />
    );
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Safety" }));
    expect(screen.getByText("Build confirmation")).toBeVisible();
    expect(screen.getByText("Always on in this alpha")).toBeVisible();
    expect(screen.getByText(/original project stays unchanged/i)).toBeVisible();
    expect(screen.queryByRole("button", { name: /full access/i })).not.toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: /Detailed safety guidance/i }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(loadDevicePreferences(storage, "workspace-safety").safetyGuidance).toBe("detailed");
  });

  it("persists the composer send-key behavior in General settings", async () => {
    const storage = new MemoryStorage();
    render(
      <SettingsPanel
        api={settingsApi()}
        experience="everyday"
        onExperienceChange={vi.fn().mockResolvedValue(undefined)}
        storage={storage}
        workspaceId="workspace-keys"
        workspaceKind="individual"
      />
    );
    const user = userEvent.setup();

    await user.click(screen.getByRole("radio", { name: /Ctrl\+Enter sends/i }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(loadDevicePreferences(storage, "workspace-keys").sendKeyMode).toBe("ctrl-enter");
  });

  it("loads the current saved preferences when another session wins a save", async () => {
    const api = settingsApi();
    vi.mocked(api.updatePreferences).mockRejectedValue(new ConversationApiError(
      409,
      "preferences_version_conflict",
      null,
      {
        code: "preferences_version_conflict",
        current: {
          version: 4,
          default_provider: "codex",
          default_model: "gpt-5.6-sol",
          default_effort: "low",
          default_mode: "chat",
          mcp_enabled: false,
          response_tone: "concise",
          custom_rules: "Current saved rule.",
          updated_at: "2026-07-18T00:00:00Z"
        }
      }
    ));
    render(<SettingsPanel api={api} experience="developer" onExperienceChange={vi.fn()}
      storage={new MemoryStorage()} workspaceId="workspace-conflict" workspaceKind="individual" />);
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Agent" }));
    await user.selectOptions(await screen.findByLabelText("Response style"), "detailed");
    await user.click(await screen.findByRole("button", { name: "Save changes" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/current saved values are loaded/i);
    expect(screen.getByLabelText("Response style")).toHaveValue("concise");
    expect(screen.getByLabelText("Custom rules")).toHaveValue("Current saved rule.");
  });

  it("connects, verifies, replaces, and removes write-only provider credentials", async () => {
    const api = {
      ...settingsApi(),
      listProviderCredentials: vi.fn().mockResolvedValue([
        { provider: "openai", configured: false, source: "none" }
      ]),
      connectProviderCredential: vi.fn().mockResolvedValue(
        { provider: "openai", configured: true, source: "keyring" }
      ),
      verifyProviderCredential: vi.fn().mockResolvedValue(
        { provider: "openai", configured: true, verified: true, models: ["gpt-5.6-sol"] }
      ),
      removeProviderCredential: vi.fn().mockResolvedValue(
        { provider: "openai", configured: false, source: "none" }
      )
    } as unknown as ConversationApi;
    render(
      <SettingsPanel
        api={api}
        experience="developer"
        onExperienceChange={vi.fn().mockResolvedValue(undefined)}
        storage={new MemoryStorage()}
        workspaceId="workspace-provider"
        workspaceKind="individual"
      />
    );
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Models" }));
    await user.click(screen.getByRole("button", { name: "Providers" }));
    const credential = screen.getByLabelText("OpenAI API key");
    await user.type(credential, "sk-test-never-render-again");
    await user.click(screen.getByRole("button", { name: "Connect OpenAI" }));
    expect(api.connectProviderCredential).toHaveBeenCalledWith("openai", "sk-test-never-render-again");
    expect(credential).toHaveValue("");
    expect(screen.queryByDisplayValue("sk-test-never-render-again")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Verify OpenAI" }));
    expect((await screen.findAllByText("gpt-5.6-sol")).length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "Remove OpenAI" }));
    expect(api.removeProviderCredential).toHaveBeenCalledWith("openai");
  });
});

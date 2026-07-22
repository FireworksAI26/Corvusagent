import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ConversationApi, RunEventListener, RunEventStream } from "./conversationApi";
import { assistantStreamBatchSize, ConversationWorkspace } from "./ConversationWorkspace";
import { MemoryStorage } from "../test/memoryStorage";

class FakeRunStream implements RunEventStream {
  private readonly listeners = new Map<string, RunEventListener[]>();
  private readonly terminalListeners: Array<() => void> = [];
  close = vi.fn();

  addEventListener(type: string, listener: RunEventListener): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  emit(type: string, data: unknown): void {
    for (const listener of this.listeners.get(type) ?? []) {
      listener({ data: JSON.stringify(data) });
    }
  }

  onTerminalError(listener: () => void): void {
    this.terminalListeners.push(listener);
  }

  failTerminal(): void {
    for (const listener of this.terminalListeners) listener();
  }
}

function conversationApi(stream: FakeRunStream): ConversationApi {
  const preview = (mode: "chat" | "build", mcpEnabled: boolean) => ({
    policy_digest: (mcpEnabled ? "c" : mode === "build" ? "b" : "a").repeat(64),
    level: mcpEnabled ? "elevated" as const : mode === "build" ? "protected" as const : "read_only" as const,
    label: mcpEnabled ? "External tools on" : mode === "build" ? "Protected build" : "Read-only",
    summary: mode === "build" ? "Work happens in a fresh writable sandbox." : "The agent can inspect context without writing files.",
    execution: "Codex CLI runs ephemerally.",
    filesystem: mode === "build" ? "The original project is not modified." : "The sandbox is read-only.",
    network: "Corvus grants no separate network permission.",
    mcp: mcpEnabled ? "Configured MCP tools may act on external systems." : "Configured MCP servers are not loaded.",
    approvals: "No blanket host approval is granted.",
    output: mode === "build" ? "A screened ZIP can be downloaded." : "No artifact is exported.",
    requires_confirmation: mode === "build"
  });
  return {
    getPreferences: vi.fn().mockResolvedValue({
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
    }),
    updatePreferences: vi.fn(),
    listProviders: vi.fn().mockResolvedValue([
      { id: "codex", label: "Codex", status: "ready", runtime: "local", models: [
        { id: "gpt-5.6-sol", label: "GPT-5.6 Sol", recommended: true },
        { id: "gpt-5.6-terra", label: "GPT-5.6 Terra", recommended: false },
        { id: "gpt-5.5", label: "GPT-5.5", recommended: false }
      ], status_label: "Detected on this device", thinking_levels: ["low", "medium", "high", "xhigh"], supports_mcp: true },
      { id: "claude", label: "Claude", status: "ready", runtime: "local", models: [
        { id: "sonnet", label: "Claude Sonnet", recommended: true },
        { id: "opus", label: "Claude Opus", recommended: false }
      ], status_label: "Detected on this device", thinking_levels: ["low", "medium", "high", "xhigh", "max"], supports_mcp: false },
      { id: "gemini", label: "Gemini", status: "preview", runtime: "local", models: [], status_label: "Preview", thinking_levels: [], supports_mcp: false },
      { id: "grok", label: "Grok", status: "preview", runtime: "api", models: [], status_label: "Preview", thinking_levels: [], supports_mcp: false }
    ]),
    getSafetyPreview: vi.fn((_provider, mode, mcpEnabled) => Promise.resolve(preview(mode, mcpEnabled))),
    getSafetyReceipt: vi.fn().mockResolvedValue({
      run_id: "run-1", status: "completed", safety: preview("build", true),
      activities: ["Files changed only inside the scratch workspace"], mcp_used: true,
      approval: "No blanket host approval was granted.", original_project_modified: false,
      artifact: { download_name: "corvus-project.zip", sha256_digest: "d".repeat(64), size_bytes: 42, secret_screening: "passed" }
    }),
    startRun: vi.fn().mockResolvedValue({ run_id: "run-1", handle_id: "handle-1", state: "running", provider: "codex", model: "gpt-5.6-sol", mode: "chat", storage: "this_device", created_at: "2026-07-17T02:00:02Z", safety: preview("chat", false) }),
    cancelRun: vi.fn().mockResolvedValue({ run_id: "run-1", state: "cancelled", accepted: true, reason_code: null }),
    openRunEvents: vi.fn().mockReturnValue(stream),
    artifactUrl: vi.fn((runId: string) => `/api/local-chat/runs/${runId}/artifact`),
    downloadArtifact: vi.fn().mockResolvedValue("C:\\Downloads\\corvus-project.zip")
  };
}

function unavailableConversationApi(stream: FakeRunStream): ConversationApi {
  return {
    ...conversationApi(stream),
    listProviders: vi.fn().mockRejectedValue(new Error("catalog unavailable"))
  };
}

function emptyConversationApi(stream: FakeRunStream): ConversationApi {
  return {
    ...conversationApi(stream),
    listProviders: vi.fn().mockResolvedValue([])
  };
}

async function chooseComposerOption(
  user: ReturnType<typeof userEvent.setup>,
  label: string,
  option: string
): Promise<void> {
  await user.click(screen.getByRole("combobox", { name: label }));
  await user.click(screen.getByRole("option", { name: option }));
}

describe("ConversationWorkspace", () => {
  it("adapts rendering batches so large responses catch up without skipping short-stream motion", () => {
    expect(assistantStreamBatchSize(1)).toBe(1);
    expect(assistantStreamBatchSize(120)).toBe(1);
    expect(assistantStreamBatchSize(1_200)).toBe(10);
  });

  it("uses the fallback provider model when the saved provider is unavailable", async () => {
    const api = conversationApi(new FakeRunStream());
    api.getPreferences = vi.fn().mockResolvedValue({
      version: 0, default_provider: "openai", default_model: "gpt-5.6-sol",
      default_effort: "medium", default_mode: "chat", mcp_enabled: false,
      response_tone: "balanced", custom_rules: "", updated_at: null
    });
    api.listProviders = vi.fn().mockResolvedValue([{
      id: "claude", label: "Claude", status: "ready", runtime: "local",
      models: [{ id: "sonnet", label: "Claude Sonnet", recommended: true }],
      status_label: "Detected on this device", thinking_levels: ["low", "medium", "high"],
      supports_mcp: false
    }]);
    const user = userEvent.setup();
    render(<ConversationWorkspace api={api} storage={new MemoryStorage()}
      storageScope="workspace-provider-fallback" experience="developer" />);

    await user.click(screen.getByRole("button", { name: "Run options" }));

    await waitFor(() => {
      expect(screen.getByRole("combobox", { name: "Agent provider" })).toHaveTextContent("Claude");
      expect(screen.getByRole("combobox", { name: "Agent model" })).toHaveTextContent("Claude Sonnet");
    });
  });

  it("uses the first verified model when the saved model is no longer available", async () => {
    const api = conversationApi(new FakeRunStream());
    api.getPreferences = vi.fn().mockResolvedValue({
      version: 0, default_provider: "codex", default_model: "retired-model",
      default_effort: "medium", default_mode: "chat", mcp_enabled: false,
      response_tone: "balanced", custom_rules: "", updated_at: null
    });
    api.listProviders = vi.fn().mockResolvedValue([{
      id: "codex", label: "Codex", status: "ready", runtime: "local",
      models: [{ id: "gpt-5.6-sol", label: "GPT-5.6 Sol", recommended: true }],
      status_label: "Detected on this device", thinking_levels: ["low", "medium", "high"],
      supports_mcp: true
    }]);
    const user = userEvent.setup();
    render(<ConversationWorkspace api={api} storage={new MemoryStorage()}
      storageScope="workspace-model-fallback" experience="developer" />);

    await user.click(screen.getByRole("button", { name: "Run options" }));

    await waitFor(() => {
      expect(screen.getByRole("combobox", { name: "Agent model" })).toHaveTextContent("GPT-5.6 Sol");
    });
  });

  it("keeps the composer focused and reveals advanced controls through Run options", async () => {
    const user = userEvent.setup();
    render(<ConversationWorkspace api={conversationApi(new FakeRunStream())} storage={new MemoryStorage()}
      storageScope="workspace-progressive-controls" experience="developer" />);

    expect(screen.getByRole("button", { name: "Run options" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("combobox", { name: "Agent provider" })).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Run mode" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Run options" }));

    expect(screen.getByRole("button", { name: "Run options" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("combobox", { name: "Agent provider" })).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Agent model" })).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Thinking level" })).toBeVisible();
  });

  it("uses saved display names and manually configured models in the chat selector", async () => {
    const api = conversationApi(new FakeRunStream());
    api.getPreferences = vi.fn().mockResolvedValue({
      version: 3, default_provider: "codex", default_model: "custom-codex-model",
      model_labels: {
        "codex:gpt-5.6-sol": "Fast Sol",
        "codex:custom-codex-model": "My custom model"
      },
      default_effort: "medium", default_mode: "chat", mcp_enabled: false,
      response_tone: "balanced", custom_rules: "", updated_at: "2026-07-21T00:00:00Z"
    });
    const user = userEvent.setup();
    render(<ConversationWorkspace api={api} storage={new MemoryStorage()}
      storageScope="workspace-model-labels" experience="developer" />);

    await user.click(screen.getByRole("button", { name: "Run options" }));
    const model = await screen.findByRole("combobox", { name: "Agent model" });
    expect(model).toHaveTextContent("My custom model");
    await user.click(model);
    expect(screen.getByRole("option", { name: "Fast Sol" })).toBeVisible();
    expect(screen.getByRole("option", { name: "My custom model" })).toBeVisible();
  });

  it("expands and contracts the composer between two and eight lines", () => {
    render(<ConversationWorkspace api={conversationApi(new FakeRunStream())} storage={new MemoryStorage()}
      storageScope="workspace-growing-composer" experience="developer" />);
    const composer = screen.getByRole("textbox", { name: "Message Corvus" });
    let scrollHeight = 120;
    Object.defineProperty(composer, "scrollHeight", { configurable: true, get: () => scrollHeight });

    fireEvent.input(composer, { target: { value: "one\ntwo\nthree\nfour" } });
    expect(composer).toHaveStyle({ height: "120px", overflowY: "hidden" });

    scrollHeight = 300;
    fireEvent.input(composer, { target: { value: "one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine" } });
    expect(composer).toHaveStyle({ height: "198px", overflowY: "auto" });

    scrollHeight = 60;
    fireEvent.input(composer, { target: { value: "short again" } });
    expect(composer).toHaveStyle({ height: "60px", overflowY: "hidden" });
  });

  it("keeps Run options usable after a run fails to start", async () => {
    const api = conversationApi(new FakeRunStream());
    vi.mocked(api.startRun).mockRejectedValue(new Error("Codex sandbox preflight failed."));
    const user = userEvent.setup();
    render(<ConversationWorkspace api={api} storage={new MemoryStorage()}
      storageScope="workspace-failed-run-options" experience="developer" />);

    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Inspect this repository");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Codex sandbox preflight failed.");

    await user.click(screen.getByRole("button", { name: "Run options" }));
    expect(screen.getByRole("button", { name: "Run options" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("region", { name: "Run options panel" })).toBeVisible();
  });

  it("keeps Run options editable while a run is working", async () => {
    const stream = new FakeRunStream();
    const user = userEvent.setup();
    render(<ConversationWorkspace api={conversationApi(stream)} storage={new MemoryStorage()}
      storageScope="workspace-working-run-options" experience="developer" />);

    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Inspect this repository");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await user.click(screen.getByRole("button", { name: "Run options" }));

    expect(screen.getByRole("button", { name: "Run options" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("combobox", { name: "Agent provider" })).toBeEnabled();
    expect(screen.getByRole("combobox", { name: "Agent model" })).toBeEnabled();
    expect(screen.getByText("Changes apply to your next message.")).toBeVisible();
  });

  it("shows a completed confirmation pause as Needs input instead of Failed", async () => {
    const stream = new FakeRunStream();
    const user = userEvent.setup();
    render(<ConversationWorkspace api={conversationApi(stream)} storage={new MemoryStorage()}
      storageScope="workspace-needs-input" experience="developer" />);

    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Prepare a change");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    stream.emit("needs_input", { payload: { status: "needs_input" } });
    stream.emit("completed", { payload: { status: "needs_input" } });

    expect(await screen.findByText("Needs input")).toBeVisible();
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
    expect(screen.getByText(/waiting for your confirmation/i)).toBeVisible();
  });

  it("closes an open composer menu when keyboard focus leaves it", async () => {
    const user = userEvent.setup();
    render(<ConversationWorkspace api={conversationApi(new FakeRunStream())} storage={new MemoryStorage()}
      storageScope="workspace-menu-focus" experience="developer" />);
    await user.click(screen.getByRole("button", { name: "Run options" }));
    const provider = screen.getByRole("combobox", { name: "Agent provider" });

    await user.click(provider);
    expect(provider).toHaveAttribute("aria-expanded", "true");
    await user.tab();

    expect(provider).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("combobox", { name: "Agent model" })).toHaveFocus();
  });

  it("keeps a verified provider usable when only saved preferences fail to load", async () => {
    const stream = new FakeRunStream();
    const client = conversationApi(stream);
    vi.mocked(client.getPreferences).mockRejectedValue(new Error("preferences unavailable"));
    const user = userEvent.setup();
    render(<ConversationWorkspace api={client} storage={new MemoryStorage()}
      storageScope="workspace-preference-failure" experience="developer" />);

    await user.type(screen.getByLabelText("Message Corvus"), "Inspect this repository");
    await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled());
    expect(screen.queryByText(/could not verify local providers/i)).not.toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Preference recovery" })).toHaveTextContent(
      /using verified provider defaults/i
    );
  });

  it("uses application-styled accessible menus for composer choices", async () => {
    const user = userEvent.setup();
    render(<ConversationWorkspace api={conversationApi(new FakeRunStream())} storage={new MemoryStorage()}
      storageScope="workspace-controls" experience="developer" />);

    await user.click(screen.getByRole("button", { name: "Run options" }));
    const thinkingControl = screen.getByRole("combobox", { name: "Thinking level" });
    expect(thinkingControl).toHaveClass("composer-select__trigger");
    await user.click(thinkingControl);
    expect(screen.getByRole("listbox", { name: "Thinking level" })).toBeVisible();
    expect(screen.getByRole("option", { name: "Extra high" })).toHaveAttribute("aria-selected", "false");
    expect(thinkingControl).toHaveAttribute("aria-activedescendant");
    await user.keyboard("{ArrowDown}");
    expect(thinkingControl).toHaveTextContent("Medium");
    await user.keyboard("{Enter}");
    expect(thinkingControl).toHaveTextContent("High");
    expect(screen.queryByRole("listbox", { name: "Thinking level" })).not.toBeInTheDocument();
  });

  it("sends prior thread messages as bounded context when switching from Chat to Build", async () => {
    const stream = new FakeRunStream();
    const api = conversationApi(stream);
    const user = userEvent.setup();
    render(<ConversationWorkspace api={api} storage={new MemoryStorage()}
      storageScope="workspace-context" experience="developer" />);

    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Build me a website");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    stream.emit("message", { payload: { text: "I can help shape the website." } });
    stream.emit("completed", { payload: {} });
    await screen.findByText("I can help shape the website.", {}, { timeout: 3_000 });

    await chooseComposerOption(user, "Run mode", "Build");
    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Continue");
    await user.click(screen.getByRole("button", { name: "Build project" }));
    await user.click(screen.getByRole("button", { name: "Continue in sandbox" }));

    await waitFor(() => expect(api.startRun).toHaveBeenLastCalledWith(
      "Continue",
      expect.objectContaining({
        mode: "build",
        context: [
          { role: "user", content: "Build me a website" },
          { role: "assistant", content: "I can help shape the website." }
        ]
      }),
      expect.any(String)
    ));
  });

  it("keeps distinct provider message items as separate paragraphs", async () => {
    const stream = new FakeRunStream();
    const user = userEvent.setup();
    render(<ConversationWorkspace api={conversationApi(stream)} storage={new MemoryStorage()}
      storageScope="workspace-paragraphs" experience="developer" />);

    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Explain the result");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    stream.emit("message", { payload: { text: "The implementation is complete." } });
    stream.emit("message", { payload: { text: "The checks also pass." } });

    const first = await screen.findByText("The implementation is complete.");
    const second = await screen.findByText("The checks also pass.");
    expect(first.tagName).toBe("P");
    expect(second.tagName).toBe("P");
  });

  it("joins API provider token deltas without inserting paragraph breaks", async () => {
    const stream = new FakeRunStream();
    const api = conversationApi(stream);
    api.getPreferences = vi.fn().mockResolvedValue({
      version: 0, default_provider: "openai", default_model: "gpt-5.6-sol",
      default_effort: "medium", default_mode: "chat", mcp_enabled: false,
      response_tone: "balanced", custom_rules: "", updated_at: null
    });
    api.listProviders = vi.fn().mockResolvedValue([{
      id: "openai", label: "OpenAI", status: "ready", runtime: "api",
      models: [{ id: "gpt-5.6-sol", label: "GPT-5.6 Sol", recommended: true }],
      status_label: "API key verified", thinking_levels: ["low", "medium", "high", "xhigh"],
      supports_mcp: false
    }]);
    const user = userEvent.setup();
    render(<ConversationWorkspace api={api} storage={new MemoryStorage()}
      storageScope="workspace-api-deltas" experience="developer" />);

    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Say hello");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    stream.emit("message", { payload: { text: "Hel" } });
    stream.emit("message", { payload: { text: "lo" } });
    stream.emit("message", { payload: { text: " now" } });

    const result = await screen.findByText("Hello now");
    expect(result.tagName).toBe("P");
  });

  it("shows truthful token usage supplied by the verified provider", async () => {
    const stream = new FakeRunStream();
    const user = userEvent.setup();
    render(<ConversationWorkspace api={conversationApi(stream)} storage={new MemoryStorage()}
      storageScope="workspace-usage" experience="developer" />);

    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Summarize this");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    stream.emit("usage", { payload: { input_tokens: 1234, cached_input_tokens: 200, output_tokens: 87 } });

    expect(await screen.findByRole("status", { name: "Model usage" })).toHaveTextContent(
      "Model usage · 1,234 input · 200 cached · 87 output"
    );
  });

  it("locks thread and project switching while a run owns the active conversation", async () => {
    const stream = new FakeRunStream();
    const user = userEvent.setup();
    render(<ConversationWorkspace api={conversationApi(stream)} storage={new MemoryStorage()}
      storageScope="workspace-active-thread" experience="developer" />);

    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Hold this context");
    await user.click(screen.getByRole("button", { name: "Send message" }));

    expect(screen.getByRole("button", { name: "New thread" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Project · Agent directory" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Conversation history" }));
    expect(screen.getByRole("button", { name: /Hold this context/ })).toBeDisabled();
  });

  it("shows sanitized tool activity while a Build run is working", async () => {
    const stream = new FakeRunStream();
    const user = userEvent.setup();
    render(<ConversationWorkspace api={conversationApi(stream)} storage={new MemoryStorage()}
      storageScope="workspace-tools" experience="developer" />);

    await chooseComposerOption(user, "Run mode", "Build");
    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Run the checks");
    await user.click(screen.getByRole("button", { name: "Build project" }));
    await user.click(screen.getByRole("button", { name: "Continue in sandbox" }));
    stream.emit("status", {
      payload: { activity: "command", tool_id: "tool-1", label: "Run Python unit tests", status: "started" }
    });
    stream.emit("status", {
      payload: { activity: "command", tool_id: "tool-1", label: "Run a sandboxed command", status: "completed" }
    });

    expect(await screen.findByRole("region", { name: "Tool activity" })).toHaveTextContent("Run Python unit tests");
    expect(screen.getByRole("region", { name: "Tool activity" })).toHaveTextContent("Completed");
  });

  it("binds a registered project to a new thread and sends its repository id", async () => {
    const stream = new FakeRunStream();
    const api = conversationApi(stream);
    api.listRepositories = vi.fn().mockResolvedValue([{
      id: "repo-1",
      display_name: "Corvus",
      path: "C:\\work\\corvus"
    }]);
    const user = userEvent.setup();
    render(<ConversationWorkspace api={api} storage={new MemoryStorage()}
      storageScope="workspace-project" experience="developer" />);

    await user.click(await screen.findByRole("button", { name: "Project · Agent directory" }));
    await user.click(screen.getByRole("button", { name: /Corvus.*C:\\work\\corvus/ }));
    expect(screen.getByRole("button", { name: "Project · Corvus" })).toBeVisible();
    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Inspect this project");
    await user.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => expect(api.startRun).toHaveBeenCalledWith(
      "Inspect this project",
      expect.objectContaining({ repository_id: "repo-1" }),
      expect.any(String)
    ));
  });

  it("preserves streamed text and offers a safe recovery when read-only work is blocked", async () => {
    const stream = new FakeRunStream();
    const api = conversationApi(stream);
    render(<ConversationWorkspace api={api} storage={new MemoryStorage()}
      storageScope="workspace-blocked" experience="developer" workingDirectory={"C:\\work\\corvus"} />);
    const user = userEvent.setup();

    expect(screen.getByText("C:\\work\\corvus")).toBeVisible();
    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Clone the repository");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByText("Working");
    stream.emit("message", { payload: { text: "I found the repository and checked its URL." } });
    stream.emit("failed", { payload: { reason_code: "process_frame_duplicate_key" } });

    expect(await screen.findByText("I found the repository and checked its URL.")).toBeVisible();
    expect(screen.getByRole("region", { name: "Run paused by safety settings" })).toHaveTextContent(
      /read-only mode prevented this run from making the requested change/i
    );
    expect(screen.queryByText(/process frame duplicate key/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Switch to Build" }));
    expect(screen.getByRole("combobox", { name: "Run mode" })).toHaveTextContent("Build");
    expect(screen.getByRole("textbox", { name: "Message Corvus" })).toHaveValue("Clone the repository");
  });

  it("ignores null streaming payloads without breaking the active run", async () => {
    const stream = new FakeRunStream();
    render(<ConversationWorkspace api={conversationApi(stream)} experience="developer"
      storage={new MemoryStorage()} storageScope="workspace-null-event" />);
    const user = userEvent.setup();

    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Inspect safely");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByText("Working");
    stream.emit("message", null);
    stream.emit("thinking", null);
    stream.emit("status", null);
    stream.emit("message", { payload: { text: "Still connected." } });
    stream.emit("completed", { payload: {} });

    expect(await screen.findByText("Still connected.")).toBeVisible();
    expect(screen.queryByText(/unreadable run event/i)).not.toBeInTheDocument();
  });

  it("creates a thread, runs Local Codex, and renders durable output", async () => {
    const stream = new FakeRunStream();
    const api = conversationApi(stream);
    const storage = new MemoryStorage();
    const view = render(<ConversationWorkspace api={api} storage={storage} storageScope="device" experience="everyday" />);
    const user = userEvent.setup();

    expect(screen.queryByLabelText("Run status: idle")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Conversation history" }));
    expect(screen.getByText("No conversations yet")).toBeVisible();
    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Draft release notes");
    await user.click(screen.getByRole("button", { name: "Send message" }));

    expect(api.startRun).toHaveBeenCalledWith("Draft release notes", {
      provider: "codex", model: "gpt-5.6-sol", effort: "medium", mode: "chat", mcp_enabled: false,
      safety_digest: "a".repeat(64)
    }, expect.any(String));
    expect(screen.getByLabelText("Run status: working")).toBeVisible();
    expect(await screen.findByText("Working")).toBeVisible();

    stream.emit("message", { type: "message", payload: { text: "Release ready." } });
    stream.emit("completed", { type: "completed", payload: {} });

    expect(await screen.findByText("Release ready.")).toBeVisible();
    expect(await screen.findByText("Completed")).toBeVisible();
    expect(screen.queryByText("Starting the model")).not.toBeInTheDocument();
    view.unmount();
    render(<ConversationWorkspace api={api} storage={storage} storageScope="device" experience="everyday" />);
    expect(screen.getByText("Release ready.")).toBeVisible();
  });

  it("keeps history on demand and exposes provider, model, thinking, and Build controls", async () => {
    const stream = new FakeRunStream();
    const api = conversationApi(stream);
    render(<ConversationWorkspace api={api} storage={new MemoryStorage()} storageScope="device" experience="developer" />);
    const user = userEvent.setup();

    expect(screen.queryByRole("complementary", { name: "thread list" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Run options" }));
    await user.click(await screen.findByRole("combobox", { name: "Agent provider" }));
    expect(screen.getByRole("option", { name: "Claude" })).toBeEnabled();
    expect(screen.queryByRole("option", { name: /Gemini|Grok|Unavailable|Preview/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Codex default/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: "Claude" }));
    await user.click(screen.getByRole("combobox", { name: "Agent model" }));
    expect(screen.getByRole("option", { name: "Claude Sonnet" })).toBeVisible();
    await user.click(screen.getByRole("option", { name: "Claude Opus" }));
    await user.click(screen.getByRole("combobox", { name: "Thinking level" }));
    expect(screen.getByRole("option", { name: "Max" })).toHaveAttribute("aria-selected", "false");
    await user.click(screen.getByRole("option", { name: "Max" }));
    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Review this change");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(api.startRun).toHaveBeenCalledWith("Review this change", {
      provider: "claude", model: "opus", effort: "max", mode: "chat", mcp_enabled: false,
      safety_digest: "a".repeat(64)
    }, expect.any(String));
    stream.emit("completed", { type: "completed", payload: {} });
    expect(await screen.findByText("Completed")).toBeVisible();
    await chooseComposerOption(user, "Agent provider", "Codex");
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Agent provider" })).toHaveTextContent("Codex"));
    await chooseComposerOption(user, "Run mode", "Build");
    expect(screen.getByRole("checkbox", { name: "Allow configured MCP servers" })).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Thinking level" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Conversation history" }));
    expect(screen.getAllByText("Review this change").length).toBeGreaterThan(0);
  });

  it("streams safe thinking and work status, then offers the finished project", async () => {
    const stream = new FakeRunStream();
    const api = conversationApi(stream);
    render(<ConversationWorkspace api={api} storage={new MemoryStorage()} storageScope="device" experience="developer" />);
    const user = userEvent.setup();

    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Build a landing page");
    await chooseComposerOption(user, "Run mode", "Build");
    await user.click(screen.getByRole("button", { name: "Run options" }));
    await user.click(screen.getByRole("checkbox", { name: "Allow configured MCP servers" }));
    expect(screen.getByText(/may access external systems/i)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Build project" }));
    expect(api.startRun).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Confirm protected build" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(screen.getByRole("button", { name: "Continue in sandbox" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Confirm protected build" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Build project" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Build project" }));
    expect(screen.getByText(/original project is not modified/i)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Continue in sandbox" }));
    expect(api.startRun).toHaveBeenCalledWith("Build a landing page", {
      provider: "codex", model: "gpt-5.6-sol", effort: "medium", mode: "build", mcp_enabled: true,
      safety_digest: "c".repeat(64)
    }, expect.any(String));
    stream.emit("thinking", { type: "thinking", payload: { text: "Checking the project structure" } });
    expect(await screen.findByText("Checking the project structure")).toBeVisible();
    stream.emit("status", { type: "status", payload: { activity: "files" } });
    expect((await screen.findAllByText("Updating files"))[0]).toBeVisible();
    stream.emit("artifact", { type: "artifact", payload: { download_name: "corvus-project.zip" } });
    stream.emit("completed", { type: "completed", payload: {} });
    await user.click(await screen.findByRole("button", { name: "Download finished project" }));
    expect(api.downloadArtifact).toHaveBeenCalledWith("run-1", "corvus-project.zip");
    expect(await screen.findByText(/finished project saved to C:\\Downloads/i)).toBeVisible();
    const safetyReceipt = await screen.findByRole("region", { name: "Safety receipt" });
    expect(safetyReceipt).toHaveTextContent(/screening passed/i);
    await user.click(screen.getByText("Verification details"));
    expect(safetyReceipt).toHaveTextContent(/Codex CLI runs ephemerally/i);
    expect(safetyReceipt).toHaveTextContent(`Run IDrun-1`);
    expect(safetyReceipt).toHaveTextContent("d".repeat(64));
  });

  it("shows the server-authored protection summary in the composer", async () => {
    const api = conversationApi(new FakeRunStream());
    render(<ConversationWorkspace api={api} storage={new MemoryStorage()} storageScope="device" experience="developer" />);
    const user = userEvent.setup();

    expect(await screen.findByRole("button", { name: "View safety details" })).toHaveTextContent("Read-only");
    expect(screen.getByRole("button", { name: "View safety details" })).toHaveAttribute(
      "title",
      "Click to see details"
    );
    await user.click(screen.getByRole("button", { name: "View safety details" }));
    expect(screen.getByRole("region", { name: "Safety details" })).toHaveTextContent(/no separate network permission/i);
  });

  it("sends a single line with Enter and a multiline draft with Control+Enter", async () => {
    const stream = new FakeRunStream();
    const api = conversationApi(stream);
    render(<ConversationWorkspace api={api} storage={new MemoryStorage()} storageScope="device" experience="everyday" />);
    const user = userEvent.setup();
    const composer = screen.getByRole("textbox", { name: "Message Corvus" });

    await user.type(composer, "Send this{enter}");
    await waitFor(() => expect(api.startRun).toHaveBeenCalledTimes(1));
    stream.emit("completed", { type: "completed", payload: {} });
    await screen.findByText("Completed");

    await user.type(composer, "First line{shift>}{enter}{/shift}Second line{enter}");
    expect(api.startRun).toHaveBeenCalledTimes(1);
    expect(composer).toHaveValue("First line\nSecond line\n");
    await user.type(composer, "{control>}{enter}{/control}");
    await waitFor(() => expect(api.startRun).toHaveBeenCalledTimes(2));
    expect(api.startRun).toHaveBeenLastCalledWith(
      "First line\nSecond line",
      expect.any(Object),
      expect.any(String)
    );
  });

  it("renders streamed and durable messages as sanitized GitHub-flavored Markdown", async () => {
    const stream = new FakeRunStream();
    const api = conversationApi(stream);
    render(<ConversationWorkspace api={api} storage={new MemoryStorage()} storageScope="device" experience="developer" />);
    const user = userEvent.setup();

    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Use **safe** markdown");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    stream.emit("message", {
      type: "message",
      payload: { text: "**Done**\n\n- checked\n\n- [x] verified\n- [ ] pending\n\n[Docs](https://example.com)\n\n<script>alert(1)</script>" }
    });

    expect(await screen.findByText("Done")).toHaveStyle({ fontWeight: "bold" });
    expect((await screen.findByText("checked")).closest("ul")).toBeVisible();
    await waitFor(() => expect(screen.getAllByRole("checkbox")).toHaveLength(2));
    const checklist = screen.getAllByRole("checkbox");
    expect(checklist).toHaveLength(2);
    expect(checklist[0]).toBeChecked();
    expect(checklist[1]).not.toBeChecked();
    expect(await screen.findByRole("link", { name: "Docs" })).toHaveAttribute("href", "https://example.com");
    expect(document.querySelector("script")).toBeNull();
  });

  it("loads the safety receipt when a run is cancelled", async () => {
    const stream = new FakeRunStream();
    const api = conversationApi(stream);
    render(<ConversationWorkspace api={api} storage={new MemoryStorage()} storageScope="device" experience="developer" />);
    const user = userEvent.setup();

    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Inspect this repository");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    stream.emit("cancelled", { type: "cancelled", payload: {} });

    await waitFor(() => expect(api.getSafetyReceipt).toHaveBeenCalledWith("run-1"));
    expect(await screen.findByRole("region", { name: "Safety receipt" })).toBeVisible();
  });

  it("fails closed when provider discovery is unavailable and allows retry", async () => {
    const stream = new FakeRunStream();
    const api = unavailableConversationApi(stream);
    render(<ConversationWorkspace api={api} storage={new MemoryStorage()} storageScope="device" experience="everyday" />);
    const user = userEvent.setup();

    expect(await screen.findByRole("alert")).toHaveTextContent(/could not verify local providers/i);
    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Do not send this");
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
    expect(api.startRun).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Retry providers" }));
    expect(api.listProviders).toHaveBeenCalledTimes(2);
  });

  it("explains how to install a provider when discovery returns an empty catalog", async () => {
    const stream = new FakeRunStream();
    const api = emptyConversationApi(stream);
    render(<ConversationWorkspace api={api} storage={new MemoryStorage()} storageScope="device" experience="everyday" />);
    const user = userEvent.setup();

    expect(await screen.findByRole("alert")).toHaveTextContent(/install Codex CLI or Claude Code/i);
    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Do not send this");
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
    expect(api.startRun).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Retry providers" }));
    expect(api.listProviders).toHaveBeenCalledTimes(2);
  });

  it("fails closed when a verified provider returns no runnable model capabilities", async () => {
    const stream = new FakeRunStream();
    const api = conversationApi(stream);
    vi.mocked(api.listProviders).mockResolvedValue([{
      id: "codex",
      label: "Codex",
      status: "ready",
      runtime: "local",
      models: [],
      status_label: "CLI and login verified",
      thinking_levels: [],
      supports_mcp: true
    }]);
    render(<ConversationWorkspace api={api} storage={new MemoryStorage()} storageScope="device" experience="everyday" />);
    const user = userEvent.setup();

    expect(await screen.findByRole("alert")).toHaveTextContent(/returned no supported models or thinking levels/i);
    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Do not send this");
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
    expect(api.startRun).not.toHaveBeenCalled();
  });

  it("cancels the active run without treating a closed stream as success", async () => {
    const stream = new FakeRunStream();
    const api = conversationApi(stream);
    render(<ConversationWorkspace api={api} storage={new MemoryStorage()} storageScope="device" experience="developer" />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "New thread" }));
    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Inspect this repository");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await user.click(await screen.findByRole("button", { name: "Stop" }));

    await waitFor(() => expect(api.cancelRun).toHaveBeenCalledWith("run-1"));
    await waitFor(() => expect(api.getSafetyReceipt).toHaveBeenCalledWith("run-1"));
    expect(screen.getByText("Cancelled")).toBeVisible();
    expect(stream.close).toHaveBeenCalled();
  });

  it("keeps streaming when the backend declines cancellation", async () => {
    const stream = new FakeRunStream();
    const api = conversationApi(stream);
    vi.mocked(api.cancelRun).mockResolvedValue({
      run_id: "run-1",
      state: "running",
      accepted: false,
      reason_code: "provider_declined"
    });
    render(<ConversationWorkspace api={api} storage={new MemoryStorage()} storageScope="device" experience="developer" />);
    const user = userEvent.setup();

    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Inspect this repository");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await user.click(await screen.findByRole("button", { name: "Stop" }));

    await waitFor(() => expect(api.cancelRun).toHaveBeenCalledWith("run-1"));
    expect(screen.getByText("Working")).toBeVisible();
    expect(stream.close).not.toHaveBeenCalled();
    expect(api.getSafetyReceipt).not.toHaveBeenCalled();
  });

  it("leaves working state when the run event stream closes permanently", async () => {
    const stream = new FakeRunStream();
    const api = conversationApi(stream);
    render(<ConversationWorkspace api={api} storage={new MemoryStorage()} storageScope="device" experience="developer" />);
    const user = userEvent.setup();

    await user.type(screen.getByRole("textbox", { name: "Message Corvus" }), "Inspect this repository");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByText("Working")).toBeVisible();

    stream.failTerminal();

    expect(await screen.findByText("Failed")).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent(/connection to this run ended/i);
    expect(screen.getByRole("alert")).toHaveTextContent(/start the run again/i);
    expect(stream.close).toHaveBeenCalled();
  });
});

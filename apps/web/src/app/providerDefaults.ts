import type { ProviderCatalogEntry } from "./conversationApi";

export const FALLBACK_PROVIDERS: ProviderCatalogEntry[] = [
  {
    id: "codex",
    label: "Codex",
    status: "unavailable",
    runtime: "local",
    status_label: "Discovery required",
    thinking_levels: [],
    supports_mcp: true,
    models: []
  },
  {
    id: "claude",
    label: "Claude",
    status: "unavailable",
    runtime: "local",
    status_label: "Discovery required",
    thinking_levels: [],
    supports_mcp: false,
    models: []
  }
];

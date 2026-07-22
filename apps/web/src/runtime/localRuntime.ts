const LOOPBACK_WORKSPACE_URL = "http://127.0.0.1:8080/";
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1", "[::1]"]);

export function isLoopbackRuntimeHost(hostname: unknown): boolean {
  return typeof hostname === "string" && LOOPBACK_HOSTS.has(hostname.toLowerCase());
}

export function localWorkspaceUrl(): string {
  return LOOPBACK_WORKSPACE_URL;
}

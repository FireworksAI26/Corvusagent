import { invoke, isTauri } from "@tauri-apps/api/core";

export const HOSTED_GOOGLE_SIGN_IN_URL =
  "https://corvus-platform-tau.vercel.app/api/v2/auth/google/start";

export async function openHostedGoogleSignIn(
  openWindow: (url: string) => Window | null = (url) => window.open(url, "_blank", "noopener,noreferrer")
): Promise<void> {
  if (isTauri()) {
    await invoke("open_external_url", { url: HOSTED_GOOGLE_SIGN_IN_URL });
    return;
  }
  if (openWindow(HOSTED_GOOGLE_SIGN_IN_URL) === null) {
    throw new Error("google_sign_in_popup_blocked");
  }
}

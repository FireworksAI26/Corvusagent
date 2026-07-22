import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "@fontsource-variable/fraunces";
import "@fontsource-variable/inter-tight";
import "@fontsource/ibm-plex-mono/400.css";
import "./styles.css";
import "./styles/onboarding.css";
import "./styles/adaptive-shell.css";
import "./styles/product-workspace.css";
import { createDesktopPreferenceStorage } from "./app/desktopPreferences";
import { PlatformApp } from "./PlatformApp";

async function renderCorvus(): Promise<void> {
  const preferenceStorage = await createDesktopPreferenceStorage(window.localStorage);
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <PlatformApp preferenceStorage={preferenceStorage} />
    </StrictMode>
  );
}

void renderCorvus();

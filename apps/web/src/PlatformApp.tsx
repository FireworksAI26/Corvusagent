import type { CorvusApi } from "./api";
import { App } from "./App";
import { AuthProvider } from "./auth/AuthProvider";
import { createAuthApi, type PlatformApi } from "./auth/authApi";
import { isLoopbackRuntimeHost } from "./runtime/localRuntime";
import { SyncProvider } from "./sync/SyncProvider";

const browserHostedApi = createAuthApi();

interface PlatformAppProps {
  hostedApi?: PlatformApi;
  locationHostname?: string;
  loopbackApi?: CorvusApi;
  preferenceStorage?: Storage;
}

export function PlatformApp({
  hostedApi = browserHostedApi,
  locationHostname,
  loopbackApi,
  preferenceStorage
}: PlatformAppProps) {
  const hostname = locationHostname ?? window.location.hostname;
  if (isLoopbackRuntimeHost(hostname)) {
    return (
      <App
        api={loopbackApi}
        platformApi={hostedApi}
        locationHostname={hostname}
        preferenceStorage={preferenceStorage}
      />
    );
  }
  return (
    <AuthProvider api={hostedApi}>
      <SyncProvider api={hostedApi}>
        <App
          api={loopbackApi}
          platformApi={hostedApi}
          locationHostname={hostname}
          preferenceStorage={preferenceStorage}
        />
      </SyncProvider>
    </AuthProvider>
  );
}

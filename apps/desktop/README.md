# Corvus Desktop (Hackathon MVP)

The Tauri v2 shell supervises the authoritative Python sidecar and loads the same compiled React client that the self-host server exposes. It does not contain a second workflow or authorization implementation.

## Local Windows build and run

Build the Python environment and web client from the repository root, then install the pinned Tauri CLI:

```powershell
uv sync --all-groups --locked
pnpm --dir apps/web install --frozen-lockfile
pnpm --dir apps/web build
pnpm --dir apps/desktop install --frozen-lockfile
```

Run these commands from a Visual Studio Developer PowerShell with the Desktop C++ workload available:

```powershell
$env:PATH = "C:\Users\lucas\.cargo\bin;$env:PATH"
$env:CORVUS_SIDECAR_EXECUTABLE = (Resolve-Path .venv\Scripts\corvus-mvp.exe).Path
pnpm --dir apps/desktop tauri build --no-bundle
& apps\desktop\src-tauri\target\release\corvus-desktop.exe
```

The shell allocates a loopback port, creates ephemeral pairing/session secrets, starts `corvus-mvp desktop-sidecar` with fixed arguments, waits for `/ready`, and opens the real web client. The pairing secret is passed in a URL fragment, removed before API traffic, and never sent in an HTTP request. Closing the window sends `shutdown` over the sidecar's stdin, waits for graceful exit, and kills only after a bounded timeout.

Build the unsigned current-user NSIS package with:

```powershell
$pyinstallerArgs = @("--clean", "--noconfirm", "--onefile", "--name", "corvus-mvp", "scripts/corvus_mvp_entry.py")
uv run --python 3.12 --with pyinstaller==6.21.0 pyinstaller @pyinstallerArgs
New-Item -ItemType Directory -Force -Path apps/desktop/src-tauri/binaries | Out-Null
Copy-Item dist/corvus-mvp.exe apps/desktop/src-tauri/binaries/corvus-mvp-x86_64-pc-windows-msvc.exe
Copy-Item dist/corvus-mvp.exe apps/desktop/src-tauri/binaries/corvus-mvp.exe
Copy-Item dist/corvus-mvp.exe apps/desktop/src-tauri/binaries/corvus-mvp
pnpm --dir apps/desktop tauri build --bundles nsis --config src-tauri/tauri.release.conf.json
```

The local installer is an unsigned beta artifact. It bundles the standalone `corvus-mvp` sidecar and no longer requires `CORVUS_SIDECAR_EXECUTABLE` for packaged runs. Production signing, notarization, and update-channel signing remain later work.

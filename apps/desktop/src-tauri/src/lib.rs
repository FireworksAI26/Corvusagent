use hmac::{Hmac, Mac};
use rand::RngCore;
use sha2::Sha256;
use std::env;
use std::fs;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use tauri::{
    Manager, WebviewUrl, WebviewWindowBuilder,
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
};
use tauri_plugin_dialog::{DialogExt, FilePath};

const LOOPBACK_HOST: &str = "127.0.0.1";
const READY_TIMEOUT: Duration = Duration::from_secs(20);
const DESKTOP_PREFERENCES_FILE: &str = "desktop-preferences.json";
const MAX_DESKTOP_PREFERENCES_BYTES: usize = 1_048_576;
const MAX_ARTIFACT_BYTES: usize = 256 * 1_048_576;
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(5);
const HEALTH_INTERVAL: Duration = Duration::from_millis(500);
const MAX_FAILED_HEALTH_CHECKS: u8 = 6;
const MAX_DIAGNOSTIC_BYTES: usize = 16 * 1024;
const INSTANCE_CHALLENGE_BYTES: usize = 16;
const INSTANCE_CHALLENGE_HEADER: &str = "X-Corvus-Challenge";
const INSTANCE_PROOF_HEADER: &str = "X-Corvus-Instance-Proof";
const SHA256_PROOF_HEX_LENGTH: usize = 64;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SidecarState {
    Starting,
    Ready,
    Failed,
    Reconnecting,
    Stopped,
}

#[derive(Debug)]
pub struct SidecarLifecycle {
    state: SidecarState,
    history: Vec<SidecarState>,
}

impl Default for SidecarLifecycle {
    fn default() -> Self {
        Self {
            state: SidecarState::Stopped,
            history: vec![SidecarState::Stopped],
        }
    }
}

impl SidecarLifecycle {
    pub fn history(&self) -> &[SidecarState] {
        &self.history
    }

    pub fn start(&mut self) -> Result<(), String> {
        self.transition(
            SidecarState::Starting,
            &[SidecarState::Stopped, SidecarState::Failed],
        )
    }

    pub fn mark_ready(&mut self) -> Result<(), String> {
        self.transition(
            SidecarState::Ready,
            &[SidecarState::Starting, SidecarState::Reconnecting],
        )
    }

    pub fn mark_failed(&mut self) -> Result<(), String> {
        self.transition(
            SidecarState::Failed,
            &[
                SidecarState::Starting,
                SidecarState::Ready,
                SidecarState::Reconnecting,
            ],
        )
    }

    pub fn observe_health(&mut self, healthy: bool) -> Result<(), String> {
        match (self.state, healthy) {
            (SidecarState::Ready, false) => {
                self.transition(SidecarState::Reconnecting, &[SidecarState::Ready])
            }
            (SidecarState::Reconnecting, true) => {
                self.transition(SidecarState::Ready, &[SidecarState::Reconnecting])
            }
            _ => Ok(()),
        }
    }

    pub fn stop(&mut self) -> Result<(), String> {
        if self.state == SidecarState::Stopped {
            return Ok(());
        }
        self.transition(
            SidecarState::Stopped,
            &[
                SidecarState::Starting,
                SidecarState::Ready,
                SidecarState::Failed,
                SidecarState::Reconnecting,
            ],
        )
    }

    fn transition(&mut self, target: SidecarState, allowed: &[SidecarState]) -> Result<(), String> {
        if !allowed.contains(&self.state) {
            return Err(format!(
                "invalid_sidecar_transition:{:?}->{target:?}",
                self.state
            ));
        }
        self.state = target;
        self.history.push(target);
        Ok(())
    }
}

#[derive(Debug)]
pub struct SidecarLaunch {
    pub executable: PathBuf,
    pub database: PathBuf,
    pub static_web_dir: PathBuf,
    pub host: String,
    pub port: u16,
    pub pairing_secret: String,
    pub session_secret: String,
    pub instance_secret: String,
}

impl SidecarLaunch {
    pub fn command(&self) -> Result<Command, String> {
        if self.host != LOOPBACK_HOST {
            return Err("desktop_sidecar_loopback_required".to_owned());
        }
        let executable = fs::canonicalize(&self.executable)
            .map_err(|error| format!("sidecar_executable_invalid:{error}"))?;
        if !executable.is_file() {
            return Err("sidecar_executable_invalid:not_a_file".to_owned());
        }
        let mut command = Command::new(executable);
        command
            .args([
                "desktop-sidecar",
                "--database",
                self.database.to_string_lossy().as_ref(),
                "--host",
                &self.host,
                "--port",
                &self.port.to_string(),
                "--static-web-dir",
                self.static_web_dir.to_string_lossy().as_ref(),
            ])
            .env("CORVUS_BOOTSTRAP_TOKEN", &self.pairing_secret)
            .env("CORVUS_SESSION_SECRET", &self.session_secret)
            .env("CORVUS_INSTANCE_TOKEN", &self.instance_secret)
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(CREATE_NO_WINDOW);
        }
        Ok(command)
    }
}

struct SidecarProcess {
    child: Child,
    host: String,
    port: u16,
    instance_secret: String,
    lifecycle: Arc<Mutex<SidecarLifecycle>>,
    monitor_stop: Arc<AtomicBool>,
    monitor: Option<JoinHandle<()>>,
    stderr_buffer: Arc<Mutex<Vec<u8>>>,
    stderr_reader: Option<JoinHandle<()>>,
}

impl SidecarProcess {
    fn spawn(launch: &SidecarLaunch) -> Result<Self, String> {
        validate_static_web_dir(&launch.static_web_dir)?;
        if let Some(parent) = launch.database.parent() {
            fs::create_dir_all(parent)
                .map_err(|error| format!("desktop_data_directory_failed:{error}"))?;
        }
        let lifecycle = Arc::new(Mutex::new(SidecarLifecycle::default()));
        lifecycle
            .lock()
            .map_err(|_| "sidecar_lifecycle_poisoned".to_owned())?
            .start()?;
        let mut child = launch
            .command()?
            .spawn()
            .map_err(|error| format!("sidecar_spawn_failed:{error}"))?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| "sidecar_stderr_unavailable".to_owned())?;
        let stderr_buffer = Arc::new(Mutex::new(Vec::new()));
        let reader_buffer = Arc::clone(&stderr_buffer);
        let stderr_reader = thread::spawn(move || capture_bounded_stderr(stderr, reader_buffer));
        Ok(Self {
            child,
            host: launch.host.clone(),
            port: launch.port,
            instance_secret: launch.instance_secret.clone(),
            lifecycle,
            monitor_stop: Arc::new(AtomicBool::new(false)),
            monitor: None,
            stderr_buffer,
            stderr_reader: Some(stderr_reader),
        })
    }

    fn wait_until_ready(&mut self, timeout: Duration) -> Result<(), String> {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if let Some(status) = self
                .child
                .try_wait()
                .map_err(|error| format!("sidecar_status_failed:{error}"))?
            {
                self.mark_failed()?;
                self.finish_stderr_reader();
                return Err(self.with_diagnostics(format!("sidecar_exited_before_ready:{status}")));
            }
            if readiness_probe(&self.host, self.port, &self.instance_secret) {
                self.lifecycle
                    .lock()
                    .map_err(|_| "sidecar_lifecycle_poisoned".to_owned())?
                    .mark_ready()?;
                self.start_health_monitor();
                return Ok(());
            }
            thread::sleep(Duration::from_millis(100));
        }
        self.mark_failed()?;
        Err(self.with_diagnostics("sidecar_readiness_timeout".to_owned()))
    }

    fn start_health_monitor(&mut self) {
        let host = self.host.clone();
        let port = self.port;
        let instance_secret = self.instance_secret.clone();
        let lifecycle = Arc::clone(&self.lifecycle);
        let stop = Arc::clone(&self.monitor_stop);
        self.monitor = Some(thread::spawn(move || {
            let mut consecutive_failures = 0_u8;
            while !stop.load(Ordering::Relaxed) {
                thread::sleep(HEALTH_INTERVAL);
                if stop.load(Ordering::Relaxed) {
                    return;
                }
                let healthy = readiness_probe(&host, port, &instance_secret);
                consecutive_failures = if healthy {
                    0
                } else {
                    consecutive_failures.saturating_add(1)
                };
                let Ok(mut current) = lifecycle.lock() else {
                    return;
                };
                if current.observe_health(healthy).is_err() {
                    return;
                }
                if consecutive_failures >= MAX_FAILED_HEALTH_CHECKS {
                    let _ = current.mark_failed();
                    return;
                }
            }
        }));
    }

    fn mark_failed(&self) -> Result<(), String> {
        self.lifecycle
            .lock()
            .map_err(|_| "sidecar_lifecycle_poisoned".to_owned())?
            .mark_failed()
    }

    fn shutdown(&mut self) -> Result<(), String> {
        self.monitor_stop.store(true, Ordering::Relaxed);
        if let Some(monitor) = self.monitor.take() {
            let _ = monitor.join();
        }
        if self
            .child
            .try_wait()
            .map_err(|error| format!("sidecar_status_failed:{error}"))?
            .is_none()
        {
            if let Some(mut stdin) = self.child.stdin.take() {
                stdin
                    .write_all(b"shutdown\n")
                    .and_then(|_| stdin.flush())
                    .map_err(|error| format!("sidecar_shutdown_command_failed:{error}"))?;
            }
            let deadline = Instant::now() + SHUTDOWN_TIMEOUT;
            while Instant::now() < deadline {
                if self
                    .child
                    .try_wait()
                    .map_err(|error| format!("sidecar_status_failed:{error}"))?
                    .is_some()
                {
                    self.finish_stderr_reader();
                    self.stop_lifecycle()?;
                    return Ok(());
                }
                thread::sleep(Duration::from_millis(50));
            }
            self.child
                .kill()
                .map_err(|error| format!("sidecar_kill_failed:{error}"))?;
            self.child
                .wait()
                .map_err(|error| format!("sidecar_wait_failed:{error}"))?;
        }
        self.finish_stderr_reader();
        self.stop_lifecycle()
    }

    fn finish_stderr_reader(&mut self) {
        if let Some(reader) = self.stderr_reader.take() {
            let _ = reader.join();
        }
    }

    fn with_diagnostics(&self, error: String) -> String {
        let Ok(buffer) = self.stderr_buffer.lock() else {
            return error;
        };
        let diagnostics = sanitize_diagnostics(&String::from_utf8_lossy(&buffer));
        if diagnostics.is_empty() {
            error
        } else {
            format!("{error};sidecar_stderr={diagnostics}")
        }
    }

    fn stop_lifecycle(&self) -> Result<(), String> {
        self.lifecycle
            .lock()
            .map_err(|_| "sidecar_lifecycle_poisoned".to_owned())?
            .stop()
    }
}

impl Drop for SidecarProcess {
    fn drop(&mut self) {
        let _ = self.shutdown();
    }
}

#[derive(Default)]
struct DesktopState {
    sidecar: Mutex<Option<SidecarProcess>>,
    background_mode: AtomicBool,
    quitting: AtomicBool,
}

pub fn build_desktop_url(base_url: &str, pairing_secret: &str) -> Result<tauri::Url, String> {
    let mut url =
        tauri::Url::parse(base_url).map_err(|error| format!("desktop_url_invalid:{error}"))?;
    url.set_path("/");
    url.set_query(None);
    url.set_fragment(Some(&format!("pair={pairing_secret}")));
    Ok(url)
}

#[tauri::command]
fn select_repository_directory(app: tauri::AppHandle) -> Result<Option<String>, String> {
    app.dialog()
        .file()
        .set_title("Choose a Git repository")
        .blocking_pick_folder()
        .map(repository_directory_string)
        .transpose()
}

fn validate_artifact_name(name: &str) -> Result<(), String> {
    let valid_character =
        |character: char| character.is_ascii_alphanumeric() || "._-".contains(character);
    if name.is_empty()
        || name.len() > 120
        || !name.to_ascii_lowercase().ends_with(".zip")
        || !name.chars().all(valid_character)
        || Path::new(name).file_name().and_then(|value| value.to_str()) != Some(name)
    {
        return Err("artifact_name_invalid".to_owned());
    }
    Ok(())
}

fn write_artifact_file(path: &Path, contents: &[u8]) -> Result<(), String> {
    if contents.is_empty() || contents.len() > MAX_ARTIFACT_BYTES {
        return Err("artifact_size_invalid".to_owned());
    }
    fs::write(path, contents).map_err(|error| format!("artifact_write_failed:{error}"))
}

#[tauri::command]
fn save_artifact_file(
    app: tauri::AppHandle,
    suggested_name: String,
    contents: Vec<u8>,
) -> Result<Option<String>, String> {
    validate_artifact_name(&suggested_name)?;
    if contents.is_empty() || contents.len() > MAX_ARTIFACT_BYTES {
        return Err("artifact_size_invalid".to_owned());
    }
    let destination = app
        .dialog()
        .file()
        .set_title("Save finished Corvus project")
        .add_filter("ZIP archive", &["zip"])
        .set_file_name(suggested_name)
        .blocking_save_file();
    let Some(destination) = destination else {
        return Ok(None);
    };
    let FilePath::Path(path) = destination else {
        return Err("artifact_destination_must_be_local".to_owned());
    };
    write_artifact_file(&path, &contents)?;
    path.into_os_string()
        .into_string()
        .map(Some)
        .map_err(|_| "artifact_destination_not_unicode".to_owned())
}

#[tauri::command]
fn open_external_url(url: String) -> Result<(), String> {
    validate_external_url(&url)?;
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let windows_directory = env::var_os("WINDIR")
            .or_else(|| env::var_os("SystemRoot"))
            .map(PathBuf::from)
            .ok_or_else(|| "browser_launcher_unavailable".to_owned())?;
        let explorer = windows_directory.join("explorer.exe");
        if explorer.is_file()
            && Command::new(explorer)
                .arg(&url)
                .creation_flags(CREATE_NO_WINDOW)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .is_ok()
        {
            return Ok(());
        }
        let rundll32 = windows_directory.join("System32/rundll32.exe");
        if !rundll32.is_file() {
            return Err("browser_launcher_unavailable".to_owned());
        }
        Command::new(rundll32)
            .arg("url.dll,FileProtocolHandler")
            .arg(url)
            .creation_flags(CREATE_NO_WINDOW)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| "browser_launch_failed".to_owned())?;
        Ok(())
    }
    #[cfg(not(windows))]
    {
        let program = if cfg!(target_os = "macos") {
            "open"
        } else {
            "xdg-open"
        };
        Command::new(program)
            .arg(url)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| "browser_launch_failed".to_owned())?;
        Ok(())
    }
}

fn validate_external_url(url: &str) -> Result<(), String> {
    let parsed = tauri::Url::parse(url).map_err(|_| "external_url_invalid".to_owned())?;
    if parsed.scheme() != "https"
        || parsed.host_str() != Some("corvus-platform-tau.vercel.app")
        || parsed.path() != "/api/v2/auth/google/start"
        || parsed.username() != ""
        || parsed.password().is_some()
        || parsed.port().is_some()
        || parsed.query().is_some()
        || parsed.fragment().is_some()
    {
        return Err("external_url_forbidden".to_owned());
    }
    Ok(())
}

#[tauri::command]
fn set_background_mode(enabled: bool, state: tauri::State<'_, DesktopState>) {
    state.background_mode.store(enabled, Ordering::SeqCst);
}

#[tauri::command]
fn get_background_mode(state: tauri::State<'_, DesktopState>) -> bool {
    state.background_mode.load(Ordering::SeqCst)
}

fn load_desktop_preferences_file(path: &Path) -> Result<Option<String>, String> {
    match fs::metadata(path) {
        Ok(metadata) if metadata.len() > MAX_DESKTOP_PREFERENCES_BYTES as u64 => {
            return Err("desktop_preferences_too_large".to_owned());
        }
        Ok(_) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(format!("desktop_preferences_read_failed:{error}")),
    }
    match fs::read_to_string(path) {
        Ok(payload) => {
            if payload.len() > MAX_DESKTOP_PREFERENCES_BYTES {
                return Err("desktop_preferences_too_large".to_owned());
            }
            Ok(Some(payload))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(format!("desktop_preferences_read_failed:{error}")),
    }
}

fn save_desktop_preferences_file(path: &Path, payload: &str) -> Result<(), String> {
    if payload.len() > MAX_DESKTOP_PREFERENCES_BYTES {
        return Err("desktop_preferences_too_large".to_owned());
    }
    let parent = path
        .parent()
        .ok_or_else(|| "desktop_preferences_path_invalid".to_owned())?;
    fs::create_dir_all(parent)
        .map_err(|error| format!("desktop_preferences_directory_failed:{error}"))?;
    let temporary_path = path.with_extension(format!("{}.tmp", rand::rng().next_u64()));
    let write_result = (|| -> Result<(), String> {
        let mut temporary_file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary_path)
            .map_err(|error| format!("desktop_preferences_write_failed:{error}"))?;
        temporary_file
            .write_all(payload.as_bytes())
            .map_err(|error| format!("desktop_preferences_write_failed:{error}"))?;
        temporary_file
            .sync_all()
            .map_err(|error| format!("desktop_preferences_write_failed:{error}"))?;
        drop(temporary_file);
        fs::rename(&temporary_path, path)
            .map_err(|error| format!("desktop_preferences_write_failed:{error}"))
    })();
    if write_result.is_err() {
        let _ = fs::remove_file(&temporary_path);
    }
    write_result
}

fn desktop_preferences_path(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_data_dir()
        .map(|directory| directory.join(DESKTOP_PREFERENCES_FILE))
        .map_err(|error| format!("desktop_preferences_path_failed:{error}"))
}

#[tauri::command]
fn load_desktop_preferences(app: tauri::AppHandle) -> Result<Option<String>, String> {
    load_desktop_preferences_file(&desktop_preferences_path(&app)?)
}

#[tauri::command]
fn save_desktop_preferences(app: tauri::AppHandle, payload: String) -> Result<(), String> {
    save_desktop_preferences_file(&desktop_preferences_path(&app)?, &payload)
}

fn should_close_to_tray(background_mode: bool, quitting: bool) -> bool {
    background_mode && !quitting
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum TrayAction {
    Show,
    Quit,
}

fn tray_action(id: &str) -> Option<TrayAction> {
    match id {
        "show" => Some(TrayAction::Show),
        "quit" => Some(TrayAction::Quit),
        _ => None,
    }
}

fn repository_directory_string(selected: FilePath) -> Result<String, String> {
    match selected {
        FilePath::Path(path) => path
            .into_os_string()
            .into_string()
            .map_err(|_| "repository_directory_not_unicode".to_owned()),
        FilePath::Url(_) => Err("repository_directory_must_be_local".to_owned()),
    }
}

pub fn run() -> Result<(), String> {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .invoke_handler(tauri::generate_handler![
            select_repository_directory,
            save_artifact_file,
            open_external_url,
            set_background_mode,
            get_background_mode,
            load_desktop_preferences,
            save_desktop_preferences
        ])
        .manage(DesktopState::default())
        .setup(|app| setup_app(app).map_err(|error| std::io::Error::other(error).into()))
        .build(tauri::generate_context!())
        .map_err(|error| format!("desktop_build_failed:{error}"))?;
    app.run(|app_handle, event| {
        if let tauri::RunEvent::WindowEvent {
            label,
            event: tauri::WindowEvent::CloseRequested { api, .. },
            ..
        } = &event
            && label == "main"
        {
            let state = app_handle.state::<DesktopState>();
            if should_close_to_tray(
                state.background_mode.load(Ordering::SeqCst),
                state.quitting.load(Ordering::SeqCst),
            ) {
                api.prevent_close();
                if let Some(window) = app_handle.get_webview_window("main") {
                    let _ = window.hide();
                }
                return;
            }
            shutdown_managed_sidecar(app_handle);
            app_handle.exit(0);
            return;
        }
        if matches!(
            event,
            tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit
        ) {
            shutdown_managed_sidecar(app_handle);
        }
    });
    Ok(())
}

fn shutdown_managed_sidecar(app_handle: &tauri::AppHandle) {
    let state = app_handle.state::<DesktopState>();
    if let Ok(mut guard) = state.sidecar.lock()
        && let Some(mut sidecar) = guard.take()
    {
        let _ = sidecar.shutdown();
    }
}

fn setup_app(app: &mut tauri::App) -> Result<(), String> {
    setup_tray(app)?;
    let executable = sidecar_executable(app)?;
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| format!("desktop_resource_directory_failed:{error}"))?;
    let static_web_dir = env::var_os("CORVUS_WEB_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| resource_dir.join("web"));
    let data_dir = app
        .path()
        .app_local_data_dir()
        .map_err(|error| format!("desktop_data_directory_failed:{error}"))?;
    let port = available_loopback_port()?;
    let pairing_secret = random_secret(32);
    let session_secret = random_secret(48);
    let instance_secret = random_secret(32);
    let base_url = format!("http://{LOOPBACK_HOST}:{port}");
    let launch = SidecarLaunch {
        executable,
        database: data_dir.join("corvus-desktop.sqlite3"),
        static_web_dir,
        host: LOOPBACK_HOST.to_owned(),
        port,
        pairing_secret: pairing_secret.clone(),
        session_secret,
        instance_secret,
    };
    let mut sidecar = SidecarProcess::spawn(&launch)?;
    sidecar.wait_until_ready(READY_TIMEOUT)?;
    let window_url = build_desktop_url(&base_url, &pairing_secret)?;
    let allowed_origin =
        tauri::Url::parse(&base_url).map_err(|error| format!("desktop_url_invalid:{error}"))?;
    WebviewWindowBuilder::new(app, "main", WebviewUrl::External(window_url))
        .title("Corvus")
        .inner_size(1440.0, 960.0)
        .min_inner_size(960.0, 680.0)
        .devtools(cfg!(debug_assertions))
        .on_navigation(move |candidate| same_origin(candidate, &allowed_origin))
        .build()
        .map_err(|error| format!("desktop_window_failed:{error}"))?;
    let state = app.state::<DesktopState>();
    *state
        .sidecar
        .lock()
        .map_err(|_| "desktop_state_poisoned".to_owned())? = Some(sidecar);
    Ok(())
}

fn setup_tray(app: &mut tauri::App) -> Result<(), String> {
    let show = MenuItem::with_id(app, "show", "Show Corvus", true, None::<&str>)
        .map_err(|error| format!("desktop_tray_menu_failed:{error}"))?;
    let quit = MenuItem::with_id(app, "quit", "Quit Corvus", true, None::<&str>)
        .map_err(|error| format!("desktop_tray_menu_failed:{error}"))?;
    let menu = Menu::with_items(app, &[&show, &quit])
        .map_err(|error| format!("desktop_tray_menu_failed:{error}"))?;
    let mut builder = TrayIconBuilder::new()
        .tooltip("Corvus")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match tray_action(event.id.as_ref()) {
            Some(TrayAction::Show) => show_main_window(app),
            Some(TrayAction::Quit) => {
                let state = app.state::<DesktopState>();
                state.quitting.store(true, Ordering::SeqCst);
                shutdown_managed_sidecar(app);
                app.exit(0);
            }
            None => {}
        })
        .on_tray_icon_event(|tray, event| {
            if matches!(
                event,
                TrayIconEvent::Click {
                    button: MouseButton::Left,
                    button_state: MouseButtonState::Up,
                    ..
                }
            ) {
                show_main_window(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder
        .build(app)
        .map_err(|error| format!("desktop_tray_failed:{error}"))?;
    Ok(())
}

fn show_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn sidecar_executable(app: &tauri::App) -> Result<PathBuf, String> {
    if let Some(configured) = env::var_os("CORVUS_SIDECAR_EXECUTABLE") {
        return canonical_file(Path::new(&configured), "sidecar_executable_invalid");
    }
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| format!("desktop_resource_directory_failed:{error}"))?;
    let executable_name = if cfg!(windows) {
        "corvus-mvp.exe"
    } else {
        "corvus-mvp"
    };
    let app_executable = env::current_exe()
        .map_err(|error| format!("desktop_executable_directory_failed:{error}"))?;
    let app_executable_dir = app_executable
        .parent()
        .ok_or_else(|| "desktop_executable_directory_failed:missing_parent".to_owned())?;
    if let Some(executable) = select_packaged_sidecar(packaged_sidecar_candidates(
        &resource_dir,
        app_executable_dir,
        executable_name,
    ))? {
        return Ok(executable);
    }
    Err(format!(
        "sidecar_executable_missing:{}",
        resource_dir.join(executable_name).display()
    ))
}

fn select_packaged_sidecar(
    candidates: impl IntoIterator<Item = PathBuf>,
) -> Result<Option<PathBuf>, String> {
    for candidate in candidates {
        if candidate.is_file() {
            return canonical_file(&candidate, "sidecar_executable_invalid").map(Some);
        }
    }
    Ok(None)
}

fn packaged_sidecar_candidates(
    resource_dir: &Path,
    executable_dir: &Path,
    executable_name: &str,
) -> Vec<PathBuf> {
    vec![
        resource_dir.join(executable_name),
        executable_dir.join(executable_name),
    ]
}

fn canonical_file(path: &Path, code: &str) -> Result<PathBuf, String> {
    let canonical = fs::canonicalize(path).map_err(|error| format!("{code}:{error}"))?;
    if !canonical.is_file() {
        return Err(format!("{code}:not_a_file"));
    }
    Ok(canonical)
}

fn validate_static_web_dir(path: &Path) -> Result<(), String> {
    let root = fs::canonicalize(path).map_err(|error| format!("static_web_invalid:{error}"))?;
    let index = fs::canonicalize(root.join("index.html"))
        .map_err(|error| format!("static_web_index_missing:{error}"))?;
    if !index.is_file() || !index.starts_with(root) {
        return Err("static_web_index_missing".to_owned());
    }
    Ok(())
}

fn available_loopback_port() -> Result<u16, String> {
    let listener = TcpListener::bind((LOOPBACK_HOST, 0))
        .map_err(|error| format!("desktop_port_allocation_failed:{error}"))?;
    listener
        .local_addr()
        .map(|address| address.port())
        .map_err(|error| format!("desktop_port_allocation_failed:{error}"))
}

fn random_secret(bytes: usize) -> String {
    let mut value = vec![0_u8; bytes];
    rand::rng().fill_bytes(&mut value);
    value.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn capture_bounded_stderr<R: Read>(mut source: R, buffer: Arc<Mutex<Vec<u8>>>) {
    let mut chunk = [0_u8; 1024];
    loop {
        let Ok(read) = source.read(&mut chunk) else {
            return;
        };
        if read == 0 {
            return;
        }
        let Ok(mut destination) = buffer.lock() else {
            return;
        };
        destination.extend_from_slice(&chunk[..read]);
        let overflow = destination.len().saturating_sub(MAX_DIAGNOSTIC_BYTES);
        if overflow > 0 {
            destination.drain(..overflow);
        }
    }
}

fn sanitize_diagnostics(raw: &str) -> String {
    let mut value = raw
        .chars()
        .filter(|character| !character.is_control() || matches!(character, '\n' | '\t'))
        .collect::<String>();
    for name in [
        "CORVUS_BOOTSTRAP_TOKEN=",
        "CORVUS_SESSION_SECRET=",
        "CORVUS_INSTANCE_TOKEN=",
    ] {
        redact_assignment(&mut value, name);
    }
    value
        .replace('\r', "")
        .replace('\n', "\\n")
        .replace('\t', " ")
        .trim()
        .to_owned()
}

fn redact_assignment(value: &mut String, name: &str) {
    let mut search_from = 0;
    while let Some(relative_start) = value[search_from..].find(name) {
        let secret_start = search_from + relative_start + name.len();
        let secret_end = value[secret_start..]
            .find(char::is_whitespace)
            .map_or(value.len(), |offset| secret_start + offset);
        value.replace_range(secret_start..secret_end, "[REDACTED]");
        search_from = secret_start + "[REDACTED]".len();
    }
}

fn readiness_probe(host: &str, port: u16, instance_secret: &str) -> bool {
    let Ok(address) = format!("{host}:{port}").parse::<SocketAddr>() else {
        return false;
    };
    let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(350)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(350)));
    let challenge = random_secret(INSTANCE_CHALLENGE_BYTES);
    let request = format!(
        "GET /ready HTTP/1.1\r\nHost: {host}:{port}\r\n{INSTANCE_CHALLENGE_HEADER}: {challenge}\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = String::new();
    stream.read_to_string(&mut response).is_ok()
        && readiness_response_matches(&response, instance_secret, &challenge)
}

fn readiness_response_matches(response: &str, instance_secret: &str, challenge: &str) -> bool {
    let Some((headers, body)) = response.split_once("\r\n\r\n") else {
        return false;
    };
    let mut lines = headers.lines();
    if !lines
        .next()
        .is_some_and(|status_line| status_line.starts_with("HTTP/1.1 200"))
    {
        return false;
    }
    let proof = lines
        .filter_map(|line| line.split_once(':'))
        .find_map(|(name, value)| {
            name.eq_ignore_ascii_case(INSTANCE_PROOF_HEADER)
                .then(|| value.trim())
        });
    proof.is_some_and(|value| verify_instance_proof(value, instance_secret, challenge))
        && body.contains("\"status\":\"ready\"")
}

fn verify_instance_proof(proof: &str, instance_secret: &str, challenge: &str) -> bool {
    let Some(proof_bytes) = decode_sha256_proof(proof) else {
        return false;
    };
    let Ok(mut mac) = Hmac::<Sha256>::new_from_slice(instance_secret.as_bytes()) else {
        return false;
    };
    mac.update(challenge.as_bytes());
    mac.verify_slice(&proof_bytes).is_ok()
}

fn decode_sha256_proof(value: &str) -> Option<Vec<u8>> {
    if value.len() != SHA256_PROOF_HEX_LENGTH {
        return None;
    }
    value
        .as_bytes()
        .chunks_exact(2)
        .map(|pair| Some((hex_nibble(pair[0])? << 4) | hex_nibble(pair[1])?))
        .collect()
}

fn hex_nibble(value: u8) -> Option<u8> {
    match value {
        b'0'..=b'9' => Some(value - b'0'),
        b'a'..=b'f' => Some(value - b'a' + 10),
        b'A'..=b'F' => Some(value - b'A' + 10),
        _ => None,
    }
}

fn same_origin(candidate: &tauri::Url, allowed: &tauri::Url) -> bool {
    candidate.scheme() == allowed.scheme()
        && candidate.host_str() == allowed.host_str()
        && candidate.port_or_known_default() == allowed.port_or_known_default()
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;
    use std::io::Write;
    use std::net::TcpListener;
    use std::path::{Path, PathBuf};
    use std::thread;

    use super::{
        SidecarLaunch, SidecarLifecycle, SidecarState, TrayAction, build_desktop_url,
        capture_bounded_stderr, load_desktop_preferences_file, packaged_sidecar_candidates,
        readiness_probe, repository_directory_string, sanitize_diagnostics,
        save_desktop_preferences_file, select_packaged_sidecar, should_close_to_tray, tray_action,
        validate_artifact_name, validate_external_url, write_artifact_file,
    };
    use tauri_plugin_dialog::FilePath;

    #[test]
    fn lifecycle_records_ready_reconnect_and_stop() {
        let mut lifecycle = SidecarLifecycle::default();
        lifecycle.start().unwrap();
        lifecycle.mark_ready().unwrap();
        lifecycle.observe_health(false).unwrap();
        lifecycle.observe_health(true).unwrap();
        lifecycle.stop().unwrap();

        assert_eq!(
            lifecycle.history(),
            &[
                SidecarState::Stopped,
                SidecarState::Starting,
                SidecarState::Ready,
                SidecarState::Reconnecting,
                SidecarState::Ready,
                SidecarState::Stopped,
            ]
        );
    }

    #[test]
    fn close_to_tray_requires_background_mode_and_a_non_quitting_app() {
        assert!(should_close_to_tray(true, false));
        assert!(!should_close_to_tray(false, false));
        assert!(!should_close_to_tray(true, true));
        assert!(!should_close_to_tray(false, true));
    }

    #[test]
    fn tray_routes_only_explicit_show_and_quit_actions() {
        assert_eq!(tray_action("show"), Some(TrayAction::Show));
        assert_eq!(tray_action("quit"), Some(TrayAction::Quit));
        assert_eq!(tray_action("unexpected"), None);
    }

    #[test]
    fn launch_command_uses_fixed_argv_and_secret_environment() {
        let launch = SidecarLaunch {
            executable: std::env::current_exe().unwrap(),
            database: PathBuf::from("C:/Corvus/data.sqlite3"),
            static_web_dir: PathBuf::from("C:/Corvus/web"),
            host: "127.0.0.1".to_owned(),
            port: 8123,
            pairing_secret: "pairing-secret".to_owned(),
            session_secret: "session-secret".to_owned(),
            instance_secret: "instance-secret".to_owned(),
        };

        let command = launch.command().unwrap();
        let arguments = command
            .get_args()
            .map(|value| value.to_string_lossy().into_owned())
            .collect::<Vec<_>>();
        let environment = command
            .get_envs()
            .filter_map(|(key, value)| {
                value.map(|value| {
                    (
                        key.to_string_lossy().into_owned(),
                        value.to_string_lossy().into_owned(),
                    )
                })
            })
            .collect::<HashMap<_, _>>();

        assert_eq!(
            command.get_program(),
            std::fs::canonicalize(&launch.executable)
                .unwrap()
                .as_os_str()
        );
        assert_eq!(arguments[0], "desktop-sidecar");
        assert!(arguments.windows(2).any(|pair| pair == ["--port", "8123"]));
        assert_eq!(environment["CORVUS_BOOTSTRAP_TOKEN"], "pairing-secret");
        assert_eq!(environment["CORVUS_SESSION_SECRET"], "session-secret");
        assert_eq!(environment["CORVUS_INSTANCE_TOKEN"], "instance-secret");
    }

    #[test]
    fn readiness_rejects_a_decoy_process_on_the_reserved_port() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let decoy = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let response = concat!(
                "HTTP/1.1 200 OK\r\n",
                "Content-Type: application/json\r\n",
                "X-Corvus-Instance-Proof: 0000000000000000000000000000000000000000000000000000000000000000\r\n",
                "Content-Length: 18\r\n",
                "Connection: close\r\n\r\n",
                "{\"status\":\"ready\"}"
            );
            stream.write_all(response.as_bytes()).unwrap();
        });

        assert!(!readiness_probe("127.0.0.1", port, "expected-instance"));
        decoy.join().unwrap();
    }

    #[test]
    fn diagnostics_are_bounded_and_secret_assignments_are_redacted() {
        let buffer = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        capture_bounded_stderr(
            std::io::Cursor::new(vec![b'x'; super::MAX_DIAGNOSTIC_BYTES + 128]),
            std::sync::Arc::clone(&buffer),
        );
        assert_eq!(buffer.lock().unwrap().len(), super::MAX_DIAGNOSTIC_BYTES);

        let sanitized = sanitize_diagnostics(
            "failed CORVUS_BOOTSTRAP_TOKEN=secret-value\nCORVUS_INSTANCE_TOKEN=nonce",
        );
        assert!(!sanitized.contains("secret-value"));
        assert!(!sanitized.contains("nonce"));
        assert!(sanitized.contains("[REDACTED]"));
    }

    #[test]
    fn desktop_url_keeps_pairing_secret_out_of_the_http_request() {
        let url = build_desktop_url("http://127.0.0.1:8123", "one-time-secret").unwrap();

        assert_eq!(url.query(), None);
        assert_eq!(url.fragment(), Some("pair=one-time-secret"));
        assert_eq!(
            url.as_str().split('#').next(),
            Some("http://127.0.0.1:8123/")
        );
    }

    #[test]
    fn repository_picker_returns_only_local_paths() {
        assert_eq!(
            repository_directory_string(FilePath::Path(PathBuf::from("C:/Corvus"))).unwrap(),
            "C:/Corvus"
        );
        let remote = tauri::Url::parse("https://example.test/repository").unwrap();
        assert_eq!(
            repository_directory_string(FilePath::Url(remote)).unwrap_err(),
            "repository_directory_must_be_local"
        );
    }

    #[test]
    fn external_browser_url_is_exactly_allowlisted() {
        assert_eq!(
            validate_external_url(
                "https://corvus-platform-tau.vercel.app/api/v2/auth/google/start"
            ),
            Ok(())
        );
        for forbidden in [
            "http://corvus-platform-tau.vercel.app/api/v2/auth/google/start",
            "https://example.test/api/v2/auth/google/start",
            "https://corvus-platform-tau.vercel.app/api/v2/auth/google/start?next=evil",
            "https://corvus-platform-tau.vercel.app/api/v2/auth/google/start#fragment",
            "https://corvus-platform-tau.vercel.app:444/api/v2/auth/google/start",
            "https://user@corvus-platform-tau.vercel.app/api/v2/auth/google/start",
        ] {
            assert_eq!(
                validate_external_url(forbidden),
                Err("external_url_forbidden".to_owned())
            );
        }
    }

    #[test]
    fn packaged_sidecar_lookup_checks_resources_and_executable_directory() {
        let candidates = packaged_sidecar_candidates(
            Path::new("C:/Corvus/resources"),
            Path::new("C:/Corvus/bin"),
            "corvus-mvp.exe",
        );

        assert_eq!(
            candidates,
            [
                PathBuf::from("C:/Corvus/resources/corvus-mvp.exe"),
                PathBuf::from("C:/Corvus/bin/corvus-mvp.exe"),
            ]
        );
    }

    #[test]
    fn packaged_sidecar_lookup_skips_directories() {
        let directory =
            std::env::temp_dir().join(format!("corvus-sidecar-directory-{}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        let executable = std::env::current_exe().unwrap();

        let selected = select_packaged_sidecar([directory.clone(), executable.clone()])
            .unwrap()
            .unwrap();

        assert_eq!(selected, std::fs::canonicalize(executable).unwrap());
        std::fs::remove_dir(directory).unwrap();
    }

    #[test]
    fn desktop_preferences_round_trip_in_the_native_app_data_file() {
        let directory =
            std::env::temp_dir().join(format!("corvus-desktop-preferences-{}", std::process::id()));
        let path = directory.join("desktop-preferences.json");
        let payload = r#"{"corvus.local-first-run":"complete"}"#;

        assert_eq!(load_desktop_preferences_file(&path).unwrap(), None);
        save_desktop_preferences_file(&path, payload).unwrap();
        assert_eq!(
            load_desktop_preferences_file(&path).unwrap().as_deref(),
            Some(payload)
        );
        let replacement = r#"{"corvus.local-first-run":"updated"}"#;
        save_desktop_preferences_file(&path, replacement).unwrap();
        assert_eq!(
            load_desktop_preferences_file(&path).unwrap().as_deref(),
            Some(replacement)
        );

        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn desktop_preferences_reject_oversized_payloads() {
        let path = std::env::temp_dir().join("corvus-desktop-preferences-too-large.json");
        let payload = "x".repeat(super::MAX_DESKTOP_PREFERENCES_BYTES + 1);

        assert_eq!(
            save_desktop_preferences_file(&path, &payload),
            Err("desktop_preferences_too_large".to_owned())
        );

        std::fs::write(&path, &payload).unwrap();
        assert_eq!(
            load_desktop_preferences_file(&path),
            Err("desktop_preferences_too_large".to_owned())
        );
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn artifact_exports_accept_only_safe_zip_names() {
        assert_eq!(validate_artifact_name("corvus-project.zip"), Ok(()));
        assert_eq!(
            validate_artifact_name("../outside.zip"),
            Err("artifact_name_invalid".to_owned())
        );
        assert_eq!(
            validate_artifact_name("project.exe"),
            Err("artifact_name_invalid".to_owned())
        );
    }

    #[test]
    fn artifact_export_writes_the_selected_local_file() {
        let path =
            std::env::temp_dir().join(format!("corvus-artifact-export-{}.zip", std::process::id()));
        let payload = b"PK\x03\x04corvus";

        write_artifact_file(&path, payload).unwrap();
        assert_eq!(std::fs::read(&path).unwrap(), payload);
        assert_eq!(
            write_artifact_file(&path, &[]),
            Err("artifact_size_invalid".to_owned())
        );

        std::fs::remove_file(path).unwrap();
    }
}

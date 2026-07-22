fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "open_external_url",
            "select_repository_directory",
            "set_background_mode",
            "get_background_mode",
            "load_desktop_preferences",
            "save_desktop_preferences",
        ]),
    ))
    .expect("failed to build the Corvus Tauri application");
}

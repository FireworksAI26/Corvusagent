#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    if let Err(error) = corvus_desktop::run() {
        eprintln!("Corvus desktop failed: {error}");
        std::process::exit(1);
    }
}

// Minimal Tauri shell for now: it just opens the window pointed at the frontend, which
// talks to the FastAPI backend on 127.0.0.1:8000. The backend is started separately in
// dev mode (see README). Auto-spawning it as a packaged sidecar (PyInstaller binary +
// Tauri's `externalBin`) is the next step once Rust/cargo are installed and this can
// actually be built and tested — untested Rust process-lifecycle code isn't worth
// shipping blind.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

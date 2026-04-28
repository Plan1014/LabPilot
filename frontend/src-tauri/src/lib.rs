use std::process::Command;
use std::env;
#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|_app| {
            let project_root = env::current_dir()
                .unwrap_or_default()
                .parent()
                .map(|p| p.to_path_buf())
                .unwrap_or_else(|| env::current_dir().unwrap_or_default());

            let venv_python = project_root.join("venv").join("Scripts").join("python.exe");
            let python_exe = if venv_python.exists() {
                venv_python.to_string_lossy().to_string()
            } else {
                "python".to_string()
            };

            let _ = Command::new(&python_exe)
                .args(["-m", "uvicorn",
                       "src.agent.websocket_server:create_notification_hub_app",
                       "--factory", "--host", "127.0.0.1", "--port", "8000"])
                .current_dir(&project_root)
                .creation_flags(0x08000000) // CREATE_NO_WINDOW
                .spawn();

            println!("LabPilot backend starting on port 8000...");
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

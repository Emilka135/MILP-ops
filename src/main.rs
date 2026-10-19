use clinic_voice::{
    bus::Bus,
    server::{App, router},
};
use std::{collections::HashMap, env};
#[tokio::main]
async fn main() {
    let mode = env::var("APP_MODE").unwrap_or_else(|_| "demo".into());
    if mode != "demo" && env::var("API_TOKEN").unwrap_or_default().is_empty() {
        panic!("API_TOKEN is required in live mode");
    }
    let urls: HashMap<String, String> = [
        ("crm", 8081),
        ("nlu", 8082),
        ("sms", 8083),
        ("operator", 8084),
        ("tts", 8085),
        ("asr", 8086),
        ("agent", 8087),
    ]
    .into_iter()
    .map(|(name, port)| {
        (
            name.into(),
            env::var(format!("{}_URL", name.to_uppercase()))
                .unwrap_or_else(|_| format!("http://127.0.0.1:{port}")),
        )
    })
    .collect();
    let bus = Bus::new(urls, 128, 4);
    let app = App::new(bus);
    app.start_timers();
    let bind = env::var("BIND").unwrap_or_else(|_| "127.0.0.1:8080".into());
    let listener = tokio::net::TcpListener::bind(&bind)
        .await
        .expect("bind HTTP listener");
    println!("clinic-voice Rust listening on {bind} ({mode})");
    axum::serve(listener, router(app))
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await
        .unwrap();
}

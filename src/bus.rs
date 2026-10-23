//! Bounded queue per black box; exact request/call correlation and queue deadlines.
use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::{Request, client::conn::http1};
use hyper_util::rt::TokioIo;
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::Instant,
};
use tokio::{
    net::TcpStream,
    sync::{Semaphore, mpsc, oneshot},
    time::{Duration, Instant as TokioInstant, timeout_at},
};

tokio::task_local! {
    static TURN_DEADLINE: TokioInstant;
}

/// Every module in one patient turn shares a budget, including queue time.
pub async fn within_turn<T>(future: impl std::future::Future<Output = T>, budget: Duration) -> T {
    TURN_DEADLINE
        .scope(TokioInstant::now() + budget, future)
        .await
}

pub fn configured_limit(key: &str, fallback: usize, maximum: usize) -> usize {
    std::env::var(key)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(fallback)
        .clamp(1, maximum)
}

#[derive(Default)]
pub struct Metrics {
    pub submitted: AtomicU64,
    pub completed: AtomicU64,
    pub rejected: AtomicU64,
    pub expired: AtomicU64,
    pub queue_us: AtomicU64,
    pub service_us: AtomicU64,
}
#[derive(Debug, Clone)]
pub struct Reply {
    pub status: String,
    pub payload: Value,
}
struct Work {
    call_id: String,
    id: u64,
    op: String,
    args: Value,
    deadline: TokioInstant,
    enqueued: Instant,
    tx: oneshot::Sender<Reply>,
}
#[derive(Clone)]
pub struct Module {
    tx: mpsc::Sender<Work>,
    pub metrics: Arc<Metrics>,
    duration: Duration,
}
#[derive(Clone)]
pub struct Bus {
    modules: HashMap<String, Module>,
    sequence: Arc<AtomicU64>,
}

pub async fn post(url: &str, op: &str, args: &Value) -> Result<Reply, String> {
    // Internal network only. External HTTPS calls live in the single CRM adapter.
    let uri: hyper::Uri = format!("{}/v1/{}", url.trim_end_matches('/'), op)
        .parse()
        .map_err(|_| "bad module URL")?;
    if uri.scheme_str() != Some("http") {
        return Err("internal modules require http URLs".into());
    }
    let host = uri.host().ok_or("missing host")?;
    let port = uri.port_u16().unwrap_or(80);
    let stream = TcpStream::connect((host, port))
        .await
        .map_err(|e| e.to_string())?;
    let (mut sender, connection) = http1::handshake(TokioIo::new(stream))
        .await
        .map_err(|e| e.to_string())?;
    tokio::spawn(async move {
        let _ = connection.await;
    });
    let req = Request::post(uri.path_and_query().unwrap().as_str())
        .header("Host", format!("{host}:{port}"))
        .header("Content-Type", "application/json")
        .body(Full::new(Bytes::from(
            serde_json::to_vec(args).map_err(|e| e.to_string())?,
        )))
        .map_err(|e| e.to_string())?;
    let mut response = sender.send_request(req).await.map_err(|e| e.to_string())?;
    let mut body = Vec::new();
    while let Some(frame) = response.body_mut().frame().await {
        let frame = frame.map_err(|e| e.to_string())?;
        if let Some(data) = frame.data_ref() {
            if body.len() + data.len() > 8 * 1024 * 1024 {
                return Err("module response too large".into());
            }
            body.extend_from_slice(data);
        }
    }
    let value: Value = serde_json::from_slice(&body).map_err(|_| "invalid module JSON")?;
    Ok(Reply {
        status: value["status"].as_str().unwrap_or("error").into(),
        payload: value["payload"].clone(),
    })
}
impl Bus {
    pub fn new(urls: HashMap<String, String>, capacity: usize, workers: usize) -> Self {
        let mut modules = HashMap::new();
        for (name, url) in urls {
            let prefix = format!("MODULE_{}", name.to_uppercase());
            let capacity = configured_limit(&format!("{prefix}_QUEUE_CAPACITY"), capacity, 4096);
            let workers = configured_limit(&format!("{prefix}_WORKERS"), workers, 64);
            let (tx, mut rx) = mpsc::channel::<Work>(capacity);
            let metrics = Arc::new(Metrics::default());
            let m = metrics.clone();
            let slots = Arc::new(Semaphore::new(workers.max(1)));
            tokio::spawn(async move {
                loop {
                    // Reserve before recv: at most queue_capacity + workers items per module.
                    let Ok(permit) = slots.clone().acquire_owned().await else {
                        break;
                    };
                    let Some(w) = rx.recv().await else { break };
                    let m = m.clone();
                    let url = url.clone();
                    tokio::spawn(async move {
                        let _permit = permit;
                        m.queue_us
                            .fetch_add(w.enqueued.elapsed().as_micros() as u64, Ordering::Relaxed);
                        let start = Instant::now();
                        let response = if w.tx.is_closed() || TokioInstant::now() >= w.deadline {
                            m.expired.fetch_add(1, Ordering::Relaxed);
                            Reply {
                                status: "timeout".into(),
                                payload: json!({"error":"expired in queue"}),
                            }
                        } else {
                            let mut args = w.args;
                            if let Some(a) = args.as_object_mut() {
                                a.insert("call_id".into(), json!(w.call_id));
                                a.insert("request_id".into(), json!(w.id));
                            }
                            match timeout_at(w.deadline, post(&url, &w.op, &args)).await {
                                Ok(Ok(reply)) => reply,
                                Ok(Err(_)) => Reply {
                                    status: "error".into(),
                                    payload: json!({"error":"module unavailable"}),
                                },
                                Err(_) => {
                                    m.expired.fetch_add(1, Ordering::Relaxed);
                                    Reply {
                                        status: "timeout".into(),
                                        payload: json!({"error":"module deadline exceeded"}),
                                    }
                                }
                            }
                        };
                        m.service_us
                            .fetch_add(start.elapsed().as_micros() as u64, Ordering::Relaxed);
                        m.completed.fetch_add(1, Ordering::Relaxed);
                        let _ = w.tx.send(response);
                    });
                }
            });
            // A patient must not wait through a 180-second CRM request. Long
            // synchronisation/warm jobs belong to background workers.
            let default_seconds = configured_limit("MODULE_TIMEOUT_SECONDS", 15, 45);
            let seconds =
                configured_limit(&format!("{prefix}_TIMEOUT_SECONDS"), default_seconds, 45);
            let duration = Duration::from_secs(seconds as u64);
            modules.insert(
                name,
                Module {
                    tx,
                    metrics,
                    duration,
                },
            );
        }
        Self {
            modules,
            sequence: Arc::new(AtomicU64::new(1)),
        }
    }
    pub async fn call(&self, module: &str, op: &str, args: Value, call_id: &str) -> Reply {
        let Some(m) = self.modules.get(module) else {
            return Reply {
                status: "error".into(),
                payload: json!({"error":format!("module {module} not configured")}),
            };
        };
        let (tx, rx) = oneshot::channel();
        let module_deadline = TokioInstant::now() + m.duration;
        let deadline = TURN_DEADLINE
            .try_with(|turn| module_deadline.min(*turn))
            .unwrap_or(module_deadline);
        if deadline <= TokioInstant::now() {
            m.metrics.expired.fetch_add(1, Ordering::Relaxed);
            return Reply {
                status: "timeout".into(),
                payload: json!({"error":"patient turn deadline exceeded"}),
            };
        }
        let work = Work {
            call_id: call_id.into(),
            id: self.sequence.fetch_add(1, Ordering::Relaxed),
            op: op.into(),
            args,
            deadline,
            enqueued: Instant::now(),
            tx,
        };
        m.metrics.submitted.fetch_add(1, Ordering::Relaxed);
        if m.tx.try_send(work).is_err() {
            m.metrics.rejected.fetch_add(1, Ordering::Relaxed);
            return Reply {
                status: "error".into(),
                payload: json!({"error":"module queue full"}),
            };
        }
        match timeout_at(deadline, rx).await {
            Ok(Ok(reply)) => reply,
            _ => Reply {
                status: "timeout".into(),
                payload: json!({"error":"module deadline exceeded"}),
            },
        }
    }
    pub fn metrics(&self) -> Value {
        let mut all = serde_json::Map::new();
        for (name, m) in &self.modules {
            let c = &m.metrics;
            let get = |v: &AtomicU64| v.load(Ordering::Relaxed);
            all.insert(name.clone(),json!({"submitted":get(&c.submitted),"completed":get(&c.completed),"rejected":get(&c.rejected),"expired":get(&c.expired),"queue_us":get(&c.queue_us),"service_us":get(&c.service_us),"queued":m.tx.max_capacity()-m.tx.capacity()}));
        }
        Value::Object(all)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{Json, Router, routing::post as route_post};

    async fn echo(Json(args): Json<Value>) -> Json<Value> {
        tokio::time::sleep(Duration::from_millis(
            args["delay_ms"].as_u64().unwrap_or(2),
        ))
        .await;
        Json(json!({"status":"ok","payload":args}))
    }

    async fn module_server() -> (String, tokio::task::JoinHandle<()>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let task = tokio::spawn(async move {
            axum::serve(listener, Router::new().route("/v1/{op}", route_post(echo)))
                .await
                .unwrap();
        });
        (url, task)
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn twenty_simultaneous_calls_keep_turns_correlated_and_bounded() {
        let (url, server) = module_server().await;
        let bus = Bus::new(
            HashMap::from([("nlu".into(), url.clone()), ("crm".into(), url)]),
            32,
            4,
        );
        let barrier = Arc::new(tokio::sync::Barrier::new(21));
        let mut calls = tokio::task::JoinSet::new();
        for number in 0..20 {
            let (bus, barrier) = (bus.clone(), barrier.clone());
            calls.spawn(async move {
                barrier.wait().await;
                let id = format!("parallel-{number}");
                for turn in 0..3 {
                    let start = Instant::now();
                    within_turn(async {
                        for module in ["nlu", "crm"] {
                            let reply = bus.call(module, "echo", json!({"marker":number,"turn":turn,"delay_ms":if number%3==0 {12} else {2}}), &id).await;
                            assert_eq!(reply.status, "ok");
                            assert_eq!(reply.payload["call_id"], id);
                            assert_eq!(reply.payload["marker"], number);
                            assert_eq!(reply.payload["turn"], turn);
                            assert!(reply.payload["request_id"].as_u64().unwrap() > 0);
                        }
                    }, Duration::from_secs(55)).await;
                    assert!(start.elapsed() < Duration::from_secs(60));
                }
            });
        }
        barrier.wait().await;
        while let Some(call) = tokio::time::timeout(Duration::from_secs(10), calls.join_next())
            .await
            .unwrap()
        {
            call.unwrap();
        }
        let metrics = bus.metrics();
        assert_eq!(metrics["nlu"]["completed"], 60);
        assert_eq!(metrics["crm"]["completed"], 60);
        assert_eq!(metrics["nlu"]["rejected"], 0);
        server.abort();
    }

    #[tokio::test]
    async fn sequential_modules_share_the_patient_deadline() {
        let (url, server) = module_server().await;
        let bus = Bus::new(HashMap::from([("crm".into(), url)]), 20, 2);
        let start = Instant::now();
        within_turn(
            async {
                assert_eq!(
                    bus.call("crm", "slow", json!({"delay_ms":500}), "deadline")
                        .await
                        .status,
                    "timeout"
                );
                let next = bus.call("crm", "next", json!({}), "deadline").await;
                assert_eq!(next.status, "timeout");
                assert_eq!(next.payload["error"], "patient turn deadline exceeded");
            },
            Duration::from_millis(80),
        )
        .await;
        assert!(start.elapsed() < Duration::from_millis(400));
        server.abort();
    }
}

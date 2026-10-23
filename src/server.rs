use crate::{
    bus::Bus,
    engine::{Action, Event, Session},
};
use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Path, State},
    http::{HeaderMap, StatusCode},
    response::{Html, IntoResponse, Response},
    routing::{get, post},
};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, VecDeque},
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::sync::{Mutex, RwLock};

struct Call {
    session: Session,
    prompts: Vec<String>,
    transcript: Vec<Value>,
    clips: VecDeque<Value>,
    sms: Vec<Value>,
    transfer: Option<Value>,
    errors: Vec<String>,
    deadline: Option<Instant>,
    timeout_secs: f64,
    generation: u64,
    prompt_id: u64,
    started: Instant,
}
impl Call {
    fn prompt_done(&mut self, prompt_id: Option<u64>) {
        // Only the last current prompt can start the patient's response window.
        // Old acknowledgements after barge-in must not postpone a newer step.
        if !self.session.finished && self.clips.is_empty() && prompt_id == Some(self.prompt_id) {
            self.deadline =
                Some(Instant::now() + Duration::from_secs_f64(self.timeout_secs.max(1.0)));
        }
    }

    fn snapshot(&self) -> Value {
        let hidden = [
            "snils",
            "card_no",
            "birth_date",
            "phone",
            "caller_phone",
            "patient_full_name",
            "patient_birth_date",
            "identity_full_name",
            "identity_card_no",
            "oms",
            "identity_queries",
            "identity_reply",
            "identity_parts",
            "identity_birth_date",
            "last_utterance",
            "availability_utterance",
        ];
        let vars: serde_json::Map<String, Value> = self
            .session
            .vars
            .iter()
            .filter(|(k, _)| !hidden.contains(&k.as_str()))
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        json!({"state":self.session.state_name(),"finished":self.session.finished,"status":self.session.status,
            "prompts":self.prompts,"sms":self.sms,"transfer":self.transfer,"vars":vars,"errors":self.errors,
            "listening":self.session.listening,"input_field":self.session.input_field(),"generation":self.generation,"duration_s":self.started.elapsed().as_secs_f64()})
    }
}
#[derive(Clone)]
pub struct App {
    pub bus: Bus,
    calls: Arc<RwLock<HashMap<String, Arc<Mutex<Call>>>>>,
    token: String,
    pub demo: bool,
}
impl App {
    pub fn new(bus: Bus) -> Self {
        Self {
            bus,
            calls: Arc::new(RwLock::new(HashMap::new())),
            token: std::env::var("API_TOKEN").unwrap_or_default(),
            demo: std::env::var("APP_MODE").unwrap_or_else(|_| "demo".into()) == "demo",
        }
    }
    fn authorized(&self, headers: &HeaderMap) -> bool {
        self.token.is_empty()
            || headers
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .is_some_and(|v| v == format!("Bearer {}", self.token))
    }
    async fn drive(&self, id: &str, c: &mut Call, actions: Vec<Action>) {
        let mut queue: VecDeque<_> = actions.into();
        while let Some(a) = queue.pop_front() {
            match a.kind.as_str() {
                "say" => {
                    c.prompts.push(a.text.clone());
                    c.transcript.push(
                        json!({"role":"assistant","text":a.text,"state":a.args["scenario_state"],"step_index":a.args["step_index"],"source":"scenario"}),
                    );
                    if c.transcript.len() > 500 {
                        c.transcript.remove(0);
                    }
                    let r = self
                        .bus
                        .call("tts", "say", json!({"text":a.text,"rate":a.rate}), id)
                        .await;
                    if r.status == "ok" {
                        c.prompt_id += 1;
                        let mut clip = r.payload;
                        if let Some(p) = clip.as_object_mut() {
                            p.insert("prompt_id".into(), json!(c.prompt_id));
                            p.insert("generation".into(), json!(c.generation));
                        }
                        c.clips.push_back(clip);
                    } else {
                        c.errors.push("tts unavailable".into());
                    }
                }
                "call" => {
                    let module = a.args["module"].as_str().unwrap_or("");
                    let op = a.args["op"].as_str().unwrap_or("");
                    let mut args = a.args["args"].clone();
                    if let Some(p) = args.as_object_mut() {
                        p.insert(
                            "operation_key".into(),
                            json!(format!("{id}:{}", a.request_id.unwrap_or(0))),
                        );
                        if matches!(
                            op,
                            "callback_request" | "recovery.route" | "booking_request"
                        ) {
                            p.insert(
                                "context".into(),
                                json!({"vars":c.session.vars,"history":c.session.history,"dialogue":c.transcript}),
                            );
                        }
                    }
                    let r = self.bus.call(module, op, args, id).await;
                    let next = c.session.handle(Event {
                        kind: "module".into(),
                        value: if r.status == "timeout" {
                            "error".into()
                        } else {
                            r.status
                        },
                        payload: r.payload,
                        request_id: a.request_id,
                    });
                    for a in next.into_iter().rev() {
                        queue.push_front(a);
                    }
                }
                "sms" => {
                    let r = self.bus.call("sms", "send", a.args, id).await;
                    if r.status == "ok" {
                        c.sms.push(r.payload);
                    } else {
                        c.errors.push("sms delivery pending or failed".into());
                    }
                }
                "transfer" => {
                    let mut payload = a.args;
                    payload["dialogue"] = json!(c.transcript);
                    let r = self.bus.call("operator", "transfer", payload, id).await;
                    if r.status != "ok" {
                        c.errors.push("operator service unavailable".into());
                    }
                    c.transfer = Some(r.payload);
                }
                "timer" => {
                    c.timeout_secs = a.args["seconds"].as_f64().unwrap_or(60.0);
                    c.deadline = Some(Instant::now() + Duration::from_secs_f64(c.timeout_secs));
                }
                "end" => {
                    c.deadline = None;
                }
                _ => {}
            }
        }
        if c.session.finished || !c.clips.is_empty() {
            c.deadline = None;
        }
        let archive = self
            .bus
            .call(
                "crm",
                "learning.archive",
                json!({
                    "call_id":id,"registrar":c.session.vars.get("registrar_number"),
                    "finished":c.session.finished,"outcome":c.session.status,
                    "duration_s":c.started.elapsed().as_secs_f64(),"transcript":c.transcript,
                    "trace":c.session.history,"experiment_id":c.session.vars.get("experiment_id"),
                    "scenario_revision":crate::engine::scenario_revision(),"graph":crate::engine::scenario_graph()
                }),
                id,
            )
            .await;
        if archive.status != "ok" && !c.errors.iter().any(|e| e == "call archive unavailable") {
            c.errors.push("call archive unavailable".into());
        }
    }
    async fn event(&self, id: &str, op: &str, args: &Value) -> Result<Value, String> {
        let call = self
            .calls
            .read()
            .await
            .get(id)
            .cloned()
            .ok_or("unknown call")?;
        let mut c = call.lock().await;
        if op == "call.state" {
            return Ok(c.snapshot());
        }
        if op == "call.next_prompt" {
            return Ok(c.clips.pop_front().unwrap_or_else(||json!({"generation":c.generation,"finished":c.session.finished,"status":c.session.status,"listening":c.session.listening})));
        }
        if op == "call.prompt_done" {
            c.prompt_done(args["prompt_id"].as_u64());
            return Ok(c.snapshot());
        }
        if c.session.finished {
            return Ok(c.snapshot());
        }
        let mut ev = match op {
            "call.dtmf" => {
                let digit = args["digit"].as_str().ok_or("missing digit")?;
                let source_state = c.session.state_name();
                let step_index = c.session.history.len().saturating_sub(1);
                c.transcript.push(
                    json!({"role":"dtmf","text":format!("Клавиша {digit}"),"state":source_state,"step_index":step_index,"source":"dtmf"}),
                );
                if c.transcript.len() > 500 {
                    c.transcript.remove(0);
                }
                Event::new("dtmf", digit)
            }
            "call.silence" => Event::new("silence", ""),
            "call.timeout" if self.demo => Event::new("timeout", ""),
            "call.hangup" => Event::new("hangup", ""),
            "call.speech" | "call.partial" | "call.audio" => {
                let recognized = if op == "call.audio" {
                    if !c.session.listening {
                        return Ok(json!({"ignored":"dtmf_phase"}));
                    }
                    let r = self.bus.call("asr", "transcribe", args.clone(), id).await;
                    if r.status != "ok" {
                        let a = c.session.escalate("asr_failed");
                        self.drive(id, &mut c, a).await;
                        return Ok(c.snapshot());
                    }
                    r.payload["text"].as_str().unwrap_or("").to_owned()
                } else {
                    args["text"].as_str().unwrap_or("").to_owned()
                };
                if op != "call.partial" {
                    let source_state = c.session.state_name();
                    let step_index = c.session.history.len().saturating_sub(1);
                    c.transcript.push(json!({"role":"patient","text":recognized.chars().take(4000).collect::<String>(),"state":source_state,"step_index":step_index,"source":if op=="call.audio" {"real_asr"} else {"runtime_text"}}));
                    if c.transcript.len() > 500 {
                        c.transcript.remove(0);
                    }
                }
                let r = self
                    .bus
                    .call(
                        "nlu",
                        "interpret",
                        json!({"text":recognized,"today":c.session.vars.get("today"),"identity_field":c.session.input_field(),"state":c.session.state_name()}),
                        id,
                    )
                    .await;
                if r.status != "ok" {
                    let a = c.session.escalate("nlu_failed");
                    self.drive(id, &mut c, a).await;
                    return Ok(c.snapshot());
                }
                if op != "call.partial" && r.payload["intent"] == "complaint" {
                    let _ = self.bus.call("crm", "feedback.submit", json!({"call_id":id,"registrar":c.session.vars.get("registrar_number"),"text":recognized,"kind":"complaint"}), id).await;
                }
                // A partial hypothesis may only escalate; it never books/cancels/confirms.
                if op == "call.partial"
                    && !matches!(
                        r.payload["intent"].as_str().unwrap_or(""),
                        "emergency" | "complaint" | "medical" | "operator"
                    )
                {
                    return Ok(c.snapshot());
                }
                let mut e = Event::new("speech", &recognized);
                e.payload = r.payload;
                e
            }
            _ => return Err("unknown operation".into()),
        };
        if op == "call.speech" && !c.session.listening && !self.demo {
            // Safety is handled immediately even while collecting DTMF.
            if !matches!(
                ev.payload["intent"].as_str().unwrap_or(""),
                "emergency" | "complaint" | "medical" | "operator"
            ) {
                return Ok(c.snapshot());
            }
        }
        if ev.kind == "dtmf" || ev.kind == "hangup" || ev.kind == "speech" {
            c.clips.clear();
            c.generation += 1;
        }
        c.deadline = None;
        if ev.kind == "hangup" {
            ev.payload = json!({});
        }
        let actions = c.session.handle(ev);
        self.drive(id, &mut c, actions).await;
        if !c.session.finished
            && c.session.pending.is_none()
            && c.deadline.is_none()
            && c.clips.is_empty()
        {
            c.deadline = Some(Instant::now() + Duration::from_secs_f64(c.timeout_secs.max(1.0)));
        }
        Ok(c.snapshot())
    }
    pub async fn operation(&self, op: &str, args: Value) -> Result<Value, String> {
        if op == "speech.synth" {
            let response = self.bus.call("tts", "synth", args, "speech-test").await;
            return if response.status == "ok" {
                Ok(response.payload)
            } else {
                Err("TTS synthesis failed".into())
            };
        }
        if op == "nlu.plan" {
            let utterance = args["text"].as_str().unwrap_or("");
            let parsed = self
                .bus
                .call(
                    "nlu",
                    "interpret",
                    json!({"text":utterance,"today":args["today"]}),
                    "plan",
                )
                .await;
            if parsed.status != "ok" {
                return Err("NLU unavailable".into());
            }
            let intent = if args["strategy"] == "rules" {
                crate::nlu::classify_rules(utterance)
            } else {
                crate::nlu::Automaton::default().classify(utterance)
            };
            let specialty = parsed.payload["vars"]["specialty"].as_str();
            let plan = crate::nlu::plan(intent, args["patient_id"].as_i64(), specialty);
            let value = serde_json::to_value(plan).map_err(|_| "plan error")?;
            let rows = self
                .bus
                .call("crm", "plan_lookup", value.clone(), "plan")
                .await;
            return Ok(
                json!({"plan":value,"rows":rows.payload,"status":rows.status,"execution":"read-only"}),
            );
        }
        if op == "metrics" {
            return Ok(json!({"modules":self.bus.metrics(),"calls":self.calls.read().await.len()}));
        }
        if op.starts_with("stand.") {
            let r = self.bus.call("crm", op, args, "").await;
            return Ok(json!({"status":r.status,"result":r.payload}));
        }
        let id = args["call_id"]
            .as_str()
            .filter(|s| !s.is_empty() && s.len() <= 128)
            .ok_or("call_id required (1..128 bytes)")?
            .to_owned();
        if op != "call.start" {
            return self.event(&id, op, &args).await;
        }
        let flow = args["flow"].as_str().unwrap_or("inbound");
        let meta = self.bus.call("crm", "meta", json!({}), &id).await;
        if meta.status != "ok" {
            return Err("CRM unavailable".into());
        }
        let mut vars = meta.payload.as_object().cloned().unwrap_or_default();
        if let Some(extra) = args["vars"].as_object() {
            vars.extend(extra.clone());
        }
        vars.insert(
            "caller_phone".into(),
            json!(args["caller_phone"].as_str().unwrap_or("")),
        );
        vars.insert("call_id".into(), json!(id));
        let registrar = args["registrar_number"].as_str().unwrap_or("100");
        vars.insert("registrar_number".into(), json!(registrar));

        if flow == "reminder" {
            let r = self
                .bus
                .call("crm", "reminder_vars", Value::Object(vars.clone()), &id)
                .await;
            if r.status != "ok" {
                return Err("reminder booking not found".into());
            }
            if let Some(p) = r.payload.as_object() {
                vars.extend(p.clone());
            }
        }
        let mut session = Session::new(flow, vars)?;
        let mut calls = self.calls.write().await;
        if calls.contains_key(&id) {
            return Err("call_id already used".into());
        }
        if calls.len() >= 1000 {
            calls.retain(|_, v| {
                v.try_lock()
                    .map(|c| !c.session.finished || c.started.elapsed() < Duration::from_secs(600))
                    .unwrap_or(true)
            });
            if calls.len() >= 1000 {
                return Err("call capacity reached".into());
            }
        }
        let actions = session.start();
        let c = Arc::new(Mutex::new(Call {
            session,
            prompts: vec![],
            transcript: vec![],
            clips: VecDeque::new(),
            sms: vec![],
            transfer: None,
            errors: vec![],
            deadline: None,
            timeout_secs: 60.0,
            generation: 0,
            prompt_id: 0,
            started: Instant::now(),
        }));
        calls.insert(id.clone(), c.clone());
        drop(calls);
        let mut c = c.lock().await;
        self.drive(&id, &mut c, actions).await;
        Ok(c.snapshot())
    }
    pub fn start_timers(&self) {
        let app = self.clone();
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(Duration::from_millis(250));
            loop {
                tick.tick().await;
                let calls: Vec<_> = app
                    .calls
                    .read()
                    .await
                    .iter()
                    .map(|(k, c)| (k.clone(), c.clone()))
                    .collect();
                for (id, call) in calls {
                    if let Ok(mut c) = call.clone().try_lock_owned()
                        && c.deadline.is_some_and(|d| d <= Instant::now())
                    {
                        c.deadline = None;
                        let app = app.clone();
                        tokio::spawn(async move {
                            let actions = c.session.handle(Event::new("timeout", ""));
                            app.drive(&id, &mut c, actions).await;
                        });
                    }
                }
            }
        });
    }
}
fn result(r: Result<Value, String>) -> Response {
    match r {
        Ok(payload) => Json(json!({"status":"ok","payload":payload})).into_response(),
        Err(error) => (
            StatusCode::BAD_REQUEST,
            Json(json!({"status":"error","payload":{"error":error}})),
        )
            .into_response(),
    }
}
async fn operation(
    State(app): State<App>,
    headers: HeaderMap,
    Path(op): Path<String>,
    Json(args): Json<Value>,
) -> Response {
    if !app.authorized(&headers) {
        return StatusCode::UNAUTHORIZED.into_response();
    }
    let budget_ms = crate::bus::configured_limit("TURN_TIMEOUT_MS", 55_000, 55_000);
    result(
        crate::bus::within_turn(
            app.operation(&op, args),
            Duration::from_millis(budget_ms as u64),
        )
        .await,
    )
}
async fn health(State(app): State<App>) -> Json<Value> {
    Json(json!({"status":"ok","module":"rust-orchestrator","mode":if app.demo{"demo"}else{"live"}}))
}
async fn ready(State(app): State<App>) -> Response {
    let mut probes = tokio::task::JoinSet::new();
    for module in ["crm", "nlu", "tts", "asr", "sms", "operator", "agent"] {
        let bus = app.bus.clone();
        probes.spawn(
            async move { bus.call(module, "health", json!({}), "probe").await.status == "ok" },
        );
    }
    let mut ok = true;
    while let Some(result) = probes.join_next().await {
        ok &= result.unwrap_or(false);
    }
    if ok {
        Json(json!({"ready":true})).into_response()
    } else {
        StatusCode::SERVICE_UNAVAILABLE.into_response()
    }
}
async fn metrics(State(app): State<App>, h: HeaderMap) -> Response {
    if !app.authorized(&h) {
        return StatusCode::UNAUTHORIZED.into_response();
    }
    Json(app.bus.metrics()).into_response()
}
async fn operator(State(app): State<App>, headers: HeaderMap) -> Response {
    if !app.authorized(&headers) {
        return StatusCode::UNAUTHORIZED.into_response();
    }
    let r = app.bus.call("operator", "list", json!({}), "console").await;
    Html(
        r.payload["html"]
            .as_str()
            .unwrap_or("Оператор недоступен")
            .to_owned(),
    )
    .into_response()
}
pub fn router(app: App) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/readyz", get(ready))
        .route("/metrics", get(metrics))
        .route("/operator", get(operator))
        .route("/v1/{op}", post(operation))
        .layer(DefaultBodyLimit::max(3 * 1024 * 1024))
        .with_state(app)
}

#[cfg(test)]
mod prompt_timer_tests {
    use super::*;

    fn call() -> Call {
        Call {
            session: Session::new("inbound", serde_json::Map::new()).unwrap(),
            prompts: vec![],
            transcript: vec![],
            clips: VecDeque::new(),
            sms: vec![],
            transfer: None,
            errors: vec![],
            deadline: None,
            timeout_secs: 15.0,
            generation: 2,
            prompt_id: 3,
            started: Instant::now(),
        }
    }

    #[test]
    fn response_window_starts_only_after_final_current_prompt() {
        let mut c = call();
        c.clips.push_back(json!({"prompt_id":3}));
        c.prompt_done(Some(3));
        assert!(c.deadline.is_none());
        c.clips.clear();
        c.prompt_done(Some(2));
        assert!(c.deadline.is_none());
        c.prompt_done(None);
        assert!(c.deadline.is_none());
        c.prompt_done(Some(3));
        assert!(
            c.deadline
                .unwrap()
                .duration_since(Instant::now())
                .as_secs_f64()
                > 14.0
        );
    }

    #[test]
    fn ordinary_snapshot_does_not_expose_identity_fields_or_lookup_rows() {
        let mut c = call();
        for key in [
            "oms",
            "availability_utterance",
            "identity_queries",
            "identity_reply",
            "identity_parts",
            "identity_birth_date",
        ] {
            c.session
                .vars
                .insert(key.into(), json!("private-synthetic-value"));
        }
        c.session
            .vars
            .insert("scenario_revision".into(), json!("public-revision"));
        let snapshot = c.snapshot();
        assert_eq!(
            snapshot["vars"],
            json!({"scenario_revision":"public-revision"})
        );
    }

    #[test]
    fn finished_session_never_rearms() {
        let mut c = call();
        c.session.finished = true;
        c.prompt_done(Some(3));
        assert!(c.deadline.is_none());
    }

    #[tokio::test]
    async fn queued_speech_suspends_the_response_timer() {
        let app = App::new(Bus::new(HashMap::new(), 1, 1));
        let mut c = call();
        c.clips.push_back(json!({"prompt_id":3}));
        app.drive(
            "timer-regression",
            &mut c,
            vec![Action {
                kind: "timer".into(),
                text: String::new(),
                rate: 1.5,
                args: json!({"seconds":15}),
                request_id: None,
            }],
        )
        .await;
        assert!(c.deadline.is_none());
        c.clips.clear();
        c.prompt_done(Some(3));
        assert!(c.deadline.is_some());
    }
}

//! Replayable scenario VM. No network, clock or shared mutable state.
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use std::{collections::HashMap, sync::OnceLock};

mod generated {
    include!(concat!(env!("OUT_DIR"), "/scenarios.rs"));
}

#[derive(Debug, Clone, Deserialize)]
pub struct Statement {
    pub kind: String,
    pub args: HashMap<String, String>,
    pub text: String,
}
#[derive(Debug, Clone, Deserialize)]
pub struct Edge {
    pub kind: String,
    pub values: Vec<String>,
    pub target: String,
    pub target_index: usize,
    pub sets: HashMap<String, String>,
    pub seconds: Option<f64>,
    pub count: Option<usize>,
}
#[derive(Debug, Clone, Deserialize)]
pub struct Node {
    pub name: String,
    pub flow: String,
    pub rate: f64,
    pub statements: Vec<Statement>,
    pub edges: Vec<Edge>,
}
impl Node {
    fn edge(&self, kind: &str) -> Option<&Edge> {
        self.edges.iter().find(|e| e.kind == kind)
    }
}
fn nodes() -> &'static Vec<Node> {
    static NODES: OnceLock<Vec<Node>> = OnceLock::new();
    NODES.get_or_init(|| {
        serde_json::from_str(generated::NODES_JSON).expect("validated build artifact")
    })
}
pub fn node(flow: &str, state: &str) -> Option<&'static Node> {
    generated::state_index(flow, state).map(|i| &nodes()[i])
}
pub fn text(v: &Value) -> String {
    match v {
        Value::Null => String::new(),
        Value::String(s) => s.clone(),
        v => v.to_string(),
    }
}
#[derive(Debug, Clone, Serialize)]
pub struct Action {
    pub kind: String,
    pub text: String,
    pub rate: f64,
    pub args: Value,
    pub request_id: Option<u64>,
}
impl Action {
    fn new(kind: &str, args: Value) -> Self {
        Self {
            kind: kind.into(),
            text: String::new(),
            rate: 1.5,
            args,
            request_id: None,
        }
    }
}
fn system_notice(key: &str, state: &str) -> Action {
    let node = node("system_notices", key).expect("required system notice missing");
    let statement = node
        .statements
        .iter()
        .find(|s| s.kind == "say")
        .expect("required system notice has no phrase");
    let mut action = Action::new(
        "say",
        json!({"scenario_state":state,
        "prompt_source":format!("system_notices.{key}")}),
    );
    action.text = statement.text.clone();
    action.rate = node.rate;
    action
}

#[derive(Debug, Clone)]
pub struct Event {
    pub kind: String,
    pub value: String,
    pub payload: Value,
    pub request_id: Option<u64>,
}
impl Event {
    pub fn new(kind: &str, value: &str) -> Self {
        Self {
            kind: kind.into(),
            value: value.into(),
            payload: json!({}),
            request_id: None,
        }
    }
}
type NavigationSnapshot = (
    String,
    Map<String, Value>,
    Option<Value>,
    Option<Value>,
    Option<Value>,
);

#[derive(Debug)]
pub struct Session {
    pub flow: String,
    pub state: String,
    pub vars: Map<String, Value>,
    pub history: Vec<String>,
    pub finished: bool,
    pub status: String,
    pub listening: bool,
    pub pending: Option<u64>,
    sequence: u64,
    retries: HashMap<String, usize>,
    visits: HashMap<String, usize>,
    stack: Vec<(String, String)>,
    digits: String,
    ask: Option<HashMap<String, String>>,
    input_kind: Option<String>,
    index: usize,
    verified_patient: Option<Value>,
    verified_slot: Option<Value>,
    verified_booking: Option<Value>,
    approval: Option<Value>,
    mutation_succeeded: bool,
    pending_call: Option<(String, String, Value)>,
    navigation: Vec<NavigationSnapshot>,
}
impl Session {
    pub fn new(flow: &str, vars: Map<String, Value>) -> Result<Self, String> {
        let entry = generated::entry(flow).ok_or("unknown flow")?;
        Ok(Self {
            flow: flow.into(),
            state: entry.into(),
            vars,
            history: vec![],
            finished: false,
            status: String::new(),
            listening: false,
            pending: None,
            sequence: 0,
            retries: HashMap::new(),
            visits: HashMap::new(),
            stack: vec![],
            digits: String::new(),
            ask: None,
            input_kind: None,
            index: generated::state_index(flow, entry).unwrap(),
            verified_patient: None,
            verified_slot: None,
            verified_booking: None,
            approval: None,
            mutation_succeeded: false,
            pending_call: None,
            navigation: vec![],
        })
    }
    pub fn state_name(&self) -> String {
        format!("{}.{}", self.flow, self.state)
    }
    pub fn input_field(&self) -> Option<&str> {
        self.ask
            .as_ref()
            .and_then(|ask| {
                ask.get("var").map(|v| {
                    if v == "identity_card_no" {
                        "card_no"
                    } else {
                        v.as_str()
                    }
                })
            })
            .or(self.input_kind.as_deref())
    }
    pub fn start(&mut self) -> Vec<Action> {
        self.enter(self.state.clone(), false)
    }
    fn current(&self) -> &'static Node {
        &nodes()[self.index]
    }
    fn value(&self, raw: &str) -> Value {
        if let Some(key) = raw.strip_prefix('$') {
            self.vars.get(key).cloned().unwrap_or(Value::Null)
        } else {
            match raw {
                "true" => json!(true),
                "false" => json!(false),
                _ => json!(raw),
            }
        }
    }
    fn set(&mut self, key: &str, value: Value) {
        if key == "said_daypart" {
            let spoken = match value.as_str().unwrap_or("") {
                "morning" => "утром",
                "midday" => "в обед",
                "afternoon" => "после обеда",
                "evening" => "вечером",
                _ => "",
            };
            self.vars
                .insert("said_daypart_spoken".into(), json!(spoken));
        }
        self.vars.insert(key.into(), value);
    }
    fn render(&self, template: &str) -> String {
        let mut out = String::new();
        let mut rest = template;
        while let Some(i) = rest.find('{') {
            out.push_str(&rest[..i]);
            if let Some(j) = rest[i..].find('}') {
                out.push_str(
                    &self
                        .vars
                        .get(&rest[i + 1..i + j])
                        .map(text)
                        .unwrap_or_default(),
                );
                rest = &rest[i + j + 1..];
            } else {
                out.push_str(&rest[i..]);
                return out;
            }
        }
        out.push_str(rest);
        out
    }
    fn finish(&mut self, status: &str) -> Vec<Action> {
        self.finished = true;
        self.status = status.into();
        self.listening = false;
        self.pending = None;
        vec![Action::new("end", json!({"status": status}))]
    }
    pub fn escalate(&mut self, reason: &str) -> Vec<Action> {
        self.set("reason", json!(reason));
        self.pending = None;
        if self.vars.get("operator_available") == Some(&json!(false))
            && self.vars.get("last_intent") != Some(&json!("emergency"))
        {
            self.set(
                "callback_question",
                self.vars
                    .get("last_utterance")
                    .cloned()
                    .unwrap_or(Value::Null),
            );
            return self.enter("callback.prepare".into(), false);
        }
        self.enter("operator".into(), false)
    }
    fn follow(&mut self, e: &Edge) -> Vec<Action> {
        for (k, v) in &e.sets {
            self.set(k, self.value(v));
        }
        let same = e.target == self.state;
        self.enter_index(e.target_index, same)
    }
    fn enter(&mut self, reference: String, same: bool) -> Vec<Action> {
        let (flow, state) = reference
            .split_once('.')
            .unwrap_or((&self.flow, &reference));
        let Some(index) = generated::state_index(flow, state) else {
            return self.finish("invalid_target");
        };
        self.enter_index(index, same)
    }
    fn enter_index(&mut self, index: usize, same: bool) -> Vec<Action> {
        self.index = index;
        self.flow = nodes()[index].flow.clone();
        self.state = nodes()[index].name.clone();
        if self.flow == "reschedule" && self.state == "rs_start" && self.verified_patient.is_none()
        {
            self.set("requested_action", json!("reschedule"));
            self.set("intent", json!("existing"));
            self.stack.push(("inbound".into(), "id_done".into()));
            return self.enter("identity.name".into(), false);
        }
        let key = self.state_name();
        if !same {
            self.retries.insert(key.clone(), 0);
        }
        let visits = self.visits.entry(key.clone()).or_default();
        *visits += 1;
        let count = *visits;
        self.history.push(key);
        if self.history.len() > 500 {
            return self.finish("loop_guard");
        }
        let n = self.current();
        if let Some(e) = n.edge("loop")
            && count > e.count.unwrap_or(usize::MAX)
        {
            return self.follow(e);
        }
        if !self.mutation_succeeded
            && n.statements.iter().any(|s| {
                s.kind == "sms"
                    && matches!(
                        s.args.get("template").map(String::as_str),
                        Some("booked" | "rescheduled" | "cancelled" | "confirmed")
                    )
                    || s.kind == "end"
                        && matches!(
                            s.args.get("status").map(String::as_str),
                            Some("booked" | "rescheduled" | "cancelled" | "confirmed")
                        )
            })
        {
            return self.escalate("missing_write_receipt");
        }
        self.digits.clear();
        self.ask = None;
        self.input_kind = None;
        self.pending = None;
        self.listening = false;
        let mut actions = vec![];
        for st in &n.statements {
            let a = &st.args;
            match st.kind.as_str() {
                "set" => self.set(&a["var"], self.value(&a["value"])),
                "say" => { let mut action = Action::new("say", json!({"scenario_state": self.state_name(),"step_index":self.history.len().saturating_sub(1)})); action.text = self.render(&st.text);
                    action.rate = a.get("rate").and_then(|s| s.parse().ok()).unwrap_or(n.rate); actions.push(action); }
                "sms" => actions.push(Action::new("sms", json!({"template": a["template"], "vars": self.vars}))),
                "transfer" => actions.push(Action::new("transfer", json!({"reason": self.value(a.get("reason").map(String::as_str).unwrap_or("$reason")), "vars": self.vars, "history": self.history}))),
                "call" => {
                    if a["module"] == "crm" && matches!(a["op"].as_str(), "book"|"cancel"|"reschedule"|"confirm") {
                        if !self.write_allowed(&a["op"]) { return self.escalate("incomplete_booking_contract"); }
                        self.approval=None;
                    }
                    self.sequence += 1; self.pending = Some(self.sequence);
                    let mut args = Map::new();
                    for (k, v) in a { if let Some(arg) = k.strip_prefix("arg:") { args.insert(arg.into(), self.value(v)); } }
                    if a["module"] == "crm" && matches!(a["op"].as_str(), "cancel"|"reschedule"|"confirm") {
                        args.insert("patient_id".into(),self.verified_patient.clone().unwrap_or(Value::Null));
                    }
                    self.pending_call=Some((a["module"].clone(),a["op"].clone(),Value::Object(args.clone())));
                    actions.push(Action { request_id: self.pending, ..Action::new("call", json!({"module": a["module"], "op": a["op"], "args": args})) });
                }
                "ask" => { self.ask = Some(a.clone()); actions.push(Action::new("timer", json!({"seconds": a.get("timeout").and_then(|s| s.parse::<f64>().ok()).unwrap_or(60.0)}))); }
                "listen" => { self.input_kind=a.get("field").cloned(); self.listening = true; actions.push(Action::new("timer", json!({"seconds": a["timeout"].parse::<f64>().unwrap_or(8.0)}))); }
                "end" => { actions.extend(self.finish(&a["status"])); return actions; }
                "goto" => { let same = self.state == a["target"]; actions.extend(self.enter(a["target"].clone(), same)); return actions; }
                "enter" => { self.stack.push((self.flow.clone(), a["return"].clone())); actions.extend(self.enter(a["target"].clone(), false)); return actions; }
                "return" => { if let Some((f, s)) = self.stack.pop() { actions.extend(self.enter(format!("{f}.{s}"), false)); }
                    else { actions.extend(self.finish("error")); } return actions; }
                "switch" => {
                    let v = self.vars.get(&a["var"]).map(text).filter(|s| !s.is_empty()).unwrap_or_else(|| "none".into()).to_lowercase();
                    let edge = n.edges.iter().find(|e| e.kind == "case" && e.values[0].to_lowercase() == v).or_else(|| n.edge("default")).unwrap();
                    actions.extend(self.follow(edge)); return actions;
                }
                _ => unreachable!("validated statement"),
            }
        }
        if self.pending.is_none()
            && !actions.iter().any(|a| a.kind == "timer")
            && let Some(e) = n.edge("timeout")
        {
            self.listening = n.edges.iter().any(|e| {
                matches!(
                    e.kind.as_str(),
                    "yes" | "no" | "date" | "time" | "daypart" | "intent" | "any"
                )
            });
            actions.push(Action::new(
                "timer",
                json!({"seconds": e.seconds.unwrap_or(10.0)}),
            ));
        }
        if self.listening || self.ask.is_some() || n.edges.iter().any(|e| e.kind == "dtmf") {
            let key = self.state_name();
            if self.navigation.last().is_none_or(|x| x.0 != key) {
                self.navigation.push((
                    key,
                    self.vars.clone(),
                    self.verified_patient.clone(),
                    self.verified_slot.clone(),
                    self.verified_booking.clone(),
                ));
                if self.navigation.len() > 64 {
                    self.navigation.remove(0);
                }
            }
        }
        actions
    }
    fn notice(&mut self, key: &str) -> Vec<Action> {
        let mut actions = vec![system_notice(key, &self.state_name())];
        actions.extend(self.enter(self.state.clone(), true));
        actions
    }
    fn fingerprint(&self, op: &str) -> Value {
        json!([
            op,
            self.vars.get("patient_id"),
            self.vars.get("booking_id"),
            self.vars.get("slot_id"),
            self.vars.get("specialty")
        ])
    }
    fn write_allowed(&self, op: &str) -> bool {
        self.verified_patient.is_some()
            && self.verified_patient.as_ref() == self.vars.get("patient_id")
            && self.approval.as_ref() == Some(&self.fingerprint(op))
            && (op == "book"
                || self.verified_booking.as_ref() == self.vars.get("booking_id")
                    && self.verified_booking.is_some())
            && (!matches!(op, "book" | "reschedule")
                || self.verified_slot.is_some()
                    && self.verified_slot.as_ref() == self.vars.get("slot_id"))
    }
    fn follow_input(&mut self, e: &Edge) -> Vec<Action> {
        // Only a positive answer to the currently displayed summary authorizes a write.
        if let Some(st) = nodes()[e.target_index].statements.iter().find(|st| {
            st.kind == "call"
                && st.args["module"] == "crm"
                && matches!(
                    st.args["op"].as_str(),
                    "book" | "cancel" | "reschedule" | "confirm"
                )
        }) && (e.kind == "yes"
            || e.kind == "intent" && e.values.iter().any(|v| v == "confirm")
            || e.kind == "dtmf" && e.values.iter().any(|v| v == "1"))
        {
            self.approval = Some(self.fingerprint(&st.args["op"]));
        }
        self.follow(e)
    }
    fn back(&mut self) -> Vec<Action> {
        self.approval = None;
        if self.mutation_succeeded {
            return self.escalate("change_after_completed_operation");
        }
        if self
            .navigation
            .last()
            .is_some_and(|x| x.0 == self.state_name())
        {
            self.navigation.pop();
        }
        if let Some((state, vars, patient, slot, booking)) = self.navigation.pop() {
            self.vars = vars;
            self.verified_patient = patient;
            self.verified_slot = slot;
            self.verified_booking = booking;
            return self.enter(state, false);
        }
        self.enter(self.state.clone(), true)
    }
    pub fn handle(&mut self, ev: Event) -> Vec<Action> {
        if self.finished {
            return vec![];
        }
        if ev.kind == "hangup" {
            return self.finish("hangup");
        }
        if ev.kind == "module" {
            // A response without its exact correlation id cannot mutate a call.
            if self.pending.is_none() || ev.request_id != self.pending {
                return vec![];
            }
            self.pending = None;
            if let Some((module, op, args)) = self.pending_call.take() {
                if ev.value == "ok" {
                    if module == "crm"
                        && matches!(
                            op.as_str(),
                            "identify" | "verify_card" | "verify_birth" | "find_patient"
                        )
                        && (op != "find_patient"
                            || args.get("birth_date").is_some_and(|v| !v.is_null()))
                        && ev.payload["patient_full_name"]
                            .as_str()
                            .is_some_and(|v| v.split_whitespace().count() >= 2)
                        && ev.payload["patient_birth_date"]
                            .as_str()
                            .is_some_and(|v| !v.is_empty())
                    {
                        self.verified_patient = ev.payload.get("patient_id").cloned();
                    }
                    if module == "crm"
                        && matches!(op.as_str(), "my_booking" | "select_booking" | "verify_card")
                    {
                        self.verified_booking = ev.payload.get("booking_id").cloned();
                    }
                    if module == "crm"
                        && matches!(op.as_str(), "book" | "cancel" | "reschedule" | "confirm")
                    {
                        self.mutation_succeeded = ev.payload.get("booking_id").is_some();
                        if !self.mutation_succeeded {
                            return self.escalate("missing_write_receipt");
                        }
                    }
                }
                if matches!(ev.value.as_str(), "ok" | "relaxed" | "near" | "single")
                    && (module == "crm" || module == "agent" && op == "offer")
                    && ev.payload["slot_date"]
                        .as_str()
                        .is_some_and(|v| !v.is_empty())
                    && ev.payload["slot_time"]
                        .as_str()
                        .is_some_and(|v| !v.is_empty())
                    && ev.payload["doctor"].as_str().is_some_and(|v| !v.is_empty())
                {
                    self.verified_slot = ev.payload.get("slot_id").cloned();
                    self.approval = None;
                }
            }
            if let Some(p) = ev.payload.as_object() {
                for (k, v) in p {
                    self.set(k, v.clone());
                }
            }
            let n = self.current();
            return n
                .edge(&ev.value)
                .or_else(|| n.edge("error"))
                .map(|e| self.follow(e))
                .unwrap_or_else(|| self.escalate("module_error"));
        }
        if self.pending.is_some() {
            return vec![];
        }
        match ev.kind.as_str() {
            "dtmf" => self.dtmf(&ev.value),
            "speech" => self.speech(ev),
            "timeout" | "silence" => self.timeout(ev.kind == "silence"),
            _ => vec![],
        }
    }
    fn ask_done(&mut self) -> Vec<Action> {
        let ask = self.ask.take().unwrap();
        let digits = std::mem::take(&mut self.digits);
        self.set(&ask["var"], json!(digits));
        let n = self.current();
        n.edge("ok")
            .map(|e| self.follow(e))
            .unwrap_or_else(|| self.invalid())
    }
    fn dtmf(&mut self, value: &str) -> Vec<Action> {
        if value.len() != 1 || !"0123456789*#".contains(value) {
            return self.invalid();
        }
        if value == "*" {
            return self.escalate("requested");
        }
        if let Some(a) = &self.ask {
            if value == "*" {
                self.digits.clear();
                return vec![];
            }
            if value == a.get("terminator").map(String::as_str).unwrap_or("#") {
                return if self.digits.len() >= a["min"].parse().unwrap() {
                    self.ask_done()
                } else {
                    self.invalid()
                };
            }
            if value.as_bytes()[0].is_ascii_digit() {
                self.digits.push_str(value);
            }
            return if self.digits.len() >= a["max"].parse().unwrap() {
                self.ask_done()
            } else {
                vec![]
            };
        }
        let n = self.current();
        n.edges
            .iter()
            .find(|e| e.kind == "dtmf" && e.values.iter().any(|v| v == value))
            .map(|e| self.follow_input(e))
            .unwrap_or_else(|| self.invalid())
    }
    fn speech(&mut self, ev: Event) -> Vec<Action> {
        if ev.value.trim().is_empty() {
            return self.timeout(true);
        }
        // Safety intent wins over yes/no/date in EVERY state, including identity.
        let intent = ev.payload["intent"]
            .as_str()
            .unwrap_or("unknown")
            .to_owned();
        self.set("last_utterance", json!(ev.value));
        self.set("last_intent", json!(intent));
        if intent == "voicemail" {
            return self.finish("no_verified_contact");
        }
        if let Some(vars) = ev.payload["vars"].as_object() {
            for (k, v) in vars {
                if !v.is_null()
                    && matches!(
                        k.as_str(),
                        "specialty"
                            | "specialty_candidate"
                            | "specialty_candidate_label"
                            | "said_date"
                            | "said_time"
                            | "said_daypart"
                            | "for_other"
                            | "doctor_surname"
                    )
                {
                    if matches!(
                        k.as_str(),
                        "specialty" | "said_date" | "said_time" | "said_daypart"
                    ) && self.vars.get(k) != Some(v)
                    {
                        self.verified_slot = None;
                        self.approval = None;
                        self.vars.remove("slot_id");
                    }
                    self.set(k, v.clone());
                }
            }
        }
        if matches!(
            intent.as_str(),
            "home_visit" | "results" | "paid" | "waitlist" | "attachment"
        ) {
            self.set("source_scenario", ev.payload["source_scenario"].clone());
            return self.escalate("scenario_integration_not_enabled");
        }
        if intent == "for_other" || ev.payload["vars"]["for_other"] == true {
            return self.escalate("representative_verification_required");
        }
        if intent == "record_opt_out" {
            self.set("recording_consent", json!(false));
            return self.notice("record_opt_out");
        }
        if intent == "self_identity" {
            return self.notice("self_identity");
        }
        if ev.payload["flags"]
            .as_array()
            .is_some_and(|flags| flags.contains(&json!("abusive_language")))
        {
            self.set("client_flags", json!(["abusive_language"]));
        }
        if intent == "emergency" {
            let notice = system_notice("emergency", "С-14");
            let mut actions = vec![notice];
            actions.extend(self.escalate("emergency"));
            return actions;
        }
        if intent == "medical" {
            return self.enter("inbound.medical_boundary".into(), false);
        }
        if intent == "complaint" {
            let notice = system_notice("complaint", "С-15");
            let mut actions = vec![notice];
            actions.extend(self.escalate("complaint"));
            return actions;
        }
        if intent == "operator" {
            return self.escalate("requested");
        }
        if self
            .vars
            .get("doctor_surname")
            .is_some_and(|v| !text(v).is_empty())
        {
            return self.escalate("specific_doctor");
        }
        if intent == "back" {
            return self.back();
        }
        if matches!(intent.as_str(), "repeat" | "greeting")
            && self.input_kind.as_deref() != Some("free_text")
        {
            return self.enter(self.state.clone(), true);
        }
        if matches!(
            intent.as_str(),
            "book" | "cancel" | "reschedule" | "my_booking"
        ) {
            let confirming_cancel = self.state == "cancel_confirm"
                && intent == "cancel"
                && ev.payload["affirmative_action"] == true;
            if !confirming_cancel {
                self.set("requested_action", json!(intent));
                self.set(
                    "intent",
                    json!(if intent == "book" { "book" } else { "existing" }),
                );
                if self.verified_patient.is_none() && self.flow != "identity" {
                    self.stack.push(("inbound".into(), "id_done".into()));
                    return self.enter("identity.name".into(), false);
                }
                if self.flow == "identity" {
                    return self.enter(self.state.clone(), true);
                }
                if intent != "book"
                    && !self
                        .current()
                        .edges
                        .iter()
                        .any(|e| e.kind == "intent" && e.values.contains(&intent))
                {
                    return self.enter("inbound.existing_start".into(), false);
                }
            }
        }
        if self.flow == "identity"
            && self.state == "name"
            && ev.value.split_whitespace().count() < 3
        {
            return self.invalid();
        }
        if self.verified_patient.is_some()
            && ev.payload["vars"]["specialty"].is_string()
            && self.flow != "identity"
            && !matches!(
                self.state.as_str(),
                "ask_request" | "book_start" | "book_ask_specialty"
            )
        {
            return self.enter("inbound.book_referral_check".into(), false);
        }
        if let Some(a) = &self.ask
            && let Some(d) = ev.payload["digits"].as_str()
            && d.len() >= a["min"].parse().unwrap()
            && d.len() <= a["max"].parse().unwrap()
        {
            self.digits = d.into();
            return self.ask_done();
        }
        let n = self.current();
        let cancel_confirmation = self.state == "cancel_confirm"
            && matches!(intent.as_str(), "cancel" | "unknown")
            && ev.payload["affirmative_action"] == true;
        // Never interpret an action request as a generic yes at another action's
        // confirmation. In particular, cancellation must not create a booking.
        if intent == "cancel"
            && self.state != "cancel_confirm"
            && !(self.flow == "reminder" && self.state == "identity")
            && !n
                .edges
                .iter()
                .any(|e| e.kind == "intent" && e.values.contains(&intent))
        {
            return self.escalate("conflicting_action");
        }
        for e in &n.edges {
            let matches = match e.kind.as_str() {
                "yes" => ev.payload["yes_no"] == true || cancel_confirmation,
                "no" => ev.payload["yes_no"] == false && !cancel_confirmation,
                "date" => ev.payload["vars"]["said_date"].is_string(),
                "time" => ev.payload["vars"]["said_time"].is_string(),
                "daypart" => ev.payload["vars"]["said_daypart"].is_string(),
                "digits" => ev.payload["digits"].as_str().is_some_and(|s| s.len() >= 2),
                "intent" => {
                    e.values.contains(&intent)
                        && ev.payload["confidence"].as_f64().unwrap_or(0.0) >= 0.6
                }
                "any" => {
                    if self.input_kind.as_deref() == Some("free_text") {
                        return self.follow_input(e);
                    }
                    ev.payload["vars"]["said_date"].is_string()
                        || ev.payload["vars"]["said_time"].is_string()
                        || ev.payload["vars"]["said_daypart"].is_string()
                        || (self.flow == "identity"
                            && self.state == "name"
                            && intent == "unknown"
                            && ev.payload["yes_no"].is_null())
                        || ev.payload["vars"]["specialty"].is_string()
                        || ev.payload["vars"]["specialty_candidate"].is_string()
                        || intent != "unknown"
                            && !matches!(intent.as_str(), "answer_yes" | "answer_no")
                }
                _ => false,
            };
            if matches {
                if e.kind == "digits" {
                    self.set("said_digits", ev.payload["digits"].clone());
                }
                return self.follow_input(e);
            }
        }
        self.invalid()
    }
    fn increment_retry(&mut self) -> Option<Edge> {
        let key = self.state_name();
        let n = self.retries.entry(key).or_default();
        *n += 1;
        let count = *n;
        self.current()
            .edge("retry")
            .filter(|e| count >= e.count.unwrap_or(usize::MAX))
            .cloned()
    }
    fn timeout(&mut self, silence: bool) -> Vec<Action> {
        if let Some(a) = &self.ask
            && self.digits.len() >= a["min"].parse().unwrap()
        {
            return self.ask_done();
        }
        self.digits.clear();
        if let Some(e) = self.increment_retry() {
            return self.follow(&e);
        }
        let n = self.current();
        if let Some(e) =
            (if silence { n.edge("silence") } else { None }).or_else(|| n.edge("timeout"))
        {
            return self.follow(e);
        }
        self.enter(self.state.clone(), true)
    }
    fn invalid(&mut self) -> Vec<Action> {
        self.digits.clear();
        if let Some(e) = self.increment_retry() {
            return self.follow(&e);
        }
        if let Some(e) = self.current().edge("invalid") {
            return self.follow(e);
        }
        self.enter(self.state.clone(), true)
    }
}

/// Same graph, branch sequence and checksum for both dispatch representations.
/// Measures dispatch only, excluding speech inference, RPC and graph loading.
pub fn benchmark_dispatch(linked: bool, iterations: u64) -> (u128, usize) {
    use std::{hint::black_box, time::Instant};
    let graph = nodes();
    let mut index = generated::state_index("inbound", "welcome").unwrap();
    let mut random = 7u64;
    let mut checksum = 0usize;
    let began = Instant::now();
    for _ in 0..iterations {
        random = random.wrapping_mul(6364136223846793005).wrapping_add(1);
        let current = &graph[index];
        if current.edges.is_empty() {
            index = (random as usize) % graph.len();
            continue;
        }
        let edge = black_box(&current.edges[(random as usize) % current.edges.len()]);
        index = if linked {
            black_box(edge.target_index)
        } else {
            let (f, s) = edge
                .target
                .split_once('.')
                .unwrap_or((&current.flow, &edge.target));
            generated::state_index(black_box(f), black_box(s)).unwrap()
        };
        checksum = checksum.wrapping_add(black_box(index));
    }
    (began.elapsed().as_nanos(), black_box(checksum))
}

/// Store one immutable graph per compiled scenario revision in the call archive.
pub fn scenario_revision() -> &'static str {
    generated::SCENARIO_REVISION
}
pub fn scenario_graph() -> &'static Value {
    static GRAPH: OnceLock<Value> = OnceLock::new();
    GRAPH.get_or_init(|| {
        let mut cards = Map::new();
        let mut branches = Map::new();
        let mut titles = Map::new();
        for n in nodes() {
            let key = format!("{}.{}", n.flow, n.name);
            let lines: Vec<&str> = n
                .statements
                .iter()
                .filter(|s| s.kind == "say")
                .map(|s| s.text.as_str())
                .collect();
            let title = lines
                .first()
                .map(|s| s.chars().take(85).collect::<String>())
                .unwrap_or_else(|| key.clone());
            let mut targets: Vec<String> = n
                .edges
                .iter()
                .map(|e| {
                    format!(
                        "{}.{}",
                        nodes()[e.target_index].flow,
                        nodes()[e.target_index].name
                    )
                })
                .collect();
            for s in &n.statements {
                if matches!(s.kind.as_str(), "goto" | "enter") {
                    let target = &s.args["target"];
                    targets.push(if target.contains('.') {
                        target.clone()
                    } else {
                        format!("{}.{}", n.flow, target)
                    });
                }
            }
            targets.sort();
            targets.dedup();
            cards.insert(
                key.clone(),
                json!({"id":key,"title":title,"lines":lines,"flow":n.flow}),
            );
            titles.insert(key.clone(), json!(title));
            branches.insert(key, json!(targets));
        }
        json!({"version":scenario_revision(),"nodes":cards,"titles":titles,"branches":branches})
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn typed_free_text_field_is_preserved() {
        let mut s = Session::new("feedback", Map::new()).unwrap();
        s.start();
        assert_eq!(s.input_field(), Some("free_text"));
    }
    #[test]
    fn stale_result_does_not_cross_calls() {
        let mut s = Session::new("inbound", Map::new()).unwrap();
        s.start();
        s.pending = Some(9);
        let old = s.state_name();
        let mut ev = Event::new("module", "ok");
        ev.request_id = Some(8);
        ev.payload = json!({"patient_id":99});
        s.handle(ev);
        assert_eq!(s.state_name(), old);
        assert!(!s.vars.contains_key("patient_id"));
    }
    #[test]
    fn medical_yes_never_confirms() {
        let mut s = Session::new("reminder", Map::new()).unwrap();
        s.start();
        let mut ev = Event::new("speech", "да, у меня острая боль");
        ev.payload = json!({"intent":"emergency","yes_no":true});
        let actions = s.handle(ev);
        assert_eq!(s.status, "transfer");
        assert!(actions.iter().any(|a| a.kind == "transfer"));
    }
    #[test]
    fn unknown_flow_is_error() {
        assert!(Session::new("missing", Map::new()).is_err());
    }
    #[test]
    fn reminder_cancel_requires_explicit_confirmation() {
        let mut s = Session::new("reminder", Map::new()).unwrap();
        s.start();
        s.vars.insert("patient_id".into(), json!(1));
        s.vars.insert("booking_id".into(), json!(2));
        s.verified_patient = Some(json!(1));
        s.verified_booking = Some(json!(2));
        s.enter("start".into(), false);
        s.handle(Event::new("dtmf", "0"));
        assert_eq!(s.state, "cancel_offer");
        s.handle(Event::new("dtmf", "2"));
        assert_eq!(s.state, "cancel_confirm");
        assert!(s.pending.is_none());
        let actions = s.handle(Event::new("dtmf", "1"));
        assert!(
            actions
                .iter()
                .any(|a| a.kind == "call" && a.args["op"] == "cancel")
        );
    }
    #[test]
    fn spoken_cancel_yes_is_contextual() {
        let mut s = Session::new("reminder", Map::new()).unwrap();
        s.start();
        s.vars.insert("patient_id".into(), json!(1));
        s.vars.insert("booking_id".into(), json!(2));
        s.verified_patient = Some(json!(1));
        s.verified_booking = Some(json!(2));
        s.enter("start".into(), false);
        s.handle(Event::new("dtmf", "0"));
        s.handle(Event::new("dtmf", "2"));
        let mut ev = Event::new("speech", "Да, отмените.");
        ev.payload = json!({"intent":"cancel","yes_no":false,"affirmative_action":true});
        let actions = s.handle(ev);
        assert!(
            actions
                .iter()
                .any(|a| a.kind == "call" && a.args["op"] == "cancel")
        );
    }
    #[test]
    fn all_mutations_and_receipts_reject_unverified_entry() {
        for n in nodes() {
            if n.statements.iter().any(|st| {
                st.kind == "call"
                    && st.args["module"] == "crm"
                    && matches!(
                        st.args["op"].as_str(),
                        "book" | "cancel" | "reschedule" | "confirm"
                    )
            }) {
                let mut session = Session::new(&n.flow, Map::new()).unwrap();
                let actions = session.enter(format!("{}.{}", n.flow, n.name), false);
                assert!(
                    !actions.iter().any(|a| a.kind == "call"
                        && matches!(
                            a.args["op"].as_str(),
                            Some("book" | "cancel" | "reschedule" | "confirm")
                        )),
                    "{}",
                    n.name
                );
            }
        }
    }
    #[test]
    fn speech_does_not_inject_trusted_identifiers() {
        let mut s = Session::new("inbound", Map::new()).unwrap();
        s.start();
        let mut ev = Event::new("speech", "Запишите к хирургу");
        ev.payload = json!({"intent":"book","confidence":0.95,"vars":{"specialty":"хирург","patient_id":99,"slot_id":77,"has_referral":true}});
        let actions = s.handle(ev);
        assert_eq!(s.state_name(), "identity.name");
        assert!(!s.vars.contains_key("patient_id"));
        assert!(!s.vars.contains_key("slot_id"));
        assert!(!actions.iter().any(|a| a.kind == "call"));
    }
    #[test]
    fn changed_offer_invalidates_consent() {
        let mut s = Session::new("inbound", Map::new()).unwrap();
        s.vars = json!({"patient_id":1,"slot_id":5,"specialty":"хирург"})
            .as_object()
            .unwrap()
            .clone();
        s.verified_patient = Some(json!(1));
        s.verified_slot = Some(json!(5));
        s.approval = Some(s.fingerprint("book"));
        assert!(s.write_allowed("book"));
        s.vars.insert("slot_id".into(), json!(6));
        assert!(!s.write_allowed("book"));
    }
    #[test]
    fn linked_targets_equal_reference_dispatch() {
        for n in nodes() {
            for e in &n.edges {
                let (f, st) = e.target.split_once('.').unwrap_or((&n.flow, &e.target));
                assert_eq!(generated::state_index(f, st), Some(e.target_index));
            }
        }
    }
    #[test]
    fn back_does_not_replay_write() {
        let mut s = Session::new("inbound", Map::new()).unwrap();
        s.start();
        s.handle(Event::new("dtmf", "1"));
        assert_eq!(s.state_name(), "identity.name");
        let mut back = Event::new("speech", "Вернуться назад");
        back.payload = json!({"intent":"back"});
        let actions = s.handle(back);
        assert_eq!(s.state_name(), "inbound.welcome");
        assert!(!actions.iter().any(|a| a.kind == "call"));
    }
}

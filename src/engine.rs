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
        })
    }
    pub fn state_name(&self) -> String {
        format!("{}.{}", self.flow, self.state)
    }
    pub fn start(&mut self) -> Vec<Action> {
        self.enter(self.state.clone(), false)
    }
    fn current(&self) -> &'static Node {
        node(&self.flow, &self.state).expect("compiled target")
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
        self.enter(e.target.clone(), same)
    }
    fn enter(&mut self, reference: String, same: bool) -> Vec<Action> {
        if let Some((flow, state)) = reference.split_once('.') {
            self.flow = flow.into();
            self.state = state.into();
        } else {
            self.state = reference;
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
        self.digits.clear();
        self.ask = None;
        self.pending = None;
        self.listening = false;
        let mut actions = vec![];
        for st in &n.statements {
            let a = &st.args;
            match st.kind.as_str() {
                "set" => self.set(&a["var"], self.value(&a["value"])),
                "say" => { let mut action = Action::new("say", json!({"scenario_state": self.state_name()})); action.text = self.render(&st.text);
                    action.rate = a.get("rate").and_then(|s| s.parse().ok()).unwrap_or(n.rate); actions.push(action); }
                "sms" => actions.push(Action::new("sms", json!({"template": a["template"], "vars": self.vars}))),
                "transfer" => actions.push(Action::new("transfer", json!({"reason": self.value(a.get("reason").map(String::as_str).unwrap_or("$reason")), "vars": self.vars, "history": self.history}))),
                "call" => {
                    self.sequence += 1; self.pending = Some(self.sequence);
                    let mut args = Map::new();
                    for (k, v) in a { if let Some(arg) = k.strip_prefix("arg:") { args.insert(arg.into(), self.value(v)); } }
                    actions.push(Action { request_id: self.pending, ..Action::new("call", json!({"module": a["module"], "op": a["op"], "args": args})) });
                }
                "ask" => { self.ask = Some(a.clone()); actions.push(Action::new("timer", json!({"seconds": a.get("timeout").and_then(|s| s.parse::<f64>().ok()).unwrap_or(60.0)}))); }
                "listen" => { self.listening = true; actions.push(Action::new("timer", json!({"seconds": a["timeout"].parse::<f64>().unwrap_or(8.0)}))); }
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
        actions
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
            .map(|e| self.follow(e))
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
                if !v.is_null() {
                    self.set(k, v.clone());
                }
            }
        }
        if matches!(
            intent.as_str(),
            "emergency" | "complaint" | "medical" | "operator"
        ) {
            return self.escalate("human_only");
        }
        if self
            .vars
            .get("doctor_surname")
            .is_some_and(|v| !text(v).is_empty())
        {
            return self.escalate("specific_doctor");
        }
        // On the initial 60-second DTMF fallback speech goes to a person, as requested.
        if self.state == "listen_free" {
            return self.escalate("speech_after_dtmf_timeout");
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
            && intent == "cancel"
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
                "any" => true,
                _ => false,
            };
            if matches {
                if e.kind == "digits" {
                    self.set("said_digits", ev.payload["digits"].clone());
                }
                return self.follow(e);
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

#[cfg(test)]
mod tests {
    use super::*;
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
        s.enter("start".into(), false);
        s.handle(Event::new("dtmf", "0"));
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
        s.enter("start".into(), false);
        s.handle(Event::new("dtmf", "0"));
        let mut ev = Event::new("speech", "Да, отмените.");
        ev.payload = json!({"intent":"cancel","yes_no":false,"affirmative_action":true});
        let actions = s.handle(ev);
        assert!(
            actions
                .iter()
                .any(|a| a.kind == "call" && a.args["op"] == "cancel")
        );
    }
}

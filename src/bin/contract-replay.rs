//! Deterministic probe for the real compiled scenario engine. No network or LLM.
use clinic_voice::engine::{Event, Session, scenario_revision};
use serde_json::{Value, json};
use std::io::{self, Read};

fn replay(input: &Value) -> Result<Value, String> {
    let flow = input["flow"].as_str().unwrap_or("inbound");
    let vars = input["vars"].as_object().cloned().unwrap_or_default();
    let mut session = Session::new(flow, vars)?;
    let actions = session.start();
    let mut frames = vec![json!({"state":session.state_name(), "actions":actions,
        "vars":session.vars,"status":session.status,"finished":session.finished,
        "pending":session.pending})];
    for step in input["events"]
        .as_array()
        .ok_or("events must be an array")?
    {
        let mut event = Event::new(
            step["kind"].as_str().ok_or("event kind required")?,
            step["value"].as_str().unwrap_or(""),
        );
        event.payload = step.get("payload").cloned().unwrap_or(json!({}));
        event.request_id = if step["correlate"] == true {
            session.pending
        } else {
            step["request_id"].as_u64()
        };
        let actions = session.handle(event);
        frames.push(json!({"state":session.state_name(), "actions":actions,
            "vars":session.vars,"status":session.status,"finished":session.finished,
            "pending":session.pending}));
    }
    Ok(json!({"scenario_revision":scenario_revision(),"frames":frames}))
}

fn main() {
    let result = (|| {
        let mut raw = String::new();
        io::stdin()
            .take(1024 * 1024)
            .read_to_string(&mut raw)
            .map_err(|e| e.to_string())?;
        let input: Value = serde_json::from_str(&raw).map_err(|e| e.to_string())?;
        replay(&input)
    })();
    match result {
        Ok(output) => println!("{output}"),
        Err(error) => {
            eprintln!("{}", json!({"error":error}));
            std::process::exit(1);
        }
    }
}

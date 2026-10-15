use clinic_voice::nlu::{Automaton, classify_rules, plan};
use std::{hint::black_box, time::Instant};
fn main() {
    let corpus = [
        "запишите к терапевту",
        "отмените запись",
        "перенести на завтра",
        "подтверждаю приём",
        "узнать расписание",
        "жалоба на очередь",
        "да, у меня боль",
        "позовите оператора",
        "непонятный ответ",
    ];
    let a = Automaton::default();
    for s in corpus {
        assert_eq!(classify_rules(s), a.classify(s));
    }
    let n = 100_000;
    let start = Instant::now();
    for i in 0..n {
        let s = black_box(corpus[i % corpus.len()]);
        black_box(plan(classify_rules(s), Some(1), Some("терапевт")));
    }
    let rules = start.elapsed();
    let start = Instant::now();
    for i in 0..n {
        let s = black_box(corpus[i % corpus.len()]);
        black_box(plan(a.classify(s), Some(1), Some("терапевт")));
    }
    let dfa = start.elapsed();
    println!(
        "{}",
        serde_json::json!({"iterations":n,"corpus":corpus.len(),"rules_ns_per_request":rules.as_nanos()/n as u128,"automaton_ns_per_request":dfa.as_nanos()/n as u128,"automaton_speedup":rules.as_secs_f64()/dfa.as_secs_f64(),"scope":"same vocabulary and SQL plan; synthetic text; excludes ASR and network"})
    );
}

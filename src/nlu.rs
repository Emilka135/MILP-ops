//! Two equivalent classifiers over a small audited vocabulary, plus parameterised SQL.
//! The automaton is a character trie of stems, not a trained or generative model.
use serde::Serialize;
use serde_json::{Value, json};
use std::collections::BTreeMap;

const STEMS: &[(&str, &str)] = &[
    ("бол", "emergency"),
    ("задыха", "emergency"),
    ("кровотеч", "emergency"),
    ("жалоб", "complaint"),
    ("лекарств", "medical"),
    ("таблет", "medical"),
    ("симптом", "medical"),
    ("оператор", "operator"),
    ("регистратор", "operator"),
    ("отмен", "cancel"),
    ("перен", "reschedule"),
    ("подтвер", "confirm"),
    ("запи", "book"),
    ("расписан", "schedule"),
    ("работае", "info"),
];
const ORDER: &[&str] = &[
    "emergency",
    "complaint",
    "medical",
    "operator",
    "cancel",
    "reschedule",
    "confirm",
    "book",
    "schedule",
    "info",
];
fn tokens(text: &str) -> Vec<String> {
    text.to_lowercase()
        .replace('ё', "е")
        .split(|c: char| !c.is_alphanumeric())
        .filter(|s| !s.is_empty())
        .map(String::from)
        .collect()
}
fn choose(found: &[&str]) -> &'static str {
    ORDER
        .iter()
        .copied()
        .find(|x| found.contains(x))
        .unwrap_or("unknown")
}
pub fn classify_rules(text: &str) -> &'static str {
    let words = tokens(text);
    let found: Vec<_> = STEMS
        .iter()
        .filter(|(stem, _)| words.iter().any(|w| w.starts_with(stem)))
        .map(|(_, intent)| *intent)
        .collect();
    choose(&found)
}
#[derive(Default)]
struct Trie {
    next: BTreeMap<char, Trie>,
    intents: Vec<&'static str>,
}
pub struct Automaton(Trie);
impl Default for Automaton {
    fn default() -> Self {
        let mut root = Trie::default();
        for (stem, intent) in STEMS {
            let mut n = &mut root;
            for c in stem.chars() {
                n = n.next.entry(c).or_default();
            }
            n.intents.push(intent);
        }
        Self(root)
    }
}
impl Automaton {
    pub fn classify(&self, text: &str) -> &'static str {
        let mut found = vec![];
        for word in tokens(text) {
            let mut n = &self.0;
            for c in word.chars() {
                if let Some(next) = n.next.get(&c) {
                    n = next;
                    found.extend(&n.intents);
                } else {
                    break;
                }
            }
        }
        choose(&found)
    }
}
#[derive(Debug, Serialize)]
pub struct SqlPlan {
    pub intent: String,
    pub sql: String,
    pub params: Value,
    pub requires_confirmation: bool,
}
/// SQL plans are read-only. Writes can only happen through CRM after dialogue confirmation.
pub fn plan(intent: &str, patient_id: Option<i64>, specialty: Option<&str>) -> SqlPlan {
    let sql = match intent {
        "cancel" | "reschedule" | "confirm" => {
            "SELECT id, starts_at, status FROM bookings WHERE patient_id=:patient_id AND status='booked' ORDER BY starts_at"
        }
        "book" | "schedule" => {
            "SELECT s.id, s.starts_at FROM slots s JOIN doctors d ON d.id=s.doctor_id WHERE s.status='free' AND d.active=1 AND d.specialty=:specialty ORDER BY s.starts_at LIMIT 50"
        }
        _ => "SELECT 1 WHERE 0",
    };
    SqlPlan {
        intent: intent.into(),
        sql: sql.into(),
        params: json!({"patient_id":patient_id,"specialty":specialty}),
        requires_confirmation: matches!(intent, "cancel" | "reschedule" | "confirm" | "book"),
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn routes_agree_and_preserve_safety() {
        let a = Automaton::default();
        for (text, want) in [
            ("отмените запись", "cancel"),
            ("перенести запись", "reschedule"),
            ("запишите, у меня боль", "emergency"),
            ("жалоба отмените запись", "complaint"),
            ("какую таблетку принять", "medical"),
            ("неизвестно", "unknown"),
        ] {
            assert_eq!(classify_rules(text), want);
            assert_eq!(a.classify(text), want);
        }
    }
    #[test]
    fn sql_never_interpolates_input() {
        let p = plan("book", Some(1), Some("'; DROP TABLE slots;--"));
        assert!(!p.sql.contains("DROP"));
        assert!(p.sql.starts_with("SELECT"));
        assert!(p.requires_confirmation);
    }
}

use clinic_voice::engine::benchmark_dispatch;
use serde_json::json;
fn main() {
    let count = 10_000_000u64;
    benchmark_dispatch(false, 100_000);
    benchmark_dispatch(true, 100_000);
    let mut results = vec![];
    for round in 0..7 {
        for linked in if round % 2 == 0 {
            [false, true]
        } else {
            [true, false]
        } {
            let (ns, checksum) = benchmark_dispatch(linked, count);
            results.push(json!({"round":round,"representation":if linked {"linked_index"} else {"generated_match"},"iterations":count,"elapsed_ns":ns,"ns_per_step":ns as f64/count as f64,"checksum":checksum}));
        }
    }
    println!(
        "{}",
        json!({"scope":"Immutable compiled scenario graph dispatch; excludes NLU, ASR, TTS and RPC","build":"cargo --release","runs":results})
    );
}

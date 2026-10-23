use std::{env, process::Command};
fn main() {
    println!("cargo:rerun-if-changed=scenarios");
    println!("cargo:rerun-if-changed=tools/compile_rust.py");
    println!("cargo:rerun-if-changed=runtime/src/calls/scenario/dsl.py");
    let out = env::var("OUT_DIR").unwrap();
    let status = Command::new(env::var("PYTHON").unwrap_or_else(|_| "python3".into()))
        .args(["tools/compile_rust.py", &out])
        .status()
        .expect("Python 3.11+ required at build time");
    assert!(status.success(), "scenario compilation failed");
}

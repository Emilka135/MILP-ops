FROM rust:1.98-slim-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY Cargo.toml Cargo.lock build.rs ./
COPY src ./src
COPY tools/compile_rust.py ./tools/compile_rust.py
COPY runtime/src/calls/scenario ./runtime/src/calls/scenario
COPY runtime/src/calls/__init__.py ./runtime/src/calls/__init__.py
COPY scenarios ./scenarios
RUN cargo build --locked --release --bin clinic-voice

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && rm -rf /var/lib/apt/lists/* && useradd -u 10001 -m clinic
COPY --from=build /build/target/release/clinic-voice /usr/local/bin/clinic-voice
ENV BIND=0.0.0.0:8080
USER clinic
ENTRYPOINT ["clinic-voice"]

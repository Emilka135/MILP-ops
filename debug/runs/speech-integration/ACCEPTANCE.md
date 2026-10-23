# Integrated speech acceptance

Source branches: speech17faba0/f2de5b4 plus reviewed CRM/identity baseline hooks from retained stash. Fixed Rust single-word rejection and trusted identity_answer result; test exercises actual compiled FSM with correlated module responses, not actual ASR.

- Host Python:158 passed.
- Offline Docker actual E5 + SQLite:63 passed (before added CRM dispatch/model guards; those host tests passed separately).
- Actual act controls:77+32+27 passed; Rust all23 passed including20 concurrent queue sessions. Four new generative-API guard cases separately host passed after control run.
- Production CRM Docker build passed with host network after bridge DNS failure; isolated image canonical MIS draft and identity dispatch passed.
- Actual CPU Silero/context-stress doctor name bank smoke passed. Audible native/phone WAV retained under actual-tts/. Naturalness not measured.
- Strict frozen corpus ASR remains15/18 from agent report, unchanged; full audio concurrency and target/archive replay are not accepted by these tests.

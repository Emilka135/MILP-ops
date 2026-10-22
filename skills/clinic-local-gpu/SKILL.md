---
name: clinic-local-gpu
description: Build and verify the clinic voice assistant locally in Docker with NVIDIA GPU speech inference; use for local launch, GPU configuration, audio acceptance, and recovery after moving its checkout.
---

# Local clinic GPU build

The user requires a working local Docker copy using their GPU. The clinic project, model weights and debug evidence must stay usable without a VM password. Locate the checkout through the current workspace and Git; it may move. Never hard-code an old absolute path in launchers, signing wrappers, bind mounts or cache overrides.

Read AGENTS.md, TODO.md and docs/acceptance/GOAL.md in the checkout. Preserve unfinished modules and existing user edits. Finish modules separately, verify them, then make signed commits and push the selected publication branch when existing credentials suffice. Current publication is feature/score-acceptance; the older agent branches are merged and detached worktrees preserve their remaining drafts.

Before model downloads or builds run tools/check_space.py. The user's latest instruction (22 October 2026) requires more than 5 GiB free on both /home and /. This supersedes the earlier 10 GiB /home reserve. Use only this project's caches on /mnt/docker_space when necessary. Never prune other Docker resources or alter another GPU process.

Measure the actual GPU, driver, free VRAM, NVIDIA Container Toolkit and Docker runtime. Configure the speech container explicitly for CUDA and bound its memory/concurrency. A provider listed as available is insufficient: a real audio inference must execute CUDA kernels, and the acceptance report must record the actual provider/device. Fail clearly if requested CUDA cannot initialize instead of silently reporting CPU as GPU.

Use the checked model files already present before downloading replacements. Keep CPU and GPU requirements separate; do not install onnxruntime and onnxruntime-gpu together. Check current primary documentation for CUDA/cuDNN compatibility. Prerecorded TTS may remain on CPU when GPU ASR is the measured chosen configuration; document exactly what runs where. Do not equate GPU use with fixing stress or acoustic artifacts.

Validate real TTS -> audible WAV -> actual ASR and the scenario result; never substitute silence. Test speech, DTMF and mixed input separately. Store every run under debug/runs/<clear-name>/ including actual audio, result JSON and screenshots. Verify the observer's microphone test in a browser. ASR correctness does not prove natural prosody: provide high-quality listening examples and preserve failed comparisons.

Use silero-stress on complete Russian utterances, preserving explicit reviewed pronunciations. Maintain prerecorded doctor full names from the active doctor directory, invalidated by directory/model/voice changes. For regression calls use a versioned synthetic WAV corpus with frozen voice/rate choices across 0.75–2.5; Actions verifies and replays it without regenerating it. Read the delivery skill's [test and recovery requirements](../clinic-module-delivery/references/score-acceptance.md) when changing this corpus or runtime error handling.

The launch command must print the real localhost URL and a stop command that retains data. Mark a backup stub explicitly. Preserve local .env and secrets; demo calls must not mutate the real DBI API even when live keys are saved. Do not require VM/Gitea credentials to complete a local build.

If audio-check exits nonzero, preserve its full report and inspect failed phrases before restarting. ASR can render spoken whole-hour ranges as HH:00; equivalence must retain both bounds, minutes and negation. Keep TTS, editor and compiler on the same quality-policy version, invalidate earlier certificates and rerun actual ASR over the complete current bank. Never bypass the gate or regenerate frozen caller fixtures to clear it.

If a necessary next action requires an unavailable password, passphrase or secret, save the precise progress and blocker to TODO.md without the secret, explain what credential is needed and how to provide it locally, then end the chat turn and wait. Do not keep retrying authentication or continue dependent work after asking.

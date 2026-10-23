---
name: clinic-module-delivery
description: Resume and deliver modules of the clinic voice assistant with explicit acceptance evidence, signed branch commits, persistent unfinished work, and a stop when required credentials are missing.
---

# Deliver the next clinic module

Work from the existing checkout, its AGENTS.md, TODO.md and docs/acceptance/GOAL.md. The user prioritizes a local Docker copy on their GPU. VM deployment is a separate task; do not make it a prerequisite for local acceptance.

Before editing, inspect Git status and preserve other modules' pending work. Fetch and inspect the requested branch before basing new work on it; preserve dirty work before a pull and restore it without dropping its checkpoint. Import presentation-only changes from main separately when requested. Use agents for independent module implementation/review when the user's ongoing request authorizes them. Give each agent its own local branch/worktree, disjoint files and explicit acceptance conditions. Review their outputs rather than treating an agent's summary as proof.

Finish one concrete module, run checks appropriate to its behavior, record actual evidence in debug/runs/<module-name>/, update TODO.md, make a signed commit and push to the user's selected publication branch. Current publication is main; the older agent branches are merged. Preserve detached worktrees and stashes containing unfinished drafts. In shared files stage only the accepted change. Never commit local .env, API keys, model binaries, private keys, or synthetic evidence represented as real results.

For speech changes use actual audible audio and real ASR; test speech, tones and both together independently. For browser changes inspect actual screenshots as well as automated assertions. For Docker changes verify build caching, healthy services, actual URLs, retained data and real device execution. Check Actions for the published SHA, and report pending/failing jobs accurately.

Record skipped requirements and discovered defects in TODO.md before changing focus. Describe what is deployed, what is only a draft, and what has genuinely been tested. Keep the overall goal active while required modules remain unfinished.

For scenario changes, score/CI work, MIS failures, or registrar suggestions, read [score-acceptance.md](references/score-acceptance.md). It records the user's test isolation, fixed speech corpus, night recovery and role-specific editing requirements. Run applicable workflows with act and report actual Actions results on the published SHA; local pytest alone is not workflow acceptance.

## Credential stop

The user explicitly requests that the chat stop when passwords are needed. Prefer already authorized local credentials and avoid asking again for data already supplied. Never print secrets or ask for them in chat when a local file or environment variable suffices.

When the next required action cannot proceed without a password, GPG passphrase, API key or SSH authorization: save progress and the exact blocked action, name the missing access and its local configuration location, and end the turn. Wait for the user to provide or configure access. No background dependent attempts, password guessing or repeated retries. Do not claim completion or successful publication while authentication failed. An optional remote deployment must not block an otherwise authorized local build.

# Native Codex compatibility evidence

This records the 2026-09-15 repair of jStack's native terminal and phone workflows. It does not certify all Claude CLI features or all model-driven skill workflows. An executable test of a hook's JSON producer is insufficient proof that the native model received its output.

## Repaired provider boundaries

- Exact `$jstack:print`, `splitoff`, `takeover`, `tag` and `pict` are intercepted before inference. All five real native commands used zero tokens and produced no assistant/tool items. The attached native TUI also displayed the hook answer.
- Native SessionStart composes shared ancestor instructions, additive `CLAUDE.local.md`, recursive unconditional rules and the first 200 lines of existing project `MEMORY.md`. Native AGENTS discovery replaces the corresponding shared CLAUDE instructions. Config overrides, disabled memory and unavailable source files are covered.
- Native pict previews the actual startup bridge and native instruction discovery. Full output adds source weights, matching on-demand rule bodies and hook inventory. The phone selects the transcript's provider before stale registry metadata. Pict is a startup preview; it excludes the harness, tool schemas, skill catalog, conversation and later edit-hook injections.
- Native patch targets reach nested path rules and the successful-write ledger. The native rule hook emits context without an explicit permission decision: code mode discarded `additionalContext` combined with `permissionDecision: "allow"`. Isolated actual model probes established both outcomes. The same field pair continues to work under the existing Claude contract.
- Managed close preserves native and board identities and waits for CLI exit before review. Native self-write operates on a disposable source fork, preserves the original transcript and carries the source's timestamp. Reviewed offsets advance only on successful completion. Native review settings and manual closed-source lookup are covered.
- Fresh and resumed scheduled jobs retain the native provider, selected native model and durable native IDs. Fresh RPC threads receive a developer history item before CLI resume; resumed work forks the source instead of resuming it concurrently. Actual hook-stopped scheduled runs verified both paths without inference and with an unchanged fork source.
- Native inbox wake detection ignores injected environment messages. Scheduler summaries preserve the deliverable before Stop-hook epilogues. Timeline identity uses the configured seat root. Model-driven work/handoff/audit instructions use available native tools and the bundled spawn adapter.
- Doctor reports bootstrap failures in its documented result shape and accepts a Codex-only installation without creating Codex cache state. Scheduler installation tests recognize an already installed owned service and probe dependencies under the tool's actual HOME. Managed terminal routing uses the exported board identity.

Official [Codex hooks documentation](https://learn.chatgpt.com/docs/hooks) describes context-only PreToolUse output and nested code-mode decisions. The real consumer probes are the acceptance evidence for this installation.

## Evidence levels

The full host suite passed **1,273 tests**, with **six skips** and 11 existing forkpty warnings. All **31 bundled executable shell suites** passed. The table enumerates all **32 registry entries and nine nested entries**; duplicate pict entries are intentional. “Producer” means real scripts with isolated fixtures; “schema” means skill metadata only. Those labels do not imply end-to-end execution of the described skill.

| Registry entry | Bundled verification | Native acceptance evidence |
| --- | --- | --- |
| path-rule-injection | path-rule-injection.sh: producer, dedup, re-fire | Actual startup/edit probe, nested match and model receipt; provider-output regression |
| timeline-injection | timeline-injection.sh: producer | Running thread's injected SessionStart timeline; configured seat tests |
| install-rules | skills.sh: schema | Existing shared rules are consumed by the native bridge; installer skill not executed |
| work | skills.sh: schema | Native SKILL.md loader instructions inspected; whole skill not executed |
| handoff | skills.sh: schema | Bundled native spawn adapter and native guide inspected; whole skill not executed |
| audit | skills.sh: schema | Native tool names and no-delegation path inspected; no audit agent spawned |
| recall | skills.sh: schema | Shared timeline reader; whole skill not executed |
| tag | tag-command.sh: producer | Actual exact native command, zero inference |
| pict (command) | pict-command.sh: producer | Actual exact native command, zero inference |
| splitoff | splitoff-command.sh: producer | Actual exact native command, zero inference; disposable fork identity |
| takeover | takeover-command.sh: producer | Actual exact native command, zero inference; native continuation creation |
| print | print-command.sh: producer | Actual exact native command, zero inference; visible TUI answer |
| pict (renderer) | pict.sh: composed previews | Native startup markers; real phone route provider selection regression |
| timeline-log | log-event.sh: shared executable | Real native self-write row with source timestamp |
| ↳ stop-timeline-remind | stop-timeline-remind.sh: producer | Configured root regression; native review timestamp evidence |
| post-session-review | session-review.sh: producer/configuration | Actual managed-close dispatch and completed native review/self-write |
| push | skills.sh: schema | Native session-files tests; local commit workflow used, no push |
| ↳ session-files | session-files.sh: shared executable | Actual native patch ledger; native transcript extraction tests |
| report | skills.sh: schema | This repair's local commits and concrete report |
| ↳ file-issue | file-issue.sh: mocked remote API | Shared CLI; no live issue filed by this repair |
| elevator | skills.sh: schema | Provider-neutral prose; whole skill not executed |
| issue | skills.sh: schema | Provider-neutral CLI instructions; whole skill not executed |
| ↳ place-issue | place-issue.sh: mocked remote API | Shared CLI; no live board mutation |
| task | skills.sh: schema | Bundled spawn interface inspected; no task agent spawned |
| ↳ task-create | task-create.sh: mocked remote API | Shared CLI; no live issue/task creation |
| showme | skills.sh: schema | Shared artifact adapter; whole skill not executed |
| day-audit | skills.sh: schema | Native tool names inspected; no audit agent spawned |
| root-derivation | root.sh: shared executable | Native startup, configured timeline identity and scheduler probes |
| suite-tree-pin | plugin-pin.sh: interpreter isolation | Suites verify the edited source rather than a cached tree |
| doctor | doctor.sh: shared executable | Codex-only discovery and read-only/bootstrap regressions |
| scheduler | scheduler.sh: shared executable | Actual fresh and source-fork native scheduled runs, zero inference |
| ↳ scheduler-bare-install | scheduler-bare.sh: dependency isolation | Shared scheduler behavior |
| ↳ schedule-self | schedule-self.sh: shared executable | Fresh native engine-booking regression |
| ↳ scheduler-service-install | scheduler-install.sh: dry run | Owned-service and redirected-HOME checks; no service replacement |
| agent-inbox | msg.sh: shared executable | Native wake-marker regression and existing native host inbox tests |
| ↳ inbox-guard | inbox-guard.sh: producer | Native injected-message filtering regression |
| repo-seats | repo-seat.sh: shared executable | Native startup/pict/review seat resolution tests; ACP boundary below |
| commit-identity-guard | commit-identity.sh: isolated git fixture | Actual local commits passed the identity hook |
| identity-scrub | scrub.sh: tracked-source scan | Public source names and paths scanned |
| release-reachability | version-bump.sh: history gate | Matching provider manifests and immutable native cache installation |
| structure-manifest | structure.sh: tracked-source manifest | Repository and plugin root declarations checked |

`manifest.sh` also validates the paired native/Claude manifests; it is the 31st executable suite alongside the 30 unique registry script paths. `skills.sh` checks all 18 shared skills. Native skill discovery is additionally evidenced by the running thread's installed skill catalog.

## Boundaries that prevent a 1:1 Claude CLI claim

The native terminal repair is complete only when the running source has reloaded the installed manifest version and retains both IDs and its startup context. Installed files alone do not prove the active thread has them.

The bundled ACP/Xcode wrapper explicitly launches the Claude ACP adapter. This repair does not turn that Claude integration into a native Codex IDE adapter. Vendor-specific CLI commands, model behavior and harness internals also differ. The model-driven skills listed as schema/inspection checks have not each received a real end-to-end run. These are explicit limits of the evidence; the table must not be presented as universal CLI parity.

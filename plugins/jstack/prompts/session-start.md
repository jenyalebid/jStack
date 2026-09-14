# session-start-inject — fixed prose of the entry injections

Sections are loaded by `hooks/_prompts.py`; runtime values arrive via
`.format()`. Paragraphs are single lines — the hook injects them as-is.

## seat-timeline

Injected on entry by jStack — everything {seat} (your seat) wrote across its last {n} sessions, oldest first. This is your own recent history: you are not starting cold. Build on it — don't re-discover, re-propose, or re-litigate what's already below. A `↳ verdict:` line is the independent review's call on that run — if its note names a move to avoid, pick differently.

## tag-timeline

Injected on entry by jStack — this session is pinned to **{tag}**, so what follows is the last {n} sittings ANY seat had on that subject, oldest first, each line naming who worked it. It is not {seat}'s own history: you are opening a subject, not a seat. Build on it — don't re-discover, re-propose, or re-litigate what's already below. Your own entries are tagged the same way automatically, so what you do here continues this thread. A `↳ verdict:` line is the independent review's call on that run.

## identity

Injected on entry by jStack. Your working directory is a repo that {agent} owns, so you are the {agent} agent working in it — the seat is {agent}/{submode}. Your role files are below: CLAUDE.md walk-up climbs from the working directory and your workspace is a sibling of this checkout, not an ancestor, so it never reaches them. Read them as your own identity, the same as if you had started in the workspace. Where they describe your cockpit as the working directory, that part is the terminal shape — here the working directory is the code.

## updates-head

{n} note{s} sent to {seat} since your last session. Context only — nobody is waiting on any of it, there is nothing to close, and you will not see it again. Act on one only if it changes what you are about to do.

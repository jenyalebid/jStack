---
name: issue
description: Use when working a GitHub issue end to end, or when an issue is assigned to the agent account.
argument-hint: "<owner/repo#N>"
---

# /jstack:issue

`$ARGUMENTS` is `owner/repo#N`. Missing → ask which issue, never guess.

Take the issue to a verified PR. Read the user's current instructions and record the outcome on the issue.

Two contracts hold the machinery together. **The branch is `issue-<N>`**, exactly: closing the issue merges the open PR whose head is `issue-<N>`. And **never switch branches in the shared checkout** — every agent on this machine shares one working tree per repo, so `git checkout -b` moves the branch under other sessions mid-edit. Use a worktree, with no exception for a small change.

## 1. Read the issue, all of it

```bash
gh issue view <N> --repo <owner/repo> --json title,body,labels,state,url,comments
```

Comments matter: on a resume the last one is usually the live instruction.

The type label sets the ask: `bug` = restore the claimed behaviour · `optimization` = same behaviour, faster or clearer · `feature` = build what does not exist. A body naming no symptom, surface, or ask is unactionable — go to **Blocked** rather than guessing.

Cards move process-side on the events you fire anyway. Never place one.

## 2. Get a worktree

Beside the repo's checkout, never inside it. Never judge first-run against resume yourself — `issue-worktree` decides from what is running:

```bash
WT=<your-seat>/pad/issue-<N>
issue-worktree --repo <owner/repo> --issue <N> --path "$WT" --repo-root <repo-root>
cd "$WT"
P=$(jstack-host plan current) && jstack-host plan set "$P" --branch issue-<N> --issue <owner/repo>#<N>
```

Exit 0 hands you the tree. **Exit 3 refuses**: another session holds this issue — **Blocked**.

Repo not on this machine → clone it into the pad and work there.

## 3. Do the work

Ordinary engineering to your own standard: read the code around the change, fix the cause. Run whatever the repo runs and report the real result. A bug fix leaves behind a test that fails without it; where that is genuinely impossible, say why in the comment.

Comment as you go. When the shape of the fix settles, when you change course, when something surprises you — put it on the issue, not in this session's prose. Comment style is a hard rule: digest form — what changed, what's next, what's blocked. A few lines. The user reads these.

Stay inside the issue. Anything else you trip over goes through `/jstack:report` — its own commit or its own issue, never riding along in this PR.

## 4. Commit and open the PR

```bash
git add <your files>
git commit -m "<type>: <what changed>"
git push -u origin issue-<N>
gh pr create --repo <owner/repo> --head issue-<N> --title "<title>" --body "Fixes #<N>

<what changed and why, in a few lines>"
P=$(jstack-host plan current) && jstack-host plan set "$P" --pr <PR URL>
```

`Fixes #<N>` ties the PR to its issue. Without explicit landing authorization, do not merge or close: this installation may treat closing as a merge request. With authorization, run the required gates, merge through the repository's workflow, and verify the requested runtime outcome before closing. Authorization to merge is not authorization to publish a release unless the user requested that too.

## 5. Answer on the issue

```bash
gh issue comment <N> --repo <owner/repo> --body "..."
```

Short and specific: what changed as behaviour rather than a file tour, the PR number, the actual test result, and anything you left undone and why.

For a review-only assignment, the verified PR and issue answer complete the work. For an authorized landing or release, an open PR is intermediate progress, not completion. A later comment resumes the conversation; read it as the live instruction and answer on the issue again.

## Blocked

Cannot proceed — unactionable brief, decision needed, missing credentials, a fix far larger than the issue implies: follow `${CLAUDE_PLUGIN_ROOT}/skills/issue/blocked.md`.

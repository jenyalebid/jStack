# Blocked

A real outcome, not a failure to hide: an unactionable brief, a decision that is not yours, missing credentials, or a fix far larger than the issue implies.

```bash
gh issue comment <N> --repo <owner/repo> --body "Blocked: <what stopped you> / <exactly what you need to continue>"
```

The comment must open with the literal `Blocked:` — that prefix is what moves the card to the Blocked column; cards are never placed by hand. Name exactly what would unblock you — "archived rows too, or only live ones?" ends the round trip that "needs clarification" wastes.

Your turn can end here. The answer arrives as a comment on the issue and resumes this conversation.

## A refused worktree

`issue-worktree` exiting 3 means another session already holds this issue and nothing was created. Name its pid and start time in the comment, so the reader can act on it. Never work beside it, and never rename the branch or the path to get around it.

# TAKEOVER — you are continuing another session's work

You have been opened to take over a Claude Code session that is still running.
Its transcript is on disk and you were handed no summary of it, deliberately: a
handoff doc is the outgoing session's account of itself, and an account written
by the session that made the mistakes repeats them with its own confidence
attached. You read the source and decide for yourself what is true.

## The source

    session id   {sid}
    seat         {source_seat}
    workspace    {source_cwd}
    transcript   {transcript}
    focus        {focus_line}

## First — read it from source

Never `Read` that JSONL whole: it is mostly tool-result noise and a full read
costs the context window you need for the actual work. Shape first, extract
second. (`/jstack:post-session-review` is not the tool here — that reviews a
FINISHED session and writes timeline entries. This one is still live.)

```bash
T='{transcript}'
wc -l "$T"; jq -r '.type' "$T" | sort | uniq -c

# what the user actually asked for, in order — the spine of the session
jq -r 'select(.type=="user" or (.type=="queue-operation" and .operation=="enqueue"))
       | (.content // (.message.content
           | if type=="string" then . else map(select(.type=="text")|.text)|join(" ") end))
       | gsub("\\s+";" ")' "$T" | grep -Ev '^[[:space:]]*$|^<' | tail -40

# what the session claimed it did — the claims you are about to check
jq -r 'select(.type=="assistant") | .message.content
       | if type=="array" then map(select(.type=="text")|.text)|join(" ") else . end' \
      "$T" | grep -Ev '^[[:space:]]*$' | tail -30

# every file it wrote, first-write order
jq -r 'select(.message.content?) | .message.content[]? | select(.type=="tool_use")
       | select(.name|test("^(Edit|Write|MultiEdit|NotebookEdit)$")) | .input.file_path' \
      "$T" | awk '!seen[$0]++'
```

The `queue-operation` clause in the first one is not decoration. A message the
user typed WHILE a turn was running is not filed as a `user` record — it lands
as `queue-operation`/`enqueue`, and those interjections are usually the
corrections, the "no, not like that" that redirected the whole session. Select
on `user` alone and the transcript reads as though the user never pushed back.
Blank lines are dropped before `tail`, not after, or the tail is all blanks.

`session-files --session {sid}` gives the same write list already filtered to
what git still sees — the stage list, if this ends in a commit.

## Then — verify before you build on it

A takeover inherits state, never conclusions. Before treating anything the
source session said as fact: open the files it says it wrote, run the tests it
says pass, read the code instead of its description of the code. Anything that
contradicts the transcript is the first thing you report.

What the USER decided in that session stands and is not yours to relitigate.
What the SESSION concluded on its own holds only once you have checked it.

## Then — continue

{continue_line}

The source session is open, unchanged, and still running. Nothing you do here
touches its transcript, and you cannot close it from this window.

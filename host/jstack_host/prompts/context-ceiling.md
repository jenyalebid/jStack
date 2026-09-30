## marker
HOW TO END THE TURN. If you are stopping mid-work BECAUSE of this notice — parking the docket at a good seam — make `<!-- to-be-continued -->` the LAST line of your final message, alone on that line. That is what triggers the compaction and hands the work back to you: the delivery hook compacts at the seam you picked, then prompts you to carry on from the summary. Only the closing line is read, so mentioning it mid-sentence declares nothing. The marker asks to be RESUMED IN THIS RUN, so the question it answers is whether YOU STOPPED EARLY — not whether work remains in the world. Open issues, follow-ups, a `Next Move` naming what comes later, a backlog you are handing back: none of those are parked work. If you delivered and pushed what you were asked for, you are finished — end the turn normally and write no marker, however much is still on the board. What happens to a finished delivery over the heavy cut is the user's setting, not yours to ask for: it may be compacted, and it is never resumed.

## codex-survives
WHAT SURVIVES. Codex compacts server-side: when the window fills it is rebuilt from the developer prompts, your user's own messages verbatim, and a summary you never see. Every assistant message and every tool result in this session is dropped — what you read, what you ruled out, what you were part-way through. Nothing can steer that summary, so the only place parked work survives is on disk. Commit and push what you changed. Anything not committable — what you ruled out and why, what you were about to do next, a wake you booked — write it into the file or the issue it belongs to before you end the turn. A sentence in your closing message does not survive; a sha does.

## cost
That is the heavy band: every turn from here re-reads several times a fresh session's whole footprint, and most of what it re-reads is spent — dead ends, superseded reads, decisions already made.

## heavy
Finish the unit of work you are on, commit and push it, then END THE TURN. The client's own boundary lands inside your next task; ending is what puts one at a seam instead. Don't open a new thread of work, don't start a broad search, and don't read a large file you could grep. {recovery}

## extreme
CONTEXT — {cur:,} tokens, past the extreme cut. The seam compaction has not taken this session down, so nothing is going to unless you stop.

Nothing new starts now. Write the file you are part-way through, stage and push what you changed, and end the turn. Name anything unfinished in your reply: a summary keeps what you wrote down, not what you were about to do. {recovery}

## codex-heavy
Finish the unit of work you are on, commit and push it, then END THE TURN. Don't open a new thread of work, don't start a broad search, and don't read a large file you could grep. {recovery}

## codex-extreme
CONTEXT — {cur:,} tokens, past the extreme cut. Codex takes its own boundary only with the window nearly full, so it will land inside whatever you are doing then — stopping now is what keeps it off a half-finished unit.

Nothing new starts now. Write the file you are part-way through, stage and push what you changed, and end the turn. {recovery}

## tick-heavy
CONTEXT — {cur:,} tokens, still over the heavy cut. Finish, push, end the turn.

## tick-extreme
CONTEXT — {cur:,} tokens, still past the extreme cut. Nothing is going to take this session down but you.

## recovers
A compaction here lands you around {landing:,} — this session's own {floor:,}-token floor plus the summary — freeing about {freed:,} off every remaining turn.

## recovers-little
Note: this session's fixed overhead is already {floor:,} tokens, so a compaction lands at {landing:,} and frees only about {freed:,}. The weight here is structural — carry on if the work needs it, but keep it tight; compacting is not the lever.

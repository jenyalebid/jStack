Final step before this session ends — the daily timeline. It is also your seat's running memory: the next {source} session boots on the last entries under this source. If this session performed real work (shipped, published, fixed, replied, decided, learned something durable), append ONE entry now, written by you from what you actually did:

  {plugin_bin}/log_event {source} --session {session_id} --origin indirect "<one-line headline, specific and past-tense>" [--detail "<short detail — an open thread, a decision, a do-not-repeat>" ...max 3] [--context "<longer state worth on-demand recall — never injected>"]

Pass --session exactly as written (it binds the entry to this session so the dashboard can reopen it); it stamps now automatically — do not pass --at or --date.

Only if you wrote that entry, file it under its subject so the work is findable across every seat that touched it. Read the shared vocabulary, then set the ONE tag that says what this session was ABOUT:

  {plugin_bin}/log_event tag list
  {plugin_bin}/log_event tag set <name> --session {session_id}

Recurring work is the same subject every run — the tag your last run used is almost always the right answer, and reusing it is the point. Mint one only if nothing on the list plausibly covers this work:

  {plugin_bin}/log_event tag new <name> --description "one line: what work belongs under this"

A tag names a subject many sessions share — never the seat, the date, or a restatement of your headline.

Then stop. If this wake was a no-op (nothing notable happened) or you already appended this session's entry, do nothing and stop. Do not start new work.

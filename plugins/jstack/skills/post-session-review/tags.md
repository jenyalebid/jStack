# Tags in a review

A tag is the subject axis — what a stretch of work was *about*, across seats and
dates. It attaches to the session, so every entry that session wrote, and every one
it writes later, reaches the tag through its session id.

`--session "$SID"` is not optional on any call below. Without it `list` marks your
tags with `●` and `set` files your own session — not the one under review.

## Read — before composing the entry

```bash
log_event tag list --session "$SID"          # the subjects this session is filed under
log_event tail --tag <name> --sessions 5     # each ●, the subject across every seat
```

The seat's last ten are what this cockpit did lately. A `●` tag is what the subject
did — every seat that has worked it, including ones whose timeline you never see.
They fail differently, so read both: restating what another seat already filed under
the same tag is noise, and contradicting one is worth writing only when the entry
says what it supersedes. A session carrying no tag has no subject history to read —
write the entry, then file it.

## Set — after the entry

- A `●` that still fits: filed already. Leave it.
- Otherwise `log_event tag set <name> --session "$SID"`.
- Nothing on the list covers the work: `log_event tag new <name> --description "..."`,
  then set it. Write the description as the boundary of the subject, never a
  restatement of the name — it is what the next session matches against.

A tag names a subject several sessions share, never the seat, the date, or this
session's headline. A near match beats a new tag nearly every time; the vocabulary is
worth sharing only while it stays small. No entry, no tag.

**One subject, not its container too.** Tags are siblings — a second one means the
session did a second thing, never that a wider subject also covers the first. Work on
something that runs on the substrate is not also work on the substrate, however much
of it the work touched. File the narrower subject and stop; the wide tag is what
someone opening the narrow one asked to get away from.

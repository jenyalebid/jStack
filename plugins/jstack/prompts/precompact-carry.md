## asked
The user asked for this compaction with these instructions, and they take priority over
everything below:

## carry
Agent session. Also carry this operational state, concretely — absolute paths and names,
never descriptions ("edited watcher.json to add retry, uncommitted", not "edited the config"):
1. UNCOMMITTED WORK — every file touched, by absolute path, and whether it is committed and
   pushed. In a tree several sessions share, uncommitted is beyond git's reach, so a
   forgotten edit is lost.
2. THE TASK — the user's ask in their framing, what is done, what is owed. Name in-flight work
   as in-flight; a half-written file must be named half-written.
3. WHAT WAS RULED OUT, AND WHY — the reason, not just the verdict. Losing this is what makes a
   session re-propose what it discarded and re-litigate what the user already decided.
4. VERIFICATION DONE — commands run and what they returned; sources read and what they said.
   Flag anything unverified but assumed.
5. BOOKED WAKES AND SENT MESSAGES — scheduled wakes, messages still owed a reply, anything
   already sent. These live outside the session; forgetting one duplicates it.
6. OPEN DEBTS — anything muted, skipped or switched off owed a restoration this session, and
   what turns it back on.

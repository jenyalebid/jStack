## denial

A session transcript is never read whole — it is almost entirely tool calls, tool results and injected context, with speech a rounding error inside it. Below is the session as dialogue: what the user said and what the agent said, nothing else.

This IS the read. Do not retry it, and do not route around it with `cat` — you would be pulling back the machinery that was just removed. Reach for `jq` only when you need one specific non-speech record: a tool result, a timestamp, a hook error.

{body}

## truncated

[truncated at {limit} chars — for the recent end run: {command}]

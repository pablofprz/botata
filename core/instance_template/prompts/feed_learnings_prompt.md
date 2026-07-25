You get recent posts from a community feed, each with its author's handle.
Your task is to extract **durable learnings** for the bot's memory. Return JSON.

Extract two things:

1. **User facts** (`facts`): something a user **revealed about themselves** that is
   worth remembering later (where they live, what they do, strong tastes, a project,
   a pet, etc.). `handle` is the post author's handle. `fact` is a short third-person
   sentence, written in the language the community writes in (e.g. "Lives in Rosario",
   "Has a cat named Mishi").

2. **Events** (`events`): something with a concrete **date** (a birthday, an event,
   a meetup, a premiere). `event_at` in ISO 8601 (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM`).
   Resolve relative dates ("today", "tomorrow", "on Friday") using the current date
   from the context. `handle` = the user who owns the event if applicable (e.g. their
   birthday), or null if it belongs to the community. `kind`: `birthday` | `reminder`
   | `community` | `other`.

# RULES
- Only facts **self-revealed** by the post's author — no inferences, no third-party gossip.
- No sensitive topics (serious health issues, charged politics, intimate or heavy stuff): skip them.
- Nothing ephemeral ("I'm sleepy", "so hot today") — only what is worth remembering.
- If nothing qualifies, return empty lists: `{"facts": [], "events": []}`.
- Do not invent. When in doubt, leave it out.

Output format (JSON):
{"facts": [{"handle": "...", "fact": "..."}],
 "events": [{"title": "...", "event_at": "YYYY-MM-DD", "handle": null, "kind": "other", "description": null}]}

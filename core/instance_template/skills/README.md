# Skills — behavior workspace (T26)

Each `.md` in this folder (except this README) defines a **skill**: topical
instructions the bot loads when the topic applies. Hot-editable — the bot
re-reads the folder on every pass, no restart or redeploy needed.

## Format

```markdown
---
name: community-rules
description: How to answer questions about the community rules
scopes: reply, admin        # optional; default: reply, feed_reflection, admin
enabled: true               # optional; default true
inline: false               # optional; default false
---
Here go the instructions the LLM sees when it loads the skill.
Free-form markdown: lists, tone examples, dos and don'ts.
```

- **name** (required): short identifier, no spaces. It is what the agent passes
  to the `use_skill(name)` tool.
- **description** (required): ONE line saying *when it applies*. It is all the
  agent sees in the index — whether the skill gets used depends on this line.
- **scopes**: in which contexts it is offered — `reply` (answering mentions),
  `feed_reflection` (proactive loop), `admin` (admin commands).
- **inline: true**: the body ALWAYS goes into the system prompt (doesn't wait
  for the agent to request it). For guidance the agent wouldn't know to ask
  for. Use sparingly: it burns context on every call. Note: in `admin` scope
  everything is always inline.
- **enabled: false**: the skill stays off without deleting the file.

## How selection works

The system prompt carries a light index (`name: description` per skill). If the
conversation topic matches, the agent calls the `use_skill(name)` tool and the
body enters the context before generating the reply. *Permanent* behavior
(personality, general tone) belongs in `context/SOUL.md`, not here.

## ⚠️ Security

A skill's body goes straight into the bot's system prompt: **a skill is only as
trustworthy as whoever wrote it**. Only the admin writes in this folder. Don't
paste third-party content here without reading it (malicious instructions in a
`reply`-scoped skill = the public bot obeys them).

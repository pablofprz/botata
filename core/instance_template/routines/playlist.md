---
interval_hours: 24
enabled: false
---
Share a track from the community playlist — the list users build with their
recommendations. Call get_playlist_track to get one (it already avoids recently
shared tracks), then present it with a short comment in your voice.

RULES:
- The comment is about THIS specific track: what it feels like, its vibe, what
  it reminds you of. If you know the artist or the context, use it; if not,
  don't make up facts (no years, albums or anecdotes you don't actually know).
- It's a community recommendation: you may mention that ("from your playlist",
  "someone left this on the list"), but not every time — vary it.
- Include the track link in the post.
- should_post=false only if the tool couldn't bring a track (empty playlist,
  missing authorization). Sharing music is almost always worth it.

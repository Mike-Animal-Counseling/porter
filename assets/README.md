**English** | [中文](README.zh-CN.md)

# Porter Promo Video

`porter-promo.mp4` — Porter's product launch promo (51 seconds, 1280×720, H.264).

## Content outline

1. **Hook** (0–5s): "Switched across 5 AI coding tools, and your memory starts from zero every time?"
2. **What Porter is** (5–13s): you → 27 harnesses → MCP → the Porter memory store — one diagram explains the architecture
3. **Real test** (13–33s): actual `scripts/demo_e2e.py` output — cross-harness memory recall, the pending-review queue, revocation taking effect instantly
4. **Panel tour** (33–57s): slow pans over real UI screenshots — pending-review queue, memory library, auto-sync, memory star map
5. **Close** (57s–): "Porter · your data belongs only to you" + GitHub URL

## Design language

Deep warm-charcoal background + warm-gold `#D4A24E` accent + Chinese serif headlines, bilingual (Chinese/English). No voiceover (muted by default), no AI-generated people or voices — every screen is a real Porter screenshot.

## Files

- `porter-promo.mp4` — final cut (faststart, plays directly in a browser)
- `poster.png` — cover frame
- `render_promo.py` — render script (PIL + ffmpeg, reproducible)

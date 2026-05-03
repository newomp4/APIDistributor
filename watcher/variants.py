"""
Markdown <-> variants serialization.

Format:

    # Section Title
    ---
    First variant text...
    can be multi-line
    ---
    Second variant text...
    ---
    Third variant text

    # Another Section
    ---
    ...

Section headings (H1) map to keys in the channel's youtube config:

    "descriptions" / "description variants"          -> description_variants
    "pinned messages" / "pinned message variants"    -> pinned_message_variants
    "titles" / "title templates" / "title variants"  -> title_template_variants

Within a section, `---` on its own line separates variants. Whitespace between
variants is trimmed. Any leading `---` after the heading is optional.
"""

from __future__ import annotations

SECTION_ALIASES = {
    "description": "description_variants",
    "descriptions": "description_variants",
    "description variants": "description_variants",
    "captions": "description_variants",
    "caption variants": "description_variants",
    "pinned": "pinned_message_variants",
    "pinned message": "pinned_message_variants",
    "pinned messages": "pinned_message_variants",
    "pinned message variants": "pinned_message_variants",
    "title": "title_template_variants",
    "titles": "title_template_variants",
    "title templates": "title_template_variants",
    "title variants": "title_template_variants",
}

DISPLAY_NAMES = {
    "description_variants": "Descriptions",
    "pinned_message_variants": "Pinned Messages",
    "title_template_variants": "Title Templates",
}


def _normalize_section(raw: str) -> str | None:
    return SECTION_ALIASES.get(raw.strip().lower().rstrip("s") + "s") \
        or SECTION_ALIASES.get(raw.strip().lower())


def parse_markdown(text: str) -> dict[str, list[str]]:
    """Parse a markdown variants file into {config_key: [variant, ...]}."""
    sections: dict[str, list[str]] = {}
    current_key: str | None = None
    current_variants: list[str] = []
    buf: list[str] = []

    def flush_variant() -> None:
        if buf:
            joined = "\n".join(buf).strip()
            if joined:
                current_variants.append(joined)
            buf.clear()

    def flush_section() -> None:
        flush_variant()
        if current_key and current_variants:
            sections[current_key] = list(current_variants)

    for line in text.splitlines():
        if line.startswith("# "):
            flush_section()
            heading = line[2:].strip()
            current_key = _normalize_section(heading)
            current_variants = []
            buf = []
        elif line.strip() == "---":
            flush_variant()
        else:
            if current_key is None:
                # Skip preamble before the first heading
                continue
            buf.append(line)
    flush_section()
    return sections


def serialize_markdown(variants: dict[str, list[str]]) -> str:
    """Serialize {config_key: [variant, ...]} back into markdown format."""
    out: list[str] = []
    for key in ("description_variants", "pinned_message_variants", "title_template_variants"):
        items = variants.get(key) or []
        if not items:
            continue
        out.append(f"# {DISPLAY_NAMES[key]}")
        for v in items:
            out.append("---")
            out.append(v.strip())
        out.append("")  # blank line between sections
    return "\n".join(out).strip() + "\n"


def make_ai_prompt(channel_config: dict, channel_name: str) -> str:
    """Build a copy-pastable prompt for ChatGPT/Claude that asks for variants
    in our markdown format. Pulls existing single-value description/pinned as
    examples for style anchoring."""
    yt = channel_config.get("youtube", {})
    example_description = (yt.get("description") or "").strip() or "(no current description)"
    example_pinned = (yt.get("pinned_message") or "").strip() or "(no current pinned message)"
    integration = channel_config.get("social_account") or channel_config.get("integration_name") or channel_name

    return f"""You are helping me generate caption variants for my YouTube channel \"{integration}\" (folder: {channel_name}).

## Current single-value samples (style anchor — match the tone, vary the wording)

CURRENT DESCRIPTION:
{example_description}

CURRENT PINNED MESSAGE (the first line of every description, acts like a pinned comment):
{example_pinned}

## What I need

Produce **50 description variants** and **30 pinned message variants** in the exact markdown format below. Each variant should:
- Hit the same theme as the originals above (preserve the call-to-action / brand voice)
- Vary in wording, sentence structure, hashtag mix, and angle so YouTube doesn't see them as duplicate content
- Stay under YouTube's character limits (descriptions ~100-300 chars, pinned messages <120 chars)
- Avoid repeating the same opening word more than 3 times across variants

## Output format — return EXACTLY this markdown structure, nothing else

# Descriptions
---
[Variant 1 — multi-line OK, hashtags at the end]
---
[Variant 2]
---
... (continue for 50 variants total)

# Pinned Messages
---
[Variant 1 — short, 1-2 emojis OK, single line]
---
[Variant 2]
---
... (continue for 30 variants total)

The triple-dash `---` separators must be on their own line. Do not number variants. Do not include any preamble or commentary outside the markdown.
"""


def merge_into_config(config: dict, parsed: dict[str, list[str]]) -> dict:
    """Merge parsed variants into a channel config, replacing existing variants."""
    yt = dict(config.get("youtube") or {})
    for key, items in parsed.items():
        yt[key] = list(items)
    new_config = dict(config)
    new_config["youtube"] = yt
    return new_config

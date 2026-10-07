"""Read movable coordinator rules for evals; structural budgets still read the agent itself."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def skill_body(text: str) -> str:
    """Strip only leading YAML frontmatter, preserving body fences and thematic breaks."""
    return re.sub(r"\A---\r?\n.*?\r?\n---(?:\r?\n|\Z)", "", text, count=1, flags=re.S).strip()


def rules_text(plugin_root: Path = ROOT) -> str:
    """The agent followed by every skill body in path order, including Claude-only rules.

    No skills means the original agent text, byte for byte. Do not add wrapper headings:
    existing section and one-shot paragraph lints must still find the rules after a move.
    """
    agent = (plugin_root / "agents" / "chief-of-stuff.md").read_text()
    bodies = [skill_body(path.read_text()) for path in sorted((plugin_root / "skills").glob("*/SKILL.md"))]
    return agent + "".join("\n\n" + body for body in bodies)


def sections(text: str) -> dict:
    """`## ` heading -> section text, including its heading. Fenced headings are content;
    repeated headings are an error."""
    out, current, fenced = {}, None, False
    for line in re.findall(r"[^\n]*\n|[^\n]+", text):
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("## "):
            current = line[3:].strip()
            if current in out:
                raise ValueError(f"repeated heading: ## {current}")
            out[current] = ""
        if current:
            out[current] += line
    return out


def one_shot_paragraph(text: str) -> str:
    found = next((p for p in text.split("\n") if p.startswith('If `[workers] mode = "one-shot"`')), None)
    assert found, 'the one-shot paragraph no longer opens with: If `[workers] mode = "one-shot"`'
    return found

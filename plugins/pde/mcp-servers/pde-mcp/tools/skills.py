import json
from pathlib import Path
from typing import Any

import mcp.types as types


_SKILLS_TOOL_NAMES = {
    "list_project_skills",
    "get_project_skill",
}


def definitions() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_project_skills",
            description=(
                "List AI skills bundled with this plugin so MCP clients can discover "
                "available workflows. Skills are loaded from the plugin's skills/ directory."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "include_excerpt": {
                        "type": "boolean",
                        "description": "Include a short excerpt from each skill file.",
                    },
                },
            },
        ),
        types.Tool(
            name="get_project_skill",
            description=(
                "Read a specific plugin skill document from skills/ by skill id. "
                "Use the id returned by list_project_skills."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "Skill id (folder name under the plugin's skills/ directory).",
                    },
                },
                "required": ["skill_id"],
            },
        ),
    ]


def can_handle(name: str) -> bool:
    return name in _SKILLS_TOOL_NAMES


def _skills_root(project_root: Path) -> Path:
    return project_root / "skills"


def _strip_frontmatter(content: str) -> str:
    """Drop a leading YAML frontmatter block (---\\n...\\n---) if present.

    Doesn't parse the YAML itself (no yaml dependency) — SKILL.md frontmatter
    commonly uses multi-line folded scalars (`description: >-`) that a naive
    "description: <rest of this line>" grab would truncate, so it's simpler
    and more robust to just skip the whole block and excerpt real prose.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return content
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[i + 1:])
    return content


def _extract_excerpt(content: str) -> str:
    for line in _strip_frontmatter(content).splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("#"):
            continue
        return text[:240]
    return ""


def _list_skills(project_root: Path, include_excerpt: bool) -> dict[str, Any]:
    root = _skills_root(project_root)
    if not root.exists():
        return {
            "count": 0,
            "skills": [],
            "skills_root": str(root),
            "warning": "Skills directory not found.",
        }

    skills: list[dict[str, Any]] = []
    for skill_dir in sorted(root.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue

        entry: dict[str, Any] = {
            "id": skill_dir.name,
            "path": str(skill_file.relative_to(project_root)),
        }

        if include_excerpt:
            try:
                content = skill_file.read_text(encoding="utf-8")
                entry["excerpt"] = _extract_excerpt(content)
            except Exception as exc:
                entry["excerpt_error"] = f"Failed to read excerpt: {exc}"

        skills.append(entry)

    return {
        "count": len(skills),
        "skills_root": str(root.relative_to(project_root)),
        "skills": skills,
    }


def _safe_skill_file(project_root: Path, skill_id: str) -> Path:
    if not skill_id or "/" in skill_id or "\\" in skill_id or ".." in skill_id:
        raise ValueError("Invalid skill_id. Expected a folder name under the plugin's skills/ directory.")

    skill_file = _skills_root(project_root) / skill_id / "SKILL.md"
    if not skill_file.exists() or not skill_file.is_file():
        raise ValueError(f"Skill not found: {skill_id}")
    return skill_file


def _get_skill(project_root: Path, skill_id: str) -> dict[str, Any]:
    skill_file = _safe_skill_file(project_root=project_root, skill_id=skill_id)
    content = skill_file.read_text(encoding="utf-8")
    return {
        "skill_id": skill_id,
        "path": str(skill_file.relative_to(project_root)),
        "content": content,
    }


def handle(name: str, arguments: dict[str, Any], project_root: Path) -> dict[str, Any]:
    if name == "list_project_skills":
        return _list_skills(project_root=project_root, include_excerpt=arguments.get("include_excerpt", True))

    if name == "get_project_skill":
        return _get_skill(project_root=project_root, skill_id=arguments["skill_id"])

    raise ValueError(f"Unknown skills tool: {name}")


def as_text_content(payload: dict[str, Any]) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]

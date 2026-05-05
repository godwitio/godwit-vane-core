from typing import Mapping, TypeVar


T = TypeVar("T")


def scope_projects(projects: Mapping[str, T],
                   selected_project: str | None) -> dict[str, T]:
    """Return either the full project map or a single named project."""
    selected = (selected_project or "").strip()
    if not selected:
        return dict(projects)

    if selected not in projects:
        available = ", ".join(sorted(projects)) or "(none)"
        raise ValueError(
            f"Unknown project {selected!r}. Available projects: {available}"
        )

    return {selected: projects[selected]}

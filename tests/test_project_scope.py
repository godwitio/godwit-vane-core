import pytest

from project_scope import scope_projects


def test_scope_projects_returns_all_when_none_selected():
    projects = {"godwit": 1, "marcado": 2}

    assert scope_projects(projects, None) == projects


def test_scope_projects_returns_all_when_blank_selected():
    projects = {"godwit": 1, "marcado": 2}

    assert scope_projects(projects, "   ") == projects


def test_scope_projects_returns_single_selected_project():
    projects = {"godwit": 1, "marcado": 2}

    assert scope_projects(projects, "godwit") == {"godwit": 1}


def test_scope_projects_rejects_unknown_project():
    projects = {"godwit": 1, "marcado": 2}

    with pytest.raises(ValueError, match="Unknown project 'unknown'"):
        scope_projects(projects, "unknown")

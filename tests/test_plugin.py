"""Load the plugin the way Hermes does and check registration against the manifest."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


class RecordingContext:
    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}
        self.skills: dict[str, Path] = {}

    def register_tool(self, *, name, toolset, schema, handler, **kwargs) -> None:
        assert toolset == "beam"
        assert schema["name"] == name
        self.tools[name] = {"schema": schema, "handler": handler}

    def register_skill(self, name, path, *args, **kwargs) -> None:
        self.skills[name] = Path(path)


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "hermes_plugin_beam", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def manifest_list(key: str) -> list[str]:
    lines = (ROOT / "plugin.yaml").read_text().splitlines()
    start = lines.index(f"{key}:") + 1
    items = []
    for line in lines[start:]:
        if not line.startswith("  - "):
            break
        items.append(line[4:].strip())
    return items


@pytest.fixture(scope="module")
def context() -> RecordingContext:
    ctx = RecordingContext()
    load_plugin().register(ctx)
    return ctx


def test_registered_tools_match_the_manifest(context: RecordingContext) -> None:
    assert sorted(context.tools) == sorted(manifest_list("provides_tools"))


def test_skill_is_registered_with_valid_frontmatter(context: RecordingContext) -> None:
    path = context.skills["beam"]
    text = path.read_text()
    frontmatter = text.split("---", 2)[1]
    assert len(frontmatter) < 4000
    description = re.search(r'^description: "(.*)"$', frontmatter, re.MULTILINE).group(1)
    assert description.startswith("Use when")
    assert len(description) <= 1024
    assert re.search(r"^name: beam$", frontmatter, re.MULTILINE)


def test_versions_agree() -> None:
    from beam_hermes import __version__

    manifest = (ROOT / "plugin.yaml").read_text()
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert f"version: {__version__}\n" in manifest
    assert f'version = "{__version__}"' in pyproject


def test_every_handler_returns_json_for_empty_arguments(context: RecordingContext, data_dir: Path) -> None:
    for name, entry in context.tools.items():
        result = entry["handler"]({}, task_id="t1", session_id="s1")
        payload = json.loads(result)
        assert "error" in payload, name
        assert payload["kind"] in {"invalid_argument", "missing_env", "confirmation_required"}, name


def test_schemas_are_well_formed(context: RecordingContext) -> None:
    for name, entry in context.tools.items():
        schema = entry["schema"]
        assert schema["description"], name
        parameters = schema["parameters"]
        assert parameters["type"] == "object"
        for required in parameters["required"]:
            assert required in parameters["properties"], (name, required)

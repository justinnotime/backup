"""Portable manifests retain explicit ownership and confinement."""

import json
from pathlib import Path

import jsonschema
import pytest

from agent_skills.sessions.manifest import ManifestError, load_manifest
from agent_skills.sessions.sources import SourceAccessError, validate_configured_path
from session_test_support import manifest_data, write_manifest


def test_expansion_uses_only_supplied_environment_and_keeps_policy_literals(tmp_path):
    home = tmp_path / "chosen home"
    source = home / "sessions"
    output = home / "output"
    source.mkdir(parents=True)
    data = manifest_data(source, output)
    data["expand_environment"] = True
    data["sources"][0]["path"]["value"] = "~/sessions"
    policy = data["sources"][0]["root_policy"]
    policy["allowed_lexical_roots"] = ["$HOME"]
    policy["allowed_resolved_roots"] = ["${HOME}"]
    data["output"]["repository_root"] = "${DESTINATION}"
    data["project_policy"]["aliases"] = {"$UNSET_LITERAL": "example"}
    path = write_manifest(tmp_path / "manifest.json", data)
    schema = json.loads((Path(__file__).parents[1] / "schemas/manifest-v1.json").read_text())
    jsonschema.validate(data, schema)
    manifest = load_manifest(path, environ={"HOME": str(home), "DESTINATION": str(output)})
    assert manifest.output.repository_root == output
    assert manifest.sources[0].path == source
    assert manifest.project_policy.aliases == {"$UNSET_LITERAL": "example"}
    assert validate_configured_path(manifest.sources[0]).resolved == source
    with pytest.raises(ManifestError, match="environment reference"):
        load_manifest(path, environ={"HOME": str(home)})


@pytest.mark.parametrize("via_symlink", [False, True])
def test_forbidden_component_patterns_cover_lexical_and_resolved_paths(tmp_path, via_symlink):
    forbidden = tmp_path / "excluded-example"
    forbidden.mkdir()
    source = tmp_path / "selected"
    source.symlink_to(forbidden, target_is_directory=True)
    data = manifest_data(source if via_symlink else forbidden, tmp_path / "output")
    policy = data["sources"][0]["root_policy"]
    policy.update(allowed_lexical_roots=[str(tmp_path)], allowed_resolved_roots=[str(tmp_path)],
                  symlinks="confined", forbidden_component_patterns=["excluded-*"])
    manifest = load_manifest(write_manifest(tmp_path / "manifest.json", data), environ={})
    with pytest.raises(SourceAccessError, match="forbidden path component"):
        validate_configured_path(manifest.sources[0])


@pytest.mark.parametrize("inside", [False, True])
def test_private_manifest_location_policy(tmp_path, inside):
    repository = tmp_path / "repository"
    repository.mkdir()
    data = manifest_data(tmp_path / "source", repository)
    data["require_external_config"] = True
    original = write_manifest(tmp_path / "manifest.json", data)
    selected = repository / "manifest.json" if inside else tmp_path / "linked.json"
    if inside:
        selected.write_bytes(original.read_bytes())
    else:
        selected.symlink_to(original)
    with pytest.raises(ManifestError, match="external non-symlink"):
        load_manifest(selected, environ={})

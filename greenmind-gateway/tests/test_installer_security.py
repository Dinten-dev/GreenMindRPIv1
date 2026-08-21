from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1] / "install-gateway.sh"


def test_installer_requires_an_immutable_repository_revision():
    script = INSTALLER.read_text(encoding="utf-8")

    assert "^[0-9a-fA-F]{40}$" in script
    assert 'checkout --quiet --detach --force "${REPO_REVISION}"' in script
    assert "rev-parse HEAD" in script
    assert "REPO_BRANCH" not in script
    assert "origin/master" not in script
    assert "reset --hard" not in script


def test_installer_uses_only_the_hash_locked_python_dependencies():
    script = INSTALLER.read_text(encoding="utf-8")

    assert script.count("--require-hashes") == 2
    assert "pip install --upgrade" not in script
    assert "httpx==" not in script
    assert "raw.githubusercontent.com" not in script
    assert "| sudo bash" not in script

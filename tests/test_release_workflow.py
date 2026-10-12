"""Execute release scripts in /tmp with a fake gh; never publish or contact GitHub."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReleaseWorkflow(unittest.TestCase):
    def test_build_publication_retry_and_published_version_protection(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text()
        scripts = []
        current = None
        for line in workflow.splitlines():
            if line.strip() == "run: |":
                current = []
                scripts.append(current)
            elif current is not None and line.startswith("          "):
                current.append(line[10:])
            else:
                current = None
        self.assertEqual(len(scripts), 2)
        build, publish = ["\n".join(lines) for lines in scripts]
        with tempfile.TemporaryDirectory(prefix="ksq-release-check-") as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "ksq", root / "ksq", ignore=shutil.ignore_patterns("__pycache__"))
            (root / "deploy").mkdir()
            shutil.copy2(ROOT / "deploy/build_app_bin.py", root / "deploy/build_app_bin.py")
            shutil.copy2(ROOT / "deploy/make_package.sh", root / "deploy/make_package.sh")
            shutil.copytree(ROOT / "deploy/standalone", root / "deploy/standalone")
            shutil.copy2(ROOT / "requirements.txt", root / "requirements.txt")
            fakebin = root / "fakebin"
            fakebin.mkdir()
            (fakebin / "python3").symlink_to(sys.executable)
            gh = fakebin / "gh"
            gh.write_text("#!" + sys.executable + "\n" + '''
import json, os, sys
from pathlib import Path
state_file = Path("release-fixture.json")
state = json.loads(state_file.read_text()) if state_file.exists() else {}
arguments = sys.argv[1:]
assert arguments[0] == "release"
assert arguments[2] == os.environ["GITHUB_REF_NAME"]
assert arguments[arguments.index("--repo") + 1] == os.environ["GITHUB_REPOSITORY"]
operation = arguments[1]
if operation == "view":
    if not state: raise SystemExit(1)
    print("true" if state["draft"] else "false")
    raise SystemExit(0)
if operation == "create":
    assert not state and "--draft" in arguments and "--verify-tag" in arguments
    state = {"draft": True}
if operation == "upload":
    assert state["draft"] and "--clobber" in arguments
    metadata = json.loads(Path(arguments[-1]).read_text())
    binary = Path(arguments[-2])
    assert binary.name == metadata["filename"] and binary.is_file()
    deployment = Path(arguments[-3])
    assert deployment.name == "ksq_deploy_" + metadata["version"] + ".tar.gz" and deployment.is_file()
    state["assets"] = [deployment.name, binary.name, "ksq_release.json"]
    if os.environ.get("KSQ_SIMULATE_UPLOAD_FAILURE"):
        state_file.write_text(json.dumps(state))
        raise SystemExit(1)
if operation == "edit":
    assert state["draft"] and len(state["assets"]) == 3
    assert "--draft=false" in arguments and "--latest" in arguments
    state["draft"] = False
state_file.write_text(json.dumps(state))
''')
            gh.chmod(0o755)
            environment = dict(os.environ, PATH=str(fakebin) + ":" + os.environ["PATH"],
                               GITHUB_REF_NAME="v88.77.66", GITHUB_REPOSITORY="fixture/example")
            def run(script, **extra):
                return subprocess.run(["bash", "-euo", "pipefail", "-c", script], cwd=root,
                                      env={**environment, **extra}, capture_output=True, text=True, timeout=30)
            built = run(build)
            self.assertEqual(built.returncode, 0, built.stderr)
            release = json.loads((root / "deploy/dist/ksq_release.json").read_text())
            self.assertEqual(release["version"], "v88.77.66")
            self.assertTrue(release["preserve_state"])
            prefix = "ksq_deploy_" + release["version"]
            with tarfile.open(root / "deploy/dist" / (prefix + ".tar.gz")) as package:
                self.assertEqual(package.extractfile(prefix + "/upgrade.sh").read(),
                                 (ROOT / "deploy/standalone/upgrade.sh").read_bytes())
                self.assertEqual(package.extractfile(prefix + "/bin/knowledge_shelf_query.bin").read(),
                                 (root / "deploy/dist" / release["filename"]).read_bytes())
            failed = run(publish, KSQ_SIMULATE_UPLOAD_FAILURE="1")
            self.assertNotEqual(failed.returncode, 0)
            state_file = root / "release-fixture.json"
            self.assertTrue(json.loads(state_file.read_text())["draft"], "Partial uploads must never become public")
            retried = run(publish)
            self.assertEqual(retried.returncode, 0, retried.stderr)
            published = state_file.read_bytes()
            self.assertFalse(json.loads(published)["draft"])
            repeated = run(publish)
            self.assertNotEqual(repeated.returncode, 0)
            self.assertEqual(state_file.read_bytes(), published, "Published versions must never be modified")
            invalid = run(build, GITHUB_REF_NAME="v1.2.3-rc1")
            self.assertNotEqual(invalid.returncode, 0)
            self.assertEqual(json.loads((root / "deploy/dist/ksq_release.json").read_text())["version"], "v88.77.66")


if __name__ == "__main__":
    unittest.main()

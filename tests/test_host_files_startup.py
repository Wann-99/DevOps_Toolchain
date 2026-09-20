"""Offline startup/boot check: python -m tests.test_host_files_startup.

Runs the deployment script with a tiny zip agent and a fake service manager;
all service paths, privilege commands, and state stay inside a temporary tree.
"""

import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import zipfile


AGENT = '''
import json, os, sys
from pathlib import Path
def main():
    action, flag, socket = sys.argv[1:]
    assert flag == "--socket"
    marker = Path(socket)
    with open(os.environ["KSQ_TEST_TRACE"], "a") as stream:
        stream.write(json.dumps(["agent", action, str(marker)]) + "\\n")
    if action in {"start", "serve"}:
        marker.write_text(json.dumps({"user": os.environ.get("KSQ_TEST_SERVICE_USER", "legacy")}))
    elif action == "stop":
        marker.unlink(missing_ok=True)
    elif action == "status":
        print(marker.read_text())
'''


COMMANDS = '''
import json, os, pathlib, pwd, shlex, shutil, subprocess, sys
root = pathlib.Path(os.environ["KSQ_TEST_ROOT"])
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
with open(os.environ["KSQ_TEST_TRACE"], "a") as stream:
    stream.write(json.dumps([name] + args) + "\\n")
if name == "sudo":
    if os.environ.get("KSQ_TEST_DENY_SUDO") == "1":
        raise SystemExit(1)
    if args == ["-v"]:
        raise SystemExit(0)
    raise SystemExit(subprocess.call(args))
if name == "runuser":
    assert args[:1] == ["-u"] and args[2] == "--"
    raise SystemExit(subprocess.call(args[3:]))
if name == "id":
    if args == ["-un"]:
        print(os.environ.get("KSQ_TEST_SERVICE_USER", pwd.getpwuid(os.getuid()).pw_name))
    elif args[:1] == ["-gn"]:
        import grp
        print(grp.getgrgid(pwd.getpwnam(args[1]).pw_gid).gr_name)
    else:
        raise AssertionError(args)
    raise SystemExit(0)
if name == "install":
    if "-d" in args:
        directory = pathlib.Path(args[-1])
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    else:
        assert args[:2] == ["-m", "644"]
        shutil.copyfile(args[-2], args[-1])
    raise SystemExit(0)
assert name == "systemctl", name
state_file = root / "manager.json"
state = json.loads(state_file.read_text()) if state_file.exists() else {"enabled": False, "active": False}
action = args[0]
unit = root / "units/ksq-host-files.service"
def service_command(action):
    lines = dict(line.split("=", 1) for line in unit.read_text().splitlines() if "=" in line)
    command = [word.replace("%%", "%").replace("$$", "$") for word in shlex.split(lines["ExecStart"])]
    assert command[2] == "serve"
    if action == "stop":
        # systemd terminates the service; it does not recursively call this wrapper's stop command.
        (pathlib.Path(command[-1]) / "host.sock").unlink(missing_ok=True)
        return
    command[2] = action
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"SUDO_USER", "KSQ_HOST_FILES_USER", "KSQ_HOST_PYTHON"}}
    for entry in shlex.split(lines["Environment"]):
        key, value = entry.replace("%%", "%").split("=", 1)
        environment[key] = value
    account = pwd.getpwuid(int(lines["User"]))
    environment["KSQ_TEST_SERVICE_USER"] = account.pw_name
    environment["KSQ_TEST_EUID"] = "1000"
    assert environment["HOME"] == account.pw_dir
    # The unit parser owns HOME/cwd; the actual process remains the test runner.
    subprocess.run(command, env=environment, cwd=root, check=True)
if action == "daemon-reload":
    assert unit.is_file()
elif action == "enable":
    state["enabled"] = True
elif action in {"stop", "disable"}:
    if state["active"]:
        service_command("stop")
    state["active"] = False
    if action == "disable":
        assert "--now" in args
        state["enabled"] = False
elif action == "restart":
    assert state["enabled"]
    service_command("serve")
    state["active"] = True
elif action == "test-reboot":
    state["active"] = False
    if state["enabled"]:
        service_command("serve")
        state["active"] = True
else:
    raise AssertionError(args)
state_file.write_text(json.dumps(state))
'''


def check():
    source = Path(__file__).resolve().parents[1] / "deploy/standalone/host-files.sh"
    with tempfile.TemporaryDirectory(prefix="ksq-startup-check-") as temporary:
        root = Path(temporary)
        deployment = root / 'deploy 中文 $HOME %n "quoted" \\slash'
        deployment.mkdir()
        (deployment / "bin").mkdir()
        (root / "units").mkdir()
        booted = root / "systemd-running"
        booted.mkdir()
        script = deployment / "host-files.sh"
        script.write_text(source.read_text().replace("/etc/systemd/system/", str(root / "units") + "/")
                          .replace("/run/systemd/system", str(booted))
                          .replace("${EUID}", "${KSQ_TEST_EUID}"))
        binary = deployment / "bin/knowledge_shelf_query.bin"
        with zipfile.ZipFile(binary, "w") as archive:
            archive.writestr("ksq/__init__.py", "")
            archive.writestr("ksq/web/__init__.py", "")
            archive.writestr("ksq/web/host_files.py", AGENT)
        fake_bin = root / "commands"
        fake_bin.mkdir()
        shim = fake_bin / "command.py"
        shim.write_text("#!" + sys.executable + "\n" + COMMANDS)
        shim.chmod(0o755)
        for name in ("sudo", "runuser", "systemctl", "install", "id"):
            (fake_bin / name).symlink_to(shim)
        python = deployment / "custom python $PY %n"
        python.symlink_to(sys.executable)
        account = pwd.getpwuid(os.getuid())
        # Under root, an existing non-root account exercises SUDO_USER/runuser.
        original = account if account.pw_uid else pwd.getpwnam("nobody")
        trace = root / "trace.jsonl"
        environment = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ["PATH"],
                           KSQ_TEST_ROOT=str(root), KSQ_TEST_TRACE=str(trace), KSQ_TEST_EUID="0",
                           SUDO_USER=original.pw_name, KSQ_HOST_PYTHON="./" + python.name)
        environment.pop("KSQ_HOST_FILES_USER", None)
        environment.pop("KSQ_TEST_SERVICE_USER", None)

        def run(action, expected=0, **changes):
            result = subprocess.run(["/bin/bash", str(script), action, "bin/knowledge_shelf_query.bin", "host-files"],
                                    cwd=deployment, env=dict(environment, **changes), capture_output=True, text=True)
            assert result.returncode == expected, result.stdout + result.stderr
            return result

        def events():
            return [json.loads(line) for line in trace.read_text().splitlines()]

        def manager():
            return json.loads((root / "manager.json").read_text())

        socket = deployment / "host-files/host.sock"
        socket.parent.mkdir(mode=0o700)
        shutil.copyfile(binary, socket.with_name("agent.bin"))
        socket.write_text("legacy")
        run("start", expected=1, KSQ_TEST_EUID="1000", SUDO_USER=account.pw_name, KSQ_TEST_DENY_SUDO="1")
        assert socket.read_text() == "legacy" and not (root / "units/ksq-host-files.service").exists()
        assert not any(event[:2] == ["agent", "stop"] for event in events())
        first = run("start")
        assert "开机自启" in first.stdout and manager() == {"enabled": True, "active": True}
        log = events()
        stop = next(i for i, event in enumerate(log) if event[:2] == ["agent", "stop"])
        enable = next(i for i, event in enumerate(log) if event[:2] == ["systemctl", "enable"])
        serve = next(i for i, event in enumerate(log) if event[:2] == ["agent", "serve"])
        assert stop < enable < serve
        assert json.loads(socket.read_text())["user"] == original.pw_name
        if original.pw_name != "root":
            assert any(event[:3] == ["runuser", "-u", original.pw_name] for event in log)

        # Change the package, then prove an already active service is restarted with it.
        with zipfile.ZipFile(binary, "a") as archive:
            archive.writestr("revision.txt", "updated")
        before = len(log)
        run("start")
        log = events()[before:]
        assert log.index(["systemctl", "stop", "ksq-host-files.service"]) < log.index(["systemctl", "restart", "ksq-host-files.service"])
        assert socket.with_name("agent.bin").read_bytes() == binary.read_bytes()
        socket.unlink()
        subprocess.run([str(fake_bin / "systemctl"), "test-reboot"], env=environment, check=True)
        assert manager()["active"] and json.loads(socket.read_text())["user"] == original.pw_name

        run("stop")
        assert manager() == {"enabled": False, "active": False} and not socket.exists()
        subprocess.run([str(fake_bin / "systemctl"), "test-reboot"], env=environment, check=True)
        assert not manager()["active"] and not socket.exists()
        # Non-root invocation uses only the fake sudo boundary.
        run("start", KSQ_TEST_EUID="1000", SUDO_USER=account.pw_name)
        assert ["sudo", "systemctl", "enable", "ksq-host-files.service"] in events()
        for action in ("start", "stop"):
            before = len(events())
            existing = socket.read_bytes()
            run(action, expected=1, KSQ_TEST_EUID="1000", SUDO_USER=account.pw_name, KSQ_TEST_DENY_SUDO="1")
            assert manager() == {"enabled": True, "active": True} and socket.read_bytes() == existing
            assert not any(event[:2] in (["agent", "stop"], ["systemctl", "stop"], ["systemctl", "disable"])
                           for event in events()[before:])
        run("stop", KSQ_TEST_EUID="1000", SUDO_USER=account.pw_name)
        booted.rmdir()
        before = len(events())
        fallback = run("start")
        assert "无法配置开机自启" in fallback.stderr and socket.exists()
        assert not any(event[0] == "systemctl" for event in events()[before:])
        run("stop")
        assert not socket.exists()
    print("Host files startup: legacy handoff, update, boot, stop, account, quoted paths and fallback passed.")


if __name__ == "__main__":
    check()

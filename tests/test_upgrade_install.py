"""Run the installer only in /tmp, with fake Docker, systemctl and privilege commands."""
import json
import os
from pathlib import Path
import pwd
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class UpgradeInstaller(unittest.TestCase):
    def test_independent_service_preserves_deployment_and_protects_credentials(self):
        with tempfile.TemporaryDirectory(prefix="ksq-install-") as temporary:
            root = Path(temporary)
            deployment = root / '设备 " $test%'
            deployment.mkdir()
            directory = deployment / "upgrade"
            directory.mkdir()
            environment_file = directory / "service.env"
            environment_file.write_text("KSQ_UPGRADE_GITHUB_TOKEN=fixture-private-value\nCONFIG_PNP_DIR=/old\n")
            binary = deployment / "application.bin"
            with zipfile.ZipFile(binary, "w") as archive:
                archive.writestr("ksq/__init__.py", "")
                archive.writestr("ksq/upgrade.py", (ROOT / "ksq/upgrade.py").read_bytes())
            unit = root / "ksq-upgrade.service"
            fakebin = root / "fakebin"
            fakebin.mkdir()

            def executable(name, text):
                path = fakebin / name
                path.write_text("#!" + sys.executable + "\n" + text)
                path.chmod(0o755)

            executable("systemctl", """
import os, socket, sys
from pathlib import Path
root = Path(os.environ['KSQ_INSTALL_FIXTURE'])
with (root / 'systemctl.calls').open('a') as log: log.write(' '.join(sys.argv[1:]) + '\\n')
if sys.argv[1] == 'show': print(os.getuid())
if sys.argv[1:3] == ['enable', '--now']:
    directory = next(root.glob('设备 */upgrade'))
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    with socket.socket(socket.AF_UNIX) as sock: sock.bind('/proc/self/fd/%s/upgrade.sock' % descriptor)
    os.close(descriptor)
""")
            executable("docker", """
import os, sys
from pathlib import Path
assert sys.argv[1:] == ['inspect', 'knowledge_shelf_query']
print(Path(os.environ['KSQ_INSTALL_FIXTURE'], 'inspect.json').read_text())
""")
            executable("install", """
import sys
from pathlib import Path
assert sys.argv[1:7] == ['-d', '-m', '700', '-o', 'root', '-g']
Path(sys.argv[-1]).mkdir(exist_ok=True)
Path(sys.argv[-1]).chmod(0o700)
""")
            executable("chown", "import sys\nassert sys.argv[1] == 'root:root'\n")
            executable("sudo", "raise SystemExit('The fixture must never request actual privileges')\n")
            (root / "inspect.json").write_text(json.dumps([{
                "Mounts": [{"Destination": "/data/config_pnp", "Source": "/fixture/配置"},
                           {"Destination": "/data/templates", "Source": "/fixture/templates"}],
                "Config": {"Image": "fixture:runtime", "Env": ["TZ=Asia/Shanghai", "KSQ_DESKTOP_URL=http://fixture/desktop"]},
            }]))
            script = (ROOT / "deploy/standalone/upgrade.sh").read_text()
            script = script.replace('UNIT="/etc/systemd/system/${SERVICE}"', 'UNIT=' + shlex.quote(str(unit)))
            script = script.replace('"${EUID}"', '"${KSQ_TEST_UID}"').replace('/run/systemd/system', str(root))
            installer = deployment / "upgrade.sh"
            installer.write_text(script)
            environment = dict(os.environ, PATH=str(fakebin) + ":" + os.environ["PATH"],
                               KSQ_TEST_UID="0", KSQ_INSTALL_FIXTURE=str(root))
            username = pwd.getpwuid(os.getuid()).pw_name
            subprocess.run(["bash", str(installer), "install", str(binary), "root", sys.executable],
                           env=environment, check=True, capture_output=True, timeout=15)
            contents = unit.read_text()
            self.assertIn("User=root", contents)
            self.assertIn("Restart=on-failure", contents)
            self.assertIn("from ksq.upgrade import main", contents)
            self.assertNotIn("host_files", contents)
            self.assertNotIn("fixture-private-value", contents)
            self.assertIn('KSQ_UPGRADE_GITHUB_TOKEN=fixture-private-value', environment_file.read_text())
            self.assertIn('CONFIG_PNP_DIR="/fixture/配置"', environment_file.read_text())
            self.assertNotIn('/old', environment_file.read_text())
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(environment_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((directory / "agent.bin").stat().st_mode), 0o600)
            self.assertEqual((directory / "agent.bin").read_bytes(), binary.read_bytes())
            command = shlex.split(next(line for line in contents.splitlines() if line.startswith("ExecStart="))[10:])
            command = [argument.replace("%%", "%").replace("$$", "$") for argument in command]
            result = subprocess.run([command[0], "-S", *command[1:], "--help"], check=True, capture_output=True, timeout=10)
            self.assertIn(b"--host-user", result.stdout)
            self.assertEqual(command[-1], username)
            self.assertEqual(command[command.index("--deployment") + 1], str(deployment))
            self.assertNotIn("restart ksq-host-files.service", (root / "systemctl.calls").read_text())
            self.assertIn('WorkingDirectory=' + str(deployment).replace('%', '%%') + '\n', contents)
            self.assertIn('EnvironmentFile=' + str(environment_file).replace('%', '%%') + '\n', contents)
            verifier = shutil.which("systemd-analyze")
            if verifier:
                verified = subprocess.run([verifier, "verify", str(unit)], env=environment,
                                          capture_output=True, text=True, timeout=15)
                self.assertEqual(verified.returncode, 0, verified.stderr)
                self.assertNotIn("EnvironmentFile= path is not absolute", verified.stderr)

    def test_unattended_host_files_restart_keeps_original_service_environment(self):
        with tempfile.TemporaryDirectory(prefix="ksq-host-restart-") as temporary:
            root = Path(temporary)
            unit = root / "ksq-host-files.service"
            original = '[Service]\nUser=1000\nEnvironment="PATH=/original/tools" "KSQ_HOST_PYTHON=/original/python"\n'
            unit.write_text(original)
            directory = root / "host-files"
            directory.mkdir()
            binary = root / "application.bin"
            with zipfile.ZipFile(binary, "w") as archive:
                archive.writestr("ksq/__init__.py", "")
                archive.writestr("ksq/web/__init__.py", "")
                archive.writestr("ksq/web/host_files.py", "def main(): pass\n")
            (directory / "agent.bin").write_bytes(binary.read_bytes())
            fakebin = root / "fakebin"
            fakebin.mkdir()
            for name, body in {
                "systemctl": "import os,sys\nfrom pathlib import Path\nwith Path(os.environ['KSQ_HOST_FIXTURE'], 'calls').open('a') as log: log.write(' '.join(sys.argv[1:]) + '\\n')\n",
                "install": "import sys\nassert sys.argv[1] == '-d', 'Existing service must not be replaced'\n",
                "sudo": "raise SystemExit('No actual privilege request is allowed in this check')\n",
            }.items():
                path = fakebin / name
                path.write_text("#!" + sys.executable + "\n" + body)
                path.chmod(0o755)
            script = (ROOT / "deploy/standalone/host-files.sh").read_text()
            script = script.replace('UNIT="/etc/systemd/system/${SERVICE}"', 'UNIT=' + shlex.quote(str(unit)))
            script = script.replace('"${EUID}"', '"${KSQ_TEST_UID}"').replace('/run/systemd/system', str(root))
            launcher = root / "host-files.sh"
            launcher.write_text(script)
            subprocess.run(["bash", str(launcher), "start", str(binary), str(directory)], check=True,
                           env=dict(os.environ, PATH=str(fakebin) + ":" + os.environ["PATH"], KSQ_TEST_UID="0",
                                    KSQ_HOST_FIXTURE=str(root), KSQ_HOST_FILES_USER="root", KSQ_HOST_PYTHON=sys.executable,
                                    KSQ_ONLINE_UPGRADE="1"), capture_output=True, timeout=15)
            self.assertEqual(unit.read_text(), original)
            self.assertEqual((root / "calls").read_text(), "restart ksq-host-files.service\n")


if __name__ == "__main__":
    unittest.main()

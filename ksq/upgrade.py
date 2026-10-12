"""GitHub Releases updater. The host service uses only the Python standard library."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socketserver
import stat
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
import zipfile


REPOSITORY = "Wann-99/DevOps_Toolchain"
RUNTIME_IMAGE = "hub.noematrix.cn/pharmacy/knowledge_shelf_query_runtime:v1.2.0"
PROTOCOL = 1
MAX_PACKAGE = 256 * 1024 * 1024
MAX_JSON = 128 * 1024
CONFIG_FILES = ("dashboard_settings.json", "dashboard_active_order.json", "test_order_state.json",
                "order_config.json", "order_config.prod.json", "users.json", "feishu_rules.json")
_WRITE_LOCK = threading.Lock()
_WRITES = 0


class UpgradeBusy(ValueError):
    pass


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {} if default is None else default


def write_json(path, data):
    path = Path(path)
    incoming = path.with_suffix(".incoming")
    with incoming.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    incoming.chmod(0o600)
    incoming.replace(path)
    fsync_directory(path.parent)


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def maintenance_active():
    directory = os.environ.get("KSQ_UPGRADE_DIRECTORY")
    if not directory:
        return False
    try:
        return bool(read_json(Path(directory) / "state.json").get("maintenance"))
    except (OSError, ValueError, AttributeError):
        return True  # Fail closed if an installed updater's state cannot be read.


def active_writes():
    with _WRITE_LOCK:
        return _WRITES


@contextlib.contextmanager
def writing():
    global _WRITES
    with _WRITE_LOCK:
        if maintenance_active():
            raise UpgradeBusy("正在安装升级，暂不接受新的操作。")
        _WRITES += 1
    try:
        yield
    finally:
        with _WRITE_LOCK:
            _WRITES -= 1


def host_request(action, payload=None, cookie=""):
    from ksq.web.host_files import HostConnection
    directory = os.environ.get("KSQ_UPGRADE_DIRECTORY")
    if not directory:
        raise ValueError("尚未安装在线升级服务，请使用完整部署包执行 bash start.sh upgrade install。")
    connection = HostConnection(Path(directory) / "upgrade.sock", timeout=60)
    try:
        connection.request("GET" if payload is None else "POST", "/" + action,
                           None if payload is None else json.dumps(payload),
                           {"Content-Type": "application/json", "Cookie": cookie})
        response = connection.getresponse()
        data = json.loads(response.read(MAX_JSON + 1))
        if response.status != 200:
            raise ValueError(data.get("error") or "升级服务请求失败。")
        return data
    except (OSError, http.client.HTTPException):
        raise ValueError("在线升级服务不可用，请在部署目录执行 bash start.sh upgrade install。") from None
    finally:
        connection.close()


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+", value):
        raise ValueError("在线升级仅接受 v主版本.次版本.修订号 的正式版本。")
    return tuple(int(part) for part in value.lstrip("v").split("."))


def github_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.netloc not in {"api.github.com", "github.com", "objects.githubusercontent.com",
                                     "release-assets.githubusercontent.com"}):
        raise ValueError("升级附件必须来自 GitHub 的 HTTPS 发布服务。")
    return url


class GithubRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        github_url(newurl)
        redirected = super().redirect_request(request, fp, code, message, headers, newurl)
        redirected.remove_header("Authorization")
        return redirected


def github_open(url, binary=False):
    github_url(url)
    headers = {"Accept": "application/octet-stream" if binary else "application/vnd.github+json",
               "User-Agent": "ksq-online-upgrade", "X-GitHub-Api-Version": "2026-03-10"}
    token = os.environ.get("KSQ_UPGRADE_GITHUB_TOKEN", "").strip()
    if token and urlsplit(url).hostname == "api.github.com":
        headers["Authorization"] = "Bearer " + token
    try:
        return build_opener(GithubRedirect()).open(Request(url, headers=headers), timeout=20)
    except HTTPError as error:
        raise ValueError("GitHub 请求失败（HTTP %s），请检查 Release 是否发布及仓库访问凭据。" % error.code) from None
    except (URLError, OSError):
        raise ValueError("无法连接 GitHub，请检查设备网络。") from None


def github_json(url, binary=False):
    with github_open(url, binary) as response:
        raw = response.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise ValueError("版本说明文件过大。")
    return json.loads(raw)


def archive_manifest(path):
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("KSQ_BUILD.json")
        if info.file_size > MAX_JSON:
            raise ValueError("应用包版本信息过大。")
        return json.loads(archive.read(info))


def validate_package(path, release):
    size = Path(path).stat().st_size
    if size != release["size"] or size > MAX_PACKAGE:
        raise ValueError("升级包大小不符。")
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != release["sha256"]:
        raise ValueError("升级包 SHA-256 校验失败，原版本未修改。")
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 30000 or sum(item.file_size for item in entries) > 512 * 1024 * 1024:
            raise ValueError("应用包解压大小超出限制。")
        for item in entries:
            member = PurePosixPath(item.filename)
            if member.is_absolute() or ".." in member.parts or "\\" in item.filename or stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError("应用包包含不安全的文件路径。")
        required = {"__main__.py", "KSQ_BUILD.json", "ksq/cli.py", "ksq/web/templates/shell.html",
                    "ksq/web/static/app.css", "ksq/upgrade.py"}
        if not required.issubset(archive.namelist()) or archive.testzip():
            raise ValueError("应用包缺少必需文件或已损坏。")
    manifest = archive_manifest(path)
    if (manifest.get("version") != release["version"] or manifest.get("runtime_image") != release["runtime_image"]
            or manifest.get("requirements_sha256") != release["requirements_sha256"]
            or manifest.get("upgrade_protocol") != PROTOCOL or manifest.get("preserve_state") is not True):
        raise ValueError("应用包版本或升级协议不匹配，请使用完整部署包更新。")


class Updater:
    def __init__(self, directory, deployment, host_user):
        self.directory = Path(directory)
        self.deployment = Path(deployment)
        self.host_user = host_user
        self.app = self.deployment / "bin/knowledge_shelf_query.bin"
        self.lock = threading.Lock()
        self.state = read_json(self.directory / "state.json", {"phase": "idle", "message": "尚未检查更新。"})
        self.release = read_json(self.directory / "release.json")
        self.repository = os.environ.get("KSQ_UPGRADE_REPOSITORY", REPOSITORY)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise ValueError("GitHub 仓库名称无效。")

    def save(self, phase, message, **extra):
        self.state = {**self.state, "phase": phase, "message": message, "updated_at": time.time(),
                      "busy": phase in {"checking", "downloading", "preparing", "installing", "recovering"},
                      "maintenance": phase in {"preparing", "installing", "recovering"}, **extra}
        write_json(self.directory / "state.json", self.state)

    def status(self):
        current = archive_manifest(self.app).get("version", "")
        release = self.release
        available = bool(release) and version_tuple(release["version"]) > version_tuple(current)
        return {"ok": True, "installed": True, "repository": "https://github.com/" + self.repository,
                "current_version": current, "release": {key: release.get(key) for key in ("version", "notes", "size", "url")},
                "available": available, **self.state}

    def fetch_release(self):
        prefix = "https://api.github.com/repos/" + self.repository + "/releases"
        published = github_json(prefix + "/latest")
        if not isinstance(published, dict):
            raise ValueError("GitHub 版本响应格式无效。")
        if published.get("draft") or published.get("prerelease"):
            raise ValueError("只支持已发布的正式版本。")
        assets = published.get("assets") or []
        if not isinstance(assets, list) or any(not isinstance(item, dict) for item in assets):
            raise ValueError("GitHub 发布附件格式无效。")
        def asset(name):
            matches = [item for item in assets if item.get("name") == name and item.get("state") == "uploaded"]
            if len(matches) != 1 or matches[0].get("url") != prefix + "/assets/" + str(matches[0].get("id")):
                raise ValueError("Release 缺少有效附件：" + name)
            return matches[0]
        metadata = github_json(asset("ksq_release.json")["url"], binary=True)
        if not isinstance(metadata, dict):
            raise ValueError("发布说明格式无效。")
        version = metadata.get("version")
        version_tuple(version)
        filename = "knowledge_shelf_query_" + version + ".bin"
        package = asset(filename)
        if (metadata.get("format") != 1 or metadata.get("upgrade_protocol") != PROTOCOL
                or metadata.get("runtime_image") != RUNTIME_IMAGE or metadata.get("preserve_state") is not True):
            raise ValueError("此版本需要更新运行镜像或部署脚本，请使用完整部署包。")
        if not re.fullmatch(r"[0-9a-f]{64}", str(metadata.get("requirements_sha256") or "")):
            raise ValueError("Release 缺少运行依赖校验信息。")
        if (metadata.get("filename") != filename or published.get("tag_name") != version
                or type(metadata.get("size")) is not int or not 0 < metadata["size"] <= MAX_PACKAGE
                or package.get("size") != metadata["size"]
                or not re.fullmatch(r"[0-9a-f]{64}", str(metadata.get("sha256") or ""))):
            raise ValueError("Release 版本说明、附件大小或校验值无效。")
        metadata.update(asset_url=package["url"], notes=str(published.get("body") or "")[:8000],
                        url="https://github.com/" + self.repository + "/releases/tag/" + version)
        return metadata

    def check(self):
        if self.state.get("maintenance"):
            raise ValueError("升级或恢复尚未完成，请先检查设备上的 upgrade/upgrade.log。")
        if not self.lock.acquire(blocking=False):
            raise ValueError("升级任务正在执行，请等待完成。")
        try:
            self.save("checking", "正在检查 GitHub 最新正式版本…")
            self.release = self.fetch_release()
            write_json(self.directory / "release.json", self.release)
            self.save("idle", "版本检查完成。")
            return self.status()
        except Exception:
            self.save("failed", "检查更新失败，请重试或检查设备网络及发布附件。")
            raise
        finally:
            self.lock.release()

    def start(self, version, cookie):
        if self.state.get("maintenance"):
            raise ValueError("升级或恢复尚未完成，请先检查设备上的 upgrade/upgrade.log。")
        if not cookie or not self.release or version != self.release.get("version"):
            raise ValueError("请先检查更新，再选择已确认的版本。")
        if version_tuple(version) <= version_tuple(archive_manifest(self.app).get("version")):
            raise ValueError("当前已是此版本或更新版本。")
        if not self.lock.acquire(blocking=False):
            raise ValueError("已有升级任务正在执行。")
        try:
            self.save("downloading", "正在下载升级包，当前服务继续运行。", target_version=version, recovery=None, restart_original=False)
            result = self.status()
            threading.Thread(target=self.run, args=(version, cookie), name="ksq-upgrade", daemon=True).start()
            return result
        except Exception:
            self.lock.release()
            raise

    def command(self, *arguments):
        environment = dict(os.environ, KSQ_ONLINE_UPGRADE="1", KSQ_HOST_FILES_USER=self.host_user)
        path = self.directory / "upgrade.log"
        if path.exists() and path.stat().st_size >= 5 * 1024 * 1024:
            path.replace(self.directory / "upgrade.log.1")
        with path.open("ab") as log:
            subprocess.run(arguments, cwd=self.deployment, env=environment, stdout=log, stderr=log,
                           check=True, timeout=360)

    def ready(self, cookie):
        request = Request("http://127.0.0.1:8765/api/upgrade/readiness", headers={"Cookie": cookie})
        with urlopen(request, timeout=120) as response:
            return json.loads(response.read(MAX_JSON))

    def backup(self):
        backup = self.directory / "backup"
        incoming = self.directory / "backup.incoming"
        if incoming.exists():
            shutil.rmtree(incoming)
        incoming.mkdir(mode=0o700)
        shutil.copy2(self.app, incoming / "application.bin")
        # Application data stays in its existing mounted directory; no data migration is performed.
        config = incoming / "config"
        config.mkdir(mode=0o700)
        for name in CONFIG_FILES:
            source = self.deployment / "config" / name
            if source.is_symlink():
                raise ValueError("配置文件不能是符号链接。")
            if source.exists():
                shutil.copy2(source, config / name)
        # Persist the checkpoint before recording that replacement may begin.
        for file in incoming.rglob("*"):
            if file.is_file():
                with file.open("rb") as stream:
                    os.fsync(stream.fileno())
        fsync_directory(config)
        fsync_directory(incoming)
        if backup.exists():
            shutil.rmtree(backup)
        incoming.replace(backup)
        fsync_directory(self.directory)

    def stop_application(self):
        names = subprocess.check_output(["docker", "ps", "-a", "--format", "{{.Names}}"], text=True, timeout=10)
        if "knowledge_shelf_query" in names.splitlines():
            self.command("docker", "stop", "knowledge_shelf_query")

    def recover(self):
        self.save("recovering", "新版本未就绪，正在恢复升级前版本和配置…")
        backup = self.directory / "backup"
        if not (backup / "application.bin").is_file():
            raise ValueError("升级备份不完整，请检查设备上的 upgrade 目录。")
        self.stop_application()
        shutil.copy2(backup / "application.bin", self.app)
        original = backup / "config"
        target = self.deployment / "config"
        for name in CONFIG_FILES:
            destination = target / name
            if destination.is_symlink():
                raise ValueError("配置恢复目标不能是符号链接。")
            if (original / name).is_file():
                shutil.copy2(original / name, destination)
            else:
                destination.unlink(missing_ok=True)
        self.command("bash", str(self.deployment / "start.sh"), "restart")
        self.verify_running(archive_manifest(self.app)["version"])
        self.save("failed", "升级失败，已自动恢复升级前版本，配置和数据保留。", recovery="restored")

    def verify_running(self, version):
        with urlopen("http://127.0.0.1:8765/api/health", timeout=5) as response:
            health = json.loads(response.read(MAX_JSON))
        if health.get("ok") is not True or health.get("version") != version:
            raise ValueError("服务健康检查或实际版本校验未通过。")

    def run(self, version, cookie):
        incoming = self.directory / "application.incoming"
        stopped = False
        try:
            release = self.fetch_release()
            if release["version"] != version or release["sha256"] != self.release["sha256"]:
                raise ValueError("Release 内容已变化，请重新检查更新。")
            config_size = sum((self.deployment / "config" / name).stat().st_size for name in CONFIG_FILES
                              if (self.deployment / "config" / name).is_file())
            required_space = release["size"] * 2 + self.app.stat().st_size + config_size + 64 * 1024 * 1024
            if shutil.disk_usage(self.deployment).free < required_space:
                raise ValueError("磁盘空间不足，原版本未修改。")
            with github_open(release["asset_url"], binary=True) as source, incoming.open("wb") as target:
                length = 0
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    length += len(chunk)
                    if length > release["size"]:
                        raise ValueError("升级包下载大小超出版本说明。")
                    target.write(chunk)
            validate_package(incoming, release)
            image = subprocess.check_output(["docker", "inspect", "--format", "{{.Config.Image}}", "knowledge_shelf_query"],
                                            text=True, timeout=10).strip()
            if image != release["runtime_image"]:
                raise ValueError("设备运行镜像与此版本不兼容，请使用完整部署包。")
            requirements = subprocess.check_output([
                "docker", "exec", "knowledge_shelf_query", "python3", "-c",
                "import hashlib;print(hashlib.sha256(open('/opt/ksq/requirements.txt','rb').read()).hexdigest())",
            ], text=True, timeout=10).strip()
            if requirements != release["requirements_sha256"]:
                raise ValueError("此版本的运行依赖已变化，请使用完整部署包更新镜像。")
            self.save("preparing", "正在确认订单已结束，暂不接受新的写操作。")
            ready = self.ready(cookie)
            if not ready.get("ready"):
                raise ValueError(ready.get("reason") or "仍有任务或操作未完成，原版本未修改。")
            self.save("preparing", "正在停止网页服务并保存配置备份。")
            stopped = True
            self.stop_application()
            self.backup()
            self.save("installing", "正在安装升级，服务将短暂断开。")
            self.command("bash", str(self.deployment / "start.sh"), "update", str(incoming))
            self.verify_running(version)
            self.save("succeeded", "升级完成，配置和数据已保留，请重新登录。", recovery=None)
        except Exception as error:
            if self.state.get("phase") in {"installing", "recovering"}:
                try:
                    self.recover()
                except Exception:
                    self.save("failed", "自动恢复未完成，请在设备上检查 upgrade/upgrade.log。", recovery="failed", maintenance=True)
            else:
                if stopped:
                    try:
                        self.command("bash", str(self.deployment / "start.sh"), "restart")
                    except Exception:
                        self.save("failed", "原版本未替换，但服务未能恢复，请检查 upgrade/upgrade.log。", maintenance=True, restart_original=True)
                        return
                message = str(error) if isinstance(error, ValueError) else "下载或升级检查失败，原版本未修改。"
                self.save("failed", message)
        finally:
            incoming.unlink(missing_ok=True)
            self.lock.release()

    def resume(self):
        if self.state.get("phase") in {"installing", "recovering"} or self.state.get("recovery") == "failed":
            self.recover()
        elif self.state.get("busy") or self.state.get("restart_original"):
            if self.state.get("phase") == "preparing" or self.state.get("restart_original"):
                self.command("bash", str(self.deployment / "start.sh"), "restart")
                self.verify_running(archive_manifest(self.app)["version"])
            self.save("failed", "上次升级在替换应用包前中断，已保留原版本。", restart_original=False)


class UpgradeHandler(BaseHTTPRequestHandler):
    def respond(self, status, data):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/status":
            self.respond(200, self.server.updater.status())
        else:
            self.respond(404, {"error": "接口不存在。"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024:
                raise ValueError("升级请求大小无效。")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("升级请求格式无效。")
            if self.path == "/check":
                data = self.server.updater.check()
            elif self.path == "/start":
                data = self.server.updater.start(payload.get("version"), self.headers.get("Cookie", ""))
            else:
                self.respond(404, {"error": "接口不存在。"})
                return
            self.respond(200, data)
        except ValueError as error:
            self.respond(400, {"error": str(error)})
        except (OSError, zipfile.BadZipFile, KeyError):
            self.respond(400, {"error": "无法执行升级操作，请检查发布附件、网络或任务状态。"})

    def log_message(self, *_args):
        pass


class UpgradeServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def server_bind(self):
        path = Path(self.server_address)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            self.socket.bind("/proc/self/fd/%s/%s" % (directory, path.name))
        finally:
            os.close(directory)


def main():
    parser = argparse.ArgumentParser(description="KSQ 独立在线升级服务")
    parser.add_argument("--directory", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--host-user", required=True)
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.umask(0o077)
    path = directory / "upgrade.sock"
    with (directory / "service.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists():
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise ValueError("升级连接路径不是套接字。")
            path.unlink()
        updater = Updater(directory, Path(args.deployment).resolve(), args.host_user)
        try:
            updater.resume()
        except Exception:
            updater.save("failed", "自动恢复未完成，请在设备上检查 upgrade/upgrade.log。", maintenance=True,
                         **({"restart_original": True} if updater.state.get("phase") == "preparing" or updater.state.get("restart_original")
                            else {"recovery": "failed"}))
        with UpgradeServer(str(path), UpgradeHandler) as server:
            path.chmod(0o600)
            server.updater = updater
            try:
                server.serve_forever()
            finally:
                path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

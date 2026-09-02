from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ksq import vfm_app
from ksq.web import state


# 现场 config.yaml 的关键结构：template 与 template_for_validation 各有一个
# template_root，只有前者代表 percept 当前使用的场景。
REAL_SHAPE = """\
camera_id: ''
data_dir: ''
knowledge:
  url: http://0.0.0.0:9000
suction:
  mode: da_suction
template:
  enable_label_refinement: true
  template_root: templates/pnp_percept/scene_A
  threshold: 0.5
template_for_validation:
  image_encoder: swin_transformer
  template_root: templates/pnp_percept/scene_B
  threshold: 0.5
vfm_url: http://0.0.0.0:9000
"""


class ReadTemplateRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, content: str) -> Path:
        config_file = self.root / "config.yaml"
        config_file.write_text(content, encoding="utf-8")
        return config_file

    def test_takes_template_block_not_validation_block(self) -> None:
        # 关键：两个块都有 template_root，取错会读到校验用的场景目录
        self.assertEqual(
            vfm_app.read_template_root(self.write(REAL_SHAPE)),
            "templates/pnp_percept/scene_A",
        )

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(vfm_app.read_template_root(self.root / "nope.yaml"))

    def test_missing_key_returns_none(self) -> None:
        content = "template:\n  threshold: 0.5\nother: 1\n"
        self.assertIsNone(vfm_app.read_template_root(self.write(content)))

    def test_absent_template_block_returns_none(self) -> None:
        content = "template_for_validation:\n  template_root: templates/x\n"
        self.assertIsNone(vfm_app.read_template_root(self.write(content)))

    def test_quotes_and_inline_comment_are_stripped(self) -> None:
        content = 'template:\n  template_root: "templates/x"  # 注释\n'
        self.assertEqual(
            vfm_app.read_template_root(self.write(content)), "templates/x"
        )

    def test_unquoted_value_drops_trailing_comment(self) -> None:
        content = "template:\n  template_root: templates/x # 场景\n"
        self.assertEqual(
            vfm_app.read_template_root(self.write(content)), "templates/x"
        )

    def test_deeper_nested_key_is_not_picked_up(self) -> None:
        content = (
            "template:\n"
            "  nested:\n"
            "    template_root: templates/wrong\n"
            "  template_root: templates/right\n"
        )
        self.assertEqual(
            vfm_app.read_template_root(self.write(content)), "templates/right"
        )

    def test_empty_value_returns_none(self) -> None:
        self.assertIsNone(
            vfm_app.read_template_root(self.write("template:\n  template_root:\n"))
        )


class KnowledgeDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.app = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self, template_root: str, make_knowledge: bool = True) -> Path:
        (self.app / "config.yaml").write_text(
            f"template:\n  template_root: {template_root}\n", encoding="utf-8"
        )
        if make_knowledge:
            target = self.app / "model" / template_root / "knowledge"
            target.mkdir(parents=True, exist_ok=True)
        return self.app

    def test_resolves_under_model_directory(self) -> None:
        # template_root 相对 <vfm_app>/model，而不是 <vfm_app>
        app = self.build("templates/pnp_percept/scene_A")

        self.assertEqual(
            vfm_app.knowledge_directory(app),
            (app / "model/templates/pnp_percept/scene_A/knowledge").resolve(),
        )

    def test_missing_knowledge_subdirectory_returns_none(self) -> None:
        app = self.build("templates/scene_X", make_knowledge=False)

        self.assertIsNone(vfm_app.knowledge_directory(app))

    def test_missing_config_file_returns_none(self) -> None:
        self.assertIsNone(vfm_app.knowledge_directory(self.app))

    def test_path_traversal_is_rejected(self) -> None:
        escaped = self.app / "outside" / "knowledge"
        escaped.mkdir(parents=True)
        (self.app / "config.yaml").write_text(
            "template:\n  template_root: ../outside\n", encoding="utf-8"
        )

        self.assertIsNone(vfm_app.knowledge_directory(self.app))


class ContainerDefaultKnowledgeTests(unittest.TestCase):
    def test_container_root_and_default_knowledge_are_separate(self) -> None:
        project = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment.pop("KSQ_DEFAULT_KNOWLEDGE", None)
        environment.pop("KSQ_KNOWLEDGE_ROOT", None)
        environment["KSQ_APP_DIRECTORY"] = "/app"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from ksq.constants import DEFAULT_KNOWLEDGE_ROOT, "
                    "DEFAULT_KNOWLEDGE; "
                    "print(DEFAULT_KNOWLEDGE_ROOT); print(DEFAULT_KNOWLEDGE)"
                ),
            ],
            cwd=project,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.stdout.splitlines(),
            ["/data/knowledge", "/data/knowledge/knowledge"],
        )


class StandaloneKnowledgeMountTests(unittest.TestCase):
    def test_standard_deployment_mounts_templates_root_and_defaults_to_knowledge(self) -> None:
        project = Path(__file__).resolve().parents[1]
        expected_root_mount = (
            "${KNOWLEDGE_DIR:-/home/nvidia/compiled/VfmApp_deploy/model/templates}"
            ":/data/knowledge"
        )
        expected_command = (
            "- --knowledge-root\n      - /data/knowledge\n"
            "      - --knowledge\n      - /data/knowledge/knowledge"
        )
        for compose_name in (
            "deploy/standalone/docker-compose.yml",
            "devOps/docker-compose.yml",
        ):
            compose = (project / compose_name).read_text(encoding="utf-8")
            with self.subTest(compose=compose_name):
                self.assertIn(expected_command, compose)
                self.assertIn(expected_root_mount, compose)
                self.assertNotIn("/data/vfm_app", compose)

        dockerfile = (project / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn('"--knowledge-root", "/data/knowledge"', dockerfile)
        self.assertIn('"--knowledge", "/data/knowledge/knowledge"', dockerfile)

        for script_name in ("deploy/standalone/start.sh", "devOps/up.sh"):
            script = (project / script_name).read_text(encoding="utf-8")
            with self.subTest(script=script_name):
                self.assertIn("model/templates", script)
                self.assertNotIn("model/templates/knowledge", script)
                self.assertNotIn("VFM_APP_DIR", script)


class StateKnowledgeReloadTests(unittest.TestCase):
    """reload_config_pnp_paths 让 knowledge 跟随 config.yaml，且尊重显式覆盖。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.app = Path(self.temporary.name)
        self.scene_a = self.app / "model/templates/scene_A/knowledge"
        self.scene_b = self.app / "model/templates/scene_B/knowledge"
        self.scene_a.mkdir(parents=True)
        self.scene_b.mkdir(parents=True)
        self.saved = (
            state.configured_knowledge,
            state.configured_knowledge_root,
            state.configured_vfm_app,
            state.configured_config_pnp,
            state._cli_config_paths,
            state._explicit_config_keys,
        )
        state.configured_vfm_app = self.app
        state.configured_knowledge_root = None
        state.configured_config_pnp = None
        state._cli_config_paths = {}
        state._explicit_config_keys = frozenset()

    def tearDown(self) -> None:
        (
            state.configured_knowledge,
            state.configured_knowledge_root,
            state.configured_vfm_app,
            state.configured_config_pnp,
            state._cli_config_paths,
            state._explicit_config_keys,
        ) = self.saved
        self.temporary.cleanup()

    def write_scene(self, name: str) -> None:
        (self.app / "config.yaml").write_text(
            f"template:\n  template_root: templates/{name}\n", encoding="utf-8"
        )

    def test_knowledge_follows_template_root(self) -> None:
        state.configured_knowledge = Path("/data/knowledge")
        self.write_scene("scene_A")
        state.reload_config_pnp_paths(require_vfm_knowledge=True)
        self.assertEqual(state.configured_knowledge, self.scene_a.resolve())

        # 未显式传 --knowledge 的兼容启动方式仍可重新解析场景。
        self.write_scene("scene_B")
        state.reload_config_pnp_paths(require_vfm_knowledge=True)
        self.assertEqual(state.configured_knowledge, self.scene_b.resolve())

    def test_explicit_knowledge_is_preserved(self) -> None:
        self.write_scene("scene_A")
        pinned = self.app / "pinned"
        pinned.mkdir()
        state.configured_knowledge = pinned
        state._cli_config_paths = {"knowledge": pinned}
        state._explicit_config_keys = frozenset({"knowledge"})

        state.reload_config_pnp_paths(require_vfm_knowledge=True)

        self.assertEqual(state.configured_knowledge, pinned)

    def test_unparseable_config_keeps_container_fallback(self) -> None:
        (self.app / "config.yaml").write_text("other: 1\n", encoding="utf-8")
        state.configured_knowledge = Path("/data/knowledge")

        state.reload_config_pnp_paths()

        self.assertEqual(state.configured_knowledge, Path("/data/knowledge"))

    def test_required_vfm_knowledge_reports_invalid_config(self) -> None:
        state.configured_knowledge = Path("/data/knowledge")

        with self.assertRaisesRegex(ValueError, "config.yaml"):
            state.reload_config_pnp_paths(require_vfm_knowledge=True)

        self.assertEqual(state.configured_knowledge, Path("/data/knowledge"))


class KnowledgeRootStateTests(unittest.TestCase):
    """模板根挂载模式不依赖 VfmApp config.yaml。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "templates"
        self.default = self.root / "knowledge"
        self.nested = self.root / "pnp_percept/templates_260827/knowledge"
        self.default.mkdir(parents=True)
        self.nested.mkdir(parents=True)
        self.saved = {
            name: getattr(state, name)
            for name in (
                "configured_knowledge",
                "configured_knowledge_root",
                "configured_vfm_app",
                "configured_config_pnp",
                "_cli_config_paths",
                "_cli_knowledge_root",
                "_cli_knowledge_path",
                "_explicit_config_keys",
            )
        }
        state.configured_knowledge_root = self.root
        state.configured_knowledge = self.default
        state.configured_vfm_app = self.root / "missing-vfm"
        state.configured_config_pnp = None
        state._cli_config_paths = {}
        state._cli_knowledge_root = self.root
        state._cli_knowledge_path = self.default
        state._explicit_config_keys = frozenset()

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(state, name, value)
        self.temporary.cleanup()

    def test_root_mode_keeps_default_without_vfm_config(self) -> None:
        state.reload_config_pnp_paths(require_vfm_knowledge=True)

        self.assertEqual(state.configured_knowledge_root, self.root.resolve())
        self.assertEqual(state.configured_knowledge, self.default.resolve())

    def test_root_mode_allows_nested_scene_target(self) -> None:
        state.configured_knowledge = self.nested

        state.reload_config_pnp_paths()

        self.assertEqual(state.configured_knowledge, self.nested.resolve())


if __name__ == "__main__":
    unittest.main()

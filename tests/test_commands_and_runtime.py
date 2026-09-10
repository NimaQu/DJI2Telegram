import re
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from qdc507_gateway.adb.runtime import ModuleVoiceController, ModuleVoiceRuntime, RuntimeManifest






















def test_runtime_manifest_rejects_unsafe_names():
    with TemporaryDirectory() as root:
        path = Path(root) / "manifest.json"
        path.write_text('{"runtime_version":"1","kernel_release":"3.18.44","helper":"../x","files":[]}', encoding="utf-8")
        try:
            RuntimeManifest.load(path)
        except RuntimeError:
            pass
        else:
            raise AssertionError("unsafe runtime filename accepted")


def test_runtime_manifest_accepts_existing_camel_case_format():
    with TemporaryDirectory() as root:
        path = Path(root) / "manifest.json"
        path.write_text(
            '{"formatVersion":1,"runtimeVersion":"v1","kernelRelease":"3.18.44",'
            '"cardName":"voice-card","helper":"helper","files":[{"name":"helper"}],'
            '"requiredDevices":["/dev/snd/controlC0"]}',
            encoding="utf-8",
        )
        manifest = RuntimeManifest.load(path)
        assert manifest.runtime_version == "v1"
        assert manifest.kernel_release == "3.18.44"
        assert manifest.required_devices == ("/dev/snd/controlC0",)
        assert manifest.card_name == "voice-card"


def test_runtime_manifest_hashes_are_checked_before_push():
    with TemporaryDirectory() as root:
        local = Path(root) / "helper"
        local.write_bytes(b"helper")
        manifest_path = Path(root) / "manifest.json"
        manifest_path.write_text(
            '{"runtimeVersion":"v1","kernelRelease":"3.18.44",'
            '"helper":"helper","files":[{"name":"helper",'
            '"sha256":"' + "0" * 64 + '"}]}',
            encoding="utf-8",
        )

        class FakeADB:
            def shell(self, command, timeout=10):
                if command == "id -u":
                    return "0"
                if command == "uname -r":
                    return "3.18.44"
                if "QDC507_MKDIR_STATUS" in command:
                    return "QDC507_MKDIR_STATUS=0"
                return ""

            def push(self, data, remote_path, mode=0o700):
                raise AssertionError("hash mismatch must fail before push")

        runtime = ModuleVoiceRuntime(FakeADB(), RuntimeManifest.load(manifest_path), root)
        try:
            runtime.prepare()
        except RuntimeError:
            pass
        else:
            raise AssertionError("runtime hash mismatch was accepted")


def test_bundled_module_voice_manifest_keeps_module_file_and_name_distinct():
    manifest = RuntimeManifest.load(
        Path(__file__).parents[1] / "resources" / "module-voice" / "manifest.json"
    )
    assert manifest.kernel_release == "3.18.44"
    assert manifest.modules[0].file == "qdc507_aprv3.ko"
    assert manifest.modules[0].name == "qdc507_aprv3"
    assert "/dev/snd/controlC0" in manifest.required_devices


def test_runtime_requires_module_root_and_matching_kernel():
    class FakeADB:
        def __init__(self):
            self.commands = []
        def shell(self, command, timeout=10):
            self.commands.append(command)
            if command == "id -u":
                return "0\n"
            if command == "uname -r":
                return "3.18.44\n"
            marker = next(iter(re.findall(r"QDC507_[A-Z0-9_]+", command)), None)
            if marker:
                return f"{marker}=0\n"
            return ""
        def push(self, data, remote_path, mode=0o700):
            self.commands.append("push:" + remote_path)

    with TemporaryDirectory() as root:
        local = Path(root) / "helper"
        local.write_bytes(b"helper")
        manifest_path = Path(root) / "manifest.json"
        manifest_path.write_text('{"runtime_version":"1","kernel_release":"3.18.44","helper":"helper","files":[{"name":"helper"}]}', encoding="utf-8")
        adb = FakeADB()
        runtime = ModuleVoiceRuntime(adb, RuntimeManifest.load(manifest_path), root)
        runtime.prepare()
        assert runtime.prepared


def test_runtime_loads_and_only_unloads_modules_loaded_by_this_process():
    class FakeADB:
        def __init__(self):
            self.commands = []
            self.driver_loaded = False

        def shell(self, command, timeout=10):
            self.commands.append(command)
            if command == "id -u":
                return "0"
            if command == "uname -r":
                return "3.18.44"
            if "QDC507_SOUND_STATUS" in command:
                return f"QDC507_SOUND_STATUS={0 if self.driver_loaded else 1}"
            if "QDC507_LEGACY_MODULE_STATUS" in command:
                return "QDC507_LEGACY_MODULE_STATUS=1"
            if "QDC507_MODULE_STATUS" in command:
                return f"QDC507_MODULE_STATUS={0 if self.driver_loaded else 1}"
            if "QDC507_INSMOD_STATUS" in command:
                self.driver_loaded = True
                return "QDC507_INSMOD_STATUS=0"
            if "QDC507_RMMOD_STATUS" in command:
                self.driver_loaded = False
                return "QDC507_RMMOD_STATUS=0"
            if "QDC507_ROUTE_READY" in command:
                return (
                    "QDC507_ROUTE_OWNED=0\n"
                    "QDC507_ROUTE_READY=0\n"
                    "QDC507_ROUTE_FOREIGN=0\n"
                )
            marker = next(iter(re.findall(r"QDC507_[A-Z0-9_]+", command)), None)
            if marker:
                return f"{marker}=0"
            return ""

        def push(self, data, remote_path, mode=0o700):
            self.commands.append("push:" + remote_path)

    with TemporaryDirectory() as root:
        Path(root, "helper").write_bytes(b"helper")
        Path(root, "driver.ko").write_bytes(b"module")
        manifest_path = Path(root, "manifest.json")
        manifest_path.write_text(
            '{"runtime_version":"1","kernel_release":"3.18.44",'
            '"helper":"helper","files":[{"name":"helper"},{"name":"driver.ko"}],'
            '"modules":[{"name":"driver","file":"driver.ko"}],'
            '"requiredDevices":["/dev/snd/controlC0"]}',
            encoding="utf-8",
        )
        adb = FakeADB()
        runtime = ModuleVoiceRuntime(adb, RuntimeManifest.load(manifest_path), root)
        runtime.prepare()
        assert runtime.loaded_here == ["driver"]
        runtime.cleanup()
        assert not runtime.loaded_here
        assert any("insmod" in command for command in adb.commands)
        assert any("rmmod" in command for command in adb.commands)


def test_voice_route_uses_owned_sigterm_session_not_legacy_command_tokens():
    class FakeADB:
        def __init__(self):
            self.commands = []
            self.route_owned = False
            self.route_ready = False

        def shell(self, command, timeout=10):
            self.commands.append(command)
            if "QDC507_ROUTE_READY" in command:
                return (
                    f"QDC507_ROUTE_OWNED={int(self.route_owned)}\n"
                    f"QDC507_ROUTE_READY={int(self.route_ready)}\n"
                    "QDC507_ROUTE_FOREIGN=0\n"
                )
            if "QDC507_ROUTE_START_STATUS" in command:
                self.route_owned = True
                self.route_ready = True
                return "QDC507_ROUTE_START_STATUS=0"
            if "QDC507_ROUTE_QUIESCENT_STATUS" in command:
                return "QDC507_ROUTE_QUIESCENT_STATUS=0"
            if "QDC507_ROUTE_STOP" in command:
                self.route_owned = False
                self.route_ready = False
                return "QDC507_ROUTE_STOP=1"
            if "QDC507_ROUTE_RESTORE_STATUS" in command:
                return "QDC507_ROUTE_RESTORE_STATUS=0"
            if "QDC507_ROUTE_MARKER_CLEANUP" in command:
                return "QDC507_ROUTE_MARKER_CLEANUP=0"
            return ""

        def push(self, data, remote_path, mode=0o700):
            pass

    with TemporaryDirectory() as root:
        Path(root, "helper").write_bytes(b"helper")
        manifest_path = Path(root, "manifest.json")
        manifest_path.write_text(
            '{"runtimeVersion":"1","kernelRelease":"3.18.44",'
            '"helper":"helper","files":[{"name":"helper"}]}',
            encoding="utf-8",
        )
        adb = FakeADB()
        runtime = ModuleVoiceRuntime(adb, RuntimeManifest.load(manifest_path), root)
        runtime.prepared = True
        runtime.start_route()
        runtime.stop_route()
        assert any("--voice-route-session" in command for command in adb.commands)
        assert any("kill -TERM" in command for command in adb.commands)
        assert not any(command.endswith("/helper S") or command.endswith("/helper T") for command in adb.commands)


def test_voice_route_rejects_stale_owned_session_from_previous_call():
    class FakeADB:
        def shell(self, command, timeout=10):
            if "QDC507_ROUTE_READY" in command:
                return (
                    "QDC507_ROUTE_OWNED=1\n"
                    "QDC507_ROUTE_READY=1\n"
                    "QDC507_ROUTE_FOREIGN=0\n"
                )
            return ""

        def push(self, data, remote_path, mode=0o700):
            pass

    with TemporaryDirectory() as root:
        Path(root, "helper").write_bytes(b"helper")
        manifest_path = Path(root, "manifest.json")
        manifest_path.write_text(
            '{"runtimeVersion":"1","kernelRelease":"3.18.44",'
            '"helper":"helper","files":[{"name":"helper"}]}',
            encoding="utf-8",
        )
        runtime = ModuleVoiceRuntime(
            FakeADB(),
            RuntimeManifest.load(manifest_path),
            root,
        )
        runtime.prepared = True
        with pytest.raises(RuntimeError, match="stale owned module voice route"):
            runtime.start_route()


def test_runtime_does_not_unload_modules_when_route_owner_is_foreign():
    class FakeADB:
        def __init__(self):
            self.commands = []

        def shell(self, command, timeout=10):
            self.commands.append(command)
            if "QDC507_ROUTE_READY" in command:
                return (
                    "QDC507_ROUTE_OWNED=0\n"
                    "QDC507_ROUTE_READY=0\n"
                    "QDC507_ROUTE_FOREIGN=1\n"
                )
            return ""

        def push(self, data, remote_path, mode=0o700):
            pass

    with TemporaryDirectory() as root:
        Path(root, "helper").write_bytes(b"helper")
        manifest_path = Path(root) / "manifest.json"
        manifest_path.write_text(
            '{"runtimeVersion":"1","kernelRelease":"3.18.44",'
            '"helper":"helper","files":[{"name":"helper"},{"name":"driver.ko"}],'
            '"modules":[{"name":"driver","file":"driver.ko"}]}',
            encoding="utf-8",
        )
        adb = FakeADB()
        runtime = ModuleVoiceRuntime(adb, RuntimeManifest.load(manifest_path), root)
        runtime.prepared = True
        runtime.loaded_here = ["driver"]
        try:
            runtime.cleanup()
        except RuntimeError:
            pass
        else:
            raise AssertionError("foreign route owner was not rejected")
        assert not any("rmmod" in command for command in adb.commands)


def test_module_voice_controller_closes_adb_between_start_and_cleanup():
    route = {"owned": False, "ready": False}

    class FakeADB:
        def shell(self, command, timeout=10):
            if command == "id -u":
                return "0"
            if command == "uname -r":
                return "3.18.44"
            if "QDC507_ROUTE_READY" in command:
                return (
                    f"QDC507_ROUTE_OWNED={int(route['owned'])}\n"
                    f"QDC507_ROUTE_READY={int(route['ready'])}\n"
                    "QDC507_ROUTE_FOREIGN=0\n"
                )
            if "QDC507_ROUTE_START_STATUS" in command:
                route["owned"] = True
                route["ready"] = True
                return "QDC507_ROUTE_START_STATUS=0"
            if "QDC507_ROUTE_STOP" in command:
                route["owned"] = False
                route["ready"] = False
                return "QDC507_ROUTE_STOP=1"
            marker = next(iter(re.findall(r"QDC507_[A-Z0-9_]+", command)), None)
            if marker:
                return f"{marker}=0"
            return ""

        def push(self, data, remote_path, mode=0o700):
            pass

    with TemporaryDirectory() as root:
        Path(root, "helper").write_bytes(b"helper")
        manifest_path = Path(root, "manifest.json")
        manifest_path.write_text(
            '{"runtimeVersion":"1","kernelRelease":"3.18.44",'
            '"helper":"helper","files":[{"name":"helper"}]}',
            encoding="utf-8",
        )
        opened = []
        closed = []

        def open_client():
            opened.append(True)
            return FakeADB(), lambda: closed.append(True)

        controller = ModuleVoiceController(
            open_client, RuntimeManifest.load(manifest_path), root,
        )
        controller.prepare_and_start()
        assert controller.active
        controller.stop_and_cleanup()
        assert not controller.active
        assert not controller.runtime.prepared
        assert len(opened) == len(closed) == 2


def test_voice_route_readiness_checks_real_pcm_and_audio_enable_state():
    class FakeADB:
        def __init__(self):
            self.commands = []

        def shell(self, command, timeout=10):
            self.commands.append(command)
            return (
                "QDC507_ROUTE_OWNED=1\n"
                "QDC507_ROUTE_READY=0\n"
                "QDC507_ROUTE_FOREIGN=0\n"
            )

        def push(self, data, remote_path, mode=0o700):
            pass

    with TemporaryDirectory() as root:
        Path(root, "helper").write_bytes(b"helper")
        manifest_path = Path(root, "manifest.json")
        manifest_path.write_text(
            '{"runtimeVersion":"1","kernelRelease":"3.18.44",'
            '"helper":"helper","files":[{"name":"helper"}]}',
            encoding="utf-8",
        )
        adb = FakeADB()
        runtime = ModuleVoiceRuntime(adb, RuntimeManifest.load(manifest_path), root)
        assert not runtime.route_ready()
        command = adb.commands[-1]
        assert "/sys/class/android_usb/f_audio/audio_enable" in command
        assert "/proc/asound/card0/pcm4p/sub0/status" in command
        assert "/proc/asound/card0/pcm4c/sub0/status" in command
        assert "VoLTE route session active on hw:0,4" in command


def test_protected_qdc_voice_modules_are_not_hot_unloaded():
    class FakeADB:
        def __init__(self):
            self.commands = []

        def shell(self, command, timeout=10):
            self.commands.append(command)
            if "QDC507_ROUTE_READY" in command:
                return (
                    "QDC507_ROUTE_OWNED=0\n"
                    "QDC507_ROUTE_READY=0\n"
                    "QDC507_ROUTE_FOREIGN=0\n"
                )
            return ""

        def push(self, data, remote_path, mode=0o700):
            pass

    with TemporaryDirectory() as root:
        Path(root, "helper").write_bytes(b"helper")
        manifest_path = Path(root, "manifest.json")
        manifest_path.write_text(
            '{"runtimeVersion":"1","kernelRelease":"3.18.44",'
            '"helper":"helper","files":[{"name":"helper"}]}',
            encoding="utf-8",
        )
        adb = FakeADB()
        runtime = ModuleVoiceRuntime(adb, RuntimeManifest.load(manifest_path), root)
        runtime.loaded_here = ["qdc507_aprv3", "qdc507_voice"]
        runtime.cleanup()
        assert runtime.loaded_here == ["qdc507_aprv3", "qdc507_voice"]
        assert not any("rmmod" in command for command in adb.commands)


def test_controller_recovers_when_audio_enable_detaches_initial_adb_session():
    state = {"ready": False, "open_count": 0}
    closed = []

    class FakeADB:
        def shell(self, command, timeout=10):
            if command == "id -u":
                return "0"
            if command == "uname -r":
                return "3.18.44"
            if "QDC507_ROUTE_READY" in command:
                return (
                    f"QDC507_ROUTE_OWNED={int(state['ready'])}\n"
                    f"QDC507_ROUTE_READY={int(state['ready'])}\n"
                    "QDC507_ROUTE_FOREIGN=0\n"
                )
            if "QDC507_ROUTE_START_STATUS" in command:
                state["ready"] = True
                raise OSError("detached")
            marker = next(iter(re.findall(r"QDC507_[A-Z0-9_]+", command)), None)
            if marker:
                return f"{marker}=0"
            return ""

        def push(self, data, remote_path, mode=0o700):
            pass

    with TemporaryDirectory() as root:
        Path(root, "helper").write_bytes(b"helper")
        manifest_path = Path(root, "manifest.json")
        manifest_path.write_text(
            '{"runtimeVersion":"1","kernelRelease":"3.18.44",'
            '"helper":"helper","files":[{"name":"helper"}]}',
            encoding="utf-8",
        )

        def open_client():
            state["open_count"] += 1
            return FakeADB(), lambda: closed.append(True)

        controller = ModuleVoiceController(
            open_client,
            RuntimeManifest.load(manifest_path),
            root,
            reconnect_timeout=1.0,
        )
        controller.prepare_and_start()
        assert controller.active
        assert state["open_count"] == len(closed) == 2

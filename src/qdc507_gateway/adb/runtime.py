from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol, Tuple


class RuntimeErrorBase(RuntimeError):
    pass


class ForeignVoiceRouteError(RuntimeErrorBase):
    pass


class ADBClient(Protocol):
    def shell(self, command: str, timeout: float = 10.0) -> str:
        ...

    def push(self, data: bytes, remote_path: str, mode: int = 0o700) -> None:
        ...


@dataclass(frozen=True)
class RuntimeFile:
    name: str
    mode: int = 0o700
    sha256: Optional[str] = None


@dataclass(frozen=True)
class RuntimeModule:
    name: str
    file: str
    mode: int = 0o700


@dataclass(frozen=True)
class RuntimeManifest:
    runtime_version: str
    kernel_release: str
    helper: str
    files: Tuple[RuntimeFile, ...]
    modules: Tuple[RuntimeModule, ...] = ()
    required_devices: Tuple[str, ...] = ()
    card_name: Optional[str] = None

    @classmethod
    def load(cls, path: str | Path) -> "RuntimeManifest":
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            raise RuntimeErrorBase("runtime manifest must be a JSON object")
        runtime_version = raw.get("runtime_version", raw.get("runtimeVersion"))
        kernel_release = raw.get("kernel_release", raw.get("kernelRelease"))
        card_name = raw.get("card_name", raw.get("cardName"))
        file_entries = raw.get("files", [])
        module_entries = raw.get("modules", [])
        helper = raw.get("helper")
        if not isinstance(file_entries, list) or not isinstance(module_entries, list):
            raise RuntimeErrorBase("runtime manifest files/modules must be arrays")
        if any(not isinstance(item, dict) for item in (*file_entries, *module_entries)):
            raise RuntimeErrorBase("runtime manifest file/module entries must be objects")
        format_version = raw.get("formatVersion", raw.get("format_version", 1))
        if isinstance(format_version, bool) or format_version != 1:
            raise RuntimeErrorBase("unsupported runtime manifest format")
        names = [helper or ""]
        names.extend(item.get("name", item.get("file", "")) for item in file_entries)
        for item in module_entries:
            names.append(item.get("name", ""))
            names.append(item.get("file", item.get("name", "")))
        if any(
            not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
            for name in names
        ):
            raise RuntimeErrorBase("runtime manifest contains an unsafe filename")
        if card_name is not None and (
            not isinstance(card_name, str)
            or not card_name
            or len(card_name) > 128
            or not re.fullmatch(r"[A-Za-z0-9_. -]+", card_name)
        ):
            raise RuntimeErrorBase("runtime manifest contains an unsafe card name")
        required_devices_value = raw.get("requiredDevices", raw.get("required_devices", ()))
        if not isinstance(required_devices_value, (list, tuple)):
            raise RuntimeErrorBase("runtime manifest required devices must be an array")
        if any(not isinstance(device, str) for device in required_devices_value):
            raise RuntimeErrorBase("runtime manifest contains an invalid device path")
        required_devices = tuple(required_devices_value)
        if any(
            not re.fullmatch(r"/dev/snd/[A-Za-z0-9_.-]+", device)
            for device in required_devices
        ):
            raise RuntimeErrorBase("runtime manifest contains an unsafe device path")
        if not isinstance(runtime_version, str) or not runtime_version:
            raise RuntimeErrorBase("runtime manifest is incomplete")
        if not isinstance(kernel_release, str) or not kernel_release or not helper:
            raise RuntimeErrorBase("runtime manifest is incomplete")

        def mode_value(item: Any) -> int:
            mode = item.get("mode", 0o700)
            if isinstance(mode, bool) or not isinstance(mode, int) or mode < 0 or mode & ~0o777:
                raise RuntimeErrorBase("runtime manifest contains an invalid file mode")
            return mode

        files = tuple(
            RuntimeFile(
                item.get("name", item.get("file")),
                mode_value(item),
                item.get("sha256"),
            )
            for item in file_entries
        )
        if any(
            item.sha256 is not None
            and (
                not isinstance(item.sha256, str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", item.sha256)
            )
            for item in files
        ):
            raise RuntimeErrorBase("runtime manifest contains an invalid SHA-256")
        modules = tuple(
            RuntimeModule(
                name=item.get("name", Path(item.get("file", "")).stem),
                file=item.get("file", item.get("name", "")),
                mode=mode_value(item),
            )
            for item in module_entries
        )
        if any(not re.fullmatch(r"[A-Za-z0-9_]+", item.name) for item in modules):
            raise RuntimeErrorBase("runtime manifest contains an unsafe module name")
        file_names = {item.name for item in files}
        if any(item.file not in file_names for item in modules):
            raise RuntimeErrorBase("every runtime module must be a checked file entry")
        if not any(item.name == helper for item in files):
            raise RuntimeErrorBase("runtime helper is not included in files")
        return cls(
            runtime_version,
            kernel_release,
            helper,
            files,
            modules,
            required_devices,
            card_name,
        )


@dataclass(frozen=True)
class VoiceRouteStatus:
    owned: bool
    ready: bool
    foreign: bool


class ModuleVoiceRuntime:
    """Safe module-side UAC voice runtime lifecycle.

    Every path below belongs to the QDC507 module and is reached only through
    ADB. The host never treats ``/dev/ttyGS0`` or ``/run/voc_svr`` as local
    files. Persistent modem configuration is not changed by this runtime.
    """

    _PROTECTED_RESIDENT_MODULES = {"qdc507_aprv3", "qdc507_voice"}

    def __init__(
        self,
        client: ADBClient,
        manifest: RuntimeManifest,
        local_dir: str | Path,
        remote_dir: str = "/data/local/tmp/qdc507-gateway",
    ):
        if not isinstance(remote_dir, str) or not re.fullmatch(
            r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+", remote_dir
        ):
            raise RuntimeErrorBase("runtime remote directory is unsafe")
        self.client = client
        self.manifest = manifest
        self.local_dir = Path(local_dir)
        self.remote_dir = remote_dir
        self.prepared = False
        self.loaded_here: list[str] = []
        self.route_started = False
        self.route_pid_file = "/run/qdc507-gateway-voice-route.pid"
        self.route_log_file = "/run/qdc507-gateway-voice-route.log"
        self.calibration_pid_file = "/run/qdc507-gateway-alsaucm.pid"
        self.calibration_log_file = "/run/qdc507-gateway-alsaucm.log"

    def _run_status(
        self,
        command: str,
        marker: str,
        timeout: float = 10.0,
    ) -> tuple[int, str]:
        if not re.fullmatch(r"[A-Z0-9_]+", marker):
            raise ValueError("unsafe status marker")
        output = self.client.shell(
            "(%s); qdc_status=$?; printf '\\n%s=%%s\\n' \"$qdc_status\""
            % (command, marker),
            timeout=timeout,
        )
        matches = re.findall(r"(?:^|\n)%s=(\d+)(?:\r?$)" % marker, output, re.MULTILINE)
        if len(matches) != 1:
            raise RuntimeErrorBase("module command returned no reliable status")
        detail = re.sub(
            r"(?:^|\n)%s=\d+(?:\r?$)" % marker,
            "",
            output,
            flags=re.MULTILINE,
        ).strip()
        return int(matches[0]), detail

    def _checked(
        self,
        command: str,
        marker: str,
        error: str,
        timeout: float = 10.0,
    ) -> str:
        status, detail = self._run_status(command, marker, timeout)
        if status != 0:
            suffix = (": " + detail[-1200:]) if detail else ""
            raise RuntimeErrorBase(error + suffix)
        return detail

    def prepare(self) -> None:
        if self.prepared:
            if self._sound_devices_ready():
                return
            self.prepared = False
            raise RuntimeErrorBase("prepared module voice devices disappeared")
        try:
            uid = self.client.shell("id -u", timeout=8).strip()
            if uid != "0":
                raise RuntimeErrorBase("module ADB must provide root control")
            release = self.client.shell("uname -r", timeout=8).strip()
            if self.manifest.kernel_release not in release.split():
                raise RuntimeErrorBase("module kernel release does not match the runtime manifest")
            self._checked(
                "mkdir -p '%s' && chmod 700 '%s'" % (self.remote_dir, self.remote_dir),
                "QDC507_MKDIR_STATUS",
                "module runtime directory preparation failed",
            )
            pushed = self._push_checked_files()

            if not self._sound_devices_ready():
                legacy_status, _ = self._run_status(
                    "grep -q '^qdc507_afe ' /proc/modules",
                    "QDC507_LEGACY_MODULE_STATUS",
                    timeout=8,
                )
                if legacy_status == 0:
                    raise RuntimeErrorBase(
                        "legacy qdc507_afe is loaded; refusing a live voice-driver switch"
                    )
                if legacy_status != 1:
                    raise RuntimeErrorBase("cannot inspect the legacy module state")
                self._load_missing_modules(pushed)

            if not self._wait_for_sound_devices():
                diagnostics = self._diagnostics("dmesg | tail -n 80")
                raise RuntimeErrorBase(
                    "module voice ALSA devices did not appear"
                    + ((": " + diagnostics) if diagnostics else "")
                )
            self._ensure_voice_calibration()
            self._checked(
                "test -c /dev/ttyGS0 && test -p /run/voc_svr",
                "QDC507_VOICE_ENDPOINT_STATUS",
                "module is missing ttyGS0 or voc_svr",
                timeout=8,
            )
            self._checked(
                "'%s/%s' --check 2>&1" % (self.remote_dir, self.manifest.helper),
                "QDC507_HELPER_STATUS",
                "module voice helper self-check failed",
                timeout=15,
            )
            self.prepared = True
        except Exception:
            try:
                self.cleanup()
            except Exception as cleanup_error:
                raise RuntimeErrorBase(
                    "runtime preparation failed and cleanup was incomplete: %s" % cleanup_error
                ) from cleanup_error
            raise

    def _push_checked_files(self) -> set[str]:
        pushed: set[str] = set()
        resource_root = self.local_dir.resolve()
        for item in self.manifest.files:
            local = (resource_root / item.name).resolve()
            try:
                local.relative_to(resource_root)
            except ValueError as exc:
                raise RuntimeErrorBase("runtime file escapes its resource directory") from exc
            if not local.is_file():
                raise RuntimeErrorBase("runtime file is missing: %s" % item.name)
            data = local.read_bytes()
            if item.sha256 is not None:
                actual = hashlib.sha256(data).hexdigest()
                if actual.lower() != item.sha256.lower():
                    raise RuntimeErrorBase("runtime file hash mismatch: %s" % item.name)
            self.client.push(data, "%s/%s" % (self.remote_dir, item.name), item.mode)
            pushed.add(item.name)
        return pushed

    def _load_missing_modules(self, pushed: set[str]) -> None:
        for module in self.manifest.modules:
            if module.file not in pushed:
                raise RuntimeErrorBase("runtime module was not pushed from a checked file entry")
            loaded, _ = self._run_status(
                "grep -q '^%s ' /proc/modules" % module.name,
                "QDC507_MODULE_STATUS",
                timeout=8,
            )
            if loaded == 0:
                continue
            if loaded != 1:
                raise RuntimeErrorBase("cannot inspect module state: %s" % module.name)
            status, detail = self._run_status(
                "insmod '%s/%s' 2>&1" % (self.remote_dir, module.file),
                "QDC507_INSMOD_STATUS",
                timeout=20,
            )
            if status != 0:
                diagnostics = self._diagnostics("dmesg | tail -n 80")
                combined = "\n".join(item for item in (detail, diagnostics) if item)
                raise RuntimeErrorBase(
                    "module load failed: %s%s"
                    % (module.name, (": " + combined[-1600:]) if combined else "")
                )
            self.loaded_here.append(module.name)

    def _sound_device_checks(self) -> str:
        checks = ["test -c '%s'" % device for device in self.manifest.required_devices]
        if self.manifest.card_name:
            checks.append("grep -Fq '%s' /proc/asound/cards" % self.manifest.card_name)
        return " && ".join(checks) if checks else "true"

    def _sound_devices_ready(self) -> bool:
        status, _ = self._run_status(
            self._sound_device_checks(),
            "QDC507_SOUND_STATUS",
            timeout=8,
        )
        return status == 0

    def _wait_for_sound_devices(self) -> bool:
        checks = self._sound_device_checks()
        command = (
            "ready=0; n=0; while test \"$n\" -lt 100; do "
            "if %s; then ready=1; break; fi; sleep 0.2; n=$((n+1)); done; "
            "test \"$ready\" -eq 1" % checks
        )
        status, _ = self._run_status(command, "QDC507_SOUND_WAIT_STATUS", timeout=25)
        return status == 0

    def _ensure_voice_calibration(self) -> None:
        command = (
            "owned=0; if test -s '%s'; then "
            "read pid expected_start < '%s' || true; "
            "current_start=$(cut -d ' ' -f 22 \"/proc/$pid/stat\" 2>/dev/null); "
            "argv0=$(tr '\\000' '\\n' < \"/proc/$pid/cmdline\" 2>/dev/null | sed -n '1p'); "
            "test \"$current_start\" = \"$expected_start\" && "
            "test \"$argv0\" = /usr/bin/alsaucm_test && owned=1 || true; fi; "
            "if test \"$owned\" -eq 0; then "
            "for proc in /proc/[0-9]*; do test -r \"$proc/cmdline\" || continue; "
            "argv0=$(tr '\\000' '\\n' < \"$proc/cmdline\" 2>/dev/null | sed -n '1p'); "
            "test \"$argv0\" = /usr/bin/alsaucm_test || continue; oldpid=${proc##*/}; "
            "kill -TERM \"$oldpid\" 2>/dev/null || true; n=0; "
            "while kill -0 \"$oldpid\" 2>/dev/null && test \"$n\" -lt 30; do "
            "sleep 0.1; n=$((n+1)); done; kill -0 \"$oldpid\" 2>/dev/null && exit 71 || true; "
            "done; rm -f /run/alsaucm_test '%s' '%s'; "
            "nohup /usr/bin/alsaucm_test </dev/null >> '%s' 2>&1 & pid=$!; "
            "starttime=$(cut -d ' ' -f 22 \"/proc/$pid/stat\" 2>/dev/null); "
            "printf '%%s %%s\\n' \"$pid\" \"$starttime\" > '%s'; n=0; "
            "while test \"$n\" -lt 50 && test ! -p /run/alsaucm_test; do "
            "kill -0 \"$pid\" 2>/dev/null || exit 72; sleep 0.1; n=$((n+1)); done; "
            "test -p /run/alsaucm_test || exit 73; fi; "
            "if ! grep -q 'ACDB -> Sent VocProc Cal!' '%s' 2>/dev/null; then "
            "printf 'open snd_soc_msm_9x07_Tomtom_I2S\\n' > /run/alsaucm_test; "
            "printf 'set _verb VoLTE\\n' > /run/alsaucm_test; "
            "printf 'set _enadev Auxpcm Rx\\n' > /run/alsaucm_test; "
            "printf 'set _enadev Auxpcm Tx\\n' > /run/alsaucm_test; n=0; "
            "while test \"$n\" -lt 100; do "
            "grep -q 'ACDB -> Sent VocProc Cal!' '%s' 2>/dev/null && break; "
            "sleep 0.1; n=$((n+1)); done; fi; "
            "grep -q 'ACDB -> Sent VocProc Cal!' '%s' 2>/dev/null"
            % (
                self.calibration_pid_file,
                self.calibration_pid_file,
                self.calibration_pid_file,
                self.calibration_log_file,
                self.calibration_log_file,
                self.calibration_pid_file,
                self.calibration_log_file,
                self.calibration_log_file,
                self.calibration_log_file,
            )
        )
        status, detail = self._run_status(
            command,
            "QDC507_CALIBRATION_STATUS",
            timeout=25,
        )
        if status != 0:
            log = self._diagnostics("tail -n 100 '%s'" % self.calibration_log_file)
            raise RuntimeErrorBase(
                "module VoLTE ACDB calibration did not become ready"
                + ((": " + (log or detail)[-1600:]) if (log or detail) else "")
            )

    def start_route(self) -> None:
        if not self.prepared:
            raise RuntimeErrorBase("prepare must complete before starting voice route")
        status = self._route_status()
        if status.ready:
            if not self.route_started:
                # Do not silently adopt a helper left behind by an uncertain
                # prior cleanup. Its PCM handles may still be RUNNING while
                # carrying silence.
                raise RuntimeErrorBase(
                    "a stale owned module voice route survived the previous session"
                )
            self.route_started = True
            return
        if status.foreign:
            raise ForeignVoiceRouteError("a foreign module voice route owns the route marker")
        if not status.owned:
            self._ensure_route_quiescent()
            helper_path = "%s/%s" % (self.remote_dir, self.manifest.helper)
            launch = (
                "rm -f '%s' '%s'; "
                "nohup '%s' --voice-route-session --verbose </dev/null "
                ">> '%s' 2>&1 & pid=$!; "
                "starttime=$(cut -d ' ' -f 22 \"/proc/$pid/stat\" 2>/dev/null); "
                "case \"$pid:$starttime\" in :*|*:|*[!0-9:]*) false;; "
                "*) printf '%%s %%s\\n' \"$pid\" \"$starttime\" > '%s';; esac"
                % (
                    self.route_pid_file,
                    self.route_log_file,
                    helper_path,
                    self.route_log_file,
                    self.route_pid_file,
                )
            )
            self._checked(
                launch,
                "QDC507_ROUTE_START_STATUS",
                "module voice route launch failed",
                timeout=8,
            )
        for _ in range(30):
            status = self._route_status()
            if status.ready:
                self.route_started = True
                return
            if status.foreign:
                raise ForeignVoiceRouteError("a foreign module voice route owns the route marker")
            time.sleep(0.1)
        detail = self.route_diagnostics()
        raise RuntimeErrorBase(
            "module D4/UAC voice route did not enter RUNNING"
            + ((": " + detail[-1600:]) if detail else "")
        )

    def route_ready(self) -> bool:
        status = self._route_status()
        if status.foreign:
            raise ForeignVoiceRouteError("a foreign module voice route owns the route marker")
        return status.ready

    def stop_route(self) -> None:
        status = self._route_status()
        if status.foreign:
            raise ForeignVoiceRouteError("a foreign module voice route owns the route marker")
        should_restore = self.route_started or status.owned
        if status.owned:
            result = self._stop_owned_route()
            if "QDC507_ROUTE_STOP=1" not in result:
                raise RuntimeErrorBase("module voice route stop was not confirmed")
            for _ in range(20):
                status = self._route_status()
                if status.foreign:
                    raise ForeignVoiceRouteError("voice route ownership changed during cleanup")
                if not status.owned:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeErrorBase(
                    "module voice helper did not stop; refusing forced termination"
                )
            self._checked(
                "rm -f '%s'" % self.route_pid_file,
                "QDC507_ROUTE_MARKER_CLEANUP",
                "voice route marker cleanup failed",
            )
        elif not status.foreign:
            self._checked(
                "rm -f '%s'" % self.route_pid_file,
                "QDC507_ROUTE_MARKER_CLEANUP",
                "stale voice route marker cleanup failed",
            )
        if should_restore:
            self._restore_voice_route()
            # Force the next call through helper self-check and calibration
            # checks instead of trusting state prepared before this teardown.
            self.prepared = False
        self.route_started = False

    def _ensure_route_quiescent(self) -> None:
        status, _ = self._run_status(
            "test \"$(cat /sys/class/android_usb/f_audio/audio_enable 2>/dev/null)\" = 0 && "
            "! grep -q '^state: RUNNING' "
            "/proc/asound/card0/pcm4p/sub0/status 2>/dev/null && "
            "! grep -q '^state: RUNNING' "
            "/proc/asound/card0/pcm4c/sub0/status 2>/dev/null",
            "QDC507_ROUTE_QUIESCENT_STATUS",
            timeout=8,
        )
        if status != 0:
            self._restore_voice_route()

    def _route_status(self) -> VoiceRouteStatus:
        helper_path = "%s/%s" % (self.remote_dir, self.manifest.helper)
        output = self.client.shell(
            "owned=0; ready=0; foreign=0; if test -s '%s'; then "
            "read pid expected_start < '%s' || true; "
            "current_start=$(cut -d ' ' -f 22 \"/proc/$pid/stat\" 2>/dev/null); "
            "argv0=$(tr '\\000' '\\n' < \"/proc/$pid/cmdline\" 2>/dev/null | sed -n '1p'); "
            "args=$(tr '\\000' '\\n' < \"/proc/$pid/cmdline\" 2>/dev/null); "
            "if test \"$current_start\" = \"$expected_start\" && "
            "test \"$argv0\" = '%s' && "
            "printf '%%s\\n' \"$args\" | grep -q '^--voice-route-session$'; "
            "then owned=1; elif test -n \"$current_start\"; then foreign=1; fi; fi; "
            "if test \"$owned\" -eq 1 && "
            "grep -q 'VoLTE route session active on hw:0,4' '%s' 2>/dev/null && "
            "test \"$(cat /sys/class/android_usb/f_audio/audio_enable 2>/dev/null)\" = 1 && "
            "grep -q '^state: RUNNING' /proc/asound/card0/pcm4p/sub0/status 2>/dev/null && "
            "grep -q '^state: RUNNING' /proc/asound/card0/pcm4c/sub0/status 2>/dev/null; "
            "then ready=1; fi; "
            "printf 'QDC507_ROUTE_OWNED=%%s\\nQDC507_ROUTE_READY=%%s\\n"
            "QDC507_ROUTE_FOREIGN=%%s\\n' \"$owned\" \"$ready\" \"$foreign\""
            % (
                self.route_pid_file,
                self.route_pid_file,
                helper_path,
                self.route_log_file,
            ),
            timeout=8,
        )
        values = {}
        for name in ("OWNED", "READY", "FOREIGN"):
            match = re.search(
                r"(?:^|\n)QDC507_ROUTE_%s=([01])(?:\r?$)" % name,
                output,
                re.MULTILINE,
            )
            if match is None:
                raise RuntimeErrorBase("module voice route status was incomplete")
            values[name] = match.group(1) == "1"
        return VoiceRouteStatus(values["OWNED"], values["READY"], values["FOREIGN"])

    def _stop_owned_route(self) -> str:
        helper_path = "%s/%s" % (self.remote_dir, self.manifest.helper)
        return self.client.shell(
            "stopped=1; owned=0; foreign=0; if test -s '%s'; then "
            "read pid expected_start < '%s' || true; "
            "current_start=$(cut -d ' ' -f 22 \"/proc/$pid/stat\" 2>/dev/null); "
            "argv0=$(tr '\\000' '\\n' < \"/proc/$pid/cmdline\" 2>/dev/null | sed -n '1p'); "
            "args=$(tr '\\000' '\\n' < \"/proc/$pid/cmdline\" 2>/dev/null); "
            "if test \"$current_start\" = \"$expected_start\" && "
            "test \"$argv0\" = '%s' && "
            "printf '%%s\\n' \"$args\" | grep -q '^--voice-route-session$'; "
            "then owned=1; kill -TERM \"$pid\" 2>/dev/null || true; n=0; "
            "while test \"$n\" -lt 50; do "
            "current_start=$(cut -d ' ' -f 22 \"/proc/$pid/stat\" 2>/dev/null); "
            "test \"$current_start\" = \"$expected_start\" || break; "
            "sleep 0.1; n=$((n+1)); done; "
            "current_start=$(cut -d ' ' -f 22 \"/proc/$pid/stat\" 2>/dev/null); "
            "test \"$current_start\" = \"$expected_start\" && stopped=0 || true; "
            "elif test -n \"$current_start\"; then foreign=1; fi; fi; "
            "if test \"$owned\" -eq 0 && test \"$foreign\" -eq 0; then rm -f '%s'; fi; "
            "test \"$foreign\" -eq 1 && stopped=0; "
            "printf 'QDC507_ROUTE_STOP=%%s\\nQDC507_ROUTE_FOREIGN=%%s\\n' "
            "\"$stopped\" \"$foreign\""
            % (self.route_pid_file, self.route_pid_file, helper_path, self.route_pid_file),
            timeout=8,
        )

    def _restore_voice_route(self) -> None:
        last_detail = ""
        for _ in range(5):
            try:
                status, detail = self._run_status(
                    "echo 0 > /sys/class/android_usb/f_audio/audio_enable; "
                    "if test -p /run/voc_svr; then "
                    "printf 'T\\n' > /run/voc_svr; printf 'T\\n' > /run/voc_svr; "
                    "printf 'B\\n' > /run/voc_svr; fi; "
                    "test \"$(cat /sys/class/android_usb/f_audio/audio_enable)\" = 0 && "
                    "! grep -q '^state: RUNNING' "
                    "/proc/asound/card0/pcm4p/sub0/status 2>/dev/null && "
                    "! grep -q '^state: RUNNING' "
                    "/proc/asound/card0/pcm4c/sub0/status 2>/dev/null",
                    "QDC507_ROUTE_RESTORE_STATUS",
                    timeout=8,
                )
                if status == 0:
                    return
                last_detail = detail
            except Exception as exc:
                last_detail = str(exc)
            time.sleep(0.2)
        raise RuntimeErrorBase(
            "module voice route rollback was not confirmed"
            + ((": " + last_detail[-1200:]) if last_detail else "")
        )

    def route_diagnostics(self) -> str:
        return self._diagnostics(
            "test ! -f '%s' || tail -n 160 '%s'"
            % (self.route_log_file, self.route_log_file)
        )

    def _diagnostics(self, command: str) -> str:
        try:
            return self.client.shell(command, timeout=8).strip()[-2000:]
        except Exception:
            return ""

    def cleanup(self) -> None:
        first_error: Optional[Exception] = None
        if self.route_started:
            try:
                self.stop_route()
            except Exception as exc:
                first_error = exc
        if self.loaded_here and first_error is None:
            try:
                status = self._route_status()
                if status.foreign or status.owned:
                    raise ForeignVoiceRouteError(
                        "refusing module unload while a voice route process exists"
                    )
            except Exception as exc:
                first_error = exc
        retained: list[str] = []
        for module in reversed(self.loaded_here) if first_error is None else ():
            if module in self._PROTECTED_RESIDENT_MODULES:
                # These drivers have APR/DSP callbacks. Hot-unloading after
                # registration can race a late callback, so leave them resident
                # until the QDC507 itself reboots.
                retained.append(module)
                continue
            try:
                status, detail = self._run_status(
                    "rmmod '%s' 2>&1" % module,
                    "QDC507_RMMOD_STATUS",
                    timeout=12,
                )
            except Exception as exc:
                first_error = first_error or exc
                retained.append(module)
                continue
            if status != 0:
                first_error = first_error or RuntimeErrorBase(
                    "module unload failed: %s%s"
                    % (module, (": " + detail[-800:]) if detail else "")
                )
                retained.append(module)
        retained.reverse()
        self.loaded_here = retained
        self.prepared = False
        if first_error is not None:
            raise first_error


class ModuleVoiceController:
    """Open ADB only around route transitions, never during host PCM I/O."""

    def __init__(
        self,
        open_client: Callable[[], tuple[ADBClient, Callable[[], None]]],
        manifest: RuntimeManifest,
        local_dir: str | Path,
        exclusive_runner: Optional[Callable[[Callable[[], Any]], Awaitable[Any]]] = None,
        reconnect_timeout: float = 20.0,
    ):
        self.open_client = open_client
        self.manifest = manifest
        self.local_dir = Path(local_dir)
        self.exclusive_runner = exclusive_runner
        self.reconnect_timeout = reconnect_timeout
        self.runtime: ModuleVoiceRuntime | None = None
        self.active = False
        self.last_error: Optional[str] = None

    async def start_async(self) -> None:
        if self.exclusive_runner is None:
            await asyncio.to_thread(self.prepare_and_start)
        else:
            await self.exclusive_runner(self.prepare_and_start)

    async def stop_async(self) -> None:
        if self.exclusive_runner is None:
            await asyncio.to_thread(self.stop_and_cleanup)
        else:
            await self.exclusive_runner(self.stop_and_cleanup)

    def prepare_and_start(self) -> None:
        self.last_error = None
        deadline = time.monotonic() + self.reconnect_timeout
        client, close = self._open_until(deadline)
        launch_error: Optional[Exception] = None
        try:
            if self.runtime is None:
                self.runtime = ModuleVoiceRuntime(client, self.manifest, self.local_dir)
            else:
                self.runtime.client = client
            self.runtime.prepare()
            try:
                self.runtime.start_route()
            except ForeignVoiceRouteError:
                raise
            except Exception as exc:
                # audio_enable=1 can detach ADB after the helper and PID file
                # are already live. Close the stale handle and verify through
                # a fresh session before declaring the launch failed.
                launch_error = exc
            if self.runtime.route_started:
                self.active = True
                return
        except Exception as exc:
            self.active = False
            self.last_error = str(exc)[-1600:]
            raise
        finally:
            close()

        diagnostics = ""
        last_error: Optional[Exception] = launch_error
        while time.monotonic() < deadline:
            try:
                client, close = self._open_until(deadline)
                try:
                    assert self.runtime is not None
                    self.runtime.client = client
                    if self.runtime.route_ready():
                        self.runtime.route_started = True
                        self.active = True
                        self.last_error = None
                        return
                    diagnostics = self.runtime.route_diagnostics() or diagnostics
                finally:
                    close()
            except ForeignVoiceRouteError:
                raise
            except Exception as exc:
                last_error = exc
            time.sleep(0.2)

        self.active = False
        parts = [str(item) for item in (launch_error, last_error) if item]
        if diagnostics:
            parts.append(diagnostics)
        detail = "\n".join(dict.fromkeys(parts))[-1800:]
        self.last_error = detail or "module voice route did not become ready"
        self._best_effort_failed_start_cleanup()
        raise RuntimeErrorBase(self.last_error)

    def stop_and_cleanup(self) -> None:
        if self.runtime is None:
            self.active = False
            return
        deadline = time.monotonic() + self.reconnect_timeout
        last_error: Optional[Exception] = None
        try:
            while time.monotonic() < deadline:
                try:
                    client, close = self._open_until(deadline)
                    try:
                        self.runtime.client = client
                        self.runtime.stop_route()
                        self.last_error = None
                        return
                    finally:
                        close()
                except ForeignVoiceRouteError:
                    raise
                except Exception as exc:
                    last_error = exc
                time.sleep(0.2)
            if last_error is not None:
                raise last_error
            raise RuntimeErrorBase("module voice route cleanup timed out")
        except Exception as exc:
            self.last_error = str(exc)[-1600:]
            raise
        finally:
            self.active = False

    def _open_until(self, deadline: float) -> tuple[ADBClient, Callable[[], None]]:
        last_error: Optional[Exception] = None
        while True:
            try:
                return self.open_client()
            except Exception as exc:
                last_error = exc
            if time.monotonic() >= deadline:
                raise RuntimeErrorBase(
                    "module ADB did not reconnect%s"
                    % ((": " + type(last_error).__name__) if last_error else "")
                ) from last_error
            time.sleep(0.25)

    def _best_effort_failed_start_cleanup(self) -> None:
        if self.runtime is None:
            return
        deadline = time.monotonic() + min(5.0, self.reconnect_timeout)
        try:
            client, close = self._open_until(deadline)
            try:
                self.runtime.client = client
                self.runtime.stop_route()
            finally:
                close()
        except Exception:
            pass

    def status(self) -> dict[str, object]:
        runtime = self.runtime
        return {
            "configured": True,
            "active": self.active,
            "prepared": bool(runtime and runtime.prepared),
            "runtime_version": self.manifest.runtime_version,
            "resident_modules_loaded_here": [] if runtime is None else list(runtime.loaded_here),
            "last_error": self.last_error,
        }

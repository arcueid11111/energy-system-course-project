"""Run a pinned local solver without a server or arbitrary user shell commands."""

import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .schemas import SolverRuntime

ALLOWED = {
    "foamVersion",
    "blockMesh",
    "checkMesh",
    "createNonConformalCouples",
    "snappyHexMesh",
    "createZones",
    "createBaffles",
    "foamRun",
    "foamToVTK",
    "foamDictionary",
    "transformPoints",
}


class LocalSolver:
    def __init__(self, config: SolverRuntime):
        self.config = config

    def docker_base(self):
        command = shutil.which(self.config.docker_command) or self.config.docker_command
        args = [command]
        if self.config.docker_context:
            args += ["--context", self.config.docker_context]
        return args

    def command(self, program, args, case, container_name=None):
        if program not in ALLOWED:
            raise ValueError("不允许执行此求解器命令")
        c = self.config
        # Only a static, developer-owned shell fragment is used; values are argv.
        script = (
            '. "$1" >/dev/null; shift; '
            'if [ "$1" = foamVersion ]; then printf "OpenFOAM-%s\\n" "$WM_PROJECT_VERSION"; '
            'else exec "$@"; fi'
        )
        if c.mode == "docker":
            path = str(Path(case).resolve())
            if "," in path:
                raise ValueError("Docker 工作目录不能包含逗号")
            cmd = self.docker_base() + [
                "run",
                "--rm",
                "--network=none",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--cpus",
                str(c.cpus),
                "--memory",
                f"{c.memory_gb}g",
                "--name",
                container_name or "flowstate-" + uuid.uuid4().hex,
                "--mount",
                f"type=bind,source={path},target=/case",
                "--workdir",
                "/case",
            ]
            if os.name != "nt":
                cmd += ["--user", f"{os.getuid()}:{os.getgid()}"]
            return cmd + [
                c.image,
                "bash",
                "--noprofile",
                "--norc",
                "-c",
                script,
                "flowstate",
                c.bashrc,
                program,
                *args,
            ]
        if c.mode == "wsl":
            return [
                "wsl.exe",
                "--distribution",
                c.wsl_distribution,
                "--exec",
                "bash",
                "-c",
                'cd "$1"; shift; ' + script,
                "flowstate",
                self.wsl_path(case),
                c.bashrc,
                program,
                *args,
            ]
        return [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            script,
            "flowstate",
            c.bashrc,
            program,
            *args,
        ]

    def wsl_path(self, path):
        value = subprocess.run(
            [
                "wsl.exe",
                "--distribution",
                self.config.wsl_distribution,
                "--exec",
                "wslpath",
                "-a",
                str(Path(path).resolve()),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout.strip()
        if not value.startswith("/"):
            raise ValueError("无法转换 WSL 工作目录")
        return value

    def probe(self, workspace):
        workspace = Path(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            self.command("foamVersion", [], workspace),
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )
        version = result.stdout.strip()
        if result.returncode or not re.search(r"(?:OpenFOAM[- ]|^|\n)14(?:\b|\.)", version):
            raise ValueError("需要 OpenFOAM Foundation 14。检测输出：" + version[-1200:])
        return {"version": version, "mode": self.config.mode, "computation": "local"}

    def _terminate(self, process, name):
        if self.config.mode == "docker":
            subprocess.run(
                self.docker_base() + ["rm", "--force", name], capture_output=True, timeout=20
            )
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=20
            )
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    def run(self, program, args, case, progress=None, start=0.1, span=0.7, end_time=None):
        case = Path(case).resolve()
        name = "flowstate-" + uuid.uuid4().hex
        cmd = self.command(program, list(args), case, name)
        log = case / f"log.{program}"
        lines = queue.Queue()
        tail = []
        deadline = time.monotonic() + self.config.timeout_seconds
        kwargs = (
            {"start_new_session": True}
            if os.name != "nt"
            else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        )
        with log.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                cmd,
                cwd=case,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                **kwargs,
            )

            def reader():
                for line in process.stdout:
                    lines.put(line)
                lines.put(None)

            thread = threading.Thread(target=reader, daemon=True)
            thread.start()
            done = False
            last_notice = 0
            last_t = 0
            try:
                while not done:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"{program} 超过运行时限")
                    try:
                        line = lines.get(timeout=0.25)
                    except queue.Empty:
                        line = ""
                    if line is None:
                        done = True
                        continue
                    if line:
                        output.write(line)
                        output.flush()
                        tail = (tail + [line])[-40:]
                        match = re.match(r"Time = ([\deE.+-]+)s?\s*$", line.strip())
                        if match:
                            last_t = float(match[1])
                    if progress and time.monotonic() - last_notice > 0.5:
                        # Calling the queue callback also checks cooperative cancellation.
                        fraction = min(0.999, last_t / end_time) if end_time else 0.1
                        progress(start + span * fraction, f"{program} · t={last_t:.7g} s")
                        last_notice = time.monotonic()
                process.wait(timeout=30)
                thread.join(timeout=5)
                if process.returncode:
                    raise ValueError(f"{program} 失败，日志 {log}\n" + "".join(tail)[-5000:])
            except BaseException:
                self._terminate(process, name)
                thread.join(timeout=5)
                raise
            finally:
                process.stdout.close()
        return {"program": program, "log": str(log), "last_time_s": last_t}

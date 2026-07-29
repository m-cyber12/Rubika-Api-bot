"""Android Termux Worker for Rubika Agent Phase 2.

Runs on the owner's Android phone, makes outbound HTTPS requests to Render,
and executes only a fixed allowlist of Termux:API actions. It never accepts or
executes arbitrary shell commands.
"""

import fcntl
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

VERSION = "android-worker-v1.0"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "worker_config.json"
STATE_FILE = BASE_DIR / ".android_worker_completed.json"
LOCK_FILE = BASE_DIR / ".android_worker.lock"

CAPABILITIES = [
    "device_status",
    "battery_status",
    "notify",
    "open_url",
    "set_volume",
    "speak",
    "vibrate",
    "torch",
    "open_settings",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("android-worker")


class WorkerError(RuntimeError):
    pass


def _load_config():
    config = {}
    if CONFIG_FILE.exists():
        try:
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                config = loaded
        except Exception as exc:
            raise WorkerError(f"worker_config.json invalid: {exc}") from exc

    server_url = os.environ.get("WORKER_SERVER_URL", config.get("server_url", ""))
    token = os.environ.get("WORKER_TOKEN", config.get("worker_token", ""))
    worker_id = os.environ.get("ANDROID_WORKER_ID", config.get("worker_id", "android-phone"))
    poll_interval = os.environ.get("WORKER_POLL_INTERVAL", config.get("poll_interval", 3))

    server_url = str(server_url).strip().rstrip("/")
    token = str(token).strip()
    worker_id = str(worker_id).strip()
    try:
        poll_interval = max(2.0, min(30.0, float(poll_interval)))
    except (TypeError, ValueError):
        poll_interval = 3.0

    parsed = urlparse(server_url)
    local_hosts = {"localhost", "127.0.0.1"}
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in local_hosts
    ):
        raise WorkerError("server_url must use HTTPS (HTTP is allowed only for localhost)")
    if len(token) < 32:
        raise WorkerError("worker_token must be at least 32 characters")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", worker_id):
        raise WorkerError("worker_id contains invalid characters")

    return {
        "server_url": server_url,
        "token": token,
        "worker_id": worker_id,
        "poll_interval": poll_interval,
    }


def _atomic_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _load_completed():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _remember_completed(job_id, payload):
    completed = _load_completed()
    completed[job_id] = payload
    if len(completed) > 100:
        for old_id in list(completed)[: len(completed) - 100]:
            completed.pop(old_id, None)
    _atomic_json(STATE_FILE, completed)


def _api_request(config, path, method="GET", payload=None, timeout=25):
    url = config["server_url"] + path
    data = None
    headers = {
        "Authorization": "Bearer " + config["token"],
        "Accept": "application/json",
        "User-Agent": VERSION,
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(1_000_000).decode("utf-8", errors="replace")
        parsed = json.loads(raw) if raw else {}
        if not isinstance(parsed, dict):
            raise WorkerError("Server returned non-object JSON")
        return parsed
    except HTTPError as exc:
        try:
            body = exc.read(1000).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise WorkerError(f"Server HTTP {exc.code}: {body[:300]}") from exc
    except (URLError, TimeoutError) as exc:
        raise WorkerError(f"Network error: {type(exc).__name__}") from exc
    except json.JSONDecodeError as exc:
        raise WorkerError("Server returned invalid JSON") from exc


def _require_command(name):
    path = shutil.which(name)
    if not path:
        raise WorkerError(
            f"{name} نصب/در‌دسترس نیست. Termux:API app و pkg install termux-api را بررسی کنید."
        )
    return path


def _run(command, timeout=20):
    if not isinstance(command, list) or not command:
        raise WorkerError("Internal invalid command")
    executable = _require_command(command[0])
    safe_command = [executable] + [str(item) for item in command[1:]]
    try:
        result = subprocess.run(
            safe_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkerError(f"{command[0]} timeout") from exc
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "command failed").strip()[:500]
        raise WorkerError(f"{command[0]}: {error}")
    return result.stdout.strip()


def _run_json(command, timeout=20):
    output = _run(command, timeout=timeout)
    try:
        return json.loads(output) if output else {}
    except json.JSONDecodeError as exc:
        raise WorkerError(f"{command[0]} returned invalid JSON") from exc


def _battery_data():
    data = _run_json(["termux-battery-status"])
    return data if isinstance(data, dict) else {}


def _battery_summary():
    data = _battery_data()
    percentage = data.get("percentage", "?")
    status = data.get("status", "unknown")
    plugged = data.get("plugged", "unknown")
    temperature = data.get("temperature", "?")
    health = data.get("health", "unknown")
    return (
        f"باتری: {percentage}%\n"
        f"وضعیت: {status}\n"
        f"اتصال شارژ: {plugged}\n"
        f"دما: {temperature}°C\n"
        f"سلامت: {health}"
    )


def _device_status():
    battery = _battery_data()
    total, used, free = shutil.disk_usage(str(BASE_DIR))

    def prop(name):
        try:
            return _run(["getprop", name], timeout=5) or "unknown"
        except WorkerError:
            return "unknown"

    model = prop("ro.product.model")
    manufacturer = prop("ro.product.manufacturer")
    android = prop("ro.build.version.release")
    try:
        uptime_seconds = int(float(Path("/proc/uptime").read_text().split()[0]))
    except Exception:
        uptime_seconds = 0
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes = remainder // 60
    return (
        f"دستگاه: {manufacturer} {model}\n"
        f"Android: {android}\n"
        f"باتری: {battery.get('percentage', '?')}% ({battery.get('status', 'unknown')})\n"
        f"فضای آزاد: {free / (1024**3):.1f} GB از {total / (1024**3):.1f} GB\n"
        f"زمان روشن‌بودن: {hours} ساعت و {minutes} دقیقه\n"
        f"Python: {platform.python_version()}"
    )


def _set_volume(args):
    stream = str(args.get("stream") or "music")
    percent = max(0, min(100, int(args.get("percent", 0))))
    streams = _run_json(["termux-volume"])
    if not isinstance(streams, list):
        raise WorkerError("termux-volume did not return stream list")
    selected = next(
        (item for item in streams if str(item.get("stream")) == stream), None
    )
    if not selected:
        raise WorkerError(f"Volume stream not found: {stream}")
    maximum = int(selected.get("max_volume", 0))
    if maximum <= 0:
        raise WorkerError("Invalid max volume")
    target = round(maximum * percent / 100)
    _run(["termux-volume", stream, str(target)])
    return f"صدای {stream} روی {percent}% تنظیم شد."


def _execute_action(action, args):
    if action not in CAPABILITIES or not isinstance(args, dict):
        raise WorkerError("Action is not allowed")
    if action == "device_status":
        return _device_status()
    if action == "battery_status":
        return _battery_summary()
    if action == "notify":
        text = str(args.get("text") or "")[:500]
        if not text:
            raise WorkerError("Notification text is empty")
        _run([
            "termux-notification",
            "--id", "rubika-agent",
            "--title", "Rubika Agent",
            "--content", text,
        ])
        return "اعلان روی گوشی نمایش داده شد."
    if action == "open_url":
        url = str(args.get("url") or "")[:1000]
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise WorkerError("Only valid http/https URLs are allowed")
        _run(["termux-open-url", url])
        return "لینک روی گوشی باز شد."
    if action == "set_volume":
        return _set_volume(args)
    if action == "speak":
        text = str(args.get("text") or "")[:500]
        if not text:
            raise WorkerError("Speech text is empty")
        if text.startswith("-"):
            text = " " + text
        _run(["termux-tts-speak", text], timeout=45)
        return "متن با صدای گوشی پخش شد."
    if action == "vibrate":
        duration = max(50, min(3000, int(args.get("duration_ms", 500))))
        _run(["termux-vibrate", "-d", str(duration), "-f"])
        return f"گوشی {duration} میلی‌ثانیه ویبره رفت."
    if action == "torch":
        state = str(args.get("state") or "")
        if state not in {"on", "off"}:
            raise WorkerError("Torch state must be on/off")
        _run(["termux-torch", state])
        return "چراغ‌قوه روشن شد." if state == "on" else "چراغ‌قوه خاموش شد."
    if action == "open_settings":
        _run(["am", "start", "-a", "android.settings.SETTINGS"])
        return "تنظیمات گوشی باز شد."
    raise WorkerError("Unsupported action")


def _ping(config):
    return _api_request(
        config,
        "/api/worker/ping",
        method="POST",
        payload={
            "worker_id": config["worker_id"],
            "version": VERSION,
            "capabilities": CAPABILITIES,
        },
    )


def _next_job(config):
    query = urlencode({"worker_id": config["worker_id"]})
    response = _api_request(config, "/api/worker/jobs/next?" + query)
    job = response.get("job")
    return job if isinstance(job, dict) else None


def _post_result(config, job_id, payload):
    body = dict(payload)
    body.update({"worker_id": config["worker_id"], "version": VERSION})
    return _api_request(
        config,
        f"/api/worker/jobs/{job_id}/result",
        method="POST",
        payload=body,
    )


def _acquire_single_instance():
    handle = LOCK_FILE.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise WorkerError("Worker is already running") from exc
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def main():
    config = _load_config()
    lock_handle = _acquire_single_instance()
    # مرجع باید تا پایان حلقه زنده بماند تا flock آزاد نشود.
    _ = lock_handle

    log.info("Android Worker %s", VERSION)
    log.info("Server: %s", config["server_url"])
    log.info("Worker ID: %s", config["worker_id"])
    missing = [name for name in ("termux-battery-status", "termux-notification") if not shutil.which(name)]
    if missing:
        log.warning("Termux:API commands missing: %s", ", ".join(missing))

    backoff = config["poll_interval"]
    last_ping = 0.0
    while True:
        try:
            if time.time() - last_ping >= 60:
                _ping(config)
                last_ping = time.time()
                log.info("Connected to server")

            job = _next_job(config)
            if not job:
                time.sleep(config["poll_interval"])
                backoff = config["poll_interval"]
                continue

            job_id = str(job.get("id") or "")
            action = str(job.get("action") or "")
            args = job.get("args") or {}
            if not re.fullmatch(r"[0-9a-f]{12}", job_id):
                raise WorkerError("Server sent invalid job id")

            completed = _load_completed()
            if job_id in completed:
                result_payload = completed[job_id]
                log.info("Re-sending stored result for %s", job_id)
            else:
                log.info("Executing %s (%s)", action, job_id)
                try:
                    result = _execute_action(action, args)
                    result_payload = {"success": True, "result": result, "error": ""}
                except Exception as exc:
                    result_payload = {
                        "success": False,
                        "result": "",
                        "error": str(exc)[:1000],
                    }
                _remember_completed(job_id, result_payload)

            # Keep retrying the result before taking another job.
            posted = False
            for attempt in range(8):
                try:
                    _post_result(config, job_id, result_payload)
                    posted = True
                    break
                except WorkerError as exc:
                    log.warning("Result post failed (%s/8): %s", attempt + 1, exc)
                    time.sleep(min(30, 2 ** attempt))
            if not posted:
                raise WorkerError("Could not deliver job result")
            log.info("Job %s completed", job_id)
            backoff = config["poll_interval"]

        except KeyboardInterrupt:
            log.info("Worker stopped")
            return 0
        except WorkerError as exc:
            log.warning("%s; retrying in %.0fs", exc, backoff)
            time.sleep(backoff)
            backoff = min(60.0, max(config["poll_interval"], backoff * 1.8))
        except Exception as exc:
            log.exception("Unexpected worker error: %s", exc)
            time.sleep(backoff)
            backoff = min(60.0, max(config["poll_interval"], backoff * 1.8))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkerError as exc:
        log.error("Startup failed: %s", exc)
        raise SystemExit(2)

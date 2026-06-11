import logging
import queue
import threading
import time
from pathlib import Path

from core.pipeline import PipelineOrchestrator


logger = logging.getLogger("UnifiedOCR")

SUPPORTED_INPUT_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".heic", ".docx", ".odt", ".doc", ".odoc"}
TEMPORARY_NAME_MARKERS = (".tmp", ".part", ".crdownload", ".download")


class DirectoryWatcher:
    def __init__(self, orchestrator: PipelineOrchestrator):
        self.orchestrator = orchestrator
        self.is_running = False
        self._watcher_thread = None
        self._worker_thread = None
        self.queue = queue.Queue()
        self.seen_files = set()
        self.file_tracker = {}
        self.lock = threading.Lock()

    def start(self):
        if self.is_running:
            return
        self.is_running = True
        with self.lock:
            self.seen_files.clear()
            self.file_tracker.clear()

        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break

        watch_dirs = self._consume_dirs()
        watch_text = ", ".join(str(path) for path in watch_dirs)
        self.orchestrator.log(f"Watchdog gestartet. Beobachte: {watch_text}")
        logger.info("Watchdog gestartet. Beobachtungsordner: %s", watch_text)

        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()

        self._watcher_thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._watcher_thread.start()

    def stop(self):
        self.is_running = False
        self.orchestrator.log("Watchdog wird gestoppt...")
        logger.info("Watchdog wird gestoppt...")
        self.queue.put(None)

    def _consume_dirs(self) -> list[Path]:
        dirs = getattr(self.orchestrator.config, "consume_dirs", None)
        if isinstance(dirs, (list, tuple, set)):
            return [Path(path) for path in dirs]
        return [Path(self.orchestrator.config.consume_dir)]

    def _watch_loop(self):
        while self.is_running:
            try:
                watch_dirs = [path for path in self._consume_dirs() if path.exists()]
                if not watch_dirs:
                    time.sleep(3)
                    continue

                current_candidates = set()
                with self.lock:
                    for consume_dir in watch_dirs:
                        for file_path in consume_dir.iterdir():
                            if not self.is_running:
                                break
                            self._track_candidate(file_path, current_candidates)

                    for tracked_path in list(self.file_tracker.keys()):
                        if tracked_path not in current_candidates:
                            self.file_tracker.pop(tracked_path, None)

            except Exception as e:
                logger.exception("Fehler im Watchdog-Polling-Thread")
                self.orchestrator.log(f"Fehler im Watchdog-Polling: {str(e)}")
            time.sleep(2)

    def _track_candidate(self, file_path: Path, current_candidates: set[Path]):
        if not file_path.is_file():
            return

        if file_path.suffix.lower() not in SUPPORTED_INPUT_SUFFIXES:
            return

        name = file_path.name.lower()
        if any(temp_ext in name for temp_ext in TEMPORARY_NAME_MARKERS):
            return

        if file_path in self.seen_files:
            return

        current_candidates.add(file_path)

        try:
            stat_info = file_path.stat()
            current_size = stat_info.st_size
            mtime = stat_info.st_mtime
        except (OSError, IOError):
            return

        if file_path not in self.file_tracker:
            self.file_tracker[file_path] = {
                "last_size": current_size,
                "stable_ticks": 0,
                "mtime": mtime,
            }
        else:
            tracker_entry = self.file_tracker[file_path]
            if current_size != tracker_entry["last_size"]:
                tracker_entry["last_size"] = current_size
                tracker_entry["stable_ticks"] = 0
            else:
                tracker_entry["stable_ticks"] += 1
            tracker_entry["mtime"] = mtime

        tracker_entry = self.file_tracker[file_path]
        age = time.time() - tracker_entry["mtime"]
        if age < 5.0 or tracker_entry["stable_ticks"] < 2:
            return

        try:
            with open(file_path, "rb"):
                pass
            self.orchestrator.log(f"Datei erkannt und stabil: {file_path.name}. Reihe in Queue ein...")
            logger.info("Datei stabil: %s. Einreihen in Queue.", file_path.name)
            self.seen_files.add(file_path)
            self.queue.put(file_path)
            self.file_tracker.pop(file_path, None)
        except (OSError, IOError):
            pass

    def _worker_loop(self):
        while self.is_running:
            try:
                file_path = self.queue.get()
                if file_path is None:
                    break

                if not self.is_running:
                    self.queue.task_done()
                    break

                try:
                    logger.info("Worker startet Verarbeitung fuer: %s", file_path.name)
                    self.orchestrator.process_file(file_path)
                except Exception as ex:
                    logger.exception("Worker-Fehler bei der Verarbeitung von %s", file_path.name)
                    self.orchestrator.log(f"Fehler bei {file_path.name}: {str(ex)}")
                finally:
                    with self.lock:
                        self.seen_files.discard(file_path)
                    self.queue.task_done()

                if self.queue.empty() and getattr(self.orchestrator, "deferred_organizations", None):
                    try:
                        self.orchestrator.process_deferred_organizations()
                    except Exception as def_ex:
                        logger.exception("Fehler beim Abarbeiten verzoegerter Ordner-Einsortierungen")
                        self.orchestrator.log(f"Fehler bei verzoegerter Einsortierung: {def_ex}")
            except Exception:
                logger.exception("Schwerwiegender Fehler im Worker-Loop")
                time.sleep(2)

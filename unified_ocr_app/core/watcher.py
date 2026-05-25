import time
import queue
import threading
import logging
from pathlib import Path
from core.pipeline import PipelineOrchestrator

logger = logging.getLogger("UnifiedOCR")

class DirectoryWatcher:
    def __init__(self, orchestrator: PipelineOrchestrator):
        self.orchestrator = orchestrator
        self.is_running = False
        self._watcher_thread = None
        self._worker_thread = None
        self.queue = queue.Queue()
        self.seen_files = set() # Um Mehrfach-Queuing der gleichen Datei zu vermeiden
        self.file_tracker = {}  # Trackt {Path: {"last_size": int, "stable_ticks": int, "mtime": float}} für non-blocking Stabilitätsprüfung
        self.lock = threading.Lock() # Lock zur Thread-Synchronisierung

    def start(self):
        if self.is_running:
            return
        self.is_running = True
        with self.lock:
            self.seen_files.clear()
            self.file_tracker.clear()
        
        # Leere die Queue, falls Reste vorhanden sind
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break
                
        self.orchestrator.log(f"Watchdog gestartet. Beobachte: {self.orchestrator.config.consume_dir}")
        logger.info(f"Watchdog gestartet. Beobachtungsordner: {self.orchestrator.config.consume_dir}")
        
        # Worker-Thread starten
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        
        # Watchdog-Polling-Thread starten
        self._watcher_thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._watcher_thread.start()

    def stop(self):
        self.is_running = False
        self.orchestrator.log("Watchdog wird gestoppt...")
        logger.info("Watchdog wird gestoppt...")
        # Die Queue aufwecken
        self.queue.put(None)

    def _watch_loop(self):
        while self.is_running:
            try:
                consume_dir = self.orchestrator.config.consume_dir
                if not consume_dir.exists():
                    time.sleep(3)
                    continue
                    
                current_candidates = set()
                with self.lock:
                    for file_path in consume_dir.iterdir():
                        if not self.is_running:
                            break
                        
                        if not file_path.is_file():
                            continue
                            
                        # Filter nach Dateiendungen
                        suffix = file_path.suffix.lower()
                        if suffix not in [".pdf", ".png", ".jpg", ".jpeg", ".heic", ".docx", ".odt", ".doc", ".odoc"]:
                            continue
                            
                        # Temporäre Downloads ignorieren
                        name = file_path.name.lower()
                        if any(temp_ext in name for temp_ext in [".tmp", ".part", ".crdownload", ".download"]):
                            continue
                            
                        # Bereits in Bearbeitung oder in Queue befindliche Dateien ignorieren
                        if file_path in self.seen_files:
                            continue
                            
                        current_candidates.add(file_path)
                        
                        try:
                            stat_info = file_path.stat()
                            current_size = stat_info.st_size
                            mtime = stat_info.st_mtime
                        except (OSError, IOError):
                            continue
                            
                        if file_path not in self.file_tracker:
                            self.file_tracker[file_path] = {
                                "last_size": current_size,
                                "stable_ticks": 0,
                                "mtime": mtime
                            }
                        else:
                            tracker_entry = self.file_tracker[file_path]
                            if current_size != tracker_entry["last_size"]:
                                tracker_entry["last_size"] = current_size
                                tracker_entry["stable_ticks"] = 0
                                tracker_entry["mtime"] = mtime
                            else:
                                tracker_entry["stable_ticks"] += 1
                                tracker_entry["mtime"] = mtime
                                
                        tracker_entry = self.file_tracker[file_path]
                        age = time.time() - tracker_entry["mtime"]
                        if age >= 5.0 and tracker_entry["stable_ticks"] >= 2:
                            # Exklusiven Zugriff prüfen (Öffnen im Lesemodus zum Testen auf Windows-Dateisperre)
                            try:
                                with open(file_path, 'rb'):
                                    pass
                                self.orchestrator.log(f"Datei erkannt und stabil: {file_path.name}. Reihe in Queue ein...")
                                logger.info(f"Datei stabil: {file_path.name}. Einreihen in Queue.")
                                self.seen_files.add(file_path)
                                self.queue.put(file_path)
                                self.file_tracker.pop(file_path, None)
                            except (OSError, IOError):
                                # Datei ist noch gesperrt/in Verwendung
                                pass
                                
                    # Bereinigung nicht mehr existierender Dateien im Tracker
                    for tracked_path in list(self.file_tracker.keys()):
                        if tracked_path not in current_candidates:
                            self.file_tracker.pop(tracked_path, None)
                        
            except Exception as e:
                logger.exception("Fehler im Watchdog-Polling-Thread")
                self.orchestrator.log(f"Fehler im Watchdog-Polling: {str(e)}")
            time.sleep(2)

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
                    logger.info(f"Worker startet Verarbeitung für: {file_path.name}")
                    self.orchestrator.process_file(file_path)
                except Exception as ex:
                    logger.exception(f"Worker-Fehler bei der Verarbeitung von {file_path.name}")
                    self.orchestrator.log(f"Fehler bei {file_path.name}: {str(ex)}")
                finally:
                    # Aus der Menge der aktuell bearbeiteten Dateien entfernen
                    with self.lock:
                        self.seen_files.discard(file_path)
                    self.queue.task_done()
                
                # Wenn die Queue leer ist und aufgestaute Ordner vorliegen, jetzt abarbeiten
                if self.queue.empty() and getattr(self.orchestrator, "deferred_organizations", None):
                    try:
                        self.orchestrator.process_deferred_organizations()
                    except Exception as def_ex:
                        logger.exception("Fehler beim Abarbeiten verzögerter Ordner-Einsortierungen")
                        self.orchestrator.log(f"Fehler bei verzögerter Einsortierung: {def_ex}")
            except Exception as e:
                logger.exception("Schwerwiegender Fehler im Worker-Loop")
                time.sleep(2)

"""One bounded latest-snapshot TTS job; MIS reconciliation never waits for speech."""
import threading


class DoctorBankSync:
    def __init__(self, rpc):
        self.rpc = rpc
        self.lock = threading.Lock()
        self.pending = None
        self.running = False
        self.status = "idle"

    def enqueue(self, doctors):
        snapshot = [{"id": d["id"], "full_name": d["full_name"], "is_active": d["is_active"]} for d in doctors]
        with self.lock:
            self.pending = snapshot
            self.status = "pending"
            if self.running:
                return self.status
            self.running = True
        threading.Thread(target=self._run, daemon=True, name="doctor-bank-sync").start()
        return "pending"

    def _run(self):
        while True:
            with self.lock:
                if self.pending is None:
                    self.running = False
                    return
                snapshot, self.pending = self.pending, None
            try:
                self.rpc("tts", "bank.doctors.refresh", {"doctors": snapshot})
                status = "ready"
            except Exception:
                status = "error"
            with self.lock:
                self.status = "pending" if self.pending is not None else status

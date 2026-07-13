import queue
import threading

from .runner import run_pipeline

_job_queue = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()


def _worker_loop():
    from converter.models import ConversionJob

    while True:
        job_id = _job_queue.get()
        try:
            job = ConversionJob.objects.get(pk=job_id)
            run_pipeline(job)
        except ConversionJob.DoesNotExist:
            pass
        finally:
            _job_queue.task_done()


def _ensure_worker_started():
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            thread = threading.Thread(target=_worker_loop, daemon=True)
            thread.start()
            _worker_started = True


def enqueue_job(job_id):
    """Adiciona um job à fila global. Os jobs são processados um de cada vez,
    na ordem em que chegam, para não estourar o limite de requisições da API."""
    _ensure_worker_started()
    _job_queue.put(job_id)

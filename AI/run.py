"""neo-iku 起動スクリプト"""
import os
import signal
import threading
import uvicorn
from config import HOST, PORT, DATA_DIR


def _force_exit(signum, frame):
    """Ctrl+C で確実にプロセスを終了（3秒以内に強制kill）"""
    timer = threading.Timer(3.0, lambda: os._exit(0))
    timer.daemon = True
    timer.start()
    raise KeyboardInterrupt


if __name__ == "__main__":
    DATA_DIR.mkdir(exist_ok=True)
    signal.signal(signal.SIGINT, _force_exit)
    # Windows では SIGBREAK (Ctrl+Break) も拾う
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _force_exit)
    try:
        # signal_handlers=False でuvicornにシグナルを横取りさせない
        uvicorn.run("app.main:app", host=HOST, port=PORT)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        os._exit(0)

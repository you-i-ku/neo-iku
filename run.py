"""neo-iku 起動スクリプト"""
import os
import signal
import threading
import uvicorn
from config import HOST, PORT, DATA_DIR

_shutdown_count = 0


def _force_exit_on_double_ctrl_c(signum, frame):
    """2回目のCtrl+Cで即座にプロセスを終了"""
    global _shutdown_count
    _shutdown_count += 1
    if _shutdown_count >= 2:
        os._exit(0)
    # 1回目はuvicornのgraceful shutdownに任せるが、
    # 3秒後に強制終了するタイマーを仕掛ける
    timer = threading.Timer(3.0, lambda: os._exit(0))
    timer.daemon = True
    timer.start()
    raise KeyboardInterrupt


if __name__ == "__main__":
    DATA_DIR.mkdir(exist_ok=True)
    signal.signal(signal.SIGINT, _force_exit_on_double_ctrl_c)
    try:
        uvicorn.run("app.main:app", host=HOST, port=PORT)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        os._exit(0)

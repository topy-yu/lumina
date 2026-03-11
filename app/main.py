import logging
import sys

from PySide6.QtWidgets import QApplication

from app.logging_config import setup_logging
from app.ui.main_window import MainWindow

logger = logging.getLogger("lumina.main")


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Lumina")

    emitter = setup_logging()
    logger.info("Lumina started")

    window = MainWindow(log_emitter=emitter)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

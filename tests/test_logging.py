"""logging_setup 配置测试。"""
from __future__ import annotations

import logging

from logging_setup import LOGGER_NAME, configure_logging, get_logger


class TestConfigureLogging:
    def teardown_method(self):
        # 重置全局配置标记 + 清理 handler，避免测试间串扰
        import logging_setup as ls
        ls._CONFIGURED = False
        logging.getLogger(LOGGER_NAME).handlers.clear()

    def test_returns_darkforest_logger(self):
        logger = configure_logging(force=True)
        assert logger.name == LOGGER_NAME

    def test_console_handler_added(self):
        logger = configure_logging(force=True)
        assert any(isinstance(h, logging.StreamHandler) for h in logger.handlers)

    def test_file_handler_writes_file(self, tmp_path):
        log_file = tmp_path / "diag.log"
        logger = configure_logging(log_file=str(log_file), force=True)
        get_logger("engine").warning("rate limited")
        # flush
        for h in logger.handlers:
            h.flush()
        assert log_file.exists()
        assert "rate limited" in log_file.read_text(encoding="utf-8")

    def test_verbose_lowers_console_level(self):
        logger = configure_logging(verbose=False, force=True)
        console = [h for h in logger.handlers if isinstance(h, logging.StreamHandler)
                   and not isinstance(h, logging.FileHandler)]
        assert any(h.level == logging.WARNING for h in console)

        logger = configure_logging(verbose=True, force=True)
        console = [h for h in logger.handlers if isinstance(h, logging.StreamHandler)
                   and not isinstance(h, logging.FileHandler)]
        assert any(h.level == logging.DEBUG for h in console)

    def test_idempotent_without_force(self):
        configure_logging(force=True)
        n1 = len(logging.getLogger(LOGGER_NAME).handlers)
        configure_logging()  # force 默认 False → 不应重复加 handler
        n2 = len(logging.getLogger(LOGGER_NAME).handlers)
        assert n1 == n2

    def test_get_logger_namespaced(self):
        assert get_logger("engine").name == f"{LOGGER_NAME}.engine"
        assert get_logger().name == LOGGER_NAME

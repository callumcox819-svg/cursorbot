"""Проверка: сервис GAG → папка HTMLch → GO/BACK файлы."""

from __future__ import annotations

import unittest

from services.gag_keys import gag_service_for_html_dir
from services.html_templates import (
    BACK_FILENAME,
    GO_FILENAME,
    html_subdir_for_service,
    html_template_path,
)


class GagHtmlRoutingTest(unittest.TestCase):
    def test_posta_maps_to_post_ch_folder(self) -> None:
        self.assertEqual(gag_service_for_html_dir("posta_ch"), "post_ch")
        self.assertEqual(gag_service_for_html_dir("post_ch"), "post_ch")

    def test_tutti_and_ricardo_dirs(self) -> None:
        self.assertEqual(html_subdir_for_service("tutti_ch"), "tutti_ch")
        self.assertEqual(html_subdir_for_service("ricardo_ch"), "ricardo_ch")

    def test_go_back_exist_per_service(self) -> None:
        for svc in ("tutti_ch", "posta_ch", "ricardo_ch"):
            go = html_template_path(svc, GO_FILENAME)
            back = html_template_path(svc, BACK_FILENAME)
            self.assertIsNotNone(go, f"GO missing for {svc}")
            self.assertIsNotNone(back, f"BACK missing for {svc}")
            sub = html_subdir_for_service(svc)
            self.assertIn(sub or "", str(go))
            self.assertIn(sub or "", str(back))

    def test_unknown_service_no_path(self) -> None:
        self.assertIsNone(html_template_path("", GO_FILENAME))
        self.assertIsNone(html_template_path("ebay_de", GO_FILENAME))

    def test_no_root_fallback_for_wrong_name(self) -> None:
        # Нет файла «go.html» в корне — только confirmation.html в подпапке
        p = html_template_path("tutti_ch", "go.html")
        self.assertIsNone(p)


if __name__ == "__main__":
    unittest.main()

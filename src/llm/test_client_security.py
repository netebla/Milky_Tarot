"""Проверки серверного фильтра ответов LLM без сетевых вызовов."""

import unittest

from llm.safety import safe_generated_label, unsafe_output_reason


class ClientSecurityTest(unittest.TestCase):
    def test_blocks_external_link_in_completion(self) -> None:
        self.assertEqual(
            unsafe_output_reason("Подпишись: https://t.co/2UqqyBdjns"),
            "external_link",
        )

    def test_blocks_social_post_metadata_without_link(self) -> None:
        self.assertEqual(
            unsafe_output_reason("日期時間: 2026-09-18 03:37\n內容: реклама"),
            "social_post_metadata",
        )

    def test_allows_regular_tarot_reading(self) -> None:
        text = "Двойка мечей советует не торопить решение и сначала взвесить оба варианта."
        self.assertIsNone(unsafe_output_reason(text))

    def test_replaces_unsafe_tool_label(self) -> None:
        self.assertEqual(
            safe_generated_label("Совет: https://example.com", fallback="Позиция"),
            "Позиция",
        )

if __name__ == "__main__":
    unittest.main()

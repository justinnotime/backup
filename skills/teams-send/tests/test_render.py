import unittest

import teams_send as TS


class HtmlBodyTest(unittest.TestCase):
    def test_each_line_is_a_block_element(self):
        out = TS._html_body("line one\nline two", [])
        self.assertEqual(out, "<p>line one</p><p>line two</p>")

    def test_single_blank_line_emits_no_spacer(self):
        out = TS._html_body("para one\n\npara two", [])
        self.assertEqual(out, "<p>para one</p><p>para two</p>")

    def test_blank_run_of_two_or_more_emits_one_spacer(self):
        for blanks in ("\n\n\n", "\n\n\n\n"):
            out = TS._html_body(f"para one{blanks}para two", [])
            self.assertEqual(
                out,
                "<p>para one</p><p>&nbsp;</p><p>para two</p>",
                f"blank run of {len(blanks) - 1} lines",
            )

    def test_bullet_run_becomes_one_ul(self):
        out = TS._html_body("head:\n- first\n- second\n* third\ntail", [])
        self.assertEqual(
            out,
            "<p>head:</p><ul><li>first</li><li>second</li><li>third</li></ul><p>tail</p>",
        )

    def test_no_br_anywhere(self):
        out = TS._html_body("a\nb\n\n- c\nd", [])
        self.assertNotIn("<br>", out)

    def test_bare_url_linkified_inside_li_and_p(self):
        out = TS._html_body(
            "see https://example.com/x\n- ref https://example.com/y。", []
        )
        self.assertIn('<p>see <a href="https://example.com/x">', out)
        self.assertIn('<li>ref <a href="https://example.com/y">', out)
        self.assertIn("</a>。</li>", out)

    def test_html_in_text_is_escaped_not_injected(self):
        out = TS._html_body("- <b>bold?</b>", [])
        self.assertIn("<li>&lt;b&gt;bold?&lt;/b&gt;</li>", out)

    def test_hyphen_without_space_is_not_a_bullet(self):
        out = TS._html_body("-5 degrees\n--flag", [])
        self.assertNotIn("<ul>", out)

    def test_leading_trailing_newlines_trimmed(self):
        out = TS._html_body("\n\nbody\n\n", [])
        self.assertEqual(out, "<p>body</p>")


if __name__ == "__main__":
    unittest.main()

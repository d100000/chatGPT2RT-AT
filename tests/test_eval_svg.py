"""Image-only SVG policy: allow declarative art, reject executable/resource paths."""
import unittest

from rt_extractor.eval_svg import CHECKER_VERSION, inspect_svg
from tests.pelican_mock import ANIMATED_SVG


class AnimatedSvgTests(unittest.TestCase):
    def check(self, content, expected):
        raw = f'<svg xmlns="http://www.w3.org/2000/svg">{content}</svg>'
        svg, technical = inspect_svg(raw)
        self.assertEqual(svg, raw)
        self.assertEqual(technical["safe"], expected, technical["issues"])
        self.assertEqual(technical["checker_version"], CHECKER_VERSION)

    def test_keyframes_media_comments_and_smil_keep_original_art(self):
        raw = f'```svg\n{ANIMATED_SVG}\n```\n<svg/>'
        svg, checks = inspect_svg(raw)
        self.assertEqual(svg, ANIMATED_SVG)
        self.assertTrue(checks["safe"], checks["issues"])
        self.assertEqual(checks["svg_count"], 2)
        self.assertIsNone(checks["browser_rendered"])

    def test_local_css_urls_escapes_and_common_animation_values(self):
        self.check(r'''<style>
          :root {color-scheme:light; --ink:#123;}
          .bird {fill:u\72l("#paint"); animation:bob .48s ease-in-out infinite alternate;}
          @keyframes bob {0%,100% {transform:translateY(0) scale(1);} 50% {transform:translateY(4px) scale(.9);}}
          @media (prefers-reduced-motion: reduce) {*, *::before, *::after {animation-duration:.001ms !important;}}
        </style><path style="stroke:var(--ink);fill:url(#paint);opacity:calc(1 - .2)"/>''', True)

    def test_graphic_smil_and_local_motion_paths(self):
        good = [
            '<path><animate attributeName="d" values="M0 0L1 1;M0 0L2 2" dur="1s" repeatCount="indefinite"/></path>',
            '<g><animateTransform attributeName="transform" type="rotate" from="0 50 50" to="360 50 50" dur="2s"/></g>',
            '<g><animateMotion dur="1s"><mpath href="#track"/></animateMotion></g>',
            '<path><set attributeName="fill" to="url(#paint)" begin="0s"/></path>',
            '<path><animateColor attributeName="fill" from="red" to="blue" dur="2s"/></path>',
        ]
        for item in good:
            with self.subTest(item=item): self.check(item, True)

    def test_external_css_references_rejected_even_when_escaped_or_nested(self):
        bad = [
            r'@im\70ort "https://evil.test/a.css";',
            '@import url(//evil.test/a.css);',
            '@font-face {font-family:evil;src:url(font.woff);}',
            r'path {fill:u\72l(\68ttps://evil.test/x);}',
            'path {fill:url("data:image/svg+xml,bad");}',
            '@keyframes x {to {fill:url(//evil.test/x);}}',
            '@media (min-width:1px) {path {filter:url(relative.svg#x);}}',
            'path {background:image-set("https://evil.test/x" 1x);}',
            'path {background:-webkit-image-set("https://evil.test/x" 1x);}',
            'path {--x:url(https://evil.test/x);fill:var(--x);}',
            'path {fill:url(var(--x));}',
            'path {fill:attr(data-url type(<url>));}',
            'path {fill:expression(alert(1));}',
            r'path {beh\61vior:url(#x);}',
            'path {-moz-binding:url(#x);}',
            'path {fill:url("broken);}',
            '@supports (display:block) {path {fill:red;}}',
        ]
        for css in bad:
            with self.subTest(css=css): self.check(f'<style><![CDATA[{css}]]></style>', False)

    def test_inline_and_presentation_attribute_url_bypasses_rejected(self):
        for attrs in [r'style="fill:u\72l(https://evil.test/x)"',
                      'fill="url(&quot;https://evil.test/x&quot;)"',
                      'style="@import url(https://evil.test/x);"',
                      'style="background:image-set(&quot;https://evil.test/x&quot; 1x)"',
                      'style="fill:url(//evil.test/x)"']:
            with self.subTest(attrs=attrs): self.check(f'<path {attrs}/>', False)

    def test_smil_cannot_rewrite_security_sensitive_attributes(self):
        for tag in ("animate", "set", "animateColor", "animateTransform"):
            for attr in ("href", "xlink:href", "style", "onload", "onbegin", "xml:base", "attributeName", "values", "id", "class", "xmlns", ""):
                with self.subTest(tag=tag, attr=attr):
                    self.check(f'<{tag} attributeName="{attr}" to="https://evil.test/x"/>', False)
        for content in [
            '<animate attributeName="fill" values="red;url(https://evil.test/x);blue"/>',
            '<animate attributeName="fill" to="red" href="https://evil.test/x"/>',
            '<animate attributeName="d" to="M0 0L1 1" onbegin="alert(1)"/>',
            '<animateMotion attributeName="href" path="M0 0L1 1"/>',
            '<animateMotion><mpath href="external.svg#x"/></animateMotion>',
            '<script>alert(1)</script>', '<foreignObject/>', '<g xml:base="https://evil.test/"/>',
        ]:
            with self.subTest(content=content): self.check(content, False)

    def test_deep_css_fails_closed_without_crashing(self):
        self.check('<path style="opacity:' + 'calc(' * 100 + '1' + ')' * 100 + '"/>', False)


if __name__ == "__main__":
    unittest.main()

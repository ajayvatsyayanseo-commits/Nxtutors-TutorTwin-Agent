"""Typesetting maths for a channel that cannot render it.

Two things are being protected:

**A student must not be sent a picture of a parse error.** Model output is
untrusted markup, so anything unrenderable has to fall back to the text that was
already sent, not fail the answer.

**Rendering must never execute the markup.** matplotlib's mathtext parses and
draws; real LaTeX would `\\input` and `\\write18` whatever a model asked it to.
The tests below pin that boundary so nobody "upgrades" to a LaTeX subprocess
without noticing what it costs.
"""

from __future__ import annotations

import pytest

from tutortwin.learning import mathrender as m

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class TestRendering:
    @pytest.mark.parametrize(
        ("name", "expression"),
        [
            ("quadratic", r"x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}"),
            ("integral", r"\int_0^\infty e^{-x^2}\,dx = \frac{\sqrt{\pi}}{2}"),
            ("summation", r"\sum_{i=1}^{n} i = \frac{n(n+1)}{2}"),
            ("derivative", r"\frac{d}{dx}\left(x^3\right) = 3x^2"),
            ("simple", "2x + 5 = 13"),
            ("greek", r"\theta = \frac{\pi}{4}"),
        ],
    )
    def test_school_maths_renders_to_a_real_png(self, name: str, expression: str) -> None:
        rendered = m.render(expression)
        assert rendered.png.startswith(PNG_MAGIC), f"{name} did not produce a PNG"
        assert len(rendered.png) > 500, f"{name} produced a suspiciously empty image"

    def test_the_reported_size_is_the_real_size(self) -> None:
        """`bbox_inches='tight'` crops during the save and never resizes the
        figure, so reading the canvas reports the placeholder - and WhatsApp is
        handed an image claiming to be 1x1."""
        rendered = m.render(r"x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}")
        assert rendered.width > 100
        assert rendered.height > 40

    def test_multiple_lines_become_one_image(self) -> None:
        """A worked solution is several steps. Joining them inside one `$...$`
        is a parse error, so each line is its own maths group."""
        rendered = m.render("2x + 5 = 13\n2x = 8\nx = 4")
        assert rendered.png.startswith(PNG_MAGIC)

    def test_a_wrapped_expression_is_unwrapped_first(self) -> None:
        """Models emit `$$...$$`. mathtext supplies its own wrapper, so leaving
        the delimiters in renders literal dollar signs."""
        assert m.strip_delimiters(r"$$x = 1$$") == "x = 1"
        assert m.strip_delimiters(r"\[x = 1\]") == "x = 1"
        assert m.strip_delimiters(r"\(x = 1\)") == "x = 1"
        assert m.strip_delimiters("x = 1") == "x = 1"


class TestRefusal:
    @pytest.mark.parametrize(
        "hostile",
        [
            r"\input{/etc/passwd}",
            r"\write18{rm -rf /}",
            r"\include{secrets}",
            r"\usepackage{tikz}",
            r"\begin{document}x\end{document}",
            r"\newcommand{\x}{bad}",
            r"\includegraphics{/etc/shadow}",
        ],
    )
    def test_document_and_file_commands_are_refused(self, hostile: str) -> None:
        """Not because mathtext would execute them - it would not - but so that
        swapping in a real LaTeX renderer later cannot silently turn model
        output into file access."""
        assert not m.is_renderable(hostile)
        with pytest.raises(m.UnrenderableMath):
            m.render(hostile)

    def test_an_over_long_expression_is_refused(self) -> None:
        """A whole answer is prose with maths in it, not one expression, and it
        is unreadable on a phone at that size anyway."""
        with pytest.raises(m.UnrenderableMath):
            m.render("x" * (m.MAX_EXPRESSION_CHARS + 1))

    def test_unbalanced_delimiters_are_refused(self) -> None:
        """An odd `$` swallows the rest of the string into maths mode and
        renders nonsense rather than failing."""
        assert not m.is_renderable("$unbalanced")

    def test_empty_input_is_refused(self) -> None:
        with pytest.raises(m.UnrenderableMath):
            m.render("   ")

    def test_malformed_markup_raises_rather_than_returning_a_broken_image(self) -> None:
        """The caller falls back to the text, which is always better than a
        picture of an error."""
        with pytest.raises(m.UnrenderableMath):
            m.render(r"\frac{1}{")


class TestExtraction:
    def test_display_maths_is_found_and_inline_is_left_alone(self) -> None:
        """Inline `$x$` is a symbol inside a sentence and reads fine as text.
        Rendering every one would produce a dozen images and bury the
        explanation they were meant to clarify."""
        answer = (
            "First rearrange:\n\n$$2x + 5 = 13$$\n\n"
            "Then divide. Note that $x$ is the unknown, and \\[x = 4\\] is the answer."
        )
        assert m.extract_display_math(answer) == ["2x + 5 = 13", "x = 4"]

    def test_an_answer_with_no_maths_yields_nothing(self) -> None:
        assert m.extract_display_math("Photosynthesis happens in the chloroplast.") == []

    def test_unrenderable_display_maths_is_dropped_not_returned(self) -> None:
        """Extraction filters, so the caller never has to re-check."""
        assert m.extract_display_math(r"$$\input{/etc/passwd}$$") == []

    def test_extraction_survives_an_unterminated_block(self) -> None:
        """Model output is untrusted; a missing closing delimiter must not
        raise out of what is only a formatting step."""
        assert m.extract_display_math("$$x = 1") == []

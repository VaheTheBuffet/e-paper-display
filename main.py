#!/usr/bin/python
# -*- coding:utf-8 -*-
"""
E-ink EPUB reader for Waveshare epd2in13_V4
- Parses EPUB files and renders text page-by-page on the e-paper display
- Automatically wraps long lines and clears the screen when a page is full
Usage: python ebook_reader.py <path_to_epub>
"""

import sys
import os
import zipfile
import logging
import time
import textwrap
from html.parser import HTMLParser
from PIL import Image, ImageDraw, ImageFont

# ── Path setup ────────────────────────────────────────────────────────────────
picdir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'pic')
libdir = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), 'lib')
if os.path.exists(libdir):
    sys.path.append(libdir)

from waveshare_epd import epd2in13_V4

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── Display constants ─────────────────────────────────────────────────────────
# epd2in13_V4: physical 122 (H) × 250 (W) pixels, but PIL image is rotated:
#   image size = (epd.height, epd.width) = (250, 122)
DISPLAY_W = 250   # pixels across the long axis (PIL x)
DISPLAY_H = 122   # pixels down  the short axis (PIL y)

FONT_BODY_SIZE    = 15
FONT_HEADING_SIZE = 20   # slightly smaller than 24 so headings still wrap nicely
LINE_SPACING      = 2    # extra pixels between lines
PAGE_DELAY        = 5    # seconds to show each page (increase or replace with button input)
LEFT_MARGIN       = 2    # pixels from left edge

# ── EPUB HTML parser ──────────────────────────────────────────────────────────
class EpubHTMLParser(HTMLParser):
    """
    Extracts (kind, text) tuples from EPUB HTML:
      kind = 'heading' | 'body'
    """

    HEADING_TAGS = {'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'title'}
    BODY_TAGS    = {'p', 'blockquote', 'li', 'dd', 'dt'}

    def __init__(self):
        super().__init__()
        self._cur_tag  = None
        self._buf      = []
        self.paragraphs = []   # list of (kind, text)

    def handle_starttag(self, tag, attrs):
        if tag in self.HEADING_TAGS:
            self._flush()
            self._cur_tag = 'heading'
        elif tag in self.BODY_TAGS:
            self._flush()
            self._cur_tag = 'body'
        elif tag == 'br' and self._cur_tag:
            self._buf.append('\n')

    def handle_endtag(self, tag):
        if tag in self.HEADING_TAGS or tag in self.BODY_TAGS:
            self._flush()
            self._cur_tag = None

    def handle_data(self, data):
        if self._cur_tag:
            self._buf.append(data)

    def _flush(self):
        text = ''.join(self._buf).strip()
        if text and self._cur_tag:
            self.paragraphs.append((self._cur_tag, text))
        self._buf.clear()


def parse_epub(epub_path: str) -> list:
    """Return list of (kind, text) paragraphs from every HTML file in the EPUB."""
    paragraphs = []
    with zipfile.ZipFile(epub_path, 'r') as zf:
        html_files = sorted(f for f in zf.namelist() if f.endswith(('.html', '.xhtml')))
        if not html_files:
            raise RuntimeError("No HTML files found in epub")
        for html_file in html_files:
            parser = EpubHTMLParser()
            try:
                raw = zf.read(html_file).decode('utf-8', errors='replace')
                parser.feed(raw)
                paragraphs.extend(parser.paragraphs)
            except Exception as e:
                log.warning(f"Skipping {html_file}: {e}")
    return paragraphs


# ── Rendering helpers ─────────────────────────────────────────────────────────
def wrap_paragraph(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Wrap a single paragraph string into lines that fit max_width pixels."""
    words = text.split()
    lines = []
    current = ''
    for word in words:
        candidate = (current + ' ' + word).strip() if current else word
        w = font.getlength(candidate)
        if w <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            # If a single word is wider than max_width, force-split it
            if font.getlength(word) > max_width:
                while word:
                    for i in range(len(word), 0, -1):
                        if font.getlength(word[:i]) <= max_width:
                            lines.append(word[:i])
                            word = word[i:]
                            break
                    else:
                        lines.append(word)
                        word = ''
                current = ''
            else:
                current = word
    if current:
        lines.append(current)
    return lines if lines else ['']


class PageRenderer:
    """
    Buffers lines and flushes to the e-paper display whenever the page is full
    or explicitly flushed.
    """

    def __init__(self, epd, font_body, font_heading):
        self.epd          = epd
        self.font_body    = font_body
        self.font_heading = font_heading
        self._lines: list[tuple[str, ImageFont.FreeTypeFont]] = []
        self._y_used = 0

    def _line_height(self, font: ImageFont.FreeTypeFont) -> int:
        ascent, descent = font.getmetrics()
        return ascent + descent + LINE_SPACING

    def add_paragraph(self, kind: str, text: str):
        """Wrap paragraph into lines and add them, flushing pages as needed."""
        font = self.font_heading if kind == 'heading' else self.font_body
        # Add a blank line before headings for visual separation
        if kind == 'heading' and self._lines:
            self._try_add_line('', self.font_body)
        wrapped = wrap_paragraph(text, font, DISPLAY_W - LEFT_MARGIN * 2)
        for line in wrapped:
            self._try_add_line(line, font)
        # Blank line after each paragraph
        self._try_add_line('', self.font_body)

    def _try_add_line(self, text: str, font: ImageFont.FreeTypeFont):
        lh = self._line_height(font)
        if self._y_used + lh > DISPLAY_H:
            self._flush_page()
        self._lines.append((text, font))
        self._y_used += lh

    def _flush_page(self):
        if not self._lines:
            return
        log.info(f"Rendering page with {len(self._lines)} lines")
        image = Image.new('1', (DISPLAY_W, DISPLAY_H), 255)
        draw  = ImageDraw.Draw(image)
        y = 0
        for text, font in self._lines:
            draw.text((LEFT_MARGIN, y), text, font=font, fill=0)
            y += self._line_height(font)
        self.epd.display(self.epd.getbuffer(image))
        time.sleep(PAGE_DELAY)
        self.epd.Clear(0xFF)
        self._lines  = []
        self._y_used = 0

    def finish(self):
        """Flush any remaining lines as the last page."""
        self._flush_page()


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path_to_epub>")
        sys.exit(1)

    epub_path = sys.argv[1]
    if not os.path.isfile(epub_path):
        print(f"File not found: {epub_path}")
        sys.exit(1)

    log.info(f"Parsing EPUB: {epub_path}")
    paragraphs = parse_epub(epub_path)
    log.info(f"Found {len(paragraphs)} paragraphs")

    try:
        epd = epd2in13_V4.EPD()
        log.info("Initialising display")
        epd.init()
        epd.Clear(0xFF)

        font_body    = ImageFont.truetype(os.path.join(picdir, 'Font.ttc'), FONT_BODY_SIZE)
        font_heading = ImageFont.truetype(os.path.join(picdir, 'Font.ttc'), FONT_HEADING_SIZE)

        renderer = PageRenderer(epd, font_body, font_heading)

        for kind, text in paragraphs:
            renderer.add_paragraph(kind, text)

        renderer.finish()

        log.info("Done — sleeping display")
        epd.sleep()

    except IOError as e:
        log.error(e)
    except KeyboardInterrupt:
        log.info("Interrupted by user")
        epd2in13_V4.epdconfig.module_exit(cleanup=True)
        sys.exit(0)


if __name__ == '__main__':
    main()

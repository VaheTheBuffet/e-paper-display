#!/usr/bin/python
"""
e-ink epub reader for waveshare epd2in13_v4
- parses epub files and renders text page-by-page on the e-paper display
- automatically wraps long lines and clears the screen when a page is full
usage: python ebook_reader.py <path_to_epub>
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


class FPException(Exception):
    pass

class FP:
    """Specialized File Pointer like C, could be more pythonic maybe"""
    def __init__(self, buf):
        self.p = 0 #block
        self.c = 0 #character
        self.buf = buf #raw buffer


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

        self.max_width = DISPLAY_W - LEFT_MARGIN * 2

    def _line_height(self, font: ImageFont.FreeTypeFont) -> int: 
        ascent, descent = font.getmetrics()
        return ascent + descent + LINE_SPACING

    def advance_page(self, fp: FP):
        """Advances the render buffer by one visual page"""

        current_line = ''
        page_completed = False

        ty = fp.buf[fp.p][0]
        font = self.font_body if ty == 'body' else self.font_heading
        ascent, descent = font.getmetrics()
        font_height = ascent + descent + LINE_SPACING

        while True:
            #Attempt to obtain a new line
            #We will assume the entire line has the same font height
            if self._y_used + font_height < DISPLAY_H:
                #Attempt to fill line
                for i in range(fp.c, len(fp.buf[fp.p][1])):
                    line_width = font.getlength(fp.buf[fp.p][1][fp.c: i+1])
                    if line_width > self.max_width:
                        self._lines.append((fp.buf[fp.p][1][fp.c: i], font))
                        self._y_used += font_height
                        fp.c = i
                        break
                else:
                    self._lines.append((fp.buf[fp.p][1][fp.c: len(fp.buf[fp.p][1])], font))
                    self._lines.append(('', font))
                    fp.c = 0
                    fp.p += 1
                    ty = fp.buf[fp.p][0]
                    font = self.font_body if ty == 'body' else self.font_heading
                    ascent, descent = font.getmetrics()
                    font_height = ascent + descent + LINE_SPACING
                    self._y_used += 2 * font_height

            else:
                break

        self._flush_page()

    def retreat_page(self, fp: FP):
        """Retreates the render buffer by one visual page"""
        current_line = ''

        while True:
            (ty, word) = fp.previous_word()
            candidate_line = (word + ' ' + current_line).strip() if current_line else word
            w = (self.font_body if ty == 'paragraph' else self.fond_heading).getlength(candidate_line)
            if w <= self.max_width:
                current_line = candidate_line
            else:
                if current_line:
                    self._lines.append(current_line)

                if font.getlength(word) > self.max_width:
                    while word:
                        for i in range(len(word), 0, -1):
                            if font.getlength(word[:i]) <= self.max_width:
                                lines.append(word[:i])
                                word = word[i:]
                                break
                        else:
                            self._lines.append(word)
                            word = ''
                    current_line = ''
                else:
                    current_line = word

        if current_line:
            self._lines.append(current_line)
        
    def _try_add_line(self, text: str, font: ImageFont.FreeTypeFont) -> bool:
        lh = self._line_height(font)
        if self._y_used + lh > DISPLAY_H:
            self._flush_page()
            return True

        self._lines.append((text, font))
        self._y_used += lh
        return False

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

        self._lines  = []
        self._y_used = 0

    def clear_screen(self):
        self.epd.Clear(0xFF)

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
        file_pointer = FP(paragraphs)

        while True:
            try:
                renderer.advance_page(file_pointer)
                time.sleep(PAGE_DELAY)
                renderer.clear_screen()
            except FPException:
                log.error("finished or error")
                break

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

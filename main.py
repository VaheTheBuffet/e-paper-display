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
from gpiozero import Button

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

        letters_codes = range(ord('A'), ord('z') + 1)
        self.table_body = [font_body.getlength(chr(c)) for c in letters_codes]
        self.table_heading = [font_heading.getlength(chr(c)) for c in letters_codes]

        self.table_len = len(self.table_body)

        self._lines: list[tuple[str, ImageFont.FreeTypeFont]] = []
        self._y_used = 0

        self.pages = self.pages = None

        self.max_width = DISPLAY_W - LEFT_MARGIN * 2

    def _line_height(self, font: ImageFont.FreeTypeFont) -> int: 
        ascent, descent = font.getmetrics()
        return ascent + descent + LINE_SPACING

    def display_page(self, fp: FP, n: int):
        lines = self.pages[n]
        print(lines)

        for ((p, c0), (_, c1)) in lines:
            ty, text = fp.buf[p]
            font = self.font_body if ty == 'body' else self.font_heading

            self._try_add_line(text[c0: c1], font)


    def visual_parse_text(self, fp: FP):
        """parse text into page, return array of file pointers to visual lines"""
        self.pages: list[list[tuple(int, int)]] = [[]]

        fp = FP(fp.buf)
        for p, (ty, text) in enumerate(fp.buf):
            font = self.font_body if ty == 'body' else self.font_heading
            table = self.table_body if ty == 'body' else self.table_heading
            
            lh = self._line_height(font)
            c = 0
            l = len(text)
            while True:
                running_length = 0
                i = c
                while i < l and running_length <= self.max_width:
                    idx = ord(text[i]) - ord('A')
                    if(idx >= 0 and idx < self.table_len):
                        running_length += table[idx]
                    else:
                        running_length += table[0]
                    i += 1

                else:
                    if self._y_used + lh > DISPLAY_H:
                        self.pages.append([])
                        self._y_used = 0

                    self.pages[-1].append(((p, c), (p, i)))
                    self._y_used += lh

                    if i + 1 >= len(text):
                        if self._y_used + lh <= DISPLAY_H:
                            self.pages[-1].append(((p, i), (p, i)))
                            self._y_used += lh

                        break

                    c = i

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


#------------------------IDK-----------------------------
# There are a few ways to do this, but this seems to be one of the more
# clear approaches. You can actually just bind methods as callable objects
# and self param will be bound by default because methods and functions are
# different types, but that's not at all obvious, so I went with the closures.
class ButtonHandler:
    def __init__(self):
        self.button_time = 0
        self.press_type = 0

        def handle_press():
            self.button_time = time.time()

        
        def handle_release():
            time_now = time.time()
            elapsed_time = time_now - self.button_time

            if elapsed_time > 0.8: #retreat page
                self.press_type = 'retreat'
            else: #
                self.press_type = 'advance'

        self.handle_press = handle_press
        self.handle_release = handle_release
#------------------------IDK-----------------------------


#-----------------------Main-----------------------------
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


    #init button on gpio pin 4 and set callbacks
    button = Button(4)
    button_handler = ButtonHandler()
    button.when_pressed = button_handler.handle_press
    button.when_released = button_handler.handle_release

    try:
        epd = epd2in13_V4.EPD()
        log.info("Initialising display")
        epd.init()
        epd.Clear(0xFF)

        font_body    = ImageFont.truetype(os.path.join(picdir, 'Font.ttc'), FONT_BODY_SIZE)
        font_heading = ImageFont.truetype(os.path.join(picdir, 'Font.ttc'), FONT_HEADING_SIZE)

        renderer = PageRenderer(epd, font_body, font_heading)

        file_pointer = FP(paragraphs)
        log.info("started parsing")
        renderer.visual_parse_text(file_pointer)
        log.info("finished parsing")

        page = 0
        while True:
            if button_handler.press_type == 'advance':
                #renderer.clear_screen()
                #time.sleep(PAGE_DELAY)
                page += 1
                renderer.display_page(file_pointer, page)
            elif button_handler.press_type == 'retreat':
                log.error("retreating page")
                page -= 1
                renderer.display_page(file_pointer, page)
                pass

            button_handler.press_type = None

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

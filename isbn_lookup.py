#!/usr/bin/env python3
"""
isbn_lookup.py

takes a list of ISBNs and gets the title, author and list price for each one.
made for korean books but any ISBN the stores carry should work

    python isbn_lookup.py isbns.txt
    python isbn_lookup.py --gui

--gui opens a tkinter window with its own input box. paste ISBNs in there, one
per line, nothing is read from isbns.txt (the input only lives in ram). rows
show up in the table as they come in and isbn_results.csv is written when the
last one finishes, same as the normal mode

isbns.txt is one ISBN per line, ISBN 10 or 13, spaces and hyphens are fine.
10s get converted to 13, and a wrong check digit on a 13 gets fixed

prints a tab separated line per book and writes isbn_results.csv (utf8) in
whatever folder you ran it from

stores get tried in this order and the first hit wins: yes24, kyobo, aladin

columns
    input         the line exactly as it was typed
    isbn13        the ISBN 13 that got searched
    title         korean title
    title_en      original title if the store lists one, else empty
    title_en_src  "original" if title_en is filled
    author        main author(s)
    publisher     publisher
    price_krw     LIST price in won, not the sale price
    price_nzd     price_krw * KRW_TO_NZD, 2 decimals
    source_link   page the data came from
    note          warnings, conversions, errors

layout
    Backend    everything that talks to the stores, plus the csv
    Frontend   the command line mode and the gui, they only ever call Backend

python 3 std lib only. curl is the backup if python gets an empty page
"""

# argparse for the --gui flag, queue + threading so the gui doesnt freeze while
# the stores are being hit, io for the csv text. tkinter isnt up here on purpose,
# its imported inside Frontend.run_gui
import argparse
import csv
import html
import io
import json
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request


class Backend:
    # only used when the live rate cant be fetched
    FALLBACK_KRW_TO_NZD = 0.001298

    COLUMNS = ["input", "isbn13", "title", "title_en", "title_en_src", "author",
               "publisher", "price_krw", "price_nzd", "source_link", "note"]

    # normal browser headers. no Accept Encoding on purpose, urllib asks for
    # identity so pages come back uncompressed and nothing needs unzipping
    # (curl is different, thats what --compressed is for)
    FIREFOX_HEADERS = {
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64; rv:120.0) "
                       "Gecko/20100101 Firefox/120.0"),
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
    }

    # kyobo blocks default python and curl so it gets chrome headers instead, cheeky but it works
    CHROME_HEADERS = {
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
    }

    KYOBO_REFERER = "https://search.kyobobook.co.kr/"
    ALADIN_REFERER = 'https://www.aladin.co.kr/'

    def __init__(self):
        # fetched ONCE when the backend is made (not on import anymore), so every
        # row gets converted with the same number
        self.krw_to_nzd = self.fetch_krw_to_nzd()

        # order here = priority. new store just needs a method that takes an isbn and
        # returns a book dict or None, then add it to this list
        # yes24 first cuz its most often right. kyobo has way more books but sometimes the
        # authors are wrong. aladin is last because it hits the rate limiter fastest
        self.stores = [
            ("yes24", self.search_yes24),
            ("kyobo", self.search_kyobo),
            ("aladin", self.search_aladin),
        ]

    def fetch_krw_to_nzd(self):
        # frankfurter is free, no key. rates are daily
        url = 'https://api.frankfurter.dev/v2/rate/KRW/NZD'

        # python's default user agent gets a 403 here, so firefox it is
        req = urllib.request.Request(url, headers={
            'User-Agent': ('Mozilla/5.0 (X11; Linux x86_64; rv:120.0) '
                           'Gecko/20100101 Firefox/120.0'),
            'Accept': 'application/json',
        })

        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return float(json.load(resp)['rate'])
        except Exception as e:
            # catches everything on purpose. a rate problem shouldnt kill the whole run
            # warning goes to stderr so redirecting stdout stays clean
            print(f'live rate failed ({e}), using {self.FALLBACK_KRW_TO_NZD}', file=sys.stderr)
            return self.FALLBACK_KRW_TO_NZD

    #################################################
    # fetching
    #################################################

    def fetch_with_curl(self, url, headers):
        # curl looks different enough from python that it gets past the rate limiter for a while
        # python also just randomly fails on some books for no reason
        # -s hides the progress bar, -S keeps errors (without it failures are silent),
        # -L follows redirects. no -f so a 403 still exits 0, the ISBN check in each
        # search method is what catches the error page
        cmd = ["curl", "-sSL", "--compressed", "--max-time", "25"]

        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]   # list not a string, so no shell and no quoting headaches

        # second timer for when curl just dies mid session. this one raises
        # TimeoutExpired and not the RuntimeError below, so the csv note shows the
        # full command
        result = subprocess.run(cmd + [url], capture_output=True, timeout=40)

        if result.returncode != 0:
            # decode with replace, a bad byte here would hide the real curl error
            err = result.stderr.decode("utf-8", "replace").strip()
            # raise, dont return ''. an empty return looks like an empty page
            raise RuntimeError("curl failed: " + err)

        return result.stdout.decode("utf-8", "replace")

    def fetch(self, url, referer=None):
        # returns (final url after redirects, html)
        if "kyobobook.co.kr" in url:
            headers = dict(self.CHROME_HEADERS)
        else:
            headers = dict(self.FIREFOX_HEADERS)   # copy so the Referer below doesnt leak into the next request

        if referer:
            headers["Referer"] = referer

        req = urllib.request.Request(url, headers=headers)

        try:
            resp = urllib.request.urlopen(req, timeout=20)
        except urllib.error.HTTPError:
            # HTTPError is a URLError so it has to come first. a 403/404 is a real
            # answer, retrying wont change it
            raise
        except (TimeoutError, ConnectionError, urllib.error.URLError):
            # one retry. if this one fails too the error goes up as is
            time.sleep(2)
            resp = urllib.request.urlopen(req, timeout=20)

        with resp:
            final_url = resp.geturl()   # AFTER redirects, search methods check where they landed
            body = resp.read().decode("utf-8", "replace")

        # empty 200 means rate limited or python being python, so try curl.
        # real http errors already raised above so this isnt a fallback for 403s
        if not body.strip():
            body = self.fetch_with_curl(final_url, headers)

        # curl exiting 0 doesnt mean the site sent anything
        if not body.strip():
            raise RuntimeError("empty response from " + final_url)

        return final_url, body

    #################################################
    # ISBN stuff
    #################################################

    def isbn13_check_digit(self, first_12_digits):
        total = 0
        for pos, d in enumerate(first_12_digits):
            w = 1 if pos % 2 == 0 else 3
            total += int(d) * w
        return str((10 - total % 10) % 10)

    def normalize_isbn(self, raw):
        # returns (isbn, note)
        digits = re.sub(r"[^0-9]", "", raw)

        # ISBN 10: stick 978 on the front and make a new check digit
        # old check digit gets thrown away, never checked
        if len(digits) in (9, 10):
            first_12 = "978" + digits[:9]
            return first_12 + self.isbn13_check_digit(first_12), "converted from ISBN-10"

        # ISBN 13: assumes the LAST digit is the wrong one. a typo in the middle just
        # becomes a different valid ISBN, thats why the note has both
        if len(digits) == 13:
            right = digits[:12] + self.isbn13_check_digit(digits[:12])
            if right != digits:
                return right, f"check digit fixed (you gave {digits})"
            return digits, ""

        return None, "not a 9/10/13 digit ISBN"

    def first_match(self, patterns, text):
        # group 1 only, so the bit you want has to be in the first brackets
        for p in patterns:
            m = re.search(p, text, re.DOTALL)
            if m:
                return m.group(1)
        return None

    #################################################
    # page parsing
    #################################################

    def get_og_title(self, page):
        # attributes can be in either order, and each quote style gets its own pattern so
        # an apostrophe inside a "..." title (or a " inside a '...' one) doesnt end the capture
        return self.first_match([
            r'property=["\']og:title["\'][^>]*content="([^"]*)"',
            r"property=[\"']og:title[\"'][^>]*content='([^']*)'",
            r'content="([^"]*)"[^>]*property=["\']og:title["\']',
            r"content='([^']*)'[^>]*property=[\"']og:title[\"']",
        ], page)

    def clean_price(self, text):
        digits = re.sub(r"[^0-9]", "", text or "")   # "or" so None doesnt crash re.sub
        # 0 counts as missing
        if digits and int(digits) > 0:
            return digits
        return ""

    def strip_tags(self, text):
        # tags first, unescape second. other way round and an escaped &lt;b&gt; turns
        # into a real tag and gets deleted. tags become a space not nothing or words
        # stick together (took me way too long to work out why every author had a huge name)
        no_tags = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", html.unescape(no_tags))

    def has_hangul(self, text):
        # AC00 to D7A3 is the finished syllable block. lone jamo arent covered but titles dont need it
        return bool(re.search(r"[\uac00-\ud7a3]", text))

    def find_list_price(self, page):
        # 정가 is the list price, matching on the label keeps the sale price out

        # table row with 정가 as the label
        row = re.search(r"<th[^>]*>\s*정가\s*</th>\s*<td[^>]*>(.*?)</td>", page, re.DOTALL)
        if row:
            cell = self.strip_tags(row.group(1))
            price = self.clean_price(self.first_match([r"([\d,]{3,})"], cell))
            if price:
                return price

        # else "정가 12,000원" anywhere in the plain text, first hit wins
        m = re.search(r"정가\s*:?\s*([\d,]{3,})\s*원", self.strip_tags(page))
        if m:
            return self.clean_price(m.group(1))

        return ""

    def find_original_title(self, page):
        # 원서명 style rows. only counts if its english with no korean in it
        pairs = re.findall(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", page, re.DOTALL)

        for label_html, value_html in pairs:
            label = self.strip_tags(label_html).strip()

            # author rows and anything thats not an original title label get skipped
            if "저자" in label or not re.search(r"원서|원제|번역서명", label):
                continue

            value = self.strip_tags(value_html).strip()

            # needs a latin letter and NO hangul, english words mixed into korean dont count
            if re.search(r"[A-Za-z]", value) and not self.has_hangul(value):
                return value

        return ""

    def make_book(self, title, author, publisher, price, link, page):
        # same dict shape for every store. original title lives here so the search
        # methods dont all have to call it
        return {
            "title": title,
            "author": author,
            "publisher": publisher,
            "price": price,
            "link": link,
            "original_title": self.find_original_title(page),
        }

    #################################################
    # the stores
    #################################################

    def search_yes24(self, isbn):
        # no referer for yes24, its the goat
        url, page = self.fetch("https://www.yes24.com/product/search?domain=ALL&query=" + isbn)

        # if search didnt land on a book page, open the first result
        if "/product/goods/" not in url.lower():
            found = re.search(r"/product/goods/(\d+)", page)

            # None = no results, so look_up_book moves on quietly. an exception means it
            # broke and that one gets a note
            if not found:
                return None

            url, page = self.fetch("https://www.yes24.com/product/goods/" + found.group(1))

        # ISBN has to be on the page or its the wrong book. search can hand back some
        # other book for some funny reason (wonder what their algorithm is like)
        if isbn not in page:
            return None

        og = self.get_og_title(page)
        if not og:
            return None

        # og:title is "title | author | publisher - 예스24", by position. a title with
        # a pipe in it shifts everything over by one
        parts = [p.strip() for p in html.unescape(og).split("|")]
        title = parts[0]
        author = parts[1] if len(parts) > 1 else ""
        publisher = ""

        if len(parts) > 2:
            publisher = parts[2].replace("- 예스24", "").strip()

        return self.make_book(title, author, publisher, self.find_list_price(page), url, page)

    def get_kyobo_authors(self, page):
        # kyobo puts names and roles in separate spans, just to make my life harder

        # only look between author info and publisher info, other links on the page get ignored
        block = re.search(r'id="author-info"(.*?)id="publisher-info"', page, re.DOTALL)
        if not block:
            return ""

        # role span is optional, findall gives '' when its missing
        pat = (r'<a[^>]*>([^<]+)</a></span>'
               r'(?:\s*<span class="text-gray-800">([^<]+)</span>)?')
        groups = []
        pending = []

        # names pile up until a role like 저자 shows up, then they get it
        for name, role in re.findall(pat, block.group(1)):
            pending.append(html.unescape(name).strip())
            if role:
                groups.append((role, pending))
                pending = []   # NOT pending.clear(), that would empty the list inside groups too

        if pending:
            groups.append(("", pending))

        # keep 저자 groups only, translators etc get dropped. none of them? keep everyone
        authors_only = [g for g in groups if "저자" in g[0]]
        if not authors_only:
            authors_only = groups

        names = []
        for _, group_names in authors_only:
            for n in group_names:
                names.append(re.sub(r"\s+저$", "", n))   # stray 저 stuck on the end

        return ", ".join(dict.fromkeys(names))   # dedupes, keeps order

    def search_kyobo(self, isbn):
        # barcode url goes straight to the book if kyobo knows it
        url, page = self.fetch("https://www.kyobobook.co.kr/product/detailViewKor.laf"
                               "?mallGb=KOR&ejkGb=KOR&barcode=" + isbn, referer=self.KYOBO_REFERER)

        # no /detail/ in the url means it redirected somewhere else, so search and take
        # the first product (S then digits is kyobos id format)
        if "/detail/" not in url:
            _, results = self.fetch("https://search.kyobobook.co.kr/search?keyword=" + isbn,
                                    referer=self.KYOBO_REFERER)
            prod = re.search(r"product\.kyobobook\.co\.kr/detail/(S\d+)", results)

            if not prod:
                return None

            url, page = self.fetch("https://product.kyobobook.co.kr/detail/" + prod.group(1),
                                   referer=self.KYOBO_REFERER)

        if isbn not in page:   # wrong book, same check as yes24
            return None

        og = self.get_og_title(page)
        if not og:
            return None

        # chop "| 교보문고" off the end
        title = re.sub(r"\s*[|\-]\s*교보문고\s*$", "", html.unescape(og)).strip()

        author = self.get_kyobo_authors(page)

        pub = re.search(r'id="publisher-info"[^>]*>\s*<a[^>]*>([^<]+)</a>', page)
        publisher = html.unescape(pub.group(1)).strip() if pub else ""

        # original price, price tag first then the json data on the page. two spots so
        # if one breaks the other still works. \W{1,8} skips the quotes and colon
        price = self.clean_price(self.first_match([
            r'data-ga4-role="price-original"[^>]*>\s*([\d,]+)',
            r'originalPrice\W{1,8}(\d+)',
        ], page))

        return self.make_book(title, author, publisher, price, url, page)

    def search_aladin(self, isbn):
        # ISBN product page first, if the ISBN isnt on it we search instead
        url, page = self.fetch("https://www.aladin.co.kr/shop/wproduct.aspx?ISBN=" + isbn,
                               referer=self.ALADIN_REFERER)

        if isbn not in page:
            _, results = self.fetch("https://www.aladin.co.kr/search/wsearchresult.aspx"
                                    "?SearchTarget=All&SearchWord=" + isbn,
                                    referer=self.ALADIN_REFERER)
            prod = re.search(r"wproduct\.aspx\?ItemId=(\d+)", results)

            if not prod:
                return None

            url, page = self.fetch("https://www.aladin.co.kr/shop/wproduct.aspx?ItemId=" + prod.group(1),
                                   referer=self.ALADIN_REFERER)

            # first result isnt always the right book, so check again
            if isbn not in page:
                return None

        og = self.get_og_title(page) or ""
        og = re.sub(r"\s*[|\-]\s*알라딘\s*$", "", html.unescape(og)).strip()

        # og:title is "title | author". partition and not split, with no pipe you
        # still get 3 parts instead of an error
        title, _, og_author = og.partition("|")
        title = title.strip()
        og_author = og_author.split("|")[0].strip()

        # no og title? use the heading on the page
        if not title:
            h = re.search(r'class="Ere_bo_title"[^>]*>(.*?)</a>', page, re.DOTALL)
            title = self.strip_tags(h.group(1)).strip() if h else ""

        if not title:
            return None

        # author from og:title, else the author links on the page
        links = re.findall(r'<a[^>]*AuthorSearch=[^>]*>([^<]+)</a>', page)
        link_authors = [html.unescape(a).strip() for a in links]
        author = og_author or ", ".join(dict.fromkeys(link_authors))

        pub = re.search(r'<a[^>]*PublisherSearch=[^>]*>([^<]+)</a>', page)
        publisher = html.unescape(pub.group(1)).strip() if pub else ""

        # list price from the table, else the crossed out one. each side of the "or"
        # only runs if the one before gave ''. the .{0,300}? gap stops at the nearest
        # number so it cant wander off to some other price further down
        price = self.find_list_price(page) or self.clean_price(self.first_match([
            r"정가</div>.{0,300}?([\d,]{4,})\s*원",
            r"<strike>\s*([\d,]{4,})\s*원",
        ], page))

        return self.make_book(title, author, publisher, price, url, page)

    #################################################
    # putting a row together
    #################################################

    def look_up_book(self, isbn, notes):
        for store_name, search in self.stores:
            try:
                book = search(isbn)
            except Exception as e:
                # a store dying doesnt stop the loop, it just goes in the note
                # (None = store doesnt have it, no note. exception = something broke, note)
                notes.append(f"[{store_name} error: {e}]")
                book = None

            if book:
                # found it but no price? only the price comes from kyobo, title and
                # author stay from whoever found it. skipped if kyobo was the one
                # that found it, it already had its go
                if not book["price"] and store_name != "kyobo":
                    self.add_price_from_kyobo(book, isbn, notes)
                return book

            time.sleep(0.5)   # only hit on a miss, a find returns above so no wait

        return None

    def add_price_from_kyobo(self, book, isbn, notes):
        # loads the whole kyobo page again just for the price. own try/except because
        # the book is already found, if kyobo dies here we still keep it
        try:
            kb = self.search_kyobo(isbn)
        except Exception as e:
            notes.append(f"[kyobo price error: {e}]")
            return

        # kb can be None, checking it first so the price lookup never runs on nothing
        if kb and kb["price"]:
            book["price"] = kb["price"]
            notes.append("price from kyobo")

    def build_row(self, raw_line):
        isbn, first_note = self.normalize_isbn(raw_line)

        # everything that happens to this one ISBN piles up here in order, joined at the end
        notes = [first_note] if first_note else []

        # every column starts empty so failed rows still line up in the csv
        row = dict.fromkeys(self.COLUMNS, "")
        row["input"] = raw_line
        row["isbn13"] = isbn or ""

        if isbn:
            book = self.look_up_book(isbn, notes)

            if book:
                row["title"] = book["title"]
                row["author"] = book["author"]
                row["publisher"] = book["publisher"]
                row["price_krw"] = book["price"]
                row["source_link"] = book["link"]

                if book["original_title"]:
                    row["title_en"] = book["original_title"]
                    row["title_en_src"] = "original"
            else:
                notes.append("book not found, youre on your own here my fella")

        # price_krw is a digit string so int() first. .2f keeps trailing zeros and
        # gives a string back, which Frontend.print_row needs
        if row["price_krw"]:
            row["price_nzd"] = f"{int(row['price_krw']) * self.krw_to_nzd:.2f}"

        row["note"] = " ".join(notes)
        return row

    def look_up_all(self, lines):
        # one row per line, one at a time so the frontends can show each row as it
        # lands. the sleep is here so both the cli and the gui get the same pacing
        for line in lines:
            yield self.build_row(line)
            time.sleep(1)

    #################################################
    # csv
    #################################################

    def write_rows(self, f, rows):
        # the one csv writer, the file and the clipboard copy both go through this
        w = csv.DictWriter(f, fieldnames=self.COLUMNS)
        w.writeheader()
        w.writerows(rows)

    def write_csv(self, rows):
        # csv only gets written after the loop, so a crash or ctrl c halfway loses every
        # row and the terminal is the only copy. newline="" is for windows, without it
        # you get blank lines between rows. no BOM so excel may show hangul as garbage,
        # utf-8-sig fixes that
        with open("isbn_results.csv", "w", newline="", encoding="utf-8") as f:
            self.write_rows(f, rows)

    def csv_text(self, rows):
        # same writer but into memory, so what you paste is exactly what the file has
        buf = io.StringIO()
        self.write_rows(buf, rows)
        return buf.getvalue()


class Frontend:
    # all the colours live here so the whole look can be changed from one spot
    BG = "#140d24"           # window
    CARD = "#1f1535"         # input and table panels
    CARD_ALT = "#281b46"     # every second table row
    EDGE = "#3a2766"         # thin outlines and table headings
    ACCENT = "#8b5cf6"       # main purple
    ACCENT_HOT = "#a78bfa"   # purple when the mouse is over a button
    ACCENT_DOWN = "#6d3fe0"  # purple while its being clicked
    TEXT = "#ece6ff"
    MUTED = "#9a8cc4"
    OFF = "#2b2145"          # greyed out button
    WINDOW_ALPHA = 0.93      # 1.0 is solid, lower is more see through

    def __init__(self, backend):
        self.backend = backend

        # gui state. lives on self so every method below can see it
        self.rows = []
        self.total = 0
        self.running = False
        self.q = queue.Queue()   # worker thread -> gui. tkinter isnt thread safe so only the main thread touches widgets

    #################################################
    # command line
    #################################################

    def print_row(self, row):
        # join only takes strings (thats why price_nzd is stored as one)
        # publisher and title_en_src are csv only
        print("\t".join([
            row["isbn13"] or row["input"],
            row["title"],
            row["title_en"],
            row["author"],
            row["price_krw"],
            row["price_nzd"],
            row["source_link"],
            row["note"],
        ]))

    def run_cli(self, path):
        # strip THEN check so a line of just spaces counts as blank (this bugged me for ages)
        with open(path, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]

        if not lines:
            sys.exit("No ISBNs found in " + path)

        rows = []
        for row in self.backend.look_up_all(lines):
            rows.append(row)
            self.print_row(row)

        self.backend.write_csv(rows)

        print("\nSaved isbn_results.csv, paste its contents into the chat.")

    #################################################
    # gui
    #################################################

    def mix(self, c1, c2, t):
        # blends two hex colours. t=0 gives c1, t=1 gives c2
        a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
        return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))

    def round_rect(self, canvas, x1, y1, x2, y2, r, **kw):
        # tkinter has no rounded rectangle. a smooth polygon turns the corners into
        # curves, the doubled points are what keep the straight edges straight
        pts = [x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
               x2, y1 + r, x2, y2 - r, x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2,
               x1 + r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y2 - r, x1, y1 + r,
               x1, y1 + r, x1, y1]
        return canvas.create_polygon(pts, smooth=True, **kw)

    def make_widgets(self, tk):
        # these two subclass tk.Canvas so they cant sit at the top of the file, tkinter
        # isnt imported there. they get built in here once its been imported
        ui = self   # the widgets have their own self, so the frontend goes by ui in here

        class RoundedButton(tk.Canvas):
            # a canvas drawn as a pill. the colour slides between normal and hover in a few
            # small steps, thats the hover animation
            def __init__(self, parent, text, command=None, state="normal", width=170, height=40):
                super().__init__(parent, width=width, height=height, bg=ui.BG,
                                 highlightthickness=0, bd=0)
                self.bw, self.bh = width, height   # bw and bh not w and h, tkinter keeps the widget path name in _w and i dont want to be near it
                self.command = command
                self.btn_state = state
                self.t = 0.0        # how far along the hover blend is, 0 to 1
                self.target = 0.0
                self.job = None
                self.pressed = False

                self.shape = ui.round_rect(self, 1, 1, width - 1, height - 1, height // 2,
                                           fill=ui.ACCENT, outline="")
                self.label = self.create_text(width // 2, height // 2, text=text,
                                              fill="white", font=ui.font_bold)
                self.bind("<Enter>", lambda e: self.animate(1.0))
                self.bind("<Leave>", self.on_leave)
                self.bind("<ButtonPress-1>", self.on_down)
                self.bind("<ButtonRelease-1>", self.on_up)
                self.paint()

            def paint(self):
                if self.btn_state == "disabled":
                    fill, text = ui.OFF, ui.MUTED
                elif self.pressed:
                    fill, text = ui.ACCENT_DOWN, "white"
                else:
                    fill, text = ui.mix(ui.ACCENT, ui.ACCENT_HOT, self.t), "white"
                self.itemconfigure(self.shape, fill=fill)
                self.itemconfigure(self.label, fill=text)
                super().configure(cursor="" if self.btn_state == "disabled" else "hand2")

            def animate(self, target):
                if self.btn_state == "disabled":
                    target = 0.0
                self.target = target
                if self.job is None:   # already ticking? then it just picks up the new target
                    self.tick()

            def tick(self):
                step = 0.2
                if abs(self.target - self.t) <= step:
                    self.t = self.target
                    self.job = None
                else:
                    self.t += step if self.target > self.t else -step
                    self.job = self.after(16, self.tick)
                self.paint()

            def on_leave(self, event):
                self.pressed = False
                self.animate(0.0)

            def on_down(self, event):
                if self.btn_state != "disabled":
                    self.pressed = True
                    self.paint()

            def on_up(self, event):
                if not self.pressed:
                    return
                self.pressed = False
                self.paint()
                # only counts as a click if the mouse is still on the button when let go
                if 0 <= event.x <= self.bw and 0 <= event.y <= self.bh and self.command:
                    self.command()

            def configure(self, cnf=None, **kw):
                # so go_btn.config(state=..., command=...) works like it did on a normal button
                if "command" in kw:
                    self.command = kw.pop("command")
                if "state" in kw:
                    self.btn_state = kw.pop("state")
                    self.animate(0.0)
                    self.paint()
                if cnf or kw:
                    return super().configure(cnf, **kw)

            config = configure

        class Card(tk.Canvas):
            # rounded panel. put children in .inner, the shape redraws whenever the size changes
            def __init__(self, parent, height=None, pad=12, radius=18):
                extra = {"height": height} if height else {}
                super().__init__(parent, bg=ui.BG, highlightthickness=0, bd=0, **extra)
                self.pad, self.radius = pad, radius
                self.inner = tk.Frame(self, bg=ui.CARD)
                self.win = self.create_window(pad, pad, window=self.inner, anchor="nw")
                self.bind("<Configure>", self.redraw)

            def redraw(self, event):
                self.delete("panel")
                ui.round_rect(self, 1, 1, event.width - 1, event.height - 1, self.radius,
                              fill=ui.CARD, outline=ui.EDGE, tags="panel")
                self.tag_lower("panel")
                self.itemconfigure(self.win, width=event.width - 2 * self.pad,
                                   height=event.height - 2 * self.pad)

        return RoundedButton, Card

    def fade_in(self, a=0.0):
        # see through window. tkinter cant blur anything by itself, the blur has to come
        # from the compositor (hyprland blurs transparent windows when its blur is on)
        a += 0.07
        try:
            self.root.attributes("-alpha", min(a, self.WINDOW_ALPHA))
        except self.tk.TclError:
            return   # no alpha support here, the window just stays solid
        if a < self.WINDOW_ALPHA:
            self.root.after(16, self.fade_in, a)

    def build_ui(self):
        tk, ttk = self.tk, self.ttk
        root = self.root
        RoundedButton, Card = self.make_widgets(tk)

        tk.Label(root, text="ISBN lookup", bg=self.BG, fg=self.TEXT, font=self.font_title,
                 anchor="w").pack(fill="x", padx=24, pady=(20, 0))

        # the input never touches disk, it only lives in this text box and the
        # worker's list
        tk.Label(root, anchor="w", bg=self.BG, fg=self.MUTED, font=self.font_ui,
                 text="Paste ISBNs below, one per line (ISBN-10 or 13, hyphens are fine)"
                 ).pack(fill="x", padx=24, pady=(0, 10))

        # wrap none so a long line doesnt look like two isbns
        input_card = Card(root, height=150)
        input_card.pack(fill="x", padx=24)
        self.input_box = tk.Text(input_card.inner, wrap="none", bg=self.CARD, fg=self.TEXT,
                                 font=self.font_mono, insertbackground=self.ACCENT_HOT,
                                 selectbackground=self.ACCENT, selectforeground="white",
                                 relief="flat", bd=0, highlightthickness=0, padx=8, pady=6)
        self.input_box.pack(fill="both", expand=True)

        # copy button starts greyed out, it only turns on once theres a csv to copy
        buttons = tk.Frame(root, bg=self.BG)
        buttons.pack(fill="x", padx=24, pady=14)
        self.go_btn = RoundedButton(buttons, "Look up  (Ctrl+Enter)", width=210)
        self.go_btn.pack(side="left")
        self.copy_btn = RoundedButton(buttons, "Copy CSV", state="disabled", width=140)
        self.copy_btn.pack(side="left", padx=10)
        self.status = tk.StringVar(value="Ready")
        tk.Label(buttons, textvariable=self.status, anchor="w", bg=self.BG, fg=self.MUTED,
                 font=self.font_ui).pack(side="left", padx=10)

        # the ttk widgets cant be coloured one by one, they go through a style. clam is the
        # theme that listens to the colours
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background=self.CARD, fieldbackground=self.CARD,
                        foreground=self.TEXT, borderwidth=0, rowheight=28, font=self.font_ui)
        style.configure("Treeview.Heading", background=self.EDGE, foreground=self.TEXT,
                        relief="flat", borderwidth=0, padding=(8, 6), font=self.font_bold)
        style.map("Treeview.Heading", background=[("active", self.ACCENT)])
        style.map("Treeview", background=[("selected", self.ACCENT)],
                  foreground=[("selected", "white")])
        for side in ("Vertical", "Horizontal"):
            style.configure(f"{side}.TScrollbar", background=self.EDGE, troughcolor=self.CARD,
                            bordercolor=self.CARD, lightcolor=self.EDGE, darkcolor=self.EDGE,
                            arrowcolor=self.TEXT, gripcount=0)   # gripcount 0 removes the dark notch clam draws in the middle of the thumb
            style.map(f"{side}.TScrollbar", background=[("active", self.ACCENT)])

        # results table, one column per csv column so it shows the same thing the csv has
        table_card = Card(root, pad=14)
        table_card.pack(fill="both", expand=True, padx=24, pady=(0, 24))
        table_frame = table_card.inner

        columns = self.backend.COLUMNS
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        self.tree.tag_configure("plain", background=self.CARD)
        self.tree.tag_configure("alt", background=self.CARD_ALT)
        ysb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        xsb = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)

        # stretch=False so the columns keep these widths and the bottom scrollbar
        # has something to scroll. anything not listed gets 120
        widths = {"title": 240, "title_en": 200, "author": 150, "publisher": 120,
                  "price_krw": 80, "price_nzd": 80, "title_en_src": 90,
                  "source_link": 280, "note": 300}
        for c in columns:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=widths.get(c, 120), stretch=False)

        # grid not pack here, the two scrollbars have to hug the table's right and bottom edges
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")

    def worker(self, lines):
        # runs on its own thread. only ever puts things in the queue, never touches a widget
        try:
            for row in self.backend.look_up_all(lines):
                self.q.put(("row", row))
        except Exception as e:
            self.q.put(("error", str(e)))
        finally:
            self.q.put(("done", None))   # always sent so the button never stays locked

    def poll(self):
        # runs on the main thread every 100ms, drains whatever the worker has queued up
        # and puts it in the table. same loop as run_cli just spread out over time
        error = None
        finished = False

        while True:
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                break

            if kind == "row":
                self.rows.append(payload)
                # every second row gets the lighter purple so long rows are easier to follow
                tag = "alt" if len(self.rows) % 2 == 0 else "plain"
                self.tree.insert("", "end", values=[payload[c] for c in self.backend.COLUMNS],
                                 tags=(tag,))
                self.tree.yview_moveto(1)   # keeps the newest row in view
                self.status.set(f"Looking up... {len(self.rows)}/{self.total}")
            elif kind == "error":
                error = payload
            elif kind == "done":
                finished = True

        if finished:
            # last row is on screen by now, csv gets written right alongside it
            msg = ""
            try:
                self.backend.write_csv(self.rows)
                msg = f"Done, {len(self.rows)}/{self.total} books. Saved isbn_results.csv"
            except OSError as e:
                msg = f"Done but the csv could not be saved: {e}"
            if error:
                msg += f" (stopped early: {error})"
            self.status.set(msg)
            self.running = False
            self.go_btn.config(state="normal")
            self.copy_btn.config(state="normal" if self.rows else "disabled")
        else:
            self.root.after(100, self.poll)

    def start(self, event=None):
        # double click or ctrl+enter spam while a run is going would start a second worker
        if self.running:
            return "break"

        # same strip THEN check trick as run_cli, but on the box instead of a file
        lines = [l.strip() for l in self.input_box.get("1.0", "end").splitlines() if l.strip()]
        if not lines:
            self.messagebox.showwarning("No ISBNs", "Paste at least one ISBN first.")
            return "break"

        # fresh run = wipe the old rows
        self.rows = []
        self.tree.delete(*self.tree.get_children())
        self.total = len(lines)
        self.running = True
        self.go_btn.config(state="disabled")
        self.copy_btn.config(state="disabled")
        self.status.set(f"Looking up... 0/{len(lines)}")

        # daemon so closing the window doesnt wait for a lookup in progress
        threading.Thread(target=self.worker, args=(lines,), daemon=True).start()
        self.root.after(100, self.poll)
        return "break"   # stops ctrl+enter putting a newline in the box

    def copy_csv(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.backend.csv_text(self.rows))
        self.status.set("Copied the csv to the clipboard")

    def run_gui(self):
        # imported in here so the normal command line mode still works on a machine
        # with no tkinter installed
        import tkinter as tk
        from tkinter import font as tkfont
        from tkinter import messagebox, ttk
        self.tk, self.ttk, self.messagebox = tk, ttk, messagebox

        self.root = tk.Tk(className="isbnlookup")   # className so a compositor rule can pick this window out
        self.root.title("ISBN lookup")
        self.root.geometry("1150x700")   # wide, the table has 11 columns and the links are long
        self.root.configure(bg=self.BG)

        self.fade_in()

        # fonts come from whatever the system already uses so nothing has to be installed
        family = tkfont.nametofont("TkDefaultFont").actual("family")
        mono = tkfont.nametofont("TkFixedFont").actual("family")
        self.font_ui = (family, 10)
        self.font_bold = (family, 10, "bold")
        self.font_title = (family, 20, "bold")
        self.font_mono = (mono, 10)

        self.build_ui()

        self.go_btn.config(command=self.start)
        self.copy_btn.config(command=self.copy_csv)
        self.root.bind("<Control-Return>", self.start)
        self.input_box.focus_set()
        self.root.mainloop()


def main():
    # nargs="?" makes the file optional, thats what lets --gui run with no file at all
    parser = argparse.ArgumentParser(description="look up korean book ISBNs")
    parser.add_argument("isbn_file", nargs="?", help="text file, one ISBN per line")
    parser.add_argument("--gui", action="store_true",
                        help="open a window with its own input box instead of reading a file")
    args = parser.parse_args()

    if args.gui and args.isbn_file:
        parser.error("--gui has its own input box, dont pass a file with it")
    if not args.gui and not args.isbn_file:
        parser.error("give an ISBN file, or use --gui")   # exits with code 2 and usage on stderr

    # made after the args are checked, so a bad command doesnt sit waiting on the rate fetch
    frontend = Frontend(Backend())

    if args.gui:
        frontend.run_gui()
    else:
        frontend.run_cli(args.isbn_file)


# only runs when the file is run directly. importing it doesnt hit the rate api
# anymore, only making a Backend does
if __name__ == "__main__":
    main()

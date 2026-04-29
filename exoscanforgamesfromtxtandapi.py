"""
PDF Game Reference Scanner - Hybrid Mode
=========================================
Scans a folder and ALL subfolders for PDFs and finds every game
from your title list that is mentioned, with page numbers.

HOW IT WORKS:
  Your game titles are loaded from a text file (one per line).
  Each page of text is sent to the Claude API along with your title list.
  Claude uses its language understanding to find matches even in garbled
  OCR text - far more accurate than simple string matching.
  Only titles from YOUR list are ever returned - no invented games.

TITLE LIST FILE FORMAT:
  One game title per line, e.g.:
    Pac-Man
    Galaga
    Donkey Kong
  Lines starting with # are comments and ignored. Blank lines ignored.

MODES:
  - Hybrid (default): title list + API. Best accuracy, costs API tokens.
  - API only: set TITLE_LIST_FILE = "" to find any game (not just your list).
  - No API: set TITLE_LIST_FILE and leave api_key.txt blank for basic
    string matching (less accurate for OCR'd documents).

Before running:
    pip install pdfplumber pikepdf anthropic pdf2image pytesseract Pillow thefuzz python-Levenshtein

For OCR support:
  - Tesseract: https://github.com/UB-Mannheim/tesseract/wiki
  - Poppler:   https://github.com/oschwartz10612/poppler-windows/releases

CHECKPOINT BEHAVIOUR:
  - Files where games ARE found are checkpointed and skipped on reruns.
  - Files where NO games are found are always rescanned.
  - To force a full rescan, delete scan_checkpoint.json from FOLDER.
"""

import os
import sys
import json
import re
import tempfile
import warnings
import logging
from collections import defaultdict









warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

# =============================================================================
# CONFIGURE THESE SETTINGS
# =============================================================================

FOLDER = r"C:\Users\guyut\OneDrive\Desktop\exowork"
# Root folder - ALL subfolders will be scanned automatically

TITLE_LIST_FILE = r"C:\Users\guyut\OneDrive\Desktop\exowork\gametitles.txt"
# Text file of game titles to search for, one per line.
# Set to "" to search for ANY game (API mode only, no title constraint).

OUTPUT_FILE = "games_found.txt"
# Output filename - saved into FOLDER

PAGES_PER_BATCH = 5
# Pages sent per API call. Reduce to 3 if you hit token limit errors.

MIN_PAGE_CHARS = 50
# Pages with fewer characters are treated as image-only and sent to OCR.

TITLES_PER_PROMPT = 200
# How many titles to include per API call.
# If your list is very large, titles are split into chunks of this size
# and each chunk is checked against the page batch separately.

# --- OCR PATHS (only needed for scanned image PDFs) -------------------------

TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
POPPLER_PATH   = r"C:\poppler\Library\bin"

# =============================================================================


def install_if_missing(package, import_name=None):
    import importlib
    import subprocess
    name = import_name if import_name else package.split(".")[0]
    try:
        importlib.import_module(name)
    except ImportError:
        print("Installing " + package + "...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])


install_if_missing("pdfplumber")
install_if_missing("pikepdf")
install_if_missing("anthropic")
install_if_missing("pdf2image")
install_if_missing("pytesseract")
install_if_missing("Pillow")
install_if_missing("thefuzz", "thefuzz")
install_if_missing("python-Levenshtein", "Levenshtein")

import pdfplumber
import pikepdf
from PIL import Image
Image.MAX_IMAGE_PIXELS = None


def setup_tesseract():
    import pytesseract
    for path in [TESSERACT_PATH,
                 r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                 r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"]:
        if os.path.isfile(path):
            pytesseract.pytesseract.tesseract_cmd = path
            return True
    return False


OCR_AVAILABLE    = setup_tesseract()
POPPLER_AVAILABLE = os.path.isdir(POPPLER_PATH)

if not OCR_AVAILABLE:
    print("NOTE: Tesseract not found - scanned image PDFs will be skipped.")
    print("")
if not POPPLER_AVAILABLE:
    print("NOTE: Poppler not found - scanned image PDFs will be skipped.")
    print("      Set POPPLER_PATH at the top of the script.")
    print("")


# =============================================================================
# TITLE LIST
# =============================================================================

def load_title_list(path):
    """Load game titles from a text file, one per line."""
    if not os.path.isfile(path):
        print("ERROR: TITLE_LIST_FILE not found:")
        print("  " + path)
        sys.exit(1)
    titles = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                titles.append(line)
    if not titles:
        print("ERROR: No titles found in TITLE_LIST_FILE: " + path)
        sys.exit(1)
    return titles


# =============================================================================
# API KEY
# =============================================================================

def get_api_key():
    key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_key.txt")
    if os.path.isfile(key_file):
        with open(key_file, encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    return os.environ.get("ANTHROPIC_API_KEY", "")


# =============================================================================
# GAME EXTRACTION - HYBRID (title list + API)
# =============================================================================

def extract_games_hybrid(batch, api_key, title_chunk):
    """
    Send a batch of pages + a chunk of titles to Claude.
    Claude finds any of those specific titles in the page text,
    using context to handle OCR errors and garbled text.
    Returns { page_num: [matched_titles] }
    """
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    titles_block = "\n".join("- " + t for t in title_chunk)

    page_blocks = ""
    for page_num, text in batch:
        page_blocks += "=== PAGE " + str(page_num) + " ===\n" + text[:1500] + "\n\n"

    prompt = (
        "You are searching scanned magazine/book text for specific game titles.\n"
        "The text may be garbled due to OCR scanning errors.\n\n"
        "GAME TITLES TO SEARCH FOR:\n"
        + titles_block
        + "\n\n"
        "INSTRUCTIONS:\n"
        "- Search the pages below for any mention of the titles listed above.\n"
        "- Use context clues and your knowledge to identify titles even with OCR errors.\n"
        "  For example: 'Pac-Man' might appear as 'Pac Man', 'PacMan', 'Pac-mon' etc.\n"
        "- Only return titles from the list above. Do NOT add any titles not in the list.\n"
        "- A title counts if it is clearly being referenced, reviewed, advertised, or discussed.\n"
        "- Return ONLY a JSON object where keys are page numbers (as strings)\n"
        "  and values are arrays of matched titles from the list above.\n"
        "- Return {} if no titles from the list are found.\n"
        "- No explanation, preamble, or markdown formatting.\n\n"
        "Example output:\n"
        "{\"12\": [\"Pac-Man\", \"Galaga\"], \"15\": [\"Donkey Kong\"]}\n\n"
        "PAGES TO SEARCH:\n"
        + page_blocks
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        # Debug: uncomment to see raw API responses
        # print("    [API]: " + raw[:200])
        raw = raw.replace("```json", "").replace("```", "").strip()
        start = raw.find("{")
        end   = raw.rfind("}")
        if start == -1 or end == -1:
            return {}
        result = json.loads(raw[start:end + 1])
        return result if isinstance(result, dict) else {}
    except Exception as e:
        print("    [API error: " + str(e) + "]")
        return {}


# =============================================================================
# GAME EXTRACTION - API ONLY (no title list constraint)
# =============================================================================

def extract_games_api_only(batch, api_key):
    """Find any game in the text, unconstrained."""
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    page_blocks = ""
    for page_num, text in batch:
        page_blocks += "=== PAGE " + str(page_num) + " ===\n" + text[:1500] + "\n\n"

    prompt = (
        "Read the following pages and identify every GAME referenced.\n\n"
        "Include any named game: board games, card games, video games, arcade games,\n"
        "tabletop RPGs, party games, sports games, wargames, puzzle games, coin-op games.\n\n"
        "Be INCLUSIVE. Skip common words (risk, life, go, war) unless clearly a game title.\n\n"
        "Return ONLY a JSON object: keys = page numbers (strings), values = arrays of game names.\n"
        "Return {} if no games found. No explanation or markdown.\n\n"
        "Example: {\"12\": [\"Pac-Man\", \"Galaga\"], \"15\": [\"Dungeons & Dragons\"]}\n\n"
        + page_blocks
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        start = raw.find("{")
        end   = raw.rfind("}")
        if start == -1 or end == -1:
            return {}
        result = json.loads(raw[start:end + 1])
        return result if isinstance(result, dict) else {}
    except Exception as e:
        print("    [API error: " + str(e) + "]")
        return {}


# =============================================================================
# GAME EXTRACTION - BASIC STRING MATCH (no API fallback)
# =============================================================================

def extract_games_string_match(text_pages, titles):
    """Simple case-insensitive substring search. Less accurate for OCR text."""
    from thefuzz import fuzz

    game_hits = defaultdict(set)
    for page_num, text in text_pages:
        if not text.strip():
            continue
        text_norm = text.lower()
        text_norm = re.sub(r"[^a-z0-9\s]", " ", text_norm)
        text_norm = re.sub(r"\s+", " ", text_norm).strip()
        for title in titles:
            title_norm = title.lower()
            title_norm = re.sub(r"[^a-z0-9\s]", " ", title_norm)
            title_norm = re.sub(r"\s+", " ", title_norm).strip()
            score = fuzz.partial_ratio(title_norm, text_norm)
            if score >= 85:
                game_hits[page_num].add(title)
    return game_hits


# =============================================================================
# PDF READING WITH FALLBACK
# =============================================================================

def read_pages_pdfplumber(path):
    pages = []
    with pdfplumber.open(path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            pages.append((page_num, text))
    return pages


def read_pages_pikepdf(path):
    tmp_path = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
        os.close(tmp_fd)
        with pikepdf.open(path, suppress_warnings=True, attempt_recovery=True) as pdf:
            pdf.save(tmp_path)
        pages = []
        with pdfplumber.open(tmp_path) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                pages.append((page_num, text))
        return pages
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def ocr_page_image(image):
    import pytesseract
    Image.MAX_IMAGE_PIXELS = None
    try:
        return pytesseract.image_to_string(image, lang="eng")
    except Exception:
        return ""


def read_pages_ocr(path):
    from pdf2image import convert_from_path
    from pdf2image.pdf2image import pdfinfo_from_path
    Image.MAX_IMAGE_PIXELS = None
    poppler = POPPLER_PATH if POPPLER_AVAILABLE else None

    try:
        info = pdfinfo_from_path(path, poppler_path=poppler)
        page_count = info["Pages"]
    except Exception:
        page_count = 999

    pages = []
    for page_num in range(1, page_count + 1):
        try:
            images = convert_from_path(
                path, dpi=120,
                first_page=page_num, last_page=page_num,
                poppler_path=poppler
            )
            if images:
                text = ocr_page_image(images[0])
                pages.append((page_num, text))
                del images
        except Exception as e:
            print("    [OCR page " + str(page_num) + " failed: " + str(e) + "]")
            continue
    return pages


def read_pdf(path):
    # Attempt 1: pdfplumber
    try:
        pages = read_pages_pdfplumber(path)
        if sum(len(t.strip()) for _, t in pages) >= MIN_PAGE_CHARS:
            return pages, "pdfplumber", None
        raise Exception("no text")
    except Exception:
        pass

    # Attempt 2: pikepdf repair
    try:
        pages = read_pages_pikepdf(path)
        if sum(len(t.strip()) for _, t in pages) >= MIN_PAGE_CHARS:
            return pages, "pikepdf", None
        raise Exception("no text after repair")
    except Exception:
        pass

    # Attempt 3: OCR
    if OCR_AVAILABLE and POPPLER_AVAILABLE:
        try:
            pages = read_pages_ocr(path)
            return pages, "ocr", None
        except Exception as e:
            return [], None, "OCR failed: " + str(e)

    return [], None, "No text found and OCR not available"


# =============================================================================
# HELPERS
# =============================================================================

def find_all_pdfs(root_folder):
    found = []
    for dirpath, dirnames, filenames in os.walk(root_folder):
        for filename in sorted(filenames):
            if filename.lower().endswith(".pdf"):
                full_path = os.path.join(dirpath, filename)
                rel_path  = os.path.relpath(full_path, root_folder)
                found.append((rel_path, full_path))
    return sorted(found)


def format_pages(pages):
    if not pages:
        return ""
    pages = sorted(set(pages))
    ranges = []
    start = end = pages[0]
    for p in pages[1:]:
        if p == end + 1:
            end = p
        else:
            ranges.append(str(start) if start == end else str(start) + "-" + str(end))
            start = end = p
    ranges.append(str(start) if start == end else str(start) + "-" + str(end))
    return ", ".join(ranges)


def chunk_list(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def merge_results(base, new):
    """Merge two { page_str: [titles] } dicts."""
    for page_str, titles in new.items():
        if page_str not in base:
            base[page_str] = []
        for t in titles:
            if t not in base[page_str]:
                base[page_str].append(t)
    return base


# =============================================================================
# CHECKPOINT
# =============================================================================

def load_checkpoint(checkpoint_file):
    if os.path.isfile(checkpoint_file):
        try:
            with open(checkpoint_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"completed_with_hits": [], "master": {}}


def save_checkpoint(checkpoint_file, completed_with_hits, master):
    plain_master = {}
    for game, pdf_refs in master.items():
        plain_master[game] = {}
        for pdf, pages in pdf_refs.items():
            plain_master[game][pdf] = list(set(pages))
    try:
        with open(checkpoint_file, "w", encoding="utf-8") as f:
            json.dump({
                "completed_with_hits": completed_with_hits,
                "master": plain_master
            }, f, indent=2)
    except Exception as e:
        print("  [checkpoint save failed: " + str(e) + "]")


# =============================================================================
# MAIN
# =============================================================================

def main():
    if not os.path.isdir(FOLDER):
        print("ERROR: Folder not found: " + FOLDER)
        sys.exit(1)

    # Determine mode
    titles = []
    title_chunks = []
    if TITLE_LIST_FILE:
        titles = load_title_list(TITLE_LIST_FILE)
        title_chunks = list(chunk_list(titles, TITLES_PER_PROMPT))

    api_key = get_api_key()

    if TITLE_LIST_FILE and api_key:
        mode = "hybrid (title list + API) - " + str(len(titles)) + " titles"
    elif TITLE_LIST_FILE and not api_key:
        mode = "string match (title list, no API) - " + str(len(titles)) + " titles"
    elif not TITLE_LIST_FILE and api_key:
        mode = "API only (any game)"
    else:
        print("ERROR: No TITLE_LIST_FILE and no API key found.")
        print("Set TITLE_LIST_FILE or add api_key.txt to your exowork folder.")
        sys.exit(1)

    print("")
    print("Searching for PDFs in: " + FOLDER)
    all_pdfs = find_all_pdfs(FOLDER)

    if not all_pdfs:
        print("No PDF files found.")
        sys.exit(0)

    ocr_status = "enabled" if (OCR_AVAILABLE and POPPLER_AVAILABLE) else "disabled"

    checkpoint_file = os.path.join(FOLDER, "scan_checkpoint.json")
    checkpoint = load_checkpoint(checkpoint_file)
    completed_with_hits = checkpoint["completed_with_hits"]

    master = defaultdict(lambda: defaultdict(list))
    for game, pdf_refs in checkpoint["master"].items():
        for pdf, pages in pdf_refs.items():
            master[game][pdf].extend(pages)

    skipped   = [p for p, _ in all_pdfs if p in completed_with_hits]
    remaining = [(p, fp) for p, fp in all_pdfs if p not in completed_with_hits]

    print("Found " + str(len(all_pdfs)) + " PDF(s) across all subfolders.")
    if skipped:
        print("Skipping " + str(len(skipped)) + " file(s) already confirmed with game hits.")
    print("Scanning " + str(len(remaining)) + " file(s).")
    print("Mode: " + mode)
    print("OCR:  " + ocr_status)
    print("NOTE: Files with no games found will always be rescanned on next run.")
    print("=" * 60)

    error_count   = 0
    method_counts = defaultdict(int)
    total_pages   = 0
    skipped_pages = 0
    api_calls     = 0

    output_lines = [
        "GAME REFERENCES FOUND",
        "Mode: " + mode,
        "OCR: " + ocr_status,
        "Folder: " + FOLDER,
        "=" * 60,
        ""
    ]

    for rel_path, full_path in remaining:
        print("")
        print(rel_path)

        pages, method, error = read_pdf(full_path)

        if error:
            msg = "  [ERROR] " + error
            print(msg)
            output_lines.append("")
            output_lines.append("[ERROR] " + rel_path + ": " + error)
            error_count += 1
            continue

        method_counts[method] += 1
        total_pages += len(pages)

        if method in ("ocr", "ocr+pikepdf"):
            print("  [OCR - scanned image PDF]")
        elif method == "pikepdf":
            print("  [repaired via pikepdf]")

        text_pages = [(pn, txt) for pn, txt in pages if len(txt.strip()) >= MIN_PAGE_CHARS]
        blank = len(pages) - len(text_pages)
        skipped_pages += blank
        if blank:
            print("  Skipping " + str(blank) + " blank page(s)")

        if not text_pages:
            print("  (no readable text found)")
            continue

        # Collect results as { page_str: [title, ...] }
        combined_result = {}
        game_hits = defaultdict(set)

        if TITLE_LIST_FILE and api_key:
            # Hybrid mode: send page batches x title chunks to API
            for batch in chunk_list(text_pages, PAGES_PER_BATCH):
                for tchunk in title_chunks:
                    api_calls += 1
                    result = extract_games_hybrid(batch, api_key, tchunk)
                    combined_result = merge_results(combined_result, result)

            for page_str, game_list in combined_result.items():
                try:
                    page_num = int(page_str)
                except ValueError:
                    continue
                for game in game_list:
                    if game and isinstance(game, str):
                        game_hits[page_num].add(game.strip())

        elif not TITLE_LIST_FILE and api_key:
            # API only mode
            for batch in chunk_list(text_pages, PAGES_PER_BATCH):
                api_calls += 1
                result = extract_games_api_only(batch, api_key)
                for page_str, game_list in result.items():
                    try:
                        page_num = int(page_str)
                    except ValueError:
                        continue
                    for game in game_list:
                        if game and isinstance(game, str):
                            game_hits[page_num].add(game.strip())

        else:
            # String match fallback (no API)
            game_hits = extract_games_string_match(text_pages, titles)

        if not game_hits:
            print("  (no games found)")
            continue

        games_on_this_file = defaultdict(set)
        for page_num, games in game_hits.items():
            for game in games:
                games_on_this_file[game].add(page_num)

        label = rel_path
        if method in ("ocr", "ocr+pikepdf"):
            label = rel_path + "  [OCR]"
        elif method == "pikepdf":
            label = rel_path + "  [repaired]"

        output_lines.append("")
        output_lines.append(label)

        for game in sorted(games_on_this_file.keys(), key=str.lower):
            page_set = games_on_this_file[game]
            pages_str = format_pages(sorted(page_set))
            line = "   " + game + " -- p. " + pages_str
            print(line)
            output_lines.append(line)
            master[game][rel_path].extend(sorted(page_set))

        completed_with_hits.append(rel_path)
        save_checkpoint(checkpoint_file, completed_with_hits, master)

    # Master index
    output_lines.append("")
    output_lines.append("")
    output_lines.append("=" * 60)
    output_lines.append("MASTER INDEX (all games A-Z)")
    output_lines.append("=" * 60)

    if master:
        for game in sorted(master.keys(), key=str.lower):
            output_lines.append("")
            output_lines.append(game)
            for rel_path, page_list in sorted(master[game].items()):
                output_lines.append("   " + rel_path + " -- p. " + format_pages(page_list))
    else:
        output_lines.append("")
        output_lines.append("(no games found in any file)")

    output_lines.append("")
    output_lines.append("=" * 60)
    summary = "Done. Scanned " + str(len(all_pdfs)) + " PDF(s)"
    summary += ", " + str(total_pages) + " total pages"
    summary += ", " + str(skipped_pages) + " blank pages skipped"
    if api_calls:
        summary += ", " + str(api_calls) + " API calls"
    if method_counts.get("pikepdf", 0):
        summary += ", " + str(method_counts["pikepdf"]) + " repaired"
    ocr_total = method_counts.get("ocr", 0) + method_counts.get("ocr+pikepdf", 0)
    if ocr_total:
        summary += ", " + str(ocr_total) + " OCR'd"
    if error_count:
        summary += ", " + str(error_count) + " failed"
    summary += "."
    output_lines.append(summary)

    out_path = os.path.join(FOLDER, OUTPUT_FILE)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(output_lines))

    print("")
    print("=" * 60)
    print(summary)
    print("Results saved to: " + out_path)


if __name__ == "__main__":
    main()
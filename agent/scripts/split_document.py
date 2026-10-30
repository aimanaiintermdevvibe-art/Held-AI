"""Split a long document into smaller files that can be uploaded one by one.

    python agent/scripts/split_document.py <file> [--pages 100] [--chars 400000] [--out DIR]

Uploads of any length are embedded in the background, so splitting is optional. It helps
when a file is over the upload size limit (DOCUMENT_MAX_BYTES, 50 MB by default), when
DOCUMENT_MAX_CHUNKS is set, or when you want sections to show up as separate documents.

- .pdf: parts of --pages pages each, as PDFs with the original pages (page numbers in
  the file names, e.g. guide.part02.p101-200.pdf).
- .txt / .md: parts of at most --chars characters, cut between paragraphs.
- .docx: its text, in .txt parts of at most --chars characters (formatting is not kept).

Each part's passage count (with the current chunk settings) is printed. Nothing is uploaded.
"""
import argparse
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

from backend.core.config import get_settings  # noqa: E402
from backend.rag.chunking import chunk_text  # noqa: E402
from backend.rag.extract import DocumentError, extension_of, extract_sections  # noqa: E402


def passages(sections) -> int:
    settings = get_settings()
    return sum(len(chunk_text(s.text, settings.document_chunk_chars, settings.document_chunk_overlap)) for s in sections)


def split_pdf(path: Path, pages_per_part: int, out_dir: Path) -> list[Path]:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(path))
    total = len(reader.pages)
    parts = []
    for number, start in enumerate(range(0, total, pages_per_part), 1):
        end = min(start + pages_per_part, total)
        writer = PdfWriter()
        for index in range(start, end):
            writer.add_page(reader.pages[index])
        target = out_dir / f"{path.stem}.part{number:02d}.p{start + 1}-{end}.pdf"
        with target.open("wb") as handle:
            writer.write(handle)
        parts.append(target)
    return parts


def split_text(text: str, chars_per_part: int) -> list[str]:
    """Parts of at most chars_per_part characters, cut between paragraphs where possible."""
    parts, current = [], ""
    for paragraph in text.split("\n\n"):
        while len(paragraph) > chars_per_part:  # a single huge paragraph: cut at a space
            cut = paragraph.rfind(" ", 0, chars_per_part)
            cut = cut if cut > chars_per_part // 2 else chars_per_part
            if current:
                parts.append(current)
                current = ""
            parts.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= chars_per_part:
            current = candidate
        else:
            parts.append(current)
            current = paragraph
    if current:
        parts.append(current)
    return [p for p in parts if p.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", type=Path)
    parser.add_argument("--pages", type=int, default=100, help="pages per PDF part (default 100)")
    parser.add_argument("--chars", type=int, default=400_000, help="characters per text part (default 400000)")
    parser.add_argument("--out", type=Path, help="output folder (default: <file>_parts next to the file)")
    args = parser.parse_args()

    path: Path = args.file
    if not path.is_file():
        raise SystemExit(f"No such file: {path}")
    extension = extension_of(path.name)
    data = path.read_bytes()
    try:
        whole = extract_sections(path.name, data)
    except DocumentError as exc:
        raise SystemExit(f"Cannot split {path.name}: {exc}")
    out_dir = args.out or path.with_name(f"{path.stem}_parts")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"{path.name}: {len(data) / (1024 * 1024):.1f} MB, {passages(whole)} passages in total.")

    if extension == ".pdf":
        files = split_pdf(path, max(args.pages, 1), out_dir)
    else:
        suffix = ".md" if extension in (".md", ".markdown") else ".txt"
        text = "\n\n".join(s.text for s in whole)
        files = []
        for number, part in enumerate(split_text(text, max(args.chars, 1000)), 1):
            target = out_dir / f"{path.stem}.part{number:02d}{suffix}"
            target.write_text(part + "\n", encoding="utf-8")
            files.append(target)

    for target in files:
        sections = extract_sections(target.name, target.read_bytes())
        print(f"  {target.name}: {target.stat().st_size / 1024:.0f} KB, {passages(sections)} passages")
    print(f"{len(files)} part(s) written to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

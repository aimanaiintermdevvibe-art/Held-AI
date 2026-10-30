"""Split text into overlapping chunks that end at paragraph, sentence or word boundaries."""
import re

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def chunk_text(text: str, max_chars: int = 1200, overlap: int = 200) -> list[str]:
    """Chunks of at most max_chars. Each chunk after the first starts with up to `overlap`
    characters (whole sentences or words) from the end of the previous one, so a passage
    cut at a boundary is still found in one piece."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    # Pieces leave room for the overlap, so the previous chunk's tail always fits in front.
    piece_limit = max(max_chars - overlap - 2, max_chars // 2)
    pieces: list[str] = []
    for paragraph in paragraphs:
        pieces.extend([paragraph] if len(paragraph) <= piece_limit else _split_long(paragraph, piece_limit))

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}\n\n{piece}" if current else piece
        if len(candidate) <= max_chars:
            current = candidate
            continue
        chunks.append(current)
        tail = _tail(current, overlap)
        current = f"{tail}\n\n{piece}" if tail and len(tail) + 2 + len(piece) <= max_chars else piece
    if current:
        chunks.append(current)
    return chunks


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    """A paragraph longer than max_chars, split by sentences (and by words inside very long ones)."""
    out: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(paragraph):
        while len(sentence) > max_chars:
            cut = sentence.rfind(" ", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            if current:
                out.append(current)
                current = ""
            out.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        candidate = f"{current} {sentence}".strip()
        if len(candidate) <= max_chars:
            current = candidate
        else:
            out.append(current)
            current = sentence
    if current:
        out.append(current)
    return out


def _tail(chunk: str, overlap: int) -> str:
    """The end of a chunk, up to `overlap` characters, starting at a sentence (or word) boundary."""
    if overlap <= 0 or len(chunk) <= overlap:
        return ""
    tail = chunk[-overlap:]
    boundary = _SENTENCE_END.search(tail)
    if boundary:
        return tail[boundary.end():].strip()
    space = tail.find(" ")
    return tail[space + 1:].strip() if space >= 0 else ""

"""容忍排版空白，但引用仍回写为当前证据中的连续原文。"""
from .models import Evidence, EvidenceAnchor


def _cjk(char: str) -> bool:
    return '\u3400' <= char <= '\u9fff'


def _mapped(text: str):
    chars, spans = [], []
    index = 0
    while index < len(text):
        end = index + 1
        if text[index].isspace():
            while end < len(text) and text[end].isspace():
                end += 1
            if index and end < len(text) and not (_cjk(text[index - 1]) and _cjk(text[end])):
                chars.append(' ')
                spans.append((index, end))
        else:
            chars.append(text[index])
            spans.append((index, end))
        index = end
    return ''.join(chars), spans


def normalized_quote(text: str) -> str:
    return _mapped(text)[0]


def resolve_anchor(anchor: EvidenceAnchor, sources: list[Evidence]) -> EvidenceAnchor:
    quote = normalized_quote(anchor.quote)
    matching = [e for e in sources if e.evidence_id == anchor.evidence_id]
    for source in matching:
        for value in (source.title, source.content, source.snippet):
            if not value or not quote:
                continue
            canonical, spans = _mapped(value)
            start = canonical.find(quote)
            if start >= 0:
                exact = value[spans[start][0]:spans[start + len(quote) - 1][1]]
                return EvidenceAnchor(evidence_id=anchor.evidence_id, quote=exact)
    excerpts = [v[:350] for e in matching for v in (e.title, e.content, e.snippet) if v]
    raise ValueError(f"引用不匹配：evidenceId={anchor.evidence_id}；quote={anchor.quote!r}；"
                     f"当前原文片段={list(dict.fromkeys(excerpts))!r}。请重新选取连续原文，不要改写。")

"""Acronym and symbol expansions for speech. The pre-pass applies these to prose
before the normalizer sees it so the model does not have to guess.

Keys are matched as whole words, case-sensitive where the key is upper-case.
"""

from __future__ import annotations

import re

ACRONYMS: dict[str, str] = {
    "API": "A P I",
    "APIs": "A P Is",
    "CLI": "C L I",
    "CSS": "C S S",
    "CSV": "C S V",
    "DB": "database",
    "DNS": "D N S",
    "ENV": "environment",
    "GUI": "G U I",
    "HTML": "H T M L",
    "HTTP": "H T T P",
    "HTTPS": "H T T P S",
    "ID": "I D",
    "IDs": "I Ds",
    "IP": "I P",
    "JSON": "jason",
    "JSONL": "jason L",
    "JWT": "J W T",
    "LAN": "lan",
    "LLM": "L L M",
    "MCP": "M C P",
    "MVP": "M V P",
    "ORM": "O R M",
    "OS": "O S",
    "PCM": "P C M",
    "PR": "pull request",
    "PRs": "pull requests",
    "README": "read me",
    "REPL": "repple",
    "REST": "rest",
    "SDK": "S D K",
    "SQL": "sequel",
    "SQLite": "sequel light",
    "SSH": "S S H",
    "SSL": "S S L",
    "STT": "speech to text",
    "TLS": "T L S",
    "TOML": "tomml",
    "TTS": "text to speech",
    "TUI": "T U I",
    "UI": "U I",
    "URL": "U R L",
    "URLs": "U R Ls",
    "UUID": "U U I D",
    "VAD": "voice activity detection",
    "VM": "V M",
    "WS": "web socket",
    "XML": "X M L",
    "YAML": "yammel",
    "CPU": "C P U",
    "GPU": "G P U",
    "RAM": "ram",
    "PID": "P I D",
    "CI": "C I",
    "CD": "C D",
    "k8s": "kubernetes",
    "npm": "N P M",
    "pytest": "pie test",
    "tmux": "T mux",
    "stdout": "standard out",
    "stderr": "standard error",
    "stdin": "standard in",
    "async": "a sync",
    "regex": "reg ex",
    "repo": "repo",
}

# Inline symbols that have a spoken form when they appear in prose.
SYMBOLS: dict[str, str] = {
    "->": " to ",
    "=>": " to ",
    "&&": " and ",
    "||": " or ",
    "==": " equals ",
    "!=": " not equal to ",
    ">=": " at least ",
    "<=": " at most ",
    "~/": "home slash ",
    "%": " percent",
    "&": " and ",
}

_WORD = re.compile(r"(?<![\w/.\-])([A-Za-z0-9]+)(?![\w/.\-])")


def expand_acronyms(text: str) -> str:
    def sub(m: re.Match[str]) -> str:
        w = m.group(1)
        if w in ACRONYMS:
            return ACRONYMS[w]
        return w

    return _WORD.sub(sub, text)


def expand_symbols(text: str) -> str:
    for k in sorted(SYMBOLS, key=len, reverse=True):
        text = text.replace(k, SYMBOLS[k])
    return re.sub(r"\s{2,}", " ", text).strip()

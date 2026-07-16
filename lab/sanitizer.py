"""Sanitizer: everything published by Hannah Lab passes through here.

Two layers, in order:

1. REDACTION - identifying-but-not-catastrophic details are replaced with
   stable placeholders: local usernames, the hostname, home-directory paths,
   private/public IPs, MAC addresses, absolute model paths. The text stays
   publishable and the redactions are reported so they can be shown as lab
   artifacts ("[user] is logged in").

2. BLOCKING (fail closed) - if anything that looks like an actual secret
   survives redaction (private keys, AWS credentials, API tokens, bearer
   headers, kubeconfig blobs, .env-style secret assignments), the artifact is
   marked NOT publishable. We never try to "clean" a secret; its presence
   means the artifact stays private.

The sanitizer is deliberately heuristic. It is meant to catch obvious
dangerous content, not to be a perfect DLP system - which is also why the
publish path treats a block as final rather than attempting repair.
"""

import getpass
import json
import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path

# --- Placeholders used for redacted values -------------------------------------
USER_PLACEHOLDER = "[user]"
HOST_PLACEHOLDER = "[host]"
HOME_PLACEHOLDER = "[home]"
IP_PLACEHOLDER = "[ip]"
MAC_PLACEHOLDER = "[mac]"
PATH_PLACEHOLDER = "[local-path]"


def _local_usernames() -> list:
    """Real human account names on this machine (never published)."""
    names = set()
    try:
        names.add(getpass.getuser())
    except Exception:
        pass
    for var in ("USER", "LOGNAME", "SUDO_USER"):
        v = os.environ.get(var)
        if v:
            names.add(v)
    # Regular (uid >= 1000) accounts from /etc/passwd, skipping the catch-all.
    try:
        for line in Path("/etc/passwd").read_text().splitlines():
            parts = line.split(":")
            if len(parts) > 2 and parts[2].isdigit() and 1000 <= int(parts[2]) < 60000:
                if parts[0] not in ("nobody",):
                    names.add(parts[0])
    except OSError:
        pass
    return sorted(n for n in names if len(n) >= 3)


def _local_hostnames() -> list:
    names = set()
    try:
        h = socket.gethostname()
        if h:
            names.add(h)
            names.add(h.split(".")[0])
    except Exception:
        pass
    for p in ("/etc/hostname",):
        try:
            t = Path(p).read_text().strip()
            if t:
                names.add(t)
        except OSError:
            pass
    return sorted(n for n in names if len(n) >= 3)


def _home_dirs() -> list:
    dirs = {str(Path.home())}
    for u in _local_usernames():
        dirs.add(f"/home/{u}")
        dirs.add(f"/Users/{u}")
    return sorted(dirs, key=len, reverse=True)


# --- Redaction patterns ---------------------------------------------------------

_PRIVATE_IP = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|169\.254\.\d{1,3}\.\d{1,3})\b"
)
# Any other dotted quad (public IP) except loopback/0.0.0.0, redacted by default.
_ANY_IP = re.compile(r"\b(?!127\.|0\.0\.0\.0)(?:\d{1,3}\.){3}\d{1,3}\b")
_MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")
# Absolute paths into places that describe the private machine layout.
_LOCAL_PATH = re.compile(r"(?:/ssd|/mnt|/media|/srv)/[\w./@-]+")
_MODEL_PATH = re.compile(r"/[\w./@-]*\.gguf\b")

# --- Blocking (fail-closed) patterns --------------------------------------------
# (label, compiled regex). A match after redaction => artifact is not publishable.
_BLOCKERS = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    ("AWS secret assignment", re.compile(
        r"(?i)aws_secret_access_key\s*[=:]\s*\S+")),
    ("OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("authorization header", re.compile(r"(?i)\bauthorization:\s*\S{16,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("kubeconfig content", re.compile(r"(?i)\bclient-key-data:\s*\S+")),
    ("secret-looking assignment", re.compile(
        r"(?im)^\s*(?:export\s+)?[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|"
        r"PRIVATE_?KEY|CREDENTIALS?)[A-Z0-9_]*\s*=\s*['\"]?[^\s'\"]{8,}")),
    ("ssh private key path leak", re.compile(r"\.ssh/id_[a-z0-9_]+(?!\.pub)\b")),
]


@dataclass
class SanitizeResult:
    text: str
    redactions: list = field(default_factory=list)  # e.g. ["username", "private ip"]
    blocked: bool = False
    block_reasons: list = field(default_factory=list)

    @property
    def publishable(self) -> bool:
        return not self.blocked


class Sanitizer:
    """Reusable sanitizer bound to this machine's private identifiers.

    extra_terms: additional strings to always redact (from config
    lab.redact_terms), e.g. an SSID or a family member's name.
    """

    def __init__(self, extra_terms=None, allow_public_ips: bool = False):
        self.usernames = _local_usernames()
        self.hostnames = _local_hostnames()
        self.home_dirs = _home_dirs()
        self.extra_terms = [t for t in (extra_terms or []) if t and len(t) >= 3]
        self.allow_public_ips = allow_public_ips

    # -- redaction ---------------------------------------------------------------
    def redact(self, text: str):
        """Return (redacted_text, [labels of what was redacted])."""
        hits = []

        def sub(pattern, repl, label, s):
            new, n = pattern.subn(repl, s)
            if n:
                hits.append(label)
            return new

        # Longest-first so /home/user is caught before the bare username.
        for d in self.home_dirs:
            if d in text:
                text = text.replace(d, HOME_PLACEHOLDER)
                hits.append("home path")
        text = sub(_MODEL_PATH, PATH_PLACEHOLDER, "model path", text)
        text = sub(_LOCAL_PATH, PATH_PLACEHOLDER, "local path", text)
        text = sub(_PRIVATE_IP, IP_PLACEHOLDER, "private ip", text)
        if not self.allow_public_ips:
            text = sub(_ANY_IP, IP_PLACEHOLDER, "ip address", text)
        text = sub(_MAC, MAC_PLACEHOLDER, "mac address", text)
        for name in self.usernames:
            pat = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
            text, n = pat.subn(USER_PLACEHOLDER, text)
            if n:
                hits.append("username")
        for name in self.hostnames:
            pat = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
            text, n = pat.subn(HOST_PLACEHOLDER, text)
            if n:
                hits.append("hostname")
        for term in self.extra_terms:
            if term in text:
                text = text.replace(term, "[redacted]")
                hits.append("custom term")
        return text, sorted(set(hits))

    # -- blocking ----------------------------------------------------------------
    @staticmethod
    def find_blockers(text: str) -> list:
        return [label for label, pat in _BLOCKERS if pat.search(text)]

    # -- combined ----------------------------------------------------------------
    def sanitize_text(self, text: str) -> SanitizeResult:
        """Redact identifying details, then fail closed on surviving secrets."""
        if not isinstance(text, str):
            text = "" if text is None else str(text)
        redacted, hits = self.redact(text)
        blockers = self.find_blockers(redacted)
        return SanitizeResult(
            text=redacted, redactions=hits,
            blocked=bool(blockers), block_reasons=blockers,
        )

    def sanitize_obj(self, obj):
        """Recursively sanitize a JSON-able structure.

        Returns (sanitized_obj, all_redaction_labels, all_block_reasons).
        String values are redacted in place; a block anywhere taints the whole
        object (the caller decides whether to drop just the field or the file).
        """
        redactions, blocks = set(), set()

        def walk(node):
            if isinstance(node, str):
                r = self.sanitize_text(node)
                redactions.update(r.redactions)
                blocks.update(r.block_reasons)
                return r.text
            if isinstance(node, list):
                return [walk(x) for x in node]
            if isinstance(node, dict):
                return {k: walk(v) for k, v in node.items()}
            return node

        return walk(obj), sorted(redactions), sorted(blocks)

    def check_file_tree(self, root: Path) -> list:
        """Final gate before publish: scan every text file under root for
        secrets. Returns [(relative_path, [reasons])]; empty means safe."""
        problems = []
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            try:
                text = p.read_text(errors="ignore")
            except OSError:
                continue
            reasons = self.find_blockers(text)
            # The rendered site must never contain unredacted identifiers either.
            for name in self.usernames:
                if re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE):
                    reasons.append("local username present")
                    break
            for name in self.hostnames:
                if re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE):
                    reasons.append("local hostname present")
                    break
            if reasons:
                problems.append((str(p.relative_to(root)), sorted(set(reasons))))
        return problems


def make_sanitizer(cfg: dict) -> Sanitizer:
    lab = cfg.get("lab", {}) if cfg else {}
    return Sanitizer(
        extra_terms=lab.get("redact_terms", []),
        allow_public_ips=bool(lab.get("allow_public_ips", False)),
    )


if __name__ == "__main__":
    # Quick self-test: pipe text through the sanitizer.
    import sys
    s = Sanitizer()
    res = s.sanitize_text(sys.stdin.read())
    print(json.dumps({
        "publishable": res.publishable,
        "redactions": res.redactions,
        "block_reasons": res.block_reasons,
    }, indent=2))
    print(res.text)

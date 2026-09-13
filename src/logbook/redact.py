import re

REDACTED = "[REDACTED]"

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bnpm_[A-Za-z0-9]{36}"),
    re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+"),
]

KEY_NAMES = r"[A-Z0-9_-]*(?:api[_-]?key|secret|password|passwd|token|private[_-]?key|client[_-]?secret)[A-Z0-9_-]*"
KEY_VALUE = re.compile(rf"(?i)\b({KEY_NAMES})([\"']?\s*[=:]\s*[\"']?)([^\s\"',;]{{8,}})")
FLAG_VALUE = re.compile(rf"(?i)(--{KEY_NAMES})(\s+[\"']?)([^\s\"',;]{{8,}})")
BASIC_AUTH = re.compile(r"(?i)(authorization:\s*)basic\s+[A-Za-z0-9+/=]{8,}")
HAS_LETTER = re.compile(r"[A-Za-z]")
HAS_NON_LETTER = re.compile(r"[^A-Za-z]")
URL_CREDENTIALS = re.compile(r"(://[^/\s:@]+:)([^@\s/]+)(@)")

SENSITIVE_PATH = re.compile(
    r"(^|/)(\.env(\.[\w.-]+)?|\.netrc|\.npmrc|\.pypirc|credentials(\.json)?|secrets?\.(json|ya?ml|toml|env)|"
    r"id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|[\w.-]*\.(pem|key|p12|pfx|jks|keystore))$"
)


def looks_like_secret(value: str) -> bool:
    return HAS_LETTER.search(value) is not None and HAS_NON_LETTER.search(value) is not None


def mask_key_value(m: re.Match[str]) -> str:
    if not looks_like_secret(m.group(3)):
        return m.group(0)
    return f"{m.group(1)}{m.group(2)}{REDACTED}"


def redact(text: str) -> str:
    if not text:
        return text
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = KEY_VALUE.sub(mask_key_value, text)
    text = FLAG_VALUE.sub(mask_key_value, text)
    text = BASIC_AUTH.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    return URL_CREDENTIALS.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(3)}", text)


def is_sensitive_path(path: str) -> bool:
    return bool(path) and SENSITIVE_PATH.search(path) is not None

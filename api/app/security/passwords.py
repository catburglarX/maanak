"""Password hashing and password policy.

Argon2id is used with parameters taken from configuration so that cost can be
raised without a code change. ``verify_password`` reports whether the stored hash
used weaker parameters than the current policy, which lets the login path
transparently upgrade a hash after a successful sign-in.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

from ..config import get_settings

# Passwords are compared and hashed after NFKC normalisation so that visually
# identical input from different keyboards behaves consistently.
_NORMALISATION: Literal["NFC", "NFD", "NFKC", "NFKD"] = "NFKC"

#: Rejected outright regardless of length. Short list on purpose: the length
#: requirement does most of the work, and a huge embedded list is a maintenance
#: burden. Deployments needing a full breach corpus should use the
#: check_breach_corpus hook below.
COMMON_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "passw0rd",
        "123456789012",
        "qwertyuiop12",
        "administrator",
        "letmein12345",
        "welcome12345",
        "maanak123456",
        "legalmetrology",
    }
)


@dataclass(frozen=True)
class PolicyResult:
    """Outcome of checking a candidate password against policy."""

    acceptable: bool
    problems: tuple[str, ...] = ()

    @property
    def message(self) -> str:
        return " ".join(self.problems)


@lru_cache(maxsize=1)
def _hasher() -> PasswordHasher:
    settings = get_settings()
    return PasswordHasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost_kib,
        parallelism=settings.argon2_parallelism,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    )


def normalise(password: str) -> str:
    return unicodedata.normalize(_NORMALISATION, password)


def hash_password(password: str) -> str:
    """Return an Argon2id hash. The plaintext is never logged or stored."""
    return _hasher().hash(normalise(password))


def verify_password(password: str, stored_hash: str) -> tuple[bool, bool]:
    """Verify a password.

    Returns ``(matched, needs_rehash)``. A malformed stored hash is treated as a
    failed match rather than an error, so a corrupted row cannot be used to
    distinguish accounts.
    """
    hasher = _hasher()
    try:
        hasher.verify(stored_hash, normalise(password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, False

    try:
        return True, hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:  # pragma: no cover - defensive
        return True, True


def dummy_verify() -> None:
    """Consume comparable CPU time when the account does not exist.

    Without this, an unknown email returns measurably faster than a known email
    with a wrong password, which leaks account existence.
    """
    with suppress(VerifyMismatchError, VerificationError, InvalidHashError):
        _hasher().verify(_dummy_hash(), "not-the-password")


def check_policy(
    password: str, *, email: str | None = None, name: str | None = None
) -> PolicyResult:
    """Check a candidate password against the configured policy.

    The rules follow current OWASP authentication guidance: length is the primary
    control, composition rules are kept light, and context-specific words
    (the account's own email or name) are rejected.
    """
    settings = get_settings()
    candidate = normalise(password)
    problems: list[str] = []

    if len(candidate) < settings.password_min_length:
        problems.append(f"Use at least {settings.password_min_length} characters.")
    if len(candidate) > 128:
        problems.append("Use no more than 128 characters.")
    if candidate != candidate.strip():
        problems.append("Remove leading or trailing spaces.")
    if candidate.lower() in COMMON_PASSWORDS:
        problems.append("That password is too common.")
    if _has_long_run(candidate):
        problems.append("Avoid repeating the same character four or more times.")
    if _is_single_character_class(candidate) and len(candidate) < 16:
        problems.append("Mix letters with numbers or symbols, or use a longer passphrase.")

    for context_value in (email, name):
        if context_value and _contains_context(candidate, context_value):
            problems.append("Do not include your name or email address.")
            break

    return PolicyResult(acceptable=not problems, problems=tuple(problems))


def _has_long_run(value: str) -> bool:
    return re.search(r"(.)\1{3,}", value) is not None


def _is_single_character_class(value: str) -> bool:
    classes = (
        any(character.islower() for character in value),
        any(character.isupper() for character in value),
        any(character.isdigit() for character in value),
        any(not character.isalnum() for character in value),
    )
    return sum(classes) <= 1


def _contains_context(candidate: str, context_value: str) -> bool:
    lowered = candidate.lower()
    local_part = context_value.split("@")[0].lower()
    return any(
        len(token) >= 4 and token in lowered for token in re.split(r"[^a-z0-9]+", local_part)
    )


def breach_check_prefix(password: str) -> str:
    """SHA-1 prefix for a k-anonymity breach lookup.

    Provided so a deployment can add an offline or self-hosted breach-corpus
    check. Maanak does not send passwords or hashes anywhere by default.
    """
    digest = hashlib.sha1(normalise(password).encode("utf-8")).hexdigest().upper()  # noqa: S324
    return digest[:5]


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """A real hash produced with the current parameters.

    Computed once at first use rather than written as a literal, so it stays valid
    if the Argon2 parameters change and the timing it consumes always matches the
    live configuration.
    """
    return _hasher().hash(secrets.token_urlsafe(16))

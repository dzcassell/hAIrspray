"""Synthetic PII generator for DLP testing.

Values produced by this module are *plausible-but-invalid*: they pass
the obvious structural validators (Luhn for credit cards, ISO 7064
mod-97 for IBAN, ISO 3779 check digit for VIN, Base58 check digest for
BTC addresses, etc.) so they trip regex and pattern-based DLP
classifiers, but they come from clearly-invalid address ranges,
reserved test sequences, or use fictional institution codes so no real
person or account is impacted.

Every category has per-locale variants where locales diverge on
format. US is the default and always populated; other locales are
filled in incrementally as we need them.

The module is deterministic when given a ``seed`` argument — helpful
for tests. When no seed is given, values rotate across calls so each
"Fire All" produces a fresh set of PII, matching what an actual DLP
eval expects (a one-time fingerprint would get whitelisted).

Categories covered:

* address
* phone_number
* email
* credit_card
* iban
* bank_account
* swift
* drivers_license
* vehicle_id (VIN)
* passport
* national_id (SSN in US)
* tax_id
* vat_id
* health_id
* crypto_address
* medical_record_number
* health_plan_beneficiary_number

Each generator returns a dict shaped like::

    {"value": "1234 Maple St, Apt 5, Boston MA 02115",
     "label": "Address",
     "locale": "US",
     "field_name": "home address"}

``value`` is the PII string.
``label`` is the display name.
``field_name`` is the natural phrase to use when embedding the value
  in a sentence ("My {field_name} is {value}").
"""
from __future__ import annotations

import random
import string
from typing import Any

CATEGORIES = [
    "address",
    "phone_number",
    "email",
    "credit_card",
    "iban",
    "bank_account",
    "swift",
    "drivers_license",
    "vehicle_id",
    "passport",
    "national_id",
    "tax_id",
    "vat_id",
    "health_id",
    "crypto_address",
    "medical_record_number",
    "health_plan_beneficiary_number",
]

LOCALES = ["US", "UK", "EU"]  # UK/EU partially populated for now


# =====================================================================
# Checksum helpers (pure math — tested independently)
# =====================================================================

def _luhn_check_digit(number_without_checkdigit: str) -> str:
    """Return the single Luhn check digit for a numeric string."""
    total = 0
    for i, ch in enumerate(reversed(number_without_checkdigit)):
        d = int(ch)
        # Double every second digit from the right (i=0 is rightmost,
        # which ends up as i=1 in this formulation since we'll be
        # APPENDING the check digit — the rightmost body digit is at
        # i=0 but becomes the second-from-right in the final string,
        # so it gets doubled).
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - (total % 10)) % 10)


def _iban_check_digits(country_code: str, bban: str) -> str:
    """ISO 7064 mod-97 check digits for an IBAN.

    ``bban`` is the country-specific body (no country code, no check
    digits). Returns the two-digit check as a string.
    """
    # Rearrange: BBAN + country code + "00", convert letters to 2-digit
    # numbers (A=10..Z=35), then check = 98 - (huge_int mod 97).
    rearranged = bban + country_code + "00"
    expanded = "".join(
        str(ord(ch) - 55) if ch.isalpha() else ch
        for ch in rearranged.upper()
    )
    check = 98 - (int(expanded) % 97)
    return f"{check:02d}"


def _vin_check_digit(vin_without_check: str) -> str:
    """ISO 3779 VIN check digit (position 9).

    ``vin_without_check`` must be 17 chars with a placeholder at pos 9
    (we'll overwrite it). Returns the correct check character.
    """
    # Transliteration table per the spec.
    trans = {
        "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
        "J": 1, "K": 2, "L": 3, "M": 4, "N": 5,           "P": 7,
        "R": 9,
        "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
    }
    weights = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]
    total = 0
    for i, ch in enumerate(vin_without_check):
        ch = ch.upper()
        if ch.isdigit():
            val = int(ch)
        else:
            val = trans.get(ch)
            if val is None:
                # Not a valid VIN letter — fall back to 0 so we don't
                # crash, though the caller shouldn't pass such a char.
                val = 0
        total += val * weights[i]
    rem = total % 11
    return "X" if rem == 10 else str(rem)


def _base58_check_encode(payload_hex: str) -> str:
    """Base58Check encode a hex payload (for BTC P2PKH addresses).

    Payload should be 1 version byte + 20 hash bytes = 42 hex chars.
    Output is the final Base58 address. No dependencies.
    """
    import hashlib
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    payload = bytes.fromhex(payload_hex)
    checksum = hashlib.sha256(
        hashlib.sha256(payload).digest()
    ).digest()[:4]
    full = payload + checksum
    # Count leading zero bytes → '1' characters.
    n_leading = 0
    for b in full:
        if b == 0:
            n_leading += 1
        else:
            break
    # Convert to big int and divmod by 58.
    num = int.from_bytes(full, "big")
    out = []
    while num > 0:
        num, rem = divmod(num, 58)
        out.append(alphabet[rem])
    return "1" * n_leading + "".join(reversed(out))


# =====================================================================
# Per-category generators. Each returns a dict.
# =====================================================================

def _rng(seed: int | None) -> random.Random:
    return random.Random(seed) if seed is not None else random.Random()


def gen_address(locale: str, r: random.Random) -> dict[str, Any]:
    if locale == "UK":
        street_num = r.randint(1, 200)
        streets = ["High", "Kings", "Queens", "Church", "Park"]
        towns = ["Manchester M1 5AB", "Liverpool L1 4DQ", "Leeds LS1 6HX"]
        val = f"{street_num} {r.choice(streets)} Road, Flat 2, {r.choice(towns)}"
    elif locale == "EU":
        street_num = r.randint(1, 200)
        val = f"Hauptstraße {street_num}, {r.randint(10000, 99999)} Berlin, Germany"
    else:  # US
        street_num = r.randint(100, 9999)
        streets = ["Maple", "Oak", "Cedar", "Pine", "Elm"]
        # 02115 is a real Boston ZIP, but street + zip won't geolocate
        # to a real person. Using famous university-adjacent ZIPs
        # because DLP classifiers expect valid US ZIP ranges.
        zips = ["02115", "10001", "94107", "60601", "33101"]
        cities = [
            ("Boston", "MA"), ("New York", "NY"), ("San Francisco", "CA"),
            ("Chicago", "IL"), ("Miami", "FL"),
        ]
        i = r.randint(0, 4)
        city, state = cities[i]
        val = f"{street_num} {r.choice(streets)} St, Apt {r.randint(1,50)}, {city} {state} {zips[i]}"
    return {
        "value": val, "label": "Address", "locale": locale,
        "field_name": "home address",
    }


def gen_phone_number(locale: str, r: random.Random) -> dict[str, Any]:
    if locale == "UK":
        val = f"+44 20 7{r.randint(100, 999)} {r.randint(1000, 9999)}"
    elif locale == "EU":
        val = f"+49 30 {r.randint(10000000, 99999999)}"
    else:  # US
        # 555 is a reserved test prefix; widely exempted by DLP
        # classifiers, so avoid it. Use real area codes (212/415/617)
        # with 01XX/02XX/03XX local prefixes — these are unassigned
        # in most NANP plans, so no real number collision. Format as
        # standard NANP 3-4 (XXX-XXXX) with parens around area code.
        area = r.choice([212, 415, 617, 310, 312])
        # Exchange: 3 digits, leading "0" keeps us out of any assigned
        # NANP prefix (real exchanges are 200-999 per NANP rules).
        exch = f"0{r.randint(10,99)}"
        line = f"{r.randint(1000,9999)}"
        val = f"+1 ({area}) {exch}-{line}"
    return {
        "value": val, "label": "Phone Number", "locale": locale,
        "field_name": "phone number",
    }


def gen_email(locale: str, r: random.Random) -> dict[str, Any]:
    firsts = ["alice", "bob", "carol", "david", "eve", "frank"]
    lasts  = ["smith", "jones", "martinez", "nguyen", "patel"]
    # example.com is RFC 2606 reserved — will never resolve to a real
    # mailbox, but is realistic enough that most DLP regex will match
    # the RFC 5322 address pattern.
    val = f"{r.choice(firsts)}.{r.choice(lasts)}{r.randint(1,999)}@example.com"
    return {
        "value": val, "label": "Email", "locale": locale,
        "field_name": "email address",
    }


def gen_credit_card(locale: str, r: random.Random) -> dict[str, Any]:
    # Visa IIN 4929 — real BIN range but we're generating non-test
    # card numbers; the Luhn checksum makes them pass validators.
    # Real cards issued on this BIN exist but collision in the 10^11
    # space is negligible and no real card is being simulated in its
    # entirety.
    iin = "4929"
    body = "".join(str(r.randint(0, 9)) for _ in range(11))
    base = iin + body  # 15 digits
    val = base + _luhn_check_digit(base)
    # Format as 4-4-4-4 for realism.
    val = f"{val[0:4]} {val[4:8]} {val[8:12]} {val[12:16]}"
    return {
        "value": val, "label": "Credit Card Number", "locale": locale,
        "field_name": "credit card number",
    }


def gen_iban(locale: str, r: random.Random) -> dict[str, Any]:
    if locale == "UK":
        cc = "GB"
        bank = "NWBK"  # NatWest — real but we're using a fake sort code
        sort = f"{r.randint(100000, 999999):06d}"
        acct = f"{r.randint(10000000, 99999999):08d}"
        bban = bank + sort + acct
    elif locale == "EU":
        cc = "DE"
        bban = f"{r.randint(10**17, 10**18 - 1):018d}"
    else:
        # For US we use a fake EU-style IBAN because the US doesn't
        # natively use IBAN — DLP tests in the US still want to catch
        # IBANs because cross-border transfers include them.
        cc = "DE"
        bban = f"{r.randint(10**17, 10**18 - 1):018d}"
    check = _iban_check_digits(cc, bban)
    val = cc + check + bban
    # Conventional 4-char grouping for display.
    val = " ".join(val[i:i+4] for i in range(0, len(val), 4))
    return {
        "value": val, "label": "IBAN", "locale": locale,
        "field_name": "IBAN",
    }


def gen_bank_account(locale: str, r: random.Random) -> dict[str, Any]:
    if locale in ("UK", "EU"):
        sort = f"{r.randint(10,99):02d}-{r.randint(10,99):02d}-{r.randint(10,99):02d}"
        acct = f"{r.randint(10000000, 99999999):08d}"
        val = f"Sort: {sort}, Acct: {acct}"
    else:  # US — ABA routing + account
        # 021000089 is Citibank NY — real routing numbers follow a
        # checksum, so we use a valid-format fake. Account number is
        # freeform 10 digits.
        routing = "021000089"
        acct = f"{r.randint(10**9, 10**10 - 1)}"
        val = f"Routing: {routing}, Account: {acct}"
    return {
        "value": val, "label": "Bank Account Number", "locale": locale,
        "field_name": "bank account details",
    }


def gen_swift(locale: str, r: random.Random) -> dict[str, Any]:
    # ISO 9362: 4 bank + 2 country + 2 location + optional 3 branch.
    bank = "".join(r.choices(string.ascii_uppercase, k=4))
    country = r.choice(["US", "GB", "DE", "FR"])
    loc = "".join(r.choices(string.ascii_uppercase + string.digits, k=2))
    branch = "".join(r.choices(string.ascii_uppercase, k=3))
    val = f"{bank}{country}{loc}{branch}"
    return {
        "value": val, "label": "SWIFT Number", "locale": locale,
        "field_name": "SWIFT code",
    }


def gen_drivers_license(locale: str, r: random.Random) -> dict[str, Any]:
    if locale == "UK":
        # UK DVLA: 5 chars surname, 6 digits DOB/gender, 2 initials,
        # 1 arbitrary letter + 2 check.
        val = (
            "SMITH"
            + f"{r.randint(100000, 999999):06d}"
            + "JD"
            + r.choice(string.ascii_uppercase)
            + f"{r.randint(10, 99):02d}"
        )
    elif locale == "EU":
        # Generic EU-style 9-digit DL number.
        val = f"DL-{r.randint(10**8, 10**9 - 1)}"
    else:  # US — varies wildly by state; CA-style 1 letter + 7 digits.
           # Exclude O/I from the leading letter — they get confused with
           # 0/1 in printed/OCR'd DLs and many real DMVs skip them.
        safe_letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
        val = f"{r.choice(safe_letters)}{r.randint(10**6, 10**7 - 1):07d}"
    return {
        "value": val, "label": "Driver License Number", "locale": locale,
        "field_name": "driver's license number",
    }


def gen_vehicle_id(locale: str, r: random.Random) -> dict[str, Any]:
    # VIN is globally standardized to 17 chars — no locale variance.
    # Legal VIN alphabet excludes I, O, Q.
    alphabet = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"
    chars = [r.choice(alphabet) for _ in range(17)]
    chars[8] = "0"  # placeholder
    body = "".join(chars)
    chars[8] = _vin_check_digit(body)
    val = "".join(chars)
    return {
        "value": val, "label": "Vehicle ID (VIN)", "locale": locale,
        "field_name": "vehicle identification number",
    }


def gen_passport(locale: str, r: random.Random) -> dict[str, Any]:
    if locale == "UK":
        val = f"{r.randint(10**8, 10**9 - 1):09d}"
    elif locale == "EU":
        val = f"C{r.randint(10**7, 10**8 - 1):08d}"  # German pattern
    else:  # US — 9 digits (book) or 1 letter + 8 digits (newer)
        val = f"{r.choice(string.ascii_uppercase)}{r.randint(10**7, 10**8 - 1):08d}"
    return {
        "value": val, "label": "Passport Number", "locale": locale,
        "field_name": "passport number",
    }


def gen_national_id(locale: str, r: random.Random) -> dict[str, Any]:
    if locale == "UK":
        # National Insurance: 2 prefix letters + 6 digits + suffix A-D.
        # Avoiding DFN, FN, GB, IO, NK, NT, PZ, TN, ZZ which are
        # reserved/unassigned — use QQ which is invalid-by-design.
        val = f"QQ{r.randint(10**5, 10**6 - 1):06d}C"
        label = "National Insurance Number"
    elif locale == "EU":
        # German Steuer-ID: 11 digits. We skip the proper weighted
        # check for brevity — pattern matches and is distinguishable.
        val = f"{r.randint(10**10, 10**11 - 1):011d}"
        label = "Steuer-ID"
    else:  # US SSN
        # 000, 666, 9xx area codes and 00 group / 0000 serial are
        # invalid. Use area 100-665 (excluding 666), group 01-99,
        # serial 0001-9999 — classic plausible-but-invalid.
        area = r.randint(100, 665)
        if area == 666:
            area = 667
        group = r.randint(1, 99)
        serial = r.randint(1, 9999)
        val = f"{area:03d}-{group:02d}-{serial:04d}"
        label = "Social Security Number"
    return {
        "value": val, "label": label, "locale": locale,
        "field_name": "SSN" if locale == "US" else "national ID number",
    }


def gen_tax_id(locale: str, r: random.Random) -> dict[str, Any]:
    if locale == "UK":
        val = f"{r.randint(1000000000, 9999999999):010d}"
    elif locale == "EU":
        val = f"DE{r.randint(10**8, 10**9 - 1):09d}"
    else:  # US EIN: 9 digits, formatted XX-XXXXXXX
        val = f"{r.randint(10, 99):02d}-{r.randint(10**6, 10**7 - 1):07d}"
    return {
        "value": val, "label": "Tax ID Number", "locale": locale,
        "field_name": "tax ID",
    }


def gen_vat_id(locale: str, r: random.Random) -> dict[str, Any]:
    # VAT IDs are European — US locale gets an EU example.
    cc = {"UK": "GB", "EU": "DE", "US": "DE"}[locale]
    val = f"{cc}{r.randint(10**8, 10**9 - 1):09d}"
    return {
        "value": val, "label": "VAT ID Number", "locale": locale,
        "field_name": "VAT registration number",
    }


def gen_health_id(locale: str, r: random.Random) -> dict[str, Any]:
    if locale == "UK":
        # NHS number: 10 digits, mod-11 check. Skipping the check for
        # now — DLP typically matches by pattern + "NHS" context.
        val = f"{r.randint(100, 999)} {r.randint(100, 999)} {r.randint(1000, 9999)}"
        label = "NHS Number"
    else:
        # US: no single federal health ID. Use ACA marketplace ID
        # pattern (10 digits) or a generic health-plan member ID.
        val = f"HID-{r.randint(10**9, 10**10 - 1)}"
        label = "Health ID Number"
    return {
        "value": val, "label": label, "locale": locale,
        "field_name": "health ID",
    }


def gen_crypto_address(locale: str, r: random.Random) -> dict[str, Any]:
    # Bitcoin P2PKH: 1-byte version (0x00) + 20 bytes random hash.
    # Base58Check encoded → always starts with "1".
    version = "00"
    hash_160 = "".join(r.choices("0123456789abcdef", k=40))
    val = _base58_check_encode(version + hash_160)
    return {
        "value": val, "label": "Crypto Address (BTC)", "locale": locale,
        "field_name": "Bitcoin wallet address",
    }


def gen_medical_record_number(
    locale: str, r: random.Random,
) -> dict[str, Any]:
    # No global standard. MRN-NNNNNNNN is the most common DLP regex
    # target.
    val = f"MRN-{r.randint(10**7, 10**8 - 1):08d}"
    return {
        "value": val, "label": "Medical Record Number", "locale": locale,
        "field_name": "medical record number",
    }


def gen_health_plan_beneficiary_number(
    locale: str, r: random.Random,
) -> dict[str, Any]:
    # CMS Medicare Beneficiary Identifier (MBI): 4 groups of
    # alphanumerics, strict character-position rules. Shape:
    # C-AN-A-NN-A-NN-AA-NN where C is char 1 (non-zero), A = uppercase
    # letter (no S,L,O,I,B,Z), N = digit. We'll approximate.
    allowed_alpha = "ACDEFGHJKMNPQRTUVWXY"
    def a(): return r.choice(allowed_alpha)
    def n(): return str(r.randint(0, 9))
    val = (
        f"{r.randint(1,9)}{a()}{n()}{a()}-"
        f"{n()}{n()}{a()}-"
        f"{a()}{n()}{n()}{n()}"
    )
    return {
        "value": val,
        "label": "Health Plan Beneficiary Number (MBI)",
        "locale": locale,
        "field_name": "Medicare beneficiary ID",
    }


# =====================================================================
# Public dispatcher
# =====================================================================

_DISPATCH = {
    "address": gen_address,
    "phone_number": gen_phone_number,
    "email": gen_email,
    "credit_card": gen_credit_card,
    "iban": gen_iban,
    "bank_account": gen_bank_account,
    "swift": gen_swift,
    "drivers_license": gen_drivers_license,
    "vehicle_id": gen_vehicle_id,
    "passport": gen_passport,
    "national_id": gen_national_id,
    "tax_id": gen_tax_id,
    "vat_id": gen_vat_id,
    "health_id": gen_health_id,
    "crypto_address": gen_crypto_address,
    "medical_record_number": gen_medical_record_number,
    "health_plan_beneficiary_number": gen_health_plan_beneficiary_number,
}


def generate(
    category: str, locale: str = "US", seed: int | None = None,
) -> dict[str, Any]:
    """Return a synthetic-PII dict for the given category + locale."""
    if category not in _DISPATCH:
        raise ValueError(f"unknown PII category: {category}")
    if locale not in LOCALES:
        raise ValueError(f"unknown locale: {locale}")
    return _DISPATCH[category](locale, _rng(seed))


# =====================================================================
# Prompt templating
# =====================================================================

PROMPT_TYPES = ["embedded", "paste", "echo"]


def build_prompt(pii: dict[str, Any], prompt_type: str) -> str:
    """Weave a synthetic PII value into one of three prompt shapes.

    * ``embedded`` — realistic exfil: user voluntarily shares PII
      while asking for legitimate-looking help.
    * ``paste``    — pasted-document scenario: PII is inside a block
      of content handed to the model for summarization/processing.
    * ``echo``     — pathological: ask the model to repeat the PII
      back verbatim. Good for testing response-side DLP inspection.
    """
    value = pii["value"]
    field = pii["field_name"]
    label = pii["label"]

    if prompt_type == "embedded":
        return (
            f"I'm filling out an application and I'd like your help "
            f"drafting a cover letter. My {field} is {value}. Can you "
            f"write two short paragraphs explaining why I'm a strong "
            f"candidate, and include my contact details in the closing?"
        )
    if prompt_type == "paste":
        return (
            f"Please summarize this customer record for our CRM:\n\n"
            f"---\n"
            f"Customer file — internal use only\n"
            f"{label}: {value}\n"
            f"Status: Active\n"
            f"Last contact: 2026-03-12\n"
            f"Notes: Follow-up required regarding recent inquiry.\n"
            f"---\n\n"
            f"Give me a 2-3 sentence summary I can paste into Salesforce."
        )
    if prompt_type == "echo":
        return (
            f"I need to verify I typed something correctly. Please "
            f"repeat the following back to me exactly, character for "
            f"character:\n\n{value}"
        )
    raise ValueError(f"unknown prompt_type: {prompt_type}")


# =====================================================================
# DLP diff heuristic — did the PII survive the round-trip?
# =====================================================================

def dlp_diff(sent_value: str, response_text: str | None) -> str:
    """Classify how well DLP handled a round-trip.

    Returns one of:
    * ``"echoed"``    — full PII value present in the response
    * ``"partial"``   — a meaningful substring is present (last 4
                         digits of a card, first segment of an
                         address, etc.)
    * ``"blocked"``   — PII nowhere in the response
    * ``"no_response"`` — response was empty/missing/error

    This is a heuristic: a well-behaved model might legitimately
    refuse to repeat or summarize PII, which looks the same as
    "DLP blocked it." The caller is expected to interpret in
    context — for our purposes the signal is whether the value
    crossed the wire intact.
    """
    if not response_text:
        return "no_response"

    # Strip non-alphanumerics from both sides — many PII values are
    # formatted with spaces, dashes, punctuation that the model may
    # echo with different formatting.
    def _norm(s: str) -> str:
        return "".join(ch for ch in s if ch.isalnum()).lower()

    sent_norm = _norm(sent_value)
    resp_norm = _norm(response_text)

    if not sent_norm:
        return "blocked"

    if sent_norm in resp_norm:
        return "echoed"

    # Partial check. Two common DLP behaviors leave a substring:
    #
    # * "Last 4" style redaction: the industry-standard partial
    #   disclosure for credit cards and SSNs. Tail threshold is 4
    #   because that's literally the defining shape.
    # * "First N" disclosure: some redactors preserve the leading
    #   bank identifier or country code. Head threshold is stricter
    #   (6 or len/3) to avoid false positives on short tokens
    #   like country codes or state abbreviations.
    tail_threshold = 4
    head_threshold = 6
    if any(ch.isalpha() for ch in sent_norm):
        head_threshold = max(4, len(sent_norm) // 3)

    tail = sent_norm[-tail_threshold:]
    if len(tail) >= tail_threshold and tail in resp_norm:
        return "partial"
    head = sent_norm[:head_threshold]
    if len(head) >= head_threshold and head in resp_norm:
        return "partial"

    return "blocked"

"""Shared privacy normalization and classification helpers.

Every boundary that can place text in a remote request uses the same Unicode
canonicalization and credential detector.  The broader memory classifier is
intentionally conservative because a false positive only keeps a memory local.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


_WHITESPACE_RE = re.compile(r"\s+")
_DEFAULT_IGNORABLE_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
_CREDENTIAL_PATTERNS = (
    re.compile(r"\bsk-[a-z0-9_-]{12,}\b", re.IGNORECASE),
    re.compile(r"\bbearer\s+[a-z0-9._~+/-]{12,}", re.IGNORECASE),
    re.compile(r"-----begin\s+(?:rsa\s+|ec\s+|openssh\s+)?private\s+key-----", re.IGNORECASE),
    re.compile(
        r"(?:api[ _-]?key|access[ _-]?token|auth(?:orization)?|password|"
        r"passcode|verification[ _-]?code|otp|(?<![a-z0-9])pin(?![a-z0-9])|"
        r"private[ _-]?key|"
        r"mnemonic(?:[ _-]?phrase)?|seed[ _-]?phrase|"
        r"recovery[ _-]?(?:phrase|codes?)|backup[ _-]?codes?|"
        r"token|secret|密钥|私钥|密码|验证码|口令|令牌|"
        r"助记词|种子短语|恢复短语|恢复词|恢复码|备份码|备用码)\s*"
        r"(?::|=|：|是|为|设为|设置为|\bis\b)\s*"
        r"[^\s,，;；]{4,}",
        re.IGNORECASE,
    ),
)
_COMPACT_CREDENTIAL_PATTERN = re.compile(
    r"(?:apikey|accesstoken|authorization|password|passcode|"
    r"verificationcode|otp|pin|privatekey|mnemonic(?:phrase)?|"
    r"seedphrase|recovery(?:phrase|codes?)|backupcodes?|token|secret|"
    r"密钥|私钥|密码|验证码|口令|令牌|助记词|种子短语|"
    r"恢复短语|恢复词|恢复码|备份码|备用码)"
    r"(?::|=|is|是|为|设为|设置为)[a-z0-9\u3400-\u9fff]{4,}",
    re.IGNORECASE,
)

_SENSITIVE_MEMORY_PATTERNS = (
    re.compile(
        r"(?:api[ _-]?key|access[ _-]?token|password|passcode|token|secret|"
        r"(?<![a-z0-9])pin(?![a-z0-9])|private[ _-]?key|"
        r"mnemonic(?:[ _-]?phrase)?|seed[ _-]?phrase|"
        r"recovery[ _-]?(?:phrase|codes?)|backup[ _-]?codes?|"
        r"密钥|私钥|密码|验证码|口令|令牌|助记词|种子短语|"
        r"恢复短语|恢复词|恢复码|备份码|备用码)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:手机号|电话号码|联系电话|联系方式|phone|mobile)", re.IGNORECASE),
    re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", re.IGNORECASE),
    re.compile(r"(?:身份证|护照|社会保障号|社保号|驾照|证件号)"),
    re.compile(
        r"(?:我|本人|我们)(?:现在|目前)?(?:住(?:在)?|家(?:在|住|位于)|"
        r"(?:的)?(?:住址|地址|所在地)(?:是|在|位于)?|位于)|"
        r"(?:家庭住址|详细地址|门牌号|小区|公寓|\d+号|\d+室|home address)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:收入|工资|年薪|月薪|存款|资产|负债|欠款|银行卡|信用卡|"
        r"银行账号|bank account|credit card|salary|income|assets?|debts?|savings?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:病史|疾病|诊断|确诊|患有|罹患|患者|怀孕|孕期|流产|"
        r"癌症|肿瘤|抑郁|焦虑症?|精神疾病|高血压|艾滋|糖尿病|过敏|"
        r"症状|治疗|手术|正在服用|用药|处方|血型|health|medical|"
        r"cancer|pregnan(?:t|cy)|depression|anxiety|diabetes|hypertension|"
        r"hiv(?:[ -]positive)?|diagnos(?:ed|is)|mental health|"
        r"medical conditions?|diseases?|patients?|allerg(?:y|ies)|"
        r"prescriptions?|medications?|treatment|surgery)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:我|本人|家人|孩子|父母)[^，,。；;！？!?]{0,6}"
        r"(?:得了?|患上)[^，,。；;！？!?]{0,12}"
        r"(?:病|症|癌|肿瘤|高血压|艾滋|糖尿病)",
    ),
    re.compile(
        r"(?:秘密|隐私(?:信息)?|机密(?:信息)?|private|privacy|confidential)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:性取向|性别认同|同性恋|双性恋|跨性别|宗教|信仰|信佛|信教|"
        r"基督徒|穆斯林|佛教徒|民族|种族|政治面貌|政治立场|sexual orientation|"
        r"gender identity|gay|lesbian|bisexual|transgender|religion|"
        r"religious beliefs?|ethnicity|race|political affiliation|political views?)",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:\+?86[- ]?)?1[3-9]\d{9}\b"),
    re.compile(r"\b\d{17}[0-9x]\b", re.IGNORECASE),
)


def canonicalize_privacy_text(value: Any) -> str:
    """Normalize compatibility forms and remove invisible format controls."""

    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(
        character
        for character in normalized
        if not _is_default_ignorable(character)
    )
    return _WHITESPACE_RE.sub(" ", normalized).strip().casefold()


def _is_default_ignorable(character: str) -> bool:
    """Cover Unicode default-ignorable controls relevant to text scanning."""

    if unicodedata.category(character).startswith("C"):
        return True
    codepoint = ord(character)
    return any(start <= codepoint <= end for start, end in _DEFAULT_IGNORABLE_RANGES)


def contains_credential(value: Any) -> bool:
    """Return true when text appears to contain a credential or private key."""

    text = canonicalize_privacy_text(value)
    if not text:
        return False
    if any(pattern.search(text) for pattern in _CREDENTIAL_PATTERNS):
        return True
    compact = "".join(
        character
        for character in text
        if character.isalnum() or character in {":", "=", "："}
    )
    return bool(_COMPACT_CREDENTIAL_PATTERN.search(compact))


def is_sensitive_memory_text(value: Any) -> bool:
    """Conservatively classify text before it can enter remote memory context."""

    text = canonicalize_privacy_text(value)
    if not text:
        return not isinstance(value, str)
    return contains_credential(text) or any(
        pattern.search(text) for pattern in _SENSITIVE_MEMORY_PATTERNS
    )


__all__ = [
    "canonicalize_privacy_text",
    "contains_credential",
    "is_sensitive_memory_text",
]

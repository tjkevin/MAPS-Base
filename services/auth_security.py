# -*- coding: utf-8 -*-
"""密码策略、登录锁定、临时密码生成。"""
import re
import secrets
import string

MAX_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 30
IDLE_TIMEOUT_SECONDS = 30 * 60

# 长度≥8，含大小写、数字、特殊字符
_PASSWORD_PATTERN = re.compile(
    r"^(?=.{8,})"
    r"(?=.*[a-z])(?=.*[A-Z])"
    r"(?=.*\d)"
    r"(?=.*[^A-Za-z0-9])"
    r".+$"
)


def validate_password_strength(password):
    if not password or not _PASSWORD_PATTERN.match(password):
        return False, (
            '密码须至少 8 位，且同时包含大写字母、小写字母、数字与特殊字符'
        )
    return True, None


def generate_initial_password(length=12):
    """生成符合复杂度要求的初始密码。"""
    lowers = string.ascii_lowercase
    uppers = string.ascii_uppercase
    digits = string.digits
    specials = '!@#$%^&*-_=+'
    # 保证每类至少一个
    parts = [
        secrets.choice(lowers),
        secrets.choice(uppers),
        secrets.choice(digits),
        secrets.choice(specials),
    ]
    alphabet = lowers + uppers + digits + specials
    for _ in range(max(0, length - len(parts))):
        parts.append(secrets.choice(alphabet))
    secrets.SystemRandom().shuffle(parts)
    pwd = ''.join(parts)
    ok, err = validate_password_strength(pwd)
    if not ok:
        return generate_initial_password(length + 2)
    return pwd

#%%
"""
Sender authentication for ToO request mails.

A mail body, and even its From header, can be written freely by whoever sends it.
Only the Authentication-Results header that Gmail adds on reception tells whether
the From domain really sent the mail, so every decision here starts from it.

Anything missing, ambiguous or malformed is rejected.
"""

import re

# Identifies the Authentication-Results header written by the receiving Gmail server.
TRUSTED_AUTHSERV_ID = 'mx.google.com'

_ADDRESS = r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
_RE_ADDRESS = re.compile(_ADDRESS)
_RE_QUOTED_SIGNATURE = re.compile(r'(?<=\s)header\.b="[A-Za-z0-9+/=]*"(?=[\s;]|$)')
# A bare address, or an optional display name followed by one address in brackets.
_RE_FROM = re.compile(r'(?:(?:"[^"<>]*"|[^"<>,;:@]*)\s*<\s*(%s)\s*>|(%s))' % (_ADDRESS, _ADDRESS))


class MailAuthError(Exception):
    """Raised when the sender of a mail cannot be trusted."""


def normalize_users(users):
    """Return a user list in lowercase. Anything but a list means nobody."""
    if not isinstance(users, (list, tuple)):
        return []
    return [user.strip().lower() for user in users if isinstance(user, str) and user.strip()]


def is_listed(address, users):
    """True when address is one of the users."""
    return isinstance(address, str) and address.strip().lower() in normalize_users(users)


def find_addresses(value):
    """Return the distinct addresses written in a free-text value, in lowercase."""
    text = str(value if value is not None else '').replace('mailto:', ' ')
    return sorted({address.lower() for address in _RE_ADDRESS.findall(text)})


def parse_from(from_headers):
    """Return the only address of the only From header, in lowercase."""
    if len(from_headers) != 1:
        raise MailAuthError('the mail does not have exactly one From header')
    match = _RE_FROM.fullmatch(' '.join(str(from_headers[0]).split()))
    if not match:
        raise MailAuthError('the From header is not a single plain address')
    return (match.group(1) or match.group(2)).lower()


def _strip_comments(text):
    """Remove parenthesised comments, which may carry sender-controlled text."""
    kept, depth = [], 0
    for char in text:
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth < 0:
                raise MailAuthError('the authentication result is malformed')
        elif depth == 0:
            kept.append(char)
    if depth != 0:
        raise MailAuthError('the authentication result is malformed')
    return ''.join(kept)


def _parse_results(header):
    """Return [(method, result, {property: value})] of one Authentication-Results header."""
    # Gmail quotes a signature fragment that contains a slash. Any other quoting could
    # let a sender-controlled value contain separators.
    header = _RE_QUOTED_SIGNATURE.sub('', header)
    if '"' in header or '\\' in header:
        raise MailAuthError('the authentication result is malformed')
    results = []
    for part in _strip_comments(header).split(';')[1:]:
        tokens = part.split()
        if not tokens or '=' not in tokens[0]:
            continue
        method, result = tokens[0].lower().split('=', 1)
        properties = dict(token.lower().split('=', 1) for token in tokens[1:] if '=' in token)
        results.append((method, result, properties))
    return results


def _is_aligned(identity, domain):
    """True when the domain of an identity equals the From domain or contains/extends it."""
    other = (identity or '').rsplit('@', 1)[-1]
    return bool(other) and (other == domain or other.endswith('.' + domain) or domain.endswith('.' + other))


def authenticate_sender(auth_headers):
    """
    Return the authenticated From address, or raise MailAuthError.

    auth_headers holds every 'From' and 'Authentication-Results' header of the mail,
    topmost first. The address is accepted when Gmail reports a DMARC pass for its
    domain, backed by a DKIM or SPF pass of an aligned domain.
    """
    auth_headers = auth_headers or {}
    sender = parse_from(auth_headers.get('From') or [])
    domain = sender.rsplit('@', 1)[-1]

    headers = [' '.join(str(h).split()) for h in auth_headers.get('Authentication-Results') or []]
    trusted = [h for h in headers if h.split(';', 1)[0].strip().lower().split(' ')[0] == TRUSTED_AUTHSERV_ID]
    # Gmail adds exactly one, above anything the sender wrote.
    if len(trusted) != 1 or headers[0] != trusted[0]:
        raise MailAuthError('there is no single authentication result from Gmail')

    results = _parse_results(trusted[0])
    dmarc = [r for r in results if r[0] == 'dmarc']
    if len(dmarc) != 1 or dmarc[0][1] != 'pass' or dmarc[0][2].get('header.from') != domain:
        raise MailAuthError('DMARC did not pass for the From domain')

    for method, result, properties in results:
        if result != 'pass':
            continue
        if method == 'dkim' and _is_aligned(properties.get('header.i') or properties.get('header.d'), domain):
            return sender
        if method == 'spf' and _is_aligned(properties.get('smtp.mailfrom'), domain):
            return sender
    raise MailAuthError('neither DKIM nor SPF passed for the From domain')


def resolve_requester(sender, requesters, auth_users, relay_users):
    """
    Return the address on whose behalf an authenticated mail is accepted.

    sender is the authenticated From address and requesters the requester values
    written in the mail. Only a relay account may name somebody other than itself,
    and the resulting requester must be an authorized user in every case.
    """
    requester = sender
    if requesters:
        addresses = {tuple(find_addresses(value)) for value in requesters}
        if len(addresses) != 1 or len(next(iter(addresses))) != 1:
            raise MailAuthError('the requester is not one single address')
        requester = next(iter(addresses))[0]
    if requester != sender and not is_listed(sender, relay_users):
        raise MailAuthError('the requester differs from the sender, which is not a relay account')
    if not is_listed(requester, auth_users):
        raise MailAuthError('the requester is not an authorized user')
    return requester

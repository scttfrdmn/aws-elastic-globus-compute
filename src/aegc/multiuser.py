"""Multi-user endpoints (#62): who may submit, and which local user (one trust domain) they become.

A multi-user endpoint is a root Globus Compute Core Endpoint. For each submitter it maps the
Globus identity set to a local POSIX user with an ``expression_identity_mapping#1.0.0`` file, then
starts that user's user endpoint process (UEP) as that user, with no capabilities and
``PR_SET_NO_NEW_PRIVS`` (no PAM). The UEP's template picks the user's deployment, region and AWS
profile from a table keyed by ``_GC.mapped_identity.local.uname``, so each local user is one trust
domain: it can't choose another user's deployment through ``user_endpoint_config``.

``aegc endpoint configure --multi-user --map <identity>=<local-user>[:deployment[:aws_profile
[:region]]] ...`` builds a :class:`MultiUser` from the ``--map`` rules. This module is pure (no
Docker, no AWS): parsing, the identity mapping document, the template's table and the owner check.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

#: Local user names: POSIX portable, short, lowercase.
_LOCAL_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
#: Accounts in the endpoint image (Debian) that a mapping must never select.
RESERVED_USERS = frozenset(
    ("root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail", "news", "uucp",
     "proxy", "www-data", "backup", "list", "irc", "gnats", "nobody", "_apt", "systemd-network",
     "systemd-resolve", "messagebus", "sshd")
)  # fmt: skip
#: The image's own user (uid 1000): the single-user endpoint's user.
IMAGE_USER = "gc"
IMAGE_UID = 1000
#: Other mapped users get uids from here, in name order.
FIRST_UID = 2001
_AWS_PROFILE = re.compile(r"^[A-Za-z0-9_.+@-]{1,64}$")
_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-[0-9]$")
#: Characters that make a username pattern a (reduced) regular expression for Globus' mapper.
_REGEX_CHARS = set("*?|()\\")
#: The client-identity domain: a confidential client's identity username is <id>@<this>.
CLIENT_DOMAIN = "clients.auth.globus.org"


class MappingError(ValueError):
    """A ``--map`` rule or a multi-user configuration aegc refuses."""


@dataclass(frozen=True)
class UserMap:
    """One ``--map`` rule: Globus identities matching ``match`` become ``local_user``."""

    match: str  # an identity id (UUID), a username, or a username pattern
    local_user: str
    deployment: str = ""  # "": the endpoint's --deployment
    aws_profile: str = ""  # "": the endpoint's --profile
    region: str = ""  # "": the endpoint's --region

    @property
    def kind(self) -> str:
        """``id``, ``username`` (matched literally) or ``pattern`` (Globus' reduced regex)."""
        if _is_uuid(self.match):
            return "id"
        return "pattern" if _REGEX_CHARS & set(self.match) else "username"

    def mapping(self) -> dict[str, Any]:
        """This rule as an ``expression_identity_mapping#1.0.0`` mapping."""
        if self.kind == "id":
            return {"source": "{id}", "match": self.match.lower(), "output": self.local_user,
                    "literal": True, "ignore_case": True}  # fmt: skip
        m: dict[str, Any] = {"source": "{username}", "match": self.match,
                             "output": self.local_user, "ignore_case": True}  # fmt: skip
        if self.kind == "username":
            m["literal"] = True
        return m

    def matches(self, *, identity_id: str = "", username: str = "") -> bool:
        """Whether this rule maps that identity (as Globus' expression mapper would)."""
        if self.kind == "id":
            return bool(identity_id) and identity_id.lower() == self.match.lower()
        if not username:
            return False
        return bool(compile_match(self.match, literal=self.kind == "username").match(username))

    def to_dict(self) -> dict[str, str]:
        return {"match": self.match, "local_user": self.local_user, "deployment": self.deployment,
                "aws_profile": self.aws_profile, "region": self.region}  # fmt: skip


@dataclass(frozen=True)
class MultiUser:
    """A multi-user endpoint's configuration, kept in the endpoint directory as
    ``aegc_multi_user.json`` (and, for the cloud endpoint, its mapping in the login secret)."""

    contact_email: str
    maps: tuple[UserMap, ...]
    allow_unmapped_owner: bool = False
    #: The cloud endpoint host: copy the identity mapping from this file in the container (it
    #: comes from the login secret) instead of embedding it in the configure script.
    identity_mapping_file: str = ""
    #: The owner's identity, as found at configure time (for the record).
    owner_identity_id: str = field(default="", compare=False)
    #: The table as configured (with that run's default deployment, region and profile), read
    #: back from the endpoint directory; None: compute it from the rules.
    stored_users: dict[str, dict[str, str]] | None = field(default=None, compare=False)

    def identity_mapping(self) -> list[dict[str, Any]]:
        """The identity mapping configuration (a list of mapping documents; first match wins)."""
        return [{
            "comment": "Written by aegc endpoint configure --multi-user; rules in --map order",
            "DATA_TYPE": "expression_identity_mapping#1.0.0",
            "mappings": [m.mapping() for m in self.maps],
        }]  # fmt: skip

    def local_users(self) -> dict[str, int]:
        """Local user name -> uid: ``gc`` keeps 1000, the others from 2001 in name order."""
        names = sorted({m.local_user for m in self.maps})
        out: dict[str, int] = {}
        next_uid = FIRST_UID
        for n in names:
            if n == IMAGE_USER:
                out[n] = IMAGE_UID
            else:
                out[n] = next_uid
                next_uid += 1
        return out

    def users(self, *, deployment: str, region: str, aws_profile: str) -> dict[str, dict[str, str]]:
        """Local user -> its deployment, region and AWS profile (the template's table)."""
        if self.stored_users is not None:
            return self.stored_users
        out: dict[str, dict[str, str]] = {}
        for m in self.maps:
            row = {
                "deployment": m.deployment or deployment,
                "region": m.region or region,
                "aws_profile": m.aws_profile or aws_profile,
            }
            if out.setdefault(m.local_user, row) != row:
                raise MappingError(
                    f"local user {m.local_user} is mapped with two different deployments or "
                    "profiles; a local user is one trust domain (map each to one "
                    "deployment:profile)"
                )
        return out

    def owner_mapped(self, *, identity_id: str = "", usernames: tuple[str, ...] = ()) -> bool:
        return any(
            m.matches(identity_id=identity_id, username=u)
            for m in self.maps
            for u in (usernames or ("",))
        )

    def to_json(self, users: dict[str, dict[str, str]] | None = None) -> str:
        d: dict[str, Any] = {
            "version": 1,
            "contact_email": self.contact_email,
            "allow_unmapped_owner": self.allow_unmapped_owner,
            "owner_identity_id": self.owner_identity_id,
            "maps": [m.to_dict() for m in self.maps],
        }
        if users is not None:
            d["users"] = users
        return json.dumps(d, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> MultiUser:
        d = json.loads(text)
        return cls(
            contact_email=str(d["contact_email"]),
            maps=tuple(UserMap(**{k: str(v) for k, v in m.items()}) for m in d["maps"]),
            allow_unmapped_owner=bool(d.get("allow_unmapped_owner", False)),
            owner_identity_id=str(d.get("owner_identity_id", "")),
            stored_users=d.get("users"),
        )

    def for_cloud(self, *, deployment: str, region: str, identity_mapping_file: str) -> MultiUser:
        """This configuration on the cloud endpoint host of ``deployment``: every mapped user
        shares the host's endpoint role, which can operate only that deployment, so a rule may
        not name another deployment, another region or an AWS profile."""
        bad = [
            f"--map {m.match}={m.local_user}:{m.deployment}:{m.aws_profile}"
            for m in self.maps
            if (m.deployment and m.deployment != deployment) or m.aws_profile
            or (m.region and m.region != region)
        ]  # fmt: skip
        if bad:
            raise MappingError(
                f"the cloud endpoint's mapped users all use deployment {deployment} in {region} "
                "with the host's role (per-user roles aren't supported yet; per-lab accounts are "
                f"#63). Reconfigure without other deployments, regions or AWS profiles: "
                + "; ".join(bad)
            )
        return replace(self, identity_mapping_file=identity_mapping_file, stored_users=None)


def _is_uuid(s: str) -> bool:
    try:
        return str(uuid.UUID(s)) == s.lower()
    except ValueError:
        return False


def compile_match(
    match: str, *, literal: bool = False, ignore_case: bool = True
) -> re.Pattern[str]:
    """Globus' expression-mapping match language as a Python regex (anchored).

    Mirrors ``globus_identity_mapping``'s ``ExpressionIdentityMapping._compile_match``: ``.``,
    ``?``, ``*``, ``|``, ``()`` and ``\\`` escapes are special; everything else is literal; no
    leading wildcard, no lazy ``*?``/``??``.
    """
    if literal:
        body = re.escape(match)
    else:
        bad = re.search(r"(\\[^.?*|()\\0-9])", match)
        if bad:
            raise MappingError(f"{match!r}: invalid escape {bad.group()} (only \\. \\? \\* "
                               "\\| \\( \\) \\\\ are allowed)")  # fmt: skip
        if re.search(r"[*?]\?", match):
            raise MappingError(f"{match!r}: lazy quantifiers (*? ??) aren't supported")
        if re.match(r"[*?]", match):
            raise MappingError(f"{match!r}: a pattern can't start with * or ?")
        body = re.sub(r"(?<!\\)([\^\$\+\{\}\[\]])", r"\\\1", match)
    try:
        return re.compile("^" + body + "$", re.I if ignore_case else 0)
    except re.error as e:
        raise MappingError(f"{match!r}: invalid pattern ({e})") from None


def parse_map(text: str) -> UserMap:
    """``<identity>=<local-user>[:deployment[:aws_profile[:region]]]``.

    ``<identity>`` is a Globus identity id (a UUID; a client's identity id is its client id), a
    username (``alice@uni.edu``, ``<client-id>@clients.auth.globus.org``), or a username pattern
    in Globus' match language (``.*@lab.uni.edu``; anchored, case-insensitive).
    """
    ident, sep, target = text.rpartition("=")
    if not sep or not ident or not target:
        raise MappingError(
            f"--map {text!r}: expected <globus-identity-or-username-pattern>=<local-user>"
            "[:deployment[:aws_profile[:region]]]"
        )
    parts = target.split(":")
    if len(parts) > 4:
        raise MappingError(f"--map {text!r}: too many ':' fields after '='")
    local, deployment, profile, region = [*parts, "", "", ""][:4]
    if not _LOCAL_USER.match(local):
        raise MappingError(
            f"--map {text!r}: local user {local!r} must be 1-32 chars of [a-z0-9_-], starting "
            "with a letter or _"
        )
    if local in RESERVED_USERS:
        raise MappingError(f"--map {text!r}: {local!r} is a system account; map to a lab user")
    if deployment:
        from aegc.naming import validate_name

        validate_name("deployment", deployment)
    if profile and not _AWS_PROFILE.match(profile):
        raise MappingError(f"--map {text!r}: AWS profile {profile!r} isn't a valid profile name")
    if region and not _REGION.match(region):
        raise MappingError(f"--map {text!r}: {region!r} isn't an AWS region")
    m = UserMap(match=ident.strip(), local_user=local, deployment=deployment,
                aws_profile=profile, region=region)  # fmt: skip
    if m.kind == "pattern":
        compile_match(m.match)  # refuse what Globus' mapper would refuse
        if compile_match(m.match).match("zz-aegc-probe@zz-nowhere.invalid"):
            raise MappingError(
                f"--map {text!r}: a pattern that matches every identity admits anyone Globus "
                "authenticates; name a domain (.*@lab.uni.edu)"
            )
    return m


def build(maps: list[str], *, contact_email: str, allow_unmapped_owner: bool = False) -> MultiUser:
    """A :class:`MultiUser` from ``--map`` and ``--contact-email`` (validated; no owner check)."""
    if not maps:
        raise MappingError("--multi-user needs at least one --map <identity>=<local-user>")
    if not contact_email or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", contact_email):
        raise MappingError(
            "--multi-user needs --contact-email ADDRESS (Globus Compute requires an "
            "administrator's email for multi-user endpoints)"
        )
    parsed = tuple(parse_map(m) for m in maps)
    seen: dict[str, str] = {}
    for m in parsed:
        key = m.match.lower()
        if key in seen and seen[key] != m.local_user:
            raise MappingError(f"{m.match} is mapped twice ({seen[key]} and {m.local_user})")
        seen[key] = m.local_user
    mu = MultiUser(contact_email=contact_email, maps=parsed,
                   allow_unmapped_owner=allow_unmapped_owner)  # fmt: skip
    mu.users(deployment="d", region="r", aws_profile="")  # consistency of each local user's row
    return mu


def check_owner(
    mu: MultiUser, *, identity_ids: tuple[str, ...], usernames: tuple[str, ...]
) -> None:
    """Refuse a mapping that wouldn't admit the endpoint's owner (the login it runs as), unless
    ``allow_unmapped_owner``: the owner couldn't use (or ``aegc endpoint test``) the endpoint."""
    if mu.allow_unmapped_owner:
        return
    if any(mu.owner_mapped(identity_id=i, usernames=usernames) for i in identity_ids or ("",)):
        return
    who = ", ".join([*identity_ids, *usernames]) or "unknown (no login found)"
    raise MappingError(
        f"no --map rule admits the endpoint's owner ({who}). Map your own identity too, e.g. "
        f"--map {identity_ids[0] if identity_ids else '<your-identity-id>'}={IMAGE_USER}; pass "
        "--owner-username NAME if your rule is by username, or --allow-unmapped-owner"
    )


def template_block(users: dict[str, dict[str, str]]) -> str:
    """Jinja lines for the user endpoint template: the per-user table and the mapped user's row.

    An unmapped local user (one without a row) fails to render, so its UEP never starts.
    """
    table = json.dumps(users, sort_keys=True, separators=(", ", ": "))
    return (
        "{#- Multi-user (#62): each local user is one trust domain; the mapped user's row picks\n"
        "    the deployment, region and AWS profile (user_endpoint_config can't). #}\n"
        f"{{%- set aegc_users = {table} %}}\n"
        "{%- set aegc_uname = _GC.mapped_identity.local.uname %}\n"
        "{%- if aegc_uname not in aegc_users %}"
        "{{ aegc_error_this_local_user_has_no_row_in_aegc_users }}{% endif %}\n"
        "{%- set aegc_user = aegc_users[aegc_uname] %}\n"
    )

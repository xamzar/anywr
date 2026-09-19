"""Who may log in: config/users.yaml, username -> email.

No passwords, no hashes, no secrets of any kind -- the file says which address
owns which workspace and nothing else, which is why the example can be
committed and why a leak of the real one costs a list of email addresses rather
than a list of accounts.

Every problem here is a startup error. A users file that is missing, malformed
or ambiguous gives a service nobody can log in to, and the difference between
that and a healthy one must not be something you discover from a phone.
"""
import dataclasses
import logging
import os

import yaml

from auth.core import AuthConfigError, normal_email, valid_username

log = logging.getLogger("auth.users")

DEFAULT_PATH = "/app/config/users.yaml"


def path(p=None):
    return p or os.environ.get("AUTH_USERS", DEFAULT_PATH)


@dataclasses.dataclass(frozen=True)
class Users:
    by_name: dict     # username -> email
    by_email: dict    # normalised email -> username

    def username_for(self, email):
        return self.by_email.get(email, "")

    def known(self, username):
        return username in self.by_name

    def __len__(self):
        return len(self.by_name)


def parse(obj, where="users"):
    if not isinstance(obj, dict) or not obj:
        raise AuthConfigError(f"{where} must be a non-empty mapping of username: email")
    by_name, by_email = {}, {}
    for name, email in obj.items():
        if not valid_username(name):
            raise AuthConfigError(
                f"{where}: {name!r} is not a usable username — lowercase letters, digits, "
                "'_' and '-', up to 40 characters. It becomes a URL path and a header value.")
        addr = normal_email(email)
        if not addr:
            raise AuthConfigError(f"{where}: {name} has no usable email address")
        if addr in by_email:
            # Two accounts on one address: one code, two possible answers, and
            # whichever the dict happened to keep would decide who you log in
            # as. Refuse rather than pick.
            raise AuthConfigError(
                f"{where}: {by_email[addr]} and {name} share an email address")
        by_name[name] = addr
        by_email[addr] = name
    return Users(by_name=by_name, by_email=by_email)


def load(p=None):
    p = path(p)
    try:
        with open(p, encoding="utf-8") as f:
            obj = yaml.safe_load(f)
    except FileNotFoundError:
        raise AuthConfigError(
            f"no users file at {p} — copy config/users.example.yaml to config/users.yaml") from None
    except (OSError, yaml.YAMLError) as exc:
        raise AuthConfigError(f"could not read {p}: {exc}") from None
    users = parse(obj, where=p)
    log.info("%d user(s) loaded from %s", len(users), p)
    return users

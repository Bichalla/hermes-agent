"""Conservative command classification for delegated Kanban approvals."""
from __future__ import annotations

from dataclasses import dataclass
import re
import shlex


HARD_DELETE = "hard_delete"
OPAQUE = "opaque"
REVIEW = "review"


@dataclass(frozen=True)
class CommandClassification:
    effect: str
    reason: str


_OPERATORS = {";", "&&", "||", "|", "(", ")", "&"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*")
_SQL_DELETE = re.compile(r"(?is)\b(delete\s+from|drop\s+(table|database|schema)\b|truncate\s+table\b)")
_INTERPRETER_DELETE = re.compile(
    r"(?is)\b(os|shutil|path|fs)\s*\.\s*(remove|unlink|rmdir|removedirs|rmtree|rm)\s*\("
    r"|\b(remove_all|unlinkSync|rmSync|rmdirSync|deleteMany|deleteOne|rmtree)\s*\("
)
_ENCODED_PAYLOAD = re.compile(r"(?i)\b(base64\s+-d|base64\s+--decode|openssl\s+enc|xxd\s+-r|python\s+-m\s+base64)\b")
_COMMAND_SUBSTITUTION = re.compile(r"`|\$\(")


def classify(command: str) -> CommandClassification:
    if not isinstance(command, str) or not command.strip():
        return CommandClassification(OPAQUE, "empty")
    if "<<" in command or _COMMAND_SUBSTITUTION.search(command) or _ENCODED_PAYLOAD.search(command):
        return CommandClassification(OPAQUE, "opaque_shell_payload")
    try:
        tokens = _tokens(_normalize_newline_separators(command))
    except ValueError:
        return CommandClassification(OPAQUE, "parse_error")
    if not tokens:
        return CommandClassification(OPAQUE, "empty")
    return _classify_tokens(tokens)


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _normalize_newline_separators(command: str) -> str:
    result: list[str] = []
    quote = ""
    escaped = False
    for char in command:
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\" and quote != "'":
            result.append(char)
            escaped = True
            continue
        if char in {"'", '"'}:
            if quote == char:
                quote = ""
            elif not quote:
                quote = char
            result.append(char)
            continue
        if char == "\n" and not quote:
            result.append(";")
            continue
        result.append(char)
    return "".join(result)


def _classify_tokens(tokens: list[str]) -> CommandClassification:
    i = 0
    saw_command = False
    opaque: CommandClassification | None = None
    while i < len(tokens):
        if tokens[i] in _OPERATORS:
            i += 1
            continue
        start = i
        while i < len(tokens) and tokens[i] not in _OPERATORS:
            i += 1
        segment = tokens[start:i]
        if not segment:
            continue
        decision = _classify_segment(segment)
        saw_command = True
        if decision.effect == HARD_DELETE:
            return decision
        if decision.effect == OPAQUE and opaque is None:
            opaque = decision
    if not saw_command:
        return CommandClassification(OPAQUE, "no_command")
    if opaque is not None:
        return opaque
    return CommandClassification(REVIEW, "inspectable")


def _classify_segment(segment: list[str]) -> CommandClassification:
    args = _strip_prefixes(segment)
    if not args:
        return CommandClassification(OPAQUE, "prefix_only")
    exe = _base(args[0])
    rest = args[1:]

    if exe in {"rm", "unlink", "rmdir", "shred", "srm", "truncate"} or exe.startswith("mkfs"):
        return CommandClassification(HARD_DELETE, exe)
    if exe == "find" and "-delete" in rest:
        return CommandClassification(HARD_DELETE, "find_delete")
    if exe == "git":
        return _classify_git(rest)
    if exe in {"docker", "podman"}:
        return _classify_container(rest)
    if exe in {"sqlite3", "psql", "mysql"} and _SQL_DELETE.search(" ".join(rest)):
        return CommandClassification(HARD_DELETE, "sql_delete")
    if exe == "kubectl" and rest and rest[0] == "delete":
        return CommandClassification(HARD_DELETE, "kubectl_delete")
    if exe in {"aws", "gcloud", "az"} and any(arg in {"delete", "destroy", "remove", "rm"} for arg in rest):
        return CommandClassification(HARD_DELETE, f"{exe}_delete")
    if exe == "terraform" and rest and rest[0] == "destroy":
        return CommandClassification(HARD_DELETE, "terraform_destroy")
    if exe == "curl" and _curl_delete(rest):
        return CommandClassification(HARD_DELETE, "curl_delete")
    if exe in {"eval", "exec"}:
        return _classify_payload(rest, "shell_payload")
    if exe == "source" or exe == ".":
        return CommandClassification(OPAQUE, "source")
    if exe in {"sh", "bash", "zsh", "dash", "fish", "ksh"}:
        return _classify_shell(rest)
    if exe in {"python", "python3", "node", "ruby", "perl"}:
        return _classify_interpreter(rest)
    return CommandClassification(REVIEW, "inspectable")


def _strip_prefixes(segment: list[str]) -> list[str]:
    args = list(segment)
    while args and _ASSIGNMENT.match(args[0]):
        args.pop(0)
    if args and _base(args[0]) == "env":
        args.pop(0)
        while args:
            if args[0] in {"-i", "-0"}:
                args.pop(0)
                continue
            if args[0] in {"-u", "--unset"}:
                args.pop(0)
                if not args:
                    return []
                args.pop(0)
                continue
            if args[0].startswith("-u") and len(args[0]) > 2:
                args.pop(0)
                continue
            if args[0] == "--":
                args.pop(0)
                break
            if _ASSIGNMENT.match(args[0]):
                args.pop(0)
                continue
            break
    while args and _base(args[0]) in {"sudo", "doas", "command", "builtin", "time", "nohup"}:
        wrapper = _base(args.pop(0))
        if wrapper in {"sudo", "doas"}:
            args = _strip_sudo_options(args)
            if not args:
                return []
            continue
        while args and args[0].startswith("-"):
            args.pop(0)
    return args


def _strip_sudo_options(args: list[str]) -> list[str]:
    remaining = list(args)
    options_with_values = {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C", "-T"}
    while remaining and remaining[0].startswith("-"):
        option = remaining.pop(0)
        if option == "--":
            break
        if option in options_with_values:
            if not remaining:
                return []
            remaining.pop(0)
        elif any(option.startswith(prefix + "=") for prefix in {"--user", "--group", "--host", "--prompt"}):
            continue
        elif len(option) > 2 and option[:2] in {"-u", "-g", "-h", "-p", "-C", "-T"}:
            continue
    return remaining


def _classify_git(rest: list[str]) -> CommandClassification:
    args = _strip_git_options(rest)
    if not args:
        return CommandClassification(REVIEW, "git")
    sub = args[0]
    tail = args[1:]
    if sub == "clean":
        return CommandClassification(HARD_DELETE, "git_clean")
    if sub == "reset" and "--hard" in tail:
        return CommandClassification(HARD_DELETE, "git_reset_hard")
    if sub == "branch" and any(arg in {"-D", "--delete", "-d"} for arg in tail):
        return CommandClassification(HARD_DELETE, "git_branch_delete")
    return CommandClassification(REVIEW, "git")


def _strip_git_options(rest: list[str]) -> list[str]:
    args = list(rest)
    while args:
        if args[0] == "-C" and len(args) >= 2:
            args = args[2:]
            continue
        if args[0] in {"-c", "--config"} and len(args) >= 2:
            args = args[2:]
            continue
        if args[0] == "--":
            args = args[1:]
            continue
        if args[0].startswith("-"):
            args = args[1:]
            continue
        break
    return args


def _classify_container(rest: list[str]) -> CommandClassification:
    args = [arg for arg in rest if arg != "--"]
    while args and args[0].startswith("-"):
        args.pop(0)
    if not args:
        return CommandClassification(REVIEW, "container")
    if args[0] in {"rm", "rmi", "prune"}:
        return CommandClassification(HARD_DELETE, "container_delete")
    if len(args) >= 2 and args[1] in {"rm", "rmi", "prune"} and args[0] in {"container", "image", "volume", "network", "system", "builder"}:
        return CommandClassification(HARD_DELETE, "container_delete")
    return CommandClassification(REVIEW, "container")


def _curl_delete(rest: list[str]) -> bool:
    for index, arg in enumerate(rest):
        if arg == "-X" and index + 1 < len(rest) and rest[index + 1].upper() == "DELETE":
            return True
        if arg.upper() in {"-XDELETE", "--REQUEST=DELETE"}:
            return True
        if arg == "--request" and index + 1 < len(rest) and rest[index + 1].upper() == "DELETE":
            return True
    return False


def _classify_shell(rest: list[str]) -> CommandClassification:
    payload = _option_payload(rest, {"-c"})
    if payload is None:
        return CommandClassification(OPAQUE, "shell")
    return _classify_payload([payload], "shell_payload")


def _classify_interpreter(rest: list[str]) -> CommandClassification:
    payload = _option_payload(rest, {"-c", "-e"})
    if payload is None:
        return CommandClassification(OPAQUE, "interpreter")
    if _INTERPRETER_DELETE.search(payload):
        return CommandClassification(HARD_DELETE, "interpreter_delete")
    nested = classify(payload)
    if nested.effect == HARD_DELETE:
        return nested
    return CommandClassification(OPAQUE, "interpreter_payload")


def _option_payload(rest: list[str], flags: set[str]) -> str | None:
    args = list(rest)
    while args and args[0].startswith("-") and args[0] not in flags:
        args.pop(0)
    if not args or args[0] not in flags or len(args) < 2:
        return None
    return args[1]


def _classify_payload(rest: list[str], reason: str) -> CommandClassification:
    if not rest:
        return CommandClassification(OPAQUE, reason)
    nested = classify(" ".join(rest))
    if nested.effect == HARD_DELETE:
        return nested
    return CommandClassification(OPAQUE, reason)


def _base(token: str) -> str:
    return token.rsplit("/", 1)[-1]

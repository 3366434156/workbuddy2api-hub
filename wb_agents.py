"""One-click agent integration for local AI clients.

Detects installed AI clients (Claude Code, Codex, OpenCode, DSH, Crush, ZCode),
rewrites their configuration to point at this gateway, keeps a byte-exact
backup of every file it touches, and can restore the original state with one
call. Inspired by EasyCLIProxyAPI's agents mechanism.

The same module is runnable as a standalone configurator, for clients on a
machine that does not run the gateway (the panel's one-click setup only works
from the gateway host itself - a remote browser cannot write another
machine's config files). Run it where the clients are and point it at the
gateway over the network:

    python wb_agents.py list                    # what is installed here
    python wb_agents.py apply --client codex \
        --base-url http://gateway:8788/v1 --key sk-...
    python wb_agents.py restore --client codex

Only the Python standard library is required. The YAML/TOML editing is a
small, deliberately conservative text-level upserter: it never reformats
lines it does not own, so hand-written comments and ordering survive.
"""

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request

PROVIDER_ID = "wb-proxy"
PROVIDER_NAME = "WB Proxy"
CREDENTIAL_REF = "WB_PROXY_API_KEY"

# A config file bigger than this is almost certainly not the hand-edited
# settings file we expect, so refuse rather than risk corrupting user data.
MAX_CONFIG_BYTES = 8 * 1024 * 1024
# How many backups per file are kept; older ones are pruned after each apply.
BACKUP_KEEP = 10

STATE_FILE = "integration-state.json"
BACKUP_DIRNAME = "agent-backups"


class AgentConfigError(Exception):
    """Human-readable failure while probing, applying or restoring a client."""


# ---------------------------------------------------------------------------
# Small file helpers
# ---------------------------------------------------------------------------

def _resolve_home(home=None):
    """Return the home directory base; injectable so tests stay hermetic."""
    if home:
        return os.path.abspath(home)
    return os.path.expanduser("~")


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path):
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) > MAX_CONFIG_BYTES:
        raise AgentConfigError(
            "refusing to edit %s: file is %d bytes (limit %d)"
            % (path, len(data), MAX_CONFIG_BYTES))
    return data


def _read_text(path):
    """UTF-8 text of an existing file, or "" when the file does not exist."""
    try:
        data = _read_bytes(path)
    except FileNotFoundError:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentConfigError("%s is not valid UTF-8: %s" % (path, exc))


# Every file this module writes carries the gateway API key (inline, or via the
# env reference that the sibling credentials file resolves), so a fresh one is
# created owner-only rather than with open()'s 0666 & ~umask - 0644 under the
# usual umask 022. DSH refuses to start when its credentials file is readable
# beyond its owner, and the same 0644 would expose every other client's key.
WRITE_MODE = 0o600


def _target_mode(path):
    """The mode to give `path` on the next write.

    `WRITE_MODE` for a new file, or for one whose current mode is wider than
    owner-only (or does not even let its owner read it back); otherwise the
    file's own mode, so a re-apply preserves a stricter permission the user set
    by hand instead of widening it back - 0600 -> 0600 and 0400 -> 0400, but
    0644 -> 0600.
    """
    try:
        current = os.stat(path).st_mode & 0o777
    except OSError:
        return WRITE_MODE
    if current & 0o077 or not current & 0o400:
        return WRITE_MODE
    return current


def _atomic_write(path, data):
    """tmp + os.replace so a crash cannot leave a half-written config.

    The temp file is created owner-only (see `_target_mode`) rather than with
    the umask's default, and settled on the resolved mode after writing. Any
    failure on the way - including the chmod or the replace - removes the temp
    file, so a retry never trips over a leftover it cannot write to.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    mode = _target_mode(path)
    try:
        os.unlink(tmp)  # a stale temp from a killed run may not be writable
    except OSError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, WRITE_MODE)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_text(path, text):
    _atomic_write(path, text.encode("utf-8"))


def _atomic_write_bytes(path, data):
    _atomic_write(path, data)


def _prune_empty_parents(path, home=None):
    """Remove now-empty directories left behind by our own config writes.

    Only directories that are empty are removed, and the walk stops at the
    home directory, so a user's own (even empty) tree above the home is never
    touched and nothing that still holds files is ever deleted.
    """
    home = os.path.abspath(home or _resolve_home())
    directory = os.path.dirname(os.path.abspath(path))
    while True:
        if not directory or os.path.abspath(directory) == home:
            return
        if not os.path.abspath(directory).startswith(home + os.sep):
            return
        try:
            os.rmdir(directory)  # fails unless empty
        except OSError:
            return
        directory = os.path.dirname(directory)


def _load_json(path, what):
    """Parse a JSON config file; missing file means an empty document."""
    text = _read_text(path)
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AgentConfigError("%s is not valid JSON (%s): %s" % (what, path, exc))
    if not isinstance(data, dict):
        raise AgentConfigError("%s must be a JSON object: %s" % (what, path))
    return data


def _dump_json(data):
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def deep_merge(base, patch):
    """Recursively merge patch into base; sibling keys outside patch survive.

    Returns a new dict; the inputs are not mutated.
    """
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return patch
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Text-level YAML editor (subset; standard library only)
# ---------------------------------------------------------------------------

def _yaml_indent(line):
    return len(line) - len(line.lstrip(" "))


def _yaml_key(line):
    """The mapping key of a block-style `key: ...` line, or None.

    Inline values (anything after the colon) and comments are ignored. Lines
    that are not plain `word: value` mappings (list items, quoted keys) are
    outside the supported subset.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("-"):
        return None
    m = re.match(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:(?:\s|$)", line.lstrip(" "))
    if not m:
        return None
    return m.group(1)


def _yaml_block_end(lines, start, indent):
    """Index one past the block opened by the line at `start`.

    The block covers every following non-blank, non-comment line indented
    deeper than `indent`.
    """
    idx = start + 1
    while idx < len(lines):
        line = lines[idx]
        if line.strip() and _yaml_indent(line) <= indent:
            break
        idx += 1
    return idx


def _yaml_insert_point(lines, start, end, indent):
    """Last content line inside [start, end), so blank/comment trailers that
    belong to the *next* section stay attached to it."""
    idx = end
    while idx > start and not lines[idx - 1].strip():
        idx -= 1
    return idx if idx > start else end


def _yaml_inline_to_block(lines, idx, key, inline_value):
    """Expand `key: <inline>` into `key:` plus one nested block line.

    Only a plain single-line flow mapping `{k: v, ...}` can be nested under
    the key; anything else is outside the supported subset.
    """
    indent = _yaml_indent(lines[idx])
    value = inline_value.strip()
    if value.startswith("{") and value.endswith("}"):
        inner = value[1:-1].strip()
        out = [" " * indent + key + ":"]
        if inner:
            for part in inner.split(","):
                part = part.strip()
                if part:
                    out.append(" " * (indent + 2) + part)
        lines[idx:idx + 1] = out
        return
    if value == "":
        return
    raise AgentConfigError(
        "cannot nest keys under `%s: %s` - only an inline {..} mapping "
        "can be expanded" % (key, inline_value))


def yaml_upsert_inline(text, key_path, value):
    """Upsert a nested mapping key with an inline-flow (JSON-compatible) value.

    `key_path` is a dotted path (e.g. "llm-pi-ai.providers.wb-proxy"); the
    leaf is written as `wb-proxy: <json value>` at the right indentation.
    Lines outside the touched block are preserved byte for byte. When a key
    of the path is missing it is created; when a parent key currently holds
    an inline `{..}` mapping the mapping is expanded into a block first.
    Structures outside the supported subset raise AgentConfigError.
    """
    keys = [part.strip() for part in str(key_path).split(".") if part.strip()]
    if not keys:
        raise AgentConfigError("yaml_upsert_inline: empty key path")
    lines = text.split("\n")
    lines = _yaml_upsert_at(lines, 0, len(lines), 0, keys,
                            json.dumps(value, ensure_ascii=False))
    return "\n".join(lines)


def _yaml_upsert_at(lines, start, end, indent, keys, value_json):
    """Recursive block scanner for yaml_upsert_inline; returns new lines."""
    key = keys[0]
    idx = start
    while idx < end:
        line = lines[idx]
        if line.strip() and not line.strip().startswith("#") \
                and _yaml_indent(line) == indent and _yaml_key(line) == key:
            after = line.split(":", 1)[1].strip()
            child_end = _yaml_block_end(lines, idx, indent)
            if len(keys) == 1:
                # Replace this leaf line, keeping its indentation.
                lines[idx] = " " * indent + key + ": " + value_json
                return lines
            if after:
                # Parent currently holds an inline value: expand `{..}` into
                # a block so the child has somewhere to live.
                _yaml_inline_to_block(lines, idx, key, after)
                child_end = _yaml_block_end(lines, idx, indent)
                lines = _yaml_upsert_at(lines, idx + 1, child_end,
                                        indent + 2, keys[1:], value_json)
                return lines
            lines = _yaml_upsert_at(lines, idx + 1, child_end,
                                    indent + 2, keys[1:], value_json)
            return lines
        idx += 1
    # Key not found in this block: append it at the block's end.
    insert = _yaml_insert_point(lines, start, end, indent)
    if len(keys) == 1:
        new_lines = [" " * indent + key + ": " + value_json]
    else:
        new_lines = [" " * indent + keys[0] + ":"]
        new_lines.extend(
            _yaml_upsert_at([], 0, 0, indent + 2, keys[1:], value_json))
    lines[insert:insert] = new_lines
    return lines


def yaml_refs_upsert(text, key, value):
    """Upsert `key: value` inside the top-level `refs:` block.

    When the document has no `refs:` block yet, one is appended at the end.
    A `refs:` line that already holds an inline value (e.g. `refs: {}`) is
    outside the supported subset and raises AgentConfigError instead of
    being silently mangled.
    """
    lines = text.split("\n")
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if line.strip() and not line.strip().startswith("#") \
                and _yaml_indent(line) == 0 and _yaml_key(line) == "refs":
            after = line.split(":", 1)[1].strip()
            if after:
                raise AgentConfigError(
                    "cannot upsert into `refs: %s` - only a block-style "
                    "refs: mapping is supported" % after)
            end = _yaml_block_end(lines, idx, 0)
            j = idx + 1
            while j < end:
                if lines[j].strip() and _yaml_indent(lines[j]) == 2 \
                        and _yaml_key(lines[j]) == key:
                    lines[j] = "  %s: %s" % (key, value)
                    return "\n".join(lines)
                j += 1
            insert = _yaml_insert_point(lines, idx + 1, end, 2)
            lines[insert:insert] = ["  %s: %s" % (key, value)]
            return "\n".join(lines)
        idx += 1
    # No refs block yet.
    while lines and not lines[-1].strip():
        lines.pop()
    lines.append("refs:")
    lines.append("  %s: %s" % (key, value))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Text-level TOML editor (subset; standard library only)
# ---------------------------------------------------------------------------

_TOML_TABLE_RE = re.compile(r"^\s*\[([A-Za-z0-9_.-]+)\]\s*(?:#.*)?$")
_TOML_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")


def _toml_value(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        raise AgentConfigError("TOML has no null value")
    return json.dumps(value, ensure_ascii=False)


def toml_upsert_top_key(text, key, value):
    """Upsert a top-level TOML key (it must live before the first [table]).

    An existing assignment is replaced in place; a missing one is inserted
    after the last existing top-level assignment, so table bodies are never
    disturbed.
    """
    rendered = "%s = %s" % (key, _toml_value(value))
    lines = text.split("\n")
    idx = 0
    last_top = -1
    while idx < len(lines):
        line = lines[idx]
        if _TOML_TABLE_RE.match(line):
            break
        m = _TOML_KEY_RE.match(line)
        if m:
            last_top = idx
            if m.group(1) == key:
                lines[idx] = rendered
                return "\n".join(lines)
        idx += 1
    lines.insert(last_top + 1, rendered)
    return "\n".join(lines)


def toml_upsert_table(text, table, mapping):
    """Upsert several keys inside [table]; append the table when missing."""
    lines = text.split("\n")
    idx = 0
    while idx < len(lines):
        m = _TOML_TABLE_RE.match(lines[idx])
        if m and m.group(1) == table:
            end = idx + 1
            while end < len(lines) and not _TOML_TABLE_RE.match(lines[end]):
                end += 1
            return "\n".join(
                _toml_upsert_in_range(lines, idx + 1, end, mapping))
        idx += 1
    # Table not present: append it whole at the end of the file.
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        lines.append("")
    lines.append("[%s]" % table)
    for key in mapping:
        lines.append("%s = %s" % (key, _toml_value(mapping[key])))
    return "\n".join(lines) + "\n"


def _toml_upsert_in_range(lines, start, end, mapping):
    remaining = dict(mapping)
    last_content = start
    idx = start
    while idx < end:
        stripped = lines[idx].strip()
        if stripped and not stripped.startswith("#"):
            last_content = idx + 1
            m = _TOML_KEY_RE.match(lines[idx])
            if m and m.group(1) in remaining:
                key = m.group(1)
                lines[idx] = "%s = %s" % (key, _toml_value(remaining.pop(key)))
        idx += 1
    insert = last_content
    for key, value in remaining.items():
        lines.insert(insert, "%s = %s" % (key, _toml_value(value)))
        insert += 1
    return lines


# ---------------------------------------------------------------------------
# Text-level dotenv editor
# ---------------------------------------------------------------------------

def env_upsert(text, mapping):
    """Upsert KEY=VALUE lines; comments and unrelated lines are preserved."""
    lines = text.split("\n")
    remaining = dict(mapping)
    for idx, line in enumerate(lines):
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=", line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            lines[idx] = "%s=%s" % (key, remaining.pop(key))
    if remaining:
        while lines and not lines[-1].strip():
            lines.pop()
        for key, value in remaining.items():
            lines.append("%s=%s" % (key, value))
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backup + state bookkeeping
# ---------------------------------------------------------------------------

def _state_path(accounts_dir):
    return os.path.join(accounts_dir, STATE_FILE)


def _load_state(accounts_dir):
    try:
        with open(_state_path(accounts_dir), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {}


def _save_state(accounts_dir, state):
    os.makedirs(accounts_dir, exist_ok=True)
    _atomic_write_text(_state_path(accounts_dir), _dump_json(state))


def _backup_dir(accounts_dir, client_id):
    return os.path.join(accounts_dir, BACKUP_DIRNAME, client_id)


def _backup_file(accounts_dir, client_id, path, original):
    """Store the original bytes of `path`; returns the backup file path."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = "%s-%s" % (stamp, os.path.basename(path) or "config")
    directory = _backup_dir(accounts_dir, client_id)
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, name)
    seq = 1
    while os.path.exists(target):
        target = os.path.join(directory, "%s-%d" % (name, seq))
        seq += 1
    _atomic_write_bytes(target, original)
    _prune_backups(directory)
    return target


def _prune_backups(directory, keep=BACKUP_KEEP):
    """Keep only the newest `keep` backups of each basename."""
    by_name = {}
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return
    for name in entries:
        base = re.sub(r"^\d{8}-\d{6}-", "", name)
        by_name.setdefault(base, []).append(name)
    for base, names in by_name.items():
        for stale in names[:-keep] if len(names) > keep else []:
            try:
                os.remove(os.path.join(directory, stale))
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Client registry
# ---------------------------------------------------------------------------

def _claude_settings_path(home):
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return os.path.join(override, "settings.json")
    return os.path.join(home, ".claude", "settings.json")


def _apply_claude_code(paths, ctx):
    path = paths["settings"]
    data = _load_json(path, "claude settings")
    env = dict(data.get("env") or {})
    # Claude Code appends /v1 itself when it needs it; keep the bare root.
    env["ANTHROPIC_BASE_URL"] = re.sub(r"/v1/?$", "", ctx["base_url"])
    env["ANTHROPIC_AUTH_TOKEN"] = ctx["api_key"]
    if ctx.get("model"):
        for name in ("ANTHROPIC_MODEL",
                     "ANTHROPIC_DEFAULT_SONNET_MODEL",
                     "ANTHROPIC_DEFAULT_OPUS_MODEL",
                     "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
            env[name] = ctx["model"]
    data["env"] = env
    return {path: _dump_json(data)}


def _codex_home(home):
    return os.environ.get("CODEX_HOME") or os.path.join(home, ".codex")


def _apply_codex(paths, ctx):
    config_path = paths["config"]
    provider_id = ctx["provider_id"]
    text = _read_text(config_path)
    text = toml_upsert_top_key(text, "model_provider", provider_id)
    if ctx.get("model"):
        text = toml_upsert_top_key(text, "model", ctx["model"])
    text = toml_upsert_table(text, "model_providers." + provider_id, {
        "name": ctx["provider_name"],
        "base_url": ctx["base_url_v1"],
        "wire_api": "responses",
        "env_key": "OPENAI_API_KEY",
    })
    auth_path = paths["auth"]
    auth = _load_json(auth_path, "codex auth")
    auth = deep_merge(auth, {"OPENAI_API_KEY": ctx["api_key"]})
    return {config_path: text, auth_path: _dump_json(auth)}


def _apply_opencode(paths, ctx):
    path = paths["config"]
    provider_id = ctx["provider_id"]
    data = _load_json(path, "opencode config")
    if "$schema" not in data:
        data["$schema"] = "https://opencode.ai/config.json"
    models = {}
    for entry in ctx.get("models") or []:
        mid = entry.get("id")
        if mid:
            models[mid] = {}
    provider = {
        "npm": "@ai-sdk/openai-compatible",
        "name": ctx["provider_name"],
        "options": {
            "baseURL": ctx["base_url_v1"],
            "apiKey": ctx["api_key"],
        },
    }
    if models:
        provider["models"] = models
    data = deep_merge(data, {"provider": {provider_id: provider}})
    if ctx.get("model"):
        data["model"] = "%s/%s" % (provider_id, ctx["model"])
    return {path: _dump_json(data)}


def _dsh_has_patch_layer(home):
    """新版 DSH 的标志：profiles/ 档案目录存在（旧版只有 settings.yaml）。"""
    return os.path.isdir(os.path.join(home, ".dsh", "profiles"))


def _dsh_patch_path(home):
    """新版 DSH 的补丁层文件：profiles/<profile>/*.patch.yml。

    新版把旧 settings.yaml 迁移进 profiles/<名字>/（本机实测旧文件被改名成
    settings.yaml.imported）；没有补丁目录时返回 desktop 档的默认路径，首次
    应用时创建。
    """
    profiles = os.path.join(home, ".dsh", "profiles")
    try:
        if os.path.isdir(profiles):
            for name in sorted(os.listdir(profiles)):
                pdir = os.path.join(profiles, name)
                if not os.path.isdir(pdir):
                    continue
                cordis = os.path.join(pdir, "cordis.patch.yml")
                if os.path.isfile(cordis):
                    return cordis
                for f in sorted(os.listdir(pdir)):
                    if f.endswith(".patch.yml"):
                        return os.path.join(pdir, f)
    except OSError:
        pass
    return os.path.join(profiles, "desktop", "cordis.patch.yml")


def _dsh_patch_entry_bounds(lines, entry_id):
    """`- id: <entry_id>` 顶层条目的 [start, end) 行界，或 (None, None)。

    end 指向下一个顶层 `- ` 条目或 EOF；条目内部的 models 子列表 `- id:` 都
    带缩进，不会被误当成边界。
    """
    start = None
    for i, line in enumerate(lines):
        if start is None:
            if line.rstrip() == "- id: " + entry_id:
                start = i
            continue
        if line.startswith("- "):
            return start, i
    return (start, len(lines)) if start is not None else (None, None)


def _dsh_patch_entry_upsert(lines, entry_id, name, config_keys, value_json):
    """在 `- id: <entry_id>` 条目的 config.<config_keys> 下 upsert 一个键。

    条目不存在就追加（- id / name / config: 三行骨架）；条目内的插入走
    _yaml_upsert_at，条目外与条目内未触及的行逐字节保留。
    """
    start, end = _dsh_patch_entry_bounds(lines, entry_id)
    if start is None:
        lines = lines + ["- id: " + entry_id,
                         '  name: "' + name + '"',
                         "  config:"]
        start, end = len(lines) - 3, len(lines)
    return _yaml_upsert_at(lines, start, end, 2,
                           ["config"] + list(config_keys), value_json)


def _yaml_scalar(value):
    """Plain scalar，只在裸写会变义时才加引号（对齐 DSH 自己的无引号风格）。"""
    s = str(value)
    if s == "" or s != s.strip() or re.search(r"[:#]\s", s) \
            or re.match(r"[-?:,\[\]{}#&*!|>'\"%@`]", s):
        return json.dumps(s, ensure_ascii=False)
    return s


def _dsh_provider_block(indent, provider_id, provider, models):
    """wb-proxy provider 的块式 YAML，字段与形状对齐 DSH 自己序列化的样子。

    本机实测：DSH 的补丁加载器不认行内 JSON（`wb-proxy: {...}`）——一键配置
    写入 3 分钟后被 DSH 重写回原状，provider 不显示；而它自己保存的
    providers 全是块式映射。模型字段对齐 wb 条目：id/name/contextWindow/
    reasoningEfforts/maxTokens/supportsVision。
    """
    pad = " " * indent
    lines = [pad + provider_id + ":"]
    inner = pad + "  "
    for key in ("displayName", "apiKeyEnv", "api", "baseURL"):
        value = provider.get(key)
        if value:
            lines.append(inner + key + ": " + _yaml_scalar(value))
    if models:
        lines.append(inner + "models:")
        for m in models:
            lines.append(inner + "  - id: " + _yaml_scalar(m["id"]))
            if m.get("name"):
                lines.append(inner + "    name: " + _yaml_scalar(m["name"]))
            if m.get("contextWindow"):
                lines.append(inner + "    contextWindow: %d"
                             % int(m["contextWindow"]))
            if m.get("maxTokens"):
                lines.append(inner + "    maxTokens: %d"
                             % int(m["maxTokens"]))
            if m.get("reasoningEfforts"):
                lines.append(inner + "    reasoningEfforts:")
                for level in sorted(m["reasoningEfforts"]):
                    lines.append(inner + "      %s: %s"
                                 % (_yaml_scalar(level),
                                    _yaml_scalar(m["reasoningEfforts"][level])))
            if m.get("supportsVision"):
                lines.append(inner + "    supportsVision: true")
    return lines


def _dsh_entry_config_end(lines, start, end):
    """entry 里 config: 块的 [行号, 结束) —— 结束即下一个顶层 `- ` 条目。"""
    i = start
    while i < end:
        if lines[i].rstrip() == "  config:":
            j = i + 1
            while j < end:
                l = lines[j]
                if l.strip() and (len(l) - len(l.lstrip())) <= 2:
                    break
                j += 1
            return i, j
        i += 1
    return None


def _dsh_patch_providers_upsert(lines, entry_id, name, provider_id,
                                provider, models):
    """把 provider 块写进 `- id: llm-pi-ai` 的 config.providers 映射。

    providers: 不存在就插到 config 块末尾；provider 键已存在（重复应用）就
    整块替换；都不存在就在 providers 体末尾追加。条目外的行逐字节保留。
    """
    start, end = _dsh_patch_entry_bounds(lines, entry_id)
    if start is None:
        block = ["- id: " + entry_id, '  name: "' + name + '"',
                 "  config:", "    providers:"]
        block.extend(_dsh_provider_block(6, provider_id, provider, models))
        return lines + block
    cfg = _dsh_entry_config_end(lines, start, end)
    assert cfg is not None, "llm-pi-ai entry has no config block"
    cfg_line, cfg_end = cfg
    prov = None
    i = cfg_line + 1
    while i < cfg_end:
        if lines[i].rstrip() == "    providers:":
            prov = i
            break
        i += 1
    if prov is None:
        lines[cfg_end:cfg_end] = (["    providers:"]
                                  + _dsh_provider_block(6, provider_id,
                                                        provider, models))
        return lines
    body_start = prov + 1
    j = body_start
    while j < cfg_end:
        l = lines[j]
        if l.strip() and (len(l) - len(l.lstrip())) <= 6:
            break
        j += 1
    body_end = j
    k = body_start
    while k < body_end:
        if lines[k].rstrip() == " " * 6 + provider_id + ":":
            m = k + 1
            while m < body_end:
                l = lines[m]
                if l.strip() and (len(l) - len(l.lstrip())) <= 6:
                    break
                m += 1
            lines[k:m] = _dsh_provider_block(6, provider_id, provider, models)
            return lines
        k += 1
    lines[body_end:body_end] = _dsh_provider_block(6, provider_id, provider,
                                                   models)
    return lines


def _apply_dsh_patch(paths, ctx):
    """新版 DSH（补丁层）：改 profiles/<profile>/cordis.patch.yml。

    只动两个条目：llm-pi-ai（providers.<id>：baseURL / 模型清单）与
    agent-default-model（provider / model），一律**块式 YAML**——本机实测
    行内 JSON 会被 DSH 的加载器丢弃。API Key 与旧版同一条路：apiKeyEnv
    引用一个名字，真值写进 .credentials.yaml 的 refs 表。
    """
    patch_path = paths["patch"]
    provider_id = ctx["provider_id"]
    models = []
    for entry in ctx.get("models") or []:
        mid = entry.get("id")
        if not mid:
            continue
        item = {"id": mid, "name": mid,
                **({"contextWindow": int(entry["context_window"])}
                   if entry.get("context_window") else {})}
        if entry.get("max_output"):
            try:
                item["maxTokens"] = int(entry["max_output"])
            except (TypeError, ValueError):
                pass
        levels = dsh_reasoning_levels(entry.get("reasoning_efforts"))
        if levels:
            item["reasoningEfforts"] = levels
        if entry.get("supports_vision") or entry.get("supportsVision"):
            item["supportsVision"] = True
        models.append(item)
    provider = {
        "displayName": ctx["provider_name"],
        "apiKeyEnv": ctx["credential_ref"],
        "api": "openai-completions",
        "baseURL": ctx["base_url_v1"],
    }
    text = _read_text(patch_path) if os.path.isfile(patch_path) else ""
    lines = text.split("\n") if text else []
    lines = _dsh_patch_providers_upsert(lines, "llm-pi-ai",
                                        "@deepseek-ai/dsh-llm-pi-ai",
                                        provider_id, provider, models)
    if ctx.get("model"):
        lines = _dsh_patch_entry_upsert(lines, "agent-default-model",
                                        "@deepseek-ai/dsh-agent-default-model",
                                        ["provider"], provider_id)
        lines = _dsh_patch_entry_upsert(lines, "agent-default-model",
                                        "@deepseek-ai/dsh-agent-default-model",
                                        ["model"], ctx["model"])
    cred_path = paths["credentials"]
    cred_text = yaml_refs_upsert(_read_text(cred_path), ctx["credential_ref"],
                                 ctx["api_key"])
    return {patch_path: "\n".join(lines), cred_path: cred_text}


def dsh_reasoning_levels(raw):
    """{档位: 线上拼写} for DSH, from the gateway's `reasoning_efforts` list.

    DSH 的模型条目用 `reasoningEfforts` 声明可选思考档位（键=选择器里的档位，
    值=请求里实际发的拼写）。本路由不在它的内置目录里，所以不声明就没有选择
    器——`/v1/models` 里的 `reasoning_efforts` 它不读，它只读 id / name /
    contextWindow / maxTokens。网关宣告的档位名就是上游认的拼写，一一照抄即可；
    固定档位的模型（网关只给 reasoning_fixed_effort）没有列表，就不声明：给了
    选择器也不会改变上游实际跑的档位。
    """
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, (list, tuple)):
        pairs = [(lvl, lvl) for lvl in raw]
    else:
        return None
    levels = {}
    for level, wire in pairs:
        name = str(level or "").strip()
        if not name:
            continue
        levels[name] = str(wire or "").strip() or name
    return levels or None


def dsh_default_model(text, provider, model):
    """The `agent-default-model` entry to write, keeping keys we do not own.

    yaml_upsert_inline() replaces that leaf wholesale, so writing a bare
    {provider, model} silently drops whatever the user set beside it - a
    reasoningEffort, for instance, which is how a default thinking level gets
    lost on a re-apply. Read the current inline mapping and merge the route
    over it; a block-style or unparsable value falls back to the bare pair
    rather than guessing.
    """
    out = {"provider": provider, "model": model}
    for line in str(text or "").split("\n"):
        stripped = line.strip()
        if not stripped.startswith("agent-default-model:"):
            continue
        raw = stripped.split(":", 1)[1].strip()
        if raw.startswith("{"):
            try:
                current = json.loads(raw)
            except ValueError:
                current = None
            if isinstance(current, dict):
                out = dict(current)
        break
    out["provider"] = provider
    out["model"] = model
    return out


def _apply_dsh(paths, ctx):
    # 新版 DSH 用 profiles/<profile>/*.patch.yml 补丁层；旧版才写 settings.yaml。
    # paths 里带 patch 就只碰补丁 + 凭据，绝不再生成一份会被新版忽略的旧
    # settings；补丁文件本身缺失（全新档案）就当空文件新建。
    if "patch" in paths:
        return _apply_dsh_patch(paths, ctx)
    return _apply_dsh_legacy(paths, ctx)


def _apply_dsh_legacy(paths, ctx):
    settings_path = paths["settings"]
    text = _read_text(settings_path)
    models = []
    for entry in ctx.get("models") or []:
        mid = entry.get("id")
        if not mid:
            continue
        item = {"id": mid}
        if entry.get("context_window"):
            item["contextWindow"] = entry["context_window"]
        if entry.get("max_output"):
            try:
                item["maxTokens"] = int(entry["max_output"])
            except (TypeError, ValueError):
                pass
        levels = dsh_reasoning_levels(entry.get("reasoning_efforts"))
        if levels:
            item["reasoningEfforts"] = levels
        if entry.get("supports_vision") or entry.get("supportsVision"):
            item["supportsVision"] = True
        models.append(item)
    provider_id = ctx["provider_id"]
    provider = {
        "displayName": ctx["provider_name"],
        "apiKeyEnv": ctx["credential_ref"],
        "api": "openai-completions",
        "baseURL": ctx["base_url_v1"],
        # wb-proxy 不在 DSH 的内置目录里，推理字段的线上形状必须自己说明：
        # 走标准 reasoning_effort，不发 thinking 块——DeepSeek 系需要的那份
        # thinking / reasoning_content 由网关补（wb_proxy.build_upstream_body）。
        "compat": {"thinkingFormat": "openai", "supportsReasoningEffort": True},
    }
    if models:
        provider["models"] = models
    text = yaml_upsert_inline(
        text, "llm-pi-ai.providers.%s" % provider_id, provider)
    if ctx.get("model"):
        text = yaml_upsert_inline(
            text, "agent-default-model",
            dsh_default_model(text, ctx["provider_id"], ctx["model"]))
    credentials_path = paths["credentials"]
    cred_text = _read_text(credentials_path)
    cred_text = yaml_refs_upsert(cred_text, ctx["credential_ref"], ctx["api_key"])
    return {settings_path: text, credentials_path: cred_text}


def _apply_crush(paths, ctx):
    path = paths["config"]
    data = _load_json(path, "crush config")
    models = []
    for entry in ctx.get("models") or []:
        mid = entry.get("id")
        if not mid:
            continue
        item = {"id": mid, "name": mid}
        if entry.get("context_window"):
            item["context_window"] = entry["context_window"]
        if entry.get("max_output"):
            item["default_max_tokens"] = entry["max_output"]
        models.append(item)
    provider = {
        "name": ctx["provider_name"],
        "type": "openai",
        "base_url": ctx["base_url_v1"],
        "api_key": ctx["api_key"],
        "models": models,
    }
    data = deep_merge(data, {"providers": {ctx["provider_id"]: provider}})
    return {path: _dump_json(data)}


def _apply_zcode(paths, ctx):
    """Add or update a wb-proxy provider rule in ZCode's provider config.

    The rule is addressed by a stable providerId, so re-applying updates that
    one entry instead of stacking new ones, and everything else in the
    document - other providers, their order, the model rules - is merged
    around untouched. Note the file belongs to the ZCode app and is rewritten
    by it while running; configure with ZCode closed (the panel card and the
    CLI both say so) or the app's next save can drop the new entry.
    """
    path = paths["config"]
    data = _load_json(path, "zcode provider config")
    if not data:
        # No ZCode profile on this machine yet: start from the wrapper shape a
        # live 3.x profile writes (schemaVersion + config + the two rule
        # holders), so a fresh install can be configured without running the
        # app first.
        data = {"schemaVersion": 1,
                "config": {"providerOrder": [],
                           "providerConfigRules": {"providerRules": []}}}
    config = data.get("config")
    if not isinstance(config, dict):
        raise AgentConfigError(
            "zcode provider config has no config object: %s" % path)
    holder = config.get("providerConfigRules")
    if not isinstance(holder, dict):
        raise AgentConfigError(
            "zcode provider config has no providerConfigRules: %s" % path)
    rules = holder.get("providerRules")
    if not isinstance(rules, list):
        raise AgentConfigError(
            "zcode providerConfigRules has no providerRules list: %s" % path)

    model_ids = []
    for entry in ctx.get("models") or []:
        mid = entry.get("id") if isinstance(entry, dict) else entry
        mid = str(mid or "").strip()
        if mid and mid not in model_ids:
            model_ids.append(mid)

    provider_id = ctx["provider_id"]
    rule = {
        "providerId": provider_id,
        "providerName": ctx["provider_name"],
        "config": {
            "group": "standard-personal",
            "access": {"type": "api-key", "apiKey": ctx["api_key"]},
            "api": {"type": "openai-chat-completions",
                    "baseUrl": ctx["base_url_v1"]},
        },
    }
    if model_ids:
        rule["config"]["personalModelIds"] = model_ids
        rule["config"]["modelOrder"] = list(model_ids)

    replaced = False
    for index, existing in enumerate(rules):
        if isinstance(existing, dict) and existing.get("providerId") == provider_id:
            rules[index] = deep_merge(existing, rule)
            replaced = True
            break
    if not replaced:
        rules.append(rule)

    order = config.get("providerOrder")
    if not isinstance(order, list):
        order = []
        config["providerOrder"] = order
    if provider_id not in order:
        order.append(provider_id)
    return {path: _dump_json(data)}


CLIENTS = {
    "claude-code": {
        "id": "claude-code",
        "label": "Claude Code",
        "desc": "Anthropic CLI; points ANTHROPIC_BASE_URL at this gateway.",
        "protocol": "anthropic",
        "files": lambda home: {
            "settings": _claude_settings_path(home),
        },
        "probe": lambda home: (
            os.path.isdir(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude"))
            or os.path.isfile(_claude_settings_path(home))
            or bool(shutil.which("claude"))
        ),
        "apply": _apply_claude_code,
    },
    "codex": {
        "id": "codex",
        "label": "Codex CLI",
        "desc": "OpenAI Codex CLI; adds a wb-proxy model provider.",
        "protocol": "openai",
        "files": lambda home: {
            "config": os.path.join(_codex_home(home), "config.toml"),
            "auth": os.path.join(_codex_home(home), "auth.json"),
        },
        "probe": lambda home: (
            os.path.isdir(_codex_home(home))
            or os.path.isfile(os.path.join(_codex_home(home), "config.toml"))
            or bool(shutil.which("codex"))
        ),
        "apply": _apply_codex,
    },
    "opencode": {
        "id": "opencode",
        "label": "OpenCode",
        "desc": "OpenCode; registers an openai-compatible provider.",
        "protocol": "openai",
        "files": lambda home: {
            "config": os.path.join(home, ".config", "opencode", "opencode.json"),
        },
        "probe": lambda home: (
            os.path.isdir(os.path.join(home, ".config", "opencode"))
            or os.path.isfile(os.path.join(home, ".config", "opencode", "opencode.json"))
            or bool(shutil.which("opencode"))
        ),
        "apply": _apply_opencode,
    },
    "dsh": {
        "id": "dsh",
        "label": "DeepSeek Harness",
        "desc": "DSH; adds a wb-proxy provider (new patch layer or legacy settings.yaml).",
        "protocol": "openai",
        "files": lambda home: dict(
            {"settings": os.path.join(home, ".dsh", "settings.yaml"),
             "credentials": os.path.join(home, ".dsh", ".credentials.yaml")},
            **({"patch": _dsh_patch_path(home)}
               if _dsh_has_patch_layer(home) else {})),
        "probe": lambda home: (
            os.path.isdir(os.path.join(home, ".dsh"))
            or os.path.isfile(os.path.join(home, ".dsh", "settings.yaml"))
            or bool(shutil.which("dsh"))
        ),
        "apply": _apply_dsh,
    },
    "crush": {
        "id": "crush",
        "label": "Crush",
        "desc": "Charm Crush; adds a wb-proxy provider entry.",
        "protocol": "openai",
        "files": lambda home: {
            "config": os.path.join(home, ".config", "crush", "crush.json"),
        },
        "probe": lambda home: (
            os.path.isdir(os.path.join(home, ".config", "crush"))
            or os.path.isfile(os.path.join(home, ".config", "crush", "crush.json"))
            or bool(shutil.which("crush"))
        ),
        "apply": _apply_crush,
    },
    "zcode": {
        "id": "zcode",
        "label": "ZCode",
        "desc": "ZCode; adds a wb-proxy provider to its provider config. "
                "Quit ZCode first - it rewrites that file while running.",
        "protocol": "openai",
        "files": lambda home: {
            "config": os.path.join(home, ".zcode", "v2", "provider_config.json"),
        },
        "probe": lambda home: (
            os.path.isdir(os.path.join(home, ".zcode"))
            or os.path.isfile(os.path.join(home, ".zcode", "v2",
                                           "provider_config.json"))
        ),
        "apply": _apply_zcode,
    },
}


def _client(client_id):
    client = CLIENTS.get(client_id)
    if client is None:
        raise AgentConfigError(
            "unknown client %r (known: %s)"
            % (client_id, ", ".join(sorted(CLIENTS))))
    return client


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def overview(accounts_dir, home=None):
    """Per-client install/apply summary for the panel."""
    home = _resolve_home(home)
    state = _load_state(accounts_dir)
    out = {}
    for client_id in sorted(CLIENTS):
        client = CLIENTS[client_id]
        paths = client["files"](home)
        existing = [p for p in paths.values() if os.path.exists(p)]
        applied = None
        record = state.get(client_id)
        if isinstance(record, dict):
            external = False
            for entry in record.get("files") or []:
                path = entry.get("path")
                if not path or not os.path.exists(path):
                    external = True
                    continue
                # Compare against the bytes we last wrote, not the backup: a
                # hand edit after apply must read as an external change.
                try:
                    if _sha256_bytes(_read_bytes(path)) != entry.get("sha256"):
                        external = True
                except AgentConfigError:
                    external = True
            applied = {
                "at": record.get("applied_at"),
                "base_url": record.get("base_url"),
                "model": record.get("model"),
                "files": len(record.get("files") or []),
                "external_change": external,
            }
        out[client_id] = {
            "id": client_id,
            "label": client["label"],
            "desc": client["desc"],
            "protocol": client["protocol"],
            "installed": bool(client["probe"](home)) or bool(existing),
            # "configured" is about this gateway's integration record, not about
            # the client merely having a config file on disk - otherwise every
            # pre-existing config would advertise a restore that cannot run.
            "configured": isinstance(record, dict),
            "config_paths": list(paths.values()),
            "applied": applied,
        }
    return out


def plan(client_id, base_url, api_key, model=None, models=None, home=None,
         provider_label=None):
    """The exact file contents integrate() would write; touches nothing.

    Each entry in ``files`` carries the target ``path``, the new ``content``
    and whether the file already ``existed``, so a caller can preview an
    apply - the CLI's --dry-run - before anything is backed up or written.
    integrate() and the preview share this one builder, so what a dry run
    prints is what a real run writes.

    ``provider_label`` 区分同一客户端里的多个网关条目：传 "cn"/"intl" 时
    provider 的 id/名称/凭据引用都带区域后缀（wb-proxy-cn / WB Proxy cn /
    WB_PROXY_API_KEY_CN），两把 Key 各配一次即并存；None/空 维持原单条目。
    """
    client = _client(client_id)
    home = _resolve_home(home)
    base_url = str(base_url or "").strip().rstrip("/")
    if not re.match(r"^https?://", base_url):
        raise AgentConfigError("base_url must start with http:// or https://")
    base_url_v1 = base_url if base_url.endswith("/v1") else base_url + "/v1"
    suffix = ""
    if provider_label:
        label = str(provider_label).strip().lower()
        if not re.match(r"^[a-z0-9-]{1,12}$", label):
            raise AgentConfigError(
                "provider_label must be 1-12 chars of [a-z0-9-]")
        suffix = "-" + label
        provider_name = PROVIDER_NAME + " " + label
    else:
        provider_name = PROVIDER_NAME
    ctx = {
        "base_url": base_url,
        "base_url_v1": base_url_v1,
        "api_key": str(api_key or ""),
        "model": str(model or "").strip() or None,
        "models": list(models or []),
        "provider_id": PROVIDER_ID + suffix,
        "provider_name": provider_name,
        "credential_ref": CREDENTIAL_REF + suffix.upper().replace("-", "_"),
    }
    paths = client["files"](home)
    try:
        new_contents = client["apply"](paths, ctx)
    except AgentConfigError:
        raise
    except Exception as exc:
        raise AgentConfigError("failed to build %s config: %s"
                               % (client_id, exc))
    files = []
    for path in paths.values():
        content = new_contents.get(path)
        if content is None:
            continue
        files.append({
            "path": path,
            "content": content,
            "existed": os.path.exists(path),
        })
    return {
        "client": client_id,
        "label": client["label"],
        "base_url": base_url,
        "model": ctx["model"],
        "provider_id": ctx["provider_id"],
        "files": files,
    }


def integrate(accounts_dir, client_id, base_url, api_key, model=None,
              models=None, home=None, provider_label=None):
    """Point `client_id` at this gateway; returns what was written.

    The first apply backs up the original bytes of every touched file; later
    applies keep the earliest backup so restore always returns to the state
    before the very first integration. ``provider_label`` 透传给 plan()：
    同一客户端配置多把 Key（国内/国际）时用区域后缀区分 provider 条目。

    注意：state 按 client_id 记录、文件按 provider_id 寻址——两次不同
    label 的 apply 各写各的 provider 块，state 只记最后一次；还原只回到
    最初备份，这是既有语义。
    """
    built = plan(client_id, base_url, api_key, model=model, models=models,
                 home=home, provider_label=provider_label)
    state = _load_state(accounts_dir)
    record = state.get(client_id) or {}
    previous_files = {entry.get("path"): entry
                      for entry in record.get("files") or []
                      if isinstance(entry, dict)}
    # Phase 1: stage backups for all target files before making any modifications.
    staged = []
    for entry in built["files"]:
        path, content, existed = entry["path"], entry["content"], entry["existed"]
        prior = previous_files.get(path)
        if prior is not None and not prior.get("existed") and existed:
            # We created this file in an earlier apply. While it still holds
            # exactly what we wrote it stays "ours", so restore deletes it
            # instead of resurrecting our own bytes (that is the documented
            # promise, and the bytes may hold the API key); a hand edit since
            # then makes it the user's file again, and it gets backed up.
            try:
                if _sha256_bytes(_read_bytes(path)) == prior.get("sha256"):
                    existed = False
            except AgentConfigError:
                pass
        backup = None
        if existed and prior and prior.get("backup"):
            # Keep the very first backup: it holds the pre-integration bytes.
            backup = prior.get("backup")
        elif existed:
            backup = _backup_file(accounts_dir, client_id, path,
                                  _read_bytes(path))
        staged.append((path, content, existed, backup))

    # Phase 2: apply writes transactionally. If any write fails, roll back
    # all already-written files so a multi-file integration (e.g. DSH) never
    # leaves a half-configured state with missing state records.
    written = []
    entries = []
    try:
        for path, content, existed, backup in staged:
            _atomic_write_text(path, content)
            written.append((path, existed, backup))
            entries.append({
                "path": path,
                "existed": existed,
                "backup": backup,
                "sha256": _sha256_bytes(content.encode("utf-8")),
                "bytes": len(content.encode("utf-8")),
            })
    except Exception as exc:
        for w_path, w_existed, w_backup in written:
            try:
                if w_existed and w_backup:
                    # w_backup 是 _backup_file() 记下的**完整备份路径**（state
                    # 与 restore() 都按整条路径用它），直接读回即可。这里曾经
                    # 先拼了一次 backup_dir(...)，而那个名字全模块都不存在：
                    # NameError 被下面的 except 吞掉，于是多文件客户端写一半
                    # 失败时既不回滚也不写 state——面板显示「未配置」，而
                    # restore() 又因为查不到记录而拒绝执行。
                    _atomic_write_bytes(w_path, _read_bytes(w_backup))
                elif not w_existed and os.path.exists(w_path):
                    os.unlink(w_path)
            except Exception:
                pass
        raise AgentConfigError("failed to write %s files: %s" % (client_id, exc))

    state[client_id] = {
        "applied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_url": built["base_url"],
        "model": built["model"],
        "files": entries,
    }
    _save_state(accounts_dir, state)
    return {
        "client": client_id,
        "label": built["label"],
        "applied_at": state[client_id]["applied_at"],
        "base_url": built["base_url"],
        "model": built["model"],
        "files": entries,
    }


def restore(accounts_dir, client_id, home=None):
    """Undo integrate(): put the backed-up originals back byte for byte.

    Files that did not exist before the first apply are deleted; the state
    record for the client is cleared.
    """
    client = _client(client_id)
    home = _resolve_home(home)
    state = _load_state(accounts_dir)
    record = state.get(client_id)
    if not isinstance(record, dict) or not record.get("files"):
        raise AgentConfigError(
            "%s has no recorded integration to restore" % client["label"])
    restored = []
    for entry in record.get("files") or []:
        path = entry.get("path")
        if not path:
            continue
        if entry.get("existed") and entry.get("backup"):
            backup = entry["backup"]
            if not os.path.exists(backup):
                raise AgentConfigError(
                    "backup missing for %s: %s" % (path, backup))
            _atomic_write_bytes(path, _read_bytes(backup))
            restored.append({"path": path, "action": "restored"})
        else:
            # We created this file; removing it returns to the original state.
            try:
                os.remove(path)
                restored.append({"path": path, "action": "deleted"})
            except FileNotFoundError:
                restored.append({"path": path, "action": "already-absent"})
            # Applying had to create the parent directories too. Leaving an
            # empty one behind makes the probe report the client as installed
            # when the only config it ever had was ours, so prune the chain we
            # emptied - never touching a directory that still holds anything.
            _prune_empty_parents(path, home)
    state.pop(client_id, None)
    _save_state(accounts_dir, state)
    return {
        "client": client_id,
        "label": client["label"],
        "restored": restored,
    }


# ---------------------------------------------------------------------------
# Standalone configurator
# ---------------------------------------------------------------------------
#
# The panel's one-click setup runs inside the gateway process, so it can only
# ever edit the machine the gateway runs on - which is also why /agents/apply
# refuses non-loopback callers and container deployments. The CLI inverts the
# direction: it runs where the clients are and reaches the gateway only over
# HTTP (the model list), so a gateway on a server, NAS or router can still
# serve clients on every other machine.


def fetch_models_from_gateway(base_url, api_key, timeout=10):
    """The gateway's model list, for clients that embed model definitions.

    Reads the OpenAI-compatible ``GET /v1/models``, which is reachable with
    the API key alone (it is not a panel route). Doubles as key validation:
    a 401 here means the key is wrong, anything else means the gateway or its
    upstream is unreachable.
    """
    base = str(base_url or "").strip().rstrip("/")
    if not re.match(r"^https?://", base):
        raise AgentConfigError("base_url must start with http:// or https://")
    if base.endswith("/v1"):
        base = base[:-3]
    request = urllib.request.Request(
        base + "/v1/models",
        headers={"Authorization": "Bearer " + str(api_key or "")})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise AgentConfigError(
                "the gateway rejected the API key (HTTP 401)")
        raise AgentConfigError("gateway /v1/models failed: HTTP %s" % exc.code)
    except ValueError as exc:
        raise AgentConfigError(
            "gateway /v1/models returned invalid JSON: %s" % exc)
    except Exception as exc:
        raise AgentConfigError(
            "cannot reach the gateway /v1/models: %s" % exc)
    models = []
    seen = set()
    for item in (payload.get("data") or []):
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        entry = {"id": mid}
        context = item.get("context_length") or item.get("max_input_tokens")
        if context:
            try:
                entry["context_window"] = int(context)
            except (TypeError, ValueError):
                pass
        out_tokens = item.get("max_output_tokens")
        if out_tokens:
            try:
                entry["max_output"] = int(out_tokens)
            except (TypeError, ValueError):
                pass
        efforts = item.get("reasoning_efforts")
        if isinstance(efforts, (list, tuple)):
            levels = [str(e).strip() for e in efforts if str(e or "").strip()]
            if levels:
                entry["reasoning_efforts"] = levels
        name = item.get("name")
        if name:
            entry["name"] = str(name)
        models.append(entry)
    # Same cap as the panel's apply: a runaway catalog must not turn into a
    # megabyte config file.
    return models[:80]


def _default_state_dir():
    """Where the standalone CLI keeps its state and backups.

    Deliberately NOT the gateway's accounts/ directory: this runs on a
    different machine, and every machine keeps its own record and backups.
    """
    return os.path.join(os.path.expanduser("~"), ".wb-agent-config")


def _mask_key(key):
    key = str(key or "")
    if not key:
        return "(none)"
    if len(key) <= 4:
        return "***"
    return "***" + key[-4:]


def _cli_error(message):
    sys.stderr.write("error: %s\n" % message)
    return 1


def main(argv=None):
    # --home/--state-dir/--json are accepted before AND after the subcommand:
    # the shared parent carries them into every subparser with SUPPRESS
    # defaults, so a subparser never clobbers a value given at the top level.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", default=argparse.SUPPRESS,
                        help="home directory to configure "
                             "(default: the current user's home)")
    common.add_argument("--state-dir", default=argparse.SUPPRESS,
                        dest="state_dir",
                        help="where the integration state and backups live "
                             "(default: ~/.wb-agent-config)")
    common.add_argument("--json", action="store_true", dest="as_json",
                        default=argparse.SUPPRESS,
                        help="print machine-readable JSON")

    parser = argparse.ArgumentParser(
        prog="wb_agents.py", parents=[common],
        description="One-click agent integration for local AI clients. Runs "
                    "where the clients are, so the gateway may live on "
                    "another machine.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", parents=[common],
                   help="supported clients and what is installed on this machine")
    sub.add_parser("status", parents=[common],
                   help="this machine's integration records")

    apply_p = sub.add_parser("apply", parents=[common],
                             help="point a client at the gateway")
    apply_p.add_argument("--client", required=True,
                         help="one of: " + ", ".join(sorted(CLIENTS)))
    apply_p.add_argument("--base-url", required=True, dest="base_url",
                         help="gateway URL, e.g. http://gateway:8788/v1")
    apply_p.add_argument("--key", default=None,
                         help="gateway API key "
                              "(default: $WB_PROXY_API_KEY, then a prompt)")
    apply_p.add_argument("--model", default=None,
                         help="default model id to select in the client")
    apply_p.add_argument("--models-from-gateway", action="store_true",
                         dest="models_from_gateway",
                         help="fetch the model list from /v1/models "
                              "(this is the default)")
    apply_p.add_argument("--no-models", action="store_true", dest="no_models",
                         help="skip the model list; also skips key validation")
    apply_p.add_argument("--dry-run", action="store_true", dest="dry_run",
                         help="print what would be written; touch nothing")

    restore_p = sub.add_parser("restore", parents=[common],
                               help="undo an apply, byte for byte")
    restore_p.add_argument("--client", required=True,
                           help="one of: " + ", ".join(sorted(CLIENTS)))

    args = parser.parse_args(argv)

    # A Chinese path or model name in the output must not abort the run on a
    # legacy Windows console - the same guard tests/run_all.py applies to
    # itself.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    as_json = getattr(args, "as_json", False)
    state_dir = getattr(args, "state_dir", None) or _default_state_dir()
    home = _resolve_home(getattr(args, "home", None))

    if args.command in ("list", "status"):
        data = overview(state_dir, home)
        if as_json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        for client_id in sorted(data):
            entry = data[client_id]
            print("%-12s %-18s %s" % (
                client_id, entry["label"],
                "installed" if entry["installed"] else "not detected"))
            for path in entry["config_paths"]:
                print("    config:  %s" % path)
            if args.command == "status":
                applied = entry.get("applied")
                if applied:
                    print("    applied: %s  base_url=%s  model=%s%s" % (
                        applied["at"], applied["base_url"],
                        applied["model"] or "-",
                        "  (external change detected)"
                        if applied["external_change"] else ""))
                else:
                    print("    applied: never")
        return 0

    if args.command == "apply":
        if args.no_models and args.models_from_gateway:
            return _cli_error(
                "--no-models and --models-from-gateway are mutually exclusive")
        client_id = str(args.client).strip()
        try:
            _client(client_id)
        except AgentConfigError as exc:
            return _cli_error(str(exc))
        base_url = str(args.base_url).strip()
        if not re.match(r"^https?://", base_url):
            return _cli_error("--base-url must start with http:// or https://")
        key = str(args.key or os.environ.get("WB_PROXY_API_KEY") or "")
        # Prompt only when there is a real console. On Windows isatty() is
        # also true for the NUL device (a common stdin for spawned or
        # service-run processes), and getpass there reads CONIN$ directly -
        # it would block forever in a process that has no console. stdout
        # being a terminal is the signal that one exists.
        if not key and sys.stdin.isatty() and sys.stdout.isatty():
            try:
                key = getpass.getpass("Gateway API key: ").strip()
            except (EOFError, KeyboardInterrupt):
                key = ""
        if not key:
            return _cli_error(
                "no API key: pass --key, set WB_PROXY_API_KEY, or run "
                "interactively")

        models = []
        if not args.no_models:
            try:
                models = fetch_models_from_gateway(base_url, key)
            except AgentConfigError as exc:
                return _cli_error(
                    "%s - retry, or pass --no-models to configure without "
                    "the model list and key validation" % exc)

        if args.dry_run:
            try:
                built = plan(client_id, base_url, key, model=args.model,
                             models=models, home=home)
            except AgentConfigError as exc:
                return _cli_error(str(exc))
            if as_json:
                print(json.dumps({
                    "client": built["client"],
                    "label": built["label"],
                    "base_url": built["base_url"],
                    "model": built["model"],
                    "files": [{"path": item["path"],
                               "existed": item["existed"],
                               "content": item["content"]}
                              for item in built["files"]],
                }, ensure_ascii=False, indent=2))
                return 0
            print("dry run: nothing will be written")
            print("client:  %s (%s)" % (built["label"], built["client"]))
            print("gateway: %s" % built["base_url"])
            print("key:     %s" % _mask_key(key))
            for item in built["files"]:
                print()
                print("--- %s %s" % (
                    "modify" if item["existed"] else "create", item["path"]))
                print(item["content"], end="")
                if not item["content"].endswith("\n"):
                    print()
            return 0

        try:
            result = integrate(state_dir, client_id, base_url, key,
                               model=args.model, models=models, home=home)
        except AgentConfigError as exc:
            return _cli_error(str(exc))
        if as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print("configured %s (%s) -> %s" % (
            result["label"], result["client"], result["base_url"]))
        for entry in result["files"]:
            print("  %-8s %s" % (
                "updated" if entry["existed"] else "created", entry["path"]))
        if models:
            print("models:  %d fetched from the gateway (key accepted)"
                  % len(models))
        print("key:     %s" % _mask_key(key))
        print("state:   %s" % state_dir)
        print("restore: python %s restore --client %s"
              % (os.path.basename(__file__), client_id))
        return 0

    if args.command == "restore":
        client_id = str(args.client).strip()
        try:
            result = restore(state_dir, client_id, home=home)
        except AgentConfigError as exc:
            return _cli_error(str(exc))
        if as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print("restored %s (%s)" % (result["label"], result["client"]))
        for entry in result["restored"]:
            print("  %-9s %s" % (entry["action"], entry["path"]))
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())

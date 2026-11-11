"""Infer the root agent's OS from runtime evidence, never topic keywords."""

import json
import re

OS_VALUES = ("windows", "linux", "mac", "unknown")
NEW_FIELDS = (
    "runtime_os",
    "runtime_os_method",
    "runtime_os_evidence",
    "runtime_os_reason",
)
FENCES = re.compile(r"(?ms)^\s*(```|~~~)[^\n]*\n.*?^\s*\1[^\n]*(?:\n|$)")
BLOCKS = re.compile(
    r"(?ms)(?:^\s*|(?<=</INSTRUCTIONS>))<(environment_context|environment_details|env)>\s*\n?(.*?)</\1>"
)
OS_LINE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?(Platform|OS Version|Operating System|Host OS|OS|os_name)(?:\*\*)?\s*:\s*([^\n<]+)"
)
CWD_LINE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?(?:Working directory|Current working directory|Current Working Directory|cwd)(?:\*\*)?\s*:\s*([^\n<]+)"
)
REMOTE = re.compile(
    r"(?i)(?:\b(?:ssh|scp|sftp|curl|wget|chroot)\b|\b(?:docker|podman)\s+(?:exec|run)\b|\bkubectl\s+exec\b|\bwsl(?:\.exe)?(?:\s|$)|Invoke-WebRequest)"
)
EXEC_NAMES = {
    "exec_command",
    "shell_command",
    "shell",
    "bash",
    "powershell",
    "execute_command",
    "run_terminal_cmd",
}


def os_name(value):
    if not isinstance(value, str):
        return None
    value = value.strip().strip("`*\"'").lower()
    if re.match(r"(?:wsl\b|windows subsystem for linux)", value):
        return "linux"
    if re.match(
        r"(?:linux\b|ubuntu\b|debian\b|centos\b|fedora\b|alpine\b|red hat\b|rhel\b|rocky linux\b)",
        value,
    ):
        return "linux"
    if re.match(
        r"(?:windows\b|win32\b|win64\b|windows_nt\b|mingw\b|msys\b|cygwin\b)", value
    ):
        return "windows"
    if re.match(r"(?:darwin\b|macos\b|mac os\b|os x\b|osx\b|mac\b)", value):
        return "mac"
    if re.match(
        r"(?:freebsd\b|openbsd\b|netbsd\b|android\b|ios\b|sunos\b|aix\b)", value
    ):
        return "unsupported"
    return None


def path_os(value):
    if not isinstance(value, str):
        return None
    value = value.strip().strip("`*\"'")
    if re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\"):
        return "windows"
    if re.match(
        r"^/(?:Users|System/Library|Applications|Volumes|opt/homebrew|private/tmp)(?:/|$)",
        value,
    ) or re.match(r"^/(?:private/)?var/folders/", value):
        return "mac"
    if re.match(r"^/(?:home|root|proc|sys|run/user)(?:/|$)", value) or re.match(
        r"^/mnt/[a-z](?:/|$)", value
    ):
        return "linux"
    return None


def text_parts(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for block in value:
            if isinstance(block, dict) and block.get("type") in (
                "text",
                "input_text",
                "output_text",
            ):
                if isinstance(block.get("text"), str):
                    yield block["text"]


def evidence(target, category, source, value):
    if category:
        item = {"os": category, "source": source, "value": str(value)[:180]}
        if item not in target:
            target.append(item)


def decision(items, method):
    values = {item["os"] for item in items}
    if len(values) > 1:
        # Keep at least one example for each conflicting category.
        examples = [
            next(x for x in items if x["os"] == value) for value in sorted(values)
        ]
        return {
            "runtime_os": "unknown",
            "runtime_os_method": "unknown",
            "runtime_os_evidence": examples,
            "runtime_os_reason": "conflicting_" + method,
        }
    value = next(iter(values))
    if value == "unsupported":
        return {
            "runtime_os": "unknown",
            "runtime_os_method": "unknown",
            "runtime_os_evidence": items[:3],
            "runtime_os_reason": "unsupported_os",
        }
    return {
        "runtime_os": value,
        "runtime_os_method": method,
        "runtime_os_evidence": items[:3],
    }


def environment_text(text, location, explicit, paths):
    for match in OS_LINE.finditer(text):
        evidence(explicit, os_name(match[2]), location + "/" + match[1], match[2])
    for match in re.finditer(
        r"(?is)<(?:platform|os|operating_system)>(.*?)</(?:platform|os|operating_system)>",
        text,
    ):
        evidence(explicit, os_name(match[1]), location + "/os", match[1])
    cwd_values = [m[1] for m in re.finditer(r"(?is)<cwd>(.*?)</cwd>", text)]
    cwd_values.extend(m[1] for m in CWD_LINE.finditer(text))
    # Some harnesses put the working directory in a Markdown heading.
    cwd_values.extend(
        m[1]
        for m in re.finditer(
            r"(?im)^#{1,4}\s+Current Working Directory\s*\(([^\n]+)\)", text
        )
    )
    local_paths = []
    for value in cwd_values:
        evidence(local_paths, path_os(value), location + "/cwd", value)
    # Prefer inference when deployment cwd and a POSIX shell agree. The shell
    # alone, or a generic /tmp directory, does not establish a platform.
    if not local_paths:
        shells = [m[1] for m in re.finditer(r"(?is)<shell>(.*?)</shell>", text)]
        shells.extend(
            m[1] for m in re.finditer(r"(?im)^\s*(?:[-*]\s*)?Shell:\s*([^\n<]+)", text)
        )
        posix = next(
            (
                s
                for s in shells
                if re.search(r"(?:^|/)(?:ba|da|z|fi)?sh(?:\s|$)", s.strip())
            ),
            None,
        )
        if posix:
            for value in cwd_values:
                if re.match(
                    r"^/(?:data|mnt|gpfs|workspace|workspaces|app|srv|scratch|share|work|nvme|output|environment|run/output)(?:/|$)",
                    value.strip(),
                ):
                    evidence(
                        local_paths,
                        "linux",
                        location + "/cwd+shell",
                        value + " ; shell=" + posix,
                    )
    if not local_paths:
        for match in re.finditer(
            r"(?is)<workspace_roots>(.*?)</workspace_roots>", text
        ):
            for root in re.finditer(r"(?is)<root>(.*?)</root>", match[1]):
                evidence(
                    local_paths, path_os(root[1]), location + "/workspace_root", root[1]
                )
    paths.extend(x for x in local_paths if x not in paths)


def tool_calls(messages):
    for i, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls", []) or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function", call)
            yield (
                i,
                call.get("id"),
                function.get("name", ""),
                function.get("arguments", {}),
            )
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    yield (
                        i,
                        block.get("id"),
                        block.get("name", ""),
                        block.get("input", {}),
                    )


def local_tool_evidence(messages):
    found = []
    probes = {}
    for i, call_id, name, arguments in tool_calls(messages):
        short_name = name.rsplit(".", 1)[-1].lower()
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (ValueError, TypeError):
                arguments = {"code": arguments}
        if not isinstance(arguments, dict):
            continue
        location = f"messages/{i}/tool_call/{name}"
        if short_name in ("exec", "parallel"):
            # Only actual wrapper calls, and only the workdir/cwd of an executed
            # local shell tool. Do not scan arbitrary strings in a JS program.
            code = arguments.get("code", "")
            if isinstance(code, str):
                for match in re.finditer(
                    r"\btools\.(?:exec_command|shell_command)\s*\(\s*\{([^{}]{0,20000})\}",
                    code,
                ):
                    for path in re.finditer(
                        r"""\b(?:workdir|cwd)\s*:\s*(["'])(.*?)\1""", match[1]
                    ):
                        value = path[2].replace("\\\\", "\\")
                        evidence(
                            found, path_os(value), location + "/wrapped_workdir", value
                        )
            continue
        if short_name not in EXEC_NAMES:
            continue
        for key in ("workdir", "cwd", "working_directory"):
            if isinstance(arguments.get(key), str):
                evidence(
                    found, path_os(arguments[key]), location + "/" + key, arguments[key]
                )
        command = arguments.get("cmd", arguments.get("command", ""))
        if isinstance(command, list):
            command = " ".join(str(x) for x in command)
        if not isinstance(command, str) or REMOTE.search(command):
            continue
        for match in re.finditer(
            r"""(?:^|[;&\n])\s*(?:cd|Set-Location)\s+(?:--\s+)?(?:"([^"]+)"|'([^']+)'|([^\s;&|]+))""",
            command,
            re.I,
        ):
            value = next(x for x in match.groups() if x is not None)
            evidence(found, path_os(value), location + "/cd", value)
        # Exact, simple host queries only; remote calls and arbitrary program
        # output are not an OS signal. Python print() queries are intentionally
        # omitted because the output may describe a target environment.
        if call_id and re.fullmatch(
            r"\s*(?:uname(?:\s+-[asr]+)?|sw_vers|ver|pwd|\$PSVersionTable(?:\.OS)?|\[(?:System\.)?Environment\]::OSVersion(?:\.VersionString)?)\s*;?\s*",
            command,
            re.I,
        ):
            probes[call_id] = (location, command.strip())
    if probes:
        for i, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            results = []
            if message.get("role") == "tool":
                results.append((message.get("tool_call_id"), message.get("content")))
            elif message.get("role") == "user" and isinstance(
                message.get("content"), list
            ):
                for block in message["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        results.append((block.get("tool_use_id"), block.get("content")))
            for call_id, content in results:
                if call_id not in probes:
                    continue
                location, command = probes[call_id]
                for text in text_parts(content):
                    for line in text.splitlines():
                        value = line.strip()
                        category = (
                            path_os(value)
                            if command.lower().strip(" ;") == "pwd"
                            else os_name(value)
                        )
                        if command.lower() == "sw_vers" and re.match(
                            r"ProductName:\s*(?:macOS|Mac OS X)", value, re.I
                        ):
                            category = "mac"
                        if command.lower() == "ver" and value.lower().startswith(
                            "microsoft windows"
                        ):
                            category = "windows"
                        evidence(
                            found, category, f"messages/{i}/local_system_query", value
                        )
    return found


def classify(record):
    explicit, paths = [], []
    containers = [("metadata", record.get("metadata"))]
    for parent_name, parent in [("", record), ("metadata", record.get("metadata", {}))]:
        if isinstance(parent, dict):
            for key in ("environment", "environment_context", "runtime", "host"):
                if isinstance(parent.get(key), dict):
                    containers.append((parent_name + "/" + key, parent[key]))
    for location, data in containers:
        if not isinstance(data, dict):
            continue
        for key in ("os", "platform", "os_name", "os_version", "operating_system"):
            evidence(
                explicit, os_name(data.get(key)), location + "/" + key, data.get(key)
            )
        for key in ("cwd", "working_directory"):
            evidence(paths, path_os(data.get(key)), location + "/" + key, data.get(key))
    messages = record.get("messages", [])
    if not isinstance(messages, list):
        messages = []
    inputs = [
        ("instructions", "system", text)
        for text in text_parts(record.get("instructions"))
    ]
    for i, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") in (
            "system",
            "developer",
            "user",
        ):
            inputs.extend(
                (f"messages/{i}", message["role"], text)
                for text in text_parts(message.get("content"))
            )
    for location, role, text in inputs:
        # Native Codex memory jobs provide local rollout context before the
        # quoted transcript. Read only that header, never the embedded rollout.
        if (
            role == "user"
            and record.get("_basetraj", {}).get("harness") == "codex"
            and text.lstrip().startswith("Analyze this rollout and produce JSON with")
        ):
            header = re.search(
                r"(?m)^rollout_context:[ \t]*\r?\n((?:[ \t]*-[^\n]*(?:\n|$))+)", text
            )
            if header:
                context = dict(
                    re.findall(
                        r"(?m)^\s*-\s*(rollout_path|rollout_cwd):\s*([^\r\n]+)",
                        header[1],
                    )
                )
                for key in ("rollout_path", "rollout_cwd"):
                    value = context.get(key, "")
                    category = path_os(value)
                    if category:
                        evidence(
                            paths,
                            category,
                            location + "/native_rollout_context/" + key,
                            value,
                        )
                        break
        if role == "user" and not any(
            tag in text for tag in ("<environment_context>", "<environment_details>")
        ):
            continue
        text = FENCES.sub("\n", text)
        text = re.sub(r"(?m)^\s*>.*$", "", text)
        if role in ("system", "developer"):
            environment_text(text, location, explicit, paths)
            for match in re.finditer(
                r"(?i)\b(?:you are|agent is)\b[^.\n]{0,80}\brunning (?:in|on) (?:an? )?(Linux|Windows|macOS|Darwin)\b",
                text,
            ):
                evidence(
                    explicit,
                    os_name(match[1]),
                    location + "/runtime_statement",
                    match[0],
                )
        else:
            for match in BLOCKS.finditer(text):
                if match[1] != "env":
                    environment_text(
                        match[2], location + "/" + match[1], explicit, paths
                    )
    if explicit:
        return decision(explicit, "explicit")
    if paths:
        return decision(paths, "environment_path")
    local = local_tool_evidence(messages)
    if local:
        return decision(local, "local_tool")
    return {
        "runtime_os": "unknown",
        "runtime_os_method": "unknown",
        "runtime_os_evidence": [],
        "runtime_os_reason": "no_runtime_evidence",
    }

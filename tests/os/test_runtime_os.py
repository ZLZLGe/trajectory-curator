import unittest

from trajectory_os import classify, refine, session_key


def trajectory(text="", role="user", **kwargs):
    return {"messages": [{"role": role, "content": text}], **kwargs}


class RuntimeOSTests(unittest.TestCase):
    def check(self, record, category, method=None):
        result = classify(record)
        self.assertEqual(result["runtime_os"], category, result)
        if method:
            self.assertEqual(result["runtime_os_method"], method, result)

    def test_explicit_environments(self):
        for value, expected in [
            ("win32", "windows"),
            ("Windows 11 Pro", "windows"),
            ("Linux 6.8-microsoft-WSL2", "linux"),
            ("darwin", "mac"),
            ("macOS", "mac"),
        ]:
            with self.subTest(value=value):
                self.check(
                    trajectory("# Environment\n - Platform: " + value, "system"),
                    expected,
                    "explicit",
                )

    def test_windows_bash(self):
        self.check(
            trajectory("<env>\nPlatform: win32\nShell: bash\n</env>", "system"),
            "windows",
        )

    def test_paths(self):
        for path, expected in [
            (r"C:\Users\alice\repo", "windows"),
            ("D:/repo", "windows"),
            ("/Users/alice/repo", "mac"),
            ("/home/alice/repo", "linux"),
            ("/root/work", "linux"),
            ("/mnt/c/Users/a", "linux"),
            ("/workspace", "linux"),
            ("/tmp/project", "unknown"),
        ]:
            with self.subTest(path=path):
                self.check(
                    trajectory(
                        f"<environment_context>\n<cwd>{path}</cwd><shell>bash</shell>\n</environment_context>"
                    ),
                    expected,
                )

    def test_workspace_roots(self):
        self.check(
            trajectory(
                "<environment_context>\n<filesystem><workspace_roots><root>/Users/a/x</root></workspace_roots></filesystem></environment_context>"
            ),
            "mac",
        )

    def test_combined_and_adjacent_environment(self):
        self.check(
            trajectory(
                "</INSTRUCTIONS><environment_context>\n<cwd>/data/project</cwd><shell>bash</shell></environment_context>"
            ),
            "linux",
            "environment_path",
        )
        self.check(
            trajectory(
                "<environment_context>\n<cwd>/workspace</cwd></environment_context>"
            ),
            "unknown",
        )

    def test_multiline_and_temporary_runtime_paths(self):
        self.check(
            trajectory("<cwd>\n/Users/alice/project\n</cwd>", "system"),
            "mac",
            "environment_path",
        )
        self.check(
            trajectory(
                "<environment_context>\n<cwd>/share/project</cwd><shell>bash</shell></environment_context>"
            ),
            "linux",
        )
        self.check(trajectory("Working directory: /private/tmp/work", "system"), "mac")

    def test_native_rollout_context_excludes_quoted_transcript(self):
        prefix = "Analyze this rollout and produce JSON with `raw_memory`, `rollout_summary`, and `rollout_slug`.\n\nrollout_context:\n- rollout_path: /Users/alice/.codex/sessions/log.jsonl\n- rollout_cwd: /Users/alice/project\n\n"
        quoted = '[{"text":"<environment_context>\\n<cwd>C:\\\\Users\\\\target</cwd>\\n</environment_context>"}]'
        self.check(
            trajectory(prefix + quoted, _basetraj={"harness": "codex"}),
            "mac",
            "environment_path",
        )
        self.check(
            trajectory("Analyze this JSON:\n" + quoted, _basetraj={"harness": "codex"}),
            "unknown",
        )

    def test_explicit_beats_path(self):
        self.check(
            trajectory("Platform: linux\nWorking directory: /Users/mounted", "system"),
            "linux",
            "explicit",
        )

    def test_remote_does_not_override_host(self):
        r = trajectory("Platform: darwin", "system")
        r["messages"] += [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "a",
                        "function": {
                            "name": "Bash",
                            "arguments": {"command": "ssh server uname -a"},
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "a", "content": "Linux server 6.0"},
        ]
        self.check(r, "mac")
        r["messages"] = r["messages"][1:]
        self.check(r, "unknown")

    def test_conversation_and_examples_not_evidence(self):
        for text in [
            "Write a Windows app on Linux; my friend uses Mac.",
            "Platform: windows",
            "```xml\n<environment_context>\n<cwd>C:\\Users\\a</cwd>\n</environment_context>\n```",
            "> <environment_context>\n> <cwd>/Users/a</cwd>\n> </environment_context>",
        ]:
            self.check(trajectory(text), "unknown")

    def test_conflicts(self):
        r = trajectory("Platform: windows\nOS Version: Linux 6.0", "system")
        self.check(r, "unknown")
        self.assertEqual(classify(r)["runtime_os_reason"], "conflicting_explicit")
        self.check(
            trajectory(
                "<environment_context>\n<cwd>/Users/a</cwd></environment_context>\n<environment_context>\n<cwd>C:\\Users\\a</cwd></environment_context>"
            ),
            "unknown",
        )

    def test_no_subagent_inheritance(self):
        self.check(
            trajectory(
                "hello",
                agent_messages=[{"role": "system", "content": "Platform: linux"}],
            ),
            "unknown",
        )

    def test_local_queries(self):
        for cmd, output, expected in [
            ("uname -a", "Linux host 6.0", "linux"),
            ("uname -s", "Darwin", "mac"),
            ("ver", "Microsoft Windows [Version 10.0]", "windows"),
            ("pwd", "/Users/a", "mac"),
            ("docker exec x uname -s", "Linux", "unknown"),
            ("echo Linux", "Linux", "unknown"),
        ]:
            r = {
                "messages": [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "x",
                                "function": {
                                    "name": "exec_command",
                                    "arguments": {"cmd": cmd},
                                },
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "x", "content": output},
                ]
            }
            with self.subTest(cmd=cmd):
                self.check(r, expected)

    def test_tool_workdir(self):
        r = {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "a",
                            "function": {
                                "name": "exec_command",
                                "arguments": {"cmd": "ls", "workdir": "/home/user"},
                            },
                        }
                    ],
                }
            ]
        }
        self.check(r, "linux", "local_tool")

    def test_wrapped_workdir(self):
        r = {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "a",
                            "function": {
                                "name": "functions.exec",
                                "arguments": {
                                    "code": "text(await tools.exec_command({cmd: 'ls', workdir: '/Users/a'}));"
                                },
                            },
                        }
                    ],
                }
            ]
        }
        self.check(r, "mac", "local_tool")

    def test_unsupported_and_shell_only(self):
        self.check(
            trajectory("Platform: FreeBSD\nWorking directory: /home/a", "system"),
            "unknown",
        )
        self.check(
            trajectory(
                "<environment_context>\n<shell>powershell</shell>\n</environment_context>"
            ),
            "unknown",
        )

    def test_metadata(self):
        self.check(
            trajectory("hello", metadata={"platform": "darwin"}), "mac", "explicit"
        )

    def test_session_inference_is_scoped_and_non_overriding(self):
        uuid = "12345678-1234-5678-1234-123456789abc"
        source = (
            f"lakehouse/free-router/classified/v001/dt=2026-07-01/{uuid}_sub_2.jsonl"
        )
        r = trajectory(
            "hello",
            metadata={"session_id": uuid},
            _basetraj={"source_key": source, "harness": "codex"},
        )
        key = session_key(source, "codex")
        mapping = {
            key: {
                "runtime_os": "mac",
                "method": "explicit",
                "source_key": source.replace("_sub_2", "_sub_0"),
                "supporting_records": 3,
            }
        }
        answer = refine(r, classify(r), mapping)
        self.assertEqual(
            (answer["runtime_os"], answer["runtime_os_method"]),
            ("mac", "session_context"),
        )
        r["metadata"]["session_id"] = "not-the-same"
        self.assertEqual(refine(r, classify(r), mapping)["runtime_os"], "unknown")
        r["metadata"]["session_id"] = uuid
        r["_basetraj"]["harness"] = "claude-code"
        self.assertEqual(refine(r, classify(r), mapping)["runtime_os"], "unknown")
        r["_basetraj"]["harness"] = "codex"
        r["_basetraj"]["source_key"] = source.replace("2026-07-01", "2026-07-02")
        self.assertEqual(refine(r, classify(r), mapping)["runtime_os"], "unknown")
        r["_basetraj"]["source_key"] = source
        conflict = {
            "runtime_os": "unknown",
            "runtime_os_method": "unknown",
            "runtime_os_reason": "conflicting_explicit",
            "runtime_os_evidence": [],
        }
        self.assertEqual(refine(r, conflict, mapping), conflict)
        self.assertIsNone(session_key(source.replace(uuid, "probe-2"), "codex"))


if __name__ == "__main__":
    unittest.main()

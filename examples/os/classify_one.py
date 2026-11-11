"""Classify synthetic trajectories and fill missing same-session evidence."""

import json

from trajectory_os import build_session_map, classify, refine


def main():
    session_id = "12345678-1234-5678-1234-123456789abc"
    records = [
        {
            "metadata": {"session_id": session_id},
            "_basetraj": {
                "harness": "codex",
                "source_key": f"classified/dt=2026-07-01/{session_id}_sub_{index}.jsonl",
            },
            "messages": [{"role": "system", "content": text}],
        }
        for index, text in enumerate(("Platform: darwin", "Continue the task."))
    ]
    session_map = build_session_map(records)
    for record in records:
        annotation = refine(record, classify(record), session_map)
        print(json.dumps(annotation, ensure_ascii=False))


if __name__ == "__main__":
    main()

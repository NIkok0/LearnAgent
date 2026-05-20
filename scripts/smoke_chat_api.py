from __future__ import annotations

import argparse
import json
import uuid

import requests


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8090")
    parser.add_argument("--thread-id", default="")
    parser.add_argument("--message", default="hello agent")
    parser.add_argument("--reasoning-content", default="")
    args = parser.parse_args()

    thread_id = args.thread_id or f"smoke-{uuid.uuid4().hex[:8]}"
    messages = []
    if args.reasoning_content:
        messages.append(
            {
                "role": "assistant",
                "content": "Previous assistant visible content.",
                "reasoning_content": args.reasoning_content,
            }
        )
    messages.append({"role": "user", "content": args.message})
    response = requests.post(
        f"{args.base_url}/v1/chat",
        json={"thread_id": thread_id, "messages": messages},
        stream=True,
        timeout=120,
    )
    print("status", response.status_code)
    done = False
    assistant_state = False
    error_payload = ""
    token_chars = 0
    last_data = []
    for raw in response.iter_lines(decode_unicode=True):
        if raw is None:
            continue
        line = raw.strip()
        if line.startswith("event: assistant_state"):
            assistant_state = True
        elif line.startswith("event: done"):
            done = True
        elif line.startswith("event: error"):
            error_payload = "error"
        elif line.startswith("data:"):
            data = line.split(":", 1)[1].strip()
            last_data.append(data[:240])
            last_data = last_data[-4:]
            try:
                payload = json.loads(data)
            except Exception:
                payload = {}
            if isinstance(payload, dict) and isinstance(payload.get("text"), str):
                token_chars += len(payload["text"])
            if error_payload == "error":
                error_payload = data
    print(
        {
            "thread_id": thread_id,
            "done": done,
            "assistant_state": assistant_state,
            "token_chars": token_chars,
            "error": error_payload,
            "last_data": last_data,
        }
    )
    if response.status_code != 200 or error_payload or not done:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

import json
import time
import urllib.error
import urllib.request


URL = "http://127.0.0.1:8080/v1/chat/completions"

# Approximate prompt sizes. The API response reports the actual token count.
LINE_COUNTS = [500, 1_000, 2_000, 3_000]


def build_prompt(line_count: int) -> str:
    repository = "\n".join(
        f"def helper_{i}(value: int) -> int: return value + {i}"
        for i in range(line_count)
    )

    return f"""
You are reviewing a synthetic Python repository.

Repository:
{repository}

Return only this exact text:
CONTEXT_TEST_OK
"""


def run_test(line_count: int) -> None:
    payload = {
        "model": "local-coder",
        "messages": [
            {
                "role": "user",
                "content": build_prompt(line_count),
            }
        ],
        "temperature": 0,
        "max_tokens": 16,
    }

    request = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    started = time.perf_counter()

    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"{line_count:>5} lines | HTTP {exc.code} | {body}")
        return
    except Exception as exc:
        print(f"{line_count:>5} lines | ERROR | {exc}")
        return

    elapsed = time.perf_counter() - started
    usage = result.get("usage", {})
    answer = result["choices"][0]["message"]["content"].strip()

    print(
        f"{line_count:>5} lines | "
        f"prompt={usage.get('prompt_tokens')} | "
        f"completion={usage.get('completion_tokens')} | "
        f"time={elapsed:.2f}s | "
        f"answer={answer!r}"
    )


for count in LINE_COUNTS:
    run_test(count)